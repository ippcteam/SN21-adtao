"""The current champion's forecast for what-if episodes (14 Oct 2026).

OBI shows each recommendation with what the SN21 champion expects it to do. It
builds what-if episodes in the served basket shape (the account today plus a
proposed change) and asks for the champion's predictions. This module answers
that, read-only:

  champion     the seat in the promotion state (standing/_promotion.json), the
               day it was taken (the promotion log), and the image it last ran
               in the daily basket (its shadow record: repo@sha256)
  run          the same sandbox, flags and limits as the daily run, with a
               shorter wall clock (the caller waits 90 s) and the champion's
               unpacked image kept warm between calls until the seat changes
  answer       {"model": {uid, hotkey, image_digest, champion_since},
                "predictions": {episode_id: {"7"|"14"|"28": {...}}}}

NEVER SCORED, NEVER STORED. Every episode id starts with "whatif"; nothing here
writes to the shadow ledger, the standings or any prediction store. The
daily run never waits for this: the caller is told "busy" while it runs.
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time

WHATIF_PREFIX = "whatif"
MAX_EPISODES = 20
FORECAST_WALL_S = 75           # the caller gives up at 90 s
SHADOW_LOOKBACK_DAYS = 14


class ForecastError(Exception):
    """A plain-words reason the forecast is not available (answered as 503)."""


class BadRequest(Exception):
    """The request is not a what-if forecast request (answered as 400)."""


# ---------------------------------------------------------------------------
# the request
# ---------------------------------------------------------------------------
def validate(body) -> list[dict]:
    if not isinstance(body, dict) or not isinstance(body.get("episodes"), list):
        raise BadRequest("expected {\"episodes\": [...]}")
    eps = body["episodes"]
    if not 1 <= len(eps) <= MAX_EPISODES:
        raise BadRequest(f"send 1 to {MAX_EPISODES} episodes")
    for e in eps:
        if not isinstance(e, dict) or not isinstance(e.get("payload"), dict):
            raise BadRequest("every episode needs a payload object")
        eid = str(e.get("episode_id") or "")
        if not eid.startswith(WHATIF_PREFIX):
            raise BadRequest("every episode_id must start with 'whatif' (these are never scored)")
    if len({str(e["episode_id"]) for e in eps}) != len(eps):
        raise BadRequest("episode ids must be unique")
    return eps


def to_stdin(episodes: list[dict]) -> list[dict]:
    """The model reads the payload with its episode_id at the top level,
    exactly as the daily run feeds it (run_daily_pipeline.fetch_basket_payloads)."""
    return [dict(e["payload"], episode_id=str(e["episode_id"])) for e in episodes]


# ---------------------------------------------------------------------------
# the champion
# ---------------------------------------------------------------------------
def champion(ledger_root: str) -> dict:
    """{hotkey, champion_since, pinned_ref}. Raises ForecastError when the seat
    is empty or the champion has no image on record."""
    from hope.scoring import standing_ledger
    state = standing_ledger.load_promotion_state(ledger_root)
    hk = state.champion
    if not hk:
        raise ForecastError("the champion seat is empty")
    return {"hotkey": hk, "champion_since": _seated_on(ledger_root, hk),
            "pinned_ref": _pinned_ref(ledger_root, hk)}


def _seated_on(ledger_root: str, hotkey: str) -> str | None:
    """The day of the latest promotion-log event that seated this hotkey."""
    from hope.scoring import standing_ledger
    path = os.path.join(standing_ledger.standing_dir(ledger_root), "_promotion_log.jsonl")
    seated = None
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                if not line.strip():
                    continue
                ev = json.loads(line)
                if hotkey in (ev.get("new"), ev.get("champion")) and ev.get("day"):
                    seated = ev["day"]
    return seated


def _pinned_ref(ledger_root: str, hotkey: str) -> str:
    """repo@sha256 the champion ran in its latest daily basket."""
    shadow = os.path.join(ledger_root, "shadow")
    days = sorted((d for d in os.listdir(shadow) if len(d) == 10), reverse=True) if os.path.isdir(shadow) else []
    for d in days[:SHADOW_LOOKBACK_DAYS]:
        p = os.path.join(shadow, d, f"{hotkey}.jsonl")
        if not os.path.exists(p):
            continue
        last = None
        with open(p) as f:
            for line in f:
                if line.strip():
                    last = json.loads(line)
        ref = (last or {}).get("image_digest") or ""
        if "@sha256:" in ref:
            return ref
    raise ForecastError("the champion has no image on record in the last two weeks")


_UID_CACHE: dict = {"at": 0.0, "map": {}}


def uid_of(hotkey: str, ttl_s: int = 3600) -> int | None:
    """The champion's UID from the metagraph, cached for an hour; None if the
    chain cannot be read (the forecast does not depend on it)."""
    if time.time() - _UID_CACHE["at"] > ttl_s:
        try:
            import bittensor as bt
            mg = bt.Subtensor(network=os.environ.get("BT_NETWORK") or "finney").metagraph(
                int(os.environ.get("SN21_NETUID", "21")))
            _UID_CACHE["map"] = {str(mg.hotkeys[i]): int(mg.uids[i]) for i in range(len(mg.hotkeys))}
            _UID_CACHE["at"] = time.time()
        except Exception:
            return _UID_CACHE["map"].get(hotkey)
    return _UID_CACHE["map"].get(hotkey)


# ---------------------------------------------------------------------------
# running it
# ---------------------------------------------------------------------------
class WarmRunner:
    """Runs a pinned image under the daily run's sandbox, keeping the most
    recent image unpacked so a second forecast skips the pull.

    One run at a time (the lock); a new digest replaces the cached one.
    """

    def __init__(self, workdir_root: str | None = None, wall_s: int = FORECAST_WALL_S):
        self.root = os.path.join(workdir_root or os.environ.get("SN21_EXECUTOR_WORKDIR") or "/tmp/executor",
                                 "forecast-cache")
        self.wall_s = wall_s
        self._lock = threading.Lock()
        self._ref = None
        self._image = None

    def _image_for(self, pinned_ref: str):
        from hope.backtest.local_executor import MIN_FREE_DISK_BYTES, _free_disk_bytes, split_pinned_ref
        from hope.backtest.ns_sandbox import cleanup_rootfs
        from hope.backtest.oci_pull import PullError, pull_and_unpack
        if self._ref == pinned_ref and self._image is not None and os.path.isdir(self._image.rootfs):
            return self._image
        if self._image is not None:
            cleanup_rootfs(self._dest)
            self._image = self._ref = None
        repo, digest = split_pinned_ref(pinned_ref)
        os.makedirs(self.root, exist_ok=True)
        if _free_disk_bytes(self.root) < MIN_FREE_DISK_BYTES:
            raise ForecastError("the validator is short of disk space")
        self._dest = os.path.join(self.root, digest.replace(":", "_"))
        shutil.rmtree(self._dest, ignore_errors=True)
        os.makedirs(self._dest)
        try:
            self._image = pull_and_unpack(repo, digest, self._dest)
        except PullError as exc:
            shutil.rmtree(self._dest, ignore_errors=True)
            raise ForecastError(f"the champion's image could not be pulled ({str(exc)[:80]})") from exc
        self._ref = pinned_ref
        return self._image

    def __call__(self, pinned_ref: str, stdin_episodes: list[dict]):
        from hope.backtest.container_runner import RunResult, _parse_output, required_horizons
        from hope.backtest.local_executor import _as_limit_bytes, _memory_enforce, _resolve_entrypoint
        from hope.backtest.ns_sandbox import RunSpec, run_sandboxed, sandbox_env
        with self._lock:
            image = self._image_for(pinned_ref)
            argv = _resolve_entrypoint(image, None)
            if argv is None:
                return RunResult(ok=False, error="no runnable entrypoint in image", episodes_in=len(stdin_episodes))
            spec = RunSpec(rootfs=image.rootfs, argv=argv, env=sandbox_env(image.config.env),
                           working_dir=image.config.working_dir, as_limit_bytes=_as_limit_bytes(),
                           memory_enforce=_memory_enforce(), wall_timeout=self.wall_s)
            blob = ("\n".join(json.dumps(e, default=str) for e in stdin_episodes) + "\n").encode()
            t0 = time.monotonic()
            res = run_sandboxed(spec, blob)
            took = round(time.monotonic() - t0, 1)
            if not res.ok:
                return RunResult(ok=False, error=res.error, episodes_in=len(stdin_episodes), duration_s=took)
            ids = {str(e["episode_id"]) for e in stdin_episodes}
            preds = _parse_output(res.stdout, ids, required_horizons(stdin_episodes))
            return RunResult(ok=True, predictions=preds, episodes_in=len(stdin_episodes),
                             predictions_out=len(preds), duration_s=took)


def docker_runner(wall_s: int = FORECAST_WALL_S):
    """Local development: the docker path with the forecast's wall clock."""
    from hope.backtest.container_runner import run_basket_docker

    def run(pinned_ref, stdin_episodes):
        return run_basket_docker(pinned_ref, stdin_episodes, timeout_s=wall_s)
    return run


def default_runner():
    from hope.backtest.execution_mode import MODE_SANDBOX, executor_mode
    return WarmRunner() if executor_mode() == MODE_SANDBOX else docker_runner()


# ---------------------------------------------------------------------------
# one request, end to end
# ---------------------------------------------------------------------------
def answer(body, ledger_root: str, runner, busy_reason: str | None = None,
           uid_lookup=uid_of) -> tuple[int, dict]:
    """(status, body) for one forecast request. Read-only."""
    try:
        eps = validate(body)
    except BadRequest as exc:
        return 400, {"error": str(exc)}
    if busy_reason:
        return 503, {"reason": f"busy: {busy_reason}"}
    try:
        champ = champion(ledger_root)
        result = runner(champ["pinned_ref"], to_stdin(eps))
    except ForecastError as exc:
        return 503, {"reason": str(exc)}
    except Exception as exc:
        return 503, {"reason": f"the champion could not be run ({type(exc).__name__})"}
    if not result.ok:
        return 503, {"reason": f"the champion's run failed ({str(result.error or '')[:80]})"}
    return 200, {
        "model": {"uid": uid_lookup(champ["hotkey"]), "hotkey": champ["hotkey"],
                  "image_digest": champ["pinned_ref"].rsplit("@", 1)[-1],
                  "champion_since": champ["champion_since"]},
        "predictions": {str(e["episode_id"]): result.predictions.get(str(e["episode_id"]))
                        for e in eps if str(e["episode_id"]) in result.predictions},
    }
