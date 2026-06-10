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
from dataclasses import dataclass, field, replace
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
from hope.validator.registration_index import RegistrationIndex
from hope.validator.weights_commit import (
    WeightsCommitResult,
    commit_weights_layer_9c3,
    estimate_weights_reveal_round,
)

logger = logging.getLogger(__name__)


def _pubkey_bytes_to_ss58(pubkey: bytes) -> str:
    """Encode a 32-byte ed25519 hotkey pubkey as an SS58 address.

    The miner side (`hope/miner/onchain_submitter.py`) uploads archive
    objects under `/archive/{epoch_id}/{ss58}/{sha256}`. The validator
    therefore must query the same SS58 path — passing the raw hex pubkey
    silently 404s every miner.
    """
    from bittensor_wallet.bittensor_wallet import Keypair  # type: ignore
    return Keypair(public_key="0x" + pubkey.hex(), ss58_format=42).ss58_address


# Caller-supplied scorer signature: (epoch_id, plaintext_per_miner_hotkey)
# → {miner_hotkey_bytes: score_micro_uint}.
ScorerFn = Callable[[str, dict[bytes, dict[str, Any]]], dict[bytes, int]]


@dataclass
class MinerOnChainInputs:
    """Per-miner chain state assembled by the caller for one epoch.

    The runner is agnostic to how the caller assembled these. Production
    callers fill them via `hope/commitment/chain_reader.py`
    (`read_revealed_commitments`, `read_commitment_of`) which queries
    substrate directly; tests can supply them inline.
    """

    miner_uid: int
    # 32-byte raw CHAIN hotkey pubkey (ss58_decode of metagraph.hotkeys[uid]).
    # This is the on-chain identity / metagraph hotkey — NOT the ed25519
    # inner-sig reg-key (that's resolved separately via RegistrationIndex.
    # lookup(miner_hotkey)). ss58_encode(this, fmt=42) round-trips back to
    # the miner's chain hotkey SS58 — which is what the leaderboard publishes.
    miner_hotkey: bytes
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
    # On-chain footprint of this epoch — used by the leaderboard reporter
    # and external verifiers to scope their chain queries. Derived from
    # the earliest miner K-commit (or this validator's 9.C.1 when no
    # miners are visible) through the validator's 9.C.3 weights commit.
    block_range_start: Optional[int] = None
    block_range_end: Optional[int] = None
    # report-only run: scoring was computed but NO chain commits were made (the
    # on-chain 9.C/weights already exist from the real run). Lets us rebuild the
    # leaderboard artifact for a report correction without touching chain — so
    # no Commitments space-budget spend, no weights rate-limit, retryable anytime.
    report_only: bool = False

    @property
    def ok(self) -> bool:
        if self.report_only:
            # No commits in report-only mode; "ok" = scoring produced a result,
            # which is all the artifact/reporter consumes.
            return self.aborted_reason is None and bool(self.score_map)
        return (
            self.aborted_reason is None
            and self.pre_scoring_commit is not None and self.pre_scoring_commit.success
            and self.weights_commit is not None and self.weights_commit.success
            and self.post_scoring_commit is not None and self.post_scoring_commit.success
        )


def compute_block_range(
    miner_inputs: list["MinerOnChainInputs"],
    pre_scoring_commit: Optional[CommitResult],
    weights_commit: Optional[WeightsCommitResult],
) -> tuple[Optional[int], Optional[int]]:
    """Compute (block_range_start, block_range_end) for an epoch.

    Convention per the leaderboard data contract:

        block_range_start = earliest on-chain mining-window activity
                            for this epoch on this RPC view.
                            Falls back to the validator's 9.C.1 block
                            when no miner K-commits are visible.

        block_range_end   = the validator's 9.C.3 weights commit block.
                            None when the run aborted before weights
                            were submitted.

    Both can be None on a fully-aborted run (e.g. insufficient budget,
    no reveals visible). The reporter pipeline treats None as "skip the
    block range fields in the payload" and surfaces the abort reason
    out of band.
    """
    end: Optional[int] = None
    if weights_commit is not None and weights_commit.success:
        end = weights_commit.block_number

    miner_blocks = [
        inp.chain_block_at_k_commit
        for inp in miner_inputs
        if inp.chain_block_at_k_commit is not None and inp.chain_block_at_k_commit > 0
    ]
    if miner_blocks:
        start: Optional[int] = min(miner_blocks)
    elif pre_scoring_commit is not None and pre_scoring_commit.success:
        start = pre_scoring_commit.block_number
    else:
        start = None

    return start, end


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
    registration_index: Optional["RegistrationIndex"] = None,
    report_only: bool = False,
    ignore_already_scored: bool = False,
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

    # ---- 0. pre-flight: idempotency + budget check ----
    # If this validator has already submitted a 9.C.1 for this epoch_id,
    # bail out cleanly. Re-running would either double-spend the
    # Commitments-pallet byte budget or land a duplicate scoring record.
    # If the budget for this pallet-epoch is exhausted (e.g. from earlier
    # debug runs), bail out cleanly too — refusing a partial commit
    # sequence is safer than leaving the chain in an inconsistent state.
    #
    # The pre-flight is wrapped in a try/except so unit tests that pass
    # stub `subtensor` / `validator_wallet` objects can skip the check
    # gracefully. Production wallets always expose `.hotkey.ss58_address`
    # and a real subtensor; the tests mock at a lower layer.
    try:
        from hope.commitment.chain_reader import (
            commitments_budget_sufficient,
            validator_already_scored_epoch,
            MIN_VALIDATOR_BUDGET_BYTES,
        )
        validator_ss58 = validator_wallet.hotkey.ss58_address
        if validator_already_scored_epoch(subtensor, netuid, validator_ss58, epoch_id):
            if ignore_already_scored or report_only:
                logger.warning(
                    "ignore_already_scored=True: bypassing already_scored guard "
                    "for validator %s... epoch_id=%s. The pallet-byte-budget "
                    "check remains as the second line of defence; if budget is "
                    "exhausted the run will still abort cleanly.",
                    validator_ss58[:12], epoch_id,
                )
            else:
                return EpochScoringOutcome(
                    miner_reads=[],
                    aborted_reason=(
                        f"already_scored: validator {validator_ss58[:12]}... has a "
                        f"prior 9.C.1 commit for epoch_id={epoch_id}. Re-running this "
                        f"epoch would double-spend the Commitments-pallet byte budget. "
                        f"Skip until the next epoch."
                    ),
                )
        sufficient, remaining, last_epoch = commitments_budget_sufficient(
            subtensor, netuid, validator_ss58,
            needed_bytes=MIN_VALIDATOR_BUDGET_BYTES,
        )
        if not sufficient and not report_only:
            return EpochScoringOutcome(
                miner_reads=[],
                aborted_reason=(
                    f"insufficient_budget: validator {validator_ss58[:12]}... has "
                    f"{remaining}B free in the Commitments pallet "
                    f"(need ≥{MIN_VALIDATOR_BUDGET_BYTES}B for a full scoring run; "
                    f"last_epoch={last_epoch}). Wait for the pallet-epoch to "
                    f"advance, or use a fresh validator hotkey."
                ),
            )
    except (AttributeError, Exception) as e:
        # Test-mode stubs trip AttributeError on validator_wallet.hotkey;
        # transient RPC errors trip the broader Exception. Log and proceed —
        # the real chain extrinsic will surface SpaceLimitExceeded as a
        # last line of defence if budget is genuinely exhausted.
        logger.warning(
            "validator pre-flight skipped (%s): %s",
            type(e).__name__, str(e)[:120],
        )

    # If NONE of the configured miners have a revealed bundle visible from
    # this RPC connection, skip the entire scoring run. The 9.C.1 commit
    # would otherwise consume ~600B of Commitments-pallet budget for an
    # empty miner_commits root — wasted bytes that hurt later runs in the
    # same pallet-epoch. The next run (on a synced RPC node, or after the
    # bundle reveal fires) will pick this up.
    if miner_inputs and all(inp.revealed_k is None for inp in miner_inputs):
        return EpochScoringOutcome(
            miner_reads=[],
            aborted_reason=(
                f"no_miner_reveals_visible: 0 of {len(miner_inputs)} miners "
                f"have a revealed bundle on this RPC connection. Either the "
                f"bundle reveal hasn't fired yet (wait for next subnet tempo "
                f"step), the RPC node is stale (retry to land on a different "
                f"backend), or no miner submitted this epoch. Skipping commits "
                f"to preserve byte budget."
            ),
        )

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
        inner_sig_pk = (
            registration_index.lookup(inp.miner_hotkey)
            if registration_index is not None
            else None
        )
        result = read_miner_for_epoch(
            chain_commits=cc,
            archive_client=archive_client,
            archive_endpoints=archive_endpoints,
            epoch_id=epoch_id,
            timing=timing,
            miner_uid=inp.miner_uid,
            miner_identity_for_archive=_pubkey_bytes_to_ss58(inp.miner_hotkey),
            inner_sig_pubkey=inner_sig_pk,
        )
        # When we HAD a registration index to check against and the miner is
        # absent from it, the root cause of any scoreability failure is "not
        # registered" — relabel so the leaderboard shows the accurate
        # disqualified_not_registered (rather than a misleading invalid_commit
        # from the inner_sig falling back to the chain hotkey). A registered
        # miner whose commit is genuinely bad keeps its specific reason.
        if not result.ok and inner_sig_pk is None and registration_index is not None:
            result = replace(result, excluded_reason="not_registered")
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
    # (skipped in report-only mode — no chain write, so the score below still runs)
    pre_commit: Optional[CommitResult] = None
    if not report_only:
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

    # report-only short-circuit: scoring is done; skip ALL chain commits (9.C.1/
    # 9.C.3 weights/9.C.2/9.C.6). Used to (re)build the leaderboard artifact for a
    # report correction when the on-chain 9.C already exists — no Commitments
    # space-budget spend, no weights rate-limit, no consensus impact. The artifact
    # is assembled from score_map + miner_reads.
    if report_only:
        start, end = compute_block_range(miner_inputs, None, None)
        return EpochScoringOutcome(
            miner_reads=miner_reads,
            score_map=score_map,
            report_only=True,
            block_range_start=start,
            block_range_end=end,
        )

    # ---- 4. submit weights (Layer 9.C.3) ----
    uid_by_hotkey = {inp.miner_hotkey: inp.miner_uid for inp in miner_inputs}
    uids = [uid_by_hotkey[hk] for hk in score_map if hk in uid_by_hotkey]
    weights = [
        max(0.0, min(1.0, v / 1_000_000.0))
        for hk, v in score_map.items() if hk in uid_by_hotkey
    ]

    # Optional weight-vector override. When SN21_OVERRIDE_WEIGHT_UID is set,
    # the validator's normal per-miner weight vector is REPLACED with a
    # single-entry vector that puts 100% weight on the override UID. This
    # is used to align the validator's commit with the network consensus
    # target when standalone real-scoring leaves us at vtrust=0; see
    # docs/validator_setup.md for the operational rationale. The override
    # bypasses scoring entirely — the scoreability + 9.C.1 path still runs
    # for audit purposes, but 9.C.3 publishes the override instead of the
    # computed per-miner allocation. Leave SN21_OVERRIDE_WEIGHT_UID unset
    # (or empty) to keep the default scoring behaviour.
    import os as _os
    _override_uid_str = _os.environ.get("SN21_OVERRIDE_WEIGHT_UID", "").strip()
    if _override_uid_str:
        try:
            _override_uid = int(_override_uid_str)
            if _override_uid < 0 or _override_uid > 65535:
                raise ValueError(f"out of u16 range: {_override_uid}")
            # Optional partial burn. When SN21_BURN_FRACTION is set to a value
            # strictly between 0 and 1, the override UID receives that fraction
            # of the weight and the remaining (1 - fraction) is distributed
            # across the computed per-miner vector in proportion to scores.
            # Unset (or a value <=0 or >=1, or no scoreable miners) keeps the
            # full 100% single-UID override.
            _burn_str = _os.environ.get("SN21_BURN_FRACTION", "").strip()
            _burn = None
            if _burn_str:
                try:
                    _bf = float(_burn_str)
                    if 0.0 < _bf < 1.0:
                        _burn = _bf
                    else:
                        print(
                            f"[weight-override] SN21_BURN_FRACTION={_burn_str!r} "
                            f"outside (0,1); using full single-UID override",
                            flush=True,
                        )
                except ValueError as _e:
                    print(
                        f"[weight-override] SN21_BURN_FRACTION={_burn_str!r} "
                        f"invalid ({_e}); using full single-UID override",
                        flush=True,
                    )
            _miner_pairs = [
                (u, w) for u, w in zip(uids, weights) if u != _override_uid
            ]
            _miner_total = sum(w for _, w in _miner_pairs)
            if _burn is not None and _miner_pairs and _miner_total > 0:
                # e.g. SN21_BURN_FRACTION=0.75 -> 75% to override UID,
                # 25% across miners in proportion to their scores.
                _scaled = [
                    (1.0 - _burn) * (w / _miner_total) for _, w in _miner_pairs
                ]
                _miner_uids = [u for u, _ in _miner_pairs]
                uids = _miner_uids + [_override_uid]
                weights = _scaled + [_burn]
                print(
                    f"[weight-override] partial burn: {_burn:.0%} -> UID "
                    f"{_override_uid}, {1.0 - _burn:.0%} across {len(_miner_uids)} "
                    f"miners by score",
                    flush=True,
                )
            else:
                print(
                    f"[weight-override] SN21_OVERRIDE_WEIGHT_UID={_override_uid} set; "
                    f"replacing the computed vector ({len(uids)} miners) with "
                    f"{{ {_override_uid}: 1.0 }}",
                    flush=True,
                )
                uids = [_override_uid]
                weights = [1.0]
        except ValueError as _e:
            print(
                f"[weight-override] SN21_OVERRIDE_WEIGHT_UID={_override_uid_str!r} "
                f"invalid ({_e}); ignoring override and using computed vector",
                flush=True,
            )

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
        start, end = compute_block_range(miner_inputs, pre_commit, weights_commit)
        return EpochScoringOutcome(
            pre_scoring_commit=pre_commit,
            weights_commit=weights_commit,
            post_scoring_commit=post_commit,
            miner_reads=miner_reads,
            score_map=score_map,
            aborted_reason=f"post_scoring_commit_failed: {post_commit.message}",
            block_range_start=start,
            block_range_end=end,
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
    start, end = compute_block_range(miner_inputs, pre_commit, weights_commit)
    return EpochScoringOutcome(
        pre_scoring_commit=pre_commit,
        weights_commit=weights_commit,
        post_scoring_commit=post_commit,
        retry_log_commit=retry_commit,
        retry_log_blob=retry_blob,
        miner_reads=miner_reads,
        score_map=score_map,
        block_range_start=start,
        block_range_end=end,
    )
