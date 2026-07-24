"""Tests for hope.reporting.histogram — binning + k-anonymity merge.

Two surfaces tested:
  * `compute_histogram` / `compute_summary` — equal-width binning and
    five-number summary statistics.
  * `merge_for_k_anonymity` — the deterministic left-merge rule. The
    aggregator and any external verifier MUST produce identical bin
    shapes given the same input; tests pin every relevant edge case.
"""

from __future__ import annotations

import pytest

from hope.reporting.histogram import (
    compute_histogram,
    compute_summary,
    merge_for_k_anonymity,
)


# ----------------------------------------------------------------------------
# compute_histogram
# ----------------------------------------------------------------------------

def test_default_histogram_15_bins_over_unit_interval():
    scores = [0.0, 0.05, 0.5, 0.99, 1.0]
    edges, counts = compute_histogram(scores)
    assert len(edges) == 16
    assert len(counts) == 15
    assert edges[0] == 0.0
    assert edges[-1] == 1.0
    assert sum(counts) == len(scores)


def test_empty_scores_returns_all_zero_bins():
    edges, counts = compute_histogram([])
    assert counts == [0] * 15
    assert len(edges) == 16


def test_score_at_upper_bound_lands_in_last_bin():
    edges, counts = compute_histogram([1.0])
    assert counts[-1] == 1
    assert sum(counts) == 1


def test_score_at_lower_bound_lands_in_first_bin():
    edges, counts = compute_histogram([0.0])
    assert counts[0] == 1


def test_out_of_range_scores_are_clipped():
    edges, counts = compute_histogram([-0.1, 1.5])
    assert counts[0] == 1     # -0.1 clipped to 0.0 → first bin
    assert counts[-1] == 1    # 1.5 clipped to 1.0 → last bin
    assert sum(counts) == 2


def test_custom_n_bins_and_range():
    edges, counts = compute_histogram(
        [0.2, 0.5, 0.8], n_bins=4, range_=(0.0, 1.0),
    )
    # Bins: [0,0.25), [0.25,0.5), [0.5,0.75), [0.75,1.0]
    assert len(counts) == 4
    assert counts == [1, 0, 1, 1]


def test_invalid_n_bins_raises():
    with pytest.raises(ValueError):
        compute_histogram([0.5], n_bins=0)


def test_invalid_range_raises():
    with pytest.raises(ValueError):
        compute_histogram([0.5], range_=(1.0, 0.0))
    with pytest.raises(ValueError):
        compute_histogram([0.5], range_=(0.5, 0.5))


def test_deterministic_across_runs():
    """Same input → byte-identical output. Aggregator depends on this."""
    scores = [0.1, 0.3, 0.3, 0.5, 0.9]
    r1 = compute_histogram(scores, n_bins=10)
    r2 = compute_histogram(scores, n_bins=10)
    assert r1 == r2


# ----------------------------------------------------------------------------
# compute_summary
# ----------------------------------------------------------------------------

def test_summary_empty_pool_returns_zeros():
    s = compute_summary([])
    assert s.mean == 0.0
    assert s.median == 0.0
    assert s.p25 == 0.0
    assert s.p75 == 0.0
    assert s.max == 0.0


def test_summary_basic_stats():
    s = compute_summary([0.0, 0.25, 0.5, 0.75, 1.0])
    assert s.mean == 0.5
    assert s.median == 0.5
    assert s.p25 == 0.25
    assert s.p75 == 0.75
    assert s.max == 1.0


def test_summary_single_value():
    s = compute_summary([0.42])
    assert s.mean == 0.42
    assert s.median == 0.42
    assert s.p25 == 0.42
    assert s.p75 == 0.42
    assert s.max == 0.42


# ----------------------------------------------------------------------------
# merge_for_k_anonymity — the deterministic Q13 left-merge rule
# ----------------------------------------------------------------------------

def test_merge_no_op_when_all_bins_meet_floor():
    edges = [0.0, 0.25, 0.5, 0.75, 1.0]
    counts = [5, 7, 5, 10]
    new_edges, new_counts = merge_for_k_anonymity(edges, counts)
    assert new_edges == edges
    assert new_counts == counts


def test_merge_leaves_zero_bins_alone():
    """Zero is not in [1, floor), so empty bins are not merged."""
    edges = [0.0, 0.25, 0.5, 0.75, 1.0]
    counts = [0, 6, 0, 8]
    new_edges, new_counts = merge_for_k_anonymity(edges, counts)
    assert new_counts == counts
    assert new_edges == edges


def test_merge_middle_small_bin_goes_left():
    """A 1≤count<floor bin merges INTO the left neighbor."""
    edges = [0.0, 0.25, 0.5, 0.75, 1.0]
    counts = [10, 3, 10, 10]   # bin 1 is small
    new_edges, new_counts = merge_for_k_anonymity(edges, counts)
    # bin 1 merged into bin 0 → counts become [13, 10, 10]
    # edges 0.25 dropped → edges become [0.0, 0.5, 0.75, 1.0]
    assert new_counts == [13, 10, 10]
    assert new_edges == [0.0, 0.5, 0.75, 1.0]


def test_merge_first_small_bin_goes_right():
    """First bin has no left neighbor; merges INTO bin 1."""
    edges = [0.0, 0.25, 0.5, 0.75, 1.0]
    counts = [2, 10, 10, 10]   # bin 0 is small
    new_edges, new_counts = merge_for_k_anonymity(edges, counts)
    assert new_counts == [12, 10, 10]
    assert new_edges == [0.0, 0.5, 0.75, 1.0]


def test_merge_iterates_to_fixed_point():
    """Two small bins in a row: each merges in turn."""
    edges = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    counts = [10, 3, 4, 10, 10]   # bins 1 and 2 are both small
    new_edges, new_counts = merge_for_k_anonymity(edges, counts)
    # Pass 1: bin 1 (count=3) merges into bin 0 → counts [13, 4, 10, 10]
    # Pass 2: bin 1 (count=4) merges into bin 0 → counts [17, 10, 10]
    assert new_counts == [17, 10, 10]
    assert new_edges == [0.0, 0.6, 0.8, 1.0]


def test_merge_all_small_collapses_appropriately():
    """A degenerate input with everything small collapses sequentially."""
    edges = [0.0, 0.25, 0.5, 0.75, 1.0]
    counts = [2, 3, 1, 4]   # all small
    new_edges, new_counts = merge_for_k_anonymity(edges, counts)
    # All four bins are <5; they all merge into a single bin.
    assert new_counts == [10]
    assert new_edges == [0.0, 1.0]


def test_merge_preserves_total_count():
    """Sum of bin_counts is invariant under merging."""
    edges = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    counts = [2, 0, 3, 4, 10]
    _, new_counts = merge_for_k_anonymity(edges, counts)
    assert sum(new_counts) == sum(counts)


def test_merge_edges_remain_monotonic():
    """After merging, edges are still strictly ascending."""
    edges = [0.0, 0.1, 0.3, 0.5, 0.6, 1.0]
    counts = [3, 2, 8, 1, 6]
    new_edges, _ = merge_for_k_anonymity(edges, counts)
    for a, b in zip(new_edges, new_edges[1:]):
        assert a < b


def test_merge_is_idempotent():
    """Running the merge twice produces the same result."""
    edges = [0.0, 0.2, 0.4, 0.6, 0.8, 1.0]
    counts = [10, 3, 4, 0, 7]
    once_edges, once_counts = merge_for_k_anonymity(edges, counts)
    twice_edges, twice_counts = merge_for_k_anonymity(once_edges, once_counts)
    assert once_edges == twice_edges
    assert once_counts == twice_counts


def test_merge_single_bin_input_unchanged():
    """One bin can't be merged with anything; bail safely."""
    edges = [0.0, 1.0]
    counts = [3]
    new_edges, new_counts = merge_for_k_anonymity(edges, counts)
    assert new_edges == edges
    assert new_counts == counts


def test_merge_floor_must_be_at_least_one():
    with pytest.raises(ValueError):
        merge_for_k_anonymity([0.0, 1.0], [0], floor=0)


def test_merge_length_mismatch_raises():
    with pytest.raises(ValueError):
        merge_for_k_anonymity([0.0, 0.5, 1.0], [1])
    with pytest.raises(ValueError):
        merge_for_k_anonymity([0.0, 1.0], [1, 2])


def test_merge_custom_floor():
    """Floor parameterizable — useful for stricter requirements."""
    edges = [0.0, 0.5, 1.0]
    counts = [7, 10]
    new_edges, new_counts = merge_for_k_anonymity(edges, counts, floor=10)
    # bin 0 has count 7 which is < 10 → merge into bin 1 (right since first)
    assert new_counts == [17]
    assert new_edges == [0.0, 1.0]
