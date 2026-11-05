"""--interrupted-day cancels the charges for models a cut-off run never reached."""

import json
import os
import sys

from hope.scoring.standing_ledger import load_cancellations
from scripts import cancel_absence_charges as C


def _ledger(root, marker):
    d = os.path.join(root, "shadow", "2026-11-03")
    os.makedirs(d)
    open(os.path.join(d, "a.jsonl"), "w").write(json.dumps({"episodes_in": 2, "predictions_out": 1}) + "\n")
    if marker:
        open(os.path.join(d, "_run.json"), "w").write("{}")
    from hope.scoring.absence_penalty import _log_path
    os.makedirs(os.path.dirname(_log_path(root)), exist_ok=True)
    with open(_log_path(root), "w") as f:
        f.writelines(json.dumps({"day": "2026-11-03", "hotkey": hk, "missed": missed, "score": 0.0}) + "\n" for hk, missed in (("a", 1), ("b", 2), ("c", 2)))


def _run(monkeypatch, root, *extra):
    monkeypatch.setattr(sys, "argv", ["x", "--ledger-root", root, "--interrupted-day", "2026-11-03", *extra])
    return C.main()


def test_cancels_only_the_models_with_no_record(tmp_path, monkeypatch):
    root = str(tmp_path)
    _ledger(root, marker=False)
    assert _run(monkeypatch, root) == 0 and load_cancellations(root) == []
    assert _run(monkeypatch, root, "--apply") == 0
    assert sorted(c["hotkey"] for c in load_cancellations(root)) == ["b", "c"]
    assert _run(monkeypatch, root, "--apply") == 1          # nothing left


def test_refuses_a_finished_day(tmp_path, monkeypatch):
    root = str(tmp_path)
    _ledger(root, marker=True)
    assert _run(monkeypatch, root, "--apply") == 1


def test_a_hotkey_outside_the_days_plan_keeps_its_charge(tmp_path, monkeypatch):
    from hope.backtest import shadow
    root = str(tmp_path)
    _ledger(root, marker=False)
    shadow.record_run_plan(root, "2026-11-03", ["a", "b"])
    shadow.record_run_plan(root, "2026-11-03", ["a"])          # a resume adds, never shrinks
    assert shadow.day_plan(root, "2026-11-03") == {"a", "b"}
    assert _run(monkeypatch, root, "--apply") == 0
    assert [c["hotkey"] for c in load_cancellations(root)] == ["b"]
