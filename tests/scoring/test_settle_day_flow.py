"""Settle-day scoring glue — shadow predictions × settled outcomes → ledger."""

import json
import os
from datetime import date

import pytest

from hope.scoring import standing_ledger as sl
from hope.scoring.episode_average import standing
from hope.scoring.settle_day_flow import (
    SettledHorizon,
    load_prediction_index,
    run_settle_day,
    score_entry,
    score_settled,
    settled_days,
)

DAY = date(2026, 8, 11)


def _pred(cost_p50, spread=0.3):
    return {
        m: {"p10": p50 - spread, "p50": p50, "p90": p50 + spread}
        for m, p50 in (
            ("cost_delta_pct", cost_p50),
            ("conversions_delta_pct", cost_p50 / 2),
            ("efficiency_delta_pct", -cost_p50 / 4),
        )
    }


def _actual(cost=0.4):
    return {"cost_delta_pct": cost, "conversions_delta_pct": cost / 2,
            "efficiency_delta_pct": -cost / 4}


# ---- pure per-entry scorer ---------------------------------------------------

def test_perfect_prediction_beats_wrong_direction():
    good = score_entry(_pred(0.4), _actual(0.4))
    bad = score_entry(_pred(-0.4), _actual(0.4))
    assert good > bad
    assert good > 0.8          # tight quantiles on the truth + full direction
    assert bad < 0.5


def test_zero_p50_on_nonzero_actual_is_direction_miss():
    abstainer = score_entry(_pred(0.0), _actual(0.4))
    committed = score_entry(_pred(0.4), _actual(0.4))
    assert committed > abstainer
    # abstainer's direction term is exactly 0
    quantile_only = score_entry(_pred(0.0), _actual(0.4))
    assert quantile_only == pytest.approx(
        score_entry(_pred(0.0), _actual(0.4)))  # deterministic


def test_missing_metric_scores_quantile_nothing_and_direction_miss():
    partial = {"cost_delta_pct": {"p10": 0.1, "p50": 0.4, "p90": 0.7}}
    full = score_entry(_pred(0.4), _actual(0.4))
    part = score_entry(partial, _actual(0.4))
    assert 0.0 < part < 1.0
    assert part < full


def test_empty_prediction_scores_zero():
    assert score_entry({}, _actual(0.4)) == 0.0


def test_saturated_actual_does_not_blow_up():
    s = score_entry(_pred(0.4), _actual(9999.999999))
    assert 0.0 <= s <= 1.0


# ---- settled-day matching ----------------------------------------------------

def _index(miners=("alpha", "beta")):
    return {"ep1": {m: {"7": _pred(0.4), "14": _pred(0.3)} for m in miners}}


def _outcomes(day=DAY):
    return [SettledHorizon("ep1", 7, 0.4, 0.2, -0.1, day)]


def test_score_settled_emits_per_miner_results():
    res = score_settled(_index(), _outcomes(), DAY)
    assert {r.miner for r in res} == {"alpha", "beta"}
    assert all(r.episode_id == "ep1" and r.horizon_days == 7 for r in res)
    assert all(r.finalized_on == DAY for r in res)


def test_missing_prediction_yields_no_entry_not_zero():
    index = {"ep1": {"alpha": {"7": _pred(0.4)}}}  # beta never predicted
    res = score_settled(index, _outcomes(), DAY)
    assert [r.miner for r in res] == ["alpha"]


def test_other_day_outcomes_ignored():
    res = score_settled(_index(), _outcomes(day=date(2026, 8, 12)), DAY)
    assert res == []


def test_unpredicted_horizon_skipped():
    index = {"ep1": {"alpha": {"14": _pred(0.3)}}}  # no 7d prediction
    res = score_settled(index, _outcomes(), DAY)
    assert res == []


# ---- shadow-ledger index -----------------------------------------------------

def _write_shadow(root, day, hotkey, predictions):
    d = os.path.join(root, "shadow", day)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{hotkey}.jsonl"), "a") as f:
        f.write(json.dumps({"day": day, "hotkey": hotkey,
                            "predictions": predictions}) + "\n")


def test_index_spans_days_and_last_record_wins(tmp_path):
    root = str(tmp_path)
    _write_shadow(root, "2026-07-27", "alpha", {"ep1": {"7": _pred(0.1)}})
    _write_shadow(root, "2026-07-28", "alpha", {"ep2": {"7": _pred(0.2)}})
    # re-run of day 27 supersedes for the same (episode, miner)
    _write_shadow(root, "2026-07-27", "alpha", {"ep1": {"7": _pred(0.9)}})
    idx = load_prediction_index(root)
    assert set(idx) == {"ep1", "ep2"}
    assert idx["ep1"]["alpha"]["7"]["cost_delta_pct"]["p50"] == 0.9


# ---- end-to-end with ledger --------------------------------------------------

def test_run_settle_day_appends_and_drives_standing(tmp_path):
    shadow_root = str(tmp_path / "shadow_root")
    ledger_root = str(tmp_path / "ledger_root")
    _write_shadow(shadow_root, "2026-07-27", "alpha",
                  {"ep1": {"7": _pred(0.4)}, "ep2": {"7": _pred(0.4)}})

    provider = lambda day: [
        SettledHorizon("ep1", 7, 0.4, 0.2, -0.1, day),
        SettledHorizon("ep2", 7, -0.4, -0.2, 0.1, day),  # alpha wrong here
    ]
    out = run_settle_day(shadow_root, ledger_root, DAY, provider)
    assert out["status"] == "settled"
    assert out["results_scored"] == 2
    assert out["entries_written"] == 2
    assert out["miners"] == 1

    entries = sl.load_entries(ledger_root, as_of=DAY)
    st = standing(entries["alpha"], as_of=DAY)
    assert st["average"] is not None
    assert 0.0 < st["average"] < 1.0
    # ep1 scored higher than ep2 (right vs wrong direction)
    scores = sorted(e.score for e in entries["alpha"])
    assert scores[0] < scores[1]


def test_run_settle_day_is_idempotent(tmp_path):
    shadow_root = str(tmp_path / "s")
    ledger_root = str(tmp_path / "l")
    _write_shadow(shadow_root, "2026-07-27", "alpha", {"ep1": {"7": _pred(0.4)}})
    provider = lambda day: [SettledHorizon("ep1", 7, 0.4, 0.2, -0.1, day)]

    first = run_settle_day(shadow_root, ledger_root, DAY, provider)
    second = run_settle_day(shadow_root, ledger_root, DAY, provider)
    assert first["status"] == "settled"
    assert second["status"] == "already_settled"
    assert str(DAY) in settled_days(ledger_root)
    # no double-append
    entries = sl.load_entries(ledger_root, as_of=DAY)
    assert len(entries["alpha"]) == 1
