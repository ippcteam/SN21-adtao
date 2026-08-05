"""Gate service + M4 switch — verdict documents, key mapping, switch safety."""

from typing import ClassVar

from hope.backtest.cutover import LEGACY, SHADOW, select_standings, weight_source
from hope.backtest.gate_service import runner_predictions_to_gate_keys


class TestKeyMapping:
    def test_horizons_flatten_and_extras_dropped(self):
        preds = {"e1": {"7": {"cost_delta_pct": {"p50": 0.1},
                              "goal_miss_probability": 0.5},
                        "14": {"cost_delta_pct": {"p50": 0.2}}}}
        out = runner_predictions_to_gate_keys(preds)
        assert set(out) == {("e1", 7), ("e1", 14)}
        assert "goal_miss_probability" not in out[("e1", 7)]

    def test_bad_horizon_keys_skipped(self):
        out = runner_predictions_to_gate_keys({"e1": {"soon": {"m": {}}}})
        assert out == {}


class TestCutoverSwitch:
    LEGACY_S: ClassVar = {"m1": 0.6}
    SHADOW_S: ClassVar = {"m2": 0.8}

    def test_default_is_legacy(self, monkeypatch):
        monkeypatch.delenv("SN21_WEIGHT_SOURCE", raising=False)
        assert weight_source() == LEGACY

    def test_unrecognised_value_is_legacy(self, monkeypatch):
        monkeypatch.setenv("SN21_WEIGHT_SOURCE", "yolo")
        assert weight_source() == LEGACY

    def test_flag_alone_cannot_switch(self, monkeypatch):
        monkeypatch.setenv("SN21_WEIGHT_SOURCE", "shadow")
        src, st = select_standings(self.LEGACY_S, self.SHADOW_S,
                                   {"cutover_possible": False})
        assert src == LEGACY and st == self.LEGACY_S

    def test_flag_plus_ready_gate_switches(self, monkeypatch):
        monkeypatch.setenv("SN21_WEIGHT_SOURCE", "shadow")
        src, st = select_standings(self.LEGACY_S, self.SHADOW_S,
                                   {"cutover_possible": True})
        assert src == SHADOW and st == self.SHADOW_S

    def test_ready_gate_without_flag_stays_legacy(self, monkeypatch):
        monkeypatch.delenv("SN21_WEIGHT_SOURCE", raising=False)
        src, _ = select_standings(self.LEGACY_S, self.SHADOW_S,
                                  {"cutover_possible": True})
        assert src == LEGACY
