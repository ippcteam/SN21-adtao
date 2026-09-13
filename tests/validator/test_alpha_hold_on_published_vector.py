"""The alpha hold is applied to the vector the executor PUBLISHES.

SN21_STAKING.md and SN21_TRANSITION_PLAN.md say a miner below the day's hold
is not paid. The chain-path gate existed but the published vector, the
allocation audit and the daily report never saw it, so the leaderboard could
show a miner earning with a fraction of the hold. These tests pin the executor
half: weights move only when enforcement is on, standings never move, and the
audit always says what the gate found.
"""

import inspect

from hope.validator import daily_loop
from hope.validator.daily_loop import apply_alpha_hold
from hope.validator.daily_stream_weights import DailyAllocation

DAY = daily_loop.date(2026, 9, 13)
ON = {"SN21_COLLATERAL_ENFORCE": "1"}
OFF: dict = {}


def _alloc(weights=None, audit=None):
    weights = weights if weights is not None else {"a": 0.5, "b": 0.3, "c": 0.2}
    return DailyAllocation(day=DAY, gated=False, day_episode_volume=300,
                           standings={"a": 0.03, "b": 0.02, "c": 0.01},
                           weights=weights,
                           earning_set_size=len(weights),
                           collapse_audit=dict(audit or {"policies": {"tenure": {}}}))


ALPHA = {"a": 900.0, "b": 100.0, "c": 1200.0}


def test_enforced_drops_the_hotkey_below_the_hold_and_renormalises():
    out = apply_alpha_hold(_alloc(), 700.0, ALPHA, environ=ON)
    assert "b" not in out.weights
    assert round(out.weights["a"], 6) == round(0.5 / 0.7, 6)
    assert round(out.weights["c"], 6) == round(0.2 / 0.7, 6)
    assert out.earning_set_size == 2
    # standings are facts; a hotkey that is not paid still scored
    assert out.standings == {"a": 0.03, "b": 0.02, "c": 0.01}
    block = out.collapse_audit["alpha_hold"]
    assert block == {"floor_alpha": 700.0, "enforced": True,
                     "below_floor": {"b": 100.0}, "excluded": ["b"],
                     "unreadable_kept": []}
    status = out.collapse_audit["policies"]["alpha_hold"]
    assert status["applied"] is True and status["excluded"] == 1
    # the other controls' status survives
    assert "tenure" in out.collapse_audit["policies"]


def test_observing_leaves_the_vector_alone_but_names_who_would_go():
    out = apply_alpha_hold(_alloc(), 700.0, ALPHA, environ=OFF)
    assert out.weights == {"a": 0.5, "b": 0.3, "c": 0.2}
    assert out.earning_set_size == 3
    block = out.collapse_audit["alpha_hold"]
    assert block["enforced"] is False
    assert block["below_floor"] == {"b": 100.0}
    assert block["excluded"] == []
    status = out.collapse_audit["policies"]["alpha_hold"]
    assert status["applied"] is False and status["below_floor"] == 1
    assert status["excluded"] == 0


def test_no_identities_means_not_applied_and_says_so():
    out = apply_alpha_hold(_alloc(), 700.0, None, environ=ON)
    assert out.weights == {"a": 0.5, "b": 0.3, "c": 0.2}
    status = out.collapse_audit["policies"]["alpha_hold"]
    assert status["applied"] is False
    assert "not applied" in status["reason"]
    assert "alpha_hold" not in out.collapse_audit


def test_an_unreadable_hotkey_is_kept_and_named():
    out = apply_alpha_hold(_alloc(), 700.0, {"a": 900.0, "b": 100.0},
                           environ=ON)
    assert "c" in out.weights
    assert out.collapse_audit["alpha_hold"]["unreadable_kept"] == ["c"]
    assert out.collapse_audit["alpha_hold"]["excluded"] == ["b"]


def test_it_refuses_to_empty_the_vector():
    out = apply_alpha_hold(_alloc(), 700.0, {"a": 1.0, "b": 1.0, "c": 1.0},
                           environ=ON)
    assert out.weights == {"a": 0.5, "b": 0.3, "c": 0.2}
    status = out.collapse_audit["policies"]["alpha_hold"]
    assert status["applied"] is False
    assert "refusing to empty" in status["reason"]


def test_the_loop_reads_alpha_and_applies_the_hold_before_publishing():
    """Wiring, asserted (see test_control_consumers): a gate nothing calls is
    the defect this whole file exists to prevent."""
    assert "alpha_reader" in inspect.signature(daily_loop.run_daily_loop).parameters
    src = inspect.getsource(daily_loop.run_daily_loop)
    assert "alloc = apply_alpha_hold(alloc, effective_floor, alpha_of" in src
    before = src.index("apply_alpha_hold(")
    assert before < src.index('f"intended_weights_{day}.json"'), \
        "the hold must be applied before the vector is written"


def test_the_executor_entrypoint_supplies_the_alpha_map():
    import scripts.run_daily_pipeline as entry

    src = inspect.getsource(entry.stage_settle)
    assert "alpha_reader=alpha_reader" in src
    assert "_shared_metagraph_readers()" in src
