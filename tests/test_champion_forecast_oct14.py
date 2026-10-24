"""Champion forecast for OBI's what-if episodes (14 Oct 2026): read-only, busy
during the daily run, never scored."""
import json
import os
import threading
from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

import scripts.forecast_relay as R
from hope.forecast import champion_forecast as F

HK = "5Champ" + "x" * 42
EP = {"episode_id": "whatiff456a80b0e", "transition_key": "BUDGET:up",
      "payload": {"episode_metadata": {"outcome_horizons_days": [7, 14]}, "action_bundle": {}},
      "system_estimate": None}
BLOCK = {m: {"p10": -0.1, "p50": 0.0, "p90": 0.1} for m in ("cost_delta_pct", "conversions_delta_pct",
                                                              "efficiency_delta_pct")}
HORIZONS = {h: dict(BLOCK, goal_miss_probability=0.4, instability_risk=0.2) for h in ("7", "14", "28")}


@pytest.fixture
def ledger(tmp_path):
    st = tmp_path / "standing"
    st.mkdir()
    (st / "_promotion.json").write_text(json.dumps({"champion": HK}))
    (st / "_promotion_log.jsonl").write_text(
        json.dumps({"type": "initial_seat", "champion": "5Old", "day": "2026-09-01"}) + "\n"
        + json.dumps({"type": "promotion", "old": "5Old", "new": HK, "day": "2026-10-01"}) + "\n")
    for day, ref in (("2026-10-12", "kk/m1@sha256:old"), ("2026-10-13", "kk/m1@sha256:new")):
        d = tmp_path / "shadow" / day
        d.mkdir(parents=True)
        (d / f"{HK}.jsonl").write_text(json.dumps({"image_digest": ref}) + "\n")
    return str(tmp_path)


def test_requests_must_be_whatif_and_small():
    assert F.validate({"episodes": [EP]}) == [EP]
    for bad in ({}, {"episodes": []}, {"episodes": [dict(EP, episode_id="abc")]},
                {"episodes": [EP] * 21}, {"episodes": [EP, EP]}, {"episodes": [dict(EP, payload=None)]}):
        with pytest.raises(F.BadRequest):
            F.validate(bad)


def test_the_model_reads_the_payload_with_its_id():
    (line,) = F.to_stdin([EP])
    assert line["episode_id"] == "whatiff456a80b0e" and "episode_metadata" in line and "payload" not in line


def test_champion_seat_date_and_latest_image(ledger):
    c = F.champion(ledger)
    assert c == {"hotkey": HK, "champion_since": "2026-10-01", "pinned_ref": "kk/m1@sha256:new"}


def test_answer_shape(ledger):
    seen = {}

    def runner(ref, stdin):
        seen["ref"], seen["stdin"] = ref, stdin
        return NS(ok=True, predictions={"whatiff456a80b0e": HORIZONS}, error=None)
    status, body = F.answer({"episodes": [EP]}, ledger, runner, uid_lookup=lambda hk: 133)
    assert status == 200 and seen["ref"] == "kk/m1@sha256:new" and seen["stdin"][0]["episode_id"] == EP["episode_id"]
    assert body == {"model": {"uid": 133, "hotkey": HK, "image_digest": "sha256:new", "champion_since": "2026-10-01"},
                    "predictions": {"whatiff456a80b0e": HORIZONS}}


def test_every_failure_is_a_plain_503(ledger, tmp_path):
    ok = lambda ref, stdin: NS(ok=True, predictions={}, error=None)
    assert F.answer({"episodes": [EP]}, ledger, ok, busy_reason="the daily basket run is in progress")[0] == 503
    s, b = F.answer({"episodes": [EP]}, ledger, lambda r, e: NS(ok=False, predictions={}, error="timeout>75s"),
                    uid_lookup=lambda hk: None)
    assert s == 503 and "timeout" in b["reason"]

    def boom(ref, stdin):
        raise RuntimeError("x")
    assert F.answer({"episodes": [EP]}, ledger, boom, uid_lookup=lambda hk: None)[0] == 503
    empty = tmp_path / "empty"
    (empty / "standing").mkdir(parents=True)
    (empty / "standing" / "_promotion.json").write_text(json.dumps({"champion": None}))
    s, b = F.answer({"episodes": [EP]}, str(empty), ok)
    assert s == 503 and "empty" in b["reason"]
    assert F.answer({"episodes": []}, ledger, ok)[0] == 400


def test_nothing_is_written_to_the_ledger(ledger):
    before = sorted(os.path.join(dp, f) for dp, _, fs in os.walk(ledger) for f in fs)
    F.answer({"episodes": [EP]}, ledger, lambda r, e: NS(ok=True, predictions={EP["episode_id"]: HORIZONS}, error=None),
             uid_lookup=lambda hk: 1)
    after = sorted(os.path.join(dp, f) for dp, _, fs in os.walk(ledger) for f in fs)
    assert before == after


def test_relay_answers_busy_and_stale_without_running():
    calls = []
    handle = lambda body: calls.append(body) or (200, {})
    assert R.serve_one({"id": "a", "created": 0, "body": {}}, handle, "the daily basket run is due")["status"] == 503
    assert R.serve_one({"id": "a", "created": 0, "body": {}}, handle, None, now=lambda: 1000)["status"] == 503
    assert R.serve_one({"id": "a", "created": 990, "body": {"x": 1}}, handle, None, now=lambda: 1000) == \
        {"status": 200, "body": {}}
    assert calls == [{"x": 1}]


def test_relay_loop_delivers_and_survives_errors():
    stop = threading.Event()
    sent, n = [], {"i": 0}

    def fetch(wait, busy):
        n["i"] += 1
        if n["i"] == 1:
            raise OSError("api restarting")
        if n["i"] == 2:
            import time
            return {"id": "r1", "created": time.time(), "body": {"episodes": []}}
        stop.set()
        return None
    R.loop(stop, lambda: None, lambda b: (400, {"error": "x"}), fetch=fetch,
           send=lambda rid, res: sent.append((rid, res)), log=lambda m: None, sleep=lambda s: None)
    assert sent == [("r1", {"status": 400, "body": {"error": "x"}})]


def test_daemon_is_busy_from_the_trigger_until_the_run_record(monkeypatch, tmp_path):
    import scripts.executor_daemon as D
    monkeypatch.setattr(D, "LEDGER_ROOT", str(tmp_path))
    monkeypatch.setattr(D, "TRIGGER_HOUR_UTC", 8)
    assert D.busy_reason(datetime(2026, 10, 14, 7, 0, tzinfo=timezone.utc)) is None
    assert D.busy_reason(datetime(2026, 10, 14, 8, 1, tzinfo=timezone.utc)) == "the daily basket run is due"
    (tmp_path / "pipeline_runs").mkdir()
    (tmp_path / "pipeline_runs" / "2026-10-14.json").write_text("{}")
    assert D.busy_reason(datetime(2026, 10, 14, 9, 40, tzinfo=timezone.utc)) is None
    D._BUSY["reason"] = "the daily basket run is in progress"
    try:
        assert D.busy_reason(datetime(2026, 10, 14, 15, 0, tzinfo=timezone.utc)) == "the daily basket run is in progress"
    finally:
        D._BUSY["reason"] = None
