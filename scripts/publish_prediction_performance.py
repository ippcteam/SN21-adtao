"""Publish the cumulative Prediction Performance document by hand.

The daily pipeline runs the same stage automatically (step 5b); this
entrypoint exists for the day the stage ships — publishing the first
document without re-running the whole day — and for re-posting after a CMS
rejection. One code path either way: this is only an argument parser in
front of stage_publish_performance.

Runs where the ledger disk is mounted (the executor service shell):

    python -m scripts.publish_prediction_performance \
        --ledger-root /var/data/sn21/ledger
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger-root", required=True)
    parser.add_argument("--day", default=str(date.today()),
                        help="as-of day stamped on the document "
                             "(default: today)")
    args = parser.parse_args()

    from scripts.run_daily_pipeline import stage_publish_performance
    out = stage_publish_performance(args.ledger_root, args.day)
    print(json.dumps(out, default=str))
    return 0 if out.get("published") else 1


if __name__ == "__main__":
    sys.exit(main())
