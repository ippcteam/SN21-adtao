"""Every branch of the daily-pipeline health verdict.

The point of the watcher is that it fires when it should and stays quiet when it
should — a false alarm every normal morning would get muted, and a missed
outage is the whole failure we are guarding against. So each case is pinned.
"""
from datetime import datetime, timezone

import pytest

from hope.validator.pipeline_health import (
    LEVEL_DEGRADED,
    LEVEL_DOWN,
    LEVEL_OK,
    assess,
)


def at(y, m, d, h):
    return datetime(y, m, d, h, 0, tzinfo=timezone.utc)


def hb(day, ok=True, failed=None):
    return {"day": day, "ok": ok,
            "summary": {"failed_stages": failed or []}}


# ---- DOWN: nothing reported / stale ----------------------------------------

def test_no_heartbeat_at_all_is_down():
    v = assess(None, at(2026, 8, 16, 18))
    assert v.level == LEVEL_DOWN and v.alerting()


def test_heartbeat_without_valid_day_is_down():
    v = assess({"day": "not-a-date", "ok": True}, at(2026, 8, 16, 18))
    assert v.level == LEVEL_DOWN


def test_two_days_behind_is_down_regardless_of_hour():
    # last ran the 14th, now the 16th early morning — still DOWN (a whole day missed)
    v = assess(hb("2026-08-14"), at(2026, 8, 16, 3))
    assert v.level == LEVEL_DOWN and "2 days behind" in v.reasons[0]


def test_one_day_behind_past_due_is_down():
    # last ran yesterday, now 18:00 UTC (past trigger 11 + grace 6 = 17) — DOWN
    v = assess(hb("2026-08-15"), at(2026, 8, 16, 18))
    assert v.level == LEVEL_DOWN and "has not run" in v.reasons[0]


# ---- OK: healthy or simply not-yet-due -------------------------------------

def test_ran_today_all_ok_is_ok():
    v = assess(hb("2026-08-16", ok=True), at(2026, 8, 16, 12))
    assert v.level == LEVEL_OK and not v.alerting()


def test_one_day_behind_before_due_is_ok():
    # last ran yesterday, now 08:00 UTC — today's run isn't due yet, no alarm
    v = assess(hb("2026-08-15"), at(2026, 8, 16, 8))
    assert v.level == LEVEL_OK and "not due yet" in v.reasons[0]


def test_future_dated_heartbeat_is_not_an_outage():
    v = assess(hb("2026-08-17"), at(2026, 8, 16, 12))
    assert v.level == LEVEL_OK


# ---- DEGRADED: ran but a stage failed --------------------------------------

def test_ran_today_but_stage_failed_is_degraded():
    v = assess(hb("2026-08-16", ok=False, failed=["settle"]), at(2026, 8, 16, 12))
    assert v.level == LEVEL_DEGRADED and "settle" in v.reasons[0] and v.alerting()


def test_degraded_names_all_failed_stages():
    v = assess(hb("2026-08-16", ok=False, failed=["intake", "publish_weights"]),
               at(2026, 8, 16, 12))
    assert "intake" in v.reasons[0] and "publish_weights" in v.reasons[0]


@pytest.mark.parametrize("hour,expected", [
    (8, LEVEL_OK),     # before trigger+grace: not due
    (16, LEVEL_OK),    # 16:00 < 17:00 cutoff: still not due
    (17, LEVEL_DOWN),  # at the cutoff: due, and missing
    (23, LEVEL_DOWN),
])
def test_due_cutoff_boundary(hour, expected):
    v = assess(hb("2026-08-15"), at(2026, 8, 16, hour))
    assert v.level == expected
