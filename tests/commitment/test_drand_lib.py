"""Tests for drand quicknet round computation and constants."""

from __future__ import annotations

import pytest

from hope.commitment.drand_lib import (
    EPOCH_BOUNDARY_SAFETY_ROUNDS,
    PRE_SCORING_MIN_OFFSET_ROUNDS,
    QUICKNET_CHAIN_HASH,
    QUICKNET_GENESIS_UNIX,
    QUICKNET_PERIOD_SECS,
    QUICKNET_PUBLIC_KEY_HEX,
    SUBMISSION_MIN_FUTURE_REVEAL_ROUNDS,
    drand_round_at,
    drand_round_to_unix,
)


class TestQuicknetConstants:
    def test_chain_hash_is_32_bytes(self):
        assert len(QUICKNET_CHAIN_HASH) == 32

    def test_chain_hash_value(self):
        # Must match subtensor pallets/drand/src/lib.rs:QUICKNET_CHAIN_HASH
        expected = bytes.fromhex(
            "52db9ba70e0cc0f6eaf7803dd07447a1f5477735fd3f661792ba94600c84e971"
        )
        assert QUICKNET_CHAIN_HASH == expected

    def test_period_is_3_seconds(self):
        assert QUICKNET_PERIOD_SECS == 3

    def test_genesis_unix(self):
        # 2023-08-23 15:09:27 UTC
        assert QUICKNET_GENESIS_UNIX == 1692803367

    def test_public_key_length(self):
        # G2 element on BLS12-381 is 96 bytes; hex is 192 chars
        assert len(QUICKNET_PUBLIC_KEY_HEX) == 192
        # Must be valid hex
        assert bytes.fromhex(QUICKNET_PUBLIC_KEY_HEX) is not None


class TestRoundAt:
    def test_round_1_at_genesis(self):
        assert drand_round_at(QUICKNET_GENESIS_UNIX) == 1

    def test_round_1_just_after_genesis(self):
        # Within first 3 seconds
        assert drand_round_at(QUICKNET_GENESIS_UNIX + 1) == 1
        assert drand_round_at(QUICKNET_GENESIS_UNIX + 2) == 1

    def test_round_2_at_3s_after_genesis(self):
        assert drand_round_at(QUICKNET_GENESIS_UNIX + 3) == 2

    def test_arbitrary_round(self):
        # 2024-01-01 00:00:00 UTC = 1704067200
        # round = (1704067200 - 1692803367) // 3 + 1 = 11263833 // 3 + 1 = 3754611 + 1 = 3754612
        assert drand_round_at(1704067200) == 3754612

    def test_returns_round_1_for_pre_genesis(self):
        # Defensive: don't return negative
        assert drand_round_at(0) == 1
        assert drand_round_at(QUICKNET_GENESIS_UNIX - 1) == 1


class TestRoundToUnix:
    def test_round_1_genesis(self):
        assert drand_round_to_unix(1) == QUICKNET_GENESIS_UNIX

    def test_round_2_genesis_plus_3(self):
        assert drand_round_to_unix(2) == QUICKNET_GENESIS_UNIX + 3

    def test_inverse_of_round_at(self):
        # round_to_unix(round_at(t)) ≤ t (returns the START of the round containing t)
        for t in [QUICKNET_GENESIS_UNIX + i for i in [0, 1, 2, 3, 4, 5, 100, 1000]]:
            r = drand_round_at(t)
            t_back = drand_round_to_unix(r)
            assert t_back <= t < t_back + QUICKNET_PERIOD_SECS

    def test_invalid_round(self):
        with pytest.raises(ValueError):
            drand_round_to_unix(0)
        with pytest.raises(ValueError):
            drand_round_to_unix(-1)


class TestTimingConstants:
    def test_pre_scoring_min_offset(self):
        # CL-1: at least 100 rounds (~5 min)
        assert PRE_SCORING_MIN_OFFSET_ROUNDS == 100
        # 100 rounds × 3s = 300 seconds = 5 minutes
        assert PRE_SCORING_MIN_OFFSET_ROUNDS * QUICKNET_PERIOD_SECS == 300

    def test_submission_min_future_reveal(self):
        # H-5: at least 20 rounds (~60 seconds)
        assert SUBMISSION_MIN_FUTURE_REVEAL_ROUNDS == 20
        assert SUBMISSION_MIN_FUTURE_REVEAL_ROUNDS * QUICKNET_PERIOD_SECS == 60

    def test_epoch_boundary_safety(self):
        # CL-11: at least 200 rounds (~10 min)
        assert EPOCH_BOUNDARY_SAFETY_ROUNDS == 200
        assert EPOCH_BOUNDARY_SAFETY_ROUNDS * QUICKNET_PERIOD_SECS == 600
