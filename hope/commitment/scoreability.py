"""Scoreability rule for Layer 9.B miner predictions.

Once a validator (or public verifier) has fetched AES_ct from an archive,
auto-decrypted K from chain, and AES-GCM-decrypted the prediction CBOR, the
following EIGHT checks must ALL pass for the prediction to enter scoring:

  1. ON_CHAIN_PRESENT: The miner has all three on-chain commits at the
     correct UID/hotkey for this epoch (TimelockEncrypted K, Sha256 ct,
     Raw self_archive_url). Missing any → exclude.

  2. CIPHERTEXT_MATCH: SHA-256(AES_ct) equals the on-chain Sha256 commit.
     Mismatch → exclude (and the on-chain hash wins for audit).

  3. AAD_BIND: AES-GCM decryption succeeded with AAD = aes_gcm_aad(epoch_id).
     Failure → exclude (replay or wrong epoch binding).

  4. CANONICAL_ENCODING: The decrypted plaintext round-trips through
     canonical CBOR byte-for-byte. Non-canonical → exclude.

  5. INNER_SIG: The plaintext's `inner_sig` verifies against
     `plaintext.miner_hotkey`, AND that hotkey equals the chain storage
     account that wrote the K commit. Either fails → exclude.

  6. EPOCH_MATCH: plaintext.epoch_id equals the epoch being scored. Avoids
     a malicious validator splicing a different epoch's K into this epoch's
     ciphertext slot. Mismatch → exclude.

  7. HORIZON_SHAPE: Every horizon entry has the required fields, allowed
     horizon labels, monotone P10≤P50≤P90, and probabilities in [0, ppm].
     Any malformed → exclude.

  8. TIMING_BOUND: plaintext.submitted_round is within the protocol-allowed
     window [epoch_open_round, miner_deadline_round] AND the chain block
     where the K commit landed is also within that window. Out-of-window →
     exclude.

Each check returns a discrete failure code so the retry log + verifier diff
can attribute exclusions precisely. A prediction that passes all 8 returns
ScoreabilityResult(ok=True, plaintext=...).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any

from cryptography.exceptions import InvalidTag

from hope.commitment.canonical import (
    canonical_cbor_dumps,
    canonical_cbor_loads,
)
from hope.commitment.prediction_payload import (
    ALLOWED_HORIZONS,
    PROBABILITY_SCALE,
    decrypt_prediction,
    verify_prediction_inner_sig,
)


class ScoreabilityFailure(str, Enum):
    """Discrete failure codes for the 8-check scoreability rule."""

    ON_CHAIN_PRESENT_MISSING_K = "on_chain_present.missing_k"
    ON_CHAIN_PRESENT_MISSING_SHA = "on_chain_present.missing_sha"
    ON_CHAIN_PRESENT_MISSING_URL = "on_chain_present.missing_url"
    CIPHERTEXT_MATCH_FAIL = "ciphertext_match.fail"
    AAD_BIND_INVALID_TAG = "aad_bind.invalid_tag"
    AAD_BIND_DECODE_ERROR = "aad_bind.decode_error"
    CANONICAL_ENCODING_FAIL = "canonical_encoding.fail"
    INNER_SIG_FAIL = "inner_sig.fail"
    INNER_SIG_HOTKEY_MISMATCH = "inner_sig.hotkey_mismatch"
    EPOCH_MATCH_FAIL = "epoch_match.fail"
    HORIZON_SHAPE_FAIL = "horizon_shape.fail"
    TIMING_OUT_OF_WINDOW = "timing.out_of_window"


@dataclass(frozen=True)
class OnChainCommitTriple:
    """The three on-chain commits a miner produces per epoch.

    Any of these may be None (i.e., the miner did not commit it) — the
    scoreability rule rejects the prediction if any are missing.
    """

    timelock_k_present: bool                # 9.B TimelockEncrypted(K) commit
    sha256_ct_commit: bytes | None       # 9.B Sha256(AES_ct) commit (32 bytes)
    self_archive_url: str | None         # 9.B Raw{N} commit
    chain_block_at_k_commit: int | None  # block where TLE K landed


@dataclass(frozen=True)
class TimingBounds:
    """Per-epoch timing window for accepting miner submissions.

    epoch_open_round/deadline_round are drand rounds bounding when the miner's
    plaintext.submitted_round must fall. chain_window_*_block are the block
    range during which the on-chain K commit must have been included.
    """

    epoch_open_round: int
    miner_deadline_round: int
    chain_window_min_block: int
    chain_window_max_block: int


@dataclass(frozen=True)
class ScoreabilityResult:
    """Outcome of the 8-check scoreability rule for one miner prediction."""

    ok: bool
    failure: ScoreabilityFailure | None
    plaintext: dict[str, Any] | None  # decoded only if ok=True
    detail: str = ""


def evaluate_scoreability(
    *,
    aes_ct: bytes,
    aes_key: bytes,
    epoch_id: str,
    miner_hotkey_from_chain: bytes,
    on_chain: OnChainCommitTriple,
    timing: TimingBounds,
) -> ScoreabilityResult:
    """Run all 8 checks against a fetched ciphertext + revealed key.

    Args:
        aes_ct: archive-fetched ciphertext.
        aes_key: chain-revealed K.
        epoch_id: the epoch being scored (drives AAD binding).
        miner_hotkey_from_chain: 32-byte ed25519 public key for the storage
            account that wrote the K commit. Substrate guarantees the writer
            controls this key, so this is the trust anchor.
        on_chain: the three commit fields read from chain.
        timing: protocol timing bounds for this epoch.

    Returns:
        ScoreabilityResult with ok=True only if every check passes; otherwise
        the first failure encountered with a discrete failure code.
    """
    # 1. ON_CHAIN_PRESENT
    if not on_chain.timelock_k_present:
        return _fail(ScoreabilityFailure.ON_CHAIN_PRESENT_MISSING_K)
    if not on_chain.sha256_ct_commit or len(on_chain.sha256_ct_commit) != 32:
        return _fail(ScoreabilityFailure.ON_CHAIN_PRESENT_MISSING_SHA)
    if not on_chain.self_archive_url:
        return _fail(ScoreabilityFailure.ON_CHAIN_PRESENT_MISSING_URL)

    # 2. CIPHERTEXT_MATCH
    actual_sha = hashlib.sha256(aes_ct).digest()
    if actual_sha != on_chain.sha256_ct_commit:
        return _fail(
            ScoreabilityFailure.CIPHERTEXT_MATCH_FAIL,
            detail=f"on_chain_sha={on_chain.sha256_ct_commit.hex()[:16]}.. "
                   f"actual_sha={actual_sha.hex()[:16]}..",
        )

    # 3. AAD_BIND  (also runs CBOR decode; we re-canonicalise below for #4)
    try:
        plaintext = decrypt_prediction(aes_ct, aes_key, epoch_id=epoch_id)
    except InvalidTag:
        return _fail(ScoreabilityFailure.AAD_BIND_INVALID_TAG)
    except Exception as e:
        return _fail(
            ScoreabilityFailure.AAD_BIND_DECODE_ERROR, detail=str(e)[:120]
        )

    # 4. CANONICAL_ENCODING — decrypt_prediction does CBOR decode; re-encode
    #    and compare to the inner-decrypted plaintext bytes. We need the raw
    #    CBOR for that round-trip; recompute from aes_ct.
    try:
        raw_cbor = _extract_cbor_from_decrypt(aes_ct, aes_key, epoch_id)
    except Exception as e:
        return _fail(
            ScoreabilityFailure.CANONICAL_ENCODING_FAIL,
            detail=f"recompute failed: {e}",
        )
    if canonical_cbor_dumps(canonical_cbor_loads(raw_cbor)) != raw_cbor:
        return _fail(ScoreabilityFailure.CANONICAL_ENCODING_FAIL)

    # 5. INNER_SIG  — verify against the chain-anchored miner hotkey.
    if not verify_prediction_inner_sig(plaintext, miner_hotkey_from_chain):
        if plaintext.get("miner_hotkey") != miner_hotkey_from_chain:
            return _fail(ScoreabilityFailure.INNER_SIG_HOTKEY_MISMATCH)
        return _fail(ScoreabilityFailure.INNER_SIG_FAIL)

    # 6. EPOCH_MATCH
    if plaintext.get("epoch_id") != epoch_id:
        return _fail(
            ScoreabilityFailure.EPOCH_MATCH_FAIL,
            detail=f"plaintext.epoch_id={plaintext.get('epoch_id')!r} "
                   f"epoch_arg={epoch_id!r}",
        )

    # 7. HORIZON_SHAPE
    horizon_err = _check_horizon_shape(plaintext)
    if horizon_err is not None:
        return _fail(ScoreabilityFailure.HORIZON_SHAPE_FAIL, detail=horizon_err)

    # 8. TIMING_BOUND
    submitted_round = plaintext.get("submitted_round")
    if not isinstance(submitted_round, int):
        return _fail(
            ScoreabilityFailure.TIMING_OUT_OF_WINDOW,
            detail="submitted_round missing or not int",
        )
    if not (timing.epoch_open_round <= submitted_round <= timing.miner_deadline_round):
        return _fail(
            ScoreabilityFailure.TIMING_OUT_OF_WINDOW,
            detail=f"submitted_round={submitted_round} not in "
                   f"[{timing.epoch_open_round}, {timing.miner_deadline_round}]",
        )
    block = on_chain.chain_block_at_k_commit
    if block is None or not (
        timing.chain_window_min_block <= block <= timing.chain_window_max_block
    ):
        return _fail(
            ScoreabilityFailure.TIMING_OUT_OF_WINDOW,
            detail=f"k_commit_block={block} not in "
                   f"[{timing.chain_window_min_block}, "
                   f"{timing.chain_window_max_block}]",
        )

    return ScoreabilityResult(ok=True, failure=None, plaintext=plaintext)


def _extract_cbor_from_decrypt(
    aes_ct: bytes, aes_key: bytes, epoch_id: str
) -> bytes:
    """Re-decrypt and return the raw plaintext bytes (no CBOR decode).

    `decrypt_prediction` returns the decoded dict; for the canonical-encoding
    round-trip we need the raw bytes. We re-run AES-GCM here rather than
    refactoring decrypt_prediction to expose internals.
    """
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    from hope.commitment.canonical import aes_gcm_aad
    from hope.commitment.prediction_payload import AES_NONCE_BYTES

    nonce = aes_ct[:AES_NONCE_BYTES]
    ct_with_tag = aes_ct[AES_NONCE_BYTES:]
    aad = aes_gcm_aad(epoch_id)
    cipher = AESGCM(aes_key)
    return cipher.decrypt(nonce, ct_with_tag, aad)


def _check_horizon_shape(plaintext: dict[str, Any]) -> str | None:
    horizons = plaintext.get("horizons")
    if not isinstance(horizons, list) or not horizons:
        return "horizons missing or empty"
    seen: set[str] = set()
    for i, h in enumerate(horizons):
        if not isinstance(h, dict):
            return f"horizons[{i}] not a map"
        label = h.get("h")
        if label not in ALLOWED_HORIZONS:
            return f"horizons[{i}].h={label!r} not in {ALLOWED_HORIZONS}"
        if label in seen:
            return f"duplicate horizon label {label!r}"
        seen.add(label)

        for q_field in ("cost_q", "conv_q", "eff_q"):
            triple = h.get(q_field)
            if (not isinstance(triple, list) or len(triple) != 3
                    or not all(isinstance(x, int) for x in triple)):
                return f"horizons[{i}].{q_field} not a 3-int list"
            if not (triple[0] <= triple[1] <= triple[2]):
                return f"horizons[{i}].{q_field} not monotone: {triple}"

        for p_field in ("miss_p", "instab_p"):
            v = h.get(p_field)
            if not isinstance(v, int) or not (0 <= v <= PROBABILITY_SCALE):
                return f"horizons[{i}].{p_field}={v!r} not uint in [0, {PROBABILITY_SCALE}]"
    return None


def _fail(code: ScoreabilityFailure, detail: str = "") -> ScoreabilityResult:
    return ScoreabilityResult(
        ok=False, failure=code, plaintext=None, detail=detail
    )
