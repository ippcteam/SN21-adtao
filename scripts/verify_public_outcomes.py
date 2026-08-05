#!/usr/bin/env python3
"""Verify a published public-outcomes JSON against the on-chain anchor.

Reads a published outcomes file (e.g. from the public data repo), recomputes its
canonical digest, and checks it against the operator's on-chain `sn21-outcomes-v1`
commitment. A pass means the file you downloaded is byte-for-byte the outcome set
the operator anchored on chain — unaltered and the same for everyone.

NO HOPE_API_KEY needed — this reads only the local file + the chain, so any miner
or third party can run it.

Usage:
    python scripts/verify_public_outcomes.py \\
        --file data/outcomes/WR-2026-W21-PUB-E1.json \\
        --outcome-signer 5CDWCc1MRmaD2XxnvGNvEamwNVWuLFDmbPoPYyyLZ43UeiJQ \\
        --network finney --netuid 21
"""
from __future__ import annotations

import argparse
import json
import sys


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--file", required=True, help="published outcomes JSON")
    p.add_argument("--outcome-signer", required=True,
                   help="SS58 of the operator's outcome-signer hotkey")
    p.add_argument("--network", default="finney")
    p.add_argument("--netuid", type=int, default=21)
    args = p.parse_args(argv)

    with open(args.file) as f:
        pub = json.load(f)
    epoch = pub.get("epoch_id", "")

    from hope.commitment.outcome_commitment import (
        outcomes_digest,
        parse_outcome_commitment,
        read_outcome_commitment,
    )
    from hope.validator._subtensor import make_subtensor

    digest = outcomes_digest(epoch, pub)
    st = make_subtensor(args.network)
    raw = read_outcome_commitment(st, args.netuid, args.outcome_signer)
    parsed = parse_outcome_commitment(raw) if raw else None
    onchain = parsed[1].hex() if parsed else "NONE"
    ok = parsed is not None and parsed[0] == epoch and parsed[1] == digest

    print(f"[verify-public] epoch={epoch} episodes={len(pub.get('episodes', []))}")
    print(f"  file digest    : {digest.hex()}")
    print(f"  on-chain digest: {onchain}")
    print(f"  -> {'OK ✓ outcomes match the on-chain anchor' if ok else 'MISMATCH ✗'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
