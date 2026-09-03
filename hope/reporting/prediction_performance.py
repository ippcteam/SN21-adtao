"""Prediction Performance — the cumulative by-change-type page document.

Rob (2 Sept): one public page, next to the leaderboard, answering "on each
kind of change, how good is the best model, how good is the current winner,
and what are the scores made of" — switchable across the 7/14/28-day
horizons. The daily accuracy table answers this for ONE day; this module
folds every settled entry since the rich-baseline cutoff into one document,
refreshed daily.

WHY IT READS THE RECEIPTS AND NOTHING ELSE
    Same discipline as the winner series: every number on the page must be
    recomputable from documents that are already published per entry. The
    receipts carry each (episode, horizon, miner) score with its four
    components; the per-basket transition-key maps say what kind of change
    each episode was and which basket day it entered. There is no page-only
    metric to drift from the leaderboard.

THE CUTOFF IS BY BASKET DAY, NOT SETTLE DAY
    "Start from the richer baseline" means episodes that ENTERED from the
    2026-08-20 basket onwards — the day the basket mix flipped from almost
    entirely budget/pause to majority rich types (measured, not guessed).
    An old-era episode settling late must not sneak in through its settle
    date, so membership is decided by the basket that carried the episode.

ROLLUPS
    The top level is a small set of groups a reader can hold in their head;
    each group's rows are the actual transition keys inside it. Grouping is
    presentation: every entry lands in exactly one group, nothing is
    dropped, and anything unmatched lands in "other" where it stays
    visible. UNKNOWN keys stay visible too, as their own group.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping

CUTOFF_DAY = "2026-08-20"
HORIZONS = ("7", "14", "28")
COMPONENT_KEYS = ("quantile", "direction", "coverage", "goal")

# A row needs this many settled scores before its position reads as solid;
# below it the page shows the row with an explicit early-data marking.
MIN_N_SOLID = 500
# A miner needs this many scored entries on a cell before they can be named
# "best" there — one lucky episode is not a champion. Matches the daily
# accuracy table so the two surfaces cannot disagree about what "best" means.
MIN_N_FOR_BEST = 5

_TARGETING_FAMILIES = frozenset({
    "KEYWORD_ADD", "KEYWORD_REMOVE", "KEYWORD_PAUSE", "KEYWORD_ENABLE",
    "KEYWORD_BID_CHANGE", "KEYWORD_CHANGE",
    "NEGATIVE_KEYWORD_REMOVE", "NEGATIVE_KEYWORD_CHANGE",
    "GEO_CHANGE", "AUDIENCE_CHANGE", "DEVICE_TARGETING_CHANGE",
    "PLACEMENT_CHANGE", "SCHEDULE_CHANGE", "DEMOGRAPHIC_CHANGE",
    "CRITERION_CHANGE", "CRITERION_REMOVE", "CRITERION_PAUSE",
    "CRITERION_ENABLE", "CRITERION_URL_CHANGE", "CRITERION_BID_CHANGE",
})

# Ordered: the page renders groups in this order, densest concepts first.
GROUP_DEFS = (
    ("budget", "Budget change",
     lambda fam: fam in ("BUDGET", "BUDGET_CHANGE")),
    ("bid_switch", "Bid strategy switch",
     lambda fam: fam == "BID_SWITCH"),
    ("bid_target", "Bid target change",
     lambda fam: fam in ("TARGET", "TARGET_VALUE_CHANGE",
                         "ADGROUP_TARGET_CHANGE")),
    ("negative_keyword", "Negative keyword add",
     lambda fam: fam == "NEGATIVE_KEYWORD_ADD"),
    ("targeting", "Targeting change",
     lambda fam: fam in _TARGETING_FAMILIES),
    ("pause_enable", "Campaign and ad-group pause / enable",
     lambda fam: fam in ("CAMPAIGN_PAUSE", "CAMPAIGN_ENABLE",
                         "ADGROUP_PAUSE", "ADGROUP_ENABLE")),
    ("ads_assets", "Ad and asset changes",
     lambda fam: fam.startswith("AD_") or fam.startswith("ASSET_")),
    ("combined", "Combined changes",
     lambda fam: fam == "COMPOSITE"),
    ("unlabelled", "Unlabelled",
     lambda fam: fam == "UNKNOWN"),
)


def family_of(transition_key: str | None) -> str:
    if not transition_key:
        return "UNKNOWN"
    return str(transition_key).split(":")[0]


def group_of(transition_key: str | None) -> str:
    fam = family_of(transition_key)
    for key, _label, member in GROUP_DEFS:
        if member(fam):
            return key
    return "other"


def _mean(values) -> float | None:
    return round(sum(values) / len(values), 6) if values else None


class _Cell:
    """One (rollup row, horizon) accumulator."""

    __slots__ = ("scores", "comp_sums", "comp_n", "by_miner", "episodes")

    def __init__(self):
        self.scores: list[float] = []
        self.comp_sums = dict.fromkeys(COMPONENT_KEYS, 0.0)
        self.comp_n = 0
        self.by_miner: dict[str, list[float]] = defaultdict(list)
        # Distinct account changes behind this cell. `scores` counts model
        # PREDICTIONS (one change scored by every model), which is why a row
        # can read "119" — one change, 119 models. Tracking episodes lets the
        # page show changes and predictions apart (Rob, 3 Sept).
        self.episodes: set[str] = set()

    def add(self, miner: str, score: float, components: Mapping | None,
            episode_id: str | None = None):
        self.scores.append(score)
        self.by_miner[miner].append(score)
        if episode_id is not None:
            self.episodes.add(episode_id)
        if components and all(k in components for k in COMPONENT_KEYS):
            for k in COMPONENT_KEYS:
                self.comp_sums[k] += float(components[k])
            self.comp_n += 1

    def render(self, winner: str | None, uid_of: Mapping[str, int],
               min_n_solid: int, min_n_for_best: int) -> dict:
        best = None
        for miner, scores in self.by_miner.items():
            if len(scores) < min_n_for_best:
                continue
            cand = {"uid": uid_of.get(miner), "mean": _mean(scores),
                    "n": len(scores)}
            if best is None or (cand["mean"], cand["n"]) > (best["mean"],
                                                            best["n"]):
                best = cand
        winner_scores = self.by_miner.get(winner) if winner else None
        return {
            "n": len(self.scores),
            "episodes": len(self.episodes),
            "field_mean": _mean(self.scores),
            "best": best,
            "winner": ({"uid": uid_of.get(winner),
                        "mean": _mean(winner_scores),
                        "n": len(winner_scores)} if winner_scores else None),
            "components": ({k: round(self.comp_sums[k] / self.comp_n, 6)
                            for k in COMPONENT_KEYS} if self.comp_n else None),
            "components_n": self.comp_n,
            "solid": len(self.scores) >= min_n_solid,
        }


def build_performance_document(
    entries: Iterable[Mapping],
    key_of: Mapping[str, str],
    basket_day_of: Mapping[str, str],
    *,
    as_of: str,
    winner: str | None,
    uid_of: Mapping[str, int],
    cutoff_day: str = CUTOFF_DAY,
    min_n_solid: int = MIN_N_SOLID,
    min_n_for_best: int = MIN_N_FOR_BEST,
) -> dict:
    """Fold receipt entries into the page document. Deterministic, JSON-ready.

    entries: receipt entry dicts — episode_id, horizon_days, miner, score,
        components (dict or None). An entry's own transition_key field, when
        present and not UNKNOWN, wins over `key_of` (the receipt recorded
        what the settle actually used).
    key_of / basket_day_of: episode_id -> transition_key / basket day, from
        the per-basket maps. An episode with no known basket day cannot be
        placed against the cutoff, so it is EXCLUDED and counted in
        totals.dropped_no_basket_day — dropped visibly, never silently.
    winner: hotkey of the current leaderboard #1 (None on a day with no
        standings; winner cells then render None).
    """
    cells: dict[tuple[str, str, str], _Cell] = {}
    dropped_no_basket = 0
    dropped_horizon = 0
    dropped_pre_cutoff = 0
    miners_seen: set[str] = set()

    for e in entries:
        eid = str(e["episode_id"])
        h = str(int(e["horizon_days"]))
        if h not in HORIZONS:
            dropped_horizon += 1
            continue
        bday = basket_day_of.get(eid)
        if bday is None:
            dropped_no_basket += 1
            continue
        if bday < cutoff_day:
            dropped_pre_cutoff += 1
            continue
        tkey = e.get("transition_key")
        if not tkey or tkey == "UNKNOWN":
            tkey = key_of.get(eid, "UNKNOWN")
        gkey = group_of(tkey)
        miner = str(e["miner"])
        miners_seen.add(miner)
        score = float(e["score"])
        comp = e.get("components")
        for row in (str(tkey), None):        # detail row + group roll-up
            ck = (gkey, row or "__group__", h)
            cell = cells.get(ck)
            if cell is None:
                cell = cells[ck] = _Cell()
            cell.add(miner, score, comp, episode_id=eid)

    labels = {k: label for k, label, _ in GROUP_DEFS}
    labels["other"] = "Other changes"
    groups = []
    order = [k for k, _, _ in GROUP_DEFS] + ["other"]
    for gkey in order:
        row_keys = sorted({rk for (g, rk, _h) in cells
                           if g == gkey and rk != "__group__"})
        if not row_keys:
            continue
        def _cells_for(row_key):
            return {h: cells[(gkey, row_key, h)].render(
                        winner, uid_of, min_n_solid, min_n_for_best)
                    for h in HORIZONS if (gkey, row_key, h) in cells}
        groups.append({
            "key": gkey,
            "label": labels[gkey],
            "cells": _cells_for("__group__"),
            "rows": [{"key": rk, "cells": _cells_for(rk)}
                     for rk in row_keys],
        })

    total_entries = sum(len(c.scores) for (g, rk, _h), c in cells.items()
                        if rk == "__group__")
    return {
        "feed": "sn21-prediction-performance",
        "as_of": as_of,
        "cutoff_day": cutoff_day,
        "horizons": list(HORIZONS),
        "winner_uid": uid_of.get(winner) if winner else None,
        "groups": groups,
        "totals": {
            "entries": total_entries,
            "miners": len(miners_seen),
            "groups": len(groups),
            "dropped_no_basket_day": dropped_no_basket,
            "dropped_unknown_horizon": dropped_horizon,
            "dropped_before_cutoff": dropped_pre_cutoff,
        },
        "min_n_solid": min_n_solid,
        "min_n_for_best": min_n_for_best,
    }
