"""A shadow day cut off part way is finished, not skipped (4 Nov 2026)."""

import os

import scripts.run_daily_pipeline as P
import scripts.run_shadow_day_bd as B
from hope.backtest import shadow
from hope.backtest.container_runner import RunResult
from hope.backtest.shadow import ShadowModel

EPS = [{"episode_id": "e1"}, {"episode_id": "e2"}]


def _setup(monkeypatch, ran):
    models = [ShadowModel(hotkey=h, image_digest=f"r@sha256:{h}", admitted_at="2026-11-04")
              for h in ("a", "b", "c")]
    monkeypatch.setattr(B, "admitted_models", lambda *a, **k: (list(models), {}))
    monkeypatch.setattr(P, "basket_runner", lambda: lambda img, eps: (
        ran.append(img) or RunResult(ok=True, predictions={"e1": {}}, episodes_in=len(eps),
                                     predictions_out=1)))
    monkeypatch.setattr(P, "_write_model_since", lambda root: None)


def test_a_cut_off_day_runs_only_the_models_with_no_record(tmp_path, monkeypatch):
    root, ran = str(tmp_path), []
    _setup(monkeypatch, ran)
    shadow.record_day(root, "2026-11-03", ShadowModel("a", "r@sha256:a", "x"),
                      RunResult(ok=True, predictions={"e1": {}}, episodes_in=2, predictions_out=1))
    before = open(os.path.join(shadow.shadow_dir(root, "2026-11-03"), "a.jsonl")).read()
    out = P.stage_shadow(root, "BD-2026-11-03", EPS, include_reference=False)
    assert sorted(ran) == ["r@sha256:b", "r@sha256:c"]
    assert out["resumed_after"] == 1 and out["models_run"] == 2
    assert open(os.path.join(shadow.shadow_dir(root, "2026-11-03"), "a.jsonl")).read() == before
    assert shadow.run_complete(root, "2026-11-03")


def test_a_finished_day_stays_locked(tmp_path, monkeypatch):
    root, ran = str(tmp_path), []
    _setup(monkeypatch, ran)
    P.stage_shadow(root, "BD-2026-11-03", EPS, include_reference=False)
    ran.clear()
    out = P.stage_shadow(root, "BD-2026-11-03", EPS, include_reference=False)
    assert ran == [] and "skipped" in out


def test_the_resume_cancels_the_charges_for_the_models_it_never_reached(tmp_path, monkeypatch):
    import json

    from hope.scoring.absence_penalty import _log_path
    from hope.scoring.standing_ledger import load_cancellations
    root, ran = str(tmp_path), []
    _setup(monkeypatch, ran)
    shadow.record_day(root, "2026-11-03", ShadowModel("a", "r@sha256:a", "x"),
                      RunResult(ok=True, predictions={"e1": {}}, episodes_in=2, predictions_out=1))
    os.makedirs(os.path.dirname(_log_path(root)), exist_ok=True)
    with open(_log_path(root), "w") as f:
        f.writelines(json.dumps({"day": "2026-11-03", "hotkey": hk, "missed": m, "score": 0.0}) + "\n"
                     for hk, m in (("a", 1), ("b", 2), ("c", 2)))
    P.stage_shadow(root, "BD-2026-11-03", EPS, include_reference=False)
    assert sorted(c["hotkey"] for c in load_cancellations(root)) == ["b", "c"]


def test_a_finished_day_runs_again_only_the_models_that_failed_on_our_side(tmp_path, monkeypatch):
    root, ran = str(tmp_path), []
    _setup(monkeypatch, ran)
    day = "2026-11-03"
    shadow.record_day(root, day, ShadowModel("a", "r@sha256:a", "x"),
                      RunResult(ok=True, predictions={"e1": {}}, episodes_in=2, predictions_out=1))
    shadow.record_day(root, day, ShadowModel("b", "r@sha256:b", "x"),
                      RunResult(ok=False, error="disk_low: 1500 MB free", episodes_in=2, predictions_out=0))
    shadow.record_day(root, day, ShadowModel("c", "r@sha256:c", "x"),
                      RunResult(ok=False, error="exit=1", episodes_in=2, predictions_out=0))
    shadow.record_run_marker(root, day, 2, 3)
    out = P.stage_shadow(root, "BD-2026-11-03", EPS, include_reference=False)
    assert ran == ["r@sha256:b"] and out["retried_ours"] == 1
    assert shadow.last_record(root, day, "b")["ok"] is True
    ran.clear()
    assert "skipped" in P.stage_shadow(root, "BD-2026-11-03", EPS, include_reference=False) and ran == []
