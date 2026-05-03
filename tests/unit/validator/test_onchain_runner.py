"""End-to-end test for hope/validator/onchain_runner.py — full Layer 9.C orchestration."""

from __future__ import annotations

import hashlib
import os
from typing import Optional
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.archives import (
    ArchiveEndpoint,
    FetchAggregate,
    FetchResult,
)
from hope.commitment.canonical import canonical_cbor_loads
from hope.commitment.on_chain import CommitResult
from hope.commitment.prediction_payload import (
    build_horizon_entry,
    build_prediction_plaintext,
    encrypt_prediction,
)
from hope.commitment.scoreability import TimingBounds
from hope.validator.onchain_runner import (
    EpochScoringOutcome,
    MinerOnChainInputs,
    run_epoch_scoring,
)
from hope.validator.weights_commit import WeightsCommitResult


def _ok_commit(block: int, reveal_round: Optional[int] = None) -> CommitResult:
    return CommitResult(
        success=True, message="OK", block_number=block,
        extrinsic_hash="0x" + "ab" * 32, reveal_round=reveal_round,
    )


def _bad_commit(msg: str) -> CommitResult:
    return CommitResult(success=False, message=msg, block_number=None, extrinsic_hash=None, reveal_round=None)


def _make_miner_input(epoch_id: str, uid: int, block: int):
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key().public_bytes_raw()
    horizons = [
        build_horizon_entry("7", (-5.0, 0.0, 5.0), (-2.0, 1.0, 4.0), (-3.0, 0.5, 4.5), 0.1, 0.05),
    ]
    plain = build_prediction_plaintext(
        epoch_id=epoch_id, miner_hotkey=pk, submitted_round=12345,
        horizons=horizons, miner_signing_key=sk,
    )
    enc = encrypt_prediction(plain, epoch_id=epoch_id)
    sha_ct = hashlib.sha256(enc.aes_ct).digest()
    return MinerOnChainInputs(
        miner_uid=uid,
        miner_hotkey=pk,
        revealed_k=enc.aes_key,
        sha256_ct_commit=sha_ct,
        self_archive_url="https://m/x",
        chain_block_at_k_commit=block,
        k_reveal_round=12345700 + uid,
    ), enc.aes_ct


class _Archive:
    """In-memory archive — keyed by sha256."""
    def __init__(self):
        self._store = {}
    def store(self, sha, ct):
        self._store[sha] = ct
    def fetch_first_match(self, endpoints, *, epoch_id, miner_identity, expected_sha256):
        ct = self._store.get(expected_sha256)
        if ct is None:
            return FetchAggregate(aes_ct=None, winner=None, attempts=[
                FetchResult(endpoint=ep, ok=False, aes_ct=None, sha256_match=None,
                            status_code=502, error="x", elapsed_ms=1)
                for ep in endpoints
            ])
        winner = FetchResult(
            endpoint=endpoints[0], ok=True, aes_ct=ct, sha256_match=True,
            status_code=200, elapsed_ms=1,
        )
        return FetchAggregate(aes_ct=ct, winner=winner, attempts=[winner])


@pytest.fixture
def setup():
    val_sk = Ed25519PrivateKey.generate()
    val_pk = val_sk.public_key().public_bytes_raw()
    timing = TimingBounds(
        epoch_open_round=12000, miner_deadline_round=13000,
        chain_window_min_block=7038000, chain_window_max_block=7039000,
    )
    archive = _Archive()
    inputs = []
    for i in range(5):
        inp, ct = _make_miner_input("EPOCH-A", uid=i, block=7038900 + i)
        archive.store(inp.sha256_ct_commit, ct)
        inputs.append(inp)
    endpoints = [ArchiveEndpoint(tier=2, base_url="https://hope")]
    return {
        "val_sk": val_sk, "val_pk": val_pk, "timing": timing,
        "archive": archive, "inputs": inputs, "endpoints": endpoints,
    }


def _scorer_fixed(value):
    def fn(epoch_id, plaintext_per_miner):
        return {hk: value for hk in plaintext_per_miner}
    return fn


class TestRunEpochScoring:
    def test_happy_path(self, setup):
        with (
            patch(
                "hope.validator.onchain_runner.submit_pre_scoring_state_layer_9c1",
                return_value=_ok_commit(7038910, reveal_round=12345710),
            ),
            patch(
                "hope.validator.onchain_runner.commit_weights_layer_9c3",
                return_value=WeightsCommitResult(
                    success=True, message="OK",
                    block_number=7038920, block_hash=os.urandom(32),
                    extrinsic_hash="0x" + "cd" * 32,
                ),
            ),
            patch(
                "hope.validator.onchain_runner.submit_post_scoring_artifacts_layer_9c2",
                return_value=_ok_commit(7038930, reveal_round=12345730),
            ),
            patch(
                "hope.validator.onchain_runner.submit_retry_log_attestation_layer_9c6",
            ) as p_retry,
        ):
            out = run_epoch_scoring(
                subtensor=object(),
                validator_wallet=object(),
                netuid=21,
                epoch_id="EPOCH-A",
                epoch_idx=42,
                validator_hotkey=setup["val_pk"],
                validator_signing_key=setup["val_sk"],
                miner_inputs=setup["inputs"],
                archive_endpoints=setup["endpoints"],
                archive_client=setup["archive"],
                timing=setup["timing"],
                outcomes_release_round=12000,
                outcomes_fetched_at_round=12010,
                scoring_inputs_hash=os.urandom(32),
                scorer=_scorer_fixed(500_000),
                blocks_until_pre_scoring_reveal=300,
                blocks_until_post_scoring_reveal=600,
                blocks_until_weights_reveal=360,
            )
        assert out.ok
        assert len(out.miner_reads) == 5
        assert all(r.ok for r in out.miner_reads)
        assert len(out.score_map) == 5
        assert all(v == 500_000 for v in out.score_map.values())
        # No archive failures → no retry log
        p_retry.assert_not_called()
        assert out.retry_log_commit is None

    def test_archive_failure_emits_retry_log(self, setup):
        # Drop one ciphertext from the archive — that miner becomes plaintext_unavailable.
        first = setup["inputs"][0]
        setup["archive"]._store.pop(first.sha256_ct_commit)

        with (
            patch(
                "hope.validator.onchain_runner.submit_pre_scoring_state_layer_9c1",
                return_value=_ok_commit(7038910, reveal_round=12345710),
            ),
            patch(
                "hope.validator.onchain_runner.commit_weights_layer_9c3",
                return_value=WeightsCommitResult(
                    success=True, message="OK",
                    block_number=7038920, block_hash=os.urandom(32),
                    extrinsic_hash="0x" + "cd" * 32,
                ),
            ),
            patch(
                "hope.validator.onchain_runner.submit_post_scoring_artifacts_layer_9c2",
                return_value=_ok_commit(7038930, reveal_round=12345730),
            ),
            patch(
                "hope.validator.onchain_runner.submit_retry_log_attestation_layer_9c6",
                return_value=_ok_commit(7038940),
            ) as p_retry,
        ):
            out = run_epoch_scoring(
                subtensor=object(),
                validator_wallet=object(),
                netuid=21,
                epoch_id="EPOCH-A",
                epoch_idx=42,
                validator_hotkey=setup["val_pk"],
                validator_signing_key=setup["val_sk"],
                miner_inputs=setup["inputs"],
                archive_endpoints=setup["endpoints"],
                archive_client=setup["archive"],
                timing=setup["timing"],
                outcomes_release_round=12000,
                outcomes_fetched_at_round=12010,
                scoring_inputs_hash=os.urandom(32),
                scorer=_scorer_fixed(500_000),
                blocks_until_pre_scoring_reveal=300,
                blocks_until_post_scoring_reveal=600,
                blocks_until_weights_reveal=360,
            )
        assert out.ok
        # 4 scored, 1 excluded
        assert len(out.score_map) == 4
        # Retry log was committed
        assert p_retry.called
        assert out.retry_log_commit is not None
        assert out.retry_log_commit.success
        assert out.retry_log_blob is not None

    def test_pre_scoring_failure_aborts(self, setup):
        with (
            patch(
                "hope.validator.onchain_runner.submit_pre_scoring_state_layer_9c1",
                return_value=_bad_commit("rate-limited"),
            ),
            patch(
                "hope.validator.onchain_runner.commit_weights_layer_9c3"
            ) as p_w,
            patch(
                "hope.validator.onchain_runner.submit_post_scoring_artifacts_layer_9c2"
            ) as p_post,
        ):
            out = run_epoch_scoring(
                subtensor=object(),
                validator_wallet=object(),
                netuid=21,
                epoch_id="EPOCH-A",
                epoch_idx=42,
                validator_hotkey=setup["val_pk"],
                validator_signing_key=setup["val_sk"],
                miner_inputs=setup["inputs"],
                archive_endpoints=setup["endpoints"],
                archive_client=setup["archive"],
                timing=setup["timing"],
                outcomes_release_round=12000,
                outcomes_fetched_at_round=12010,
                scoring_inputs_hash=os.urandom(32),
                scorer=_scorer_fixed(500_000),
                blocks_until_pre_scoring_reveal=300,
                blocks_until_post_scoring_reveal=600,
                blocks_until_weights_reveal=360,
            )
        assert not out.ok
        assert "pre_scoring_commit_failed" in out.aborted_reason
        p_w.assert_not_called()
        p_post.assert_not_called()

    def test_weights_commit_failure_aborts(self, setup):
        with (
            patch(
                "hope.validator.onchain_runner.submit_pre_scoring_state_layer_9c1",
                return_value=_ok_commit(7038910, reveal_round=12345710),
            ),
            patch(
                "hope.validator.onchain_runner.commit_weights_layer_9c3",
                return_value=WeightsCommitResult(
                    success=False, message="rate-limited",
                    block_number=None, block_hash=None, extrinsic_hash=None,
                ),
            ),
            patch(
                "hope.validator.onchain_runner.submit_post_scoring_artifacts_layer_9c2"
            ) as p_post,
        ):
            out = run_epoch_scoring(
                subtensor=object(),
                validator_wallet=object(),
                netuid=21,
                epoch_id="EPOCH-A",
                epoch_idx=42,
                validator_hotkey=setup["val_pk"],
                validator_signing_key=setup["val_sk"],
                miner_inputs=setup["inputs"],
                archive_endpoints=setup["endpoints"],
                archive_client=setup["archive"],
                timing=setup["timing"],
                outcomes_release_round=12000,
                outcomes_fetched_at_round=12010,
                scoring_inputs_hash=os.urandom(32),
                scorer=_scorer_fixed(500_000),
                blocks_until_pre_scoring_reveal=300,
                blocks_until_post_scoring_reveal=600,
                blocks_until_weights_reveal=360,
            )
        assert not out.ok
        assert "weights_commit_failed" in out.aborted_reason
        p_post.assert_not_called()

    def test_no_scoreable_miners_aborts_at_weights(self, setup):
        # Drop EVERY ciphertext → every miner excluded → empty score_map → empty uids.
        setup["archive"]._store.clear()
        with (
            patch(
                "hope.validator.onchain_runner.submit_pre_scoring_state_layer_9c1",
                return_value=_ok_commit(7038910),
            ),
            patch(
                "hope.validator.onchain_runner.submit_retry_log_attestation_layer_9c6",
                return_value=_ok_commit(7038940),
            ),
        ):
            out = run_epoch_scoring(
                subtensor=object(),
                validator_wallet=object(),
                netuid=21,
                epoch_id="EPOCH-A",
                epoch_idx=42,
                validator_hotkey=setup["val_pk"],
                validator_signing_key=setup["val_sk"],
                miner_inputs=setup["inputs"],
                archive_endpoints=setup["endpoints"],
                archive_client=setup["archive"],
                timing=setup["timing"],
                outcomes_release_round=12000,
                outcomes_fetched_at_round=12010,
                scoring_inputs_hash=os.urandom(32),
                scorer=_scorer_fixed(500_000),
                blocks_until_pre_scoring_reveal=300,
                blocks_until_post_scoring_reveal=600,
                blocks_until_weights_reveal=360,
            )
        assert not out.ok
        assert "no scoreable miners" in (out.weights_commit.message if out.weights_commit else "")

    def test_pre_scoring_blob_canonical(self, setup):
        captured = {}
        def capture(**kwargs):
            captured["pre_blob"] = kwargs["pre_scoring_state_cbor"]
            return _ok_commit(7038910)

        with (
            patch(
                "hope.validator.onchain_runner.submit_pre_scoring_state_layer_9c1",
                side_effect=capture,
            ),
            patch(
                "hope.validator.onchain_runner.commit_weights_layer_9c3",
                return_value=WeightsCommitResult(
                    success=True, message="OK",
                    block_number=7038920, block_hash=os.urandom(32),
                    extrinsic_hash="0x" + "cd" * 32,
                ),
            ),
            patch(
                "hope.validator.onchain_runner.submit_post_scoring_artifacts_layer_9c2",
                return_value=_ok_commit(7038930),
            ),
        ):
            run_epoch_scoring(
                subtensor=object(),
                validator_wallet=object(),
                netuid=21,
                epoch_id="EPOCH-A",
                epoch_idx=42,
                validator_hotkey=setup["val_pk"],
                validator_signing_key=setup["val_sk"],
                miner_inputs=setup["inputs"],
                archive_endpoints=setup["endpoints"],
                archive_client=setup["archive"],
                timing=setup["timing"],
                outcomes_release_round=12000,
                outcomes_fetched_at_round=12010,
                scoring_inputs_hash=os.urandom(32),
                scorer=_scorer_fixed(500_000),
                blocks_until_pre_scoring_reveal=300,
                blocks_until_post_scoring_reveal=600,
                blocks_until_weights_reveal=360,
            )
        # The pre-scoring blob must decode to a CBOR map with our fields.
        decoded = canonical_cbor_loads(captured["pre_blob"])
        assert decoded["epoch_id"] == "EPOCH-A"
        assert decoded["n_miners"] == 5
        assert decoded["n_excluded"] == 0
        assert decoded["validator_hotkey"] == setup["val_pk"]
