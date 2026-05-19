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
import json
import logging
import os
import sys
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
        # Same subprocess pattern as _refresh_metagraph below — the
        # initial chain read would otherwise leave 30-100 MB of
        # substrate-interface state pinned in the parent daemon for the
        # rest of its lifetime, even before the first refresh runs.
        import subprocess as _subprocess
        cmd = [
            sys.executable, "-m", "hope.validator.metagraph_dump",
            "--network", network,
            "--netuid", str(netuid),
        ]
        try:
            result = _subprocess.run(
                cmd, capture_output=True, text=True, timeout=60,
            )
            if result.returncode == 0:
                payload = json.loads(result.stdout)
                hotkeys = list(payload["hotkeys"])
                registered_miners = set(hotkeys)
                uid_map = {hk: uid for uid, hk in enumerate(hotkeys)}
                logger.info("Metagraph loaded: netuid=%d, n=%d", netuid, len(hotkeys))
            else:
                logger.warning(
                    "Could not load metagraph (subprocess exit %d: %s); "
                    "auth will reject all miners until first refresh",
                    result.returncode, result.stderr.strip(),
                )
        except Exception as e:
            logger.warning(
                "Could not load metagraph (%s); auth will reject all "
                "miners until first refresh", e,
            )

    # Load the validator's own hotkey ss58 so the /verification endpoint
    # can query the right RevealedCommitments entries. Wallet load is
    # read-only (no decryption, no password); only the public ss58 is
    # needed by chain-state endpoints. Failure is non-fatal — endpoints
    # that depend on it will return 404 with a clear message.
    validator_hotkey_ss58: str = ""
    if not no_chain:
        try:
            import bittensor as bt
            wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
            validator_hotkey_ss58 = str(wallet.hotkey.ss58_address)
            logger.info("Validator hotkey ss58: %s", validator_hotkey_ss58)
        except Exception as e:
            logger.warning(
                "Could not load validator wallet (%s); chain-state endpoints "
                "(/verification etc.) will return 404 until configured.", e,
            )

    state: dict = {
        "current_epoch_id": release_key,
        "episodes": epoch_data.episodes,
        "deadline": deadline,
        "submission_open": True,
        "predictions": {},
        "prediction_receipts": {},
        "registered_miners": registered_miners,
        "uid_map": uid_map,
        "validator_hotkey_ss58": validator_hotkey_ss58,
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

    Memory: the chain read happens in a short-lived subprocess
    (`hope.validator.metagraph_dump`) which exits after printing the
    hotkey list as JSON. Any objects bittensor / substrate-interface
    retained internally — websocket buffers, ScaleType caches, async
    task references — die with the subprocess. Doing this in-process
    (even with Subtensor close() + explicit gc.collect()) accumulates
    30-100 MB of RSS per refresh that the daemon can never reclaim;
    the subprocess approach keeps daemon RSS flat across an unlimited
    number of refresh cycles. The cost is ~1-2s of subprocess startup
    every refresh interval, which is fine for the 120s cadence we
    operate at.
    """
    cfg = state.get("_chain_config", {})
    if cfg.get("no_chain"):
        return

    import subprocess as _subprocess
    cmd = [
        sys.executable, "-m", "hope.validator.metagraph_dump",
        "--network", cfg["network"],
        "--netuid", str(cfg["netuid"]),
    ]
    try:
        result = _subprocess.run(
            cmd, capture_output=True, text=True, timeout=60,
        )
    except _subprocess.TimeoutExpired:
        logger.warning("metagraph refresh subprocess timed out after 60s")
        return
    except Exception as e:
        logger.warning("metagraph refresh subprocess failed to start: %s", e)
        return

    if result.returncode != 0:
        logger.warning(
            "metagraph refresh subprocess exited %d: %s",
            result.returncode, result.stderr.strip(),
        )
        return

    try:
        payload = json.loads(result.stdout)
        hotkeys = list(payload["hotkeys"])
    except (json.JSONDecodeError, KeyError, TypeError) as e:
        logger.warning("metagraph refresh subprocess output unparseable: %s", e)
        return

    new_miners = set(hotkeys)
    new_uid_map = {hk: uid for uid, hk in enumerate(hotkeys)}

    added = new_miners - state["registered_miners"]
    removed = state["registered_miners"] - new_miners
    state["registered_miners"] = new_miners
    state["uid_map"] = new_uid_map
    if added or removed:
        logger.info(
            "metagraph refresh: n=%d (added %d, removed %d hotkey(s))",
            len(new_miners), len(added), len(removed),
        )


async def _metagraph_refresh_loop(state: dict) -> None:
    """Background task: refresh metagraph every METAGRAPH_REFRESH_INTERVAL_SECONDS.

    `_refresh_metagraph` calls `subprocess.run(...)` which is synchronous and
    blocks the calling thread for the subprocess's whole lifetime — up to 60s
    on subnets with large metagraphs (e.g. ~30-60s on mainnet netuid 21 with
    256 hotkeys). Running it inline in this async coroutine froze the entire
    FastAPI event loop during each refresh, making every /health, /debug/*,
    and /v1/* request time out at Render's load-balancer ceiling. Wrapping
    in `asyncio.to_thread` runs the sync call in a thread pool so the event
    loop keeps serving HTTP requests while the chain read is in flight.
    """
    while True:
        try:
            await asyncio.sleep(METAGRAPH_REFRESH_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return
        try:
            await asyncio.to_thread(_refresh_metagraph, state)
        except Exception as e:
            logger.warning("metagraph refresh task error: %s", e)


def main():
    """CLI entry point: `hope-validator-api`."""
    # Start tracemalloc BEFORE any non-stdlib imports so it captures allocations
    # made by FastAPI, bittensor, substrate-interface, httpx, etc. — gives us
    # accurate attribution for the live RSS-leak investigation. Costs 10-30%
    # memory + small CPU tax. Default ON while the leak is being chased; set
    # SN21_TRACEMALLOC=0 in the deployment env once the source is identified
    # and a fix has been confirmed in production.
    if os.environ.get("SN21_TRACEMALLOC", "1") != "0":
        import tracemalloc
        tracemalloc.start(15)  # 15 stack frames per allocation site
        logger.warning("tracemalloc enabled (default on; set SN21_TRACEMALLOC=0 to disable)")

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
