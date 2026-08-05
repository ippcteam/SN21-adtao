"""Unit tests for hope/hope_outcomes/release_commit.py — Layer 9.A.1."""

from __future__ import annotations

import hashlib
import os

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.canonical import canonical_cbor_dumps, canonical_cbor_loads
from hope.commitment.inner_sig import verify_inner_sig
from hope.hope_outcomes.release_commit import (
    RELEASE_COMMIT_VERSION,
    EpisodeRef,
    build_release_commit_plaintext,
    compute_episodes_root,
    compute_release_commit_digest,
)


@pytest.fixture
def signer():
    sk = Ed25519PrivateKey.generate()
    return sk, sk.public_key().public_bytes_raw()


@pytest.fixture
def episodes():
    return [
        EpisodeRef(
            episode_id=f"EP-{i:03d}",
            query_cbor=canonical_cbor_dumps({"campaign_id": i, "goal": "CPA"}),
        )
        for i in range(20)
    ]


class TestComputeEpisodesRoot:
    def test_deterministic(self, episodes):
        a = compute_episodes_root(episodes)
        b = compute_episodes_root(episodes)
        assert a == b
        assert len(a) == 32

    def test_order_independent(self, episodes):
        a = compute_episodes_root(episodes)
        b = compute_episodes_root(list(reversed(episodes)))
        assert a == b

    def test_changing_query_changes_root(self, episodes):
        a = compute_episodes_root(episodes)
        modified = list(episodes)
        modified[5] = EpisodeRef(
            episode_id=modified[5].episode_id,
            query_cbor=canonical_cbor_dumps({"campaign_id": 9999}),
        )
        b = compute_episodes_root(modified)
        assert a != b

    def test_duplicate_episode_id_rejected(self):
        with pytest.raises(ValueError, match="duplicate episode_id"):
            compute_episodes_root([
                EpisodeRef(episode_id="EP-X", query_cbor=b"a"),
                EpisodeRef(episode_id="EP-X", query_cbor=b"b"),
            ])

    def test_empty_returns_32_bytes(self):
        root = compute_episodes_root([])
        assert len(root) == 32


class TestBuildReleaseCommit:
    def test_basic(self, signer, episodes):
        sk, pk = signer
        plain = build_release_commit_plaintext(
            outcome_signer_hotkey=pk,
            outcome_signer_signing_key=sk,
            epoch_id="EPOCH-A",
            epoch_idx=1,
            release_round=12345600,
            deadline_round=12345700,
            horizons=["7", "14"],
            episodes=episodes,
            scoring_metadata_hash=os.urandom(32),
        )
        decoded = canonical_cbor_loads(plain)
        assert decoded["v"] == RELEASE_COMMIT_VERSION
        assert decoded["epoch_id"] == "EPOCH-A"
        assert decoded["epoch_idx"] == 1
        assert decoded["horizons"] == ["7", "14"]
        assert decoded["outcome_signer_hotkey"] == pk
        assert "inner_sig" in decoded
        assert verify_inner_sig(decoded, pk, hotkey_field="outcome_signer_hotkey")

    def test_canonical_round_trip(self, signer, episodes):
        sk, pk = signer
        plain = build_release_commit_plaintext(
            outcome_signer_hotkey=pk,
            outcome_signer_signing_key=sk,
            epoch_id="EPOCH-A",
            epoch_idx=1,
            release_round=100,
            deadline_round=200,
            horizons=["7"],
            episodes=episodes,
            scoring_metadata_hash=b"\x00" * 32,
        )
        re_encoded = canonical_cbor_dumps(canonical_cbor_loads(plain))
        assert re_encoded == plain

    def test_signing_key_mismatch_rejected(self, episodes):
        sk1 = Ed25519PrivateKey.generate()
        sk2 = Ed25519PrivateKey.generate()
        with pytest.raises(ValueError, match="does not match"):
            build_release_commit_plaintext(
                outcome_signer_hotkey=sk1.public_key().public_bytes_raw(),
                outcome_signer_signing_key=sk2,
                epoch_id="EPOCH-A",
                epoch_idx=1,
                release_round=100,
                deadline_round=200,
                horizons=["7"],
                episodes=episodes,
                scoring_metadata_hash=b"\x00" * 32,
            )

    def test_deadline_must_exceed_release(self, signer, episodes):
        sk, pk = signer
        with pytest.raises(ValueError, match="must exceed"):
            build_release_commit_plaintext(
                outcome_signer_hotkey=pk,
                outcome_signer_signing_key=sk,
                epoch_id="EPOCH-A",
                epoch_idx=1,
                release_round=200,
                deadline_round=100,
                horizons=["7"],
                episodes=episodes,
                scoring_metadata_hash=b"\x00" * 32,
            )

    def test_invalid_signer_hotkey_length(self, episodes):
        sk = Ed25519PrivateKey.generate()
        with pytest.raises(ValueError, match="32 bytes"):
            build_release_commit_plaintext(
                outcome_signer_hotkey=b"\x00" * 31,
                outcome_signer_signing_key=sk,
                epoch_id="EPOCH-A",
                epoch_idx=1,
                release_round=100,
                deadline_round=200,
                horizons=["7"],
                episodes=episodes,
                scoring_metadata_hash=b"\x00" * 32,
            )

    def test_empty_horizons_rejected(self, signer, episodes):
        sk, pk = signer
        with pytest.raises(ValueError, match="horizons must be non-empty"):
            build_release_commit_plaintext(
                outcome_signer_hotkey=pk,
                outcome_signer_signing_key=sk,
                epoch_id="EPOCH-A",
                epoch_idx=1,
                release_round=100,
                deadline_round=200,
                horizons=[],
                episodes=episodes,
                scoring_metadata_hash=b"\x00" * 32,
            )


class TestComputeReleaseCommitDigest:
    def test_blake2b_32_bytes(self, signer, episodes):
        sk, pk = signer
        plain = build_release_commit_plaintext(
            outcome_signer_hotkey=pk,
            outcome_signer_signing_key=sk,
            epoch_id="X",
            epoch_idx=0,
            release_round=1,
            deadline_round=2,
            horizons=["7"],
            episodes=episodes,
            scoring_metadata_hash=b"\x00" * 32,
        )
        d = compute_release_commit_digest(plain)
        assert len(d) == 32
        assert d == hashlib.blake2b(plain, digest_size=32).digest()
