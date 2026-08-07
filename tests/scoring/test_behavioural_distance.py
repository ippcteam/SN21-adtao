"""The scaled distance measurement.

This used to be a standalone grouping control and is no longer one: a single
distance has a single boundary, and a boundary is a target. It is now ONE of
the four signals in the lineage test, and the grouping behaviour it used to
own is covered by tests/scoring/test_anticlone_redteam.py.

What remains here is the measurement itself, which still has to be right.

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

from hope.scoring.duplication import MIN_OVERLAP_ROWS, behavioural_distance

# Test-only calibration. Production values are red-teamed, not defaulted.
TEST_TAU = 0.02


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
