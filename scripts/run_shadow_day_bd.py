"""Run one shadow day against a BD- daily basket (M3 glue validation).

Loads the basket's reserved episodes from the OBI registry, runs the
reference model container against them under the published isolation
flags, and records the day in the shadow ledger. This is the end-to-end
orchestration glue: basket -> model -> predictions -> attested ledger.
Scoring of these predictions follows on the settle clock (day 15/22/36).

Usage:
    python3 scripts/run_shadow_day_bd.py BD-2026-07-27 [--ledger-root DIR]

Requires: docker image sn21-reference-model:v1 (M2), OBI repo alongside
for DB access.
"""

import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
sys.path.insert(0, "/Users/macbookm1/Documents/Projects/obi")

from hope.backtest.container_runner import run_basket_docker  # noqa: E402
from hope.backtest.shadow import ShadowModel, run_shadow_day  # noqa: E402

REFERENCE_IMAGE = "sn21-reference-model:v1"


def load_basket_episodes(release_key: str) -> list[dict]:
    from app.models import get_session
    from sqlalchemy import text as T
    with get_session() as s:
        rows = s.execute(T("""
            SELECT c.id, c.campaign_id_hash, c.action_type, c.action_window_start,
                   c.action_window_end, c.transition_key, c.from_value, c.to_value
            FROM bittensor_episode_candidates c
            JOIN bittensor_release_registry r ON c.reserved_by_release_id = r.id
            WHERE r.release_key = :k
            ORDER BY c.id
        """), {"k": release_key}).fetchall()
    return [
        {
            "episode_id": str(r[0]),
            "campaign_id_hash": r[1],
            "action_type": r[2],
            "action_window_start": str(r[3]),
            "action_window_end": str(r[4]),
            "transition_key": r[5],
            "from_value": float(r[6]) if r[6] is not None else None,
            "to_value": float(r[7]) if r[7] is not None else None,
        }
        for r in rows
    ]


def main():
    if len(sys.argv) < 2 or not sys.argv[1].startswith("BD-"):
        print(__doc__)
        sys.exit(2)
    release_key = sys.argv[1]
    root = "./sn21_ledger"
    if "--ledger-root" in sys.argv:
        root = sys.argv[sys.argv.index("--ledger-root") + 1]

    episodes = load_basket_episodes(release_key)
    print(f"{release_key}: {len(episodes)} reserved episodes loaded")
    if not episodes:
        print("nothing to run — basket empty or key wrong")
        sys.exit(1)

    day = release_key.replace("BD-", "")
    model = ShadowModel(hotkey="reference-v1", image_digest=REFERENCE_IMAGE,
                        admitted_at=str(date.today()))

    def runner(m, eps):
        return run_basket_docker(m.image_digest, eps)

    summary = run_shadow_day(day, episodes, [model], runner, root)
    print(json.dumps(summary, indent=1, default=str))


if __name__ == "__main__":
    main()
