"""Short-lived CLI: read the validator's 9.C.2 post-scoring artifact for a
given epoch_id from chain RevealedCommitments and print it as JSON.

Invoked as a subprocess by the validator HTTP API's /verification endpoint
so that bittensor / substrate-interface state dies with the subprocess on
exit, rather than accumulating in the long-running daemon (same pattern as
`hope.validator.metagraph_dump`).

Output schema (single JSON line on stdout):

    On success — 9.C.2 plaintext found for epoch_id:
      {"found": true, "data": {<9.C.2 fields, bytes hex-encoded>}}

    Epoch not yet revealed for this validator:
      {"found": false, "n_revealed": <int>}

Non-zero exit + a one-line JSON error on stderr on hard failure (RPC
unreachable, etc.). The caller treats stdout JSON as authoritative.
"""
from __future__ import annotations

import argparse
import json
import sys


def _bytes_to_hex(obj):
    """Recursively convert bytes → hex strings so the result is JSON-clean."""
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    if isinstance(obj, dict):
        return {k: _bytes_to_hex(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_bytes_to_hex(v) for v in obj]
    return obj


def _is_post_scoring_artifact(payload: dict) -> bool:
    """Heuristic: 9.C.2 has these identifying keys, others (9.C.1, 9.C.6) don't."""
    return all(
        k in payload
        for k in ("final_score_root", "scoring_hash", "weights_commit_block_hash")
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dump SN21 validator's 9.C.2 post-scoring artifact for an epoch.",
    )
    parser.add_argument("--network", required=True, choices=["test", "finney", "local"])
    parser.add_argument("--netuid", type=int, required=True)
    parser.add_argument("--validator-hotkey", required=True,
                        help="ss58 of the validator hotkey whose 9.C.2 to fetch")
    parser.add_argument("--epoch-id", required=True,
                        help="release_key string to match against")
    args = parser.parse_args()

    try:
        import bittensor as bt
        from hope.commitment.canonical import canonical_cbor_loads
        from hope.commitment.chain_reader import (
            decode_revealed_tle_plaintext,
            read_revealed_commitments,
        )
        with bt.Subtensor(network=args.network) as subtensor:
            revealed = read_revealed_commitments(
                subtensor, args.netuid, args.validator_hotkey,
            )
    except Exception as exc:
        print(json.dumps({
            "error": f"{type(exc).__name__}: {exc}",
        }), file=sys.stderr)
        return 1

    for entry in revealed:
        try:
            payload_bytes = decode_revealed_tle_plaintext(entry.payload_bytes)
            payload = canonical_cbor_loads(payload_bytes)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        if payload.get("epoch_id") != args.epoch_id:
            continue
        if not _is_post_scoring_artifact(payload):
            continue
        out = {"found": True, "data": _bytes_to_hex(payload)}
        sys.stdout.write(json.dumps(out) + "\n")
        return 0

    sys.stdout.write(json.dumps({
        "found": False,
        "n_revealed": len(revealed),
    }) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
