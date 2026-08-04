"""D7 weight curve — pins: no cliff at crossover, threshold zeroing,
ceiling, ratio preservation, determinism on ties."""
import pytest

from hope.scoring.weight_curve import CurveParams, curve_weights, earning_set


def test_two_miners_split_top_and_second_ratio():
    w = curve_weights({"a": 0.9, "b": 0.8})
    # ratios of the published curve preserved (Rob 2026-07-28): 0.50 : 0.25
    assert w["a"] / w["b"] == pytest.approx(0.50 / 0.25)
    assert sum(w.values()) == pytest.approx(1.0)


def test_no_cliff_crossover_is_rank_swap_not_flip_to_100(A=0.800, B=0.801):
    # strict winner-take-all would flip 100% of weight on any crossover;
    # under the curve the winner gets top_share-normalized, never 1.0.
    w = curve_weights({"a": A, "b": B})
    assert w["b"] < 1.0
    assert w["a"] > 0.0


def test_threshold_zeroes_below():
    # Inclusive semantics (audit 2026-07-29): AT the threshold earns,
    # only BELOW is zeroed.
    p = CurveParams(score_threshold=0.5)
    w = curve_weights({"good": 0.7, "bad": 0.4, "at": 0.5}, p)
    assert w["bad"] == 0.0
    assert w["at"] > 0.0
    assert w["good"] > w["at"]


def test_none_standing_gets_zero():
    w = curve_weights({"a": 0.9, "new": None})
    assert w["new"] == 0.0


def test_hard_ceiling_20():
    standings = {f"m{i:02d}": 0.9 - i * 0.001 for i in range(30)}
    w = curve_weights(standings)
    assert sum(1 for v in w.values() if v > 0) == 20


def test_geometric_tail():
    standings = {f"m{i}": 0.9 - i * 0.01 for i in range(5)}
    w = curve_weights(standings)
    ordered = [w[f"m{i}"] for i in range(5)]
    # tail: each after THIRD is half the previous (50/25/10 fixed shares)
    assert ordered[3] == pytest.approx(ordered[2] * 0.5)
    assert ordered[4] == pytest.approx(ordered[3] * 0.5)


def test_deterministic_tiebreak():
    w1 = curve_weights({"zeta": 0.8, "alpha": 0.8})
    w2 = curve_weights({"alpha": 0.8, "zeta": 0.8})
    assert w1 == w2
    assert w1["alpha"] > w1["zeta"]  # id asc wins the tie


def test_expected_earning_set_shape():
    # 12 live models: expected set 5-15 per v0.5 — all earn under the curve
    standings = {f"m{i}": 0.9 - i * 0.02 for i in range(12)}
    assert len(earning_set(standings)) == 12


def test_empty_and_all_below_threshold():
    assert curve_weights({}) == {}
    p = CurveParams(score_threshold=0.9)
    w = curve_weights({"a": 0.5}, p)
    assert w == {"a": 0.0}


def test_at_threshold_standing_earns():
    """Audit 2026-07-29: threshold is inclusive — AT the published
    threshold earns; only BELOW is zeroed."""
    w = curve_weights({"at": 0.30, "below": 0.29, "above": 0.40},
                      CurveParams(score_threshold=0.30))
    assert w["at"] > 0
    assert w["below"] == 0.0
    assert w["above"] > w["at"]
