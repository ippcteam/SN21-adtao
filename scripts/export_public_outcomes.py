#!/usr/bin/env python3
"""Export an epoch's revealed outcomes as a public, verifiable JSON.

Fetches the (keyed) operator package, extracts ONLY the public, digest-relevant
fields — `episode_id` + `validator_only_outcomes` — and writes a clean JSON that
is safe to publish (no `customer_id_hash`, `account_context`, `payload`, or
`system_estimate`; none of those are in the on-chain digest and some may be
identifying).

The on-chain `sn21-outcomes-v1` anchor's digest is computed over exactly these
two fields (see `hope.commitment.outcome_commitment.outcomes_digest`), so the
published subset reproduces the same digest as the full package — i.e. the file
is independently verifiable against chain with NO HOPE_API_KEY
(`scripts/verify_public_outcomes.py`).

Run AFTER the epoch's outcomes are revealed + anchored. Requires
HOPE_API_KEY / HOPE_API_URL.

Usage:
    python scripts/export_public_outcomes.py --release WR-2026-W21-PUB-E1 \\
        --out data/outcomes/WR-2026-W21-PUB-E1.json \\
        --verify-onchain --outcome-signer 5CDWCc1MRmaD2XxnvGNvEamwNVWuLFDmbPoPYyyLZ43UeiJQ
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--release", required=True, help="epoch_id / release_key")
    p.add_argument("--out", required=True, help="output JSON path")
    p.add_argument("--network", default="finney")
    p.add_argument("--netuid", type=int, default=21)
    p.add_argument("--verify-onchain", action="store_true",
                   help="confirm the export's digest matches the on-chain anchor")
    p.add_argument("--outcome-signer", default="",
                   help="SS58 of the outcome-signer (required with --verify-onchain)")
    args = p.parse_args(argv)

    from hope.validator.data_client import HopeDataClient
    from hope.commitment.outcome_commitment import outcomes_digest

    client = HopeDataClient()
    pkg = asyncio.run(client.fetch_package(args.release, include_outcomes=True))

    # SAFE public subset: ONLY episode_id + validator_only_outcomes. Everything
    # else in the operator package (customer_id_hash, account_context, payload,
    # system_estimate, transition_key) is deliberately dropped.
    public_eps = []
    for ep in pkg.get("episodes", []):
        voo = ep.get("validator_only_outcomes")
        if voo is None:
            continue
        public_eps.append({
            "episode_id": str(ep.get("episode_id", "")),
            "validator_only_outcomes": voo,
        })
    public_eps.sort(key=lambda e: e["episode_id"])
    public = {"epoch_id": args.release, "episodes": public_eps}

    digest = outcomes_digest(args.release, public)
    full_digest = outcomes_digest(args.release, pkg)
    print(f"[export] {args.release}: {len(public_eps)} episodes with outcomes | "
          f"digest={digest.hex()}", flush=True)

    # The public subset MUST reproduce the full package's digest, else we'd be
    # publishing something that doesn't match what's anchored.
    if digest != full_digest:
        print(f"ERROR: public-subset digest {digest.hex()} != full-package digest "
              f"{full_digest.hex()}; refusing to write.", file=sys.stderr)
        return 1

    if args.verify_onchain:
        if not args.outcome_signer:
            print("ERROR: --verify-onchain requires --outcome-signer", file=sys.stderr)
            return 1
        from hope.commitment.outcome_commitment import (
            parse_outcome_commitment, read_outcome_commitment,
        )
        from hope.validator._subtensor import make_subtensor
        st = make_subtensor(args.network)
        raw = read_outcome_commitment(st, args.netuid, args.outcome_signer)
        parsed = parse_outcome_commitment(raw) if raw else None
        if parsed is None:
            print("ERROR: no sn21-outcomes-v1 anchor on chain for that signer",
                  file=sys.stderr)
            return 1
        if parsed[0] != args.release or parsed[1] != digest:
            print(f"ERROR: on-chain anchor (epoch={parsed[0]} digest={parsed[1].hex()}) "
                  f"does not match this export.", file=sys.stderr)
            return 1
        print(f"[export] on-chain anchor MATCHES ✓ "
              f"(signer {args.outcome_signer[:12]}...)", flush=True)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(public, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"[export] wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
