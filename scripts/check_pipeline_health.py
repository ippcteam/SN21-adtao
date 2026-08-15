"""Independent watcher for the SN21 daily pipeline.

Runs somewhere OTHER than the executor (a cron, a separate service) so it can
still fire when the executor itself is down. It reads the latest pipeline
heartbeat from the operator API, judges it (hope.validator.pipeline_health), and
on trouble posts to SN21_ALERT_WEBHOOK (Discord- or Slack-compatible) and exits
non-zero so any scheduler's own failure alerting also trips.

    python3 -m scripts.check_pipeline_health            # check + alert if unhealthy
    python3 -m scripts.check_pipeline_health --dry-run  # never posts; prints verdict
    SN21_ALERT_WEBHOOK=<url>  to enable the push alert (else logs only).

Exit code: 0 healthy, 1 alerting (DEGRADED/DOWN), 2 could not check.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from hope.validator.pipeline_health import assess  # noqa: E402


def _api_get(path):
    base = (os.environ.get("HOPE_API_URL") or "").rstrip("/")
    key = (os.environ.get("HOPE_API_KEY") or "").strip()
    if not base or not key:
        raise SystemExit("HOPE_API_URL and HOPE_API_KEY are required")
    req = urllib.request.Request(
        f"{base}/internal/bittensor/v1/{path}", headers={"X-API-Key": key})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode() or "{}")
        except Exception:
            return e.code, {}


def post_alert(message: str) -> bool:
    """Post to SN21_ALERT_WEBHOOK if set. Payload carries both Discord's
    `content` and Slack's `text`, so one URL works for either. Returns whether
    it posted."""
    url = (os.environ.get("SN21_ALERT_WEBHOOK") or "").strip()
    if not url:
        return False
    body = json.dumps({"content": message, "text": message}).encode()
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30):
            return True
    except Exception as exc:   # noqa: BLE001
        print(f"[alert] webhook POST failed: {exc}", flush=True)
        return False


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="print the verdict but never post the alert")
    p.add_argument("--trigger-hour", type=int,
                   default=int(os.environ.get("SN21_PIPELINE_TRIGGER_HOUR_UTC", "11")))
    p.add_argument("--grace-hours", type=int,
                   default=int(os.environ.get("SN21_PIPELINE_GRACE_HOURS", "6")))
    args = p.parse_args()

    try:
        status, payload = _api_get("daily/pipeline-heartbeat?day=latest")
    except SystemExit:
        raise
    except Exception as exc:   # noqa: BLE001
        print(f"[health] could not reach the heartbeat endpoint: {exc}", flush=True)
        return 2

    heartbeat = payload if (status == 200 and payload.get("success")) else None
    now = datetime.now(timezone.utc)
    v = assess(heartbeat, now, trigger_hour=args.trigger_hour,
               grace_hours=args.grace_hours)

    line = f"[health] {v.level} (last run: {v.day or 'never'}) — {'; '.join(v.reasons)}"
    print(line, flush=True)

    if v.alerting():
        msg = (f":rotating_light: SN21 daily pipeline {v.level} — "
               f"last run {v.day or 'never'}. {'; '.join(v.reasons)} "
               f"(checked {now.strftime('%Y-%m-%d %H:%M UTC')})")
        if args.dry_run:
            print(f"[health] DRY-RUN — would alert: {msg}", flush=True)
        else:
            posted = post_alert(msg)
            where = "posted" if posted else "NOT posted (no SN21_ALERT_WEBHOOK set)"
            print(f"[health] alert {where}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
