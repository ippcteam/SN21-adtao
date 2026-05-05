"""Layer 9.B end-to-end miner submission orchestration.

Pipeline (per epoch, per miner):

  1. build_prediction_plaintext       — gather predictions, attach inner_sig.
  2. encrypt_prediction               — AES-GCM with fresh K, AAD = epoch.
  3. POST AES_ct to all archive tiers — Tier-2 mandatory, others best effort.
  4. Submit on-chain commits          — three separate extrinsics:
       a. TimelockEncrypted(K) for auto-decrypt at deadline + safety margin.
       b. Sha256(AES_ct).
       c. Raw{N}(self_archive_url).

Step 4 currently runs as three extrinsics (proven path). The single-extrinsic
multi-field variant in `submit_layer_9b_multi_field` is staged but unverified
on testnet (see Q36 in the architecture doc).

A miner that fails step 3 Tier-2 should NOT proceed to step 4 — without a
durable archive, validators cannot fetch AES_ct after K reveals, and the
miner will be excluded from scoring with a `plaintext_unavailable` reason.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.archives import (
    ArchiveClient,
    ArchiveEndpoint,
    UploadResult,
)
from hope.commitment.episode_artifacts import (
    PerEpisodeEntry,
    build_per_episode_bundle,
    compute_episodes_imt_root,
    build_per_episode_entry,
)
from hope.commitment.on_chain import (
    CommitResult,
    submit_miner_prediction_layer_9b,
    submit_raw_url_commit_layer_9b,
    submit_sha256_commit,
)
from hope.commitment.prediction_payload import (
    EncryptedPrediction,
    build_prediction_plaintext,
    encrypt_prediction,
)

logger = logging.getLogger(__name__)


@dataclass
class MinerSubmissionResult:
    """Outcome of one miner's full Layer 9.B submission for one epoch.

    `ok` is True iff a Tier-2 upload succeeded AND all three on-chain extrinsics
    landed. The breakdown lets the miner runner log per-step outcomes for
    operations and the retry log.
    """

    ok: bool
    encrypted: Optional[EncryptedPrediction]
    archive_uploads: list[UploadResult] = field(default_factory=list)
    chain_k_commit: Optional[CommitResult] = None
    chain_sha_commit: Optional[CommitResult] = None
    chain_url_commit: Optional[CommitResult] = None
    bundle_uploads: list[UploadResult] = field(default_factory=list)
    episodes_root: Optional[bytes] = None
    failure_reason: Optional[str] = None


def submit_miner_epoch(
    *,
    subtensor,
    miner_wallet,
    netuid: int,
    epoch_id: str,
    miner_hotkey: bytes,
    miner_signing_key: Ed25519PrivateKey,
    submitted_round: int,
    horizons: list[dict[str, Any]],
    self_archive_url: str,
    archive_endpoints: list[ArchiveEndpoint],
    blocks_until_reveal: int,
    archive_client: Optional[ArchiveClient] = None,
    require_tier_2: bool = True,
    upload_auth_headers: Optional[dict[str, str]] = None,
    miner_identity_for_archive: Optional[str] = None,
    per_episode_entries: Optional[list[PerEpisodeEntry]] = None,
) -> MinerSubmissionResult:
    """Run the full Layer 9.B pipeline for one miner / one epoch.

    Args:
        subtensor: Bittensor `Subtensor` for chain extrinsics.
        miner_wallet: Bittensor `Wallet` whose hotkey signs commits.
        netuid: subnet ID.
        epoch_id: the operator release_key, [A-Z0-9-]{1,80}.
        miner_hotkey: 32-byte raw ed25519 public key (must match wallet hotkey
            and `miner_signing_key`).
        miner_signing_key: ed25519 private key for inner_sig.
        submitted_round: drand round at submission, embedded in plaintext.
        horizons: list of horizon dicts (from build_horizon_entry).
        self_archive_url: Tier-3 URL announced on chain (≤128 bytes utf-8).
        archive_endpoints: list of (tier, base_url, name); ordering does NOT
            matter for upload — every tier is attempted.
        blocks_until_reveal: number of subtensor blocks until K auto-decrypts.
        archive_client: optional pre-built client; one is created if None.
        require_tier_2: if True (default), abort before chain commits if no
            tier-2 endpoint accepts the upload.
        upload_auth_headers: forwarded to archive POSTs (e.g., signed nonce).
        miner_identity_for_archive: archive path component identifying the
            miner; defaults to the SS58 derived from miner_wallet.hotkey.

    Returns:
        MinerSubmissionResult with per-step outcomes.
    """
    # Phase E: when per-episode entries are supplied, build the off-chain
    # bundle, compute its IMT root, and bind both the root and the bundle
    # bytes' SHA-256 inside the aggregated plaintext. The bundle is uploaded
    # alongside the AES_ct on the same content-addressed archive path so
    # verifiers can fetch by SHA after K reveals.
    episodes_root: Optional[bytes] = None
    bundle_bytes: Optional[bytes] = None
    bundle_sha256: Optional[bytes] = None
    if per_episode_entries:
        encoded_entries = [build_per_episode_entry(e) for e in per_episode_entries]
        episodes_root = compute_episodes_imt_root(encoded_entries)
        bundle_bytes = build_per_episode_bundle(
            epoch_id=epoch_id,
            miner_hotkey=miner_hotkey,
            submitted_round=submitted_round,
            entries=per_episode_entries,
            miner_signing_key=miner_signing_key,
        )
        bundle_sha256 = hashlib.sha256(bundle_bytes).digest()

    plaintext = build_prediction_plaintext(
        epoch_id=epoch_id,
        miner_hotkey=miner_hotkey,
        submitted_round=submitted_round,
        horizons=horizons,
        miner_signing_key=miner_signing_key,
        episodes_root=episodes_root,
        episodes_bundle_sha256=bundle_sha256,
    )
    encrypted = encrypt_prediction(plaintext, epoch_id=epoch_id)
    sha256_ct = hashlib.sha256(encrypted.aes_ct).digest()

    if archive_client is None:
        archive_client = ArchiveClient()
    if miner_identity_for_archive is None:
        miner_identity_for_archive = _derive_ss58(miner_wallet)

    upload_results = archive_client.upload_to_all(
        archive_endpoints,
        epoch_id=epoch_id,
        miner_identity=miner_identity_for_archive,
        aes_ct=encrypted.aes_ct,
        auth_headers=upload_auth_headers,
    )

    bundle_uploads: list[UploadResult] = []
    if bundle_bytes is not None:
        bundle_uploads = archive_client.upload_to_all(
            archive_endpoints,
            epoch_id=epoch_id,
            miner_identity=miner_identity_for_archive,
            aes_ct=bundle_bytes,
            auth_headers=upload_auth_headers,
        )

    tier_2_ok = any(r.endpoint.tier == 2 and r.ok for r in upload_results)
    if require_tier_2 and not tier_2_ok:
        return MinerSubmissionResult(
            ok=False,
            encrypted=encrypted,
            archive_uploads=upload_results,
            failure_reason="tier_2_archive_upload_failed",
        )

    k_commit = submit_miner_prediction_layer_9b(
        subtensor=subtensor,
        miner_wallet=miner_wallet,
        netuid=netuid,
        aes_key=encrypted.aes_key,
        blocks_until_reveal=blocks_until_reveal,
    )
    if not k_commit.success:
        return MinerSubmissionResult(
            ok=False,
            encrypted=encrypted,
            archive_uploads=upload_results,
            chain_k_commit=k_commit,
            failure_reason=f"chain_k_commit_failed: {k_commit.message}",
        )

    sha_commit = submit_sha256_commit(
        subtensor=subtensor,
        wallet=miner_wallet,
        netuid=netuid,
        hash_bytes=sha256_ct,
    )
    if not sha_commit.success:
        return MinerSubmissionResult(
            ok=False,
            encrypted=encrypted,
            archive_uploads=upload_results,
            chain_k_commit=k_commit,
            chain_sha_commit=sha_commit,
            failure_reason=f"chain_sha_commit_failed: {sha_commit.message}",
        )

    url_commit = submit_raw_url_commit_layer_9b(
        subtensor=subtensor,
        miner_wallet=miner_wallet,
        netuid=netuid,
        self_archive_url=self_archive_url,
    )
    if not url_commit.success:
        return MinerSubmissionResult(
            ok=False,
            encrypted=encrypted,
            archive_uploads=upload_results,
            chain_k_commit=k_commit,
            chain_sha_commit=sha_commit,
            chain_url_commit=url_commit,
            failure_reason=f"chain_url_commit_failed: {url_commit.message}",
        )

    logger.info(
        "miner 9.B submission ok epoch=%s tier_uploads=%s "
        "k_block=%s sha_block=%s url_block=%s reveal_round=%s",
        epoch_id,
        [(r.endpoint.tier, r.ok) for r in upload_results],
        k_commit.block_number, sha_commit.block_number, url_commit.block_number,
        k_commit.reveal_round,
    )
    return MinerSubmissionResult(
        ok=True,
        encrypted=encrypted,
        archive_uploads=upload_results,
        chain_k_commit=k_commit,
        chain_sha_commit=sha_commit,
        chain_url_commit=url_commit,
        bundle_uploads=bundle_uploads,
        episodes_root=episodes_root,
    )


def _derive_ss58(wallet) -> str:
    """Best-effort SS58 extraction for archive path identity."""
    hotkey = getattr(wallet, "hotkey", None)
    if hotkey is None:
        raise ValueError("wallet has no hotkey")
    ss58 = getattr(hotkey, "ss58_address", None)
    if not ss58:
        raise ValueError("wallet.hotkey has no ss58_address")
    return ss58
