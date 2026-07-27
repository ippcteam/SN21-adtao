"""W2 — [D9] soft-phase collateral floors: capture path + advisory view."""

from datetime import date

import pytest

from hope.scoring.collateral_floor import (
    LAUNCH_FLOOR_ALPHA,
    PLUS_4WK_FLOOR_ALPHA,
    CaptureState,
    add_voluntary,
    compliance_view,
    floor_for_day,
    fold_day,
)

LAUNCH = date(2026, 8, 10)


def test_floor_steps_at_four_weeks():
    assert floor_for_day(LAUNCH, LAUNCH) == LAUNCH_FLOOR_ALPHA
    assert floor_for_day(date(2026, 9, 6), LAUNCH) == LAUNCH_FLOOR_ALPHA   # day 27
    assert floor_for_day(date(2026, 9, 7), LAUNCH) == PLUS_4WK_FLOOR_ALPHA # day 28


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
