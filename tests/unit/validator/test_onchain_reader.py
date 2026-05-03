"""End-to-end tests for hope/validator/onchain_reader.py."""

from __future__ import annotations

import hashlib
from typing import Optional

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.archives import ArchiveEndpoint, FetchAggregate, FetchResult
from hope.commitment.prediction_payload import (
    build_horizon_entry,
    build_prediction_plaintext,
    encrypt_prediction,
)
from hope.commitment.scoreability import ScoreabilityFailure, TimingBounds
from hope.validator.onchain_reader import (
    ChainCommits9B,
    assemble_chain_commits,
    read_miner_for_epoch,
)


class FakeArchiveClient:
    def __init__(self, response: FetchAggregate):
        self._response = response
        self.calls = []

    def fetch_first_match(self, endpoints, **kwargs):
        self.calls.append(kwargs)
        return self._response


def _make_prediction(epoch_id="EPOCH-A"):
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key().public_bytes_raw()
    horizons = [
        build_horizon_entry("7", (-5.0, 0.0, 5.0), (-2.0, 1.0, 4.0), (-3.0, 0.5, 4.5), 0.1, 0.05),
    ]
    plain = build_prediction_plaintext(
        epoch_id=epoch_id, miner_hotkey=pk, submitted_round=12345,
        horizons=horizons, miner_signing_key=sk,
    )
    return sk, pk, plain, encrypt_prediction(plain, epoch_id=epoch_id)


@pytest.fixture
def setup():
    sk, pk, plain, enc = _make_prediction()
    sha_ct = hashlib.sha256(enc.aes_ct).digest()
    chain = ChainCommits9B(
        timelock_k_revealed=enc.aes_key,
        sha256_ct_commit=sha_ct,
        self_archive_url="https://m/x",
        chain_block_at_k_commit=7038900,
        miner_hotkey=pk,
    )
    timing = TimingBounds(
        epoch_open_round=12000,
        miner_deadline_round=13000,
        chain_window_min_block=7038000,
        chain_window_max_block=7039000,
    )
    endpoints = [
        ArchiveEndpoint(tier=1, base_url="https://t1"),
        ArchiveEndpoint(tier=2, base_url="https://t2"),
    ]
    return {
        "pk": pk, "plain": plain, "enc": enc, "sha_ct": sha_ct,
        "chain": chain, "timing": timing, "endpoints": endpoints,
    }


class TestHappyPath:
    def test_full_read_succeeds(self, setup):
        winner = FetchResult(
            endpoint=setup["endpoints"][1], ok=True, aes_ct=setup["enc"].aes_ct,
            sha256_match=True, status_code=200, elapsed_ms=10,
        )
        agg = FetchAggregate(aes_ct=setup["enc"].aes_ct, winner=winner, attempts=[winner])
        client = FakeArchiveClient(agg)
        result = read_miner_for_epoch(
            chain_commits=setup["chain"],
            archive_client=client,
            archive_endpoints=setup["endpoints"],
            epoch_id="EPOCH-A",
            timing=setup["timing"],
            miner_uid=7,
            miner_identity_for_archive="5xyz",
        )
        assert result.ok
        assert result.plaintext == setup["plain"]
        assert result.excluded_reason is None
        assert result.fetch is not None
        assert result.scoreability.ok


class TestFailures:
    def test_no_archive_returns_plaintext_unavailable(self, setup):
        agg = FetchAggregate(aes_ct=None, winner=None, attempts=[
            FetchResult(endpoint=setup["endpoints"][0], ok=False, aes_ct=None,
                        sha256_match=None, status_code=502, error="x", elapsed_ms=1),
            FetchResult(endpoint=setup["endpoints"][1], ok=False, aes_ct=None,
                        sha256_match=None, status_code=503, error="y", elapsed_ms=1),
        ])
        client = FakeArchiveClient(agg)
        result = read_miner_for_epoch(
            chain_commits=setup["chain"],
            archive_client=client,
            archive_endpoints=setup["endpoints"],
            epoch_id="EPOCH-A",
            timing=setup["timing"],
            miner_uid=7,
            miner_identity_for_archive="5xyz",
        )
        assert not result.ok
        assert result.excluded_reason == "plaintext_unavailable"
        assert result.scoreability is None

    def test_missing_k_short_circuits_no_fetch(self, setup):
        client = FakeArchiveClient(FetchAggregate(aes_ct=None, winner=None, attempts=[]))
        chain = ChainCommits9B(
            timelock_k_revealed=None,
            sha256_ct_commit=setup["chain"].sha256_ct_commit,
            self_archive_url="https://m",
            chain_block_at_k_commit=7038900,
            miner_hotkey=setup["pk"],
        )
        result = read_miner_for_epoch(
            chain_commits=chain,
            archive_client=client,
            archive_endpoints=setup["endpoints"],
            epoch_id="EPOCH-A",
            timing=setup["timing"],
            miner_uid=7,
            miner_identity_for_archive="5xyz",
        )
        assert not result.ok
        assert result.excluded_reason == ScoreabilityFailure.ON_CHAIN_PRESENT_MISSING_K.value
        assert result.fetch is None
        assert client.calls == []

    def test_missing_sha_short_circuits_no_fetch(self, setup):
        client = FakeArchiveClient(FetchAggregate(aes_ct=None, winner=None, attempts=[]))
        chain = ChainCommits9B(
            timelock_k_revealed=setup["enc"].aes_key,
            sha256_ct_commit=None,
            self_archive_url="https://m",
            chain_block_at_k_commit=7038900,
            miner_hotkey=setup["pk"],
        )
        result = read_miner_for_epoch(
            chain_commits=chain,
            archive_client=client,
            archive_endpoints=setup["endpoints"],
            epoch_id="EPOCH-A",
            timing=setup["timing"],
            miner_uid=7,
            miner_identity_for_archive="5xyz",
        )
        assert not result.ok
        assert result.excluded_reason == ScoreabilityFailure.ON_CHAIN_PRESENT_MISSING_SHA.value
        assert client.calls == []


class TestAssembleChainCommits:
    def test_basic(self, setup):
        c = assemble_chain_commits(
            revealed_k_plaintext=setup["enc"].aes_key,
            sha256_ct_commit=setup["sha_ct"],
            self_archive_url="https://m",
            chain_block_at_k_commit=10,
            miner_hotkey=setup["pk"],
        )
        assert c.timelock_k_revealed == setup["enc"].aes_key
        assert c.sha256_ct_commit == setup["sha_ct"]

    def test_invalid_hotkey_raises(self, setup):
        with pytest.raises(ValueError, match="32 bytes"):
            assemble_chain_commits(
                revealed_k_plaintext=None,
                sha256_ct_commit=None,
                self_archive_url=None,
                chain_block_at_k_commit=None,
                miner_hotkey=b"\x00" * 31,
            )

    def test_wrong_k_length_treated_as_missing(self, setup):
        c = assemble_chain_commits(
            revealed_k_plaintext=b"\x00" * 16,  # too short
            sha256_ct_commit=setup["sha_ct"],
            self_archive_url="https://m",
            chain_block_at_k_commit=10,
            miner_hotkey=setup["pk"],
        )
        assert c.timelock_k_revealed is None

    def test_wrong_sha_length_treated_as_missing(self, setup):
        c = assemble_chain_commits(
            revealed_k_plaintext=setup["enc"].aes_key,
            sha256_ct_commit=b"\x00" * 31,
            self_archive_url="https://m",
            chain_block_at_k_commit=10,
            miner_hotkey=setup["pk"],
        )
        assert c.sha256_ct_commit is None
