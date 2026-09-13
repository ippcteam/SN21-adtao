"""The namespace exec only runs on Linux, so these test the pure logic that
decides what will be exec'd and under what limits — the parts a mistake in
would make the sandbox unsafe or unable to start."""

import resource

import pytest

from hope.backtest import ns_sandbox
from hope.backtest.ns_sandbox import RunSpec, _rlimits, resolve_argv, sandbox_env


def test_sandbox_env_does_not_inherit_the_executor_environment(monkeypatch):
    """The load-bearing secret barrier: nothing from the executor's own env —
    where the signing key and DB creds live — reaches the miner process."""
    monkeypatch.setenv("SN21_INGEST_API_KEY", "super-secret")
    monkeypatch.setenv("ED25519_KEY_B64", "also-secret")
    env = sandbox_env(["FOO=bar"])
    assert "SN21_INGEST_API_KEY" not in env
    assert "ED25519_KEY_B64" not in env
    assert env["FOO"] == "bar"


def test_sandbox_env_has_a_sane_default_path():
    env = sandbox_env([])
    assert env["PATH"].startswith("/usr/local/bin")
    assert env["PYTHONHASHSEED"] == "0"     # determinism aid


def test_sandbox_env_keeps_image_declared_vars():
    env = sandbox_env(["MODEL_DIR=/opt/model", "SEED=7"])
    assert env["MODEL_DIR"] == "/opt/model"
    assert env["SEED"] == "7"


def test_resolve_argv_prefers_override():
    assert resolve_argv(["/entry"], ["/x", "--y"]) == ["/x", "--y"]


def test_resolve_argv_uses_image_when_no_override():
    assert resolve_argv(["/entry", "--serve"]) == ["/entry", "--serve"]


def test_resolve_argv_refuses_empty():
    with pytest.raises(ValueError):
        resolve_argv([], None)


def test_rlimits_are_only_the_safe_ones():
    """CPU and FSIZE are safe and meaningful here; NPROC (per-uid-global) and
    AS (virtual, not RSS) are deliberately excluded because they break correct
    behaviour on a shared PaaS host — memory/pids caps are a cgroup job."""
    spec = RunSpec(rootfs="/x", argv=["/y"])
    limits = dict((r, (s, h)) for r, s, h in _rlimits(spec))
    assert limits[resource.RLIMIT_CPU][0] == 15 * 60         # 15 min CPU
    assert resource.RLIMIT_FSIZE in limits
    assert resource.RLIMIT_NPROC not in limits              # per-uid-global
    assert resource.RLIMIT_AS not in limits                 # virtual != RSS


def test_unavailable_when_unshare_binary_absent(monkeypatch):
    """Without the unshare binary the sandbox reports itself unavailable rather
    than pretending to isolate (this is also the macOS case)."""
    monkeypatch.setattr(ns_sandbox.shutil, "which", lambda _name: None)
    result = ns_sandbox.run_sandboxed(RunSpec(rootfs="/x", argv=["/y"]), b"")
    assert result.ok is False
    assert result.error == ns_sandbox.ERR_SANDBOX_UNAVAILABLE


def test_unshare_command_carries_the_isolation_flags():
    """The network wall and the chroot are the load-bearing parts — assert they
    are always in the command, in the proven form."""
    spec = RunSpec(rootfs="/img/rootfs", argv=["python3", "/model.py"],
                   working_dir="/app")
    cmd = ns_sandbox.unshare_command(spec, "/usr/bin/unshare")
    assert cmd[0] == "/usr/bin/unshare"
    assert "--net" in cmd                       # required network isolation
    assert "--user" in cmd and "--map-root-user" in cmd
    assert "--root=/img/rootfs" in cmd          # chroot into the image
    assert "--wd=/app" in cmd
    # the model argv comes after the -- separator, in order
    sep = cmd.index("--")
    assert cmd[sep + 1:] == ["python3", "/model.py"]


def test_unshare_command_defaults_workdir_to_root():
    spec = RunSpec(rootfs="/r", argv=["/x"], working_dir="")
    cmd = ns_sandbox.unshare_command(spec, "unshare")
    assert "--wd=/" in cmd


# ---- resident memory is enforced at the published budget --------------------

def _fake_proc(root, procs):
    """procs: {pid: (session_id, rss_kb, comm)} written in /proc's shape."""
    for pid, (sid, rss_kb, comm) in procs.items():
        d = root / str(pid)
        d.mkdir()
        # pid (comm) state ppid pgrp session tty ...
        (d / "stat").write_text(f"{pid} ({comm}) S 1 {pid} {sid} 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0\n")
        (d / "status").write_text(f"Name:\t{comm}\nVmPeak:\t 999999 kB\nVmRSS:\t {rss_kb} kB\n")
    (root / "self").mkdir()          # non-numeric entries are ignored
    (root / "meminfo").write_text("MemTotal: 1 kB\n")


def test_session_rss_sums_the_whole_session_and_nothing_else(tmp_path):
    _fake_proc(tmp_path, {
        4242: (4242, 300 * 1024, "unshare"),
        4243: (4242, 200 * 1024, "python3 my model.py"),   # spaces in comm
        4244: (4242, 50 * 1024, "worker (thread)"),         # parens in comm
        9999: (9999, 800 * 1024, "other job"),
    })
    assert ns_sandbox.session_rss_bytes(4242, proc_root=str(tmp_path)) == 550 * (1 << 20)
    assert ns_sandbox.session_rss_bytes(9999, proc_root=str(tmp_path)) == 800 * (1 << 20)
    assert ns_sandbox.session_rss_bytes(1, proc_root=str(tmp_path)) == 0


def test_session_rss_skips_processes_that_vanish(tmp_path):
    _fake_proc(tmp_path, {7: (7, 100 * 1024, "a")})
    (tmp_path / "8").mkdir()                        # exists, but no stat/status
    assert ns_sandbox.session_rss_bytes(7, proc_root=str(tmp_path)) == 100 * (1 << 20)


def test_the_budget_defaults_to_the_published_gigabyte():
    spec = RunSpec(rootfs="/x", argv=["/y"])
    assert spec.memory_bytes == 1 << 30


def test_a_memory_kill_reads_like_a_docker_memory_kill():
    """chronic_failure classifies exit 137 as REASON_MEMORY: the two
    executors must report the same breach the same way."""
    from hope.scoring.chronic_failure import (
        OOM_EXIT_CODE, REASON_MEMORY, classify_failure, failure_reason, FAULT_MINER,
    )
    spec = RunSpec(rootfs="/x", argv=["/y"])
    r = ns_sandbox.oom_result(spec, observed_bytes=1300 << 20)
    assert not r.ok and r.exit_code == OOM_EXIT_CODE == ns_sandbox.OOM_EXIT_CODE
    assert r.error.startswith(f"exit={OOM_EXIT_CODE}")
    assert "1024MB" in r.error and "1300MB" in r.error
    assert classify_failure(False, r.error) == FAULT_MINER
    assert failure_reason(False, r.error) == REASON_MEMORY


def test_observe_mode_records_the_peak_without_killing():
    from hope.backtest import local_executor as le
    assert le._memory_enforce({}) is True
    assert le._memory_enforce({"SN21_SANDBOX_RSS_MODE": "observe"}) is False
    assert le._memory_enforce({"SN21_SANDBOX_RSS_MODE": "ENFORCE"}) is True
    spec = RunSpec(rootfs="/x", argv=["/y"], memory_enforce=False)
    assert spec.memory_enforce is False and spec.memory_bytes == 1 << 30
