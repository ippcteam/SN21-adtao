"""E1 — the daily score-flow: what enters the standing each day.

Design v0.5 §2/§4 + GAP-2 v2 §4: on any calendar day, three different day-
baskets finish maturing (7d at day 15, 14d at day 22, 28d at day 36 under
the settle clock). Each (episode, horizon) result finalising that day
becomes ONE weighted entry in the miner's episode-age average:

    entry.score  = the (episode, horizon) score
    entry.weight = horizon blend weight (D-STREAM_HORIZON_WEIGHTS)
    entry.day    = the finalisation day (age decay starts here)

Why per-(episode,horizon) entries with blend weights, not per-episode
blended scores: an episode's horizons finalise on DIFFERENT days; waiting
for all three would delay 7d signal by three weeks, and folding day-
averages is the rejected day-weighted form (D13). With blend weights
summing to 1, one episode contributes exactly 1.0 total entry weight
across its life — "each prediction contributes equally" (D13) holds at
the episode level while signal lands as early as each horizon allows.

Pure module: no I/O. Orchestration (which baskets finalise today, invoking
the v2 scorer per miner) wires in with the Payload-v2 submission shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable

# Daily-stream horizons (v0.5 §2). The launch HORIZONS=[7,14] constant is
# unchanged; the stream adds 28d at cutover.
DAILY_STREAM_HORIZONS = [7, 14, 28]

# Blend weights for the daily stream, per measurement resolution — v0.5
# keeps "horizons blend at 7/14/28"; these extend the launch 2-horizon
# weights with the 28d share. Review-set numbers ([GAP-2]); shares per
# resolution sum to 1.0.
DAILY_STREAM_HORIZON_WEIGHTS = {
    "high":   {"7": 0.30, "14": 0.40, "28": 0.30},
    "medium": {"7": 0.25, "14": 0.40, "28": 0.35},
    "low":    {"7": 0.20, "14": 0.40, "28": 0.40},
}


@dataclass(frozen=True)
class HorizonResult:
    """One finalised (episode, horizon) score for one miner."""
    episode_id: str
    horizon_days: int
    miner: str
    score: float                 # [0,1] from the v2 formula
    finalized_on: date           # settle-clock date (day 15/22/36)
    resolution: str = "high"     # measurement resolution → blend weights


@dataclass(frozen=True)
class WeightedEntry:
    """What the standing average consumes: score + entry weight + entry day."""
    miner: str
    score: float
    weight: float
    entered_on: date
    episode_id: str = ""
    horizon_days: int = 0


def horizon_entry_weight(horizon_days: int, resolution: str = "high") -> float:
    """Blend weight for a horizon under the daily stream. Unknown resolution
    falls back to 'high'; unknown horizon is an error (guards against a
    silently-unweighted new horizon)."""
    table = DAILY_STREAM_HORIZON_WEIGHTS.get(resolution,
                                             DAILY_STREAM_HORIZON_WEIGHTS["high"])
    key = str(horizon_days)
    if key not in table:
        raise ValueError(f"no blend weight for horizon {horizon_days}")
    return table[key]


def day_flow(results: Iterable[HorizonResult], day: date) -> list[WeightedEntry]:
    """The entries a given day contributes to standings: every (episode,
    horizon, miner) result finalised on that day, weighted by its horizon's
    blend share. Results from other days are ignored (they entered/enter on
    their own finalisation days — nothing is ever rescored, v0.5 §4)."""
    return [
        WeightedEntry(
            miner=r.miner,
            score=r.score,
            weight=horizon_entry_weight(r.horizon_days, r.resolution),
            entered_on=r.finalized_on,
            episode_id=r.episode_id,
            horizon_days=r.horizon_days,
        )
        for r in results
        if r.finalized_on == day
    ]


def episode_total_weight(resolution: str = "high") -> float:
    """Invariant guard: one episode's entries sum to exactly 1.0 across its
    three horizons — the D13 'each prediction contributes equally' property."""
    return sum(DAILY_STREAM_HORIZON_WEIGHTS[resolution].values())
