"""What a day's weight vector would be under a different curve tail — on a COPY.

    python3 -m scripts.preview_curve_tail --day 2026-09-09 --tails 0.5,0.8

Runs the real allocation (hope.validator.daily_stream_weights.allocation_from_ledger)
against a copy of the ledger, once per tail, and prints the earning set side by side:
rank, hotkey, standing, share under each tail, and the 16-bit chain weight each share
rounds to. The live ledger is never opened for writing: allocation_from_ledger persists
promotion state and appends audit events, so it runs on the copy only.

WHY A COPY, NOT A DRY-RUN FLAG. The allocation has no read-only mode, and adding one
would mean a second code path that the daily run never exercises. Copying the ledger
(minus the bulky per-episode documents the allocation does not read) costs a few hundred
megabytes on the executor disk and exercises exactly the code the next morning will run.

The published tail is whatever SN21_CURVE_TAIL_DECAY / SN21_CURVE_TAIL_EFFECTIVE_FROM say
for the day (the published 0.5 when unset); each requested tail is forced through the
environment the allocation reads, so the effective-date logic is exercised too.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from hope.scoring.standing_method import (  # noqa: E402
    CURVE_TAIL_DECAY_ENV,
    CURVE_TAIL_EFFECTIVE_FROM_ENV,
    curve_tail_decay,
)
from hope.validator.daily_stream_weights import allocation_from_ledger  # noqa: E402

# Bulky documents the allocation does not read. Everything else is copied.
SKIP = ("prediction_performance", "pipeline_runs", "tkeys")


def copy_ledger(src: str, dst: str) -> str:
    if os.path.isdir(dst):
        shutil.rmtree(dst)
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(*SKIP))
    return dst


def u16(share: float) -> int:
    return int(round(share * 65535))


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--day", default=str(datetime.now(timezone.utc).date()))
    p.add_argument("--tails", default="0.5,0.8")
    p.add_argument("--ledger-root",
                   default=os.environ.get("SN21_LEDGER_ROOT", "/var/data/sn21/ledger"))
    p.add_argument("--workdir",
                   default=os.environ.get("SN21_EXECUTOR_WORKDIR") or "/tmp")
    p.add_argument("--episodes", type=int, default=None,
                   help="day_episode_volume for the D3 gate (default: large enough to pass)")
    args = p.parse_args(argv)

    day = date.fromisoformat(args.day)
    tails = [float(t) for t in args.tails.split(",") if t.strip()]
    print(f"[preview] published tail in force on {day} from the live environment: "
          f"{curve_tail_decay(os.environ, day)}")

    results = {}
    for tail in tails:
        copy = copy_ledger(args.ledger_root,
                           os.path.join(args.workdir, "curve_preview", f"ledger_{tail}"))
        env = dict(os.environ)
        env[CURVE_TAIL_DECAY_ENV] = str(tail)
        env.pop(CURVE_TAIL_EFFECTIVE_FROM_ENV, None)      # force it for this preview
        alloc = allocation_from_ledger(copy, day, args.episodes or 10_000, environ=env)
        results[tail] = alloc
        print(f"[preview] tail {tail}: gated={alloc.gated} earning_set={alloc.earning_set_size}")

    first = results[tails[0]]
    order = sorted(first.weights.items(), key=lambda kv: -kv[1])
    order = [hk for hk, w in order if w > 0]
    for tail in tails[1:]:
        extra = [hk for hk, w in results[tail].weights.items() if w > 0 and hk not in order]
        order.extend(sorted(extra, key=lambda hk: -results[tail].weights[hk]))

    head = f"{'rank':>4}  {'hotkey':<14} {'standing':>10}"
    for tail in tails:
        head += f"  {'share@' + str(tail):>10} {'u16':>6}"
    print(head)
    for i, hk in enumerate(order, 1):
        line = f"{i:>4}  {hk[:12] + '..':<14} {first.standings.get(hk, float('nan')):>10.4f}"
        for tail in tails:
            w = results[tail].weights.get(hk, 0.0)
            line += f"  {w:>10.4%} {u16(w):>6}"
        print(line)
    for tail in tails:
        ws = sorted((w for w in results[tail].weights.values() if w > 0), reverse=True)
        top3 = sum(ws[:3]); mid = sum(ws[3:10]); tail_band = sum(ws[10:20])
        zero = sum(1 for w in ws if u16(w) == 0)
        print(f"[preview] tail {tail}: top3={top3:.1%} ranks4-10={mid:.1%} "
              f"ranks11-20={tail_band:.1%} paid_on_chain={len(ws) - zero}/{len(ws)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
