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
from datetime import date, timedelta
from typing import Callable, Optional

# ROB'S DATED SCHEDULE (timetable sheet, 2026-08-03). This SUPERSEDES the
# 2026-08-01 weekly ramp (300 -> 475 -> 650 -> 825 -> 1000), which was five even
# steps measured in weeks from an anchor. The sheet is different in three ways
# and every one of them matters:
#
#   1. It STARTS AT ZERO. Miners hold nothing until 10 August. The old ladder
#      opened at 300 a on day one.
#   2. It has SIX rungs, not five: 0, 150, 300, 450, 700, 1000.
#   3. It is keyed to FIXED CALENDAR DATES, not weeks-since-launch. The gaps
#      are deliberately uneven (8 days, 7, 14, 7).
#
# The uneven gaps are the tell, and they answer a question we had open. Each
# rung lands exactly on a PAYOUT MILESTONE:
#
#   2026-08-10  bridge payout begins (prior week carried over)      ->  150 a
#   2026-08-18  first daily 7-day score payout                      ->  300 a
#   2026-08-25  first 14-day score payout                           ->  450 a
#   2026-09-08  first 28-day (35 settled) payout                    ->  700 a
#   2026-09-15  terminal                                            -> 1000 a
#
# Those dates are not arbitrary: they fall out of the settle clock
# (action_window_end + 1 + horizon + 7) applied to the first live daily bundle
# on 2026-08-03. Verified exactly — 7d -> 18 Aug, 14d -> 25 Aug, 28d -> 8 Sep.
# So a miner's obligation rises precisely when a new payout horizon starts
# paying them, which is the coherent reading of the policy and resolves the
# open SN21_LADDER_ANCHOR question: the anchor is NEITHER `launch` NOR
# `first_settlement`. It is the settlement calendar.
FIRST_LIVE_BUNDLE_DAY = date(2026, 8, 3)

ALPHA_SCHEDULE = (
    (date(2026, 8, 3), 0.0),
    (date(2026, 8, 10), 150.0),
    (date(2026, 8, 18), 300.0),
    (date(2026, 8, 25), 450.0),
    (date(2026, 9, 8), 700.0),
    (date(2026, 9, 15), 1000.0),
)

# Planned burn, same sheet, same dated shape. Currently a STATIC
# SN21_BURN_FRACTION=0.45 on the validator host, which is correct only until
# 10 August — after that a static value over-burns.
BURN_SCHEDULE = (
    (date(2026, 8, 3), 0.45),
    (date(2026, 8, 10), 0.30),
    (date(2026, 8, 25), 0.15),
    (date(2026, 9, 15), 0.00),
)

# The values alone, kept for display and for callers that want the shape
# without the dates.
ALPHA_LADDER = tuple(a for _, a in ALPHA_SCHEDULE)

# First and terminal rungs, named for callers that need one or the other
# without knowing the shape. daily_loop imports LAUNCH_FLOOR_ALPHA as a default.
#
# LAUNCH_FLOOR_ALPHA IS NOW 0.0, NOT 300.0. Rob's schedule starts miners
# holding nothing until 10 August; the superseded weekly ramp opened at 300 a
# on day one. Anything that treated "the launch floor" as a non-zero obligation
# is now wrong by 300 alpha.
LAUNCH_FLOOR_ALPHA = ALPHA_LADDER[0]
TERMINAL_FLOOR_ALPHA = ALPHA_LADDER[-1]

# Suggested owner-set chain params for v435 activation (documented here so
# the policy travels as one object; enforced by the CHAIN, not this module).
SUGGESTED_LOCK_SHARE_P = (0.75, 0.90)
SUGGESTED_DRAIN_RATIO_K = 0.5


# Rob's IM launch date arrives as CONFIGURATION, never as a code edit. The
# ladder is the only thing in either repo that needs to know the date (verified
# by grep across hope-sn21 and OBI), so this one env value is what stands
# between "Rob names a date" and "the ladder starts stepping" — no deploy, no
# release, no code review on launch day.
IM_LAUNCH_DATE_ENV = "SN21_IM_LAUNCH_DATE"


def launch_date_from(environ) -> Optional[date]:
    """Rob's IM launch date from config, or None if unset.

    Unset and MALFORMED both return None, and None means "hold at week 0".
    That direction is deliberate: a typo in a deploy variable must never
    silently promote every miner to a higher collateral floor. Failing to the
    lowest rung is the only safe failure for an obligation.
    """
    raw = (environ.get(IM_LAUNCH_DATE_ENV) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        print(f"[collateral] {IM_LAUNCH_DATE_ENV}={raw!r} is not an ISO date "
              f"— holding the floor at week 0 ({ALPHA_LADDER[0]} a)", flush=True)
        return None


# WHAT THE LADDER'S CLOCK COUNTS FROM — [PENDING ROB]. He gave the rungs
# ("one step per week over four weeks") but never said what week 1 counts
# from, and the two candidates are not equivalent:
#
#   launch           week 1 begins at the IM launch date. Inherited default —
#                    the schedule this replaced read "300 a at IM launch".
#   first_settlement week 1 begins the day the subnet first settles anything.
#
# It matters because of cold start: nobody is placement-eligible until
# predictions settle (~day 15), and the capture path escrows from EARNINGS —
# so under `launch` the 475 and 650 rungs arrive before any miner has earned
# anything to escrow. Whether that is acceptable is Rob's call.
#
# Both are CONFIG. This exists so his one-word answer stays a flag flip
# instead of becoming a code change and a deploy on launch day, which is the
# single thing the launch-date work set out to eliminate.
LADDER_ANCHOR_ENV = "SN21_LADDER_ANCHOR"
ANCHOR_LAUNCH = "launch"
ANCHOR_FIRST_SETTLEMENT = "first_settlement"


def ladder_anchor_from(environ) -> str:
    """Which anchor is configured. Unknown values fall back to `launch` — the
    inherited default — rather than guessing at the stricter one."""
    raw = (environ.get(LADDER_ANCHOR_ENV) or "").strip().lower()
    if raw in (ANCHOR_LAUNCH, ANCHOR_FIRST_SETTLEMENT):
        return raw
    if raw:
        print(f"[collateral] {LADDER_ANCHOR_ENV}={raw!r} unrecognised — "
              f"anchoring to {ANCHOR_LAUNCH}", flush=True)
    return ANCHOR_LAUNCH


def active_floor(day: date, environ,
                 first_settlement: Optional[date] = None) -> float:
    """The floor in force on `day` per configuration.

    Rob's dated schedule (2026-08-03) replaced the week-counting model, so the
    anchor question this used to resolve — `launch` vs `first_settlement` — no
    longer applies: the rungs are pinned to PAYOUT MILESTONES on the settlement
    calendar, which is neither. `first_settlement` is still accepted so the
    signature and daily_loop's call site are unchanged, but it is not consulted.

    SN21_IM_LAUNCH_DATE now means THE FIRST LIVE DAILY BUNDLE DATE. Setting it
    shifts the entire schedule by its offset from 2026-08-03, so if the launch
    moves, every rung moves with it and stays on its payout milestone. Unset
    means use the sheet's literal dates.

    Unset and MALFORMED both fall through to the sheet as published. A typo in
    a deploy variable must never silently move a miner's obligation.
    """
    if (environ.get(LADDER_ANCHOR_ENV) or "").strip():
        print(f"[collateral] {LADDER_ANCHOR_ENV} is set but no longer used — "
              f"Rob's 2026-08-03 timetable pins the ladder to payout dates, "
              f"not to weeks from an anchor. Ignoring.", flush=True)
    return floor_for_day(day, launch_date_from(environ))


def _step_value(day: date, schedule, shift_days: int = 0) -> float:
    """Value of the latest step whose date has arrived. Shared by both
    schedules so they cannot diverge in how a boundary day is treated.

    A step takes effect ON its date, not the day after — Rob's sheet reads
    "Monday 10 August ... 150", so the 10th is already 150.

    Before the first step, returns the FIRST value. Back-dated folds happen
    when a settle run catches up, and a miner must never be judged against a
    floor that did not exist yet.
    """
    value = schedule[0][1]
    for when, v in schedule:
        if day >= when + timedelta(days=shift_days):
            value = v
        else:
            break
    return value


def floor_for_day(day: date, launch_day: Optional[date] = None) -> float:
    """The flat alpha floor in force on `day`, per Rob's dated ALPHA_SCHEDULE.

    `launch_day` is the FIRST LIVE DAILY BUNDLE date. Passing one shifts the
    whole schedule by the difference from 2026-08-03, so if the launch moves
    the rungs move WITH it and stay pinned to their payout milestones —
    rather than the schedule silently stepping on calendar dates that no
    longer mean anything. Omit it and the sheet's literal dates are used.

    Note the shape change: this used to compute a week index from a launch
    date. Rob's sheet is not weekly — the gaps are 8, 7, 14 and 7 days,
    because each rung lands on a payout milestone. A week-based reading
    cannot express it.
    """
    shift = 0 if launch_day is None else (launch_day - FIRST_LIVE_BUNDLE_DAY).days
    return _step_value(day, ALPHA_SCHEDULE, shift)


def burn_for_day(day: date, launch_day: Optional[date] = None) -> float:
    """Planned burn fraction in force on `day`, per Rob's BURN_SCHEDULE.

    45% -> 30% (10 Aug) -> 15% (25 Aug) -> 0% (15 Sep). The validator host
    currently carries a STATIC SN21_BURN_FRACTION=0.45, which is correct only
    until 10 August; after that a static value over-burns every day. This
    function is the schedule; wiring it to the weight setter is a separate,
    deliberate step because burn changes what miners are paid.
    """
    shift = 0 if launch_day is None else (launch_day - FIRST_LIVE_BUNDLE_DAY).days
    return _step_value(day, BURN_SCHEDULE, shift)


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
