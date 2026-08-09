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

from hope.scoring.settle_day_flow import (
    http_outcomes_provider,
    operator_outcomes_provider,
)
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


def _outcomes_provider(environ=None):
    """Where the day's settled truth comes from.

    Two sources, same shape. If SN21_OUTCOMES_API_URL and _API_KEY are set the
    loop asks the operator's keyed API over HTTP and needs NO database login —
    which is what makes it safe to run on the public-facing validator. With
    them unset it reads the operator database directly, which suits an
    internal host that already has those credentials.

    The choice is a deployment decision, not a behavioural one: the scoring
    path cannot tell the two apart.
    """
    env = os.environ if environ is None else environ
    url = (env.get("SN21_OUTCOMES_API_URL") or "").strip()
    key = (env.get("SN21_OUTCOMES_API_KEY") or "").strip()
    if url and key:
        print(f"[outcomes] over HTTP from {url} — no database login on this host",
              flush=True)
        return http_outcomes_provider(url, key)
    if url or key:
        # Half-configured means somebody intended HTTP. Falling back to a
        # database read would silently need credentials this host should not
        # have, so refuse rather than guess.
        missing = "SN21_OUTCOMES_API_KEY" if url else "SN21_OUTCOMES_API_URL"
        raise SystemExit(f"[outcomes] {missing} is unset but its partner is "
                         f"set — refusing to fall back to a direct database "
                         f"read on a host configured for HTTP")
    print("[outcomes] direct from the operator database", flush=True)
    return operator_outcomes_provider


def _coldkey_reader(environ=None):
    """A callable returning {hotkey: coldkey}, or None with the reason printed.

    The coldkey cap is the cheapest anti-sybil control there is — one coldkey,
    one earning seat — and it needs exactly one fact the chain already knows.
    Returning None disables the cap rather than guessing: a hotkey whose owner
    we could not read has not been shown to be farming anything, and taking a
    seat away over our own blind spot is the worse error.

    Injected rather than imported so the loop keeps working, and keeps being
    testable, without a chain.
    """
    env = os.environ if environ is None else environ
    network = (env.get("SN21_BT_NETWORK") or "").strip()
    netuid_raw = (env.get("SN21_NETUID") or "").strip()
    missing = [n for n, v in (("SN21_BT_NETWORK", network),
                              ("SN21_NETUID", netuid_raw)) if not v]
    if missing:
        print(f"[coldkey-cap] {', '.join(missing)} unset — the one-seat-per-"
              f"coldkey cap is OFF", flush=True)
        return None
    try:
        netuid = int(netuid_raw)
    except ValueError:
        print(f"[coldkey-cap] SN21_NETUID={netuid_raw!r} is not an integer — "
              f"cap is OFF", flush=True)
        return None

    def read():
        import bittensor as bt

        metagraph = bt.subtensor(network=network).metagraph(netuid)
        # Both lists are indexed by uid, so they line up positionally.
        return {hk: ck for hk, ck in zip(metagraph.hotkeys, metagraph.coldkeys)}

    print(f"[coldkey-cap] ON — reading identities from network={network} "
          f"netuid={netuid}", flush=True)
    return read


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
    """How many episodes the day's basket held — over HTTP when configured.

    Same reasoning as the outcomes provider: this is one integer, and reading
    it directly is the last thing that would force a database login onto the
    public-facing validator.
    """
    url = (os.environ.get("SN21_OUTCOMES_API_URL") or "").strip()
    key = (os.environ.get("SN21_OUTCOMES_API_KEY") or "").strip()
    if url and key:
        import json as _json
        import urllib.request
        req = urllib.request.Request(
            f"{url.rstrip('/')}/internal/bittensor/v1/daily/basket-volume"
            f"?day={day.isoformat()}",
            headers={"X-API-Key": key})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return int(_json.loads(resp.read().decode()).get("episode_count") or 0)

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


# Options that take a value. Their value must not be mistaken for the day.
_VALUED_OPTS = ("--shadow-root", "--ledger-root")


def _parse_args(argv):
    """(day, shadow_root, ledger_root) from the command line.

    The previous version filtered out anything starting with "--" and treated
    whatever was left as the date. Option VALUES do not start with "--", so
    `--ledger-root /var/lib/...` left the path in the positional list and the
    loop died parsing a directory as a date. It failed in under thirty seconds
    every time, on a host with no readable logs, which made it look like an
    environment problem for most of a morning.
    """
    day = None
    shadow_root = ledger_root = "./sn21_ledger"
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in _VALUED_OPTS:
            value = argv[i + 1] if i + 1 < len(argv) else None
            if value is None:
                raise SystemExit(f"{a} needs a path")
            if a == "--shadow-root":
                shadow_root = value
            else:
                ledger_root = value
            i += 2
            continue
        if a.startswith("--"):
            i += 1
            continue
        if day is None:
            try:
                day = date.fromisoformat(a)
            except ValueError:
                raise SystemExit(
                    f"expected a YYYY-MM-DD day, got {a!r}. If that was meant "
                    f"as an option value, its option is missing.") from None
        i += 1
    return (day or datetime.now(timezone.utc).date()), shadow_root, ledger_root


def main():
    day, shadow_root, ledger_root = _parse_args(sys.argv[1:])

    key = _key_loader()
    summary = run_daily_loop(
        shadow_root=shadow_root,
        ledger_root=ledger_root,
        day=day,
        outcomes_provider=_outcomes_provider(),
        key_loader=(lambda: key) if key is not None else None,
        day_volume_provider=_basket_volume,
        chain_committer=_anchor_committer(ledger_root),
        coldkey_reader=_coldkey_reader(),
    )
    print(json.dumps(summary, indent=1, default=str))


if __name__ == "__main__":
    main()
