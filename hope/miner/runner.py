"""Miner Runner — main entry point for SN21 miners.

Runs one epoch via the Layer 9.B on-chain submission path:

  1. Fetch episodes from the validator's HTTP episode API (Phase D —
     episodes will move to the on-chain `episodes_root` in Phase E).
  2. Generate a `Prediction` per episode using the configured model.
  3. Aggregate per-horizon entries into the canonical CBOR payload.
  4. AES-GCM encrypt the payload, timelock-encrypt the AES key to a
     future drand round, and commit Sha256(AES_ct) + TLE'd K + Raw(URL)
     on chain.
  5. Push AES_ct to the configured Tier-2 / Tier-3 archive endpoints.

The legacy HTTP-only submission path was removed — every prediction is
now anchored on chain so it can be independently verified.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.commitment.archives import ArchiveEndpoint
from hope.commitment.prediction_payload import build_horizon_entry
from hope.miner.episode_client import EpisodeClient
from hope.miner.onchain_submitter import (
    MinerSubmissionResult,
    submit_miner_epoch,
)
from hope.miner.prediction_engine import PredictionEngine
from hope.miner.models.baseline import BaselineModel
from hope.protocol.prediction import Prediction

logger = logging.getLogger(__name__)


class MinerRunner:
    """Wraps a prediction model and runs one Layer 9.B on-chain epoch."""

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
                inner_sig. The on-chain binding from Bittensor hotkey to
                ed25519 public key is established once via
                `scripts/sn21_keys.py register` (or
                `hope.commitment.registration.submit_registration_commit`).
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


def main():
    """CLI entry point for the miner — Layer 9.B on-chain only."""
    import argparse

    from hope._cli_help import SafeHelpFormatter
    parser = argparse.ArgumentParser(
        description="SN21 Miner", formatter_class=SafeHelpFormatter,
    )
    parser.add_argument("--validator-url", type=str, default="http://localhost:8080",
                        help="Validator HTTP API URL (used to fetch episodes only — "
                             "Phase D)")
    parser.add_argument("--wallet-name", type=str, required=True,
                        help="Bittensor wallet name")
    parser.add_argument("--wallet-hotkey", type=str, default="default",
                        help="Wallet hotkey name")
    parser.add_argument("--epoch", type=str, required=True,
                        help="Epoch ID (release_key) to submit predictions for")
    parser.add_argument("--model", type=str, default="baseline",
                        choices=["baseline", "trained"],
                        help="Prediction model. 'baseline' = bundled reference model. "
                             "'trained' = load a model file produced by "
                             "`scripts/train_example_model.py --save-model <path>`.")
    parser.add_argument("--model-file", type=str, default=None,
                        help="Path to a saved model file (required when --model trained).")
    parser.add_argument("--netuid", type=int, default=21,
                        help="Subtensor netuid (default: 21 mainnet; testnet is 466)")
    parser.add_argument("--bt-network", default="finney",
                        help="Bittensor network name (default: finney; "
                             "use 'test' for testnet)")
    parser.add_argument("--archive-tier-2", action="append", default=[], required=True,
                        help="Tier-2 archive base URL (repeat for multiple)")
    parser.add_argument("--archive-tier-3", required=True,
                        help="Tier-3 (self) archive base URL")
    parser.add_argument("--blocks-until-reveal", type=int, default=300,
                        help="Subtensor blocks until K auto-decrypts")
    parser.add_argument("--ed25519-key-file", default=None,
                        help="Path to ed25519 PEM private key for inner_sig; "
                             "if omitted and the wallet hotkey is ed25519, that key is used")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Bittensor's own logging wrapper (loguru-based) attaches a colored
    # handler to the root logger by default, which causes (a) every log
    # line to appear twice and (b) raw ANSI escapes to leak into piped
    # output (e.g. `hope-miner ... | tee miner.log`). Force `propagate=False`
    # on the bittensor loggers and disable ANSI when stdout isn't a TTY.
    import sys as _sys
    try:
        import bittensor as _bt  # noqa: F401
        for name in ("bittensor", "btlogging", "loguru"):
            lg = logging.getLogger(name)
            lg.propagate = False
        if not _sys.stdout.isatty():
            try:
                from bittensor.utils import btlogging as _btlog  # type: ignore
                if hasattr(_btlog, "logging") and hasattr(_btlog.logging, "set_config"):
                    _btlog.logging.set_config(colors=False)
            except Exception:
                pass
    except ImportError:
        pass

    import bittensor as bt
    wallet = bt.Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)
    hotkey = wallet.hotkey.ss58_address
    logger.info(f"Loaded wallet {args.wallet_name}/{args.wallet_hotkey}: {hotkey[:16]}...")

    if args.model == "trained":
        if not args.model_file:
            parser.error("--model trained requires --model-file <path>")
        from hope.miner.models.trained import TrainedXGBoostModel
        model_instance = TrainedXGBoostModel(args.model_file)
    else:
        model_instance = BaselineModel()

    runner = MinerRunner(
        model=model_instance,
        hotkey=hotkey,
        wallet=wallet,
        validator_url=args.validator_url,
    )

    try:
        result = _run_epoch_onchain_cli(args, runner, wallet)
    except RuntimeError as e:
        # episode_client raises RuntimeError with a translated message for
        # validator HTTP errors (401/403/404/422). Surface it cleanly so
        # the miner sees actionable text instead of a traceback.
        import sys as _sys
        print(f"\n❌ {e}", file=_sys.stderr)
        _sys.exit(1)
    print("\nResults:")
    for k, v in result.items():
        print(f"  {k}: {v}")

    # Submission already landed and was logged to chain; bypass the slow
    # interpreter shutdown path that bittensor + xgboost + httpx
    # generators trigger via finalize_modules → _PyGC_CollectNoFail (~12
    # min on a fresh wallet). os._exit returns the success code without
    # running atexit / __del__ chains. Safe here because all critical
    # state (chain commits, archive uploads) is durable; no buffered
    # writes are still pending. stdout/stderr are flushed first.
    import os as _os
    import sys as _sys
    _sys.stdout.flush()
    _sys.stderr.flush()
    _os._exit(0)


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
        endpoints.append(ArchiveEndpoint(tier=2, base_url=url, name=f"tier-2-{i}"))
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
        "bundle_block": result.chain_bundle_commit.block_number if result.chain_bundle_commit else None,
        "reveal_round": result.chain_bundle_commit.reveal_round if result.chain_bundle_commit else None,
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
