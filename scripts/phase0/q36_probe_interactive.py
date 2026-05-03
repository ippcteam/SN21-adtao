"""Q36 interactive testnet probe — multi-field commit feasibility.

Run: python scripts/phase0/q36_probe_interactive.py

What it does:

  1. Submits a single multi-field commit (Sha256 + TimelockEncrypted + Raw{N})
     using `submit_layer_9b_multi_field`. This packs all three Layer 9.B
     fields into ONE extrinsic.
  2. Waits up to N drand rounds past the K reveal_round.
  3. Reads back via `get_revealed_commitment_by_hotkey` and `get_commitment`.
     Verifies:
       (a) the extrinsic was accepted in one inclusion;
       (b) the chain auto-decrypted the K side;
       (c) BOTH the Sha256 commit and the Raw URL commit are still readable.

Expected outcomes (we don't know yet — that's what the probe answers):

  - PASS: Phase B/C can switch to the single-extrinsic path, saving 2 of 3
    extrinsic fees per epoch + tighter reveal window.
  - FAIL (auto-decrypt skips multi-variant info): keep the 3-extrinsic path.
    Document the chain limitation and propose a runtime patch upstream.
  - FAIL (extrinsic rejected): the chain doesn't accept multi-variant
    fields; keep the 3-extrinsic path and lock in the architecture.

Safety: testnet only; submits ONE extrinsic + a few read calls; auto-yes
flag mirrors the Q11 probe. The TimelockEncrypted reveal_round is set to
~5 minutes ahead by default so we don't hold up the operator for long.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional


@dataclass
class ProbeResult:
    extrinsic_accepted: bool
    extrinsic_block: Optional[int]
    extrinsic_hash: Optional[str]
    reveal_round: Optional[int]
    waited_seconds: int
    revealed_k_present: bool
    revealed_k_hex: Optional[str]
    sha_commit_readable: bool
    sha_commit_hex: Optional[str]
    raw_url_readable: bool
    raw_url_value: Optional[str]
    notes: str = ""


def _confirm(prompt: str, auto_yes: bool) -> bool:
    if auto_yes:
        return True
    sys.stderr.write(f"{prompt} [y/N] ")
    sys.stderr.flush()
    return sys.stdin.readline().strip().lower() == "y"


def main() -> int:
    parser = argparse.ArgumentParser(description="Q36 multi-field commit probe (testnet)")
    parser.add_argument("--wallet-name", default="sn21-testnet-1")
    parser.add_argument("--wallet-hotkey", default="validator")
    parser.add_argument("--netuid", type=int, default=466)
    parser.add_argument("--blocks-until-reveal", type=int, default=25,
                        help="Subtensor blocks until K auto-decrypts (default 25 ≈ 5 min)")
    parser.add_argument("--probe-url", default="https://probe.sn21.example/q36",
                        help="Raw{N} URL to publish in the multi-field commit")
    parser.add_argument("--reveal-poll-interval-secs", type=int, default=20)
    parser.add_argument("--reveal-poll-max-secs", type=int, default=600)
    parser.add_argument("--yes-i-have-testnet-tao", action="store_true")
    parser.add_argument("--output", default="scripts/phase0/results/q36_multi_field.json")
    args = parser.parse_args()

    print("Q36 probe — TESTNET ONLY")
    print(f"  wallet: {args.wallet_name}/{args.wallet_hotkey}")
    print(f"  netuid: {args.netuid}")
    print(f"  reveal in: ~{args.blocks_until_reveal} blocks (~{args.blocks_until_reveal * 12 / 60:.1f} min)")
    print()
    if not _confirm(
        "Submit a multi-field commit extrinsic now?",
        args.yes_i_have_testnet_tao,
    ):
        print("aborted.")
        return 1

    try:
        import bittensor as bt
    except ImportError:
        print("ERROR: bittensor not installed.", file=sys.stderr)
        return 2

    from hope.commitment.on_chain import submit_layer_9b_multi_field

    print("connecting to testnet...")
    wallet = bt.Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)
    st = bt.Subtensor(network="test")

    aes_key = os.urandom(32)
    sha_ct = b"\xab" * 32
    print()
    print("submitting multi-field commit...")
    try:
        commit = submit_layer_9b_multi_field(
            subtensor=st, miner_wallet=wallet, netuid=args.netuid,
            aes_key=aes_key,
            sha256_ct=sha_ct,
            self_archive_url=args.probe_url,
            blocks_until_reveal=args.blocks_until_reveal,
            wait_for_finalization=True,
            raise_error=False,
        )
    except Exception as e:
        print(f"  EXCEPTION: {type(e).__name__}: {e}")
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        Path(args.output).write_text(json.dumps({
            "extrinsic_accepted": False,
            "exception": f"{type(e).__name__}: {e}",
        }, indent=2))
        return 3

    print(f"  success={commit.success} block={commit.block_number} reveal_round={commit.reveal_round}")

    # Wait for the reveal window.
    print()
    print("waiting for K auto-decrypt...")
    waited = 0
    revealed_k: Optional[bytes] = None
    while waited < args.reveal_poll_max_secs:
        time.sleep(args.reveal_poll_interval_secs)
        waited += args.reveal_poll_interval_secs
        revealed = st.get_revealed_commitment_by_hotkey(
            netuid=args.netuid,
            hotkey_ss58=wallet.hotkey.ss58_address,
        ) or ()
        for entry in revealed:
            if len(entry) != 2:
                continue
            _block, payload = entry
            if isinstance(payload, str):
                pb = bytes.fromhex(payload[2:] if payload.startswith("0x") else payload)
            elif isinstance(payload, (bytes, bytearray)):
                pb = bytes(payload)
            else:
                continue
            if len(pb) == 32 and pb == aes_key:
                revealed_k = pb
                break
        if revealed_k is not None:
            print(f"  K revealed after {waited}s")
            break
        else:
            print(f"  ... still waiting (waited={waited}s)")

    # Read sha256 + url commits (whichever is the latest non-timelock entry).
    sha_readable = False
    sha_hex: Optional[str] = None
    url_readable = False
    url_value: Optional[str] = None
    try:
        # Walk all UIDs and find ours, then read latest commitment.
        metagraph = st.metagraph(netuid=args.netuid)
        my_ss58 = wallet.hotkey.ss58_address
        if my_ss58 in metagraph.hotkeys:
            uid = metagraph.hotkeys.index(my_ss58)
            latest = st.get_commitment(netuid=args.netuid, uid=uid)
            if latest:
                if isinstance(latest, str):
                    raw = bytes.fromhex(latest[2:] if latest.startswith("0x") else latest)
                else:
                    raw = bytes(latest)
                if len(raw) == 32:
                    sha_readable = True
                    sha_hex = raw.hex()
                else:
                    url_readable = True
                    url_value = raw.decode("utf-8", errors="replace")
    except Exception as e:
        print(f"  read error: {type(e).__name__}: {e}")

    result = ProbeResult(
        extrinsic_accepted=commit.success,
        extrinsic_block=commit.block_number,
        extrinsic_hash=commit.extrinsic_hash,
        reveal_round=commit.reveal_round,
        waited_seconds=waited,
        revealed_k_present=revealed_k is not None,
        revealed_k_hex=revealed_k.hex() if revealed_k else None,
        sha_commit_readable=sha_readable,
        sha_commit_hex=sha_hex,
        raw_url_readable=url_readable,
        raw_url_value=url_value,
        notes=(
            "PASS: multi-field commit + auto-decrypt + read all 3 fields."
            if commit.success and revealed_k is not None and (sha_readable or url_readable)
            else "FAIL: see fields. The 3-extrinsic fallback remains the safe path."
        ),
    )
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(asdict(result), indent=2))
    print()
    print(f"wrote: {args.output}")
    print(f"VERDICT: {result.notes}")
    return 0 if "PASS" in result.notes else 1


if __name__ == "__main__":
    sys.exit(main())
