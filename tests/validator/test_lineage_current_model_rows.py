"""Lineage compares the models that are running, not what a hotkey left behind.

WHY THIS EXISTS

    A hotkey that stops running keeps producing settled rows for weeks: its
    28-day outcomes land a month after the basket. Those rows are its ONLY
    overlap with everyone else, and on a basket where many models agree
    they correlate 1.0 with all of them. So the retired hotkey linked to
    every model it touched, and being similar to everyone made it the
    medoid — the member that decides who stays in the group.

    Live on 2026-09-21: one retired hotkey (rank 123, no rows under its
    current commitment) merged sixteen models that correlate about 0.71 on
    the rows they produce now. The day's highest standing was excluded and
    earned nothing, while the seat went to rank 9. Reported by a miner,
    reproduced from the published receipt.

    Comparing each hotkey on its current model is what the published rules
    already promise: the exclusion "lapses the moment the hotkey runs a
    model of its own" (SN21_REWARDS.md) and precedence follows "the
    earliest commitment that actually produced the behaviour in question"
    (SN21_THREAT_MODEL.md §1.7).
"""

import json
import os
from datetime import date

from hope.validator.daily_stream_weights import (
    lineage_from_receipts,
    predictions_from_receipt,
)

DAY = date(2026, 8, 20)
LINEAGE_ON = {
    "SN21_LINEAGE_CORR_MIN": "0.98",
    "SN21_LINEAGE_SIGN_MIN": "0.95",
    "SN21_LINEAGE_DISTANCE_MAX": "0.05",
    "SN21_LINEAGE_DISAGREE_MAX": "0.10",
    "SN21_LINEAGE_PARAMS_VERSION": "test-v1",
}
METRICS = ("cost_delta_pct", "conversions_delta_pct", "efficiency_delta_pct")


def _prediction(value):
    return {m: {"p10": value - 0.1, "p50": value, "p90": value + 0.1}
            for m in METRICS}


def _entry(miner, episode, horizon, value, model, predicted_on):
    return {"miner": miner, "episode_id": episode, "horizon_days": horizon,
            "prediction": _prediction(value), "model": model,
            "predicted_on": predicted_on}


def _ledger(tmp_path, entries, outcomes, model_since=None):
    root = str(tmp_path)
    d = os.path.join(root, "receipts")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{DAY}.json"), "w") as fh:
        json.dump({"document": {"metrics": {"entries": entries,
                                            "outcomes": outcomes}}}, fh)
    if model_since is not None:
        with open(os.path.join(root, "model_since.json"), "w") as fh:
            json.dump(model_since, fh)
    return root


def _world():
    """Two genuinely different models, plus a retired hotkey.

    STALE ran the old basket only, and on that basket every model answered
    the same — the sentinel-heavy case. On the fresh basket A and B diverge
    sharply, which is what the four signals should be reading.
    """
    entries, outcomes = [], []
    # old basket: everyone agrees (this is the trap)
    for i in range(40):
        ep = f"old{i:03d}"
        for miner, model in (("A", "sha256:aaaa000000000001"),
                             ("B", "sha256:bbbb000000000001"),
                             ("STALE", "sha256:5555000000000001")):
            entries.append(_entry(miner, ep, 28, 0.10 + i * 0.01, model,
                                  "2026-07-20"))
        outcomes.append({"episode_id": ep, "horizon_days": 28,
                         **{m: 0.1 for m in METRICS}})
    # fresh basket: A and B are plainly different models; STALE is absent
    for i in range(40):
        ep = f"new{i:03d}"
        entries.append(_entry("A", ep, 7, 0.10 + i * 0.01,
                              "sha256:aaaa000000000002", "2026-08-10"))
        entries.append(_entry("B", ep, 7, 0.90 - i * 0.02,
                              "sha256:bbbb000000000002", "2026-08-10"))
        outcomes.append({"episode_id": ep, "horizon_days": 7,
                         **{m: 0.1 for m in METRICS}})
    return entries, outcomes


CURRENT = {
    "A": {"digest": "sha256:aaaa000000000002", "since": "2026-08-10"},
    "B": {"digest": "sha256:bbbb000000000002", "since": "2026-08-10"},
    "STALE": {"digest": "sha256:5555000000000099", "since": "2026-08-05"},
}


def test_a_retired_hotkey_no_longer_merges_two_different_models(tmp_path):
    entries, outcomes = _world()
    root = _ledger(tmp_path, entries, outcomes, model_since=CURRENT)
    groups, audit = lineage_from_receipts(root, DAY, LINEAGE_ON)
    assert groups == [], (
        "A and B differ on the rows they produce now; only the retired "
        "hotkey's old basket made them look alike")
    assert audit.get("rows") == "each hotkey's current model only"


def test_without_the_fix_the_same_receipt_merges_them(tmp_path):
    """The bug, pinned: with no current-model map the old rows still rule."""
    entries, outcomes = _world()
    root = _ledger(tmp_path, entries, outcomes)          # no model_since file
    groups, audit = lineage_from_receipts(root, DAY, LINEAGE_ON)
    assert groups, "this is the behaviour the fix replaces"
    assert set(groups[0].members) == {"A", "B", "STALE"}
    assert audit.get("rows") == "every row on the receipt (no current-model map)"


def test_genuine_copies_are_still_caught_on_current_rows(tmp_path):
    entries, outcomes = _world()
    for e in list(entries):
        if e["miner"] == "A":
            copy = dict(e)
            copy["miner"] = "COPY"
            copy["model"] = e["model"].replace("aaaa", "cccc")
            entries.append(copy)
    since = dict(CURRENT)
    since["COPY"] = {"digest": "sha256:cccc000000000002", "since": "2026-08-10"}
    root = _ledger(tmp_path, entries, outcomes, model_since=since)
    groups, _audit = lineage_from_receipts(root, DAY, LINEAGE_ON)
    assert len(groups) == 1
    assert set(groups[0].members) == {"A", "COPY"}


def test_the_audit_publishes_the_digest_each_hotkey_was_compared_under(tmp_path):
    entries, outcomes = _world()
    for e in list(entries):
        if e["miner"] == "A":
            copy = dict(e); copy["miner"] = "COPY"
            copy["model"] = e["model"].replace("aaaa", "cccc")
            entries.append(copy)
    since = dict(CURRENT)
    since["COPY"] = {"digest": "sha256:cccc000000000002", "since": "2026-08-10"}
    root = _ledger(tmp_path, entries, outcomes, model_since=since)
    _groups, audit = lineage_from_receipts(root, DAY, LINEAGE_ON)
    # a miner holding the receipt and this map can redo the filter exactly
    assert audit["current_model"]["A"] == "aaaa000000000002"
    assert audit["current_model"]["COPY"] == "cccc000000000002"
    assert audit["comparable_hotkeys"] == len(audit["current_model"])


class TestRowSelection:
    def test_only_rows_from_the_current_digest_are_returned(self):
        entries = [
            _entry("A", "e1", 7, 0.1, "sha256:aaaa000000000001", "2026-07-20"),
            _entry("A", "e2", 7, 0.2, "sha256:aaaa000000000002", "2026-08-10"),
        ]
        out = predictions_from_receipt(
            entries, {"A": "sha256:aaaa000000000002"})
        assert list(out["A"]) == ["e2"]

    def test_a_hotkey_with_no_known_digest_contributes_nothing(self):
        entries = [_entry("A", "e1", 7, 0.1, "sha256:aaaa000000000001",
                          "2026-07-20")]
        assert predictions_from_receipt(entries, {}) == {}
        assert predictions_from_receipt(entries, {"A": ""}) == {}

    def test_a_hotkey_with_no_rows_under_its_current_digest_contributes_nothing(self):
        entries = [_entry("A", "e1", 7, 0.1, "sha256:aaaa000000000001",
                          "2026-07-20")]
        assert predictions_from_receipt(
            entries, {"A": "sha256:aaaa000000000002"}) == {}

    def test_a_full_digest_matches_the_receipt_s_short_one(self):
        entries = [_entry("A", "e1", 7, 0.1, "sha256:aaaa000000000002",
                          "2026-08-10")]
        full = "sha256:aaaa000000000002" + "f" * 48
        assert predictions_from_receipt(entries, {"A": full})["A"]

    def test_no_map_means_every_row_as_before(self):
        entries = [
            _entry("A", "e1", 7, 0.1, "sha256:aaaa000000000001", "2026-07-20"),
            _entry("A", "e2", 7, 0.2, "sha256:aaaa000000000002", "2026-08-10"),
        ]
        assert sorted(predictions_from_receipt(entries, None)["A"]) == ["e1", "e2"]
