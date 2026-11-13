"""The day-by-day series of Prediction Performance per change group (13 Nov 2026).

Each day's Prediction Performance document (prediction_performance/<day>.json
on the ledger) is cumulative and about 100 KB; the website showed only the
newest, so nobody could see whether a group was getting better or worse. This
reduces the last SERIES_DAYS documents to the few numbers a chart needs - each
group's field mean, episodes and whether it is solid, per horizon, as lists
lined up with `days` (None where the group had no result that day) -
and is published at /v1/daily/prediction-performance-series. Derived entirely
from documents already published, so it can be recomputed by anyone.
"""
from __future__ import annotations

import json
import os

SERIES_DAYS = 30


def build_series(perf_dir: str, days: int = SERIES_DAYS) -> dict | None:
    """The series over the newest `days` documents in `perf_dir`, or None."""
    if not os.path.isdir(perf_dir):
        return None
    names = sorted(n[:-5] for n in os.listdir(perf_dir) if n.endswith(".json"))[-days:]
    loaded: list[tuple[str, dict]] = []
    for day in names:
        try:
            with open(os.path.join(perf_dir, f"{day}.json")) as f:
                loaded.append((day, json.load(f)))
        except (OSError, ValueError):
            continue
    out_days = [d for d, _ in loaded]
    horizons: list[str] = []
    for _, doc in loaded:
        for h in doc.get("horizons") or []:
            if str(h) not in horizons:
                horizons.append(str(h))
    groups: dict = {}
    for i, (_, doc) in enumerate(loaded):
        for g in doc.get("groups") or []:
            key = g.get("key")
            if not key:
                continue
            entry = groups.setdefault(key, {"key": key, "label": g.get("label"), "series": {
                h: {"field_mean": [None] * len(loaded), "episodes": [None] * len(loaded),
                    "solid": [False] * len(loaded)} for h in horizons}})
            entry["label"] = g.get("label") or entry["label"]
            for h, cell in (g.get("cells") or {}).items():
                if isinstance(cell, dict) and str(h) in entry["series"]:
                    ser = entry["series"][str(h)]
                    ser["field_mean"][i] = cell.get("field_mean")
                    ser["episodes"][i] = cell.get("episodes")
                    ser["solid"][i] = bool(cell.get("solid"))
    if not out_days:
        return None
    # Which group each change family (the part of a transition key before ':')
    # belongs to in the newest document, so a page that lists a miner's
    # results by family can link each to its group. A family in more than one
    # group takes the first that isn't "combined".
    family_group: dict = {}
    for g in (loaded[-1][1].get("groups") or []):
        for r in g.get("rows") or []:
            fam = str(r.get("key") or "").split(":", 1)[0]
            if fam and (fam not in family_group or family_group[fam] == "combined"):
                family_group[fam] = g.get("key")
    return {"as_of": out_days[-1], "days": out_days, "horizons": horizons,
            "groups": list(groups.values()), "family_group": family_group,
            "source": "/v1/daily/prediction-performance, one document per day"}
