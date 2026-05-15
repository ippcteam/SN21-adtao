"""Tests for hope/validator/data_client.py.

Currently scoped to the auto-release-discovery helper. The full
HopeDataClient is exercised by integration tests against the live data
backend rather than unit-tested here, since most of its surface area is
thin HTTP wrappers.
"""

from __future__ import annotations

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from hope.validator.data_client import HopeDataClient


@pytest.fixture
def env_client(monkeypatch):
    """A HopeDataClient with the auth env vars set to dummy values.

    Avoids the constructor's "HOPE_API_KEY must be set" guard so the
    tests can focus on the discovery logic itself.
    """
    monkeypatch.setenv("HOPE_API_KEY", "test-key")
    monkeypatch.setenv("HOPE_API_URL", "https://test.example/api")
    return HopeDataClient()


def test_discover_picks_lexically_latest_created_at(env_client):
    """Newest `created_at` wins regardless of input ordering."""
    releases = [
        {"release_key": "WR-2026-W17-PUB-E1", "created_at": "2026-04-27T00:00:00Z"},
        {"release_key": "WR-2026-W19-PUB-E1", "created_at": "2026-05-11T00:00:00Z"},
        {"release_key": "WR-2026-W18-PUB-E1", "created_at": "2026-05-04T00:00:00Z"},
    ]
    with patch.object(env_client, "list_releases", new=AsyncMock(return_value=releases)):
        key = asyncio.run(env_client.discover_latest_release())
    assert key == "WR-2026-W19-PUB-E1"


def test_discover_handles_missing_created_at(env_client):
    """Records without created_at sort to the bottom; non-missing record wins."""
    releases = [
        {"release_key": "WITHOUT_TS"},
        {"release_key": "WITH_TS", "created_at": "2026-05-11T00:00:00Z"},
    ]
    with patch.object(env_client, "list_releases", new=AsyncMock(return_value=releases)):
        key = asyncio.run(env_client.discover_latest_release())
    assert key == "WITH_TS"


def test_discover_raises_when_no_releases(env_client):
    """Empty release list should be a hard error, not a silent None."""
    with patch.object(env_client, "list_releases", new=AsyncMock(return_value=[])):
        with pytest.raises(RuntimeError, match="no releases"):
            asyncio.run(env_client.discover_latest_release())


def test_discover_raises_when_release_key_missing(env_client):
    """A latest record without release_key is a backend-shape regression."""
    releases = [
        {"created_at": "2026-05-11T00:00:00Z"},  # missing release_key
    ]
    with patch.object(env_client, "list_releases", new=AsyncMock(return_value=releases)):
        with pytest.raises(RuntimeError, match="no `release_key` field"):
            asyncio.run(env_client.discover_latest_release())


def test_constructor_requires_credentials():
    """The constructor should fail loudly without HOPE_API_KEY / HOPE_API_URL."""
    # Clear env to simulate an unconfigured caller.
    for var in ("HOPE_API_KEY", "HOPE_API_URL"):
        if var in os.environ:
            del os.environ[var]
    with pytest.raises(ValueError, match="HOPE_API_KEY"):
        HopeDataClient()
