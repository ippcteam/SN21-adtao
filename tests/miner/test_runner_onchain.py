"""End-to-end test for MinerRunner.run_epoch_onchain."""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.archives import ArchiveEndpoint, UploadResult
from hope.commitment.on_chain import CommitResult
from hope.miner.prediction_engine import PredictionEngine
from hope.miner.runner import MinerRunner, _avg
from hope.protocol.episode import AccountState, Episode, EpisodeMetadata
from hope.protocol.prediction import (
    HorizonPrediction,
    Prediction,
    QuantilePrediction,
)


class StubModel(PredictionEngine):
    """Returns a fixed prediction for any episode."""

    def predict(self, episode):
        return Prediction(
            episode_id=episode.episode_metadata.episode_id,
            miner_id="stub",
            submitted_at=datetime.utcnow(),
            horizons={
                "7": HorizonPrediction(
                    cost_delta_pct=QuantilePrediction(p10=-5.0, p50=0.0, p90=5.0),
                    conversions_delta_pct=QuantilePrediction(p10=-3.0, p50=1.0, p90=4.0),
                    efficiency_delta_pct=QuantilePrediction(p10=-2.0, p50=0.5, p90=4.0),
                    goal_miss_probability=0.10,
                    instability_risk=0.05,
                ),
                "14": HorizonPrediction(
                    cost_delta_pct=QuantilePrediction(p10=-7.0, p50=-1.0, p90=7.0),
                    conversions_delta_pct=QuantilePrediction(p10=-4.0, p50=0.5, p90=5.0),
                    efficiency_delta_pct=QuantilePrediction(p10=-3.0, p50=0.0, p90=5.0),
                    goal_miss_probability=0.15,
                    instability_risk=0.08,
                ),
            },
        )


class FakeWallet:
    class _HK:
        ss58_address = "5Hoo2cRURm8A36WupNHyBkdby3wyBEpwj7MAgpC9sLnhxJNw"

    hotkey = _HK()


def _ep(idx: int) -> Episode:
    md = EpisodeMetadata(episode_id=f"EP-{idx:03d}")
    return Episode(
        episode_metadata=md,
        account_state=AccountState(customer_id_hash=f"CUST-{idx:03d}"),
    )


def _ok_commit(block: int, reveal_round=None) -> CommitResult:
    return CommitResult(
        success=True, message="OK", block_number=block,
        extrinsic_hash="0x" + "ab" * 32, reveal_round=reveal_round,
    )


class FakeArchiveClient:
    def __init__(self, results_factory=None):
        self._factory = results_factory or (
            lambda eps, **_: [UploadResult(endpoint=e, ok=True, status_code=200) for e in eps]
        )

    def upload_to_all(self, endpoints, **kwargs):
        return self._factory(endpoints, **kwargs)


def test_run_epoch_onchain_happy_path(monkeypatch):
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key().public_bytes_raw()

    runner = MinerRunner(model=StubModel(), hotkey="5xyz", wallet=FakeWallet())
    episodes = [_ep(i) for i in range(5)]

    endpoints = [
        ArchiveEndpoint(tier=1, base_url="https://val", name="val"),
        ArchiveEndpoint(tier=2, base_url="https://archive.example.io", name="archive2"),
        ArchiveEndpoint(tier=3, base_url="https://miner", name="miner"),
    ]

    # Patch the chain submission helpers used by submit_miner_epoch.
    with (
        patch(
            "hope.miner.onchain_submitter.submit_miner_prediction_layer_9b",
            return_value=_ok_commit(7038901, reveal_round=12345710),
        ),
        # Inject our fake archive client by monkeypatching the default factory
        # in submit_miner_epoch. Easiest path: patch ArchiveClient so
        # submit_miner_epoch's default-construct yields our fake.
        patch("hope.miner.onchain_submitter.ArchiveClient", new=FakeArchiveClient),
    ):
        result = runner.run_epoch_onchain(
            epoch_id="EPOCH-A",
            episodes=episodes,
            subtensor=object(),
            miner_wallet=FakeWallet(),
            netuid=466,
            miner_hotkey_bytes=pk,
            miner_signing_key=sk,
            submitted_round=12345700,
            archive_endpoints=endpoints,
            self_archive_url="https://miner.example/archive/EPOCH-A",
            blocks_until_reveal=300,
        )

    assert result.ok
    assert all(r.ok for r in result.archive_uploads)
    assert result.chain_bundle_commit.success


def test_no_predictions_raises(monkeypatch):
    """Empty episode list → no predictions → no horizons → ValueError."""

    class EmptyModel(PredictionEngine):
        def predict(self, episode):
            raise RuntimeError("can't predict anything")

    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key().public_bytes_raw()
    runner = MinerRunner(model=EmptyModel(), hotkey="x", wallet=FakeWallet())

    with pytest.raises(ValueError, match="no horizon entries"):
        runner.run_epoch_onchain(
            epoch_id="EPOCH-A",
            episodes=[_ep(0)],
            subtensor=object(),
            miner_wallet=FakeWallet(),
            netuid=466,
            miner_hotkey_bytes=pk,
            miner_signing_key=sk,
            submitted_round=12345700,
            archive_endpoints=[ArchiveEndpoint(tier=2, base_url="https://archive.example.io")],
            self_archive_url="https://m/x",
            blocks_until_reveal=300,
        )


def test_horizon_aggregation_average():
    """Multiple predictions with varying quantiles average to the mean."""
    runner = MinerRunner(model=StubModel(), hotkey="x", wallet=FakeWallet())
    preds = [
        Prediction(
            episode_id=f"EP-{i}",
            miner_id="m",
            submitted_at=datetime.utcnow(),
            horizons={
                "7": HorizonPrediction(
                    cost_delta_pct=QuantilePrediction(p10=-i*1.0, p50=0.0, p90=i*1.0),
                    conversions_delta_pct=QuantilePrediction(p10=0.0, p50=0.0, p90=0.0),
                    efficiency_delta_pct=QuantilePrediction(p10=0.0, p50=0.0, p90=0.0),
                    goal_miss_probability=0.1,
                    instability_risk=0.05,
                ),
            },
        )
        for i in range(1, 4)
    ]
    horizons = runner._predictions_to_horizon_entries(preds)
    assert len(horizons) == 1
    h = horizons[0]
    # cost_p10 averages -1, -2, -3 → -2; cost_p90 → +2 → ×10 = 20 deci-pct
    assert h["cost_q"] == [-20, 0, 20]


def test_avg_helper():
    assert _avg([1, 2, 3]) == 2.0
    assert _avg([]) == 0.0
