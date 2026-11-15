"""The first chain block at which each hotkey was seen committing each digest.

The chain holds only a hotkey's LATEST commitment, with its block: committing
the same digest again moves the block later, and committing another digest
loses the old one. Two hotkeys that commit one model before the same intake
first run it on the same day, so the day cannot order them; the block can
(MrCyRex, 13 Nov 2026). This keeps the earliest block seen per (hotkey,
digest), read from the commitments the daily run already reads, so it can
order such a tie. It starts on the day it is deployed and is never back-filled:
a block not seen is unknown, and unknown sorts after every known block.

registry/_commit_blocks.json: {"blocks": {hotkey: {short digest: block}}}
"""
from __future__ import annotations

import json
import os

from hope.backtest.model_registry import parse_model_commitment

RECORD = os.path.join("registry", "_commit_blocks.json")


def short(digest) -> str | None:
    """The 16-hex form the receipt publishes (as hope.scoring.model_line.short)."""
    text = str(digest or "").strip()
    if ":" in text:
        text = text.rsplit(":", 1)[1]
    return text[:16].lower() or None


def load(root: str) -> dict:
    try:
        with open(os.path.join(root, RECORD)) as f:
            return (json.load(f) or {}).get("blocks") or {}
    except (OSError, ValueError):
        return {}


def remember(root: str, commitments: dict, save_it: bool = True) -> dict:
    """Fold today's commitments (hotkey -> (block, raw commitment)) in.
    The earliest block per (hotkey, digest) is kept; a block of 0 or None is
    unknown and not recorded."""
    blocks = {hk: dict(v) for hk, v in load(root).items()}
    for hk, value in (commitments or {}).items():
        try:
            block, raw = value
            block = int(block) if block is not None else 0
        except (TypeError, ValueError):
            continue
        parsed = parse_model_commitment(raw)
        dg = short(parsed and parsed.get("digest"))
        if not dg or block <= 0:
            continue
        mine = blocks.setdefault(hk, {})
        mine[dg] = min(mine.get(dg) or block, block)
    if save_it:
        path = os.path.join(root, RECORD)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump({"blocks": blocks}, f, indent=1, sort_keys=True)
        os.replace(path + ".tmp", path)
    return blocks
