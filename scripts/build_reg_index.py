#!/usr/bin/env python3
"""Rolling, self-serve SN21 registration-index builder.

Build (and inherently verify) the miner registration index DIRECTLY from chain
— no operator hand-off. Point this at YOUR archival node and you get the same
index every validator gets, verified-by-construction (each `sn21-reg-v1`
binding's ed25519 signature is checked before it's indexed).

WHY ROLLING / DAILY:
    A miner's `sn21-reg-v1` commit is overwritten ~1 block later by its
    prediction bundle, so it lives in *current* chain state for only ~1 block.
    Reconstructing it weeks later from a flaky public archive is painful.
    Scanning DAILY instead catches every registration within ~24h of commit
    (cheap recent reads; instant on a local archival node), via the
    `Commitments.Commitment` EVENT, which fires at the commit block regardless
    of what overwrites the slot afterwards. Run this once a day (cron/timer) or
    with --loop, and by the time the submission window closes the index already
    holds every registered miner — no reactive recovery, no misses.

PERSISTENCE (resume cheaply):
    --index PATH        the reg-index JSON — a BARE LIST, the exact format the
                        scorer (`--reg-index-prebuilt`), `verify_epoch.py`, and
                        the prediction server already consume. UNCHANGED.
    PATH + ".state.json"  sidecar checkpoint {last_scanned_block, ...}. The next
                        run resumes from last_scanned_block+1 — so a daily tick
                        only scans ~one day of blocks, not the whole chain.
    Keep both on a PERSISTENT disk (on Render: a web/worker service — cron
    services have no disk; co-locating with the archive node service is ideal).

USAGE
  Daily tick (one-shot; resumes from the sidecar, scans new blocks, exits):
    SN21_SUBTENSOR_URL=ws://localhost:9944 \\
    python scripts/build_reg_index.py --network finney --netuid 21 \\
        --role miner --index /var/data/sn21/reg-index.json

  Cold start (no sidecar yet) — backfill an explicit range once, then daily:
    python scripts/build_reg_index.py ... --index ... --backfill-start 8200000

  Long-running loop (for the consolidated daemon):
    python scripts/build_reg_index.py ... --index ... --loop --interval-hours 24

The output is byte-for-byte the same shape as `probe_registration_index.py`'s
`--output-json`, so existing consumers need no change.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Optional

from hope.validator._subtensor import make_subtensor
from hope.validator.registration_index import RegistrationIndex
from hope.commitment.registration import RegistrationRole

logger = logging.getLogger("build_reg_index")

_ROLES = {
    "miner": RegistrationRole.MINER,
    "validator": RegistrationRole.VALIDATOR,
    "outcome_signer": RegistrationRole.OUTCOME_SIGNER,
}

# A miner registers then submits its bundle in the next block(s); without a
# prior checkpoint we re-scan a safety window back from head so a freshly
# started builder still catches registrations from the last day or two.
DEFAULT_COLD_START_LOOKBACK_BLOCKS = 14_400  # ~2 days at 12s/block


def _atomic_write_json(path: str, obj) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, sort_keys=True)
    os.replace(tmp, path)


def _state_path(index_path: str) -> str:
    return f"{index_path}.state.json"


def _save(index_path: str, role: str, netuid: int,
          last_scanned_block: int, entries: list) -> None:
    """Persist the index (bare list) + the sidecar checkpoint, atomically."""
    _atomic_write_json(index_path, entries)
    _atomic_write_json(_state_path(index_path), {
        "last_scanned_block": int(last_scanned_block),
        "entries": len(entries),
        "role": role,
        "netuid": netuid,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    })


def _load(index_path: str, index: RegistrationIndex) -> Optional[int]:
    """Load a prior index list + sidecar checkpoint into `index`.

    Returns the restored last_scanned_block, or None if no checkpoint.
    """
    if os.path.exists(index_path):
        try:
            with open(index_path) as f:
                prior = json.load(f)
            if isinstance(prior, list):
                merged = index.merge_json(prior)
                logger.info("loaded %d prior entries from %s", merged, index_path)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not load %s (%s); starting fresh", index_path, exc)

    sp = _state_path(index_path)
    if os.path.exists(sp):
        try:
            with open(sp) as f:
                state = json.load(f)
            blk = int(state["last_scanned_block"])
            index.set_last_scanned_block(blk)
            logger.info("resumed checkpoint: last_scanned_block=%d", blk)
            return blk
        except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
            logger.warning("could not load checkpoint %s (%s)", sp, exc)
    return None


def build_once(index: RegistrationIndex, index_path: str, role: str, netuid: int,
               *, backfill_start: Optional[int], cold_start_lookback: int,
               checkpoint_every: int, end_block: Optional[int] = None) -> int:
    """One pass: scan from the checkpoint (or cold-start window) to the target, persist.

    The target is chain head, unless `end_block` caps it (for chunked
    cold-start backfills or bounded runs). Returns new registrations verified.
    """
    try:
        head = int(index._subtensor.get_current_block())  # read-only
    except Exception as exc:
        logger.error("get_current_block failed: %s", exc)
        return 0
    target = head if end_block is None else min(int(end_block), head)

    last = index.last_scanned_block
    if last is None:
        start = backfill_start if backfill_start is not None else max(0, target - cold_start_lookback)
        logger.info("cold start: scanning [%d, %d] (%d blocks)", start, target, target - start + 1)
    elif last >= target:
        logger.info("up to date (last_scanned_block=%d >= target=%d)", last, target)
        return 0
    else:
        start = last + 1
        logger.info("incremental: scanning [%d, %d] (%d blocks)", start, target, target - start + 1)

    def _checkpoint(block_num, entries):
        _save(index_path, role, netuid, block_num, entries)
        logger.info("checkpoint @block %d: %d entries", block_num, len(entries))

    found = index.scan_range(
        start, target,
        checkpoint_callback=_checkpoint,
        checkpoint_every=max(1, checkpoint_every),
    )
    final_block = index.last_scanned_block if index.last_scanned_block is not None else head
    _save(index_path, role, netuid, final_block, index.to_json())
    logger.info("pass complete: +%d new, %d total, head=%d, stats=%s",
                found, index.size, final_block, index.stats)
    return found


def main(argv: Optional[list] = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--network", default="finney",
                   help="Subtensor network or wss:// URL (overridden by SN21_SUBTENSOR_URL).")
    p.add_argument("--netuid", type=int, default=21)
    p.add_argument("--role", choices=list(_ROLES), default="miner")
    p.add_argument("--index", required=True,
                   help="Path to the reg-index JSON (bare list). Sidecar checkpoint "
                        "is written to <index>.state.json. Keep on a persistent disk.")
    p.add_argument("--backfill-start", type=int, default=None,
                   help="On cold start (no sidecar), scan from this block to head. "
                        "If omitted, scans a safety window back from head.")
    p.add_argument("--end-block", type=int, default=None,
                   help="Cap the scan ceiling for this pass (default chain head). "
                        "Use for chunked cold-start backfills or bounded runs.")
    p.add_argument("--cold-start-lookback-blocks", type=int,
                   default=DEFAULT_COLD_START_LOOKBACK_BLOCKS,
                   help="Cold-start window when --backfill-start is omitted "
                        f"(default {DEFAULT_COLD_START_LOOKBACK_BLOCKS} ~2 days).")
    p.add_argument("--checkpoint-every", type=int, default=500,
                   help="Persist index+sidecar every N scanned blocks (crash safety).")
    p.add_argument("--reconnect", action="store_true",
                   help="Rebuild the RPC connection after repeated read failures "
                        "(recommended against the flaky public archive).")
    p.add_argument("--loop", action="store_true",
                   help="Run forever, one pass per --interval-hours (for the daemon).")
    p.add_argument("--interval-hours", type=float, default=24.0,
                   help="Hours between passes in --loop mode (default 24).")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args(argv)

    sub = make_subtensor(args.network)
    # bittensor's import hijacks stdlib logging (calls logging.disable() and
    # strips the root handler), silently swallowing our scan progress. Undo the
    # global disable and attach a DEDICATED handler to our logger with
    # propagate=False, so the build stays observable regardless of what
    # bittensor does to the root logger.
    logging.disable(logging.NOTSET)
    _lvl = getattr(logging, args.log_level.upper(), logging.INFO)
    _h = logging.StreamHandler(sys.stdout)
    _h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.handlers[:] = [_h]
    logger.setLevel(_lvl)
    logger.propagate = False
    reconnect_target = args.network if args.reconnect else None
    index = RegistrationIndex(
        sub, args.netuid,
        expected_role=_ROLES[args.role],
        reconnect_network=reconnect_target,
    )
    _load(args.index, index)

    def _one():
        return build_once(
            index, args.index, args.role, args.netuid,
            backfill_start=args.backfill_start,
            cold_start_lookback=args.cold_start_lookback_blocks,
            checkpoint_every=args.checkpoint_every,
            end_block=args.end_block,
        )

    if not args.loop:
        _one()
        return 0

    logger.info("loop mode: one pass every %.1fh", args.interval_hours)
    while True:
        try:
            _one()
        except KeyboardInterrupt:
            logger.info("interrupted; exiting loop")
            return 0
        except Exception as exc:
            logger.exception("pass failed (will retry next interval): %s", exc)
        time.sleep(max(60.0, args.interval_hours * 3600.0))


if __name__ == "__main__":
    sys.exit(main())
