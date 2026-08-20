"""Accuracy by change type — the one aggregation behind three surfaces.

Rob (20 Aug): (a) the public page next to the leaderboard — winning-model
accuracy per change type with a 7/14/28-day selector; (b) per-miner
win/lose feedback per change type (the original-design promise); (c) the
outcome-by-source benchmark chart shares the same episode metadata.

PURE module: consumes scored (miner, episode, horizon) entries that the
settle flow already produces in memory, plus each episode's transition_key.
No I/O. The daily pipeline calls `aggregate_type_accuracy` after settle and
writes the result next to the other daily artifacts; the publish rail
carries it to the site.

An entry's `score` is the settle-flow final score in [0,1] (same number
that feeds standings — the leaderboard and this page can never disagree).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

# A miner needs this many scored predictions on a type+horizon before they
# can be named "best" on it — one lucky episode is not a champion.
MIN_N_FOR_BEST = 5


@dataclass(frozen=True)
class ScoredEntry:
    """One settled prediction score, as the settle flow computes it."""
    miner: str            # miner id / hotkey (whatever the caller keys on)
    episode_id: str
    transition_key: str   # e.g. "BUDGET:up_large", "COMPOSITE:AD_CREATE+2"
    horizon: str          # "7" | "14" | "28"
    score: float          # final settle score in [0,1]


def type_family(transition_key: str | None) -> str:
    """Bucket a transition_key into its low-cardinality family."""
    if not transition_key:
        return "UNKNOWN"
    return str(transition_key).split(":")[0]


def aggregate_type_accuracy(entries, champion_miner: str | None = None) -> dict:
    """The full aggregation. Deterministic and JSON-ready.

    Returns:
      by_type[family][horizon] = {n, field_mean, champion_mean|None,
                                  best: {miner, mean, n}|None}
      by_miner[miner][family][horizon] = {n, mean, field_mean, delta}
      totals = {entries, miners, types}

    champion_miner: the CURRENT champion's id, so the public page can show
    "the winning model" per type even where it is not the best there.
    `best` is the top mean among miners with >= MIN_N_FOR_BEST entries.
    """
    cell = defaultdict(list)                 # (family, horizon) -> [score]
    mcell = defaultdict(list)                # (miner, family, horizon) -> [score]
    for e in entries:
        fam = type_family(e.transition_key)
        cell[(fam, e.horizon)].append(e.score)
        mcell[(e.miner, fam, e.horizon)].append(e.score)

    def mean(v):
        return round(sum(v) / len(v), 6) if v else None

    by_type: dict = {}
    for (fam, h), scores in cell.items():
        best = None
        for (m, f2, h2), ms in mcell.items():
            if f2 == fam and h2 == h and len(ms) >= MIN_N_FOR_BEST:
                cand = {"miner": m, "mean": mean(ms), "n": len(ms)}
                if best is None or (cand["mean"], cand["n"]) > (best["mean"], best["n"]):
                    best = cand
        champ_scores = mcell.get((champion_miner, fam, h)) if champion_miner else None
        by_type.setdefault(fam, {})[h] = {
            "n": len(scores),
            "field_mean": mean(scores),
            "champion_mean": mean(champ_scores) if champ_scores else None,
            "best": best,
        }

    by_miner: dict = {}
    for (m, fam, h), ms in mcell.items():
        field = mean(cell[(fam, h)])
        mm = mean(ms)
        by_miner.setdefault(m, {}).setdefault(fam, {})[h] = {
            "n": len(ms),
            "mean": mm,
            "field_mean": field,
            "delta": round(mm - field, 6),
        }

    return {
        "by_type": {f: dict(sorted(hs.items())) for f, hs in sorted(by_type.items())},
        "by_miner": {m: {f: dict(sorted(hs.items())) for f, hs in sorted(fs.items())}
                     for m, fs in sorted(by_miner.items())},
        "totals": {
            "entries": sum(len(v) for v in cell.values()),
            "miners": len({m for (m, _, _) in mcell}),
            "types": len({f for (f, _) in cell}),
        },
        "min_n_for_best": MIN_N_FOR_BEST,
    }
