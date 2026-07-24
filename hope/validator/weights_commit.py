"""Layer 9.C.3 — validator weights commit via Bittensor's TLE'd `set_weights`.

The chain's `pallets/subtensor` accepts `set_weights(uids, weights)` and, when
the subnet has `commit_reveal_version=4` enabled, internally runs:

  1. Validator's salt is generated locally.
  2. (uids, weights, salt) are TLE-encrypted using drand quicknet.
  3. The encrypted blob is committed on-chain (TimelockedWeights storage).
  4. At the next subnet epoch boundary, the chain auto-decrypts, validates
     the salt against the commit, and applies the weights via Yuma consensus.

Layer 9.C.3 ties this commit to the validator's 9.C.2 post-scoring artifacts:
the `weights_commit_block_hash` field in 9.C.2 must equal the block hash where
this `set_weights` extrinsic landed. Any post-hoc rewrite of weights would
have to land in a DIFFERENT block, breaking the 9.C.2 binding and being caught
by `verify_epoch.py`.

Typical flow (validator runner):

  1. Score miners → produce {uid: weight_float} per protocol scoring.
  2. Call `commit_weights_layer_9c3(...)` — submits the extrinsic.
  3. Capture the returned `block_number` + `block_hash`.
  4. Build `post_scoring_artifacts` (Layer 9.C.2) embedding both.
  5. TLE-commit 9.C.2.

Quantization:
  - Bittensor accepts float weights in [0, 1]; the SDK normalises and
    quantises them to uint16. We pass floats so the SDK + chain handle
    the quantisation deterministically.
  - Caller MUST ensure UIDs are in subnet range and weights sum reasonably.

Failure handling:
  - `set_weights` retries internally up to `max_attempts` times (default 5).
  - We surface success / failure via `WeightsCommitResult.success`. A failed
    weights commit means the validator should NOT publish 9.C.2 (which
    promises a weight commit happened) — runner logic must skip 9.C.2 and
    re-attempt next epoch.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WeightsCommitResult:
    """Result of a Layer 9.C.3 weights commit attempt."""

    success: bool
    message: str
    block_number: Optional[int]
    block_hash: Optional[bytes]   # 32-byte raw hash, suitable for 9.C.2 binding
    extrinsic_hash: Optional[str]


def commit_weights_layer_9c3(
    *,
    subtensor,
    validator_wallet,
    netuid: int,
    uids: list[int],
    weights: list[float],
    version_key: int = 10002001,
    block_time_secs: float = 12.0,
    commit_reveal_version: int = 4,
    max_attempts: int = 5,
    wait_for_finalization: bool = True,
    raise_error: bool = False,
    verify_applied: bool = True,
) -> WeightsCommitResult:
    """Submit weights via `subtensor.set_weights` with TLE commit-reveal v4.

    Args:
        subtensor: Bittensor `Subtensor` instance.
        validator_wallet: `Wallet` whose hotkey signs the extrinsic.
        netuid: subnet ID.
        uids: list of integer UIDs (parallel to `weights`).
        weights: list of float weights in [0, 1]; SDK quantises to uint16.
        version_key: protocol version key required by Yuma; default 10002001.
        block_time_secs: block-time used by the SDK to compute reveal_round.
        commit_reveal_version: 4 = TLE'd commit-reveal (the protocol path we want).
        max_attempts: SDK-side retry budget; we just forward.

    Returns:
        WeightsCommitResult — `success=True` iff the extrinsic was included.
    """
    if len(uids) != len(weights):
        raise ValueError(
            f"uids/weights length mismatch: {len(uids)} vs {len(weights)}"
        )
    if not uids:
        raise ValueError("uids/weights must be non-empty")

    # Capture LastUpdate BEFORE the commit so we can confirm afterwards that the
    # weights actually landed. `set_weights` can return success=True even when
    # the chain silently rejected the commit (e.g. WeightsSetRateLimit), and the
    # empty-receipt fallback below would then stamp a bogus block — a false
    # positive. LastUpdate only advances when the commit is genuinely accepted.
    pre_last_update = (
        _read_validator_last_update(subtensor, netuid, validator_wallet)
        if verify_applied else None
    )

    response = subtensor.set_weights(
        wallet=validator_wallet,
        netuid=netuid,
        uids=list(uids),
        weights=list(weights),
        block_time=block_time_secs,
        commit_reveal_version=commit_reveal_version,
        version_key=version_key,
        max_attempts=max_attempts,
        wait_for_inclusion=True,
        wait_for_finalization=wait_for_finalization,
        raise_error=raise_error,
    )

    success = bool(getattr(response, "success", False))
    message = str(getattr(response, "message", ""))[:200]

    block_number: Optional[int] = None
    block_hash: Optional[bytes] = None
    extrinsic_hash: Optional[str] = None

    receipt = getattr(response, "extrinsic_receipt", None)
    if receipt is not None:
        block_number = getattr(receipt, "block_number", None)
        eh = getattr(receipt, "extrinsic_hash", None)
        if eh is not None:
            extrinsic_hash = eh.hex() if isinstance(eh, (bytes, bytearray)) else str(eh)
            if extrinsic_hash and not extrinsic_hash.startswith("0x"):
                extrinsic_hash = "0x" + extrinsic_hash

    if success:
        # Primary path: extract from receipt.
        if block_number is not None:
            block_hash = _resolve_block_hash(subtensor, block_number)
        # Fallback path: when the SDK doesn't populate extrinsic_receipt
        # (Bittensor 10.2.1 + commit-reveal v4 sometimes returns None even
        # on success), use the current block's hash as the binding target.
        # This is conservative — the chain finality protects within ~6
        # blocks, and 9.C.2's block_hash is published in a TLE'd commit
        # that can't be back-edited. A few blocks of slack is acceptable.
        if block_hash is None:
            try:
                current = subtensor.get_current_block()
                block_hash = _resolve_block_hash(subtensor, current)
                if block_hash is not None:
                    block_number = current
                    logger.warning(
                        "9.C.3 receipt was empty; using current block %d as binding "
                        "fallback", current,
                    )
            except (AttributeError, Exception) as e:
                logger.warning("9.C.3 fallback block_hash lookup failed: %s", e)

    # Post-commit verification: confirm the weights ACTUALLY landed. A
    # success=True from set_weights does not guarantee application — the chain
    # can reject within WeightsSetRateLimit and the SDK still reports success
    # with an empty receipt. LastUpdate advances only on an accepted commit, so
    # if it did not move, downgrade to failure (and drop the fallback block_hash
    # so we never bind 9.C.2 to a commit that didn't happen). Best-effort: only
    # downgrade on a CONFIDENT non-advance (clean pre + post reads); any read
    # failure leaves the result untouched.
    if success and verify_applied and pre_last_update is not None:
        post_last_update = _read_validator_last_update(
            subtensor, netuid, validator_wallet
        )
        if post_last_update is not None and post_last_update <= pre_last_update:
            logger.warning(
                "9.C.3 set_weights reported success but LastUpdate did not advance "
                "(pre=%s post=%s) — likely WeightsSetRateLimit; treating as FAILED",
                pre_last_update, post_last_update,
            )
            success = False
            message = (
                (message + " | " if message else "")
                + f"not_applied: LastUpdate stuck at {post_last_update} "
                f"(rate-limited / commit rejected)"
            )
            block_hash = None
            block_number = None

    logger.info(
        "9.C.3 weights commit netuid=%d uids=%d success=%s block=%s",
        netuid, len(uids), success, block_number,
    )
    return WeightsCommitResult(
        success=success,
        message=message,
        block_number=block_number,
        block_hash=block_hash,
        extrinsic_hash=extrinsic_hash,
    )


def _read_validator_last_update(subtensor, netuid: int, validator_wallet) -> Optional[int]:
    """Best-effort read of ``LastUpdate[netuid][validator_uid]`` (the block at
    which this validator last successfully set weights).

    Used to confirm a commit actually landed. Returns None on any failure
    (no hotkey, uid not found, RPC error) — callers treat None as "could not
    verify" and leave the success flag unchanged.
    """
    try:
        ss58 = validator_wallet.hotkey.ss58_address
    except Exception:
        return None
    try:
        uid = subtensor.get_uid_for_hotkey_on_subnet(ss58, netuid)
    except Exception as e:
        logger.warning("LastUpdate verify: uid lookup failed: %s", e)
        return None
    if uid is None:
        return None
    try:
        q = subtensor.substrate.query("SubtensorModule", "LastUpdate", [netuid])
        v = q.value if hasattr(q, "value") else q
        if v is None or uid >= len(v):
            return None
        return int(v[uid])
    except Exception as e:
        logger.warning("LastUpdate verify: storage read failed: %s", e)
        return None


def _resolve_block_hash(subtensor, block_number: int) -> Optional[bytes]:
    """Best-effort lookup of block_hash for a block number.

    The Bittensor SDK exposes the underlying substrate interface as
    `subtensor.substrate`; calling `get_block_hash` returns a 0x-hex string
    which we normalise to raw 32 bytes for 9.C.2 binding.
    """
    substrate = getattr(subtensor, "substrate", None)
    if substrate is None:
        return None
    try:
        h = substrate.get_block_hash(block_number)
    except Exception as e:
        logger.warning("failed to resolve block_hash for #%d: %s", block_number, e)
        return None
    if h is None:
        return None
    if isinstance(h, (bytes, bytearray)):
        return bytes(h)
    if isinstance(h, str):
        s = h[2:] if h.startswith("0x") else h
        try:
            return bytes.fromhex(s)
        except ValueError:
            return None
    return None


def estimate_weights_reveal_round(
    *,
    current_round: int,
    blocks_until_reveal: int,
    block_time_secs: float = 12.0,
    drand_period_secs: float = 3.0,
) -> int:
    """Approximate the drand round at which a v4 weights commit will reveal.

    The chain TLE-encrypts to `current_round + ceil((blocks_until_reveal *
    block_time) / drand_period)`. Caller passes the SDK-derived
    `blocks_until_reveal` (typically `tempo` blocks for the next epoch).
    """
    import math

    rounds = math.ceil((blocks_until_reveal * block_time_secs) / drand_period_secs)
    return int(current_round + rounds)
