#!/usr/bin/env python3
"""Operator tool (P4b): commit an epoch's canonical outcome digest on chain.

After the submission deadline, fetch the (signed) outcome package for an epoch,
compute its canonical digest, and publish

    b"sn21-outcomes-v1:" + sha256_digest + epoch_id

to the Commitments pallet under the outcome-signer wallet. Validators then verify
the outcomes they fetch against this immutable, one-set-for-all anchor (see
`scripts/verify_outcomes.py` / `hope.commitment.outcome_commitment`).

Run AFTER outcomes are revealed for the epoch. Requires HOPE_API_KEY/HOPE_API_URL
and the outcome-signer wallet.

Usage:
    python scripts/commit_outcomes.py --release WR-2026-W22-PUB-E1 \\
        --network finney --netuid 21 \\
        --wallet-name <signer-wallet> --wallet-hotkey <signer-hotkey>
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

logger = logging.getLogger("commit_outcomes")


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--release", required=True, help="epoch_id / release_key")
    p.add_argument("--network", default="finney")
    p.add_argument("--netuid", type=int, default=21)
    p.add_argument("--wallet-name", required=True)
    p.add_argument("--wallet-hotkey", default="default")
    p.add_argument("--dry-run", action="store_true",
                   help="compute + print the digest/payload but do NOT commit")
    args = p.parse_args(argv)

    from hope.validator.data_client import HopeDataClient
    from hope.commitment.outcome_commitment import outcomes_digest, build_outcome_commitment

    client = HopeDataClient()
    package = asyncio.run(client.fetch_package(args.release, include_outcomes=True))
    digest = outcomes_digest(args.release, package)
    payload = build_outcome_commitment(args.release, digest)
    n_out = sum(1 for ep in package.get("episodes", [])
                if ep.get("validator_only_outcomes") is not None)
    print(f"[commit-outcomes] epoch={args.release} outcomes={n_out} "
          f"digest={digest.hex()} payload_len={len(payload)}", flush=True)

    if args.dry_run:
        print("[commit-outcomes] --dry-run: not committing", flush=True)
        return 0

    import bittensor as bt
    from hope.validator._log import configure_logging
    from hope.validator._subtensor import make_subtensor
    from bittensor.core.extrinsics.serving import publish_metadata_extrinsic

    subtensor = make_subtensor(args.network)
    configure_logging(logger, "INFO")
    wallet = bt.Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)
    resp = publish_metadata_extrinsic(
        subtensor=subtensor,
        wallet=wallet,
        netuid=args.netuid,
        data_type=f"Raw{len(payload)}",
        data=payload,
        wait_for_inclusion=True,
        wait_for_finalization=True,
        raise_error=False,
    )
    ok = bool(resp[0]) if isinstance(resp, tuple) else bool(resp)
    logger.info("outcome commitment for %s committed=%s", args.release, ok)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
