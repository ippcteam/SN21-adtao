"""Short-lived CLI that prints the current metagraph hotkeys as JSON.

Invoked as a subprocess by `hope.validator.serve._refresh_metagraph` so
that bittensor / substrate-interface internal state (which can't be fully
released by close() + gc.collect()) dies with the subprocess on exit.
The parent daemon's RSS stays flat across an unlimited number of refresh
cycles, instead of accumulating 30-100 MB per refresh as it did when
the metagraph load happened in-process.

Output schema (single JSON line on stdout):
    {"netuid": 466, "n": 12, "hotkeys": ["5G...", "5H...", ...]}

Non-zero exit status + a one-line message on stderr if the chain read
fails for any reason. The parent treats failures as "skip this refresh,
try again next interval" — registered_miners isn't mutated on error.
"""
from __future__ import annotations

import argparse
import json
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dump SN21 metagraph hotkeys as JSON (single line).",
    )
    parser.add_argument("--network", required=True,
                        choices=["test", "finney", "local"])
    parser.add_argument("--netuid", type=int, required=True)
    args = parser.parse_args()

    try:
        from hope.validator._subtensor import make_subtensor
        with make_subtensor(args.network) as subtensor:
            metagraph = subtensor.metagraph(netuid=args.netuid)
            hotkeys = list(metagraph.hotkeys)
    except Exception as exc:
        print(f"metagraph_dump failed: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return 1

    payload = {"netuid": args.netuid, "n": len(hotkeys), "hotkeys": hotkeys}
    sys.stdout.write(json.dumps(payload) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
