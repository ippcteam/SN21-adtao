"""Unit tests for hope/commitment/archives.py — three-tier archive client.

Uses an httpx MockTransport in-process so no real network I/O occurs.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from hope.commitment.archives import (
    ArchiveClient,
    ArchiveEndpoint,
    DEFAULT_MAX_AES_CT_BYTES,
)


@pytest.fixture
def aes_ct() -> bytes:
    return b"hello cipher payload " * 8  # arbitrary deterministic bytes


@pytest.fixture
def aes_ct_sha(aes_ct) -> bytes:
    return hashlib.sha256(aes_ct).digest()


@pytest.fixture
def endpoints():
    return [
        ArchiveEndpoint(tier=1, base_url="https://val.example", name="val-1"),
        ArchiveEndpoint(tier=2, base_url="https://hope.example", name="hope"),
        ArchiveEndpoint(tier=3, base_url="https://miner.example", name="miner"),
    ]


# ---------- ArchiveEndpoint validation ----------


class TestArchiveEndpointValidation:
    def test_invalid_tier(self):
        with pytest.raises(ValueError, match="tier must be"):
            ArchiveEndpoint(tier=4, base_url="https://x")

    def test_empty_base_url(self):
        with pytest.raises(ValueError, match="base_url"):
            ArchiveEndpoint(tier=1, base_url="")

    def test_trailing_slash_rejected(self):
        with pytest.raises(ValueError, match="base_url"):
            ArchiveEndpoint(tier=1, base_url="https://x/")


# ---------- Upload ----------


class TestUpload:
    def _make_client_with_handler(self, handler):
        """Patch ArchiveClient.upload to use an httpx MockTransport."""
        import hope.commitment.archives as mod

        original_client = httpx.Client

        def patched(*args, **kwargs):
            # Drop transport from kwargs if pre-set, force ours.
            kwargs.pop("transport", None)
            return original_client(*args, transport=httpx.MockTransport(handler), **kwargs)

        mod.httpx.Client = patched  # type: ignore[attr-defined]
        return original_client

    def _restore(self, original):
        import hope.commitment.archives as mod
        mod.httpx.Client = original  # type: ignore[attr-defined]

    def test_upload_success(self, aes_ct, endpoints):
        seen_paths = []

        def handler(request):
            seen_paths.append(str(request.url.path))
            assert request.method == "POST"
            return httpx.Response(200)

        original = self._make_client_with_handler(handler)
        try:
            client = ArchiveClient()
            res = client.upload(
                endpoints[1],
                epoch_id="EP1",
                miner_identity="5MinerSS58...",
                aes_ct=aes_ct,
            )
            assert res.ok
            assert res.status_code == 200
            assert seen_paths == [
                f"/archive/EP1/5MinerSS58.../{hashlib.sha256(aes_ct).hexdigest()}"
            ]
        finally:
            self._restore(original)

    def test_upload_http_500(self, aes_ct, endpoints):
        def handler(request):
            return httpx.Response(500, text="server error")

        original = self._make_client_with_handler(handler)
        try:
            client = ArchiveClient()
            res = client.upload(
                endpoints[1],
                epoch_id="EP1",
                miner_identity="X",
                aes_ct=aes_ct,
            )
            assert not res.ok
            assert res.status_code == 500
            assert "500" in (res.error or "")
        finally:
            self._restore(original)

    def test_upload_too_large(self, endpoints):
        client = ArchiveClient(max_aes_ct_bytes=10)
        res = client.upload(
            endpoints[1],
            epoch_id="EP1",
            miner_identity="X",
            aes_ct=b"X" * 100,
        )
        assert not res.ok
        assert res.status_code is None
        assert "too large" in (res.error or "")

    def test_upload_to_all(self, aes_ct, endpoints):
        seen = []

        def handler(request):
            seen.append(request.url.host)
            return httpx.Response(200)

        original = self._make_client_with_handler(handler)
        try:
            client = ArchiveClient()
            results = client.upload_to_all(
                endpoints,
                epoch_id="EP1",
                miner_identity="X",
                aes_ct=aes_ct,
            )
            assert all(r.ok for r in results)
            assert len(results) == 3
            assert seen == ["val.example", "hope.example", "miner.example"]
        finally:
            self._restore(original)

    def test_upload_auth_headers_forwarded(self, aes_ct, endpoints):
        seen_headers = {}

        def handler(request):
            for k, v in request.headers.items():
                seen_headers[k.lower()] = v
            return httpx.Response(200)

        original = self._make_client_with_handler(handler)
        try:
            client = ArchiveClient()
            client.upload(
                endpoints[1],
                epoch_id="EP1",
                miner_identity="X",
                aes_ct=aes_ct,
                auth_headers={"X-Miner-Hotkey": "5xyz", "X-Miner-Sig": "deadbeef"},
            )
            assert seen_headers.get("x-miner-hotkey") == "5xyz"
            assert seen_headers.get("x-miner-sig") == "deadbeef"
        finally:
            self._restore(original)


# ---------- Fetch ----------


class TestFetch:
    def _patch_client(self, handler):
        import hope.commitment.archives as mod
        original = httpx.Client

        def patched(*args, **kwargs):
            kwargs.pop("transport", None)
            return original(*args, transport=httpx.MockTransport(handler), **kwargs)

        mod.httpx.Client = patched  # type: ignore[attr-defined]
        return original

    def _restore(self, original):
        import hope.commitment.archives as mod
        mod.httpx.Client = original  # type: ignore[attr-defined]

    def test_fetch_match(self, aes_ct, aes_ct_sha, endpoints):
        def handler(request):
            return httpx.Response(200, content=aes_ct)

        original = self._patch_client(handler)
        try:
            client = ArchiveClient()
            res = client.fetch(
                endpoints[1],
                epoch_id="EP1",
                miner_identity="X",
                expected_sha256=aes_ct_sha,
            )
            assert res.ok
            assert res.aes_ct == aes_ct
            assert res.sha256_match is True
            assert res.status_code == 200
        finally:
            self._restore(original)

    def test_fetch_sha_mismatch(self, aes_ct, aes_ct_sha, endpoints):
        def handler(request):
            return httpx.Response(200, content=b"DIFFERENT BYTES")

        original = self._patch_client(handler)
        try:
            client = ArchiveClient()
            res = client.fetch(
                endpoints[1],
                epoch_id="EP1",
                miner_identity="X",
                expected_sha256=aes_ct_sha,
            )
            assert not res.ok
            assert res.aes_ct is None
            assert res.sha256_match is False
            assert "sha256 mismatch" in (res.error or "")
        finally:
            self._restore(original)

    def test_fetch_404(self, aes_ct_sha, endpoints):
        def handler(request):
            return httpx.Response(404)

        original = self._patch_client(handler)
        try:
            client = ArchiveClient()
            res = client.fetch(
                endpoints[1],
                epoch_id="EP1",
                miner_identity="X",
                expected_sha256=aes_ct_sha,
            )
            assert not res.ok
            assert res.status_code == 404
        finally:
            self._restore(original)

    def test_fetch_body_too_large(self, aes_ct_sha, endpoints):
        large = b"X" * 5000

        def handler(request):
            return httpx.Response(200, content=large)

        original = self._patch_client(handler)
        try:
            client = ArchiveClient(max_aes_ct_bytes=1024)
            res = client.fetch(
                endpoints[1],
                epoch_id="EP1",
                miner_identity="X",
                expected_sha256=aes_ct_sha,
            )
            assert not res.ok
            assert "too large" in (res.error or "")
        finally:
            self._restore(original)

    def test_fetch_first_match_winners_tier_1(self, aes_ct, aes_ct_sha, endpoints):
        """First tier returns matching bytes — Tier-2 / Tier-3 not consulted."""

        call_count = {"n": 0}

        def handler(request):
            call_count["n"] += 1
            return httpx.Response(200, content=aes_ct)

        original = self._patch_client(handler)
        try:
            client = ArchiveClient()
            agg = client.fetch_first_match(
                endpoints,
                epoch_id="EP1",
                miner_identity="X",
                expected_sha256=aes_ct_sha,
            )
            assert agg.ok
            assert agg.aes_ct == aes_ct
            assert agg.winner is not None
            assert agg.winner.endpoint.tier == 1
            assert call_count["n"] == 1
        finally:
            self._restore(original)

    def test_fetch_first_match_falls_back_to_tier_2(self, aes_ct, aes_ct_sha, endpoints):
        def handler(request):
            if request.url.host == "val.example":
                return httpx.Response(502)
            return httpx.Response(200, content=aes_ct)

        original = self._patch_client(handler)
        try:
            client = ArchiveClient()
            agg = client.fetch_first_match(
                endpoints,
                epoch_id="EP1",
                miner_identity="X",
                expected_sha256=aes_ct_sha,
            )
            assert agg.ok
            assert agg.winner.endpoint.tier == 2
            assert len(agg.attempts) == 2  # tier 1 failed, tier 2 succeeded
        finally:
            self._restore(original)

    def test_fetch_first_match_all_fail(self, aes_ct_sha, endpoints):
        def handler(request):
            return httpx.Response(503)

        original = self._patch_client(handler)
        try:
            client = ArchiveClient()
            agg = client.fetch_first_match(
                endpoints,
                epoch_id="EP1",
                miner_identity="X",
                expected_sha256=aes_ct_sha,
            )
            assert not agg.ok
            assert agg.aes_ct is None
            assert len(agg.attempts) == 3
            for a in agg.attempts:
                assert a.status_code == 503
        finally:
            self._restore(original)

    def test_fetch_first_match_skips_tier_returning_wrong_bytes(self, aes_ct, aes_ct_sha, endpoints):
        """Tier-1 lies (wrong bytes), Tier-2 has truth — verifier picks Tier-2."""

        def handler(request):
            if request.url.host == "val.example":
                return httpx.Response(200, content=b"LIES")
            return httpx.Response(200, content=aes_ct)

        original = self._patch_client(handler)
        try:
            client = ArchiveClient()
            agg = client.fetch_first_match(
                endpoints,
                epoch_id="EP1",
                miner_identity="X",
                expected_sha256=aes_ct_sha,
            )
            assert agg.ok
            assert agg.winner.endpoint.tier == 2
            tier1_attempt = next(a for a in agg.attempts if a.endpoint.tier == 1)
            assert tier1_attempt.sha256_match is False
        finally:
            self._restore(original)

    def test_fetch_invalid_expected_sha_length(self, endpoints):
        client = ArchiveClient()
        with pytest.raises(ValueError, match="32 bytes"):
            client.fetch(
                endpoints[1],
                epoch_id="EP1",
                miner_identity="X",
                expected_sha256=b"\x00" * 31,
            )
