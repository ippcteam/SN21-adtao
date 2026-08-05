"""D11 Feed 1 — daily full-cohort prediction accuracy (aggregation only).

Consumes the day's scored results (the same per-(episode,horizon) shapes the
E1 score-flow produces) and yields the metrics dict the rail publishes.
Aggregates only — no miner identities beyond count (the public feed proves
the scoring is real; leaderboards are a chain concern, not this feed's).
Publishing clocks per v0.5 §13: first partials ~day 15, full depth day 36.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from hope.scoring.daily_score_flow import HorizonResult

ACCURACY_FEED_NAME = "sn21-prediction-accuracy-daily"


def build_accuracy_metrics(results: Iterable[HorizonResult]) -> dict:
    """Aggregate one day's finalised (episode, horizon, miner) scores."""
    by_h: dict[int, list[float]] = defaultdict(list)
    episodes: set[str] = set()
    miners: set[str] = set()
    for r in results:
        by_h[r.horizon_days].append(r.score)
        episodes.add(r.episode_id)
        miners.add(r.miner)

    horizons = {}
    for h, scores in sorted(by_h.items()):
        horizons[str(h)] = {
            "scored": len(scores),
            "mean_score": round(sum(scores) / len(scores), 6),
            "min_score": round(min(scores), 6),
            "max_score": round(max(scores), 6),
        }
    total = sum(len(v) for v in by_h.values())
    return {
        "feed": ACCURACY_FEED_NAME,
        "episodes_finalised": len(episodes),
        "miners_scored": len(miners),
        "results_total": total,
        "horizons": horizons,
    }
