"""Phase 0 — Drand quicknet TLE round-trip diagnostic (LEGACY).

Phase 0 Q34 superseded this: the `timelock` PyPI package only publishes
dev wheels (`timelock 0.0.1.dev0`) and won't install in CI on Linux/x86_64
or macOS arm64. SN21 uses `bittensor_drand` (already a transitive dep of
`bittensor`) for all TLE encryption — see
`hope/commitment/on_chain.py:submit_timelock_commit`.

This script is preserved for reference only. It will skip on systems
without the `timelock` package (the common case) instead of failing.

Run:
    python scripts/phase0/drand_tle_roundtrip.py
"""

from __future__ import annotations

import json
import sys
import time
from urllib.request import urlopen

try:
    from timelock import Timelock  # type: ignore[import-not-found]
except ImportError:
    print("SKIPPED: 'timelock' PyPI package not installed (Phase 0 Q34: "
          "bittensor_drand replaces it; see hope/commitment/on_chain.py).")
    sys.exit(0)

from hope.commitment.drand_lib import (
    QUICKNET_CHAIN_HASH,
    QUICKNET_PUBLIC_KEY_HEX,
    drand_round_at,
)


DRAND_API_BASE = f"https://api.drand.sh/{QUICKNET_CHAIN_HASH.hex()}"


def fetch_drand_round_info() -> dict:
    """Fetch quicknet chain info from drand HTTP API."""
    url = f"{DRAND_API_BASE}/info"
    with urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def fetch_drand_round_signature(round_number: int) -> bytes:
    """Fetch the BLS signature for a given drand round."""
    url = f"{DRAND_API_BASE}/public/{round_number}"
    with urlopen(url, timeout=10) as resp:
        data = json.loads(resp.read())
    return bytes.fromhex(data["signature"])


def main() -> int:
    print("Phase 0 — Drand TLE round-trip diagnostic")
    print("=" * 50)

    # Step 1: confirm chain info matches our hardcoded constants
    print("Step 1: Fetching quicknet chain info...")
    info = fetch_drand_round_info()
    chain_pk = info["public_key"]
    period = info["period"]
    genesis = info["genesis_time"]

    print(f"  Period:        {period}s (expected 3)")
    print(f"  Genesis time:  {genesis} (expected 1692803367)")
    print(f"  Public key:    {chain_pk[:32]}...")

    if period != 3:
        print(f"FAIL: drand quicknet period is {period}, expected 3")
        return 1
    if chain_pk != QUICKNET_PUBLIC_KEY_HEX:
        print("FAIL: drand quicknet public key doesn't match our hardcoded value")
        print(f"  hardcoded: {QUICKNET_PUBLIC_KEY_HEX[:32]}...")
        print(f"  fetched:   {chain_pk[:32]}...")
        return 1
    if genesis != 1692803367:
        print(f"FAIL: drand genesis time is {genesis}, expected 1692803367")
        return 1
    print("  OK — chain info matches hardcoded constants")

    # Step 2: encrypt to a round 30 seconds in the future
    print("\nStep 2: TLE encrypting to round 30s in the future...")
    now = int(time.time())
    target_round = drand_round_at(now + 30)
    print(f"  Current Unix time: {now}")
    print(f"  Target round:      {target_round}")

    plaintext = b"hope-sn21-tle-roundtrip-test-" + now.to_bytes(8, "big")
    print(f"  Plaintext:         {plaintext}")

    tl = Timelock(QUICKNET_PUBLIC_KEY_HEX)
    ephemeral_sk = bytearray(b"x" * 32)  # ephemeral; safe to use a fixed value here
    ciphertext = tl.tle(target_round, plaintext, ephemeral_sk)
    print(f"  Ciphertext length: {len(ciphertext)} bytes")
    if len(ciphertext) > 1024:
        print(f"FAIL: ciphertext is {len(ciphertext)} bytes; exceeds TimelockEncrypted limit (1024)")
        return 1
    print("  OK — fits within 1024-byte TimelockEncrypted ceiling")

    # Step 3: poll drand until target round is published
    print(f"\nStep 3: Waiting for round {target_round} to be published...")
    deadline = now + 90  # 90s timeout
    sig_bytes = None
    while time.time() < deadline:
        try:
            sig_bytes = fetch_drand_round_signature(target_round)
            break
        except Exception:
            time.sleep(2)
    if sig_bytes is None:
        print(f"FAIL: round {target_round} not published within 90s")
        return 1
    print(f"  OK — round {target_round} signature fetched ({len(sig_bytes)} bytes)")

    # Step 4: decrypt
    print("\nStep 4: Decrypting ciphertext with the round signature...")
    decrypted = tl.tld(ciphertext, bytearray(sig_bytes))
    decrypted_bytes = bytes(decrypted)
    print(f"  Decrypted: {decrypted_bytes}")

    if decrypted_bytes != plaintext:
        print("FAIL: decrypted bytes don't match plaintext")
        print(f"  expected: {plaintext}")
        print(f"  got:      {decrypted_bytes}")
        return 1

    print("\n" + "=" * 50)
    print("PASS — TLE round-trip works against drand quicknet")
    print(f"  Ciphertext size: {len(ciphertext)} bytes (≤1024 OK)")
    print(f"  Round latency:   ~{int(time.time() - now)}s wall-clock")
    return 0


if __name__ == "__main__":
    sys.exit(main())
