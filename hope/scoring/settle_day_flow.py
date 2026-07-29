"""Settle-day scoring — shadow predictions meet settled outcomes (E1 inflow).

The missing glue between M3 shadow mode and the D13 standings: when an
(episode, horizon) outcome finalises under the settle clock, every shadow
prediction for that episode is scored, becomes a HorizonResult, flows
through day_flow (horizon blend weights), and lands in the standing
ledger. From the first settlements (~stream day 15) standings begin
differentiating with no manual step.

Scoring formula: the backtest gate's published pair (gate.py) at
per-(episode,horizon) granularity — 0.7 × quantile + 0.3 × direction,
pinball loss normalised by the entry's own actual magnitudes, and the
zero-abstention rule (a zero p50 against a nonzero actual is a direction
MISS; without it a persistence model scores perfect direction by
abstaining — caught on the real W28–W30 corpus). The chain scorer
(onchain_adapter.score_one_miner_per_episode) is NOT reused here because
it blends a probability-calibration term whose truth (goal-miss /
instability observations) the outcome tables do not carry; inventing
that truth would corrupt 30% of every score.

Missing prediction for a settled episode → NO entry, not a zero: under
D13 each prediction contributes exactly one unit of evidence weight, so
absence self-discounts the miner's evidence mass (and the cold-start
floors) without fabricating a judgement they never made. Abstention
economics are handled where they belong: coverage requirements at
admission ([M1] ≥90%) and the participation gate at weights.

Idempotency: a settle-day is recorded in <ledger_root>/standing/
_settled_days.json after a successful append; re-running that day is a
loud no-op (double-appending would double-count evidence).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Callable, Iterable, Optional

from hope.backtest.gate import METRICS, QUANTILES, pinball
from hope.scoring.daily_score_flow import HorizonResult, day_flow
from hope.scoring import standing_ledger

# Floor for the per-entry pinball normalisation scale — same spirit as
# gate_score's 1e-9 guard, but per entry a tiny |actual| would otherwise
# zero any honest prediction; 0.01 (1%) is the smallest move the stream
# treats as a real magnitude.
MIN_ENTRY_SCALE = 0.01


@dataclass(frozen=True)
class SettledHorizon:
    """One finalised (episode, horizon) truth, in delta fractions."""
    episode_id: str
    horizon_days: int
    cost_delta_pct: float
    conversions_delta_pct: float
    efficiency_delta_pct: float
    finalized_on: date


def score_entry(pred: dict, actual: dict[str, float]) -> float:
    """Gate formula for ONE (episode, horizon): 0.7×quantile + 0.3×direction.

    ``pred`` is the shadow/model shape: {metric: {p10, p50, p90}, ...}
    (extra keys like goal_miss_probability are ignored). ``actual`` maps
    the three METRICS to delta fractions. Metrics the model omitted score
    nothing on the quantile side and count as direction misses when the
    actual is nonzero (same abstention discipline).
    """
    losses: list[float] = []
    scales: list[float] = []
    dir_hits = 0
    dir_total = 0
    for m in METRICS:
        a = actual[m]
        trio = pred.get(m)
        if trio:
            for q, key in QUANTILES:
                losses.append(pinball(a, float(trio[key]), q))
            scales.append(abs(a))
        if a != 0:
            dir_total += 1
            p50 = float(trio["p50"]) if trio else 0.0
            if p50 != 0 and (a > 0) == (p50 > 0):
                dir_hits += 1
    if not losses:
        return 0.0
    scale = max(sum(scales) / len(scales), MIN_ENTRY_SCALE)
    quantile = max(0.0, 1.0 - (sum(losses) / len(losses)) / scale)
    direction = (dir_hits / dir_total) if dir_total else 0.0
    return round(0.7 * quantile + 0.3 * direction, 6)


def score_settled(
    prediction_index: dict[str, dict[str, dict]],
    outcomes: Iterable[SettledHorizon],
    day: date,
) -> list[HorizonResult]:
    """Every (settled outcome × miner prediction) finalised on `day`.

    ``prediction_index``: episode_id -> miner -> horizons dict (the model
    output shape: {"7": {...}, "14": {...}, "28": {...}}). Episodes with
    no prediction from a miner yield no entry for that miner (see module
    docstring). Outcomes finalised on other days are ignored — they enter
    on their own settle days, nothing is rescored (v0.5 §4).
    """
    results: list[HorizonResult] = []
    for o in outcomes:
        if o.finalized_on != day:
            continue
        actual = {
            "cost_delta_pct": o.cost_delta_pct,
            "conversions_delta_pct": o.conversions_delta_pct,
            "efficiency_delta_pct": o.efficiency_delta_pct,
        }
        for miner, horizons in (prediction_index.get(o.episode_id) or {}).items():
            pred = horizons.get(str(o.horizon_days))
            if not pred:
                continue
            results.append(HorizonResult(
                episode_id=o.episode_id,
                horizon_days=o.horizon_days,
                miner=miner,
                score=score_entry(pred, actual),
                finalized_on=day,
            ))
    return results


# ---- shadow-ledger prediction lookup ----------------------------------------

def load_prediction_index(shadow_root: str) -> dict[str, dict[str, dict]]:
    """episode_id -> miner -> horizons, across ALL shadow days.

    An episode predicted on day X settles on days X+15/X+22/X+36, so the
    lookup must span day directories. Later records win per (episode,
    miner) — a re-run day supersedes (record_day appends; last line is
    the operative run, matching finalize_day's `lines[-1]` discipline).
    """
    index: dict[str, dict[str, dict]] = {}
    base = os.path.join(shadow_root, "shadow")
    if not os.path.isdir(base):
        return index
    for day_dir in sorted(os.listdir(base)):
        d = os.path.join(base, day_dir)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".jsonl"):
                continue
            with open(os.path.join(d, fn)) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    miner = rec.get("hotkey")
                    for ep_id, horizons in (rec.get("predictions") or {}).items():
                        index.setdefault(ep_id, {})[miner] = horizons
    return index


# ---- idempotency marker ------------------------------------------------------

def _marker_path(ledger_root: str) -> str:
    return os.path.join(standing_ledger.standing_dir(ledger_root),
                        "_settled_days.json")


def settled_days(ledger_root: str) -> set[str]:
    path = _marker_path(ledger_root)
    if not os.path.exists(path):
        return set()
    with open(path) as f:
        return set(json.load(f).get("days", []))


def _mark_settled(ledger_root: str, day: date) -> None:
    days = settled_days(ledger_root)
    days.add(str(day))
    os.makedirs(standing_ledger.standing_dir(ledger_root), exist_ok=True)
    tmp = _marker_path(ledger_root) + ".tmp"
    with open(tmp, "w") as f:
        json.dump({"days": sorted(days)}, f)
    os.replace(tmp, _marker_path(ledger_root))


# ---- the daily entrypoint ----------------------------------------------------

def run_settle_day(
    shadow_root: str,
    ledger_root: str,
    day: date,
    outcomes_provider: Callable[[date], list[SettledHorizon]],
) -> dict:
    """Score everything that settled on `day` into the standing ledger.

    Injected ``outcomes_provider(day)`` returns that day's finalised
    horizons (see obi_outcomes_provider for the production reader).
    Loud skip when the day was already processed.
    """
    if str(day) in settled_days(ledger_root):
        return {"status": "already_settled", "day": str(day),
                "reason": "settle-day marker present; re-append would "
                          "double-count evidence"}

    outcomes = outcomes_provider(day)
    index = load_prediction_index(shadow_root)
    results = score_settled(index, outcomes, day)
    entries = day_flow(results, day)
    written = standing_ledger.append_entries(ledger_root, entries)
    _mark_settled(ledger_root, day)

    return {
        "status": "settled",
        "day": str(day),
        "outcomes": len(outcomes),
        "results_scored": len(results),
        "entries_written": written,
        "miners": len({r.miner for r in results}),
    }


# ---- production outcomes reader (reference implementation) -------------------

def obi_outcomes_provider(day: date) -> list[SettledHorizon]:
    """Read the day's finalised horizons from OBI's outcome table.

    Reference implementation for the validator/ops host — requires the
    OBI repo importable (same pattern as scripts/run_shadow_day_bd.py);
    the module itself stays importable without it. Efficiency maps to
    cpa_delta_pct (see hope/backtest/corpus.py). Values arrive as delta
    fractions, possibly saturated at ±9999.999999 (the outcome writer's
    numeric(10,6) clamp) — passed through unchanged; the per-entry scale
    normalisation keeps saturated actuals from distorting neighbours.
    """
    import sys
    sys.path.insert(0, "/Users/macbookm1/Documents/Projects/obi")
    from app.models import get_session
    from sqlalchemy import text as T

    with get_session() as s:
        rows = s.execute(T("""
            SELECT o.episode_candidate_id, o.horizon_days,
                   o.cost_delta_pct, o.conversions_delta_pct, o.cpa_delta_pct
            FROM bittensor_episode_outcomes o
            WHERE o.measured_at::date = :day
        """), {"day": day}).fetchall()

    return [
        SettledHorizon(
            episode_id=str(r[0]),
            horizon_days=int(r[1]),
            cost_delta_pct=float(r[2] or 0),
            conversions_delta_pct=float(r[3] or 0),
            efficiency_delta_pct=float(r[4] or 0),
            finalized_on=day,
        )
        for r in rows
    ]
