"""Build the measured change-type weight table (DRAFT) from real scored data.

Produces the table behind the type-weighted scoring amendment: per change-type
family — frequency, field accuracy, how many miners qualify, headroom (how far
the best models pull away from the median), and the derived weight. Output is
always status=draft: ratification is a human act on the published amendment,
and the scoring loader refuses drafts.

Data sources, in order of preference:
  * --receipts-dir  the ledger's signed daily receipt files (executor disk)
  * --mirror-url    the public daily mirror (no key needed), fetching
                    /v1/daily/<day>/receipt for each day in the window

Labels come from the per-basket transition-key maps (tkeys/) written at
resolve time; run scripts.backfill_transition_key_maps first for baskets that
predate the maps.

Usage on the executor:

    python -m scripts.build_type_weight_table --days 28 \
        --out /var/data/sn21/ledger/type_weights_draft.json --markdown

Writes the JSON table and, with --markdown, a review table for the amendment.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from hope.scoring.type_weights import compute_table                # noqa: E402
from scripts.run_daily_pipeline import _transition_key_provider    # noqa: E402


def _receipt_entries_from_dir(receipts_dir: str, days: list[str]):
    for d in days:
        p = os.path.join(receipts_dir, f"{d}.json")
        if not os.path.exists(p):
            continue
        with open(p) as fh:
            doc = json.load(fh)
        yield d, ((doc.get("document") or doc).get("metrics") or {}).get(
            "entries") or []


def _receipt_entries_from_mirror(mirror_url: str, days: list[str]):
    for d in days:
        url = f"{mirror_url.rstrip('/')}/v1/daily/{d}/receipt"
        try:
            with urllib.request.urlopen(url, timeout=120) as resp:
                doc = json.loads(resp.read().decode())
        except Exception as e:  # noqa: BLE001 — a missing day is data, not a crash
            print(f"{d}: no receipt ({e})", flush=True)
            continue
        yield d, ((doc.get("document") or doc).get("metrics") or {}).get(
            "entries") or []


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ledger-root",
                   default=os.environ.get("SN21_LEDGER_ROOT",
                                          "/var/data/sn21/ledger"))
    p.add_argument("--receipts-dir", default=None,
                   help="directory of <day>.json receipts (default: fetch "
                        "from --mirror-url)")
    p.add_argument("--mirror-url",
                   default="https://hope-ads-backend.onrender.com")
    p.add_argument("--days", type=int, default=28,
                   help="trailing window of settle days to measure")
    p.add_argument("--end", default=str(date.today()),
                   help="last day of the window (default today)")
    p.add_argument("--out", required=True, help="path for the draft JSON table")
    p.add_argument("--markdown", action="store_true",
                   help="also print the review table")
    args = p.parse_args()

    y, m, dd = (int(x) for x in args.end.split("-"))
    end = date(y, m, dd)
    days = [str(end - timedelta(days=i)) for i in range(args.days)]

    source = (_receipt_entries_from_dir(args.receipts_dir, days)
              if args.receipts_dir
              else _receipt_entries_from_mirror(args.mirror_url, days))

    provider = _transition_key_provider(args.ledger_root)

    triples = []
    unlabelled = 0
    days_used = 0
    # Dedup: a re-settled entry can appear in more than one day's receipt;
    # the entry is the same evidence and must be measured once.
    seen: set = set()
    for d, entries in source:
        if not entries:
            continue
        days_used += 1
        ids = sorted({str(e.get("episode_id")) for e in entries})
        tmap = provider(ids)
        for e in entries:
            key = (str(e.get("miner")), str(e.get("episode_id")),
                   str(e.get("horizon_days")))
            if key in seen:
                continue
            seen.add(key)
            tkey = tmap.get(str(e.get("episode_id")))
            if tkey is None:
                unlabelled += 1
            triples.append((str(e.get("miner")), tkey, float(e.get("score"))))

    print(f"days with receipts: {days_used}/{len(days)}  "
          f"entries: {len(triples)}  unlabelled: {unlabelled} "
          f"({100.0 * unlabelled / len(triples):.1f}%)" if triples else
          "no entries found", flush=True)
    if not triples:
        return 1
    if unlabelled and unlabelled / len(triples) > 0.5:
        print("WARNING: over half the entries are unlabelled — run "
              "scripts.backfill_transition_key_maps first, or the table "
              "measures mostly UNKNOWN.", flush=True)

    table = compute_table(triples,
                          window_start=str(end - timedelta(days=args.days - 1)),
                          window_end=str(end))
    table.save(args.out)
    print(f"draft table written: {args.out}", flush=True)

    if args.markdown:
        print("\n| family | share | entries | field mean | miners | "
              "headroom | weight |")
        print("|---|---|---|---|---|---|---|")
        fams = sorted(table.families.items(),
                      key=lambda kv: -kv[1].n_entries)
        for fam, s in fams:
            head = f"{s.headroom:.4f}" if s.headroom is not None else "below gates"
            print(f"| {fam} | {100 * s.freq_share:.1f}% | {s.n_entries} | "
                  f"{s.field_mean:.4f} | {s.miners_qualified} | {head} | "
                  f"{s.weight:.3f} |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
