"""Miner Runner — main entry point for SN21 miners.

Handles the complete miner lifecycle:
1. Load Bittensor wallet for signing
2. Fetch episodes from validator HTTP API
3. Run prediction model on each episode
4. Submit signed predictions

Submission paths:
- HTTP path (legacy): submits a JSON payload to the validator API.
- ON-CHAIN path (Layer 9.B, default in chain mode): packages predictions as
  AES-GCM(ciphertext) + TimelockEncrypted(K) + Sha256(ct) + Raw(URL) and
  publishes to the chain plus the Tier-2/Tier-3 archive endpoints.

Use `run_epoch_onchain(...)` or pass `--mode onchain` on the CLI to run the
verifiable-scoring path. The HTTP path stays available for the operator's own data
ingest until the chain path is the authoritative source.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.archives import ArchiveEndpoint
from hope.commitment.prediction_payload import build_horizon_entry
from hope.miner.episode_client import EpisodeClient
from hope.miner.onchain_submitter import (
    MinerSubmissionResult,
    submit_miner_epoch,
)
from hope.miner.prediction_client import PredictionClient
from hope.miner.prediction_engine import PredictionEngine
from hope.miner.models.baseline import BaselineModel
from hope.protocol.prediction import Prediction

logger = logging.getLogger(__name__)


class MinerRunner:
    """Main miner runner — HTTP only, wallet-authenticated."""

    def __init__(
        self,
        model: PredictionEngine | None = None,
        hotkey: str = "",
        wallet=None,
        validator_url: str = "http://localhost:8080",
    ):
        self.model = model or BaselineModel()
        self.hotkey = hotkey
        self.wallet = wallet
        self.validator_url = validator_url
        self.episode_client = EpisodeClient(hotkey=hotkey, wallet=wallet)
        self.prediction_client = PredictionClient(hotkey=hotkey, wallet=wallet)

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

    def run_epoch_onchain(
        self,
        *,
        epoch_id: str,
        episodes: list,
        subtensor,
        miner_wallet,
        netuid: int,
        miner_hotkey_bytes: bytes,
        miner_signing_key: Ed25519PrivateKey,
        submitted_round: int,
        archive_endpoints: list[ArchiveEndpoint],
        self_archive_url: str,
        blocks_until_reveal: int,
        upload_auth_headers: Optional[dict[str, str]] = None,
    ) -> MinerSubmissionResult:
        """Run one epoch and submit predictions via the Layer 9.B chain path.

        The miner generates a `Prediction` per episode (via `self.model`),
        converts each into the canonical-CBOR horizon entries, packages them
        into one prediction blob with inner_sig, and submits:
          1. AES_ct → archive endpoints (Tier-1/2/3).
          2. TimelockEncrypted(K) → chain.
          3. Sha256(AES_ct)        → chain.
          4. Raw(self_archive_url) → chain.

        Args:
            epoch_id: the operator release_key.
            episodes: list of Episode objects; same shape `self.model.predict`
                accepts. The runner does NOT fetch episodes here — the caller
                owns episode discovery (HTTP, GraphQL, on-chain `episodes_root`
                lookup, etc.) and passes them in.
            subtensor: Bittensor `Subtensor` instance.
            miner_wallet: Bittensor `Wallet` whose hotkey signs commits.
            netuid: subnet ID.
            miner_hotkey_bytes: 32-byte raw ed25519 public key matching
                `miner_signing_key`. Embedded in the prediction CBOR.
            miner_signing_key: ed25519 private key used to sign the prediction
                inner_sig. Phase D will bind this to the Bittensor hotkey via
                a registration commit; for now it is supplied externally.
            submitted_round: drand round at submission time (must be within
                the epoch's [open, deadline] window).
            archive_endpoints: list of (tier, base_url) targets for AES_ct.
            self_archive_url: Tier-3 URL announced on chain.
            blocks_until_reveal: subtensor blocks until K auto-decrypts.
            upload_auth_headers: optional headers attached to archive POSTs.

        Returns:
            `MinerSubmissionResult` with per-step outcomes.
        """
        self.model.on_epoch_start(epoch_id, len(episodes))

        predictions: list[Prediction] = []
        for ep in episodes:
            try:
                predictions.append(self.model.predict(ep))
            except Exception as e:
                ep_id = getattr(getattr(ep, "episode_metadata", None), "episode_id", "?")
                logger.warning("predict failed for %s: %s", ep_id, e)

        horizons = self._predictions_to_horizon_entries(predictions)
        if not horizons:
            raise ValueError(
                f"epoch {epoch_id}: no horizon entries to submit "
                f"(0/{len(episodes)} episodes produced predictions)"
            )

        return submit_miner_epoch(
            subtensor=subtensor,
            miner_wallet=miner_wallet,
            netuid=netuid,
            epoch_id=epoch_id,
            miner_hotkey=miner_hotkey_bytes,
            miner_signing_key=miner_signing_key,
            submitted_round=submitted_round,
            horizons=horizons,
            self_archive_url=self_archive_url,
            archive_endpoints=archive_endpoints,
            blocks_until_reveal=blocks_until_reveal,
            upload_auth_headers=upload_auth_headers,
        )

    @staticmethod
    def _predictions_to_horizon_entries(predictions: list[Prediction]) -> list[dict]:
        """Aggregate per-episode `Prediction`s into per-horizon CBOR entries.

        The Layer 9.B prediction CBOR carries ONE entry per horizon for the
        whole epoch (e.g. P10/P50/P90 over the entire batch's deltas), not
        per episode. We average each episode's quantiles within the horizon
        — a deterministic shape that's verifier-reproducible and matches
        the launch-default submit path. Miners that want per-(episode,
        horizon) granularity should pass `per_episode_entries=...` to
        `submit_miner_epoch(...)` directly (Phase E path); see
        `hope/commitment/episode_artifacts.py`.
        """
        if not predictions:
            return []

        per_horizon: dict[str, list[Prediction]] = {}
        for p in predictions:
            for h in p.horizons:
                per_horizon.setdefault(h, []).append(p)

        entries: list[dict] = []
        for horizon, preds in sorted(per_horizon.items()):
            cost_p10 = _avg(p.horizons[horizon].cost_delta_pct.p10 for p in preds)
            cost_p50 = _avg(p.horizons[horizon].cost_delta_pct.p50 for p in preds)
            cost_p90 = _avg(p.horizons[horizon].cost_delta_pct.p90 for p in preds)
            conv_p10 = _avg(p.horizons[horizon].conversions_delta_pct.p10 for p in preds)
            conv_p50 = _avg(p.horizons[horizon].conversions_delta_pct.p50 for p in preds)
            conv_p90 = _avg(p.horizons[horizon].conversions_delta_pct.p90 for p in preds)
            eff_p10 = _avg(p.horizons[horizon].efficiency_delta_pct.p10 for p in preds)
            eff_p50 = _avg(p.horizons[horizon].efficiency_delta_pct.p50 for p in preds)
            eff_p90 = _avg(p.horizons[horizon].efficiency_delta_pct.p90 for p in preds)
            miss = _avg(p.horizons[horizon].goal_miss_probability for p in preds)
            instab = _avg(p.horizons[horizon].instability_risk for p in preds)
            entries.append(build_horizon_entry(
                horizon=horizon,
                cost_q=(cost_p10, cost_p50, cost_p90),
                conv_q=(conv_p10, conv_p50, conv_p90),
                eff_q=(eff_p10, eff_p50, eff_p90),
                goal_miss_probability=max(0.0, min(1.0, miss)),
                instability_risk=max(0.0, min(1.0, instab)),
            ))
        return entries

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

    parser = argparse.ArgumentParser(description="SN21 Miner")
    parser.add_argument("--validator-url", type=str, default="http://localhost:8080",
                        help="Validator HTTP API URL")
    parser.add_argument("--wallet-name", type=str, required=True,
                        help="Bittensor wallet name (required for signing)")
    parser.add_argument("--wallet-hotkey", type=str, default="default",
                        help="Wallet hotkey name")
    parser.add_argument("--epoch", type=str, default=None,
                        help="Epoch ID (omit for auto-discover from validator)")
    parser.add_argument("--model", type=str, default="baseline",
                        choices=["baseline"], help="Prediction model")
    parser.add_argument("--continuous", action="store_true",
                        help="Run continuous (polls validator for new epochs)")
    parser.add_argument("--poll-interval", type=int, default=30,
                        help="Seconds between health checks in continuous mode")
    parser.add_argument("--mode", choices=["http", "onchain"], default="http",
                        help="Submission path (http=legacy, onchain=Layer 9.B)")
    parser.add_argument("--netuid", type=int, default=21,
                        help="Subtensor netuid (chain mode only)")
    parser.add_argument("--bt-network", default="finney",
                        help="Bittensor network name (chain mode only)")
    parser.add_argument("--archive-tier-2", action="append", default=[],
                        help="Tier-2 archive base URL (chain mode; repeat for multiple)")
    parser.add_argument("--archive-tier-3", default=None,
                        help="Tier-3 (self) archive base URL (chain mode only)")
    parser.add_argument("--blocks-until-reveal", type=int, default=300,
                        help="Subtensor blocks until K auto-decrypts (chain mode)")
    parser.add_argument("--ed25519-key-file", default=None,
                        help="Path to ed25519 PEM private key for inner_sig (chain mode); "
                             "if omitted and the wallet hotkey is ed25519, that key is used")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Load Bittensor wallet for request signing
    import bittensor as bt
    wallet = bt.wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)
    hotkey = wallet.hotkey.ss58_address
    logger.info(f"Loaded wallet {args.wallet_name}/{args.wallet_hotkey}: {hotkey[:16]}...")

    runner = MinerRunner(
        model=BaselineModel(),
        hotkey=hotkey,
        wallet=wallet,
        validator_url=args.validator_url,
    )

    if args.mode == "onchain":
        if not args.epoch:
            parser.error("--mode onchain requires --epoch (auto-discover unsupported in chain mode)")
        if not args.archive_tier_2 or not args.archive_tier_3:
            parser.error("--mode onchain requires --archive-tier-2 and --archive-tier-3")
        result = _run_epoch_onchain_cli(args, runner, wallet)
        print("\nResults:")
        for k, v in result.items():
            print(f"  {k}: {v}")
    elif args.continuous:
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


def _avg(it) -> float:
    """Mean of an iterable of floats; returns 0.0 if empty."""
    vals = list(it)
    return sum(vals) / len(vals) if vals else 0.0


def _run_epoch_onchain_cli(args, runner: "MinerRunner", wallet) -> dict:
    """Bridge from CLI flags to `MinerRunner.run_epoch_onchain`.

    Loads chain + ed25519 signing key, fetches episodes via HTTP (the chain
    path doesn't yet read episodes from the on-chain `episodes_root`; that
    is Phase E), and submits one epoch.
    """
    import bittensor as bt
    from cryptography.hazmat.primitives import serialization
    from hope.commitment.archives import ArchiveEndpoint
    from hope.commitment.drand_lib import drand_round_at
    import time as _time

    subtensor = bt.Subtensor(network=args.bt_network)

    # Load (or derive) the ed25519 inner_sig signing key.
    if args.ed25519_key_file:
        with open(args.ed25519_key_file, "rb") as f:
            sk = serialization.load_pem_private_key(f.read(), password=None)
        if not isinstance(sk, Ed25519PrivateKey):
            raise SystemExit(f"{args.ed25519_key_file} is not an ed25519 private key")
    else:
        sk = _derive_ed25519_from_wallet(wallet)
    miner_pk = sk.public_key().public_bytes_raw()

    # Episode discovery still goes via HTTP (Phase D).
    episodes = asyncio.run(
        runner.episode_client.fetch_all_episodes(args.validator_url, args.epoch)
    )

    endpoints: list[ArchiveEndpoint] = []
    for i, url in enumerate(args.archive_tier_2):
        endpoints.append(ArchiveEndpoint(tier=2, base_url=url, name=f"hope-{i}"))
    if args.archive_tier_3:
        endpoints.append(ArchiveEndpoint(tier=3, base_url=args.archive_tier_3, name="self"))

    submitted_round = drand_round_at(int(_time.time()))

    result = runner.run_epoch_onchain(
        epoch_id=args.epoch,
        episodes=episodes,
        subtensor=subtensor,
        miner_wallet=wallet,
        netuid=args.netuid,
        miner_hotkey_bytes=miner_pk,
        miner_signing_key=sk,
        submitted_round=submitted_round,
        archive_endpoints=endpoints,
        self_archive_url=args.archive_tier_3 or args.archive_tier_2[0],
        blocks_until_reveal=args.blocks_until_reveal,
    )
    return {
        "ok": result.ok,
        "failure_reason": result.failure_reason,
        "k_block": result.chain_k_commit.block_number if result.chain_k_commit else None,
        "sha_block": result.chain_sha_commit.block_number if result.chain_sha_commit else None,
        "url_block": result.chain_url_commit.block_number if result.chain_url_commit else None,
        "reveal_round": result.chain_k_commit.reveal_round if result.chain_k_commit else None,
        "uploads_ok": [(r.endpoint.tier, r.ok) for r in result.archive_uploads],
    }


def _derive_ed25519_from_wallet(wallet) -> Ed25519PrivateKey:
    """Best-effort ed25519 derivation from a Bittensor wallet's hotkey.

    Bittensor `Keypair` exposes `private_key` bytes when crypto_type is ed25519.
    For sr25519 hotkeys, this raises — the caller must supply --ed25519-key-file.
    """
    hotkey = getattr(wallet, "hotkey", None)
    if hotkey is None:
        raise SystemExit("wallet has no hotkey")
    crypto_type = getattr(hotkey, "crypto_type", None)
    if crypto_type is not None and int(crypto_type) != 0:  # 0 = Ed25519, 1 = sr25519
        raise SystemExit(
            "wallet hotkey is not ed25519; supply --ed25519-key-file or "
            "regenerate the wallet with crypto_type=Ed25519"
        )
    private_key = getattr(hotkey, "private_key", None)
    if private_key is None:
        raise SystemExit(
            "wallet.hotkey.private_key not available; supply --ed25519-key-file"
        )
    if isinstance(private_key, str):
        private_key = bytes.fromhex(private_key[2:] if private_key.startswith("0x") else private_key)
    if len(private_key) == 64:
        # ed25519 secret keys are sometimes stored as the 64-byte expanded form;
        # the seed is the first 32 bytes.
        private_key = private_key[:32]
    if len(private_key) != 32:
        raise SystemExit(f"ed25519 private key has unexpected length: {len(private_key)}")
    return Ed25519PrivateKey.from_private_bytes(private_key)


if __name__ == "__main__":
    main()
