"""Pure aggregator — turns a private EpochArtifact into a public payload.

The single entry point is `aggregate(artifact) -> EpochReportPayload`.

Pure function discipline:
  * No I/O. No clock. No randomness. No chain reads. No env lookups.
  * Same input → byte-identical output, forever.
  * The verifier (`scripts/verify_epoch.py --emit-report`) calls this
    function on a chain-reconstructed artifact and produces the same
    payload as the operator, by construction.

Anti-doxxing discipline (v1/v2):
  * The return type is `EpochReportPayload`, which forbids extra fields
    (`extra="forbid"`). Per-UID data from the artifact never reaches
    the public payload — only counts, shares, and distribution shape.

v3 contract change (CMS-side scope expansion):
  * `EpochReportPayload.miner_results` now carries per-UID rows
    (Cacheon-style leaderboard table) when populated. Per the new
    CMS spec, full UID + hotkey is published in the wire payload;
    browser-side display truncation is the dashboard's concern.
  * The aggregate-only fields (score_distribution, tier_distribution,
    top_n_scores, baseline_beat_rate) remain — both views coexist.
"""

from __future__ import annotations

from typing import Any

from hope.reporting.epoch_artifact import EpochArtifact
from hope.reporting.histogram import (
    compute_histogram,
    compute_summary,
    merge_for_k_anonymity,
)
from hope.reporting.payload import (
    COMPETITIVE_EMISSION_SHARE,
    ELITE_EMISSION_SHARE,
    PARTICIPATING_EMISSION_SHARE,
    POOL_SIZE_DISTRIBUTION_FLOOR,
    EmergencyIntervention,
    EpochReportPayload,
    MinerResult,
    PolicyOutcome,
    ScoreDistribution,
    TierDistribution,
    TierSlice,
)

# In v1 the simplified gate is "raw_score > baseline" with baseline=0.
# Phase 1 of richer scoring (Q11) plumbs in per-episode conditional
# priors; the aggregator becomes a more meaningful filter at that
# point. Until then this constant is the bar miners must clear.
BASELINE_SCORE_V1 = 0.0


def _qualifying_scores(artifact: EpochArtifact) -> list[float]:
    """Return raw_score values for miners that passed the participation gate.

    Cross-references `artifact.per_uid_scores` (per-miner detail) with
    `artifact.tier_result["qualifying"]` (the gate-passing hotkey set).
    """
    qualifying_hotkeys = set(artifact.tier_result.get("qualifying", []))
    return [
        float(row["raw_score"])
        for row in artifact.per_uid_scores
        if row.get("hotkey") in qualifying_hotkeys
    ]


def _baseline_beat_rate(
    qualifying_scores: list[float], baseline: float = BASELINE_SCORE_V1
) -> float:
    """Fraction of qualifying miners with raw_score strictly above baseline.

    Per contract §3.3: denominator is pool_size, numerator is the count
    of qualifying miners whose miner_score beats the epoch's baseline.
    ``baseline`` is the artifact's ``baseline_score`` (the predict-zero
    submission scored by the live scorer) so the published rate matches
    the per-miner ``met_baseline`` flags on the leaderboard. Falls back
    to BASELINE_SCORE_V1 (=0) for legacy artifacts without a baseline,
    where the gate's raw_score > 0 makes the rate 1.0 by construction.
    """
    pool_size = len(qualifying_scores)
    if pool_size == 0:
        return 0.0
    beats = sum(1 for s in qualifying_scores if s > baseline)
    return beats / pool_size


def _build_score_distribution(
    qualifying_scores: list[float],
    *,
    n_bins: int = 15,
    score_range: tuple[float, float] = (0.0, 1.0),
    k_anon_floor: int = 5,
) -> ScoreDistribution:
    """Histogram + summary, with k-anonymity merge applied."""
    edges, counts = compute_histogram(
        qualifying_scores, n_bins=n_bins, range_=score_range,
    )
    edges, counts = merge_for_k_anonymity(edges, counts, floor=k_anon_floor)
    summary = compute_summary(qualifying_scores)
    return ScoreDistribution(bin_edges=edges, bin_counts=counts, summary=summary)


def _build_tier_distribution(
    tier_result: dict[str, Any],
    *,
    pool_size: int,
    tier_split_active: bool,
) -> TierDistribution:
    """Build the public tier_distribution from the artifact's tier_result.

    When `tier_split_active` is False (pool below the floor), per Q14
    all three counts are zero. The emission-share constants are still
    set so the payload is self-describing regardless of pool state.
    """
    if tier_split_active:
        elite_count = len(tier_result.get("elite", []))
        competitive_count = len(tier_result.get("competitive", []))
        participating_count = len(tier_result.get("participating", []))
        elite_floor_met = bool(tier_result.get("elite_floor_cleared", False))
    else:
        elite_count = 0
        competitive_count = 0
        participating_count = 0
        elite_floor_met = False

    def _slice(count: int, emission_share: float) -> TierSlice:
        share = (count / pool_size) if pool_size > 0 else 0.0
        return TierSlice(
            count=count,
            share_of_pool=share,
            share_of_emissions=emission_share,
        )

    return TierDistribution(
        elite=_slice(elite_count, ELITE_EMISSION_SHARE),
        competitive=_slice(competitive_count, COMPETITIVE_EMISSION_SHARE),
        participating=_slice(participating_count, PARTICIPATING_EMISSION_SHARE),
        elite_floor_met=elite_floor_met,
    )


TOP_N_SCORES_MAX = 20


def aggregate(
    artifact: EpochArtifact,
    *,
    n_bins: int = 20,
    score_range: tuple[float, float] = (0.0, 1.0),
    k_anon_floor: int = 5,
    pool_size_floor: int = POOL_SIZE_DISTRIBUTION_FLOOR,
    commentary_markdown: str | None = None,
    top_n: int = TOP_N_SCORES_MAX,
    supersedes: str | None = None,
    epoch_id_override: str | None = None,
    epoch_membership_uids: set[int] | None = None,
    accuracy_by_type: dict | None = None,
    collapse_audit: dict | None = None,
) -> EpochReportPayload:
    """Aggregate a private artifact into the public payload.

    Args:
        artifact: the operator-private record produced by
            `hope.reporting.epoch_artifact.build_artifact`.
        n_bins: histogram resolution. Default 20 (v2 — finer than v1's 15
            per the §8 Q1 contract follow-up; bins of width 0.05 over
            [0, 1] show distribution shape better for medium pools).
        score_range: histogram domain (default `(0.0, 1.0)` per Q3).
        k_anon_floor: k-anonymity floor (default 5 per contract §3.2).
        pool_size_floor: pool-size threshold below which the histogram
            is omitted and tiers collapse (default 15 per
            `tiered_weights.MIN_MINERS_FOR_TIER_SPLIT`).
        commentary_markdown: optional human commentary. Default None
            for routine epochs (Q20). Operator can override via the
            writer to pre-populate for special epochs.
        top_n: maximum number of ranked top scores to surface in
            ``top_n_scores`` (v2 field). Default 20; capped by the
            schema's ``max_length=20``. Set to 0 to omit.

    Returns:
        A fully-populated `EpochReportPayload` ready to POST.
    """
    qualifying_scores = _qualifying_scores(artifact)
    pool_size = len(qualifying_scores)

    pool_below_floor = pool_size < pool_size_floor
    tier_split_active = not pool_below_floor

    if pool_below_floor:
        score_distribution: ScoreDistribution | None = None
        top_n_scores: list[float] | None = None
    else:
        score_distribution = _build_score_distribution(
            qualifying_scores,
            n_bins=n_bins,
            score_range=score_range,
            k_anon_floor=k_anon_floor,
        )
        # Top-N ranked scores (descending) — payload-only, no UIDs.
        # If the pool is smaller than top_n, the list is just len(pool).
        # If top_n is 0 the field is omitted entirely.
        if top_n > 0:
            ranked = sorted(qualifying_scores, reverse=True)
            top_n_scores = ranked[: min(top_n, len(ranked))]
        else:
            top_n_scores = None

    tier_distribution = _build_tier_distribution(
        artifact.tier_result,
        pool_size=pool_size,
        tier_split_active=tier_split_active,
    )

    miner_results = _build_miner_results(
        artifact, tier_split_active=tier_split_active,
        epoch_membership_uids=epoch_membership_uids,
        collapse_audit=collapse_audit)

    # v1 routine emergency state — always false. Q19 freezes this until
    # trigger-state machines land in SN21_REWARD_MECHANISM.md.
    emergency = EmergencyIntervention(triggered=False)

    return EpochReportPayload(
        accuracy_by_type=_public_type_accuracy(accuracy_by_type),
        # `epoch_id_override` is used by the correction flow (IA D-13): a
        # frozen/published epoch can't be mutated, so a correction is posted
        # under a new `{orig}-COR-N` epoch_id with `supersedes=orig`. Stays a
        # pure function — same (artifact, override) in → same payload out.
        epoch_id=epoch_id_override or artifact.epoch_id,
        epoch_type=artifact.epoch_type,
        epoch_subtype=artifact.epoch_subtype,
        block_range_start=artifact.block_range_start or 0,
        block_range_end=artifact.block_range_end or 0,
        scoring_formula_version=artifact.scoring_formula_version,
        scoring_formula_commit=artifact.scoring_formula_commit,
        horizon_set=list(artifact.horizon_set),
        epoch_type_multiplier=artifact.epoch_type_multiplier,
        pool_size=pool_size,
        total_registered_uids=artifact.total_registered_uids,
        pool_size_below_distribution_floor=pool_below_floor,
        baseline_beat_rate=_baseline_beat_rate(
            qualifying_scores,
            baseline=float(getattr(artifact, "baseline_score", 0.0) or 0.0),
        ),
        baseline_score=max(0.0, min(1.0, float(getattr(artifact, "baseline_score", 0.0) or 0.0))),
        score_distribution=score_distribution,
        tier_distribution=tier_distribution,
        tier_split_active=tier_split_active,
        emergency_intervention=emergency,
        validator_output_snapshot_timestamp=artifact.validator_output_snapshot_timestamp,
        chain_fetch_timestamp=artifact.chain_fetch_timestamp,
        commentary_markdown=commentary_markdown,
        top_n_scores=top_n_scores,
        supersedes=supersedes,
        miner_results=miner_results,
        aggregator_version=4,
    )


def _hotkey_to_ss58(value: str) -> str:
    """Normalise a per-UID hotkey field to the chain SS58 address.

    `artifact.per_uid_scores` and the tier rosters carry the chain hotkey
    as the 64-char hex of its 32-byte raw pubkey — the internal scoring
    identity (see `_build_per_uid_scores`, which writes `hotkey.hex()`).
    The leaderboard wire contract requires the SS58 form (starts with 5,
    47–48 chars), so we encode at the publish boundary here.

    This is a PURE deterministic encoding (ss58 = base58(pubkey ++ checksum)):
    same hex in → same SS58 out, no I/O / clock / chain read — so it keeps
    the aggregator's byte-identical-output guarantee intact, and the
    verifier reconstructs the identical payload from the same artifact.

    Already-SS58 values pass through unchanged, so the function is safe to
    apply to any hotkey-shaped field regardless of upstream representation.
    """
    if len(value) in (47, 48) and value.startswith("5"):
        return value  # already an SS58 address
    if len(value) == 64:
        try:
            int(value, 16)  # confirm it is raw-pubkey hex before encoding
        except ValueError:
            return value
        from bittensor_wallet.bittensor_wallet import Keypair  # type: ignore
        return Keypair(public_key="0x" + value, ss58_format=42).ss58_address
    return value  # unknown shape — surface it downstream rather than mangle


def policies_by_hotkey(collapse_audit: dict | None) -> dict:
    """Turn the fleet-level allocation audit into per-miner reasons.

    The audit publishes each control as its own list — who the coldkey cap
    dropped, who tenure held back, who was suppressed as a copy. That is the
    right shape for verifying the RULE, and the wrong shape for answering a
    miner's actual question, which is "why am I not being paid today". This
    inverts it, so the answer travels in the miner's own row.

    Deliberately reads only what the audit already publishes: no new source
    of truth, and nothing here can say a control did something the audit does
    not already show it doing.
    """
    from collections import defaultdict

    out = defaultdict(list)
    audit = collapse_audit or {}
    if not isinstance(audit, dict):
        return {}

    cap = audit.get("coldkey_cap")
    if isinstance(cap, dict):
        # `contested` lists a coldkey's hotkeys ALPHABETICALLY, not by rank —
        # the ordering that decided the seat (standing, then commit block) is
        # not published. So the holder is derived rather than assumed: it is
        # the member of the group that was not dropped. Reading the first
        # entry as the winner names the wrong hotkey, and can name one that
        # was itself dropped, which is worse than naming nobody.
        dropped = set(cap.get("dropped") or [])
        contested = cap.get("contested") if isinstance(cap.get("contested"), dict) else {}
        holder_of = {}
        for _coldkey, hotkeys in contested.items():
            if not isinstance(hotkeys, list):
                continue
            holders = [hk for hk in hotkeys if hk not in dropped]
            # Exactly one seat per coldkey today. If that ever changes, or the
            # audit is partial, say nothing rather than pick one arbitrarily.
            if len(holders) != 1:
                continue
            for losing in hotkeys:
                if losing in dropped:
                    holder_of[losing] = holders[0]
        for hk in cap.get("dropped") or []:
            holder = holder_of.get(hk)
            out[hk].append(PolicyOutcome(
                control="coldkey_cap",
                detail=("One coldkey holds one earning seat. Another hotkey "
                        "with the same owner holds it today."),
                counterparty=holder))

    for hk in audit.get("suppressed") or []:
        out[hk].append(PolicyOutcome(
            control="one_payer",
            detail=("This model is already earning under an earlier "
                    "submission. Identical models pay once.")))

    lineage = audit.get("lineage")
    if isinstance(lineage, dict):
        for group in lineage.get("groups") or []:
            if not isinstance(group, dict):
                continue
            payee = group.get("payee")
            for hk in group.get("eliminated") or []:
                out[hk].append(PolicyOutcome(
                    control="lineage",
                    detail=("Same behaviour lineage as an earlier submission; "
                            "the earliest in the group earns."),
                    counterparty=payee))

    tenure = audit.get("tenure_gated")
    if isinstance(tenure, dict):
        min_days = tenure.get("min_days")
        stood_down = bool(tenure.get("stood_down"))
        for hk in tenure.get("hotkeys") or []:
            out[hk].append(PolicyOutcome(
                control="tenure",
                detail=(
                    f"Fewer than {min_days} scored days. Scores still count "
                    f"and the model keeps running — tenure accrues by showing "
                    f"up." + (" The gate stood down today because applying it "
                             "would have emptied the paid set." if stood_down else "")
                )))

    return dict(out)


def _build_miner_results(
    artifact: EpochArtifact,
    *,
    tier_split_active: bool,
    epoch_membership_uids: set[int] | None = None,
    collapse_audit: dict | None = None,
) -> list[MinerResult]:
    """Build the per-UID Cacheon-style table from artifact.per_uid_scores.

    For each scored miner: status='scored', tier resolved against the
    artifact's tier_result. Miners that the upstream runner excluded
    appear in artifact.tier_result['excluded'] (when populated) and
    are mapped onto the operator's `disqualified_*` status enum.

    `epoch_membership_uids`, when given, scopes the *scored* set to an
    eligible cohort: a miner that scored but whose uid is NOT in the set is
    re-labeled `disqualified_not_in_epoch` (tier cleared) instead of `scored`.
    Used for re-run epochs that should only credit the original participants
    (e.g. WR-2026-W21-RERUN-E1 → the W21-16) while still showing the others.
    Already-disqualified rows are untouched (the filter only narrows scored).
    """
    policy_notes = policies_by_hotkey(collapse_audit)

    # Map hotkey → tier from the artifact's tier allocation.
    tier_by_hotkey: dict[str, str] = {}
    if tier_split_active:
        for hk in artifact.tier_result.get("elite", []) or []:
            tier_by_hotkey[hk] = "elite"
        for hk in artifact.tier_result.get("competitive", []) or []:
            tier_by_hotkey[hk] = "competitive"
        for hk in artifact.tier_result.get("participating", []) or []:
            tier_by_hotkey[hk] = "participating"

    # Allocator exclusions (hotkey → reason): below_baseline, coverage, etc.
    # Merge these into the per-miner row so a scored-but-excluded miner shows
    # ONE row with the correct disqualified status (not a "scored" row plus a
    # duplicate). The below-baseline case is the important one post-baseline-fix.
    excluded_map: dict[str, str] = {}
    _ex = artifact.tier_result.get("excluded", {}) if isinstance(artifact.tier_result, dict) else {}
    if isinstance(_ex, dict):
        excluded_map = {str(hk): str(reason) for hk, reason in _ex.items()}

    results: list[MinerResult] = []
    emitted_hotkeys: set[str] = set()
    for entry in artifact.per_uid_scores:
        # Skip rows lacking the fields we need.
        try:
            uid = int(entry["uid"])
            hotkey = str(entry["hotkey"])
            raw_score = float(entry["raw_score"])
        except (KeyError, TypeError, ValueError):
            continue
        # Clamp into the wire range — the schema rejects >1.0 / <0.0.
        clamped = max(0.0, min(1.0, raw_score))
        # Per-entry status override (v4) — when present, the entry was a
        # synthesized disqualification row not produced by normal scoring.
        # When absent, default to "scored" (the historical aggregator path).
        raw_status = entry.get("status")
        if isinstance(raw_status, str) and raw_status:
            status = _map_exclusion_to_status(raw_status)
            # DQ rows always have tier=null per the operator's v4 spec.
            tier = None
        elif hotkey in excluded_map:
            # Scored a number but the allocator excluded it (below_baseline,
            # coverage, …). Show the exclusion, not a tiered "scored" row —
            # this is the "Competitive but earns 0 on chain" fix.
            status = _map_exclusion_to_status(excluded_map[hotkey])
            tier = None
        elif epoch_membership_uids is not None and uid not in epoch_membership_uids:
            # Scored, but not part of this epoch's eligible cohort (re-run scoped
            # to the original participants). Show the row, flagged + tier cleared.
            status = "disqualified_not_in_epoch"
            tier = None
        elif tier_split_active and hotkey in tier_by_hotkey:
            # Funded by the allocator INCLUDING the flat-week fallback: in a
            # flat week no miner beats predict-zero (met_baseline=False for all),
            # but the top fraction is still funded and earns on chain. The
            # published row must match the funded set, so show 'scored' with the
            # allocated tier — not a 'disqualified_below_threshold' that would
            # contradict the on-chain payout. (Normal weeks hit the else branch
            # below with met_baseline=True; this only adds the below-baseline-
            # but-funded case.)
            status = "scored"
            tier = tier_by_hotkey[hotkey]
        elif not bool(entry.get("met_baseline", True)):
            # Scored a number but did NOT clear the baseline AND was not funded
            # by the fallback → earns 0 on chain and gets NO tier. Show that
            # honestly instead of a tiered "scored" row (the "Competitive but
            # earns 0 on chain" contradiction — the published table must match
            # the funded set). Old artifacts lacking met_baseline default True.
            status = "disqualified_below_threshold"
            tier = None
        else:
            status = "scored"
            tier = tier_by_hotkey.get(hotkey) if tier_split_active else None
            # Validate the tier value against the Literal — defence in depth
            # against artifact corruption.
            if tier not in ("elite", "competitive", "participating"):
                tier = None
        results.append(MinerResult(
            uid=uid,
            hotkey=_hotkey_to_ss58(hotkey),
            score=clamped,
            status=status,
            tier=tier,
            met_baseline=bool(entry.get("met_baseline", status == "scored")),
            # Keyed on the RAW hotkey: the audit records whatever the
            # allocation used, which is the same string the standings use.
            policies=policy_notes.get(hotkey, []),
        ))
        emitted_hotkeys.add(hotkey)

    # Excluded miners with NO per_uid_scores row (score-less exclusions). Those
    # that DID score were already emitted above with their exclusion merged in,
    # so skip them here to avoid a duplicate UID row.
    if excluded_map:
        for hotkey, reason in excluded_map.items():
            if str(hotkey) in emitted_hotkeys:
                continue
            status = _map_exclusion_to_status(str(reason))
            # Excluded miners may not have a uid in artifact — skip if absent.
            uid = _find_uid_for_hotkey(artifact, str(hotkey))
            if uid is None:
                continue
            results.append(MinerResult(
                uid=uid,
                hotkey=_hotkey_to_ss58(str(hotkey)),
                score=0.0,
                status=status,
                tier=None,
                met_baseline=False,
                policies=policy_notes.get(str(hotkey), []),
            ))

    return results


_EXCLUSION_STATUS_MAP = {
    "scored": "scored",
    # Generic disqualification reasons (existing in v3):
    "below_threshold": "disqualified_below_threshold",
    "below_baseline": "disqualified_below_threshold",  # allocator gate reason
    "missing_snapshot": "disqualified_missing_snapshot",
    "invalid_commit": "disqualified_invalid_commit",
    "inner_sig_hotkey_mismatch": "disqualified_invalid_commit",
    "hotkey_mismatch": "disqualified_invalid_commit",
    "plaintext_unavailable": "disqualified_plaintext_unavailable",
    # v4 additions — the CMS Disqualifier panel renders dedicated cards
    # for these so miners can self-diagnose.
    "not_registered": "disqualified_not_registered",
    "unregistered": "disqualified_not_registered",
    "no_registration": "disqualified_not_registered",
    "late_submission": "disqualified_late_submission",
    "late": "disqualified_late_submission",
    "not_in_epoch": "disqualified_not_in_epoch",
    "not_in_w21": "disqualified_not_in_epoch",
    "disqualified_not_in_epoch": "disqualified_not_in_epoch",
    # Pass-through if upstream already emits the canonical status form:
    "disqualified_below_threshold": "disqualified_below_threshold",
    "disqualified_missing_snapshot": "disqualified_missing_snapshot",
    "disqualified_invalid_commit": "disqualified_invalid_commit",
    "disqualified_plaintext_unavailable": "disqualified_plaintext_unavailable",
    "disqualified_not_registered": "disqualified_not_registered",
    "disqualified_late_submission": "disqualified_late_submission",
    "disqualified_other": "disqualified_other",
}


def _map_exclusion_to_status(reason: str) -> str:
    """Map an upstream exclusion code onto the operator's v4 status enum.

    Returns ``disqualified_other`` for unmapped reasons so unknown codes
    surface in the dashboard rather than getting silently dropped.
    """
    return _EXCLUSION_STATUS_MAP.get(reason, "disqualified_other")


def _find_uid_for_hotkey(artifact: EpochArtifact, hotkey: str) -> int | None:
    """Look up uid for hotkey in artifact.per_uid_scores.

    Excluded miners may not appear in per_uid_scores; if so, we have
    no uid to publish and skip the row. Future runner versions can
    populate an `excluded_with_uid` map to fix that.
    """
    for entry in artifact.per_uid_scores:
        if entry.get("hotkey") == hotkey:
            try:
                return int(entry["uid"])
            except (KeyError, TypeError, ValueError):
                return None
    return None


def _public_type_accuracy(raw: dict | None) -> dict | None:
    """Reduce the daily accuracy artifact to its PUBLIC cut.

    Keeps n / field_mean / champion_mean per (family, horizon); drops the
    'best' block (it names a miner) and everything per-miner — the payload
    invariant is aggregate-only, and this is where it is enforced for the
    accuracy feed."""
    if not raw:
        return None
    by_type = raw.get("by_type", raw)
    out: dict = {}
    for fam, horizons in by_type.items():
        if not isinstance(horizons, dict):
            continue
        for h, cell in horizons.items():
            if not isinstance(cell, dict):
                continue
            out.setdefault(str(fam), {})[str(h)] = {
                "n": int(cell.get("n", 0)),
                "field_mean": cell.get("field_mean"),
                "champion_mean": cell.get("champion_mean"),
            }
    return out or None
