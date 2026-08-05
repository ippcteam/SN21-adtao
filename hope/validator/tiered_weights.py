"""Tiered emission allocator (SN21_REWARD_MECHANISM.md Components 1-2).

Implements the launch reward mechanism:

* **Participation gate.** Beat the conditional-prior baseline, ≥80%
  episode coverage, and per-bucket coverage thresholds.
* **EMA tier placement.** Four-epoch EMA with alpha = 0.5; cold-start
  rules for epochs 1-3.
* **Tier bands.** Elite (top 20%, +1·sigma quality floor) → 60%;
  Competitive (next 40%) → 30%; Participating (bottom 40%) → 10%.
* **Elite floor redistribution.** When the top-20% does not clear
  baseline + 1·sigma, the Elite pool is redistributed 30:10 to
  Competitive:Participating in the same proportion as the rest of the
  pool.
* **Single-pool fallback.** Fewer than 15 qualifying miners share one
  proportional pool with no tier split.

The output is a ``dict[hotkey -> weight]`` summing to 1.0 over
qualifying miners. Failed-gate miners get exactly 0.0. Burn is applied
on top of this allocation by the existing ``WeightSetter`` path.
"""

from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# EMA tier-placement smoothing — four-epoch window with alpha = 0.5.
TIER_EMA_ALPHA = 0.5

# Coverage gate thresholds (overall and per-bucket).
COVERAGE_GATE_FRACTION = 0.80
PER_BUCKET_GATE_FRACTION = 0.60
PER_BUCKET_MIN_EPISODES_FOR_FRACTION = 10
PER_BUCKET_MIN_SUBMISSIONS_FOR_SMALL_BUCKETS = 3

# Elite quality floor: EMA must clear baseline + (k * sigma).
ELITE_K_SIGMA = 1.0

# Tier pool shares.
ELITE_POOL_SHARE = 0.60
COMPETITIVE_POOL_SHARE = 0.30
PARTICIPATING_POOL_SHARE = 0.10

# When Elite is empty, redistribute its share to Competitive:Participating
# in 30:10 of its 60% — i.e., Competitive picks up 0.45, Participating 0.15.
ELITE_REDISTRIBUTION_TO_COMPETITIVE = 0.30 / (0.30 + 0.10)
ELITE_REDISTRIBUTION_TO_PARTICIPATING = 0.10 / (0.30 + 0.10)

# Below this many qualifying miners, skip tier split and use a single
# proportional pool.
MIN_MINERS_FOR_TIER_SPLIT = 15

# Flat-week fallback. In a statistically flat outcome week, "predict-zero" can
# be near-optimal, so a healthy field can score just under the predict-zero
# baseline and few or ZERO miners clear the participation gate — which would
# burn (nearly) the whole epoch. Whenever fewer than MIN_MINERS_FOR_TIER_SPLIT
# miners beat baseline, the funded pool is topped up from the COVERAGE-passing
# field (ranked by raw_score; baseline-beaters always funded and on top) to
# max(all winners, FLATWEEK_FUND_FRACTION of the eligible field, the tier-split
# floor), and paid through the same 20/40/40 → 60/30/10
# Elite/Competitive/Participating bands, proportional by score within each band
# — never a straight/flat split. Coverage failures are still excluded (no
# reward for under-submitting). Tunable via SN21_FLATWEEK_FUND_FRACTION; set to
# 0 to disable the below-baseline top-up (winners-only when any exist,
# full burn on a zero-winner week).
FLATWEEK_FUND_FRACTION = 0.50


@dataclass
class MinerEpochInputs:
    """Per-miner inputs for one epoch's tiered allocation.

    All fields are derived from the validator's epoch scoring + the
    chain-anchored release artifact. ``baseline_score`` is the score the
    conditional-prior (or fall-through) baseline achieves on the same
    episode set; the gate requires ``raw_score > baseline_score``.

    ``per_bucket_coverage`` is keyed by ``(campaign_type,
    measurement_resolution)`` — the same bucket key used in the
    epoch-structure spec — with values
    ``(submitted_episodes, total_episodes_in_bucket)``.
    """

    hotkey: str
    raw_score: float
    baseline_score: float
    coverage_fraction: float
    per_bucket_coverage: dict[tuple[str, str], tuple[int, int]] = field(default_factory=dict)


@dataclass
class TierAllocationResult:
    """Outcome of a tiered allocation pass for one epoch."""

    weights: dict[str, float]                # hotkey -> normalized weight, sums to 1.0
    qualifying: list[str]                    # hotkeys that passed the gate
    excluded: dict[str, str]                 # hotkey -> reason
    elite: list[str] = field(default_factory=list)
    competitive: list[str] = field(default_factory=list)
    participating: list[str] = field(default_factory=list)
    elite_floor_cleared: bool = True


class TieredAllocator:
    """Stateful EMA tracker + tier allocator across epochs."""

    def __init__(self) -> None:
        # Last four (most recent first) raw scores per hotkey, used to compute
        # the four-epoch tier-placement EMA. Cleared per hotkey when the EMA
        # falls to zero coverage to keep the cold-start path honest.
        self._score_history: dict[str, list[float]] = {}

    def _ema_score(self, hotkey: str, current: float) -> float:
        """Four-epoch EMA with alpha = 0.5 (cold-start aware).

        Spec:
            EMA = 0.5*current + 0.25*(t-1) + 0.125*(t-2) + 0.0625*(t-3)

        Cold start:
            epoch 1 → current only
            epoch 2 → simple two-epoch average
            epoch 3+ → full EMA over available history
        """
        history = self._score_history.get(hotkey, [])
        # history is most-recent-first BEFORE this epoch — the current epoch
        # has not yet been pushed.
        if not history:
            return current
        if len(history) == 1:
            return (current + history[0]) / 2.0

        weights = [0.5, 0.25, 0.125, 0.0625]
        seq = [current, *history[:3]]  # most recent first, up to 4 entries
        used = weights[: len(seq)]
        norm = sum(used)
        return sum(w * s for w, s in zip(used, seq)) / norm

    def _record_score(self, hotkey: str, current: float) -> None:
        history = self._score_history.setdefault(hotkey, [])
        history.insert(0, current)
        del history[4:]

    def _passes_gate(self, m: MinerEpochInputs) -> tuple[bool, str | None]:
        if m.coverage_fraction < COVERAGE_GATE_FRACTION:
            return False, "coverage_below_gate"
        if m.raw_score <= m.baseline_score:
            return False, "below_baseline"

        for bucket, (submitted, total) in m.per_bucket_coverage.items():
            if total >= PER_BUCKET_MIN_EPISODES_FOR_FRACTION:
                if total > 0 and (submitted / total) < PER_BUCKET_GATE_FRACTION:
                    return False, f"bucket_coverage_below_60pct:{bucket[0]}:{bucket[1]}"
            else:
                # Small bucket: submit at least 3 (or all if fewer than 3).
                required = min(PER_BUCKET_MIN_SUBMISSIONS_FOR_SMALL_BUCKETS, total)
                if submitted < required:
                    return False, f"small_bucket_under_min:{bucket[0]}:{bucket[1]}"
        return True, None

    def _flat_week_fallback(
        self,
        inputs: list[MinerEpochInputs],
        excluded: dict[str, str],
        winners: list[MinerEpochInputs] | None = None,
    ) -> TierAllocationResult:
        """Fund a tiered pool when fewer than the tier-split floor beat predict-zero.

        Covers both the zero-winner flat week AND the small-winner week (a
        handful beat baseline — below ``MIN_MINERS_FOR_TIER_SPLIT``). In both
        cases the funded pool is topped up from miners whose ONLY gate failure
        was ``below_baseline`` (they met coverage and bucket gates — real
        participants who just couldn't beat a flat week). Coverage/bucket
        failures stay excluded. ``winners`` (baseline-beaters) are always
        funded and — having raw_score above baseline by construction — rank at
        the top of the merged pool. The pool is paid through the same
        20/40/40 → 60/30/10 Elite/Competitive/Participating bands as a normal
        week, proportional by score within each band, so top predictions keep
        a meaningfully larger share (never a straight/flat split). Pool size =
        max(all winners, ``SN21_FLATWEEK_FUND_FRACTION`` of the eligible
        field, the tier-split floor).
        """
        winners = winners or []
        frac = float(os.environ.get("SN21_FLATWEEK_FUND_FRACTION", str(FLATWEEK_FUND_FRACTION)))
        if frac <= 0 and not winners:
            logger.warning("flat week: 0 miners beat baseline and fallback disabled — full burn")
            return TierAllocationResult(
                weights={}, qualifying=[], excluded=excluded, elite_floor_cleared=False
            )

        below = sorted(
            (m for m in inputs if excluded.get(m.hotkey) == "below_baseline" and m.raw_score > 0),
            key=lambda m: m.raw_score,
            reverse=True,
        )
        # Winners first (all funded), then the below-baseline field by score.
        # Winners' raw_score > baseline >= below-baseline scores, so this is
        # equivalent to one merged sort by raw_score descending.
        eligible = sorted(winners, key=lambda m: m.raw_score, reverse=True) + below
        if not eligible:
            return TierAllocationResult(
                weights={}, qualifying=[], excluded=excluded, elite_floor_cleared=False
            )

        if frac <= 0:
            # Top-up disabled: fund the baseline-beaters only (the zero-winner
            # case already returned full-burn above).
            n_fund = max(len(winners), 1)
        else:
            n_fund = max(
                len(winners),                                   # every baseline-beater is funded
                round(len(eligible) * frac),                    # the fallback fraction of the field
                min(MIN_MINERS_FOR_TIER_SPLIT, len(eligible)),  # a healthy pool for the tier bands
                1,
            )
        funded = eligible[:n_fund]
        for m in funded:                       # funded miners are no longer "excluded"
            excluded.pop(m.hotkey, None)

        n_elite = max(1, round(n_fund * 0.20))
        n_comp = round(n_fund * 0.40)
        bands = [
            ("elite", funded[:n_elite], ELITE_POOL_SHARE),
            ("competitive", funded[n_elite:n_elite + n_comp], COMPETITIVE_POOL_SHARE),
            ("participating", funded[n_elite + n_comp:], PARTICIPATING_POOL_SHARE),
        ]
        weights: dict[str, float] = {}
        tiers: dict[str, list[str]] = {"elite": [], "competitive": [], "participating": []}
        for name, band, pool in bands:
            total = sum(m.raw_score for m in band)
            for m in band:
                weights[m.hotkey] = (pool * m.raw_score / total) if total > 0 else 0.0
                tiers[name].append(m.hotkey)

        # Renormalize to sum to 1.0 (covers the case where a band is empty at
        # very small n_fund, so its pool share isn't silently dropped).
        wsum = sum(weights.values())
        if wsum > 0:
            weights = {hk: w / wsum for hk, w in weights.items()}

        logger.warning(
            "flat week: %d/%d beat baseline (tier-split floor %d); fallback funded "
            "top %d through 60/30/10 bands (frac=%.2f)",
            len(winners), len(inputs), MIN_MINERS_FOR_TIER_SPLIT, n_fund, frac,
        )
        return TierAllocationResult(
            weights=weights,
            qualifying=[m.hotkey for m in funded],
            excluded=excluded,
            elite=tiers["elite"],
            competitive=tiers["competitive"],
            participating=tiers["participating"],
            elite_floor_cleared=False,
        )

    def allocate(self, inputs: list[MinerEpochInputs]) -> TierAllocationResult:
        """Run the gate, EMA tier placement, and pool allocation."""
        weights: dict[str, float] = {}
        qualifying: list[str] = []
        excluded: dict[str, str] = {}

        gate_passed: list[tuple[MinerEpochInputs, float]] = []
        for m in inputs:
            ok, reason = self._passes_gate(m)
            if not ok:
                excluded[m.hotkey] = reason or "gate_failed"
                continue
            ema = self._ema_score(m.hotkey, m.raw_score)
            gate_passed.append((m, ema))
            qualifying.append(m.hotkey)

        # Always update history AFTER computing EMA so the current epoch
        # contributes to next epoch's window.
        for m in inputs:
            self._record_score(m.hotkey, m.raw_score)

        if not gate_passed:
            # Flat week: nobody beat predict-zero. Rather than burn the whole
            # epoch, fund the top fraction of the coverage-passing field
            # through the standard 60/30/10 tier bands.
            return self._flat_week_fallback(inputs, excluded)

        # Small-winner week: fewer baseline-beaters than the tier-split floor.
        # A straight proportional pool over a handful of clustered scores pays
        # near-flat and starves the healthy coverage-passing field, so instead
        # top the pool up from the below-baseline field and pay everyone
        # through the same 20/40/40 → 60/30/10 bands. Baseline-beaters rank at
        # the top of the merged pool (their scores clear baseline), landing in
        # Elite/Competitive by construction.
        if len(gate_passed) < MIN_MINERS_FOR_TIER_SPLIT:
            return self._flat_week_fallback(
                inputs, excluded, winners=[m for m, _ in gate_passed],
            )

        # Sort by EMA descending for tier placement.
        ordered = sorted(gate_passed, key=lambda pair: pair[1], reverse=True)
        n = len(ordered)
        n_elite_candidates = max(1, round(n * 0.20))
        n_competitive = max(1, round(n * 0.40))

        # Elite quality floor: per the reward doc, the floor uses the
        # baseline value of qualifying miners' (raw - baseline). We
        # approximate as: floor = mean_baseline + k * sigma_of_(raw -
        # baseline). When sigma is zero (all identical) the floor
        # collapses to baseline.
        deltas = [m.raw_score - m.baseline_score for m, _ in ordered]
        delta_sigma = _stdev(deltas)
        baseline_avg = sum(m.baseline_score for m, _ in ordered) / len(ordered)
        elite_floor = baseline_avg + ELITE_K_SIGMA * delta_sigma

        elite_candidates = ordered[:n_elite_candidates]
        elite = [
            m.hotkey for m, ema in elite_candidates if ema >= elite_floor
        ]
        elite_floor_cleared = bool(elite)

        if elite_floor_cleared:
            competitive = [m.hotkey for m, _ in ordered[n_elite_candidates : n_elite_candidates + n_competitive]]
            participating = [m.hotkey for m, _ in ordered[n_elite_candidates + n_competitive :]]
            elite_share = ELITE_POOL_SHARE
            competitive_share = COMPETITIVE_POOL_SHARE
            participating_share = PARTICIPATING_POOL_SHARE
        else:
            elite = []
            # Elite pool redistributes 30:10 to Competitive:Participating —
            # the top 60% of miners shifts into the redistributed pool, and
            # the bottom 40% holds Participating's redistributed share.
            competitive = [
                m.hotkey for m, _ in ordered[: n_elite_candidates + n_competitive]
            ]
            participating = [
                m.hotkey for m, _ in ordered[n_elite_candidates + n_competitive :]
            ]
            elite_share = 0.0
            competitive_share = (
                COMPETITIVE_POOL_SHARE
                + ELITE_POOL_SHARE * ELITE_REDISTRIBUTION_TO_COMPETITIVE
            )
            participating_share = (
                PARTICIPATING_POOL_SHARE
                + ELITE_POOL_SHARE * ELITE_REDISTRIBUTION_TO_PARTICIPATING
            )

        miner_score = {m.hotkey: m.raw_score for m, _ in ordered}

        weights.update(_proportional_pool(elite, miner_score, elite_share))
        weights.update(_proportional_pool(competitive, miner_score, competitive_share))
        weights.update(_proportional_pool(participating, miner_score, participating_share))

        total = sum(weights.values())
        if total > 0 and abs(total - 1.0) > 1e-9:
            # Numerical drift across three pool normalizations — re-normalize.
            weights = {k: v / total for k, v in weights.items()}

        return TierAllocationResult(
            weights=weights,
            qualifying=qualifying,
            excluded=excluded,
            elite=elite,
            competitive=competitive,
            participating=participating,
            elite_floor_cleared=elite_floor_cleared,
        )


def _proportional_pool(
    members: list[str],
    miner_score: dict[str, float],
    pool_share: float,
) -> dict[str, float]:
    """Distribute ``pool_share`` across ``members`` proportionally to current-
    epoch score (the within-tier rule from the reward spec)."""
    if not members or pool_share <= 0:
        return {}
    total = sum(miner_score[h] for h in members)
    if total <= 0:
        # All members have zero current-epoch score — split evenly so the pool
        # share isn't lost.
        even = pool_share / len(members)
        return {h: even for h in members}
    return {h: pool_share * (miner_score[h] / total) for h in members}


def _stdev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    variance = sum((v - mean) ** 2 for v in values) / (len(values) - 1)
    return math.sqrt(variance)
