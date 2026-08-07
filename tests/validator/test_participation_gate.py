"""The coverage gate — pricing cherry-picking.

THE ATTACK. A standing is the mean of the entries that EXIST: `score_settled`
does `if not pred: continue`, so a skipped episode creates no entry and cannot
pull an average down. Answer only the easy third of each basket and your
standing becomes the mean of your best third. On a 50/25/10 curve that is a
large gain, it needs no second hotkey, and NO anti-clone control can see it —
a cherry-picker runs a genuinely independent model, so lineage grouping,
coldkey caps and precedence are all blind. Coverage is the only thing that
prices it.

The gate itself (hope/scoring/participation.py) was written for the bridge and
had zero production importers. These tests cover connecting it, and the ways
that connection could go wrong.
"""

import json
import os
from datetime import date, timedelta

from hope.backtest import shadow
from hope.scoring.participation import ParticipationParams, bridge_multiplier, day_verdict
from hope.validator.daily_stream_weights import (
    participation_gate_enabled,
    participation_multipliers,
)

PARAMS = ParticipationParams()          # 0.75 floor, 0.5 decay, zero at 3


def _record(root, day, hotkey, episodes_in, predictions_out, ok=True):
    d = os.path.join(root, "shadow", day)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{hotkey}.jsonl"), "a") as f:
        f.write(json.dumps({
            "day": day, "hotkey": hotkey, "ok": ok, "error": None,
            "episodes_in": episodes_in, "predictions_out": predictions_out,
        }) + "\n")


# ---- the attack ------------------------------------------------------------

def test_a_cherry_picker_answering_a_third_of_the_basket_is_missed():
    assert day_verdict(300, 100, True, PARAMS) == "missed"


def test_full_coverage_is_submitted():
    assert day_verdict(300, 300, True, PARAMS) == "submitted"


def test_the_floor_is_where_it_says():
    assert day_verdict(100, 75, True, PARAMS) == "submitted"     # exactly 0.75
    assert day_verdict(100, 74, True, PARAMS) == "missed"


def test_persistent_cherry_picking_reaches_zero_weight():
    """Three consecutive days below the floor and the bridge pays nothing."""
    assert bridge_multiplier(["missed"] * 3, PARAMS) == 0.0
    assert bridge_multiplier(["missed"], PARAMS) == 0.5
    assert bridge_multiplier(["missed", "missed"], PARAMS) == 0.25


def test_a_miner_who_recovers_is_not_punished_forever():
    """Only consecutive misses at the tail count — scoring is where history
    lives, this is a participation test."""
    assert bridge_multiplier(["missed", "missed", "submitted"], PARAMS) == 1.0


# ---- the outage case that must never punish a miner ------------------------

def test_a_day_the_subnet_did_not_run_is_not_a_miss(tmp_path):
    """On 2026-08-03 a worker died and no bundle shipped. A naive gate would
    have marked every hotkey absent for OUR failure — and five mirroring
    validators would have propagated it within the hour."""
    root = str(tmp_path)
    day = date(2026, 8, 20)
    for n in (2, 1, 0):
        d = (day - timedelta(days=n)).isoformat()
        if n == 1:
            continue                     # the subnet did not run that day
        _record(root, d, "hkA", 100, 100)

    mult = participation_multipliers(root, day, environ={}, window_days=3)
    assert mult["hkA"] == 1.0


def test_an_empty_bundle_is_not_a_miss():
    """Nothing to predict means failing to predict it is not a failure — the
    published thin-day rule."""
    assert day_verdict(0, 0, True, PARAMS) == "subnet_down"


# ---- reading the ledger ----------------------------------------------------

def test_multipliers_come_from_delivered_predictions(tmp_path):
    root = str(tmp_path)
    day = date(2026, 8, 20)
    for n in (2, 1, 0):
        d = (day - timedelta(days=n)).isoformat()
        _record(root, d, "honest", 300, 300)
        _record(root, d, "picker", 300, 100)      # a third of the basket

    mult = participation_multipliers(root, day, environ={}, window_days=3)
    assert mult["honest"] == 1.0
    assert mult["picker"] == 0.0                  # three misses


def test_a_rerun_supersedes_the_run_it_repeated(tmp_path):
    """Last record wins, same discipline as day_run_status — a recovered day
    must not become a permanent penalty."""
    root = str(tmp_path)
    day = date(2026, 8, 20)
    d = day.isoformat()
    _record(root, d, "hkA", 300, 10)      # first attempt, thin
    _record(root, d, "hkA", 300, 300)     # re-run, complete
    assert shadow.day_coverage(root, d)["hkA"] == (300, 300)


def test_ok_true_with_no_predictions_still_counts_as_a_miss(tmp_path):
    """A container printing rubbish exits cleanly and records ok=True with zero
    predictions. Gating on `ok` would pay exactly the free-riding the gate
    exists to stop."""
    root = str(tmp_path)
    day = date(2026, 8, 20)
    for n in (2, 1, 0):
        _record(root, (day - timedelta(days=n)).isoformat(),
                "rubbish", 300, 0, ok=True)
    mult = participation_multipliers(root, day, environ={}, window_days=3)
    assert mult["rubbish"] == 0.0


# ---- the gate is off until the floor is published --------------------------

def test_the_gate_is_off_by_default():
    assert participation_gate_enabled({}) is False


def test_the_gate_turns_on_explicitly():
    assert participation_gate_enabled({"SN21_PARTICIPATION_GATE": "1"}) is True
    assert participation_gate_enabled({"SN21_PARTICIPATION_GATE": "false"}) is False


# ---- it must reach WEIGHTS, and only weights -------------------------------

def _alloc(participation=None):
    from hope.scoring.champion_promotion import PromotionState
    from hope.scoring.standing_ledger import ScoredEpisode
    from hope.validator.daily_stream_weights import compute_daily_allocation

    day = date(2026, 8, 20)
    entries = {
        hk: [ScoredEpisode(score=s, scored_on=day) for _ in range(300)]
        for hk, s in (("honest", 0.80), ("picker", 0.90))
    }
    return compute_daily_allocation(
        entries, day, day_episode_volume=500,
        promotion_state=PromotionState(),
        participation=participation,
    )


def test_the_multiplier_reaches_the_weight_vector():
    """A cherry-picker with the BETTER average still loses the income, which is
    the whole point — a higher mean bought by skipping is not a better model."""
    before = _alloc()
    after = _alloc({"picker": 0.0, "honest": 1.0})

    assert before.weights["picker"] > before.weights["honest"]
    assert after.weights["picker"] == 0.0
    assert after.weights["honest"] == 1.0


def test_standings_are_untouched():
    """Coverage is liveness-shaped, not accuracy-shaped. Scores are facts."""
    after = _alloc({"picker": 0.0, "honest": 1.0})
    assert abs(after.standings["picker"] - 0.90) < 1e-9


def test_weights_still_sum_to_one():
    after = _alloc({"picker": 0.5, "honest": 1.0})
    assert abs(sum(after.weights.values()) - 1.0) < 1e-9


def test_an_unknown_hotkey_is_not_penalised():
    """No coverage record means not observed missing. Absence of a measurement
    must never cost anyone their earnings."""
    after = _alloc({"picker": 1.0})          # 'honest' absent from the map
    assert after.weights["honest"] > 0


def test_no_participation_map_changes_nothing():
    assert _alloc().weights == _alloc({}).weights


def test_a_uniform_multiplier_is_no_penalty():
    """Stated so it is not mistaken for a bug: the chain renormalises whatever
    we submit, so a multiplier that shrank everyone equally cannot express a
    penalty. It bites when one miner covers less than another — the only case
    that matters."""
    assert _alloc({"picker": 0.5, "honest": 0.5}).weights == _alloc().weights
