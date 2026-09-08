"""Run the admitted models against the episodes a basket gained after its
shadow day ran, and merge their predictions into the day's ledger.

    python3 -m scripts.supplement_shadow_day --basket BD-2026-09-07 [--dry-run]

WHY THIS EXISTS (8 Sept 2026). The operator supplemented BD-2026-09-07 on the
data side after it had been frozen and served on a fifth of the day's changes
("no lean day"). The shadow day for 2026-09-07 had already run, and the daily
pipeline refuses to run a shadow day twice — correctly: predictions lock once.
This is the narrow, deliberate exception: it runs each admitted model on ONLY
the added episodes, and appends a merged ledger line per model that keeps the
original predictions verbatim and joins the new ones (hope.backtest.shadow.
record_supplement), so every reader of the ledger sees the whole day.

The added set is derived from the per-basket transition-key map written at
resolve time: an episode in the basket package that the map does not know is
one the morning run never saw. The map is rewritten with the full package
afterwards, and the day's run marker is updated to the new episode count.

Then re-run the daily pipeline for the day (rm the run record + restart the
executor): resolve refreshes the maps, shadow stays skipped (locked), settle is
idempotent, and the report/performance documents are re-posted as -COR-n.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from hope.backtest import shadow as shadow_store  # noqa: E402
from hope.backtest.execution_mode import basket_runner  # noqa: E402
from scripts.run_daily_pipeline import (  # noqa: E402
    fetch_basket_payloads,
    tkeys_dir,
    write_transition_key_map,
)
from scripts.run_shadow_day_bd import admitted_models  # noqa: E402


def log(msg):
    print(f"[supplement] {datetime.now(timezone.utc).isoformat()} {msg}", flush=True)


def added_payloads(ledger_root: str, basket_key: str, payloads: list) -> list:
    """Payloads the morning run never saw: not in the basket's tkey map."""
    path = os.path.join(tkeys_dir(ledger_root), f"{basket_key}.json")
    known: set = set()
    if os.path.exists(path):
        with open(path) as fh:
            known = set(json.load(fh).keys())
    return [p for p in payloads if str(p.get("episode_id")) not in known]


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--basket", required=True, help="basket key, e.g. BD-2026-09-07")
    p.add_argument("--ledger-root",
                   default=os.environ.get("SN21_LEDGER_ROOT", "/var/data/sn21/ledger"))
    p.add_argument("--dry-run", action="store_true",
                   help="list the added episodes and the models; run nothing, write nothing")
    args = p.parse_args(argv)

    day = args.basket.replace("BD-", "")
    if not shadow_store.subnet_ran(args.ledger_root, day):
        log(f"shadow day {day} has not run — nothing to supplement; run the daily pipeline instead")
        return 2

    payloads = fetch_basket_payloads(args.basket)
    added = added_payloads(args.ledger_root, args.basket, payloads)
    added_ids = [str(x["episode_id"]) for x in added]
    log(f"{args.basket}: {len(payloads)} payloads in the package, {len(added)} added since the shadow run")
    if not added:
        return 0

    as_of = str(datetime.now(timezone.utc).date())
    models, stats = admitted_models(
        args.ledger_root, os.environ.get("SN21_NETWORK", "finney"),
        int(os.environ.get("SN21_NETUID", "21")), as_of)
    log(f"admitted models: {len(models)} (registry {stats})")
    if args.dry_run:
        for m in models[:5]:
            log(f"  would run {m.hotkey} on {len(added)} episodes")
        return 0

    run_basket = basket_runner()
    summary = {}
    for i, m in enumerate(models, 1):
        res = run_basket(m.image_digest, added)
        path = shadow_store.record_supplement(
            args.ledger_root, day, m, res, added_ids, args.basket)
        summary[m.hotkey] = {"ok": res.ok, "predictions": res.predictions_out,
                             "error": res.error}
        log(f"  [{i}/{len(models)}] {m.hotkey[:12]} ok={res.ok} "
            f"predictions={res.predictions_out}/{len(added)} -> {os.path.basename(path)}")

    n_map = write_transition_key_map(args.ledger_root, args.basket, payloads)
    shadow_store.record_run_marker(args.ledger_root, day, len(payloads), len(summary),
                                   generated_at=datetime.now(timezone.utc).isoformat())
    ok = sum(1 for s in summary.values() if s["ok"])
    log(f"done: {ok}/{len(summary)} models ok on {len(added)} added episodes; "
        f"tkey map {n_map} ids; run marker episodes={len(payloads)}")
    print(json.dumps({"basket": args.basket, "added": len(added), "models": len(summary),
                      "ok": ok, "results": summary}, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
