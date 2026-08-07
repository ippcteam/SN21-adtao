"""RED-TEAM GATE. Every probe from the 2026-08-07 ruling must pass before the
anti-clone controls are enabled in production.

    "If a probe takes 2+ seats, do not ship; tighten metrics / medoid rule /
     soft penalty."

The probes are adversarial by construction: each one is a way to turn one
model into two paydays, and each must yield exactly one earner. The last two
are the counterweight — genuinely different models must keep their seats, or
the control is just a tax on participation.

Parameters here are TEST calibration, not production policy. Production values
must come from the operator's own red team against real models (explicit
non-goal: "do not accept miner-proposed tau/K").
"""


from hope.scoring.coldkey_cap import apply_coldkey_cap
from hope.scoring.duplication import (
    LineageParams,
    lineage_collisions,
    pair_signals,
    same_lineage,
)

# Calibration used by these probes only.
PARAMS = LineageParams(
    corr_min=0.98,
    sign_agree_min=0.95,
    distance_max=0.05,
    disagree_max=0.10,
    disagree_delta=0.05,
    version="redteam-test",
)

N = 60


def _preds(values):
    return {f"e{i}": {"7": {"cost_delta_pct": {"p50": v}}}
            for i, v in enumerate(values)}


def _actuals(n=N, value=1.0):
    return {(f"e{i}", "7", "cost_delta_pct"): value for i in range(n)}


def _leader(n=N):
    """A model with real structure — a flat vector would make correlation
    meaningless and every probe trivially passable."""
    return [((i * 37) % 100) / 100.0 - 0.5 for i in range(n)]


def _earners(by_miner, precedence=None):
    """How many seats survive collapse."""
    groups, _ = lineage_collisions(by_miner, _actuals(), PARAMS,
                                   precedence=precedence)
    suppressed = {hk for g in groups for hk in g.copies}
    return len(set(by_miner) - suppressed)


# ---- probes that must collapse to ONE earner -------------------------------

def test_probe_exact_weight_copy_across_n_hotkeys():
    base = _leader()
    by_miner = {f"hk{i}": _preds(base) for i in range(5)}
    assert _earners(by_miner) == 1


def test_probe_epsilon_noise_on_last_decimals():
    """The evasion that defeats byte-equality."""
    base = _leader()
    by_miner = {
        "author": _preds(base),
        "clone": _preds([v + 1e-7 for v in base]),
        "clone2": _preds([v - 2e-7 for v in base]),
    }
    assert _earners(by_miner) == 1


def test_probe_hyperparameter_nudge_keeping_the_score():
    """A small uniform shift — still the same model's behaviour."""
    base = _leader()
    by_miner = {"author": _preds(base),
                "nudged": _preds([v * 1.002 for v in base])}
    assert _earners(by_miner) == 1


def test_probe_distilled_smaller_net_same_predictions():
    """Different architecture, same outputs. Behaviour is what pays."""
    base = _leader()
    by_miner = {"author": _preds(base),
                "distilled": _preds([round(v, 3) for v in base])}
    assert _earners(by_miner) == 1


def test_probe_chain_of_ten_epsilon_steps():
    """A4. Each neighbour is a hair apart; the ends are far. The medoid rule
    must not let the chain drag the whole ladder into one group NOR let the
    ladder walk clones out of the group."""
    base = _leader()
    by_miner = {f"step{i}": _preds([v + i * 0.002 for v in base])
                for i in range(10)}
    assert _earners(by_miner) == 1


def test_probe_cross_coldkey_same_image():
    """A6. Identity wash does not help — lineage is measured on behaviour,
    which does not care whose coldkey signed it."""
    base = _leader()
    by_miner = {"farmA": _preds(base), "farmB": _preds(base),
                "farmC": _preds(base)}
    assert _earners(by_miner) == 1


def test_probe_selective_perturbation_to_lift_the_mean():
    """The obvious attack on a MEAN distance: move a few rows a lot, leave the
    rest identical. The disagreement RATE is what catches it — this is why one
    scalar was not enough."""
    base = _leader()
    heavy = list(base)
    for i in range(6):                 # 10% of rows moved hard
        heavy[i] += 3.0
    by_miner = {"author": _preds(base), "spiked": _preds(heavy)}

    signals = pair_signals(_preds(base), _preds(heavy), _actuals())

    # The interesting result, and not the one expected: spiking hard enough to
    # clear the mean-distance test ALSO destroys correlation (0.23 here). The
    # signals interlock — an attacker cannot buy distance without paying in
    # similarity, which is precisely the argument for AND-of-metrics over one
    # scalar. There is no "looks identical but scores as independent" band to
    # sit in.
    assert signals.distance > PARAMS.distance_max        # escapes on distance
    assert signals.correlation < 0.5                     # …but no longer resembles the leader

    # It does take a second seat — and it is no longer a clone in any sense
    # that matters. To lift the mean past 0.05 by spiking a tenth of the rows,
    # each spiked row moves ~0.50 scaled units: a deliberate 50% error on 10%
    # of the basket, on a curve that pays 50/25/10. The seat is bought with the
    # accuracy the seat pays for.
    assert _earners(by_miner) == 2

    cost_per_spiked_row = PARAMS.distance_max * N / 6
    assert cost_per_spiked_row > 0.4, (
        "escaping on mean distance must stay expensive in prediction error; "
        "if a calibration makes it cheap, the soft-penalty fallback is needed")


def test_probe_attacker_optimises_for_seats_under_known_params():
    """Sitting just inside every threshold at once is the whole game. A model
    that keeps correlation and sign agreement to stay cheap cannot also open
    enough distance to look independent."""
    base = _leader()
    # Maximum perturbation that still holds correlation ~1: a uniform scale.
    by_miner = {"author": _preds(base),
                "optimised": _preds([v * 1.04 for v in base])}
    signals = pair_signals(_preds(base), _preds([v * 1.04 for v in base]),
                           _actuals())
    assert signals.correlation > 0.99          # kept, as the attacker wants
    assert _earners(by_miner) == 1             # and it does not buy a seat


# ---- the counterweight: honest models must keep their seats ----------------

def test_probe_two_genuinely_different_models_keep_two_seats():
    """Measured separation between independent lineages was 0.4346."""
    base = _leader()
    other = [(-v * 0.7 + 0.31) for v in base]
    by_miner = {"ours": _preds(base), "theirs": _preds(other)}
    assert _earners(by_miner) == 2


def test_probe_an_uncalibrated_parameter_set_suppresses_nobody():
    """Fail-closed on configuration: with no red-teamed numbers the control is
    OFF, not guessing. An uncalibrated threshold that decides pay is worse
    than none."""
    base = _leader()
    by_miner = {"a": _preds(base), "b": _preds(base)}
    groups, audit = lineage_collisions(by_miner, _actuals(), LineageParams())
    assert groups == [] and audit == {}


def test_probe_thin_overlap_never_costs_a_seat():
    short = {f"e{i}": {"7": {"cost_delta_pct": {"p50": 0.1}}} for i in range(5)}
    assert pair_signals(short, short, _actuals(5)) is None
    assert same_lineage(None, PARAMS) is False


# ---- Layer 1: the coldkey cap ----------------------------------------------

def test_probe_one_coldkey_many_hotkeys_takes_one_seat():
    """A5. K=1 — any higher K just publishes the farm size to build."""
    standings = {"hkA": 0.70, "hkB": 0.69, "hkC": 0.68}
    result = apply_coldkey_cap(standings, {"hkA": "ck1", "hkB": "ck1",
                                           "hkC": "ck1"})
    assert set(result.kept) == {"hkA"}
    assert result.dropped == ("hkB", "hkC")


def test_probe_the_cap_keeps_the_best_not_the_first():
    standings = {"aaa": 0.60, "zzz": 0.80}
    result = apply_coldkey_cap(standings, {"aaa": "ck", "zzz": "ck"})
    assert set(result.kept) == {"zzz"}


def test_probe_the_cap_ties_break_on_earlier_commit():
    standings = {"early": 0.70, "late": 0.70}
    result = apply_coldkey_cap(standings, {"early": "ck", "late": "ck"},
                               commit_block={"early": 100, "late": 900})
    assert set(result.kept) == {"early"}


def test_probe_distinct_coldkeys_are_untouched_by_layer_one():
    """Which is exactly why the lineage layer exists behind it."""
    standings = {"hk1": 0.7, "hk2": 0.7}
    result = apply_coldkey_cap(standings, {"hk1": "ckA", "hk2": "ckB"})
    assert set(result.kept) == {"hk1", "hk2"}
    assert result.dropped == ()


def test_probe_an_unreadable_coldkey_does_not_confiscate_a_seat():
    """Fail-safe, and a known soft edge — recorded, not papered over."""
    standings = {"known": 0.7, "unknown": 0.6}
    result = apply_coldkey_cap(standings, {"known": "ck"})
    assert set(result.kept) == {"known", "unknown"}


def test_the_cap_publishes_what_it_did():
    standings = {"hkA": 0.70, "hkB": 0.69}
    result = apply_coldkey_cap(standings, {"hkA": "ck1", "hkB": "ck1"})
    payload = result.as_dict()
    assert payload["dropped"] == ["hkB"]
    assert payload["contested"]["ck1"] == ["hkA", "hkB"]
