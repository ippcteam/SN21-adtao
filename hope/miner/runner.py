"""Miner Runner — main entry point for HOPE SN21 miners.

Handles the complete miner lifecycle:
1. Initialize Bittensor wallet, subtensor, register axon
2. Listen for EpochAnnouncement Synapse from validator
3. Fetch episodes from validator HTTP API
4. Run prediction model on each episode
5. Submit predictions back to validator
6. Listen for CommitmentReveal and verify scores

Supports both on-chain mode (with Bittensor) and HTTP-only mode.
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
    """Main miner runner with optional Bittensor integration."""

    def __init__(
        self,
        model: PredictionEngine | None = None,
        hotkey: str = "miner_default",
        validator_url: str = "http://localhost:8080",
        # Bittensor config
        network: str = "test",
        netuid: int = 0,
        wallet_name: str = "adtao_miners",
        wallet_hotkey: str = "miner1",
        no_chain: bool = False,
        axon_port: int = 8091,
    ):
        self.model = model or BaselineModel()
        self.hotkey = hotkey
        self.validator_url = validator_url
        self.episode_client = EpisodeClient(hotkey=hotkey)
        self.prediction_client = PredictionClient(hotkey=hotkey)

        # Bittensor config
        self.network = network
        self.netuid = netuid
        self.wallet_name = wallet_name
        self.wallet_hotkey = wallet_hotkey
        self.no_chain = no_chain
        self.axon_port = axon_port

        # Bittensor components
        self.wallet = None
        self.subtensor = None
        self.metagraph = None
        self.axon = None

    def init_bittensor(self):
        """Initialize Bittensor wallet, subtensor, and register axon."""
        if self.no_chain:
            logger.info("Running without chain (--no-chain mode)")
            return

        try:
            import bittensor as bt

            # Wallet
            self.wallet = bt.wallet(
                name=self.wallet_name,
                hotkey=self.wallet_hotkey,
            )
            self.hotkey = self.wallet.hotkey.ss58_address
            logger.info(f"Wallet loaded: {self.wallet_name}/{self.wallet_hotkey}")
            logger.info(f"Hotkey address: {self.hotkey}")

            # Update clients with real hotkey
            self.episode_client = EpisodeClient(hotkey=self.hotkey)
            self.prediction_client = PredictionClient(hotkey=self.hotkey)

            # Subtensor
            self.subtensor = bt.subtensor(network=self.network)
            logger.info(f"Connected to subtensor: {self.network}")

            # Metagraph
            self.metagraph = self.subtensor.metagraph(netuid=self.netuid)
            logger.info(f"Metagraph loaded: netuid={self.netuid}, n={self.metagraph.n}")

            # Axon — register handlers for incoming Synapses
            self.axon = bt.axon(wallet=self.wallet, port=self.axon_port)
            self._register_axon_handlers()

            # Serve axon on the network
            self.axon.serve(netuid=self.netuid, subtensor=self.subtensor)
            self.axon.start()
            logger.info(f"Axon started on port {self.axon_port}")

        except ImportError:
            logger.warning("Bittensor not installed — running in HTTP-only mode")
            self.no_chain = True
        except Exception as e:
            logger.error(f"Failed to initialize Bittensor: {e}")
            self.no_chain = True

    def _register_axon_handlers(self):
        """Register Synapse forward handlers on the axon."""
        from hope.protocol.synapse import EpochAnnouncement, Heartbeat, CommitmentReveal

        def handle_epoch_announcement(synapse: EpochAnnouncement) -> EpochAnnouncement:
            """Handle incoming EpochAnnouncement from validator."""
            logger.info(
                f"Received EpochAnnouncement: epoch={synapse.epoch_id}, "
                f"episodes={synapse.episode_count}, deadline={synapse.deadline}"
            )
            # Store for processing
            self._pending_epoch = {
                "epoch_id": synapse.epoch_id,
                "episode_count": synapse.episode_count,
                "api_endpoint": synapse.api_endpoint,
                "deadline": synapse.deadline,
            }
            return synapse

        def handle_heartbeat(synapse: Heartbeat) -> Heartbeat:
            """Handle heartbeat check from validator."""
            synapse.miner_version = "0.1.0"
            synapse.miner_status = "ready"
            return synapse

        def handle_commitment_reveal(synapse: CommitmentReveal) -> CommitmentReveal:
            """Handle CommitmentReveal from validator."""
            logger.info(
                f"Received CommitmentReveal: epoch={synapse.epoch_id}, "
                f"outcomes_url={synapse.outcomes_url}"
            )
            return synapse

        self.axon.attach(
            forward_fn=handle_epoch_announcement,
        ).attach(
            forward_fn=handle_heartbeat,
        ).attach(
            forward_fn=handle_commitment_reveal,
        )

    async def run_epoch(self, epoch_id: str, validator_url: str | None = None) -> dict:
        """Run the full miner cycle for one epoch.

        1. Fetch all episodes
        2. Generate predictions
        3. Submit predictions
        """
        url = validator_url or self.validator_url

        logger.info(f"Starting epoch {epoch_id} with model {self.model.name}")
        self.model.on_epoch_start(epoch_id, 0)

        # Step 1: Fetch episodes
        logger.info(f"Fetching episodes from {url}...")
        start = time.time()
        episodes = await self.episode_client.fetch_all_episodes(url, epoch_id)
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
        result = await self.prediction_client.submit_predictions(url, epoch_id, predictions)

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

    def run_continuous(self, epoch_id: str):
        """Run continuous miner loop — listen for epochs and process them.

        In on-chain mode: listens via axon for EpochAnnouncement.
        In HTTP-only mode: polls the validator health endpoint.
        """
        self.init_bittensor()

        logger.info(f"Miner running continuously (model: {self.model.name})")
        self._pending_epoch = None

        while True:
            try:
                # Check for pending epoch from Synapse
                if self._pending_epoch:
                    pending = self._pending_epoch
                    self._pending_epoch = None

                    loop = asyncio.new_event_loop()
                    result = loop.run_until_complete(
                        self.run_epoch(pending["epoch_id"], pending.get("api_endpoint"))
                    )
                    loop.close()

                    logger.info(f"Epoch result: {result.get('accepted', 0)} accepted")
                else:
                    # In HTTP-only mode, process the given epoch_id
                    if self.no_chain:
                        loop = asyncio.new_event_loop()
                        result = loop.run_until_complete(self.run_epoch(epoch_id))
                        loop.close()
                        logger.info(f"Epoch result: {result.get('accepted', 0)} accepted")
                        break  # Single run in HTTP mode

                # Sync metagraph periodically
                if self.metagraph:
                    self.metagraph.sync()

                time.sleep(30)

            except KeyboardInterrupt:
                logger.info("Miner shutting down")
                if self.axon:
                    self.axon.stop()
                break
            except Exception as e:
                logger.error(f"Miner error: {e}")
                time.sleep(60)


def main():
    """CLI entry point for the miner."""
    import argparse

    parser = argparse.ArgumentParser(description="HOPE SN21 Miner")
    parser.add_argument("--validator-url", type=str, default="http://localhost:8080",
                        help="Validator HTTP API URL")
    parser.add_argument("--epoch", type=str, default="WR-2026-W17-PUB-E1",
                        help="Epoch ID to process")
    parser.add_argument("--model", type=str, default="baseline",
                        choices=["baseline"], help="Prediction model to use")

    # Bittensor args
    parser.add_argument("--network", type=str, default="test",
                        choices=["test", "finney", "local"],
                        help="Bittensor network")
    parser.add_argument("--netuid", type=int, default=0, help="Subnet netuid")
    parser.add_argument("--wallet-name", type=str, default="adtao_miners",
                        help="Wallet name")
    parser.add_argument("--wallet-hotkey", type=str, default="miner1",
                        help="Wallet hotkey name")
    parser.add_argument("--no-chain", action="store_true",
                        help="Run without Bittensor chain (HTTP only)")
    parser.add_argument("--axon-port", type=int, default=8091,
                        help="Port for axon server")

    # Mode
    parser.add_argument("--continuous", action="store_true",
                        help="Run continuous miner loop")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    model = BaselineModel()
    runner = MinerRunner(
        model=model,
        validator_url=args.validator_url,
        hotkey=args.wallet_hotkey,
        network=args.network,
        netuid=args.netuid,
        wallet_name=args.wallet_name,
        wallet_hotkey=args.wallet_hotkey,
        no_chain=args.no_chain,
        axon_port=args.axon_port,
    )

    if args.continuous:
        runner.run_continuous(args.epoch)
    else:
        # Single epoch run (HTTP only)
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(runner.run_epoch(args.epoch))
        loop.close()

        print("\nResults:")
        for k, v in result.items():
            print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
