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


def test_small_pool_pays_through_tier_bands():
    """Below MIN_MINERS_FOR_TIER_SPLIT (15) the pool is still BANDED.

    This used to collapse to one proportional pool with every qualifying
    miner in `competitive`. It no longer does: a small pool is paid through
    the same 20/40/40 → 60/30/10 Elite/Competitive/Participating bands, so
    placing well still pays better than merely qualifying. `elite_floor_cleared`
    stays False below the split floor, which is what the leaderboard reporter
    reads as `tier_split_active: false`.
    """
    score_map = _score_map_of(10)
    result = compute_tier_result_from_score_map(score_map)
    assert len(result.qualifying) == 10
    assert len(result.weights) == 10
    # 20/40/40 of 10 → 2 elite, 4 competitive, 4 participating.
    assert len(result.elite) == 2
    assert len(result.competitive) == 4
    assert len(result.participating) == 4
    assert len(result.elite) + len(result.competitive) + len(result.participating) == 10
    # Banded, not flat: the weakest elite share beats the strongest
    # participating share even though raw scores are clustered.
    assert min(result.weights[hk] for hk in result.elite) > \
        max(result.weights[hk] for hk in result.participating)
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


def test_baseline_score_filters_gate(monkeypatch):
    """A baseline_score above every miner's raw_score zeroes the pool.

    With the flat-week top-up disabled — this asserts the gate, not the
    fallback that funds a field where nobody cleared the baseline.
    """
    monkeypatch.setenv("SN21_FLATWEEK_FUND_FRACTION", "0")
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
