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


def _deadline_for(opened):
    """The deadline the guard will compute for a given newest-release
    created_at — mirrors data_client's two branches.

    Kept in the test rather than imported so a change to the RULE has to be
    made deliberately in both places, not silently inherited.
    """
    from hope.validator.epoch_manager import MINING_CLOSE_HOUR_UTC
    if opened.weekday() == 0:            # Monday cadence: same-day close
        return opened.replace(hour=MINING_CLOSE_HOUR_UTC, minute=0,
                              second=0, microsecond=0)
    return next_mining_close(opened)


def test_blocks_release_that_has_not_closed():
    """The guard must refuse to score an epoch whose window is still open.

    DATE-INDEPENDENT BY CONSTRUCTION. The original seeded the newest release at
    `now` and asserted it ALWAYS raises. That held six days a week and failed
    every Monday after 05:00 UTC: a Monday created_at takes the same-day-close
    branch, so the deadline is 05:00 TODAY, already past — and the epoch really
    has closed, so not raising is correct. The test was wrong, not the code, but
    it cost real time to prove that on 2026-08-03. This version picks a
    created_at whose deadline is genuinely in the future on ANY day.
    """
    now = datetime.now(timezone.utc)
    # Walk back to a created_at whose computed deadline is still ahead of now.
    opened = next(o for o in (now + timedelta(days=d) for d in range(0, 8))
                  if _deadline_for(o) > now)
    c = _client([
        {"release_key": "WR-2026-W98-PUB-E1", "created_at": opened.isoformat()},
        {"release_key": "WR-2026-W97-PUB-E1",
         "created_at": (opened - timedelta(days=7)).isoformat()},
    ])
    assert _deadline_for(opened) > now, "test setup failed to find an open epoch"
    with pytest.raises(RuntimeError, match="has not closed yet"):
        asyncio.run(c.discover_scoreable_release())


def test_the_monday_branch_allows_scoring_once_the_close_hour_has_passed():
    """The other half of the rule, which the old test never covered: on a
    Monday AFTER 05:00 the prior epoch has genuinely closed and must be
    scoreable. This is the case that exposed the flaw above."""
    from hope.validator.epoch_manager import MINING_CLOSE_HOUR_UTC
    monday = datetime.now(timezone.utc) - timedelta(days=datetime.now(timezone.utc).weekday())
    opened = monday.replace(hour=MINING_CLOSE_HOUR_UTC, minute=0, second=0,
                            microsecond=0) - timedelta(days=7)
    c = _client([
        {"release_key": "WR-2026-W98-PUB-E1", "created_at": opened.isoformat()},
        {"release_key": "WR-2026-W97-PUB-E1",
         "created_at": (opened - timedelta(days=7)).isoformat()},
    ])
    assert asyncio.run(c.discover_scoreable_release()) == "WR-2026-W97-PUB-E1"


def test_returns_release_once_closed():
    now = datetime.now(timezone.utc)
    # newest created 14 days ago → its Monday-close is long past → prior epoch
    # is closed → scoreable.
    c = _client([
        {"release_key": "WR-2026-W98-PUB-E1", "created_at": (now - timedelta(days=14)).isoformat()},
        {"release_key": "WR-2026-W97-PUB-E1", "created_at": (now - timedelta(days=21)).isoformat()},
    ])
    assert asyncio.run(c.discover_scoreable_release()) == "WR-2026-W97-PUB-E1"
