"""On-chain commit-reveal client for SN21 verifiable scoring (Layers 9.A, 9.B, 9.C).

This module wraps the Bittensor SDK's commitments primitives and exposes
high-level functions per the architecture v0.7 spec. It uses both:

- `subtensor.set_commitment(wallet, netuid, data: str)` — for plain Raw{N}
  commits where N≤128 bytes (e.g., hex-encoded Sha256 hashes).
- `publish_metadata_extrinsic(..., data_type="Sha256", data=<32 bytes>)` — for
  binary 32-byte hash commits (Sha256 variant).
- `publish_metadata_extrinsic(..., data_type="TimelockEncrypted", data={...})`
  paired with `bittensor_drand.encrypt(...)` — for binary CBOR plaintext
  encrypted to a future drand round.
- `subtensor.get_revealed_commitment_by_hotkey(...)` — for verifier reads.

Q35 resolution (2026-05-03): we use the LOWER-LEVEL `publish_metadata_extrinsic`
for `Sha256` and `TimelockEncrypted` variants, NOT the higher-level
`set_commitment(data: str)` / `set_reveal_commitment(data: str)` SDK helpers.
The string-based helpers add hex/utf-8 wrapping that wastes plaintext capacity.

Empirical measurement (2026-05-03, testnet netuid 466):
- TLE overhead: 254 bytes (354B plaintext → 610B ciphertext).
- Max ciphertext per TimelockEncrypted field: 1024 bytes (from chain types.rs).
- Max plaintext per timelock commit: ~380 bytes (raw bytes get hex-encoded for `get_encrypted_commitment`, doubling on-the-wire byte count).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Optional

import bittensor_drand

from hope.commitment.prediction_payload import build_miner_onchain_bundle


# Importing ``bittensor.core.extrinsics.serving`` eagerly triggers
# bittensor's CLI logging machine, which scans ``sys.argv`` for ``--help``
# and hijacks argparse output for any script that pulls in
# ``hope.commitment.*`` at module level (e.g. ``verify_epoch.py --help``).
# We expose ``publish_metadata_extrinsic`` via a module-level ``__getattr__``
# so it imports on first access, not at module import. This is PEP 562; it
# also keeps ``mock.patch("hope.commitment.on_chain.publish_metadata_extrinsic")``
# working for tests, since attribute access still resolves through __getattr__.
def __getattr__(name):
    if name == "publish_metadata_extrinsic":
        from bittensor.core.extrinsics.serving import (
            publish_metadata_extrinsic as _pme,
        )
        globals()["publish_metadata_extrinsic"] = _pme
        return _pme
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def _publish_metadata_extrinsic():
    """Resolve the lazy attribute. Checks globals first so test mocks (placed
    via mock.patch) take precedence over the live bittensor import."""
    if "publish_metadata_extrinsic" in globals():
        return globals()["publish_metadata_extrinsic"]
    return __getattr__("publish_metadata_extrinsic")

# Raw{N} field variant capacities (chain-side `Data` enum range).
RAW_FIELD_MAX_BYTES = 128


# Empirical TLE overhead measured on testnet 2026-05-04.
# Empirical testing confirmed the chain ONLY auto-decrypts TLE'd commits produced
# by `bittensor_drand.get_encrypted_commitment(data: str, ...)` — NOT
# `bittensor_drand.encrypt(data: bytes, ...)`. Both the SDK helper
# `subtensor.set_reveal_commitment(data, blocks_until_reveal)` and our
# `submit_timelock_commit` below use the former.
#
# `get_encrypted_commitment` takes a STRING. To carry binary plaintext
# (CBOR), we hex-encode: 1 byte → 2 hex chars. The chain stores the
# auto-decrypted plaintext as `<scale_compact_length><utf8_bytes>` —
# the reader strips the prefix + hex-decodes back to bytes.
#
# Effective budget:
#   chain ciphertext cap (TimelockEncrypted) ≈ 1024 bytes
#   TLE wrapper overhead                     ≈ 240 bytes
#   ⇒ usable hex-encoded payload             ≈ 784 chars
#   ⇒ raw binary plaintext                   ≈ 392 bytes
#
# Plus a SCALE compact length prefix (1-2 bytes) added by the chain.
# Conservative cap: 380 bytes raw plaintext.
_TLE_OVERHEAD_BYTES = 240
MAX_TLE_PLAINTEXT_BYTES = 380  # raw bytes; hex-encoded becomes 760 chars

# Default safety margin for timelock reveal: how many drand rounds beyond the
# nominal target round to wait. Mitigates pulse-publication latency.
DEFAULT_REVEAL_SAFETY_ROUNDS = 20  # 60 seconds at quicknet 3s/round


@dataclass(frozen=True)
class CommitResult:
    """Result of submitting an on-chain commit."""

    success: bool
    message: str
    block_number: Optional[int]
    extrinsic_hash: Optional[str]
    reveal_round: Optional[int]  # only set for timelock commits


@dataclass(frozen=True)
class RevealedCommit:
    """A single revealed timelock commitment retrieved from chain."""

    block_number: int     # block at which the reveal landed
    plaintext: bytes      # decrypted plaintext bytes


# ============================================================================
# Low-level primitives
# ============================================================================


def submit_sha256_commit(
    subtensor,
    wallet,
    netuid: int,
    hash_bytes: bytes,
    *,
    wait_for_finalization: bool = True,
    raise_error: bool = False,
) -> CommitResult:
    """Submit a 32-byte SHA-256 hash as a `Data::Sha256` commitment.

    Used by Layers 9.A.1 (release_commit_digest), 9.A.2 (reveal_blob_hash),
    and 9.C.6 (retry_log_blob_hash). Cost: 32 bytes against MaxSpace.

    Args:
        subtensor: Bittensor `Subtensor` instance.
        wallet: Bittensor `Wallet` (hotkey signs the extrinsic).
        netuid: Subnet ID (21 for SN21 mainnet; 466 for current testnet).
        hash_bytes: Exactly 32 bytes of SHA-256 digest.
        wait_for_finalization: True for ~36s GRANDPA finality wait.
        raise_error: True to raise on failure; False returns CommitResult.success=False.

    Returns:
        CommitResult with success flag, message, and extrinsic block.
    """
    if len(hash_bytes) != 32:
        raise ValueError(f"hash_bytes must be 32 bytes, got {len(hash_bytes)}")

    response = _publish_metadata_extrinsic()(
        subtensor=subtensor,
        wallet=wallet,
        netuid=netuid,
        data_type="Sha256",
        data=hash_bytes,
        wait_for_inclusion=True,
        wait_for_finalization=wait_for_finalization,
        raise_error=raise_error,
    )

    return _resolve_block_via_chain(
        _to_commit_result(response, reveal_round=None),
        subtensor=subtensor, netuid=netuid, wallet=wallet,
    )


def submit_timelock_commit(
    subtensor,
    wallet,
    netuid: int,
    plaintext: bytes,
    blocks_until_reveal: int,
    *,
    block_time_secs: float = 12.0,
    wait_for_finalization: bool = True,
    raise_error: bool = False,
) -> CommitResult:
    """Submit binary plaintext encrypted to a future drand round.

    Used by Layers 9.B (miner predictions: AES key K only), 9.C.1 (pre-scoring
    state), and 9.C.2 (post-scoring artifacts).

    The chain auto-decrypts ONLY commits produced by
    `bittensor_drand.get_encrypted_commitment(data: str, ...)` — a different
    C function from `bittensor_drand.encrypt(data: bytes, ...)`. We hex-encode
    binary plaintext to a string and call the right helper.

    The chain stores the auto-decrypted plaintext as
    `<SCALE_compact_length_prefix><utf8_bytes>`. Readers strip the prefix
    and hex-decode the payload back to bytes (see chain_reader).

    Args:
        plaintext: Binary CBOR-encoded payload. Must be ≤ MAX_TLE_PLAINTEXT_BYTES
            (380 bytes; hex-encoded form fits in chain's ~1024B TLE cap).
        blocks_until_reveal: Number of chain blocks from now until the chain
            auto-decrypts. Translated internally to a drand round number.
        block_time_secs: Average block time (default 12s for subtensor).

    Returns:
        CommitResult with reveal_round populated.

    Raises:
        ValueError: if plaintext exceeds MAX_TLE_PLAINTEXT_BYTES.
    """
    if len(plaintext) > MAX_TLE_PLAINTEXT_BYTES:
        raise ValueError(
            f"plaintext too large: {len(plaintext)} bytes > "
            f"MAX_TLE_PLAINTEXT_BYTES={MAX_TLE_PLAINTEXT_BYTES}. "
            f"Trim the payload or split across multiple commits."
        )

    # Hex-encode bytes → string for `get_encrypted_commitment`.
    plaintext_str = plaintext.hex()
    encrypted, reveal_round = bittensor_drand.get_encrypted_commitment(
        plaintext_str, blocks_until_reveal, block_time_secs
    )

    response = _publish_metadata_extrinsic()(
        subtensor=subtensor,
        wallet=wallet,
        netuid=netuid,
        data_type="TimelockEncrypted",
        data={"encrypted": encrypted, "reveal_round": reveal_round},
        wait_for_inclusion=True,
        wait_for_finalization=wait_for_finalization,
        raise_error=raise_error,
    )

    return _resolve_block_via_chain(
        _to_commit_result(response, reveal_round=reveal_round),
        subtensor=subtensor, netuid=netuid, wallet=wallet,
    )


def read_revealed_commitments(
    subtensor,
    netuid: int,
    hotkey_ss58: str,
    block: Optional[int] = None,
) -> list[RevealedCommit]:
    """Read all auto-decrypted timelock commitments for a hotkey.

    Used by validator (to read miner predictions after R) and by verifier
    (to read everything for fraud detection).

    The chain retains the last 10 reveals per (netuid, account) per
    `RevealedCommitments` storage map. Older reveals are pruned and must be
    archived off-chain by operator shadow.

    Args:
        subtensor: Bittensor `Subtensor` instance.
        netuid: Subnet ID.
        hotkey_ss58: SS58 address of the hotkey to read.
        block: Optional historical block height (requires archive RPC node).

    Returns:
        List of RevealedCommit, ordered oldest-first. Empty list if no reveals.
    """
    raw = subtensor.get_revealed_commitment_by_hotkey(
        netuid=netuid, hotkey_ss58=hotkey_ss58, block=block
    )
    if raw is None:
        return []

    # SDK returns tuple of tuples: ((block_number, hex_string), ...)
    out: list[RevealedCommit] = []
    for entry in raw:
        if len(entry) != 2:
            continue
        block_num, payload = entry
        if isinstance(payload, str):
            # Hex-encoded; strip 0x prefix if present
            payload_bytes = bytes.fromhex(payload[2:] if payload.startswith("0x") else payload)
        elif isinstance(payload, (bytes, bytearray)):
            payload_bytes = bytes(payload)
        else:
            continue
        out.append(RevealedCommit(block_number=block_num, plaintext=payload_bytes))
    return out


def get_commitment(
    subtensor,
    netuid: int,
    uid: int,
    block: Optional[int] = None,
) -> Optional[str]:
    """Read the current (non-timelock) commitment for a UID.

    Used by verifier to read 9.A.1 release_commit_digest, 9.A.2 reveal_hash,
    9.C.6 retry_log_hash. Returns the raw stored string (typically hex-encoded).

    Returns None if no commitment exists at (netuid, uid).
    """
    return subtensor.get_commitment(netuid=netuid, uid=uid, block=block)


# ============================================================================
# Layer-specific high-level helpers
# ============================================================================


def submit_release_commit_layer_9a1(
    subtensor,
    hope_outcome_signer_wallet,
    netuid: int,
    release_commit_digest: bytes,
) -> CommitResult:
    """Layer 9.A.1 — the operator commits release digest at T=0.

    `release_commit_digest` is the BLAKE2b-256 hash of the canonical CBOR
    encoding of the release_commit map (per protocol spec v1 §1.1).
    """
    if len(release_commit_digest) != 32:
        raise ValueError(
            f"release_commit_digest must be 32 bytes (BLAKE2b-256), "
            f"got {len(release_commit_digest)}"
        )
    return submit_sha256_commit(
        subtensor=subtensor,
        wallet=hope_outcome_signer_wallet,
        netuid=netuid,
        hash_bytes=release_commit_digest,
    )


def submit_outcome_reveal_hash_layer_9a2(
    subtensor,
    hope_outcome_signer_wallet,
    netuid: int,
    reveal_blob_sha256: bytes,
) -> CommitResult:
    """Layer 9.A.2 — the operator commits reveal blob hash post-deadline.

    `reveal_blob_sha256` is SHA-256 of the off-chain JSON reveal blob containing
    salts, canonical queries, and measured outcomes (per protocol spec v1 §1.2).
    Only after this commit is finalized may the operator serve the blob via HTTPS
    (commit-then-serve gate, CL-9 in v0.7).
    """
    if len(reveal_blob_sha256) != 32:
        raise ValueError(
            f"reveal_blob_sha256 must be 32 bytes, got {len(reveal_blob_sha256)}"
        )
    return submit_sha256_commit(
        subtensor=subtensor,
        wallet=hope_outcome_signer_wallet,
        netuid=netuid,
        hash_bytes=reveal_blob_sha256,
    )


def submit_miner_prediction_layer_9b(
    subtensor,
    miner_wallet,
    netuid: int,
    aes_key: bytes,
    sha256_ct: bytes,
    self_archive_url: str,
    blocks_until_reveal: int,
) -> CommitResult:
    """Layer 9.B — miner timelock-commits the {K, sha256(ct), url} bundle.

    A miner publishes a single TimelockEncrypted commit per epoch whose
    plaintext is the canonical-CBOR bundle of:
      - AES-GCM key ``K`` (decrypts the off-chain prediction blob)
      - ``sha256(AES_ct)`` (binds the off-chain ciphertext)
      - ``self_archive_url`` (Tier-3 fetch location)

    The chain auto-decrypts the bundle at the drand round derived from
    ``blocks_until_reveal``. Validators read the revealed plaintext from
    `RevealedCommitments`, fetch AES_ct from ``url``, SHA-cross-check,
    decrypt with ``K``, and verify inner_sig against the on-chain
    hotkey↔ed25519 binding.

    Why a single bundled commit (and not three separate ones):
      Substrate's `set_commitment` is single-slot, last-write-wins on
      `CommitmentOf`. A separate-extrinsics flow (TLE'd K → Sha256 → Raw URL)
      causes the TLE'd K to be overwritten before its reveal_round fires,
      so the chain has nothing to auto-decrypt. Bundling all three fields
      into one TLE'd commit removes the overwriting hazard entirely.
    """
    plaintext = build_miner_onchain_bundle(
        aes_key=aes_key,
        sha256_ct=sha256_ct,
        self_archive_url=self_archive_url,
    )
    return submit_timelock_commit(
        subtensor=subtensor,
        wallet=miner_wallet,
        netuid=netuid,
        plaintext=plaintext,
        blocks_until_reveal=blocks_until_reveal,
    )


def submit_raw_url_commit_layer_9b(
    subtensor,
    miner_wallet,
    netuid: int,
    self_archive_url: str,
    *,
    wait_for_finalization: bool = True,
    raise_error: bool = False,
) -> CommitResult:
    """Layer 9.B — miner publishes the Tier-3 self-archive URL.

    Encoded as `Raw{N}` where N = len(url_utf8). The chain caps Raw to 128
    bytes, so URLs longer than that must be split (or hosted via a shorter
    redirect). UTF-8 encoded; ASCII-only URLs encode 1 char → 1 byte.

    Args:
        self_archive_url: e.g. ``https://miner.example/archive/{epoch}``.

    Raises:
        ValueError: URL exceeds 128 bytes UTF-8.
    """
    url_bytes = self_archive_url.encode("utf-8")
    n = len(url_bytes)
    if n == 0:
        raise ValueError("self_archive_url must be non-empty")
    if n > RAW_FIELD_MAX_BYTES:
        raise ValueError(
            f"self_archive_url too long: {n} bytes > {RAW_FIELD_MAX_BYTES}"
        )

    response = _publish_metadata_extrinsic()(
        subtensor=subtensor,
        wallet=miner_wallet,
        netuid=netuid,
        data_type=f"Raw{n}",
        data=url_bytes,
        wait_for_inclusion=True,
        wait_for_finalization=wait_for_finalization,
        raise_error=raise_error,
    )
    return _resolve_block_via_chain(
        _to_commit_result(response, reveal_round=None),
        subtensor=subtensor, netuid=netuid, wallet=miner_wallet,
    )


def submit_layer_9b_multi_field(
    subtensor,
    miner_wallet,
    netuid: int,
    *,
    aes_key: bytes,
    sha256_ct: bytes,
    self_archive_url: str,
    blocks_until_reveal: int,
    block_time_secs: float = 12.0,
    wait_for_finalization: bool = True,
    raise_error: bool = False,
) -> CommitResult:
    """DO NOT USE — multi-field commit confirmed broken on testnet (Q36).

    Empirical result (testnet 466, 2026-05-03, hotkey UID 0):
      - Extrinsic ACCEPTED on chain (extrinsic_hash returned).
      - But: `get_revealed_commitment_by_hotkey` returns nothing for the
        TLE'd K side, even after `reveal_round` (target + 10 min slack).
      - And: `get_commitment` returns nothing for the Sha256 / Raw{N} sides.

    Conclusion: the chain accepts a multi-variant `info.fields[0]` list, but
    neither the auto-decrypt path nor the SDK readback supports nested
    Data variants. Production code MUST use the 3-extrinsic path:
      `submit_miner_prediction_layer_9b` + `submit_sha256_commit` +
      `submit_raw_url_commit_layer_9b`.

    This helper is retained for future testing if the chain runtime adds
    multi-variant support. Calling it raises by default; pass
    `force_run=True` (not exposed here — patch in your fork) only for
    diagnostic re-runs.
    """
    raise NotImplementedError(
        "Q36 (testnet 2026-05-03): multi-field commits are accepted by chain "
        "but neither auto-decrypted nor SDK-readable. Use the 3-extrinsic "
        "path (submit_miner_prediction_layer_9b + submit_sha256_commit + "
        "submit_raw_url_commit_layer_9b) instead."
    )
    # Unreachable, but preserved so the original wiring is documented.
    # noinspection PyUnreachableCode
    if len(aes_key) != 32:
        raise ValueError(f"aes_key must be 32 bytes, got {len(aes_key)}")
    if len(sha256_ct) != 32:
        raise ValueError(f"sha256_ct must be 32 bytes, got {len(sha256_ct)}")
    url_bytes = self_archive_url.encode("utf-8")
    n_url = len(url_bytes)
    if not (1 <= n_url <= RAW_FIELD_MAX_BYTES):
        raise ValueError(
            f"self_archive_url length {n_url} out of [1, {RAW_FIELD_MAX_BYTES}]"
        )

    from bittensor.core.extrinsics.pallets.commitments import Commitments

    encrypted, reveal_round = bittensor_drand.encrypt(
        aes_key, blocks_until_reveal, block_time_secs
    )

    fields = [
        {"TimelockEncrypted": {"encrypted": encrypted, "reveal_round": reveal_round}},
        {"Sha256": sha256_ct},
        {f"Raw{n_url}": url_bytes},
    ]

    info = {"fields": [fields]}
    call = Commitments(subtensor).set_commitment(netuid=netuid, info=info)
    response = subtensor.sign_and_send_extrinsic(
        call=call,
        wallet=miner_wallet,
        sign_with="hotkey",
        wait_for_inclusion=True,
        wait_for_finalization=wait_for_finalization,
        raise_error=raise_error,
    )
    return _to_commit_result(response, reveal_round=reveal_round)


def submit_pre_scoring_state_layer_9c1(
    subtensor,
    validator_wallet,
    netuid: int,
    pre_scoring_state_cbor: bytes,
    blocks_until_reveal: int,
) -> CommitResult:
    """Layer 9.C.1 — validator commits pre-scoring state (TLE'd).

    `pre_scoring_state_cbor` is the canonical CBOR encoding of the
    pre_scoring_state map (per protocol spec v1 §3.1) WITH inner_sig already
    computed by `inner_sig.add_inner_sig`.

    Caller must ensure plaintext is within MAX_TLE_PLAINTEXT_BYTES (380, set by hex-encoding overhead).
    """
    return submit_timelock_commit(
        subtensor=subtensor,
        wallet=validator_wallet,
        netuid=netuid,
        plaintext=pre_scoring_state_cbor,
        blocks_until_reveal=blocks_until_reveal,
    )


def submit_post_scoring_artifacts_layer_9c2(
    subtensor,
    validator_wallet,
    netuid: int,
    post_scoring_artifacts_cbor: bytes,
    blocks_until_reveal: int,
) -> CommitResult:
    """Layer 9.C.2 — validator commits post-scoring artifacts (TLE'd).

    Same shape as 9.C.1 but contains scoring outputs (scoring_hash,
    final_score_root, weights_commit_block_hash) per protocol spec v1 §3.2.
    """
    return submit_timelock_commit(
        subtensor=subtensor,
        wallet=validator_wallet,
        netuid=netuid,
        plaintext=post_scoring_artifacts_cbor,
        blocks_until_reveal=blocks_until_reveal,
    )


def submit_retry_log_attestation_layer_9c6(
    subtensor,
    validator_wallet,
    netuid: int,
    retry_log_blob_sha256: bytes,
) -> CommitResult:
    """Layer 9.C.6 — validator commits retry log blob hash.

    Used when miners are excluded for `plaintext_unavailable`. The off-chain
    retry log blob (served via validator HTTPS) records the attempts made to
    retrieve AES_ct from each archive tier. Per protocol spec v1 §3.6.
    """
    if len(retry_log_blob_sha256) != 32:
        raise ValueError(
            f"retry_log_blob_sha256 must be 32 bytes, got {len(retry_log_blob_sha256)}"
        )
    return submit_sha256_commit(
        subtensor=subtensor,
        wallet=validator_wallet,
        netuid=netuid,
        hash_bytes=retry_log_blob_sha256,
    )


# ============================================================================
# Internal helpers
# ============================================================================


def _resolve_block_via_chain(result: "CommitResult", *, subtensor, netuid, wallet) -> "CommitResult":
    """If the SDK didn't surface a block_number, recover it from CommitmentOf.

    ``set_commitment`` updates a single (netuid, hotkey) slot whose stored
    value includes the block of the latest write. After a successful
    commit, the slot's ``block`` field is the block we just landed on.
    Cheap chain query, runs only when the receipt lost the info.
    """
    if not result.success or result.block_number is not None:
        return result
    try:
        ss58 = wallet.hotkey.ss58_address
        commit = subtensor.substrate.query("Commitments", "CommitmentOf", [netuid, ss58])
        val = commit.value if hasattr(commit, "value") else commit
        if isinstance(val, dict) and val.get("block") is not None:
            from dataclasses import replace
            return replace(result, block_number=int(val["block"]))
    except Exception:
        pass
    return result


def _to_commit_result(response, *, reveal_round: Optional[int]) -> CommitResult:
    """Map a Bittensor SDK ExtrinsicResponse to our CommitResult type.

    The SDK shape varies across versions — some expose block info directly
    on the response, some on a nested ``extrinsic_receipt``, some on
    neither. We try every known path and fall through to ``None`` when the
    block isn't surfaced; callers can recover it with a chain query (see
    ``submit_timelock_commit`` / friends, which post-fetch as a fallback).
    """
    success = bool(getattr(response, "success", False))
    message = str(getattr(response, "message", ""))[:200]

    def _first_int(obj, *names):
        # Only accept actual ints; MagicMock attributes auto-resolve to
        # mock objects truthy under `is not None`, so in tests we'd pick
        # up bogus values. Real bittensor SDK returns plain ints.
        for n in names:
            v = getattr(obj, n, None)
            if isinstance(v, int) and not isinstance(v, bool):
                return v
        return None

    def _first_str_or_bytes(obj, *names):
        for n in names:
            v = getattr(obj, n, None)
            if isinstance(v, (str, bytes, bytearray)):
                return v
        return None

    # Preferred path: extrinsic_receipt (matches existing test fixtures
    # and bittensor 8.x/9.x SDK shape).
    receipt = getattr(response, "extrinsic_receipt", None)
    block_number = None
    extrinsic_hash = None
    if receipt is not None:
        block_number = _first_int(receipt, "block_number", "block_num", "block")
        extrinsic_hash = _first_str_or_bytes(receipt, "extrinsic_hash", "tx_hash", "hash")

    # Fallback: top-level on response (newer SDK shapes flatten this).
    if block_number is None:
        block_number = _first_int(response, "block_number", "block_num", "block")
    if extrinsic_hash is None:
        extrinsic_hash = _first_str_or_bytes(response, "extrinsic_hash", "tx_hash", "hash")

    if extrinsic_hash and isinstance(extrinsic_hash, (bytes, bytearray)):
        extrinsic_hash = "0x" + extrinsic_hash.hex()

    return CommitResult(
        success=success,
        message=message,
        block_number=block_number,
        extrinsic_hash=str(extrinsic_hash) if extrinsic_hash else None,
        reveal_round=reveal_round,
    )


def compute_blake2b_256(data: bytes) -> bytes:
    """BLAKE2b-256 helper used throughout the protocol."""
    return hashlib.blake2b(data, digest_size=32).digest()


def compute_sha256(data: bytes) -> bytes:
    """SHA-256 helper for chain commits (matches Data::Sha256 variant)."""
    return hashlib.sha256(data).digest()
