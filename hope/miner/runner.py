"""Miner Runner — main entry point for HOPE SN21 miners.

Handles the complete miner lifecycle:
1. Connect to the validator
2. Fetch episodes for the current epoch
3. Run the prediction model on each episode
4. Submit predictions back to the validator

Usage:
    # With baseline model
    hope-miner --validator-url http://localhost:8080 --hotkey my_hotkey

    # With custom model
    from hope.miner.runner import MinerRunner
    from my_model import MyPredictionEngine
    runner = MinerRunner(model=MyPredictionEngine(), ...)
    await runner.run_epoch("WR-2026-W17-PUB-E1")
"""

from __future__ import annotations

import asyncio
import logging
import time

from hope.miner.episode_client import EpisodeClient
from hope.miner.prediction_client import PredictionClient
from hope.miner.prediction_engine import PredictionEngine
from hope.miner.models.baseline import BaselineModel

logger = logging.getLogger(__name__)


class MinerRunner:
    """Main miner runner."""

    def __init__(
        self,
        model: PredictionEngine | None = None,
        hotkey: str = "miner_default",
        validator_url: str = "http://localhost:8080",
    ):
        self.model = model or BaselineModel()
        self.hotkey = hotkey
        self.validator_url = validator_url
        self.episode_client = EpisodeClient(hotkey=hotkey)
        self.prediction_client = PredictionClient(hotkey=hotkey)

    async def run_epoch(self, epoch_id: str) -> dict:
        """Run the full miner cycle for one epoch.

        1. Fetch all episodes
        2. Generate predictions
        3. Submit predictions

        Returns submission result.
        """
        logger.info(f"Starting epoch {epoch_id} with model {self.model.name}")
        self.model.on_epoch_start(epoch_id, 0)

        # Step 1: Fetch episodes
        logger.info(f"Fetching episodes from {self.validator_url}...")
        start = time.time()
        episodes = await self.episode_client.fetch_all_episodes(
            self.validator_url, epoch_id
        )
        fetch_time = time.time() - start
        logger.info(f"Fetched {len(episodes)} episodes in {fetch_time:.1f}s")

        # Step 2: Generate predictions
        logger.info(f"Generating predictions with {self.model.name}...")
        start = time.time()
        predictions = []
        for ep in episodes:
            try:
                pred = self.model.predict(ep)
                predictions.append(pred)
            except Exception as e:
                logger.warning(f"Failed to predict {ep.episode_metadata.episode_id}: {e}")

        predict_time = time.time() - start
        logger.info(f"Generated {len(predictions)} predictions in {predict_time:.1f}s")

        # Step 3: Submit predictions
        logger.info("Submitting predictions...")
        result = await self.prediction_client.submit_predictions(
            self.validator_url, epoch_id, predictions
        )

        logger.info(
            f"Epoch {epoch_id} complete: "
            f"{result.get('accepted', 0)} accepted, "
            f"{result.get('rejected', 0)} rejected"
        )

        return {
            "epoch_id": epoch_id,
            "model": self.model.name,
            "episodes_fetched": len(episodes),
            "predictions_made": len(predictions),
            "fetch_time_s": round(fetch_time, 1),
            "predict_time_s": round(predict_time, 1),
            **result,
        }


def main():
    """CLI entry point for the miner."""
    import argparse

    parser = argparse.ArgumentParser(description="HOPE SN21 Miner")
    parser.add_argument("--validator-url", type=str, default="http://localhost:8080",
                        help="Validator HTTP API URL")
    parser.add_argument("--hotkey", type=str, default="miner_default",
                        help="Miner hotkey for authentication")
    parser.add_argument("--epoch", type=str, default="WR-2026-W17-PUB-E1",
                        help="Epoch ID to process")
    parser.add_argument("--model", type=str, default="baseline",
                        choices=["baseline"],
                        help="Prediction model to use")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    model = BaselineModel()
    runner = MinerRunner(model=model, hotkey=args.hotkey, validator_url=args.validator_url)

    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(runner.run_epoch(args.epoch))
    loop.close()

    print("\nResults:")
    for k, v in result.items():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
