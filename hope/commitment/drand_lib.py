"""Drand quicknet parameters and round-time conversion.

Constants are hardcoded from the drand quicknet launch announcement
(https://docs.drand.love/blog/2023/10/16/quicknet-is-live/) and verified
against subtensor's `pallets/drand/src/lib.rs:QUICKNET_CHAIN_HASH`.

Both `bittensor-drand` (weight commits) and `timelock` PyPI (arbitrary
payloads) target this same chain, so encryption produced off-chain is
decryptable by subtensor's `pallet_commitments::reveal_timelocked_commitments()`.
"""

from __future__ import annotations

# Drand quicknet chain hash; matches subtensor pallet_drand QUICKNET_CHAIN_HASH
QUICKNET_CHAIN_HASH: bytes = bytes.fromhex(
    "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971"
)

# Quicknet genesis Unix time (2023-08-23 15:09:27 UTC)
QUICKNET_GENESIS_UNIX: int = 1692803367

# Round period in seconds
QUICKNET_PERIOD_SECS: int = 3

# Quicknet G2 public key (96 bytes, BLS12-381 G2 element, hex)
# Used by `timelock` library as Timelock(pk_hex) parameter
QUICKNET_PUBLIC_KEY_HEX: str = (
    "83cf0f2896adee7eb8b5f01fcad3912212c437e0073e911fb90022d3e760183c"
    "8c4b450b6a0a6c3ac6a5776a2d1064510d1fec758c921cc22b0e17e63aaf4bcb"
    "5ed66304de9cf809bd274ca73bab4af5a6e9c76a4bc09e76eae8991ef5ece45a"
)


def drand_round_at(unix_time: int) -> int:
    """Compute the drand quicknet round number for a given Unix timestamp.

    Round formula per drand spec:
        round = (target_unix_time - genesis_time) // period + 1

    Returns 1 for unix_time == QUICKNET_GENESIS_UNIX. Returns the round whose
    pulse will be published at-or-after `unix_time`.

    For times before genesis, returns 1 (first round). Caller should validate.
    """
    if unix_time < QUICKNET_GENESIS_UNIX:
        return 1
    return (unix_time - QUICKNET_GENESIS_UNIX) // QUICKNET_PERIOD_SECS + 1


def drand_round_to_unix(round_number: int) -> int:
    """Inverse of drand_round_at: return the Unix time at which `round_number` is expected to be published."""
    if round_number < 1:
        raise ValueError("round_number must be >= 1")
    return QUICKNET_GENESIS_UNIX + (round_number - 1) * QUICKNET_PERIOD_SECS


# CL-1 timing constraint: validator's 9.C.1 reveal_round must be at least
# this many drand rounds in the future relative to the miner deadline reveal
# round (R). 100 rounds = 5 minutes — sufficient for finality + scoring.
PRE_SCORING_MIN_OFFSET_ROUNDS: int = 100

# Submission-time minimum future-reveal offset (H-5 fix): TLE field's
# reveal_round must be at least this many rounds beyond current_round at
# submission time, regardless of layer. 20 rounds = 60 seconds.
SUBMISSION_MIN_FUTURE_REVEAL_ROUNDS: int = 20

# CL-11 epoch-boundary safety margin: all reveal rounds must be at least
# this many rounds before next epoch starts.
EPOCH_BOUNDARY_SAFETY_ROUNDS: int = 200
