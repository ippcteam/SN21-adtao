"""Backfill per-basket transition-key maps for baskets that ran before the
maps existed.

The daily pipeline now persists {episode_id: transition_key} at resolve time
(run_daily_pipeline.write_transition_key_map). Episodes from earlier baskets
still settle for up to 36 days after their basket day, and without a map
their scored entries label as UNKNOWN on the by-type page. The baskets are
still served by the operator API, so the maps can be rebuilt exactly.

Usage (on the executor, where the ledger disk is mounted):

    python -m scripts.backfill_transition_key_maps --days 40
    python -m scripts.backfill_transition_key_maps --basket BD-2026-08-24

Idempotent: an existing map file is skipped unless --force. Read-only against
the API; writes only ledger_root/tkeys/.
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from scripts.run_daily_pipeline import (  # noqa: E402
    fetch_basket_payloads,
    tkeys_dir,
    write_transition_key_map,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ledger-root",
                   default=os.environ.get("SN21_LEDGER_ROOT",
                                          "/var/data/sn21/ledger"))
    p.add_argument("--days", type=int, default=40,
                   help="walk BD-<day> keys this many days back (default 40, "
                        "covering the 35-day scoring window with margin)")
    p.add_argument("--basket", action="append", default=[],
                   help="explicit basket key(s) instead of the day walk")
    p.add_argument("--force", action="store_true",
                   help="rewrite maps that already exist")
    args = p.parse_args()

    if args.basket:
        keys = list(args.basket)
    else:
        # Do NOT filter on the releases listing: it serves only recent
        # releases, while entries settling today come from baskets 10-36
        # days old — exactly the ones the listing has aged out. Measured
        # 2026-09-01: filtering left 69.9% of scored entries unlabelled.
        # Try every candidate key directly; the package endpoint 404s for
        # baskets that never existed and the walk records those as failed
        # fetches (which the summary distinguishes from written maps).
        today = date.today()
        keys = [f"BD-{today - timedelta(days=i)}"
                for i in range(1, args.days + 1)]

    done = skipped = failed = 0
    for key in keys:
        out_path = os.path.join(tkeys_dir(args.ledger_root), f"{key}.json")
        if os.path.exists(out_path) and not args.force:
            skipped += 1
            continue
        try:
            payloads = fetch_basket_payloads(key)
            n = write_transition_key_map(args.ledger_root, key, payloads)
            print(f"{key}: {n} keys", flush=True)
            done += 1
        except Exception as e:  # noqa: BLE001 — one basket must not stop the walk
            if "404" in str(e):
                skipped += 1          # basket never existed for that day
            else:
                print(f"{key}: FAILED {e}", flush=True)
                failed += 1

    print(f"done={done} skipped={skipped} failed={failed}", flush=True)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
