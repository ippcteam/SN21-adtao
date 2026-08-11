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
