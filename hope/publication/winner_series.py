"""Winning-model accuracy series — week over week, one line per horizon.

The chart this feeds answers one question: is the best model on the subnet
getting better, and does long-horizon prediction work? Requested 5 Aug 2026.

WHY IT READS THE RECEIPT AND NOTHING ELSE
    A chart that claims to prove our success has to be provable itself, or it
    is marketing. Every plotted point is the mean of scores that are already
    published, per entry, inside the daily receipt — so anyone can recompute
    a point from the same documents `verify_day.py` reads, and get our number.
    There is no site-only metric and no separate accuracy store to drift.

WHAT A POINT IS
    For one ISO week and one horizon: the mean final score of the WINNING
    model's entries that SETTLED in that week, plus the count behind it.
    Settled, not submitted — an entry belongs to the week its outcome
    finalised, which is the week its score became real.

WHAT THIS MODULE DELIBERATELY DOES NOT DO
    * It never interpolates. A week with no settled entries for a horizon
      produces NO POINT, so the line has an honest gap rather than a
      confident invention. The 28-day line therefore cannot begin before the
      first 28-day settlement (8 September 2026) and must never be backfilled.
    * It does not decide who the winner is. Standings are an INPUT, injected
      per week, because the standing ledger is I/O and this module is pure —
      the same shape as collateral_floor and weight_curve.
    * It does not censor. Censored horizons never reach a receipt's entries,
      so they are excluded by construction rather than by a filter here that
      could drift from the settle path.

NAMING AND THE PUBLISHED DOCUMENT
    `winner` (the hotkey) is returned in every point because a roll-up that
    hides its subject cannot be checked. The site renders the line unnamed
    under the aggregate-only rule, so the serving layer is what drops the
    field — one place, deliberately, rather than a module that quietly knows
    less than it should.

Pure module: no I/O, no clock, no environment.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta

# The horizons a miner is scored on. 28 has no history before 8 Sep 2026 and
# that is a fact about the data, not a setting.
HORIZONS = (7, 14, 28)

# Scores are meaned then rounded so two validators rolling up the same
# receipts produce byte-identical documents; the feed hashes canonical JSON.
SCORE_DP = 6


def iso_week(day: date) -> str:
    """`2026-W32`. The day boundary is already midnight EST when it reaches a
    receipt — the settle clock assigns it — so bucketing the date is enough
    and no timezone maths belongs here."""
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def week_bounds(week: str) -> tuple[date, date]:
    """Monday and Sunday of an ISO week label, inclusive."""
    year_s, week_s = week.split("-W")
    monday = date.fromisocalendar(int(year_s), int(week_s), 1)
    return monday, monday + timedelta(days=6)


@dataclass(frozen=True)
class SeriesPoint:
    week: str
    horizon_days: int
    winner: str | None       # dropped by the serving layer, kept here to check
    mean_score: float
    entries: int             # the n behind the mean — a point of 1 is not a trend

    def as_dict(self) -> dict:
        start, end = week_bounds(self.week)
        return {
            "week": self.week,
            "week_start": str(start),
            "week_end": str(end),
            "horizon_days": self.horizon_days,
            "winner": self.winner,
            "mean_score": self.mean_score,
            "entries": self.entries,
        }


def entries_by_week(receipts: Iterable[Mapping]) -> dict[str, list[dict]]:
    """Group every receipt entry by the ISO week its outcome finalised in.

    Entries carry their own `finalized_on`, so a receipt published late — or
    one covering a day whose entries settled across a boundary — still lands
    each entry in the right week. Reading the receipt's own day instead would
    put a Monday-settled entry in the wrong week whenever publication slipped.
    """
    out: dict[str, list[dict]] = {}
    for receipt in receipts:
        for entry in receipt.get("entries", ()):
            raw = entry.get("finalized_on")
            if not raw:
                continue                       # cannot be placed in a week
            day = date.fromisoformat(str(raw))
            out.setdefault(iso_week(day), []).append(entry)
    return out


def winner_for_week(
    standings: Mapping[str, float],
    evidence: Mapping[str, int] | None = None,
) -> str | None:
    """The top standing at the week's close, ties broken by evidence mass.

    A tie on a rounded standing is not rare early on, when everyone has few
    scored entries. Breaking it by evidence prefers the model that has been
    tested more, and falling through to the hotkey keeps the choice
    deterministic — an arbitrary but stable winner beats a chart that changes
    when the dict order does.
    """
    if not standings:
        return None
    ev = evidence or {}
    return max(
        standings,
        key=lambda hk: (standings[hk], ev.get(hk, 0), hk),
    )


def roll_up(
    receipts: Iterable[Mapping],
    standings_by_week: Mapping[str, Mapping[str, float]],
    evidence_by_week: Mapping[str, Mapping[str, int]] | None = None,
    horizons: Iterable[int] = HORIZONS,
) -> list[SeriesPoint]:
    """Receipts + per-week standings in, one point per (week, horizon) out.

    A week with no standing (nobody placed yet) yields no points at all
    rather than a zero: "nobody had won yet" and "the winner scored nothing"
    are different statements and the chart must not merge them.
    """
    grouped = entries_by_week(receipts)
    evidence_by_week = evidence_by_week or {}
    points: list[SeriesPoint] = []

    for week in sorted(grouped):
        winner = winner_for_week(
            standings_by_week.get(week, {}), evidence_by_week.get(week, {}),
        )
        if winner is None:
            continue
        theirs = [e for e in grouped[week] if e.get("miner") == winner]
        for horizon in horizons:
            scores = [
                e["score"] for e in theirs
                if int(e.get("horizon_days", 0)) == horizon
                and e.get("score") is not None
            ]
            if not scores:
                continue               # honest gap; never an interpolated point
            points.append(SeriesPoint(
                week=week,
                horizon_days=horizon,
                winner=winner,
                mean_score=round(sum(scores) / len(scores), SCORE_DP),
                entries=len(scores),
            ))

    points.sort(key=lambda p: (p.week, p.horizon_days))
    return points


def build_series(
    receipts: Iterable[Mapping],
    standings_by_week: Mapping[str, Mapping[str, float]],
    evidence_by_week: Mapping[str, Mapping[str, int]] | None = None,
    horizons: Iterable[int] = HORIZONS,
    era: str = "daily",
) -> dict:
    """The series document body, ready for the publication rail.

    `era` marks where the daily stream begins so the chart can draw the
    boundary with the weekly era rather than implying one continuous
    measurement regime across a mechanism change.
    """
    points = roll_up(receipts, standings_by_week, evidence_by_week, horizons)
    weeks = sorted({p.week for p in points})
    return {
        "feed": "winner_accuracy_series",
        "era": era,
        "horizons": sorted(set(horizons)),
        "points": [p.as_dict() for p in points],
        "weeks_covered": len(weeks),
        "first_week": weeks[0] if weeks else None,
        "last_week": weeks[-1] if weeks else None,
    }
