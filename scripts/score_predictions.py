"""Offline scoring tool — miners can verify their scores locally.

Usage:
    # Score predictions against a live release
    python scripts/score_predictions.py --release WR-2026-W18-PUB-E1

    # Score from local files
    python scripts/score_predictions.py --episodes episodes.json --predictions predictions.json --outcomes outcomes.json

    # Run baseline model and score it
    python scripts/score_predictions.py --release WR-2026-W18-PUB-E1 --run-baseline
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# Add parent to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from hope.protocol.episode import Episode
from hope.protocol.prediction import Prediction
from hope.protocol.outcomes import Outcome
from hope.scoring import EpochScorer, ScoringWeights


def load_from_training_file(path: str):
    """Load episodes + outcomes from a `training_episodes.json` bundle.

    The training file is a list of `{"input": <episode>, "outcome": {...}}`.
    We synthesise an Outcome per example whose t7 / t14 fields carry the
    measured deltas. Returns `(episodes, file_predictions, outcomes)` to
    match the shape of `load_from_files` so the calling code is uniform.
    """
    with open(path) as f:
        examples = json.load(f)
    episodes: list[Episode] = []
    outcomes: list[Outcome] = []
    for ex in examples:
        ep = Episode.model_validate(ex["input"])
        episodes.append(ep)
        oc = ex.get("outcome") or {}
        # Outcome.model_validate is permissive about missing horizons.
        outcomes.append(Outcome.model_validate({
            "episode_id": ep.episode_metadata.episode_id,
            "t7": oc.get("t7"),
            "t14": oc.get("t14"),
        }))
    return episodes, {}, outcomes


def load_from_files(episodes_path: str, predictions_path: str, outcomes_path: str):
    """Load episodes, predictions, and outcomes from JSON files."""
    with open(episodes_path) as f:
        episodes_raw = json.load(f)
    with open(predictions_path) as f:
        predictions_raw = json.load(f)
    with open(outcomes_path) as f:
        outcomes_raw = json.load(f)

    episodes = [Episode.model_validate(ep) for ep in episodes_raw]
    outcomes = [Outcome.model_validate(o) for o in outcomes_raw]

    predictions = {}
    for p in predictions_raw:
        miner_id = p.get("miner_id", "unknown")
        pred = Prediction.model_validate(p)
        predictions.setdefault(miner_id, []).append(pred)

    return episodes, predictions, outcomes


async def load_from_release(release_key: str, api_key: str = "", api_url: str | None = None):
    """Fetch episodes and outcomes from the live data API."""
    from hope.validator.data_client import HopeDataClient

    kwargs = {"api_key": api_key}
    if api_url:
        kwargs["base_url"] = api_url
    client = HopeDataClient(**kwargs)
    data = await client.fetch_epoch_data(release_key)
    return data.episodes, data.outcomes


def run_baseline(episodes: list[Episode]) -> dict[str, list[Prediction]]:
    """Run the baseline model on all episodes."""
    from hope.miner.models.baseline import BaselineModel

    model = BaselineModel()
    predictions = []

    for ep in episodes:
        try:
            pred = model.predict(ep)
            predictions.append(pred)
        except Exception as e:
            print(f"  Warning: failed to predict {ep.episode_metadata.episode_id}: {e}")

    return {"baseline_model": predictions}


def main():
    parser = argparse.ArgumentParser(description="the operator SN21 Offline Scoring Tool")
    parser.add_argument("--release", type=str,
                        help="Release key to fetch from operator data API. "
                             "Requires HOPE_API_KEY (operator-issued) — see --help for "
                             "the offline-only path that skips this.")
    parser.add_argument("--episodes", type=str, help="Path to episodes JSON file")
    parser.add_argument("--predictions", type=str, help="Path to predictions JSON file")
    parser.add_argument("--outcomes", type=str, help="Path to outcomes JSON file")
    parser.add_argument("--run-baseline", action="store_true",
                        help="Run baseline model and score it")
    parser.add_argument("--training-data", type=str, default=None,
                        help="Path to training_episodes.json — uses bundled offline "
                             "data instead of fetching from the operator API. "
                             "Default fallback: data/training/training_episodes.json "
                             "when --release is given without HOPE_API_KEY.")
    parser.add_argument("--api-key", type=str, default=os.environ.get("HOPE_API_KEY", ""),
                        help="Operator data API key (or HOPE_API_KEY env var). "
                             "ONLY needed for --release fetches against the live API. "
                             "Most miner self-scoring should use --training-data instead.")
    parser.add_argument("--api-url", type=str, default=None,
                        help="Override data API base URL")
    parser.add_argument("--verbose", action="store_true", help="Show per-episode scores")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Resolve a sensible training_data fallback for offline scoring when
    # --release is given without an API key. Miners shouldn't need an
    # operator-issued key to self-score against the bundled examples.
    if args.release and not args.api_key and not args.training_data:
        from pathlib import Path
        bundled = Path(__file__).resolve().parent.parent / "data" / "training" / "training_episodes.json"
        if bundled.exists():
            print(
                f"\n⚠️  No HOPE_API_KEY set — falling back to bundled training data "
                f"at {bundled}.\n"
                f"   This scores against the {len(json.load(open(bundled)))}-example "
                f"sample dataset, not the live release.\n"
                f"   Pass --training-data <path> to override or set HOPE_API_KEY for "
                f"live data.\n"
            )
            args.training_data = str(bundled)
            args.release = None  # consumed; switch to local-file path

    # Load data
    if args.training_data:
        print(f"Loading offline training data from {args.training_data}...")
        episodes, file_predictions, outcomes = load_from_training_file(args.training_data)
        print(f"  Episodes: {len(episodes)}, outcomes: {len(outcomes)}")
    elif args.release:
        if not args.api_key:
            print(
                "Error: --release fetches from the operator API, which requires HOPE_API_KEY.\n"
                "       For miner self-scoring, use --training-data <path> instead "
                "(or omit it to use the bundled fallback).",
                file=sys.stderr,
            )
            sys.exit(2)
        print(f"Fetching data from data API for {args.release}...")
        loop = asyncio.new_event_loop()
        episodes, outcomes = loop.run_until_complete(
            load_from_release(args.release, args.api_key, args.api_url)
        )
        loop.close()
        print(f"  Episodes: {len(episodes)}")
        print(f"  Outcomes with t7: {sum(1 for o in outcomes if o.t7)}")
        print(f"  Outcomes with t14: {sum(1 for o in outcomes if o.t14)}")
    elif args.episodes and args.outcomes:
        episodes, file_predictions, outcomes = load_from_files(
            args.episodes, args.predictions or args.episodes, args.outcomes
        )
    else:
        print("Error: provide --release, --episodes/--outcomes, or --training-data")
        sys.exit(1)

    # Get predictions
    if args.run_baseline or args.release:
        print("\nRunning baseline model...")
        all_predictions = run_baseline(episodes)
        for mid, preds in all_predictions.items():
            print(f"  {mid}: {len(preds)} predictions generated")
    elif args.predictions:
        all_predictions = file_predictions
    else:
        print(
            "Error: nothing to score.\n"
            "  Pass --run-baseline to score the bundled baseline model against\n"
            "  the loaded outcomes, or --predictions /path/to/preds.json to\n"
            "  score your own predictions file.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Score
    print("\nScoring...")
    scorer = EpochScorer()
    scores = scorer.score_epoch(all_predictions, episodes, outcomes)

    # Results
    print(f"\n{'='*60}")
    print("  SCORING RESULTS")
    print(f"{'='*60}")

    for miner_id, score in scores.items():
        print(f"\n  Miner: {miner_id}")
        print(f"  Episodes scored: {score.episodes_scored}")
        print(f"  Raw score:       {score.raw_score:.4f}")
        print(f"  Skill score:     {score.skill_score:.4f}")
        print(f"  Null penalty:    {score.null_penalty:.4f}")
        print(f"  Final score:     {score.final_score:.4f}")

        if args.verbose and score.episode_scores:
            print("\n  Per-episode breakdown:")
            for ep_score in score.episode_scores:
                print(f"    {ep_score.episode_id}: {ep_score.weighted_total:.4f} "
                      f"(res={ep_score.measurement_resolution})")
                for h_key, h_score in ep_score.horizon_scores.items():
                    print(f"      t{h_key}: quantile={h_score.quantile_accuracy:.3f} "
                          f"calibration={h_score.calibration:.3f} "
                          f"directional={h_score.directional:.3f} "
                          f"goal={h_score.goal_accuracy:.3f} "
                          f"total={h_score.weighted_total:.3f}")

    print(f"\n{'='*60}")

    # Scoring weights used
    weights = ScoringWeights()
    print("\n  Scoring weights:")
    print(f"    Quantile accuracy: {weights.quantile_accuracy}")
    print(f"    Calibration:       {weights.calibration}")
    print(f"    Directional:       {weights.directional}")
    print(f"    Goal accuracy:     {weights.goal_accuracy}")
    print(f"    Valid ranges:      {weights.validate_ranges()}")


if __name__ == "__main__":
    main()
