"""Dry run of the prediction-day standing, from the executor's disk, changing nothing.

    python3 -m scripts.standing_preview [--day YYYY-MM-DD] [--top 25] [--hotkeys hk1,hk2]

Computes what the "current model, current form" amendment would rank on the
day, beside the rule in force, from the receipts and the model-boundary file
already on the ledger disk. Writes <ledger>/standing_preview/<day>.json — the
same document the daily loop writes under SN21_STANDING_AGE_BASIS_PREVIEW —
and prints the summary and the largest movers. It does NOT run the pipeline:
no settle, no vector, no report, no mirror. The file it writes is mirrored
only by a later pipeline run, and only while SN21_STANDING_PREVIEW_PUBLISH is
set.

If the model-boundary file is missing it is built from the shadow ledger
(the first basket day each hotkey's current digest ran); --model-since forces
a full rebuild without the day-to-day cache.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from hope.scoring.model_epoch import (  # noqa: E402
    load_model_since, load_model_since_raw, model_since_from_shadow, model_since_path,
    write_model_since,
)
from hope.scoring.standing_method import standing_preview  # noqa: E402


def _short(hk: str) -> str:
    return f"{hk[:6]}..{hk[-4:]}" if len(hk) > 12 else hk


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--day", default=datetime.now(timezone.utc).date().isoformat())
    p.add_argument("--ledger-root",
                   default=os.environ.get("SN21_LEDGER_ROOT", "/var/data/sn21/ledger"))
    p.add_argument("--top", type=int, default=25, help="rows to print")
    p.add_argument("--hotkeys", default="", help="comma-separated hotkeys to print in full")
    p.add_argument("--model-since", action="store_true",
                   help="rebuild model_since.json from the shadow ledger first (full walk, no cache)")
    p.add_argument("--no-write", action="store_true", help="print only; do not write the file")
    p.add_argument("--no-controls", action="store_true",
                   help="rank standings only, without running the earning controls "
                        "(no metagraph read; faster; no paid-set comparison)")
    args = p.parse_args(argv)
    day = date.fromisoformat(args.day)
    root = args.ledger_root

    if args.model_since or not os.path.exists(model_since_path(root)):
        mapping = model_since_from_shadow(root, None if args.model_since else load_model_since_raw(root))
        print(f"[preview] model_since.json written for {write_model_since(root, mapping)} hotkeys "
              f"from the shadow ledger", flush=True)
    since = load_model_since(root)
    print(f"[preview] model boundaries on file: {len(since)} hotkeys", flush=True)

    if args.no_controls:
        out = standing_preview(root, day, os.environ, model_since=since or None)
    else:
        # The same inputs the settle stage gives the allocation: identities
        # and alpha from one metagraph read, the floor in force, the day's
        # basket volume. Everything runs in dry-run form; nothing is written
        # but the preview file.
        from hope.scoring.collateral_floor import active_floor
        from scripts.run_daily_loop import _basket_volume
        from scripts.run_daily_pipeline import _metagraph_maps
        maps = _metagraph_maps() or {}
        try:
            vol = int(_basket_volume(day))
        except Exception:  # noqa: BLE001 — a missing volume only affects the D3 flag
            vol = 0
        print(f"[preview] identities {len(maps.get('coldkey') or {})}, alpha "
              f"{len(maps.get('alpha') or {})}, floor {active_floor(day, os.environ):.0f}, "
              f"day volume {vol}; running both allocations (dry run)", flush=True)
        out = standing_preview(root, day, os.environ, model_since=since or None,
                               with_controls=True, coldkey_of=maps.get("coldkey"),
                               alpha_of=maps.get("alpha"),
                               alpha_floor=float(active_floor(day, os.environ)),
                               day_episode_volume=vol)
    if not args.no_write:
        d = os.path.join(root, "standing_preview")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{day}.json")
        with open(path + ".tmp", "w") as f:
            json.dump(out, f, indent=1, sort_keys=True)
        os.replace(path + ".tmp", path)
        print(f"[preview] written {path}", flush=True)

    s = out["summary"]
    print(f"\n=== standing preview {day} (dry run; nothing applied) ===")
    print(f"rule in force: {out['current']}")
    print(f"preview      : { {k: v for k, v in out['preview'].items() if k not in ('model_since', 'previous_model')} }")
    pm = out["preview"].get("previous_model") or {}
    print(f"previous-model discount: {len(pm.get('hotkeys_discounted') or [])} hotkey(s), "
          f"{pm.get('entries_discounted', 0)} entries; factors "
          f"{ {_short(k): v for k, v in list((pm.get('factor') or {}).items())[:10]} }")
    print(f"ranked: current {s['hotkeys_ranked_current']}, preview {s['hotkeys_ranked_preview']}; "
          f"top-20 seats changed {s['top20_seats_changed']}")
    print(f"enter top-20: {[_short(h) for h in s['enter_top20']]}")
    print(f"leave top-20: {[_short(h) for h in s['leave_top20']]}")
    if "paid_now" in s:
        print(f"PAID SET: now {len(s['paid_now'])}, preview {len(s['paid_preview'])}; "
              f"seats changed {s['paid_seats_changed']}")
        print(f"  enter paid: {[_short(h) for h in s['enter_paid']]}")
        print(f"  leave paid: {[_short(h) for h in s['leave_paid']]}")

    rows = out["hotkeys"]
    def _move(hk):
        r = rows[hk]
        if r["current_rank"] is None or r["preview_rank"] is None:
            return 0
        return r["current_rank"] - r["preview_rank"]
    print(f"\n{'hotkey':16} {'now':>5} {'new':>5} {'move':>5} {'now_rel':>9} {'new_rel':>9}  paid now->new")
    ordered = sorted(rows, key=lambda h: (rows[h]["preview_rank"] or 10**6))
    for hk in ordered[:args.top]:
        r = rows[hk]
        paid = ""
        if "paid_now" in r:
            paid = f"  {'Y' if r['paid_now'] else '-'} -> {'Y' if r['paid_preview'] else '-'}"
        print(f"{_short(hk):16} {str(r['current_rank']):>5} {str(r['preview_rank']):>5} "
              f"{_move(hk):>+5} {str(r['current_relative']):>9} {str(r['preview_relative']):>9}{paid}")
    movers = sorted(rows, key=lambda h: -abs(_move(h)))[:10]
    print("\nlargest moves:")
    for hk in movers:
        r = rows[hk]
        print(f"  {_short(hk):16} {r['current_rank']} -> {r['preview_rank']} ({_move(hk):+d})")
    wanted = [h.strip() for h in args.hotkeys.split(",") if h.strip()]
    for hk in wanted:
        print(f"\n{hk}: {rows.get(hk)}  model_since={out['preview']['model_since'].get(hk)}")
    print("\n===PREVIEW-END===", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
