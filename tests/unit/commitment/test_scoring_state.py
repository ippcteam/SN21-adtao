"""Unit tests for hope/commitment/scoring_state.py — 9.C.1 / 9.C.2 builders."""

from __future__ import annotations

import os

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.canonical import canonical_cbor_dumps, canonical_cbor_loads
from hope.commitment.inner_sig import verify_inner_sig
from hope.commitment.on_chain import MAX_TLE_PLAINTEXT_BYTES
from hope.commitment.scoring_state import (
    SCORING_STATE_VERSION,
    ExcludedMinerRecord,
    MinerCommitRecord,
    ScoredMinerRecord,
    build_post_scoring_artifacts,
    build_pre_scoring_state,
    compute_excluded_miners_hash,
    compute_final_score_root,
    compute_miner_commits_root,
)


@pytest.fixture
def val_key():
    return Ed25519PrivateKey.generate()


@pytest.fixture
def val_pk(val_key):
    return val_key.public_key().public_bytes_raw()


@pytest.fixture
def miner_records():
    return [
        MinerCommitRecord(
            miner_hotkey=os.urandom(32),
            k_block=7038900 + i,
            k_round=12345700 + i,
            sha256_ct=os.urandom(32),
        )
        for i in range(20)
    ]


# ---------- compute_miner_commits_root ----------


class TestMinerCommitsRoot:
    def test_deterministic(self, miner_records):
        a = compute_miner_commits_root(miner_records)
        b = compute_miner_commits_root(miner_records)
        assert a == b
        assert len(a) == 32

    def test_order_independent(self, miner_records):
        """IMT sorts internally; reordering input must yield same root."""
        a = compute_miner_commits_root(miner_records)
        b = compute_miner_commits_root(list(reversed(miner_records)))
        assert a == b

    def test_different_input_different_root(self, miner_records):
        a = compute_miner_commits_root(miner_records)
        b = compute_miner_commits_root(miner_records[:10])
        assert a != b

    def test_changing_one_record_changes_root(self, miner_records):
        a = compute_miner_commits_root(miner_records)
        modified = list(miner_records)
        modified[5] = MinerCommitRecord(
            miner_hotkey=miner_records[5].miner_hotkey,
            k_block=miner_records[5].k_block + 1,  # one block later
            k_round=miner_records[5].k_round,
            sha256_ct=miner_records[5].sha256_ct,
        )
        b = compute_miner_commits_root(modified)
        assert a != b

    def test_empty_input_returns_32_bytes(self):
        root = compute_miner_commits_root([])
        assert len(root) == 32

    def test_invalid_hotkey_length(self):
        with pytest.raises(ValueError, match="32 bytes"):
            compute_miner_commits_root([
                MinerCommitRecord(
                    miner_hotkey=b"\x00" * 31,
                    k_block=1,
                    k_round=1,
                    sha256_ct=os.urandom(32),
                )
            ])

    def test_invalid_sha_length(self):
        with pytest.raises(ValueError, match="32 bytes"):
            compute_miner_commits_root([
                MinerCommitRecord(
                    miner_hotkey=os.urandom(32),
                    k_block=1,
                    k_round=1,
                    sha256_ct=b"\x00" * 31,
                )
            ])


# ---------- compute_excluded_miners_hash ----------


class TestExcludedHash:
    def test_deterministic(self):
        recs = [
            ExcludedMinerRecord(miner_hotkey=os.urandom(32), miner_uid=i, reason="r")
            for i in range(5)
        ]
        a = compute_excluded_miners_hash(recs)
        b = compute_excluded_miners_hash(recs)
        assert a == b

    def test_order_independent(self):
        recs = [
            ExcludedMinerRecord(miner_hotkey=os.urandom(32), miner_uid=i, reason="r")
            for i in range(5)
        ]
        a = compute_excluded_miners_hash(recs)
        b = compute_excluded_miners_hash(list(reversed(recs)))
        assert a == b

    def test_empty_list_yields_canonical_hash(self):
        h1 = compute_excluded_miners_hash([])
        h2 = compute_excluded_miners_hash([])
        assert h1 == h2
        assert len(h1) == 32

    def test_reason_change_changes_hash(self):
        rec = ExcludedMinerRecord(
            miner_hotkey=b"\x42" * 32, miner_uid=1, reason="reason_a"
        )
        rec2 = ExcludedMinerRecord(
            miner_hotkey=b"\x42" * 32, miner_uid=1, reason="reason_b"
        )
        assert compute_excluded_miners_hash([rec]) != compute_excluded_miners_hash([rec2])


# ---------- build_pre_scoring_state ----------


class TestBuildPreScoringState:
    def test_basic_structure(self, val_key, val_pk, miner_records):
        excluded = [
            ExcludedMinerRecord(miner_hotkey=os.urandom(32), miner_uid=i, reason="inner_sig.fail")
            for i in range(3)
        ]
        blob = build_pre_scoring_state(
            validator_hotkey=val_pk,
            validator_signing_key=val_key,
            epoch_id="EPOCH-A",
            epoch_idx=42,
            outcomes_release_round=12345600,
            outcomes_fetched_at_round=12345650,
            miner_commits=miner_records,
            excluded_miners=excluded,
        )
        decoded = canonical_cbor_loads(blob)
        assert decoded["v"] == SCORING_STATE_VERSION
        assert decoded["validator_hotkey"] == val_pk
        assert decoded["epoch_id"] == "EPOCH-A"
        assert decoded["epoch_idx"] == 42
        assert decoded["n_miners"] == 20
        assert decoded["n_excluded"] == 3
        assert "inner_sig" in decoded
        assert verify_inner_sig(decoded, val_pk, hotkey_field="validator_hotkey")

    def test_canonical_round_trip(self, val_key, val_pk, miner_records):
        blob = build_pre_scoring_state(
            validator_hotkey=val_pk,
            validator_signing_key=val_key,
            epoch_id="EPOCH-A",
            epoch_idx=1,
            outcomes_release_round=100,
            outcomes_fetched_at_round=110,
            miner_commits=miner_records,
            excluded_miners=[],
        )
        re_encoded = canonical_cbor_dumps(canonical_cbor_loads(blob))
        assert re_encoded == blob

    def test_fits_under_tle_budget(self, val_key, val_pk):
        # 200 miners + 20 exclusions — bigger than realistic SN21 population.
        miners = [
            MinerCommitRecord(
                miner_hotkey=os.urandom(32),
                k_block=7038900 + i,
                k_round=12345700 + i,
                sha256_ct=os.urandom(32),
            )
            for i in range(200)
        ]
        excluded = [
            ExcludedMinerRecord(miner_hotkey=os.urandom(32), miner_uid=i, reason="r")
            for i in range(20)
        ]
        blob = build_pre_scoring_state(
            validator_hotkey=val_pk,
            validator_signing_key=val_key,
            epoch_id="EPOCH-A",
            epoch_idx=1,
            outcomes_release_round=100,
            outcomes_fetched_at_round=110,
            miner_commits=miners,
            excluded_miners=excluded,
        )
        assert len(blob) <= MAX_TLE_PLAINTEXT_BYTES

    def test_invalid_validator_hotkey_length(self, val_key, miner_records):
        with pytest.raises(ValueError, match="32 bytes"):
            build_pre_scoring_state(
                validator_hotkey=b"\x00" * 31,
                validator_signing_key=val_key,
                epoch_id="EP",
                epoch_idx=0,
                outcomes_release_round=1,
                outcomes_fetched_at_round=1,
                miner_commits=miner_records,
                excluded_miners=[],
            )


# ---------- compute_final_score_root + build_post_scoring_artifacts ----------


class TestPostScoring:
    def test_final_score_root_deterministic(self):
        recs = [
            ScoredMinerRecord(miner_hotkey=os.urandom(32), score_micro=500_000 + i)
            for i in range(10)
        ]
        a = compute_final_score_root(recs)
        b = compute_final_score_root(list(reversed(recs)))
        assert a == b
        assert len(a) == 32

    def test_score_must_be_non_negative(self):
        with pytest.raises(ValueError, match="non-negative"):
            compute_final_score_root([
                ScoredMinerRecord(miner_hotkey=os.urandom(32), score_micro=-1)
            ])

    def test_build_post_scoring_round_trip(self, val_key, val_pk):
        scored = [
            ScoredMinerRecord(miner_hotkey=os.urandom(32), score_micro=500_000 + i)
            for i in range(20)
        ]
        blob = build_post_scoring_artifacts(
            validator_hotkey=val_pk,
            validator_signing_key=val_key,
            epoch_id="EPOCH-A",
            epoch_idx=42,
            scoring_inputs_hash=os.urandom(32),
            scored_miners=scored,
            weights_commit_block_hash=os.urandom(32),
            weights_reveal_round=12345800,
        )
        decoded = canonical_cbor_loads(blob)
        assert decoded["v"] == SCORING_STATE_VERSION
        assert decoded["validator_hotkey"] == val_pk
        assert decoded["n_miners_scored"] == 20
        assert verify_inner_sig(decoded, val_pk, hotkey_field="validator_hotkey")
        assert canonical_cbor_dumps(decoded) == blob

    def test_post_scoring_fits_budget(self, val_key, val_pk):
        scored = [
            ScoredMinerRecord(miner_hotkey=os.urandom(32), score_micro=500_000 + i)
            for i in range(200)
        ]
        blob = build_post_scoring_artifacts(
            validator_hotkey=val_pk,
            validator_signing_key=val_key,
            epoch_id="EPOCH-A",
            epoch_idx=1,
            scoring_inputs_hash=os.urandom(32),
            scored_miners=scored,
            weights_commit_block_hash=os.urandom(32),
            weights_reveal_round=12345800,
        )
        assert len(blob) <= MAX_TLE_PLAINTEXT_BYTES

    def test_invalid_scoring_hash_length(self, val_key, val_pk):
        with pytest.raises(ValueError, match="32 bytes"):
            build_post_scoring_artifacts(
                validator_hotkey=val_pk,
                validator_signing_key=val_key,
                epoch_id="EP",
                epoch_idx=0,
                scoring_inputs_hash=b"\x00" * 31,
                scored_miners=[],
                weights_commit_block_hash=os.urandom(32),
                weights_reveal_round=1,
            )

    def test_invalid_block_hash_length(self, val_key, val_pk):
        with pytest.raises(ValueError, match="32 bytes"):
            build_post_scoring_artifacts(
                validator_hotkey=val_pk,
                validator_signing_key=val_key,
                epoch_id="EP",
                epoch_idx=0,
                scoring_inputs_hash=os.urandom(32),
                scored_miners=[],
                weights_commit_block_hash=b"\x00" * 31,
                weights_reveal_round=1,
            )

    def test_inner_sig_does_not_verify_for_other_validator(self, val_key, val_pk):
        blob = build_post_scoring_artifacts(
            validator_hotkey=val_pk,
            validator_signing_key=val_key,
            epoch_id="EP",
            epoch_idx=0,
            scoring_inputs_hash=os.urandom(32),
            scored_miners=[],
            weights_commit_block_hash=os.urandom(32),
            weights_reveal_round=1,
        )
        decoded = canonical_cbor_loads(blob)
        # Try to verify against a different hotkey → must fail.
        wrong = b"\x99" * 32
        assert not verify_inner_sig(decoded, wrong, hotkey_field="validator_hotkey")
