"""Miner Runner — main entry point for HOPE SN21 miners.

Handles the complete miner lifecycle:
1. Fetch episodes from validator HTTP API
2. Run prediction model on each episode
3. Submit predictions back to validator

HTTP-only architecture — no Synapses.
Miners discover epochs via the validator's /health endpoint and
interact entirely over HTTP.
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
    """Main miner runner — HTTP only."""

    def __init__(
        self,
        model: PredictionEngine | None = None,
        hotkey: str = "miner_default",
        validator_url: str = "https://validator.adtao.io",
    ):
        self.model = model or BaselineModel()
        self.hotkey = hotkey
        self.validator_url = validator_url
        self.episode_client = EpisodeClient(hotkey=hotkey)
        self.prediction_client = PredictionClient(hotkey=hotkey)

    async def run_epoch(self, epoch_id: str) -> dict:
        """Run the full miner cycle for one epoch."""
        logger.info(f"Starting epoch {epoch_id} with model {self.model.name}")
        self.model.on_epoch_start(epoch_id, 0)

        # Fetch episodes
        logger.info(f"Fetching episodes from {self.validator_url}...")
        start = time.time()
        episodes = await self.episode_client.fetch_all_episodes(
            self.validator_url, epoch_id
        )
        fetch_time = time.time() - start
        logger.info(f"Fetched {len(episodes)} episodes in {fetch_time:.1f}s")

        # Generate predictions
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

        # Submit predictions
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

    async def discover_epoch(self) -> str | None:
        """Check the validator's health endpoint for the current epoch."""
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.validator_url}/health")
                if resp.status_code == 200:
                    return resp.json().get("current_epoch")
        except Exception as e:
            logger.debug(f"Health check failed: {e}")
        return None

    async def run_continuous(self, poll_interval: int = 30):
        """Run continuous miner loop — poll validator for new epochs."""
        logger.info(f"Miner running continuously (model: {self.model.name})")
        last_epoch = None

        while True:
            try:
                current_epoch = await self.discover_epoch()
                if current_epoch and current_epoch != last_epoch:
                    logger.info(f"New epoch detected: {current_epoch}")
                    result = await self.run_epoch(current_epoch)
                    logger.info(f"Epoch result: {result.get('accepted', 0)} accepted")
                    last_epoch = current_epoch

                await asyncio.sleep(poll_interval)

            except KeyboardInterrupt:
                logger.info("Miner shutting down")
                break
            except Exception as e:
                logger.error(f"Miner error: {e}")
                await asyncio.sleep(60)


def main():
    """CLI entry point for the miner."""
    import argparse

    parser = argparse.ArgumentParser(description="HOPE SN21 Miner")
    parser.add_argument("--validator-url", type=str, default="https://validator.adtao.io",
                        help="Validator HTTP API URL")
    parser.add_argument("--hotkey", type=str, default="miner_default",
                        help="Miner hotkey for authentication")
    parser.add_argument("--epoch", type=str, default=None,
                        help="Epoch ID (omit for auto-discover from validator)")
    parser.add_argument("--model", type=str, default="baseline",
                        choices=["baseline"], help="Prediction model")
    parser.add_argument("--continuous", action="store_true",
                        help="Run continuous (polls validator for new epochs)")
    parser.add_argument("--poll-interval", type=int, default=30,
                        help="Seconds between health checks in continuous mode")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    runner = MinerRunner(
        model=BaselineModel(), hotkey=args.hotkey, validator_url=args.validator_url
    )

    if args.continuous:
        asyncio.run(runner.run_continuous(args.poll_interval))
    elif args.epoch:
        result = asyncio.run(runner.run_epoch(args.epoch))
        print("\nResults:")
        for k, v in result.items():
            print(f"  {k}: {v}")
    else:
        loop = asyncio.new_event_loop()
        epoch_id = loop.run_until_complete(runner.discover_epoch())
        if epoch_id:
            result = loop.run_until_complete(runner.run_epoch(epoch_id))
            print("\nResults:")
            for k, v in result.items():
                print(f"  {k}: {v}")
        else:
            print("No active epoch found. Is the validator running?")
        loop.close()


if __name__ == "__main__":
    main()
