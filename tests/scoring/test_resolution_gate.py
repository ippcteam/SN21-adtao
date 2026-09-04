"""Measurement-resolution gate (rule amendment 2026-09-05): the operator's
derived resolution reaches settle, the April blend and episode weights apply
from a published date, receipts carry both, the relative loader uses them."""
from __future__ import annotations

from datetime import date

import pytest

from hope.scoring import standing_method
from hope.scoring.daily_score_flow import (
    HorizonResult,
    day_flow,
    entry_weight,
    horizon_entry_weight,
    resolution_in_force,
)
from hope.scoring.settle_day_flow import SettledHorizon, score_settled


class TestGateByDate:
    def test_off_means_high(self):
        assert resolution_in_force(date(2026, 9, 6), "medium", {}) == "high"

    def test_before_the_date_is_high_after_is_derived(self):
        env = {"SN21_RESOLUTION_GATE_FROM": "2026-09-06"}
        assert resolution_in_force(date(2026, 9, 5), "medium", env) == "high"
        assert resolution_in_force(date(2026, 9, 6), "medium", env) == "medium"
        assert resolution_in_force(date(2026, 9, 6), "LOW", env) == "low"
        assert resolution_in_force(date(2026, 9, 6), "weird", env) == "high"
        assert resolution_in_force(date(2026, 9, 6), None, env) == "high"


class TestWeights:
    def test_entry_weight_is_blend_times_episode_weight(self):
        assert entry_weight(7, "high") == pytest.approx(0.20)
        assert entry_weight(7, "medium") == pytest.approx(0.15 * 0.7)
        assert entry_weight(7, "low") == pytest.approx(0.0)
        assert entry_weight(28, "low") == pytest.approx(0.80 * 0.4)
        assert entry_weight(14, "medium") == pytest.approx(0.30 * 0.7)

    def test_day_flow_uses_the_resolution(self):
        d = date(2026, 9, 6)
        rs = [HorizonResult("e1", 7, "m", 0.6, d, resolution="high"),
              HorizonResult("e2", 7, "m", 0.6, d, resolution="medium"),
              HorizonResult("e3", 7, "m", 0.6, d, resolution="low")]
        ws = {e.episode_id: e.weight for e in day_flow(rs, d)}
        assert ws["e1"] == pytest.approx(0.20)
        assert ws["e2"] == pytest.approx(0.105)
        assert ws["e3"] == pytest.approx(0.0)


class TestSettlePassesResolution:
    def _outcome(self, day, res):
        return SettledHorizon(episode_id="e1", horizon_days=7, cost_delta_pct=0.1,
                              conversions_delta_pct=0.1, efficiency_delta_pct=0.1,
                              finalized_on=day, resolution=res)

    def _index(self):
        return {"e1": {"m": {"7": {"cost_delta_pct": {"p10": 0.0, "p50": 0.1, "p90": 0.2},
                                    "conversions_delta_pct": {"p10": 0.0, "p50": 0.1, "p90": 0.2},
                                    "efficiency_delta_pct": {"p10": 0.0, "p50": 0.1, "p90": 0.2}}}}}

    def test_high_before_the_gate_and_derived_after(self):
        env = {"SN21_RESOLUTION_GATE_FROM": "2026-09-06"}
        before = score_settled(self._index(), [self._outcome(date(2026, 9, 5), "medium")], environ=env)
        after = score_settled(self._index(), [self._outcome(date(2026, 9, 6), "medium")], environ=env)
        assert before[0].resolution == "high"
        assert after[0].resolution == "medium"

    def test_provider_row_default(self):
        o = SettledHorizon(episode_id="e", horizon_days=7, cost_delta_pct=0, conversions_delta_pct=0,
                           efficiency_delta_pct=0, finalized_on=date(2026, 9, 6))
        assert o.resolution == "high"


class TestReceiptAndLoader:
    def test_receipt_entries_carry_resolution_and_weight(self):
        from hope.publication.receipt_feed import build_receipt_metrics
        import inspect
        src = inspect.getsource(build_receipt_metrics)
        assert '"resolution": r.resolution' in src and '"weight": entry_weight(h, r.resolution)' in src

    def test_relative_loader_prefers_the_receipt_weight(self, tmp_path):
        import json, os
        root = str(tmp_path); d = os.path.join(root, "receipts"); os.makedirs(d)
        ents = [{"miner": "A", "episode_id": "e1", "horizon_days": 7, "score": 0.8, "finalized_on": "2026-09-06",
                 "resolution": "medium", "weight": 0.105},
                {"miner": "B", "episode_id": "e1", "horizon_days": 7, "score": 0.6, "finalized_on": "2026-09-06",
                 "resolution": "medium", "weight": 0.105},
                {"miner": "A", "episode_id": "e0", "horizon_days": 7, "score": 0.5, "finalized_on": "2026-09-06"}]
        with open(os.path.join(d, "2026-09-06.json"), "w") as f:
            json.dump({"document": {"metrics": {"entries": ents}}}, f)
        got = standing_method.load_relative_entries(root, date(2026, 9, 6))
        by_w = sorted((round(e.weight, 3), round(e.score, 3)) for e in got["A"])
        assert by_w == [(0.105, 0.1), (0.2, 0.0)]     # medium row 0.105; legacy row falls back to 0.20
