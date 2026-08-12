"""The admission corpus from the public bundle: episodes in the live contract
shape, outcomes as gate truth, efficiency mapped from CPA."""

import json

from hope.backtest.bundle_corpus import build_from_bundle


def _bundle(tmp_path, records):
    path = tmp_path / "bundle.jsonl"
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return str(path)


def _rec(eid, cost=0.1):
    return {
        "episode_id": eid,
        "input": {"episode_metadata": {"episode_id": eid},
                  "account_state": {"spend": 100}},
        "labels": {"7": {"cost_delta_pct": cost,
                         "conversions_delta_pct": 0.2,
                         "cpa_delta_pct": -0.1},
                   "14": {"cost_delta_pct": cost * 2,
                          "conversions_delta_pct": 0.1,
                          "cpa_delta_pct": -0.05}},
    }


def test_episodes_are_in_the_live_contract_shape(tmp_path):
    eps, _ = build_from_bundle(_bundle(tmp_path, [_rec("a")]))
    assert eps[0]["episode_id"] == "a"
    assert "account_state" in eps[0]        # lifted from input
    assert "input" not in eps[0]


def test_outcomes_map_efficiency_from_cpa(tmp_path):
    _, outs = build_from_bundle(_bundle(tmp_path, [_rec("a")]))
    by_h = {o.horizon_days: o for o in outs}
    assert by_h[7].efficiency_delta_pct == -0.1     # cpa -> efficiency
    assert by_h[7].cost_delta_pct == 0.1


def test_manifest_line_and_unlabelled_skipped(tmp_path):
    recs = [{"_manifest": {"x": 1}},
            {"episode_id": "b", "input": {}, "labels": {}},   # no usable label
            _rec("c")]
    eps, outs = build_from_bundle(_bundle(tmp_path, recs))
    assert [e["episode_id"] for e in eps] == ["c"]
    assert all(o.episode_id == "c" for o in outs)


def test_limit_caps_episode_count(tmp_path):
    eps, _ = build_from_bundle(
        _bundle(tmp_path, [_rec(str(i)) for i in range(10)]), limit=3)
    assert len(eps) == 3
