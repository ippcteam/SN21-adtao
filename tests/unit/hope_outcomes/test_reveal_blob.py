"""Unit tests for hope/hope_outcomes/reveal_blob.py — Layer 9.A.2."""

from __future__ import annotations

import hashlib
import json
import os

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.hope_outcomes.reveal_blob import (
    REVEAL_BLOB_VERSION,
    EpisodeOutcome,
    HorizonOutcomeMeasured,
    build_reveal_blob,
    compute_reveal_blob_sha256,
    verify_reveal_blob,
)


@pytest.fixture
def signer():
    sk = Ed25519PrivateKey.generate()
    return sk, sk.public_key().public_bytes_raw()


def _outcome(ep_id):
    return EpisodeOutcome(
        episode_id=ep_id,
        salt=os.urandom(16),
        outcomes=[
            HorizonOutcomeMeasured(
                horizon="7",
                cost_delta_pct=-3.7,
                conversions_delta_pct=12.5,
                efficiency_delta_pct=8.2,
                goal_miss=0,
            ),
            HorizonOutcomeMeasured(
                horizon="14",
                cost_delta_pct=-4.1,
                conversions_delta_pct=15.0,
                efficiency_delta_pct=9.8,
                goal_miss=0,
            ),
        ],
    )


class TestBuildVerifyRoundTrip:
    def test_basic(self, signer):
        sk, pk = signer
        episodes = [_outcome(f"EP-{i:03d}") for i in range(5)]
        blob = build_reveal_blob(
            outcome_signer_hotkey=pk,
            outcome_signer_signing_key=sk,
            epoch_id="EPOCH-A",
            epoch_idx=1,
            release_commit_plaintext_sha256=os.urandom(32),
            deadline_round=100,
            measured_at_round=200,
            horizons=["7", "14"],
            episodes=episodes,
        )
        decoded = json.loads(blob)
        assert decoded["v"] == REVEAL_BLOB_VERSION
        assert decoded["epoch_id"] == "EPOCH-A"
        assert decoded["outcome_signer_hotkey_hex"] == pk.hex()
        assert len(decoded["episodes"]) == 5
        assert "inner_sig_hex" in decoded
        assert verify_reveal_blob(blob)

    def test_sha256_matches(self, signer):
        sk, pk = signer
        blob = build_reveal_blob(
            outcome_signer_hotkey=pk,
            outcome_signer_signing_key=sk,
            epoch_id="EPOCH-A",
            epoch_idx=1,
            release_commit_plaintext_sha256=os.urandom(32),
            deadline_round=100,
            measured_at_round=200,
            horizons=["7", "14"],
            episodes=[_outcome("EP-001")],
        )
        digest = compute_reveal_blob_sha256(blob)
        assert digest == hashlib.sha256(blob).digest()


class TestValidation:
    def test_invalid_signer_hotkey_length(self, signer):
        sk, _ = signer
        with pytest.raises(ValueError, match="32 bytes"):
            build_reveal_blob(
                outcome_signer_hotkey=b"\x00" * 31,
                outcome_signer_signing_key=sk,
                epoch_id="EPOCH-A",
                epoch_idx=1,
                release_commit_plaintext_sha256=os.urandom(32),
                deadline_round=100,
                measured_at_round=200,
                horizons=["7"],
                episodes=[],
            )

    def test_signing_key_mismatch_rejected(self):
        sk1 = Ed25519PrivateKey.generate()
        sk2 = Ed25519PrivateKey.generate()
        with pytest.raises(ValueError, match="does not match"):
            build_reveal_blob(
                outcome_signer_hotkey=sk1.public_key().public_bytes_raw(),
                outcome_signer_signing_key=sk2,
                epoch_id="EPOCH-A",
                epoch_idx=1,
                release_commit_plaintext_sha256=os.urandom(32),
                deadline_round=100,
                measured_at_round=200,
                horizons=["7"],
                episodes=[],
            )

    def test_release_sha_must_be_32(self, signer):
        sk, pk = signer
        with pytest.raises(ValueError, match="32 bytes"):
            build_reveal_blob(
                outcome_signer_hotkey=pk,
                outcome_signer_signing_key=sk,
                epoch_id="EPOCH-A",
                epoch_idx=1,
                release_commit_plaintext_sha256=b"\x00" * 31,
                deadline_round=100,
                measured_at_round=200,
                horizons=["7"],
                episodes=[],
            )

    def test_measured_must_not_precede_deadline(self, signer):
        sk, pk = signer
        with pytest.raises(ValueError, match="must be >="):
            build_reveal_blob(
                outcome_signer_hotkey=pk,
                outcome_signer_signing_key=sk,
                epoch_id="EPOCH-A",
                epoch_idx=1,
                release_commit_plaintext_sha256=os.urandom(32),
                deadline_round=200,
                measured_at_round=100,
                horizons=["7"],
                episodes=[],
            )

    def test_unknown_horizon_in_episode_rejected(self, signer):
        sk, pk = signer
        bad_ep = EpisodeOutcome(
            episode_id="EP-1",
            salt=os.urandom(16),
            outcomes=[
                HorizonOutcomeMeasured(
                    horizon="30",
                    cost_delta_pct=0, conversions_delta_pct=0,
                    efficiency_delta_pct=0, goal_miss=0,
                ),
            ],
        )
        with pytest.raises(ValueError, match="horizon"):
            build_reveal_blob(
                outcome_signer_hotkey=pk,
                outcome_signer_signing_key=sk,
                epoch_id="EPOCH-A",
                epoch_idx=1,
                release_commit_plaintext_sha256=os.urandom(32),
                deadline_round=100,
                measured_at_round=200,
                horizons=["7", "14"],
                episodes=[bad_ep],
            )

    def test_invalid_goal_miss_value(self, signer):
        sk, pk = signer
        bad = EpisodeOutcome(
            episode_id="EP-1",
            salt=os.urandom(16),
            outcomes=[
                HorizonOutcomeMeasured(
                    horizon="7", cost_delta_pct=0, conversions_delta_pct=0,
                    efficiency_delta_pct=0, goal_miss=2,
                ),
            ],
        )
        with pytest.raises(ValueError, match="goal_miss"):
            build_reveal_blob(
                outcome_signer_hotkey=pk,
                outcome_signer_signing_key=sk,
                epoch_id="EPOCH-A",
                epoch_idx=1,
                release_commit_plaintext_sha256=os.urandom(32),
                deadline_round=100,
                measured_at_round=200,
                horizons=["7"],
                episodes=[bad],
            )

    def test_duplicate_episode_id_rejected(self, signer):
        sk, pk = signer
        with pytest.raises(ValueError, match="duplicate"):
            build_reveal_blob(
                outcome_signer_hotkey=pk,
                outcome_signer_signing_key=sk,
                epoch_id="EPOCH-A",
                epoch_idx=1,
                release_commit_plaintext_sha256=os.urandom(32),
                deadline_round=100,
                measured_at_round=200,
                horizons=["7", "14"],
                episodes=[_outcome("EP-1"), _outcome("EP-1")],
            )

    def test_bad_salt_length_rejected(self, signer):
        sk, pk = signer
        bad = EpisodeOutcome(
            episode_id="EP-1",
            salt=b"\x00" * 8,
            outcomes=[
                HorizonOutcomeMeasured(
                    horizon="7", cost_delta_pct=0, conversions_delta_pct=0,
                    efficiency_delta_pct=0, goal_miss=0,
                ),
            ],
        )
        with pytest.raises(ValueError, match="salt"):
            build_reveal_blob(
                outcome_signer_hotkey=pk,
                outcome_signer_signing_key=sk,
                epoch_id="EPOCH-A",
                epoch_idx=1,
                release_commit_plaintext_sha256=os.urandom(32),
                deadline_round=100,
                measured_at_round=200,
                horizons=["7"],
                episodes=[bad],
            )


class TestVerifyRejectsTampering:
    def test_tampered_outcome_fails_verify(self, signer):
        sk, pk = signer
        blob = build_reveal_blob(
            outcome_signer_hotkey=pk,
            outcome_signer_signing_key=sk,
            epoch_id="EPOCH-A",
            epoch_idx=1,
            release_commit_plaintext_sha256=os.urandom(32),
            deadline_round=100,
            measured_at_round=200,
            horizons=["7", "14"],
            episodes=[_outcome("EP-1")],
        )
        # Mutate one outcome value, keep the original signature.
        d = json.loads(blob)
        d["episodes"][0]["outcomes"]["7"]["cost_delta_pct"] = 99.9
        tampered = json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")
        assert not verify_reveal_blob(tampered)

    def test_tampered_signer_pk_fails(self, signer):
        sk, pk = signer
        blob = build_reveal_blob(
            outcome_signer_hotkey=pk,
            outcome_signer_signing_key=sk,
            epoch_id="EPOCH-A",
            epoch_idx=1,
            release_commit_plaintext_sha256=os.urandom(32),
            deadline_round=100,
            measured_at_round=200,
            horizons=["7", "14"],
            episodes=[_outcome("EP-1")],
        )
        d = json.loads(blob)
        d["outcome_signer_hotkey_hex"] = "00" * 32
        tampered = json.dumps(d, sort_keys=True, separators=(",", ":")).encode("utf-8")
        assert not verify_reveal_blob(tampered)

    def test_missing_inner_sig_returns_false(self):
        bad = json.dumps({"v": 1, "outcome_signer_hotkey_hex": "00" * 32}).encode("utf-8")
        assert not verify_reveal_blob(bad)

    def test_invalid_json_returns_false(self):
        assert not verify_reveal_blob(b"not json")
