"""Phase 0 — Q11 follow-up: empirically measure RateLimit window duration.

After a previous burst of submissions hit `SpaceLimitExceeded(Module)`, this
script retries every 60s until a small commit succeeds. It records:
- Block at first SpaceLimitExceeded
- Block at first successful retry
- Window in blocks + seconds
- Tempo ratio (window / subnet tempo)

Run in background:
    nohup python scripts/phase0/q11_retry_loop.py --baseline-block 7038735 \\
        > scripts/phase0/results/q11_retry.log 2>&1 &

Output: scripts/phase0/results/q11_window.json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import bittensor as bt


def try_one_commit(st, wallet, netuid: int, suffix: str) -> tuple[bool, str, int]:
    """Submit a tiny `set_commitment` and return (success, error_msg, block_number)."""
    block = st.get_current_block()
    payload = f"q11-retry-probe-{suffix}"  # deterministic, < 32 bytes
    try:
        result = st.set_commitment(
            wallet=wallet,
            netuid=netuid,
            data=payload,
            wait_for_inclusion=True,
            wait_for_finalization=False,
            raise_error=False,
        )
        ok = bool(getattr(result, "success", False))
        msg = str(getattr(result, "message", ""))[:120]
        return (ok, msg, block)
    except Exception as e:
        return (False, str(e)[:120], block)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--baseline-block",
        type=int,
        required=True,
        help="The block at which SpaceLimitExceeded first occurred (from earlier session)",
    )
    parser.add_argument("--wallet-name", default="sn21-testnet-1")
    parser.add_argument("--wallet-hotkey", default="validator")
    parser.add_argument("--netuid", type=int, default=466)
    parser.add_argument("--retry-interval-secs", type=int, default=60)
    parser.add_argument("--max-tries", type=int, default=300)
    args = parser.parse_args()

    print(f"[q11_retry_loop] Connecting to testnet, netuid={args.netuid}")
    wallet = bt.Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)
    st = bt.Subtensor(network="test")

    hyperparams = st.get_subnet_hyperparameters(args.netuid)
    tempo = hyperparams.tempo
    print(f"[q11_retry_loop] Subnet tempo: {tempo} blocks ({tempo * 12 / 60:.1f} min)")
    print(f"[q11_retry_loop] Baseline (rate-limit hit) block: {args.baseline_block}")

    log: list[dict] = []
    success_block: int | None = None
    success_time: float | None = None

    start_time = time.time()
    for attempt in range(args.max_tries):
        ok, msg, block = try_one_commit(st, wallet, args.netuid, suffix=str(attempt))
        elapsed_s = int(time.time() - start_time)
        log.append(
            {
                "attempt": attempt,
                "ok": ok,
                "block": block,
                "msg": msg,
                "elapsed_secs_since_start": elapsed_s,
            }
        )
        if ok:
            success_block = block
            success_time = time.time()
            print(f"[q11_retry_loop] SUCCESS @ block {block} after {elapsed_s}s ({attempt+1} attempts)")
            break
        else:
            short = "rate-limit" if "SpaceLimitExceeded" in msg else "other"
            print(
                f"[q11_retry_loop] attempt {attempt+1}: FAIL @ block {block} "
                f"after {elapsed_s}s ({short}): {msg[:60]}"
            )
            time.sleep(args.retry_interval_secs)

    if success_block is None:
        print(
            f"[q11_retry_loop] No success after {args.max_tries} attempts; "
            f"window may exceed {args.max_tries * args.retry_interval_secs}s"
        )
        return 1

    window_blocks = success_block - args.baseline_block
    window_secs = success_block * 12 - args.baseline_block * 12  # rough; assumes 12s/block

    print()
    print("=" * 50)
    print(f"Q11 RESULT:")
    print(f"  Rate-limit hit at block:  {args.baseline_block}")
    print(f"  Resumed at block:         {success_block}")
    print(f"  Window blocks:            {window_blocks}")
    print(f"  Window secs (~12s/block): {window_secs}")
    print(f"  Window minutes:           {window_secs / 60:.1f}")
    print(f"  Subnet tempo:             {tempo} blocks ({tempo * 12 / 60:.1f} min)")
    print(f"  Tempo ratio:              {window_blocks / tempo:.2f} tempos")
    print()
    if abs(window_blocks - tempo) <= 5:
        print("  → Window appears to be ONE TEMPO")
    elif window_blocks <= tempo / 2:
        print(f"  → Window is shorter than tempo: ~{window_secs / 60:.1f} min")
    else:
        print(f"  → Window is {window_blocks} blocks; investigate manually")

    out_dir = Path(__file__).parent / "results"
    out_dir.mkdir(exist_ok=True)
    out_path = out_dir / "q11_window.json"
    out_path.write_text(
        json.dumps(
            {
                "baseline_block": args.baseline_block,
                "success_block": success_block,
                "window_blocks": window_blocks,
                "window_seconds": window_secs,
                "subnet_tempo_blocks": tempo,
                "tempo_ratio": window_blocks / tempo,
                "log": log,
            },
            indent=2,
        )
    )
    print(f"Result written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
