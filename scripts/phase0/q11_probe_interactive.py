"""Q11 interactive testnet probe — RateLimit window measurement.

Run: python scripts/phase0/q11_probe_interactive.py

What it does (interactively, with confirmation before each extrinsic):

  1. Connects to Bittensor testnet, loads wallet sn21-testnet-1/validator.
  2. Submits small Sha256 commits in a tight loop until SpaceLimitExceeded.
     Records the block where rate-limit hits.
  3. Polls every 30 seconds, retrying the SAME commit; records the block
     where it succeeds. The window = success_block - hit_block.
  4. Writes findings to scripts/phase0/results/q11_window.json.

Safety: every extrinsic is preceded by a y/N prompt by default. Pass
--yes-i-have-testnet-tao to skip prompts (still testnet-only — there is no
mainnet path in this script). The probe consumes ~10-20 µTAO of fees,
which is free on testnet faucets.

The result is one number: how many blocks (and seconds) before MaxSpace
clears for that hotkey. We use this to size the validator's TLE
blocks_until_reveal so commits land within the same window.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass
class ProbeAttempt:
    attempt: int
    block: Optional[int]
    success: bool
    message: str
    elapsed_secs_since_start: int


def _confirm(prompt: str, auto_yes: bool) -> bool:
    if auto_yes:
        return True
    sys.stderr.write(f"{prompt} [y/N] ")
    sys.stderr.flush()
    return sys.stdin.readline().strip().lower() == "y"


def _build_payload(suffix: str, target_bytes: int) -> str:
    """Build a payload of exactly `target_bytes` UTF-8 length (≤ 128).

    Pads with deterministic ASCII so the same suffix always produces the
    same bytes — useful for retry idempotency checks on chain.
    """
    prefix = f"q11-probe-{suffix}-"
    pad_len = max(0, target_bytes - len(prefix))
    return prefix + ("X" * pad_len)


def _try_one_commit(st, wallet, netuid: int, suffix: str, payload_bytes: int = 128) -> tuple[bool, str, Optional[int]]:
    """Submit a single commit of `payload_bytes` size; return (success, msg, block)."""
    block = None
    try:
        block = st.get_current_block()
    except Exception:
        pass
    payload = _build_payload(suffix, payload_bytes)
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
        msg = str(getattr(result, "message", ""))[:200]
        return ok, msg, block
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:200], block


def main() -> int:
    parser = argparse.ArgumentParser(description="Q11 RateLimit window probe (testnet)")
    parser.add_argument("--wallet-name", default="sn21-testnet-1")
    parser.add_argument("--wallet-hotkey", default="validator")
    parser.add_argument("--netuid", type=int, default=466)
    parser.add_argument("--retry-interval-secs", type=int, default=30)
    parser.add_argument("--max-tries", type=int, default=120,
                        help="Cap the recovery polling loop (default 120 ≈ 1h at 30s)")
    parser.add_argument("--max-burst", type=int, default=40,
                        help="Cap the initial burst probes")
    parser.add_argument("--payload-bytes", type=int, default=128,
                        help="Bytes per burst commit (max 128 for Raw{N}); larger trips MaxSpace faster")
    parser.add_argument("--yes-i-have-testnet-tao", action="store_true",
                        help="Skip per-extrinsic confirmation (still testnet only)")
    parser.add_argument("--output", default="scripts/phase0/results/q11_window.json")
    args = parser.parse_args()

    print("Q11 probe — TESTNET ONLY")
    print(f"  wallet: {args.wallet_name}/{args.wallet_hotkey}")
    print(f"  netuid: {args.netuid}")
    print()
    if not _confirm(
        "Submit testnet extrinsics? You'll need a small testnet TAO balance.",
        args.yes_i_have_testnet_tao,
    ):
        print("aborted.")
        return 1

    try:
        import bittensor as bt
    except ImportError:
        print("ERROR: bittensor not installed.", file=sys.stderr)
        return 2

    print("connecting to testnet...")
    wallet = bt.Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)
    st = bt.Subtensor(network="test")
    hyperparams = st.get_subnet_hyperparameters(args.netuid)
    tempo = hyperparams.tempo
    print(f"  subnet tempo = {tempo} blocks (~{tempo * 12 / 60:.1f} min)")

    log: list[ProbeAttempt] = []
    start = time.time()

    # Phase 1: burst until SpaceLimitExceeded.
    hit_block: Optional[int] = None
    print()
    print(f"phase 1: burst probes ({args.payload_bytes}B each) until SpaceLimitExceeded...")
    for attempt in range(args.max_burst):
        ok, msg, block = _try_one_commit(
            st, wallet, args.netuid, suffix=f"burst-{attempt}",
            payload_bytes=args.payload_bytes,
        )
        elapsed = int(time.time() - start)
        log.append(ProbeAttempt(attempt=attempt, block=block, success=ok,
                                message=msg, elapsed_secs_since_start=elapsed))
        short = "rate-limit" if "SpaceLimit" in msg else ("ok" if ok else "other")
        print(f"  burst[{attempt}] block={block} {short}: {msg[:60]}")
        if "SpaceLimit" in msg:
            hit_block = block
            break

    if hit_block is None:
        print("never hit SpaceLimitExceeded; probe inconclusive (stop manually if needed).")

    # Phase 2: poll until success.
    print()
    print("phase 2: polling until next successful commit...")
    success_block: Optional[int] = None
    for attempt in range(args.max_tries):
        time.sleep(args.retry_interval_secs)
        # Recovery phase uses a TINY payload (≤32 bytes) — we want to detect
        # window expiry, not consume more MaxSpace.
        ok, msg, block = _try_one_commit(
            st, wallet, args.netuid, suffix=f"recover-{attempt}",
            payload_bytes=32,
        )
        elapsed = int(time.time() - start)
        log.append(ProbeAttempt(attempt=args.max_burst + attempt, block=block,
                                success=ok, message=msg,
                                elapsed_secs_since_start=elapsed))
        short = "ok" if ok else ("rate-limit" if "SpaceLimit" in msg else "other")
        print(f"  recover[{attempt}] block={block} {short}: {msg[:60]}")
        if ok:
            success_block = block
            break

    out_dir = Path(args.output).parent
    out_dir.mkdir(parents=True, exist_ok=True)

    result: dict = {
        "wallet": f"{args.wallet_name}/{args.wallet_hotkey}",
        "netuid": args.netuid,
        "subnet_tempo_blocks": tempo,
        "hit_block": hit_block,
        "success_block": success_block,
        "log": [asdict(a) for a in log],
    }
    if hit_block is not None and success_block is not None:
        window = success_block - hit_block
        result["window_blocks"] = window
        result["window_secs_estimated"] = window * 12
        result["window_minutes_estimated"] = round(window * 12 / 60.0, 2)
        result["tempo_ratio"] = round(window / tempo, 3)

    Path(args.output).write_text(json.dumps(result, indent=2))
    print()
    print(f"wrote: {args.output}")
    if "window_blocks" in result:
        print(f"WINDOW: {result['window_blocks']} blocks ≈ {result['window_minutes_estimated']} min ({result['tempo_ratio']}× tempo)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
