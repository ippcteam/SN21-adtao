"""Run the validator's daily loop (settle -> capture -> advisory ->
publish -> weights intent) for a given day.

Usage:
    python3 scripts/run_daily_loop.py [YYYY-MM-DD] \
        [--shadow-root DIR] [--ledger-root DIR]

Defaults: day = today (UTC), roots = ./sn21_ledger. Wire-ins:
- outcomes: settle_day_flow.obi_outcomes_provider (needs the OBI repo)
- ed25519 key: SN21_ED25519_KEY_FILE (publication skipped without it)
- day volume: episode_count of the day's BD- basket (OBI registry)
- earnings: zero pre-M4 (the M4 cutover injects the real provider)
- anchor: off unless SN21_ANCHOR_COMMITS=true AND the host wires a
  committer (deliberately not constructed here — chain spend is explicit)
"""

import json
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

from hope.scoring.settle_day_flow import obi_outcomes_provider  # noqa: E402
from hope.validator.daily_loop import run_daily_loop  # noqa: E402


def _key_loader():
    path = os.environ.get("SN21_ED25519_KEY_FILE", "")
    if not path or not os.path.exists(path):
        return None
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    with open(path, "rb") as f:
        return Ed25519PrivateKey.from_private_bytes(f.read()[:32])


def _basket_volume(day: date) -> int:
    sys.path.insert(0, "/Users/macbookm1/Documents/Projects/obi")
    from app.models import get_session
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
        outcomes_provider=obi_outcomes_provider,
        key_loader=(lambda: key) if key is not None else None,
        day_volume_provider=_basket_volume,
    )
    print(json.dumps(summary, indent=1, default=str))


if __name__ == "__main__":
    main()
