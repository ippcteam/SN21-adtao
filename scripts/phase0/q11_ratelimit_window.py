"""Phase 0 — Q11: empirically determine the RateLimit window of pallet_commitments.

Submits set_commitment extrinsics in rapid succession until rate-limited, then
measures how long after the rate-limit until commits are accepted again. This
empirically resolves whether `MaxSpace=3,100 bytes` is per-tempo, per-Bittensor-epoch,
or per fixed-time-interval.

Run:
    python scripts/phase0/q11_ratelimit_window.py \\
        --wallet-name test-validator --netuid 21

Output: scripts/phase0/results/q11_ratelimit.json

Method:
1. Submit small (32-byte Sha256) commits, each costing some space against MaxSpace.
2. Continue until a submission fails with rate-limit error (SpaceLimitExceeded).
3. Note current block + tempo + Bittensor epoch number.
4. Wait + retry every 60s; note when commits are accepted again.
5. Compute: window_blocks = (resume_block - rate_limit_block).
6. Compare to subnet's tempo and epoch boundaries → conclude window unit.

WARNING: This submits MANY real extrinsics on testnet. Use a dedicated wallet.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

try:
    import bittensor as bt
except ImportError:
    print("ERROR: pip install 'bittensor>=8.0.0'")
    sys.exit(1)


def submit_one_commit(
    subtensor, wallet, netuid: int, payload_byte: int
) -> tuple[bool, str, int]:
    """Try to submit one small commit. Returns (success, error_str, block_number)."""
    test_hash = bytes([payload_byte]) * 32
    data = {"fields": [{"Sha256": list(test_hash)}]}
    block_before = subtensor.get_current_block()
    submit_method = getattr(subtensor, "set_commitment", None) or getattr(subtensor, "commit", None)
    try:
        result = submit_method(wallet=wallet, netuid=netuid, data=data)
        return (True, str(result), block_before)
    except Exception as e:
        return (False, str(e), block_before)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wallet-name", required=True)
    parser.add_argument("--wallet-hotkey", default="default")
    parser.add_argument("--netuid", type=int, default=21)
    parser.add_argument("--max-attempts", type=int, default=200)
    parser.add_argument("--retry-interval-secs", type=int, default=60)
    args = parser.parse_args()

    print(f"Phase 0 — Q11 RateLimit window measurement (testnet netuid {args.netuid})")
    wallet = bt.Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)
    subtensor = bt.Subtensor(network="test")

    # Subnet hyperparameters for context
    hyperparams = subtensor.get_subnet_hyperparameters(args.netuid)
    tempo = hyperparams.tempo
    print(f"  Subnet tempo: {tempo} blocks")
    print(f"  Each commit:  32 bytes (Sha256 only)")
    print(f"  MaxSpace:     3,100 bytes (default)")
    print(f"  Expected:     ~96 commits before rate limit (3100/32)")

    log: list[dict] = []
    rate_limit_block: int | None = None
    rate_limit_time: float | None = None

    # Phase 1: submit until rate-limited
    print("\nPhase 1: submitting commits until rate-limited...")
    for i in range(args.max_attempts):
        ok, msg, block = submit_one_commit(subtensor, wallet, args.netuid, payload_byte=i % 256)
        log.append({"attempt": i, "ok": ok, "block": block, "msg": msg[:100], "ts": time.time()})
        if not ok:
            print(f"  Attempt {i + 1}: FAIL @ block {block}")
            print(f"    Error: {msg[:200]}")
            if "SpaceLimitExceeded" in msg or "RateLimit" in msg or "limit" in msg.lower():
                rate_limit_block = block
                rate_limit_time = time.time()
                print(f"    Rate-limit detected at block {block}.")
                break
            else:
                print(f"    Unexpected error; aborting.")
                break
        else:
            if i % 10 == 0:
                print(f"  Attempt {i + 1}: OK @ block {block}")

    if rate_limit_block is None:
        print("\nNo rate-limit hit after max attempts. Increase --max-attempts.")
        return 1

    # Phase 2: wait + retry until commits are accepted again
    print("\nPhase 2: waiting for rate-limit window to expire...")
    resume_block: int | None = None
    while True:
        time.sleep(args.retry_interval_secs)
        ok, msg, block = submit_one_commit(subtensor, wallet, args.netuid, payload_byte=200)
        log.append({"attempt": "retry", "ok": ok, "block": block, "msg": msg[:100], "ts": time.time()})
        if ok:
            resume_block = block
            print(f"  Commits accepted again @ block {block}")
            break
        else:
            print(f"  Still rate-limited @ block {block}")

    # Compute window
    window_blocks = resume_block - rate_limit_block
    window_secs = (rate_limit_time and (time.time() - rate_limit_time)) or 0
    print(f"\n{'=' * 50}")
    print(f"Q11 RESULT:")
    print(f"  Rate-limit hit at block:  {rate_limit_block}")
    print(f"  Resumed at block:         {resume_block}")
    print(f"  Window blocks:            {window_blocks}")
    print(f"  Window seconds (approx):  {int(window_secs)}")
    print(f"  Subnet tempo:             {tempo} blocks")
    print(f"  Tempo ratio:              {window_blocks / tempo:.2f} tempos")
    print()

    if abs(window_blocks - tempo) <= 5:
        print("  → Window appears to be ONE TEMPO (~1 hour at 12s blocks)")
    elif window_blocks >= 7200:  # 1 day worth
        print("  → Window appears to be at least 1 DAY")
    else:
        print(f"  → Window is {window_blocks} blocks; investigate manually")

    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "q11_ratelimit.json"
    out_path.write_text(
        json.dumps(
            {
                "rate_limit_block": rate_limit_block,
                "resume_block": resume_block,
                "window_blocks": window_blocks,
                "window_seconds_approx": int(window_secs),
                "subnet_tempo_blocks": tempo,
                "tempo_ratio": window_blocks / tempo,
                "log": log,
            },
            indent=2,
        )
    )
    print(f"  Written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
