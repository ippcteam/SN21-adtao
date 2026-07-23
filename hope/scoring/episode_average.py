"""D13 — episode-weighted moving average with exponential age decay.

Design v0.5 §6 [D13] + GAP-2 v2 (2026-07-24): a miner's standing is the
episode-age-weighted mean of their scored predictions,

    S = sum(w_i * s_i) / sum(w_i),   w_i = 0.5 ** (age_days_i / half_life)

Every episode enters at equal weight on its scoring day and decays only by
age. A "day" has no weight of its own: a 40-episode Saturday contributes 40
units against a 250-episode Tuesday's 250 — thin days self-discount in exact
proportion to how thin they are, with no volume threshold and no calendar
rule. The rejected alternative (folding per-day averages once per day) is
deliberately NOT implemented here and is guarded against in tests: it would
re-introduce day-weighting and silently change the effective half-life.

This module is pure and stateless; wiring into tier/weight placement is the
separate D7-curve step.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Optional

# GAP-2 v2 §7 — review-adjustable within [10, 14].
DEFAULT_HALF_LIFE_DAYS = 12.0
# Episodes older than this contribute ~0.13 weight and are dropped so the
# working set stays bounded and auditable (matches stream depth: day 35 is
# when a basket's last horizon lands).
DEFAULT_WINDOW_DAYS = 35
# Cold start (GAP-2 v2 §3.4): evidence mass, not calendar.
PLACEMENT_FLOOR_PREDICTIONS = 250
FULL_STANDING_PREDICTIONS = 1000


@dataclass(frozen=True)
class ScoredEpisode:
    """One scored prediction: final score in [0,1] and the day it was scored."""
    score: float
    scored_on: date


def episode_weight(age_days: float, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """w = 0.5^(age/half_life); age 0 → 1.0, age == half_life → 0.5."""
    if age_days < 0:
        raise ValueError(f"age_days must be >= 0, got {age_days}")
    return 0.5 ** (age_days / half_life_days)


def episode_weighted_average(
    episodes: Iterable[ScoredEpisode],
    as_of: date,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> Optional[float]:
    """The D13 standing: age-weighted mean over the retained window.

    Returns None when no episode falls inside the window (a miner with no
    recent scored work has no standing, which is distinct from a standing
    of zero).
    """
    num = 0.0
    den = 0.0
    for ep in episodes:
        age = (as_of - ep.scored_on).days
        if age < 0 or age > window_days:
            continue
        w = episode_weight(age, half_life_days)
        num += w * ep.score
        den += w
    return (num / den) if den > 0 else None


def scored_prediction_count(
    episodes: Iterable[ScoredEpisode], as_of: date, window_days: int = DEFAULT_WINDOW_DAYS
) -> int:
    """Evidence mass inside the window — drives the cold-start floors."""
    return sum(
        1 for ep in episodes
        if 0 <= (as_of - ep.scored_on).days <= window_days
    )


def standing(
    episodes: list[ScoredEpisode],
    as_of: date,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> dict:
    """Full placement view: average + cold-start gates (GAP-2 v2 §3.4).

    placement_eligible: may enter tier/curve placement (>= 250 predictions).
    full_standing:      no cold-start dampening      (>= 1000 predictions).
    Champion promotion additionally requires >= 14 scored DAYS — that gate is
    calendar-denominated by design ([D8] condition 3) and belongs to the
    promotion rule, not to this average.
    """
    n = scored_prediction_count(episodes, as_of, window_days)
    avg = episode_weighted_average(episodes, as_of, half_life_days, window_days)
    return {
        "average": avg,
        "scored_predictions": n,
        "placement_eligible": n >= PLACEMENT_FLOOR_PREDICTIONS,
        "full_standing": n >= FULL_STANDING_PREDICTIONS,
    }
