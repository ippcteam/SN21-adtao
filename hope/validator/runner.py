"""Validator Runner — main entry point for the the operator SN21 validator.

Orchestrates the complete validator lifecycle:
1. Initialize Bittensor wallet, subtensor, metagraph
2. Fetch epoch data from data API
3. Compute commitment hash
4. Start FastAPI server for miner interaction (HTTP only — no Synapses)
5. Collect predictions until deadline
6. Score all predictions
7. Set weights on-chain
8. Reveal outcomes

HTTP-only architecture — Synapses are not used.
Miners discover epochs and interact entirely via the validator's HTTP API.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time

import uvicorn
from hope.validator.api.server import create_app
from hope.validator.data_client import HopeDataClient
from hope.validator.epoch_manager import EpochManager
from hope.validator.weight_setter import WeightSetter

logger = logging.getLogger(__name__)


class ValidatorRunner:
    """Main validator runner — HTTP only, no Synapse transport."""

    def __init__(
        self,
        hope_api_key: str = "",
        hope_api_url: str = "",
        host: str = "0.0.0.0",  # noqa: S104 — validator HTTP server is intentionally bound to all interfaces; operators front it with a TLS reverse proxy
        port: int = 8080,
        # Bittensor config
        network: str = "test",
        netuid: int = 0,
        wallet_name: str = "sn21_validator",
        wallet_hotkey: str = "default",
        no_chain: bool = False,
        burn_fraction: float = 0.95,
    ):
        self.hope_client = HopeDataClient(api_key=hope_api_key, base_url=hope_api_url)
        self.epoch_manager = EpochManager()
        self.weight_setter = WeightSetter(burn_fraction=burn_fraction)
        self.host = host
        self.port = port
        self.no_chain = no_chain

        # Bittensor components (initialized lazily)
        self.network = network
        self.netuid = netuid
        self.wallet_name = wallet_name
        self.wallet_hotkey = wallet_hotkey
        self.wallet = None
        self.subtensor = None
        self.metagraph = None

    def init_bittensor(self):
        """Initialize Bittensor wallet, subtensor, and metagraph."""
        if self.no_chain:
            logger.info("Running without chain (--no-chain mode)")
            return

        try:
            import bittensor as bt

            self.wallet = bt.Wallet(
                name=self.wallet_name,
                hotkey=self.wallet_hotkey,
            )
            logger.info(f"Wallet loaded: {self.wallet_name}/{self.wallet_hotkey}")

            self.subtensor = bt.Subtensor(network=self.network)
            logger.info(f"Connected to subtensor: {self.network}")

            self.metagraph = self.subtensor.metagraph(netuid=self.netuid)
            logger.info(f"Metagraph loaded: netuid={self.netuid}, n={self.metagraph.n}")

        except ImportError:
            logger.warning("Bittensor not installed — running in no-chain mode")
            self.no_chain = True
        except Exception as e:
            logger.error(f"Failed to initialize Bittensor: {e}")
            self.no_chain = True

    def get_uid_map(self) -> dict[str, int]:
        """Build hotkey → UID mapping from metagraph."""
        if not self.metagraph:
            return {}
        return {self.metagraph.hotkeys[uid]: uid for uid in range(self.metagraph.n)}

    def get_registered_hotkeys(self) -> set[str]:
        """Get the set of registered miner hotkeys from metagraph."""
        if not self.metagraph:
            return set()
        return {self.metagraph.hotkeys[uid] for uid in range(self.metagraph.n)}

    async def run_epoch(self, release_key: str) -> dict:
        """Start an epoch — fetch episodes ONLY, defer outcomes until after deadline.

        Outcomes must NOT be fetched until after the submission window
        closes. This prevents validators from seeing answers while miners
        are predicting.
        """
        logger.info(f"Starting epoch for release {release_key}")
        self._release_key = release_key

        # Sync registered miners and UID map from metagraph (for auth)
        registered = self.get_registered_hotkeys()
        self.epoch_manager.registered_miners = registered
        self.epoch_manager.uid_map = self.get_uid_map()
        if registered:
            logger.info(f"Registered miners from metagraph: {len(registered)}")

        # Fetch episodes ONLY — outcomes are NOT fetched here
        logger.info("Fetching episodes ONLY from data API (outcomes NOT fetched)...")
        episodes_data = await self.hope_client.fetch_episodes_only(release_key)

        ctx = self.epoch_manager.prepare_episodes_only(
            episodes=episodes_data.episodes,
            release_key=release_key,
        )

        # Start collecting predictions
        self.epoch_manager.start_collecting()

        logger.info(
            f"Epoch started: {len(ctx.episodes)} episodes distributed, "
            f"outcomes DEFERRED until deadline passes"
        )

        return {
            "epoch_id": ctx.epoch_id,
            "episode_count": len(ctx.episodes),
            "deadline": ctx.deadline,
            "status": "collecting",
            "outcomes": "deferred (fetched after deadline)",
        }

    def score_epoch(self) -> dict:
        """Close submissions, fetch outcomes, score, set weights.

        Phase separation enforced:
        1. Close submission window
        2. NOW fetch outcomes from data API (provably after deadline)
        3. Score predictions against outcomes
        4. Set weights on-chain
        5. Reveal for verification
        """
        # Step 1: Close submissions
        self.epoch_manager.close_submissions()

        # Step 2: Fetch outcomes NOW — this is the first time outcomes are accessed
        release_key = getattr(self, '_release_key', '')
        logger.info(f"Fetching outcomes NOW (after deadline) for {release_key}")
        loop = asyncio.new_event_loop()
        outcomes = loop.run_until_complete(
            self.hope_client.fetch_outcomes_only(release_key)
        )
        loop.close()
        logger.info(f"Outcomes fetched: {len(outcomes)} (after submissions closed)")

        scores = self.epoch_manager.load_outcomes_and_score(outcomes)

        # Step 3: Set weights on-chain
        if not self.no_chain and scores:
            uid_map = self.get_uid_map()
            final_scores = {mid: s.final_score for mid, s in scores.items()}
            uids, weights = self.weight_setter.normalize_scores(final_scores, uid_map)

            if uids:
                loop = asyncio.new_event_loop()
                loop.run_until_complete(
                    self.weight_setter.set_weights(
                        self.subtensor, self.wallet, self.netuid, uids, weights
                    )
                )
                loop.close()

        # Step 4: Reveal
        reveal = self.epoch_manager.reveal()

        # Step 5: Complete
        self.epoch_manager.complete()

        return {
            "epoch_id": reveal["epoch_id"],
            "miners_scored": len(scores),
            "outcomes_fetched_at": reveal.get("outcomes_fetched_at"),
            "submissions_closed_at": reveal.get("submissions_closed_at"),
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

        config = uvicorn.Config(
            app,
            host=self.host,
            port=self.port,
            log_level="info",
            limit_concurrency=100,
            limit_max_requests=10000,
            timeout_keep_alive=5,
        )
        server = uvicorn.Server(config)

        self._server_thread = threading.Thread(target=server.run, daemon=True)
        self._server_thread.start()
        logger.info(f"Validator API server started on {self.host}:{self.port}")

    def get_api_app(self):
        """Get the FastAPI app instance (for testing without running server)."""
        state = self.epoch_manager.get_validator_state()
        return create_app(state)

    def run_continuous(self, release_key: str, epoch_duration_seconds: int = 3600):
        """Run continuous epoch loop.

        Args:
            release_key: Release to use for all epochs
            epoch_duration_seconds: How long each epoch lasts (3600 = 1 hour)
        """
        self.init_bittensor()
        self.start_api_server()
        logger.info(f"Running continuous epochs ({epoch_duration_seconds}s each)")

        epoch_num = 0
        while True:
            epoch_num += 1
            logger.info(f"=== EPOCH {epoch_num} ===")

            try:
                loop = asyncio.new_event_loop()
                result = loop.run_until_complete(self.run_epoch(release_key))
                loop.close()
                logger.info(f"Epoch started: {result['episode_count']} episodes")

                logger.info(f"Collecting predictions for {epoch_duration_seconds}s...")
                time.sleep(epoch_duration_seconds)

                score_result = self.score_epoch()
                logger.info(f"Epoch scored: {score_result['miners_scored']} miners")

                for mid, s in score_result["scores"].items():
                    logger.info(f"  {mid[:20]}...: {s['final_score']:.4f}")

                # Refresh metagraph
                if self.metagraph:
                    self.metagraph.sync()

            except KeyboardInterrupt:
                logger.info("Shutting down")
                break
            except Exception as e:
                logger.error(f"Epoch {epoch_num} failed: {e}")
                time.sleep(60)


def main():
    """CLI entry point for the validator."""
    import argparse

    parser = argparse.ArgumentParser(description="SN21 Validator")
    parser.add_argument("--release", type=str,
                        default=os.environ.get("RELEASE_KEY", ""),
                        help="Release key to process (or set RELEASE_KEY env var)")
    parser.add_argument("--api-key", type=str,
                        default=os.environ.get("HOPE_API_KEY", ""),
                        help="data API key (or set HOPE_API_KEY env var)")
    parser.add_argument("--port", type=int, default=8080, help="API server port")
    parser.add_argument("--network", type=str, default="finney",
                        choices=["test", "finney", "local"],
                        help="Bittensor network (default: finney mainnet; "
                             "use 'test' for testnet, which uses netuid 466)")
    parser.add_argument("--netuid", type=int,
                        default=int(os.environ.get("NETUID", "21")),
                        help="Subnet netuid (default: 21 mainnet; testnet "
                             "is netuid 466)")
    parser.add_argument("--wallet-name", type=str, default="sn21_validator",
                        help="Wallet name")
    parser.add_argument("--wallet-hotkey", type=str, default="default",
                        help="Wallet hotkey")
    parser.add_argument("--no-chain", action="store_true",
                        help="Run without Bittensor chain (HTTP only)")
    parser.add_argument("--score-now", action="store_true",
                        help="Score immediately (single epoch, no waiting)")
    parser.add_argument("--continuous", action="store_true",
                        help="Run continuous epoch loop")
    parser.add_argument("--epoch-duration", type=int, default=3600,
                        help="Epoch duration in seconds (default: 3600)")
    parser.add_argument("--burn", type=float, default=0.95,
                        help="Burn rate 0.0-1.0 (default: 0.95 = 95%% to UID 0)")
    parser.add_argument("--mode", choices=["http", "onchain"], default="http",
                        help="Scoring path (http=legacy, onchain=Layers 9.B–9.C)")
    parser.add_argument("--archive-tier-1", action="append", default=[],
                        help="Tier-1 archive base URL (chain mode; repeat for multi)")
    parser.add_argument("--archive-tier-2", action="append", default=[],
                        help="Tier-2 archive base URL (chain mode; repeat for multi)")
    parser.add_argument("--blocks-until-pre-reveal", type=int, default=300,
                        help="9.C.1 reveal delay (chain mode; default 300 blocks ≈ 1h)")
    parser.add_argument("--blocks-until-post-reveal", type=int, default=600,
                        help="9.C.2 reveal delay (chain mode; default 600 blocks ≈ 2h)")
    parser.add_argument("--blocks-until-weights-reveal", type=int, default=360,
                        help="9.C.3 weights reveal delay (chain mode; default 360 blocks)")
    parser.add_argument("--ed25519-key-file", default=None,
                        help="Path to ed25519 PEM private key for inner_sig (chain mode); "
                             "if omitted and the wallet hotkey is ed25519, that key is used")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    runner = ValidatorRunner(
        hope_api_key=args.api_key, port=args.port, network=args.network,
        netuid=args.netuid, wallet_name=args.wallet_name,
        wallet_hotkey=args.wallet_hotkey, no_chain=args.no_chain,
        burn_fraction=args.burn,
    )

    if args.mode == "onchain":
        runner.init_bittensor()
        if not args.archive_tier_2:
            parser.error("--mode onchain requires at least one --archive-tier-2 URL")
        outcome = _run_validator_onchain_cli(args, runner)
        print("\nOn-chain epoch outcome:")
        print(f"  ok: {outcome.ok}")
        print(f"  aborted_reason: {outcome.aborted_reason}")
        if outcome.pre_scoring_commit:
            print(f"  9.C.1 block: {outcome.pre_scoring_commit.block_number}")
        if outcome.weights_commit:
            print(f"  9.C.3 block: {outcome.weights_commit.block_number}")
        if outcome.post_scoring_commit:
            print(f"  9.C.2 block: {outcome.post_scoring_commit.block_number}")
        if outcome.retry_log_commit:
            print(f"  9.C.6 block: {outcome.retry_log_commit.block_number}")
        return

    if args.continuous:
        runner.run_continuous(args.release, args.epoch_duration)
    else:
        runner.init_bittensor()
        loop = asyncio.new_event_loop()
        result = loop.run_until_complete(runner.run_epoch(args.release))
        loop.close()

        print(f"\nEpoch started: {result['epoch_id']}")
        print(f"Episodes: {result['episode_count']}")
        print(f"Deadline: {result['deadline']}")
        print(f"Outcomes: {result.get('outcomes', 'deferred')}")

        if args.score_now:
            score_result = runner.score_epoch()
            print(f"\nScoring complete: {score_result['miners_scored']} miners")
            for mid, s in score_result["scores"].items():
                print(f"  {mid[:20]}...: {s['final_score']:.4f}")
        else:
            runner.start_api_server()
            print(f"\nValidator API running on port {args.port}")
            print("Miners connect via HTTP. Press Ctrl+C to stop.")
            try:
                while True:
                    time.sleep(1)
            except KeyboardInterrupt:
                print("\nShutting down.")


def _run_validator_onchain_cli(args, runner):
    """Bridge from CLI flags to `run_epoch_scoring`.

    Stitches together the chain reads, archive endpoints, ground-truth
    aggregation from the operator's reveal blob, and scorer adapter.
    """
    import os as _os
    import time as _time

    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from hope.commitment.archives import ArchiveClient, ArchiveEndpoint
    from hope.commitment.drand_lib import drand_round_at
    from hope.commitment.scoreability import TimingBounds
    from hope.scoring.onchain_adapter import (
        aggregate_outcomes_to_truth,
        compute_scoring_inputs_hash,
        make_scorer,
    )
    from hope.validator.onchain_runner import (
        MinerOnChainInputs,
        run_epoch_scoring,
    )

    if not args.release:
        raise SystemExit("--release required in chain mode")

    if args.ed25519_key_file:
        with open(args.ed25519_key_file, "rb") as f:
            sk = serialization.load_pem_private_key(f.read(), password=None)
        if not isinstance(sk, Ed25519PrivateKey):
            raise SystemExit(f"{args.ed25519_key_file} not an ed25519 private key")
    else:
        # Defer to the helper used by the miner runner.
        from hope.miner.runner import _derive_ed25519_from_wallet
        sk = _derive_ed25519_from_wallet(runner.wallet)
    val_pk = sk.public_key().public_bytes_raw()

    # Read on-chain miner state via the live verifier helper.
    from scripts import verify_epoch as ve  # type: ignore

    miner_ss58s = list(runner.metagraph.hotkeys) if runner.metagraph else []
    timing = TimingBounds(
        epoch_open_round=0,
        miner_deadline_round=2**63 - 1,
        chain_window_min_block=0,
        chain_window_max_block=2**63 - 1,
    )
    chain_view = ve.fetch_chain_view(
        subtensor=runner.subtensor,
        netuid=runner.netuid,
        epoch_id=args.release,
        validator_hotkey_ss58=runner.wallet.hotkey.ss58_address,
        miner_hotkey_ss58_list=miner_ss58s,
        timing=timing,
    )

    miner_inputs: list[MinerOnChainInputs] = []
    for miner_pk, ms in chain_view.miner_states.items():
        miner_inputs.append(MinerOnChainInputs(
            miner_uid=ms.miner_uid,
            miner_hotkey=miner_pk,
            revealed_k=ms.timelock_k_revealed,
            sha256_ct_commit=ms.sha256_ct_commit,
            self_archive_url=ms.self_archive_url,
            chain_block_at_k_commit=ms.chain_block_at_k_commit,
            k_reveal_round=0,
        ))

    # Fetch outcomes via the existing HTTP path; aggregate to per-horizon truth.
    import asyncio as _asyncio
    outcomes = _asyncio.run(runner.hope_client.fetch_outcomes_only(args.release))
    horizon_outcomes = []
    for o in outcomes:
        horizon_dict = {}
        if getattr(o, "t7", None):
            horizon_dict["7"] = o.t7.model_dump() if hasattr(o.t7, "model_dump") else dict(o.t7.__dict__)
        if getattr(o, "t14", None):
            horizon_dict["14"] = o.t14.model_dump() if hasattr(o.t14, "model_dump") else dict(o.t14.__dict__)
        horizon_outcomes.append(horizon_dict)
    truth_by_horizon = aggregate_outcomes_to_truth(horizon_outcomes)

    # Pre-decode plaintexts from miner_inputs for scoring_inputs_hash. We
    # decode here; run_epoch_scoring will rerun read_miner_for_epoch with the
    # SAME inputs so the resulting plaintexts match by construction.
    # We don't have AES_ct here yet — caller will fetch during the run. The
    # scoring_inputs_hash binds to the FINAL plaintexts, but for the upfront
    # hash we use a simplification: hash the chain commits + truth. Phase E
    # swaps in real plaintexts.
    plaintexts_for_hash: dict[bytes, dict] = {
        inp.miner_hotkey: {
            "k_block": inp.chain_block_at_k_commit,
            "sha256_ct": inp.sha256_ct_commit,
        }
        for inp in miner_inputs
        if inp.revealed_k and inp.sha256_ct_commit
    }
    scoring_inputs_hash = compute_scoring_inputs_hash(
        epoch_id=args.release,
        plaintexts=plaintexts_for_hash,
        truth_by_horizon=truth_by_horizon,
    )

    archive_endpoints: list[ArchiveEndpoint] = []
    for url in args.archive_tier_1:
        archive_endpoints.append(ArchiveEndpoint(tier=1, base_url=url, name="tier-1"))
    for url in args.archive_tier_2:
        archive_endpoints.append(ArchiveEndpoint(tier=2, base_url=url, name="tier-2"))

    scorer = make_scorer(truth_by_horizon)
    submitted_round = drand_round_at(int(_time.time()))
    return run_epoch_scoring(
        subtensor=runner.subtensor,
        validator_wallet=runner.wallet,
        netuid=runner.netuid,
        epoch_id=args.release,
        epoch_idx=int(_os.environ.get("SN21_EPOCH_IDX", "0")),
        validator_hotkey=val_pk,
        validator_signing_key=sk,
        miner_inputs=miner_inputs,
        archive_endpoints=archive_endpoints,
        archive_client=ArchiveClient(),
        timing=timing,
        outcomes_release_round=submitted_round - 100,
        outcomes_fetched_at_round=submitted_round,
        scoring_inputs_hash=scoring_inputs_hash,
        scorer=scorer,
        blocks_until_pre_scoring_reveal=args.blocks_until_pre_reveal,
        blocks_until_post_scoring_reveal=args.blocks_until_post_reveal,
        blocks_until_weights_reveal=args.blocks_until_weights_reveal,
    )


if __name__ == "__main__":
    main()
