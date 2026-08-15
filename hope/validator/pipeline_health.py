"""Assess the daily pipeline's health from its reported heartbeat.

Pure and side-effect free so every case can be unit-tested: given the latest
heartbeat (or its absence) and the current time, decide whether to alert and
say WHY. The watcher (scripts/check_pipeline_health.py) does the I/O; this module
does the judgement.

Levels:
  OK        — ran today and every stage succeeded, OR today's run is not yet due.
  DEGRADED  — ran today but a stage failed (scores/vector may be incomplete).
  DOWN      — no heartbeat at all, or the pipeline has not run when it should have.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

LEVEL_OK = "OK"
LEVEL_DEGRADED = "DEGRADED"
LEVEL_DOWN = "DOWN"


@dataclass
class Verdict:
    level: str
    reasons: list = field(default_factory=list)
    day: str | None = None

    def alerting(self) -> bool:
        return self.level != LEVEL_OK


def _parse_date(v):
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def assess(heartbeat: dict | None, now: datetime,
           trigger_hour: int = 11, grace_hours: int = 6) -> Verdict:
    """`heartbeat` is the API's latest run report ({day, ok, summary}) or None.

    `trigger_hour`+`grace_hours` define when today's run is expected to exist:
    the basket is delivered ~09:30 UTC and the pipeline triggers at `trigger_hour`
    (default 11); we only call a MISSING today's run DOWN once we are past
    trigger+grace (default 17:00 UTC), so a normal morning is never a false alarm.
    """
    if not heartbeat:
        return Verdict(LEVEL_DOWN,
                       ["no pipeline heartbeat reported — the executor has never "
                        "checked in (service down, or never deployed with the "
                        "heartbeat)"])

    hb_day = _parse_date(heartbeat.get("day"))
    if hb_day is None:
        return Verdict(LEVEL_DOWN, ["heartbeat carried no valid day"])

    today = now.date()
    ok = bool(heartbeat.get("ok"))
    summary = heartbeat.get("summary") or {}
    failed = summary.get("failed_stages") or []

    if hb_day == today:
        if ok:
            return Verdict(LEVEL_OK, ["ran today; all stages ok"], str(hb_day))
        return Verdict(LEVEL_DEGRADED,
                       [f"ran today but stage(s) failed: {', '.join(failed) or 'unknown'}"],
                       str(hb_day))

    if hb_day > today:   # clock skew or a future-dated report; not an outage
        return Verdict(LEVEL_OK, [f"latest run dated {hb_day} (ahead of now)"], str(hb_day))

    days_behind = (today - hb_day).days
    if days_behind >= 2:
        return Verdict(LEVEL_DOWN,
                       [f"pipeline last ran {hb_day} — {days_behind} days behind"],
                       str(hb_day))

    # exactly one day behind: DOWN only once today's run is actually due.
    past_due = now.hour >= (trigger_hour + grace_hours)
    if past_due:
        return Verdict(LEVEL_DOWN,
                       [f"today's pipeline has not run (last run {hb_day}, now past "
                        f"{trigger_hour + grace_hours}:00 UTC)"], str(hb_day))
    return Verdict(LEVEL_OK,
                   [f"today's run not due yet (last run {hb_day})"], str(hb_day))
