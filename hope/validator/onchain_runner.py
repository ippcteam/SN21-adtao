"""Layer 9.C end-to-end orchestration for the validator.

This module is the validator-side counterpart to
`hope/miner/onchain_submitter.py`. Per epoch, after the miner deadline:

  1. Read each miner's three on-chain commits → (revealed_K, sha256_ct, url).
  2. Fetch AES_ct from archive tiers; skip miners whose archive failed.
  3. Run the 8-check scoreability rule per miner.
  4. Build the 9.C.1 `pre_scoring_state` CBOR and TLE-commit it on chain.
  5. Run the project's scoring on the accepted predictions.
  6. Submit weights via `commit_weights_layer_9c3` (Layer 9.C.3).
  7. Build the 9.C.2 `post_scoring_artifacts` CBOR (binding the weights
     commit's block_hash + reveal_round) and TLE-commit it on chain.
  8. If any miners were excluded for `plaintext_unavailable`, build and
     commit a 9.C.6 retry-log attestation.

The orchestration is split from the existing `ValidatorRunner` so the new
chain path can be unit-tested without the FastAPI server, the HTTP data
client, or the legacy WeightSetter.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.archives import ArchiveClient, ArchiveEndpoint
from hope.commitment.on_chain import (
    CommitResult,
    submit_post_scoring_artifacts_layer_9c2,
    submit_pre_scoring_state_layer_9c1,
    submit_retry_log_attestation_layer_9c6,
)
from hope.commitment.retry_log import (
    RetryLogAttempt,
    RetryLogMinerEntry,
    attempt_from_fetch_result,
    build_retry_log_blob,
    compute_retry_log_sha256,
)
from hope.commitment.scoreability import TimingBounds
from hope.commitment.scoring_state import (
    ExcludedMinerRecord,
    MinerCommitRecord,
    ScoredMinerRecord,
    build_post_scoring_artifacts,
    build_pre_scoring_state,
)
from hope.validator.onchain_reader import (
    MinerReadResult,
    assemble_chain_commits,
    read_miner_for_epoch,
)
from hope.validator.weights_commit import (
    WeightsCommitResult,
    commit_weights_layer_9c3,
    estimate_weights_reveal_round,
)

logger = logging.getLogger(__name__)


# Caller-supplied scorer signature: (epoch_id, plaintext_per_miner_hotkey)
# → {miner_hotkey_bytes: score_micro_uint}.
ScorerFn = Callable[[str, dict[bytes, dict[str, Any]]], dict[bytes, int]]


@dataclass
class MinerOnChainInputs:
    """Per-miner chain state assembled by the caller for one epoch.

    The runner is agnostic to HOW the caller obtained these — Phase C wires
    them from `subtensor.get_revealed_commitment_by_hotkey` etc., but tests
    can supply them directly.
    """

    miner_uid: int
    miner_hotkey: bytes  # 32-byte raw ed25519 pubkey
    revealed_k: Optional[bytes]
    sha256_ct_commit: Optional[bytes]
    self_archive_url: Optional[str]
    chain_block_at_k_commit: Optional[int]
    k_reveal_round: int  # drand round embedded in the chain TLE K commit


@dataclass
class EpochScoringOutcome:
    """Outcome of `run_epoch_scoring(...)` — every chain commit + per-miner read."""

    pre_scoring_commit: Optional[CommitResult] = None
    weights_commit: Optional[WeightsCommitResult] = None
    post_scoring_commit: Optional[CommitResult] = None
    retry_log_commit: Optional[CommitResult] = None
    retry_log_blob: Optional[bytes] = None
    miner_reads: list[MinerReadResult] = field(default_factory=list)
    score_map: dict[bytes, int] = field(default_factory=dict)
    aborted_reason: Optional[str] = None

    @property
    def ok(self) -> bool:
        return (
            self.aborted_reason is None
            and self.pre_scoring_commit is not None and self.pre_scoring_commit.success
            and self.weights_commit is not None and self.weights_commit.success
            and self.post_scoring_commit is not None and self.post_scoring_commit.success
        )


def run_epoch_scoring(
    *,
    subtensor,
    validator_wallet,
    netuid: int,
    epoch_id: str,
    epoch_idx: int,
    validator_hotkey: bytes,
    validator_signing_key: Ed25519PrivateKey,
    miner_inputs: list[MinerOnChainInputs],
    archive_endpoints: list[ArchiveEndpoint],
    archive_client: Optional[ArchiveClient],
    timing: TimingBounds,
    outcomes_release_round: int,
    outcomes_fetched_at_round: int,
    scoring_inputs_hash: bytes,
    scorer: ScorerFn,
    blocks_until_pre_scoring_reveal: int,
    blocks_until_post_scoring_reveal: int,
    blocks_until_weights_reveal: int,
) -> EpochScoringOutcome:
    """Run the full Layer 9.C orchestration for one validator-epoch.

    Args (selected):
        miner_inputs: per-miner chain state — caller already read chain.
        scorer: function the caller supplies (typically wrapping the
            project's `EpochScorer`) that maps accepted plaintexts to
            score_micro per miner. Same `scorer` shape used by
            `scripts/verify_epoch.py` so the verifier and the validator
            agree by construction.
        outcomes_release_round / outcomes_fetched_at_round: drand round at
            which the operator released outcomes (9.A.2) and when the validator
            fetched them. Both bind into 9.C.1.
        scoring_inputs_hash: 32-byte SHA-256 over the canonical-CBOR
            encoding of the validator's scoring inputs (predictions +
            outcomes + reference series). Bound into 9.C.2.
        blocks_until_pre/post/weights_reveal: TLE blocks-until-decrypt for
            each commit. The pre-scoring commit may reveal early so other
            validators / verifiers see it before the next epoch starts.

    Returns:
        `EpochScoringOutcome` with per-step commit results. `ok=True` only
        if all four commits succeeded (9.C.1, 9.C.3, 9.C.2, 9.C.6 if needed).
    """
    if archive_client is None:
        archive_client = ArchiveClient()

    # ---- 1. read each miner's chain triple + run scoreability ----
    miner_reads: list[MinerReadResult] = []
    score_inputs: dict[bytes, dict[str, Any]] = {}
    miner_commits: list[MinerCommitRecord] = []
    excluded: list[ExcludedMinerRecord] = []
    retry_entries: list[RetryLogMinerEntry] = []

    for inp in miner_inputs:
        cc = assemble_chain_commits(
            revealed_k_plaintext=inp.revealed_k,
            sha256_ct_commit=inp.sha256_ct_commit,
            self_archive_url=inp.self_archive_url,
            chain_block_at_k_commit=inp.chain_block_at_k_commit,
            miner_hotkey=inp.miner_hotkey,
        )
        result = read_miner_for_epoch(
            chain_commits=cc,
            archive_client=archive_client,
            archive_endpoints=archive_endpoints,
            epoch_id=epoch_id,
            timing=timing,
            miner_uid=inp.miner_uid,
            miner_identity_for_archive=inp.miner_hotkey.hex(),
        )
        miner_reads.append(result)

        # The 9.C.1 miner_commits_root covers EVERY miner whose K and
        # Sha256 commits landed in-window, regardless of scoring outcome.
        if inp.revealed_k is not None and inp.sha256_ct_commit is not None:
            miner_commits.append(MinerCommitRecord(
                miner_hotkey=inp.miner_hotkey,
                k_block=inp.chain_block_at_k_commit or 0,
                k_round=inp.k_reveal_round,
                sha256_ct=inp.sha256_ct_commit,
            ))

        if result.ok and result.plaintext is not None:
            score_inputs[inp.miner_hotkey] = result.plaintext
        else:
            reason = result.excluded_reason or "unknown"
            excluded.append(ExcludedMinerRecord(
                miner_hotkey=inp.miner_hotkey,
                miner_uid=inp.miner_uid,
                reason=reason,
            ))
            if reason == "plaintext_unavailable" and result.fetch is not None:
                attempts: list[RetryLogAttempt] = [
                    attempt_from_fetch_result(a) for a in result.fetch.attempts
                ]
                retry_entries.append(RetryLogMinerEntry(
                    miner_hotkey=inp.miner_hotkey,
                    miner_uid=inp.miner_uid,
                    expected_sha256_ct=inp.sha256_ct_commit or b"",
                    attempts=attempts,
                ))

    # ---- 2. build + commit 9.C.1 pre-scoring state ----
    pre_blob = build_pre_scoring_state(
        validator_hotkey=validator_hotkey,
        validator_signing_key=validator_signing_key,
        epoch_id=epoch_id,
        epoch_idx=epoch_idx,
        outcomes_release_round=outcomes_release_round,
        outcomes_fetched_at_round=outcomes_fetched_at_round,
        miner_commits=miner_commits,
        excluded_miners=excluded,
    )
    pre_commit = submit_pre_scoring_state_layer_9c1(
        subtensor=subtensor,
        validator_wallet=validator_wallet,
        netuid=netuid,
        pre_scoring_state_cbor=pre_blob,
        blocks_until_reveal=blocks_until_pre_scoring_reveal,
    )
    if not pre_commit.success:
        return EpochScoringOutcome(
            pre_scoring_commit=pre_commit,
            miner_reads=miner_reads,
            aborted_reason=f"pre_scoring_commit_failed: {pre_commit.message}",
        )

    # ---- 3. score accepted miners ----
    score_map = scorer(epoch_id, score_inputs)
    scored_records = [
        ScoredMinerRecord(miner_hotkey=hk, score_micro=v) for hk, v in score_map.items()
    ]

    # ---- 4. submit weights (Layer 9.C.3) ----
    uid_by_hotkey = {inp.miner_hotkey: inp.miner_uid for inp in miner_inputs}
    uids = [uid_by_hotkey[hk] for hk in score_map if hk in uid_by_hotkey]
    weights = [
        max(0.0, min(1.0, v / 1_000_000.0))
        for hk, v in score_map.items() if hk in uid_by_hotkey
    ]
    weights_commit = (
        commit_weights_layer_9c3(
            subtensor=subtensor,
            validator_wallet=validator_wallet,
            netuid=netuid,
            uids=uids,
            weights=weights,
        )
        if uids
        else WeightsCommitResult(
            success=False,
            message="no scoreable miners; skipping weights commit",
            block_number=None, block_hash=None, extrinsic_hash=None,
        )
    )
    if not weights_commit.success or weights_commit.block_hash is None:
        return EpochScoringOutcome(
            pre_scoring_commit=pre_commit,
            weights_commit=weights_commit,
            miner_reads=miner_reads,
            score_map=score_map,
            aborted_reason=f"weights_commit_failed: {weights_commit.message}",
        )
    weights_reveal_round = estimate_weights_reveal_round(
        current_round=outcomes_fetched_at_round,
        blocks_until_reveal=blocks_until_weights_reveal,
    )

    # ---- 5. build + commit 9.C.2 post-scoring artifacts ----
    post_blob = build_post_scoring_artifacts(
        validator_hotkey=validator_hotkey,
        validator_signing_key=validator_signing_key,
        epoch_id=epoch_id,
        epoch_idx=epoch_idx,
        scoring_inputs_hash=scoring_inputs_hash,
        scored_miners=scored_records,
        weights_commit_block_hash=weights_commit.block_hash,
        weights_reveal_round=weights_reveal_round,
    )
    post_commit = submit_post_scoring_artifacts_layer_9c2(
        subtensor=subtensor,
        validator_wallet=validator_wallet,
        netuid=netuid,
        post_scoring_artifacts_cbor=post_blob,
        blocks_until_reveal=blocks_until_post_scoring_reveal,
    )
    if not post_commit.success:
        return EpochScoringOutcome(
            pre_scoring_commit=pre_commit,
            weights_commit=weights_commit,
            post_scoring_commit=post_commit,
            miner_reads=miner_reads,
            score_map=score_map,
            aborted_reason=f"post_scoring_commit_failed: {post_commit.message}",
        )

    # ---- 6. optional 9.C.6 retry log ----
    retry_blob: Optional[bytes] = None
    retry_commit: Optional[CommitResult] = None
    if retry_entries:
        retry_blob = build_retry_log_blob(
            validator_hotkey=validator_hotkey,
            epoch_id=epoch_id,
            epoch_idx=epoch_idx,
            miner_entries=retry_entries,
        )
        retry_sha256 = compute_retry_log_sha256(retry_blob)
        retry_commit = submit_retry_log_attestation_layer_9c6(
            subtensor=subtensor,
            validator_wallet=validator_wallet,
            netuid=netuid,
            retry_log_blob_sha256=retry_sha256,
        )

    logger.info(
        "epoch %s scoring complete: scored=%d excluded=%d retry_log=%s "
        "weights_block=%s",
        epoch_id, len(score_map), len(excluded), retry_commit is not None,
        weights_commit.block_number,
    )
    return EpochScoringOutcome(
        pre_scoring_commit=pre_commit,
        weights_commit=weights_commit,
        post_scoring_commit=post_commit,
        retry_log_commit=retry_commit,
        retry_log_blob=retry_blob,
        miner_reads=miner_reads,
        score_map=score_map,
    )
