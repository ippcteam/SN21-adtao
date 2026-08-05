#!/usr/bin/env python3
"""Reg-index staleness alarm — ground-truth health for the registration index.

The reg-index is only as good as how recently its EVENT scan
(`scripts/build_reg_index.py`) reached chain head. If that scan stalls, the
index silently stops seeing new registrations: freshly-bound miners are absent,
get served as DQ "not registered", score 0, and (on a full subnet) get pruned —
all while logs look green. This is exactly what happened on 2026-06-04, when the
scan froze at block 8,332,540 and went unnoticed for ~11 days.

This is the reg-index analogue of the validator's `LastUpdate`-gap check for
weights: read the builder's checkpoint (`<index>.state.json` → `last_scanned_block`),
compare it to chain head, and if the gap exceeds the threshold, raise a loud,
actionable WARNING. Run it as the FIRST daemon tick step so it fires every tick
regardless of whether the builder itself ran or succeeded.

Exit code is 0 on a fresh index and 0 when it cannot determine the gap (never
disrupts the rest of the tick); it returns a non-zero code ONLY when the index
is confirmed stale, so an external monitor can scrape it too.

Usage:
    python -m scripts.check_reg_index_staleness --network finney \\
        --index /var/data/sn21-validator/sn21-reg-index.json --threshold-blocks 2000
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

logger = logging.getLogger("check_reg_index_staleness")

# A reg-index more than this many blocks behind head is treated as stale. ~2000
# blocks ≈ 6.7h at 12s/block — comfortably longer than a healthy daily tick's
# cadence, short enough to catch a freeze the same day instead of 11 days later.
DEFAULT_THRESHOLD_BLOCKS = 2000

EXIT_FRESH = 0
EXIT_UNKNOWN = 0  # cannot determine → don't disrupt the tick
EXIT_STALE = 3    # confirmed stale → loud + scrapeable by external monitors


def read_last_scanned_block(state_path: str) -> int | None:
    """Return `last_scanned_block` from the builder's `<index>.state.json`
    sidecar, or None if the sidecar is missing/unreadable/lacks the field."""
    try:
        with open(state_path, "r", encoding="utf-8") as fh:
            state = json.load(fh)
    except (OSError, ValueError):
        return None
    block = state.get("last_scanned_block")
    try:
        return int(block) if block is not None else None
    except (TypeError, ValueError):
        return None


def evaluate(last_scanned_block: int | None, head: int | None,
             threshold_blocks: int) -> tuple[int | None, bool]:
    """Pure core: return (gap, is_stale).

    gap is `head - last_scanned_block` (None if either input is unknown).
    is_stale is True only when gap is known AND exceeds threshold_blocks.
    """
    if last_scanned_block is None or head is None:
        return None, False
    gap = int(head) - int(last_scanned_block)
    return gap, gap > threshold_blocks


def _warn_stale(gap: int, last_scanned_block: int, head: int,
                threshold_blocks: int) -> None:
    logger.warning(
        "\n"
        "==================== REG-INDEX STALE ====================\n"
        " reg-index is %d blocks behind chain head (threshold %d).\n"
        "   last_scanned_block = %d\n"
        "   chain head         = %d\n"
        " The event scan has stalled. Miners who bound after block %d are\n"
        " INVISIBLE to scoring (served as DQ 'not registered') and will be\n"
        " pruned on a full subnet. Catch it up now:\n"
        "   hope-reg-index-builder --network \"$SN21_REG_INDEX_ARCHIVE_URL\" \\\n"
        "     --netuid 21 --role miner --index \"$SN21_REG_INDEX_PATH\" \\\n"
        "     --max-blocks-per-pass 200000 --checkpoint-every 1000 --reconnect\n"
        "=========================================================",
        gap, threshold_blocks, last_scanned_block, head, last_scanned_block,
    )


def main(argv: list | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--network", default=os.environ.get("BT_NETWORK", "finney"),
                   help="Lite node is fine — we only read chain head.")
    p.add_argument("--index", default=os.environ.get("SN21_REG_INDEX_PATH", ""),
                   help="Path to the reg-index JSON; its <index>.state.json "
                        "sidecar carries last_scanned_block.")
    p.add_argument("--threshold-blocks", type=int,
                   default=int(os.environ.get("SN21_REG_INDEX_STALENESS_ALARM_BLOCKS",
                                              str(DEFAULT_THRESHOLD_BLOCKS))))
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level.upper(), logging.INFO),
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if not args.index:
        logger.warning("no --index/SN21_REG_INDEX_PATH given; cannot check staleness")
        return EXIT_UNKNOWN

    state_path = args.index + ".state.json"
    last_scanned = read_last_scanned_block(state_path)
    if last_scanned is None:
        logger.warning("no checkpoint at %s (builder never ran a full pass?); "
                       "cannot assess staleness", state_path)
        return EXIT_UNKNOWN

    try:
        from hope.validator._subtensor import make_subtensor
        head = int(make_subtensor(args.network).get_current_block())
    except Exception as exc:  # chain unreachable — don't disrupt the tick
        logger.warning("could not read chain head (%s); skipping staleness check",
                       str(exc)[:80])
        return EXIT_UNKNOWN

    gap, is_stale = evaluate(last_scanned, head, args.threshold_blocks)
    if is_stale:
        _warn_stale(gap, last_scanned, head, args.threshold_blocks)
        return EXIT_STALE
    logger.info("reg-index fresh: %d blocks behind head (<= %d); last_scanned=%d head=%d",
                gap, args.threshold_blocks, last_scanned, head)
    return EXIT_FRESH


if __name__ == "__main__":
    sys.exit(main())
