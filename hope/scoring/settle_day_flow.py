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

Formula restoration (2026-07-30), behind SN21_SETTLE_SCORING_V2
-----------------------------------------------------------------
The gate pair above is the SIMPLIFIED formula. The published spec
(docs/SN21_REWARD_MECHANISM.md:133) is four components:

    0.50×quantile + 0.20×calibration + 0.15×directional + 0.15×goal

with directional and goal both scoring "the account's primary goal
metric" (spec:137-138), not the three-metric average. The simplification
dropped calibration and goal wholesale on the grounds that the outcome
tables carry no truth for them. Re-excavation found that half true:

  * calibration = probability-calibration (goal_miss / instability
    frequencies) AND interval coverage. The probability half genuinely
    has no truth in our tables and stays dropped. The COVERAGE half —
    does [P10,P90] contain the actual — is computable from exactly the
    data we already have, and comes back at its own half-weight (0.10).
  * goal = P50 accuracy on the goal metric. Fully computable. The
    legacy scorer already implements it (onchain_adapter._p50_goal_score)
    and the resolver that reads each account's configured goal already
    runs in production (OBI episode_payload_builder_service:282-307).

So v2 = 0.50 quantile + 0.10 coverage + 0.15 directional + 0.15 goal,
renormalised by 0.90 (the dropped probability half is removed from the
denominator, not silently redistributed — a miner cannot be scored on a
component we cannot measure, and must not be diluted by it either).

Nothing here is invented: each component is the legacy implementation
ported to this module's units (see the constants block). The ONE
deliberate deviation is the quantile term, which keeps this module's
per-entry normalisation (by the entry's own actual magnitudes) rather
than the legacy fixed PINBALL_SCALE=300 dpct — the legacy constant's own
code comment records that at 300 "a half-right prediction, a do-nothing,
and a wrong-direction one all score ~0.86", i.e. it does not
differentiate. The spec names the component, not the constant.

The goal metric is `efficiency_delta_pct` in prediction space; which
COLUMN feeds its truth (cpa_delta_pct vs conversion_value_delta_pct)
is the account-goal question, resolved in obi_outcomes_provider.

Default OFF. The composition and the efficiency-truth basis are both
miner-facing and ship with Rob's governance amendment; the flag is the
single switch for both, so one announcement covers one behaviour change.
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

# ---- v2 restoration constants ------------------------------------------------
# Ported from hope/scoring/onchain_adapter.py, converted from that module's
# DECI-PERCENT integers to this module's DELTA FRACTIONS. The conversion is
# ÷1000: deci-percent = percent×10, fraction = percent÷100. OBI writes these
# as ratios — outcome_measurement_service:191 `(o_cpa - b_cpa) / max(b_cpa, 1)`
# — so 0.40 here is the +40% that is 400 dpct there.
#
# The account's primary goal metric, in prediction space. Miners always
# predict a metric named efficiency_delta_pct; the account's goal decides
# which measured column supplies its truth (see obi_outcomes_provider).
GOAL_METRIC = "efficiency_delta_pct"

# _p50_goal_score: 1.0 at exact, decaying linearly to 0. Legacy 400 dpct.
P50_GOAL_SCALE = 0.40
# _coverage_score: convex width penalty — a band this wide earns no coverage
# credit at all, so coverage cannot be bought with [-inf, +inf]. Legacy 1000 dpct.
INTERVAL_WIDTH_SCALE = 1.0
# _direction_score: below this magnitude a move is "flat". Legacy 5 dpct.
DIRECTION_FLAT_BAND = 0.005

# Spec weights (docs/SN21_REWARD_MECHANISM.md:133), with the probability half
# of calibration removed and the remainder renormalised — see module docstring.
W_QUANTILE = 0.50
W_COVERAGE = 0.10      # = 0.20 calibration × the computable half
W_DIRECTION = 0.15
W_GOAL = 0.15
W_TOTAL = W_QUANTILE + W_COVERAGE + W_DIRECTION + W_GOAL   # 0.90

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
    # Which measured column supplied efficiency_delta_pct: "cpa" (the
    # account's goal is cost-per-acquisition, or it has no configured goal)
    # or "conversion_value" (a ROAS/value goal). Recorded, never scored on —
    # it exists so a verifier can see WHY an entry's goal metric is what it
    # is without re-running the resolver. Default preserves the pre-v2
    # behaviour, where efficiency was always CPA.
    goal_basis: str = "cpa"


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


def entry_components(pred: dict, actual: dict[str, float]) -> tuple[float, float]:
    """(quantile, direction) components of score_entry, same arithmetic.

    Exposed for the J3 vertical-error series, which stores RAW components
    so the weekly aggregate survives a formula recomposition (the goal-
    metric term is in front of Rob). Kept as a separate walk of the same
    logic rather than a refactor of score_entry so the scoring hot path
    and its tests stay byte-identical.
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
        return 0.0, 0.0
    scale = max(sum(scales) / len(scales), MIN_ENTRY_SCALE)
    quantile = max(0.0, 1.0 - (sum(losses) / len(losses)) / scale)
    direction = (dir_hits / dir_total) if dir_total else 0.0
    return round(quantile, 6), round(direction, 6)


# ---- v2 components (spec restoration; see module docstring) ------------------
# Each is the legacy onchain_adapter implementation in this module's units.
# A MISSING metric scores 0 in all three rather than being skipped: the
# quantile term averages only over metrics the miner supplied, so omission
# must cost somewhere or a partial predictor drops their hard metrics and
# gains. This matches the existing direction term, which already counts an
# omitted metric as a miss.

def _coverage_score(trio: Optional[dict], actual: float) -> float:
    """Does [P10,P90] contain the actual, less a convex width penalty.

    Note what the legacy formula does and does not do (measured, not
    assumed): the penalty saturates at 1.0 and carries a 0.5 coefficient,
    so ANY covering band scores at least 0.5 no matter how absurdly wide.
    Coverage alone therefore does not punish a band-inflation strategy —
    the QUANTILE term does, and decisively. A [-5.0, +5.0] band against a
    0.4 actual takes coverage 0.5 but drives pinball loss to the
    normalisation scale, collapsing the 0.50-weighted quantile term to
    0.0. Net, inflation is heavily loss-making. Ported unchanged from
    onchain_adapter._coverage_score — the shape is the system's own.
    """
    if not trio:
        return 0.0
    p10, p90 = float(trio["p10"]), float(trio["p90"])
    covered = 1.0 if p10 <= actual <= p90 else 0.0
    width_penalty = min(1.0, max(0.0, p90 - p10) / INTERVAL_WIDTH_SCALE)
    return max(0.0, covered - 0.5 * width_penalty)


def _goal_p50_score(trio: Optional[dict], actual: float) -> float:
    """P50 accuracy on the goal metric: 1.0 exact, decaying linearly to 0."""
    if not trio:
        return 0.0
    return max(0.0, 1.0 - abs(float(trio["p50"]) - actual) / P50_GOAL_SCALE)


def _goal_direction_score(trio: Optional[dict], actual: float) -> float:
    """Direction of the goal metric only (spec:137), legacy semantics.

    Both-flat earns HALF, not full: predicting flat is the trivial call and
    must not score like a committed up/down one. The legacy comment records
    why — giving it 1.0 floored the predict-zero baseline higher than it
    deserved on stable epochs. This is the v2 form of the same abstention
    discipline the gate formula enforces with its zero-p50 rule.
    """
    if not trio:
        return 0.0
    p50 = float(trio["p50"])
    sp = 0 if abs(p50) < DIRECTION_FLAT_BAND else (1 if p50 > 0 else -1)
    st = 0 if abs(actual) < DIRECTION_FLAT_BAND else (1 if actual > 0 else -1)
    if sp != st:
        return 0.0
    return 0.5 if sp == 0 else 1.0


def entry_components_v2(pred: dict, actual: dict[str, float]) -> dict:
    """The four spec components for one (episode, horizon) entry.

    Quantile is this module's per-entry-normalised term (shared with the v1
    scorer — one implementation, no drift). Coverage averages the three
    metrics; direction and goal score the GOAL metric alone, per spec:137-138.
    """
    quantile, _v1_direction = entry_components(pred, actual)
    coverage = sum(_coverage_score(pred.get(m), actual[m])
                   for m in METRICS) / len(METRICS)
    goal_trio = pred.get(GOAL_METRIC)
    goal_actual = actual[GOAL_METRIC]
    return {
        "quantile": quantile,
        "coverage": round(coverage, 6),
        "direction": round(_goal_direction_score(goal_trio, goal_actual), 6),
        "goal": round(_goal_p50_score(goal_trio, goal_actual), 6),
    }


def score_entry_v2(pred: dict, actual: dict[str, float]) -> float:
    """Spec-restored formula for ONE (episode, horizon). See module docstring.

    An empty prediction still scores exactly 0.0, as under v1.
    """
    c = entry_components_v2(pred, actual)
    blended = (
        W_QUANTILE * c["quantile"]
        + W_COVERAGE * c["coverage"]
        + W_DIRECTION * c["direction"]
        + W_GOAL * c["goal"]
    ) / W_TOTAL
    return round(max(0.0, min(1.0, blended)), 6)


# ---- goal-basis resolution (Rob, 2026-07-31) ---------------------------------
# "yes, ecomm ROAS, lead gen CPA. Yes, go with account configured (or implied)
# goal, not campaign bid strategy."
#
# Configured = accountgoals.metric_type. IMPLIED = the account's taxonomy root,
# which is the account-level ecommerce signal the system already carries; the
# campaign's bid strategy is deliberately NOT consulted, per Rob.
#
# Then the GUARD, which is not optional. outcome_measurement_service.py:195
# computes conversion-value delta as
#     cv_delta = (o_cv - b_cv) / max(b_cv, 1) if b_cv else 0
# so when an account's baseline conversion value is zero the truth is a CONSTANT
# ZERO no matter what happened. Scoring such an episode on conversion value hands
# any miner who predicts zero a near-perfect goal term, the both-flat half of
# direction, trivial coverage and a third of quantile — roughly 30% of the
# composed score, free and deterministic. So a value basis requires that the
# account actually recorded conversion value in the baseline window.
#
# Measured 2026-08-01 (jayesh, exact per-episode signal): of 874 measured
# ecommerce-tagged episodes 554 (63%) survive the guard and 320 (37%) fall back
# to CPA — value-based truth is ~7% of all episodes, NOT the ~10% an
# account-level proxy suggested. The gap is the point: an account can record
# conversion value somewhere over six months yet have none in a specific
# episode's baseline window. Per-episode catches those; account-level misses them.
VALUE_GOAL_MARKERS = ("ROAS", "VALUE", "REVENUE")
TAXONOMY_VALUE_ROOT = "retail"


def resolve_goal_basis(goal_metric_type: Optional[str],
                       taxonomy_root: Optional[str],
                       baseline_conv_value_micros) -> tuple[str, bool]:
    """(basis, guard_applied) for one episode. Pure.

    Cascade: configured account goal -> implied account taxonomy -> CPA.
    Then the zero-baseline guard vetoes a value basis. Returned separately
    from the basis so the veto is countable rather than silent.
    """
    metric = (goal_metric_type or "").strip()
    if metric:
        # A CONFIGURED goal wins outright — taxonomy must never override it.
        # Same precedence jayesh's J3 map states: "goal type is present and
        # says not-ecommerce, so taxonomy does NOT override." Without this
        # branch a configured CPA goal on a retail account was silently
        # flipped to conversion_value.
        intended = ("conversion_value"
                    if any(m in metric.upper() for m in VALUE_GOAL_MARKERS)
                    else "cpa")
    elif (taxonomy_root or "").strip().lower() == TAXONOMY_VALUE_ROOT:
        intended = "conversion_value"
    else:
        intended = "cpa"

    if intended == "conversion_value" and not float(baseline_conv_value_micros or 0) > 0:
        return "cpa", True
    return intended, False


def basis_for_row(frozen_basis: Optional[str],
                  frozen_guarded,
                  goal_metric_type: Optional[str],
                  taxonomy_root: Optional[str],
                  baseline_conv_value_micros) -> tuple[str, bool, bool]:
    """(basis, guard_applied, was_frozen) for one measured row.

    THE FROZEN VALUE WINS. OBI resolves the basis at REVEAL and persists it on
    ``bittensor_episode_candidates.goal_basis`` (see
    app/services/bittensor/goal_basis_service.py). When it is present, this
    reader uses it verbatim and does not consult accountgoals, the taxonomy or
    the baseline at all — that is the whole point: the tables those come from
    keep moving after reveal, and re-deriving here would re-open the gap the
    freeze closes.

    ``resolve_goal_basis`` is the PRE-FREEZE FALLBACK, not dead code: the
    4,377 candidates reserved before the freeze shipped carry NULL and must
    still score. Their basis is recomputed, and the counts are reported
    separately so the residual is visible rather than assumed away.
    """
    if frozen_basis:
        return str(frozen_basis), bool(frozen_guarded), True
    basis, guarded = resolve_goal_basis(
        goal_metric_type, taxonomy_root, baseline_conv_value_micros)
    return basis, guarded, False


def settle_scoring_v2_enabled(environ=os.environ) -> bool:
    """Single source of truth for which settle formula is active.

    Default OFF: the composition and the efficiency-truth basis are
    miner-facing and ship with Rob's governance amendment.
    """
    return environ.get("SN21_SETTLE_SCORING_V2", "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def score_entry_active(pred: dict, actual: dict[str, float],
                       environ=os.environ) -> float:
    """The active per-entry scorer. Every scoring path goes through here so
    the ledger can never mix formulas within a run."""
    if settle_scoring_v2_enabled(environ):
        return score_entry_v2(pred, actual)
    return score_entry(pred, actual)


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
                score=score_entry_active(pred, actual),
                finalized_on=o.finalized_on,
            ))
    return results


def score_settled_with_components(
    prediction_index: dict,
    outcomes: Iterable[SettledHorizon],
) -> tuple[list[HorizonResult], dict]:
    """score_settled plus a components map for the J3 vertical series:
    {(episode_id, horizon_days, miner): (quantile, direction, coverage, goal)}
    — raw parts of each entry's score, stored so the series survives a
    formula recomposition (Rob's pending decision).

    Positions 0 and 1 keep their original meaning for existing consumers.
    Position 1 is the ACTIVE formula's direction term: the three-metric
    average under v1, the goal metric alone under v2 (spec:137). Coverage
    and goal are computed either way — under v1 they are unused by the
    score but still recorded, which is the whole point of the series.
    """
    outcomes = list(outcomes)  # consumed twice; a generator would starve pass 2
    results = score_settled(prediction_index, outcomes)
    comps: dict = {}
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
            v1_quantile, v1_direction = entry_components(pred, actual)
            c2 = entry_components_v2(pred, actual)
            direction = (c2["direction"] if settle_scoring_v2_enabled()
                         else v1_direction)
            comps[(o.episode_id, o.horizon_days, miner)] = (
                v1_quantile, direction, c2["coverage"], c2["goal"],
            )
    return results, comps


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
    results, components = score_settled_with_components(index, outcomes)

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
        # aggregation and the J3 vertical series) — not part of the
        # JSON-able summary contract
        out["horizon_results"] = results
        out["components"] = components
    return out


# ---- production outcomes reader (reference implementation) -------------------

def _has_frozen_basis_column(session) -> bool:
    """Whether the OBI database has migration 20260801_btc_goal_basis applied.

    The validator host and OBI deploy independently, so this reader can run
    against a database that predates the freeze columns. A probe beats a
    validator that crashes on `column c.goal_basis does not exist`; without the
    columns everything simply recomputes, which is the documented pre-freeze
    behaviour, and the provenance line says so out loud.

    Three things this gets right that the first cut did not:

    * SAVEPOINT. A failed statement poisons the whole transaction on
      PostgreSQL, so a bare try/except here would leave the session unusable
      and the very next query — the real one — would fail with
      "current transaction is aborted". begin_nested() rolls back just the
      probe. This is the same defect the OBI side was repaired for; it lived
      on in this twin.
    * BOTH columns. The query this gates selects goal_basis AND
      goal_basis_guarded. Probing only the first means a half-applied
      migration reads as applied and the reader crashes on the second — the
      exact failure the probe exists to prevent.
    * table_schema pinned. Without it, a same-named table in another schema
      on the search_path answers for ours.
    """
    from sqlalchemy import text as T
    try:
        with session.begin_nested():
            n = session.execute(T("""
                SELECT count(*) FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'bittensor_episode_candidates'
                  AND column_name IN ('goal_basis', 'goal_basis_guarded')
            """)).scalar() or 0
        if 0 < n < 2:
            print(f"[settle-day] PARTIAL freeze migration: {n}/2 goal_basis "
                  f"columns present — recomputing every basis", flush=True)
        return n == 2
    except Exception as err:  # noqa: BLE001
        print(f"[settle-day] frozen-basis column probe failed ({err}) — "
              f"recomputing every basis", flush=True)
        return False


def _has_censoring_column(session) -> bool:
    """Whether OBI migration 20260804_censoring is applied. Same probe
    discipline as _has_frozen_basis_column (SAVEPOINT, current_schema) and the
    same reason: the validator host and OBI deploy independently, and a reader
    that crashes on `column o.censored_reason does not exist` takes the whole
    settle run with it. Without the column, no rows are censored yet and the
    filter is a no-op by definition — say so and read everything."""
    from sqlalchemy import text as T
    try:
        with session.begin_nested():
            n = session.execute(T("""
                SELECT count(*) FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'bittensor_episode_outcomes'
                  AND column_name = 'censored_reason'
            """)).scalar() or 0
        return n == 1
    except Exception as err:  # noqa: BLE001
        print(f"[settle-day] censoring column probe failed ({err}) — "
              f"reading all outcome rows", flush=True)
        return False


def obi_outcomes_provider(day: date) -> list[SettledHorizon]:
    """All measured rows settle-dated on or before `day`, post-cutover.

    Reference implementation for the validator/ops host — requires the
    OBI repo importable (same pattern as scripts/run_shadow_day_bd.py);
    the module itself stays importable without it. Values arrive as delta
    fractions, possibly saturated at ±9999.999999 (the outcome writer's
    numeric(10,6) clamp) — passed through unchanged; the per-entry scale
    normalisation keeps saturated actuals from distorting neighbours.

    finalized_on is COMPUTED from the settle clock (action_window_end +
    1 + horizon + 7), never taken from measured_at. Rows measured before
    SETTLE_CLOCK_CUTOVER_UTC (the old 17-day launch clock — values not
    final when written) are excluded, loudly.

    EFFICIENCY TRUTH. Under v1 this is always cpa_delta_pct. Under v2 it is
    the account's own goal metric per resolve_goal_basis — cpa_delta_pct or
    conversion_value_delta_pct — which is Rob's 2026-07-31 ruling. The flag
    gates the truth basis and the formula together on purpose: both are
    miner-facing and ship with one amendment, so there is one announcement
    rather than two silent changes.

    WHAT THE FLAG DOES NOT GUARANTEE. It does not make "scored on a basis that
    was never published" impossible, and the previous version of this paragraph
    said it did. Two gaps: (1) the pre-freeze population below is scored on a
    RECOMPUTED basis while the payload publishes nothing for a NULL frozen
    column, so those episodes are scored on a basis no miner ever saw; (2) this
    flag is read in the validator process, while the one that turns the payload
    disclosure on (goal_basis_service.publish_goal_basis_enabled) is read in
    the OBI web process on another host — setting it here does not set it
    there. The pairing is a deployment convention, not an interlock.

    FREEZE AT REVEAL — shipped, with a residual. OBI now resolves the basis at
    REVEAL (the reservation UPDATE in daily_basket_service / release_service)
    and persists it on bittensor_episode_candidates.goal_basis /
    .goal_basis_guarded; app/services/bittensor/goal_basis_service.py is the
    authority and mirrors resolve_goal_basis byte-for-byte. This reader prefers
    the frozen value and never recomputes when it is present, so accountgoals
    edits, account_hierarchy_assignment retags and late-settling baselines can
    no longer move an episode's basis after a miner has seen it.

    THE RESIDUAL, stated rather than buried: episodes revealed BEFORE the
    freeze shipped have goal_basis NULL — 4,377 already-reserved candidates,
    2,041 of them in BD- baskets. They are deliberately not backfilled, since
    writing today's resolution onto them would assert a freeze that never
    happened. They fall back to resolve_goal_basis here, exactly as before, and
    the summary below reports frozen vs recomputed with denominators so the
    residual shrinks visibly as the pre-freeze population settles out.

    The frozen columns are read defensively (see _has_frozen_basis_column): a
    validator host whose OBI database has not yet had migration
    20260801_btc_goal_basis applied recomputes everything and says so, instead
    of crashing on an undefined column.
    """
    import sys
    sys.path.insert(0, "/Users/macbookm1/Documents/Projects/obi")
    from app.models import get_session
    from sqlalchemy import text as T

    goal_aware = settle_scoring_v2_enabled()

    with get_session() as s:
        frozen_cols = ("c.goal_basis AS frozen_basis, "
                       "c.goal_basis_guarded AS frozen_guarded"
                       if _has_frozen_basis_column(s)
                       else "NULL::text AS frozen_basis, "
                            "NULL::boolean AS frozen_guarded")
        # Attrition censoring (Rob 2026-08-04): censored horizons are DROPPED
        # from scoring — never a zero. Counted first and printed, because a
        # silently shrinking scoreable set is exactly the failure shape this
        # codebase keeps paying for.
        has_censoring = _has_censoring_column(s)
        censor_filter = ("AND o.censored_reason IS NULL" if has_censoring else "")
        if has_censoring:
            censored_counts = s.execute(T("""
                SELECT censored_reason, count(*) FROM bittensor_episode_outcomes
                WHERE censored_reason IS NOT NULL GROUP BY 1
            """)).fetchall()
            if censored_counts:
                print("[settle-day] censored horizons excluded from scoring: "
                      + ", ".join(f"{r[0]}={r[1]}" for r in censored_counts),
                      flush=True)
        else:
            print("[settle-day] censoring migration not applied on this DB — "
                  "no censor filter (no rows can be censored yet)", flush=True)
        rows = s.execute(T(f"""
            WITH ep AS (
                SELECT o.episode_candidate_id      AS episode_id,
                       o.horizon_days              AS horizon_days,
                       o.cost_delta_pct            AS cost_delta,
                       o.conversions_delta_pct     AS conv_delta,
                       o.cpa_delta_pct             AS cpa_delta,
                       o.conversion_value_delta_pct AS cv_delta,
                       COALESCE(o.baseline_avg_conv_value_micros, 0) AS b_cv,
                       c.action_window_end         AS window_end,
                       {frozen_cols},
                       r.google_ads_account_id     AS gads_id,
                       r.customer_id               AS customer_id
                FROM bittensor_episode_outcomes o
                JOIN bittensor_episode_candidates c ON c.id = o.episode_candidate_id
                LEFT JOIN bittensor_account_registry r ON r.id = c.bittensor_account_id
                WHERE o.measured_at >= :cutover
                  {censor_filter}
                  AND (c.action_window_end::date
                       + make_interval(days => 1 + o.horizon_days + :settle)) <= :day
            )
            SELECT ep.*,
                -- CONFIGURED goal (Rob: "account configured ... goal")
                (SELECT g.metric_type FROM accountgoals g
                  WHERE g.googleadsaccountid = ep.gads_id
                    AND g.is_active = true AND g.goal_kind = 'main'
                  ORDER BY g.priority DESC LIMIT 1) AS goal_metric_type,
                -- IMPLIED goal: account taxonomy root. lineage->>0 is the same
                -- accessor jayesh's verified J3 map uses, so this code and the
                -- measured ~7% coverage describe the same population. Ordering
                -- is explicit: an unordered LIMIT 1 under multiple assignments
                -- was an audit finding on the OBI side.
                (SELECT aha.lineage->>0 FROM account_hierarchy_assignment aha
                  WHERE aha.customer_id = ep.customer_id
                  ORDER BY aha.updated_at DESC NULLS LAST, aha.id DESC
                  LIMIT 1) AS taxonomy_root
            FROM ep
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
    basis_counts = {"cpa": 0, "conversion_value": 0}
    guarded = 0
    frozen_n = 0
    recomputed_n = 0
    for r in rows:
        m = r._mapping
        we = m["window_end"]
        we = we.date() if hasattr(we, "date") else we

        if goal_aware:
            basis, guard_hit, was_frozen = basis_for_row(
                m["frozen_basis"], m["frozen_guarded"],
                m["goal_metric_type"], m["taxonomy_root"], m["b_cv"])
            guarded += 1 if guard_hit else 0
            frozen_n += 1 if was_frozen else 0
            recomputed_n += 0 if was_frozen else 1
        else:
            basis, guard_hit = "cpa", False

        efficiency = (m["cv_delta"] if basis == "conversion_value"
                      else m["cpa_delta"])
        # .get, not [basis]: the frozen column is written by another repo, and
        # an unrecognised value must show up in the summary rather than take
        # the validator down with a KeyError.
        basis_counts[basis] = basis_counts.get(basis, 0) + 1

        out.append(SettledHorizon(
            episode_id=str(m["episode_id"]),
            horizon_days=int(m["horizon_days"]),
            cost_delta_pct=float(m["cost_delta"] or 0),
            conversions_delta_pct=float(m["conv_delta"] or 0),
            efficiency_delta_pct=float(efficiency or 0),
            finalized_on=settle_date(we, int(m["horizon_days"])),
            goal_basis=basis,
        ))

    # Fail loud, with denominators — a coverage number without its denominator
    # is exactly what the 2026-08-01 bus rule forbids, and these two lines are
    # the ones that end up quoted in the amendment.
    total = len(out)
    if goal_aware and total:
        value_n = basis_counts["conversion_value"]
        print(f"[settle-day] goal basis: {value_n}/{total} "
              f"({100.0 * value_n / total:.1f}%) conversion_value, "
              f"{basis_counts['cpa']}/{total} cpa; "
              f"zero-baseline guard moved {guarded} to cpa", flush=True)
        # Frozen vs recomputed, with denominators. recomputed > 0 is not an
        # error — it is the pre-freeze population (episodes revealed before
        # 20260801_btc_goal_basis) still being scored on a basis derived after
        # the fact. It should fall to 0 as those settle out; if it climbs, the
        # freeze has stopped writing on one of the reveal paths.
        print(f"[settle-day] basis provenance: {frozen_n}/{total} "
              f"({100.0 * frozen_n / total:.1f}%) frozen at reveal, "
              f"{recomputed_n}/{total} recomputed at settle "
              f"(pre-freeze episodes — no frozen basis exists for them)",
              flush=True)
    elif total:
        print(f"[settle-day] goal basis: cpa for all {total} rows "
              f"(SN21_SETTLE_SCORING_V2 off)", flush=True)
    return out
