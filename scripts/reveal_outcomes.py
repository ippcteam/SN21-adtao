#!/usr/bin/env python3
"""Operator tool — reveal an epoch's measured outcomes (Layer 9.A.2).

Builds the signed reveal blob from a measurement JSON, commits SHA-256(blob) on
chain under the outcome-signer wallet, and writes the blob to disk to serve.
This is the "reveal the data" step: after it finalizes, anyone can fetch the
blob and run `verify_epoch.py --truth-file <blob>` to re-derive that epoch's
scores against the on-chain 9.C roots.

COMMIT-THEN-SERVE (CL-9): only serve the blob publicly AFTER this commit lands.

Two signer materials (both belong to the outcome-signer identity, NOT the
validator):
  * --signer-ed25519-key-file : ed25519 PEM that inner-signs the blob.
  * --signer-wallet/-hotkey   : the bittensor (sr25519) wallet that submits the
                                9.A.2 extrinsic.

Measurement JSON schema (export this from the operator platform for the target epoch):

  {
    "epoch_id": "WR-2026-W21-PUB-E1",
    "epoch_idx": 21,
    "release_commit_sha256_hex": "<64 hex = W21's 9.A.1 plaintext sha256>",
    "deadline_round": 12346000,
    "measured_at_round": 12346800,
    "horizons": ["7", "14"],
    "episodes": [
      {
        "episode_id": "ep-...",
        "salt_hex": "<32 or 64 hex — the per-episode salt from the 9.A.1 build>",
        "outcomes": {
          "7":  {"cost_delta_pct": -3.1, "conversions_delta_pct": 8.0,
                 "efficiency_delta_pct": 5.0, "goal_miss": 0},
          "14": {"cost_delta_pct": -2.4, "conversions_delta_pct": 9.5,
                 "efficiency_delta_pct": 6.2, "goal_miss": 0}
        }
      }
    ]
  }

Usage:
  # dry-run: build + verify + print the sha256, NO chain write, NO wallet needed
  python scripts/reveal_outcomes.py --measurement w21_measurement.json \\
      --signer-ed25519-key-file ~/.sn21/keys/outcome-signer-ed25519.pem \\
      --out /tmp/reveal_WR-2026-W21-PUB-E1.json --dry-run

  # live: commit 9.A.2 on chain
  python scripts/reveal_outcomes.py --measurement w21_measurement.json \\
      --signer-ed25519-key-file ~/.sn21/keys/outcome-signer-ed25519.pem \\
      --signer-wallet outcome-signer --signer-hotkey default \\
      --network finney --netuid 21 --out /tmp/reveal_WR-2026-W21-PUB-E1.json
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Optional

logger = logging.getLogger("reveal_outcomes")

EXIT_OK = 0
EXIT_MISCONFIG = 1
EXIT_COMMIT_FAILED = 2


def _load_measurement(path: Path):
    """Parse the measurement JSON into (meta, episodes[EpisodeOutcome])."""
    from hope.hope_outcomes.reveal_blob import EpisodeOutcome, HorizonOutcomeMeasured

    data = json.loads(path.read_text())
    for k in ("epoch_id", "epoch_idx", "release_commit_sha256_hex",
              "deadline_round", "measured_at_round", "horizons", "episodes"):
        if k not in data:
            raise ValueError(f"measurement JSON missing required key: {k!r}")

    horizons = [str(h) for h in data["horizons"]]
    episodes = []
    for ep in data["episodes"]:
        outcomes = []
        for h, o in ep["outcomes"].items():
            outcomes.append(HorizonOutcomeMeasured(
                horizon=str(h),
                cost_delta_pct=float(o["cost_delta_pct"]),
                conversions_delta_pct=float(o["conversions_delta_pct"]),
                efficiency_delta_pct=float(o["efficiency_delta_pct"]),
                goal_miss=int(o["goal_miss"]),
            ))
        episodes.append(EpisodeOutcome(
            episode_id=str(ep["episode_id"]),
            salt=bytes.fromhex(ep["salt_hex"]),
            outcomes=outcomes,
        ))
    meta = {
        "epoch_id": str(data["epoch_id"]),
        "epoch_idx": int(data["epoch_idx"]),
        "release_commit_plaintext_sha256": bytes.fromhex(data["release_commit_sha256_hex"]),
        "deadline_round": int(data["deadline_round"]),
        "measured_at_round": int(data["measured_at_round"]),
        "horizons": horizons,
    }
    return meta, episodes


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--measurement", required=True, type=Path,
                   help="Path to the epoch measurement JSON (schema in the header).")
    p.add_argument("--signer-ed25519-key-file", required=True, type=Path,
                   help="Outcome-signer ed25519 PEM (inner-signs the reveal blob).")
    p.add_argument("--signer-wallet", default=None,
                   help="bittensor wallet name for the outcome-signer (required unless --dry-run).")
    p.add_argument("--signer-hotkey", default="default")
    p.add_argument("--network", default="finney")
    p.add_argument("--netuid", type=int, default=21)
    p.add_argument("--out", type=Path, default=None,
                   help="Write the reveal blob here (to serve AFTER the commit finalizes).")
    p.add_argument("--dry-run", action="store_true",
                   help="Build + verify + print the sha256; do NOT commit (no wallet needed).")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    from cryptography.hazmat.primitives import serialization
    from hope.hope_outcomes.reveal_blob import (
        build_reveal_blob, verify_reveal_blob, compute_reveal_blob_sha256,
    )

    # --- load measurement ---
    try:
        meta, episodes = _load_measurement(args.measurement)
    except (OSError, ValueError, KeyError, json.JSONDecodeError) as e:
        logger.error("cannot load measurement: %s", e)
        return EXIT_MISCONFIG
    if not episodes:
        logger.error("measurement has no episodes")
        return EXIT_MISCONFIG

    # --- load the outcome-signer ed25519 key ---
    try:
        signing_key = serialization.load_pem_private_key(
            args.signer_ed25519_key_file.read_bytes(), password=None)
        signer_pub = signing_key.public_key().public_bytes_raw()
    except Exception as e:
        logger.error("cannot load ed25519 signer key: %s", e)
        return EXIT_MISCONFIG

    # --- build + self-verify the blob ---
    blob = build_reveal_blob(
        outcome_signer_hotkey=signer_pub,
        outcome_signer_signing_key=signing_key,
        epoch_id=meta["epoch_id"],
        epoch_idx=meta["epoch_idx"],
        release_commit_plaintext_sha256=meta["release_commit_plaintext_sha256"],
        deadline_round=meta["deadline_round"],
        measured_at_round=meta["measured_at_round"],
        horizons=meta["horizons"],
        episodes=episodes,
    )
    if not verify_reveal_blob(blob):
        logger.error("built blob failed self-verification (inner_sig) — aborting")
        return EXIT_MISCONFIG
    sha = compute_reveal_blob_sha256(blob)
    logger.info("reveal blob built: epoch=%s episodes=%d bytes=%d sha256=%s",
                meta["epoch_id"], len(episodes), len(blob), sha.hex())

    if args.out:
        args.out.write_bytes(blob)
        logger.info("wrote reveal blob to %s (serve at /reveal/%s AFTER the commit finalizes)",
                    args.out, meta["epoch_id"])

    if args.dry_run:
        logger.info("--dry-run: NOT committing 9.A.2. Chain commit value would be sha256 above.")
        return EXIT_OK

    if not args.signer_wallet:
        logger.error("--signer-wallet is required for a live commit (or use --dry-run)")
        return EXIT_MISCONFIG

    # --- commit 9.A.2 on chain ---
    import bittensor as bt
    from hope.commitment.on_chain import submit_outcome_reveal_hash_layer_9a2
    subtensor = bt.Subtensor(network=args.network)
    wallet = bt.wallet(name=args.signer_wallet, hotkey=args.signer_hotkey)
    result = submit_outcome_reveal_hash_layer_9a2(
        subtensor=subtensor,
        hope_outcome_signer_wallet=wallet,
        netuid=args.netuid,
        reveal_blob_sha256=sha,
    )
    if not result.success:
        logger.error("9.A.2 commit FAILED: %s", result.message)
        return EXIT_COMMIT_FAILED
    logger.info("9.A.2 committed: epoch=%s block=%s", meta["epoch_id"], result.block_number)
    print(f"9.A.2 reveal committed for {meta['epoch_id']} at block {result.block_number}")
    print(f"NOW serve {args.out or '<the blob>'} at /reveal/{meta['epoch_id']} "
          f"(commit-then-serve gate satisfied).")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
