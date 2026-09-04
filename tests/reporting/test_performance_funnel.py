"""Funnel block beside the performance document."""
from __future__ import annotations

import importlib.util
import pathlib

from hope.reporting.prediction_performance import build_performance_document


def _entries():
    return [
        {"episode_id": "e1", "horizon_days": 7, "miner": "A", "score": 0.6, "components": None, "transition_key": "BUDGET:up"},
        {"episode_id": "e1", "horizon_days": 7, "miner": "B", "score": 0.5, "components": None, "transition_key": "BUDGET:up"},
        {"episode_id": "e2", "horizon_days": 14, "miner": "A", "score": 0.7, "components": None, "transition_key": "BUDGET:down"},
    ]


def test_settled_episodes_counted_once_across_horizons():
    doc = build_performance_document(
        _entries(), {}, {"e1": "2026-09-01", "e2": "2026-09-01"},
        as_of="2026-09-05", winner=None, uid_of={}, cutoff_day="2026-08-20")
    assert doc["totals"]["entries"] == 3
    assert doc["totals"]["settled_episodes"] == 2


def test_funnel_block_is_pure_arithmetic():
    spec = importlib.util.spec_from_file_location(
        "rdp", pathlib.Path(__file__).resolve().parents[2] / "scripts" / "run_daily_pipeline.py")
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    api = {"success": True, "since": "2026-08-20", "until": "2026-09-04",
           "days": [{"day": "d1"}, {"day": "d2"}],
           "totals": {"captured_raw_rows": 600000, "candidates": 1571, "excluded": 528, "eligible": 548,
                      "held_out": 107, "own_accounts": 9, "served": 470, "measured_outcome_rows": 12,
                      "excluded_by_reason": {"tiny_budget": 500}}}
    doc = {"totals": {"entries": 80101, "settled_episodes": 734}}
    b = mod._funnel_block(api, doc)
    assert b["captured_raw_rows_per_day"] == 300000
    assert (b["eligible"], b["held_out"], b["own_accounts"], b["served"]) == (548, 107, 9, 470)
    assert (b["settled_episodes"], b["scored_predictions"]) == (734, 80101)
    assert b["days"] == 2 and b["excluded_by_reason"] == {"tiny_budget": 500}
