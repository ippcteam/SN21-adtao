"""Settle-day scoring glue — shadow predictions × settled outcomes → ledger.

Reworked with the 2026-07-29 audit fixes: per-(episode,horizon) entered
markers (no per-day race), settle-clock date basis, legacy-corpus guard.
"""

import json
import os
from datetime import date, timedelta

import pytest

from hope.scoring import standing_ledger as sl
from hope.scoring.episode_average import standing
from hope.scoring.settle_day_flow import (
    SettledHorizon,
    entered_results,
    load_prediction_index,
    run_settle_day,
    score_entry,
    score_settled,
    settle_date,
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


# ---- settle clock ------------------------------------------------------------

def test_settle_date_is_window_end_plus_1_h_7():
    we = date(2026, 7, 27)
    assert settle_date(we, 7) == date(2026, 8, 11)    # day 15
    assert settle_date(we, 14) == date(2026, 8, 18)   # day 22
    assert settle_date(we, 28) == date(2026, 9, 1)    # day 36


# ---- settled matching ----------------------------------------------------

def _index(miners=("alpha", "beta")):
    return {"ep1": {m: {"7": _pred(0.4), "14": _pred(0.3)} for m in miners}}


def _outcomes(day=DAY):
    return [SettledHorizon("ep1", 7, 0.4, 0.2, -0.1, day)]


def test_score_settled_emits_per_miner_results():
    res = score_settled(_index(), _outcomes())
    assert {r.miner for r in res} == {"alpha", "beta"}
    assert all(r.episode_id == "ep1" and r.horizon_days == 7 for r in res)
    assert all(r.finalized_on == DAY for r in res)


def test_missing_prediction_yields_no_entry_not_zero():
    index = {"ep1": {"alpha": {"7": _pred(0.4)}}}  # beta never predicted
    res = score_settled(index, _outcomes())
    assert [r.miner for r in res] == ["alpha"]


def test_results_carry_their_own_settle_date():
    """Late-measured rows enter DATED to their true settle date, not the
    run day (audit: date basis must be the settle clock)."""
    older = date(2026, 8, 9)
    res = score_settled(_index(("alpha",)),
                        [SettledHorizon("ep1", 7, 0.4, 0.2, -0.1, older)])
    assert res[0].finalized_on == older


def test_unpredicted_horizon_skipped():
    index = {"ep1": {"alpha": {"14": _pred(0.3)}}}  # no 7d prediction
    res = score_settled(index, _outcomes())
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
    scores = sorted(e.score for e in entries["alpha"])
    assert scores[0] < scores[1]


def test_run_settle_day_is_idempotent_per_pair(tmp_path):
    shadow_root = str(tmp_path / "s")
    ledger_root = str(tmp_path / "l")
    _write_shadow(shadow_root, "2026-07-27", "alpha", {"ep1": {"7": _pred(0.4)}})
    provider = lambda day: [SettledHorizon("ep1", 7, 0.4, 0.2, -0.1, day)]

    first = run_settle_day(shadow_root, ledger_root, DAY, provider)
    second = run_settle_day(shadow_root, ledger_root, DAY, provider)
    assert first["new_outcomes"] == 1 and first["entries_written"] == 1
    assert second["new_outcomes"] == 0 and second["entries_written"] == 0
    assert ("ep1", 7) in entered_results(ledger_root)
    entries = sl.load_entries(ledger_root, as_of=DAY)
    assert len(entries["alpha"]) == 1  # no double-append


def test_same_day_race_recovers_on_next_run(tmp_path):
    """THE audit scenario: a settle run executes before the day's second
    measurement lands. Old per-day marker skipped the late row forever;
    per-pair markers pick it up on the next run, dated correctly."""
    shadow_root = str(tmp_path / "s")
    ledger_root = str(tmp_path / "l")
    _write_shadow(shadow_root, "2026-07-27", "alpha",
                  {"ep1": {"7": _pred(0.4)}, "ep2": {"7": _pred(0.4)}})

    early = [SettledHorizon("ep1", 7, 0.4, 0.2, -0.1, DAY)]
    late = early + [SettledHorizon("ep2", 7, 0.4, 0.2, -0.1, DAY)]

    run_settle_day(shadow_root, ledger_root, DAY, lambda d: early)
    # ep2's measurement lands later the same day; next run (same or next
    # day) sees it and enters ONLY ep2
    out = run_settle_day(shadow_root, ledger_root, DAY, lambda d: late)
    assert out["new_outcomes"] == 1
    assert out["entries_written"] == 1
    entries = sl.load_entries(ledger_root, as_of=DAY)
    assert len(entries["alpha"]) == 2  # both entered exactly once


def test_backfill_enters_on_true_settle_date(tmp_path):
    """A row settle-dated 3 days ago, measured late, enters dated to its
    settle date — standings age it correctly from that date."""
    shadow_root = str(tmp_path / "s")
    ledger_root = str(tmp_path / "l")
    _write_shadow(shadow_root, "2026-07-27", "alpha", {"ep1": {"7": _pred(0.4)}})
    settle = DAY - timedelta(days=3)
    provider = lambda d: [SettledHorizon("ep1", 7, 0.4, 0.2, -0.1, settle)]

    out = run_settle_day(shadow_root, ledger_root, DAY, provider)
    assert out["settle_dates"] == [str(settle)]
    entries = sl.load_entries(ledger_root, as_of=DAY)
    assert entries["alpha"][0].scored_on == settle
