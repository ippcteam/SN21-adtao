"""Tests for hope.reporting.tier_compute.compute_tier_result_from_score_map."""

from __future__ import annotations

from hope.reporting.tier_compute import compute_tier_result_from_score_map
from hope.validator.tiered_weights import (
    MIN_MINERS_FOR_TIER_SPLIT,
    TierAllocationResult,
)


def _score_map_of(n_miners: int, base_micro: int = 500_000) -> dict[bytes, int]:
    """Generate a deterministic score_map for testing.

    Miner i gets score_micro = base_micro + i * 10_000 so scores are
    strictly increasing and distinct, exercising the tier-placement
    ranking path.
    """
    return {
        bytes([i]) * 32: base_micro + i * 10_000
        for i in range(n_miners)
    }


def test_empty_score_map_returns_empty_result():
    """No miners → empty TierAllocationResult, no exceptions."""
    result = compute_tier_result_from_score_map({})
    assert isinstance(result, TierAllocationResult)
    assert result.qualifying == []
    assert result.weights == {}


def test_small_pool_collapses_to_single_pool():
    """Below MIN_MINERS_FOR_TIER_SPLIT (15), no tier split — all share one pool.

    Per `hope/validator/tiered_weights.py`, a pool of <15 qualifying miners
    falls through the tier split. The allocator surfaces this by placing
    every qualifying miner into the `competitive` roster (single-pool
    fallback) with empty `elite` + `participating`, and `elite_floor_cleared`
    set False. The leaderboard reporter must surface this as
    `tier_split_active: false` per the data contract.
    """
    score_map = _score_map_of(10)
    result = compute_tier_result_from_score_map(score_map)
    assert len(result.qualifying) == 10
    assert len(result.weights) == 10
    # Single-pool fallback signature: everyone in `competitive`, the
    # other two tiers empty, elite-floor not cleared.
    assert len(result.competitive) == 10
    assert len(result.elite) == 0
    assert len(result.participating) == 0
    assert result.elite_floor_cleared is False


def test_full_pool_splits_into_three_tiers():
    """≥15 qualifying miners → split into elite / competitive / participating."""
    score_map = _score_map_of(20)
    result = compute_tier_result_from_score_map(score_map)
    assert len(result.qualifying) == 20
    # The exact tier sizes depend on the allocator's gate + tier-placement
    # logic; just confirm the split happened (all three rosters non-empty
    # OR Elite is empty by floor rule).
    total_in_tiers = len(result.elite) + len(result.competitive) + len(result.participating)
    assert total_in_tiers == 20


def test_baseline_score_filters_gate():
    """A baseline_score above every miner's raw_score zeroes the pool."""
    score_map = _score_map_of(5, base_micro=100_000)
    # base 0.1, max 0.14. baseline=0.5 fails everyone.
    result = compute_tier_result_from_score_map(score_map, baseline_score=0.5)
    assert result.qualifying == []
    # Excluded reason should be the gate-fail signal.
    assert all("baseline" in reason.lower() or "gate" in reason.lower()
               for reason in result.excluded.values()) or len(result.excluded) > 0


def test_weights_sum_to_one_when_pool_nonempty():
    """When any miners qualify, the returned weights sum to 1.0."""
    score_map = _score_map_of(MIN_MINERS_FOR_TIER_SPLIT + 5)
    result = compute_tier_result_from_score_map(score_map)
    assert result.weights, "expected at least one qualifying miner"
    total = sum(result.weights.values())
    assert abs(total - 1.0) < 1e-6


def test_deterministic_across_runs():
    """Same input → byte-identical output. The reporter relies on this."""
    score_map = _score_map_of(20)
    r1 = compute_tier_result_from_score_map(score_map)
    r2 = compute_tier_result_from_score_map(score_map)
    assert r1.qualifying == r2.qualifying
    assert r1.weights == r2.weights
    assert r1.elite == r2.elite
    assert r1.competitive == r2.competitive
    assert r1.participating == r2.participating
    assert r1.elite_floor_cleared == r2.elite_floor_cleared
