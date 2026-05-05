"""Phase #1: live scorer wiring in scripts/verify_epoch.py.

Asserts that:

* ``make_live_scorer`` produces a scorer that uses the production
  ``score_one_miner`` adapter, not a placeholder.
* ``_load_truth_file`` round-trips the documented JSON schema.
* The recorded-epoch fixture pairs with a synthesised chain view to
  produce ``ok=True`` end-to-end (a regression here would mean the
  verifier silently fell back to the placeholder).
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

from hope.commitment.archives import ArchiveEndpoint, FetchAggregate, FetchResult
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
)
from hope.scoring.onchain_adapter import HorizonTruth, score_one_miner

import verify_epoch as ve


FIXTURE_PATH = Path(__file__).resolve().parents[2] / "fixtures" / "recorded_epoch" / "recorded_epoch.json"


class _InMemArchive:
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


def test_load_truth_file_decodes_recorded_fixture():
    truth = ve._load_truth_file(str(FIXTURE_PATH))
    assert "7" in truth and "14" in truth
    assert isinstance(truth["7"], HorizonTruth)
    assert truth["7"].goal_miss_freq_ppm == 500_000
    assert truth["14"].instab_freq_ppm == 200_000


def test_make_live_scorer_uses_production_scorer():
    """make_live_scorer must invoke score_one_miner — not return zeros."""
    truth = ve._load_truth_file(str(FIXTURE_PATH))
    scorer = ve.make_live_scorer(truth)

    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key().public_bytes_raw()
    horizons = [
        build_horizon_entry("7", (-1.0, 0.0, 1.0), (-1.0, 0.0, 1.0), (-1.0, 0.0, 1.0), 0.5, 0.2),
        build_horizon_entry("14", (-1.0, 0.0, 1.0), (-1.0, 0.0, 1.0), (-1.0, 0.0, 1.0), 0.5, 0.2),
    ]
    plaintext = build_prediction_plaintext(
        epoch_id="RECORDED-EPOCH-1", miner_hotkey=pk, submitted_round=12345,
        horizons=horizons, miner_signing_key=sk,
    )

    expected = score_one_miner(plaintext, truth)
    actual = scorer("RECORDED-EPOCH-1", {pk: plaintext})
    assert actual == {pk: expected}
    # And the score is non-zero — i.e. real scoring happened.
    assert actual[pk] > 0


def test_recorded_epoch_passes_end_to_end():
    """A complete chain view + the recorded truth file must yield ok=True.

    Regression guard: if the verifier ever falls back to the placeholder
    (return-empty) scorer, the chain final_score_root won't match the
    derived one and this test fails.
    """
    truth = ve._load_truth_file(str(FIXTURE_PATH))
    scorer = ve.make_live_scorer(truth)

    epoch_id = "RECORDED-EPOCH-1"
    val_sk = Ed25519PrivateKey.generate()
    val_pk = val_sk.public_key().public_bytes_raw()
    timing = TimingBounds(
        epoch_open_round=10000, miner_deadline_round=20000,
        chain_window_min_block=7000000, chain_window_max_block=7100000,
    )
    archive = _InMemArchive()
    miner_states: dict[bytes, ve.ChainMinerState] = {}
    miner_records: list[MinerCommitRecord] = []
    plaintexts: dict[bytes, dict] = {}

    for i in range(5):
        sk = Ed25519PrivateKey.generate()
        pk = sk.public_key().public_bytes_raw()
        horizons = [
            build_horizon_entry(
                "7",
                (-2.0 + i, 0.0 + i, 2.0 + i),
                (-1.5, 0.0, 1.5),
                (-1.0, 0.0, 1.0),
                0.5, 0.2,
            ),
            build_horizon_entry(
                "14",
                (-3.0, 0.0, 3.0),
                (-2.0, 0.0, 2.0),
                (-1.5, 0.0, 1.5),
                0.5, 0.2,
            ),
        ]
        plaintext = build_prediction_plaintext(
            epoch_id=epoch_id, miner_hotkey=pk, submitted_round=12345,
            horizons=horizons, miner_signing_key=sk,
        )
        enc = encrypt_prediction(plaintext, epoch_id=epoch_id)
        sha_ct = hashlib.sha256(enc.aes_ct).digest()
        archive.store(sha_ct, enc.aes_ct)
        block = 7038900 + i
        miner_states[pk] = ve.ChainMinerState(
            miner_uid=i,
            timelock_k_revealed=enc.aes_key,
            sha256_ct_commit=sha_ct,
            self_archive_url="https://m/x",
            chain_block_at_k_commit=block,
        )
        miner_records.append(MinerCommitRecord(
            miner_hotkey=pk, k_block=block, k_round=0, sha256_ct=sha_ct,
        ))
        plaintexts[pk] = plaintext

    # The validator that committed the chain artifact ran the same live
    # scorer the verifier now runs — score every miner with `score_one_miner`.
    score_map = {pk: score_one_miner(pl, truth) for pk, pl in plaintexts.items()}
    scored_records = [
        ScoredMinerRecord(miner_hotkey=pk, score_micro=int(score))
        for pk, score in score_map.items()
    ]

    pre = build_pre_scoring_state(
        validator_hotkey=val_pk,
        validator_signing_key=val_sk,
        epoch_id=epoch_id,
        epoch_idx=42,
        outcomes_release_round=12000,
        outcomes_fetched_at_round=12010,
        miner_commits=miner_records,
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
    endpoints = [ArchiveEndpoint(tier=2, base_url="https://operator-shadow.example.io")]
    verdict = ve.verify_epoch(
        chain_view=chain_view, epoch_id=epoch_id, validator_hotkey=val_pk,
        archive_endpoints=endpoints, archive_client=archive, scorer=scorer,
    )
    assert verdict.ok, (
        f"recorded-epoch fixture must pass: "
        f"miner_commits_match={verdict.miner_commits_match}, "
        f"final_score_match={verdict.final_score_match}, "
        f"inner_sig_pre={verdict.inner_sig_valid_pre}, "
        f"inner_sig_post={verdict.inner_sig_valid_post}"
    )
    assert verdict.final_score_match
    assert verdict.miner_commits_match


def test_placeholder_scorer_is_no_longer_referenced():
    """Regression: the placeholder must not be present in verify_epoch.py main.

    A future refactor that re-introduces a placeholder would break the
    verifier's launch claim; this test pins the file's invariant."""
    src = (Path(__file__).resolve().parents[3] / "scripts" / "verify_epoch.py").read_text()
    assert "_placeholder_scorer" not in src
    assert "make_live_scorer" in src
