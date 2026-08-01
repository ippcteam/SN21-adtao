"""Settle-scoring v2 — the spec-restored formula, the goal cascade, the guard.

v1's tests (test_settle_day_flow.py) are the frozen contract for the shipped
formula and must keep passing untouched with the flag off. These cover what
Rob ratified on 2026-07-31: score each account against its own goal, restore
the components our data can actually measure.

The test that matters most here is test_zero_baseline_is_a_free_score_without
_the_guard — it pins the exploit the guard exists to close, so nobody can
delete the guard later and still see green.
"""

import pytest

from hope.scoring.settle_day_flow import (
    DIRECTION_FLAT_BAND,
    P50_GOAL_SCALE,
    W_COVERAGE,
    W_DIRECTION,
    W_GOAL,
    W_QUANTILE,
    W_TOTAL,
    _coverage_score,
    _goal_direction_score,
    _goal_p50_score,
    entry_components,
    entry_components_v2,
    resolve_goal_basis,
    score_entry,
    score_entry_active,
    score_entry_v2,
)

METRIC_NAMES = ("cost_delta_pct", "conversions_delta_pct", "efficiency_delta_pct")


def _pred(p50, spread=0.1):
    return {m: {"p10": p50 - spread, "p50": p50, "p90": p50 + spread}
            for m in METRIC_NAMES}


def _actual(v):
    return {m: v for m in METRIC_NAMES}


# ---- the goal cascade (Rob 2026-07-31) ---------------------------------------

@pytest.mark.parametrize("metric_type", ["TARGET_ROAS", "roas", "Conversion_Value",
                                         "REVENUE", "target_roas_value"])
def test_configured_value_goal_selects_conversion_value(metric_type):
    basis, guarded = resolve_goal_basis(metric_type, None, 5_000_000)
    assert basis == "conversion_value"
    assert guarded is False


@pytest.mark.parametrize("metric_type", ["CPA", "TARGET_CPA", "COST_PER_ACQUISITION"])
def test_configured_cost_goal_selects_cpa(metric_type):
    assert resolve_goal_basis(metric_type, "retail", 5_000_000)[0] == "cpa"


def test_implied_goal_falls_through_to_taxonomy_root():
    """No configured goal: the ACCOUNT's taxonomy root implies it. This is the
    only implied signal — campaign bid strategy is deliberately not consulted."""
    assert resolve_goal_basis(None, "retail", 5_000_000)[0] == "conversion_value"
    assert resolve_goal_basis(None, "home_services", 5_000_000)[0] == "cpa"
    assert resolve_goal_basis(None, None, 5_000_000)[0] == "cpa"


def test_configured_goal_beats_taxonomy():
    """A configured CPA goal on a retail account stays CPA — configured wins."""
    assert resolve_goal_basis("CPA", "retail", 5_000_000)[0] == "cpa"


# ---- THE GUARD ---------------------------------------------------------------

@pytest.mark.parametrize("b_cv", [0, None, 0.0])
def test_zero_baseline_conversion_value_vetoes_a_value_basis(b_cv):
    """outcome_measurement_service sets cv_delta to a constant 0 when the
    baseline conversion value is 0, so a value basis there is unscoreable."""
    basis, guarded = resolve_goal_basis("TARGET_ROAS", "retail", b_cv)
    assert basis == "cpa"
    assert guarded is True


def test_guard_does_not_fire_when_account_records_value():
    basis, guarded = resolve_goal_basis(None, "retail", 1)
    assert basis == "conversion_value"
    assert guarded is False


def test_guard_never_fires_on_an_already_cpa_basis():
    """A CPA episode is untouched — the veto only ever removes a value basis,
    so `guarded` stays an honest count of episodes the guard actually moved."""
    assert resolve_goal_basis("CPA", "home_services", 0) == ("cpa", False)


def test_zero_baseline_is_a_free_score_without_the_guard():
    """REGRESSION PIN for the exploit the guard closes.

    When baseline conversion value is 0 the goal-metric truth is a constant 0.
    A miner predicting 0 then takes the goal term outright, the both-flat half
    of direction, and full coverage — a large slice of the composed score for
    a prediction carrying no information. This test asserts the exploit is
    REAL (so the guard is justified) and that resolve_goal_basis refuses to
    hand out that basis. If someone deletes the guard, the second half fails.
    """
    # spread=0 is the OPTIMAL exploiting play: a point mass on zero. Anything
    # wider bleeds quantile, so this is the score a rational exploiter takes.
    exploit = _pred(0.0, spread=0.0)
    free = entry_components_v2(exploit, _actual(0.0))
    assert free["quantile"] == 1.0        # zero loss against an immovable zero
    assert free["goal"] == 1.0            # exact P50 on a truth that cannot move
    assert free["direction"] == 0.5       # both flat — the trivial call
    assert free["coverage"] == 1.0        # a point mass on 0 "contains" 0
    # 0.9167 = (0.50 + 0.10 + 0.075 + 0.15) / 0.90, for zero information.
    assert score_entry_v2(exploit, _actual(0.0)) == pytest.approx(0.916667, abs=1e-5)

    # ...and the guard is what stops that basis being selected at all.
    assert resolve_goal_basis("TARGET_ROAS", "retail", 0)[0] == "cpa"


# ---- components --------------------------------------------------------------

def test_coverage_rewards_containment_and_penalises_width():
    tight = _coverage_score({"p10": 0.35, "p50": 0.4, "p90": 0.45}, 0.4)
    wide = _coverage_score({"p10": -0.3, "p50": 0.4, "p90": 0.7}, 0.4)
    missed = _coverage_score({"p10": 0.5, "p50": 0.6, "p90": 0.7}, 0.4)
    assert tight > wide > missed == 0.0


def test_wide_band_floors_coverage_at_half_but_quantile_punishes_it():
    """Documents the real shape of the ported legacy formula, not a hoped-for
    one: the width penalty saturates at 1.0 with a 0.5 coefficient, so ANY
    covering band keeps at least 0.5 coverage however absurd. Coverage alone
    does not deter band inflation — the 0.50-weighted quantile term does, by
    collapsing to zero. Net, the strategy loses far more than it gains."""
    wide = {"p10": -5.0, "p50": 0.0, "p90": 5.0}
    assert _coverage_score(wide, 0.4) == 0.5          # floor, not zero
    assert _coverage_score({"p10": -0.5, "p50": 0.0, "p90": 0.5}, 0.4) == 0.5

    inflated = {m: dict(wide) for m in METRIC_NAMES}
    assert entry_components_v2(inflated, _actual(0.4))["quantile"] == 0.0
    assert score_entry_v2(inflated, _actual(0.4)) < score_entry_v2(_pred(0.4), _actual(0.4))


def test_goal_score_decays_linearly_and_floors_at_zero():
    assert _goal_p50_score({"p50": 0.4}, 0.4) == 1.0
    assert _goal_p50_score({"p50": 0.4}, 0.4 + P50_GOAL_SCALE / 2) == pytest.approx(0.5)
    assert _goal_p50_score({"p50": 0.4}, 0.4 + P50_GOAL_SCALE * 2) == 0.0


def test_direction_half_credits_both_flat_and_zeroes_a_miss():
    flat = DIRECTION_FLAT_BAND / 2
    assert _goal_direction_score({"p50": 0.4}, 0.4) == 1.0        # correct, committed
    assert _goal_direction_score({"p50": flat}, flat) == 0.5      # both flat — trivial
    assert _goal_direction_score({"p50": -0.4}, 0.4) == 0.0       # wrong sign
    assert _goal_direction_score({"p50": flat}, 0.4) == 0.0       # abstained on a real move


def test_missing_metric_scores_zero_in_every_component():
    """Omission must cost: the quantile term averages only over metrics the
    miner supplied, so dropping a hard metric would otherwise be free."""
    assert _coverage_score(None, 0.4) == 0.0
    assert _goal_p50_score(None, 0.4) == 0.0
    assert _goal_direction_score(None, 0.4) == 0.0


# ---- composition -------------------------------------------------------------

def test_v2_composes_its_components_with_renormalisation():
    pred, actual = _pred(0.4), _actual(0.4)
    c = entry_components_v2(pred, actual)
    expected = (W_QUANTILE * c["quantile"] + W_COVERAGE * c["coverage"]
                + W_DIRECTION * c["direction"] + W_GOAL * c["goal"]) / W_TOTAL
    assert score_entry_v2(pred, actual) == pytest.approx(round(expected, 6))


def test_weights_are_the_spec_table_minus_the_unmeasurable_half():
    assert (W_QUANTILE, W_DIRECTION, W_GOAL) == (0.50, 0.15, 0.15)
    assert W_COVERAGE == 0.10          # half of the spec's 0.20 calibration
    assert W_TOTAL == pytest.approx(0.90)


def test_good_prediction_beats_wrong_direction_under_v2():
    assert score_entry_v2(_pred(0.4), _actual(0.4)) > score_entry_v2(_pred(-0.4), _actual(0.4))


def test_empty_prediction_still_scores_zero():
    assert score_entry_v2({}, _actual(0.4)) == 0.0


def test_direction_scores_the_goal_metric_alone_not_the_average():
    """spec:137 — directional is the goal metric only. A prediction right on
    cost and conversions but wrong on the goal metric must score 0 direction."""
    pred = _pred(0.4)
    pred["efficiency_delta_pct"] = {"p10": -0.5, "p50": -0.4, "p90": -0.3}
    actual = _actual(0.4)
    assert entry_components_v2(pred, actual)["direction"] == 0.0
    # v1 averaged all three, so it still credits the two correct metrics
    assert entry_components(pred, actual)[1] > 0.0


# ---- flag routing ------------------------------------------------------------

def test_active_scorer_follows_the_flag():
    pred, actual = _pred(0.4), _actual(0.4)
    off = score_entry_active(pred, actual, environ={})
    on = score_entry_active(pred, actual, environ={"SN21_SETTLE_SCORING_V2": "true"})
    assert off == score_entry(pred, actual)
    assert on == score_entry_v2(pred, actual)


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_flag_accepts_the_usual_truthy_spellings(raw):
    pred, actual = _pred(0.4), _actual(0.4)
    assert score_entry_active(pred, actual,
                              environ={"SN21_SETTLE_SCORING_V2": raw}) == score_entry_v2(pred, actual)


@pytest.mark.parametrize("raw", ["", "0", "false", "off", "no", " "])
def test_flag_defaults_closed(raw):
    pred, actual = _pred(0.4), _actual(0.4)
    assert score_entry_active(pred, actual,
                              environ={"SN21_SETTLE_SCORING_V2": raw}) == score_entry(pred, actual)
