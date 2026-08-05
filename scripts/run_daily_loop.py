"""Run the validator's daily loop (settle -> capture -> advisory ->
publish -> weights intent) for a given day.

Usage:
    python3 scripts/run_daily_loop.py [YYYY-MM-DD] \
        [--shadow-root DIR] [--ledger-root DIR]

Defaults: day = today (UTC), roots = ./sn21_ledger. Wire-ins:
- outcomes: settle_day_flow.operator_outcomes_provider (needs the operator platform package)
- ed25519 key: SN21_ED25519_KEY_FILE (publication skipped without it)
- day volume: episode_count of the day's BD- basket (the operator's release registry)
- earnings: zero pre-M4 (the M4 cutover injects the real provider)
- anchor: OFF unless SN21_ANCHOR_COMMITS is truthy. When it is, this script
  builds the chain committer from the wallet settings below and the loop
  commits the feed's rolling Merkle root once per published day.

Anchoring settings (all required together, and only read when the flag is on):
    SN21_ANCHOR_COMMITS   1/true/yes/on — the switch
    SN21_WALLET_NAME      bittensor wallet holding the validator hotkey
    SN21_WALLET_HOTKEY    hotkey name (default: "default")
    SN21_BT_NETWORK       "finney" (mainnet) or "test" (testnet)
    SN21_NETUID           21 mainnet / 466 testnet

Chain spend stays explicit: with the flag off nothing here imports bittensor
or touches a wallet, and a missing setting refuses to anchor with a stated
reason rather than guessing at a network or a hotkey.
"""

import json
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from hope.scoring.settle_day_flow import operator_outcomes_provider
from hope.validator.daily_loop import run_daily_loop


def _key_loader():
    path = os.environ.get("SN21_ED25519_KEY_FILE", "")
    if not path or not os.path.exists(path):
        return None
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    with open(path, "rb") as f:
        return Ed25519PrivateKey.from_private_bytes(f.read()[:32])


ANCHOR_FLAG_ENV = "SN21_ANCHOR_COMMITS"
_TRUTHY = ("1", "true", "yes", "on")


def _anchor_committer(ledger_root: str, environ=None):
    """The chain committer, or None with the reason printed.

    Returns None unless the flag is on AND the wallet settings are complete.
    A half-configured anchor must not fall back to a default network or a
    guessed hotkey: committing the right root from the wrong identity is
    worse than not committing, because it looks anchored and verifies against
    nothing.
    """
    env = os.environ if environ is None else environ
    if (env.get(ANCHOR_FLAG_ENV) or "").strip().lower() not in _TRUTHY:
        return None                       # the loop reports "off" itself

    wallet_name = (env.get("SN21_WALLET_NAME") or "").strip()
    hotkey = (env.get("SN21_WALLET_HOTKEY") or "default").strip()
    network = (env.get("SN21_BT_NETWORK") or "").strip()
    netuid_raw = (env.get("SN21_NETUID") or "").strip()
    missing = [n for n, v in (("SN21_WALLET_NAME", wallet_name),
                              ("SN21_BT_NETWORK", network),
                              ("SN21_NETUID", netuid_raw)) if not v]
    if missing:
        print(f"[anchor] {ANCHOR_FLAG_ENV} is on but {', '.join(missing)} "
              f"unset — NOT anchoring", flush=True)
        return None
    try:
        netuid = int(netuid_raw)
    except ValueError:
        print(f"[anchor] SN21_NETUID={netuid_raw!r} is not an integer — "
              f"NOT anchoring", flush=True)
        return None

    import bittensor as bt

    from hope.publication.anchor import bittensor_committer

    print(f"[anchor] committing the feed root from wallet={wallet_name} "
          f"hotkey={hotkey} network={network} netuid={netuid}", flush=True)
    return bittensor_committer(
        bt.subtensor(network=network),
        bt.wallet(name=wallet_name, hotkey=hotkey),
        netuid,
        ledger_root,
    )


def _basket_volume(day: date) -> int:
    _platform_path = os.environ.get("SN21_PLATFORM_PATH")
    if _platform_path:
        sys.path.insert(0, _platform_path)
    try:
        from app.models import get_session
    except ImportError as exc:
        raise RuntimeError(
            "the operator data platform package is not importable — set "
            "SN21_PLATFORM_PATH to its location."
        ) from exc
    from sqlalchemy import text as T
    with get_session() as s:
        n = s.execute(T(
            "SELECT episode_count FROM bittensor_release_registry "
            "WHERE release_key = :k"
        ), {"k": f"BD-{day}"}).scalar()
    return int(n or 0)


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    day = date.fromisoformat(args[0]) if args else datetime.now(timezone.utc).date()

    def opt(name, default):
        return (sys.argv[sys.argv.index(name) + 1]
                if name in sys.argv else default)

    shadow_root = opt("--shadow-root", "./sn21_ledger")
    ledger_root = opt("--ledger-root", "./sn21_ledger")

    key = _key_loader()
    summary = run_daily_loop(
        shadow_root=shadow_root,
        ledger_root=ledger_root,
        day=day,
        outcomes_provider=operator_outcomes_provider,
        key_loader=(lambda: key) if key is not None else None,
        day_volume_provider=_basket_volume,
        chain_committer=_anchor_committer(ledger_root),
    )
    print(json.dumps(summary, indent=1, default=str))


if __name__ == "__main__":
    main()
