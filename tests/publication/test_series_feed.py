"""The ledger-reading half of the accuracy series, and its endpoint.

The roll-up itself is tested in test_winner_series. What matters here is
that the right inputs reach it: standings recomputed as of each week's own
close, receipts read from the published envelopes, and an empty ledger
producing an empty series with a stated reason rather than an error.
"""

import json
import os
from datetime import date, timedelta

from hope.publication.series_feed import (
    build_series_document,
    load_receipt_metrics,
    published_receipt_days,
    standings_at,
)
from hope.scoring.daily_score_flow import WeightedEntry
from hope.scoring.standing_ledger import append_entries

W1 = "2026-W34"          # Mon 17 Aug – Sun 23 Aug 2026
SUN_W1 = date(2026, 8, 23)


def _entry(miner, horizon, score, day):
    return {"episode_id": f"e-{horizon}", "horizon_days": horizon,
            "miner": miner, "prediction": {}, "components": None,
            "score": score, "finalized_on": str(day)}


def _write_receipt(root, day, *entries):
    d = os.path.join(root, "receipts")
    os.makedirs(d, exist_ok=True)
    envelope = {
        "document": {"feed": "daily_receipt", "as_of": str(day),
                     "metrics": {"entries": list(entries)}},
        "sha256": "0" * 64,
    }
    with open(os.path.join(d, f"{day}.json"), "w") as f:
        json.dump(envelope, f)


def _stand(root, miner, scored_on, score, weight=1.0, n=1):
    append_entries(root, [
        WeightedEntry(miner=miner, episode_id=f"ep{i}", horizon_days=7,
                      score=score, weight=weight, entered_on=scored_on)
        for i in range(n)
    ])


# ---- reading what is on disk ------------------------------------------------

def test_published_days_and_metrics_come_from_the_envelopes(tmp_path):
    root = str(tmp_path)
    _write_receipt(root, "2026-08-19", _entry("hkA", 7, 0.8, "2026-08-19"))
    _write_receipt(root, "2026-08-18", _entry("hkA", 7, 0.6, "2026-08-18"))

    assert published_receipt_days(root) == ["2026-08-18", "2026-08-19"]
    metrics = load_receipt_metrics(root)
    assert [m["entries"][0]["score"] for m in metrics] == [0.6, 0.8]


def test_an_unreadable_receipt_is_skipped_not_fatal(tmp_path):
    """One corrupt file must not take out a chart covering every other week."""
    root = str(tmp_path)
    _write_receipt(root, "2026-08-18", _entry("hkA", 7, 0.8, "2026-08-18"))
    with open(os.path.join(root, "receipts", "2026-08-19.json"), "w") as f:
        f.write("{not json")

    assert len(load_receipt_metrics(root)) == 1
    assert published_receipt_days(root) == ["2026-08-18", "2026-08-19"]


def test_no_ledger_at_all_reads_as_empty(tmp_path):
    root = str(tmp_path / "nothing-here")
    assert published_receipt_days(root) == []
    assert load_receipt_metrics(root) == []


# ---- standings as of a date -------------------------------------------------

def test_standing_is_read_as_of_the_given_day(tmp_path):
    root = str(tmp_path)
    _stand(root, "hkA", SUN_W1, 0.9)
    _stand(root, "hkB", SUN_W1, 0.2)

    standing, evidence = standings_at(root, SUN_W1)
    assert standing["hkA"] > standing["hkB"]
    assert evidence["hkA"] == 1.0


def test_a_later_result_cannot_change_an_earlier_week(tmp_path):
    """The winner of a week is whoever led at ITS close. If standings were
    read as of today, last month's point would move whenever the lead
    changed — history would redraw itself."""
    root = str(tmp_path)
    _stand(root, "hkA", SUN_W1, 0.9)                     # leads at W1 close
    _stand(root, "hkB", SUN_W1 + timedelta(days=3), 1.0)  # storms ahead later

    at_close, _ = standings_at(root, SUN_W1)
    assert "hkB" not in at_close          # no standing yet, and not a zero
    later, _ = standings_at(root, SUN_W1 + timedelta(days=3))
    assert later["hkB"] > later["hkA"]


def test_a_miner_with_no_in_window_entries_is_not_ranked_as_zero(tmp_path):
    """episode_weighted_average returns None when nothing is in window. Left
    in the standings dict, that None gets compared against a float while
    picking the winner and raises — so the endpoint would fall over the first
    time a miner went quiet for a whole window."""
    root = str(tmp_path)
    _stand(root, "hkA", SUN_W1, 0.9)
    _stand(root, "hkQuiet", SUN_W1 - timedelta(days=400), 0.5)

    standing, evidence = standings_at(root, SUN_W1)
    assert "hkQuiet" not in standing and "hkQuiet" not in evidence
    assert None not in standing.values()

    _write_receipt(root, "2026-08-19", _entry("hkA", 7, 0.8, "2026-08-19"))
    doc = build_series_document(root)      # must not raise
    assert doc["points"][0]["winner"] == "hkA"


# ---- the document -----------------------------------------------------------

def test_document_rolls_the_winner_up_per_horizon(tmp_path):
    root = str(tmp_path)
    _write_receipt(root, "2026-08-19",
                   _entry("hkA", 7, 0.80, "2026-08-19"),
                   _entry("hkA", 14, 0.60, "2026-08-19"),
                   _entry("hkB", 7, 0.10, "2026-08-19"))
    _stand(root, "hkA", SUN_W1, 0.9)
    _stand(root, "hkB", SUN_W1, 0.1)

    doc = build_series_document(root)
    by_h = {p["horizon_days"]: p for p in doc["points"]}
    assert by_h[7]["mean_score"] == 0.8        # the winner's, not the field's
    assert by_h[14]["mean_score"] == 0.6
    assert by_h[7]["winner"] == "hkA"
    assert doc["published_days"] == 1
    assert doc["latest_published_day"] == "2026-08-19"


def test_an_open_week_is_marked_incomplete(tmp_path):
    """A week whose Sunday has not been reached can still change hands.
    Hiding it would make the chart look a week stale; including it silently
    would present a provisional leader as settled."""
    root = str(tmp_path)
    _write_receipt(root, "2026-08-19", _entry("hkA", 7, 0.8, "2026-08-19"))
    _stand(root, "hkA", SUN_W1, 0.9)

    (point,) = build_series_document(root)["points"]
    assert point["week_end"] == "2026-08-23"
    assert point["complete"] is False          # feed only reaches the 19th

    _write_receipt(root, "2026-08-23", _entry("hkA", 7, 0.7, "2026-08-23"))
    points = build_series_document(root)["points"]
    assert all(p["complete"] for p in points)


def test_an_empty_ledger_says_why_it_is_empty(tmp_path):
    """'No data' and 'nothing has settled yet' look identical on a chart."""
    doc = build_series_document(str(tmp_path))
    assert doc["points"] == []
    assert doc["empty_reason"] == "no_receipts_published"
    assert doc["weeks_covered"] == 0


def test_receipts_without_any_standing_yield_no_points_and_say_so(tmp_path):
    root = str(tmp_path)
    _write_receipt(root, "2026-08-19", _entry("hkA", 7, 0.8, "2026-08-19"))
    doc = build_series_document(root)          # no standing ledger written
    assert doc["points"] == []
    assert doc["empty_reason"] == "no_week_has_a_standing"
