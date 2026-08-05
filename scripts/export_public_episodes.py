#!/usr/bin/env python3
"""Export an epoch's episode INPUTS as a public, trainable companion file.

The published outcomes (`data/outcomes/<epoch>.json`) are labels only — not
trainable without the episode features. This exports those features: the exact
episode INPUT payloads the validator serves registered miners (the same shape as
the bundled `data/training/training_episodes.json` `input` block), keyed by
`episode_id`. Join with the outcomes file by `episode_id` to reconstruct
(input, outcome) training pairs.

It pulls via `fetch_episodes_only` (inputs WITHOUT `validator_only_outcomes`) and
runs a PII guard before writing: the inputs are de-identified by design (all
identifiers are SHA-256 hashes; everything else is categorical/numeric), so the
guard ABORTS if any email- or URL-looking value appears.

Run AFTER the epoch's submission deadline. Requires HOPE_API_KEY / HOPE_API_URL.

Usage:
    python scripts/export_public_episodes.py --release WR-2026-W21-PUB-E1 \\
        --out data/episodes/WR-2026-W21-PUB-E1.json \\
        --cross-check data/outcomes/WR-2026-W21-PUB-E1.json
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_URL = re.compile(r"https?://", re.IGNORECASE)


def _pii_scan(obj, path=""):
    """Return a list of (path, value) for any email/URL-looking string leaf."""
    hits = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            hits += _pii_scan(v, f"{path}/{k}")
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            hits += _pii_scan(v, f"{path}[{i}]")
    elif isinstance(obj, str) and (_EMAIL.search(obj) or _URL.search(obj)):
        hits.append((path, obj[:80]))
    return hits


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--release", required=True, help="epoch_id / release_key")
    p.add_argument("--out", required=True, help="output JSON path")
    p.add_argument("--cross-check", default="",
                   help="published outcomes JSON — assert every outcome episode_id "
                        "has a matching input (so the join is complete)")
    args = p.parse_args(argv)

    from hope.validator.data_client import HopeDataClient

    client = HopeDataClient()
    epoch = asyncio.run(client.fetch_episodes_only(args.release))

    episodes = []
    for ep in epoch.episodes:
        episodes.append({
            "episode_id": str(ep.episode_metadata.episode_id),
            "input": ep.model_dump(mode="json"),
        })
    episodes.sort(key=lambda e: e["episode_id"])
    out = {
        "epoch_id": args.release,
        "schema_version": epoch.schema_version,
        "episodes": episodes,
    }

    # PII guard — de-identified by design; refuse to write if anything leaks.
    hits = _pii_scan(episodes)
    if hits:
        print(f"ERROR: PII guard found {len(hits)} email/URL-looking value(s); "
              f"refusing to write:", file=sys.stderr)
        for path, val in hits[:10]:
            print(f"  {path} = {val}", file=sys.stderr)
        return 1

    print(f"[export] {args.release}: {len(episodes)} episode inputs "
          f"(schema {epoch.schema_version}) | PII guard: clean", flush=True)

    if args.cross_check:
        with open(args.cross_check) as f:
            oc = json.load(f)
        outcome_ids = {e["episode_id"] for e in oc.get("episodes", [])}
        input_ids = {e["episode_id"] for e in episodes}
        missing = outcome_ids - input_ids
        if missing:
            print(f"ERROR: {len(missing)} outcome episode_id(s) have no matching "
                  f"input: {sorted(missing)[:5]}", file=sys.stderr)
            return 1
        print(f"[export] cross-check OK: all {len(outcome_ids)} published outcomes "
              f"have a matching input", flush=True)

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"[export] wrote {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
