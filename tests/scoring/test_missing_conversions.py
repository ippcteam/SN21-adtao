"""Too few conversions to measure (operator rule, switched off until announced).

An outcome with no conversions result is scored on what it does have, the same
way a missing efficiency result is: never read as a 0% change."""
from datetime import date

from hope.scoring import settle_day_flow as S

PRED = {"cost_delta_pct": {"p10": -0.2, "p50": 0.0, "p90": 0.2},
        "conversions_delta_pct": {"p10": -0.5, "p50": 0.4, "p90": 0.9},
        "efficiency_delta_pct": {"p10": -0.3, "p50": 0.0, "p90": 0.3}}


def test_a_missing_conversions_result_is_skipped_not_read_as_zero():
    actual = {"cost_delta_pct": 0.05, "conversions_delta_pct": None, "efficiency_delta_pct": None}
    with_none = S.score_entry_v2(PRED, actual)
    as_zero = S.score_entry_v2(PRED, {**actual, "conversions_delta_pct": 0.0})
    cost_only = S.score_entry_v2({"cost_delta_pct": PRED["cost_delta_pct"]},
                                 {"cost_delta_pct": 0.05})
    assert with_none == cost_only and with_none != as_zero


def test_the_settled_horizon_keeps_none_from_the_endpoint():
    row = {"episode_id": "e1", "horizon_days": 7, "cost_delta_pct": 0.1,
           "conversions_delta_pct": None, "efficiency_delta_pct": None,
           "finalized_on": "2026-11-10"}
    h = S.SettledHorizon(
        episode_id=row["episode_id"], horizon_days=7, cost_delta_pct=0.1,
        conversions_delta_pct=(None if row.get("conversions_delta_pct") is None
                               else float(row["conversions_delta_pct"])),
        efficiency_delta_pct=None, finalized_on=date(2026, 11, 10))
    assert h.conversions_delta_pct is None


def test_both_providers_parse_a_missing_conversions_value_as_none():
    import inspect
    src = inspect.getsource(S)
    assert 'conversions_delta_pct=float(r["conversions_delta_pct"])' not in src
    assert 'conversions_delta_pct=float(m["conv_delta"] or 0)' not in src


# ---- the admission gate reads the same labels --------------------------------

def test_the_gate_skips_an_unmeasurable_metric_instead_of_reading_zero():
    from hope.backtest import gate as G
    pred = {("e1", 7): {"cost_delta_pct": {"p10": -0.2, "p50": 0.05, "p90": 0.2},
                        "conversions_delta_pct": {"p10": -0.9, "p50": -0.8, "p90": -0.7},
                        "efficiency_delta_pct": {"p10": -0.2, "p50": 0.0, "p90": 0.2}}}
    none_row = G.OutcomeRow("e1", 7, 0.05, None, None)
    cost_only = G.gate_score([G.OutcomeRow("e1", 7, 0.05, None, None)],
                             {("e1", 7): {"cost_delta_pct": pred[("e1", 7)]["cost_delta_pct"]}})
    assert G.gate_score([none_row], pred) == cost_only
    assert G.gate_score([G.OutcomeRow("e1", 7, 0.05, 0.0, 0.0)], pred) != cost_only
    assert G.corpus_spread([none_row])["conversions_delta_pct"] == 0.1    # nothing measured: default


def test_corpus_labels_keep_none_and_absent_still_reads_zero():
    from hope.backtest.gate import label_value
    assert label_value({"x": None}, "x") is None
    assert label_value({}, "x") == 0.0
    assert label_value({"x": "0.25"}, "x") == 0.25
