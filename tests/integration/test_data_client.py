"""Integration test — fetch real data from the live HOPE API."""

import asyncio

import pytest

from hope.validator.data_client import HopeDataClient


LIVE_API_KEY = "hope-bt-internal-2026"
LIVE_RELEASE_KEY = "WR-2026-W18-PUB-E1"


@pytest.fixture
def client():
    return HopeDataClient(api_key=LIVE_API_KEY)


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TestHopeDataClientLive:
    """Tests against the live HOPE Bittensor API on Render."""

    def test_list_releases(self, client):
        releases = _run(client.list_releases())
        assert len(releases) >= 1
        keys = [r["release_key"] for r in releases]
        assert LIVE_RELEASE_KEY in keys

    def test_fetch_release_metadata(self, client):
        meta = _run(client.fetch_release_metadata(LIVE_RELEASE_KEY))
        assert meta["success"] is True
        release = meta["release"]
        assert release["schema_version"] == "v1.9"
        assert release["phase_number"] == 1

    def test_fetch_epoch_data(self, client):
        data = _run(client.fetch_epoch_data(LIVE_RELEASE_KEY))
        assert data.release_key == LIVE_RELEASE_KEY
        assert data.schema_version == "v1.9"
        assert data.episode_count > 0
        assert len(data.episodes) == data.episode_count
        assert len(data.outcomes) == data.episode_count

    def test_episodes_are_valid(self, client):
        data = _run(client.fetch_epoch_data(LIVE_RELEASE_KEY))
        ep = data.episodes[0]
        assert ep.episode_metadata.schema_version == "v1.9"
        assert ep.episode_metadata.episode_id != ""
        assert len(ep.date_index) == 60
        assert len(ep.action_bundle.actions) > 0
        assert ep.account_state.customer_id_hash != ""
        assert len(ep.pre_window.campaigns) > 0
        campaign = list(ep.pre_window.campaigns.values())[0]
        assert campaign.campaign_type == "SEARCH"

    def test_outcomes_parsed(self, client):
        data = _run(client.fetch_epoch_data(LIVE_RELEASE_KEY))
        with_t7 = sum(1 for o in data.outcomes if o.t7 is not None)
        assert with_t7 > 0, "Expected at least some episodes with t7 outcomes"

    def test_package_hash_verification(self, client):
        data = _run(client.fetch_epoch_data(LIVE_RELEASE_KEY))
        assert client.verify_package_hash(data), "Package hash should verify"

    def test_governance_summary(self, client):
        summary = _run(client.fetch_governance_summary())
        assert summary["success"] is True
        assert summary["accounts"] > 4000
