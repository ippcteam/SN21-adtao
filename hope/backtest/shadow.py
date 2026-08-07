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
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

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


# Day-level marker: proof the subnet ATTEMPTED the day, written even when
# zero models were admitted. Without it an empty/absent day directory is
# ambiguous between "nobody ran" and "we never ran it" — see subnet_ran.
RUN_MARKER = "_run.json"

# Shadow day directories are named by the BASKET day (BD-<day> minus the
# prefix), never by the day the script happened to execute.
_DAY_DIR_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def record_run_marker(root: str, day: str, episodes: int, models: int,
                      generated_at: str | None = None) -> str:
    """Mark that the subnet RAN this shadow day. Written atomically."""
    d = shadow_dir(root, day)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, RUN_MARKER)
    rec = {"day": day, "episodes": episodes, "models": models}
    if generated_at:
        rec["generated_at"] = generated_at
    with open(path + ".tmp", "w") as f:
        json.dump(rec, f)
    os.replace(path + ".tmp", path)
    return path


def subnet_ran(root: str, day: str) -> bool:
    """Did WE execute this shadow day at all?

    True when the day carries a run marker or at least one model file.
    False for a missing/empty day directory — which is a statement about
    the SUBNET, not about any miner: the shadow day is driven by a manual
    script (scripts/run_shadow_day_bd.py, no automated caller), which is
    why 2026-07-30 is absent from the live ledger while 07-27/28/29/31 are
    present. Liveness consumers MUST skip a day that returns False.
    """
    d = shadow_dir(root, day)
    if not os.path.isdir(d):
        return False
    if os.path.exists(os.path.join(d, RUN_MARKER)):
        return True
    return any(fn.endswith(".jsonl") for fn in os.listdir(d))


def shadow_days(root: str) -> list:
    """Every shadow day present on disk that the subnet RAN, ascending.

    The shadow ledger is keyed by BASKET day, and the basket a run covers is
    NOT the day the run happens: scripts/shadow_daily.sh runs `BD-$(date -d
    yesterday)`, so the day written on 2026-07-30 is 2026-07-29. Any consumer
    that wants "the execution facts we have" must ask the ledger which days
    exist rather than assume its own calendar day is one of them (the
    liveness step in validator/daily_loop.py did assume that, and therefore
    observed nothing on every real day).
    """
    d = os.path.join(root, "shadow")
    if not os.path.isdir(d):
        return []
    return sorted(name for name in os.listdir(d)
                  if _DAY_DIR_RE.match(name) and subnet_ran(root, name))


def day_run_status(root: str, day: str) -> dict:
    """hotkey -> (ok, error) for one shadow day: the EXECUTION facts.

    LAST record wins per hotkey (same `lines[-1]` discipline as
    finalize_day and settle_day_flow.load_prediction_index): record_day
    appends, so a failed run followed by a same-day re-run reads as the
    re-run — a recovered day must never become a permanent strike.

    An empty dict means THE SUBNET DID NOT RUN — never "everybody failed".
    Pair it with subnet_ran() before drawing any conclusion from silence.

    A record whose operative line carries NO `ok` field (or a null one) is
    skipped, not reported as a failure. record_day always writes `ok`, but
    other producers of shadow lines (fixtures, prediction-only writers) do
    not, and `bool(last.get("ok"))` turned every such line into ok=False with
    no error string — which the liveness policy reads as an unattributable
    failed run and records against the miner's day. Absence of a status is
    not a status.
    """
    d = shadow_dir(root, day)
    out: dict = {}
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".jsonl"):
            continue
        hotkey = fn[:-6]
        # The operative record is the last line that CARRIES a status, not the
        # last line full stop. Skipping the hotkey whenever the final append
        # happens to lack `ok` would erase a genuine ok:false recorded earlier
        # in the same day — turning a real failed run into "did not run", which
        # is precisely backwards for a liveness policy. Absence of a status is
        # not a status; it is also not an eraser of one.
        last_with_status = None
        with open(os.path.join(d, fn)) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("ok") is not None:
                    last_with_status = rec
        if last_with_status is None:
            continue
        out[hotkey] = (bool(last_with_status["ok"]),
                       last_with_status.get("error"))
    return out



def day_coverage(root: str, day: str) -> dict:
    """hotkey -> (episodes_in, predictions_out) for one shadow day.

    The COVERAGE facts, as `day_run_status` is the execution facts. Same
    last-record-wins discipline for the same reason: a re-run supersedes the
    run it repeated.

    Why coverage and not `ok`: a container that prints rubbish exits cleanly
    and is recorded as a SUCCESSFUL run with zero predictions, because
    _parse_output treats non-JSON as never fatal. So `ok=True` is satisfiable
    by a model that did nothing, and a participation gate reading `ok` would
    pay exactly the free-riding it exists to stop. What was actually delivered
    is the only honest measure.

    A record missing `episodes_in` is skipped rather than counted as zero —
    absence of a measurement is not a measurement of absence.
    """
    d = shadow_dir(root, day)
    out: dict = {}
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".jsonl"):
            continue
        hotkey = fn[:-6]
        last_with_coverage = None
        with open(os.path.join(d, fn)) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                if rec.get("episodes_in") is not None:
                    last_with_coverage = rec
        if last_with_coverage is None:
            continue
        out[hotkey] = (int(last_with_coverage.get("episodes_in") or 0),
                       int(last_with_coverage.get("predictions_out") or 0))
    return out


def finalize_day(root: str, day: str, generated_at: str,
                 private_key=None, prev_sha: str | None = None) -> dict:
    """Summarise + (optionally) attest the day's shadow ledger via the rail."""
    d = shadow_dir(root, day)
    per_model = {}
    if os.path.isdir(d):
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".jsonl"):
                continue
            hotkey = fn[:-6]
            raw = Path(d, fn).read_text().splitlines()
            lines = [json.loads(l) for l in raw if l.strip()]
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
    # Marker last: the day is "run" once every admitted model has been
    # attempted. A zero-model day still marks, so liveness can tell it
    # apart from a day nobody ever launched.
    record_run_marker(root, day, len(episodes), len(summary))
    return {"day": day, "episodes": len(episodes), "models_run": len(summary),
            "results": summary}


def cutover_ready(shadow_entries: dict[str, list[ScoredEpisode]],
                  as_of, min_scored_days: int = 7) -> dict:
    """The M3->M4 gate: which models have >= N distinct scored days in shadow.
    (Operator decision: >=7 scored days before the weights switch.)"""
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
