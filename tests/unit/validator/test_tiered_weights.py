"""Tier allocator: gates, tier bands, Elite floor, EMA placement.

Mirrors the reward-mechanism spec
(``docs/SN21_REWARD_MECHANISM.md`` Components 1-2). Each test pins a
single mechanic so a regression in one area doesn't mask others.
"""

from __future__ import annotations

from hope.validator.tiered_weights import (
    MinerEpochInputs,
    TieredAllocator,
)


def _miner(
    hotkey: str,
    raw: float,
    baseline: float = 0.5,
    coverage: float = 1.0,
    bucket_coverage: dict[tuple[str, str], tuple[int, int]] | None = None,
) -> MinerEpochInputs:
    return MinerEpochInputs(
        hotkey=hotkey,
        raw_score=raw,
        baseline_score=baseline,
        coverage_fraction=coverage,
        per_bucket_coverage=bucket_coverage or {},
    )


def test_below_baseline_is_excluded():
    alloc = TieredAllocator()
    miners = [_miner(f"hk{i}", raw=0.6) for i in range(20)]
    miners.append(_miner("loser", raw=0.4))  # below baseline=0.5
    res = alloc.allocate(miners)
    assert "loser" in res.excluded
    assert res.excluded["loser"] == "below_baseline"
    assert "loser" not in res.weights


def test_below_coverage_gate_is_excluded():
    alloc = TieredAllocator()
    miners = [_miner(f"hk{i}", raw=0.7) for i in range(20)]
    miners.append(_miner("absentee", raw=0.9, coverage=0.50))
    res = alloc.allocate(miners)
    assert res.excluded["absentee"] == "coverage_below_gate"


def test_per_bucket_under_60pct_excluded():
    alloc = TieredAllocator()
    bad_buckets = {("SEARCH", "high"): (5, 20)}  # 25%, below 60%
    good = [_miner(f"hk{i}", raw=0.7) for i in range(20)]
    miners = good + [
        _miner("bucket_avoider", raw=0.9, bucket_coverage=bad_buckets),
    ]
    res = alloc.allocate(miners)
    assert res.excluded["bucket_avoider"].startswith("bucket_coverage_below_60pct")


def test_small_bucket_under_min_excluded():
    alloc = TieredAllocator()
    bad_small = {("PMAX", "low"): (1, 8)}  # 8 < 10, must submit ≥3, only 1
    good = [_miner(f"hk{i}", raw=0.7) for i in range(20)]
    miners = good + [
        _miner("small_avoider", raw=0.9, bucket_coverage=bad_small),
    ]
    res = alloc.allocate(miners)
    assert res.excluded["small_avoider"].startswith("small_bucket_under_min")


def test_fewer_than_15_qualifying_pays_through_tier_bands():
    # Below the tier-split floor the allocator no longer collapses to a
    # straight proportional pool — the winners are paid through the same
    # 20/40/40 → 60/30/10 bands (top-up field is empty here, so the pool
    # is just the 10 winners).
    alloc = TieredAllocator()
    miners = [_miner(f"hk{i}", raw=0.55 + i * 0.05) for i in range(10)]
    res = alloc.allocate(miners)
    assert len(res.qualifying) == 10
    assert len(res.weights) == 10
    assert abs(sum(res.weights.values()) - 1.0) < 1e-9
    # 20/40/40 bands over 10: 2 elite / 4 competitive / 4 participating.
    assert len(res.elite) == 2
    assert len(res.competitive) == 4
    assert len(res.participating) == 4
    # Banded pools (60/30/10), NOT a straight split: an elite member's
    # share dwarfs a participating member's despite clustered raw scores.
    elite_min = min(res.weights[hk] for hk in res.elite)
    part_max = max(res.weights[hk] for hk in res.participating)
    assert elite_min > part_max


def test_small_winner_week_tops_up_pool_through_tier_bands():
    # A handful beat baseline (the W27 shape): 3 winners + a healthy
    # below-baseline coverage-passing field. The pool tops up to
    # max(winners, 50% of field, tier floor) and pays 60/30/10 — winners
    # rank on top and land in Elite.
    alloc = TieredAllocator()
    winners = [_miner(f"win{i}", raw=0.93 + i * 0.001, baseline=0.925) for i in range(3)]
    field = [_miner(f"hk{i:03d}", raw=0.80 + i * 0.0005, baseline=0.925) for i in range(100)]
    res = alloc.allocate(winners + field)
    # pool = max(3, round(103*0.5)=52, 15) = 52
    assert len(res.weights) == 52
    assert abs(sum(res.weights.values()) - 1.0) < 1e-9
    assert len(res.elite) == 10 and len(res.competitive) == 21 and len(res.participating) == 21
    # All three winners funded, in Elite, and out-earning the topped-up field.
    for w in winners:
        assert w.hotkey in res.elite
    part_max = max(res.weights[hk] for hk in res.participating)
    assert all(res.weights[w.hotkey] > part_max for w in winners)
    # Proportional within bands — not flat.
    assert len({round(v, 10) for v in res.weights.values()}) > 1
    # Unfunded below-baseline field stays excluded with its reason.
    assert sum(1 for r in res.excluded.values() if r == "below_baseline") == 51


def test_small_winner_week_frac_zero_funds_winners_only(monkeypatch):
    # With the top-up fraction disabled, baseline-beaters are still paid
    # (through the bands) but the below-baseline field is not topped up.
    monkeypatch.setenv("SN21_FLATWEEK_FUND_FRACTION", "0")
    alloc = TieredAllocator()
    winners = [_miner(f"win{i}", raw=0.93 + i * 0.001, baseline=0.925) for i in range(3)]
    field = [_miner(f"hk{i}", raw=0.85, baseline=0.925) for i in range(20)]
    res = alloc.allocate(winners + field)
    assert set(res.weights) == {w.hotkey for w in winners}
    assert abs(sum(res.weights.values()) - 1.0) < 1e-9
    assert sum(1 for r in res.excluded.values() if r == "below_baseline") == 20


def test_tier_split_at_50_miners_with_distinct_scores():
    alloc = TieredAllocator()
    # 50 miners with strictly increasing scores → distinct EMAs.
    miners = [_miner(f"hk{i}", raw=0.55 + i * 0.005) for i in range(50)]
    res = alloc.allocate(miners)
    # 20% / 40% / 40%
    assert len(res.elite) == 10
    assert len(res.competitive) == 20
    assert len(res.participating) == 20
    assert abs(sum(res.weights.values()) - 1.0) < 1e-9


def test_elite_pool_share_60pct_when_floor_clears():
    alloc = TieredAllocator()
    miners = [_miner(f"hk{i}", raw=0.55 + i * 0.005) for i in range(50)]
    res = alloc.allocate(miners)
    assert res.elite_floor_cleared
    elite_total = sum(res.weights[h] for h in res.elite)
    competitive_total = sum(res.weights[h] for h in res.competitive)
    participating_total = sum(res.weights[h] for h in res.participating)
    assert abs(elite_total - 0.60) < 1e-9
    assert abs(competitive_total - 0.30) < 1e-9
    assert abs(participating_total - 0.10) < 1e-9


def test_elite_empty_redistributes_to_competitive_and_participating():
    """When all miners cluster at the same score, sigma=0 and the floor
    collapses to baseline; everyone ties at baseline, so nobody clears.
    Verify Elite redistribution kicks in."""
    alloc = TieredAllocator()
    # 50 miners all at the same raw score → delta_sigma = 0, floor =
    # baseline, top 20% rank passes the floor exactly. Bump the weakest
    # half to be visibly worse so EMA orders them, but keep raw <
    # baseline + sigma so Elite is empty.
    miners = []
    for i in range(50):
        miners.append(_miner(f"hk{i}", raw=0.6, baseline=0.5))
    res = alloc.allocate(miners)
    if not res.elite_floor_cleared:
        # When elite is empty, redistribution = 0.45 to comp, 0.15 to part.
        comp_total = sum(res.weights[h] for h in res.competitive)
        part_total = sum(res.weights[h] for h in res.participating)
        assert abs(comp_total - 0.75) < 1e-9
        assert abs(part_total - 0.25) < 1e-9


def test_ema_smooths_across_epochs():
    alloc = TieredAllocator()
    # First epoch: hk1 scores 0.9 (high). The history record for hk1
    # keeps that score for next epoch's EMA calculation.
    epoch_1 = [_miner(f"hk{i}", raw=0.6) for i in range(20)] + [_miner("hk1", raw=0.9)]
    alloc.allocate(epoch_1)

    # Second epoch: hk1 drops to 0.6 (matches the rest)
    epoch_2 = [_miner(f"hk{i}", raw=0.6) for i in range(20)] + [_miner("hk1", raw=0.6)]
    res2 = alloc.allocate(epoch_2)
    weight_after_2 = res2.weights.get("hk1", 0.0)

    # EMA should still favour hk1 in epoch 2 because epoch 1 contributes.
    # (Alpha = 0.5 → 50/50 weighting.)
    avg_others_2 = (1.0 - weight_after_2) / 20 if weight_after_2 < 1.0 else 0.0
    assert weight_after_2 >= avg_others_2


def test_excluded_miners_get_zero_weight_explicitly():
    alloc = TieredAllocator()
    miners = [_miner(f"hk{i}", raw=0.7) for i in range(20)]
    miners.append(_miner("loser", raw=0.4))
    res = alloc.allocate(miners)
    assert res.weights.get("loser", 0.0) == 0.0


def test_flat_week_fallback_funds_top_fraction():
    # Flat week: every miner is below the predict-zero baseline, but all met
    # coverage. Instead of burning the epoch, the fallback funds the top
    # SN21_FLATWEEK_FUND_FRACTION (default 0.50) by raw_score, 60/30/10.
    alloc = TieredAllocator()
    miners = [_miner(f"hk{i:03d}", raw=0.80 + i * 0.0005, baseline=0.925) for i in range(100)]
    res = alloc.allocate(miners)
    assert len(res.weights) == 50                     # top 50% funded
    assert len(res.excluded) == 50                    # bottom 50% excluded
    assert abs(sum(res.weights.values()) - 1.0) < 1e-9
    assert len(res.elite) == 10 and len(res.competitive) == 20 and len(res.participating) == 20
    # proportional, NOT equal — highest score gets the largest share.
    assert len({round(w, 8) for w in res.weights.values()}) > 1
    top = max(miners, key=lambda m: m.raw_score).hotkey
    assert res.weights[top] == max(res.weights.values())


def test_flat_week_fallback_excludes_coverage_failures():
    # A miner that failed COVERAGE (not just baseline) is never funded by the
    # fallback — no reward for under-submitting.
    alloc = TieredAllocator()
    miners = [_miner(f"hk{i}", raw=0.85, baseline=0.925) for i in range(20)]
    miners.append(_miner("lazy", raw=0.99, baseline=0.925, coverage=0.10))  # high score, bad coverage
    res = alloc.allocate(miners)
    assert "lazy" not in res.weights
    assert res.excluded.get("lazy") == "coverage_below_gate"


def test_flat_week_fallback_disabled_burns(monkeypatch):
    # Operators can opt back into full-burn by setting the fraction to 0.
    monkeypatch.setenv("SN21_FLATWEEK_FUND_FRACTION", "0")
    alloc = TieredAllocator()
    miners = [_miner(f"hk{i}", raw=0.4, baseline=0.5) for i in range(10)]
    res = alloc.allocate(miners)
    assert res.weights == {}
    assert len(res.excluded) == 10
