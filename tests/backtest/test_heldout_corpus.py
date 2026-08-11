"""The admission corpus must exclude what miners trained on, and must be
reproducible by anyone holding the same export."""

import json

import pytest

from hope.backtest.heldout_corpus import (
    build,
    outcome_rows,
    published_cutoff,
    select,
)


def _record(episode_id, start, horizons=(7, 14), cost=0.1):
    return {
        "episode_candidate_id": episode_id,
        "input": {
            "episode_metadata": {
                "episode_id": episode_id,
                "action_window_start": f"{start}T00:00:00+00:00",
            },
            "account_state": {"spend": 100},
        },
        "settled_outcomes": {
            str(h): {"cost_delta_pct": cost,
                     "conversions_delta_pct": 0.2,
                     "cpa_delta_pct": -0.1}
            for h in horizons
        },
    }


def _write(tmp_path, name, records):
    path = tmp_path / name
    with open(path, "w") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")
    return str(path)


def test_episodes_on_or_before_the_cutoff_are_excluded():
    records = [_record("a", "2026-06-01"), _record("b", "2026-06-28"),
               _record("c", "2026-06-29")]
    episodes, _ = select(records, cutoff="2026-06-28")
    assert [e["episode_id"] for e in episodes] == ["c"]


def test_payload_is_the_live_contract_shape():
    """episode_id at the top level beside the v2.0 blocks — what a container
    actually reads on a normal day."""
    episodes, _ = select([_record("a", "2026-07-01")], cutoff="2026-06-28")
    payload = episodes[0]
    assert payload["episode_id"] == "a"
    assert "episode_metadata" in payload and "account_state" in payload
    assert "input" not in payload


def test_selection_is_deterministic_and_spread():
    records = [_record(f"{i:03d}", "2026-07-01") for i in range(100)]
    first, _ = select(list(records), cutoff="2026-06-28", limit=10)
    second, _ = select(list(reversed(records)), cutoff="2026-06-28", limit=10)
    assert [e["episode_id"] for e in first] == [e["episode_id"] for e in second]
    # strided, not the first ten
    assert [e["episode_id"] for e in first] != [f"{i:03d}" for i in range(10)]


def test_horizon_without_truth_is_dropped_not_zero_filled():
    rows = outcome_rows("a", {"7": {"cost_delta_pct": 0.1,
                                    "conversions_delta_pct": 0.0,
                                    "cpa_delta_pct": 0.0},
                              "14": {"cost_delta_pct": None},
                              "28": {}})
    assert [r.horizon_days for r in rows] == [7]


def test_episode_with_no_settled_outcome_is_not_in_the_corpus():
    bare = _record("a", "2026-07-01")
    bare["settled_outcomes"] = {}
    episodes, outcomes = select([bare], cutoff="2026-06-28")
    assert episodes == [] and outcomes == []


def test_cutoff_is_derived_from_the_published_bundle(tmp_path):
    bundle = _write(tmp_path, "bundle.jsonl",
                    [_record("x", "2026-05-01"), _record("y", "2026-06-28")])
    assert published_cutoff(bundle) == "2026-06-28"


def test_build_refuses_without_a_cutoff(tmp_path):
    """A corpus that silently included the training bundle would still
    produce verdicts, and they would all flatter the model."""
    labelled = _write(tmp_path, "labelled.jsonl", [_record("a", "2026-07-01")])
    with pytest.raises(ValueError):
        build(labelled)


def test_build_end_to_end(tmp_path):
    bundle = _write(tmp_path, "bundle.jsonl", [_record("old", "2026-06-28")])
    labelled = _write(tmp_path, "labelled.jsonl",
                      [_record("old", "2026-06-28"), _record("new", "2026-07-05")])
    out = build(labelled, bundle_path=bundle)
    assert out["cutoff"] == "2026-06-28"
    assert out["episode_count"] == 1
    assert out["episodes"][0]["episode_id"] == "new"
    assert out["outcome_row_count"] == 2      # 7d + 14d
