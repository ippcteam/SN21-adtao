"""Daily-stream weight allocation — E1 + D13 + D7 + D8 behind one flag.

This is the wiring layer the design leaves last (v0.5 §12: "daily weight
updates behind the [D3] volume gate"). Everything it composes is already
built and tested pure:

    daily_score_flow (E1)     — per-(episode,horizon) entries as horizons finalise
    episode_average (D13)     — age-weighted standing, cold-start gates
    weight_curve (D7)         — published rank curve, ceiling 20
    champion_promotion (D8)   — separate promotion state machine + log
    standing_ledger           — persistence for entries + promotion state

Flag: SN21_DAILY_STREAM_WEIGHTS (off by default — Rob's switch, M4).
[D3] gate: SN21_D3_MIN_DAILY_EPISODES (0 = gate disabled until Rob sets
the published threshold against the verified ~330/weekday figure). On a
gated day the standings still update (scores are facts); only the WEIGHT
UPDATE is withheld — callers keep the previous vector, which is the
design's "thin day" behaviour: no weight movement on unrepresentative
volume, no data thrown away.

Pure core (compute_daily_allocation) + a thin I/O convenience
(allocation_from_ledger) mirroring the shadow-module pattern.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from hope.scoring.champion_promotion import (
    PromotionDecision,
    PromotionParams,
    PromotionState,
    observe_day,
)
from hope.scoring.episode_average import ScoredEpisode, standing
from hope.scoring.weight_curve import CurveParams, curve_weights
from hope.scoring import standing_ledger

FLAG_ENV = "SN21_DAILY_STREAM_WEIGHTS"
D3_MIN_ENV = "SN21_D3_MIN_DAILY_EPISODES"


def daily_stream_enabled(environ=os.environ) -> bool:
    return environ.get(FLAG_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def d3_min_daily_episodes(environ=os.environ) -> int:
    """[D3] published minimum; 0 disables the gate (pre-ratification)."""
    raw = environ.get(D3_MIN_ENV, "0").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


@dataclass(frozen=True)
class DailyAllocation:
    day: date
    gated: bool                      # [D3]: True → hold previous weights
    day_episode_volume: int
    standings: dict = field(default_factory=dict)        # hotkey → D13 average
    weights: dict = field(default_factory=dict)          # hotkey → curve weight
    earning_set_size: int = 0
    promotion: Optional[PromotionDecision] = None        # [D8], never gated


def compute_daily_allocation(
    entries: dict[str, list[ScoredEpisode]],
    day: date,
    day_episode_volume: int,
    promotion_state: PromotionState,
    min_daily_episodes: int = 0,
    curve_params: CurveParams = CurveParams(),
    promotion_params: PromotionParams = PromotionParams(),
) -> DailyAllocation:
    """One day's standings → weights (D7) + promotion observation (D8).

    [D3] gates the WEIGHT UPDATE only. Promotion observation still runs on
    a gated day: [D8]'s consecutive-hold clock is calendar-real and its
    inputs (standings) are facts regardless of whether weights move.
    Placement uses only placement-eligible miners (cold-start floor,
    GAP-2 v2 §3.4); their D13 average drives both curve rank and the
    promotion margin.
    """
    placements: dict[str, float] = {}
    for hotkey, eps in entries.items():
        st = standing(eps, as_of=day)
        if st["average"] is not None and st["placement_eligible"]:
            placements[hotkey] = st["average"]

    scored_days = standing_ledger.scored_day_counts(entries)
    promotion = observe_day(
        promotion_state, day, placements, scored_days, promotion_params
    )

    gated = bool(min_daily_episodes) and day_episode_volume < min_daily_episodes
    weights = {} if gated else curve_weights(placements, curve_params)
    return DailyAllocation(
        day=day,
        gated=gated,
        day_episode_volume=day_episode_volume,
        standings=placements,
        weights=weights,
        earning_set_size=sum(1 for w in weights.values() if w > 0),
        promotion=promotion,
    )


def allocation_from_ledger(
    root: str,
    day: date,
    day_episode_volume: int,
    min_daily_episodes: Optional[int] = None,
    curve_params: CurveParams = CurveParams(),
    promotion_params: PromotionParams = PromotionParams(),
) -> DailyAllocation:
    """Load ledger + promotion state, compute, persist state + log events.

    The one impure entrypoint: everything it does beyond
    compute_daily_allocation is ledger I/O. Promotion state is persisted
    every call (last_observed advances even on uneventful days — the [D8]
    consecutive-day rule depends on it); events append to the audit log.
    """
    if min_daily_episodes is None:
        min_daily_episodes = d3_min_daily_episodes()
    entries = standing_ledger.load_entries(root, as_of=day)
    state = standing_ledger.load_promotion_state(root)
    alloc = compute_daily_allocation(
        entries, day, day_episode_volume, state,
        min_daily_episodes=min_daily_episodes,
        curve_params=curve_params,
        promotion_params=promotion_params,
    )
    if alloc.promotion is not None:
        standing_ledger.save_promotion_state(root, alloc.promotion.state)
        if alloc.promotion.event:
            standing_ledger.append_promotion_event(root, alloc.promotion.event)
    return alloc
