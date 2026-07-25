"""M3 — shadow mode: models run daily against live baskets while the legacy
flow still pays. Launch day becomes a switch of which standings drive
weights, not a big-bang (transition plan §4, Phase M3).

Storage: append-only JSONL per day under a shadow ledger dir, each day's
file finalized with a rail-attested document (hash-chained via the D11
rail) — auditability without a DB migration, trivially migrated later.

Pure orchestration core: I/O boundaries injected (basket provider, model
registry, runner, clock strings). The daily entrypoint stitches:
  basket episodes -> run each admitted model -> record predictions ->
  (later, when horizons settle) score -> D13 shadow standings.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Callable, Iterable, Optional

from hope.backtest.container_runner import RunResult
from hope.publication.rail import attest, build_document, document_sha256
from hope.scoring.episode_average import ScoredEpisode, standing


@dataclass(frozen=True)
class ShadowModel:
    hotkey: str
    image_digest: str          # what runs (docker mode) …
    admitted_at: str           # ISO date of gate pass


def shadow_dir(root: str, day: str) -> str:
    return os.path.join(root, "shadow", day)


def record_day(root: str, day: str, model: ShadowModel, result: RunResult) -> str:
    """Append one model's day-run to the shadow ledger. Returns the path."""
    d = shadow_dir(root, day)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{model.hotkey}.jsonl")
    with open(path, "a") as f:
        f.write(json.dumps({
            "day": day, "hotkey": model.hotkey, "image_digest": model.image_digest,
            "ok": result.ok, "error": result.error,
            "episodes_in": result.episodes_in, "predictions_out": result.predictions_out,
            "predictions": result.predictions,
        }, default=str) + "\n")
    return path


def finalize_day(root: str, day: str, generated_at: str,
                 private_key=None, prev_sha: Optional[str] = None) -> dict:
    """Summarise + (optionally) attest the day's shadow ledger via the rail."""
    d = shadow_dir(root, day)
    per_model = {}
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".jsonl"):
                continue
            hotkey = fn[:-6]
            lines = [json.loads(l) for l in open(os.path.join(d, fn)) if l.strip()]
            last = lines[-1] if lines else {}
            per_model[hotkey] = {
                "ok": last.get("ok"), "episodes_in": last.get("episodes_in"),
                "predictions_out": last.get("predictions_out"),
                "image_digest": last.get("image_digest"),
            }
    doc = build_document("sn21-shadow-daily", day,
                         {"models": per_model, "model_count": len(per_model)},
                         generated_at, prev_sha)
    out = {"document": doc, "sha256": document_sha256(doc)}
    if private_key is not None:
        att = attest(doc, private_key)
        out["signature_hex"] = att.signature_hex
        out["public_key_hex"] = att.public_key_hex
    return out


def run_shadow_day(day: str, episodes: list[dict], models: Iterable[ShadowModel],
                   runner: Callable[[ShadowModel, list[dict]], RunResult],
                   root: str) -> dict:
    """Execute every admitted model against the day's basket; ledger each."""
    summary = {}
    for m in models:
        res = runner(m, episodes)
        record_day(root, day, m, res)
        summary[m.hotkey] = {"ok": res.ok, "predictions": res.predictions_out,
                             "error": res.error}
    return {"day": day, "episodes": len(episodes), "models_run": len(summary),
            "results": summary}


def cutover_ready(shadow_entries: dict[str, list[ScoredEpisode]],
                  as_of, min_scored_days: int = 7) -> dict:
    """The M3->M4 gate: which models have >= N distinct scored days in shadow.
    (Khurram's decision: >=7 scored days before weights switch.)"""
    verdicts = {}
    for hotkey, eps in shadow_entries.items():
        days = {e.scored_on for e in eps}
        st = standing(eps, as_of)
        verdicts[hotkey] = {
            "scored_days": len(days),
            "ready": len(days) >= min_scored_days,
            "standing": st["average"],
            "placement_eligible": st["placement_eligible"],
        }
    ready = sum(1 for v in verdicts.values() if v["ready"])
    return {"models": verdicts, "ready_count": ready,
            "cutover_possible": ready >= 1}
