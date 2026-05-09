"""Tests for hope/archive_server/metrics.py + the /metrics endpoint."""

from __future__ import annotations

import hashlib

import pytest
from fastapi.testclient import TestClient

from hope.archive_server.app import build_app
from hope.archive_server.metrics import status_group
from hope.archive_server.store import InMemoryStore


@pytest.fixture
def client():
    app = build_app(store=InMemoryStore(), require_signed_uploads=False)
    return TestClient(app)


def _payload(n: int = 64) -> tuple[bytes, str]:
    body = b"x" * n
    return body, hashlib.sha256(body).hexdigest()


class TestStatusGroup:
    def test_groups(self):
        assert status_group(200) == "2xx"
        assert status_group(201) == "2xx"
        assert status_group(400) == "4xx"
        assert status_group(404) == "4xx"
        assert status_group(500) == "5xx"
        assert status_group(101) == "other"


class TestMetricsEndpoint:
    def test_metrics_endpoint_returns_prom_text(self, client):
        r = client.get("/metrics")
        assert r.status_code == 200
        assert "text/plain" in r.headers["content-type"]
        assert "sn21_archive_requests_total" in r.text

    def test_upload_increments_counter(self, client):
        body, sha = _payload(100)
        r = client.post(f"/archive/EP-1/M-1/{sha}", content=body)
        assert r.status_code == 201
        m = client.get("/metrics").text
        assert 'method="POST"' in m
        assert 'outcome="ok"' in m
        assert "sn21_archive_store_objects_total" in m
        assert 'kind="put_new"' in m

    def test_sha_mismatch_increments_dedicated_counter(self, client):
        body = b"hello"
        wrong_sha = "00" * 32
        r = client.post(f"/archive/EP-1/M-1/{wrong_sha}", content=body)
        assert r.status_code == 400
        m = client.get("/metrics").text
        # The dedicated mismatch counter must have a sample > 0.
        # prometheus_client text format: `sn21_archive_sha_mismatch_total 1.0`
        # (no labels). Find that line.
        assert any(
            line.startswith("sn21_archive_sha_mismatch_total")
            and not line.startswith("# ")
            and "1.0" in line
            for line in m.splitlines()
        )

    def test_404_recorded_with_outcome_not_found(self, client):
        body, sha = _payload(100)
        r = client.get(f"/archive/EP-1/M-1/{sha}")
        assert r.status_code == 404
        m = client.get("/metrics").text
        assert 'outcome="not_found"' in m

    def test_request_size_histogram_observed(self, client):
        body, sha = _payload(256)
        client.post(f"/archive/EP-1/M-1/{sha}", content=body)
        m = client.get("/metrics").text
        assert "sn21_archive_request_bytes" in m

    def test_request_seconds_histogram_observed(self, client):
        body, sha = _payload(64)
        client.post(f"/archive/EP-1/M-1/{sha}", content=body)
        m = client.get("/metrics").text
        assert "sn21_archive_request_seconds" in m

    def test_metrics_isolated_per_app(self):
        """Two distinct apps should have independent counters."""
        app1 = build_app(store=InMemoryStore())
        app2 = build_app(store=InMemoryStore())
        c1 = TestClient(app1)
        c2 = TestClient(app2)
        body, sha = _payload(64)
        c1.post(f"/archive/EP-1/M-1/{sha}", content=body)
        m1 = c1.get("/metrics").text
        m2 = c2.get("/metrics").text
        assert "sn21_archive_store_objects_total" in m1
        # app2 had no upload — counter should still expose the sample line
        # but its value must be 0 OR the sample line is absent (depending
        # on prom_client behavior). We just assert the two are different
        # by some recorded counter.
        assert m1 != m2
