"""The daily training bundle must be leak-free by construction.

Two invariants carry the whole safety story: an example is only emitted once it
has a SETTLED label (an unlabelled row can't train anything and its future
labels must never appear early), and the input a miner trains on is exactly the
input it predicts on — never the held-back validator-only outcome.
"""
from hope.backtest.daily_training_bundle import build_records, validate_leakfree


def _ep(eid, extra=None):
    ep = {"episode_id": eid, "payload": {"x": 1}, "transition_key": "BUDGET:up",
          "validator_only_outcomes": {"secret": 9}}
    if extra:
        ep.update(extra)
    return ep


def _out(eid, h, cost=0.1):
    return {"episode_id": eid, "horizon_days": h, "cost_delta_pct": cost,
            "conversions_delta_pct": 0.2, "efficiency_delta_pct": 0.05,
            "goal_basis": "cpa", "finalized_on": "2026-08-16"}


def test_only_episodes_with_settled_labels_are_emitted():
    eps = {"A": _ep("A"), "B": _ep("B")}          # B has no outcomes
    outs = {"A": [_out("A", 7)]}
    recs = build_records(eps, outs)
    assert [r["episode_id"] for r in recs] == ["A"]


def test_input_never_carries_the_validator_only_outcome():
    recs = build_records({"A": _ep("A")}, {"A": [_out("A", 7)]})
    assert "validator_only_outcomes" not in recs[0]["input"]
    assert set(recs[0]["input"]) == {"episode_id", "payload", "transition_key"}


def test_labels_carry_only_settled_horizons():
    # 7 and 14 settled; 28 not present -> must not appear
    recs = build_records({"A": _ep("A")}, {"A": [_out("A", 7), _out("A", 14)]})
    assert set(recs[0]["labels"]) == {"7", "14"}
    assert recs[0]["labels"]["7"]["cost_delta_pct"] == 0.1


def test_label_carries_the_expected_fields():
    recs = build_records({"A": _ep("A")}, {"A": [_out("A", 7)]})
    lab = recs[0]["labels"]["7"]
    assert set(lab) == {"cost_delta_pct", "conversions_delta_pct",
                        "efficiency_delta_pct", "goal_basis", "finalized_on"}


def test_records_are_deterministically_ordered():
    eps = {"B": _ep("B"), "A": _ep("A")}
    outs = {"A": [_out("A", 7)], "B": [_out("B", 7)]}
    recs = build_records(eps, outs)
    assert [r["episode_id"] for r in recs] == ["A", "B"]


def test_leakfree_passes_clean_records():
    recs = build_records({"A": _ep("A")}, {"A": [_out("A", 7)]})
    assert validate_leakfree(recs) == []


def test_leakfree_catches_leaked_field_and_bad_horizon():
    bad = [{"episode_id": "X", "input": {"validator_only_outcomes": 1}, "labels": {"7": {}}},
           {"episode_id": "Y", "input": {}, "labels": {"99": {}}}]
    problems = validate_leakfree(bad)
    assert len(problems) == 2
    assert any("leaked" in p for p in problems)
    assert any("unexpected horizon 99" in p for p in problems)
