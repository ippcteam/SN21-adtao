"""Read-only preview: who takes each copy set's seat under SN21_PAYABLE_SEAT_FROM.

Reads the published allocation audits (a ledger's allocation_audit/<day>.json,
or files passed on the command line) and, per day, lists each copy set whose
seat holders cannot be paid while another member can: who the rule would seat
and why each holder could not be paid. Writes nothing.

  python3 scripts/preview_payable_seat.py --root <ledger> --days 7
  python3 scripts/preview_payable_seat.py audit-2026-11-12.json ...

Approximate in one way: the published audit names who each control excluded,
not the precedence order in force, so the member chosen here is the first of
the published group order (seat holder, then excluded as listed). The run
itself uses the order in force.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hope.validator.daily_stream_weights import payable_seats


def reasons(doc: dict) -> dict:
    """hotkey -> why it could not be paid that day, from the audit's own lists."""
    out: dict = {}
    for hk in doc.get("deregistered") or []:
        out.setdefault(hk, "deregistered")
    for hk in (doc.get("one_coldkey_one_seat") or {}).get("excluded") or []:
        out.setdefault(hk, "second seat on a coldkey")
    for hk in (doc.get("placement") or {}).get("below") or {}:
        out.setdefault(hk, "under the placement floor")
    for hk in (doc.get("tenure") or {}).get("excluded") or []:
        out.setdefault(hk, "short of tenure")
    for hk in (doc.get("alpha_hold") or {}).get("excluded") or []:
        out.setdefault(hk, "under the alpha hold")
    return out


def preview(doc: dict) -> list:
    groups = [(g.get("seat_held_by"), list(g.get("excluded") or [])) for g in doc.get("groups") or []]
    suppressed = {m for _, ex in groups for m in ex}
    order = {}
    for payee, ex in groups:
        for m in [payee, *ex]:
            order.setdefault(m, len(order))
    return payable_seats(groups, suppressed, reasons(doc),
                         seniority={m: f"{i:06d}" for m, i in order.items()})


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*")
    ap.add_argument("--root")
    ap.add_argument("--days", type=int, default=7)
    a = ap.parse_args(argv)
    files = list(a.files)
    if a.root:
        d = os.path.join(a.root, "allocation_audit")
        files += [os.path.join(d, n) for n in sorted(os.listdir(d)) if n.endswith(".json")][-a.days:]
    for path in files:
        with open(path) as f:
            doc = json.load(f)
        doc = doc.get("document", doc)
        passed = preview(doc)
        print(f"{doc.get('day')}: {len(doc.get('groups') or [])} groups, "
              f"{len(passed)} set(s) would pass the seat")
        for p in passed:
            why = "; ".join(f"{h['hotkey'][:8]} {h['why']}" for h in p["instead_of"])
            print(f"   {p['members']} members -> {p['to'][:8]} (instead of: {why})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
