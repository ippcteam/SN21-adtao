"""Tests for the validator HTTP API."""

import os

import pytest
from fastapi.testclient import TestClient

from hope.protocol.episode import (
    Episode, EpisodeMetadata, AccountState, ActionBundle, Action,
    PreWindow, CampaignTimeSeries, AccountAggregates, BundleSummary,
)
from hope.validator.api.server import create_app


@pytest.fixture(autouse=True)
def _disable_signatures():
    """Unit tests run without real wallets — disable signature requirement."""
    old = os.environ.get("REQUIRE_SIGNATURES")
    os.environ["REQUIRE_SIGNATURES"] = "false"
    yield
    if old is None:
        os.environ.pop("REQUIRE_SIGNATURES", None)
    else:
        os.environ["REQUIRE_SIGNATURES"] = old


def _make_episode(episode_id: str = "ep_test_001") -> Episode:
    return Episode(
        episode_metadata=EpisodeMetadata(
            episode_id=episode_id,
            schema_version="v1.9",
            coverage_status="baseline",
            measurement_resolution="high",
        ),
        account_state=AccountState(customer_id_hash="abc123"),
        date_index=["2026-01-01"] * 60,
        pre_window=PreWindow(
            campaigns={"camp_hash": CampaignTimeSeries(
                campaign_type="SEARCH",
                impressions=[100] * 60,
                clicks=[10] * 60,
                cost_micros=[1000000] * 60,
                conversions=[1.0] * 60,
                conversion_value_micros=[50000000] * 60,
                impression_share=[0.5] * 60,
            )},
            account_aggregates=AccountAggregates(avg_daily_spend_micros=1000000),
        ),
        action_bundle=ActionBundle(
            actions=[Action(type="BUDGET_CHANGE", scope="campaign")],
            bundle_summary=BundleSummary(action_count=1),
        ),
    )


def _state_with_episodes():
    return {
        "current_epoch_id": "test-epoch-1",
        "episodes": [_make_episode("ep_001"), _make_episode("ep_002")],
        "deadline": "2099-12-31T23:59:59+00:00",
        "submission_open": True,
        # Auth requires registered_miners to be non-empty
        "registered_miners": {"miner123", "miner_abc"},
    }


class TestHealthEndpoint:
    def test_health(self):
        app = create_app({})
        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestAuthEnforcement:
    def test_no_registered_miners_returns_503(self):
        """B6: Empty metagraph = reject all requests."""
        app = create_app({"current_epoch_id": "test", "episodes": []})
        client = TestClient(app)
        resp = client.get(
            "/v1/epochs/test/episodes",
            headers={"X-Miner-Hotkey": "miner123"},
        )
        assert resp.status_code == 503

    def test_unregistered_miner_returns_403(self):
        app = create_app(_state_with_episodes())
        client = TestClient(app)
        resp = client.get(
            "/v1/epochs/test-epoch-1/episodes",
            headers={"X-Miner-Hotkey": "unknown_miner"},
        )
        assert resp.status_code == 403


class TestEpisodeEndpoints:
    def test_list_episodes(self):
        app = create_app(_state_with_episodes())
        client = TestClient(app)
        resp = client.get(
            "/v1/epochs/test-epoch-1/episodes",
            headers={"X-Miner-Hotkey": "miner123"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["episode_count"] == 2
        assert len(data["episodes"]) == 2

    def test_get_single_episode(self):
        app = create_app(_state_with_episodes())
        client = TestClient(app)
        resp = client.get(
            "/v1/epochs/test-epoch-1/episodes/ep_001",
            headers={"X-Miner-Hotkey": "miner123"},
        )
        assert resp.status_code == 200
        assert resp.json()["episode_id"] == "ep_001"
        assert "payload" in resp.json()

    def test_wrong_epoch_returns_404(self):
        app = create_app(_state_with_episodes())
        client = TestClient(app)
        resp = client.get(
            "/v1/epochs/wrong-epoch/episodes",
            headers={"X-Miner-Hotkey": "miner123"},
        )
        assert resp.status_code == 404

    def test_missing_hotkey_returns_422(self):
        app = create_app(_state_with_episodes())
        client = TestClient(app)
        resp = client.get("/v1/epochs/test-epoch-1/episodes")
        assert resp.status_code == 422


class TestPredictionEndpoints:
    def test_submit_valid_prediction(self):
        app = create_app(_state_with_episodes())
        client = TestClient(app)

        prediction = {
            "predictions": [{
                "episode_id": "ep_001",
                "horizons": {
                    "7": {
                        "cost_delta_pct": {"p10": -5.0, "p50": -2.0, "p90": 1.0},
                        "conversions_delta_pct": {"p10": -3.0, "p50": 0.0, "p90": 3.0},
                        "efficiency_delta_pct": {"p10": -2.0, "p50": 1.0, "p90": 4.0},
                        "goal_miss_probability": 0.2,
                        "instability_risk": 0.1,
                    },
                    "14": {
                        "cost_delta_pct": {"p10": -6.0, "p50": -3.0, "p90": 0.0},
                        "conversions_delta_pct": {"p10": -4.0, "p50": -1.0, "p90": 2.0},
                        "efficiency_delta_pct": {"p10": -3.0, "p50": 0.5, "p90": 3.5},
                        "goal_miss_probability": 0.25,
                        "instability_risk": 0.08,
                    },
                },
            }],
        }

        resp = client.post(
            "/v1/epochs/test-epoch-1/predictions",
            json=prediction,
            headers={"X-Miner-Hotkey": "miner_abc"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] == 1
        assert data["rejected"] == 0

    def test_reject_unknown_episode(self):
        app = create_app(_state_with_episodes())
        client = TestClient(app)

        prediction = {
            "predictions": [{
                "episode_id": "nonexistent_ep",
                "horizons": {
                    "7": {
                        "cost_delta_pct": {"p10": 0, "p50": 0, "p90": 0},
                        "conversions_delta_pct": {"p10": 0, "p50": 0, "p90": 0},
                        "efficiency_delta_pct": {"p10": 0, "p50": 0, "p90": 0},
                        "goal_miss_probability": 0.0,
                        "instability_risk": 0.0,
                    },
                },
            }],
        }

        resp = client.post(
            "/v1/epochs/test-epoch-1/predictions",
            json=prediction,
            headers={"X-Miner-Hotkey": "miner_abc"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] == 0
        assert data["rejected"] == 1


class TestCommitmentEndpoints:
    def test_no_commitment_returns_409(self):
        app = create_app(_state_with_episodes())
        client = TestClient(app)
        resp = client.get("/v1/epochs/test-epoch-1/commitment")
        assert resp.status_code == 409

    def test_commitment_returns_data(self):
        state = _state_with_episodes()
        state["commitment"] = {
            "hash": "abc123",
            "merkle_root": "def456",
            "committed_at": "2026-04-23T00:00:00Z",
            "episode_count": 2,
        }
        app = create_app(state)
        client = TestClient(app)
        resp = client.get("/v1/epochs/test-epoch-1/commitment")
        assert resp.status_code == 200
        assert resp.json()["commitment_hash"] == "abc123"


class TestVerificationEndpoints:
    def test_no_reveal_returns_409(self):
        app = create_app(_state_with_episodes())
        client = TestClient(app)
        resp = client.get("/v1/epochs/test-epoch-1/verification")
        assert resp.status_code == 409

    def test_scores_not_ready_returns_409(self):
        app = create_app(_state_with_episodes())
        client = TestClient(app)
        resp = client.get("/v1/epochs/test-epoch-1/scores")
        assert resp.status_code == 409
