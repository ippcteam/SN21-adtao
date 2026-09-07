"""End to end: a below-floor hotkey reaches the published table as its own row.

WHY
    Between the allocation and the CMS there are four hand-offs (audit ->
    intent -> artifact -> aggregate -> payload), and the 7 Sept 2026 report
    (uid 75) showed that a hotkey can fall through all of them without any
    single step being wrong. This holds the whole chain in one place.
"""

from substrateinterface.utils.ss58 import ss58_encode

from hope.reporting.aggregator import aggregate
from hope.reporting.epoch_artifact import build_daily_artifact
from hope.reporting.payload import EpochReportPayload


def _hk(seed: int) -> str:
    return ss58_encode(bytes([seed]) * 32, ss58_format=42)


def _publish(row_extras: bool, monkeypatch):
    monkeypatch.setenv("SN21_REPORT_ROW_EXTRAS", "1" if row_extras else "0")
    hk1, hk2, hk3, young = _hk(1), _hk(2), _hk(3), _hk(9)
    standings = {hk1: 0.03, hk2: 0.01, hk3: -0.02}
    absolute = {hk1: 0.61, hk2: 0.58, hk3: 0.55}
    uid_by_hotkey = {hk1: 1, hk2: 2, hk3: 3, young: 75}
    audit = {
        "standings": {hk: {"relative": standings[hk], "absolute": absolute[hk], "rank": i + 1}
                      for i, hk in enumerate((hk1, hk2, hk3))},
        "placement": {"floor": 50, "below": {young: {"scored_predictions": 48.33,
                                                     "absolute": 0.57}}},
    }
    art = build_daily_artifact(
        standings=standings, uid_by_hotkey=uid_by_hotkey, total_registered_uids=256,
        day="2026-09-07", display_scores=absolute,
        below_floor=audit["placement"]["below"])
    payload = aggregate(art, collapse_audit=audit, earning_set={hk1, hk2, hk3})
    return payload, young


def test_the_pending_miner_has_a_row_with_its_reason(monkeypatch):
    payload, young = _publish(True, monkeypatch)
    rows = {r.uid: r for r in payload.miner_results}
    row = rows[75]
    assert row.hotkey == young
    assert row.status == "below_placement_floor"
    assert row.tier is None
    assert row.met_baseline is False
    assert row.score == 0.57 and row.absolute_score == 0.57
    assert row.relative_standing is None
    assert [p.control for p in row.policies] == ["placement_floor"]
    assert "48.3 of the 50" in row.policies[0].detail


def test_the_row_survives_serialisation_and_the_placed_rows_are_untouched(monkeypatch):
    payload, _ = _publish(False, monkeypatch)
    wire = payload.model_dump()
    pending = [r for r in wire["miner_results"] if r["status"] == "below_placement_floor"]
    assert len(pending) == 1 and "absolute_score" not in pending[0]
    EpochReportPayload.model_validate(wire)      # round-trips through the schema
    placed = [r for r in payload.miner_results if r.status == "scored"]
    assert {r.uid for r in placed} == {1, 2, 3}
    assert all(r.tier is not None for r in placed)   # the earning set is funded
