"""Tests for HopeDataClient.discover_scoreable_release (P2).

The scorer must resolve to the latest CLOSED epoch (the release *before* the
open/newest one), not the newest — which is the open submission epoch with no
bundles yet. Network is stubbed by overriding list_releases.
"""
import asyncio

import pytest

from hope.validator.data_client import HopeDataClient


class _FakeClient(HopeDataClient):
    def __init__(self, releases):
        super().__init__(api_key="k", base_url="http://backend.test")
        self._fake = releases

    async def list_releases(self):
        return list(self._fake)


def _run(client):
    return asyncio.run(client.discover_scoreable_release())


def test_picks_release_before_the_newest():
    # newest (open) = RERUN; scoreable (closed) = W22
    c = _FakeClient([
        {"release_key": "WR-2026-W22-PUB-E1", "created_at": "2026-05-25T02:00:00"},
        {"release_key": "WR-2026-W21-RERUN-E1", "created_at": "2026-06-02T16:34:23"},
        {"release_key": "WR-2026-W21-PUB-E1", "created_at": "2026-05-18T02:00:00"},
    ])
    assert _run(c) == "WR-2026-W22-PUB-E1"


def test_after_a_newer_weekly_build_picks_the_rerun():
    # newest (open) = W24; scoreable = the RERUN that closed before it
    c = _FakeClient([
        {"release_key": "WR-2026-W24-PUB-E1", "created_at": "2026-06-08T02:00:00"},
        {"release_key": "WR-2026-W21-RERUN-E1", "created_at": "2026-06-02T16:34:23"},
        {"release_key": "WR-2026-W22-PUB-E1", "created_at": "2026-05-25T02:00:00"},
    ])
    assert _run(c) == "WR-2026-W21-RERUN-E1"


def test_raises_when_only_one_release():
    c = _FakeClient([{"release_key": "WR-2026-W22-PUB-E1", "created_at": "2026-05-25T02:00:00"}])
    with pytest.raises(RuntimeError, match="only one release"):
        _run(c)


def test_raises_when_no_releases():
    c = _FakeClient([])
    with pytest.raises(RuntimeError, match="no releases"):
        _run(c)


def test_raises_when_second_release_lacks_key():
    # A release with no release_key can't be a weekly WR- epoch, so the WR-
    # filter drops it — leaving only one weekly release to consider.
    c = _FakeClient([
        {"release_key": "WR-2026-W23-PUB-E1", "created_at": "2026-06-01T02:00:00"},
        {"created_at": "2026-05-25T02:00:00"},  # no release_key -> filtered out
    ])
    with pytest.raises(RuntimeError, match="only one release"):
        _run(c)


def test_does_not_use_newest_even_if_input_unsorted():
    # input deliberately unsorted; newest by created_at must be excluded
    c = _FakeClient([
        {"release_key": "WR-2026-W20-PUB-E1", "created_at": "2026-01-01T00:00:00"},
        {"release_key": "WR-2026-W24-PUB-E1", "created_at": "2026-06-02T00:00:00"},
        {"release_key": "WR-2026-W23-PUB-E1", "created_at": "2026-05-25T00:00:00"},
    ])
    assert _run(c) == "WR-2026-W23-PUB-E1"


def test_ignores_daily_bd_releases():
    """Daily-stream BD-YYYY-MM-DD releases share the /releases listing but no
    miner submits to them — so they must be filtered out, and the scorer must
    resolve to the second-newest WEEKLY epoch, never a daily one.

    Regression for 2026-08-03: a BD- epoch resolved as releases[1], scored
    0/256, and the weight override burned the whole W31 epoch on-chain.
    """
    c = _FakeClient([
        {"release_key": "BD-2026-05-26", "created_at": "2026-05-26T02:00:00"},
        {"release_key": "BD-2026-05-25", "created_at": "2026-05-25T02:00:00"},
        {"release_key": "WR-2026-W23-PUB-E1", "created_at": "2026-05-25T01:00:00"},
        {"release_key": "WR-2026-W22-PUB-E1", "created_at": "2026-05-18T02:00:00"},
    ])
    # BD- entries (newest by created_at) are ignored; open weekly = W23,
    # scoreable (closed) weekly = W22.
    assert _run(c) == "WR-2026-W22-PUB-E1"


def test_raises_when_only_daily_releases_present():
    """If the listing holds ONLY daily BD- epochs (no weekly), the resolver
    must raise a clear error rather than silently scoring a daily epoch."""
    c = _FakeClient([
        {"release_key": "BD-2026-08-03", "created_at": "2026-08-03T02:00:00"},
        {"release_key": "BD-2026-08-02", "created_at": "2026-08-02T02:00:00"},
    ])
    with pytest.raises(RuntimeError, match="no weekly"):
        _run(c)


def test_late_staged_newest_does_not_block_prior_scoring():
    """Regression: if the NEWEST epoch is published LATE on its Monday (after the
    05:00 close), the prior epoch must still resolve as scoreable — not get its
    deadline rolled a week forward (the W27-staged-~13:00 → W26-blocked bug)."""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    # Most recent past Monday at 13:00 UTC (a late stage, after the 05:00 close).
    monday = (now - timedelta(days=now.weekday())).replace(
        hour=13, minute=0, second=0, microsecond=0)
    if monday >= now:                      # today is Monday, before 13:00
        monday -= timedelta(days=7)
    c = _FakeClient([
        {"release_key": "WR-NEW-OPEN", "created_at": monday.replace(tzinfo=None).isoformat()},
        {"release_key": "WR-PRIOR-CLOSED", "created_at": (monday - timedelta(days=7)).replace(tzinfo=None).isoformat()},
    ])
    # Must NOT raise "has not closed yet" — the prior epoch closed at the Monday
    # 05:00, which is in the past regardless of the late 13:00 publish.
    assert _run(c) == "WR-PRIOR-CLOSED"
