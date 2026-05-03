"""End-to-end verifier tests for scripts/verify_epoch.py — deterministic, no chain.

We synthesise a complete epoch from validator + miner perspectives, store
ciphertexts in an in-memory archive, build the on-chain CBOR plaintexts, and
assert that the verifier reproduces both IMT roots byte-for-byte.

If the validator's `final_score_root` is altered (simulating a malicious
mid-flight rewrite), the verifier MUST flag the mismatch.
"""

from __future__ import annotations

import hashlib
import os
import sys
from pathlib import Path
from typing import Optional

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# Make scripts/ importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from hope.commitment.archives import ArchiveEndpoint, FetchAggregate, FetchResult
from hope.commitment.canonical import canonical_cbor_loads
from hope.commitment.prediction_payload import (
    build_horizon_entry,
    build_prediction_plaintext,
    encrypt_prediction,
)
from hope.commitment.scoreability import TimingBounds
from hope.commitment.scoring_state import (
    MinerCommitRecord,
    ScoredMinerRecord,
    build_post_scoring_artifacts,
    build_pre_scoring_state,
    compute_final_score_root,
    compute_miner_commits_root,
)

import verify_epoch as ve


class InMemArchive:
    """Returns a FetchAggregate for any expected_sha256 we have stored."""

    def __init__(self):
        self._store: dict[bytes, bytes] = {}

    def store(self, sha256_digest: bytes, aes_ct: bytes) -> None:
        self._store[sha256_digest] = aes_ct

    def fetch_first_match(self, endpoints, *, epoch_id, miner_identity, expected_sha256):
        ct = self._store.get(expected_sha256)
        if ct is None:
            return FetchAggregate(aes_ct=None, winner=None, attempts=[])
        winner = FetchResult(
            endpoint=endpoints[0], ok=True, aes_ct=ct, sha256_match=True,
            status_code=200, elapsed_ms=1,
        )
        return FetchAggregate(aes_ct=ct, winner=winner, attempts=[winner])


def _make_miner(epoch_id: str):
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key().public_bytes_raw()
    horizons = [
        build_horizon_entry("7", (-1.0, 0.0, 1.0), (-1.0, 0.0, 1.0), (-1.0, 0.0, 1.0), 0.1, 0.05),
    ]
    plain = build_prediction_plaintext(
        epoch_id=epoch_id, miner_hotkey=pk, submitted_round=12345,
        horizons=horizons, miner_signing_key=sk,
    )
    enc = encrypt_prediction(plain, epoch_id=epoch_id)
    sha_ct = hashlib.sha256(enc.aes_ct).digest()
    return sk, pk, plain, enc, sha_ct


def _deterministic_scorer(score_value: int):
    """Returns a closure that maps each miner hotkey → score_value (constant)."""
    def scorer(epoch_id, plaintext_per_miner):
        return {hk: score_value for hk in plaintext_per_miner}
    return scorer


@pytest.fixture
def epoch_setup():
    epoch_id = "EPOCH-VTEST-1"
    val_sk = Ed25519PrivateKey.generate()
    val_pk = val_sk.public_key().public_bytes_raw()
    timing = TimingBounds(
        epoch_open_round=10000, miner_deadline_round=20000,
        chain_window_min_block=7000000, chain_window_max_block=7100000,
    )

    miners = []
    archive = InMemArchive()
    miner_states: dict[bytes, ve.ChainMinerState] = {}
    miner_commit_records: list[MinerCommitRecord] = []
    scored_records: list[ScoredMinerRecord] = []
    plaintext_by_pk: dict[bytes, dict] = {}

    for i in range(8):
        sk, pk, plain, enc, sha_ct = _make_miner(epoch_id)
        archive.store(sha_ct, enc.aes_ct)
        block = 7038900 + i
        miner_states[pk] = ve.ChainMinerState(
            miner_uid=i,
            timelock_k_revealed=enc.aes_key,
            sha256_ct_commit=sha_ct,
            self_archive_url="https://m/x",
            chain_block_at_k_commit=block,
        )
        miner_commit_records.append(MinerCommitRecord(
            miner_hotkey=pk, k_block=block, k_round=0, sha256_ct=sha_ct,
        ))
        plaintext_by_pk[pk] = plain

    # Score every miner with a deterministic constant.
    for pk in plaintext_by_pk:
        scored_records.append(ScoredMinerRecord(miner_hotkey=pk, score_micro=500_000))

    pre = build_pre_scoring_state(
        validator_hotkey=val_pk,
        validator_signing_key=val_sk,
        epoch_id=epoch_id,
        epoch_idx=42,
        outcomes_release_round=12000,
        outcomes_fetched_at_round=12010,
        miner_commits=miner_commit_records,
        excluded_miners=[],
    )
    post = build_post_scoring_artifacts(
        validator_hotkey=val_pk,
        validator_signing_key=val_sk,
        epoch_id=epoch_id,
        epoch_idx=42,
        scoring_inputs_hash=os.urandom(32),
        scored_miners=scored_records,
        weights_commit_block_hash=os.urandom(32),
        weights_reveal_round=15000,
    )

    chain_view = ve.ChainView(
        pre_scoring_state_cbor=pre,
        post_scoring_artifacts_cbor=post,
        miner_states=miner_states,
        timing=timing,
    )

    endpoints = [ArchiveEndpoint(tier=2, base_url="https://hope-shadow")]

    return {
        "epoch_id": epoch_id,
        "val_pk": val_pk,
        "chain_view": chain_view,
        "archive": archive,
        "endpoints": endpoints,
    }


class TestVerifyEpoch:
    def test_honest_validator_passes(self, epoch_setup):
        verdict = ve.verify_epoch(
            chain_view=epoch_setup["chain_view"],
            epoch_id=epoch_setup["epoch_id"],
            validator_hotkey=epoch_setup["val_pk"],
            archive_endpoints=epoch_setup["endpoints"],
            archive_client=epoch_setup["archive"],
            scorer=_deterministic_scorer(500_000),
        )
        assert verdict.ok
        assert verdict.miner_commits_match
        assert verdict.final_score_match
        assert verdict.inner_sig_valid_pre
        assert verdict.inner_sig_valid_post
        assert verdict.n_miners_chain == 8
        assert verdict.n_miners_derived == 8

    def test_malicious_score_is_caught(self, epoch_setup):
        # Use a different scorer than what the validator committed.
        verdict = ve.verify_epoch(
            chain_view=epoch_setup["chain_view"],
            epoch_id=epoch_setup["epoch_id"],
            validator_hotkey=epoch_setup["val_pk"],
            archive_endpoints=epoch_setup["endpoints"],
            archive_client=epoch_setup["archive"],
            scorer=_deterministic_scorer(999_999),  # disagrees with chain's 500_000
        )
        assert not verdict.ok
        assert verdict.miner_commits_match  # commits root unaffected
        assert not verdict.final_score_match  # score root diverges

    def test_archive_loss_marks_miners_excluded(self, epoch_setup):
        # Empty archive — no miner ciphertext available.
        verdict = ve.verify_epoch(
            chain_view=epoch_setup["chain_view"],
            epoch_id=epoch_setup["epoch_id"],
            validator_hotkey=epoch_setup["val_pk"],
            archive_endpoints=epoch_setup["endpoints"],
            archive_client=InMemArchive(),
            scorer=_deterministic_scorer(500_000),
        )
        # Commits root still derives from chain alone (not archives) — passes.
        assert verdict.miner_commits_match
        # No prediction enters scorer → derived score map empty → root differs.
        assert not verdict.final_score_match
        assert verdict.excluded_count == 8

    def test_wrong_validator_hotkey_invalidates_inner_sig(self, epoch_setup):
        wrong = b"\x99" * 32
        verdict = ve.verify_epoch(
            chain_view=epoch_setup["chain_view"],
            epoch_id=epoch_setup["epoch_id"],
            validator_hotkey=wrong,
            archive_endpoints=epoch_setup["endpoints"],
            archive_client=epoch_setup["archive"],
            scorer=_deterministic_scorer(500_000),
        )
        assert not verdict.ok
        assert not verdict.inner_sig_valid_pre
        assert not verdict.inner_sig_valid_post


class TestVerdictSerialisation:
    def test_json_round_trip(self, epoch_setup):
        verdict = ve.verify_epoch(
            chain_view=epoch_setup["chain_view"],
            epoch_id=epoch_setup["epoch_id"],
            validator_hotkey=epoch_setup["val_pk"],
            archive_endpoints=epoch_setup["endpoints"],
            archive_client=epoch_setup["archive"],
            scorer=_deterministic_scorer(500_000),
        )
        import json
        payload = json.loads(verdict.as_json())
        assert payload["ok"] is True
        assert payload["chain_final_score_root"] == verdict.chain_final_score_root.hex()
