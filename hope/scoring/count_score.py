"""Conversions on small campaigns scored as a count (Sir Rob, 19 Oct 2026, options a + c).

When fewer than 3 conversions are expected after a change, the result carries no
conversion percentage (a quiet week reads -100% by chance). From
SN21_COUNT_SCORE_FROM those results are scored on the conversion COUNT instead:

- expected count E = the campaign's daily conversions before x the horizon days;
  actual count y = its daily conversions after x the same days.
- the miner's count comes from the forecast they already send: mu = E x (1 + p50).
- error is the Poisson deviance, which handles zero counts; "no change" is mu = E.
- score = D(no change) / (D(miner) + D(no change)): 0.5 is as good as "no change",
  1.0 exact, below 0.5 worse. No tuning numbers. Both exact -> 0.5.

(a) The count score is the conversion part of those results, weighed the same as
    cost (COUNT_SHARE), blended into the entry score.
(c) From SN21_COUNT_SCORE_SHOW_FROM (four weeks earlier) the same score is pooled
    per miner per day over all their small-campaign results (sums of deviances)
    and published beside the standing, not counted in it.

Measured on the 16-18 Oct receipts before building: the typical miner would score
about 0.41 on small campaigns and 0.58 on busy ones.
"""

from __future__ import annotations

import math
import os
from collections import defaultdict
from datetime import date

SWITCH_ENV = "SN21_COUNT_SCORE_FROM"            # (a) counts in the entry score
SHOW_ENV = "SN21_COUNT_SCORE_SHOW_FROM"         # (c) pooled score published, not counted
COUNT_SHARE = 0.5          # the conversion count weighs the same as cost on these results
MU_FLOOR = 1e-6


def in_force(day, environ=os.environ, env_name=SWITCH_ENV) -> bool:
    raw = (environ.get(env_name) or "").strip()
    if not raw or day is None:
        return False
    try:
        start = date.fromisoformat(raw)
    except ValueError:
        return False
    d = day if isinstance(day, date) else date.fromisoformat(str(day)[:10])
    return d >= start


def shown(day, environ=os.environ) -> bool:
    """The pooled score is published from its own date (four weeks before it
    counts), and always once the count counts."""
    return in_force(day, environ, SHOW_ENV) or in_force(day, environ)


def deviance(y: float, mu: float) -> float:
    """Poisson deviance of one count against a prediction."""
    mu = max(float(mu), MU_FLOOR)
    y = max(float(y), 0.0)
    return 2.0 * ((y * math.log(y / mu) if y > 0 else 0.0) - (y - mu))


def predicted_count(expected: float, p50) -> float:
    return max(float(expected) * (1.0 + float(p50)), 0.0)


def deviances(pred: dict, expected: float, actual: float) -> tuple[float, float] | None:
    """(miner's deviance, no-change deviance), or None without a conversions forecast."""
    trio = (pred or {}).get("conversions_delta_pct")
    if not trio or trio.get("p50") is None or not expected or expected <= 0:
        return None
    return (deviance(actual, predicted_count(expected, trio["p50"])),
            deviance(actual, expected))


def score_from(d_miner: float, d_naive: float) -> float:
    total = d_miner + d_naive
    if total <= 0:
        return 0.5
    return round(d_naive / total, 6)


def count_component(pred: dict, expected: float, actual: float) -> float | None:
    d = deviances(pred, expected, actual)
    return None if d is None else score_from(*d)


def blend(entry_score: float, count: float | None) -> float:
    """The entry's score with the count as its conversion part."""
    if count is None:
        return entry_score
    return round((1.0 - COUNT_SHARE) * entry_score + COUNT_SHARE * count, 6)


class Pooled:
    """Per miner, per day: deviance sums over every small-campaign result (option c)."""

    def __init__(self):
        self._s = defaultdict(lambda: [0.0, 0.0, 0])

    def add(self, miner: str, pred: dict, expected: float, actual: float) -> None:
        d = deviances(pred, expected, actual)
        if d is None:
            return
        row = self._s[miner]
        row[0] += d[0]
        row[1] += d[1]
        row[2] += 1

    def report(self) -> dict:
        return {m: {"results": n, "score": score_from(dm, dn)}
                for m, (dm, dn, n) in sorted(self._s.items())}
