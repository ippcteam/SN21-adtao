"""Unit tests for hope/validator/migration.py."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.archive_server.store import InMemoryStore
from hope.commitment.archives import ArchiveEndpoint, FetchAggregate, FetchResult
from hope.commitment.on_chain import CommitResult
from hope.commitment.scoreability import TimingBounds
from hope.protocol.prediction import (
    HorizonPrediction,
    Prediction,
    QuantilePrediction,
)
from hope.scoring.onchain_adapter import HorizonTruth, make_scorer
from hope.validator.migration import (
    _deterministic_aes_key,
    replay_http_predictions_as_chain_inputs,
    stash_into_archive,
)
from hope.validator.weights_commit import WeightsCommitResult


def _pred(ep_id: str, miner: str = "m") -> Prediction:
    return Prediction(
        episode_id=ep_id,
        miner_id=miner,
        submitted_at=datetime.utcnow(),
        horizons={
            "7": HorizonPrediction(
                cost_delta_pct=QuantilePrediction(p10=-3.0, p50=0.0, p90=3.0),
                conversions_delta_pct=QuantilePrediction(p10=-1.0, p50=1.0, p90=3.0),
                efficiency_delta_pct=QuantilePrediction(p10=-1.0, p50=0.5, p90=2.5),
                goal_miss_probability=0.10,
                instability_risk=0.05,
            ),
        },
    )


@pytest.fixture
def four_miners():
    miners = []
    for _ in range(4):
        sk = Ed25519PrivateKey.generate()
        pk = sk.public_key().public_bytes_raw()
        miners.append((pk, sk))
    return miners


class TestDeterministicKey:
    def test_same_inputs_same_key(self):
        a = _deterministic_aes_key("EP-1", b"\x42" * 32)
        b = _deterministic_aes_key("EP-1", b"\x42" * 32)
        assert a == b
        assert len(a) == 32

    def test_different_epoch_different_key(self):
        a = _deterministic_aes_key("EP-1", b"\x42" * 32)
        b = _deterministic_aes_key("EP-2", b"\x42" * 32)
        assert a != b

    def test_different_miner_different_key(self):
        a = _deterministic_aes_key("EP-1", b"\x42" * 32)
        b = _deterministic_aes_key("EP-1", b"\x99" * 32)
        assert a != b


class TestReplayHttpPredictions:
    def test_basic(self, four_miners):
        http_preds = {
            pk: [_pred(f"EP-{i}", miner=pk.hex()[:8]) for i in range(3)]
            for pk, _ in four_miners
        }
        keys = {pk: sk for pk, sk in four_miners}
        inputs, ct_map = replay_http_predictions_as_chain_inputs(
            epoch_id="EP-A",
            http_predictions=http_preds,
            miner_signing_keys=keys,
        )
        assert len(inputs) == 4
        assert len(ct_map) == 4
        for inp in inputs:
            assert inp.revealed_k is not None
            assert inp.sha256_ct_commit is not None
            assert inp.sha256_ct_commit in ct_map

    def test_deterministic(self, four_miners):
        http_preds = {
            pk: [_pred(f"EP-{i}", miner=pk.hex()[:8]) for i in range(3)]
            for pk, _ in four_miners
        }
        keys = {pk: sk for pk, sk in four_miners}
        inputs1, ct1 = replay_http_predictions_as_chain_inputs(
            epoch_id="EP-A", http_predictions=http_preds, miner_signing_keys=keys,
        )
        inputs2, ct2 = replay_http_predictions_as_chain_inputs(
            epoch_id="EP-A", http_predictions=http_preds, miner_signing_keys=keys,
        )
        assert {i.sha256_ct_commit for i in inputs1} == {i.sha256_ct_commit for i in inputs2}
        assert ct1 == ct2

    def test_skip_zero_predictions(self, four_miners):
        http_preds = {pk: [] for pk, _ in four_miners[:2]}
        http_preds.update({pk: [_pred("EP-0", miner=pk.hex()[:8])]
                           for pk, _ in four_miners[2:]})
        keys = {pk: sk for pk, sk in four_miners}
        inputs, _ = replay_http_predictions_as_chain_inputs(
            epoch_id="EP-A", http_predictions=http_preds, miner_signing_keys=keys,
        )
        assert len(inputs) == 2

    def test_missing_signing_key_raises(self, four_miners):
        http_preds = {
            pk: [_pred("EP-0", miner=pk.hex()[:8])] for pk, _ in four_miners
        }
        # Drop one miner's signing key
        keys = {pk: sk for (pk, sk) in four_miners[:-1]}
        with pytest.raises(ValueError, match="missing signing key"):
            replay_http_predictions_as_chain_inputs(
                epoch_id="EP-A", http_predictions=http_preds, miner_signing_keys=keys,
            )


class TestStashIntoArchive:
    def test_round_trip(self, four_miners):
        http_preds = {
            pk: [_pred("EP-0", miner=pk.hex()[:8])] for pk, _ in four_miners
        }
        keys = {pk: sk for pk, sk in four_miners}
        inputs, ct_map = replay_http_predictions_as_chain_inputs(
            epoch_id="EP-A", http_predictions=http_preds, miner_signing_keys=keys,
        )
        from hope.validator.onchain_runner import _pubkey_bytes_to_ss58
        store = InMemoryStore()
        stash_into_archive(store, epoch_id="EP-A",
                           ct_by_sha256=ct_map, miner_inputs=inputs)
        for inp in inputs:
            got = store.get(
                epoch_id="EP-A",
                miner_identity=_pubkey_bytes_to_ss58(inp.miner_hotkey),
                sha256=inp.sha256_ct_commit,
            )
            assert got is not None
            assert hashlib.sha256(got).digest() == inp.sha256_ct_commit

    def test_rejects_non_store_target(self, four_miners):
        http_preds = {pk: [_pred("EP-0", miner=pk.hex()[:8])] for pk, _ in four_miners}
        keys = {pk: sk for pk, sk in four_miners}
        inputs, ct_map = replay_http_predictions_as_chain_inputs(
            epoch_id="EP-A", http_predictions=http_preds, miner_signing_keys=keys,
        )

        class NotAStore:
            def upload(self, *args, **kwargs):
                pass

        with pytest.raises(TypeError, match="ArchiveStore"):
            stash_into_archive(NotAStore(), epoch_id="EP-A",
                               ct_by_sha256=ct_map, miner_inputs=inputs)


class TestEndToEndDualMode:
    """Build the same scoring outcome via HTTP→migration→chain pipeline,
    proving the migration tool is sound enough to use in dual-mode rehearsal.
    """

    def test_scoring_runs_against_synthesized_inputs(self, four_miners):
        from hope.validator.onchain_runner import run_epoch_scoring

        http_preds = {
            pk: [_pred(f"EP-{j}", miner=pk.hex()[:8]) for j in range(3)]
            for pk, _ in four_miners
        }
        keys = {pk: sk for pk, sk in four_miners}
        inputs, ct_map = replay_http_predictions_as_chain_inputs(
            epoch_id="EP-A", http_predictions=http_preds, miner_signing_keys=keys,
        )
        store = InMemoryStore()
        stash_into_archive(store, epoch_id="EP-A",
                           ct_by_sha256=ct_map, miner_inputs=inputs)

        # Wrap the InMemoryStore as an archive client substitute that
        # supports `fetch_first_match`. We build a thin wrapper here so
        # run_epoch_scoring can use the migration store directly.
        class _StoreAdapter:
            def __init__(self, st):
                self.st = st

            def fetch_first_match(self, endpoints, *, epoch_id, miner_identity,
                                  expected_sha256):
                got = self.st.get(epoch_id=epoch_id, miner_identity=miner_identity,
                                  sha256=expected_sha256)
                if got is None:
                    return FetchAggregate(aes_ct=None, winner=None, attempts=[])
                winner = FetchResult(
                    endpoint=endpoints[0], ok=True, aes_ct=got, sha256_match=True,
                    status_code=200, elapsed_ms=1,
                )
                return FetchAggregate(aes_ct=got, winner=winner, attempts=[winner])

        adapter = _StoreAdapter(store)

        truth = {"7": HorizonTruth(
            horizon="7",
            truth_cost_p50_dpct=0, truth_conv_p50_dpct=10, truth_eff_p50_dpct=5,
            goal_miss_freq_ppm=100_000, instab_freq_ppm=50_000,
        )}
        scorer = make_scorer(truth)

        val_sk = Ed25519PrivateKey.generate()
        val_pk = val_sk.public_key().public_bytes_raw()

        with (
            patch(
                "hope.validator.onchain_runner.submit_pre_scoring_state_layer_9c1",
                return_value=CommitResult(success=True, message="OK", block_number=1,
                                          extrinsic_hash=None, reveal_round=2),
            ),
            patch(
                "hope.validator.onchain_runner.commit_weights_layer_9c3",
                return_value=WeightsCommitResult(
                    success=True, message="OK", block_number=10,
                    block_hash=os.urandom(32), extrinsic_hash=None,
                ),
            ),
            patch(
                "hope.validator.onchain_runner.submit_post_scoring_artifacts_layer_9c2",
                return_value=CommitResult(success=True, message="OK", block_number=20,
                                          extrinsic_hash=None, reveal_round=21),
            ),
        ):
            outcome = run_epoch_scoring(
                subtensor=object(), validator_wallet=object(), netuid=21,
                epoch_id="EP-A", epoch_idx=42,
                validator_hotkey=val_pk, validator_signing_key=val_sk,
                miner_inputs=inputs, archive_endpoints=[
                    ArchiveEndpoint(tier=2, base_url="https://hope")
                ],
                archive_client=adapter,
                timing=TimingBounds(
                    epoch_open_round=12345600, miner_deadline_round=12346000,
                    chain_window_min_block=0, chain_window_max_block=2**31,
                ),
                outcomes_release_round=12345600,
                outcomes_fetched_at_round=12345700,
                scoring_inputs_hash=os.urandom(32),
                scorer=scorer,
                blocks_until_pre_scoring_reveal=300,
                blocks_until_post_scoring_reveal=600,
                blocks_until_weights_reveal=360,
            )
        assert outcome.ok
        assert len(outcome.score_map) == 4
        # Every miner predicted within the truth ±5dpct band → high scores
        assert all(v >= 800_000 for v in outcome.score_map.values())

    def test_report_only_scores_without_any_chain_commit(self, four_miners):
        """report_only=True scores + populates the outcome but makes ZERO chain
        commits — immune to the weights rate limit + Commitments space budget."""
        from hope.validator.onchain_runner import run_epoch_scoring

        http_preds = {
            pk: [_pred(f"EP-{j}", miner=pk.hex()[:8]) for j in range(3)]
            for pk, _ in four_miners
        }
        keys = {pk: sk for pk, sk in four_miners}
        inputs, ct_map = replay_http_predictions_as_chain_inputs(
            epoch_id="EP-A", http_predictions=http_preds, miner_signing_keys=keys,
        )
        store = InMemoryStore()
        stash_into_archive(store, epoch_id="EP-A",
                           ct_by_sha256=ct_map, miner_inputs=inputs)

        class _StoreAdapter:
            def __init__(self, st):
                self.st = st

            def fetch_first_match(self, endpoints, *, epoch_id, miner_identity,
                                  expected_sha256):
                got = self.st.get(epoch_id=epoch_id, miner_identity=miner_identity,
                                  sha256=expected_sha256)
                if got is None:
                    return FetchAggregate(aes_ct=None, winner=None, attempts=[])
                winner = FetchResult(endpoint=endpoints[0], ok=True, aes_ct=got,
                                     sha256_match=True, status_code=200, elapsed_ms=1)
                return FetchAggregate(aes_ct=got, winner=winner, attempts=[winner])

        truth = {"7": HorizonTruth(
            horizon="7", truth_cost_p50_dpct=0, truth_conv_p50_dpct=10,
            truth_eff_p50_dpct=5, goal_miss_freq_ppm=100_000, instab_freq_ppm=50_000,
        )}
        scorer = make_scorer(truth)
        val_sk = Ed25519PrivateKey.generate()
        val_pk = val_sk.public_key().public_bytes_raw()

        _boom = AssertionError("report_only must not commit to chain")
        with (
            patch("hope.validator.onchain_runner.submit_pre_scoring_state_layer_9c1",
                  side_effect=_boom),
            patch("hope.validator.onchain_runner.commit_weights_layer_9c3",
                  side_effect=_boom),
            patch("hope.validator.onchain_runner.submit_post_scoring_artifacts_layer_9c2",
                  side_effect=_boom),
        ):
            outcome = run_epoch_scoring(
                subtensor=object(), validator_wallet=object(), netuid=21,
                epoch_id="EP-A", epoch_idx=42,
                validator_hotkey=val_pk, validator_signing_key=val_sk,
                miner_inputs=inputs,
                archive_endpoints=[ArchiveEndpoint(tier=2, base_url="https://hope")],
                archive_client=_StoreAdapter(store),
                timing=TimingBounds(
                    epoch_open_round=12345600, miner_deadline_round=12346000,
                    chain_window_min_block=0, chain_window_max_block=2**31,
                ),
                outcomes_release_round=12345600,
                outcomes_fetched_at_round=12345700,
                scoring_inputs_hash=os.urandom(32),
                scorer=scorer,
                blocks_until_pre_scoring_reveal=300,
                blocks_until_post_scoring_reveal=600,
                blocks_until_weights_reveal=360,
                report_only=True,
            )
        # Scored fully, flagged report-only, and NO commit fired (else AssertionError).
        assert outcome.report_only is True
        assert outcome.ok is True
        assert len(outcome.score_map) == 4
        assert outcome.pre_scoring_commit is None
        assert outcome.weights_commit is None
        assert outcome.post_scoring_commit is None

    def test_partial_burn_split(self, four_miners, monkeypatch):
        """SN21_BURN_FRACTION=0.75 -> 75% to the override UID, 25% across
        miners by score (the committed vector, not a single-UID full burn)."""
        from hope.validator.onchain_runner import run_epoch_scoring

        http_preds = {
            pk: [_pred(f"EP-{j}", miner=pk.hex()[:8]) for j in range(3)]
            for pk, _ in four_miners
        }
        keys = {pk: sk for pk, sk in four_miners}
        inputs, ct_map = replay_http_predictions_as_chain_inputs(
            epoch_id="EP-A", http_predictions=http_preds, miner_signing_keys=keys,
        )
        store = InMemoryStore()
        stash_into_archive(store, epoch_id="EP-A",
                           ct_by_sha256=ct_map, miner_inputs=inputs)

        class _StoreAdapter:
            def __init__(self, st):
                self.st = st

            def fetch_first_match(self, endpoints, *, epoch_id, miner_identity,
                                  expected_sha256):
                got = self.st.get(epoch_id=epoch_id, miner_identity=miner_identity,
                                  sha256=expected_sha256)
                if got is None:
                    return FetchAggregate(aes_ct=None, winner=None, attempts=[])
                winner = FetchResult(endpoint=endpoints[0], ok=True, aes_ct=got,
                                     sha256_match=True, status_code=200, elapsed_ms=1)
                return FetchAggregate(aes_ct=got, winner=winner, attempts=[winner])

        adapter = _StoreAdapter(store)
        truth = {"7": HorizonTruth(
            horizon="7", truth_cost_p50_dpct=0, truth_conv_p50_dpct=10,
            truth_eff_p50_dpct=5, goal_miss_freq_ppm=100_000, instab_freq_ppm=50_000,
        )}
        scorer = make_scorer(truth)
        val_sk = Ed25519PrivateKey.generate()
        val_pk = val_sk.public_key().public_bytes_raw()

        monkeypatch.setenv("SN21_OVERRIDE_WEIGHT_UID", "135")
        monkeypatch.setenv("SN21_BURN_FRACTION", "0.75")
        captured: dict = {}

        def _capture(**kwargs):
            captured["uids"] = list(kwargs["uids"])
            captured["weights"] = list(kwargs["weights"])
            return WeightsCommitResult(success=True, message="OK", block_number=10,
                                       block_hash=os.urandom(32), extrinsic_hash=None)

        with (
            patch("hope.validator.onchain_runner.submit_pre_scoring_state_layer_9c1",
                  return_value=CommitResult(success=True, message="OK", block_number=1,
                                            extrinsic_hash=None, reveal_round=2)),
            patch("hope.validator.onchain_runner.commit_weights_layer_9c3",
                  side_effect=_capture),
            patch("hope.validator.onchain_runner.submit_post_scoring_artifacts_layer_9c2",
                  return_value=CommitResult(success=True, message="OK", block_number=20,
                                            extrinsic_hash=None, reveal_round=21)),
        ):
            run_epoch_scoring(
                subtensor=object(), validator_wallet=object(), netuid=21,
                epoch_id="EP-A", epoch_idx=42,
                validator_hotkey=val_pk, validator_signing_key=val_sk,
                miner_inputs=inputs,
                archive_endpoints=[ArchiveEndpoint(tier=2, base_url="https://hope")],
                archive_client=adapter,
                timing=TimingBounds(epoch_open_round=12345600,
                                    miner_deadline_round=12346000,
                                    chain_window_min_block=0, chain_window_max_block=2**31),
                outcomes_release_round=12345600, outcomes_fetched_at_round=12345700,
                scoring_inputs_hash=os.urandom(32), scorer=scorer,
                blocks_until_pre_scoring_reveal=300,
                blocks_until_post_scoring_reveal=600,
                blocks_until_weights_reveal=360,
            )

        # override UID 135 present with 75% of the weight
        assert 135 in captured["uids"]
        burn_w = captured["weights"][captured["uids"].index(135)]
        assert abs(burn_w - 0.75) < 1e-9
        # the 4 scored miners share the remaining 25%
        miner_w = [w for u, w in zip(captured["uids"], captured["weights"]) if u != 135]
        assert len(miner_w) == 4
        assert abs(sum(miner_w) - 0.25) < 1e-9

    def test_weight_allowlist_restricts_to_cohort(self, four_miners, monkeypatch):
        """SN21_WEIGHT_ALLOWLIST_UIDS restricts the miner share to the listed
        UIDs only; combined with 75% burn, only the cohort splits the 25%."""
        from hope.validator.onchain_runner import run_epoch_scoring

        http_preds = {
            pk: [_pred(f"EP-{j}", miner=pk.hex()[:8]) for j in range(3)]
            for pk, _ in four_miners
        }
        keys = {pk: sk for pk, sk in four_miners}
        inputs, ct_map = replay_http_predictions_as_chain_inputs(
            epoch_id="EP-A", http_predictions=http_preds, miner_signing_keys=keys,
        )
        store = InMemoryStore()
        stash_into_archive(store, epoch_id="EP-A",
                           ct_by_sha256=ct_map, miner_inputs=inputs)

        class _StoreAdapter:
            def __init__(self, st):
                self.st = st

            def fetch_first_match(self, endpoints, *, epoch_id, miner_identity,
                                  expected_sha256):
                got = self.st.get(epoch_id=epoch_id, miner_identity=miner_identity,
                                  sha256=expected_sha256)
                if got is None:
                    return FetchAggregate(aes_ct=None, winner=None, attempts=[])
                winner = FetchResult(endpoint=endpoints[0], ok=True, aes_ct=got,
                                     sha256_match=True, status_code=200, elapsed_ms=1)
                return FetchAggregate(aes_ct=got, winner=winner, attempts=[winner])

        adapter = _StoreAdapter(store)
        truth = {"7": HorizonTruth(
            horizon="7", truth_cost_p50_dpct=0, truth_conv_p50_dpct=10,
            truth_eff_p50_dpct=5, goal_miss_freq_ppm=100_000, instab_freq_ppm=50_000,
        )}
        scorer = make_scorer(truth)
        val_sk = Ed25519PrivateKey.generate()
        val_pk = val_sk.public_key().public_bytes_raw()

        # allowlist only 2 of the 4 scored miners
        allow = sorted(inp.miner_uid for inp in inputs)[:2]
        monkeypatch.setenv("SN21_WEIGHT_ALLOWLIST_UIDS", ",".join(str(u) for u in allow))
        monkeypatch.setenv("SN21_OVERRIDE_WEIGHT_UID", "135")
        monkeypatch.setenv("SN21_BURN_FRACTION", "0.75")
        captured: dict = {}

        def _capture(**kwargs):
            captured["uids"] = list(kwargs["uids"])
            captured["weights"] = list(kwargs["weights"])
            return WeightsCommitResult(success=True, message="OK", block_number=10,
                                       block_hash=os.urandom(32), extrinsic_hash=None)

        with (
            patch("hope.validator.onchain_runner.submit_pre_scoring_state_layer_9c1",
                  return_value=CommitResult(success=True, message="OK", block_number=1,
                                            extrinsic_hash=None, reveal_round=2)),
            patch("hope.validator.onchain_runner.commit_weights_layer_9c3",
                  side_effect=_capture),
            patch("hope.validator.onchain_runner.submit_post_scoring_artifacts_layer_9c2",
                  return_value=CommitResult(success=True, message="OK", block_number=20,
                                            extrinsic_hash=None, reveal_round=21)),
        ):
            run_epoch_scoring(
                subtensor=object(), validator_wallet=object(), netuid=21,
                epoch_id="EP-A", epoch_idx=42,
                validator_hotkey=val_pk, validator_signing_key=val_sk,
                miner_inputs=inputs,
                archive_endpoints=[ArchiveEndpoint(tier=2, base_url="https://hope")],
                archive_client=adapter,
                timing=TimingBounds(epoch_open_round=12345600,
                                    miner_deadline_round=12346000,
                                    chain_window_min_block=0, chain_window_max_block=2**31),
                outcomes_release_round=12345600, outcomes_fetched_at_round=12345700,
                scoring_inputs_hash=os.urandom(32), scorer=scorer,
                blocks_until_pre_scoring_reveal=300,
                blocks_until_post_scoring_reveal=600,
                blocks_until_weights_reveal=360,
            )

        # only the 2 allowlisted miners present (+ uid135), sharing the 25%
        miners = [(u, w) for u, w in zip(captured["uids"], captured["weights"]) if u != 135]
        assert sorted(u for u, _ in miners) == allow
        assert len(miners) == 2
        assert abs(sum(w for _, w in miners) - 0.25) < 1e-9
        burn_w = captured["weights"][captured["uids"].index(135)]
        assert abs(burn_w - 0.75) < 1e-9

    def test_tiered_weights_compose_with_burn(self, four_miners, monkeypatch):
        """SN21_TIERED_WEIGHTS routes the miner vector through the tiered
        allocator (4 miners < 15 → single proportional pool), composing with
        the 75/25 burn split: uid135 → 75%, qualifying miners → 25% by score."""
        from hope.validator.onchain_runner import run_epoch_scoring

        http_preds = {
            pk: [_pred(f"EP-{j}", miner=pk.hex()[:8]) for j in range(3)]
            for pk, _ in four_miners
        }
        keys = {pk: sk for pk, sk in four_miners}
        inputs, ct_map = replay_http_predictions_as_chain_inputs(
            epoch_id="EP-A", http_predictions=http_preds, miner_signing_keys=keys,
        )
        store = InMemoryStore()
        stash_into_archive(store, epoch_id="EP-A",
                           ct_by_sha256=ct_map, miner_inputs=inputs)

        class _StoreAdapter:
            def __init__(self, st):
                self.st = st

            def fetch_first_match(self, endpoints, *, epoch_id, miner_identity,
                                  expected_sha256):
                got = self.st.get(epoch_id=epoch_id, miner_identity=miner_identity,
                                  sha256=expected_sha256)
                if got is None:
                    return FetchAggregate(aes_ct=None, winner=None, attempts=[])
                winner = FetchResult(endpoint=endpoints[0], ok=True, aes_ct=got,
                                     sha256_match=True, status_code=200, elapsed_ms=1)
                return FetchAggregate(aes_ct=got, winner=winner, attempts=[winner])

        adapter = _StoreAdapter(store)
        truth = {"7": HorizonTruth(
            horizon="7", truth_cost_p50_dpct=0, truth_conv_p50_dpct=10,
            truth_eff_p50_dpct=5, goal_miss_freq_ppm=100_000, instab_freq_ppm=50_000,
        )}
        scorer = make_scorer(truth)
        val_sk = Ed25519PrivateKey.generate()
        val_pk = val_sk.public_key().public_bytes_raw()

        monkeypatch.setenv("SN21_TIERED_WEIGHTS", "1")
        monkeypatch.setenv("SN21_OVERRIDE_WEIGHT_UID", "135")
        monkeypatch.setenv("SN21_BURN_FRACTION", "0.75")
        captured: dict = {}

        def _capture(**kwargs):
            captured["uids"] = list(kwargs["uids"])
            captured["weights"] = list(kwargs["weights"])
            return WeightsCommitResult(success=True, message="OK", block_number=10,
                                       block_hash=os.urandom(32), extrinsic_hash=None)

        with (
            patch("hope.validator.onchain_runner.submit_pre_scoring_state_layer_9c1",
                  return_value=CommitResult(success=True, message="OK", block_number=1,
                                            extrinsic_hash=None, reveal_round=2)),
            patch("hope.validator.onchain_runner.commit_weights_layer_9c3",
                  side_effect=_capture),
            patch("hope.validator.onchain_runner.submit_post_scoring_artifacts_layer_9c2",
                  return_value=CommitResult(success=True, message="OK", block_number=20,
                                            extrinsic_hash=None, reveal_round=21)),
        ):
            run_epoch_scoring(
                subtensor=object(), validator_wallet=object(), netuid=21,
                epoch_id="EP-A", epoch_idx=42,
                validator_hotkey=val_pk, validator_signing_key=val_sk,
                miner_inputs=inputs,
                archive_endpoints=[ArchiveEndpoint(tier=2, base_url="https://hope")],
                archive_client=adapter,
                timing=TimingBounds(epoch_open_round=12345600,
                                    miner_deadline_round=12346000,
                                    chain_window_min_block=0, chain_window_max_block=2**31),
                outcomes_release_round=12345600, outcomes_fetched_at_round=12345700,
                scoring_inputs_hash=os.urandom(32), scorer=scorer,
                blocks_until_pre_scoring_reveal=300,
                blocks_until_post_scoring_reveal=600,
                blocks_until_weights_reveal=360,
                baseline_score=0.0,  # all qualify
            )

        assert 135 in captured["uids"]
        burn_w = captured["weights"][captured["uids"].index(135)]
        assert abs(burn_w - 0.75) < 1e-9
        miner_w = [w for u, w in zip(captured["uids"], captured["weights"]) if u != 135]
        assert len(miner_w) == 4
        assert abs(sum(miner_w) - 0.25) < 1e-9

    def test_tiered_gate_excludes_all_below_baseline(self, four_miners, monkeypatch):
        """A baseline above every achievable score gates out all miners → no
        miner funded (proves the participation gate is actually applied)."""
        from hope.validator.onchain_runner import run_epoch_scoring

        http_preds = {
            pk: [_pred(f"EP-{j}", miner=pk.hex()[:8]) for j in range(3)]
            for pk, _ in four_miners
        }
        keys = {pk: sk for pk, sk in four_miners}
        inputs, ct_map = replay_http_predictions_as_chain_inputs(
            epoch_id="EP-A", http_predictions=http_preds, miner_signing_keys=keys,
        )
        store = InMemoryStore()
        stash_into_archive(store, epoch_id="EP-A",
                           ct_by_sha256=ct_map, miner_inputs=inputs)

        class _StoreAdapter:
            def __init__(self, st):
                self.st = st

            def fetch_first_match(self, endpoints, *, epoch_id, miner_identity,
                                  expected_sha256):
                got = self.st.get(epoch_id=epoch_id, miner_identity=miner_identity,
                                  sha256=expected_sha256)
                if got is None:
                    return FetchAggregate(aes_ct=None, winner=None, attempts=[])
                winner = FetchResult(endpoint=endpoints[0], ok=True, aes_ct=got,
                                     sha256_match=True, status_code=200, elapsed_ms=1)
                return FetchAggregate(aes_ct=got, winner=winner, attempts=[winner])

        adapter = _StoreAdapter(store)
        truth = {"7": HorizonTruth(
            horizon="7", truth_cost_p50_dpct=0, truth_conv_p50_dpct=10,
            truth_eff_p50_dpct=5, goal_miss_freq_ppm=100_000, instab_freq_ppm=50_000,
        )}
        scorer = make_scorer(truth)
        val_sk = Ed25519PrivateKey.generate()
        val_pk = val_sk.public_key().public_bytes_raw()

        monkeypatch.setenv("SN21_TIERED_WEIGHTS", "1")
        called = {"commit": False}

        def _capture(**kwargs):
            called["commit"] = True
            return WeightsCommitResult(success=True, message="OK", block_number=10,
                                       block_hash=os.urandom(32), extrinsic_hash=None)

        with (
            patch("hope.validator.onchain_runner.submit_pre_scoring_state_layer_9c1",
                  return_value=CommitResult(success=True, message="OK", block_number=1,
                                            extrinsic_hash=None, reveal_round=2)),
            patch("hope.validator.onchain_runner.commit_weights_layer_9c3",
                  side_effect=_capture),
            patch("hope.validator.onchain_runner.submit_post_scoring_artifacts_layer_9c2",
                  return_value=CommitResult(success=True, message="OK", block_number=20,
                                            extrinsic_hash=None, reveal_round=21)),
        ):
            outcome = run_epoch_scoring(
                subtensor=object(), validator_wallet=object(), netuid=21,
                epoch_id="EP-A", epoch_idx=42,
                validator_hotkey=val_pk, validator_signing_key=val_sk,
                miner_inputs=inputs,
                archive_endpoints=[ArchiveEndpoint(tier=2, base_url="https://hope")],
                archive_client=adapter,
                timing=TimingBounds(epoch_open_round=12345600,
                                    miner_deadline_round=12346000,
                                    chain_window_min_block=0, chain_window_max_block=2**31),
                outcomes_release_round=12345600, outcomes_fetched_at_round=12345700,
                scoring_inputs_hash=os.urandom(32), scorer=scorer,
                blocks_until_pre_scoring_reveal=300,
                blocks_until_post_scoring_reveal=600,
                blocks_until_weights_reveal=360,
                baseline_score=2.0,  # above the max achievable score (1.0)
            )

        assert called["commit"] is False
        assert outcome.ok is False
        assert len(outcome.score_map) == 4  # scoring still ran; the gate is post-score
