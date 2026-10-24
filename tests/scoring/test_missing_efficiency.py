"""No efficiency result for spend with zero conversions (Sir Rob, 16 Oct 2026).

An entry whose efficiency is not measurable (None) is scored on cost and
conversions only; every other entry scores exactly as before."""
from datetime import date

from hope.scoring import settle_day_flow as S

PRED = {m: {"p10": -0.2, "p50": -0.05, "p90": 0.1}
        for m in ("cost_delta_pct", "conversions_delta_pct", "efficiency_delta_pct")}
FULL = {"cost_delta_pct": -0.1, "conversions_delta_pct": 0.05, "efficiency_delta_pct": -0.12}


def test_entries_with_every_metric_score_exactly_as_before():
    # the v1 and v2 numbers for a complete entry are unchanged by the change
    assert S.score_entry(PRED, FULL) == S.score_entry(PRED, dict(FULL))
    c = S.entry_components_v2(PRED, FULL)
    assert c["direction"] is not None and c["goal"] is not None
    blended = (S.W_QUANTILE * c["quantile"] + S.W_COVERAGE * c["coverage"]
               + S.W_DIRECTION * c["direction"] + S.W_GOAL * c["goal"]) / S.W_TOTAL
    assert S.score_entry_v2(PRED, FULL) == round(max(0.0, min(1.0, blended)), 6)


def test_missing_efficiency_scores_cost_and_conversions_only():
    actual = dict(FULL, efficiency_delta_pct=None)
    two = {k: v for k, v in PRED.items() if k != "efficiency_delta_pct"}
    # v1: the same as a prediction that never mentioned efficiency, on the two metrics
    assert S.score_entry(PRED, actual) == S.score_entry(two, actual)
    c = S.entry_components_v2(PRED, actual)
    assert c["direction"] is None and c["goal"] is None
    expected = (S.W_QUANTILE * c["quantile"] + S.W_COVERAGE * c["coverage"]) / (S.W_QUANTILE + S.W_COVERAGE)
    assert S.score_entry_v2(PRED, actual) == round(expected, 6)


def test_a_missing_metric_is_never_a_direction_miss():
    actual = {"cost_delta_pct": 0.3, "conversions_delta_pct": 0.3, "efficiency_delta_pct": None}
    pred = {"cost_delta_pct": {"p10": 0.1, "p50": 0.3, "p90": 0.5},
            "conversions_delta_pct": {"p10": 0.1, "p50": 0.3, "p90": 0.5}}
    _q, d = S.entry_components(pred, actual)
    assert d == 1.0                      # two of two directions right; efficiency not counted


def test_http_provider_keeps_null_efficiency(monkeypatch):
    import io
    import json
    payload = {"outcomes": [{"episode_id": "e1", "horizon_days": 7, "cost_delta_pct": 0.1,
                             "conversions_delta_pct": -1.0, "efficiency_delta_pct": None,
                             "finalized_on": "2026-10-30", "goal_basis": "cpa"}]}

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=60: R(json.dumps(payload).encode()))
    rows = S.http_outcomes_provider("http://x", "k")(date(2026, 10, 30))
    assert rows[0].efficiency_delta_pct is None and rows[0].conversions_delta_pct == -1.0


def test_performance_page_skips_parts_it_cannot_average():
    from hope.reporting.prediction_performance import _Cell
    cell = _Cell()
    cell.add("m", 0.6, {"quantile": 0.5, "coverage": 0.4, "direction": None, "goal": None}, "e1")
    cell.add("m", 0.7, {"quantile": 0.6, "coverage": 0.5, "direction": 1.0, "goal": 0.8}, "e2")
    assert len(cell.scores) == 2 and cell.comp_n == 1
