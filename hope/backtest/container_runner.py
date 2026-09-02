"""M2 — model execution runner (the sandbox's process layer).

Executes a miner model against a basket of episode payloads under the M0
contract: JSON line per episode on stdin -> JSON prediction line per episode
on stdout, no network, hard memory/time budgets (spec: 1GB / 15 min per
daily basket).

Two modes:
- docker: `docker run --rm -i --network=none --memory=1g --cpus=1
  --pids-limit=256 --read-only <image>` (production shape; digest-pinned)
- local_callable: a Python callable episode->prediction for harness tests
  and the reference model — identical framing, no container.

Isolation notes (v1, published in the spec): network=none closes the
exfiltration channel; read-only rootfs; budgets abort the day's run — the
episode-weighted standing makes a missed day self-penalising, no extra
punishment. Deeper hardening (seccomp, gVisor) is the published roadmap,
not pretended here.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

DEFAULT_MEMORY = "1g"
DEFAULT_CPUS = "1"
DEFAULT_TIMEOUT_S = 15 * 60  # 15 min per basket (M0 budget)

# Error-string markers. These are the FAULT TAXONOMY the liveness policy
# reads back off the shadow ledger (hope.scoring.chronic_failure), so they
# are constants rather than inline literals: renaming one must break an
# import, never silently reclassify a subnet fault as a miner fault.
ERR_TIMEOUT_PREFIX = "timeout>"            # miner fault (time budget)
ERR_EXIT_PREFIX = "exit="                  # miner fault (crash / OOM kill)
ERR_DOCKER_UNAVAILABLE = "docker_not_available"   # SUBNET fault (our host)


@dataclass
class RunResult:
    ok: bool
    predictions: dict = field(default_factory=dict)   # (episode_id -> horizons dict)
    error: str | None = None
    episodes_in: int = 0
    predictions_out: int = 0
    # Wall-clock seconds the model actually took, None when the run never
    # started (pull failure, low disk). Recorded so the wall ceiling is a
    # number chosen from measured spread, not judgement — the 2026-09-01
    # ceiling was set blind and cut off three real models.
    duration_s: float | None = None


def docker_command(image_digest: str, memory: str = DEFAULT_MEMORY,
                   cpus: str = DEFAULT_CPUS, name: str | None = None) -> list[str]:
    """The exact isolation flags are part of the published contract."""
    return [
        "docker", "run", "--rm", "-i",
        *([f"--name={name}"] if name else []),
        "--network=none",
        f"--memory={memory}", f"--memory-swap={memory}",
        f"--cpus={cpus}",
        "--pids-limit=256",
        "--read-only",
        "--security-opt=no-new-privileges",
        image_digest,
    ]


def _parse_output(stdout: str, expected_ids: set[str]) -> dict:
    preds: dict = {}
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue  # non-JSON chatter is ignored, never fatal
        eid = obj.get("episode_id")
        if eid in expected_ids and isinstance(obj.get("horizons"), dict):
            preds[eid] = obj["horizons"]
    return preds


def run_basket_docker(image_digest: str, episodes: Iterable[dict],
                      timeout_s: int = DEFAULT_TIMEOUT_S,
                      memory: str = DEFAULT_MEMORY) -> RunResult:
    eps = list(episodes)
    ids = {str(e["episode_id"]) for e in eps}
    stdin_blob = "\n".join(json.dumps(e, default=str) for e in eps) + "\n"
    # Named container so a timeout can kill the DAEMON-side process too:
    # subprocess timeout only kills the docker CLIENT — without an explicit
    # kill the container survives as a zombie model run (found in the
    # 2026-07-26 negative test round).
    cname = f"sn21-run-{uuid.uuid4().hex[:12]}"
    try:
        proc = subprocess.run(
            docker_command(image_digest, memory=memory, name=cname),
            input=stdin_blob.encode(), capture_output=True, timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired:
        subprocess.run(["docker", "kill", cname], capture_output=True, timeout=30, check=False)
        return RunResult(
            ok=False,
            error=f"{ERR_TIMEOUT_PREFIX}{timeout_s}s (container killed)",
            episodes_in=len(eps))
    except FileNotFoundError:
        return RunResult(ok=False, error=ERR_DOCKER_UNAVAILABLE, episodes_in=len(eps))
    if proc.returncode != 0:
        return RunResult(
            ok=False,
            error=f"{ERR_EXIT_PREFIX}{proc.returncode}: {proc.stderr[:200]!r}",
            episodes_in=len(eps))
    preds = _parse_output(proc.stdout.decode(errors="replace"), ids)
    return RunResult(ok=True, predictions=preds,
                     episodes_in=len(eps), predictions_out=len(preds))


def run_basket_callable(model: Callable[[dict], dict | None],
                        episodes: Iterable[dict]) -> RunResult:
    """Harness mode: same framing, python callable instead of a container.
    The callable returns the horizons dict (or None to abstain)."""
    eps = list(episodes)
    preds: dict = {}
    for e in eps:
        try:
            h = model(e)
        except Exception:
            h = None  # a crashing model simply doesn't predict that episode
        if isinstance(h, dict):
            preds[str(e["episode_id"])] = h
    return RunResult(ok=True, predictions=preds,
                     episodes_in=len(eps), predictions_out=len(preds))
