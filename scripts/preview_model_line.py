"""Read-only: which copy groups would change payee under the model-line rule.

  python3 scripts/preview_model_line.py [--days 14] [--root /var/data/sn21/ledger]

For each of the last N published allocation audits, every copy group is ranked
by the model's line (hope.scoring.model_line) against the 40-day record and the
copy-set history (the record, or the published audits when there is none yet).
A tie on the line start keeps the payee the audit named (the receipt order).
Prints each group whose payee would change, with both members' line starts.
Writes nothing.
"""
import argparse
import json
import os
import sys
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from hope.backtest.unchanged_models import load as load_models
from hope.publication.allocation_audit import FEED_DIR
from hope.scoring import model_line as ML


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--root", default=os.environ.get("SN21_LEDGER_ROOT", "/var/data/sn21/ledger"))
    a = ap.parse_args()
    models = load_models(a.root) or {}
    runs = models.get("runs") or {}
    history = ML.load(a.root, runs)
    audit_dir = os.path.join(a.root, FEED_DIR)
    days = sorted(fn[:-5] for fn in os.listdir(audit_dir) if fn.endswith(".json"))[-a.days:]
    flips = total = 0
    for day in days:
        with open(os.path.join(audit_dir, f"{day}.json")) as f:
            groups = json.load(f).get("groups") or []
        named = [[m for m in [g.get("seat_held_by"), *(g.get("excluded") or [])] if m]
                 for g in groups]
        # The audit has no row digests: a member is judged under its model of
        # that day, and is evidence only if it ran that one model for the whole
        # window a receipt's rows can come from (as the seed does).
        back = (date.fromisoformat(day) - timedelta(days=ML.ROW_WINDOW_DAYS)).isoformat()
        judged = []
        for g in named:
            j = {}
            for m in g:
                tl = ML.timeline(runs, m)
                now, then = ML.digest_on(tl, day), ML.digest_on(tl, back)
                j[m] = [d for d in {now, then} if d] if then not in (now, None) else [now] if now else []
            judged.append(j)
        before = {d: v for d, v in history.items() if d < day}
        receipt_order = {}
        for g in named:
            for i, hk in enumerate(g):
                receipt_order.setdefault(hk, f"{i:04d}")
        key = ML.precedence(judged, runs, before, receipt_order)
        for g, members in zip(groups, named):
            total += 1
            new = ML.ranked(members, key)[0]
            if new != members[0]:
                flips += 1
                print(f"{day} {g.get('kind')}: {members[0][:10]} -> {new[:10]} | "
                      + ", ".join(f"{m[:10]} line {key[m][0]}" for m in members))
    print(f"{len(days)} day(s), {total} group(s), {flips} payee change(s); "
          f"40-day record through {models.get('through')}, copy-set history "
          f"{'recorded' if os.path.exists(os.path.join(a.root, ML.RECORD)) else 'seeded from audits'} "
          f"({len(history)} day(s))")


if __name__ == "__main__":
    main()
