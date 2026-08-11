"""The execution-mode seam must default to docker (so every existing caller is
unchanged) and produce a working sandbox runner when asked."""

from hope.backtest.execution_mode import (
    MODE_DOCKER,
    MODE_SANDBOX,
    basket_runner,
    executor_mode,
)
from hope.backtest.local_executor import split_pinned_ref


def test_default_mode_is_docker():
    assert executor_mode({}) == MODE_DOCKER


def test_unknown_mode_falls_back_to_docker():
    assert executor_mode({"SN21_EXECUTOR_MODE": "nonsense"}) == MODE_DOCKER


def test_sandbox_mode_is_honoured():
    assert executor_mode({"SN21_EXECUTOR_MODE": "sandbox"}) == MODE_SANDBOX


def test_basket_runner_returns_a_callable_for_each_mode():
    assert callable(basket_runner({"SN21_EXECUTOR_MODE": "docker"}))
    assert callable(basket_runner({"SN21_EXECUTOR_MODE": "sandbox"}))


def test_split_pinned_ref():
    repo, digest = split_pinned_ref("ghcr.io/x/y@sha256:" + "a" * 64)
    assert repo == "ghcr.io/x/y"
    assert digest == "sha256:" + "a" * 64


def test_split_pinned_ref_rejects_unpinned():
    import pytest
    with pytest.raises(ValueError):
        split_pinned_ref("ghcr.io/x/y")


def test_sandbox_runner_splits_the_ref_and_calls_local(monkeypatch):
    """The sandbox runner must hand the puller a (repo, digest) pair, not the
    combined string docker takes."""
    seen = {}

    def fake_local(repo, digest, episodes, workdir_root=None):
        seen["repo"] = repo
        seen["digest"] = digest
        return "RESULT"

    import hope.backtest.local_executor as le
    monkeypatch.setattr(le, "run_basket_local", fake_local)

    run = basket_runner({"SN21_EXECUTOR_MODE": "sandbox"})
    out = run("ghcr.io/x/y@sha256:" + "b" * 64, [{"episode_id": "1"}])
    assert out == "RESULT"
    assert seen["repo"] == "ghcr.io/x/y"
    assert seen["digest"] == "sha256:" + "b" * 64
