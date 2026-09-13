"""The alpha hold is judged inside the allocation, before the curve.

SN21_STAKING.md and SN21_TRANSITION_PLAN.md say a miner below the day's hold
is not paid. The chain-path gate existed but the published vector, the
allocation audit and the daily report never saw it. It is applied like
tenure: a hotkey under the hold is not seated, the next-ranked eligible miner
takes the seat, and the paid set keeps its published size. Standings never
move; the audit always says what the gate found.
"""

import inspect
from datetime import date

from hope.scoring.champion_promotion import PromotionState
from hope.scoring.episode_average import ScoredEpisode
from hope.scoring.weight_curve import CurveParams
from hope.validator import daily_loop
from hope.validator.daily_stream_weights import compute_daily_allocation

DAY = date(2026, 9, 13)


def _entries(**standings):
    return {hk: [ScoredEpisode(score=s, scored_on=DAY) for _ in range(300)]
            for hk, s in standings.items()}


def _alloc(standings, **kw):
    return compute_daily_allocation(
        _entries(**standings), DAY, day_episode_volume=500,
        promotion_state=PromotionState(), **kw)


FIELD = {"a": 0.80, "b": 0.78, "c": 0.76, "d": 0.74}
ALPHA = {"a": 900.0, "b": 100.0, "c": 1200.0, "d": 800.0}
TWO_SEATS = CurveParams(max_earners=2)


def test_enforced_unseats_the_hotkey_below_the_hold_and_the_next_takes_the_seat():
    out = _alloc(FIELD, alpha_of=ALPHA, alpha_floor=700.0,
                 alpha_hold_enforced=True, curve_params=TWO_SEATS)
    paid = {hk for hk, w in out.weights.items() if w > 0}
    assert paid == {"a", "c"}, "b is under the hold; c moves up"
    assert out.earning_set_size == 2
    # standings are facts; a hotkey that is not paid still scored and ranks
    assert set(out.standings) == set(FIELD)
    block = out.collapse_audit["alpha_hold"]
    assert block == {"floor_alpha": 700.0, "enforced": True,
                     "below_floor": {"b": 100.0}, "excluded": ["b"],
                     "unreadable_kept": []}
    status = out.collapse_audit["policies"]["alpha_hold"]
    assert status["applied"] is True and status["excluded"] == 1


def test_observing_leaves_the_seats_alone_but_names_who_would_go():
    out = _alloc(FIELD, alpha_of=ALPHA, alpha_floor=700.0,
                 alpha_hold_enforced=False, curve_params=TWO_SEATS)
    paid = {hk for hk, w in out.weights.items() if w > 0}
    assert paid == {"a", "b"}
    block = out.collapse_audit["alpha_hold"]
    assert block["enforced"] is False
    assert block["below_floor"] == {"b": 100.0}
    assert block["excluded"] == []
    status = out.collapse_audit["policies"]["alpha_hold"]
    assert status["applied"] is False and status["below_floor"] == 1


def test_no_identities_means_not_applied_and_says_so():
    out = _alloc(FIELD, alpha_of=None, alpha_floor=700.0,
                 alpha_hold_enforced=True, curve_params=TWO_SEATS)
    paid = {hk for hk, w in out.weights.items() if w > 0}
    assert paid == {"a", "b"}
    status = out.collapse_audit["policies"]["alpha_hold"]
    assert status["applied"] is False
    assert "not applied" in status["reason"]
    assert "alpha_hold" not in out.collapse_audit


def test_an_unreadable_hotkey_is_kept_and_named():
    out = _alloc(FIELD, alpha_of={"a": 900.0, "b": 100.0, "d": 800.0},
                 alpha_floor=700.0, alpha_hold_enforced=True,
                 curve_params=TWO_SEATS)
    paid = {hk for hk, w in out.weights.items() if w > 0}
    assert paid == {"a", "c"}
    assert out.collapse_audit["alpha_hold"]["unreadable_kept"] == ["c"]
    assert out.collapse_audit["alpha_hold"]["excluded"] == ["b"]


def test_it_stands_down_rather_than_empty_the_curve():
    out = _alloc(FIELD, alpha_of={hk: 1.0 for hk in FIELD}, alpha_floor=700.0,
                 alpha_hold_enforced=True, curve_params=TWO_SEATS)
    paid = {hk for hk, w in out.weights.items() if w > 0}
    assert paid == {"a", "b"}
    status = out.collapse_audit["policies"]["alpha_hold"]
    assert status["applied"] is False
    assert "empty" in status["reason"]
    assert out.collapse_audit["alpha_hold"]["stood_down"] is True


def test_a_zero_floor_enforces_nothing():
    out = _alloc(FIELD, alpha_of=ALPHA, alpha_floor=0.0,
                 alpha_hold_enforced=True, curve_params=TWO_SEATS)
    assert out.collapse_audit["policies"]["alpha_hold"]["applied"] is False
    assert "alpha_hold" not in out.collapse_audit


def test_the_loop_reads_alpha_and_hands_it_to_the_allocation():
    """Wiring, asserted (see test_control_consumers): a gate nothing calls is
    the defect this whole file exists to prevent."""
    assert "alpha_reader" in inspect.signature(daily_loop.run_daily_loop).parameters
    src = inspect.getsource(daily_loop.run_daily_loop)
    assert "alpha_of=alpha_of" in src
    assert "alpha_floor=float(effective_floor)" in src


def test_the_executor_entrypoint_supplies_the_alpha_map():
    import scripts.run_daily_pipeline as entry

    src = inspect.getsource(entry.stage_settle)
    assert "alpha_reader=alpha_reader" in src
    assert "_shared_metagraph_readers()" in src
