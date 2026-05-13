"""Tests for hope.reporting.aggregator — the pure aggregation function."""

from __future__ import annotations

import dataclasses

from hope.reporting.aggregator import aggregate
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
    assert payload.aggregator_version == 1


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


def test_payload_carries_no_uid_or_hotkey():
    """End-to-end: no UID-shaped substring in any nested dict key
    (other than the allow-listed `total_registered_uids` aggregate count).
    """
    payload = aggregate(_artifact(n_qualifying=25))
    serialized = payload.model_dump()

    forbidden = ("uid", "hotkey", "miner_id", "ss58", "per_miner")
    allow = {"total_registered_uids"}

    def walk(obj, path=""):
        if isinstance(obj, dict):
            for k, v in obj.items():
                key_path = f"{path}.{k}" if path else k
                if k not in allow:
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
