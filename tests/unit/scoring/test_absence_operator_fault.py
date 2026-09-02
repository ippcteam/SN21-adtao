"""Operator faults never become absence charges, and wrong charges cancel.

The two promises added 2 Sept after four models were charged for failures
on the operator side (one image pull failure, three runs cut off by a wall
ceiling below the published budget):

  1. PROSPECTIVE — compute_penalties skips a hotkey whose day-run failed
     for an operator-side reason (pull, disk, sandbox, under-budget
     timeout). The model's day adds no evidence; it is not charged.
  2. RETROSPECTIVE — a charge already written is corrected by an appended
     cancellation record, never a deletion; load_entries excludes exactly
     the cancelled penalty entries and nothing else.
"""

from __future__ import annotations

from datetime import date

import pytest

from hope.scoring import standing_ledger
from hope.scoring.absence_penalty import (
    PENALTY_ENTRY_WEIGHT,
    PUBLISHED_RUN_BUDGET_S,
    operator_fault,
)
from hope.scoring.daily_score_flow import WeightedEntry


class TestOperatorFault:
    @pytest.mark.parametrize("error", [
        "pull_failed: GET https://registry-1.docker.io/v2/x/blobs/sha256:75",
        "pull timeout>600s",
        "disk_low: 900 MB free < 2048 MB required",
        "sandbox_not_available",
        "sandbox_not_available: clone failed",
    ])
    def test_infrastructure_failures_are_ours(self, error):
        assert operator_fault(error, PUBLISHED_RUN_BUDGET_S) is True

    def test_a_timeout_below_the_published_budget_is_ours(self):
        # the 2026-09-01 case: ceiling 120s against a 900s published budget
        assert operator_fault("timeout>120s", PUBLISHED_RUN_BUDGET_S) is True

    def test_a_timeout_at_or_past_the_published_budget_is_theirs(self):
        assert operator_fault("timeout>900s", PUBLISHED_RUN_BUDGET_S) is False
        assert operator_fault("timeout>960s", PUBLISHED_RUN_BUDGET_S) is False

    def test_a_timeout_with_no_known_budget_charges_the_miner(self):
        """Fail-safe direction: an unknown budget must not quietly excuse."""
        assert operator_fault("timeout>120s", None) is False

    @pytest.mark.parametrize("error", [
        "exit=1: traceback...",
        "no runnable entrypoint in image",
        None,
        "",
    ])
    def test_model_failures_and_clean_runs_are_not_ours(self, error):
        assert operator_fault(error, PUBLISHED_RUN_BUDGET_S) is False


class TestCancellation:
    def _seed(self, root, hotkey, day, real_scores, penalty_count):
        entries = [WeightedEntry(miner=hotkey, score=s, weight=0.35,
                                 entered_on=day) for s in real_scores]
        entries += [WeightedEntry(miner=hotkey, score=0.0,
                                  weight=PENALTY_ENTRY_WEIGHT,
                                  entered_on=day)
                    for _ in range(penalty_count)]
        standing_ledger.append_entries(str(root), entries)

    def test_cancelled_penalty_entries_leave_the_standing(self, tmp_path):
        hk, day = "5Test", date(2026, 9, 1)
        self._seed(tmp_path, hk, day, [0.6, 0.6], penalty_count=3)
        before = standing_ledger.load_entries(str(tmp_path), as_of=day)[hk]
        assert len(before) == 5

        standing_ledger.record_cancellation(
            str(tmp_path), str(day), hk, missed=3, score=0.0,
            weight=PENALTY_ENTRY_WEIGHT, reason="operator pull failure")
        after = standing_ledger.load_entries(str(tmp_path), as_of=day)[hk]
        assert len(after) == 2
        assert all(e.score == 0.6 for e in after)

    def test_cancellation_never_touches_real_entries(self, tmp_path):
        """A real zero-score entry at scoring weight survives: the exclusion
        matches the penalty signature (score AND weight AND day), and no
        scoring path writes weight 1.0."""
        hk, day = "5Test", date(2026, 9, 1)
        entries = [WeightedEntry(miner=hk, score=0.0, weight=0.35,
                                 entered_on=day),
                   WeightedEntry(miner=hk, score=0.0,
                                 weight=PENALTY_ENTRY_WEIGHT,
                                 entered_on=day)]
        standing_ledger.append_entries(str(tmp_path), entries)
        standing_ledger.record_cancellation(
            str(tmp_path), str(day), hk, missed=5, score=0.0,
            weight=PENALTY_ENTRY_WEIGHT, reason="test")
        left = standing_ledger.load_entries(str(tmp_path), as_of=day)[hk]
        assert len(left) == 1 and left[0].weight == 0.35

    def test_cancellation_is_scoped_to_its_day_and_hotkey(self, tmp_path):
        day1, day2 = date(2026, 9, 1), date(2026, 8, 30)
        self._seed(tmp_path, "5A", day1, [], penalty_count=2)
        self._seed(tmp_path, "5A", day2, [], penalty_count=2)
        self._seed(tmp_path, "5B", day1, [], penalty_count=2)
        standing_ledger.record_cancellation(
            str(tmp_path), str(day1), "5A", missed=2, score=0.0,
            weight=PENALTY_ENTRY_WEIGHT, reason="test")
        out = standing_ledger.load_entries(str(tmp_path), as_of=day1)
        assert len(out["5A"]) == 2          # day2's charges stay
        assert all(str(e.scored_on) == str(day2) for e in out["5A"])
        assert len(out["5B"]) == 2          # other hotkey untouched

    def test_the_record_is_append_only_and_readable(self, tmp_path):
        standing_ledger.record_cancellation(
            str(tmp_path), "2026-09-01", "5A", 909, 0.0,
            PENALTY_ENTRY_WEIGHT, "image pull failed on the operator side")
        (rec,) = standing_ledger.load_cancellations(str(tmp_path))
        assert rec["missed"] == 909
        assert "pull failed" in rec["reason"]
