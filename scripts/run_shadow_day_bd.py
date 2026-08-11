"""Run one shadow day against a BD- daily basket.

Loads the basket's reserved episodes from the operator's release registry,
runs EVERY ADMITTED MODEL against them under the published isolation flags,
and records the day in the shadow ledger. This is the end-to-end
orchestration glue: basket -> models -> predictions -> attested ledger.
Scoring of these predictions follows on the settle clock (day 15/22/36).

Usage:
    python3 scripts/run_shadow_day_bd.py BD-2026-07-27 \\
        [--ledger-root DIR] [--models admitted|reference|both] [--dry-run]

WHAT CHANGED, AND WHY IT MATTERED

    This script used to run exactly one container — a hardcoded
    `sn21-reference-model:v1`, under the fixed hotkey "reference-v1" — and
    never consulted the model registry at all. So while miners committed
    model digests on chain as the spec instructs, nothing ever executed one,
    and the shadow ledger contained a single house model every day.

    MINER_MODEL_SPEC §4 promises "the subnet executes your container against
    the basket". The registry, the digest-pinned pull and the multi-model
    ledger writer all existed and were tested; only this call site was wired
    to one image. It now builds the registry from chain, filtered by the
    admission verdicts the intake sweep writes.

THE REFERENCE MODEL STILL RUNS

    It is the published baseline and the thing every miner starts from, so it
    stays in the ledger as a comparison line. It is exempt from the copy rules
    for the same reason. `--models` controls this if a run needs one or the
    other alone.

Requires: a docker host, the operator platform package for DB access, and an
admission verdict feed produced by scripts/run_model_intake.py.
"""

import argparse
import json
import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
# The outcomes/basket readers below need the operator's data platform package
# on the path. It is not part of this repository (it reads the operator's
# private database), so its location is configuration.
_PLATFORM_PATH = os.environ.get("SN21_PLATFORM_PATH")
if _PLATFORM_PATH:
    sys.path.insert(0, _PLATFORM_PATH)

from hope.backtest.chain_commitments import (
    as_registry_reader,
    bulk_model_commitments,
)
from hope.backtest.container_runner import run_basket_docker
from hope.backtest.intake_runner import load_admitted
from hope.backtest.model_registry import build_registry
from hope.backtest.shadow import ShadowModel, run_shadow_day

REFERENCE_IMAGE = "sn21-reference-model:v1"
REFERENCE_HOTKEY = "reference-v1"


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


def admitted_models(root: str, network: str, netuid: int,
                    as_of: str) -> tuple[list, dict]:
    """The admitted miner models, resolved from chain + the verdict feed.

    A digest is runnable only when the chain proves WHO submitted it and the
    attested verdict feed proves it PASSED. Neither alone is sufficient: the
    chain cannot say whether a model beat the baseline, and a verdict without
    a live commitment belongs to a hotkey that has moved on.
    """
    import bittensor as bt

    subtensor = bt.Subtensor(network=network)
    metagraph = subtensor.metagraph(netuid=netuid)
    hotkeys = list(metagraph.hotkeys)

    admitted = load_admitted(root)
    commitments = bulk_model_commitments(subtensor, netuid, hotkeys)
    registry = build_registry(hotkeys, as_registry_reader(commitments),
                              admitted, as_of)
    stats = {
        "registered_hotkeys": len(hotkeys),
        "model_commitments": len(commitments),
        "admitted_digests_on_file": len(admitted),
        "active": registry["active_count"],
        "pending_admission": registry["pending_count"],
    }
    return list(registry["active"].values()), stats


def _parse_args(argv):
    p = argparse.ArgumentParser(description="Run one BD- shadow day")
    p.add_argument("release_key")
    p.add_argument("--ledger-root", default="./sn21_ledger")
    p.add_argument("--models", choices=("admitted", "reference", "both"),
                   default="both")
    p.add_argument("--network", default=os.environ.get("SN21_NETWORK", "finney"))
    p.add_argument("--netuid", type=int,
                   default=int(os.environ.get("SN21_NETUID", "21")))
    p.add_argument("--dry-run", action="store_true",
                   help="resolve basket and models; execute nothing")
    return p.parse_args(argv)


def main():
    argv = sys.argv[1:]
    if not argv or not argv[0].startswith("BD-"):
        print(__doc__)
        sys.exit(2)
    args = _parse_args(argv)
    root = args.ledger_root

    episodes = load_basket_episodes(args.release_key)
    print(f"{args.release_key}: {len(episodes)} reserved episodes loaded")
    if not episodes:
        print("nothing to run — basket empty or key wrong")
        sys.exit(1)

    day = args.release_key.replace("BD-", "")
    as_of = str(date.today())

    models = []
    if args.models in ("admitted", "both"):
        miner_models, stats = admitted_models(root, args.network,
                                              args.netuid, as_of)
        models.extend(miner_models)
        print(f"registry: {stats}")
        if not miner_models:
            # Loud, because it is indistinguishable in the ledger from a day
            # nobody submitted: no admitted model means the intake sweep has
            # not run or admitted nobody, not that miners are absent.
            print("WARNING: no admitted miner models — run "
                  "scripts/run_model_intake.py first")
    if args.models in ("reference", "both"):
        models.append(ShadowModel(hotkey=REFERENCE_HOTKEY,
                                  image_digest=REFERENCE_IMAGE,
                                  admitted_at=as_of))

    print(f"models to run: {len(models)}")
    for m in models:
        print(f"   {m.hotkey[:12]:<14} {m.image_digest}")

    if args.dry_run:
        print("\ndry run — basket and registry resolved, nothing executed")
        return

    def runner(m, eps):
        return run_basket_docker(m.image_digest, eps)

    summary = run_shadow_day(day, episodes, models, runner, root)
    print(json.dumps(summary, indent=1, default=str))


if __name__ == "__main__":
    main()
