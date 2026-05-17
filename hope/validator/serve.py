"""Validator HTTP API server — Phase D miner-facing episode endpoint.

Runs the FastAPI app from `hope.validator.api.server:create_app` against a
state dict built from:
  - episodes fetched from the operator's data API (HOPE_API_URL),
  - the deadline computed as now + PREDICTION_DEADLINE_HOURS,
  - registered miner hotkeys read from the metagraph.

A background task refreshes `registered_miners` and `uid_map` from the
metagraph every METAGRAPH_REFRESH_INTERVAL_SECONDS — without this, miners
registered after the validator started would be rejected with 403 until the
service is restarted (which would block every new miner from submitting
their first epoch).

Miners hit this server for `/v1/epochs/{id}/episodes` etc. On-chain scoring
(`hope-validator`) runs as a separate per-epoch process.

Memory diagnostics: when env var SN21_TRACEMALLOC=1, tracemalloc is enabled
at process startup and /debug/memory + /debug/tracemalloc endpoints become
available for live RSS leak analysis. Default off because tracemalloc adds
10-30% memory overhead.
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

# How often the background task re-reads the metagraph. Two minutes balances
# "new miner appears in metagraph within a tempo step" against "don't hammer
# the chain RPC every few seconds for a relatively static set".
METAGRAPH_REFRESH_INTERVAL_SECONDS = 120


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
            # Context-manager form ensures the websocket closes once we've
            # extracted what we need; otherwise the Subtensor instance
            # would survive the function return without ever closing
            # (no __del__ exists on bt.Subtensor). See _refresh_metagraph
            # for the same pattern + the memory-growth analysis.
            with bt.Subtensor(network=network) as subtensor:
                metagraph = subtensor.metagraph(netuid=netuid)
                registered_miners = set(metagraph.hotkeys)
                uid_map = {metagraph.hotkeys[uid]: uid for uid in range(metagraph.n)}
                logger.info("Metagraph loaded: netuid=%d, n=%d", netuid, metagraph.n)
                del metagraph
        except Exception as e:
            logger.warning("Could not load metagraph (%s); auth will reject all miners", e)

    state: dict = {
        "current_epoch_id": release_key,
        "episodes": epoch_data.episodes,
        "deadline": deadline,
        "submission_open": True,
        "predictions": {},
        "prediction_receipts": {},
        "registered_miners": registered_miners,
        "uid_map": uid_map,
        # Stashed so the FastAPI lifespan can refresh the metagraph
        # periodically without re-parsing CLI args.
        "_chain_config": {
            "no_chain": no_chain,
            "network": network,
            "netuid": netuid,
        },
    }
    return state


def _refresh_metagraph(state: dict) -> None:
    """Re-read the metagraph and replace the registered_miners + uid_map.

    Called periodically by the FastAPI lifespan task so newly-registered
    miners become visible to auth without a service restart. Swaps the
    set/dict atomically — readers (FastAPI request handlers) see either
    the old or the new copy, never a partial update.

    Memory: `bittensor.Subtensor` opens a websocket + substrate-interface
    client and has no __del__, so naively going-out-of-scope does not
    release those resources. We use the context-manager form so the
    websocket closes deterministically, drop the metagraph reference
    before returning, and run an explicit `gc.collect()` to break any
    reference cycles the substrate-interface async event loop tasks
    may have created. Without this, a starter-plan Render container
    OOMs within ~30 refreshes (~1 hour) because the leaked Subtensor
    + metagraph snapshots accumulate ~5-15 MB each per refresh.
    """
    cfg = state.get("_chain_config", {})
    if cfg.get("no_chain"):
        return

    new_miners: set[str] | None = None
    new_uid_map: dict[str, int] | None = None
    try:
        import bittensor as bt
        with bt.Subtensor(network=cfg["network"]) as subtensor:
            metagraph = subtensor.metagraph(netuid=cfg["netuid"])
            new_miners = set(metagraph.hotkeys)
            new_uid_map = {metagraph.hotkeys[uid]: uid for uid in range(metagraph.n)}
            # Drop the metagraph reference before exiting the `with`
            # block; the metagraph holds a back-reference to subtensor
            # internals, so without this it would survive __exit__.
            del metagraph
    except Exception as e:
        logger.warning("metagraph refresh failed: %s", e)
        return

    added = new_miners - state["registered_miners"]
    removed = state["registered_miners"] - new_miners
    state["registered_miners"] = new_miners
    state["uid_map"] = new_uid_map
    if added or removed:
        logger.info(
            "metagraph refresh: n=%d (added %d, removed %d hotkey(s))",
            len(new_miners), len(added), len(removed),
        )

    # Break any ref cycles substrate-interface left behind in async tasks.
    # Without this, even with .close() called, large heap allocations
    # (~5-15 MB per refresh) persist until Python's automatic cycle
    # collector eventually runs — which is too slow to keep up with a
    # 2-minute refresh cadence under a 512 MB Render starter-plan cap.
    import gc
    gc.collect()


async def _metagraph_refresh_loop(state: dict) -> None:
    """Background task: refresh metagraph every METAGRAPH_REFRESH_INTERVAL_SECONDS."""
    while True:
        try:
            await asyncio.sleep(METAGRAPH_REFRESH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
        _refresh_metagraph(state)


def main():
    """CLI entry point: `hope-validator-api`."""
    # Start tracemalloc BEFORE any non-stdlib imports so it captures allocations
    # made by FastAPI, bittensor, substrate-interface, httpx, etc. — gives us
    # accurate attribution for the live RSS-leak investigation. Default OFF
    # because tracking every allocation costs 10-30% memory and a small CPU
    # tax; only enable when SN21_TRACEMALLOC=1 is set in the deployment env.
    if os.environ.get("SN21_TRACEMALLOC") == "1":
        import tracemalloc
        tracemalloc.start(15)  # 15 stack frames per allocation site
        logger.warning("tracemalloc enabled (SN21_TRACEMALLOC=1)")

    import argparse

    from hope._cli_help import SafeHelpFormatter
    parser = argparse.ArgumentParser(
        description="SN21 Validator HTTP API (episode serving)",
        formatter_class=SafeHelpFormatter,
    )
    parser.add_argument("--release", default=os.environ.get("RELEASE_KEY", ""),
                        help="Release key (epoch ID) to serve. Pass 'auto' "
                             "(or leave RELEASE_KEY=auto in env) to discover "
                             "the latest published release from the operator "
                             "data backend at startup — useful for "
                             "third-party validators who want a set-and-"
                             "forget weekly rotation without manual env-var "
                             "edits.")
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

    if not args.release or args.release.strip().lower() == "auto":
        # Auto-discovery: query the operator data backend for the most
        # recently created release. Mirrors what deploy/validator_scoring/
        # scoring_trigger.sh does for the scoring cron — the helper lives
        # in HopeDataClient so a single algorithm serves both paths.
        from hope.validator.data_client import HopeDataClient
        try:
            client = HopeDataClient()
        except ValueError as exc:
            parser.error(
                f"--release auto requires HOPE_API_KEY and HOPE_API_URL to "
                f"be set; {exc}"
            )
        try:
            args.release = asyncio.run(client.discover_latest_release())
        except Exception as exc:
            parser.error(
                f"--release auto failed to resolve: {type(exc).__name__}: "
                f"{exc}. Either pass --release <EPOCH_ID> explicitly or "
                f"check the operator data backend at {client.base_url}."
            )
        logger.info("--release auto resolved to %s", args.release)

    state = _build_state(
        release_key=args.release,
        no_chain=args.no_chain,
        network=args.network,
        netuid=args.netuid,
        wallet_name=args.wallet_name,
        wallet_hotkey=args.wallet_hotkey,
    )
    # Hand the FastAPI lifespan a reference to the background refresh
    # coroutine so it gets started on app boot and cancelled on shutdown.
    state["_metagraph_refresh_coro"] = _metagraph_refresh_loop
    app = create_app(state)

    logger.info("Starting episode-serving API on %s:%d", args.host, args.port)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
