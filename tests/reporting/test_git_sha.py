"""Tests for hope.reporting.git_sha.current_commit_sha."""

from __future__ import annotations

import pytest

from hope.reporting.git_sha import current_commit_sha


def test_returns_full_sha():
    """Against the local repo, returns a 40-char hex string."""
    sha = current_commit_sha()
    assert isinstance(sha, str)
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


def test_falls_back_to_env_when_git_unavailable(tmp_path, monkeypatch):
    """When git is unavailable in repo_root, fallback_env supplies the SHA."""
    monkeypatch.setenv("TEST_BUILD_SHA", "a" * 40)
    sha = current_commit_sha(repo_root=tmp_path, fallback_env="TEST_BUILD_SHA")
    assert sha == "a" * 40


def test_raises_without_fallback(tmp_path):
    """Empty dir with no fallback env raises a clear RuntimeError."""
    with pytest.raises(RuntimeError, match="git rev-parse"):
        current_commit_sha(repo_root=tmp_path)


def test_raises_when_fallback_env_unset(tmp_path, monkeypatch):
    """If fallback env is named but not set, raise rather than return ''."""
    monkeypatch.delenv("ABSENT_BUILD_SHA", raising=False)
    with pytest.raises(RuntimeError, match="unset or empty"):
        current_commit_sha(repo_root=tmp_path, fallback_env="ABSENT_BUILD_SHA")
