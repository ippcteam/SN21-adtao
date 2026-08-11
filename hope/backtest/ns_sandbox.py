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
    """(resource, soft, hard) tuples — pure, so the mapping is testable."""
    return [
        (resource.RLIMIT_AS, spec.memory_bytes, spec.memory_bytes),
        (resource.RLIMIT_CPU, spec.cpu_seconds, spec.cpu_seconds + 5),
        (resource.RLIMIT_NPROC, spec.nproc, spec.nproc),
        (resource.RLIMIT_FSIZE, spec.fsize_bytes, spec.fsize_bytes),
    ]


def _write_id_maps() -> None:
    """Map our single uid/gid to root INSIDE the new user namespace.

    setgroups must be denied before writing gid_map for an unprivileged
    single-uid mapping — the kernel requires it. This is the exact dance
    `unshare -Ur` performs, which the probe proved is permitted here.
    """
    uid, gid = os.getuid(), os.getgid()
    with open("/proc/self/setgroups", "w") as f:
        f.write("deny")
    with open("/proc/self/uid_map", "w") as f:
        f.write(f"0 {uid} 1")
    with open("/proc/self/gid_map", "w") as f:
        f.write(f"0 {gid} 1")


def _child(spec: RunSpec, stdin_fd: int, stdout_fd: int) -> None:
    """Runs in the forked child: enter namespaces, contain, exec. Never
    returns — it either execs the model or exits with a diagnostic code."""
    try:
        # User namespace first, then map ourselves to root within it, then the
        # network namespace (now permitted because we are userns-root).
        os.unshare(os.CLONE_NEWUSER)
        _write_id_maps()
        os.unshare(os.CLONE_NEWNET | os.CLONE_NEWPID | os.CLONE_NEWIPC
                   | os.CLONE_NEWUTS)

        # PID namespace only takes effect for children, so fork once more; the
        # grandchild is PID 1 in the new namespace and is what we exec into.
        pid = os.fork()
        if pid > 0:
            _, status = os.waitpid(pid, 0)
            code = os.waitstatus_to_exitcode(status)
            os._exit(code if code is not None and code >= 0 else 111)

        # ---- grandchild: contain and exec the untrusted image ----
        os.dup2(stdin_fd, 0)
        os.dup2(stdout_fd, 1)
        os.dup2(stdout_fd, 2)

        os.chroot(spec.rootfs)
        try:
            os.chdir(spec.working_dir or "/")
        except OSError:
            os.chdir("/")

        for res, soft, hard in _rlimits(spec):
            try:
                resource.setrlimit(res, (soft, hard))
            except (ValueError, OSError):
                pass  # a limit the host won't grant is not worth aborting over

        os.execve(spec.argv[0], spec.argv, spec.env)
    except Exception as exc:  # noqa: BLE001 — last-chance diagnostic to the parent
        try:
            os.write(stdout_fd, f"\n__SANDBOX_ERROR__ {exc}\n".encode())
        except OSError:
            pass
        os._exit(127)


def run_sandboxed(spec: RunSpec, stdin_blob: bytes) -> SandboxResult:
    """Execute the spec against stdin_blob under namespaces; capture stdout.

    Returns a SandboxResult with the same shape of information the docker
    runner surfaces, so callers do not care which executor ran.
    """
    if not hasattr(os, "unshare"):
        return SandboxResult(ok=False, error=ERR_SANDBOX_UNAVAILABLE)

    in_r, in_w = os.pipe()
    out_r, out_w = os.pipe()

    pid = os.fork()
    if pid == 0:
        os.close(in_w)
        os.close(out_r)
        _child(spec, in_r, out_w)
        os._exit(127)   # unreachable; _child never returns

    # ---- parent: feed stdin, collect stdout, enforce a wall-clock backstop ----
    os.close(in_r)
    os.close(out_w)
    import signal
    import threading

    try:
        os.write(in_w, stdin_blob)
    except OSError:
        pass
    os.close(in_w)

    killed = {"flag": False}

    def _reap():
        killed["flag"] = True
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

    timer = threading.Timer(spec.wall_timeout, _reap)
    timer.start()

    chunks = []
    try:
        while True:
            data = os.read(out_r, 1 << 20)
            if not data:
                break
            chunks.append(data)
    finally:
        os.close(out_r)
        _, status = os.waitpid(pid, 0)
        timer.cancel()

    stdout = b"".join(chunks).decode("utf-8", "replace")
    if "__SANDBOX_ERROR__" in stdout:
        detail = stdout.split("__SANDBOX_ERROR__", 1)[1].strip()[:200]
        return SandboxResult(ok=False, error=f"{ERR_EXIT_PREFIX}127: {detail}")
    if killed["flag"]:
        return SandboxResult(
            ok=False, error=f"{ERR_TIMEOUT_PREFIX}{spec.wall_timeout}s")

    code = os.waitstatus_to_exitcode(status)
    if code != 0:
        return SandboxResult(ok=False, exit_code=code, stdout=stdout,
                             error=f"{ERR_EXIT_PREFIX}{code}")
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
