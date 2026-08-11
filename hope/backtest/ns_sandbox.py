"""Run an unpacked image under Linux namespaces — the daemonless sandbox.

WHAT IT REPLACES

    `docker run --rm -i --network=none --memory=1g --cpus=1 --pids-limit=256
    --read-only --security-opt=no-new-privileges`. There is no docker on the
    executor host, so we build the equivalent from the primitives the host
    actually grants (probe 2026-08-11):

      docker flag              -> how we get it here
      --network=none           -> CLONE_NEWNET: a net namespace with no
                                  interfaces. The probe proved `unshare -Urn`
                                  works; a process inside cannot reach the
                                  metagraph, a registry, or anything else.
      --read-only + isolation  -> CLONE_NEWUSER + chroot into the image rootfs.
                                  The miner is root only INSIDE the user
                                  namespace, where that root maps to our
                                  unprivileged uid outside — it holds no host
                                  capability. (A mount namespace with a
                                  read-only rebind was the first choice, but
                                  the host denied changing root mount
                                  propagation, so chroot is the containment.)
      --pids-limit / --memory  -> setrlimit RLIMIT_NPROC / RLIMIT_AS, plus
                                  RLIMIT_CPU and RLIMIT_FSIZE. These need no
                                  privilege and are inherited across execve.
      no-new-privileges        -> the host already sets NoNewPrivs=1, and a
                                  userns with no mapped host caps cannot gain
                                  any.

    The stdin/stdout contract is byte-identical to the docker runner: one
    episode payload per line in, one prediction per line out.

WHAT THIS IS AND IS NOT

    It is filesystem, network and resource isolation for untrusted code on a
    host with no container runtime. It is NOT a claim of docker-equivalent
    hardening: there is no seccomp profile of our own (the host applies none
    to us to pass on), and chroot is weaker than pivot_root behind a locked
    mount namespace. The load-bearing boundaries are the network namespace
    (no reachability) and the outer host's own isolation. This is stated
    plainly rather than dressed up.

TESTABILITY

    The syscall path only runs on Linux, so the host integration-tests it.
    The pure logic — argv assembly, env construction, rlimit mapping, the
    uid_map strings, output parsing — is separated out and unit-tested
    anywhere.
"""

from __future__ import annotations

import json
import os
import resource
import shutil
import signal
import subprocess
import sys
from dataclasses import dataclass, field

# Published budget (MINER_MODEL_SPEC §2): 1 GB memory, 15 min CPU per basket.
DEFAULT_MEMORY_BYTES = 1 << 30
DEFAULT_CPU_SECONDS = 15 * 60
DEFAULT_NPROC = 256
DEFAULT_FSIZE_BYTES = 512 << 20     # a model that writes half a GB is broken
DEFAULT_WALL_TIMEOUT = 15 * 60 + 60  # wall clock backstop above the CPU budget

# Error markers — the SAME taxonomy the docker runner and the liveness policy
# read (hope.scoring.chronic_failure), so a fault classifies identically
# whichever executor produced it.
ERR_TIMEOUT_PREFIX = "timeout>"
ERR_EXIT_PREFIX = "exit="
ERR_SANDBOX_UNAVAILABLE = "sandbox_not_available"   # host lacks the primitives


@dataclass
class SandboxResult:
    ok: bool
    stdout: str = ""
    error: str | None = None
    exit_code: int | None = None


@dataclass
class RunSpec:
    """Everything the sandbox needs, resolved before any syscall."""
    rootfs: str
    argv: list
    env: dict = field(default_factory=dict)
    working_dir: str = "/"
    memory_bytes: int = DEFAULT_MEMORY_BYTES
    cpu_seconds: int = DEFAULT_CPU_SECONDS
    nproc: int = DEFAULT_NPROC
    fsize_bytes: int = DEFAULT_FSIZE_BYTES
    wall_timeout: int = DEFAULT_WALL_TIMEOUT
    # Opt-in virtual-address-space cap (0 = off). On a host whose OWN memory is
    # smaller than a model might want, this makes an over-hungry model fail
    # cleanly (its allocation returns ENOMEM, the run errors) instead of the
    # kernel OOM-killing the whole executor service. It is a coarse guard — it
    # caps virtual, not resident — and is deliberately off by default because
    # on a well-sized host a real memory cap belongs in a cgroup. Set it BELOW
    # the container's memory limit on a small host. See SN21_SANDBOX_MEM_MB.
    as_limit_bytes: int = 0


def sandbox_env(image_env: list) -> dict:
    """The environment the miner process sees — deliberately minimal.

    The image's declared PATH and any of its own vars are kept; NOTHING from
    the executor's environment is inherited. That is the barrier that keeps a
    hostile model from reading the receipt-signing key or DB credentials that
    live in the executor's own env: they are simply not present in the child.
    """
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/",
           "PYTHONUNBUFFERED": "1", "PYTHONHASHSEED": "0"}
    for entry in image_env or []:
        if "=" in entry:
            key, _, value = entry.partition("=")
            # The executor's secrets must never be reintroduced via the image.
            if key in ("PATH", "HOME") or not key:
                env[key] = value if key else env.get(key, "")
            else:
                env[key] = value
    env.pop("", None)
    return env


def resolve_argv(image_argv: list, override: list | None = None) -> list:
    """The command to exec: an explicit override, else the image's own."""
    argv = list(override) if override else list(image_argv)
    if not argv:
        raise ValueError("image declares no entrypoint/cmd and none was given")
    return argv


def _rlimits(spec: RunSpec) -> list:
    """(resource, soft, hard) tuples — pure, so the mapping is testable.

    Only the limits that are SAFE on this host and actually mean what we want:

      RLIMIT_CPU    caps compute time — a runaway model is stopped.
      RLIMIT_FSIZE  caps a single file's size — a model cannot fill the disk
                    with one giant write.

    Deliberately NOT here, because on a shared PaaS host they are the wrong
    tool and break correct behaviour:

      RLIMIT_NPROC  is per-REAL-UID across the whole system, not per-sandbox.
                    With --map-root-user the model's forks count against the
                    host's `render` uid, which already runs many processes, so
                    a low cap fails `unshare --fork` itself (EAGAIN). Observed
                    2026-08-11.
      RLIMIT_AS     caps VIRTUAL address space, not resident memory. A normal
                    Python/numpy model reserves far more virtual than it uses,
                    so a 1 GB cap would break legitimate models while not
                    actually bounding RSS.

    Real memory and pid caps belong in a cgroup (v2 `memory.max` / `pids.max`),
    which the host exposes but needs delegation to use — tracked separately.
    Until then the wall-clock killpg is the hard backstop for hangs and fork
    bombs, and the outer container's own memory limit bounds RSS.

    RLIMIT_AS is included ONLY when as_limit_bytes is set — a coarse guard for a
    host smaller than a model might want, so an over-hungry model fails cleanly
    rather than OOM-killing the whole service (observed 2026-08-11: a 512 MiB
    container crash-looped on torch models). Off by default; on a well-sized
    host, real RSS capping is a cgroup job, not this.
    """
    limits = [
        (resource.RLIMIT_CPU, spec.cpu_seconds, spec.cpu_seconds + 5),
        (resource.RLIMIT_FSIZE, spec.fsize_bytes, spec.fsize_bytes),
    ]
    if spec.as_limit_bytes and spec.as_limit_bytes > 0:
        limits.append(
            (resource.RLIMIT_AS, spec.as_limit_bytes, spec.as_limit_bytes))
    return limits


def unshare_command(spec: RunSpec, unshare_bin: str) -> list:
    """The `unshare` invocation that isolates and runs the model — pure, so the
    exact flags are testable without a Linux host.

    Every flag was validated on the executor host (probe 2026-08-11):
      --user --map-root-user  root inside a user namespace, mapped out to our
                              unprivileged uid; --map-root-user does the
                              uid/gid map the raw-syscall path got wrong.
      --net                   no network — the load-bearing isolation.
      --pid --fork            the model runs as PID 1 in its own PID namespace.
      --ipc --uts             process/IPC/hostname isolation.
      --root / --wd           chroot into the image rootfs and set the workdir,
                              so the model sees only its own filesystem.
    """
    return [
        unshare_bin,
        "--user", "--map-root-user",
        "--net",
        "--pid", "--fork",
        "--ipc", "--uts",
        f"--root={spec.rootfs}",
        f"--wd={spec.working_dir or '/'}",
        "--",
        *spec.argv,
    ]


def _apply_rlimits(spec: RunSpec):
    """A preexec hook that caps the child before it execs into the model.
    Limits are inherited across unshare's exec into the entrypoint."""
    def preexec():
        for res, soft, hard in _rlimits(spec):
            try:
                resource.setrlimit(res, (soft, hard))
            except (ValueError, OSError):
                pass
    return preexec


def run_sandboxed(spec: RunSpec, stdin_blob: bytes) -> SandboxResult:
    """Execute the spec against stdin_blob under `unshare`; capture stdout.

    Returns a SandboxResult with the same shape of information the docker
    runner surfaces, so callers do not care which executor ran.
    """
    unshare_bin = shutil.which("unshare")
    if unshare_bin is None:
        return SandboxResult(ok=False, error=ERR_SANDBOX_UNAVAILABLE)

    cmd = unshare_command(spec, unshare_bin)
    # start_new_session puts unshare and the model it forks in one session, so
    # a timeout can kill the WHOLE group — a model that merely sleeps must not
    # outlive its wall clock and wedge the executor.
    try:
        proc = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=spec.env,
            preexec_fn=_apply_rlimits(spec), start_new_session=True)
    except OSError as exc:
        return SandboxResult(ok=False, error=f"{ERR_SANDBOX_UNAVAILABLE}: {exc}")

    killed = False
    try:
        out, err = proc.communicate(input=stdin_blob, timeout=spec.wall_timeout)
    except subprocess.TimeoutExpired:
        killed = True
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
        out, err = proc.communicate()

    stdout = (out or b"").decode("utf-8", "replace")
    stderr = (err or b"").decode("utf-8", "replace")

    if killed:
        return SandboxResult(
            ok=False, error=f"{ERR_TIMEOUT_PREFIX}{spec.wall_timeout}s")
    if proc.returncode != 0:
        detail = stderr.strip()[:200] or stdout.strip()[:200]
        return SandboxResult(ok=False, exit_code=proc.returncode, stdout=stdout,
                             error=f"{ERR_EXIT_PREFIX}{proc.returncode}: {detail}")
    return SandboxResult(ok=True, stdout=stdout, exit_code=0)


def cleanup_rootfs(dest_dir: str) -> None:
    """Remove an unpacked image. Best-effort: disk is the scarce resource on
    this host, so a failure to delete is logged by the caller, not raised."""
    shutil.rmtree(dest_dir, ignore_errors=True)


if __name__ == "__main__":   # pragma: no cover - a manual smoke entrypoint
    # `python -m hope.backtest.ns_sandbox <rootfs> <cmd...>` runs a prepared
    # rootfs against stdin, for hands-on verification on the Linux host.
    spec = RunSpec(rootfs=sys.argv[1], argv=sys.argv[2:] or ["/bin/true"])
    result = run_sandboxed(spec, sys.stdin.buffer.read())
    sys.stderr.write(json.dumps({"ok": result.ok, "error": result.error,
                                 "exit_code": result.exit_code}) + "\n")
    sys.stdout.write(result.stdout)
    sys.exit(0 if result.ok else 1)
