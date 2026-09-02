"""Daemonless basket execution — the docker runner's stand-in on Render.

This composes the puller (oci_pull) and the namespace sandbox (ns_sandbox)
into the SAME contract `container_runner.run_basket_docker` exposes:

    run_basket_local(image_ref, digest, episodes) -> RunResult

so every caller — the admission gate, the shadow day, scoring — is unchanged.
They ask for a basket to be run against an image; whether a docker daemon or
this namespace sandbox runs it is an execution-mode detail, and the RunResult
they get back is identical in shape and meaning.

One difference from the docker path, made explicit: docker takes a single
`repo@digest` string, but pulling without a daemon needs the repo and the
digest separately (the digest addresses the blob; the repo names where to
fetch it). So this entrypoint takes both, and the shadow ShadowModel already
carries the pinned `repo@digest`, which splits cleanly.
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from collections.abc import Iterable

from hope.backtest.container_runner import RunResult, _parse_output
from hope.backtest.ns_sandbox import RunSpec, cleanup_rootfs, run_sandboxed, sandbox_env
from hope.backtest.oci_pull import PullError, pull_and_unpack

# Opt-in virtual-memory cap for the sandboxed model, in MB. On a host whose own
# memory is smaller than a model might want, set this BELOW the container limit
# so an over-hungry model fails cleanly instead of OOM-killing the executor.
MEM_MB_ENV = "SN21_SANDBOX_MEM_MB"

# Per-model wall-clock ceiling in seconds (0/unset = the published 15-min
# budget). A dry run sets this short so a hung model is reaped in seconds
# rather than holding the whole sweep for the full budget.
WALL_S_ENV = "SN21_SANDBOX_WALL_S"


def _wall_timeout(environ=os.environ) -> int:
    raw = (environ.get(WALL_S_ENV) or "").strip()
    if not raw:
        return 0   # sentinel -> use RunSpec's default budget
    try:
        return max(1, int(float(raw)))
    except ValueError:
        return 0

# Refuse to unpack an image when free disk is below this — a multi-GB ML image
# on a small ephemeral volume evicted the whole service (observed 2026-08-11).
MIN_FREE_DISK_BYTES = 2 << 30    # 2 GB headroom


def _as_limit_bytes(environ=os.environ) -> int:
    raw = (environ.get(MEM_MB_ENV) or "").strip()
    if not raw:
        return 0
    try:
        return max(0, int(float(raw))) * (1 << 20)
    except ValueError:
        return 0


def _free_disk_bytes(path: str) -> int:
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 1 << 62   # unknown -> do not block on it


def split_pinned_ref(pinned: str) -> tuple[str, str]:
    """`repo@sha256:...` -> (repo, digest). The shadow model stores this
    pinned form; the puller needs the two halves."""
    if "@" not in pinned:
        raise ValueError(f"not a pinned ref (need repo@digest): {pinned!r}")
    repo, _, digest = pinned.partition("@")
    return repo, digest


def run_basket_local(
    image_ref: str,
    digest: str,
    episodes: Iterable[dict],
    workdir_root: str | None = None,
    entrypoint_override: list | None = None,
) -> RunResult:
    """Pull `image_ref@digest`, run it against the basket under the sandbox.

    Mirrors run_basket_docker: JSON episode per line on stdin, JSON prediction
    per line on stdout, and a RunResult carrying ok / predictions / error /
    counts. The unpacked image is always removed, success or failure — disk is
    the scarce resource on this host.
    """
    eps = list(episodes)
    ids = {str(e["episode_id"]) for e in eps}
    stdin_blob = ("\n".join(json.dumps(e, default=str) for e in eps) + "\n").encode()

    dest = tempfile.mkdtemp(prefix="sn21-img-", dir=workdir_root)
    try:
        free = _free_disk_bytes(dest)
        if free < MIN_FREE_DISK_BYTES:
            # Better to skip than to evict the whole service unpacking a huge
            # image onto a nearly-full volume.
            return RunResult(
                ok=False, episodes_in=len(eps),
                error=f"disk_low: {free >> 20} MB free < "
                      f"{MIN_FREE_DISK_BYTES >> 20} MB required")
        try:
            image = pull_and_unpack(image_ref, digest, dest)
        except PullError as exc:
            # A pull/verify failure is NOT a run failure: the fault taxonomy
            # keeps them distinct so a registry problem never strikes a miner
            # the way a crashing container would.
            return RunResult(ok=False, error=f"pull_failed: {exc}",
                             episodes_in=len(eps))

        argv = _resolve_entrypoint(image, entrypoint_override)
        if argv is None:
            return RunResult(ok=False, error="no runnable entrypoint in image",
                             episodes_in=len(eps))

        wall = _wall_timeout()
        spec = RunSpec(
            rootfs=image.rootfs,
            argv=argv,
            env=sandbox_env(image.config.env),
            working_dir=image.config.working_dir,
            as_limit_bytes=_as_limit_bytes(),
            **({"wall_timeout": wall} if wall else {}),
        )
        _t0 = time.monotonic()
        result = run_sandboxed(spec, stdin_blob)
        _took = round(time.monotonic() - _t0, 1)
        if not result.ok:
            return RunResult(ok=False, error=result.error, episodes_in=len(eps),
                             duration_s=_took)

        preds = _parse_output(result.stdout, ids)
        return RunResult(ok=True, predictions=preds,
                         episodes_in=len(eps), predictions_out=len(preds),
                         duration_s=_took)
    finally:
        cleanup_rootfs(dest)


def _resolve_entrypoint(image, override):
    """The argv to exec, with the image's declared command as the default.

    Returns None when neither the image nor an override names anything to run —
    an image with no entrypoint is not runnable, which is a rejection, not a
    crash.
    """
    if override:
        return list(override)
    argv = image.config.argv()
    if not argv:
        return None
    # An entrypoint given as a bare name resolves against the image PATH; the
    # sandbox execve needs an absolute path, so only rewrite the obvious cases.
    if argv[0].startswith("/"):
        return argv
    for prefix in ("/usr/local/bin/", "/usr/bin/", "/bin/"):
        candidate = os.path.join(image.rootfs, prefix.lstrip("/"), argv[0])
        if os.path.exists(candidate):
            return [prefix + argv[0]] + argv[1:]
    # Fall back to the shell form the OCI config implies; if it is wrong the
    # sandbox reports exec failure, which is the honest outcome.
    return argv
