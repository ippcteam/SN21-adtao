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

            from hope.validator._subtensor import make_subtensor
            self.subtensor = make_subtensor(self.network)
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
    from hope.validator._subtensor import network_arg
    parser.add_argument("--network", type=network_arg, default="finney",
                        help="Bittensor network: 'test', 'finney', 'local', "
                             "or a wss:// URL (e.g. wss://archive.example:443). "
                             "Default: finney mainnet; use 'test' for testnet "
                             "(netuid 466). For a custom archive RPC, prefer "
                             "the SN21_SUBTENSOR_URL env var so every binary "
                             "in the stack picks it up uniformly.")
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
    parser.add_argument("--reg-index-lookback-blocks", type=int, default=600,
                        help="How many blocks back from chain head to scan for "
                             "miner ed25519 registration commits (default: 600 "
                             "≈ 2 hours on 12s testnet blocks — enough to catch "
                             "registrations published right before this scoring "
                             "run). Per-block scans cost ~1 RPC second on testnet, "
                             "so wider lookbacks are impractical to run inline "
                             "with the cron. For initial backfill, run "
                             "`scripts/diag/probe_registration_index.py` against "
                             "an archive-node RPC and persist the result. Set to 0 "
                             "to skip the in-cron scan entirely.")
    parser.add_argument("--reg-index-prebuilt", type=str, default=None,
                        help="Optional path to a JSON file containing a prebuilt "
                             "registration index from an offline backfill run. "
                             "When present, its entries are merged into the index "
                             "BEFORE the in-cron incremental scan. Format: a JSON "
                             "list of {hotkey_ss58, hotkey_pk_hex, ed25519_pk_hex, "
                             "block_number} objects.")
    parser.add_argument(
        "--ignore-already-scored", action="store_true",
        default=os.environ.get("SN21_IGNORE_ALREADY_SCORED", "").lower() in ("1", "true", "yes"),
        help="Bypass the per-(validator,epoch) already_scored guard. The byte-"
             "budget check remains active. Operator opt-in only — intended for "
             "recovery from runs that landed a 9.C.1 stub without proceeding to "
             "9.C.3. Settable via SN21_IGNORE_ALREADY_SCORED=1 env.",
    )
    parser.add_argument(
        "--report-only", action="store_true",
        default=os.environ.get("SN21_REPORT_ONLY", "").lower() in ("1", "true", "yes"),
        help="Score + write the leaderboard artifact but make NO chain commits "
             "(no 9.C, no set_weights). For rebuilding/correcting a report when "
             "the on-chain 9.C already exists — immune to the Commitments space "
             "budget and the weights rate limit. Settable via SN21_REPORT_ONLY=1.",
    )

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.release or args.release.strip().lower() == "auto":
        # POST-WEEKLY WORLD (2026-08-24 onward). The weekly (WR-) epochs have
        # wound down, so --release auto can no longer resolve a scoreable weekly
        # epoch — the resolver raises on every run. When the daily stream is on,
        # commit the executor-published DAILY vector directly and skip the weekly
        # resolution ENTIRELY: attempting it every tick raised a caught
        # RuntimeError, and the fallback then tripped over a None subtensor
        # whenever the RPC was briefly unreachable, turning a benign miss into a
        # NoneType traceback. The daily commit uses the SAME composition the
        # epoch path used (allowlist -> alpha gate -> burn/override -> 9.C.3).
        _daily_on = os.environ.get(
            "SN21_DAILY_STREAM_WEIGHTS", "").strip().lower() in (
            "1", "true", "yes", "on")
        if _daily_on and not args.no_chain:
            _runner = ValidatorRunner(
                hope_api_key=args.api_key,
                network=args.network,
                netuid=args.netuid,
                wallet_name=args.wallet_name,
                wallet_hotkey=args.wallet_hotkey,
                no_chain=args.no_chain,
            )
            _runner.init_bittensor()
            from hope.validator._log import configure_logging
            configure_logging(logger, "INFO")
            if _runner.subtensor is None:
                # init_bittensor swallows a connection failure and sets
                # no_chain; a brief RPC outage must not crash the tick. Skip and
                # retry — the prior on-chain vector stands, as a gated day would.
                logger.warning(
                    "daily weight commit skipped: chain unreachable this tick "
                    "(subtensor unavailable); will retry next tick")
                return 1
            from hope.validator.onchain_runner import run_daily_weights_only
            _res = run_daily_weights_only(
                subtensor=_runner.subtensor,
                validator_wallet=_runner.wallet,
                netuid=args.netuid,
            )
            print("\nDaily-weights-only outcome:")
            print(f"  ok: {_res.success}")
            print(f"  message: {_res.message}")
            print(f"  block: {_res.block_number}")
            return 0 if _res.success else 1

        # WEEKLY STREAM (daily stream off). Resolve the SCORING target: the
        # latest CLOSED epoch, i.e. the release *before* the currently-open
        # (newest) one. The scorer must NOT use the newest (open submission)
        # release — it has no bundles yet and yields an empty run. See
        # HopeDataClient.discover_scoreable_release.
        import asyncio as _asyncio

        from hope.validator.data_client import HopeDataClient
        try:
            client = HopeDataClient()
        except ValueError as exc:
            parser.error(
                f"--release auto requires HOPE_API_KEY and HOPE_API_URL to "
                f"be set; {exc}"
            )
        try:
            args.release = _asyncio.run(client.discover_scoreable_release())
        except Exception as exc:
            parser.error(
                f"--release auto failed to resolve a scoreable (closed) epoch: "
                f"{type(exc).__name__}: {exc}. Either pass --release <EPOCH_ID> "
                f"explicitly or check the operator data backend at {client.base_url}."
            )
        logger.info("--release auto resolved to scoreable epoch %s", args.release)

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

    # `import bittensor` (inside init_bittensor) calls logging.disable(), which
    # silences our scoring/abort/weights-commit lines. Restore a visible handler
    # so the scoring run is auditable in production logs.
    from hope.validator._log import configure_logging
    configure_logging(logger, "INFO")

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


_RPC_ROTATION_MAX_ATTEMPTS = 4
_RPC_ROTATION_SLEEP_SECONDS = 5.0


def _count_visible_miners(chain_view) -> int:
    """Return the number of miners with a non-None timelock_k_revealed."""
    return sum(
        1
        for ms in chain_view.miner_states.values()
        if ms.timelock_k_revealed is not None
    )


def _subtensor_endpoint_description(subtensor) -> str:
    """Best-effort description of which RPC URL this Subtensor is talking to.

    Bittensor wraps an async_substrate_interface SubstrateInterface; the URL
    is on the underlying instance. Schema may shift between SDK versions, so
    fall back to '<unknown>' rather than crashing.
    """
    try:
        substrate = getattr(subtensor, "substrate", None)
        url = (
            getattr(substrate, "url", None)
            or getattr(substrate, "_url", None)
            or getattr(subtensor, "chain_endpoint", None)
        )
        if url:
            return str(url)
    except Exception:
        pass
    return "<unknown>"


def _current_block_or_none(subtensor) -> int | None:
    """Read the current head block via this RPC. Returns None on failure."""
    try:
        return int(subtensor.get_current_block())
    except Exception as e:
        logger.warning("get_current_block failed: %s", e)
        return None


def _make_fresh_subtensor(bt_network: str):
    """Create a new Subtensor connection, honoring SN21_SUBTENSOR_URL."""
    from hope.validator._subtensor import make_subtensor
    return make_subtensor(bt_network)


def _fetch_chain_view_with_rpc_rotation(
    *,
    initial_subtensor,
    netuid: int,
    epoch_id: str,
    validator_hotkey_ss58: str,
    miner_hotkey_ss58_list: list[str],
    timing,
    bt_network: str,
):
    """Read on-chain miner state, retrying on fresh Subtensor connections
    when 0 miners are visible.

    The Bittensor testnet RPC pool is DNS-load-balanced. A given Subtensor
    connection may land on a backend that hasn't yet replicated recent
    RevealedCommitments state — and on the operator's compute,
    DNS may stick to a backend that consistently lags. To avoid burning
    Commitments-pallet byte budget on a stale read, we:

      1. Try with the caller-supplied subtensor.
      2. If 0 miners are visible AND the metagraph has miners, drop the
         connection and instantiate a fresh `bt.Subtensor(network=...)`,
         which forces a new DNS resolution + TCP handshake — potentially
         landing on a different backend.
      3. Repeat up to _RPC_ROTATION_MAX_ATTEMPTS - 1 retries with a
         _RPC_ROTATION_SLEEP_SECONDS pause between attempts.
      4. Return whichever chain_view shows the most visible miners (and
         the matching subtensor so subsequent calls reuse the
         most-current connection).

    Returns the (chain_view, subtensor) pair. If all attempts return 0,
    the final answer is accepted and run_epoch_scoring's existing
    `no_miner_reveals_visible` abort will fire downstream.
    """
    import time as _time

    from scripts import verify_epoch as ve  # type: ignore

    def _read(sub):
        return ve.fetch_chain_view(
            subtensor=sub,
            netuid=netuid,
            epoch_id=epoch_id,
            validator_hotkey_ss58=validator_hotkey_ss58,
            miner_hotkey_ss58_list=miner_hotkey_ss58_list,
            timing=timing,
            # First-scoring path: validator hasn't published 9.C.1/9.C.2
            # yet — this run is about to CREATE them. The audit verifier
            # (verify_epoch.py) uses the default require_validator_reveals=True.
            require_validator_reveals=False,
        )

    # Diagnostics use print() rather than logger so they can never be
    # suppressed by Bittensor's loguru-vs-stdlib logging configuration.
    # The cron's log capture grabs stdout directly.
    initial_url = _subtensor_endpoint_description(initial_subtensor)
    initial_block = _current_block_or_none(initial_subtensor)
    best_view = _read(initial_subtensor)
    best_subtensor = initial_subtensor
    best_count = _count_visible_miners(best_view)
    print(
        f"[RPC-DIAG] initial read: url={initial_url} block={initial_block} "
        f"visible={best_count}/{len(miner_hotkey_ss58_list)}",
        flush=True,
    )

    if best_count > 0 or not miner_hotkey_ss58_list:
        return best_view, best_subtensor

    print(
        f"[RPC-DIAG] no miners visible on initial read (0 of "
        f"{len(miner_hotkey_ss58_list)}). Rotating up to "
        f"{_RPC_ROTATION_MAX_ATTEMPTS - 1} fresh Subtensor connections "
        f"with {_RPC_ROTATION_SLEEP_SECONDS:.0f}s between attempts.",
        flush=True,
    )

    for attempt in range(1, _RPC_ROTATION_MAX_ATTEMPTS):
        _time.sleep(_RPC_ROTATION_SLEEP_SECONDS)
        try:
            fresh = _make_fresh_subtensor(bt_network)
        except Exception as e:
            print(
                f"[RPC-DIAG] rotation {attempt}/{_RPC_ROTATION_MAX_ATTEMPTS - 1}: "
                f"connect failed: {e}",
                flush=True,
            )
            continue

        view = _read(fresh)
        count = _count_visible_miners(view)
        url = _subtensor_endpoint_description(fresh)
        block = _current_block_or_none(fresh)
        print(
            f"[RPC-DIAG] rotation {attempt}/{_RPC_ROTATION_MAX_ATTEMPTS - 1}: "
            f"url={url} block={block} "
            f"visible={count}/{len(miner_hotkey_ss58_list)}",
            flush=True,
        )

        if count > best_count:
            best_view = view
            best_subtensor = fresh
            best_count = count
            if best_count > 0:
                print(
                    f"[RPC-DIAG] rotation succeeded on attempt {attempt} "
                    f"({best_count} miners visible)",
                    flush=True,
                )
                return best_view, best_subtensor

    print(
        f"[RPC-DIAG] rotation exhausted ({_RPC_ROTATION_MAX_ATTEMPTS} attempts); "
        f"accepting final 0-miner view. If you have a known-current endpoint, "
        f"set SN21_SUBTENSOR_URL to pin (e.g. "
        f"SN21_SUBTENSOR_URL=wss://test.finney.opentensor.ai:443).",
        flush=True,
    )
    return best_view, best_subtensor


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
        predict_zero_baseline,
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

    # Read on-chain miner state via the live verifier helper. The actual
    # `ve.fetch_chain_view` call lives inside _fetch_chain_view_with_rpc_rotation;
    # this comment marks the conceptual step for readers of the runner.

    miner_ss58s = list(runner.metagraph.hotkeys) if runner.metagraph else []
    timing = TimingBounds(
        epoch_open_round=0,
        miner_deadline_round=2**63 - 1,
        chain_window_min_block=0,
        chain_window_max_block=2**63 - 1,
    )
    chain_view, runner.subtensor = _fetch_chain_view_with_rpc_rotation(
        initial_subtensor=runner.subtensor,
        netuid=runner.netuid,
        epoch_id=args.release,
        validator_hotkey_ss58=runner.wallet.hotkey.ss58_address,
        miner_hotkey_ss58_list=miner_ss58s,
        timing=timing,
        bt_network=args.network,
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

    # Build the SS58 ↔ ed25519 registration index. Miners with an sr25519
    # hotkey publish their ed25519 signing pubkey once via `sn21_keys.py
    # register`; that Raw{N} commit gets overwritten in CommitmentOf the
    # moment they submit their first TLE bundle, so we recover it from
    # Commitments-pallet event history. The index returns None for any
    # hotkey without a verified registration, in which case the scorer
    # falls back to the raw chain hotkey bytes (works for ed25519 hotkeys;
    # rejects sr25519 hotkeys on inner_sig.hotkey_mismatch).

    from hope.validator.registration_index import RegistrationIndex
    registration_index: RegistrationIndex | None = None

    # Build the index if either path is active: a non-zero in-cron lookback,
    # OR a prebuilt JSON to merge. Without this, --reg-index-lookback-blocks=0
    # (used on chains where the validator's RPC has pruned historical state)
    # silently skipped the prebuilt load too, leaving registration_index=None
    # and excluding every sr25519-hotkey miner on inner_sig.hotkey_mismatch.
    if args.reg_index_lookback_blocks > 0 or args.reg_index_prebuilt:
        registration_index = RegistrationIndex(runner.subtensor, runner.netuid)

        if args.reg_index_prebuilt:
            try:
                import json as _json
                with open(args.reg_index_prebuilt) as _f:
                    prebuilt = _json.load(_f)
                merged = registration_index.merge_json(prebuilt)
                print(
                    f"[REG-INDEX] merged {merged} entries from "
                    f"prebuilt index at {args.reg_index_prebuilt}",
                    flush=True,
                )
            except Exception as _e:
                print(
                    f"[REG-INDEX] prebuilt index load failed "
                    f"({type(_e).__name__}: {str(_e)[:120]}); continuing "
                    f"with in-cron scan only (if enabled)",
                    flush=True,
                )

        if args.reg_index_lookback_blocks > 0:
            try:
                head_block = int(runner.subtensor.get_current_block())
                start_block = max(0, head_block - args.reg_index_lookback_blocks)
                print(
                    f"[REG-INDEX] scanning blocks [{start_block}, {head_block}] "
                    f"(lookback={args.reg_index_lookback_blocks})",
                    flush=True,
                )
                found = registration_index.scan_range(start_block, head_block)
                stats = registration_index.stats
                print(
                    f"[REG-INDEX] indexed {registration_index.size} registrations "
                    f"(found {found} in this scan); "
                    f"blocks_scanned={stats['blocks_scanned']} "
                    f"events_seen={stats['events_seen']} "
                    f"candidates={stats['candidates_found']} "
                    f"verified={stats['verified']}",
                    flush=True,
                )
            except Exception as e:
                print(
                    f"[REG-INDEX] scan failed ({type(e).__name__}: {str(e)[:200]}); "
                    f"proceeding with whatever entries are already in the index "
                    f"(prebuilt merged earlier, if any). Miners without a known "
                    f"binding will be excluded as inner_sig.hotkey_mismatch.",
                    flush=True,
                )
        else:
            print(
                f"[REG-INDEX] in-cron scan disabled by "
                f"--reg-index-lookback-blocks=0; using prebuilt "
                f"({registration_index.size} entries) exclusively.",
                flush=True,
            )
    else:
        print(
            "[REG-INDEX] disabled — no prebuilt file given and "
            "--reg-index-lookback-blocks=0",
            flush=True,
        )

    scorer = make_scorer(truth_by_horizon)

    # Predict-zero baseline for the tiered participation gate
    # (SN21_REWARD_MECHANISM Component 1): the score a flat zero-quantile /
    # zero-probability prediction achieves against the same per-horizon truth.
    # Miners must beat this to qualify for a tier pool. Only consulted when
    # SN21_TIERED_WEIGHTS is enabled; harmless to compute otherwise.
    # Use the SAME scorer the miners are scored with (v2 when SN21_SCORING_V2 is
    # set) — a hardcoded-v1 baseline vs v2 miner scores gate-excluded 100% of
    # miners and burned every epoch.
    baseline_score = predict_zero_baseline(truth_by_horizon)
    print(
        f"[baseline] predict-zero baseline_score={baseline_score:.4f} "
        f"over {len(truth_by_horizon)} horizons",
        flush=True,
    )
    submitted_round = drand_round_at(int(_time.time()))

    # Stamp chain_fetch_timestamp BEFORE the run starts — closest moment
    # to when the validator observed chain state. Used by the reporter
    # if SN21_LEADERBOARD_REPORTER is enabled.
    from datetime import datetime as _dt
    from datetime import timezone as _tz
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
        registration_index=registration_index,
        ignore_already_scored=args.ignore_already_scored,
        report_only=args.report_only,
        baseline_score=baseline_score,
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
                    baseline_score=baseline_score,
                )
                logger.info("epoch artifact written: %s", artifact_path)
            except Exception as e:
                logger.warning("epoch artifact write failed (scoring unaffected): %s", e)

    return result


if __name__ == "__main__":
    main()
