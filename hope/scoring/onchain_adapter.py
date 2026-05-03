"""Adapter between the project's `EpochScorer` and the on-chain `ScorerFn`.

The on-chain pipeline wants a function:

    (epoch_id: str, plaintexts: dict[bytes, dict]) -> dict[bytes, int]

where `plaintexts[miner_hotkey]` is the decoded canonical-CBOR prediction
plaintext (one entry per horizon per miner, NOT per episode), and the
return value maps `miner_hotkey` → integer score in micro-units (×1e6).

The on-chain plaintext aggregates per-horizon quantiles for the whole
portfolio of episodes the miner predicted on; per-episode predictions are
not committed on chain to stay under the 768-byte TLE budget. Phase E may
introduce per-episode artifacts; for Phase D, this adapter scores using:

  - For each horizon, a stake-weighted CRPS-like distance between the
    miner's [P10, P50, P90] triple and the validator's aggregated truth
    triple (per-horizon, derived from outcomes).
  - For each horizon, calibration of `goal_miss_probability` and
    `instability_risk` against frequencies in the outcomes.
  - A 0..1 final score per horizon, averaged across horizons, then cast to
    uint micro-units.

The scoring is fully DETERMINISTIC — given (epoch_id, plaintexts,
truth_triples) two independent verifiers compute byte-identical scores.

`scoring_inputs_hash` is the SHA-256 over a canonical-CBOR encoding of
{epoch_id, sorted plaintexts, truth_triples}; this is the same value
embedded in the validator's 9.C.2 `scoring_hash` field. A verifier must
reproduce it byte-for-byte.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Callable

from datetime import datetime, timezone

from hope.commitment.canonical import canonical_cbor_dumps
from hope.commitment.prediction_payload import (
    PROBABILITY_SCALE,
    QUANTILE_SCALE,
)
from hope.protocol.episode import Episode
from hope.protocol.outcomes import Outcome
from hope.protocol.prediction import (
    HorizonPrediction,
    Prediction,
    QuantilePrediction,
)
from hope.scoring.scorer import EpochScorer


@dataclass(frozen=True)
class HorizonTruth:
    """Aggregated outcome triple for one horizon, in deci-percent integers.

    The validator (and verifier) computes these from the off-chain outcome
    blob (Layer 9.A.2 reveal) by averaging cost_delta_pct / conversions /
    efficiency across all episodes in scope. `goal_miss_freq_ppm` and
    `instab_freq_ppm` are observed per-portfolio frequencies of the
    `goal_miss==1` and high-variance flags respectively.

    All numeric fields are integers to avoid float non-determinism.
    """

    horizon: str
    truth_cost_p50_dpct: int     # deci-percent, ×10
    truth_conv_p50_dpct: int
    truth_eff_p50_dpct: int
    goal_miss_freq_ppm: int
    instab_freq_ppm: int


def _crps_quantile_triple(predicted_triple: list[int], truth: int) -> float:
    """Integer CRPS for an empirical CDF defined by three quantiles.

    Treat the prediction as a step CDF F(x) with three steps:
      F(x) = 0           for x < q10
      F(x) = 0.1         for q10 ≤ x < q50
      F(x) = 0.5         for q50 ≤ x < q90
      F(x) = 0.9         for q90 ≤ x  (extrapolated to 1.0 at +∞)

    CRPS(F, y) = ∫ (F(x) - 1{x ≥ y})^2 dx

    For a step function with breakpoints q1 ≤ q2 ≤ q3 and step heights h0=0,
    h1, h2, h3, h4=1, this integrates piecewise. We compute it directly from
    the triple in deci-percent units; result is a non-negative float in
    deci-percent (bigger = worse).

    All inputs are deci-percent integers. Output is raw CRPS in those units.
    """
    q10, q50, q90 = predicted_triple
    if q10 > q50 or q50 > q90:
        # Non-monotone — scoreability would have rejected, but guard anyway.
        return float("inf")

    # Step heights (left-of-breakpoint) for the predicted CDF.
    # Outside [q10, q90] we extrapolate linearly to 0 / 1 over a fixed band of
    # 500 dpct on each side so the integral is finite.
    BAND = 500
    breakpoints = [q10 - BAND, q10, q50, q90, q90 + BAND]
    cdf_values = [0.0, 0.1, 0.5, 0.9, 1.0]

    crps = 0.0
    for i in range(len(breakpoints) - 1):
        x0, x1 = breakpoints[i], breakpoints[i + 1]
        f = cdf_values[i + 1]  # CDF on (x0, x1]
        # ∫ from x0 to x1 of (F(x) - 1{x ≥ y})^2 dx
        if x1 <= truth:
            # 1{x ≥ y} = 0 over the whole segment
            crps += (x1 - x0) * f * f
        elif x0 >= truth:
            # 1{x ≥ y} = 1 over the whole segment
            crps += (x1 - x0) * (f - 1.0) ** 2
        else:
            # truth is inside (x0, x1)
            crps += (truth - x0) * f * f
            crps += (x1 - truth) * (f - 1.0) ** 2
    return crps


def _quantile_score(predicted_triple: list[int], truth_p50: int) -> float:
    """Map CRPS to a [0, 1] score using a saturating shape.

    CRPS = 0 → score 1.0. CRPS ≥ 800 dpct → score 0. Linear interpolation in
    between (the band reflects the sum of the three within-bin CRPS terms
    saturated; chosen so a "reasonable" prediction (P50 within ±20% of
    truth) scores around 0.7.
    """
    d = _crps_quantile_triple(predicted_triple, truth_p50)
    if d <= 0:
        return 1.0
    if d >= 800.0:
        return 0.0
    return 1.0 - (d / 800.0)


def _probability_score(predicted_ppm: int, observed_ppm: int) -> float:
    """Brier-like score for binary probability calibration.

    Brier = (p - observed)^2 in [0, 1]; we return 1 - Brier so higher is
    better. Both inputs are uint ppm; we rescale to [0, 1] for the math.
    """
    p = max(0.0, min(1.0, predicted_ppm / PROBABILITY_SCALE))
    y = max(0.0, min(1.0, observed_ppm / PROBABILITY_SCALE))
    return 1.0 - (p - y) ** 2


def score_one_miner(
    plaintext: dict[str, Any],
    truth_by_horizon: dict[str, HorizonTruth],
) -> int:
    """Score a single miner's plaintext against per-horizon truth.

    Returns:
        Integer in [0, 1_000_000] (micro-units). Average horizon score
        × 1e6, rounded half-to-even.
    """
    horizon_scores: list[float] = []
    for h_entry in plaintext.get("horizons", []):
        horizon = h_entry.get("h")
        truth = truth_by_horizon.get(horizon)
        if truth is None:
            # Validator scored a horizon the miner didn't include; treat as 0.
            continue
        # Pull the miner's deci-percent triples and ppm probabilities.
        cost_q = h_entry.get("cost_q") or [0, 0, 0]
        conv_q = h_entry.get("conv_q") or [0, 0, 0]
        eff_q = h_entry.get("eff_q") or [0, 0, 0]
        miss_p = int(h_entry.get("miss_p") or 0)
        instab_p = int(h_entry.get("instab_p") or 0)

        q_score = (
            _quantile_score(cost_q, truth.truth_cost_p50_dpct)
            + _quantile_score(conv_q, truth.truth_conv_p50_dpct)
            + _quantile_score(eff_q, truth.truth_eff_p50_dpct)
        ) / 3.0
        p_score = (
            _probability_score(miss_p, truth.goal_miss_freq_ppm)
            + _probability_score(instab_p, truth.instab_freq_ppm)
        ) / 2.0
        horizon_scores.append(0.7 * q_score + 0.3 * p_score)

    if not horizon_scores:
        return 0
    avg = sum(horizon_scores) / len(horizon_scores)
    return int(round(max(0.0, min(1.0, avg)) * 1_000_000))


def make_scorer(
    truth_by_horizon: dict[str, HorizonTruth],
) -> Callable[[str, dict[bytes, dict[str, Any]]], dict[bytes, int]]:
    """Build a `ScorerFn` closure bound to per-horizon ground truth.

    The closure satisfies the on-chain pipeline's signature:

        (epoch_id, plaintexts) -> {miner_hotkey: score_micro}

    Args:
        truth_by_horizon: per-horizon HorizonTruth derived from outcomes.

    Returns:
        Callable suitable for `run_epoch_scoring(scorer=...)`.
    """
    def scorer(epoch_id: str, plaintexts: dict[bytes, dict[str, Any]]) -> dict[bytes, int]:
        return {hk: score_one_miner(pt, truth_by_horizon) for hk, pt in plaintexts.items()}
    return scorer


def compute_scoring_inputs_hash(
    *,
    epoch_id: str,
    plaintexts: dict[bytes, dict[str, Any]],
    truth_by_horizon: dict[str, HorizonTruth],
) -> bytes:
    """SHA-256 over a canonical-CBOR encoding of all scoring inputs.

    This is the value embedded in 9.C.2 `scoring_hash`. A verifier must
    reproduce it byte-for-byte; any divergence in the validator's claimed
    scoring inputs and the verifier's chain reads is detectable here.

    Layout (canonical CBOR, sorted keys):

        {
          "epoch_id": tstr,
          "plaintexts": {bstr(miner_hotkey): plaintext_dict, …},
          "truth": {tstr(horizon): {field: int, …}, …},
        }
    """
    truth_payload = {
        h: {
            "cost": t.truth_cost_p50_dpct,
            "conv": t.truth_conv_p50_dpct,
            "eff": t.truth_eff_p50_dpct,
            "miss": t.goal_miss_freq_ppm,
            "instab": t.instab_freq_ppm,
        }
        for h, t in truth_by_horizon.items()
    }
    payload = {
        "epoch_id": epoch_id,
        "plaintexts": plaintexts,
        "truth": truth_payload,
    }
    return hashlib.sha256(canonical_cbor_dumps(payload)).digest()


def aggregate_outcomes_to_truth(
    outcomes_per_episode: list[dict[str, Any]],
) -> dict[str, HorizonTruth]:
    """Aggregate Layer 9.A.2 episode outcomes into per-horizon truth triples.

    Args:
        outcomes_per_episode: list of `{horizon: {cost_delta_pct: float, ...,
            goal_miss: 0|1}, ...}` dicts (one per episode), as parsed from
            the HOPE reveal blob.

    Returns:
        Map from horizon → HorizonTruth. Any horizon present in ≥1 episode
        gets aggregated; horizons absent from all episodes are omitted.

    The aggregation uses the median across episodes for cost/conv/eff (so a
    handful of outliers don't dominate) and the mean for goal_miss /
    instability frequencies. Float → integer conversions use round-half-even.
    """
    by_h: dict[str, dict[str, list[float]]] = {}
    for ep in outcomes_per_episode:
        for h, fields in ep.items():
            if not isinstance(fields, dict):
                continue
            slot = by_h.setdefault(h, {
                "cost": [], "conv": [], "eff": [], "miss": [], "instab": [],
            })
            slot["cost"].append(float(fields.get("cost_delta_pct", 0.0)))
            slot["conv"].append(float(fields.get("conversions_delta_pct", 0.0)))
            slot["eff"].append(float(fields.get("efficiency_delta_pct", 0.0)))
            slot["miss"].append(1.0 if fields.get("goal_miss", 0) else 0.0)
            slot["instab"].append(float(fields.get("instability_observed", 0.0)))

    out: dict[str, HorizonTruth] = {}
    for h, slot in by_h.items():
        if not slot["cost"]:
            continue
        out[h] = HorizonTruth(
            horizon=h,
            truth_cost_p50_dpct=int(round(_median(slot["cost"]) * QUANTILE_SCALE)),
            truth_conv_p50_dpct=int(round(_median(slot["conv"]) * QUANTILE_SCALE)),
            truth_eff_p50_dpct=int(round(_median(slot["eff"]) * QUANTILE_SCALE)),
            goal_miss_freq_ppm=int(round(_mean(slot["miss"]) * PROBABILITY_SCALE)),
            instab_freq_ppm=int(round(_mean(slot["instab"]) * PROBABILITY_SCALE)),
        )
    return out


def _bundle_entry_to_horizon_prediction(entry: dict[str, Any]) -> HorizonPrediction:
    """Convert one per-episode bundle entry into a Pydantic HorizonPrediction.

    The bundle stores cost/conv/eff as deci-percent integers and miss/instab
    as ppm uints. The Pydantic schema wants floats in percent and probability
    units, respectively. We reverse the scaling here.
    """
    return HorizonPrediction(
        cost_delta_pct=QuantilePrediction(
            p10=entry["cost_q"][0] / QUANTILE_SCALE,
            p50=entry["cost_q"][1] / QUANTILE_SCALE,
            p90=entry["cost_q"][2] / QUANTILE_SCALE,
        ),
        conversions_delta_pct=QuantilePrediction(
            p10=entry["conv_q"][0] / QUANTILE_SCALE,
            p50=entry["conv_q"][1] / QUANTILE_SCALE,
            p90=entry["conv_q"][2] / QUANTILE_SCALE,
        ),
        efficiency_delta_pct=QuantilePrediction(
            p10=entry["eff_q"][0] / QUANTILE_SCALE,
            p50=entry["eff_q"][1] / QUANTILE_SCALE,
            p90=entry["eff_q"][2] / QUANTILE_SCALE,
        ),
        goal_miss_probability=entry["miss_p"] / PROBABILITY_SCALE,
        instability_risk=entry["instab_p"] / PROBABILITY_SCALE,
    )


def bundle_to_predictions(
    bundle: dict[str, Any],
) -> list[Prediction]:
    """Convert a per-episode bundle (Phase E) into a list of Pydantic Predictions.

    Each Prediction collects ALL horizon entries for a single episode_id
    under the bundle's miner. The result is what `EpochScorer.score_miner`
    expects.
    """
    miner_hotkey: bytes = bundle["miner_hotkey"]
    miner_id = miner_hotkey.hex()

    by_episode: dict[str, dict[str, HorizonPrediction]] = {}
    for entry in bundle["entries"]:
        ep = entry["ep"]
        h = entry["h"]
        by_episode.setdefault(ep, {})[h] = _bundle_entry_to_horizon_prediction(entry)

    submitted_at = datetime.fromtimestamp(0, tz=timezone.utc)
    return [
        Prediction(
            episode_id=ep,
            miner_id=miner_id,
            submitted_at=submitted_at,
            horizons=horizons,
        )
        for ep, horizons in sorted(by_episode.items())
    ]


def wrap_epoch_scorer(
    epoch_scorer: EpochScorer,
    *,
    episodes: list[Episode],
    outcomes: list[Outcome],
    bundles_by_miner: dict[bytes, dict[str, Any]],
) -> Callable[[str, dict[bytes, dict[str, Any]]], dict[bytes, int]]:
    """Build a `ScorerFn` that delegates to `EpochScorer.score_epoch`.

    Args:
        epoch_scorer: a configured EpochScorer.
        episodes: per-episode metadata (same that miners predicted on).
        outcomes: per-episode measured outcomes.
        bundles_by_miner: per-episode prediction bundles, keyed by miner hotkey.
            The aggregated chain plaintext alone doesn't carry per-episode
            granularity; the validator must fetch the bundle alongside AES_ct
            and pass it in here.

    Returns:
        Callable matching the on-chain `ScorerFn` shape. Plaintexts the
        chain pipeline passes are the AGGREGATED ones; this adapter ignores
        them and uses the rich per-episode bundles instead. The aggregated
        plaintext IS still used for inner_sig + scoreability — this adapter
        only handles scoring, after scoreability passes.
    """
    def scorer(epoch_id: str, _aggregated_plaintexts: dict[bytes, dict[str, Any]]) -> dict[bytes, int]:
        # Build the EpochScorer-compatible inputs.
        all_predictions: dict[str, list[Prediction]] = {}
        for miner_hotkey, bundle in bundles_by_miner.items():
            if miner_hotkey not in _aggregated_plaintexts:
                # The bundle came in but the aggregated plaintext was excluded
                # by scoreability — skip scoring this miner.
                continue
            all_predictions[miner_hotkey.hex()] = bundle_to_predictions(bundle)

        miner_score_map = epoch_scorer.score_epoch(
            all_predictions=all_predictions,
            episodes=episodes,
            outcomes=outcomes,
        )

        # Map miner_id (hex) → score_micro (uint), keying back to bytes.
        out: dict[bytes, int] = {}
        for hex_id, miner_score in miner_score_map.items():
            try:
                hk = bytes.fromhex(hex_id)
            except ValueError:
                continue
            score = max(0.0, min(1.0, float(miner_score.final_score)))
            out[hk] = int(round(score * 1_000_000))
        return out

    return scorer


def _median(xs: list[float]) -> float:
    n = len(xs)
    if n == 0:
        return 0.0
    s = sorted(xs)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0
