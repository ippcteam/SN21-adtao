"""build_daily_artifact — the daily-basket path into the reporting pipe.

The daily stream scores off-chain, so there is no EpochScoringOutcome from the
validator. build_daily_artifact reconstructs the minimal artifact from the
executor's per-hotkey standings and must flow through the SAME aggregate()
publish boundary the weekly path uses — including the daily [7,14,28] horizon
set and the hex→SS58 encoding of hotkeys.
"""

from __future__ import annotations

from substrateinterface.utils.ss58 import ss58_encode

from hope.reporting.aggregator import aggregate
from hope.reporting.epoch_artifact import build_daily_artifact


def _hk(seed: int) -> str:
    """A valid SS58 hotkey (ss58_format=42, as Bittensor uses) from a seed."""
    return ss58_encode(bytes([seed]) * 32, ss58_format=42)


def _standings():
    hk1, hk2, hk3, hk4 = _hk(1), _hk(2), _hk(3), _hk(4)
    standings = {hk1: 0.80, hk2: 0.50, hk3: 0.20}
    uid_by_hotkey = {hk1: 1, hk2: 2, hk3: 3, hk4: 4}
    return standings, uid_by_hotkey, (hk1, hk2, hk3, hk4)


def test_daily_artifact_has_daily_horizons_and_epoch_id():
    standings, uid_by_hotkey, _ = _standings()
    art = build_daily_artifact(
        standings=standings,
        uid_by_hotkey=uid_by_hotkey,
        total_registered_uids=256,
        day="2026-08-16",
        block_range_start=100,
        block_range_end=200,
    )
    assert art.epoch_id == "BD-2026-08-16"
    assert art.epoch_type == "Daily"
    # The daily contract settles at three horizons, not the weekly [7,14].
    assert art.horizon_set == ["7d", "14d", "28d"]


def test_scores_map_to_micros_and_survive_ordering():
    standings, uid_by_hotkey, _ = _standings()
    art = build_daily_artifact(
        standings=standings,
        uid_by_hotkey=uid_by_hotkey,
        total_registered_uids=256,
        day="2026-08-16",
    )
    scored = {r["uid"]: r for r in art.per_uid_scores if "status" not in r}
    assert scored[1]["score_micro"] == 800_000
    assert scored[2]["score_micro"] == 500_000
    assert scored[3]["score_micro"] == 200_000
    # All three cleared the default 0.0 baseline.
    assert all(r["met_baseline"] for r in scored.values())


def test_registered_but_unscored_becomes_a_disqualification_row():
    standings, uid_by_hotkey, (hk1, hk2, hk3, hk4) = _standings()
    art = build_daily_artifact(
        standings=standings,
        uid_by_hotkey=uid_by_hotkey,
        total_registered_uids=256,
        day="2026-08-16",
        registered_hotkeys=[hk1, hk2, hk3, hk4],  # hk4 has no standing
    )
    dq = [r for r in art.per_uid_scores if r.get("status")]
    assert len(dq) == 1
    assert dq[0]["uid"] == 4
    assert dq[0]["status"] == "not_scored_this_day"
    assert dq[0]["met_baseline"] is False


def test_flows_through_aggregate_to_ss58_payload():
    """The whole point: the daily artifact publishes through aggregate()
    unchanged — hotkeys encode to SS58, horizons stay daily."""
    standings, uid_by_hotkey, _ = _standings()
    art = build_daily_artifact(
        standings=standings,
        uid_by_hotkey=uid_by_hotkey,
        total_registered_uids=256,
        day="2026-08-16",
        block_range_start=100,
        block_range_end=200,
    )
    payload = aggregate(art)
    assert payload.epoch_id == "BD-2026-08-16"
    assert payload.horizon_set == ["7d", "14d", "28d"]
    # Every published hotkey is a chain SS58 address, not the raw-pubkey hex.
    for mr in payload.miner_results:
        assert mr.hotkey.startswith("5") and 47 <= len(mr.hotkey) <= 48


def test_missing_block_range_defaults_to_zero_not_a_crash():
    standings, uid_by_hotkey, _ = _standings()
    art = build_daily_artifact(
        standings=standings,
        uid_by_hotkey=uid_by_hotkey,
        total_registered_uids=256,
        day="2026-08-16",
    )
    # aggregate() coerces None → 0 (Field(ge=0)); must not raise.
    payload = aggregate(art)
    assert payload.block_range_start == 0
    assert payload.block_range_end == 0
