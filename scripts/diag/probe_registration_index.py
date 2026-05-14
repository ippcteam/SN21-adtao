#!/usr/bin/env python3
"""Live-chain probe for RegistrationIndex against testnet 466.

Reads chain events from a configurable block range, runs the full
RegistrationIndex scan, and prints which (hotkey → ed25519_pk) bindings
were recovered. Intended as a one-shot diagnostic to confirm that miners
who ran `sn21_keys.py register` historically can still be picked up by
the validator after their CommitmentOf slot was overwritten by later
TLE bundle commits.

Usage:
    python scripts/diag/probe_registration_index.py \
        --network test --netuid 466 \
        --start-block 7100000 --end-block 7120000

Set SN21_SUBTENSOR_URL to an archive-node RPC (e.g.
wss://test-archive.chain.opentensor.ai) when scanning older block ranges,
since the default testnet node prunes history beyond ~256 blocks.
"""

from __future__ import annotations

import argparse
import os
import sys

import bittensor as bt

from hope.validator.registration_index import (
    RegistrationIndex,
    RegistrationRole,
)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--network", default="test")
    p.add_argument("--netuid", type=int, default=466)
    p.add_argument("--start-block", type=int, required=True,
                   help="Block number to start the scan at (inclusive).")
    p.add_argument("--end-block", type=int, default=None,
                   help="Block number to end the scan at (inclusive). "
                        "Default: chain head.")
    p.add_argument("--filter-hotkey", action="append", default=[],
                   help="If provided one or more times, only print bindings for "
                        "these SS58 hotkeys. Other discovered bindings still "
                        "count toward the summary but aren't dumped.")
    p.add_argument("--role", choices=["miner", "validator", "outcome_signer"],
                   default="miner")
    p.add_argument("--output-json", type=str, default=None,
                   help="If provided, write the full indexed result as a JSON "
                        "file in the schema expected by `hope-validator "
                        "--reg-index-prebuilt`. Useful for one-shot backfill: "
                        "run this against an archive RPC, then ship the JSON "
                        "to the cron container so it doesn't re-scan history "
                        "on every run.")
    args = p.parse_args()

    url = os.environ.get("SN21_SUBTENSOR_URL")
    if url:
        print(f"[probe] using SN21_SUBTENSOR_URL={url}", flush=True)
        sub = bt.Subtensor(network=url)
    else:
        sub = bt.Subtensor(network=args.network)
    head = sub.get_current_block()
    end = args.end_block if args.end_block is not None else head
    print(f"[probe] current head={head}", flush=True)
    print(f"[probe] scanning blocks [{args.start_block}, {end}] "
          f"(span={end - args.start_block + 1} blocks)", flush=True)

    role_map = {
        "miner": RegistrationRole.MINER,
        "validator": RegistrationRole.VALIDATOR,
        "outcome_signer": RegistrationRole.OUTCOME_SIGNER,
    }
    index = RegistrationIndex(sub, args.netuid, expected_role=role_map[args.role])
    found = index.scan_range(args.start_block, end)
    stats = index.stats

    print()
    print(f"[probe] scan complete: indexed={index.size} newly_found={found}")
    print(f"[probe] stats: blocks_scanned={stats['blocks_scanned']} "
          f"events_seen={stats['events_seen']} "
          f"candidates={stats['candidates_found']} "
          f"verified={stats['verified']}")

    if args.output_json:
        import json as _json
        with open(args.output_json, "w") as f:
            _json.dump(index.to_json(), f, indent=2, sort_keys=True)
        print(f"[probe] wrote {index.size} entries to {args.output_json}")

    if not index.size:
        print("[probe] no valid registrations recovered in this range.")
        return 1

    print()
    print(f"[probe] all valid {args.role.upper()} registrations:")
    filter_set = set(args.filter_hotkey) if args.filter_hotkey else None
    for entry in sorted(index.entries(), key=lambda e: e.block_number):
        if filter_set is not None and entry.hotkey_ss58 not in filter_set:
            continue
        print(
            f"  block={entry.block_number} "
            f"hotkey={entry.hotkey_ss58} "
            f"ed25519_pk={entry.ed25519_pk.hex()}"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
