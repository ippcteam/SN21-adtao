"""The executor's side of the champion forecast relay (14 Oct 2026).

The executor is a background worker: nothing can call it. OBI's POST
/forecast/champion therefore lands on the operator API, which queues the
request; this loop, a thread in the executor daemon, collects requests over
the same internal API the daily run already uses, answers each with the
champion (hope.forecast.champion_forecast) and posts the answer back.

While the daily basket run is due or running, every request is answered
"busy" at once and the API is told, so new requests get "busy" without
waiting. A request older than STALE_S is answered as stale (its caller has
gone). Read-only: nothing here touches the ledger's predictions or standings.

Switch: SN21_FORECAST_RELAY=1 on the executor.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
import urllib.request

SWITCH_ENV = "SN21_FORECAST_RELAY"
POLL_WAIT_S = 20
STALE_S = 80


def enabled(environ=os.environ) -> bool:
    return (environ.get(SWITCH_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def _base():
    url = (os.environ.get("HOPE_API_URL") or "").strip().rstrip("/")
    key = (os.environ.get("HOPE_API_KEY") or "").strip()
    return url, key


def next_request(wait_s: int, busy: str | None) -> dict | None:
    url, key = _base()
    q = {"wait": str(wait_s)}
    if busy:
        q["busy"] = busy
    req = urllib.request.Request(f"{url}/internal/bittensor/v1/forecast/next?" + urllib.parse.urlencode(q),
                                 headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=wait_s + 30) as resp:
        if resp.status == 204:
            return None
        return json.loads(resp.read().decode()).get("request")


def post_result(request_id: str, result: dict) -> None:
    url, key = _base()
    req = urllib.request.Request(f"{url}/internal/bittensor/v1/forecast/result/{urllib.parse.quote(request_id)}",
                                 data=json.dumps(result).encode(), method="POST",
                                 headers={"X-API-Key": key, "Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=30).read()


def serve_one(req: dict, handle, busy: str | None, now=time.time) -> dict:
    """The answer for one queued request: {status, body}."""
    if busy:
        return {"status": 503, "body": {"reason": f"busy: {busy}"}}
    if now() - float(req.get("created") or 0) > STALE_S:
        return {"status": 503, "body": {"reason": "the request waited too long"}}
    status, body = handle(req.get("body") or {})
    return {"status": status, "body": body}


def loop(stop: threading.Event, busy_reason, handle, fetch=next_request, send=post_result,
         log=print, sleep=time.sleep) -> None:
    """Collect and answer requests until `stop` is set. Never raises."""
    while not stop.is_set():
        busy = busy_reason()
        try:
            req = fetch(0 if busy else POLL_WAIT_S, busy)
        except Exception as exc:  # the API may be restarting
            log(f"[forecast] could not reach the operator API ({str(exc)[:120]}); retrying")
            sleep(10)
            continue
        if req is None:
            if busy:
                sleep(5)
            continue
        try:
            send(str(req["id"]), serve_one(req, handle, busy))
        except Exception as exc:
            log(f"[forecast] answer for {req.get('id')} not delivered ({str(exc)[:120]})")


def start(ledger_root: str, busy_reason, log=print) -> threading.Thread | None:
    """Start the relay thread when switched on; None otherwise."""
    if not enabled():
        return None
    from hope.forecast import champion_forecast as F
    runner = F.default_runner()

    def handle(body):
        t0 = time.time()
        status, out = F.answer(body, ledger_root, runner)
        log(f"[forecast] {status} for {len((body or {}).get('episodes') or [])} episode(s) "
            f"in {time.time() - t0:.1f}s" + (f" ({out.get('reason')})" if status != 200 else ""))
        return status, out

    stop = threading.Event()
    t = threading.Thread(target=loop, args=(stop, busy_reason, handle), kwargs={"log": log},
                         name="forecast-relay", daemon=True)
    t.start()
    log("[forecast] relay started")
    return t
