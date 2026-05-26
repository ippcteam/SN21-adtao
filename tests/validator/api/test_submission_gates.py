"""Tests for the registration + late-submission gates on POST /predictions.

These cover the two ways a miner can have their submission silently
dropped at scoring time a week later:

  1. Hotkey on the metagraph but never ran ``sn21_keys.py register``
     (no sn21-reg-v1 binding).
  2. Submission lands after the mining-window deadline.

Both are now caught at HTTP submission time with actionable errors.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from hope.validator.api.server import create_app


# Disable miner signature requirement so the tests can drive the HTTP
# layer without setting up a full bittensor wallet — the gates we are
# testing run AFTER auth, on a state dict we control directly.
@pytest.fixture(autouse=True)
def _no_sigs(monkeypatch):
    monkeypatch.setenv("REQUIRE_SIGNATURES", "false")


HOTKEY_REGISTERED = "5GregisteredAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
HOTKEY_UNREGISTERED = "5HunregisteredBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
EPOCH_ID = "WR-2026-W99-PUB-E1"


def _open_window_state(reg_set: set[str], gate_enabled: bool = True) -> dict:
    deadline = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    return {
        "current_epoch_id": EPOCH_ID,
        "episodes": [],
        "predictions": {},
        "prediction_receipts": {},
        "deadline": deadline,
        "submission_open": True,
        "registered_miners": {HOTKEY_REGISTERED, HOTKEY_UNREGISTERED},
        "uid_map": {HOTKEY_REGISTERED: 0, HOTKEY_UNREGISTERED: 1},
        "registered_ed25519_hotkeys": reg_set,
        "_reg_index_path": "/tmp/fake-reg.json" if gate_enabled else None,
    }


def _closed_window_state() -> dict:
    deadline = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    return {
        "current_epoch_id": EPOCH_ID,
        "episodes": [],
        "predictions": {},
        "prediction_receipts": {},
        "deadline": deadline,
        "submission_open": False,
        "registered_miners": {HOTKEY_REGISTERED},
        "uid_map": {HOTKEY_REGISTERED: 0},
        "registered_ed25519_hotkeys": {HOTKEY_REGISTERED},
        "_reg_index_path": "/tmp/fake-reg.json",
    }


def _post_predictions(client: TestClient, hotkey: str) -> "TestClient":
    return client.post(
        f"/v1/epochs/{EPOCH_ID}/predictions",
        headers={"X-Miner-Hotkey": hotkey},
        json={"predictions": []},
    )


# --- registration gate -----------------------------------------------------


def test_unregistered_hotkey_rejected_with_actionable_error():
    """Hotkey on metagraph but missing sn21-reg-v1 → 403 with fix hint."""
    state = _open_window_state({HOTKEY_REGISTERED})
    app = create_app(state)
    client = TestClient(app)

    resp = _post_predictions(client, HOTKEY_UNREGISTERED)

    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error"] == "hotkey_not_registered"
    assert "sn21-reg-v1" in detail["message"]
    assert "sn21_keys.py register" in detail["fix"]


def test_registered_hotkey_passes_gate():
    """When hotkey is in the index, the registration gate does not block."""
    state = _open_window_state({HOTKEY_REGISTERED})
    app = create_app(state)
    client = TestClient(app)

    resp = _post_predictions(client, HOTKEY_REGISTERED)

    # Either 200 with zero accepted (empty batch) or a downstream
    # validation error — but NOT a 403 hotkey_not_registered.
    if resp.status_code == 403:
        detail = resp.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("error") != "hotkey_not_registered"


def test_gate_open_when_no_index_loaded():
    """Empty registration set + no index path = gate disabled, no 403."""
    state = _open_window_state(set(), gate_enabled=False)
    app = create_app(state)
    client = TestClient(app)

    resp = _post_predictions(client, HOTKEY_UNREGISTERED)

    # Must not 403 with hotkey_not_registered — operator hasn't enabled
    # the gate so submissions go through.
    if resp.status_code == 403:
        detail = resp.json().get("detail", {})
        if isinstance(detail, dict):
            assert detail.get("error") != "hotkey_not_registered"


# --- late-submission gate --------------------------------------------------


def test_late_submission_returns_deadline_aware_error():
    """Past deadline → 403 with deadline_utc + seconds_late + fix."""
    state = _closed_window_state()
    app = create_app(state)
    client = TestClient(app)

    resp = _post_predictions(client, HOTKEY_REGISTERED)

    assert resp.status_code == 403
    detail = resp.json()["detail"]
    assert detail["error"] == "submission_window_closed"
    assert detail["deadline_utc"] is not None
    assert detail["seconds_late"] is not None
    assert detail["seconds_late"] > 0
    assert "next weekly epoch" in detail["fix"]


# --- /health exposure ------------------------------------------------------


def test_health_exposes_deadline_and_submission_open():
    """Miners must be able to read the deadline + open flag from /health."""
    state = _open_window_state({HOTKEY_REGISTERED})
    app = create_app(state)
    client = TestClient(app)

    resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["current_epoch"] == EPOCH_ID
    assert body["deadline_utc"] is not None
    assert body["submission_open"] is True
    # 24 hours of headroom in the fixture state.
    assert body["seconds_until_deadline"] > 0


def test_health_reports_negative_seconds_when_closed():
    state = _closed_window_state()
    app = create_app(state)
    client = TestClient(app)

    resp = client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["submission_open"] is False
    assert body["seconds_until_deadline"] < 0


# --- /v1/registration-status -----------------------------------------------


def test_registration_status_for_registered_hotkey():
    state = _open_window_state({HOTKEY_REGISTERED})
    app = create_app(state)
    client = TestClient(app)

    resp = client.get(
        "/v1/registration-status",
        headers={"X-Miner-Hotkey": HOTKEY_REGISTERED},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["registered"] is True
    assert body["gate_enabled"] is True
    assert "registered" in body["hint"]


def test_registration_status_for_unregistered_hotkey():
    state = _open_window_state({HOTKEY_REGISTERED})
    app = create_app(state)
    client = TestClient(app)

    resp = client.get(
        "/v1/registration-status",
        headers={"X-Miner-Hotkey": HOTKEY_UNREGISTERED},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["registered"] is False
    assert body["gate_enabled"] is True
    assert "sn21_keys.py register" in body["hint"]


def test_registration_status_when_gate_disabled():
    state = _open_window_state(set(), gate_enabled=False)
    app = create_app(state)
    client = TestClient(app)

    resp = client.get(
        "/v1/registration-status",
        headers={"X-Miner-Hotkey": HOTKEY_REGISTERED},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["gate_enabled"] is False
    assert "no registration index" in body["hint"].lower()
