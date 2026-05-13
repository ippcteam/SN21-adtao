"""Validator Runner — main entry point for SN21 validators.

Runs one epoch of the Layer 9.B–9.C on-chain scoring path:

  1. Initialize Bittensor wallet + subtensor + metagraph.
  2. Read each registered miner's on-chain commits (TLE'd K, Sha256(AES_ct),
     Raw(URL)) via the live verifier helper.
  3. Fetch revealed AES_ct from the configured archive endpoints.
  4. Decrypt and validate each prediction (inner_sig + scoreability rule).
  5. Fetch outcomes from the operator's data API (provably after deadline).
  6. Score predictions and commit pre-scoring (9.C.1), weights (9.C.3),
     post-scoring (9.C.2), and retry-log (9.C.6) via timelocked commits.

The legacy HTTP-only scoring path was removed — every input that affects
a miner's score is now anchored on chain, so any third party can re-run
the verifier and confirm the score.
"""

from __future__ import annotations

import logging
import os

from hope.validator.data_client import HopeDataClient

logger = logging.getLogger(__name__)


class ValidatorRunner:
    """Holds chain / wallet / data-API state for one on-chain epoch."""

    def __init__(
        self,
        hope_api_key: str = "",
        hope_api_url: str = "",
        network: str = "finney",
        netuid: int = 21,
        wallet_name: str = "sn21_validator",
        wallet_hotkey: str = "default",
        no_chain: bool = False,
    ):
        self.hope_client = HopeDataClient(api_key=hope_api_key, base_url=hope_api_url)
        self.no_chain = no_chain

        # Bittensor components (initialized lazily by init_bittensor)
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


def main():
    """CLI entry point for the validator — Layers 9.B–9.C on-chain only."""
    import argparse

    from hope._cli_help import SafeHelpFormatter
    parser = argparse.ArgumentParser(
        description="SN21 Validator", formatter_class=SafeHelpFormatter,
    )
    parser.add_argument("--release", type=str,
                        default=os.environ.get("RELEASE_KEY", ""),
                        help="Release key (epoch ID) to score (or set RELEASE_KEY env var)")
    parser.add_argument("--api-key", type=str,
                        default=os.environ.get("HOPE_API_KEY", ""),
                        help="data API key (or set HOPE_API_KEY env var)")
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
                        help="Run without Bittensor chain (offline development only)")
    parser.add_argument("--archive-tier-1", action="append", default=[],
                        help="Tier-1 archive base URL (repeat for multi)")
    parser.add_argument("--archive-tier-2", action="append", default=[],
                        help="Tier-2 archive base URL (repeat for multi). Optional — "
                             "miners' Tier-3 self-archive URLs (read from chain) are the "
                             "primary fetch source; Tier-2 is operator redundancy.")
    parser.add_argument("--blocks-until-pre-reveal", type=int, default=300,
                        help="9.C.1 reveal delay (default 300 blocks ≈ 1h)")
    parser.add_argument("--blocks-until-post-reveal", type=int, default=600,
                        help="9.C.2 reveal delay (default 600 blocks ≈ 2h)")
    parser.add_argument("--blocks-until-weights-reveal", type=int, default=360,
                        help="9.C.3 weights reveal delay (default 360 blocks)")
    parser.add_argument("--ed25519-key-file", default=None,
                        help="Path to ed25519 PEM private key for inner_sig; "
                             "if omitted and the wallet hotkey is ed25519, that key is used")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.release:
        parser.error("--release is required")

    runner = ValidatorRunner(
        hope_api_key=args.api_key,
        network=args.network,
        netuid=args.netuid,
        wallet_name=args.wallet_name,
        wallet_hotkey=args.wallet_hotkey,
        no_chain=args.no_chain,
    )
    runner.init_bittensor()

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
        # First-scoring path: validator hasn't published 9.C.1/9.C.2 yet —
        # this run is about to CREATE them. The audit verifier (verify_epoch.py)
        # uses the default require_validator_reveals=True.
        require_validator_reveals=False,
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
    # hash we use a simplification: hash the chain commits + truth (an in-progress
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

    # Stamp chain_fetch_timestamp BEFORE the run starts — closest moment
    # to when the validator observed chain state. Used by the reporter
    # if SN21_LEADERBOARD_REPORTER is enabled.
    from datetime import datetime as _dt, timezone as _tz
    chain_fetch_timestamp = _dt.now(_tz.utc).isoformat()

    result = run_epoch_scoring(
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

    # Reporter hook — writes the operator-private epoch artifact when
    # SN21_LEADERBOARD_REPORTER=1 AND the scoring run actually completed
    # successfully. Aborted runs (no_miner_reveals_visible,
    # insufficient_budget, weights_commit_failed, ...) are operational
    # issues rather than "zero qualifying miners"; they should NOT
    # produce a leaderboard placeholder report. Wrapped in try/except so
    # a reporter failure can never bring down the scoring run itself.
    from hope.reporting.flags import reporter_enabled
    if reporter_enabled():
        if not result.ok:
            logger.warning(
                "epoch artifact skipped: scoring run did not complete cleanly "
                "(aborted_reason=%s). Re-trigger the cron to retry; no leaderboard "
                "POST will be made until a successful run produces an artifact.",
                result.aborted_reason,
            )
        else:
            try:
                from hope.reporting.epoch_artifact import build_and_write_artifact
                total_uids = runner.metagraph.n if runner.metagraph else len(miner_ss58s)
                artifact_path = build_and_write_artifact(
                    outcome=result,
                    epoch_id=args.release,
                    total_registered_uids=int(total_uids),
                    chain_fetch_timestamp=chain_fetch_timestamp,
                )
                logger.info("epoch artifact written: %s", artifact_path)
            except Exception as e:
                logger.warning("epoch artifact write failed (scoring unaffected): %s", e)

    return result


if __name__ == "__main__":
    main()
