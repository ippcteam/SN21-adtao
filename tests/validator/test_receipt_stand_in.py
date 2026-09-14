"""A day with no receipt of its own still gets the copy controls.

WHY
    A day gets no receipt when every outcome that settled on it was scored by
    a later run: the rows travel in that run's receipt, and the day itself
    has nothing to publish. The one-payer and lineage controls read "today's"
    receipt for the behaviour they group on, so such a day suppressed nobody
    — and a vector published on it paid every copy the day before had
    excluded, for as long as the following days held.

    The verdict is about the models, which do not change because a receipt
    is dated a day later. So the most recent receipt before the day stands
    in, within a short bound, and the audit names which day it read.
"""

import json
import os
from datetime import date

from hope.validator.daily_stream_weights import (
    RECEIPT_STAND_IN_MAX_DAYS,
    lineage_from_receipts,
    one_payer_suppression_from_receipts,
    receipt_stand_in_day,
)

LINEAGE_ON = {
    "SN21_LINEAGE_CORR_MIN": "0.98",
    "SN21_LINEAGE_SIGN_MIN": "0.95",
    "SN21_LINEAGE_DISTANCE_MAX": "0.05",
    "SN21_LINEAGE_DISAGREE_MAX": "0.10",
    "SN21_LINEAGE_PARAMS_VERSION": "test-v1",
}


def _write_receipt(root, day, hotkeys, exact=True):
    """One receipt in which every named hotkey runs the same behaviour —
    byte-identical predictions (the exact detector's case) unless `exact`
    is False, in which case they differ in the last decimals (lineage)."""
    actuals = [round(-0.20 + 0.01 * i, 4) for i in range(40)]
    entries, outcomes = [], []
    for i, actual in enumerate(actuals):
        outcomes.append({"episode_id": f"e{i}", "horizon_days": 7,
                         "cost_delta_pct": actual})
    for n, hk in enumerate(hotkeys):
        for i, actual in enumerate(actuals):
            jitter = 0.0 if exact else 1e-9 * (n + 1)
            entries.append({
                "miner": hk, "episode_id": f"e{i}", "horizon_days": 7,
                "prediction": {"cost_delta_pct": {"p50": round(actual + jitter, 12)},
                               "conversions_delta_pct": {"p50": 0.1},
                               "efficiency_delta_pct": {"p50": 0.2}},
            })
    d = os.path.join(root, "receipts")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{day}.json"), "w") as fh:
        json.dump({"document": {"metrics": {"entries": entries,
                                            "outcomes": outcomes}}}, fh)


class TestTheStandIn:
    def test_the_most_recent_prior_receipt_within_the_bound(self):
        hist = [("2026-09-09", {}), ("2026-09-10", {}), ("2026-09-11", {})]
        assert receipt_stand_in_day(hist, date(2026, 9, 12)) == "2026-09-11"

    def test_nothing_older_than_the_bound(self):
        hist = [("2026-09-01", {})]
        far = date(2026, 9, 1 + RECEIPT_STAND_IN_MAX_DAYS + 1)
        assert receipt_stand_in_day(hist, far) is None
        near = date(2026, 9, 1 + RECEIPT_STAND_IN_MAX_DAYS)
        assert receipt_stand_in_day(hist, near) == "2026-09-01"

    def test_only_days_before_the_day_count(self):
        hist = [("2026-09-13", {}), ("2026-09-14", {})]
        assert receipt_stand_in_day(hist, date(2026, 9, 12)) is None


class TestOnePayerOnAReceiptlessDay:
    def test_yesterdays_receipt_stands_in_and_is_named(self, tmp_path):
        root = str(tmp_path)
        # Same-day ties resolve by hotkey order; the names make the seat holder explicit.
        _write_receipt(root, "2026-09-11", ["a-original", "b-copy"])
        stats: dict = {}
        out = one_payer_suppression_from_receipts(
            root, date(2026, 9, 12), {}, stats)
        assert out == frozenset({"b-copy"})
        assert stats["receipt_day"] == "2026-09-11"
        assert stats["fingerprints_today"] == 2

    def test_the_days_own_receipt_still_wins(self, tmp_path):
        root = str(tmp_path)
        _write_receipt(root, "2026-09-11", ["a-original", "b-copy"])
        _write_receipt(root, "2026-09-12", ["a-original"])
        stats: dict = {}
        out = one_payer_suppression_from_receipts(
            root, date(2026, 9, 12), {}, stats)
        assert out == frozenset()
        assert stats["receipt_day"] == "2026-09-12"

    def test_a_receipt_past_the_bound_does_not_stand_in(self, tmp_path):
        root = str(tmp_path)
        _write_receipt(root, "2026-09-01", ["a-original", "b-copy"])
        stats: dict = {}
        out = one_payer_suppression_from_receipts(
            root, date(2026, 9, 12), {}, stats)
        assert out == frozenset()
        assert stats["receipt_day"] == "2026-09-12"
        assert stats["fingerprints_today"] == 0


class TestLineageOnAReceiptlessDay:
    def test_yesterdays_receipt_stands_in_and_is_named(self, tmp_path):
        root = str(tmp_path)
        _write_receipt(root, "2026-09-11", ["author", "cloner"], exact=False)
        groups, audit = lineage_from_receipts(root, date(2026, 9, 12), LINEAGE_ON)
        assert groups and groups[0].copies == ["cloner"] or \
            list(groups[0].copies) == ["cloner"]
        assert audit["receipt_day"] == "2026-09-11"

    def test_nothing_within_the_bound_means_no_groups(self, tmp_path):
        root = str(tmp_path)
        _write_receipt(root, "2026-09-01", ["author", "cloner"], exact=False)
        groups, audit = lineage_from_receipts(root, date(2026, 9, 12), LINEAGE_ON)
        assert groups == [] and audit == {}
