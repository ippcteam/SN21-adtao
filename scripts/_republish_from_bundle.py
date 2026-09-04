#!/usr/bin/env python
"""Repair the receipt/accuracy chains after a catch-up BUNDLE receipt.

Governance-approved (Rob, 2026-08-24). Two operations, one chain rewrite:

1. SPLIT: a missed-tick catch-up stamped several days of settles into ONE
   receipt (2026-08-22: 43,315 entries spanning finalized_on 08-20/21/22)
   while the skipped days published as false zero-days. This partitions the
   bundle's `entries`/`outcomes` by finalized_on and re-emits one attested
   receipt + accuracy per day, every score/prediction/component VERBATIM.

2. RECHAIN: days AFTER the split already have correct documents (08-23:
   58,566 entries — it was never missing, the mirror had just failed to
   sync) but their prev_sha256 points at the superseded bundle. Those are
   re-signed content-verbatim onto the new chain; an accuracy doc whose
   receipt was re-signed gets its embedded receipt_sha256 updated to match.

Scores never change. Standings never change (entered-markers already hold).
This rewrites the published hash chain, so it runs ONLY with sign-off, on
the executor (ledger + signing key), archiving every superseded file.

USAGE (executor Shell):
  # DRY — throwaway key, writes only to --out-dir, prints reconciliation:
  python scripts/_republish_from_bundle.py \
      --source https://hope-bittensor-api.onrender.com/v1/daily/2026-08-22/receipt \
      --days 2026-08-21,2026-08-22 \
      --rechain-days 2026-08-23,2026-08-24 \
      --prev-sha <08-20 receipt sha> --acc-prev-sha <08-20 accuracy sha> \
      --out-dir /tmp/repair

  # APPLY — real key, installs into the ledger, rewrites both _head.json:
  ... same args plus:
      --key-file "$SN21_ED25519_KEY_FILE" --ledger-root "$SN21_LEDGER_ROOT" \
      --archive-dir "$SN21_LEDGER_ROOT/_superseded" --apply

Then re-sync the mirror (sync_mirror recent_days=None). SN21_ANCHOR_COMMITS
is currently unset, so there is no on-chain root to re-commit; when anchoring
turns on, the next commit covers the corrected history.
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

from hope.publication.accuracy_feed import (  # noqa: E402
    ACCURACY_FEED_NAME,
    build_accuracy_metrics,
)
from hope.publication.rail import (  # noqa: E402
    attest,
    build_document,
    document_sha256,
)
from hope.publication.receipt_feed import RECEIPT_FEED_NAME  # noqa: E402

DEFAULT_BASE = "https://hope-bittensor-api.onrender.com"


def _chain_ok(docs: list[dict], expect_prev: str | None) -> bool:
    """Each doc links to the previous; the first links to `expect_prev` (the
    last GOOD day being chained onto), not None."""
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


def _fetch(url_or_path: str) -> dict:
    if url_or_path.startswith("http"):
        with urllib.request.urlopen(url_or_path, timeout=120) as r:
            return json.load(r)
    with open(url_or_path) as f:
        return json.load(f)


def _load_key(path: str | None) -> tuple[Ed25519PrivateKey, bool]:
    if not path:
        return Ed25519PrivateKey.generate(), False
    with open(path, "rb") as f:
        raw = f.read()
    if len(raw) == 32:
        return Ed25519PrivateKey.from_private_bytes(raw), True
    return Ed25519PrivateKey.from_private_bytes(
        bytes.fromhex(raw.decode().strip())), True


def _receipt_metrics_for_day(bundle_metrics: dict, day: str) -> dict:
    entries = [e for e in bundle_metrics.get("entries", [])
               if e.get("finalized_on") == day]
    outcomes = [o for o in bundle_metrics.get("outcomes", [])
                if o.get("finalized_on") == day]
    m = {
        "feed": RECEIPT_FEED_NAME,
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


def _accuracy_metrics_from_entries(entries: list[dict],
                                   receipt_sha: str | None) -> dict:
    results = [types.SimpleNamespace(episode_id=e["episode_id"],
                                     horizon_days=e["horizon_days"],
                                     miner=e["miner"], score=e["score"])
               for e in entries]
    m = build_accuracy_metrics(results)
    m["receipt_sha256"] = receipt_sha
    if not results:
        m["zero_day"] = True
    return m


def _day_doc(kind: str, day: str, ledger_root: str | None, base: str) -> dict | None:
    """Existing published envelope for a day — ledger file wins, else mirror."""
    if ledger_root:
        sub = "receipts" if kind == "receipt" else "accuracy"
        p = os.path.join(ledger_root, sub, f"{day}.json")
        if os.path.exists(p):
            return _fetch(p)
        if kind == "receipt":
            return None      # a day may legitimately have no receipt
    try:
        return _fetch(f"{base}/v1/daily/{day}/{kind}")
    except urllib.error.HTTPError as e:
        if e.code == 404 and kind == "receipt":
            return None
        raise


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True,
                    help="bundle receipt to split (path or URL)")
    ap.add_argument("--days", required=True,
                    help="finalized_on days to SPLIT out, oldest first")
    ap.add_argument("--rechain-days", default="",
                    help="days AFTER the split whose existing docs are "
                         "re-signed verbatim onto the new chain, in order")
    ap.add_argument("--prev-sha", default=None,
                    help="receipt sha of the last GOOD day to chain onto")
    ap.add_argument("--acc-prev-sha", default=None,
                    help="accuracy sha of the last GOOD day to chain onto")
    ap.add_argument("--source-base", default=DEFAULT_BASE)
    ap.add_argument("--out-dir", default="/tmp/repair")
    ap.add_argument("--key-file", default=None)
    ap.add_argument("--ledger-root", default=None)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--archive-dir", default=None)
    args = ap.parse_args()

    split_days = [d.strip() for d in args.days.split(",") if d.strip()]
    rechain_days = [d.strip() for d in args.rechain_days.split(",") if d.strip()]
    for d in split_days + rechain_days:
        date.fromisoformat(d)

    key, real = _load_key(args.key_file)
    if args.apply and not (real and args.ledger_root):
        print("REFUSING --apply without a real --key-file and --ledger-root.")
        return 2
    os.makedirs(args.out_dir, exist_ok=True)

    src = _fetch(args.source)
    bm = src.get("document", {}).get("metrics", {})
    src_entries = bm.get("entries", [])
    in_split = {d: 0 for d in split_days}
    for e in src_entries:
        if e.get("finalized_on") in in_split:
            in_split[e["finalized_on"]] += 1
    leftover = len(src_entries) - sum(in_split.values())
    print(f"bundle: {len(src_entries)} entries; split coverage {in_split}; "
          f"leftover {leftover} (late settles for already-anchored days — "
          f"disclose, do not rebuild)")

    out_docs: list[tuple[str, str, object]] = []   # (day, kind, attested)
    r_prev, a_prev = args.prev_sha, args.acc_prev_sha
    receipt_head = None

    for d in split_days:
        rm = _receipt_metrics_for_day(bm, d)
        rdoc = build_document(RECEIPT_FEED_NAME, d, rm, f"{d}T00:00:00Z",
                              prev_sha256=r_prev)
        ratt = attest(rdoc, key)
        r_prev, receipt_head = ratt.sha256, d
        am = _accuracy_metrics_from_entries(rm["entries"], ratt.sha256)
        adoc = build_document(ACCURACY_FEED_NAME, d, am, f"{d}T00:00:00Z",
                              prev_sha256=a_prev)
        aatt = attest(adoc, key)
        a_prev = aatt.sha256
        out_docs += [(d, "receipt", ratt), (d, "accuracy", aatt)]
        print(f"SPLIT {d}: receipt entries={rm['entries_total']} "
              f"miners={rm['miners']} sha={ratt.sha256[:12]} | "
              f"accuracy sha={aatt.sha256[:12]}")

    for d in rechain_days:
        renv = _day_doc("receipt", d, args.ledger_root, args.source_base)
        new_receipt_sha = None
        if renv is not None:
            doc = dict(renv["document"])
            doc["prev_sha256"] = r_prev
            ratt = attest(doc, key)
            r_prev, receipt_head = ratt.sha256, d
            new_receipt_sha = ratt.sha256
            out_docs.append((d, "receipt", ratt))
        aenv = _day_doc("accuracy", d, args.ledger_root, args.source_base)
        if aenv is None:
            print(f"RECHAIN {d}: no accuracy doc — nothing to re-sign")
            continue
        adoc = dict(aenv["document"])
        adoc["prev_sha256"] = a_prev
        if new_receipt_sha is not None:
            # the accuracy doc anchors its receipt by sha — keep them married
            m = dict(adoc.get("metrics", {}))
            m["receipt_sha256"] = new_receipt_sha
            r_entries = (renv or {}).get("document", {}).get("metrics", {}).get("entries") or []
            if (m.get("zero_day") or not m.get("results_total")) and r_entries:
                # The day was published as a zero day while a receipt with
                # entries exists for it (a catch-up settle published the
                # receipt after the accuracy document had already gone out).
                # The accuracy document is derived from the receipt, so
                # rebuild it from those entries rather than re-signing a
                # zero that the receipt contradicts.
                m = _accuracy_metrics_from_entries(r_entries, new_receipt_sha)
                print(f"RECHAIN {d}: accuracy rebuilt from receipt entries "
                      f"({len(r_entries)} entries replace a zero-day document)")
            adoc["metrics"] = m
        aatt = attest(adoc, key)
        a_prev = aatt.sha256
        out_docs.append((d, "accuracy", aatt))
        print(f"RECHAIN {d}: receipt={'re-signed ' + new_receipt_sha[:12] if new_receipt_sha else 'none'} "
              f"| accuracy re-signed {aatt.sha256[:12]} "
              f"(results_total={adoc.get('metrics', {}).get('results_total')})")

    rdocs = [a.document for d, k, a in out_docs if k == "receipt"]
    adocs = [a.document for d, k, a in out_docs if k == "accuracy"]
    ok_r = _chain_ok(rdocs, args.prev_sha)
    ok_a = _chain_ok(adocs, args.acc_prev_sha)
    print(f"\nchain check: receipts={ok_r} accuracy={ok_a}")
    if not (ok_r and ok_a):
        print("REFUSING to continue with a broken rebuilt chain.")
        return 2

    for d, kind, att in out_docs:
        with open(os.path.join(args.out_dir, f"{d}.{kind}.json"), "w") as f:
            json.dump({"document": att.document, "sha256": att.sha256,
                       "signature_hex": att.signature_hex,
                       "public_key_hex": att.public_key_hex}, f, indent=1,
                      default=str)

    if not real:
        print(f"\nDRY (throwaway key). Docs in {args.out_dir}. Nothing touched.")
        return 0
    if not args.apply:
        print(f"\nSigned with the REAL key; --apply not set. Docs in {args.out_dir}.")
        return 0

    for d, kind, att in out_docs:
        sub = "receipts" if kind == "receipt" else "accuracy"
        base_dir = os.path.join(args.ledger_root, sub)
        dst = os.path.join(base_dir, f"{d}.json")
        if os.path.exists(dst):
            if not args.archive_dir:
                print(f"REFUSING to overwrite {dst} without --archive-dir")
                return 2
            arc = os.path.join(args.archive_dir, sub)
            os.makedirs(arc, exist_ok=True)
            shutil.move(dst, os.path.join(arc, f"{d}.json"))
        os.makedirs(base_dir, exist_ok=True)
        with open(dst, "w") as f:
            json.dump({"document": att.document, "sha256": att.sha256,
                       "signature_hex": att.signature_hex,
                       "public_key_hex": att.public_key_hex}, f, indent=1,
                      default=str)

    all_days = split_days + rechain_days
    with open(os.path.join(args.ledger_root, "receipts", "_head.json"), "w") as f:
        json.dump({"day": receipt_head, "sha256": r_prev}, f)
    with open(os.path.join(args.ledger_root, "accuracy", "_head.json"), "w") as f:
        json.dump({"day": all_days[-1], "sha256": a_prev}, f)
    print(f"\nAPPLIED {len(out_docs)} documents. receipts head={receipt_head}, "
          f"accuracy head={all_days[-1]}. Now re-sync the mirror "
          f"(sync_mirror recent_days=None).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
