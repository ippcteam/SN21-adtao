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
            return TierAllocationResult(
                weights={},
                qualifying=[],
                excluded=excluded,
                elite_floor_cleared=False,
            )

        # Single-pool fallback when fewer than 15 qualifying miners.
        if len(gate_passed) < MIN_MINERS_FOR_TIER_SPLIT:
            total = sum(m.raw_score for m, _ in gate_passed)
            if total <= 0:
                # Theoretically unreachable because the gate already required
                # raw_score > baseline_score >= 0 in the launch spec, but stay
                # defensive.
                weights = {m.hotkey: 1.0 / len(gate_passed) for m, _ in gate_passed}
            else:
                weights = {m.hotkey: m.raw_score / total for m, _ in gate_passed}
            return TierAllocationResult(
                weights=weights,
                qualifying=qualifying,
                excluded=excluded,
                competitive=qualifying,
                elite_floor_cleared=False,
            )

        # Sort by EMA descending for tier placement.
        ordered = sorted(gate_passed, key=lambda pair: pair[1], reverse=True)
        n = len(ordered)
        n_elite_candidates = max(1, int(round(n * 0.20)))
        n_competitive = max(1, int(round(n * 0.40)))

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
