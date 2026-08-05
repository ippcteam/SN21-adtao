"""Layer 9.B end-to-end miner submission orchestration.

Pipeline (per epoch, per miner):

  1. build_prediction_plaintext       — gather predictions, attach inner_sig.
  2. encrypt_prediction               — AES-GCM with fresh K, AAD = epoch.
  3. POST AES_ct to all archive tiers — Tier-2 mandatory, others best effort.
  4. Submit ONE on-chain commit       — TimelockEncrypted bundle that carries
       {K, sha256(AES_ct), self_archive_url}. The chain auto-decrypts the
       full bundle at the drand reveal_round; validators read all three
       fields atomically from `RevealedCommitments`.

Why a single bundled commit (not three separate extrinsics):
  Substrate's `set_commitment` is single-slot, last-write-wins on
  `CommitmentOf`. A three-extrinsic flow (TLE'd K → Sha256 → Raw URL) gets
  the TLE'd K commit overwritten by the subsequent non-TLE commits before
  its drand reveal_round fires. By the time auto-decrypt runs, the slot is
  no longer TimelockEncrypted, so the K is permanently lost from the chain
  reveal path. Bundling all three fields into ONE TLE'd commit removes that
  failure mode entirely.

A miner that fails step 3 Tier-2 should NOT proceed to step 4 — without a
durable archive, validators cannot fetch AES_ct after the bundle reveals,
and the miner will be excluded from scoring with `plaintext_unavailable`.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.archives import (
    ArchiveClient,
    ArchiveEndpoint,
    UploadResult,
)
from hope.commitment.episode_artifacts import (
    PerEpisodeEntry,
    build_per_episode_bundle,
    build_per_episode_entry,
    compute_episodes_imt_root,
)
from hope.commitment.on_chain import (
    CommitResult,
    submit_miner_prediction_layer_9b,
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

    `ok` is True iff a Tier-2 upload succeeded AND the bundled chain commit
    landed. The breakdown lets the miner runner log per-step outcomes for
    operations and the retry log.

    `chain_bundle_commit` is the TimelockEncrypted commit carrying
    {K, sha256(AES_ct), self_archive_url}. The legacy fields
    `chain_sha_commit` / `chain_url_commit` remain on the dataclass as
    deprecated `None`-valued slots so older log readers don't crash; new
    code should use `chain_bundle_commit` only.
    """

    ok: bool
    encrypted: EncryptedPrediction | None
    archive_uploads: list[UploadResult] = field(default_factory=list)
    chain_bundle_commit: CommitResult | None = None
    bundle_uploads: list[UploadResult] = field(default_factory=list)
    episodes_root: bytes | None = None
    failure_reason: str | None = None
    # Deprecated — retained for backward-compat log readers.
    chain_k_commit: CommitResult | None = None
    chain_sha_commit: CommitResult | None = None
    chain_url_commit: CommitResult | None = None


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
    archive_client: ArchiveClient | None = None,
    require_tier_2: bool = True,
    upload_auth_headers: dict[str, str] | None = None,
    miner_identity_for_archive: str | None = None,
    per_episode_entries: list[PerEpisodeEntry] | None = None,
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
    episodes_root: bytes | None = None
    bundle_bytes: bytes | None = None
    bundle_sha256: bytes | None = None
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

    bundle_commit = submit_miner_prediction_layer_9b(
        subtensor=subtensor,
        miner_wallet=miner_wallet,
        netuid=netuid,
        aes_key=encrypted.aes_key,
        sha256_ct=sha256_ct,
        self_archive_url=self_archive_url,
        blocks_until_reveal=blocks_until_reveal,
    )
    if not bundle_commit.success:
        return MinerSubmissionResult(
            ok=False,
            encrypted=encrypted,
            archive_uploads=upload_results,
            chain_bundle_commit=bundle_commit,
            failure_reason=f"chain_bundle_commit_failed: {bundle_commit.message}",
        )

    logger.info(
        "miner 9.B submission ok epoch=%s tier_uploads=%s "
        "bundle_block=%s reveal_round=%s",
        epoch_id,
        [(r.endpoint.tier, r.ok) for r in upload_results],
        bundle_commit.block_number, bundle_commit.reveal_round,
    )
    return MinerSubmissionResult(
        ok=True,
        encrypted=encrypted,
        archive_uploads=upload_results,
        chain_bundle_commit=bundle_commit,
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
