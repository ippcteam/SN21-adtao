"""H-3 — TLE auto-decrypt timing probe.

Goal: distinguish between three hypotheses for why our 9.C.1 / 9.C.2
TLE'd commits did not appear in `Commitments::RevealedCommitments` after
30+ minutes past their `reveal_round`:

  Hypothesis A: chain pulls drand pulses on a slow cron, decrypt lags.
  Hypothesis B: our `bittensor_drand.encrypt(...)` produces ciphertext
                the chain's auto-decrypt path doesn't accept.
  Hypothesis C: `RevealedCommitments` stores ONLY 1 entry per (netuid,
                hotkey) and the older one was never overwritten because
                the chain didn't decrypt the new ones.

Procedure:

  1. Submit a single tiny `TimelockEncrypted(plaintext=<unique 32B>)`
     with `blocks_until_reveal=10` (≈ 2 min).
  2. Tail `RevealedCommitments(netuid, hotkey)` every 15 seconds.
  3. Stop when the unique plaintext appears, OR after 60 minutes (we'd
     definitely expect it within that window if the chain works at all).
  4. Record:
     - Time from submission to reveal_round.
     - Time from reveal_round to RevealedCommitments showing our bytes.
     - The exact byte content stored vs what we submitted.
     - The block at which the reveal landed.

Outcomes:

  - Bytes match within ~5 min after reveal_round → Hypothesis A or C
    confirmed (decrypt works, just lagged or single-slot).
  - Bytes never appear within 60 min → Hypothesis B (TLE format issue);
    revisit `bittensor_drand.encrypt` parameters.
  - Bytes appear but are GARBLED → chain decrypts but our format
    differs from chain expectation. Tighten the spec.

Run:
    python scripts/phase0/h3_tle_decrypt_probe.py --yes-i-have-testnet-tao
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

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@dataclass
class ProbeResult:
    extrinsic_accepted: bool
    extrinsic_hash: Optional[str]
    submission_unix: int
    submission_drand_round: int
    target_reveal_round: int
    waited_seconds: int
    decrypt_observed: bool
    decrypt_round_observed: Optional[int]
    decrypt_block: Optional[int]
    decrypt_payload_hex: Optional[str]
    expected_payload_hex: str
    payload_match: Optional[bool]
    notes: str = ""


def _confirm(prompt: str, auto_yes: bool) -> bool:
    if auto_yes:
        return True
    sys.stderr.write(f"{prompt} [y/N] ")
    sys.stderr.flush()
    return sys.stdin.readline().strip().lower() == "y"


def main() -> int:
    parser = argparse.ArgumentParser(description="H-3 TLE decrypt timing probe")
    parser.add_argument("--wallet-name", default="sn21-testnet-1")
    parser.add_argument("--wallet-hotkey", default="validator")
    parser.add_argument("--netuid", type=int, default=466)
    parser.add_argument("--blocks-until-reveal", type=int, default=10,
                        help="K reveal target ≈ 10 blocks (~2 min)")
    parser.add_argument("--poll-secs", type=int, default=15)
    parser.add_argument("--max-wait-secs", type=int, default=3600,
                        help="Cap total wait time (default 60 min)")
    parser.add_argument("--yes-i-have-testnet-tao", action="store_true")
    parser.add_argument("--output",
                        default="scripts/phase0/results/h3_tle_decrypt.json")
    args = parser.parse_args()

    print("H-3 probe — TESTNET ONLY")
    print(f"  wallet: {args.wallet_name}/{args.wallet_hotkey}")
    print(f"  netuid: {args.netuid}")
    print(f"  reveal target: ~{args.blocks_until_reveal} blocks "
          f"({args.blocks_until_reveal * 12 / 60:.1f} min)")
    if not _confirm("Submit one TLE commit + poll for decrypt?",
                    args.yes_i_have_testnet_tao):
        return 1

    import bittensor as bt
    from hope.commitment.chain_reader import read_revealed_commitments
    from hope.commitment.drand_lib import drand_round_at
    from hope.commitment.on_chain import submit_timelock_commit

    wallet = bt.Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)
    st = bt.Subtensor(network="test")

    # Unique 32-byte plaintext so we can identify our reveal in the storage.
    expected = os.urandom(32)
    print(f"  expected plaintext (hex): {expected.hex()}")

    submission_unix = int(time.time())
    submission_round = drand_round_at(submission_unix)
    print()
    print("submitting TLE commit...")
    res = submit_timelock_commit(
        subtensor=st, wallet=wallet, netuid=args.netuid,
        plaintext=expected,
        blocks_until_reveal=args.blocks_until_reveal,
        wait_for_finalization=True, raise_error=False,
    )
    print(f"  success={res.success} block={res.block_number} "
          f"reveal_round={res.reveal_round}")
    if not res.success:
        Path(args.output).parent.mkdir(parents=True, exist_ok=True)
        result = ProbeResult(
            extrinsic_accepted=False,
            extrinsic_hash=res.extrinsic_hash,
            submission_unix=submission_unix,
            submission_drand_round=submission_round,
            target_reveal_round=res.reveal_round or 0,
            waited_seconds=0,
            decrypt_observed=False,
            decrypt_round_observed=None,
            decrypt_block=None,
            decrypt_payload_hex=None,
            expected_payload_hex=expected.hex(),
            payload_match=None,
            notes=f"extrinsic rejected: {res.message}",
        )
        Path(args.output).write_text(json.dumps(asdict(result), indent=2))
        print(f"\nwrote {args.output}")
        return 2

    target = res.reveal_round or 0

    print()
    print(f"polling RevealedCommitments every {args.poll_secs}s "
          f"until expected payload appears or timeout...")
    waited = 0
    decrypt_observed = False
    decrypt_block: Optional[int] = None
    decrypt_payload_hex: Optional[str] = None
    payload_match: Optional[bool] = None
    decrypt_round_at_observation: Optional[int] = None

    ss58 = wallet.hotkey.ss58_address
    while waited < args.max_wait_secs:
        time.sleep(args.poll_secs)
        waited += args.poll_secs
        try:
            entries = read_revealed_commitments(st, args.netuid, ss58)
        except Exception as e:
            print(f"  poll error: {type(e).__name__}: {e}")
            continue

        # Look for an entry containing our expected 32-byte plaintext.
        for entry in entries:
            payload = entry.payload_bytes
            # The chain may have wrapped the plaintext in a Data variant
            # prefix. Search for the 32-byte expected anywhere in the bytes.
            if expected in payload:
                decrypt_observed = True
                decrypt_block = entry.block_number
                decrypt_payload_hex = payload.hex()
                payload_match = True
                decrypt_round_at_observation = drand_round_at(int(time.time()))
                break
        if decrypt_observed:
            break
        # Also report ANY payload — to surface "garbled" reveals (Hypothesis B).
        if entries:
            latest = entries[-1]
            print(f"  [{waited}s] {len(entries)} entries; latest "
                  f"block={latest.block_number} bytes[:32]={latest.payload_bytes[:32].hex()}")
        else:
            print(f"  [{waited}s] no entries yet")

    if not decrypt_observed:
        # Final read — record whatever's there.
        try:
            final_entries = read_revealed_commitments(st, args.netuid, ss58)
            if final_entries:
                latest = final_entries[-1]
                decrypt_block = latest.block_number
                decrypt_payload_hex = latest.payload_bytes.hex()
                payload_match = False
        except Exception:
            pass

    secs_past_reveal = waited - (target - submission_round) * 3 if target else 0

    if decrypt_observed:
        notes = (f"PASS: decrypt observed {waited}s after submission "
                 f"({secs_past_reveal}s past reveal_round). Payload bytes match.")
    elif decrypt_payload_hex:
        notes = (f"PARTIAL: entries present in storage but expected payload "
                 f"({expected.hex()[:16]}...) not found among them. "
                 f"Latest payload bytes: {decrypt_payload_hex[:64]}...")
    else:
        notes = (f"FAIL: no entries present in RevealedCommitments after "
                 f"{waited}s ({secs_past_reveal}s past reveal_round). "
                 f"Likely Hypothesis B (TLE format incompatible).")

    result = ProbeResult(
        extrinsic_accepted=True,
        extrinsic_hash=res.extrinsic_hash,
        submission_unix=submission_unix,
        submission_drand_round=submission_round,
        target_reveal_round=target,
        waited_seconds=waited,
        decrypt_observed=decrypt_observed,
        decrypt_round_observed=decrypt_round_at_observation,
        decrypt_block=decrypt_block,
        decrypt_payload_hex=decrypt_payload_hex,
        expected_payload_hex=expected.hex(),
        payload_match=payload_match,
        notes=notes,
    )
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(asdict(result), indent=2))
    print()
    print(f"VERDICT: {notes}")
    print(f"wrote {args.output}")
    return 0 if decrypt_observed else 3


if __name__ == "__main__":
    sys.exit(main())
