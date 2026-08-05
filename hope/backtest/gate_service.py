"""Gate service — the admission surface: image digest in, attested verdict out.

Composes M1 (gate) + M2 (runner) + the D11 rail into the single operation a
model submission triggers: run the container against the held-out corpus,
score against the naive baseline, attest the verdict. Every verdict is a
rail document (hash-chained per the 'sn21-admission-verdicts' feed) so the
gate's history is publicly auditable — admissions are provable, rejections
are contestable with evidence.
"""

from __future__ import annotations

from hope.backtest.container_runner import run_basket_docker
from hope.backtest.gate import (
    OutcomeRow,
    admission_verdict,
    corpus_spread,
    gate_score,
    naive_baseline_prediction,
)
from hope.publication.rail import attest, build_document

ADMISSION_FEED = "sn21-admission-verdicts"


def runner_predictions_to_gate_keys(predictions: dict) -> dict:
    """Runner output {episode_id: {horizon: trio-dict}} -> gate keys
    {(episode_id, int(horizon)): metric trios} (drops non-metric extras like
    goal_miss_probability that ride in the same horizons dict)."""
    out = {}
    for eid, horizons in predictions.items():
        for h, payload in horizons.items():
            try:
                key = (eid, int(h))
            except (TypeError, ValueError):
                continue
            out[key] = {k: v for k, v in payload.items() if isinstance(v, dict)}
    return out


def gate_submission(image_digest: str,
                    episodes: list[dict],
                    outcomes: list[OutcomeRow],
                    generated_at: str,
                    hotkey: str = "",
                    prev_verdict_sha: str | None = None,
                    private_key=None,
                    timeout_s: int = 15 * 60) -> dict:
    """Run the full admission: sandbox -> gate -> attested verdict document."""
    run = run_basket_docker(image_digest, episodes, timeout_s=timeout_s)
    if not run.ok:
        verdict = {"admitted": False, "reason": f"run_failed: {run.error}",
                   "episodes_in": run.episodes_in}
    else:
        preds = runner_predictions_to_gate_keys(run.predictions)
        spread = corpus_spread(outcomes)
        base = gate_score(outcomes, {
            (o.episode_id, o.horizon_days): naive_baseline_prediction(spread)
            for o in outcomes})
        model = gate_score(outcomes, preds)
        if model is None:
            verdict = {"admitted": False, "reason": "no_scoreable_predictions",
                       "episodes_in": run.episodes_in,
                       "predictions_out": run.predictions_out}
        else:
            verdict = admission_verdict(model, base)
            verdict.update({
                "reason": "beats_baseline" if verdict["admitted"] else "below_baseline_or_coverage",
                "model_detail": model, "baseline_detail": base,
                "episodes_in": run.episodes_in,
                "predictions_out": run.predictions_out,
            })

    doc = build_document(
        ADMISSION_FEED, generated_at[:10],
        {"image_digest": image_digest, "hotkey": hotkey, "verdict": verdict},
        generated_at, prev_verdict_sha,
    )
    result = {"verdict": verdict, "document": doc}
    if private_key is not None:
        att = attest(doc, private_key)
        result.update({"sha256": att.sha256, "signature_hex": att.signature_hex,
                       "public_key_hex": att.public_key_hex,
                       "anchor_digest": att.anchor_digest})
    return result
