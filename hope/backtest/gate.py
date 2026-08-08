"""M1 — the backtest gate: admission = beat the naive baseline.

Gate metric (published, self-contained, deterministic):
    gate_score = 0.7 * quantile_score + 0.3 * direction_accuracy
- quantile_score: 1 - normalised mean pinball loss over (p10,p50,p90) for the
  three delta metrics, per settled horizon. Pinball loss is the canonical
  proper scoring rule for quantiles; lower loss -> higher score.
- direction_accuracy: fraction of (episode,horizon,metric) where sign(p50)
  matches sign(actual).

The naive baseline (persistence): p50 = 0 delta for every metric, p10/p90 =
+/- the corpus median absolute delta (calibrated spread). A model earns
admission by beating this on the SAME corpus — objective and hard to game.
Pure module: corpus and predictions in, verdict out.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

METRICS = ("cost_delta_pct", "conversions_delta_pct", "efficiency_delta_pct")
QUANTILES = ((0.1, "p10"), (0.5, "p50"), (0.9, "p90"))


@dataclass(frozen=True)
class OutcomeRow:
    """One settled (episode, horizon) truth from the corpus."""
    episode_id: str
    horizon_days: int
    cost_delta_pct: float
    conversions_delta_pct: float
    efficiency_delta_pct: float


def pinball(actual: float, pred: float, q: float) -> float:
    d = actual - pred
    return max(q * d, (q - 1) * d)


def median(xs: list[float]) -> float:
    s = sorted(xs)
    n = len(s)
    if n == 0:
        return 0.0
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2


# How much better than persistence a model must be to be admitted.
#
# Ruled 2026-08-08: "adjust the baseline so the starter model doesn't qualify."
#
# The published rules told miners the reference model "cannot outrank
# anything, since admission requires beating the baseline it defines". That
# was not true in the code: the baseline was plain persistence — predict no
# change — and the reference model is a real heuristic that beats it easily.
# So reference-runners WERE admissible, and because a group containing the
# reference is exempt from the one-payer rule, N hotkeys running the published
# starter model could each hold an earning seat for one model.
#
# Raising the bar closes that at the source and changes no published rule: it
# makes an existing published statement true. Starting from the reference and
# improving on it is exactly what "it is how everyone starts" was always meant
# to mean.
ADMISSION_MARGIN_OVER_BASELINE = 0.05


def naive_baseline_prediction(spread: dict[str, float]) -> dict:
    """Persistence: zero-change medians, corpus-calibrated symmetric spread.

    This is the FLOOR the margin is applied to, not the admission bar itself.
    See ADMISSION_MARGIN_OVER_BASELINE and admission_verdict.
    """
    return {m: {"p10": -spread[m], "p50": 0.0, "p90": spread[m]} for m in METRICS}


def corpus_spread(outcomes: Iterable[OutcomeRow]) -> dict[str, float]:
    """Median absolute delta per metric — the baseline's calibrated spread."""
    vals: dict[str, list[float]] = {m: [] for m in METRICS}
    for o in outcomes:
        for m in METRICS:
            vals[m].append(abs(getattr(o, m)))
    return {m: (median(v) if v else 0.1) for m, v in vals.items()}


def gate_score(outcomes: list[OutcomeRow],
               predictions: dict[tuple[str, int], dict]) -> dict | None:
    """Score a prediction set against settled outcomes.

    predictions: {(episode_id, horizon): {metric: {p10,p50,p90}}}
    Returns {gate_score, quantile_score, direction_accuracy, covered} or
    None when nothing overlaps (no verdict from no evidence).
    """
    losses: list[float] = []
    scales: list[float] = []
    dir_hits = 0
    dir_total = 0
    covered = 0
    for o in outcomes:
        p = predictions.get((o.episode_id, o.horizon_days))
        if not p:
            continue
        covered += 1
        for m in METRICS:
            actual = getattr(o, m)
            trio = p.get(m)
            if not trio:
                continue
            for q, key in QUANTILES:
                losses.append(pinball(actual, float(trio[key]), q))
            scales.append(abs(actual))
            # Direction: every nonzero actual is a directional question.
            # A zero p50 on a nonzero actual is a MISS — otherwise the naive
            # zero-change baseline scores perfect direction by abstention
            # (caught on the real W28-W30 corpus: baseline dir_acc came back
            # 1.0 under the old skip rule). Zero actuals carry no direction.
            if actual != 0:
                dir_total += 1
                p50 = float(trio["p50"])
                if p50 != 0 and (actual > 0) == (p50 > 0):
                    dir_hits += 1
    if covered == 0 or not losses:
        return None
    scale = max(sum(scales) / len(scales), 1e-9)
    mean_loss = sum(losses) / len(losses)
    quantile_score = max(0.0, 1.0 - mean_loss / scale)
    direction_accuracy = (dir_hits / dir_total) if dir_total else 0.0
    return {
        "gate_score": round(0.7 * quantile_score + 0.3 * direction_accuracy, 6),
        "quantile_score": round(quantile_score, 6),
        "direction_accuracy": round(direction_accuracy, 6),
        "covered": covered,
    }


def admission_verdict(model_result: dict, baseline_result: dict,
                      min_coverage_ratio: float = 0.9) -> dict:
    """ADMIT iff the model beats the baseline gate_score with adequate coverage."""
    coverage_ok = model_result["covered"] >= baseline_result["covered"] * min_coverage_ratio
    required = baseline_result["gate_score"] * (1.0 + ADMISSION_MARGIN_OVER_BASELINE)
    beats = model_result["gate_score"] > required
    return {
        "admitted": bool(coverage_ok and beats),
        "model_gate_score": model_result["gate_score"],
        "baseline_gate_score": baseline_result["gate_score"],
        "required_gate_score": round(required, 6),
        "margin_required": ADMISSION_MARGIN_OVER_BASELINE,
        "margin": round(model_result["gate_score"] - baseline_result["gate_score"], 6),
        "coverage_ok": coverage_ok,
    }
