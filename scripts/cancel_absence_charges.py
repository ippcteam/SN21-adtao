"""Cancel absence charges that operator faults caused — by the book.

Each `--charge DAY:HOTKEY:REASON` names one charged (day, hotkey) from the
published penalty log and why it is being cancelled. The script reads the
charge from the log itself (missed count and score come from the record —
never from the operator's memory), refuses anything not actually charged or
already cancelled, and appends one cancellation record per charge. Nothing
is deleted: the charge and its cancellation both stay readable forever, and
the mirror publishes them side by side.

Standings pick the cancellation up through standing_ledger.load_entries —
the one point every consumer reads through — on the next pipeline run.

Runs where the ledger disk is mounted (the executor service shell):

    python -m scripts.cancel_absence_charges \
        --ledger-root /var/data/sn21/ledger \
        --charge "2026-09-01:5G7JTZ...:image pull failed on the operator side"
"""

from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger-root", required=True)
    parser.add_argument("--charge", action="append", required=True,
                        metavar="DAY:HOTKEY:REASON",
                        help="one charged (day, hotkey) and the reason for "
                             "cancelling it; repeatable")
    parser.add_argument("--apply", action="store_true",
                        help="write the cancellations (default: dry run)")
    args = parser.parse_args()

    from hope.scoring.absence_penalty import PENALTY_ENTRY_WEIGHT, penalty_log
    from hope.scoring.standing_ledger import (
        load_cancellations,
        record_cancellation,
    )

    charges = {(p["day"], p["hotkey"]): p
               for p in penalty_log(args.ledger_root)}
    already = {(c["day"], c["hotkey"])
               for c in load_cancellations(args.ledger_root)}

    to_write = []
    for spec in args.charge:
        try:
            day, hotkey, reason = spec.split(":", 2)
        except ValueError:
            print(f"REFUSED {spec!r}: not DAY:HOTKEY:REASON")
            return 1
        reason = reason.strip()
        if not reason:
            print(f"REFUSED {day}/{hotkey}: a cancellation must say why")
            return 1
        charge = charges.get((day, hotkey))
        if charge is None:
            print(f"REFUSED {day}/{hotkey}: no such charge in the penalty log")
            return 1
        if (day, hotkey) in already:
            print(f"REFUSED {day}/{hotkey}: already cancelled")
            return 1
        to_write.append((charge, reason))

    for charge, reason in to_write:
        print(json.dumps({"day": charge["day"], "hotkey": charge["hotkey"],
                          "missed": charge["missed"],
                          "score": charge["score"],
                          "weight": PENALTY_ENTRY_WEIGHT,
                          "reason": reason}))
        if args.apply:
            record_cancellation(args.ledger_root, charge["day"],
                                charge["hotkey"], charge["missed"],
                                charge["score"], PENALTY_ENTRY_WEIGHT, reason)
    print(("APPLIED" if args.apply else "DRY RUN — re-run with --apply"),
          f"({len(to_write)} cancellation(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main())
