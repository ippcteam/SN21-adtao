#!/usr/bin/env python
"""Rebuild correct per-day receipt + accuracy documents from a catch-up
BUNDLE receipt, splitting by finalized_on. Governance-gated history repair.

WHY THIS EXISTS (2026-08-24 incident). run_settle_day is a catch-up settler:
it settles every (episode, horizon) whose markers are not yet entered, up to
`day`, and stamps them ALL into that day's receipt. When daily ticks were
missed on 08-21 and 08-23, a single run bundled 08-20-tail + 08-21 + 08-22 into
ONE receipt dated 08-22, and the accuracy docs for 08-21 and 08-22 were written
as zero-days. The scores are correct and each episode entered standings once
(global entered-markers), but the per-day PUBLIC record is wrong: two false
zero-days and a mislabeled bundle.

Re-running settle CANNOT fix it: the markers are entered, so a re-run scores
nothing and writes zero-day again. The correct data lives inside the bundle.
This tool partitions the bundle's `entries` and `outcomes` by finalized_on and
re-emits one attested receipt + accuracy document per day, copying every score,
prediction and component VERBATIM (so the numbers provably equal what was
scored) and recomputing only the per-day counts.

WHAT THIS IS NOT. It rewrites anchored history: the receipt/accuracy feeds are
append-only and hash-chained, and the rolling root is committed on chain, so
replacing days changes every sha256 from the rebuild point forward and requires
a re-anchor. That is a GOVERNANCE decision (Rob), not a routine fix — a miner
who already fetched the bundle will see it change. Run only with sign-off, and
archive the superseded receipts.

WHAT THIS DOES NOT COVER. Days whose data was never settled at all (08-23: the
bundle stops at finalized_on 08-22). Those need a real per-day settle run
BEFORE the next daily run bundles them again:
    python3 -m scripts.run_daily_pipeline --day 2026-08-23 --skip-intake
(run in day order, oldest first, so each day keeps its own receipt).

USAGE
    # DRY: partition, rebuild, self-sign with a throwaway key, reconcile counts,
    # write nothing to the ledger. Proves the split is complete and consistent.
    python scripts/_republish_from_bundle.py \
        --source https://hope-bittensor-api.onrender.com/v1/daily/2026-08-22/receipt \
        --prev-sha <sha256 of the last GOOD receipt to chain onto> \
        --acc-prev-sha <sha256 of the last GOOD accuracy doc to chain onto> \
        --days 2026-08-21,2026-08-22 --out-dir /tmp/republish

    # APPLY (executor only, real key, with sign-off): also writes into the
    # ledger receipts/ + accuracy/ dirs and rewrites the two _head.json files.
    python scripts/_republish_from_bundle.py ... --key-file $SN21_ED25519_KEY_FILE \
        --ledger-root /var/data/ledger --apply --archive-dir /var/data/ledger/_superseded
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import types
import urllib.request
from datetime import date

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cryptography.hazmat.primitives.asymmetric.ed25519 import (  # noqa: E402
    Ed25519PrivateKey,
)

from hope.publication.rail import (  # noqa: E402
    attest,
    build_document,
    document_sha256,
)
from hope.publication.accuracy_feed import build_accuracy_metrics  # noqa: E402

RECEIPT_FEED = "daily_receipt"


def _chain_ok(docs: list[dict], expect_prev: str | None) -> bool:
    """Each doc's prev_sha256 links to the previous doc's canonical hash; the
    first links to `expect_prev` (the last GOOD day we chain onto), not None —
    that is the whole point of a rebuild that continues an existing chain."""
    prev = expect_prev
    feed = None
    for d in docs:
        if feed is None:
            feed = d.get("feed")
        elif d.get("feed") != feed:
            return False
        if d.get("prev_sha256") != prev:
            return False
        prev = document_sha256(d)
    return True


def _load_source(src: str) -> dict:
    if src.startswith("http"):
        with urllib.request.urlopen(src, timeout=120) as r:
            return json.load(r)
    with open(src) as f:
        return json.load(f)


def _load_key(path: str | None) -> tuple[Ed25519PrivateKey, bool]:
    """(key, is_real). A throwaway key lets the DRY run exercise sign+chain
    without the production key; its signatures verify against its OWN pubkey,
    so the chain check still proves structural integrity."""
    if not path:
        return Ed25519PrivateKey.generate(), False
    with open(path, "rb") as f:
        raw = f.read()
    # accept raw 32-byte seed or hex
    if len(raw) == 32:
        return Ed25519PrivateKey.from_private_bytes(raw), True
    return Ed25519PrivateKey.from_private_bytes(bytes.fromhex(raw.decode().strip())), True


def _receipt_metrics_for_day(bundle_metrics: dict, day: str) -> dict:
    """Copy the bundle receipt's metrics but keep only rows finalized on `day`.
    Formula, censored and disclosure are copied verbatim; counts recomputed."""
    entries = [e for e in bundle_metrics.get("entries", [])
               if e.get("finalized_on") == day]
    outcomes = [o for o in bundle_metrics.get("outcomes", [])
                if o.get("finalized_on") == day]
    m = {
        "feed": RECEIPT_FEED,
        "formula": bundle_metrics.get("formula"),
        "outcomes": outcomes,
        "entries": entries,
        "outcomes_total": len(outcomes),
        "entries_total": len(entries),
        "miners": len({e["miner"] for e in entries}),
        "censored": bundle_metrics.get("censored", {}),
    }
    if "disclosure" in bundle_metrics:
        m["disclosure"] = bundle_metrics["disclosure"]
    return m


def _results_from_entries(entries: list[dict]) -> list:
    """Minimal HorizonResult stand-ins for build_accuracy_metrics, which reads
    only .episode_id, .horizon_days, .miner, .score (verified against
    accuracy_feed.build_accuracy_metrics)."""
    return [types.SimpleNamespace(episode_id=e["episode_id"],
                                  horizon_days=e["horizon_days"],
                                  miner=e["miner"], score=e["score"])
            for e in entries]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True, help="bundle receipt (path or URL)")
    ap.add_argument("--days", required=True, help="finalized_on days to emit, "
                    "comma-separated, in chain order (oldest first)")
    ap.add_argument("--prev-sha", default=None,
                    help="sha256 of the last GOOD receipt to chain the first "
                         "rebuilt day onto (None = start a fresh chain)")
    ap.add_argument("--acc-prev-sha", default=None,
                    help="sha256 of the last GOOD accuracy doc to chain onto")
    ap.add_argument("--out-dir", default="/tmp/republish")
    ap.add_argument("--key-file", default=None,
                    help="ed25519 key; omitted = throwaway key, DRY only")
    ap.add_argument("--ledger-root", default=None)
    ap.add_argument("--apply", action="store_true",
                    help="write into ledger receipts/ + accuracy/ and rewrite "
                         "_head.json (requires --key-file and --ledger-root)")
    ap.add_argument("--archive-dir", default=None,
                    help="move superseded ledger receipts/accuracy here first")
    args = ap.parse_args()

    src = _load_source(args.source)
    bundle_metrics = src.get("document", {}).get("metrics", {})
    days = [d.strip() for d in args.days.split(",") if d.strip()]
    for d in days:
        date.fromisoformat(d)  # validate

    key, real = _load_key(args.key_file)
    if args.apply and not (real and args.ledger_root):
        print("REFUSING --apply without a real --key-file and --ledger-root.")
        return 2

    os.makedirs(args.out_dir, exist_ok=True)

    # source reconciliation totals
    src_entries = bundle_metrics.get("entries", [])
    src_outcomes = bundle_metrics.get("outcomes", [])
    covered = {d: 0 for d in days}
    for e in src_entries:
        if e.get("finalized_on") in covered:
            covered[e["finalized_on"]] += 1
    other = sum(1 for e in src_entries if e.get("finalized_on") not in covered)

    print(f"source: {len(src_entries)} entries, {len(src_outcomes)} outcomes")
    print(f"target days: {days}")
    print(f"entries NOT in target days (left in the bundle): {other}")
    if other:
        import collections
        leftover = collections.Counter(e.get("finalized_on") for e in src_entries
                                       if e.get("finalized_on") not in covered)
        print(f"  leftover finalized_on breakdown: {dict(leftover)}")
        print("  ^ these days are ALSO in the bundle but not being rebuilt — "
              "decide whether they need their own receipts (e.g. late 08-20 "
              "settles that never reached the 08-20 receipt).")

    receipts, accuracies = [], []
    r_prev, a_prev = args.prev_sha, args.acc_prev_sha
    gen = "T00:00:00Z"
    for d in days:
        rm = _receipt_metrics_for_day(bundle_metrics, d)
        rdoc = build_document(RECEIPT_FEED, d, rm, f"{d}{gen}", prev_sha256=r_prev)
        ratt = attest(rdoc, key)
        r_prev = ratt.sha256
        receipts.append(ratt)

        results = _results_from_entries(rm["entries"])
        am = build_accuracy_metrics(results)
        am["receipt_sha256"] = ratt.sha256
        if not results:
            am["zero_day"] = True
        adoc = build_document("daily_accuracy", d, am, f"{d}{gen}",
                              prev_sha256=a_prev)
        aatt = attest(adoc, key)
        a_prev = aatt.sha256
        accuracies.append(aatt)

        print(f"\n{d}: receipt entries={rm['entries_total']} "
              f"outcomes={rm['outcomes_total']} miners={rm['miners']} "
              f"sha={ratt.sha256[:12]} | accuracy sha={aatt.sha256[:12]}")

        for att, kind in ((ratt, "receipt"), (aatt, "accuracy")):
            with open(os.path.join(args.out_dir, f"{d}.{kind}.json"), "w") as f:
                json.dump({"document": att.document, "sha256": att.sha256,
                           "signature_hex": att.signature_hex,
                           "public_key_hex": att.public_key_hex}, f, indent=1,
                          default=str)

    # structural chain check on the rebuilt series (per feed)
    ok_r = _chain_ok([a.document for a in receipts], args.prev_sha)
    ok_a = _chain_ok([a.document for a in accuracies], args.acc_prev_sha)
    print(f"\nchain check: receipts={ok_r} accuracy={ok_a}")
    covered_total = sum(covered.values())
    print(f"reconciliation: {covered_total} of {len(src_entries)} source "
          f"entries went into rebuilt days ({other} left over)")

    if not real:
        print("\nDRY (throwaway key). Wrote rebuilt docs to "
              f"{args.out_dir}. Nothing touched the ledger or the chain.")
        return 0

    if not args.apply:
        print("\nsigned with the REAL key but --apply not set: wrote files to "
              f"{args.out_dir} only. Re-run with --apply to install.")
        return 0

    # --apply: install into the ledger, archiving anything superseded.
    rdir = os.path.join(args.ledger_root, "receipts")
    adir = os.path.join(args.ledger_root, "accuracy")
    for d, ratt, aatt in zip(days, receipts, accuracies):
        for base, att, sub in ((rdir, ratt, "receipts"), (adir, aatt, "accuracy")):
            dst = os.path.join(base, f"{d}.json")
            if os.path.exists(dst):
                if not args.archive_dir:
                    print(f"REFUSING to overwrite {dst} without --archive-dir")
                    return 2
                arc = os.path.join(args.archive_dir, sub)
                os.makedirs(arc, exist_ok=True)
                shutil.move(dst, os.path.join(arc, f"{d}.json"))
            os.makedirs(base, exist_ok=True)
            with open(dst, "w") as f:
                json.dump({"document": att.document, "sha256": att.sha256,
                           "signature_hex": att.signature_hex,
                           "public_key_hex": att.public_key_hex}, f, indent=1,
                          default=str)
    # rewrite the two heads to the LAST rebuilt day
    with open(os.path.join(rdir, "_head.json"), "w") as f:
        json.dump({"day": days[-1], "sha256": receipts[-1].sha256}, f)
    with open(os.path.join(adir, "_head.json"), "w") as f:
        json.dump({"day": days[-1], "sha256": accuracies[-1].sha256}, f)
    print(f"\nAPPLIED {len(days)} days into {args.ledger_root}. "
          "Now recompute feed_root and RE-ANCHOR on chain, then push the mirror.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
