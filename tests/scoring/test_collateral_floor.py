"""W2 — [D9] soft-phase collateral floors: capture path + advisory view."""

from datetime import date

import pytest

from datetime import timedelta

from hope.scoring.collateral_floor import (
    ALPHA_LADDER,
    ALPHA_SCHEDULE,
    BURN_SCHEDULE,
    FIRST_LIVE_BUNDLE_DAY,
    burn_for_day,
    ANCHOR_FIRST_SETTLEMENT,
    ANCHOR_LAUNCH,
    ladder_anchor_from,
    active_floor,
    launch_date_from,
    LAUNCH_FLOOR_ALPHA,
    TERMINAL_FLOOR_ALPHA,
    CaptureState,
    add_voluntary,
    compliance_view,
    floor_for_day,
    fold_day,
)

LAUNCH = date(2026, 8, 10)


def test_ladder_is_the_published_dated_schedule():
    """the operator's timetable sheet, 2026-08-03. SUPERSEDES the 2026-08-01 weekly ramp
    (300 -> 475 -> 650 -> 825 -> 1000) in three ways: it starts at ZERO, it has
    SIX rungs, and it is keyed to calendar dates rather than weeks.

    Pinned literally. These are published numbers miners plan around, so a
    silent edit must fail here rather than in a miner's wallet."""
    assert ALPHA_SCHEDULE == (
        (date(2026, 8, 3), 0.0),
        (date(2026, 8, 10), 150.0),
        (date(2026, 8, 18), 300.0),
        (date(2026, 8, 25), 450.0),
        (date(2026, 9, 8), 700.0),
        (date(2026, 9, 15), 1000.0),
    )
    assert ALPHA_LADDER == (0.0, 150.0, 300.0, 450.0, 700.0, 1000.0)
    assert LAUNCH_FLOOR_ALPHA == 0.0        # was 300.0 under the old ramp
    assert TERMINAL_FLOOR_ALPHA == 1000.0


def test_burn_is_the_published_dated_schedule():
    """45% -> 30% (10 Aug) -> 15% (25 Aug) -> 0% (15 Sep). The validator host
    carries a STATIC 0.45, which is right only until 10 August."""
    assert BURN_SCHEDULE == (
        (date(2026, 8, 3), 0.45),
        (date(2026, 8, 10), 0.30),
        (date(2026, 8, 25), 0.15),
        (date(2026, 9, 15), 0.00),
    )


@pytest.mark.parametrize("day,alpha,burn", [
    ("2026-08-03", 0.0, 0.45),
    ("2026-08-09", 0.0, 0.45),      # day before the first step
    ("2026-08-10", 150.0, 0.30),    # steps ON the date, not after it
    ("2026-08-17", 150.0, 0.30),
    ("2026-08-18", 300.0, 0.30),    # first 7-day payout
    ("2026-08-24", 300.0, 0.30),
    ("2026-08-25", 450.0, 0.15),    # first 14-day payout
    ("2026-09-07", 450.0, 0.15),
    ("2026-09-08", 700.0, 0.15),    # first 28-day payout
    ("2026-09-14", 700.0, 0.15),
    ("2026-09-15", 1000.0, 0.00),   # terminal
    ("2026-12-25", 1000.0, 0.00),   # holds
])
def test_every_row_of_the_published_timetable(day, alpha, burn):
    d = date.fromisoformat(day)
    assert floor_for_day(d) == alpha
    assert burn_for_day(d) == burn


def test_the_rungs_land_on_payout_milestones():
    """WHY the gaps are uneven (8, 7, 14, 7 days) rather than weekly. Each rung
    lands on the day a new payout horizon starts paying, derived from the settle
    clock: action_window_end + 1 + horizon + 7, applied to the first live daily
    bundle on 2026-08-03. If this drifts, the ladder and the payout calendar
    have come apart and miners' obligations no longer track their income."""
    first = FIRST_LIVE_BUNDLE_DAY
    assert first + timedelta(days=1 + 7 + 7) == date(2026, 8, 18)
    assert first + timedelta(days=1 + 14 + 7) == date(2026, 8, 25)
    assert first + timedelta(days=1 + 28 + 7) == date(2026, 9, 8)
    stepped = [d for d, _ in ALPHA_SCHEDULE]
    for milestone in (date(2026, 8, 18), date(2026, 8, 25), date(2026, 9, 8)):
        assert milestone in stepped, milestone


def test_a_moved_launch_moves_the_whole_schedule_with_it():
    """SN21_IM_LAUNCH_DATE now means the FIRST LIVE DAILY BUNDLE date. If the
    launch slips, every rung must slip with it and stay on its payout
    milestone — otherwise the ladder steps on calendar dates that no longer
    mean anything and miners are charged before they are paid."""
    shifted = date(2026, 8, 10)          # a week late
    assert floor_for_day(date(2026, 8, 18), shifted) == 150.0   # was 300
    assert floor_for_day(date(2026, 8, 25), shifted) == 300.0   # was 450
    assert floor_for_day(date(2026, 9, 15), shifted) == 700.0   # was 1000


def test_pre_schedule_day_gets_the_opening_rung_not_an_error():
    """Back-dated folds happen when a settle run catches up. A miner must
    never be judged against a floor that did not exist yet."""
    assert floor_for_day(date(2026, 7, 1)) == LAUNCH_FLOOR_ALPHA


def test_ladder_is_monotonic():
    """A floor that fell would release collateral the capture path never
    drains — the frozen-floor rule assumes floors only ever rise."""
    assert list(ALPHA_LADDER) == sorted(ALPHA_LADDER)


def test_burn_only_ever_falls():
    """Burn steps down to zero. A rise would take pay away from miners after
    they had planned around it."""
    vals = [b for _, b in BURN_SCHEDULE]
    assert vals == sorted(vals, reverse=True)


def test_capture_fills_floor_before_payout():
    st = CaptureState("miner")
    f1 = fold_day(st, 200.0, 300.0)
    assert (f1.escrowed_alpha, f1.paid_alpha) == (200.0, 0.0)
    f2 = fold_day(f1.state, 200.0, 300.0)
    assert (f2.escrowed_alpha, f2.paid_alpha) == (100.0, 100.0)  # floor tops out
    f3 = fold_day(f2.state, 200.0, 300.0)
    assert (f3.escrowed_alpha, f3.paid_alpha) == (0.0, 200.0)    # normal payouts
    assert f3.state.locked_alpha == 300.0
    assert f3.state.total_earned_alpha == 600.0
    assert f3.state.total_paid_alpha == 300.0


def test_never_earned_never_owes():
    st = CaptureState("idle")
    f = fold_day(st, 0.0, 300.0)
    assert f.state.locked_alpha == 0.0
    assert f.escrowed_alpha == f.paid_alpha == 0.0


def test_frozen_floor_rule_zero_weighted_miner():
    """No earnings -> no drain -> lock frozen exactly as-is."""
    st = CaptureState("banned", locked_alpha=180.0)
    f = fold_day(st, 0.0, 300.0)
    assert f.state.locked_alpha == 180.0  # frozen, not released


def test_floor_raise_reopens_capture():
    """Review restates 300 -> 600: a floor-met miner re-enters escrow."""
    st = CaptureState("champ", locked_alpha=300.0)
    f = fold_day(st, 100.0, 600.0)
    assert (f.escrowed_alpha, f.paid_alpha) == (100.0, 0.0)


def test_voluntary_front_skips_capture():
    st = add_voluntary(CaptureState("eager"), 300.0)
    f = fold_day(st, 150.0, 300.0)
    assert (f.escrowed_alpha, f.paid_alpha) == (0.0, 150.0)
    assert st.voluntary_alpha == 300.0


def test_negative_inputs_rejected():
    with pytest.raises(ValueError):
        fold_day(CaptureState("x"), -1.0, 300.0)
    with pytest.raises(ValueError):
        add_voluntary(CaptureState("x"), -5.0)


def test_compliance_view_soft_phase():
    states = {
        "met": CaptureState("met", locked_alpha=320.0),
        "part": CaptureState("part", locked_alpha=75.0),
    }
    view = compliance_view(states, 300.0)
    assert view["policy"]["enforcement"] == "soft"
    assert view["floors_met"] == 1 and view["floors_total"] == 2
    assert view["miners"]["part"]["capture_progress"] == 0.25
    assert view["miners"]["met"]["source"] == "capture_bookkeeping"


def test_chain_reader_supersedes_bookkeeping_when_it_answers():
    states = {
        "onchain": CaptureState("onchain", locked_alpha=0.0),
        "offchain": CaptureState("offchain", locked_alpha=100.0),
    }
    reader = lambda hk: 450.0 if hk == "onchain" else None
    view = compliance_view(states, 300.0, chain_reader=reader)
    assert view["miners"]["onchain"]["source"] == "chain_native"
    assert view["miners"]["onchain"]["floor_met"] is True
    # chain silent for this miner -> soft bookkeeping stands
    assert view["miners"]["offchain"]["source"] == "capture_bookkeeping"
    assert view["miners"]["offchain"]["floor_met"] is False


# ---- the launch date is CONFIGURATION, not a code change --------------------

def test_no_launch_date_configured_uses_the_sheets_literal_dates():
    """the published timetable carries real calendar dates, so an unset launch date is not
    "hold at rung zero forever" any more — it means run the published schedule
    exactly as miners were shown it."""
    assert active_floor(date(2026, 8, 3), {}) == 0.0
    assert active_floor(date(2026, 8, 18), {}) == 300.0
    assert active_floor(date(2026, 12, 25), {}) == TERMINAL_FLOOR_ALPHA


def test_a_malformed_launch_date_falls_through_to_the_published_sheet():
    """A typo in a deploy variable must never silently move a miner's
    obligation. Falling through to the published dates is the only safe
    behaviour: it is what miners were actually told."""
    env = {"SN21_IM_LAUNCH_DATE": "next tuesday"}
    assert active_floor(date(2026, 8, 18), env) == floor_for_day(date(2026, 8, 18))
    assert launch_date_from({"SN21_IM_LAUNCH_DATE": "2026-13-45"}) is None
    assert launch_date_from({"SN21_IM_LAUNCH_DATE": "   "}) is None


def test_a_configured_launch_date_shifts_the_whole_schedule():
    """SN21_IM_LAUNCH_DATE now means the FIRST LIVE DAILY BUNDLE date. A
    launch a week late must push every rung a week later, so obligations keep
    tracking payouts."""
    env = {"SN21_IM_LAUNCH_DATE": "2026-08-10"}
    assert launch_date_from(env) == date(2026, 8, 10)
    assert active_floor(date(2026, 8, 10), env) == 0.0      # was 150 unshifted
    assert active_floor(date(2026, 8, 17), env) == 150.0
    assert active_floor(date(2026, 8, 25), env) == 300.0


def test_the_anchor_env_is_dead_and_says_so():
    """SN21_LADDER_ANCHOR asked launch-vs-first-settlement. the published timetable answers
    NEITHER — the rungs sit on payout dates. The variable is ignored rather
    than quietly honoured, because a stale setting silently changing what
    miners owe is exactly the failure the pending markers existed to prevent.
    ladder_anchor_from still parses for callers, but active_floor does not
    consult it: same day, same rung, whatever it says."""
    day = date(2026, 8, 25)
    base = {"SN21_IM_LAUNCH_DATE": "2026-08-03"}
    assert active_floor(day, base) == 450.0
    assert active_floor(day, {**base, "SN21_LADDER_ANCHOR": ANCHOR_FIRST_SETTLEMENT},
                        date(2026, 8, 20)) == 450.0
    assert active_floor(day, {**base, "SN21_LADDER_ANCHOR": ANCHOR_LAUNCH}) == 450.0


def test_first_settlement_argument_is_accepted_but_ignored():
    """daily_loop still passes it. Kept in the signature so the call site did
    not have to change, but it must not move the floor."""
    day = date(2026, 9, 8)
    assert active_floor(day, {}, None) == active_floor(day, {}, date(2026, 8, 20))



# ---- burn resolution: schedule governs, env is the operator's override lever ---------

def test_burn_env_override_beats_the_schedule():
    """SN21_BURN_FRACTION is the published "burn may be adjusted at any time"
    lever, so an explicit value must win on any date — including after the
    schedule reaches zero."""
    from hope.scoring.collateral_floor import resolve_burn_fraction
    assert resolve_burn_fraction({"SN21_BURN_FRACTION": "0.45"},
                                 date(2026, 9, 15)) == (0.45, "env")
    assert resolve_burn_fraction({"SN21_BURN_FRACTION": "0"},
                                 date(2026, 8, 3)) == (0.0, "env")


def test_burn_schedule_governs_when_env_unset():
    from hope.scoring.collateral_floor import resolve_burn_fraction
    assert resolve_burn_fraction({}, date(2026, 8, 9)) == (0.45, "schedule")
    assert resolve_burn_fraction({}, date(2026, 8, 10)) == (0.30, "schedule")
    assert resolve_burn_fraction({}, date(2026, 8, 25)) == (0.15, "schedule")
    assert resolve_burn_fraction({}, date(2026, 9, 15)) == (0.0, "schedule")


def test_malformed_burn_env_falls_to_the_schedule_not_a_constant():
    """A deploy typo must not pin burn at a stale hardcoded number — the
    schedule is the published truth."""
    from hope.scoring.collateral_floor import resolve_burn_fraction
    assert resolve_burn_fraction({"SN21_BURN_FRACTION": "garbage"},
                                 date(2026, 8, 25)) == (0.15, "schedule")
    assert resolve_burn_fraction({"SN21_BURN_FRACTION": "1.5"},
                                 date(2026, 8, 25)) == (0.15, "schedule")


def test_burn_schedule_shifts_with_the_launch_date():
    """Launch slips a week -> every burn step slips with it, same as alpha."""
    from hope.scoring.collateral_floor import resolve_burn_fraction
    env = {"SN21_IM_LAUNCH_DATE": "2026-08-10"}
    assert resolve_burn_fraction(env, date(2026, 8, 10)) == (0.45, "schedule")
    assert resolve_burn_fraction(env, date(2026, 8, 17)) == (0.30, "schedule")


def test_activation_day_is_a_noop():
    """THE ROLLOUT GUARANTEE. Removing the env before 10 Aug hands control to
    the schedule at the exact same value, so the handover itself changes
    nothing on the day it happens."""
    from hope.scoring.collateral_floor import resolve_burn_fraction
    with_env = resolve_burn_fraction({"SN21_BURN_FRACTION": "0.45"}, date(2026, 8, 6))
    without = resolve_burn_fraction({}, date(2026, 8, 6))
    assert with_env[0] == without[0] == 0.45
