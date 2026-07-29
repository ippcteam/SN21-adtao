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

Date basis (audit 2026-07-29): an entry's day is its SETTLE-CLOCK date —
action_window_end + 1 + horizon + OUTCOME_SETTLING_WINDOW_DAYS — never
the operational write date (measured_at). A pipeline outage that delays
measurement therefore shifts nothing: the late-measured row still enters
on (well, dated to) its true settle date at the next run.

Idempotency (audit 2026-07-29): per-(episode, horizon) entered-markers in
<ledger_root>/standing/_entered_results.jsonl replace the old per-day
marker. A re-run enters exactly the not-yet-entered settled results, so
the same-day race the old marker had — measurement landing after the
settle run had already marked the DAY done, silently skipping those
outcomes forever — cannot occur: they simply enter on the next run.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable, Iterable, Optional

from hope.backtest.gate import METRICS, QUANTILES, pinball
from hope.scoring.daily_score_flow import HorizonResult, day_flow
from hope.scoring import standing_ledger

# Floor for the per-entry pinball normalisation scale — same spirit as
# gate_score's 1e-9 guard, but per entry a tiny |actual| would otherwise
# zero any honest prediction; 0.01 (1%) is the smallest move the stream
# treats as a real magnitude.
MIN_ENTRY_SCALE = 0.01

# Settle clock (mirrors OBI constants.py: OUTCOME_SETTLING_WINDOW_DAYS=7;
# settle date = window_end + 1 + horizon + settle).
SETTLING_WINDOW_DAYS = 7

# Legacy-corpus boundary (audit 2026-07-29): 3,136 outcome rows measured
# before the settle-clock cutover ran under the old 17-day launch clock
# and are frozen at values that were NOT final when written. They must
# never enter live standings; the reference provider excludes them and
# reports the exclusion count loudly.
SETTLE_CLOCK_CUTOVER_UTC = datetime(2026, 7, 22, 10, 10, tzinfo=timezone.utc)


@dataclass(frozen=True)
class SettledHorizon:
    """One finalised (episode, horizon) truth, in delta fractions.

    ``finalized_on`` is the SETTLE-CLOCK date (window_end+1+h+7), not the
    measurement write date.
    """
    episode_id: str
    horizon_days: int
    cost_delta_pct: float
    conversions_delta_pct: float
    efficiency_delta_pct: float
    finalized_on: date


def settle_date(action_window_end: date, horizon_days: int) -> date:
    """The settle-clock date for an (episode, horizon)."""
    return action_window_end + timedelta(days=1 + horizon_days + SETTLING_WINDOW_DAYS)


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
) -> list[HorizonResult]:
    """Every (settled outcome × miner prediction), dated to each outcome's
    OWN settle date.

    ``prediction_index``: episode_id -> miner -> horizons dict (the model
    output shape: {"7": {...}, "14": {...}, "28": {...}}). Episodes with
    no prediction from a miner yield no entry for that miner (see module
    docstring). Nothing is rescored (v0.5 §4) — the caller's entered-
    markers guarantee each (episode, horizon) flows exactly once.
    """
    results: list[HorizonResult] = []
    for o in outcomes:
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
                finalized_on=o.finalized_on,
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


# ---- entered-result markers (idempotency) ------------------------------------

def _entered_path(ledger_root: str) -> str:
    return os.path.join(standing_ledger.standing_dir(ledger_root),
                        "_entered_results.jsonl")


def entered_results(ledger_root: str) -> set[tuple[str, int]]:
    """The (episode_id, horizon) pairs whose results already entered."""
    path = _entered_path(ledger_root)
    if not os.path.exists(path):
        return set()
    out: set[tuple[str, int]] = set()
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            out.add((str(rec["episode_id"]), int(rec["horizon_days"])))
    return out


def _mark_entered(ledger_root: str, pairs: Iterable[tuple[str, int]],
                  run_day: date) -> None:
    os.makedirs(standing_ledger.standing_dir(ledger_root), exist_ok=True)
    with open(_entered_path(ledger_root), "a") as f:
        for ep, h in pairs:
            f.write(json.dumps({"episode_id": ep, "horizon_days": h,
                                "entered_on_run": str(run_day)}) + "\n")


# ---- the daily entrypoint ----------------------------------------------------

def run_settle_day(
    shadow_root: str,
    ledger_root: str,
    day: date,
    outcomes_provider: Callable[[date], list[SettledHorizon]],
    return_results: bool = False,
) -> dict:
    """Enter every settled-but-not-yet-entered result as of `day`.

    ``outcomes_provider(day)`` returns ALL measured rows whose settle date
    is <= day (see obi_outcomes_provider); the entered-markers reduce that
    to exactly the new ones, so re-runs are cheap no-ops and late-measured
    rows enter on the next run, dated to their true settle date. Entries
    are flowed through day_flow PER settle date, preserving D13's
    entry-day semantics even for backfill.
    """
    already = entered_results(ledger_root)
    outcomes = [
        o for o in outcomes_provider(day)
        if (str(o.episode_id), int(o.horizon_days)) not in already
    ]
    index = load_prediction_index(shadow_root)
    results = score_settled(index, outcomes)

    written = 0
    by_settle_date: dict[date, list[HorizonResult]] = {}
    for r in results:
        by_settle_date.setdefault(r.finalized_on, []).append(r)
    for settle_day_, day_results in sorted(by_settle_date.items()):
        entries = day_flow(day_results, settle_day_)
        written += standing_ledger.append_entries(ledger_root, entries)

    # Mark every processed (episode,horizon) — including ones with zero
    # matching predictions: their outcome is final and no later prediction
    # can legitimately appear (models predict before windows close).
    _mark_entered(ledger_root, {(str(o.episode_id), int(o.horizon_days))
                                for o in outcomes}, day)

    out = {
        "status": "settled",
        "day": str(day),
        "new_outcomes": len(outcomes),
        "already_entered": len(already),
        "results_scored": len(results),
        "entries_written": written,
        "miners": len({r.miner for r in results}),
        "settle_dates": sorted(str(d) for d in by_settle_date),
    }
    if return_results:
        # in-process consumers only (daily_loop feeds these to the D11
        # aggregation) — not part of the JSON-able summary contract
        out["horizon_results"] = results
    return out


# ---- production outcomes reader (reference implementation) -------------------

def obi_outcomes_provider(day: date) -> list[SettledHorizon]:
    """All measured rows settle-dated on or before `day`, post-cutover.

    Reference implementation for the validator/ops host — requires the
    OBI repo importable (same pattern as scripts/run_shadow_day_bd.py);
    the module itself stays importable without it. Efficiency maps to
    cpa_delta_pct (see hope/backtest/corpus.py). Values arrive as delta
    fractions, possibly saturated at ±9999.999999 (the outcome writer's
    numeric(10,6) clamp) — passed through unchanged; the per-entry scale
    normalisation keeps saturated actuals from distorting neighbours.

    finalized_on is COMPUTED from the settle clock (action_window_end +
    1 + horizon + 7), never taken from measured_at. Rows measured before
    SETTLE_CLOCK_CUTOVER_UTC (the old 17-day launch clock — values not
    final when written) are excluded, loudly.
    """
    import sys
    sys.path.insert(0, "/Users/macbookm1/Documents/Projects/obi")
    from app.models import get_session
    from sqlalchemy import text as T

    with get_session() as s:
        rows = s.execute(T("""
            SELECT o.episode_candidate_id, o.horizon_days,
                   o.cost_delta_pct, o.conversions_delta_pct, o.cpa_delta_pct,
                   c.action_window_end, o.measured_at
            FROM bittensor_episode_outcomes o
            JOIN bittensor_episode_candidates c ON c.id = o.episode_candidate_id
            WHERE o.measured_at >= :cutover
              AND (c.action_window_end::date
                   + make_interval(days => 1 + o.horizon_days + :settle)) <= :day
        """), {"day": day, "cutover": SETTLE_CLOCK_CUTOVER_UTC,
               "settle": SETTLING_WINDOW_DAYS}).fetchall()
        legacy = s.execute(T("""
            SELECT count(*) FROM bittensor_episode_outcomes
            WHERE measured_at < :cutover
        """), {"cutover": SETTLE_CLOCK_CUTOVER_UTC}).scalar() or 0

    if legacy:
        print(f"[settle-day] excluded {legacy} pre-cutover legacy outcome rows "
              f"(old launch clock; values not final when written)", flush=True)

    out = []
    for r in rows:
        we = r[5].date() if hasattr(r[5], "date") else r[5]
        out.append(SettledHorizon(
            episode_id=str(r[0]),
            horizon_days=int(r[1]),
            cost_delta_pct=float(r[2] or 0),
            conversions_delta_pct=float(r[3] or 0),
            efficiency_delta_pct=float(r[4] or 0),
            finalized_on=settle_date(we, int(r[1])),
        ))
    return out
