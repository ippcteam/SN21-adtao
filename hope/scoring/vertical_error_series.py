"""J3 — per-vertical error series for the [D4] basket-split trigger.

Condition 2 of the published split trigger (Design v0.5 §10): the leading
model's prediction error on ecommerce episodes versus lead-gen episodes,
tracked weekly. Condition 1 (ecommerce share of daily episodes) is
instrumented operator-side; this module is the validator-side consumer of
the operator's episode→vertical map, aggregating settled scores into the
weekly series the first four-weekly review reads.

Formula caveat (deliberate): the score composition is under governance review
(a goal-metric term may be added to the 0.7 pinball + 0.3 direction
settle formula). The series therefore stores RAW per-entry components
alongside the blended error, so the weekly aggregate can be recomputed
under whichever formula is ratified — nothing here is throwaway.

Pure module: vertical map, scored entries, and clock all injected.
The daily loop appends settled entries as they enter the standing
ledger; `weekly_series` folds the accumulated store.

Storage (ledger root): <root>/vertical_series/entries.jsonl — one line
per (episode, horizon, miner) settled entry with vertical + components;
append-only, idempotent per (episode_id, horizon, miner).
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

VERTICAL_ECOMMERCE = "ecommerce"
VERTICAL_LEAD_GEN = "lead_gen"
VERTICAL_UNTAGGED = "untagged"


def _series_dir(root: str) -> str:
    return os.path.join(root, "vertical_series")


def _entries_path(root: str) -> str:
    return os.path.join(_series_dir(root), "entries.jsonl")


@dataclass(frozen=True)
class SeriesEntry:
    episode_id: str
    horizon_days: int
    miner: str
    vertical: str            # ecommerce | lead_gen | untagged
    score: float             # blended entry score under the current formula
    pinball_component: float
    direction_component: float
    entry_weight: float      # horizon blend weight
    settled_on: str          # ISO date (settle-clock date)


def append_entries(root: str, entries: Iterable[SeriesEntry]) -> int:
    """Append settled entries; idempotent per (episode, horizon, miner)."""
    os.makedirs(_series_dir(root), exist_ok=True)
    seen: set[tuple] = set()
    path = _entries_path(root)
    if os.path.exists(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                seen.add((rec["episode_id"], rec["horizon_days"], rec["miner"]))
    written = 0
    with open(path, "a") as f:
        for e in entries:
            key = (e.episode_id, e.horizon_days, e.miner)
            if key in seen:
                continue
            seen.add(key)
            f.write(json.dumps(e.__dict__) + "\n")
            written += 1
    return written


def load_entries(root: str) -> list[dict]:
    path = _entries_path(root)
    if not os.path.exists(path):
        return []
    out = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def weekly_series(
    entries: Iterable[dict],
    miner: str | None = None,
) -> list[dict]:
    """Fold entries into (iso_week, vertical) → mean error + counts.

    `miner` restricts to the leading model (the series' published object);
    None aggregates all miners (diagnostic view). Error = 1 - score;
    entries are weighted by their horizon blend weight so an episode
    contributes equally across horizons (D13 discipline carries over).
    """
    buckets: dict[tuple, dict] = {}
    for rec in entries:
        if miner is not None and rec["miner"] != miner:
            continue
        d = date.fromisoformat(rec["settled_on"])
        iso = d.isocalendar()
        key = (f"{iso[0]}-W{iso[1]:02d}", rec["vertical"])
        b = buckets.setdefault(key, {
            "weighted_error": 0.0, "weight": 0.0, "episodes": set(),
        })
        w = rec.get("entry_weight", 1.0)
        b["weighted_error"] += (1.0 - rec["score"]) * w
        b["weight"] += w
        b["episodes"].add(rec["episode_id"])

    out = []
    for (week, vertical), b in sorted(buckets.items()):
        out.append({
            "week": week,
            "vertical": vertical,
            "mean_error": round(b["weighted_error"] / b["weight"], 6)
            if b["weight"] else None,
            "episode_count": len(b["episodes"]),
        })
    return out


def condition2_view(series: list[dict]) -> list[dict]:
    """Ecommerce-vs-lead-gen weekly comparison — what the review reads.

    Emits one row per week with both verticals' mean errors and the
    ecommerce excess (positive = the model is worse on ecommerce). The
    'materially worse for N consecutive weeks' judgment is review-set
    ([D4]: numbers at first review) — this view supplies the series,
    not the verdict.
    """
    by_week: dict[str, dict] = {}
    for row in series:
        by_week.setdefault(row["week"], {})[row["vertical"]] = row
    out = []
    for week in sorted(by_week):
        ec = by_week[week].get(VERTICAL_ECOMMERCE)
        lg = by_week[week].get(VERTICAL_LEAD_GEN)
        out.append({
            "week": week,
            "ecommerce_error": ec["mean_error"] if ec else None,
            "lead_gen_error": lg["mean_error"] if lg else None,
            "ecommerce_excess": round(ec["mean_error"] - lg["mean_error"], 6)
            if ec and lg and ec["mean_error"] is not None
            and lg["mean_error"] is not None else None,
            "ecommerce_episodes": ec["episode_count"] if ec else 0,
            "lead_gen_episodes": lg["episode_count"] if lg else 0,
        })
    return out
