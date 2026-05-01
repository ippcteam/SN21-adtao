"""Generate training dataset for miners.

Pulls episodes + outcomes from HOPE API and packages them as a
downloadable training set. Miners use this to train models before
predicting on live epochs.

Usage:
    python scripts/generate_training_data.py
    python scripts/generate_training_data.py --output data/training
    python scripts/generate_training_data.py --release WR-2026-W18-PUB-E1
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from hope.validator.data_client import HopeDataClient

logger = logging.getLogger(__name__)


async def generate(release_key: str, output_dir: str, api_key: str):
    """Fetch episodes + outcomes and save as training data."""
    client = HopeDataClient(api_key=api_key)

    print(f"Fetching {release_key} from HOPE API...")
    data = await client.fetch_epoch_data(release_key)
    print(f"  Episodes: {data.episode_count}")

    with_t7 = sum(1 for o in data.outcomes if o.t7)
    with_t14 = sum(1 for o in data.outcomes if o.t14)
    print(f"  With t7 outcomes: {with_t7}")
    print(f"  With t14 outcomes: {with_t14}")

    # Build training examples — only episodes with measured outcomes
    training_examples = []
    for ep, outcome in zip(data.episodes, data.outcomes):
        if not outcome.t7 and not outcome.t14:
            continue

        example = {
            "episode_id": ep.episode_metadata.episode_id,
            "input": ep.model_dump(mode="json"),
            "outcome": {
                "t7": outcome.t7.model_dump(mode="json") if outcome.t7 else None,
                "t14": outcome.t14.model_dump(mode="json") if outcome.t14 else None,
            },
            "scoring_metadata": outcome.scoring_metadata.model_dump(mode="json"),
        }
        training_examples.append(example)

    print(f"  Training examples (with outcomes): {len(training_examples)}")

    # Save
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    # Full training set
    with open(out / "training_episodes.json", "w") as f:
        json.dump(training_examples, f, indent=2, default=str)

    # Also save episodes-only (for miners who want raw format)
    episodes_only = [ep.model_dump(mode="json") for ep in data.episodes]
    with open(out / "all_episodes.json", "w") as f:
        json.dump(episodes_only, f, indent=2, default=str)

    # Outcomes-only
    outcomes_only = [o.model_dump(mode="json") for o in data.outcomes if o.t7 or o.t14]
    with open(out / "outcomes.json", "w") as f:
        json.dump(outcomes_only, f, indent=2, default=str)

    # Summary
    summary = {
        "release_key": release_key,
        "total_episodes": data.episode_count,
        "training_episodes": len(training_examples),
        "with_t7_outcomes": with_t7,
        "with_t14_outcomes": with_t14,
        "schema_version": data.schema_version,
        "action_types": {},
    }
    for ex in training_examples:
        actions = ex["input"].get("action_bundle", {}).get("actions", [])
        if actions:
            at = actions[0].get("type", "unknown")
            summary["action_types"][at] = summary["action_types"].get(at, 0) + 1

    with open(out / "summary.json", "w") as f:
        json.dump(summary, f, indent=2)

    print(f"\nSaved to {out}/:")
    print(f"  training_episodes.json  — {len(training_examples)} examples (episodes + outcomes)")
    print(f"  all_episodes.json       — {data.episode_count} episodes (no outcomes)")
    print(f"  outcomes.json           — {len(outcomes_only)} outcomes")
    print("  summary.json            — dataset metadata")

    return training_examples


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Generate training data for miners")
    parser.add_argument("--release", type=str, default=os.environ.get("RELEASE_KEY", ""),
                        help="Release key (or set RELEASE_KEY env var)")
    parser.add_argument("--output", type=str, default="data/training")
    parser.add_argument("--api-key", type=str, default=os.environ.get("HOPE_API_KEY", ""))
    args = parser.parse_args()

    logging.basicConfig(level=logging.WARNING)
    asyncio.run(generate(args.release, args.output, args.api_key))


if __name__ == "__main__":
    main()
