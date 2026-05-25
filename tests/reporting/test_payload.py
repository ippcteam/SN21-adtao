"""Tests for hope.reporting.payload — the public payload schema.

Two responsibilities:
  1. Confirm the model accepts the contract §4 example payload.
  2. Confirm structural invariants — no UID-shaped field can sneak in,
     ranges are enforced, optionality is right.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from hope.reporting.payload import (
    COMPETITIVE_EMISSION_SHARE,
    ELITE_EMISSION_SHARE,
    EpochReportPayload,
    EmergencyIntervention,
    PARTICIPATING_EMISSION_SHARE,
    POOL_SIZE_DISTRIBUTION_FLOOR,
    ScoreDistribution,
    ScoreSummary,
    TierDistribution,
    TierSlice,
)


# Canonical "valid full payload" reused across tests. Mirrors the
# contract §4 example with Q9 (string epoch_id) applied.
_VALID_PAYLOAD = {
    "epoch_id": "WR-2026-W18-PUB-E1",
    "epoch_type": "Search",
    "epoch_subtype": "campaign-level",
    "block_range_start": 4_823_091,
    "block_range_end": 4_830_290,
    "scoring_formula_version": "1.2.1",
    "scoring_formula_commit": "a" * 40,
    "horizon_set": ["7d", "14d"],
    "epoch_type_multiplier": 1.0,
    "pool_size": 47,
    "total_registered_uids": 64,
    "pool_size_below_distribution_floor": False,
    "baseline_beat_rate": 0.766,
    "score_distribution": {
        "bin_edges": [0.10, 0.20, 0.30, 0.40, 0.50, 0.60, 0.70, 0.80, 0.90],
        "bin_counts": [5, 7, 9, 11, 8, 5, 0, 2],
        "summary": {"mean": 0.42, "median": 0.39, "p25": 0.28, "p75": 0.55, "max": 0.81},
    },
    "tier_distribution": {
        "elite": {"count": 9, "share_of_pool": 0.191, "share_of_emissions": 0.60},
        "competitive": {"count": 19, "share_of_pool": 0.404, "share_of_emissions": 0.30},
        "participating": {"count": 19, "share_of_pool": 0.404, "share_of_emissions": 0.10},
        "elite_floor_met": True,
    },
    "tier_split_active": True,
    "emergency_intervention": {"triggered": False},
    "validator_output_snapshot_timestamp": "2026-05-12T14:32:18Z",
    "chain_fetch_timestamp": "2026-05-12T14:33:02Z",
    "commentary_markdown": None,
    "aggregator_version": 1,
}


def test_contract_example_validates():
    payload = EpochReportPayload(**_VALID_PAYLOAD)
    assert payload.epoch_id == "WR-2026-W18-PUB-E1"
    assert payload.aggregator_version == 1
    assert payload.score_distribution is not None
    assert payload.score_distribution.bin_counts == [5, 7, 9, 11, 8, 5, 0, 2]


def test_aggregator_version_defaults_to_two():
    """v2 hard bump — the default is now 2. v1 payloads continue to
    validate when constructed with `aggregator_version=1` explicitly
    (covered by test_contract_example_validates)."""
    minimal = {**_VALID_PAYLOAD}
    minimal.pop("aggregator_version")
    payload = EpochReportPayload(**minimal)
    assert payload.aggregator_version == 2


def test_v1_payload_still_validates():
    """Backwards-compat — historic v1 payloads must continue to parse."""
    v1_payload = {**_VALID_PAYLOAD, "aggregator_version": 1}
    payload = EpochReportPayload(**v1_payload)
    assert payload.aggregator_version == 1
    assert payload.top_n_scores is None  # v1 had no top_n_scores


def test_score_distribution_may_be_null():
    """Pool below floor → score_distribution is None (pool too small)."""
    small_pool = {
        **_VALID_PAYLOAD,
        "pool_size": 0,
        "pool_size_below_distribution_floor": True,
        "score_distribution": None,
        "tier_split_active": False,
        "tier_distribution": {
            "elite": {"count": 0, "share_of_pool": 0.0, "share_of_emissions": 0.60},
            "competitive": {"count": 0, "share_of_pool": 0.0, "share_of_emissions": 0.30},
            "participating": {"count": 0, "share_of_pool": 0.0, "share_of_emissions": 0.10},
            "elite_floor_met": False,
        },
    }
    payload = EpochReportPayload(**small_pool)
    assert payload.score_distribution is None
    assert payload.pool_size == 0
    assert payload.tier_split_active is False


def test_extra_field_at_top_level_rejected():
    """Anti-doxxing structural invariant: no surprise fields allowed."""
    sneaky = {**_VALID_PAYLOAD, "miner_uids": [1, 2, 3, 4]}
    with pytest.raises(ValidationError):
        EpochReportPayload(**sneaky)


def test_extra_field_in_tier_slice_rejected():
    """No per-miner data allowed inside tier slices either."""
    payload = {**_VALID_PAYLOAD}
    payload["tier_distribution"] = {
        **payload["tier_distribution"],
        "elite": {
            "count": 9,
            "share_of_pool": 0.191,
            "share_of_emissions": 0.60,
            "miner_hotkeys": ["5abc..."],   # forbidden
        },
    }
    with pytest.raises(ValidationError):
        EpochReportPayload(**payload)


def test_extra_field_in_score_distribution_rejected():
    """No `per_uid_scores` or similar inside the distribution."""
    payload = {**_VALID_PAYLOAD}
    payload["score_distribution"] = {
        **payload["score_distribution"],
        "raw_scores": [0.1, 0.2, 0.3],   # forbidden
    }
    with pytest.raises(ValidationError):
        EpochReportPayload(**payload)


def test_baseline_beat_rate_must_be_in_unit_interval():
    bad = {**_VALID_PAYLOAD, "baseline_beat_rate": 1.5}
    with pytest.raises(ValidationError):
        EpochReportPayload(**bad)
    bad = {**_VALID_PAYLOAD, "baseline_beat_rate": -0.1}
    with pytest.raises(ValidationError):
        EpochReportPayload(**bad)


def test_negative_pool_size_rejected():
    bad = {**_VALID_PAYLOAD, "pool_size": -1}
    with pytest.raises(ValidationError):
        EpochReportPayload(**bad)


def test_negative_block_range_rejected():
    bad = {**_VALID_PAYLOAD, "block_range_start": -1}
    with pytest.raises(ValidationError):
        EpochReportPayload(**bad)


def test_zero_or_negative_multiplier_rejected():
    bad = {**_VALID_PAYLOAD, "epoch_type_multiplier": 0.0}
    with pytest.raises(ValidationError):
        EpochReportPayload(**bad)


def test_emergency_intervention_minimal_form():
    """v1 routine: triggered=False, everything else null."""
    ei = EmergencyIntervention(triggered=False)
    assert ei.type is None
    assert ei.outcome is None
    assert ei.summary_text is None


def test_emergency_intervention_summary_length_capped():
    too_long = "x" * 281
    with pytest.raises(ValidationError):
        EmergencyIntervention(triggered=True, type="collusion", outcome="rescored",
                              summary_text=too_long)


def test_emission_share_constants_match_spec():
    """Constants mirror SN21_REWARD_MECHANISM.md §Component 2."""
    assert ELITE_EMISSION_SHARE == 0.60
    assert COMPETITIVE_EMISSION_SHARE == 0.30
    assert PARTICIPATING_EMISSION_SHARE == 0.10
    assert abs((ELITE_EMISSION_SHARE
                + COMPETITIVE_EMISSION_SHARE
                + PARTICIPATING_EMISSION_SHARE) - 1.0) < 1e-9


def test_pool_size_floor_constant():
    """Mirrors hope/validator/tiered_weights.py:MIN_MINERS_FOR_TIER_SPLIT."""
    from hope.validator.tiered_weights import MIN_MINERS_FOR_TIER_SPLIT
    assert POOL_SIZE_DISTRIBUTION_FLOOR == MIN_MINERS_FOR_TIER_SPLIT


def test_model_dump_round_trips_to_input():
    """model_dump() produces a dict the model can reconstruct from."""
    payload = EpochReportPayload(**_VALID_PAYLOAD)
    again = EpochReportPayload(**payload.model_dump())
    assert payload == again


def test_payload_is_frozen():
    """Models are immutable post-construction — guards against in-flight mutation."""
    payload = EpochReportPayload(**_VALID_PAYLOAD)
    with pytest.raises(ValidationError):
        payload.pool_size = 999   # type: ignore[misc]


def test_no_uid_shaped_field_in_declared_schema():
    """Structural check: scan declared field names for per-miner substrings.

    `total_registered_uids` is allow-listed: it's a single int count of
    metagraph entries (subnet-wide stat), not per-UID data. Everything
    else with "uid" / "hotkey" / "miner_id" / "ss58" / "per_miner" in
    the name is forbidden — those would imply per-miner leakage.
    """
    allowlist = {"total_registered_uids"}
    forbidden_substrings = ("uid", "hotkey", "miner_id", "ss58", "per_miner")

    def assert_clean(model_name: str, name: str) -> None:
        if name in allowlist:
            return
        for sub in forbidden_substrings:
            assert sub not in name.lower(), (
                f"declared field {model_name}.{name!r} contains forbidden substring {sub!r}"
            )

    for name in EpochReportPayload.model_fields.keys():
        assert_clean("EpochReportPayload", name)
    for sub_model in (ScoreDistribution, ScoreSummary, TierDistribution,
                      TierSlice, EmergencyIntervention):
        for name in sub_model.model_fields.keys():
            assert_clean(sub_model.__name__, name)
