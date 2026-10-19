"""Small-campaign conversions scored as a count (Sir Rob, 19 Oct 2026, options a + c)."""
from datetime import date

from hope.scoring import count_score as C
from hope.scoring import settle_day_flow as F

ON = {C.SWITCH_ENV: "2026-11-06"}
DAY = date(2026, 11, 10)


def pred(p50):
    return {"cost_delta_pct": {"p10": -0.2, "p50": 0.0, "p90": 0.2},
            "conversions_delta_pct": {"p10": -0.5, "p50": p50, "p90": 0.5},
            "efficiency_delta_pct": {"p10": -0.2, "p50": 0.0, "p90": 0.2}}


def test_switch_by_settle_day():
    assert C.in_force(DAY, ON) and not C.in_force(date(2026, 11, 5), ON)
    assert not C.in_force(DAY, {}) and not C.in_force(DAY, {C.SWITCH_ENV: "soon"})


def test_deviance_handles_zero_counts():
    assert C.deviance(0, 2.0) == 4.0                 # 2 x mu when nothing happened
    assert C.deviance(3, 3) == 0.0
    assert C.deviance(2, 0) > 0                       # a zero forecast is floored, not a crash


def test_no_change_scores_half_and_exact_scores_one():
    assert C.count_component(pred(0.0), 2.0, 0.0) == 0.5        # forecast == no change
    assert C.count_component(pred(0.5), 2.0, 3.0) == 1.0        # exact count
    assert C.count_component(pred(-1.0), 2.0, 2.0) < 0.5        # predicted a collapse, nothing changed
    assert C.count_component({}, 2.0, 1.0) is None              # no conversions forecast: not scored this way


def test_blend_weighs_the_count_like_cost():
    assert C.blend(0.6, 0.4) == 0.5 and C.blend(0.6, None) == 0.6


def test_pooled_sums_deviances_per_miner():
    p = C.Pooled()
    p.add("a", pred(0.0), 2.0, 0.0)
    p.add("a", pred(0.0), 1.0, 3.0)
    p.add("b", pred(0.5), 2.0, 3.0)
    p.add("c", {}, 2.0, 3.0)                           # no forecast: left out
    r = p.report()
    assert r["a"] == {"results": 2, "score": 0.5} and r["b"]["score"] == 1.0 and "c" not in r


def _outcome(conv=None, expected=2.0, actual=0.0, day=DAY):
    return F.SettledHorizon(episode_id="e1", horizon_days=7, cost_delta_pct=0.0,
                            conversions_delta_pct=conv, efficiency_delta_pct=None,
                            finalized_on=day, expected_conversions=expected,
                            actual_conversions=actual)


def test_settle_blends_the_count_only_for_small_campaigns_from_the_date():
    idx = {"e1": {"m": {"7": pred(-1.0)}}}
    base = F.score_settled(idx, [_outcome(expected=None)], environ=ON)[0].score
    with_count = F.score_settled(idx, [_outcome()], environ=ON)[0].score
    count = C.count_component(pred(-1.0), 2.0, 0.0)
    assert with_count == C.blend(base, count)
    assert F.last_count_pooled() == {"2026-11-10": {"m": {"results": 1, "score": count}}}
    off = F.score_settled(idx, [_outcome()], environ={})[0].score
    assert off == base and F.last_count_pooled() == {}    # switch off: unchanged


def test_results_with_a_conversion_percentage_are_untouched():
    idx = {"e1": {"m": {"7": pred(0.0)}}}
    a = F.score_settled(idx, [_outcome(conv=0.1)], environ=ON)[0].score
    b = F.score_settled(idx, [_outcome(conv=0.1, expected=None)], environ=ON)[0].score
    assert a == b and F.last_count_pooled() == {}


def test_pooled_score_can_be_shown_before_it_counts():
    idx = {"e1": {"m": {"7": pred(-1.0)}}}
    show_only = {C.SHOW_ENV: "2026-11-01"}
    base = F.score_settled(idx, [_outcome(expected=None)], environ=show_only)[0].score
    shown = F.score_settled(idx, [_outcome()], environ=show_only)[0].score
    assert shown == base                               # not counted yet
    assert F.last_count_pooled()["2026-11-10"]["m"]["results"] == 1   # but published
