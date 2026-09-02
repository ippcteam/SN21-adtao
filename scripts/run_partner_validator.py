"""Reference validator loop for the SN21 daily stream.

Fetches the published daily weight vector over the keyed API and sets it
on chain from your wallet. This is the whole job of an SN21 validator —
see docs/SN21_VALIDATING.md for setup and for the behaviours that make a
correct validator look broken (commit-reveal delay, the rate limit, the
version key, the permit).

Configuration (environment):
  SN21_WEIGHTS_API_URL   default https://hope-ads-backend.onrender.com/internal/bittensor/v1/daily/weights
  SN21_WEIGHTS_API_KEY   your issued key (required)
  SN21_BT_NETWORK        default finney
  SN21_NETUID            default 21
  SN21_WALLET_NAME       default default
  SN21_WALLET_HOTKEY     default default
  SN21_VERSION_KEY       default 0
  SN21_TICK_SECONDS      default 1200

Run:  python -m scripts.run_partner_validator
"""

from __future__ import annotations

import json
import os
import time
import urllib.request


def fetch_vector(url: str, api_key: str) -> dict[str, float]:
    req = urllib.request.Request(url, headers={"X-API-Key": api_key})
    with urllib.request.urlopen(req, timeout=60) as r:
        body = json.loads(r.read())
    vector = body.get("weights") or body.get("vector") or body
    return {str(hk): float(w) for hk, w in vector.items()
            if isinstance(w, (int, float)) and float(w) > 0}


def main() -> int:
    import bittensor as bt

    api_url = os.environ.get(
        "SN21_WEIGHTS_API_URL",
        "https://hope-ads-backend.onrender.com/internal/bittensor/v1/daily/weights")
    api_key = (os.environ.get("SN21_WEIGHTS_API_KEY") or "").strip()
    if not api_key:
        raise SystemExit("SN21_WEIGHTS_API_KEY is not set")
    network = os.environ.get("SN21_BT_NETWORK", "finney")
    netuid = int(os.environ.get("SN21_NETUID", "21"))
    version_key = int(os.environ.get("SN21_VERSION_KEY", "0"))
    tick = int(os.environ.get("SN21_TICK_SECONDS", "1200"))

    wallet = bt.wallet(name=os.environ.get("SN21_WALLET_NAME", "default"),
                       hotkey=os.environ.get("SN21_WALLET_HOTKEY", "default"))
    subtensor = bt.Subtensor(network=network)

    while True:
        try:
            vector = fetch_vector(api_url, api_key)
            mg = subtensor.metagraph(netuid)
            uid_of = {str(mg.hotkeys[i]): int(mg.uids[i])
                      for i in range(len(mg.hotkeys))}
            pairs = [(uid_of[hk], w) for hk, w in vector.items()
                     if hk in uid_of]
            if not pairs:
                print("no vector entries map to registered uids; waiting",
                      flush=True)
            else:
                total = sum(w for _, w in pairs)
                uids = [u for u, _ in pairs]
                weights = [w / total for _, w in pairs]
                ok, msg = subtensor.set_weights(
                    wallet=wallet, netuid=netuid, uids=uids, weights=weights,
                    version_key=version_key, wait_for_inclusion=True)
                # An unsuccessful result inside the subnet's rate-limit
                # window is normal — the loop just waits for the next tick.
                print(f"set_weights ok={ok} ({len(uids)} uids) {msg or ''}",
                      flush=True)
        except Exception as exc:   # noqa: BLE001 — a loop must outlive a bad tick
            print(f"tick failed: {exc}", flush=True)
        time.sleep(tick)


if __name__ == "__main__":
    raise SystemExit(main())
