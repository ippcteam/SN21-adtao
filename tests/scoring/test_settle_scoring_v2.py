"""Settle-scoring v2 — the spec-restored formula, the goal cascade, the guard.

v1's tests (test_settle_day_flow.py) are the frozen contract for the shipped
formula and must keep passing untouched with the flag off. These cover what
the operator ratified on 2026-07-31: score each account against its own goal, restore
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
    _has_frozen_basis_column,
    basis_for_row,
    entry_components,
    entry_components_v2,
    operator_outcomes_provider,
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


# ---- the goal cascade (governance ruling 2026-07-31) ---------------------------------------

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


# ---- freeze at reveal --------------------------------------------------------
#
# The operator platform now decides the basis at REVEAL and persists it on
# bittensor_episode_candidates (migration 20260801_btc_goal_basis,
# app/services/bittensor/goal_basis_service.py). This reader must PREFER that
# value: the tables resolve_goal_basis reads keep moving after reveal, so
# recomputing when a frozen value exists would re-open the gap the freeze
# closes. resolve_goal_basis stays as the pre-freeze fallback — 4,377
# candidates were reserved before the freeze shipped and are deliberately not
# backfilled, since writing today's answer onto them would assert a freeze
# that never happened.

def test_frozen_basis_wins_and_nothing_is_recomputed():
    """Every recompute input says CPA; the frozen value says conversion_value.
    The frozen value must win, or the freeze is decorative."""
    basis, guarded, was_frozen = basis_for_row(
        "conversion_value", False,
        goal_metric_type="CPA", taxonomy_root="home_services",
        baseline_conv_value_micros=0)
    assert (basis, guarded, was_frozen) == ("conversion_value", False, True)


def test_frozen_basis_wins_even_when_the_live_cascade_would_say_value():
    """The mirror case: a retag to `retail` after reveal must not upgrade an
    episode that was revealed as CPA."""
    basis, _guarded, was_frozen = basis_for_row(
        "cpa", False, None, "retail", 5_000_000)
    assert (basis, was_frozen) == ("cpa", True)


def test_frozen_guard_flag_is_carried_not_re_derived():
    """A frozen row that was guarded stays guarded even though the baseline has
    since settled — the veto is part of what was frozen."""
    _basis, guarded, was_frozen = basis_for_row(
        "cpa", True, "TARGET_ROAS", "retail", 9_000_000)
    assert (guarded, was_frozen) == (True, True)


@pytest.mark.parametrize("frozen", [None, ""])
def test_null_frozen_basis_falls_back_to_the_live_cascade(frozen):
    """Pre-freeze episodes must still score, not crash."""
    assert basis_for_row(frozen, None, "TARGET_ROAS", None, 5_000_000) == (
        "conversion_value", False, False)
    assert basis_for_row(frozen, None, None, "retail", 0) == ("cpa", True, False)
    assert basis_for_row(frozen, None, None, None, 0) == ("cpa", False, False)


def test_fallback_is_exactly_resolve_goal_basis():
    """The fallback must not become a second, drifting implementation."""
    for args in [("TARGET_ROAS", "retail", 0), (None, "retail", 5),
                 ("CPA", "retail", 5), (None, None, 0)]:
        basis, guarded, was_frozen = basis_for_row(None, None, *args)
        assert (basis, guarded) == resolve_goal_basis(*args)
        assert was_frozen is False


def test_pre_migration_database_recomputes_instead_of_crashing():
    """A validator host whose operator database predates 20260801_btc_goal_basis
    must degrade to recomputation, not `column c.goal_basis does not exist`."""
    class _Boom:
        def execute(self, *_a, **_k):
            raise RuntimeError("no such table information_schema.columns")

    assert _has_frozen_basis_column(_Boom()) is False


# ---- the provider, RUN (not grepped) ----------------------------------------
#
# operator_outcomes_provider reaches into the operator platform package for its session. The fake
# below is injected as `app.models` before the provider's own import runs, and
# it behaves like the database it stands in for: when the freeze migration is
# absent, selecting c.goal_basis raises UndefinedColumn exactly as Postgres
# does. That is what makes "degrades to recomputation" a test rather than an
# assertion about source text.

class _FakeRow:
    def __init__(self, **kw):
        self._mapping = kw


class _FakeUndefinedColumn(RuntimeError):
    pass


class _FakeSession:
    def __init__(self, rows, columns_present=True):
        self.rows = rows
        self.columns_present = columns_present
        self.sql_seen = []

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.sql_seen.append(sql)
        session = self

        class _R:
            def fetchall(_self):
                if "c.goal_basis" in sql and not session.columns_present:
                    raise _FakeUndefinedColumn(
                        'column c.goal_basis does not exist')
                return session.rows

            def scalar(_self):
                if "information_schema.columns" in sql:
                    return 1 if session.columns_present else None
                return 0
        return _R()


def _install_fake_obi(monkeypatch, session):
    import sys
    import types

    module = types.ModuleType("app.models")

    class _CM:
        def __enter__(self_inner):
            return session

        def __exit__(self_inner, *exc):
            return False

    module.get_session = lambda: _CM()
    monkeypatch.setitem(sys.modules, "app", types.ModuleType("app"))
    monkeypatch.setitem(sys.modules, "app.models", module)


def _outcome_row(episode_id, frozen_basis=None, frozen_guarded=None,
                 goal_metric_type=None, taxonomy_root=None, b_cv=0,
                 cpa_delta=-10.0, cv_delta=25.0):
    from datetime import date as _date
    return _FakeRow(
        episode_id=episode_id, horizon_days=7, cost_delta=1.0, conv_delta=2.0,
        cpa_delta=cpa_delta, cv_delta=cv_delta, b_cv=b_cv,
        window_end=_date(2026, 7, 20), frozen_basis=frozen_basis,
        frozen_guarded=frozen_guarded, gads_id=1, customer_id="123",
        goal_metric_type=goal_metric_type, taxonomy_root=taxonomy_root)


def test_provider_scores_a_frozen_row_on_its_frozen_basis(monkeypatch, capsys):
    """The frozen row's efficiency must come from cv_delta even though every
    live input says cpa; the unfrozen row beside it is recomputed and lands on
    cpa_delta. One run, two provenances, both observed on the output."""
    monkeypatch.setenv("SN21_SETTLE_SCORING_V2", "true")
    session = _FakeSession([
        _outcome_row("frozen-1", frozen_basis="conversion_value",
                     frozen_guarded=False, goal_metric_type="CPA",
                     taxonomy_root="home_services"),
        _outcome_row("prefreeze-1", frozen_basis=None, taxonomy_root=None),
    ])
    _install_fake_obi(monkeypatch, session)

    from datetime import date as _date
    out = operator_outcomes_provider(_date(2026, 8, 1))

    by_id = {h.episode_id: h for h in out}
    assert by_id["frozen-1"].goal_basis == "conversion_value"
    assert by_id["frozen-1"].efficiency_delta_pct == 25.0     # cv_delta
    assert by_id["prefreeze-1"].goal_basis == "cpa"
    assert by_id["prefreeze-1"].efficiency_delta_pct == -10.0  # cpa_delta

    printed = capsys.readouterr().out
    assert "1/2 (50.0%) frozen at reveal" in printed
    assert "1/2 recomputed at settle" in printed


def test_provider_never_recomputes_over_a_frozen_value(monkeypatch):
    """A retag to `retail` after reveal must not upgrade an episode that was
    revealed as cpa — through the whole provider, not just basis_for_row."""
    monkeypatch.setenv("SN21_SETTLE_SCORING_V2", "true")
    session = _FakeSession([
        _outcome_row("frozen-cpa", frozen_basis="cpa", frozen_guarded=False,
                     taxonomy_root="retail", b_cv=9_000_000),
    ])
    _install_fake_obi(monkeypatch, session)
    from datetime import date as _date
    (settled,) = operator_outcomes_provider(_date(2026, 8, 1))
    assert settled.goal_basis == "cpa"
    assert settled.efficiency_delta_pct == -10.0


def test_provider_survives_a_database_without_the_freeze_columns(monkeypatch, capsys):
    """The pre-migration host, end to end: the probe says no, the query must
    therefore not name c.goal_basis (the fake raises UndefinedColumn if it
    does), and every row is recomputed."""
    monkeypatch.setenv("SN21_SETTLE_SCORING_V2", "true")
    session = _FakeSession([_outcome_row("old-1", taxonomy_root="retail",
                                         b_cv=9_000_000)],
                           columns_present=False)
    _install_fake_obi(monkeypatch, session)
    from datetime import date as _date
    (settled,) = operator_outcomes_provider(_date(2026, 8, 1))
    assert settled.goal_basis == "conversion_value"   # recomputed live
    assert "1/1 recomputed at settle" in capsys.readouterr().out


def test_provider_reports_an_unrecognised_frozen_basis_instead_of_crashing(monkeypatch):
    """goal_basis is written by another repo. An unknown value must appear in
    the summary, not raise KeyError inside the validator."""
    monkeypatch.setenv("SN21_SETTLE_SCORING_V2", "true")
    session = _FakeSession([_outcome_row("weird-1", frozen_basis="roas_v3")])
    _install_fake_obi(monkeypatch, session)
    from datetime import date as _date
    (settled,) = operator_outcomes_provider(_date(2026, 8, 1))
    assert settled.goal_basis == "roas_v3"
    assert settled.efficiency_delta_pct == -10.0      # falls back to cpa_delta


def test_provider_is_inert_while_the_v2_flag_is_off(monkeypatch):
    monkeypatch.setenv("SN21_SETTLE_SCORING_V2", "0")
    session = _FakeSession([_outcome_row("frozen-1",
                                         frozen_basis="conversion_value")])
    _install_fake_obi(monkeypatch, session)
    from datetime import date as _date
    (settled,) = operator_outcomes_provider(_date(2026, 8, 1))
    assert settled.goal_basis == "cpa"
    assert settled.efficiency_delta_pct == -10.0
