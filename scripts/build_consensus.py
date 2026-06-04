#!/usr/bin/env python3
"""Build the per-cell consensus for a scored epoch — out of band.

Runs after an epoch's outcomes are available. Fetches episodes + outcomes,
scores the supplied (or baseline) predictions to get each miner's skill, then
builds + persists the rolling per-cell consensus artifact the app consumes.

Touches no on-chain / weight-setting code — it only reads scored data and writes
JSON under $SN21_CONSENSUS_DIR (default ~/.sn21/cell_consensus).

Usage:
    # Empirical base-rate track from the live release outcomes:
    python scripts/build_consensus.py --release WR-2026-W23-PUB-E1

    # Add the elite-miner track from a predictions file:
    python scripts/build_consensus.py --release WR-2026-W23-PUB-E1 \
        --predictions predictions.json

    # Fully offline from files:
    python scripts/build_consensus.py --episodes eps.json --outcomes outs.json \
        --predictions preds.json --epoch-id WR-2026-W23-PUB-E1
"""

import argparse
import asyncio
import json
import os
import sys

from hope.protocol.episode import Episode
from hope.protocol.outcomes import Outcome
from hope.protocol.prediction import Prediction
from hope.scoring import EpochScorer
from hope.consensus import compute_and_persist_consensus


def _load_files(episodes_path, outcomes_path, predictions_path):
    with open(episodes_path) as f:
        episodes = [Episode.model_validate(e) for e in json.load(f)]
    with open(outcomes_path) as f:
        outcomes = [Outcome.model_validate(o) for o in json.load(f)]
    predictions = {}
    if predictions_path:
        with open(predictions_path) as f:
            for p in json.load(f):
                mid = p.get("miner_id", "file")
                predictions.setdefault(mid, []).append(Prediction.model_validate(p))
    return episodes, outcomes, predictions


def _load_release(release_key, api_key, api_url):
    from hope.validator.data_client import HopeDataClient

    kwargs = {"api_key": api_key}
    if api_url:
        kwargs["base_url"] = api_url
    client = HopeDataClient(**kwargs)
    loop = asyncio.new_event_loop()
    data = loop.run_until_complete(client.fetch_epoch_data(release_key))
    loop.close()
    return data.episodes, data.outcomes


def _baseline_predictions(episodes):
    from hope.miner.models.baseline import BaselineModel

    model = BaselineModel()
    preds = []
    for ep in episodes:
        try:
            preds.append(model.predict(ep))
        except Exception:
            pass
    return {"baseline_model": preds}


def main():
    ap = argparse.ArgumentParser(description="Build per-cell consensus for a scored epoch")
    ap.add_argument("--release", help="Release/epoch key to fetch from the data API")
    ap.add_argument("--episodes", help="Episodes JSON (offline)")
    ap.add_argument("--outcomes", help="Outcomes JSON (offline)")
    ap.add_argument("--predictions", help="Predictions JSON (elite-miner track)")
    ap.add_argument("--epoch-id", help="Epoch id (offline mode; defaults to --release)")
    ap.add_argument("--api-key", default=os.environ.get("HOPE_API_KEY", ""))
    ap.add_argument("--api-url", default=None)
    ap.add_argument("--run-baseline", action="store_true",
                    help="Use the baseline model as the prediction track")
    ap.add_argument("--publish-n", type=int,
                    default=int(os.environ.get("SN21_CONSENSUS_PUBLISH_N", "20")))
    ap.add_argument("--provisional-n", type=int,
                    default=int(os.environ.get("SN21_CONSENSUS_PROVISIONAL_N", "10")))
    ap.add_argument("--publish-url", default=os.environ.get("SN21_CONSENSUS_PUBLISH_URL"))
    args = ap.parse_args()

    if args.release:
        if not args.api_key:
            print("Error: --release requires HOPE_API_KEY", file=sys.stderr)
            sys.exit(2)
        epoch_id = args.epoch_id or args.release
        episodes, outcomes = _load_release(args.release, args.api_key, args.api_url)
        predictions = (
            _load_files(args.episodes, args.outcomes, args.predictions)[2]
            if args.predictions else {}
        )
    elif args.episodes and args.outcomes:
        epoch_id = args.epoch_id or "offline-epoch"
        episodes, outcomes, predictions = _load_files(
            args.episodes, args.outcomes, args.predictions
        )
    else:
        print("Error: provide --release or --episodes/--outcomes", file=sys.stderr)
        sys.exit(1)

    if args.run_baseline and not predictions:
        predictions = _baseline_predictions(episodes)

    # Score to get each miner's skill (only elite miners feed the prediction track).
    miner_scores = {}
    if predictions:
        miner_scores = EpochScorer().score_epoch(predictions, episodes, outcomes)

    path, artifact = compute_and_persist_consensus(
        epoch_id, episodes, outcomes, predictions, miner_scores,
        publish_n=args.publish_n, provisional_n=args.provisional_n,
        publish_url=args.publish_url,
    )

    out_cells = artifact["cells"]["outcome"]
    pred_cells = artifact["cells"]["prediction"]
    print(f"Consensus written: {path}")
    print(f"  epoch:            {epoch_id}")
    print(f"  episodes/outcomes ingested: {artifact['outcomes_ingested']}")
    print(f"  elite miners:     {artifact['elite_miner_count']}")
    print(f"  outcome cells:    {len(out_cells)} "
          f"({sum(1 for c in out_cells if c['status']=='publish')} publish)")
    print(f"  prediction cells: {len(pred_cells)} "
          f"({sum(1 for c in pred_cells if c['status']=='publish')} publish)")
    if args.publish_url:
        print(f"  published to:     {args.publish_url}")


if __name__ == "__main__":
    main()
