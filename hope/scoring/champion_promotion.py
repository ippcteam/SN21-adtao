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


def _rank_key(item: tuple[str, float]) -> tuple[float, str]:
    """Deterministic ordering shared with the weight curve: standing DESC,
    miner id ASC (weight_curve sorts by (-standing, miner)). Use with
    min() over (-standing, miner) or as sort key directly."""
    miner, standing = item
    return (-standing, miner)


def observe_day(
    state: PromotionState,
    day: date,
    standings: dict[str, float],
    scored_days: dict[str, int],
    params: PromotionParams = PromotionParams(),
) -> PromotionDecision:
    """Fold one day's standings into the promotion state machine.

    Rules:
    - no champion yet: the best ELIGIBLE miner (meeting min_scored_days)
      is seated — a young table-topper does not block an eligible second
      place (cold start: nobody seats before condition 3 is satisfiable)
    - a lead streak belongs to ONE challenger; if a different miner leads
      today, the streak restarts with them (consecutive-days is per-miner)
    - non-consecutive observations break the streak (a missed day is a
      broken hold — the published rule says consecutive)
    - SAME-DAY re-observation is streak-preserving: the runner is a
      30-minute daemon and report-only / restart re-runs revisit a date;
      without this, every intra-day tick reset lead_started to today and
      promotion could never fire (2026-07-29 audit, production-blocking).
      A same-day observation with the same leader keeps the streak; a
      same-day leader CHANGE restarts the streak (per-miner rule wins).
    - champion with NO standing (aged out / below the placement floor)
      does NOT waive the margin: the published rule is that a challenger
      must then simply LEAD ALL standings for the full hold period (the
      margin is unmeasurable against a missing average; leadership plus
      the hold and scored-days conditions still apply)
    - promotion fires only when all conditions hold on `day`
    - tie-break everywhere: standing desc, miner id asc (same direction
      as the weight curve)
    """
    if not standings:
        return PromotionDecision(replace(state, last_observed=day), False)

    # Seat the first champion: best ELIGIBLE miner (condition 3 first).
    if state.champion is None:
        eligible = [
            (m, s) for m, s in standings.items()
            if s is not None and scored_days.get(m, 0) >= params.min_scored_days
        ]
        if eligible:
            best = min(eligible, key=_rank_key)[0]
            new = PromotionState(champion=best, last_observed=day)
            return PromotionDecision(
                new, True,
                {"type": "initial_seat", "champion": best, "day": str(day)},
            )
        return PromotionDecision(replace(state, last_observed=day), False)

    champ_avg = standings.get(state.champion)
    contenders = [
        (m, s) for m, s in standings.items()
        if m != state.champion and s is not None
    ]
    if champ_avg is None:
        # Champion has no standing: margin unmeasurable — challenger must
        # lead ALL standings (documented rule above), no margin waiver.
        qualified = contenders
    else:
        qualified = [
            (m, s) for m, s in contenders
            if s >= champ_avg * (1.0 + params.margin)
        ]
    leader = min(qualified, key=_rank_key)[0] if qualified else None

    if leader is None:
        # nobody clears the bar today — every streak dies
        new = replace(state, challenger=None, lead_started=None, last_observed=day)
        return PromotionDecision(new, False)

    same_day = state.last_observed is not None and day == state.last_observed
    consecutive = state.challenger == leader and state.last_observed is not None and (
        day == state.last_observed + timedelta(days=1) or same_day
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
                "margin_met": champ_avg is not None, "scored_days": scored_days.get(leader, 0),
            },
        )

    new = replace(state, challenger=leader, lead_started=lead_started, last_observed=day)
    return PromotionDecision(new, False)
