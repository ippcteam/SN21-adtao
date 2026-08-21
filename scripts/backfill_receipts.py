"""Backfill the daily receipt feed for days scored before publication worked.

The daily loop has scored real results since 18 Aug 2026, but the receipt
feed only began publishing on 21 Aug (the signing key was unreadable on
this host before the key-loader fix, and 21 Aug itself was a zero-scored
day) — so every already-settled entry has a standing-ledger score and no
receipt behind it. This script closes that hole once.

One receipt per SETTLE DATE, oldest first onto the empty receipt chain.
That matches the standing ledger exactly: entries were dated to their true
settle date when they entered, so each backfilled receipt carries precisely
the entries the standings hold for that date. Scores are recomputed with
the same pure functions the settle step ran (score_settled_with_components
under the same formula flag) — nothing is invented and nothing in the
ledger is touched.

The accuracy-document chain is NOT backfilled: its head is already the
21 Aug zero-day document, and inserting earlier days would break the
chain's day-ordered prev links. Backfilled days therefore have a receipt
(properties 1, 3, 4 of the governance ruling — reproducibility) but no
per-day chain anchor; days from 21 Aug onward carry both. The receipt
docs themselves are attested and hash-chained among each other.

Run ON the executor (needs the shadow store, the ledger disk, and the
operator API env):

    python scripts/backfill_receipts.py --until 2026-08-20 [--dry-run]

Idempotent: a settle date whose receipt already exists is skipped
(republishing raises by design in the receipt rail).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hope.publication.receipt_feed import receipt_path, run_daily_receipt
from hope.scoring.settle_day_flow import (
    load_prediction_index,
    score_settled_with_components,
)
from scripts.run_daily_loop import _key_loader, _outcomes_provider


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--until", required=True,
                   help="last settle date to backfill (YYYY-MM-DD)")
    p.add_argument("--ledger-root",
                   default=os.environ.get("SN21_LEDGER_ROOT",
                                          "/var/data/sn21/ledger"))
    p.add_argument("--shadow-root", default=None,
                   help="default: same as ledger root")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    until = date.fromisoformat(args.until)
    ledger = args.ledger_root
    shadow = args.shadow_root or ledger

    key = _key_loader()
    if key is None and not args.dry_run:
        print("ABORT: no signing key (ED25519_KEY_B64 / SN21_ED25519_KEY_FILE)")
        return 1

    print(f"loading predictions from {shadow} ...")
    index = load_prediction_index(shadow)
    print(f"prediction index: {len(index)} episodes")

    print(f"fetching settled outcomes as of {until} ...")
    outcomes = _outcomes_provider()(until)
    print(f"outcomes: {len(outcomes)} settled (episode, horizon) rows")

    by_settle_date = defaultdict(list)
    for o in outcomes:
        by_settle_date[o.finalized_on].append(o)

    published = skipped = empty = 0
    for d in sorted(by_settle_date):
        day_outcomes = by_settle_date[d]
        if os.path.exists(receipt_path(ledger, d)):
            print(f"{d}: receipt exists — skipped")
            skipped += 1
            continue
        results, comps = score_settled_with_components(index, day_outcomes)
        if not results:
            print(f"{d}: {len(day_outcomes)} outcomes, 0 scored entries "
                  f"(no predictions cover them) — no receipt")
            empty += 1
            continue
        miners = len({r.miner for r in results})
        print(f"{d}: {len(day_outcomes)} outcomes -> {len(results)} entries "
              f"across {miners} miners")
        if args.dry_run:
            continue
        from scripts.run_daily_pipeline import _transition_key_provider
        tmap = _transition_key_provider(ledger)(
            sorted({str(r.episode_id) for r in results})) or {}
        rec = run_daily_receipt(
            ledger, d, day_outcomes, index, results, comps, key,
            generated_at=datetime.now(timezone.utc).isoformat(),
            transition_map=tmap)
        print(f"{d}: published={rec.published} sha256={rec.sha256}")
        published += 1

    print(json.dumps({"published": published, "skipped": skipped,
                      "empty": empty,
                      "settle_dates": len(by_settle_date)}))

    # Push the result to the public mirror straight away rather than waiting
    # for tomorrow's pipeline run — the whole point of the backfill is that
    # miners can fetch these today.
    if published and not args.dry_run:
        api_url = (os.environ.get("HOPE_API_URL") or "").strip()
        api_key = (os.environ.get("HOPE_API_KEY") or "").strip()
        if api_url and api_key:
            from hope.publication.mirror_sync import sync_mirror
            print("mirror sync:", json.dumps(
                sync_mirror(ledger, api_url, api_key)))
        else:
            print("mirror sync skipped: HOPE_API_URL/HOPE_API_KEY unset")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
