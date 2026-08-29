"""A day entered by a run that died must still become publishable.

WHY THIS EXISTS
    Entry into the standings is marker-gated so a re-run cannot count a
    result twice. Publication is not gated at all — it simply uses whatever
    the settle step scored in this process.

    Those two facts combine badly. A run that scores, enters, marks, and then
    dies before publication leaves a day that no later run can publish: the
    next run finds every row already entered, scores nothing, and the receipt
    is skipped for "no scored results". The standings hold the day, the
    weight vector is computed from it, and nobody outside can recompute any
    of it — including the copy controls, which read the receipts and go blind.

    The repair re-derives the day's results from the rows the crashed run
    entered. These tests pin the two properties that make that safe: it is
    scoped to that run's rows, and it enters nothing.
"""

import json
import os
from datetime import date

import pytest

from hope.scoring.settle_day_flow import (
    entered_on_run,
    entered_results,
    score_day_for_receipt,
)

DAY = date(2026, 8, 29)
EARLIER = date(2026, 8, 28)


def _markers(root, rows):
    """rows: [(episode, horizon, run_day)]"""
    d = os.path.join(root, "standing")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "_entered_results.jsonl"), "w") as fh:
        for ep, h, run_day in rows:
            fh.write(json.dumps({"episode_id": ep, "horizon_days": h,
                                 "entered_on_run": str(run_day)}) + "\n")


class TestEnteredOnRun:
    def test_it_selects_only_the_named_run(self, tmp_path):
        root = str(tmp_path)
        _markers(root, [("e1", 7, DAY), ("e2", 7, DAY), ("e3", 7, EARLIER)])
        assert entered_on_run(root, DAY) == {("e1", 7), ("e2", 7)}
        assert entered_on_run(root, EARLIER) == {("e3", 7)}

    def test_a_day_that_entered_nothing_is_empty_not_everything(self, tmp_path):
        """The dangerous failure is returning every row: the repair would
        then publish a receipt covering all of history."""
        root = str(tmp_path)
        _markers(root, [("e1", 7, EARLIER)])
        assert entered_on_run(root, DAY) == set()

    def test_no_marker_file_is_empty(self, tmp_path):
        assert entered_on_run(str(tmp_path), DAY) == set()

    def test_it_does_not_disturb_the_full_marker_read(self, tmp_path):
        """entered_results still answers the ENTRY question over all runs."""
        root = str(tmp_path)
        _markers(root, [("e1", 7, DAY), ("e3", 7, EARLIER)])
        assert entered_results(root) == {("e1", 7), ("e3", 7)}


class _Outcome:
    def __init__(self, episode_id, horizon_days):
        self.episode_id = episode_id
        self.horizon_days = horizon_days
        self.finalized_on = DAY


class TestScopeAndSafety:
    def test_it_scores_only_the_rows_that_run_entered(self, tmp_path, monkeypatch):
        root = str(tmp_path)
        _markers(root, [("e1", 7, DAY), ("e3", 7, EARLIER)])

        seen = {}

        def fake_score(index, outcomes, environ=None):
            seen["ids"] = sorted((str(o.episode_id), int(o.horizon_days))
                                 for o in outcomes)
            return [], {}

        monkeypatch.setattr("hope.scoring.settle_day_flow.load_prediction_index",
                            lambda _root: {})
        monkeypatch.setattr(
            "hope.scoring.settle_day_flow.score_settled_with_components",
            fake_score)

        provider = lambda _d: [_Outcome("e1", 7), _Outcome("e2", 7),  # noqa: E731
                               _Outcome("e3", 7)]
        score_day_for_receipt(root, root, DAY, provider)

        assert seen["ids"] == [("e1", 7)], (
            "must score this run's rows only — not e2 (never entered) and "
            "not e3 (an earlier run's)")

    def test_it_never_enters_anything(self, tmp_path, monkeypatch):
        """The property that makes re-deriving safe. If this ever calls
        append_entries, a repaired day double-counts every score in it."""
        root = str(tmp_path)
        _markers(root, [("e1", 7, DAY)])

        def explode(*a, **k):
            raise AssertionError("the repair must not touch the standings")

        monkeypatch.setattr(
            "hope.scoring.standing_ledger.append_entries", explode)
        monkeypatch.setattr("hope.scoring.settle_day_flow.load_prediction_index",
                            lambda _root: {})
        monkeypatch.setattr(
            "hope.scoring.settle_day_flow.score_settled_with_components",
            lambda i, o, environ=None: ([], {}))

        score_day_for_receipt(root, root, DAY, lambda _d: [_Outcome("e1", 7)])

    def test_the_markers_are_left_alone(self, tmp_path, monkeypatch):
        """Re-deriving must not re-stamp rows: a repair that appends markers
        would make the day look freshly entered on every later run."""
        root = str(tmp_path)
        _markers(root, [("e1", 7, DAY)])
        before = open(os.path.join(root, "standing",
                                   "_entered_results.jsonl")).read()

        monkeypatch.setattr("hope.scoring.settle_day_flow.load_prediction_index",
                            lambda _root: {})
        monkeypatch.setattr(
            "hope.scoring.settle_day_flow.score_settled_with_components",
            lambda i, o, environ=None: ([], {}))
        score_day_for_receipt(root, root, DAY, lambda _d: [_Outcome("e1", 7)])

        after = open(os.path.join(root, "standing",
                                  "_entered_results.jsonl")).read()
        assert before == after

    def test_it_returns_the_shape_the_receipt_expects(self, tmp_path, monkeypatch):
        """The publication step must not be able to tell which path fed it."""
        root = str(tmp_path)
        _markers(root, [("e1", 7, DAY)])
        monkeypatch.setattr("hope.scoring.settle_day_flow.load_prediction_index",
                            lambda _root: {"idx": 1})
        monkeypatch.setattr(
            "hope.scoring.settle_day_flow.score_settled_with_components",
            lambda i, o, environ=None: ([], {}))
        out = score_day_for_receipt(root, root, DAY,
                                    lambda _d: [_Outcome("e1", 7)])
        for key in ("horizon_results", "components", "settled_outcomes",
                    "prediction_index", "censored_counts"):
            assert key in out, f"receipt needs {key}"
