"""type_accuracy — the one aggregation behind the accuracy page, the
per-miner feedback, and the benchmark chart."""

from hope.reporting.type_accuracy import (
    MIN_N_FOR_BEST, ScoredEntry, aggregate_type_accuracy, type_family)


def _e(miner, ep, tkey, h, score):
    return ScoredEntry(miner, ep, tkey, h, score)


def test_family_bucketing():
    assert type_family("BUDGET:up_large") == "BUDGET"
    assert type_family("COMPOSITE:AD_CREATE+2") == "COMPOSITE"
    assert type_family("CAMPAIGN_PAUSE") == "CAMPAIGN_PAUSE"
    assert type_family(None) == "UNKNOWN"


def test_field_mean_and_champion_and_delta():
    entries = (
        [_e("m1", f"e{i}", "BUDGET:up", "7", 0.8) for i in range(6)]
        + [_e("m2", f"e{i}", "BUDGET:up", "7", 0.4) for i in range(6)]
    )
    out = aggregate_type_accuracy(entries, champion_miner="m2")
    cell = out["by_type"]["BUDGET"]["7"]
    assert cell["n"] == 12
    assert abs(cell["field_mean"] - 0.6) < 1e-9
    # champion (m2) shown even though m1 is best
    assert abs(cell["champion_mean"] - 0.4) < 1e-9
    assert cell["best"]["miner"] == "m1" and cell["best"]["n"] == 6
    # per-miner deltas vs field
    assert abs(out["by_miner"]["m1"]["BUDGET"]["7"]["delta"] - 0.2) < 1e-9
    assert abs(out["by_miner"]["m2"]["BUDGET"]["7"]["delta"] + 0.2) < 1e-9


def test_best_requires_min_n():
    # m1 has one lucky 1.0; m2 has MIN_N solid 0.7s — m2 must be best.
    entries = [_e("m1", "e0", "KEYWORD_ADD", "14", 1.0)] + [
        _e("m2", f"e{i}", "KEYWORD_ADD", "14", 0.7) for i in range(MIN_N_FOR_BEST)]
    out = aggregate_type_accuracy(entries)
    assert out["by_type"]["KEYWORD_ADD"]["14"]["best"]["miner"] == "m2"


def test_no_best_when_nobody_qualifies():
    out = aggregate_type_accuracy([_e("m1", "e0", "GEO_CHANGE", "28", 0.9)])
    assert out["by_type"]["GEO_CHANGE"]["28"]["best"] is None
    assert out["totals"] == {"entries": 1, "miners": 1, "types": 1}


def test_horizons_stay_separate():
    entries = [_e("m1", "e0", "BUDGET:down", "7", 0.9),
               _e("m1", "e0", "BUDGET:down", "14", 0.1)]
    out = aggregate_type_accuracy(entries)
    assert abs(out["by_type"]["BUDGET"]["7"]["field_mean"] - 0.9) < 1e-9
    assert abs(out["by_type"]["BUDGET"]["14"]["field_mean"] - 0.1) < 1e-9


def test_json_serialisable_and_deterministic():
    import json
    entries = [_e("m2", "e1", "BUDGET:up", "7", 0.5),
               _e("m1", "e2", "ASSET_CHANGE", "7", 0.6)]
    a = json.dumps(aggregate_type_accuracy(entries), sort_keys=True)
    b = json.dumps(aggregate_type_accuracy(list(reversed(entries))), sort_keys=True)
    assert a == b


def test_build_scored_entries_glue():
    from dataclasses import dataclass
    from hope.reporting.type_accuracy import build_scored_entries

    @dataclass
    class R:
        miner: str
        episode_id: str
        horizon_days: int
        score: float

    rs = [R("m1", "42", 7, 0.5), R("m1", "43", 14, 0.9)]
    entries = build_scored_entries(rs, {"42": "BUDGET:up"})
    assert entries[0].transition_key == "BUDGET:up"
    assert entries[0].horizon == "7"
    assert entries[1].transition_key == "UNKNOWN"   # missing map row buckets, never drops
