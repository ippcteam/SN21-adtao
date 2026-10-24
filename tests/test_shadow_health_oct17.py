"""The shadow run reports failures on our side and low disk (17 Oct 2026).

5-11 Oct: 58-66 models a day were refused because the disk was under 2 GB.
Each was excused from absence (our fault) and the stage succeeded, so nothing
reported it for a week and ~60 miners lost seven days of results."""
from scripts import run_daily_pipeline as P

OCT5 = {**{f"hk{i}": {"ok": False, "predictions": 0, "error": "disk_low: 1959 MB free < 2048 MB required"}
           for i in range(58)},
        **{f"ok{i}": {"ok": True, "predictions": 1056, "error": None} for i in range(39)},
        **{f"slow{i}": {"ok": False, "predictions": 0, "error": "timeout>300s"} for i in range(3)}}


def test_the_5_october_run_is_reported_as_a_problem():
    h = P.shadow_run_health(OCT5, {"/var/data/sn21/ledger": 1959})
    assert h["ok"] == 39 and h["failed"] == 61
    assert h["failed_by_error"] == {"disk_low": 58, "timeout": 3}
    assert h["operator_faults"] == 61                    # all excused from absence
    assert any("61 model(s) failed on our side" in p for p in h["problems"])
    assert any("low disk" in p for p in h["problems"])


def test_a_miner_side_failure_is_counted_but_is_not_our_problem():
    h = P.shadow_run_health({"a": {"ok": True}, "b": {"ok": False, "error": "exit=1: Traceback"}},
                            {"/var/data": 14000})
    assert h["failed"] == 1 and h["operator_faults"] == 0 and h["problems"] == []


def test_a_clean_run_with_room_reports_nothing():
    h = P.shadow_run_health({"a": {"ok": True}}, {"/var/data": 14000, "/tmp": 9000})
    assert h["problems"] == [] and h["disk_free_mb"] == {"/var/data": 14000, "/tmp": 9000}


def test_disk_free_reads_each_volume_and_skips_missing(tmp_path):
    d = P.disk_free_mb(str(tmp_path), "/no/such/path")
    assert list(d) == [str(tmp_path)] and d[str(tmp_path)] > 0


def test_the_heartbeat_carries_the_problems(monkeypatch):
    sent = {}
    monkeypatch.setattr(P, "_api_post", lambda path, body: sent.update(body) or {"success": True})
    rec = {"stages": {"shadow": {"models_run": 100, "health": P.shadow_run_health(OCT5, {"/x": 1959})},
                      "settle": {"status": "settled"}}}
    out = P.publish_pipeline_heartbeat("2026-10-06", rec)
    assert out["ok"] is True                              # no stage errored...
    assert len(sent["summary"]["problems"]) == 2          # ...but the problems are carried
