"""Model the before/after of a type-weight table on the standings.

The amendment promise: Rob sees the exact ranking change before anything goes
live. This recomputes every miner's standing twice from the same scored
entries — once with all weights 1.0 (current rules), once with the candidate
table — and prints the movement. It writes nothing and touches no ledger.

The recomputation mirrors the standing formula (horizon blend x age decay
over the window); it reads the same receipts and tkeys maps the builder uses,
so the model and the measurement cannot drift apart.

Usage on the executor:

    python -m scripts.model_type_weight_impact \
        --table /var/data/sn21/ledger/type_weights_draft.json --days 35
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from hope.scoring.daily_score_flow import horizon_entry_weight   # noqa: E402
from hope.scoring.episode_average import (                       # noqa: E402
    DEFAULT_HALF_LIFE_DAYS,
    DEFAULT_WINDOW_DAYS,
)
from hope.scoring.type_weights import TypeWeightTable            # noqa: E402
from scripts.build_type_weight_table import (                    # noqa: E402
    _receipt_entries_from_dir,
    _receipt_entries_from_mirror,
)
from scripts.run_daily_pipeline import _transition_key_provider  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--ledger-root",
                   default=os.environ.get("SN21_LEDGER_ROOT",
                                          "/var/data/sn21/ledger"))
    p.add_argument("--table", required=True)
    p.add_argument("--receipts-dir", default=None)
    p.add_argument("--mirror-url",
                   default="https://hope-ads-backend.onrender.com")
    p.add_argument("--days", type=int, default=DEFAULT_WINDOW_DAYS)
    p.add_argument("--as-of", default=str(date.today()))
    p.add_argument("--top", type=int, default=25)
    args = p.parse_args()

    with open(args.table) as fh:
        table = TypeWeightTable.from_json(json.load(fh))

    y, m, dd = (int(x) for x in args.as_of.split("-"))
    as_of = date(y, m, dd)
    days = [str(as_of - timedelta(days=i)) for i in range(args.days + 1)]

    source = (_receipt_entries_from_dir(args.receipts_dir, days)
              if args.receipts_dir
              else _receipt_entries_from_mirror(args.mirror_url, days))
    provider = _transition_key_provider(args.ledger_root)

    base_num = defaultdict(float); base_den = defaultdict(float)
    new_num = defaultdict(float); new_den = defaultdict(float)
    seen: set = set()

    for d, entries in source:
        if not entries:
            continue
        ids = sorted({str(e.get("episode_id")) for e in entries})
        tmap = provider(ids)
        for e in entries:
            key = (str(e.get("miner")), str(e.get("episode_id")),
                   str(e.get("horizon_days")))
            if key in seen:
                continue
            seen.add(key)
            # Age from the ENTRY's own finalized_on, not the receipt day: a
            # receipt carries the settle batch, which spans more than one
            # finalisation day, and the standing decays by entry age.
            fo = str(e.get("finalized_on") or d)[:10]
            try:
                yy, mm, ddd = (int(x) for x in fo.split("-"))
                age = (as_of - date(yy, mm, ddd)).days
            except Exception:  # noqa: BLE001 — unparseable date -> skip entry
                continue
            if age < 0 or age > DEFAULT_WINDOW_DAYS:
                continue
            decay = 0.5 ** (age / DEFAULT_HALF_LIFE_DAYS)
            m_ = str(e.get("miner")); s = float(e.get("score"))
            hw = horizon_entry_weight(int(e.get("horizon_days", 7)))
            w = hw * decay
            base_num[m_] += w * s; base_den[m_] += w
            tw = table.weight_for(tmap.get(str(e.get("episode_id"))))
            new_num[m_] += w * tw * s; new_den[m_] += w * tw

    base = {m_: base_num[m_] / base_den[m_] for m_ in base_num if base_den[m_]}
    new = {m_: new_num[m_] / new_den[m_] for m_ in new_num if new_den[m_]}
    b_rank = {m_: i for i, (m_, _) in enumerate(
        sorted(base.items(), key=lambda kv: -kv[1]), 1)}
    n_rank = {m_: i for i, (m_, _) in enumerate(
        sorted(new.items(), key=lambda kv: -kv[1]), 1)}

    print(f"miners: {len(base)}   entries: {len(seen)}   "
          f"as_of: {as_of}   table: {table.status}/{table.params_version}")
    print(f"\n| miner | now | with weights | move | score now | score after |")
    print("|---|---|---|---|---|---|")
    for m_, _ in sorted(base.items(), key=lambda kv: -kv[1])[:args.top]:
        print(f"| {m_[:14]}.. | #{b_rank[m_]} | #{n_rank.get(m_, '-')} | "
              f"{b_rank[m_] - n_rank.get(m_, b_rank[m_]):+d} | "
              f"{base[m_]:.6f} | {new.get(m_, 0):.6f} |")

    moved = sum(1 for m_ in base if b_rank[m_] != n_rank.get(m_))
    lead_change = (min(b_rank, key=b_rank.get) != min(n_rank, key=n_rank.get))
    print(f"\nranks changed: {moved}/{len(base)}   "
          f"leader changes: {'YES' if lead_change else 'NO'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
