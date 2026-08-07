"""Grouping on behaviour, not bytes.

Reported by a miner on 2026-08-07 and confirmed against the code: the exact
fingerprint test groups on sha256 of the canonical JSON, so two IDENTICAL
models need only disagree in the last decimal to be counted as separate
payees. Perturbing a clone is a one-line change. The one-payer rule was
therefore a rule about serialisation, not about models.

Their measurements, from the published 3,069-episode bundle:

    tightest pairs inside the suspicious cluster   0.0051 - 0.0457
    their own three variants (same architecture)   0.0522 - 0.0567
    their model vs the cluster's rank-1            0.4346

Honest lineages sit an order of magnitude apart; clones sit two orders
closer. These tests pin the mechanism at those real magnitudes.
"""

import pytest

from hope.scoring.duplication import (
    DEFAULT_TAU,
    MIN_OVERLAP_ROWS,
    behavioural_distance,
    distance_collisions,
)


def preds(base, n=40, jitter=0.0):
    """n episodes of one metric, each p50 = base plus a fixed offset."""
    return {f"e{i}": {"7": {"cost_delta_pct": {"p50": base + jitter}}}
            for i in range(n)}


def actuals(n=40, value=1.0):
    return {(f"e{i}", "7", "cost_delta_pct"): value for i in range(n)}


# ---- the distance itself ----------------------------------------------------

def test_identical_predictions_are_distance_zero():
    assert behavioural_distance(preds(-0.05), preds(-0.05), actuals()) == 0.0


def test_a_last_decimal_perturbation_is_still_essentially_zero():
    """THE REPORTED EVASION. Byte-equality sees two different models here;
    the distance sees the clone it is."""
    d = behavioural_distance(preds(-0.05), preds(-0.05, jitter=1e-6), actuals())
    assert d < 1e-5


def test_the_scale_matches_scoring():
    """|p50_A - p50_B| / max(|actual|, 1.0), as quantile_accuracy scales it —
    so a distance is denominated in the units the subnet pays on."""
    d = behavioural_distance(preds(0.0), preds(0.0, jitter=4.0),
                             actuals(value=2.0))
    assert d == pytest.approx(2.0)          # 4.0 gap / scale 2.0


def test_an_actual_below_one_does_not_inflate_the_distance():
    """max(|actual|, 1.0) — a near-zero actual must not divide a small gap
    into a huge one."""
    d = behavioural_distance(preds(0.0), preds(0.0, jitter=0.1),
                             actuals(value=0.001))
    assert d == pytest.approx(0.1)


def test_too_little_overlap_is_none_not_zero():
    """"Cannot tell" must be distinguishable from "identical" — absence of
    evidence must never cost somebody their earnings."""
    tiny = preds(-0.05, n=5)
    assert behavioural_distance(tiny, tiny, actuals(n=5)) is None


def test_the_overlap_floor_is_where_it_says():
    exact = preds(-0.05, n=MIN_OVERLAP_ROWS)
    assert behavioural_distance(exact, exact, actuals(n=MIN_OVERLAP_ROWS)) == 0.0
    short = preds(-0.05, n=MIN_OVERLAP_ROWS - 1)
    assert behavioural_distance(short, short,
                                actuals(n=MIN_OVERLAP_ROWS - 1)) is None


def test_only_shared_rows_are_compared():
    a = preds(-0.05, n=40)
    b = dict(preds(-0.05, n=40), extra={"7": {"cost_delta_pct": {"p50": 99.0}}})
    assert behavioural_distance(a, b, actuals()) == 0.0


# ---- grouping at the miner's measured magnitudes ---------------------------

def test_a_perturbed_clone_is_grouped():
    by_miner = {"author": preds(-0.05), "clone": preds(-0.05, jitter=0.001)}
    (group,) = distance_collisions(by_miner, actuals(), tau=DEFAULT_TAU,
                                   precedence={"author": 10, "clone": 900})
    assert group.kind == "same_behaviour"
    assert group.original == "author"
    assert group.copies == ("clone",)


def test_two_honest_lineages_are_left_alone():
    """Their model vs the cluster's rank-1 measured 0.4346 — twenty times tau."""
    by_miner = {"ours": preds(-0.05), "theirs": preds(-0.05, jitter=0.4346)}
    assert distance_collisions(by_miner, actuals(), tau=DEFAULT_TAU) == []


def test_their_own_variants_survive_the_proposed_tau():
    """Worth stating plainly: at the tau THEY proposed, the reporter's own
    three variants (0.0522 apart at the closest) stay separate payees. The
    mechanism is right; the number is a governance choice, and this test
    exists so whoever sets it can see what it does to real configurations."""
    by_miner = {"v1": preds(-0.05), "v2": preds(-0.05, jitter=0.0522),
                "v3": preds(-0.05, jitter=0.1044)}
    assert distance_collisions(by_miner, actuals(), tau=0.02) == []
    # …and a tau above their spread would collapse them.
    grouped = distance_collisions(by_miner, actuals(), tau=0.06)
    assert len(grouped) == 1 and len(grouped[0].members) == 3


def test_a_chain_of_near_clones_collapses_together():
    """Single linkage on purpose: a clone of a clone is still a clone, and
    all-pairs closeness would let a ladder of small perturbations walk out of
    any threshold."""
    by_miner = {
        "a": preds(-0.05),
        "b": preds(-0.05, jitter=0.015),
        "c": preds(-0.05, jitter=0.030),
        "d": preds(-0.05, jitter=0.045),
    }
    (group,) = distance_collisions(by_miner, actuals(), tau=DEFAULT_TAU,
                                   precedence={"a": 1, "b": 2, "c": 3, "d": 4})
    assert set(group.members) == {"a", "b", "c", "d"}
    assert group.original == "a"          # earliest commit pays


def test_the_group_carries_evidence_a_miner_can_recompute():
    by_miner = {"author": preds(-0.05), "clone": preds(-0.05, jitter=0.001)}
    (group,) = distance_collisions(by_miner, actuals(), tau=DEFAULT_TAU)
    assert "tau=0.02" in group.evidence
    assert "recomputable from the day's receipt" in group.evidence


def test_thin_overlap_never_produces_a_group():
    """Fail-safe: too few shared rows means no grouping, not a coin flip."""
    by_miner = {"a": preds(-0.05, n=4), "b": preds(-0.05, n=4)}
    assert distance_collisions(by_miner, actuals(n=4), tau=DEFAULT_TAU) == []


def test_one_miner_is_never_a_group():
    assert distance_collisions({"solo": preds(-0.05)}, actuals()) == []


def test_precedence_inside_the_group_is_the_existing_rule():
    """Earliest commit pays; the reporter's own precedence fix still governs
    who the payee is."""
    by_miner = {"late_but_lexically_first": preds(-0.05),
                "zzz_original": preds(-0.05, jitter=0.0005)}
    (group,) = distance_collisions(
        by_miner, actuals(), tau=DEFAULT_TAU,
        precedence={"zzz_original": 100, "late_but_lexically_first": 9000})
    assert group.original == "zzz_original"
