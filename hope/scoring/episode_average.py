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

import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

# GAP-2 v2 §7 — review-adjustable within [10, 14].
DEFAULT_HALF_LIFE_DAYS = 12.0

# Standing-method parameters (rule amendment of 2026-09-04, published before
# enabled). Both are environment values so the published number always
# matches what runs; the defaults reproduce the rule in force before the
# amendment exactly.
#   SN21_STANDING_HALF_LIFE_DAYS  age half-life of an entry (days)
#   SN21_STANDING_PRIOR_MASS      evidence mass of the prior the average is
#                                 shrunk toward (0 = plain weighted mean)
# Episodes older than this contribute ~0.13 weight and are dropped so the
# working set stays bounded and auditable (matches stream depth: day 35 is
# when a basket's last horizon lands).
DEFAULT_WINDOW_DAYS = 35

HALF_LIFE_ENV = "SN21_STANDING_HALF_LIFE_DAYS"
PRIOR_MASS_ENV = "SN21_STANDING_PRIOR_MASS"
WINDOW_ENV = "SN21_STANDING_WINDOW_DAYS"


def window_from_env(environ=os.environ) -> int:
    try:
        v = int((environ.get(WINDOW_ENV) or "").strip())
        return v if v > 0 else DEFAULT_WINDOW_DAYS
    except (TypeError, ValueError):
        return DEFAULT_WINDOW_DAYS


def half_life_from_env(environ=os.environ) -> float:
    try:
        v = float((environ.get(HALF_LIFE_ENV) or "").strip())
        return v if v > 0 else DEFAULT_HALF_LIFE_DAYS
    except (TypeError, ValueError):
        return DEFAULT_HALF_LIFE_DAYS


def prior_mass_from_env(environ=os.environ) -> float:
    try:
        v = float((environ.get(PRIOR_MASS_ENV) or "").strip())
        return v if v > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


# The three parameters above are published together with the episode-relative
# standing and take effect on ITS date (SN21_STANDING_EFFECTIVE_FROM). Before
# that date the rule in force keeps its own values whatever the environment
# holds, so the whole configuration can be set ahead of the announced day
# without moving a single standing early. The *_in_force readers are what
# every consumer of the average uses; the *_from_env readers above are the
# raw configured values.
MODE_ENV = "SN21_STANDING_MODE"
EFFECTIVE_FROM_ENV = "SN21_STANDING_EFFECTIVE_FROM"
MODE_EPISODE_RELATIVE = "episode_relative"


def amendment_in_force(environ=os.environ, day: date | None = None) -> bool:
    """True when the episode-relative standing (and its parameters) is in
    force on `day` (today when None): the mode is configured AND the
    effective date, if one is set, has arrived. Pure."""
    if (environ.get(MODE_ENV) or "").strip().lower() != MODE_EPISODE_RELATIVE:
        return False
    raw = (environ.get(EFFECTIVE_FROM_ENV) or "").strip()
    if not raw:
        return True
    try:
        start = date.fromisoformat(raw)
    except ValueError:
        return True
    return (day or date.today()) >= start


def half_life_in_force(environ=os.environ, day: date | None = None) -> float:
    return half_life_from_env(environ) if amendment_in_force(environ, day) else DEFAULT_HALF_LIFE_DAYS


# ---- rule amendment 2026-09-14: "current model, current form" --------------
#
# Entries age from the day the PREDICTION was made, not the day its outcome
# settled, so a replaced model's late-settling results do not enter a standing
# as fresh evidence. The window widens so the 28-day horizon (which lands at
# age 31) still counts, and the prior toward the field is lowered because the
# effective evidence mass under prediction-day ages is smaller. Entries from
# a hotkey's previous model count at a fraction once its current model has
# the placement floor's worth of evidence (hope.scoring.model_epoch).
#
# Wired like every other published change: announced first, applied from a
# date. Unset = the settle-day rule exactly as before this code existed.
AGE_BASIS_ENV = "SN21_STANDING_AGE_BASIS"
AGE_BASIS_EFFECTIVE_FROM_ENV = "SN21_STANDING_AGE_BASIS_EFFECTIVE_FROM"
AGE_BASIS_SETTLE = "settle_day"
AGE_BASIS_PREDICTION = "prediction_day"
PREDICTION_BASIS_WINDOW_DAYS = 42
PREDICTION_BASIS_PRIOR_MASS = 100.0
PREDICTION_BASIS_WINDOW_ENV = "SN21_STANDING_WINDOW_DAYS_V2"
PREDICTION_BASIS_PRIOR_ENV = "SN21_STANDING_PRIOR_MASS_V2"
PREVIOUS_MODEL_WEIGHT = 0.25
PREVIOUS_MODEL_THRESHOLD_MASS = 250.0
PREVIOUS_MODEL_WEIGHT_ENV = "SN21_PREVIOUS_MODEL_WEIGHT"
PREVIOUS_MODEL_THRESHOLD_ENV = "SN21_PREVIOUS_MODEL_THRESHOLD"
# Older receipts carry no prediction day. Where the executor holds the basket
# map for the episode the day is exact; otherwise it is derived from the
# settle schedule: finalized_on = basket day + 1 + horizon + settling window.
# The settling window is what the operator platform RUNS (two days, checked
# against the receipts on 2026-09-14: 10 / 17 / 31 days after the basket for
# the 7 / 14 / 28-day horizons), not the seven the scoring doc carried until
# then; a wrong lag dated every pre-amendment entry five days too early.
OUTCOME_SETTLING_WINDOW_ENV = "SN21_OUTCOME_SETTLING_WINDOW_DAYS"
DEFAULT_OUTCOME_SETTLING_WINDOW_DAYS = 2


def settle_lag_days(environ=os.environ) -> int:
    """Days from the basket day to finalized_on, beyond the horizon."""
    try:
        v = int((environ.get(OUTCOME_SETTLING_WINDOW_ENV) or "").strip())
        settle = v if v >= 0 else DEFAULT_OUTCOME_SETTLING_WINDOW_DAYS
    except (TypeError, ValueError):
        settle = DEFAULT_OUTCOME_SETTLING_WINDOW_DAYS
    return 1 + settle


# ---- rule amendment 2026-09-17: "current model, current form" (as adopted) --
#
# Entries keep settle-day ages, the published half-life, window and prior.
# What changes: a hotkey's previous-model entries are weighted down as its
# current model shows evidence (linear ramp, hope.scoring.model_epoch), and
# the field mean counts one hotkey per copy group. The prediction-day basis
# above stays available but is not part of the adopted amendment: with a
# 7-day half-life it cut the 28-day horizon's share of a standing from 29%
# to 6% (miner review, 15 September 2026).
MODEL_EPOCH_ENV = "SN21_STANDING_MODEL_EPOCH"
MODEL_EPOCH_EFFECTIVE_FROM_ENV = "SN21_STANDING_MODEL_EPOCH_EFFECTIVE_FROM"


def model_epoch_effective_from(environ=os.environ) -> date | None:
    raw = (environ.get(MODEL_EPOCH_EFFECTIVE_FROM_ENV) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def model_epoch_in_force(environ=os.environ, day: date | None = None) -> bool:
    """True when the previous-model discount and the one-per-copy-group field
    mean apply on `day`: the relative amendment is in force, the switch is
    on, and its effective date (if any) has arrived. Pure."""
    if not amendment_in_force(environ, day):
        return False
    if (environ.get(MODEL_EPOCH_ENV) or "").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    start = model_epoch_effective_from(environ)
    if start is None:
        return True
    return (day or date.today()) >= start


def age_basis_effective_from(environ=os.environ) -> date | None:
    raw = (environ.get(AGE_BASIS_EFFECTIVE_FROM_ENV) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def prediction_basis_in_force(environ=os.environ, day: date | None = None) -> bool:
    """True when entries age from their prediction day on `day`: the
    relative amendment is in force, the basis is configured, and its
    effective date (if any) has arrived. Pure."""
    if not amendment_in_force(environ, day):
        return False
    if (environ.get(AGE_BASIS_ENV) or "").strip().lower() != AGE_BASIS_PREDICTION:
        return False
    start = age_basis_effective_from(environ)
    if start is None:
        return True
    return (day or date.today()) >= start


def age_basis_in_force(environ=os.environ, day: date | None = None) -> str:
    return AGE_BASIS_PREDICTION if prediction_basis_in_force(environ, day) else AGE_BASIS_SETTLE


def _float_env(environ, name: str, default: float, minimum: float = 0.0) -> float:
    try:
        v = float((environ.get(name) or "").strip())
        return v if v >= minimum else default
    except (TypeError, ValueError):
        return default


def previous_model_weight(environ=os.environ) -> float:
    v = _float_env(environ, PREVIOUS_MODEL_WEIGHT_ENV, PREVIOUS_MODEL_WEIGHT)
    return v if 0.0 <= v <= 1.0 else PREVIOUS_MODEL_WEIGHT


def previous_model_threshold(environ=os.environ) -> float:
    return _float_env(environ, PREVIOUS_MODEL_THRESHOLD_ENV, PREVIOUS_MODEL_THRESHOLD_MASS)


def window_in_force(environ=os.environ, day: date | None = None) -> int:
    if prediction_basis_in_force(environ, day):
        try:
            v = int((environ.get(PREDICTION_BASIS_WINDOW_ENV) or "").strip())
            return v if v > 0 else PREDICTION_BASIS_WINDOW_DAYS
        except (TypeError, ValueError):
            return PREDICTION_BASIS_WINDOW_DAYS
    return window_from_env(environ) if amendment_in_force(environ, day) else DEFAULT_WINDOW_DAYS


def prior_mass_in_force(environ=os.environ, day: date | None = None) -> float:
    if prediction_basis_in_force(environ, day):
        return _float_env(environ, PREDICTION_BASIS_PRIOR_ENV, PREDICTION_BASIS_PRIOR_MASS)
    return prior_mass_from_env(environ) if amendment_in_force(environ, day) else 0.0


def _floor_from_env(name: str, default: int) -> int:
    """A published cold-start floor, overridable for the first-cycle bootstrap.

    Steady state is `default` (250 / 1000). During the reconstruction bootstrap
    the settled evidence is thin by construction, so the floor is temporarily
    lowered via env and ramps back to `default` as daily volume accumulates —
    disclosed in SN21_REWARDS.md so the published number always matches what
    runs. Read at import; the executor sets it before the process starts.
    """
    try:
        v = int((os.environ.get(name) or "").strip())
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


# Cold start (GAP-2 v2 §3.4): evidence mass, not calendar.
PLACEMENT_FLOOR_PREDICTIONS = _floor_from_env("SN21_PLACEMENT_FLOOR_PREDICTIONS", 250)
FULL_STANDING_PREDICTIONS = _floor_from_env("SN21_FULL_STANDING_PREDICTIONS", 1000)


@dataclass(frozen=True)
class ScoredEpisode:
    """One scored entry: final score in [0,1], the day it was scored, and an
    entry weight. Under the daily stream (E1), entries are per-(episode,
    horizon) with horizon blend weights summing to 1.0 per episode — so
    `weight` defaults to 1.0 and legacy whole-episode callers are unchanged."""
    score: float
    scored_on: date
    weight: float = 1.0
    # The basket day the prediction was made on, when the loader knows it.
    # Read by the previous-model discount (rule amendment 2026-09-17) to
    # decide which of a hotkey's models produced the entry. None = unknown.
    predicted_on: date | None = None
    # The day the entry's age is measured from when it differs from the day
    # it was scored: the PREDICTION day under the prediction-day basis (rule
    # amendment 2026-09-14). None = age from scored_on, the settle-day rule.
    # scored_on itself keeps its meaning everywhere else (tenure counts
    # distinct settle days; the receipt is dated by it).
    aged_from: date | None = None

    @property
    def age_day(self) -> date:
        return self.aged_from or self.scored_on


def episode_weight(age_days: float, half_life_days: float = DEFAULT_HALF_LIFE_DAYS) -> float:
    """w = 0.5^(age/half_life); age 0 → 1.0, age == half_life → 0.5."""
    if age_days < 0:
        raise ValueError(f"age_days must be >= 0, got {age_days}")
    return 0.5 ** (age_days / half_life_days)


def episode_weighted_average(
    episodes: Iterable[ScoredEpisode],
    as_of: date,
    half_life_days: float | None = None,
    window_days: int | None = None,
    prior_mass: float | None = None,
    prior_value: float = 0.0,
) -> float | None:
    """The D13 standing: age-weighted mean over the retained window.

    Returns None when no episode falls inside the window (a miner with no
    recent scored work has no standing, which is distinct from a standing
    of zero).

    `prior_mass` > 0 shrinks the average toward `prior_value` with that much
    evidence mass behind the prior (a Bayesian average): a miner with little
    evidence sits near the prior and moves out only as evidence accumulates,
    so thin evidence cannot produce a leader. Mass 0 is the plain weighted
    mean. `half_life_days`, `window_days` and `prior_mass` default to the
    values IN FORCE on `as_of` (half_life_in_force / window_in_force /
    prior_mass_in_force): the configured ones once the amendment's date has
    arrived, the published defaults before it.
    """
    if half_life_days is None:
        half_life_days = half_life_in_force(day=as_of)
    if prior_mass is None:
        prior_mass = prior_mass_in_force(day=as_of)
    if window_days is None:
        window_days = window_in_force(day=as_of)
    num = 0.0
    den = 0.0
    for ep in episodes:
        age = (as_of - ep.age_day).days
        if age < 0 or age > window_days:
            continue
        w = episode_weight(age, half_life_days) * ep.weight
        num += w * ep.score
        den += w
    if den <= 0:
        return None
    return (num + prior_mass * prior_value) / (den + prior_mass)


def scored_prediction_count(
    episodes: Iterable[ScoredEpisode], as_of: date, window_days: int | None = None
) -> float:
    """Evidence MASS inside the window — drives the cold-start floors.
    Weighted entries count by weight (three horizon-entries of one episode
    sum to 1.0 prediction, not 3), so the 250/1000 floors keep their
    episode-denominated meaning under the daily stream."""
    if window_days is None:
        window_days = window_in_force(day=as_of)
    return sum(
        ep.weight for ep in episodes
        if 0 <= (as_of - ep.age_day).days <= window_days
    )


def standing(
    episodes: list[ScoredEpisode],
    as_of: date,
    half_life_days: float | None = None,
    window_days: int | None = None,
    prior_mass: float | None = None,
    prior_value: float = 0.0,
) -> dict:
    """Full placement view: average + cold-start gates (GAP-2 v2 §3.4).

    placement_eligible: may enter tier/curve placement (>= 250 predictions).
    full_standing:      no cold-start dampening      (>= 1000 predictions).
    Champion promotion additionally requires >= 14 scored DAYS — that gate is
    calendar-denominated by design ([D8] condition 3) and belongs to the
    promotion rule, not to this average.
    """
    if window_days is None:
        window_days = window_in_force(day=as_of)
    n = scored_prediction_count(episodes, as_of, window_days)
    avg = episode_weighted_average(episodes, as_of, half_life_days, window_days,
                                   prior_mass=prior_mass, prior_value=prior_value)
    return {
        "average": avg,
        "scored_predictions": n,
        "placement_eligible": n >= PLACEMENT_FLOOR_PREDICTIONS,
        "full_standing": n >= FULL_STANDING_PREDICTIONS,
    }
