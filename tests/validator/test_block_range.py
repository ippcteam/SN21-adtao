"""Tests for compute_block_range in hope/validator/onchain_runner.py."""

from __future__ import annotations

from hope.commitment.on_chain import CommitResult
from hope.validator.onchain_runner import (
    EpochScoringOutcome,
    MinerOnChainInputs,
    compute_block_range,
)
from hope.validator.weights_commit import WeightsCommitResult


def _miner(uid: int, k_block: int | None) -> MinerOnChainInputs:
    return MinerOnChainInputs(
        miner_uid=uid,
        miner_hotkey=bytes(32),
        revealed_k=None,
        sha256_ct_commit=None,
        self_archive_url=None,
        chain_block_at_k_commit=k_block,
        k_reveal_round=0,
    )


def _commit_ok(block: int) -> CommitResult:
    return CommitResult(
        success=True,
        message="ok",
        block_number=block,
        extrinsic_hash="0x" + "a" * 64,
        reveal_round=None,
    )


def _commit_fail() -> CommitResult:
    return CommitResult(
        success=False,
        message="failed",
        block_number=None,
        extrinsic_hash=None,
        reveal_round=None,
    )


def _weights_ok(block: int) -> WeightsCommitResult:
    return WeightsCommitResult(
        success=True,
        message="ok",
        block_number=block,
        block_hash="0x" + "b" * 64,
        extrinsic_hash="0x" + "c" * 64,
    )


def _weights_fail() -> WeightsCommitResult:
    return WeightsCommitResult(
        success=False,
        message="failed",
        block_number=None,
        block_hash=None,
        extrinsic_hash=None,
    )


def test_full_success_takes_min_miner_block_and_weights_block():
    """Happy path: miner k_blocks present + weights commit succeeded."""
    miners = [_miner(0, 1_000_100), _miner(1, 1_000_050), _miner(2, 1_000_200)]
    start, end = compute_block_range(miners, _commit_ok(1_000_000), _weights_ok(1_001_000))
    assert start == 1_000_050    # min of miner k_blocks (ignores pre_commit block)
    assert end == 1_001_000      # weights commit block


def test_no_miners_falls_back_to_pre_commit_block():
    """When no miner k_blocks are visible, start = validator's 9.C.1 block."""
    start, end = compute_block_range([], _commit_ok(1_000_000), _weights_ok(1_001_000))
    assert start == 1_000_000
    assert end == 1_001_000


def test_miners_with_no_k_block_treated_as_absent():
    """Miners whose `chain_block_at_k_commit` is None don't contribute to start.

    If all miners have None, fall back to pre_commit block.
    """
    miners = [_miner(0, None), _miner(1, None), _miner(2, None)]
    start, end = compute_block_range(miners, _commit_ok(1_000_000), _weights_ok(1_001_000))
    assert start == 1_000_000
    assert end == 1_001_000


def test_mix_of_present_and_absent_k_blocks_picks_min_present():
    """Mixed None / valid k_blocks: take the min of the valid ones, ignore None."""
    miners = [_miner(0, None), _miner(1, 1_000_300), _miner(2, 1_000_120), _miner(3, None)]
    start, end = compute_block_range(miners, _commit_ok(999_000), _weights_ok(1_001_000))
    assert start == 1_000_120     # min of present miner blocks; pre_commit ignored
    assert end == 1_001_000


def test_zero_k_block_not_treated_as_real():
    """`chain_block_at_k_commit == 0` is the dataclass-default sentinel for
    "not set" in some MinerOnChainInputs constructions. Treat it as absent."""
    miners = [_miner(0, 0), _miner(1, 1_000_500)]
    start, _end = compute_block_range(miners, _commit_ok(999_000), _weights_ok(1_001_000))
    assert start == 1_000_500


def test_weights_commit_failed_returns_none_end():
    """When weights commit fails, end is None even if start is derivable."""
    miners = [_miner(0, 1_000_100)]
    start, end = compute_block_range(miners, _commit_ok(1_000_000), _weights_fail())
    assert start == 1_000_100
    assert end is None


def test_pre_commit_failed_no_miners_returns_none_start():
    """When pre_commit fails AND no miner blocks, start is None."""
    start, end = compute_block_range([], _commit_fail(), _weights_ok(1_001_000))
    assert start is None
    assert end == 1_001_000


def test_all_failed_returns_none_none():
    """Fully aborted run with no usable data → (None, None)."""
    start, end = compute_block_range([], _commit_fail(), _weights_fail())
    assert start is None
    assert end is None


def test_no_weights_commit_returns_none_end():
    """When weights commit is None (e.g. early abort), end is None."""
    miners = [_miner(0, 1_000_100)]
    start, end = compute_block_range(miners, _commit_ok(1_000_000), None)
    assert start == 1_000_100
    assert end is None


def test_outcome_has_block_range_fields():
    """EpochScoringOutcome carries block_range_start / block_range_end."""
    outcome = EpochScoringOutcome(
        block_range_start=1_000_000,
        block_range_end=1_001_000,
    )
    assert outcome.block_range_start == 1_000_000
    assert outcome.block_range_end == 1_001_000
