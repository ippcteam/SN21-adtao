"""M1 backtest gate — metric properties + admission logic."""
import pytest

from hope.backtest.gate import (
    METRICS, OutcomeRow, admission_verdict, corpus_spread, gate_score,
    naive_baseline_prediction, pinball,
)


def _o(eid, h, c=0.1, v=0.2, e=-0.1):
    return OutcomeRow(eid, h, c, v, e)


def _preds_for(outcomes, fn):
    return {(o.episode_id, o.horizon_days): fn(o) for o in outcomes}


class TestPinball:
    def test_perfect_median_zero_loss(self):
        assert pinball(0.5, 0.5, 0.5) == 0.0

    def test_asymmetry(self):
        # under-prediction at q=0.9 costs more than over-prediction
        assert pinball(1.0, 0.0, 0.9) > pinball(0.0, 1.0, 0.9)


class TestGate:
    OUT = [_o("e1", 7), _o("e2", 7, c=-0.3, v=0.1, e=0.2), _o("e1", 14, c=0.05, v=-0.1, e=0.0)]

    def test_oracle_beats_baseline(self):
        spread = corpus_spread(self.OUT)
        base = _preds_for(self.OUT, lambda o: naive_baseline_prediction(spread))
        oracle = _preds_for(self.OUT, lambda o: {
            m: {"p10": getattr(o, m) - 0.01, "p50": getattr(o, m), "p90": getattr(o, m) + 0.01}
            for m in METRICS})
        b = gate_score(self.OUT, base)
        s = gate_score(self.OUT, oracle)
        assert s["gate_score"] > b["gate_score"]
        v = admission_verdict(s, b)
        assert v["admitted"] is True and v["margin"] > 0

    def test_baseline_does_not_admit_itself(self):
        spread = corpus_spread(self.OUT)
        base = _preds_for(self.OUT, lambda o: naive_baseline_prediction(spread))
        b = gate_score(self.OUT, base)
        v = admission_verdict(b, b)
        assert v["admitted"] is False  # must BEAT, not match

    def test_low_coverage_blocks_admission(self):
        spread = corpus_spread(self.OUT)
        base = _preds_for(self.OUT, lambda o: naive_baseline_prediction(spread))
        partial = {k: v for k, v in base.items() if k[0] == "e1"}
        # even a great partial model fails the coverage floor
        great_partial = {k: {m: {"p10": -0.01, "p50": 0.0, "p90": 0.01} for m in METRICS}
                         for k in partial}
        s = gate_score(self.OUT, great_partial)
        b = gate_score(self.OUT, base)
        v = admission_verdict(s, b)
        assert v["coverage_ok"] is False and v["admitted"] is False

    def test_no_overlap_returns_none(self):
        assert gate_score(self.OUT, {}) is None


class TestDirectionAbstentionRule:
    """Real-corpus lesson (2026-07-25): zero-p50 on nonzero actual = MISS,
    else the naive baseline scores dir_acc 1.0 by abstention."""

    def test_zero_prediction_is_a_direction_miss(self):
        outs = [_o("e1", 7, c=0.5, v=0.5, e=0.5)]
        zero = {("e1", 7): {m: {"p10": -0.1, "p50": 0.0, "p90": 0.1} for m in METRICS}}
        r = gate_score(outs, zero)
        assert r["direction_accuracy"] == 0.0

    def test_zero_actual_carries_no_direction(self):
        outs = [_o("e1", 7, c=0.0, v=0.0, e=0.0)]
        zero = {("e1", 7): {m: {"p10": -0.1, "p50": 0.0, "p90": 0.1} for m in METRICS}}
        r = gate_score(outs, zero)
        assert r["direction_accuracy"] == 0.0  # 0 hits / 0 questions -> 0.0, not free credit
