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


# How many episodes the determinism re-run covers. Small on purpose: this is
# one extra container start per NEW digest, and a model that varies its output
# will almost always vary it everywhere rather than on one hidden row.
DETERMINISM_SAMPLE_EPISODES = 25


def _default_runner(image_digest, episodes, timeout_s):
    """The docker runner, ignoring the extra timeout kwarg the sandbox mode
    does not take. Kept as the default so every existing caller is unchanged."""
    return run_basket_docker(image_digest, episodes, timeout_s=timeout_s)


def _nondeterminism_detail(image_digest, episodes, first_predictions,
                           sample_size, timeout_s, runner=None):
    """None when the re-run agrees, else a short description of the first
    disagreement.

    Compares only episodes present in BOTH runs. A model that abstains on an
    episode is allowed to abstain; what it may not do is give two different
    answers to the same question.
    """
    sample = episodes[:sample_size]
    if not sample:
        return None

    run = runner or _default_runner
    second = run(image_digest, sample, timeout_s)
    if not second.ok:
        # A re-run that will not start is a liveness problem, and liveness is
        # judged elsewhere. Not evidence of nondeterminism.
        return None

    for episode_id, again in (second.predictions or {}).items():
        before = (first_predictions or {}).get(episode_id)
        if before is None or again is None:
            continue
        if before != again:
            return (f"episode {episode_id} answered differently on a re-run "
                    f"of the same input")
    return None


def gate_submission(image_digest: str,
                    episodes: list[dict],
                    outcomes: list[OutcomeRow],
                    generated_at: str,
                    hotkey: str = "",
                    prev_verdict_sha: str | None = None,
                    private_key=None,
                    timeout_s: int = 15 * 60,
                    determinism_sample: int = DETERMINISM_SAMPLE_EPISODES,
                    runner=None) -> dict:
    """Run the full admission: sandbox -> gate -> attested verdict document.

    Includes a DETERMINISM check, because the subnet publishes that a rerun
    reproduces the score. A model answering differently on identical input
    makes that false through no fault of the verifier, and makes two
    validators disagree about what a miner predicted. The spec asked for
    determinism and nothing tested it, so "strongly recommended" was as far as
    it went.

    The check is one extra container run over a small sample, compared field
    by field against the first run's answers for the same episodes.

    `runner(image, episodes, timeout_s) -> RunResult` selects the executor.
    Default docker, so every existing caller is unchanged; the Render worker
    passes the namespace-sandbox runner.
    """
    run_fn = runner or _default_runner
    run = run_fn(image_digest, episodes, timeout_s)
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
            nondeterministic = _nondeterminism_detail(
                image_digest, episodes, run.predictions, determinism_sample,
                timeout_s, runner=run_fn)
            if nondeterministic:
                # Refused, not merely noted: an admitted nondeterministic model
                # would break every later verification of every day it ran.
                verdict = {"admitted": False, "reason": "nondeterministic",
                           "detail": nondeterministic,
                           "episodes_in": run.episodes_in,
                           "predictions_out": run.predictions_out}
            else:
                verdict.update({
                    "reason": ("beats_baseline" if verdict["admitted"]
                               else "below_baseline_or_coverage"),
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
