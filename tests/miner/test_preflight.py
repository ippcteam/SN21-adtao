"""Tests for hope.miner.preflight — the miner's pre-flight checks.

Covers both checks the runner makes before generating predictions:
  1. check_registration: validator's reg-index says yes / no / gate-off.
  2. check_submission_window: deadline still in the future / past.

The checks degrade gracefully — network errors and older validators
without the new fields don't block the miner; only authoritative
negatives do.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from hope.miner.preflight import (
    PreflightError,
    check_registration,
    check_submission_window,
)


class _FakeResp:
    def __init__(self, status_code: int, body: dict | str = ""):
        self.status_code = status_code
        self._body = body
        self.text = body if isinstance(body, str) else json.dumps(body)

    def json(self):
        if isinstance(self._body, dict):
            return self._body
        raise ValueError("not json")


@pytest.fixture
def patch_get(monkeypatch):
    """Patch httpx.get with a controllable stub. Returns the setter."""
    calls = []
    response = {"value": _FakeResp(200, {})}

    def _stub(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers, "timeout": timeout})
        return response["value"]

    monkeypatch.setattr("hope.miner.preflight.httpx.get", _stub)
    return response, calls


# --- check_registration ----------------------------------------------------


def test_registration_check_passes_when_hotkey_in_index(patch_get):
    response, _ = patch_get
    response["value"] = _FakeResp(200, {
        "registered": True, "gate_enabled": True, "hint": "ok",
    })

    # Should NOT raise.
    check_registration("https://validator.example", sign_headers={"X-Miner-Hotkey": "x"})


def test_registration_check_raises_when_hotkey_missing(patch_get):
    response, _ = patch_get
    response["value"] = _FakeResp(200, {
        "registered": False,
        "gate_enabled": True,
        "hint": "Run sn21_keys.py register",
    })

    with pytest.raises(PreflightError, match="NOT registered"):
        check_registration("https://validator.example", sign_headers={"X-Miner-Hotkey": "x"})


def test_registration_check_silent_when_gate_disabled(patch_get):
    """Validator has no reg-index loaded: degrade to no-op."""
    response, _ = patch_get
    response["value"] = _FakeResp(200, {
        "registered": False, "gate_enabled": False, "hint": "no index",
    })

    # Must not raise — gate is open on this validator.
    check_registration("https://validator.example", sign_headers={"X-Miner-Hotkey": "x"})


def test_registration_check_silent_on_older_validator_404(patch_get):
    response, _ = patch_get
    response["value"] = _FakeResp(404, "not found")

    # Older validator without the endpoint — don't block.
    check_registration("https://validator.example", sign_headers={"X-Miner-Hotkey": "x"})


def test_registration_check_silent_on_network_error(monkeypatch):
    import httpx as _httpx

    def _raise(*_a, **_k):
        raise _httpx.ConnectError("boom")

    monkeypatch.setattr("hope.miner.preflight.httpx.get", _raise)

    # Network failure must not block the miner.
    check_registration("https://validator.example", sign_headers={"X-Miner-Hotkey": "x"})


# --- check_submission_window ----------------------------------------------


def test_window_check_passes_when_deadline_in_future(patch_get):
    response, _ = patch_get
    response["value"] = _FakeResp(200, {
        "current_epoch": "WR-2026-W99-PUB-E1",
        "deadline_utc": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
        "seconds_until_deadline": 86400,
        "submission_open": True,
    })

    check_submission_window("https://validator.example")


def test_window_check_raises_when_deadline_passed(patch_get):
    response, _ = patch_get
    response["value"] = _FakeResp(200, {
        "current_epoch": "WR-2026-W99-PUB-E1",
        "deadline_utc": (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),
        "seconds_until_deadline": -3600,
        "submission_open": False,
    })

    with pytest.raises(PreflightError, match="CLOSED"):
        check_submission_window("https://validator.example")


def test_window_check_raises_when_explicitly_closed(patch_get):
    """submission_open=false even if seconds_until is still positive."""
    response, _ = patch_get
    response["value"] = _FakeResp(200, {
        "current_epoch": "WR-2026-W99-PUB-E1",
        "deadline_utc": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "seconds_until_deadline": 3600,
        "submission_open": False,
    })

    with pytest.raises(PreflightError, match="CLOSED"):
        check_submission_window("https://validator.example")


def test_window_check_silent_on_older_validator(patch_get):
    """Validator pre-deadline-fields: don't block."""
    response, _ = patch_get
    response["value"] = _FakeResp(200, {
        "current_epoch": "WR-2026-W99-PUB-E1",
        "episodes_loaded": 65,
    })

    check_submission_window("https://validator.example")


def test_window_check_silent_on_network_error(monkeypatch):
    import httpx as _httpx

    def _raise(*_a, **_k):
        raise _httpx.ConnectError("boom")

    monkeypatch.setattr("hope.miner.preflight.httpx.get", _raise)

    check_submission_window("https://validator.example")
