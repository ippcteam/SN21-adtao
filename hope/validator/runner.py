"""Validator Runner — main entry point for the HOPE SN21 validator.

Orchestrates the complete validator lifecycle:
1. Fetch epoch data from HOPE API
2. Compute and store commitment
3. Start FastAPI server for miner interaction
4. Collect predictions until deadline
5. Score all predictions
6. Reveal outcomes
7. (Future) Set weights on-chain

For launch: runs a single epoch cycle. Future: continuous epoch loop.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

import uvicorn

from hope.constants import HOPE_API_BASE_URL
from hope.validator.api.server import create_app
from hope.validator.data_client import HopeDataClient
from hope.validator.epoch_manager import EpochManager

logger = logging.getLogger(__name__)


class ValidatorRunner:
    """Main validator runner."""

    def __init__(
        self,
        hope_api_key: str = "hope-bt-internal-2026",
        hope_api_url: str = HOPE_API_BASE_URL,
        host: str = "0.0.0.0",
        port: int = 8080,
    ):
        self.hope_client = HopeDataClient(api_key=hope_api_key, base_url=hope_api_url)
        self.epoch_manager = EpochManager()
        self.host = host
        self.port = port
        self._server_thread: Optional[threading.Thread] = None

    async def run_epoch(self, release_key: str) -> dict:
        """Run a complete epoch cycle for a given release.

        This is the main method — call it to process one epoch end-to-end.
        """
        logger.info(f"Starting epoch for release {release_key}")

        # Step 1: Fetch data from HOPE
        logger.info("Fetching epoch data from HOPE API...")
        epoch_data = await self.hope_client.fetch_epoch_data(release_key)

        # Verify package integrity
        if not self.hope_client.verify_package_hash(epoch_data):
            raise ValueError("Package hash verification failed — data may be corrupted")

        logger.info(
            f"Fetched {epoch_data.episode_count} episodes, "
            f"{sum(1 for o in epoch_data.outcomes if o.t7)} with t7 outcomes"
        )

        # Step 2: Prepare epoch (compute commitments)
        ctx = self.epoch_manager.prepare(epoch_data)
        logger.info(f"Epoch prepared. Commitment: {ctx.commitment_hash[:16]}...")

        # Step 3: Start distribution
        self.epoch_manager.start_distribution()

        return {
            "epoch_id": ctx.epoch_id,
            "episode_count": len(ctx.episodes),
            "commitment_hash": ctx.commitment_hash,
            "deadline": ctx.deadline,
            "status": "collecting",
        }

    def score_epoch(self) -> dict:
        """Score all submitted predictions and reveal outcomes."""
        # Score
        scores = self.epoch_manager.score()

        # Reveal
        reveal = self.epoch_manager.reveal()

        # Complete
        self.epoch_manager.complete()

        return {
            "epoch_id": reveal["epoch_id"],
            "miners_scored": len(scores),
            "scores": {
                mid: {"final_score": s.final_score, "episodes_scored": s.episodes_scored}
                for mid, s in scores.items()
            },
            "commitment_verified": True,
        }

    def start_api_server(self) -> None:
        """Start the FastAPI server in a background thread."""
        state = self.epoch_manager.get_validator_state()
        app = create_app(state)

        config = uvicorn.Config(app, host=self.host, port=self.port, log_level="info")
        server = uvicorn.Server(config)

        self._server_thread = threading.Thread(target=server.run, daemon=True)
        self._server_thread.start()
        logger.info(f"Validator API server started on {self.host}:{self.port}")

    def get_api_app(self):
        """Get the FastAPI app instance (for testing without running server)."""
        state = self.epoch_manager.get_validator_state()
        return create_app(state)


def main():
    """CLI entry point for the validator."""
    import argparse

    parser = argparse.ArgumentParser(description="HOPE SN21 Validator")
    parser.add_argument("--release", type=str, default="WR-2026-W17-PUB-E1",
                        help="Release key to process")
    parser.add_argument("--api-key", type=str, default="hope-bt-internal-2026",
                        help="HOPE API key")
    parser.add_argument("--port", type=int, default=8080, help="API server port")
    parser.add_argument("--score-now", action="store_true",
                        help="Score immediately (don't wait for deadline)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    runner = ValidatorRunner(hope_api_key=args.api_key, port=args.port)

    # Run epoch
    loop = asyncio.new_event_loop()
    result = loop.run_until_complete(runner.run_epoch(args.release))
    loop.close()

    print(f"\nEpoch started: {result['epoch_id']}")
    print(f"Episodes: {result['episode_count']}")
    print(f"Commitment: {result['commitment_hash'][:32]}...")
    print(f"Deadline: {result['deadline']}")

    if args.score_now:
        # Score immediately (for testing)
        score_result = runner.score_epoch()
        print(f"\nScoring complete: {score_result['miners_scored']} miners")
        for mid, s in score_result["scores"].items():
            print(f"  {mid[:16]}...: {s['final_score']:.4f} ({s['episodes_scored']} episodes)")
    else:
        # Start API server and wait
        runner.start_api_server()
        print(f"\nValidator API running on port {args.port}")
        print("Miners can now fetch episodes and submit predictions.")
        print("Press Ctrl+C to stop.")
        try:
            while True:
                import time
                time.sleep(1)
        except KeyboardInterrupt:
            print("\nShutting down.")


if __name__ == "__main__":
    main()
