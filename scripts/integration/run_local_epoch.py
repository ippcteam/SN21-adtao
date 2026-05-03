"""Local end-to-end integration harness for the SN21 verifiable-scoring protocol.

Runs ONE complete epoch in-process — no Bittensor node, no external archive,
no network. Useful for:

  - smoke-testing every layer's wiring after a refactor;
  - giving a human a single command that exercises HOPE → miners → validator →
    verifier and prints a green/red summary;
  - reproducing exactly-once-and-deterministically in CI before live runs.

Pipeline:

  1. Generate ed25519 keys for: HOPE outcome signer, primary validator,
     shadow validator, N miners.
  2. HOPE 9.A.1: build release_commit; pretend-publish (no chain) and remember
     digest + plaintext.
  3. Each miner builds + AES-encrypts a Layer 9.B prediction; uploads AES_ct
     to an in-process archive; pretend-submits chain commits (we record the
     state directly into a `MinerOnChainInputs` list).
  4. HOPE 9.A.2: builds reveal blob with measured outcomes; pretend-publishes.
  5. Validator runs `run_epoch_scoring(scorer=...)` with mocked chain helpers
     so it logs what would be committed.
  6. Shadow validator runs the same.
  7. Verifier runs `verify_epoch(...)` against the captured 9.C.1 / 9.C.2
     blobs and asserts both roots match.

Run:
    python -m scripts.integration.run_local_epoch
"""

from __future__ import annotations

import hashlib
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

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
from hope.hope_outcomes.release_commit import (
    EpisodeRef,
    build_release_commit_plaintext,
    compute_release_commit_digest,
)
from hope.hope_outcomes.reveal_blob import (
    EpisodeOutcome,
    HorizonOutcomeMeasured,
    build_reveal_blob,
    compute_reveal_blob_sha256,
    verify_reveal_blob,
)
from hope.hope_shadow_validator.runner import run_shadow_epoch
from hope.scoring.onchain_adapter import (
    aggregate_outcomes_to_truth,
    compute_scoring_inputs_hash,
    make_scorer,
)
from hope.validator.onchain_runner import (
    MinerOnChainInputs,
    run_epoch_scoring,
)
from hope.validator.weights_commit import WeightsCommitResult

import verify_epoch as ve  # noqa: E402


N_MINERS = 4
N_EPISODES = 6
EPOCH_ID = "EPOCH-LOCAL-01"
EPOCH_IDX = 1


@dataclass
class _LocalArchive:
    """In-process archive — keyed by sha256 of stored AES_ct."""

    _data: dict[bytes, bytes]

    @classmethod
    def empty(cls) -> "_LocalArchive":
        return cls(_data={})

    def store(self, sha256: bytes, ct: bytes) -> None:
        self._data[sha256] = ct

    def fetch_first_match(self, endpoints, *, epoch_id, miner_identity, expected_sha256):
        ct = self._data.get(expected_sha256)
        if ct is None:
            return FetchAggregate(aes_ct=None, winner=None, attempts=[])
        winner = FetchResult(
            endpoint=endpoints[0], ok=True, aes_ct=ct, sha256_match=True,
            status_code=200, elapsed_ms=1,
        )
        return FetchAggregate(aes_ct=ct, winner=winner, attempts=[winner])


def _ok_commit(block: int, reveal_round: int | None = None) -> CommitResult:
    return CommitResult(
        success=True, message="OK", block_number=block,
        extrinsic_hash="0x" + "ab" * 32, reveal_round=reveal_round,
    )


def _generate_episodes() -> list[EpisodeRef]:
    return [
        EpisodeRef(
            episode_id=f"EP-{i:03d}",
            query_cbor=hashlib.sha256(f"q{i}".encode()).digest(),
        )
        for i in range(N_EPISODES)
    ]


def _build_release_commit(signer_sk: Ed25519PrivateKey, episodes: list[EpisodeRef]):
    pk = signer_sk.public_key().public_bytes_raw()
    plain = build_release_commit_plaintext(
        outcome_signer_hotkey=pk,
        outcome_signer_signing_key=signer_sk,
        epoch_id=EPOCH_ID,
        epoch_idx=EPOCH_IDX,
        release_round=12345600,
        deadline_round=12345700,
        horizons=["7", "14"],
        episodes=episodes,
        scoring_metadata_hash=hashlib.sha256(b"scoring-meta-v1").digest(),
    )
    return plain, compute_release_commit_digest(plain)


def _build_reveal_blob(signer_sk: Ed25519PrivateKey, plain_9a1: bytes,
                       episodes: list[EpisodeRef]):
    pk = signer_sk.public_key().public_bytes_raw()
    measured = []
    for i, ep in enumerate(episodes):
        # Half the episodes are slight cost-savers; the other half slight cost-overruns.
        cost_pct = -2.5 if i % 2 == 0 else 1.7
        conv_pct = 5.2 if i % 2 == 0 else -1.0
        eff_pct = 4.0 if i % 2 == 0 else -2.0
        measured.append(EpisodeOutcome(
            episode_id=ep.episode_id,
            salt=os.urandom(16),
            outcomes=[
                HorizonOutcomeMeasured(
                    horizon="7", cost_delta_pct=cost_pct,
                    conversions_delta_pct=conv_pct,
                    efficiency_delta_pct=eff_pct, goal_miss=0,
                ),
                HorizonOutcomeMeasured(
                    horizon="14", cost_delta_pct=cost_pct * 1.5,
                    conversions_delta_pct=conv_pct * 1.2,
                    efficiency_delta_pct=eff_pct * 1.1, goal_miss=0,
                ),
            ],
        ))
    blob = build_reveal_blob(
        outcome_signer_hotkey=pk,
        outcome_signer_signing_key=signer_sk,
        epoch_id=EPOCH_ID,
        epoch_idx=EPOCH_IDX,
        release_commit_plaintext_sha256=hashlib.sha256(plain_9a1).digest(),
        deadline_round=12345700,
        measured_at_round=12346000,
        horizons=["7", "14"],
        episodes=measured,
    )
    return blob, measured


def _build_miner_submission(epoch_id: str, miner_uid: int, block: int):
    sk = Ed25519PrivateKey.generate()
    pk = sk.public_key().public_bytes_raw()
    horizons = [
        build_horizon_entry(
            "7",
            (-3.5 + miner_uid * 0.2, -2.0 + miner_uid * 0.2, -0.5 + miner_uid * 0.2),
            (4.5, 5.0, 5.5),
            (3.5, 4.0, 4.5),
            0.10,
            0.05,
        ),
        build_horizon_entry(
            "14",
            (-5.0 + miner_uid * 0.2, -3.0 + miner_uid * 0.2, -1.0 + miner_uid * 0.2),
            (5.5, 6.0, 6.5),
            (4.0, 4.4, 5.0),
            0.12,
            0.07,
        ),
    ]
    plain = build_prediction_plaintext(
        epoch_id=epoch_id, miner_hotkey=pk, submitted_round=12345650 + miner_uid,
        horizons=horizons, miner_signing_key=sk,
    )
    enc = encrypt_prediction(plain, epoch_id=epoch_id)
    sha_ct = hashlib.sha256(enc.aes_ct).digest()
    return MinerOnChainInputs(
        miner_uid=miner_uid,
        miner_hotkey=pk,
        revealed_k=enc.aes_key,
        sha256_ct_commit=sha_ct,
        self_archive_url=f"https://m{miner_uid}.example/archive/{epoch_id}",
        chain_block_at_k_commit=block,
        k_reveal_round=12345700 + miner_uid,
    ), enc.aes_ct


def main() -> int:
    print(f"== run_local_epoch — epoch_id={EPOCH_ID} miners={N_MINERS} episodes={N_EPISODES}")

    # ---- 9.A.1 release commit ----
    signer_sk = Ed25519PrivateKey.generate()
    episodes = _generate_episodes()
    plain_9a1, digest_9a1 = _build_release_commit(signer_sk, episodes)
    print(f"   9.A.1 plaintext={len(plain_9a1)}B digest={digest_9a1.hex()[:16]}...")

    # ---- 9.B miner submissions ----
    archive = _LocalArchive.empty()
    miner_inputs: list[MinerOnChainInputs] = []
    for i in range(N_MINERS):
        inp, ct = _build_miner_submission(EPOCH_ID, miner_uid=i, block=7038900 + i)
        archive.store(inp.sha256_ct_commit, ct)
        miner_inputs.append(inp)
    print(f"   9.B {N_MINERS} miners encrypted+archived (avg ct={sum(len(c) for c in archive._data.values()) // N_MINERS}B)")

    # ---- 9.A.2 reveal blob ----
    blob_9a2, measured = _build_reveal_blob(signer_sk, plain_9a1, episodes)
    sha_9a2 = compute_reveal_blob_sha256(blob_9a2)
    print(f"   9.A.2 reveal blob {len(blob_9a2)}B sha256={sha_9a2.hex()[:16]}... verify={verify_reveal_blob(blob_9a2)}")

    # ---- ground truth + scorer ----
    ep_outcomes = [
        {h.horizon: {
            "cost_delta_pct": h.cost_delta_pct,
            "conversions_delta_pct": h.conversions_delta_pct,
            "efficiency_delta_pct": h.efficiency_delta_pct,
            "goal_miss": h.goal_miss,
        } for h in m.outcomes}
        for m in measured
    ]
    truth = aggregate_outcomes_to_truth(ep_outcomes)
    print(f"   truth horizons: {sorted(truth.keys())}")
    scorer = make_scorer(truth)

    # ---- 9.C primary validator ----
    val_sk = Ed25519PrivateKey.generate()
    val_pk = val_sk.public_key().public_bytes_raw()
    timing = TimingBounds(
        epoch_open_round=12345600, miner_deadline_round=12346000,
        chain_window_min_block=7038000, chain_window_max_block=7039000,
    )
    captured_primary: dict[str, Any] = {}

    def cap_pre_primary(**kwargs):
        captured_primary["pre"] = kwargs["pre_scoring_state_cbor"]
        return _ok_commit(7038910, reveal_round=12345710)

    def cap_post_primary(**kwargs):
        captured_primary["post"] = kwargs["post_scoring_artifacts_cbor"]
        return _ok_commit(7038930, reveal_round=12345730)

    primary_block_hash = os.urandom(32)
    plaintexts_pre_score = {
        inp.miner_hotkey: {"placeholder": True} for inp in miner_inputs
    }
    primary_scoring_hash = compute_scoring_inputs_hash(
        epoch_id=EPOCH_ID, plaintexts=plaintexts_pre_score, truth_by_horizon=truth,
    )
    with (
        patch(
            "hope.validator.onchain_runner.submit_pre_scoring_state_layer_9c1",
            side_effect=cap_pre_primary,
        ),
        patch(
            "hope.validator.onchain_runner.commit_weights_layer_9c3",
            return_value=WeightsCommitResult(
                success=True, message="OK", block_number=7038920,
                block_hash=primary_block_hash, extrinsic_hash="0x" + "cd" * 32,
            ),
        ),
        patch(
            "hope.validator.onchain_runner.submit_post_scoring_artifacts_layer_9c2",
            side_effect=cap_post_primary,
        ),
    ):
        primary_outcome = run_epoch_scoring(
            subtensor=object(), validator_wallet=object(), netuid=21,
            epoch_id=EPOCH_ID, epoch_idx=EPOCH_IDX,
            validator_hotkey=val_pk, validator_signing_key=val_sk,
            miner_inputs=miner_inputs, archive_endpoints=[
                ArchiveEndpoint(tier=2, base_url="https://hope")
            ],
            archive_client=archive, timing=timing,
            outcomes_release_round=12500, outcomes_fetched_at_round=12550,
            scoring_inputs_hash=primary_scoring_hash, scorer=scorer,
            blocks_until_pre_scoring_reveal=300,
            blocks_until_post_scoring_reveal=600,
            blocks_until_weights_reveal=360,
        )
    print(f"   primary outcome ok={primary_outcome.ok} scored={len(primary_outcome.score_map)}")

    # ---- 9.E shadow validator ----
    shadow_sk = Ed25519PrivateKey.generate()
    shadow_pk = shadow_sk.public_key().public_bytes_raw()
    captured_shadow: dict[str, Any] = {}

    def cap_pre_shadow(**kwargs):
        captured_shadow["pre"] = kwargs["pre_scoring_state_cbor"]
        return _ok_commit(7038910)

    def cap_post_shadow(**kwargs):
        captured_shadow["post"] = kwargs["post_scoring_artifacts_cbor"]
        return _ok_commit(7038930)

    with (
        patch(
            "hope.validator.onchain_runner.submit_pre_scoring_state_layer_9c1",
            side_effect=cap_pre_shadow,
        ),
        patch(
            "hope.validator.onchain_runner.commit_weights_layer_9c3",
            return_value=WeightsCommitResult(
                success=True, message="OK", block_number=7038921,
                block_hash=os.urandom(32), extrinsic_hash="0x" + "cd" * 32,
            ),
        ),
        patch(
            "hope.validator.onchain_runner.submit_post_scoring_artifacts_layer_9c2",
            side_effect=cap_post_shadow,
        ),
    ):
        shadow_outcome = run_shadow_epoch(
            subtensor=object(), shadow_wallet=object(),
            shadow_hotkey=shadow_pk, shadow_signing_key=shadow_sk,
            netuid=21, epoch_id=EPOCH_ID, epoch_idx=EPOCH_IDX,
            miner_inputs=miner_inputs,
            archive_endpoints=[ArchiveEndpoint(tier=2, base_url="https://hope")],
            archive_client=archive, timing=timing,
            outcomes_release_round=12500, outcomes_fetched_at_round=12550,
            scoring_inputs_hash=primary_scoring_hash, scorer=scorer,
            blocks_until_pre_scoring_reveal=300,
            blocks_until_post_scoring_reveal=600,
            blocks_until_weights_reveal=360,
        )
    print(f"   shadow  outcome ok={shadow_outcome.ok}")

    # ---- public verification ----
    miner_states = {
        inp.miner_hotkey: ve.ChainMinerState(
            miner_uid=inp.miner_uid,
            timelock_k_revealed=inp.revealed_k,
            sha256_ct_commit=inp.sha256_ct_commit,
            self_archive_url=inp.self_archive_url,
            chain_block_at_k_commit=inp.chain_block_at_k_commit,
            k_reveal_round=inp.k_reveal_round,
        )
        for inp in miner_inputs
    }
    chain_view_primary = ve.ChainView(
        pre_scoring_state_cbor=captured_primary["pre"],
        post_scoring_artifacts_cbor=captured_primary["post"],
        miner_states=miner_states,
        timing=timing,
    )
    primary_verdict = ve.verify_epoch(
        chain_view=chain_view_primary,
        epoch_id=EPOCH_ID,
        validator_hotkey=val_pk,
        archive_endpoints=[ArchiveEndpoint(tier=2, base_url="https://hope")],
        archive_client=archive,
        scorer=scorer,
    )

    chain_view_shadow = ve.ChainView(
        pre_scoring_state_cbor=captured_shadow["pre"],
        post_scoring_artifacts_cbor=captured_shadow["post"],
        miner_states=miner_states,
        timing=timing,
    )
    shadow_verdict = ve.verify_epoch(
        chain_view=chain_view_shadow,
        epoch_id=EPOCH_ID,
        validator_hotkey=shadow_pk,
        archive_endpoints=[ArchiveEndpoint(tier=2, base_url="https://hope")],
        archive_client=archive,
        scorer=scorer,
    )

    print()
    print("== VERIFIER RESULTS ==")
    print(f"primary verdict: ok={primary_verdict.ok} miner_root_match={primary_verdict.miner_commits_match} score_root_match={primary_verdict.final_score_match}")
    print(f"shadow  verdict: ok={shadow_verdict.ok}  miner_root_match={shadow_verdict.miner_commits_match}  score_root_match={shadow_verdict.final_score_match}")

    # The two roots SHOULD match each other across primary + shadow because
    # both used the same miner_inputs and the same scorer.
    pre_pri = canonical_cbor_loads(captured_primary["pre"])
    pre_sha = canonical_cbor_loads(captured_shadow["pre"])
    miner_root_match = pre_pri["miner_commits_root"] == pre_sha["miner_commits_root"]
    print(f"shadow vs primary: miner_commits_root identical = {miner_root_match}")

    return 0 if (primary_verdict.ok and shadow_verdict.ok and miner_root_match) else 1


if __name__ == "__main__":
    sys.exit(main())
