"""The executor's supervisor — runs the daily pipeline once per UTC day.

    python3 -m scripts.executor_daemon

WHY A SELF-CONTAINED LOOP, NOT A RENDER CRON: the pipeline needs the sandbox
AND the persistent disk (the ledger accumulates predictions that settle 15+
days later), and both live on this one service. A separate cron job cannot
share the disk. So this service supervises itself.

RUN-ONCE-PER-DAY, RESTART-SAFE: the loop wakes hourly. It runs the pipeline
for a given UTC day only if (a) the clock is past the trigger hour (the basket
is delivered ~09:30 UTC; default trigger 11:00) and (b) no run record for that
day exists yet on the disk. A restart mid-day therefore neither skips nor
double-runs the day — the run record on the persistent disk is the memory.

The pipeline itself is idempotent within a day (intake skips verdicted
digests, settle uses entered-markers), but the shadow EXECUTION is not
free — re-running it would run every model again — so the once-per-day guard
is about cost and cleanliness, not correctness.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

TRIGGER_HOUR_UTC = int(os.environ.get("SN21_PIPELINE_TRIGGER_HOUR_UTC", "11"))
CHECK_INTERVAL_S = int(os.environ.get("SN21_PIPELINE_CHECK_INTERVAL_S", "3600"))
LEDGER_ROOT = os.environ.get("SN21_LEDGER_ROOT", "/var/data/sn21/ledger")

# Operator override: SN21_RERUN_DAY=YYYY-MM-DD makes that one day rerunnable
# even though its run record exists — for the case where a run completed
# against bad inputs (e.g. the 25 Aug run that scored a stale basket) and the
# day must be redone. Nothing is deleted: the loop just stops treating the
# existing record as "done" for that day until a rerun exits 0, whose record
# then overwrites the old one. Unset the var once the rerun lands.
RERUN_DAY = os.environ.get("SN21_RERUN_DAY", "")

# Missed-day catch-up (1 Nov 2026). The loop above only ever runs TODAY: a day
# whose run never got a record (the basket never resolved, the host was down,
# a deploy killed the run before its record) was dropped at midnight and needed
# a hand-run from the host's shell. With SN21_CATCHUP_FROM=YYYY-MM-DD set, each
# tick first runs every day of the last SN21_CATCHUP_DAYS (default 2, at most 7)
# that is on or after that date and has no run record, oldest first, then
# today. Oldest first keeps settle, receipts and published weights in day
# order (a skipped day otherwise lands in the next day's receipt as a bundle).
# It runs at any hour, so a day dropped at midnight is usually redone before
# the next morning's trigger. A catch-up that fails is retried on the next
# tick while the day is inside the window. The day runs the models admitted
# when it runs, as the hand-run always has. Unset = off.
CATCHUP_MAX_DAYS = 7

# Admission is the expensive stage (pull + two runs per NEW model), and it is
# idempotent — a gated digest is never re-gated. Bounding it per day spreads
# the one-time gating of the backlog over a few days instead of one multi-hour
# run, while a fully-gated field then costs nothing. Corpus size trades gate
# robustness against time.
INTAKE_LIMIT = int(os.environ.get("SN21_INTAKE_LIMIT", "25"))
CORPUS_SIZE = int(os.environ.get("SN21_CORPUS_SIZE", "60"))


def log(msg):
    print(f"[executor-daemon] {datetime.now(timezone.utc).isoformat()} {msg}",
          flush=True)


def run_record_exists(day: str) -> bool:
    return os.path.exists(os.path.join(LEDGER_ROOT, "pipeline_runs", f"{day}.json"))


def missed_days(today: date, environ=os.environ, exists=run_record_exists) -> list:
    """Past days to catch up, oldest first: inside the last SN21_CATCHUP_DAYS,
    on or after SN21_CATCHUP_FROM, with no run record. Empty when the switch is
    unset or unreadable (a bad value must not run old days)."""
    raw = (environ.get("SN21_CATCHUP_FROM") or "").strip()
    if not raw:
        return []
    try:
        floor = date.fromisoformat(raw)
        n = int(environ.get("SN21_CATCHUP_DAYS", "2"))
    except ValueError:
        return []
    n = max(0, min(n, CATCHUP_MAX_DAYS))
    days = (today - timedelta(days=k) for k in range(n, 0, -1))
    return [d.isoformat() for d in days if d >= floor and not exists(d.isoformat())]


def catch_up(today: date, runner=None, environ=os.environ) -> list:
    """Run each missed day, oldest first; [(day, exit code)]. A failed day is
    logged and left for the next tick; the later days and today still run."""
    runner = runner or run_pipeline
    done = []
    for day in missed_days(today, environ):
        log(f"catch-up: {day} has no run record — running it before today")
        _BUSY["reason"] = f"a missed day ({day}) is being run"
        try:
            rc = runner(day)
        finally:
            _BUSY["reason"] = None
        if rc != 0:
            log(f"catch-up: {day} exited {rc} — will retry next tick")
        done.append((day, rc))
    return done


def run_pipeline(day: str) -> int:
    log(f"running the daily pipeline for {day} "
        f"(intake_limit={INTAKE_LIMIT}, corpus={CORPUS_SIZE})")
    # The day is passed explicitly: a run started just before midnight must
    # not become the next day's run inside the pipeline.
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.run_daily_pipeline",
         "--day", day,
         "--no-reference",              # the reference image is not digest-pullable here
         "--intake-limit", str(INTAKE_LIMIT),
         "--corpus-size", str(CORPUS_SIZE)],
        cwd=os.path.join(os.path.dirname(__file__), os.pardir), check=False)
    log(f"pipeline for {day} exited {proc.returncode}")
    return proc.returncode


def backfill_tkey_maps_if_sparse(min_maps: int = 10) -> None:
    """One-time self-heal: label maps for baskets that predate the maps.

    The per-basket transition-key maps (tkeys/) are written at resolve time,
    so baskets that ran before the maps existed have none — and their entries
    label as UNKNOWN on the by-type page for up to 36 days. The backfill is
    idempotent (existing maps are skipped), needs the ledger disk, and this
    daemon is the only process that mounts it — one-off jobs do not — so the
    daemon runs it at startup when the maps are sparse. On a healthy host
    this is one log line and no work.
    """
    d = os.path.join(LEDGER_ROOT, "tkeys")
    have = (len([f for f in os.listdir(d) if f.endswith(".json")])
            if os.path.isdir(d) else 0)
    if have >= min_maps:
        log(f"tkey maps present ({have}) — no backfill needed")
        return
    log(f"tkey maps sparse ({have} < {min_maps}) — running backfill")
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.backfill_transition_key_maps",
         "--ledger-root", LEDGER_ROOT, "--days", "40"],
        cwd=os.path.join(os.path.dirname(__file__), os.pardir), check=False)
    log(f"tkey backfill exited {proc.returncode}")


# Why a champion forecast would be "busy" right now: the daily run is due or
# running. Read by the forecast relay thread, written by the loop below.
_BUSY = {"reason": None}


def busy_reason(now=None):
    if _BUSY["reason"]:
        return _BUSY["reason"]
    now = now or datetime.now(timezone.utc)
    if now.hour >= TRIGGER_HOUR_UTC and not run_record_exists(now.date().isoformat()):
        return "the daily basket run is due"
    return None


def main():
    try:
        from scripts.forecast_relay import start as start_forecast_relay
        start_forecast_relay(LEDGER_ROOT, busy_reason, log=log)
    except Exception as e:   # the forecast must never stop the daemon
        log(f"forecast relay not started ({e})")
    log(f"start: trigger>={TRIGGER_HOUR_UTC}:00 UTC, check every "
        f"{CHECK_INTERVAL_S}s, ledger={LEDGER_ROOT}"
        + (f", rerun override for {RERUN_DAY}" if RERUN_DAY else "")
        + (f", catch-up from {os.environ.get('SN21_CATCHUP_FROM')} "
           f"({os.environ.get('SN21_CATCHUP_DAYS', '2')} days back)"
           if os.environ.get("SN21_CATCHUP_FROM") else ""))
    try:
        backfill_tkey_maps_if_sparse()
    except Exception as e:   # labelling must never block the daemon
        log(f"tkey backfill skipped ({e})")
    rerun_pending = bool(RERUN_DAY)
    while True:
        try:
            catch_up(datetime.now(timezone.utc).date())
        except Exception as exc:   # the supervisor must never die
            log(f"ERROR in catch-up (continuing): {exc}")
        now = datetime.now(timezone.utc)
        day = now.date().isoformat()
        rerun_today = rerun_pending and day == RERUN_DAY
        try:
            if now.hour >= TRIGGER_HOUR_UTC and (
                    rerun_today or not run_record_exists(day)):
                _BUSY["reason"] = "the daily basket run is in progress"
                try:
                    rc = run_pipeline(day)
                finally:
                    _BUSY["reason"] = None
                if rerun_today and rc == 0:
                    rerun_pending = False
                    log(f"rerun override for {RERUN_DAY} satisfied "
                        "(clean run recorded)")
            else:
                reason = ("already ran today" if run_record_exists(day)
                          else f"before trigger hour ({now.hour} < "
                               f"{TRIGGER_HOUR_UTC})")
                log(f"idle — {reason}")
        except Exception as exc:   # the supervisor must never die
            log(f"ERROR in check loop (continuing): {exc}")
        time.sleep(CHECK_INTERVAL_S)


if __name__ == "__main__":
    main()
