"""Validator HTTP API server — Phase D miner-facing episode endpoint.

Runs the FastAPI app from `hope.validator.api.server:create_app` against a
state dict built from:
  - episodes fetched from the operator's data API (HOPE_API_URL),
  - the deadline computed as now + PREDICTION_DEADLINE_HOURS,
  - registered miner hotkeys read from the metagraph.

Miners hit this server for `/v1/epochs/{id}/episodes` etc. On-chain scoring
(`hope-validator`) runs as a separate per-epoch process — see
`scripts/start_validator.sh`.
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import uvicorn

from hope.constants import PREDICTION_DEADLINE_HOURS
from hope.validator.api.server import create_app
from hope.validator.data_client import HopeDataClient

logger = logging.getLogger(__name__)


def _build_state(release_key: str, no_chain: bool, network: str, netuid: int,
                 wallet_name: str, wallet_hotkey: str) -> dict:
    """Fetch episodes + metagraph state and return the validator-state dict."""
    client = HopeDataClient()
    epoch_data = asyncio.run(client.fetch_episodes_only(release_key))
    logger.info(
        "Fetched %d episodes for release %s (schema %s)",
        len(epoch_data.episodes), release_key, epoch_data.schema_version,
    )

    deadline = (datetime.now(timezone.utc)
                + timedelta(hours=PREDICTION_DEADLINE_HOURS)).isoformat()

    registered_miners: set[str] = set()
    uid_map: dict[str, int] = {}
    if not no_chain:
        try:
            import bittensor as bt
            subtensor = bt.Subtensor(network=network)
            metagraph = subtensor.metagraph(netuid=netuid)
            registered_miners = set(metagraph.hotkeys)
            uid_map = {metagraph.hotkeys[uid]: uid for uid in range(metagraph.n)}
            logger.info("Metagraph loaded: netuid=%d, n=%d", netuid, metagraph.n)
        except Exception as e:
            logger.warning("Could not load metagraph (%s); auth will reject all miners", e)

    return {
        "current_epoch_id": release_key,
        "episodes": epoch_data.episodes,
        "deadline": deadline,
        "submission_open": True,
        "predictions": {},
        "prediction_receipts": {},
        "registered_miners": registered_miners,
        "uid_map": uid_map,
    }


def main():
    """CLI entry point: `hope-validator-api`."""
    import argparse

    parser = argparse.ArgumentParser(description="SN21 Validator HTTP API (episode serving)")
    parser.add_argument("--release", default=os.environ.get("RELEASE_KEY", ""),
                        help="Release key (epoch ID) to serve")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", "8080")),
                        help="Port to bind the HTTP server")
    parser.add_argument("--host", default="0.0.0.0",  # noqa: S104 — bound by Render's TLS reverse proxy
                        help="Bind host (default 0.0.0.0)")
    parser.add_argument("--network", default="finney",
                        choices=["test", "finney", "local"],
                        help="Bittensor network (for metagraph; ignored under --no-chain)")
    parser.add_argument("--netuid", type=int,
                        default=int(os.environ.get("NETUID", "21")))
    parser.add_argument("--wallet-name", default=os.environ.get("WALLET_NAME", ""))
    parser.add_argument("--wallet-hotkey", default=os.environ.get("HOTKEY_NAME", "default"))
    parser.add_argument("--no-chain", action="store_true",
                        help="Skip metagraph load (no auth — dev/local only)")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.release:
        parser.error("--release is required (or set RELEASE_KEY)")

    state = _build_state(
        release_key=args.release,
        no_chain=args.no_chain,
        network=args.network,
        netuid=args.netuid,
        wallet_name=args.wallet_name,
        wallet_hotkey=args.wallet_hotkey,
    )
    app = create_app(state)

    logger.info("Starting episode-serving API on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
