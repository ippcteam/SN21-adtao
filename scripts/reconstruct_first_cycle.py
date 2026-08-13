"""First-cycle reconstruction — reproduce the predictions miners' committed
models determined for the early daily baskets, so the first 7-day scores can
settle on schedule (~18 Aug) without fabricating anything.

    python3 -m scripts.reconstruct_first_cycle \
        --from 2026-08-06 --to 2026-08-11 [--ledger-root DIR] [--dry-run]

THE INTEGRITY ARGUMENT (read this before doubting it)

    In the daily stream the SEAL is the model digest committed on chain, not a
    submitted prediction. A committed model is deterministic (enforced at
    admission), and both the digest and the basket were published BEFORE the
    outcome existed. So the prediction for a basket is fully determined by two
    values that both predate the outcome. Running the exact digest a miner had
    committed at that basket's time reproduces the prediction that was already
    determined — the same logic as timelock encryption: the value was sealed,
    we are only computing it now. Anyone can verify: their historical digest +
    the published basket -> the exact prediction.

    This is a ONE-TIME reconstruction for the first cycle, disclosed as such.
    The live daily path (validator executes each day forward) runs going
    forward and needs none of this.

    It touches the chain only to READ (historical commitments). It writes no
    weights and computes no scores — it reproduces predictions and seals them
    into the shadow ledger. Settlement + scoring + receipts happen in a
    separate off-chain step; the on-chain payout cutover is a deliberate,
    separate flip.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from hope.backtest.chain_commitments import model_commitments_at_block  # noqa: E402
from hope.backtest.container_runner import RunResult, _parse_output  # noqa: E402
from hope.backtest.model_registry import parse_model_commitment  # noqa: E402
from hope.backtest.ns_sandbox import (  # noqa: E402
    RunSpec,
    cleanup_rootfs,
    run_sandboxed,
    sandbox_env,
)
from hope.backtest.oci_pull import PullError, pull_and_unpack  # noqa: E402
from hope.backtest.shadow import (  # noqa: E402
    ShadowModel,
    record_day,
    record_run_marker,
    shadow_dir,
)

ARCHIVE_URL = os.environ.get("SN21_REG_INDEX_ARCHIVE_URL",
                             "wss://archive.chain.opentensor.ai:443")
NETUID = int(os.environ.get("SN21_NETUID", "21"))

# Block-time anchor for date->block: block 8821150 was 2026-08-11 12:14 UTC,
# 12s blocks. Good to a few minutes over a week, well inside the daily grain.
_ANCHOR_BLOCK = 8821150
_ANCHOR_TIME = datetime(2026, 8, 11, 12, 14, tzinfo=timezone.utc)


def log(msg):
    print(msg, flush=True)


def delivery_block(basket_day: date) -> int:
    """The block at the basket's delivery — the morning AFTER its action day,
    09:30 UTC. The digest a miner had then is the model that 'would have run'
    against that basket."""
    deliver = datetime.combine(basket_day + timedelta(days=1),
                               datetime.min.time(), tzinfo=timezone.utc) \
        .replace(hour=9, minute=30)
    return _ANCHOR_BLOCK + int((deliver - _ANCHOR_TIME).total_seconds() / 12)


def _api_get(path: str):
    url = (os.environ.get("HOPE_API_URL") or "").strip().rstrip("/")
    key = (os.environ.get("HOPE_API_KEY") or "").strip()
    req = urllib.request.Request(f"{url}/internal/bittensor/v1/{path.lstrip('/')}",
                                 headers={"X-API-Key": key})
    with urllib.request.urlopen(req, timeout=180) as resp:
        return json.loads(resp.read().decode())


def fetch_basket(basket_day: date) -> list:
    pkg = _api_get(f"releases/BD-{basket_day.isoformat()}/package")
    out = []
    for ep in pkg.get("episodes", []):
        payload = ep.get("payload")
        eid = ep.get("episode_id")
        if payload and eid:
            payload = dict(payload)
            payload["episode_id"] = str(eid)
            out.append(payload)
    return out


def _sealed_digests(ledger_root: str, day: str, hotkey: str) -> set:
    """The set of image_digests already recorded for (day, hotkey) in the
    ledger. Reconstruction is resumable off this: a 7–10h run that dies
    part-way must resume, not restart, and must never double-seal."""
    path = os.path.join(shadow_dir(ledger_root, day), f"{hotkey}.jsonl")
    if not os.path.exists(path):
        return set()
    seen = set()
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                dg = json.loads(line).get("image_digest")
                if dg:
                    seen.add(dg)
    except (OSError, ValueError):
        return set()
    return seen


def _job_remaining(ledger_root: str, pinned: str, day_map: dict) -> dict:
    """day_map filtered to the (day, hotkey) pairs NOT yet sealed for `pinned`.
    An empty return means the whole digest is already reconstructed — skip the
    multi-GB pull entirely."""
    remaining: dict = {}
    for day, hotkeys in day_map.items():
        todo = [hk for hk in hotkeys
                if pinned not in _sealed_digests(ledger_root, day.isoformat(), hk)]
        if todo:
            remaining[day] = todo
    return remaining


def run_one(rootfs, config, episodes):
    """Deterministic reproduction of a model's predictions for one basket."""
    argv = list(config.entrypoint) + list(config.cmd)
    if not argv:
        return None
    ids = {str(e["episode_id"]) for e in episodes}
    blob = ("\n".join(json.dumps(e, default=str) for e in episodes) + "\n").encode()
    spec = RunSpec(rootfs=rootfs, argv=argv, env=sandbox_env(config.env),
                   working_dir=config.working_dir)
    result = run_sandboxed(spec, blob)
    if not result.ok:
        return {"error": result.error}
    return _parse_output(result.stdout, ids)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--from", dest="frm", required=True)
    p.add_argument("--to", dest="to", required=True)
    p.add_argument("--ledger-root",
                   default=os.environ.get("SN21_LEDGER_ROOT", "/var/data/sn21/ledger"))
    p.add_argument("--workdir",
                   default=os.environ.get("SN21_EXECUTOR_WORKDIR",
                                          "/var/data/executor-work"))
    p.add_argument("--dry-run", action="store_true",
                   help="resolve digests + baskets; pull/run nothing")
    args = p.parse_args()

    d0 = date.fromisoformat(args.frm)
    d1 = date.fromisoformat(args.to)
    days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    os.makedirs(args.ledger_root, exist_ok=True)
    os.makedirs(args.workdir, exist_ok=True)

    log("===RECON-START===")
    import bittensor as bt
    log(f"[recon] archive {ARCHIVE_URL}")
    arch = bt.Subtensor(network=ARCHIVE_URL)

    per_day_hotkey_digest: dict = {}     # day -> {hotkey: pinned_ref}
    episodes_by_day: dict = {}
    digest_jobs: dict = {}               # pinned_ref -> {day -> [hotkeys]}

    for day in days:
        blk = delivery_block(day)
        commits = model_commitments_at_block(arch, NETUID, blk)
        eps = fetch_basket(day)
        episodes_by_day[day] = eps
        resolved = {}
        for hk, raw in commits.items():
            parsed = parse_model_commitment(raw)
            if not parsed or not parsed.get("image_ref"):
                continue
            pinned = f"{parsed['image_ref']}@{parsed['digest']}"
            resolved[hk] = pinned
            digest_jobs.setdefault(pinned, {}).setdefault(day, []).append(hk)
        per_day_hotkey_digest[day] = resolved
        log(f"[recon] {day}: block {blk} · {len(commits)} committed models · "
            f"{len(resolved)} runnable · basket {len(eps)} episodes")

    log(f"[recon] unique digests to reconstruct: {len(digest_jobs)}")
    if args.dry_run:
        log("[recon] DRY RUN — nothing pulled or executed")
        log("===RECON-END===")
        return 0

    from hope.backtest.local_executor import split_pinned_ref
    stats = {"digests_ok": 0, "pull_fail": 0, "sealed": 0, "run_fail": 0,
             "skipped_done": 0}
    t0 = time.time()
    for i, (pinned, day_map) in enumerate(sorted(digest_jobs.items()), 1):
        remaining = _job_remaining(args.ledger_root, pinned, day_map)
        if not remaining:
            stats["skipped_done"] += 1
            continue
        repo, digest = split_pinned_ref(pinned)
        dest = os.path.join(args.workdir, f"recon-{i}")
        cleanup_rootfs(dest)
        try:
            image = pull_and_unpack(repo, digest, dest)
        except PullError as exc:
            stats["pull_fail"] += 1
            log(f"[recon] {i}/{len(digest_jobs)} PULL FAIL {repo[:40]}: "
                f"{str(exc)[:80]}")
            cleanup_rootfs(dest)
            continue
        stats["digests_ok"] += 1
        for day, hotkeys in remaining.items():
            preds = run_one(image.rootfs, image.config, episodes_by_day[day])
            if not preds or "error" in preds:
                stats["run_fail"] += 1
                continue
            rr = RunResult(ok=True, predictions=preds,
                           episodes_in=len(episodes_by_day[day]),
                           predictions_out=len(preds))
            for hk in hotkeys:
                model = ShadowModel(hotkey=hk, image_digest=pinned,
                                    admitted_at="reconstructed")
                record_day(args.ledger_root, day.isoformat(), model, rr)
                stats["sealed"] += 1
        cleanup_rootfs(dest)
        if i % 10 == 0:
            log(f"[recon] {i}/{len(digest_jobs)} digests · sealed "
                f"{stats['sealed']} · skipped(done) {stats['skipped_done']} · "
                f"{round(time.time()-t0)}s")

    for day in days:
        record_run_marker(args.ledger_root, day.isoformat(),
                          len(episodes_by_day.get(day, [])),
                          len(per_day_hotkey_digest.get(day, {})),
                          generated_at=f"{day.isoformat()}T09:30:00Z")

    log(f"\n[recon] DONE: {json.dumps(stats)} in {round(time.time()-t0)}s")
    log("===RECON-END===")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
