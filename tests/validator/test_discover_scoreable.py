"""The scoreable-release discovery must NOT return an epoch whose submission
deadline hasn't passed yet.

Regression for the premature-scoring bug: the next epoch is created Mon ~02:00
UTC while the prior epoch's window stays open until that same Mon 05:00 — a ~3h
gap where `releases[1]` is the second-newest but still OPEN. Scoring it then
committed an incomplete 9.C.1 and the `already_scored` guard locked the epoch at
~100% burn (what happened to W24).
"""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from hope.validator.data_client import HopeDataClient
from hope.validator.epoch_manager import next_mining_close


def _client(releases):
    c = HopeDataClient.__new__(HopeDataClient)  # skip __init__ (no API key needed)

    async def _fake_list():
        return releases

    c.list_releases = _fake_list  # type: ignore[assignment]
    return c


def test_blocks_release_that_has_not_closed():
    now = datetime.now(timezone.utc)
    # Anchor the newest release on a FUTURE Monday-close so the test is
    # deterministic regardless of the wall-clock day it runs (a "created now"
    # anchor has its own 05:00 close already in the past when run on a Monday
    # afternoon, which would not block). newest close in the future → the prior
    # epoch is still open → must NOT be scoreable.
    future_close = next_mining_close(now)  # next Mon 05:00 UTC, always > now
    c = _client([
        {"release_key": "WR-2026-W31-PUB-E1", "created_at": future_close.isoformat()},
        {"release_key": "WR-2026-W30-PUB-E1", "created_at": (future_close - timedelta(days=7)).isoformat()},
    ])
    with pytest.raises(RuntimeError, match="has not closed yet"):
        asyncio.run(c.discover_scoreable_release())


def test_returns_release_once_closed():
    now = datetime.now(timezone.utc)
    # newest created 14 days ago → its Monday-close is long past → prior epoch
    # is closed → scoreable.
    c = _client([
        {"release_key": "WR-2026-W31-PUB-E1", "created_at": (now - timedelta(days=14)).isoformat()},
        {"release_key": "WR-2026-W30-PUB-E1", "created_at": (now - timedelta(days=21)).isoformat()},
    ])
    assert asyncio.run(c.discover_scoreable_release()) == "WR-2026-W30-PUB-E1"
