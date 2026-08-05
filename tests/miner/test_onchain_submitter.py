"""End-to-end tests for hope/miner/onchain_submitter.py.

These tests stub the chain (Bittensor SDK) and the archive HTTPS layer with
in-process fakes so we can exercise the full Layer 9.B orchestration without
network or chain access.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.archives import ArchiveEndpoint, UploadResult
from hope.commitment.on_chain import CommitResult
from hope.commitment.prediction_payload import build_horizon_entry
from hope.miner.onchain_submitter import submit_miner_epoch

# -- fake collaborators ------------------------------------------------------


@dataclass
class FakeWallet:
    class _HK:
        ss58_address = "5Hoo2cRURm8A36WupNHyBkdby3wyBEpwj7MAgpC9sLnhxJNw"

    hotkey = _HK()


def _ok_commit(block: int, reveal_round: int | None = None) -> CommitResult:
    return CommitResult(
        success=True,
        message="OK",
        block_number=block,
        extrinsic_hash="0x" + "ab" * 32,
        reveal_round=reveal_round,
    )


def _bad_commit(msg: str) -> CommitResult:
    return CommitResult(
        success=False, message=msg, block_number=None, extrinsic_hash=None, reveal_round=None
    )


class FakeArchiveClient:
    def __init__(self, results_factory):
        self._factory = results_factory
        self.uploaded = []

    def upload_to_all(self, endpoints, **kwargs):
        results = self._factory(endpoints, **kwargs)
        self.uploaded.append((endpoints, kwargs, results))
        return results


# -- fixtures ---------------------------------------------------------------


@pytest.fixture
def miner_keys():
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key().public_bytes_raw()
    return sk, pk


@pytest.fixture
def horizons():
    return [
        build_horizon_entry("7", (-5.0, 0.0, 5.0), (-2.0, 1.0, 4.0), (-3.0, 0.5, 4.5), 0.1, 0.05),
        build_horizon_entry("14", (-7.0, -1.0, 7.0), (-3.0, 0.5, 5.0), (-4.0, 0.0, 5.0), 0.15, 0.08),
    ]


@pytest.fixture
def endpoints():
    return [
        ArchiveEndpoint(tier=1, base_url="https://val", name="val"),
        ArchiveEndpoint(tier=2, base_url="https://archive.example.io", name="archive2"),
        ArchiveEndpoint(tier=3, base_url="https://miner", name="miner-self"),
    ]


# -- tests ------------------------------------------------------------------


class TestHappyPath:
    def test_full_pipeline_success(self, miner_keys, horizons, endpoints):
        sk, pk = miner_keys

        def factory(endpoints, **_):
            return [
                UploadResult(endpoint=ep, ok=True, status_code=200)
                for ep in endpoints
            ]
        client = FakeArchiveClient(factory)

        with patch(
            "hope.miner.onchain_submitter.submit_miner_prediction_layer_9b",
            return_value=_ok_commit(7038901, reveal_round=12345710),
        ):
            result = submit_miner_epoch(
                subtensor=object(),
                miner_wallet=FakeWallet(),
                netuid=466,
                epoch_id="EPOCH-A",
                miner_hotkey=pk,
                miner_signing_key=sk,
                submitted_round=12345700,
                horizons=horizons,
                self_archive_url="https://miner.example/archive/EPOCH-A",
                archive_endpoints=endpoints,
                blocks_until_reveal=300,
                archive_client=client,
            )
        assert result.ok
        assert all(r.ok for r in result.archive_uploads)
        assert result.chain_bundle_commit.success
        assert result.failure_reason is None

    def test_aes_ct_sha_is_consistent_across_pipeline(self, miner_keys, horizons, endpoints):
        sk, pk = miner_keys
        captured: list[bytes] = []

        def fake_bundle_commit(*, sha256_ct, **kwargs):
            captured.append(sha256_ct)
            return _ok_commit(1, reveal_round=2)

        def factory(endpoints, **kwargs):
            return [UploadResult(endpoint=ep, ok=True, status_code=200) for ep in endpoints]
        client = FakeArchiveClient(factory)

        with patch(
            "hope.miner.onchain_submitter.submit_miner_prediction_layer_9b",
            side_effect=fake_bundle_commit,
        ):
            result = submit_miner_epoch(
                subtensor=object(),
                miner_wallet=FakeWallet(),
                netuid=466,
                epoch_id="EPOCH-A",
                miner_hotkey=pk,
                miner_signing_key=sk,
                submitted_round=12345700,
                horizons=horizons,
                self_archive_url="https://m/x",
                archive_endpoints=endpoints,
                blocks_until_reveal=300,
                archive_client=client,
            )
        assert result.ok
        # The SHA we bundled into the on-chain commit matches SHA(aes_ct).
        assert captured[0] == hashlib.sha256(result.encrypted.aes_ct).digest()


class TestFailures:
    def test_tier_2_required_default_aborts_chain(self, miner_keys, horizons, endpoints):
        sk, pk = miner_keys

        def factory(endpoints, **_):
            return [
                UploadResult(endpoint=ep, ok=(ep.tier != 2), status_code=200 if ep.tier != 2 else 502)
                for ep in endpoints
            ]
        client = FakeArchiveClient(factory)

        with (
            patch(
                "hope.miner.onchain_submitter.submit_miner_prediction_layer_9b"
            ) as p_k,
        ):
            result = submit_miner_epoch(
                subtensor=object(),
                miner_wallet=FakeWallet(),
                netuid=466,
                epoch_id="EPOCH-A",
                miner_hotkey=pk,
                miner_signing_key=sk,
                submitted_round=12345700,
                horizons=horizons,
                self_archive_url="https://m/x",
                archive_endpoints=endpoints,
                blocks_until_reveal=300,
                archive_client=client,
            )
        assert not result.ok
        assert "tier_2" in (result.failure_reason or "")
        p_k.assert_not_called()

    def test_chain_k_commit_failure_short_circuits(self, miner_keys, horizons, endpoints):
        """Bundled commit failure short-circuits the pipeline cleanly."""
        sk, pk = miner_keys

        def factory(endpoints, **_):
            return [UploadResult(endpoint=ep, ok=True, status_code=200) for ep in endpoints]
        client = FakeArchiveClient(factory)

        with patch(
            "hope.miner.onchain_submitter.submit_miner_prediction_layer_9b",
            return_value=_bad_commit("RateLimit"),
        ):
            result = submit_miner_epoch(
                subtensor=object(),
                miner_wallet=FakeWallet(),
                netuid=466,
                epoch_id="EPOCH-A",
                miner_hotkey=pk,
                miner_signing_key=sk,
                submitted_round=12345700,
                horizons=horizons,
                self_archive_url="https://m/x",
                archive_endpoints=endpoints,
                blocks_until_reveal=300,
                archive_client=client,
            )
        assert not result.ok
        assert "chain_bundle_commit_failed" in (result.failure_reason or "")
        assert result.chain_bundle_commit is not None
        assert not result.chain_bundle_commit.success
