"""Reading the ledger to produce the winning-model accuracy series.

`winner_series` is pure — receipts and standings in, points out. This is the
I/O half: it reads the published receipts and the standing ledger off disk
and hands them over. Same split as everywhere else in the scoring code, and
it is what lets the roll-up be tested without a filesystem.

WHY STANDINGS ARE RECOMPUTED PER WEEK
    The winner of a week is whoever led at that week's CLOSE, not whoever
    leads today. Reading a single current standing would silently redraw
    history every time the leader changed — last month's point would move
    because of something that happened this morning. So each week's standing
    is recomputed as of its own Sunday, from the same ledger and the same
    12-day half-life the live standing uses.

PROVISIONAL WEEKS
    A week whose Sunday has not been reached by the published feed has not
    closed, so its winner can still change. Those points are marked
    `complete: false` rather than withheld: hiding the current week makes a
    live chart look a week stale, and silently including it would present a
    provisional leader as settled.

Everything here degrades to an empty series rather than an error. Before the
first settlement there are no receipts at all, and a chart that 500s in that
window is worse than one that says it has nothing yet.
"""

from __future__ import annotations

import json
import os
from datetime import date

from hope.publication.winner_series import build_series, iso_week, week_bounds
from hope.scoring.episode_average import (
    episode_weighted_average,
    scored_prediction_count,
)
from hope.scoring.standing_ledger import load_entries

RECEIPTS_DIR = "receipts"


def published_receipt_days(ledger_root: str) -> list[str]:
    """Every day the receipt feed has published, ascending."""
    directory = os.path.join(ledger_root, RECEIPTS_DIR)
    if not os.path.isdir(directory):
        return []
    return sorted(
        name[:-5] for name in os.listdir(directory)
        if name.endswith(".json") and not name.startswith("_")
    )


def load_receipt_metrics(ledger_root: str) -> list[dict]:
    """The `metrics` block of every published receipt.

    A receipt that cannot be read is skipped rather than fatal: one corrupt
    file must not take out a chart covering every other week.
    """
    directory = os.path.join(ledger_root, RECEIPTS_DIR)
    out = []
    for day in published_receipt_days(ledger_root):
        try:
            with open(os.path.join(directory, f"{day}.json")) as f:
                envelope = json.load(f)
        except (OSError, ValueError):
            continue
        metrics = (envelope.get("document") or {}).get("metrics")
        if isinstance(metrics, dict):
            out.append(metrics)
    return out


def standings_at(ledger_root: str, as_of: date) -> tuple[dict, dict]:
    """(standing, evidence) per miner as the ledger stood on `as_of`.

    Standing is the same episode-age-weighted average the live path uses, so
    a plotted winner is the miner who was actually top that week — not one
    picked by a metric that exists only for this chart.
    """
    entries = load_entries(ledger_root, as_of)
    standing, evidence = {}, {}
    for hotkey, episodes in entries.items():
        average = episode_weighted_average(episodes, as_of)
        if average is None:
            # A miner whose entries all fall outside the window has NO
            # standing — which is not a standing of zero, and must not be
            # ranked as if it were. Ranking a None against a float also
            # raises, so leaving these in would take the endpoint down the
            # first time a miner went quiet for a window.
            continue
        standing[hotkey] = average
        evidence[hotkey] = scored_prediction_count(episodes, as_of)
    return standing, evidence


def build_series_document(ledger_root: str) -> dict:
    """The full series document, ready to serve."""
    receipts = load_receipt_metrics(ledger_root)
    days = published_receipt_days(ledger_root)
    latest = days[-1] if days else None

    weeks = sorted({iso_week(date.fromisoformat(d)) for d in days})
    standings_by_week, evidence_by_week = {}, {}
    for week in weeks:
        _, sunday = week_bounds(week)
        standings_by_week[week], evidence_by_week[week] = standings_at(
            ledger_root, sunday)

    doc = build_series(receipts, standings_by_week, evidence_by_week)

    # Mark the weeks that have not closed yet. The chart needs to be able to
    # draw "so far" differently from "settled".
    for point in doc["points"]:
        point["complete"] = bool(latest) and point["week_end"] <= latest

    doc["published_days"] = len(days)
    doc["latest_published_day"] = latest
    if not doc["points"]:
        # Say WHY it is empty. "No data" and "nothing has settled yet" look
        # identical on a chart and mean very different things.
        doc["empty_reason"] = (
            "no_receipts_published" if not days else "no_week_has_a_standing")
    return doc
