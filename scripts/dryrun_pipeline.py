"""Complete DRY RUN of the daily pipeline against REAL committed models.

Exercises the whole submit -> run -> score path end to end on real miner
images, and WRITES NOTHING — no ledger, no chain, no receipts. It reads chain
commitments, downloads the PUBLIC training bundle as the scoring corpus, pulls
and executes each model in the namespace sandbox, and scores the predictions
with the production code (the admission gate AND the daily settle scorer
score_entry_v2). Every output goes to stdout; nothing is persisted.

    python3 -m scripts.dryrun_pipeline [--models N] [--episodes M]
                                       [--per-repo] [--no-determinism]

WHY THE PUBLIC BUNDLE AS CORPUS: it carries real episodes with real settled
outcomes and was already published to miners, so using it leaks nothing. For a
dry run of the PIPELINE MECHANICS — does a real image pull, run under
isolation, and produce predictions the scorer can grade — it is exactly right.
It is not a held-out set (miners have it), so the admission verdicts here are a
mechanics check, not a real gate decision. Stated plainly in the report.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from hope.backtest.execution_mode import basket_runner, executor_mode  # noqa: E402
from hope.backtest.gate import (  # noqa: E402
    METRICS,
    OutcomeRow,
    admission_verdict,
    corpus_spread,
    gate_score,
    naive_baseline_prediction,
)
from hope.backtest.gate_service import runner_predictions_to_gate_keys  # noqa: E402
from hope.scoring.settle_day_flow import (  # noqa: E402
    GOAL_METRIC,
    entry_components_v2,
    score_entry_v2,
)

BUNDLE_URL = ("https://github.com/ippcteam/SN21-adtao/releases/download/"
              "training-bundle-2026-08/SN21_training_bundle.jsonl")
# efficiency in the scorer maps to CPA in the outcome/label tables.
EFFICIENCY_LABEL = "cpa_delta_pct"


def log(msg):
    print(msg, flush=True)


def fetch_bundle(workdir: str) -> str:
    path = os.path.join(workdir, "training_bundle.jsonl")
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        log(f"[corpus] bundle already present: {path}")
        return path
    log(f"[corpus] downloading public training bundle -> {path}")
    os.makedirs(workdir, exist_ok=True)
    urllib.request.urlretrieve(BUNDLE_URL, path)
    log(f"[corpus] downloaded {os.path.getsize(path):,} bytes")
    return path


def build_corpus(bundle_path: str, limit: int):
    """(episodes, outcomes, actual_by_key) from the public bundle.

    episodes: payloads in the live contract shape (episode_id at top level).
    outcomes: gate OutcomeRow list.
    actual_by_key: {(episode_id, horizon:int): {metric: float}} for the
                   per-entry settle scorer demonstration.
    """
    episodes, outcomes, actual_by_key = [], [], {}
    with open(bundle_path) as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if "_manifest" in rec or "input" not in rec:
                continue
            eid = str(rec.get("episode_id"))
            payload = {"episode_id": eid}
            payload.update(rec["input"])
            labels = rec.get("labels") or {}
            got_label = False
            for horizon, vals in labels.items():
                if not isinstance(vals, dict) or vals.get("cost_delta_pct") is None:
                    continue
                try:
                    h = int(horizon)
                except (TypeError, ValueError):
                    continue
                actual = {
                    "cost_delta_pct": float(vals["cost_delta_pct"]),
                    "conversions_delta_pct": float(vals.get("conversions_delta_pct") or 0),
                    "efficiency_delta_pct": float(vals.get(EFFICIENCY_LABEL) or 0),
                }
                outcomes.append(OutcomeRow(
                    episode_id=eid, horizon_days=h,
                    cost_delta_pct=actual["cost_delta_pct"],
                    conversions_delta_pct=actual["conversions_delta_pct"],
                    efficiency_delta_pct=actual["efficiency_delta_pct"]))
                actual_by_key[(eid, h)] = actual
                got_label = True
            if got_label:
                episodes.append(payload)
            if len(episodes) >= limit:
                break
    return episodes, outcomes, actual_by_key


def pick_models(per_repo: bool, limit: int):
    """Real committed (uid, repo, digest) from chain — diverse across repos so
    the dry run exercises multiple registries and builds, not 10 rebuilds of
    one image."""
    import bittensor as bt

    from hope.backtest.chain_commitments import bulk_model_commitments
    from hope.backtest.model_registry import parse_model_commitment

    st = bt.Subtensor(network=os.environ.get("SN21_NETWORK", "finney"))
    netuid = int(os.environ.get("SN21_NETUID", "21"))
    mg = st.metagraph(netuid=netuid)
    commits = bulk_model_commitments(st, netuid, list(mg.hotkeys))

    hk2uid = {hk: uid for uid, hk in enumerate(mg.hotkeys)}
    parsed = []
    for hk, (block, raw) in commits.items():
        p = parse_model_commitment(raw)
        if p and p.get("image_ref"):
            parsed.append((hk2uid.get(hk, "?"), p["image_ref"], p["digest"], block))
    parsed.sort(key=lambda r: r[3])   # earliest first

    if per_repo:
        seen, out = set(), []
        for uid, ref, dig, _b in parsed:
            if ref in seen:
                continue
            seen.add(ref)
            out.append((uid, ref, dig))
            if len(out) >= limit:
                break
        return out
    return [(uid, ref, dig) for uid, ref, dig, _b in parsed[:limit]]


def score_with_settle(predictions, actual_by_key):
    """Grade the model's predictions with the PRODUCTION daily scorer
    (score_entry_v2). Returns (scored_entries, component sample)."""
    scored = []
    sample = None
    for eid, horizons in predictions.items():
        for horizon, trio in horizons.items():
            try:
                key = (str(eid), int(horizon))
            except (TypeError, ValueError):
                continue
            actual = actual_by_key.get(key)
            if actual is None:
                continue
            pred = {m: trio.get(m) for m in METRICS if isinstance(trio.get(m), dict)}
            s = score_entry_v2(pred, actual)
            scored.append(s)
            if sample is None and pred:
                sample = {"episode": key[0][:12], "horizon": key[1],
                          "components": entry_components_v2(pred, actual),
                          "score": s}
    return scored, sample


def run_one(uid, ref, dig, corpus, run_basket, do_determinism):
    episodes, outcomes, actual_by_key = corpus
    pinned = f"{ref}@{dig}"
    rec = {"uid": uid, "repo": ref, "digest": dig[:19] + "…"}
    log(f"\n[model uid {uid}] {ref}")

    t0 = time.time()
    run = run_basket(pinned, episodes)
    rec["run_seconds"] = round(time.time() - t0, 1)
    rec["ok"] = run.ok
    rec["episodes_in"] = run.episodes_in
    rec["predictions_out"] = run.predictions_out
    rec["error"] = run.error
    if not run.ok or not run.predictions_out:
        log(f"    RAN ok={run.ok} preds={run.predictions_out} "
            f"in {rec['run_seconds']}s  err={str(run.error)[:150]}")
        return rec
    rec["coverage"] = round(run.predictions_out / max(1, run.episodes_in), 3)
    log(f"    ran: {run.predictions_out}/{run.episodes_in} predictions "
        f"in {rec['run_seconds']}s  coverage={rec['coverage']}")

    # ---- gate score vs the naive baseline (admission mechanics) ----
    preds_keyed = runner_predictions_to_gate_keys(run.predictions)
    spread = corpus_spread(outcomes)
    base = gate_score(outcomes, {
        (o.episode_id, o.horizon_days): naive_baseline_prediction(spread)
        for o in outcomes})
    model = gate_score(outcomes, preds_keyed)
    if model is not None and base is not None:
        verdict = admission_verdict(model, base)
        rec["gate"] = {"model_score": round(model.get("gate_score", 0), 5),
                       "baseline_score": round(base.get("gate_score", 0), 5),
                       "covered": model.get("covered"),
                       "beats_baseline": bool(verdict.get("admitted"))}
        log(f"    gate: model={rec['gate']['model_score']} "
            f"baseline={rec['gate']['baseline_score']} "
            f"beats_baseline={rec['gate']['beats_baseline']}")

    # ---- production daily scorer (score_entry_v2) on the same predictions ----
    scored, sample = score_with_settle(run.predictions, actual_by_key)
    if scored:
        rec["settle"] = {"entries_scored": len(scored),
                         "mean_score": round(sum(scored) / len(scored), 5)}
        log(f"    settle scorer: {len(scored)} entries, "
            f"mean {rec['settle']['mean_score']}  (formula v2 .5/.1/.15/.15)")
        if sample:
            log(f"    sample: ep {sample['episode']} h{sample['horizon']} "
                f"-> {sample['components']} = {sample['score']}")

    # ---- determinism: run a small sample again, compare byte-for-byte ----
    if do_determinism:
        sample_eps = episodes[:10]
        again = run_basket(pinned, sample_eps)
        mism = 0
        for eid, first in (run.predictions or {}).items():
            if eid in (again.predictions or {}):
                if again.predictions[eid] != first:
                    mism += 1
        rec["determinism"] = {"rechecked": len(again.predictions or {}),
                              "mismatches": mism,
                              "deterministic": mism == 0}
        log(f"    determinism: {mism} mismatch(es) over "
            f"{len(again.predictions or {})} rechecked "
            f"-> {'OK' if mism == 0 else 'NON-DETERMINISTIC'}")
    return rec


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--models", type=int, default=5)
    p.add_argument("--episodes", type=int, default=40)
    p.add_argument("--per-repo", action="store_true", default=True)
    p.add_argument("--no-determinism", action="store_true")
    args = p.parse_args()

    workdir = os.environ.get("SN21_EXECUTOR_WORKDIR", "/tmp/executor")
    os.makedirs(workdir, exist_ok=True)

    log("===DRYRUN-START===")
    log(f"[env] executor mode: {executor_mode()}")
    if executor_mode() != "sandbox":
        log("[env] WARNING not in sandbox mode — set SN21_EXECUTOR_MODE=sandbox")

    bundle = fetch_bundle(workdir)
    corpus = build_corpus(bundle, args.episodes)
    episodes, outcomes, _ = corpus
    log(f"[corpus] {len(episodes)} episodes, {len(outcomes)} outcome rows "
        f"(PUBLIC bundle — mechanics check, not a held-out gate)")

    models = pick_models(args.per_repo, args.models)
    log(f"[chain] {len(models)} real committed models selected "
        f"(one per repo, earliest first)")

    run_basket = basket_runner()
    results = []
    for uid, ref, dig in models:
        try:
            results.append(run_one(uid, ref, dig, corpus, run_basket,
                                   not args.no_determinism))
        except Exception as exc:   # noqa: BLE001 - one bad model must not stop the sweep
            log(f"    ERROR {type(exc).__name__}: {exc}")
            results.append({"uid": uid, "repo": ref, "error": str(exc)})

    # ---- summary ----
    log("\n===DRYRUN-SUMMARY===")
    ran = [r for r in results if r.get("ok") and r.get("predictions_out")]
    log(f"  models tried        : {len(results)}")
    log(f"  executed w/ preds   : {len(ran)}")
    log(f"  beat naive baseline : "
        f"{sum(1 for r in ran if r.get('gate', {}).get('beats_baseline'))}")
    log(f"  deterministic       : "
        f"{sum(1 for r in ran if r.get('determinism', {}).get('deterministic'))}"
        f"/{sum(1 for r in ran if 'determinism' in r)}")
    for r in results:
        tag = "OK " if r.get("ok") and r.get("predictions_out") else "-- "
        g = r.get("gate", {})
        s = r.get("settle", {})
        log(f"  {tag}uid {str(r.get('uid')):<4} cov="
            f"{r.get('coverage', '-')} gate_model={g.get('model_score', '-')} "
            f"settle_mean={s.get('mean_score', '-')} "
            f"{r.get('repo', '')[:40]}")
    log("===DRYRUN-END===  (nothing was written — pure dry run)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
