"""D8 — champion promotion: deliberate, with hysteresis. Never a raw crossover.

Design v0.5 §7 [D8]: the live model switches only when a challenger meets
ALL of:
  1. leads the champion's moving average by >= margin (initial 5%),
  2. has held that lead for `hold_days` consecutive days (7),
  3. has at least `min_scored_days` of scored history (14).
Miss any condition and the incumbent stays. Close races leave the incumbent
in place — an incumbent that is merely tied is the safer production choice.
Condition 3 also closes the cold-start window by construction (§2).

Margin semantics: RELATIVE lead — challenger_avg >= champion_avg * (1+margin)
(published as "leads by at least 5%"). Review-set via PromotionParams.

Pure state machine over daily observations; the caller persists
PromotionState between days (promotion log = the emitted events).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from typing import Optional


@dataclass(frozen=True)
class PromotionParams:
    margin: float = 0.05          # [D8] c.1 — review-confirmed
    hold_days: int = 7            # [D8] c.2 — consecutive days
    min_scored_days: int = 14     # [D8] c.3 — also the cold-start closure


@dataclass(frozen=True)
class PromotionState:
    champion: Optional[str] = None
    # current challenger streak: who and since when (inclusive)
    challenger: Optional[str] = None
    lead_started: Optional[date] = None
    last_observed: Optional[date] = None


@dataclass(frozen=True)
class PromotionDecision:
    state: PromotionState
    promoted: bool
    event: Optional[dict] = None  # promotion-log entry when something happened


def observe_day(
    state: PromotionState,
    day: date,
    standings: dict[str, float],
    scored_days: dict[str, int],
    params: PromotionParams = PromotionParams(),
) -> PromotionDecision:
    """Fold one day's standings into the promotion state machine.

    Rules:
    - no champion yet: best miner meeting min_scored_days is seated
      (cold start: nobody can be seated before condition 3 is satisfiable)
    - a lead streak belongs to ONE challenger; if a different miner leads
      today, the streak restarts with them (consecutive-days is per-miner)
    - non-consecutive observations break the streak (a missed day is a
      broken hold — the published rule says consecutive)
    - promotion fires only when all three conditions hold on `day`
    """
    if not standings:
        return PromotionDecision(replace(state, last_observed=day), False)

    best = max(standings.items(), key=lambda kv: (kv[1], kv[0]))[0]

    # Seat the first champion (subject to condition 3).
    if state.champion is None:
        if scored_days.get(best, 0) >= params.min_scored_days:
            new = PromotionState(champion=best, last_observed=day)
            return PromotionDecision(
                new, True,
                {"type": "initial_seat", "champion": best, "day": str(day)},
            )
        return PromotionDecision(replace(state, last_observed=day), False)

    champ_avg = standings.get(state.champion)
    leader, lead_ok = None, False
    for miner, avg in standings.items():
        if miner == state.champion or avg is None:
            continue
        if champ_avg is None or avg >= champ_avg * (1.0 + params.margin):
            if leader is None or avg > standings[leader]:
                leader = miner
    lead_ok = leader is not None

    if not lead_ok:
        # nobody clears the margin today — every streak dies
        new = replace(state, challenger=None, lead_started=None, last_observed=day)
        return PromotionDecision(new, False)

    consecutive = (
        state.challenger == leader
        and state.last_observed is not None
        and day == state.last_observed + timedelta(days=1)
    )
    lead_started = state.lead_started if consecutive else day
    held = (day - lead_started).days + 1

    if held >= params.hold_days and scored_days.get(leader, 0) >= params.min_scored_days:
        new = PromotionState(champion=leader, last_observed=day)
        return PromotionDecision(
            new, True,
            {
                "type": "promotion", "old": state.champion, "new": leader,
                "day": str(day), "held_days": held,
                "margin_met": True, "scored_days": scored_days.get(leader, 0),
            },
        )

    new = replace(state, challenger=leader, lead_started=lead_started, last_observed=day)
    return PromotionDecision(new, False)
