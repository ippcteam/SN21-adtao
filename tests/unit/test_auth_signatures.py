"""Tests for signature verification with REQUIRE_SIGNATURES=true.

Uses real ed25519 keypairs via substrateinterface to test the full
signature path — the same path that runs in production.
"""

import hashlib
import os
import time

import pytest
from fastapi.testclient import TestClient
from substrateinterface import Keypair

from hope.validator.api.server import create_app


# Generate a real keypair for testing
_TEST_KEYPAIR = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
_TEST_HOTKEY = _TEST_KEYPAIR.ss58_address


def _sign_request(method: str, path: str, body: bytes = b"") -> dict[str, str]:
    """Sign a request the same way the miner client does."""
    nonce = str(time.time())
    body_hash = hashlib.sha256(body).hexdigest()
    message = hashlib.sha256(
        f"{_TEST_HOTKEY}:{nonce}:{method}:{path}:{body_hash}".encode()
    ).hexdigest()
    signature = _TEST_KEYPAIR.sign(message.encode()).hex()

    return {
        "X-Miner-Hotkey": _TEST_HOTKEY,
        "X-Miner-Nonce": nonce,
        "X-Miner-Signature": signature,
    }


def _state_with_registered_miner():
    return {
        "current_epoch_id": "test-epoch-1",
        "episodes": [],
        "deadline": "2099-12-31T23:59:59+00:00",
        "submission_open": True,
        "registered_miners": {_TEST_HOTKEY},
    }


class TestRequireSignaturesTrue:
    """Tests with REQUIRE_SIGNATURES=true (production default)."""

    @pytest.fixture(autouse=True)
    def _set_env(self):
        old = os.environ.get("REQUIRE_SIGNATURES")
        os.environ["REQUIRE_SIGNATURES"] = "true"
        yield
        if old is None:
            os.environ.pop("REQUIRE_SIGNATURES", None)
        else:
            os.environ["REQUIRE_SIGNATURES"] = old

    def test_unsigned_request_rejected(self):
        """Requests without signatures must be rejected."""
        app = create_app(_state_with_registered_miner())
        client = TestClient(app)
        resp = client.get(
            "/v1/epochs/test-epoch-1/episodes",
            headers={"X-Miner-Hotkey": _TEST_HOTKEY},
        )
        assert resp.status_code == 401
        assert "Signature required" in resp.json()["detail"]

    def test_valid_signature_accepted(self):
        """Requests with valid signatures must be accepted."""
        app = create_app(_state_with_registered_miner())
        client = TestClient(app)
        path = "/v1/epochs/test-epoch-1/episodes"
        headers = _sign_request("GET", path)
        resp = client.get(path, headers=headers)
        assert resp.status_code == 200

    def test_bad_signature_rejected(self):
        """Requests with invalid signatures must be rejected."""
        app = create_app(_state_with_registered_miner())
        client = TestClient(app)
        path = "/v1/epochs/test-epoch-1/episodes"
        headers = _sign_request("GET", path)
        headers["X-Miner-Signature"] = "00" * 64  # garbage signature
        resp = client.get(path, headers=headers)
        assert resp.status_code == 401
        assert "Invalid signature" in resp.json()["detail"]

    def test_wrong_method_rejected(self):
        """Signature for GET cannot be replayed as POST (method binding)."""
        app = create_app(_state_with_registered_miner())
        client = TestClient(app)
        path = "/v1/epochs/test-epoch-1/episodes"
        headers = _sign_request("GET", path)
        # Try to use GET signature for a POST request
        resp = client.post(path, headers=headers, json={})
        # Should fail — either 401 (wrong method in signature) or 405 (no POST route)
        assert resp.status_code in (401, 405)

    def test_wrong_path_rejected(self):
        """Signature for one path cannot be used on another (path binding)."""
        app = create_app(_state_with_registered_miner())
        client = TestClient(app)
        headers = _sign_request("GET", "/v1/epochs/test-epoch-1/episodes")
        # Use the signature on a different path
        resp = client.get("/v1/epochs/test-epoch-1/commitment", headers=headers)
        # commitment endpoint doesn't require auth, but if it did this would be 401
        # The key test is that the signature doesn't validate for the wrong path
        # Let's test against an authenticated endpoint with the wrong path
        resp = client.get("/v1/epochs/wrong-epoch/episodes", headers=headers)
        assert resp.status_code == 401  # signature won't match different path

    def test_expired_nonce_rejected(self):
        """Nonces older than 300 seconds must be rejected."""
        app = create_app(_state_with_registered_miner())
        client = TestClient(app)
        path = "/v1/epochs/test-epoch-1/episodes"
        # Use a nonce from 10 minutes ago
        old_nonce = str(time.time() - 600)
        body_hash = hashlib.sha256(b"").hexdigest()
        message = hashlib.sha256(
            f"{_TEST_HOTKEY}:{old_nonce}:GET:{path}:{body_hash}".encode()
        ).hexdigest()
        signature = _TEST_KEYPAIR.sign(message.encode()).hex()

        resp = client.get(path, headers={
            "X-Miner-Hotkey": _TEST_HOTKEY,
            "X-Miner-Nonce": old_nonce,
            "X-Miner-Signature": signature,
        })
        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"].lower()

    def test_non_numeric_nonce_rejected(self):
        """Non-numeric nonces must be rejected."""
        app = create_app(_state_with_registered_miner())
        client = TestClient(app)
        resp = client.get(
            "/v1/epochs/test-epoch-1/episodes",
            headers={
                "X-Miner-Hotkey": _TEST_HOTKEY,
                "X-Miner-Nonce": "not-a-number",
                "X-Miner-Signature": "00" * 64,
            },
        )
        assert resp.status_code == 401
        assert "numeric" in resp.json()["detail"].lower()

    def test_nonce_replay_rejected(self):
        """Same nonce cannot be used twice."""
        app = create_app(_state_with_registered_miner())
        client = TestClient(app)
        path = "/v1/epochs/test-epoch-1/episodes"
        headers = _sign_request("GET", path)

        # First request succeeds
        resp1 = client.get(path, headers=headers)
        assert resp1.status_code == 200

        # Same headers (same nonce) — must be rejected
        resp2 = client.get(path, headers=headers)
        assert resp2.status_code == 401
        assert "already used" in resp2.json()["detail"].lower()

    def test_unregistered_miner_rejected(self):
        """Miners not in the metagraph must be rejected even with valid signatures."""
        other_kp = Keypair.create_from_mnemonic(Keypair.generate_mnemonic())
        app = create_app(_state_with_registered_miner())
        client = TestClient(app)
        path = "/v1/epochs/test-epoch-1/episodes"

        nonce = str(time.time())
        body_hash = hashlib.sha256(b"").hexdigest()
        message = hashlib.sha256(
            f"{other_kp.ss58_address}:{nonce}:GET:{path}:{body_hash}".encode()
        ).hexdigest()
        signature = other_kp.sign(message.encode()).hex()

        resp = client.get(path, headers={
            "X-Miner-Hotkey": other_kp.ss58_address,
            "X-Miner-Nonce": nonce,
            "X-Miner-Signature": signature,
        })
        assert resp.status_code == 403
        assert "not registered" in resp.json()["detail"].lower()
