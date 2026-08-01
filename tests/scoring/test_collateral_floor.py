"""W2 — [D9] soft-phase collateral floors: capture path + advisory view."""

from datetime import date

import pytest

from datetime import timedelta

from hope.scoring.collateral_floor import (
    ALPHA_LADDER,
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


def test_ladder_is_robs_published_schedule():
    """Rob 2026-08-01: 300 -> 475 -> 650 -> 825 -> 1,000, one step per week.
    Pinned literally: these are published numbers miners plan around, so a
    silent edit to the tuple must fail here rather than in a miner's wallet."""
    assert ALPHA_LADDER == (300.0, 475.0, 650.0, 825.0, 1000.0)
    assert LAUNCH_FLOOR_ALPHA == 300.0
    assert TERMINAL_FLOOR_ALPHA == 1000.0


@pytest.mark.parametrize("day_offset,expected", [
    (0, 300.0), (6, 300.0),      # week 0
    (7, 475.0), (13, 475.0),     # week 1
    (14, 650.0), (20, 650.0),    # week 2
    (21, 825.0), (27, 825.0),    # week 3
    (28, 1000.0), (60, 1000.0),  # week 4+, holds at terminal
])
def test_floor_steps_weekly_and_holds_at_terminal(day_offset, expected):
    assert floor_for_day(LAUNCH + timedelta(days=day_offset), LAUNCH) == expected


def test_pre_launch_day_gets_the_launch_floor_not_an_error():
    """Back-dated folds happen when a settle run catches up. A miner must
    never be judged against a floor that did not exist yet."""
    assert floor_for_day(LAUNCH - timedelta(days=3), LAUNCH) == LAUNCH_FLOOR_ALPHA


def test_ladder_is_monotonic():
    """A floor that fell would release collateral the capture path never
    drains — the frozen-floor rule assumes floors only ever rise."""
    assert list(ALPHA_LADDER) == sorted(ALPHA_LADDER)


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

def test_ladder_holds_at_week_zero_until_a_launch_date_is_configured():
    """Before Rob names a date the floor must be the lowest rung. The ladder
    existing is not the same as the ladder running."""
    assert active_floor(date(2026, 12, 25), {}) == LAUNCH_FLOOR_ALPHA


def test_a_malformed_launch_date_fails_DOWN_not_up():
    """A typo in a deploy variable must never silently promote every miner to
    a higher collateral obligation. Failing to the lowest rung is the only
    safe direction here, so this asserts the direction, not just that it
    survives."""
    assert active_floor(date(2026, 12, 25),
                        {"SN21_IM_LAUNCH_DATE": "next tuesday"}) == LAUNCH_FLOOR_ALPHA
    assert launch_date_from({"SN21_IM_LAUNCH_DATE": "2026-13-45"}) is None
    assert launch_date_from({"SN21_IM_LAUNCH_DATE": "   "}) is None


def test_a_configured_launch_date_makes_the_ladder_step():
    env = {"SN21_IM_LAUNCH_DATE": "2026-08-10"}
    L = date(2026, 8, 10)
    assert launch_date_from(env) == L
    assert [active_floor(L + timedelta(days=d), env) for d in (0, 7, 14, 21, 28, 90)] \
        == [300.0, 475.0, 650.0, 825.0, 1000.0, 1000.0]
