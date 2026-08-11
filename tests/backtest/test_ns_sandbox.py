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


def test_rlimits_carry_the_published_budget():
    spec = RunSpec(rootfs="/x", argv=["/y"])
    limits = dict((r, (s, h)) for r, s, h in _rlimits(spec))
    assert limits[resource.RLIMIT_AS][0] == 1 << 30          # 1 GB memory
    assert limits[resource.RLIMIT_CPU][0] == 15 * 60         # 15 min CPU
    assert limits[resource.RLIMIT_NPROC][0] == 256           # pids


def test_unavailable_when_unshare_absent(monkeypatch):
    """On a host without os.unshare (e.g. macOS), the sandbox reports itself
    unavailable rather than pretending to isolate."""
    monkeypatch.delattr(ns_sandbox.os, "unshare", raising=False)
    result = ns_sandbox.run_sandboxed(RunSpec(rootfs="/x", argv=["/y"]), b"")
    assert result.ok is False
    assert result.error == ns_sandbox.ERR_SANDBOX_UNAVAILABLE


def test_reap_targets_the_process_group_not_just_the_child():
    """The timeout must kill the whole group: the model runs as PID 1 in a new
    PID namespace and survives its parent's death, so killing only the child
    leaves it holding the stdout pipe and the read loop hangs. This asserts the
    source uses killpg, because the behaviour only manifests on Linux under a
    real hang and must not silently regress."""
    import inspect
    src = inspect.getsource(ns_sandbox.run_sandboxed)
    assert "os.setsid()" in src
    assert "killpg" in src
