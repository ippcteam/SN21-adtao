"""Tests for hope.reporting.aggregator — the pure aggregation function."""

from __future__ import annotations

import dataclasses

from hope.reporting.aggregator import _hotkey_to_ss58, aggregate
from hope.reporting.epoch_artifact import EpochArtifact
from hope.reporting.payload import EpochReportPayload


def _hot(uid: int) -> str:
    return ("0" * 62) + f"{uid:02x}"


def _artifact(
    *,
    n_qualifying: int = 20,
    raw_score_base: float = 0.40,
    raw_score_step: float = 0.02,
    epoch_id: str = "WR-2026-W18-PUB-E1",
) -> EpochArtifact:
    """Build a synthetic artifact with N qualifying miners."""
    per_uid: list[dict] = []
    qualifying_hotkeys: list[str] = []
    for i in range(n_qualifying):
        hotkey = _hot(i)
        score = raw_score_base + i * raw_score_step
        per_uid.append({
            "uid": i,
            "hotkey": hotkey,
            "score_micro": int(score * 1_000_000),
            "raw_score": score,
        })
        qualifying_hotkeys.append(hotkey)

    # Synthetic tier_result with all qualifying in `competitive` for small
    # pools, or split across three tiers for big pools.
    if n_qualifying >= 15:
        # Split roughly 20% / 40% / 40%
        elite_n = max(1, n_qualifying // 5)
        comp_n = max(1, (n_qualifying * 2) // 5)
        elite = qualifying_hotkeys[:elite_n]
        competitive = qualifying_hotkeys[elite_n:elite_n + comp_n]
        participating = qualifying_hotkeys[elite_n + comp_n:]
        elite_floor_cleared = True
    else:
        elite = []
        competitive = list(qualifying_hotkeys)
        participating = []
        elite_floor_cleared = False

    tier_result = {
        "weights": {h: 1.0 / max(n_qualifying, 1) for h in qualifying_hotkeys},
        "qualifying": qualifying_hotkeys,
        "excluded": {},
        "elite": elite,
        "competitive": competitive,
        "participating": participating,
        "elite_floor_cleared": elite_floor_cleared,
    }

    return EpochArtifact(
        epoch_id=epoch_id,
        scoring_formula_version="1.2.1",
        scoring_formula_commit="a" * 40,
        epoch_type="Search",
        epoch_subtype="campaign-level",
        epoch_type_multiplier=1.0,
        horizon_set=["7d", "14d"],
        block_range_start=1_000_000,
        block_range_end=1_001_000,
        total_registered_uids=max(n_qualifying + 5, 1),
        validator_output_snapshot_timestamp="2026-05-13T17:00:00+00:00",
        chain_fetch_timestamp="2026-05-13T16:55:00+00:00",
        per_uid_scores=per_uid,
        tier_result=tier_result,
        artifact_schema_version=1,
    )


# ----------------------------------------------------------------------------
# Happy path
# ----------------------------------------------------------------------------

def test_full_pool_returns_full_payload():
    artifact = _artifact(n_qualifying=25)
    payload = aggregate(artifact)
    assert isinstance(payload, EpochReportPayload)
    assert payload.epoch_id == "WR-2026-W18-PUB-E1"
    assert payload.pool_size == 25
    assert payload.tier_split_active is True
    assert payload.pool_size_below_distribution_floor is False
    assert payload.score_distribution is not None
    assert payload.aggregator_version == 4


def test_field_pass_through_from_artifact():
    artifact = _artifact(n_qualifying=20)
    payload = aggregate(artifact)
    assert payload.scoring_formula_version == "1.2.1"
    assert payload.scoring_formula_commit == "a" * 40
    assert payload.block_range_start == 1_000_000
    assert payload.block_range_end == 1_001_000
    assert payload.horizon_set == ["7d", "14d"]
    assert payload.epoch_type == "Search"
    assert payload.epoch_subtype == "campaign-level"
    assert payload.epoch_type_multiplier == 1.0
    assert payload.total_registered_uids == 25
    assert payload.validator_output_snapshot_timestamp == "2026-05-13T17:00:00+00:00"
    assert payload.chain_fetch_timestamp == "2026-05-13T16:55:00+00:00"


def test_emergency_intervention_is_false_in_v1():
    """Q19: v1 always emits `triggered: false` regardless of state."""
    artifact = _artifact(n_qualifying=20)
    payload = aggregate(artifact)
    assert payload.emergency_intervention.triggered is False
    assert payload.emergency_intervention.type is None
    assert payload.emergency_intervention.outcome is None


def test_commentary_defaults_to_null():
    payload = aggregate(_artifact(n_qualifying=20))
    assert payload.commentary_markdown is None


def test_commentary_override_passed_through():
    payload = aggregate(_artifact(n_qualifying=20),
                        commentary_markdown="Championship week — multiplier ×3.0.")
    assert payload.commentary_markdown == "Championship week — multiplier ×3.0."


# ----------------------------------------------------------------------------
# Pool below floor
# ----------------------------------------------------------------------------

def test_small_pool_omits_distribution_and_collapses_tiers():
    """Q14: pool < 15 → score_distribution null, tiers zero, split inactive."""
    artifact = _artifact(n_qualifying=10)
    payload = aggregate(artifact)
    assert payload.pool_size == 10
    assert payload.pool_size_below_distribution_floor is True
    assert payload.tier_split_active is False
    assert payload.score_distribution is None
    # All tier counts are zero in the collapsed case (per Q14).
    assert payload.tier_distribution.elite.count == 0
    assert payload.tier_distribution.competitive.count == 0
    assert payload.tier_distribution.participating.count == 0
    assert payload.tier_distribution.elite_floor_met is False
    # Emission-share constants still set so payload is self-describing.
    assert payload.tier_distribution.elite.share_of_emissions == 0.60
    assert payload.tier_distribution.competitive.share_of_emissions == 0.30
    assert payload.tier_distribution.participating.share_of_emissions == 0.10


def test_zero_pool_emits_full_payload_with_zeros():
    """Q14: even a zero-pool epoch gets POSTed with the right defaults."""
    artifact = _artifact(n_qualifying=0)
    payload = aggregate(artifact)
    assert payload.pool_size == 0
    assert payload.pool_size_below_distribution_floor is True
    assert payload.tier_split_active is False
    assert payload.score_distribution is None
    assert payload.baseline_beat_rate == 0.0


def test_pool_at_exact_floor_activates_tier_split():
    """pool_size == 15 is the boundary; tier split active."""
    artifact = _artifact(n_qualifying=15)
    payload = aggregate(artifact)
    assert payload.pool_size == 15
    assert payload.tier_split_active is True
    assert payload.pool_size_below_distribution_floor is False
    assert payload.score_distribution is not None


# ----------------------------------------------------------------------------
# K-anonymity passthrough
# ----------------------------------------------------------------------------

def test_histogram_k_anonymity_holds_in_payload():
    """Every bin in the published histogram is 0 or >=5."""
    artifact = _artifact(n_qualifying=20)
    payload = aggregate(artifact)
    assert payload.score_distribution is not None
    for count in payload.score_distribution.bin_counts:
        assert count == 0 or count >= 5


def test_histogram_total_count_equals_pool_size():
    artifact = _artifact(n_qualifying=25)
    payload = aggregate(artifact)
    assert payload.score_distribution is not None
    assert sum(payload.score_distribution.bin_counts) == 25


# ----------------------------------------------------------------------------
# Baseline beat rate
# ----------------------------------------------------------------------------

def test_baseline_beat_rate_is_one_when_all_qualifying_above_zero():
    """v1 simplified: every qualifying miner has raw_score > 0 baseline."""
    artifact = _artifact(n_qualifying=20, raw_score_base=0.5)
    payload = aggregate(artifact)
    assert payload.baseline_beat_rate == 1.0


def test_baseline_beat_rate_zero_with_empty_pool():
    artifact = _artifact(n_qualifying=0)
    payload = aggregate(artifact)
    assert payload.baseline_beat_rate == 0.0


def test_baseline_beat_rate_handles_zero_scores():
    """Miners with raw_score == 0 do NOT count as beating baseline."""
    artifact = _artifact(n_qualifying=20, raw_score_base=0.0, raw_score_step=0.02)
    # First miner has raw_score == 0, the other 19 are positive
    payload = aggregate(artifact)
    assert payload.baseline_beat_rate == 19.0 / 20.0


# ----------------------------------------------------------------------------
# Determinism / structural invariants
# ----------------------------------------------------------------------------

def test_aggregate_is_deterministic():
    """Two calls with the same artifact produce identical payloads."""
    artifact = _artifact(n_qualifying=20)
    p1 = aggregate(artifact)
    p2 = aggregate(artifact)
    assert p1 == p2


def test_aggregate_is_pure_no_artifact_mutation():
    """The artifact passed in must not be mutated by aggregation."""
    artifact = _artifact(n_qualifying=20)
    snapshot = dataclasses.asdict(artifact)
    aggregate(artifact)
    assert dataclasses.asdict(artifact) == snapshot


def test_payload_model_dump_is_json_safe():
    """The payload should round-trip through JSON without loss."""
    import json
    payload = aggregate(_artifact(n_qualifying=25))
    serialized = payload.model_dump()
    json_str = json.dumps(serialized)
    restored = EpochReportPayload(**json.loads(json_str))
    assert restored == payload


def test_payload_aggregate_fields_carry_no_per_miner_data():
    """v3 contract change scope check: per-UID data is allowed in
    ``miner_results`` (and ONLY there). The aggregate-only fields —
    score_distribution, tier_distribution, top_n_scores — must remain
    structurally free of UID/hotkey/per-miner content.
    """
    payload = aggregate(_artifact(n_qualifying=25))
    serialized = payload.model_dump()

    forbidden = ("uid", "hotkey", "miner_id", "ss58", "per_miner")
    allow_keys = {"total_registered_uids"}
    # miner_results is the explicit per-UID surface (v3) — its content
    # is allowed to carry uid/hotkey/score per Rob's spec.
    skip_subtrees = {"miner_results"}

    def walk(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                key_path = f"{path}.{k}" if path else k
                if k in skip_subtrees:
                    continue  # don't recurse into the per-UID table
                if k not in allow_keys:
                    for sub in forbidden:
                        assert sub not in k.lower(), (
                            f"forbidden substring {sub!r} in payload key {key_path!r}"
                        )
                walk(v, key_path)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                walk(item, f"{path}[{i}]")

    walk(serialized)


# ----------------------------------------------------------------------------
# Tier counts pass through correctly when active
# ----------------------------------------------------------------------------

def test_tier_counts_match_artifact_when_split_active():
    """When tier split is active, counts come straight from artifact.tier_result."""
    artifact = _artifact(n_qualifying=20)
    elite_n = len(artifact.tier_result["elite"])
    comp_n = len(artifact.tier_result["competitive"])
    part_n = len(artifact.tier_result["participating"])

    payload = aggregate(artifact)
    assert payload.tier_distribution.elite.count == elite_n
    assert payload.tier_distribution.competitive.count == comp_n
    assert payload.tier_distribution.participating.count == part_n
    # share_of_pool = count / pool_size
    assert payload.tier_distribution.elite.share_of_pool == elite_n / 20
    assert payload.tier_distribution.competitive.share_of_pool == comp_n / 20
    assert payload.tier_distribution.participating.share_of_pool == part_n / 20


# ----------------------------------------------------------------------------
# v2: top_n_scores + finer histogram bins
# ----------------------------------------------------------------------------

class TestTopNScores:
    """Top-N ranked scores — payload-only, no UIDs, max_length=20."""

    def test_top_n_present_for_full_pool(self):
        artifact = _artifact(n_qualifying=25)
        payload = aggregate(artifact)
        assert payload.top_n_scores is not None
        assert len(payload.top_n_scores) == 20  # capped at TOP_N_SCORES_MAX
        # Descending order
        assert payload.top_n_scores == sorted(payload.top_n_scores, reverse=True)
        # No score appears that isn't in the artifact's qualifying pool
        artifact_scores = {entry["raw_score"] for entry in artifact.per_uid_scores}
        for s in payload.top_n_scores:
            assert s in artifact_scores

    def test_top_n_smaller_than_cap_returns_all(self):
        """Pool of 15 → top_n_scores has length 15 (all scores)."""
        artifact = _artifact(n_qualifying=15)
        payload = aggregate(artifact)
        assert payload.top_n_scores is not None
        assert len(payload.top_n_scores) == 15
        assert payload.top_n_scores == sorted(payload.top_n_scores, reverse=True)

    def test_top_n_none_when_pool_below_floor(self):
        """Pool below distribution floor (15) → top_n_scores None, like the histogram."""
        artifact = _artifact(n_qualifying=10)
        payload = aggregate(artifact)
        assert payload.pool_size_below_distribution_floor is True
        assert payload.score_distribution is None
        assert payload.top_n_scores is None

    def test_top_n_omitted_when_top_n_is_zero(self):
        """Explicit top_n=0 omits the field even for full pools."""
        artifact = _artifact(n_qualifying=25)
        payload = aggregate(artifact, top_n=0)
        assert payload.top_n_scores is None

    def test_top_n_respects_explicit_cap(self):
        artifact = _artifact(n_qualifying=25)
        payload = aggregate(artifact, top_n=5)
        assert payload.top_n_scores is not None
        assert len(payload.top_n_scores) == 5

    def test_top_n_schema_caps_at_20(self):
        """Even if aggregate caller passes top_n>20, payload's max_length=20 rejects."""
        import pydantic
        from hope.reporting.payload import EpochReportPayload
        # The schema-level cap is the source of truth for wire shape.
        try:
            EpochReportPayload(
                epoch_id="X",
                epoch_type="Search",
                epoch_subtype=None,
                block_range_start=0,
                block_range_end=0,
                scoring_formula_version="1",
                scoring_formula_commit="a" * 40,
                horizon_set=["7d"],
                epoch_type_multiplier=1.0,
                pool_size=21,
                total_registered_uids=21,
                pool_size_below_distribution_floor=False,
                baseline_beat_rate=0.5,
                score_distribution=None,
                tier_distribution=__import__(
                    "hope.reporting.payload", fromlist=["TierDistribution"],
                ).TierDistribution(
                    elite=__import__(
                        "hope.reporting.payload", fromlist=["TierSlice"],
                    ).TierSlice(count=0, share_of_pool=0.0, share_of_emissions=0.6),
                    competitive=__import__(
                        "hope.reporting.payload", fromlist=["TierSlice"],
                    ).TierSlice(count=0, share_of_pool=0.0, share_of_emissions=0.3),
                    participating=__import__(
                        "hope.reporting.payload", fromlist=["TierSlice"],
                    ).TierSlice(count=21, share_of_pool=1.0, share_of_emissions=0.1),
                    elite_floor_met=False,
                ),
                tier_split_active=True,
                emergency_intervention=__import__(
                    "hope.reporting.payload", fromlist=["EmergencyIntervention"],
                ).EmergencyIntervention(triggered=False),
                validator_output_snapshot_timestamp="2026-05-26T00:00:00+00:00",
                chain_fetch_timestamp="2026-05-26T00:00:00+00:00",
                top_n_scores=[0.5] * 21,  # 21 entries > 20 cap
            )
        except pydantic.ValidationError:
            pass
        else:
            raise AssertionError("Pydantic should have rejected top_n_scores with length 21")


class TestSupersedes:
    """Correction pointer — passes through to the payload's supersedes field."""

    def test_supersedes_defaults_to_none(self):
        artifact = _artifact(n_qualifying=20)
        payload = aggregate(artifact)
        assert payload.supersedes is None

    def test_supersedes_passed_through(self):
        artifact = _artifact(n_qualifying=20, epoch_id="WR-2026-W21-COR-1")
        payload = aggregate(artifact, supersedes="WR-2026-W21-PUB-E1")
        assert payload.supersedes == "WR-2026-W21-PUB-E1"
        assert payload.epoch_id == "WR-2026-W21-COR-1"


class TestMinerResults:
    """v3 per-UID table — one entry per scored miner."""

    def test_miner_results_present_for_full_pool(self):
        artifact = _artifact(n_qualifying=20)
        payload = aggregate(artifact)
        assert payload.miner_results is not None
        assert len(payload.miner_results) == 20

    def test_miner_result_fields_populated(self):
        artifact = _artifact(n_qualifying=20)
        payload = aggregate(artifact)
        sample = payload.miner_results[0]
        assert isinstance(sample.uid, int)
        assert 0 <= sample.uid <= 255
        assert isinstance(sample.hotkey, str)
        # The wire contract publishes the chain SS58 (payload.py: "hotkey
        # carries the full SS58"); the aggregator encodes the artifact's
        # 64-char hex pubkey at the publish boundary.
        assert sample.hotkey.startswith("5")
        assert 47 <= len(sample.hotkey) <= 48
        assert 0.0 <= sample.score <= 1.0
        assert sample.status == "scored"
        assert sample.tier in ("elite", "competitive", "participating", None)

    def test_miner_results_tier_mapping(self):
        """Every scored miner is mapped to a tier when the split is active."""
        artifact = _artifact(n_qualifying=20)
        payload = aggregate(artifact)
        assert payload.tier_split_active is True
        tiers = [mr.tier for mr in payload.miner_results]
        assert tiers.count("elite") == 4
        assert tiers.count("competitive") == 8
        assert tiers.count("participating") == 8
        assert tiers.count(None) == 0

    def test_miner_results_tier_none_when_split_inactive(self):
        """Below-floor pool → tier_split_active=False → all tier=None."""
        artifact = _artifact(n_qualifying=10)
        payload = aggregate(artifact)
        assert payload.pool_size_below_distribution_floor is True
        assert payload.tier_split_active is False
        # miner_results still present (per Rob's spec — optional, but
        # we always emit when we have data). All entries have tier=None.
        assert payload.miner_results is not None
        assert len(payload.miner_results) == 10
        assert all(mr.tier is None for mr in payload.miner_results)

    def test_miner_results_score_clamped_to_range(self):
        """Schema enforces 0 ≤ score ≤ 1; clamping handles out-of-range
        artifact entries."""
        artifact = _artifact(n_qualifying=20)
        # Tamper with one entry to have a slightly-over-1 raw_score
        # (simulates floating-point edge cases).
        artifact.per_uid_scores[0] = dict(artifact.per_uid_scores[0])
        artifact.per_uid_scores[0]["raw_score"] = 1.00001
        payload = aggregate(artifact)
        # Should still produce a valid payload.
        assert payload.miner_results is not None
        # The clamped entry exists at score=1.0. Published rows carry the
        # SS58 form, so match against the encoded artifact hotkey.
        first_hotkey = _hotkey_to_ss58(artifact.per_uid_scores[0]["hotkey"])
        match = next(
            mr for mr in payload.miner_results if mr.hotkey == first_hotkey
        )
        assert match.score == 1.0

    def test_excluded_miners_appear_with_disqualified_status(self):
        """tier_result.excluded entries → disqualified_* rows."""
        artifact = _artifact(n_qualifying=20)
        # Add an "excluded" entry pointing at a known hotkey from the
        # per_uid_scores list (so the uid lookup succeeds).
        excluded_hotkey = artifact.per_uid_scores[0]["hotkey"]
        artifact.tier_result["excluded"] = {
            excluded_hotkey: "inner_sig_hotkey_mismatch",
        }
        payload = aggregate(artifact)
        # The excluded entry adds one MORE row (the scored row stays too
        # because we don't dedupe — but the tier table accuracy comes
        # from tier_result, not per_uid_scores).
        excluded_rows = [
            mr for mr in payload.miner_results
            if mr.status != "scored"
        ]
        assert len(excluded_rows) == 1
        assert excluded_rows[0].status == "disqualified_invalid_commit"


class TestAggregatorV4:
    def test_aggregator_version_is_4(self):
        artifact = _artifact(n_qualifying=20)
        payload = aggregate(artifact)
        assert payload.aggregator_version == 4

    def test_synthesized_dq_row_via_per_uid_status_field(self):
        """When a per_uid_scores entry carries an explicit `status` field,
        the aggregator emits a DQ row with tier=None and the mapped enum."""
        artifact = _artifact(n_qualifying=20)
        # Add a disqualified row directly in per_uid_scores with status set
        # — simulates a synthesized DQ entry from chain reconstruction.
        artifact.per_uid_scores.append({
            "uid": 200,
            "hotkey": ("d" * 62) + "C8",
            "score_micro": 0,
            "raw_score": 0.0,
            "status": "not_registered",
        })
        payload = aggregate(artifact)
        # Find the synthesized DQ row.
        dq = next(
            mr for mr in payload.miner_results if mr.uid == 200
        )
        assert dq.status == "disqualified_not_registered"
        assert dq.tier is None
        assert dq.score == 0.0

    def test_late_submission_mapping(self):
        artifact = _artifact(n_qualifying=20)
        artifact.per_uid_scores.append({
            "uid": 201,
            "hotkey": ("e" * 62) + "C9",
            "score_micro": 0,
            "raw_score": 0.0,
            "status": "late_submission",
        })
        payload = aggregate(artifact)
        dq = next(mr for mr in payload.miner_results if mr.uid == 201)
        assert dq.status == "disqualified_late_submission"
        assert dq.tier is None

    def test_unknown_status_maps_to_other(self):
        artifact = _artifact(n_qualifying=20)
        artifact.per_uid_scores.append({
            "uid": 202,
            "hotkey": ("f" * 62) + "CA",
            "score_micro": 0,
            "raw_score": 0.0,
            "status": "some_unknown_new_reason_code",
        })
        payload = aggregate(artifact)
        dq = next(mr for mr in payload.miner_results if mr.uid == 202)
        assert dq.status == "disqualified_other"

    def test_canonical_status_passes_through_unchanged(self):
        """Upstream may already emit the canonical `disqualified_*` form."""
        artifact = _artifact(n_qualifying=20)
        artifact.per_uid_scores.append({
            "uid": 203,
            "hotkey": ("a" * 62) + "CB",
            "score_micro": 0,
            "raw_score": 0.0,
            "status": "disqualified_not_registered",
        })
        payload = aggregate(artifact)
        dq = next(mr for mr in payload.miner_results if mr.uid == 203)
        assert dq.status == "disqualified_not_registered"

    def test_default_histogram_bins_is_20(self):
        """v2 default histogram resolution. Large enough pool so k-anon
        doesn't collapse everything — checking the post-merge count
        reflects the 20-bin starting point."""
        artifact = _artifact(
            n_qualifying=60,
            raw_score_base=0.0,
            raw_score_step=0.015,  # spreads scores 0.0 → 0.885 across the 60
        )
        payload = aggregate(artifact)
        # Before k-anon merge: 20 bins. After merge: bin count ≤ 20, but
        # for a 60-score pool spread over the full range we should see
        # multiple bins survive (vs the v1 default of 15 that aggregated
        # the same input into fewer post-merge bins).
        assert payload.score_distribution is not None
        edges = payload.score_distribution.bin_edges
        assert len(edges) >= 2
        # Edges always start at 0 and end at the score_range max
        assert edges[0] == 0.0
        assert edges[-1] == 1.0


# ----------------------------------------------------------------------------
# epoch_membership_uids — re-run scoped to an eligible cohort (e.g. W21-16)
# ----------------------------------------------------------------------------

def test_membership_filter_flags_non_members_not_in_epoch():
    artifact = _artifact(n_qualifying=20)  # uids 0..19 all scored
    eligible = {0, 1, 2, 3, 4}
    payload = aggregate(artifact, epoch_membership_uids=eligible)
    by_uid = {m.uid: m for m in payload.miner_results}
    scored = [m for m in payload.miner_results if m.status == "scored"]
    flagged = [m for m in payload.miner_results if m.status == "disqualified_not_in_epoch"]
    assert {m.uid for m in scored} == eligible          # only the cohort is scored
    assert len(flagged) == 15                            # the rest are flagged, still shown
    assert all(m.tier is None for m in flagged)          # DQ rows carry no tier
    # a flagged row keeps its uid + hotkey (shown), just not credited as scored
    assert by_uid[10].status == "disqualified_not_in_epoch"


def test_membership_filter_absent_scores_everyone():
    artifact = _artifact(n_qualifying=20)
    payload = aggregate(artifact)  # no membership → unchanged behaviour
    assert all(m.status == "scored" for m in payload.miner_results)
