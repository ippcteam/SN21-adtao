#!/usr/bin/env python3
"""Run the shadow day for EVERY basket that is missing one, then heartbeat.

WHY THIS EXISTS (2026-08-03). The shadow clock is a launch gate — >= 7 scored
days before weights switch. It sat at 4/7 for days because Aug 1 and Aug 2 were
never run: shadow_daily.sh is operator-triggered, nothing schedules it, and
nothing notices when it stops. Our own notes read "day 7 ~ Aug 4" as though the
countdown ran itself.

Two things make automation actually reliable, and shadow_daily.sh has neither:

  1. CATCH-UP, not just yesterday. shadow_daily.sh runs exactly one named day,
     so a day missed for any reason is lost until a human spots it. This walks
     every BD- basket without a ledger entry, oldest first. A missed day
     self-heals on the next run.

  2. A HEARTBEAT. The ledger is local files, so nothing outside this machine can
     tell the clock stopped — including when the machine is simply off, which is
     the failure we actually had. Reporting to Postgres lets the operator platform run a dead-man
     alarm (bittensor.shadow_clock_check).

The ledger stays the source of truth: attested, hash-chained, and never derived
from the heartbeat. The heartbeat is a report ABOUT it.

Idempotent by construction — a day with a ledger entry is skipped, and the
heartbeat upserts on shadow_day. Safe to run every few minutes.

    python3 scripts/shadow_catchup.py [--ledger-root DIR] [--limit N] [--dry-run]
"""

import argparse
import json
import os
import sys
import traceback
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
# The outcomes/basket readers below need the operator's data platform package
# on the path. It is not part of this repository (it reads the operator's
# private database), so its location is configuration.
_PLATFORM_PATH = os.environ.get("SN21_PLATFORM_PATH")
if _PLATFORM_PATH:
    sys.path.insert(0, _PLATFORM_PATH)

from hope.backtest.container_runner import run_basket_docker  # noqa: E402
from hope.backtest.shadow import ShadowModel, run_shadow_day  # noqa: E402

REFERENCE_IMAGE = "sn21-reference-model:v1"


def basket_days(session):
    """Every BD- basket with episodes, oldest first."""
    from sqlalchemy import text as T
    rows = session.execute(T("""
        SELECT release_key, episode_count FROM bittensor_release_registry
        WHERE release_key LIKE 'BD-%' AND coalesce(episode_count,0) > 0
        ORDER BY release_key
    """)).fetchall()
    return [(r[0], r[1]) for r in rows]


def ledger_days(root):
    d = os.path.join(root, "shadow")
    if not os.path.isdir(d):
        return set()
    # A directory alone is not a run — an interrupted attempt can leave one
    # behind with no prediction file, and treating that as done would skip the
    # day forever.
    return {name for name in os.listdir(d)
            if os.path.isfile(os.path.join(d, name, "reference-v1.jsonl"))}


def load_basket_episodes(session, release_key):
    from sqlalchemy import text as T
    rows = session.execute(T("""
        SELECT c.id, c.campaign_id_hash, c.action_type, c.action_window_start,
               c.action_window_end, c.transition_key, c.from_value, c.to_value
        FROM bittensor_episode_candidates c
        JOIN bittensor_release_registry r ON c.reserved_by_release_id = r.id
        WHERE r.release_key = :k ORDER BY c.id
    """), {"k": release_key}).fetchall()
    return [{
        "episode_id": str(r[0]), "campaign_id_hash": r[1], "action_type": r[2],
        "action_window_start": str(r[3]), "action_window_end": str(r[4]),
        "transition_key": r[5],
        "from_value": float(r[6]) if r[6] is not None else None,
        "to_value": float(r[7]) if r[7] is not None else None,
    } for r in rows]


def heartbeat(session, day, episodes, models_run, predictions, ok, root, detail):
    """Upsert on shadow_day — a re-run must update, never accumulate."""
    from sqlalchemy import text as T
    session.execute(T("""
        INSERT INTO sn21_shadow_heartbeat
            (shadow_day, ran_at, episodes, models_run, predictions, ok,
             ledger_root, detail)
        VALUES (:d, now(), :e, :m, :p, :ok, :root, CAST(:detail AS json))
        ON CONFLICT (shadow_day) DO UPDATE SET
            ran_at = now(), episodes = EXCLUDED.episodes,
            models_run = EXCLUDED.models_run, predictions = EXCLUDED.predictions,
            ok = EXCLUDED.ok, ledger_root = EXCLUDED.ledger_root,
            detail = EXCLUDED.detail
    """), {"d": day, "e": episodes, "m": models_run, "p": predictions,
           "ok": ok, "root": root, "detail": json.dumps(detail, default=str)})
    session.commit()


def reconcile_heartbeats(session, root, have):
    """Heartbeat any ledger day that has no row yet. Runs NOTHING.

    Needed because the ledger predates the heartbeat table: without this the
    dead-man alarm would report six already-completed days as overdue on its
    first run, and an alarm that cries wolf on day one gets ignored by day two.

    Reads the counts out of the ledger itself rather than re-running the model —
    the ledger is the source of truth and this is a report about it.
    """
    from sqlalchemy import text as T
    existing = {str(r[0]) for r in session.execute(
        T("SELECT shadow_day FROM sn21_shadow_heartbeat")).fetchall()}
    added = 0
    for day in sorted(have):
        if day in existing:
            continue
        path = os.path.join(root, "shadow", day, "reference-v1.jsonl")
        episodes = preds = 0
        ok = False
        try:
            with open(path) as f:
                for line in f:
                    rec = json.loads(line)
                    episodes = rec.get("episodes_in") or 0
                    preds = rec.get("predictions_out") or 0
                    ok = bool(rec.get("ok")) and preds > 0
        except Exception as e:  # noqa: BLE001
            print(f"  reconcile {day}: unreadable ledger ({e}) — skipped",
                  file=sys.stderr)
            continue
        heartbeat(session, day, episodes, 1, preds, ok, root,
                  {"source": "reconciled from ledger"})
        added += 1
    if added:
        print(f"reconciled {added} existing ledger day(s) into the heartbeat")
    return added


def main(root, limit, dry_run):
    from app.models import get_session

    with get_session() as s:
        baskets = basket_days(s)
    have = ledger_days(root)
    if not dry_run:
        with get_session() as s:
            reconcile_heartbeats(s, root, have)
    missing = [(k, n) for k, n in baskets if k.replace("BD-", "") not in have]

    # FORWARD-ONLY. Never retroactively add days to a launch gate.
    #
    # The first version of this would have run BD-2026-07-25, 07-26 and 07-30 —
    # all DELIBERATELY excluded (pre-cap: their episodes were clustered under
    # the uncapped rules, so they are not the population miners are scored on).
    # Backfilling them would have quietly inflated a 7-day gate with days that
    # do not belong in it, and the gate would have "passed" on the strength of
    # episodes built to different rules.
    #
    # So automation only moves the clock FORWARD, past the newest day already
    # in the ledger. Historical gaps are a human decision and are reported, not
    # acted on — that is the whole point of the rule: an unattended process must
    # not be able to make a launch gate easier to satisfy.
    frontier = max(have) if have else None
    if frontier:
        behind = [(k, n) for k, n in missing if k.replace("BD-", "") < frontier]
        missing = [(k, n) for k, n in missing if k.replace("BD-", "") > frontier]
    else:
        behind = []

    print(f"baskets: {len(baskets)} | ledger days: {len(have)} | "
          f"frontier: {frontier} | to run: {len(missing)}")
    if behind:
        print(f"NOT auto-running {len(behind)} historical gap(s) — "
              + ", ".join(k for k, _ in behind))
        print("  these predate the ledger frontier. If any SHOULD count toward "
              "the gate, run it explicitly with run_shadow_day_bd.py; several "
              "are known pre-cap days that deliberately do not.")
    if not missing:
        print("shadow clock is up to date — nothing to run")
        return 0
    if limit:
        missing = missing[:limit]
    print("to run: " + ", ".join(k for k, _ in missing))
    if dry_run:
        print("--dry-run: nothing executed")
        return 0

    failures = 0
    for release_key, expected in missing:
        day = release_key.replace("BD-", "")
        try:
            with get_session() as s:
                episodes = load_basket_episodes(s, release_key)
            if not episodes:
                print(f"{release_key}: 0 episodes in registry — skipping "
                      f"(episode_count said {expected})")
                continue
            model = ShadowModel(hotkey="reference-v1",
                                image_digest=REFERENCE_IMAGE,
                                admitted_at=str(date.today()))
            summary = run_shadow_day(
                day, episodes, [model],
                lambda m, eps: run_basket_docker(m.image_digest, eps), root)
            res = (summary.get("results") or {}).get("reference-v1", {})
            preds = res.get("predictions", 0) or 0
            ok = bool(res.get("ok")) and preds > 0
            # preds > 0 deliberately: the hostile-model probe showed a broken
            # model exits CLEANLY with zero predictions, so ok=True alone would
            # record a day that contributes nothing to the gate as healthy.
            with get_session() as s:
                heartbeat(s, day, len(episodes), summary.get("models_run", 0),
                          preds, ok, root, summary)
            print(f"{release_key}: episodes={len(episodes)} predictions={preds} "
                  f"ok={ok}")
            if not ok:
                failures += 1
        except Exception:
            failures += 1
            print(f"{release_key}: FAILED\n{traceback.format_exc()}",
                  file=sys.stderr)
            # Keep going: one bad day must not block the days after it, or a
            # single poisoned basket stalls the clock exactly like before.

    after = len(ledger_days(root))
    print(f"\nledger days now: {after} (gate needs 7)")
    return 1 if failures else 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger-root", default="./sn21_ledger")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    sys.exit(main(a.ledger_root, a.limit, a.dry_run))
