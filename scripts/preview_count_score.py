"""Read-only: the standing and the paid set if the small-campaign count score counted today.

  python3 -m scripts.preview_count_score [--day YYYY-MM-DD] [--volume 1655] [--top 30]

SN21_COUNT_SCORE_FROM (5 Dec 2026) blends the conversion-count score into each
small-campaign result's entry score at settle (hope.scoring.count_score). This
re-scores the results in the standing window that way, from the receipts on the
ledger and the settled outcomes over HTTP (the run's own source), then runs the
day's allocation twice - as it is, and with the blend - with persist=False, and
prints who moves, who enters or leaves the paid set, and by how much.

Writes nothing. Needs the executor's environment (SN21_OUTCOMES_API_URL / _KEY).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from hope.scoring import count_score
from hope.scoring import standing_method as SM
from hope.scoring.settle_day_flow import http_outcomes_provider
from hope.validator import daily_stream_weights as W


def small_outcomes(day: date) -> dict:
    """(episode, horizon) -> (expected, actual) for small-campaign results."""
    url, key = os.environ["SN21_OUTCOMES_API_URL"], os.environ["SN21_OUTCOMES_API_KEY"]
    out = {}
    for o in http_outcomes_provider(url, key, lookback_days=None)(day):
        if o.conversions_delta_pct is None and o.expected_conversions:
            out[(str(o.episode_id), int(o.horizon_days))] = (
                float(o.expected_conversions), float(o.actual_conversions or 0.0))
    return out


def adjusted_scores(root: str, day: date, window_days: int, small: dict) -> dict:
    """(episode, horizon, miner) -> the entry score with the count part blended in."""
    adj = {}
    for _, path in SM._receipt_files(root, day, window_days):
        try:
            entries = SM._parse_receipt_entries(path)
        except (OSError, ValueError):
            continue
        for e in entries:
            if not isinstance(e, dict) or e.get("prediction") is None:
                continue
            k = (str(e.get("episode_id")), int(e.get("horizon_days") or 0))
            if k not in small or e.get("score") is None:
                continue
            exp, act = small[k]
            adj[(*k, str(e.get("miner")))] = count_score.blend(
                float(e["score"]), count_score.count_component(e["prediction"], exp, act))
        del entries
    return adj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", default=datetime.now(timezone.utc).date().isoformat())
    ap.add_argument("--volume", type=int, default=1655)
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--root", default=os.environ.get("SN21_LEDGER_ROOT", "/var/data/sn21/ledger"))
    a = ap.parse_args()
    day = date.fromisoformat(a.day)
    window = SM.window_in_force(os.environ, day)

    small = small_outcomes(day)
    adj = adjusted_scores(a.root, day, window, small)
    print(f"{day}: {len(small)} small-campaign results settled, {len(adj)} entries re-scored "
          f"in the {window}-day window", flush=True)

    SM._CACHE.clear()
    before = W.allocation_from_ledger(a.root, day, a.volume, persist=False)

    original = SM._entries_of

    def with_count(path):
        rows = original(path)
        for r in rows:
            k = (str(r.get("episode_id")), int(r.get("horizon_days") or 0), str(r.get("miner")))
            if k in adj:
                r["score"] = adj[k]
        return rows

    SM._entries_of = with_count
    SM._CACHE.clear()
    try:
        after = W.allocation_from_ledger(a.root, day, a.volume, persist=False)
    finally:
        SM._entries_of = original

    def ranks(alloc):
        st = (alloc.collapse_audit or {}).get("standings") or {}
        return {hk: v.get("rank") for hk, v in st.items() if isinstance(v, dict) and v.get("rank")}

    rb, ra = ranks(before), ranks(after)
    paid_b = {hk for hk, w in (before.weights or {}).items() if w > 0}
    paid_a = {hk for hk, w in (after.weights or {}).items() if w > 0}
    moves = sorted(((ra.get(hk, 999) - rb.get(hk, 999), hk) for hk in set(rb) | set(ra)), key=lambda x: -abs(x[0]))
    print(f"ranked: {len(rb)} -> {len(ra)} | paid: {len(paid_b)} -> {len(paid_a)} | "
          f"enter paid set: {len(paid_a - paid_b)}, leave: {len(paid_b - paid_a)}")
    print(f"moved: {sum(1 for d, _ in moves if d)} hotkeys; by 10+ places: {sum(1 for d, _ in moves if abs(d) >= 10)}")
    for hk in sorted(paid_a ^ paid_b, key=lambda h: rb.get(h, 999)):
        print(f"  {'ENTERS' if hk in paid_a else 'LEAVES'} {hk[:10]} rank {rb.get(hk)} -> {ra.get(hk)} | "
              f"weight {round((before.weights or {}).get(hk, 0), 4)} -> {round((after.weights or {}).get(hk, 0), 4)}")
    print("largest moves:")
    for d, hk in moves[:a.top]:
        if d:
            print(f"  {hk[:10]} rank {rb.get(hk)} -> {ra.get(hk)} ({d:+d})")
    print(json.dumps({"day": str(day), "small_results": len(small), "rescored": len(adj),
                      "paid_before": len(paid_b), "paid_after": len(paid_a),
                      "enter": len(paid_a - paid_b), "leave": len(paid_b - paid_a)}))


if __name__ == "__main__":
    main()
