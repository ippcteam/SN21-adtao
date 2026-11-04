"""The executor daemon catches up a day that never got a run record (1 Nov 2026)."""
from datetime import date

from scripts import executor_daemon as d

TODAY = date(2026, 11, 5)


def _env(**kw):
    return {"SN21_CATCHUP_FROM": "2026-11-01", **kw}


def test_off_when_unset_or_unreadable():
    never = lambda day: False
    assert d.missed_days(TODAY, {}, never) == []
    assert d.missed_days(TODAY, {"SN21_CATCHUP_FROM": "soon"}, never) == []
    assert d.missed_days(TODAY, _env(SN21_CATCHUP_DAYS="two"), never) == []


def test_missing_days_oldest_first_today_excluded():
    have = {"2026-11-04"}
    got = d.missed_days(TODAY, _env(SN21_CATCHUP_DAYS="3"), lambda day: day in have)
    assert got == ["2026-11-02", "2026-11-03"]


def test_never_before_the_switch_date():
    got = d.missed_days(TODAY, {"SN21_CATCHUP_FROM": "2026-11-04", "SN21_CATCHUP_DAYS": "7"},
                        lambda day: False)
    assert got == ["2026-11-04"]


def test_window_is_capped():
    got = d.missed_days(TODAY, {"SN21_CATCHUP_FROM": "2026-01-01", "SN21_CATCHUP_DAYS": "30"},
                        lambda day: False)
    assert len(got) == d.CATCHUP_MAX_DAYS and got[0] == "2026-10-29"


def test_default_window_is_two_days(monkeypatch):
    monkeypatch.setattr(d, "run_record_exists", lambda day: False)
    got = d.missed_days(TODAY, _env(), lambda day: False)
    assert got == ["2026-11-03", "2026-11-04"]


def test_catch_up_runs_each_and_keeps_going_after_a_failure(monkeypatch):
    monkeypatch.setattr(d, "run_record_exists", lambda day: False)
    ran = []

    def runner(day):
        ran.append((day, d._BUSY["reason"]))
        return 1 if day == "2026-11-03" else 0

    out = d.catch_up(TODAY, runner, _env())
    assert out == [("2026-11-03", 1), ("2026-11-04", 0)]
    assert [r[0] for r in ran] == ["2026-11-03", "2026-11-04"]
    assert all("missed day" in r[1] for r in ran)   # the forecast relay sees the run
    assert d._BUSY["reason"] is None


def test_catch_up_does_nothing_when_off():
    assert d.catch_up(TODAY, lambda day: 0, {}) == []


def test_pipeline_gets_the_day_explicitly(monkeypatch):
    seen = {}

    class P:
        returncode = 0

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd
        return P()

    monkeypatch.setattr(d.subprocess, "run", fake_run)
    assert d.run_pipeline("2026-11-03") == 0
    i = seen["cmd"].index("--day")
    assert seen["cmd"][i + 1] == "2026-11-03"
