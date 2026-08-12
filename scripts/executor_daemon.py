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
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

TRIGGER_HOUR_UTC = int(os.environ.get("SN21_PIPELINE_TRIGGER_HOUR_UTC", "11"))
CHECK_INTERVAL_S = int(os.environ.get("SN21_PIPELINE_CHECK_INTERVAL_S", "3600"))
LEDGER_ROOT = os.environ.get("SN21_LEDGER_ROOT", "/var/data/sn21/ledger")


def log(msg):
    print(f"[executor-daemon] {datetime.now(timezone.utc).isoformat()} {msg}",
          flush=True)


def run_record_exists(day: str) -> bool:
    return os.path.exists(os.path.join(LEDGER_ROOT, "pipeline_runs", f"{day}.json"))


def run_pipeline(day: str) -> int:
    log(f"running the daily pipeline for {day}")
    proc = subprocess.run(
        [sys.executable, "-m", "scripts.run_daily_pipeline"],
        cwd=os.path.join(os.path.dirname(__file__), os.pardir))
    log(f"pipeline for {day} exited {proc.returncode}")
    return proc.returncode


def main():
    log(f"start: trigger>={TRIGGER_HOUR_UTC}:00 UTC, check every "
        f"{CHECK_INTERVAL_S}s, ledger={LEDGER_ROOT}")
    while True:
        now = datetime.now(timezone.utc)
        day = now.date().isoformat()
        try:
            if now.hour >= TRIGGER_HOUR_UTC and not run_record_exists(day):
                run_pipeline(day)
            else:
                reason = ("already ran today" if run_record_exists(day)
                          else f"before trigger hour ({now.hour} < "
                               f"{TRIGGER_HOUR_UTC})")
                log(f"idle — {reason}")
        except Exception as exc:   # noqa: BLE001 - the supervisor must never die
            log(f"ERROR in check loop (continuing): {exc}")
        time.sleep(CHECK_INTERVAL_S)


if __name__ == "__main__":
    main()
