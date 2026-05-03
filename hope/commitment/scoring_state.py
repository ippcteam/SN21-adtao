"""Layer 9.C.1 / 9.C.2 — validator scoring-state CBOR builders.

Both layers commit a small canonical-CBOR plaintext over chain via
`submit_timelock_commit`. The plaintext for each layer must:

  - Carry `validator_hotkey` (32 raw bytes) and an inner_sig.
  - Be canonically encoded (RFC 8949 §4.2.1).
  - Fit in `MAX_TLE_PLAINTEXT_BYTES` (768 bytes after TLE overhead).

9.C.1 PRE-SCORING STATE
-----------------------
Captured BEFORE scoring runs, while predictions are still being decrypted but
before the validator decides any weights. Provides the audit baseline:

    {
      "v": 1,                                       # protocol version
      "validator_hotkey": bstr(32),
      "epoch_id": tstr,                             # release_key
      "epoch_idx": uint,                            # monotonic per-validator
      "outcomes_release_round": uint,               # drand round when 9.A K released
      "outcomes_fetched_at_round": uint,            # drand round of validator's fetch
      "miner_commits_root": bstr(32),               # IMT root over miner uid → sha256(K_chain || sha256_ct_chain)
      "excluded_miners_hash": bstr(32),             # sha256 of canonical-CBOR encoded excluded list
      "n_miners": uint,
      "n_excluded": uint,
      "inner_sig": bstr(64),
    }

`miner_commits_root` is the IMT root over leaves
    key   = miner_hotkey (32 bytes raw)
    value = blake2b_256(k_chain_record || sha256_ct_record)
where:
    k_chain_record   = canonical_cbor({"k_block": uint, "k_round": uint})
    sha256_ct_record = sha256_ct_commit_bytes (32)

The leaf value commits BOTH the chain block of the K commit AND the chain
SHA-256 of AES_ct, in one BLAKE2b digest. This single root proves that no
validator can claim a different (per-miner) chain state at scoring time —
all validators must agree on the (k_block, k_round, sha256_ct) tuple.

`excluded_miners_hash` keeps the `excluded_miners` payload off-chain: the
list of {uid, hotkey, reason} tuples can grow with the miner population but
must remain auditable. Validators publish the list via HTTPS alongside the
9.C.6 retry log.

9.C.2 POST-SCORING ARTIFACTS
----------------------------
Captured AFTER scoring runs and weights have been committed. Locks the
scoring outputs to the same chain account, defeating mid-flight rewrites:

    {
      "v": 1,
      "validator_hotkey": bstr(32),
      "epoch_id": tstr,
      "epoch_idx": uint,
      "scoring_hash": bstr(32),                     # sha256 of canonical-CBOR scoring inputs
      "final_score_root": bstr(32),                 # IMT root over uid → score_int
      "weights_commit_block_hash": bstr(32),        # block hash where commit_timelocked_weights landed
      "weights_reveal_round": uint,                 # drand round when weights auto-reveal
      "n_miners_scored": uint,
      "inner_sig": bstr(64),
    }

`final_score_root` IMT leaves are
    key   = miner_hotkey (32 bytes raw)
    value = score_int_be32_le_padded (uint score in micro-units, big-endian, left-padded to 32)

The IMT lets the public verifier prove inclusion of any single miner's
score under the chain-anchored root without downloading the whole table.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.canonical import canonical_cbor_dumps
from hope.commitment.imt import imt_root, make_leaves_with_pointers
from hope.commitment.inner_sig import add_inner_sig
from hope.commitment.on_chain import MAX_TLE_PLAINTEXT_BYTES


SCORING_STATE_VERSION = 1


@dataclass(frozen=True)
class MinerCommitRecord:
    """Per-miner chain-anchored record used to build the 9.C.1 IMT root."""

    miner_hotkey: bytes  # 32-byte raw ed25519 pubkey
    k_block: int         # subtensor block where TLE K landed
    k_round: int         # drand round encoded in K commit
    sha256_ct: bytes     # 32-byte chain-side Sha256(AES_ct) commit


@dataclass(frozen=True)
class ExcludedMinerRecord:
    """One entry of the off-chain excluded-miners list (hashed into 9.C.1)."""

    miner_hotkey: bytes  # 32-byte raw ed25519 pubkey
    miner_uid: int
    reason: str          # ScoreabilityFailure value or "plaintext_unavailable"


@dataclass(frozen=True)
class ScoredMinerRecord:
    """Per-miner score used to build the 9.C.2 IMT root."""

    miner_hotkey: bytes  # 32-byte raw ed25519 pubkey
    score_micro: int     # score expressed as a uint in micro-units (×1e6)


def compute_miner_commits_root(records: list[MinerCommitRecord]) -> bytes:
    """Compute the 9.C.1 `miner_commits_root` over per-miner chain records."""
    pairs: list[tuple[bytes, bytes]] = []
    for rec in records:
        if len(rec.miner_hotkey) != 32:
            raise ValueError(f"miner_hotkey must be 32 bytes, got {len(rec.miner_hotkey)}")
        if len(rec.sha256_ct) != 32:
            raise ValueError(f"sha256_ct must be 32 bytes, got {len(rec.sha256_ct)}")
        chain_record = canonical_cbor_dumps(
            {"k_block": int(rec.k_block), "k_round": int(rec.k_round)}
        )
        leaf_value = hashlib.blake2b(
            chain_record + rec.sha256_ct, digest_size=32
        ).digest()
        pairs.append((rec.miner_hotkey, leaf_value))

    if not pairs:
        # Empty IMT root is the empty-leaf hash bubbled up. The IMT module
        # handles the empty case by treating all leaves as default. We just
        # delegate to imt_root with an empty leaf list.
        return imt_root([])

    leaves = make_leaves_with_pointers(pairs)
    return imt_root(leaves)


def compute_excluded_miners_hash(records: list[ExcludedMinerRecord]) -> bytes:
    """Compute SHA-256 over canonical-CBOR encoding of the excluded list.

    Sorted by hotkey to keep the encoding deterministic across validators
    even when their internal iteration order differs.
    """
    sorted_records = sorted(records, key=lambda r: r.miner_hotkey)
    payload = [
        {"hotkey": r.miner_hotkey, "uid": int(r.miner_uid), "reason": r.reason}
        for r in sorted_records
    ]
    return hashlib.sha256(canonical_cbor_dumps(payload)).digest()


def build_pre_scoring_state(
    *,
    validator_hotkey: bytes,
    validator_signing_key: Ed25519PrivateKey,
    epoch_id: str,
    epoch_idx: int,
    outcomes_release_round: int,
    outcomes_fetched_at_round: int,
    miner_commits: list[MinerCommitRecord],
    excluded_miners: list[ExcludedMinerRecord],
) -> bytes:
    """Build the 9.C.1 pre-scoring CBOR blob, ready to TLE-commit.

    Returns canonical CBOR bytes. Raises if the result exceeds
    `MAX_TLE_PLAINTEXT_BYTES`.
    """
    if len(validator_hotkey) != 32:
        raise ValueError(f"validator_hotkey must be 32 bytes, got {len(validator_hotkey)}")

    plaintext: dict[str, Any] = {
        "v": SCORING_STATE_VERSION,
        "validator_hotkey": validator_hotkey,
        "epoch_id": epoch_id,
        "epoch_idx": int(epoch_idx),
        "outcomes_release_round": int(outcomes_release_round),
        "outcomes_fetched_at_round": int(outcomes_fetched_at_round),
        "miner_commits_root": compute_miner_commits_root(miner_commits),
        "excluded_miners_hash": compute_excluded_miners_hash(excluded_miners),
        "n_miners": int(len(miner_commits)),
        "n_excluded": int(len(excluded_miners)),
    }
    add_inner_sig(plaintext, validator_signing_key, hotkey_field="validator_hotkey")
    encoded = canonical_cbor_dumps(plaintext)
    _check_tle_budget(encoded, "pre_scoring_state (9.C.1)")
    return encoded


def compute_final_score_root(records: list[ScoredMinerRecord]) -> bytes:
    """Compute the 9.C.2 `final_score_root` IMT root over per-miner scores."""
    pairs: list[tuple[bytes, bytes]] = []
    for rec in records:
        if len(rec.miner_hotkey) != 32:
            raise ValueError(f"miner_hotkey must be 32 bytes, got {len(rec.miner_hotkey)}")
        if rec.score_micro < 0:
            raise ValueError(f"score_micro must be non-negative, got {rec.score_micro}")
        # Big-endian uint, left-padded to 32 bytes.
        try:
            value = int(rec.score_micro).to_bytes(32, "big", signed=False)
        except OverflowError as e:
            raise ValueError(f"score_micro too large: {rec.score_micro}") from e
        pairs.append((rec.miner_hotkey, value))

    if not pairs:
        return imt_root([])
    leaves = make_leaves_with_pointers(pairs)
    return imt_root(leaves)


def build_post_scoring_artifacts(
    *,
    validator_hotkey: bytes,
    validator_signing_key: Ed25519PrivateKey,
    epoch_id: str,
    epoch_idx: int,
    scoring_inputs_hash: bytes,
    scored_miners: list[ScoredMinerRecord],
    weights_commit_block_hash: bytes,
    weights_reveal_round: int,
) -> bytes:
    """Build the 9.C.2 post-scoring CBOR blob, ready to TLE-commit.

    Args:
        scoring_inputs_hash: 32-byte SHA-256 over the canonical CBOR encoding
            of all scoring inputs (predictions + outcomes + reference series).
        weights_commit_block_hash: 32-byte block hash where this validator's
            `commit_timelocked_weights` extrinsic landed.
        weights_reveal_round: drand round at which the chain auto-decrypts
            the weights commit.
    """
    if len(validator_hotkey) != 32:
        raise ValueError(f"validator_hotkey must be 32 bytes, got {len(validator_hotkey)}")
    if len(scoring_inputs_hash) != 32:
        raise ValueError(
            f"scoring_inputs_hash must be 32 bytes, got {len(scoring_inputs_hash)}"
        )
    if len(weights_commit_block_hash) != 32:
        raise ValueError(
            f"weights_commit_block_hash must be 32 bytes, got {len(weights_commit_block_hash)}"
        )

    plaintext: dict[str, Any] = {
        "v": SCORING_STATE_VERSION,
        "validator_hotkey": validator_hotkey,
        "epoch_id": epoch_id,
        "epoch_idx": int(epoch_idx),
        "scoring_hash": scoring_inputs_hash,
        "final_score_root": compute_final_score_root(scored_miners),
        "weights_commit_block_hash": weights_commit_block_hash,
        "weights_reveal_round": int(weights_reveal_round),
        "n_miners_scored": int(len(scored_miners)),
    }
    add_inner_sig(plaintext, validator_signing_key, hotkey_field="validator_hotkey")
    encoded = canonical_cbor_dumps(plaintext)
    _check_tle_budget(encoded, "post_scoring_artifacts (9.C.2)")
    return encoded


def _check_tle_budget(encoded: bytes, label: str) -> None:
    if len(encoded) > MAX_TLE_PLAINTEXT_BYTES:
        raise ValueError(
            f"{label} CBOR is {len(encoded)} bytes > "
            f"MAX_TLE_PLAINTEXT_BYTES={MAX_TLE_PLAINTEXT_BYTES}; "
            "trim or move fields off-chain"
        )
