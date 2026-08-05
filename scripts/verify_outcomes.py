#!/usr/bin/env python3
"""Validator tool (P4b): verify fetched outcomes against the on-chain commitment.

Fetches the (signature-verified) outcome package for an epoch, then reads the
operator's on-chain `sn21-outcomes-v1` commitment (published by the outcome-
signer) and confirms the package's canonical digest matches. A pass means the
outcomes you'd score against are exactly the immutable set the operator anchored
on chain — identical for every validator, unaltered after the fact.

Usage:
    python scripts/verify_outcomes.py --release WR-2026-W22-PUB-E1 \\
        --network finney --netuid 21 \\
        --outcome-signer <SIGNER_SS58>

Requires HOPE_API_KEY / HOPE_API_URL. Read-only — no extrinsics.
"""
from __future__ import annotations

import argparse
import asyncio
import sys


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--release", required=True, help="epoch_id / release_key")
    p.add_argument("--network", default="finney")
    p.add_argument("--netuid", type=int, default=21)
    p.add_argument("--outcome-signer", required=True,
                   help="SS58 of the operator's outcome-signer hotkey")
    args = p.parse_args(argv)

    from hope.commitment.outcome_commitment import verify_outcome_commitment
    from hope.validator._subtensor import make_subtensor
    from hope.validator.data_client import HopeDataClient

    client = HopeDataClient()
    package = asyncio.run(client.fetch_package(args.release, include_outcomes=True))
    subtensor = make_subtensor(args.network)
    ok, reason = verify_outcome_commitment(
        subtensor, args.netuid, args.outcome_signer, args.release, package,
    )
    print(f"[verify-outcomes] epoch={args.release} signer={args.outcome_signer[:12]}... "
          f"-> {'OK' if ok else 'FAIL'}: {reason}", flush=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
