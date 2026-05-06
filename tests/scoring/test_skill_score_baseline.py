"""Skill-score baseline selection.

Covers the launch-default ``conditional_prior`` baseline and the
fall-through paths for ``predict_zero`` and ``system_estimate``.
"""

from __future__ import annotations

from hope.protocol.outcomes import ConditionalPriorBaseline
from hope.scoring.skill_score import SkillScoreCalculator


def test_predict_zero_baseline_is_all_zero():
    calc = SkillScoreCalculator()
    pred = calc.compute_baseline_prediction("ep1", baseline_type="predict_zero")
    h7 = pred.horizons["7"]
    assert h7.cost_delta_pct.p50 == 0.0
    assert h7.conversions_delta_pct.p10 == 0.0
    assert h7.conversions_delta_pct.p90 == 0.0
    assert h7.goal_miss_probability == 0.0


def test_conditional_prior_baseline_centered_on_published_mean():
    prior = ConditionalPriorBaseline(
        campaign_type="SEARCH",
        action_type="BUDGET_CHANGE",
        t7_cost_delta_pct=12.5,
        t7_conversions_delta_pct=-3.0,
        t7_efficiency_delta_pct=4.0,
        t14_cost_delta_pct=18.0,
        t14_conversions_delta_pct=-5.5,
        t14_efficiency_delta_pct=6.0,
    )
    calc = SkillScoreCalculator()
    pred = calc.compute_baseline_prediction(
        "ep1",
        baseline_type="conditional_prior",
        conditional_prior=prior,
    )

    h7 = pred.horizons["7"]
    h14 = pred.horizons["14"]

    assert h7.cost_delta_pct.p50 == 12.5
    assert h14.cost_delta_pct.p50 == 18.0
    # P10 < P50 < P90
    assert h7.cost_delta_pct.p10 < h7.cost_delta_pct.p50 < h7.cost_delta_pct.p90
    assert h14.conversions_delta_pct.p10 < h14.conversions_delta_pct.p50 < h14.conversions_delta_pct.p90


def test_conditional_prior_falls_through_when_no_prior_provided():
    calc = SkillScoreCalculator()
    pred = calc.compute_baseline_prediction(
        "ep1",
        baseline_type="conditional_prior",
        conditional_prior=None,
    )
    # Falls back to predict_zero — no crash, all zeros.
    h7 = pred.horizons["7"]
    assert h7.cost_delta_pct.p50 == 0.0


def test_system_estimate_currently_falls_through_to_zero():
    """system_estimate is roadmap; until it lands, baseline is zero."""
    calc = SkillScoreCalculator()
    pred = calc.compute_baseline_prediction("ep1", baseline_type="system_estimate")
    assert pred.horizons["7"].cost_delta_pct.p50 == 0.0


def test_compute_skill_score_floors_at_zero():
    calc = SkillScoreCalculator()
    # Baseline beats miner → skill is negative → floored to 0
    assert calc.compute_skill_score(miner_score=0.4, baseline_score=0.6) == 0.0


def test_compute_skill_score_perfect_baseline_returns_zero():
    calc = SkillScoreCalculator()
    assert calc.compute_skill_score(miner_score=0.9, baseline_score=1.0) == 0.0


def test_compute_skill_score_miner_beats_baseline():
    calc = SkillScoreCalculator()
    # baseline_loss=0.5, miner_loss=0.2 → skill = 1 - 0.4 = 0.6
    assert abs(calc.compute_skill_score(miner_score=0.8, baseline_score=0.5) - 0.6) < 1e-9
