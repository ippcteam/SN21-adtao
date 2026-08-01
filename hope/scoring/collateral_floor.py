"""[D9] Collateral floors — soft-phase machinery (v0.5 §8).

The policy launches with the IM on our date; CHAIN enforcement activates
only when the network deploys v435 ("we signal that we will use it, and we
do not control that timetable"). Until then holdings are advisory and
validators score against them SOFTLY — this module is that soft phase:

  * capture-path accounting — when a model earns, its incentive is
    escrowed into the lock until the floor is met, then payouts resume.
    No upfront buy-in, no entry gate; a model that never earns owes
    nothing. Existing scorers are grandfathered onto this same path.
  * the advisory compliance view — per-miner floor state for the review
    pack and miner comms. Explicitly NOT wired into weights: "nothing
    load-bearing rests on the soft phase."
  * the frozen-floor rule — a zero-weighted miner stops earning, so
    nothing drains: the lock freezes as-is (deterrence is the ratio of
    forfeited floor to a paying slot, not the floor's size). A dethroned
    champion's floor equally stays locked until they earn again.

Floor amounts are FLAT alpha, restated at four-weekly reviews (per-miner
earnings vary too much under the §7 curve for a rewards-denominated
floor). Rob restated the schedule on 2026-08-01, replacing the original
single step (300 a -> 600 a at +4 weeks) with a WEEKLY ramp:

    week 0   300 a   (IM launch)
    week 1   475 a
    week 2   650 a
    week 3   825 a
    week 4+  1000 a

Same start, higher ceiling, and it climbs every week instead of one cliff
at day 28 — so a miner's obligation grows with their exposure rather than
jumping. See ALPHA_LADDER.

When v435 activates, native `min_locked` reads replace this bookkeeping;
`ChainFloorReader` is the injection seam (suggested owner params travel
with the policy: CollateralLockShare p~=0.75-0.9, CollateralDrainRatio
k~=0.5 — modeled in the emission lab S8).

Pure module: no I/O, no chain calls.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from typing import Callable, Optional

# Review-restated flat floors (v0.5 §8), weeks measured from the IM launch
# date, which is Rob's to set. Rob 2026-08-01: "Alpha hold: 300 -> 475 -> 650
# -> 825 -> 1,000 a, one step per week over four weeks."
#
# Index IS the week number, so ALPHA_LADDER[n] is the floor in force during
# week n and the last entry is the terminal floor. Restating at a review is
# editing this tuple — floor_for_day derives everything from it, so the
# schedule cannot drift out of step with the published numbers.
ALPHA_LADDER = (300.0, 475.0, 650.0, 825.0, 1000.0)

# First and terminal rungs, named for callers that need one or the other
# without knowing the shape. LAUNCH_FLOOR_ALPHA is kept because daily_loop
# imports it as a default.
LAUNCH_FLOOR_ALPHA = ALPHA_LADDER[0]
TERMINAL_FLOOR_ALPHA = ALPHA_LADDER[-1]

# Suggested owner-set chain params for v435 activation (documented here so
# the policy travels as one object; enforced by the CHAIN, not this module).
SUGGESTED_LOCK_SHARE_P = (0.75, 0.90)
SUGGESTED_DRAIN_RATIO_K = 0.5


def floor_for_day(day: date, launch_day: date) -> float:
    """The flat floor in force on `day`, per ALPHA_LADDER.

    Week n runs [launch_day + 7n, launch_day + 7(n+1)); the ladder holds at
    its terminal rung thereafter. A day BEFORE launch returns the week-0
    floor rather than raising: back-dated folds happen when a settle run
    catches up, and a miner must never be judged against a floor that did
    not exist yet. Review restatements can still bypass the schedule
    entirely by passing an explicit floor to fold_day.
    """
    week = (day - launch_day).days // 7
    if week < 0:
        return ALPHA_LADDER[0]
    return ALPHA_LADDER[min(week, len(ALPHA_LADDER) - 1)]


@dataclass(frozen=True)
class CaptureState:
    """One miner's soft-phase collateral bookkeeping."""
    hotkey: str
    locked_alpha: float = 0.0        # escrowed so far (capture + voluntary)
    voluntary_alpha: float = 0.0     # portion fronted via add_collateral
    total_earned_alpha: float = 0.0  # lifetime earned (audit)
    total_paid_alpha: float = 0.0    # lifetime paid out after escrow (audit)


@dataclass(frozen=True)
class DayFold:
    state: CaptureState
    escrowed_alpha: float    # captured into the lock today
    paid_alpha: float        # took home today


def fold_day(state: CaptureState, earned_alpha: float, floor_alpha: float) -> DayFold:
    """Fold one day's incentive through the capture path.

    Escrow fills the gap to the floor first; the remainder pays out. A
    zero-earning day (including zero-weighted enforcement) moves nothing —
    which IS the frozen-floor rule: no earnings, no drain, lock unchanged.
    """
    if earned_alpha < 0:
        raise ValueError("earned_alpha cannot be negative")
    gap = max(0.0, floor_alpha - state.locked_alpha)
    escrow = min(gap, earned_alpha)
    paid = earned_alpha - escrow
    new = replace(
        state,
        locked_alpha=state.locked_alpha + escrow,
        total_earned_alpha=state.total_earned_alpha + earned_alpha,
        total_paid_alpha=state.total_paid_alpha + paid,
    )
    return DayFold(new, escrow, paid)


def add_voluntary(state: CaptureState, alpha: float) -> CaptureState:
    """Miner fronts collateral voluntarily (their choice, never required)."""
    if alpha < 0:
        raise ValueError("voluntary collateral cannot be negative")
    return replace(
        state,
        locked_alpha=state.locked_alpha + alpha,
        voluntary_alpha=state.voluntary_alpha + alpha,
    )


# ---- advisory compliance view (soft scoring) --------------------------------

def compliance_view(
    states: dict[str, CaptureState],
    floor_alpha: float,
    chain_reader: Optional[Callable[[str], Optional[float]]] = None,
) -> dict:
    """The soft-phase report: floor state per miner, aggregates for the
    review pack. `chain_reader(hotkey) -> locked alpha` is the v435 seam —
    when provided (chain active), NATIVE locks supersede soft bookkeeping
    for any miner the chain answers for. Advisory only: nothing here
    touches weights until enforcement activates.
    """
    miners = {}
    met = 0
    for hotkey, st in sorted(states.items()):
        locked = st.locked_alpha
        source = "capture_bookkeeping"
        if chain_reader is not None:
            native = chain_reader(hotkey)
            if native is not None:
                locked, source = native, "chain_native"
        floor_met = locked >= floor_alpha
        met += floor_met
        miners[hotkey] = {
            "locked_alpha": round(locked, 6),
            "floor_alpha": floor_alpha,
            "floor_met": floor_met,
            "capture_progress": round(min(1.0, locked / floor_alpha), 4)
            if floor_alpha > 0 else 1.0,
            "voluntary_alpha": round(st.voluntary_alpha, 6),
            "source": source,
        }
    return {
        "policy": {
            "floor_alpha": floor_alpha,
            # Soft phase until v435 activates network-wide; per-miner
            # native reads may already supersede bookkeeping (see
            # "source" per miner). chain_available reports whether a
            # reader was wired — the honest signal the audit asked for
            # in place of a hardcoded promise.
            "enforcement": "soft",
            "chain_available": chain_reader is not None,
            "suggested_lock_share_p": list(SUGGESTED_LOCK_SHARE_P),
            "suggested_drain_ratio_k": SUGGESTED_DRAIN_RATIO_K,
        },
        "miners": miners,
        "floors_met": met,
        "floors_total": len(miners),
    }
