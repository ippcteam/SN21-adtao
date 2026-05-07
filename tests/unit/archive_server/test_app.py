"""Unit tests for hope/archive_server/app.py."""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from hope.archive_server.app import _expected_signed_message, build_app
from hope.archive_server.store import InMemoryStore


@pytest.fixture
def client_unauthed():
    app = build_app(store=InMemoryStore(), require_signed_uploads=False)
    return TestClient(app)


@pytest.fixture
def client_authed():
    app = build_app(store=InMemoryStore(), require_signed_uploads=True)
    return TestClient(app)


@pytest.fixture
def payload():
    return b"hello cipher payload " * 8


@pytest.fixture
def digest(payload):
    return hashlib.sha256(payload).digest()


@pytest.fixture
def digest_hex(digest):
    return digest.hex()


# ---------- healthz ----------


def test_healthz(client_unauthed):
    r = client_unauthed.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ---------- POST (unauth) ----------


class TestUploadUnauthed:
    def test_round_trip(self, client_unauthed, payload, digest_hex):
        r = client_unauthed.post(
            f"/archive/EP1/M1/{digest_hex}",
            content=payload,
            headers={"Content-Type": "application/octet-stream"},
        )
        assert r.status_code == 201

        r2 = client_unauthed.get(f"/archive/EP1/M1/{digest_hex}")
        assert r2.status_code == 200
        assert r2.content == payload

    def test_repeat_upload_returns_200(self, client_unauthed, payload, digest_hex):
        r = client_unauthed.post(
            f"/archive/EP1/M1/{digest_hex}",
            content=payload,
        )
        assert r.status_code == 201
        r2 = client_unauthed.post(
            f"/archive/EP1/M1/{digest_hex}",
            content=payload,
        )
        assert r2.status_code == 200

    def test_body_digest_mismatch_400(self, client_unauthed, payload):
        wrong_hex = "00" * 32
        r = client_unauthed.post(
            f"/archive/EP1/M1/{wrong_hex}",
            content=payload,
        )
        assert r.status_code == 400
        assert "sha256" in r.text.lower()

    def test_body_too_large_413(self, payload):
        # Build a small-cap app for this case.
        app = build_app(store=InMemoryStore(), max_body_bytes=10)
        client = TestClient(app)
        wrong_hex = hashlib.sha256(b"X" * 100).hexdigest()
        r = client.post(f"/archive/EP1/M1/{wrong_hex}", content=b"X" * 100)
        assert r.status_code == 413

    def test_invalid_sha_hex_400(self, client_unauthed, payload):
        # Length OK but invalid hex character
        bad = "z" * 64
        r = client_unauthed.post(f"/archive/EP1/M1/{bad}", content=payload)
        assert r.status_code == 400

    def test_short_sha_hex_400(self, client_unauthed, payload):
        r = client_unauthed.post("/archive/EP1/M1/abc123", content=payload)
        assert r.status_code == 400

    def test_path_traversal_rejected(self, client_unauthed, payload, digest_hex):
        # FastAPI normalises double-slash paths so we test by passing dots.
        r = client_unauthed.get(f"/archive/..somethingsomething/M1/{digest_hex}")
        # Either 400 or a 404 from the empty store; both prevent traversal.
        assert r.status_code in (400, 404)


# ---------- GET ----------


class TestFetch:
    def test_404_when_missing(self, client_unauthed, digest_hex):
        r = client_unauthed.get(f"/archive/EP1/M1/{digest_hex}")
        assert r.status_code == 404

    def test_invalid_sha_hex_400(self, client_unauthed):
        r = client_unauthed.get("/archive/EP1/M1/" + "z" * 64)
        assert r.status_code == 400


# ---------- POST (auth-enforced) ----------


class TestUploadAuthed:
    def test_missing_headers_401(self, client_authed, payload, digest_hex):
        r = client_authed.post(
            f"/archive/EP1/SS58X/{digest_hex}",
            content=payload,
        )
        assert r.status_code == 401

    def test_bad_signature_401(self, client_authed, payload, digest_hex):
        r = client_authed.post(
            f"/archive/EP1/SS58X/{digest_hex}",
            content=payload,
            headers={
                "X-Miner-Hotkey": "5Hoo2cRURm8A36WupNHyBkdby3wyBEpwj7MAgpC9sLnhxJNw",
                "X-Miner-Signature": "00" * 64,
            },
        )
        assert r.status_code == 401

    def test_path_identity_must_match_hotkey(self, client_authed, payload, digest_hex):
        # Even with a "valid-looking" sig, the path/header mismatch must 403.
        # Using a placeholder sig — verification fails 401 before 403, but
        # this test pins behaviour for the wiring once a real signature is
        # supplied. For the path-mismatch contract specifically, the order
        # is: format → digest → auth → identity-match. We can only assert
        # 401 here, but document the behaviour.
        r = client_authed.post(
            f"/archive/EP1/SS58Y/{digest_hex}",
            content=payload,
            headers={
                "X-Miner-Hotkey": "5Hoo2cRURm8A36WupNHyBkdby3wyBEpwj7MAgpC9sLnhxJNw",
                "X-Miner-Signature": "ab" * 64,
            },
        )
        # Will be 401 (sig fails before identity-match check); the
        # important guarantee is "not 201", which holds.
        assert r.status_code == 401


def test_expected_signed_message_format():
    msg = _expected_signed_message("EPOCH-A", "ab" * 32)
    assert msg.startswith(b"sn21-archive-v1:EPOCH-A:")
    assert msg.endswith(b"abababababababababababababababababababababababababababababababab")
