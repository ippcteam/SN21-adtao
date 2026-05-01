"""End-to-end test: full miner flow against a running validator.

Simulates what a real miner does from scratch:
1. Discover epoch via /health
2. Fetch episode list
3. Fetch individual episodes
4. Fetch batch episodes
5. Generate predictions with baseline model
6. Submit predictions with signed requests
7. Verify receipt
8. Attempt duplicate submission (should overwrite, not duplicate)
9. Attempt submission after deadline (should reject)
10. Attempt unsigned submission (should reject)
11. Attempt submission from unregistered miner (should reject)

Uses real ed25519 keypairs — same as production.
"""

import hashlib
import json
import os
import time

import pytest
from fastapi.testclient import TestClient
from substrateinterface import Keypair

from hope.miner.models.baseline import BaselineModel
from hope.protocol.episode import (
    Episode, EpisodeMetadata, AccountState, ActionBundle, Action,
    PreWindow, CampaignTimeSeries, AccountAggregates, BundleSummary,
)
from hope.validator.api.server import create_app


# Two real miners with real keypairs
MINER_A = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
MINER_B = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
OUTSIDER = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())  # not registered


def _sign(keypair: Keypair, method: str, path: str, body: bytes = b"") -> dict:
    nonce = str(time.time())
    body_hash = hashlib.sha256(body).hexdigest()
    message = hashlib.sha256(
        f"{keypair.ss58_address}:{nonce}:{method}:{path}:{body_hash}".encode()
    ).hexdigest()
    signature = keypair.sign(message.encode()).hex()
    return {
        "X-Miner-Hotkey": keypair.ss58_address,
        "X-Miner-Nonce": nonce,
        "X-Miner-Signature": signature,
    }


def _make_episode(episode_id: str) -> Episode:
    return Episode(
        episode_metadata=EpisodeMetadata(
            episode_id=episode_id,
            schema_version="v1.9",
            coverage_status="baseline",
            measurement_resolution="high",
        ),
        account_state=AccountState(customer_id_hash=f"hash_{episode_id}"),
        date_index=["2026-01-01"] * 60,
        pre_window=PreWindow(
            campaigns={"camp1": CampaignTimeSeries(
                campaign_type="SEARCH",
                impressions=[100 + i for i in range(60)],
                clicks=[10 + i for i in range(60)],
                cost_micros=[1000000 + i * 10000 for i in range(60)],
                conversions=[1.0 + i * 0.1 for i in range(60)],
                conversion_value_micros=[50000000] * 60,
                impression_share=[0.5] * 60,
            )},
            account_aggregates=AccountAggregates(avg_daily_spend_micros=1500000),
        ),
        action_bundle=ActionBundle(
            actions=[Action(type="BUDGET_CHANGE", scope="campaign")],
            bundle_summary=BundleSummary(action_count=1),
        ),
    )


def _make_prediction(episode_id: str) -> dict:
    """Build a prediction payload like a real miner would submit."""
    return {
        "episode_id": episode_id,
        "horizons": {
            "7": {
                "cost_delta_pct": {"p10": -8.0, "p50": -3.0, "p90": 2.0},
                "conversions_delta_pct": {"p10": -5.0, "p50": 1.0, "p90": 7.0},
                "efficiency_delta_pct": {"p10": -4.0, "p50": 2.0, "p90": 8.0},
                "goal_miss_probability": 0.15,
                "instability_risk": 0.1,
            },
            "14": {
                "cost_delta_pct": {"p10": -10.0, "p50": -4.0, "p90": 1.0},
                "conversions_delta_pct": {"p10": -6.0, "p50": 0.5, "p90": 6.0},
                "efficiency_delta_pct": {"p10": -5.0, "p50": 1.5, "p90": 7.0},
                "goal_miss_probability": 0.2,
                "instability_risk": 0.08,
            },
        },
    }


@pytest.fixture
def app_and_client():
    os.environ["REQUIRE_SIGNATURES"] = "true"
    episodes = [_make_episode(f"ep_{i:03d}") for i in range(10)]
    state = {
        "current_epoch_id": "test-epoch-e2e",
        "episodes": episodes,
        "deadline": "2099-12-31T23:59:59+00:00",
        "submission_open": True,
        "registered_miners": {MINER_A.ss58_address, MINER_B.ss58_address},
    }
    app = create_app(state)
    client = TestClient(app)
    yield app, client, state


class TestMinerE2EFlow:
    """Full miner lifecycle from a new miner's perspective."""

    def test_01_discover_epoch(self, app_and_client):
        """Step 1: Miner hits /health to discover current epoch."""
        _, client, _ = app_and_client
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["current_epoch"] == "test-epoch-e2e"
        assert data["episodes_loaded"] == 10

    def test_02_fetch_episode_list(self, app_and_client):
        """Step 2: Miner fetches episode list (requires signed request)."""
        _, client, _ = app_and_client
        path = "/v1/epochs/test-epoch-e2e/episodes"
        headers = _sign(MINER_A, "GET", path)
        resp = client.get(path, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["episode_count"] == 10
        assert len(data["episodes"]) == 10

    def test_03_fetch_single_episode(self, app_and_client):
        """Step 3: Miner fetches one episode's full payload."""
        _, client, _ = app_and_client
        path = "/v1/epochs/test-epoch-e2e/episodes/ep_000"
        headers = _sign(MINER_A, "GET", path)
        resp = client.get(path, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["episode_id"] == "ep_000"
        assert "payload" in data
        payload = data["payload"]
        assert payload["episode_metadata"]["schema_version"] == "v1.9"
        assert len(payload["date_index"]) == 60

    def test_04_fetch_batch_episodes(self, app_and_client):
        """Step 4: Miner fetches all episodes in one request."""
        _, client, _ = app_and_client
        path = "/v1/epochs/test-epoch-e2e/episodes_batch"
        headers = _sign(MINER_A, "GET", path)
        resp = client.get(path, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["episodes"]) == 10

    def test_05_submit_predictions(self, app_and_client):
        """Step 5: Miner submits predictions for all episodes."""
        _, client, _ = app_and_client
        path = "/v1/epochs/test-epoch-e2e/predictions"
        predictions = [_make_prediction(f"ep_{i:03d}") for i in range(10)]
        body = json.dumps({"predictions": predictions}, sort_keys=True).encode()
        headers = _sign(MINER_A, "POST", path, body)
        headers["Content-Type"] = "application/json"
        resp = client.post(path, content=body, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] == 10
        assert data["rejected"] == 0
        assert data["total_episodes_covered"] == 10
        assert "receipt_hash" in data

    def test_06_duplicate_overwrites_not_duplicates(self, app_and_client):
        """Step 6: Resubmitting for same episode overwrites — no duplication."""
        app, client, _ = app_and_client

        # Submit once
        path = "/v1/epochs/test-epoch-e2e/predictions"
        pred = [_make_prediction("ep_000")]
        body = json.dumps({"predictions": pred}, sort_keys=True).encode()
        headers = _sign(MINER_A, "POST", path, body)
        headers["Content-Type"] = "application/json"
        resp1 = client.post(path, content=body, headers=headers)
        assert resp1.status_code == 200
        assert resp1.json()["total_episodes_covered"] == 1

        # Submit again for same episode — should overwrite
        body2 = json.dumps({"predictions": pred}, sort_keys=True).encode()
        headers2 = _sign(MINER_A, "POST", path, body2)
        headers2["Content-Type"] = "application/json"
        resp2 = client.post(path, content=body2, headers=headers2)
        assert resp2.status_code == 200
        # Still 1 episode covered, not 2
        assert resp2.json()["total_episodes_covered"] == 1

    def test_07_unsigned_request_rejected(self, app_and_client):
        """Step 7: Unsigned request must be rejected (REQUIRE_SIGNATURES=true)."""
        _, client, _ = app_and_client
        resp = client.get(
            "/v1/epochs/test-epoch-e2e/episodes",
            headers={"X-Miner-Hotkey": MINER_A.ss58_address},
        )
        assert resp.status_code == 401

    def test_08_unregistered_miner_rejected(self, app_and_client):
        """Step 8: Miner not in metagraph must be rejected."""
        _, client, _ = app_and_client
        path = "/v1/epochs/test-epoch-e2e/episodes"
        headers = _sign(OUTSIDER, "GET", path)
        resp = client.get(path, headers=headers)
        assert resp.status_code == 403

    def test_09_wrong_epoch_returns_404(self, app_and_client):
        """Step 9: Requesting wrong epoch returns 404."""
        _, client, _ = app_and_client
        path = "/v1/epochs/wrong-epoch/episodes"
        headers = _sign(MINER_A, "GET", path)
        resp = client.get(path, headers=headers)
        assert resp.status_code == 404

    def test_10_invalid_episode_id_rejected(self, app_and_client):
        """Step 10: Prediction for nonexistent episode is rejected."""
        _, client, _ = app_and_client
        path = "/v1/epochs/test-epoch-e2e/predictions"
        pred = [_make_prediction("nonexistent_ep")]
        body = json.dumps({"predictions": pred}, sort_keys=True).encode()
        headers = _sign(MINER_A, "POST", path, body)
        headers["Content-Type"] = "application/json"
        resp = client.post(path, content=body, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["accepted"] == 0
        assert resp.json()["rejected"] == 1

    def test_11_out_of_range_quantiles_rejected(self, app_and_client):
        """Step 11: Extreme quantile values outside [-100, 100] are rejected."""
        _, client, _ = app_and_client
        path = "/v1/epochs/test-epoch-e2e/predictions"
        pred = [{
            "episode_id": "ep_000",
            "horizons": {
                "7": {
                    "cost_delta_pct": {"p10": -200.0, "p50": 0.0, "p90": 200.0},
                    "conversions_delta_pct": {"p10": -5.0, "p50": 1.0, "p90": 7.0},
                    "efficiency_delta_pct": {"p10": -4.0, "p50": 2.0, "p90": 8.0},
                    "goal_miss_probability": 0.15,
                    "instability_risk": 0.1,
                },
            },
        }]
        body = json.dumps({"predictions": pred}, sort_keys=True).encode()
        headers = _sign(MINER_A, "POST", path, body)
        headers["Content-Type"] = "application/json"
        resp = client.post(path, content=body, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["accepted"] == 0
        assert resp.json()["rejected"] == 1

    def test_12_batch_size_limit_enforced(self, app_and_client):
        """Step 12: Batch larger than 250 predictions is rejected."""
        _, client, _ = app_and_client
        path = "/v1/epochs/test-epoch-e2e/predictions"
        pred = [_make_prediction(f"ep_{i:03d}") for i in range(251)]
        body = json.dumps({"predictions": pred}, sort_keys=True).encode()
        headers = _sign(MINER_A, "POST", path, body)
        headers["Content-Type"] = "application/json"
        resp = client.post(path, content=body, headers=headers)
        assert resp.status_code == 422  # Pydantic validation error

    def test_13_second_miner_independent(self, app_and_client):
        """Step 13: Two miners submit independently — no interference."""
        _, client, _ = app_and_client
        path = "/v1/epochs/test-epoch-e2e/predictions"

        # Miner A submits for ep_000
        pred_a = [_make_prediction("ep_000")]
        body_a = json.dumps({"predictions": pred_a}, sort_keys=True).encode()
        headers_a = _sign(MINER_A, "POST", path, body_a)
        headers_a["Content-Type"] = "application/json"
        resp_a = client.post(path, content=body_a, headers=headers_a)
        assert resp_a.status_code == 200
        assert resp_a.json()["accepted"] == 1

        # Miner B submits for ep_001
        pred_b = [_make_prediction("ep_001")]
        body_b = json.dumps({"predictions": pred_b}, sort_keys=True).encode()
        headers_b = _sign(MINER_B, "POST", path, body_b)
        headers_b["Content-Type"] = "application/json"
        resp_b = client.post(path, content=body_b, headers=headers_b)
        assert resp_b.status_code == 200
        assert resp_b.json()["accepted"] == 1

        # Miner B's submission doesn't affect Miner A's count
        # Each miner has 1 episode covered independently
        assert resp_a.json()["total_episodes_covered"] == 1
        assert resp_b.json()["total_episodes_covered"] == 1

    def test_14_closed_submission_rejected(self, app_and_client):
        """Step 14: After deadline, predictions are rejected."""
        app, client, state = app_and_client
        # Close submissions
        state["submission_open"] = False
        app.state.validator = state

        path = "/v1/epochs/test-epoch-e2e/predictions"
        pred = [_make_prediction("ep_000")]
        body = json.dumps({"predictions": pred}, sort_keys=True).encode()
        headers = _sign(MINER_A, "POST", path, body)
        headers["Content-Type"] = "application/json"
        resp = client.post(path, content=body, headers=headers)
        assert resp.status_code == 403
        assert "closed" in resp.json()["detail"].lower()

    def test_15_baseline_model_produces_valid_predictions(self, app_and_client):
        """Step 15: The baseline model produces predictions that pass validation."""
        _, client, _ = app_and_client
        model = BaselineModel()
        episodes = [_make_episode(f"ep_{i:03d}") for i in range(5)]

        predictions = []
        for ep in episodes:
            pred = model.predict(ep)
            predictions.append({
                "episode_id": pred.episode_id,
                "horizons": {
                    h_key: {
                        "cost_delta_pct": h.cost_delta_pct.model_dump(),
                        "conversions_delta_pct": h.conversions_delta_pct.model_dump(),
                        "efficiency_delta_pct": h.efficiency_delta_pct.model_dump(),
                        "goal_miss_probability": h.goal_miss_probability,
                        "instability_risk": h.instability_risk,
                    }
                    for h_key, h in pred.horizons.items()
                },
            })

        path = "/v1/epochs/test-epoch-e2e/predictions"
        body = json.dumps({"predictions": predictions}, sort_keys=True).encode()
        headers = _sign(MINER_A, "POST", path, body)
        headers["Content-Type"] = "application/json"
        resp = client.post(path, content=body, headers=headers)
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] == 5
        assert data["rejected"] == 0
