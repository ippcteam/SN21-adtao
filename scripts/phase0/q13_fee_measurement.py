"""Phase 0 — Q13: empirically measure TAO fee for set_commitment extrinsic.

Submits a small `set_commitment` to Bittensor TESTNET, observes the actual fee
charged, and prints the measurement. This empirically resolves Q13 (per-commit
deposit / fee in TAO) from the architecture doc §10.

Run:
    python scripts/phase0/q13_fee_measurement.py \\
        --wallet-name test-validator \\
        --wallet-hotkey default \\
        --netuid 21

Prerequisites:
- A registered hotkey on testnet netuid 21 (or whichever netuid you specify)
- Hotkey funded with at least 0.01 TAO

Output: prints fee in µTAO + JSON written to scripts/phase0/results/q13_fee.json.

WARNING: This submits a REAL extrinsic on TESTNET. Do not run against mainnet.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

try:
    import bittensor as bt
except ImportError:
    print("ERROR: pip install 'bittensor>=8.0.0'")
    sys.exit(1)


def measure_fee(
    wallet_name: str,
    wallet_hotkey: str,
    netuid: int = 21,
    network: str = "test",
) -> dict:
    """Submit a tiny set_commitment and measure the fee."""
    print(f"Connecting to Bittensor {network}...")
    wallet = bt.Wallet(name=wallet_name, hotkey=wallet_hotkey)
    subtensor = bt.Subtensor(network=network)

    # Check balance first
    balance_before = subtensor.get_balance(wallet.coldkeypub.ss58_address)
    print(f"  Coldkey balance before: {balance_before}")

    # Build a minimal payload: a single Sha256 field (32 bytes)
    test_hash = b"\xab" * 32
    data = {
        "fields": [
            {"Sha256": list(test_hash)},
        ]
    }

    print(f"  Submitting set_commitment(netuid={netuid})...")
    # The exact API name depends on SDK version; try set_commitment then commit
    submit_method = getattr(subtensor, "set_commitment", None) or getattr(subtensor, "commit", None)
    if submit_method is None:
        raise RuntimeError("subtensor SDK exposes neither set_commitment nor commit")

    result = submit_method(wallet=wallet, netuid=netuid, data=data)
    print(f"  Result: {result}")

    balance_after = subtensor.get_balance(wallet.coldkeypub.ss58_address)
    print(f"  Coldkey balance after:  {balance_after}")

    fee_tao = balance_before.tao - balance_after.tao
    fee_micro = int(fee_tao * 1_000_000)

    return {
        "netuid": netuid,
        "network": network,
        "balance_before_tao": float(balance_before.tao),
        "balance_after_tao": float(balance_after.tao),
        "fee_tao": float(fee_tao),
        "fee_micro_tao": fee_micro,
        "extrinsic_result": str(result),
        "test_hash_committed": test_hash.hex(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Measure set_commitment fee on testnet")
    parser.add_argument("--wallet-name", required=True)
    parser.add_argument("--wallet-hotkey", default="default")
    parser.add_argument("--netuid", type=int, default=21)
    parser.add_argument(
        "--network",
        default="test",
        choices=["test", "finney", "local"],
        help="ALWAYS use 'test' for this diagnostic (defaults to test)",
    )
    args = parser.parse_args()

    if args.network != "test":
        print("ERROR: This script must run on testnet only. Pass --network test.")
        return 1

    result = measure_fee(args.wallet_name, args.wallet_hotkey, args.netuid, args.network)

    # Write to disk
    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "q13_fee.json"
    out_path.write_text(json.dumps(result, indent=2))

    print(f"\n{'=' * 50}")
    print(f"Q13 RESULT: set_commitment fee = {result['fee_micro_tao']} µTAO")
    print(f"  ≈ ${result['fee_tao'] * 3:.6f} at $3/TAO")
    print(f"  Result written to {out_path}")
    print(f"\nProjected annual cost at 256 miners × 52 epochs × 1 commit each:")
    annual = result["fee_micro_tao"] * 256 * 52
    print(f"  {annual / 1_000_000:.4f} TAO ≈ ${annual / 1_000_000 * 3:.2f}/year")

    return 0


if __name__ == "__main__":
    sys.exit(main())
