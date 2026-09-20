"""The admission bar sits ABOVE persistence, not level with it.

Ruled 2026-08-08. The published rules told miners the reference model "cannot
outrank anything, since admission requires beating the baseline it defines".
That was false in the code: the baseline was plain persistence — predict no
change — and the reference model is a real heuristic that clears it easily. So
reference-runners were admissible, and since a group containing the reference
is exempt from one-payer, N hotkeys running the published starter model could
each hold a seat for one model.

Raising the bar changes no published rule. It makes an existing published
statement true.
"""

from hope.backtest.gate import ADMISSION_MARGIN_OVER_BASELINE, admission_verdict


def _r(score, covered=100):
    return {"gate_score": score, "covered": covered}


def test_matching_the_baseline_is_not_admitted():
    """A model that merely reproduces persistence has added nothing."""
    assert admission_verdict(_r(0.50), _r(0.50))["admitted"] is False


def test_barely_beating_the_baseline_is_not_admitted():
    """This is the case that mattered: the starter model beats persistence by a
    little, and used to be admitted on that alone."""
    assert admission_verdict(_r(0.51), _r(0.50))["admitted"] is False


def test_clearing_the_margin_is_admitted():
    required = 0.50 * (1.0 + ADMISSION_MARGIN_OVER_BASELINE)
    assert admission_verdict(_r(required + 0.001), _r(0.50))["admitted"] is True


def test_the_verdict_publishes_the_bar_it_applied():
    """A rejected miner must be able to see what they had to clear, not just
    that they failed."""
    v = admission_verdict(_r(0.51), _r(0.50))
    assert v["required_gate_score"] == round(0.50 * 1.05, 6)
    assert v["margin_required"] == ADMISSION_MARGIN_OVER_BASELINE


def test_coverage_still_gates_independently():
    """A model can clear the margin and still fail on coverage — answering a
    handful of episodes very well is not a model."""
    assert admission_verdict(_r(0.90, covered=10), _r(0.50, covered=100))["admitted"] is False


def test_a_much_better_model_is_unaffected():
    """The margin must not become a tax on genuinely good models."""
    assert admission_verdict(_r(0.80), _r(0.50))["admitted"] is True


def test_the_gate_reports_per_horizon_on_the_same_corpus():
    """Reporting only: the decision stays on the pooled score, but every
    horizon's model and baseline score is carried so a miner can see which
    horizon is weak from the verdict itself."""
    from hope.backtest.gate import OutcomeRow, gate_by_horizon, gate_score
    outcomes = [OutcomeRow("e1", 7, 0.2, 0.1, 0.05), OutcomeRow("e1", 14, -0.1, 0.3, 0.0),
                OutcomeRow("e2", 7, 0.1, 0.1, 0.1)]
    good = {m: {"p10": -0.2, "p50": 0.1, "p90": 0.3}
            for m in ("cost_delta_pct", "conversions_delta_pct", "efficiency_delta_pct")}
    preds = {("e1", 7): good, ("e1", 14): good, ("e2", 7): good}
    base = {k: {m: {"p10": -0.1, "p50": 0.0, "p90": 0.1} for m in good} for k in preds}
    out = gate_by_horizon(outcomes, preds, base)
    assert set(out) == {"7", "14"}
    assert out["7"]["cells"] == 2 and out["14"]["cells"] == 1
    assert out["7"]["model"] == gate_score([o for o in outcomes if o.horizon_days == 7], preds)
    assert out["14"]["baseline"]["direction_accuracy"] == 0.0


def test_gate_submission_attaches_the_per_horizon_block():
    import inspect
    from hope.backtest import gate_service
    src = inspect.getsource(gate_service.gate_submission)
    assert "gate_by_horizon(outcomes, preds, base_preds)" in src
    assert '"by_horizon": by_horizon' in src
