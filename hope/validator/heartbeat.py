"""Bittensor activity-floor heartbeat for the SN21 validator.

The chain's per-subnet ``activity_cutoff`` (5,000 blocks ≈ 16.7h on mainnet)
forces validators to publish ``set_weights`` regularly or their weights
drop out of the consensus computation. SN21's authoritative scoring
runs only once per week — fine for producing meaningful weights, way
too infrequent for the chain's activity floor.

This module bridges the gap with a tiny short-lived process that does
NO scoring of its own — it just **re-asserts whatever weights the
latest weekly scoring run already committed**. Run from a cron at
~3-4h cadence; the script self-throttles so it never submits more
often than ``threshold_blocks`` apart (defaulting to 1500 blocks,
≈ 5h, which gives 3.3× safety margin against the 5,000-block cutoff
and is well above the per-subnet ``WeightsSetRateLimit``).

Design guarantees:

  1. **No scoring authority.** The submitted (uids, weights) are read
     from ``SubtensorModule.Weights[netuid][validator_uid]`` — i.e.
     whatever the chain itself reports for the most recently-revealed
     weights. The heartbeat cannot fabricate new weights.
  2. **No 9.C.* audit-chain side-effects.** Layer 9.C.1/9.C.2/9.C.6
     are only emitted by ``hope-validator`` (the weekly scoring cron).
     This heartbeat produces a bare ``set_weights`` commit; auditors
     who check "every published epoch has matching 9.C.* records"
     continue to see exactly one set per week.
  3. **Self-throttling.** If ``current_block - LastUpdate`` is below
     the threshold (e.g. because the weekly scoring cron just ran),
     the heartbeat exits with action="skipped_recent_activity" and
     does NOT submit. Cron frequency can therefore be set higher
     than strictly necessary without risk of duplicate commits.
  4. **First-deploy safe.** If the validator has never successfully
     committed weights (empty ``Weights[netuid][uid]`` storage), the
     heartbeat exits with action="skipped_no_prior_weights" and a
     clear log line. No error, no broken state.

Failure modes that bubble up as non-zero exits:
  - Chain RPC unreachable (next cron firing retries naturally)
  - WeightsSetRateLimit conflict (rare; recovered on next firing)
  - Wallet missing / unreadable on container (operator config error;
    same diagnostics as ``hope-validator``)
"""

from __future__ import annotations

import argparse
import logging
import os
from dataclasses import dataclass
from typing import Optional

from hope.validator.weights_commit import commit_weights_layer_9c3

logger = logging.getLogger(__name__)


# Default heartbeat threshold: only submit if the validator hasn't set
# weights in this many blocks. 1500 blocks ≈ 5h at 12s/block, which is
# 30% of the mainnet ActivityCutoff (5,000 blocks). Combined with a
# 3-4h cron cadence, this gives the chain at least one heartbeat per
# ActivityCutoff window even if individual cron firings fail.
DEFAULT_THRESHOLD_BLOCKS = 1500


@dataclass(frozen=True)
class HeartbeatResult:
    """Outcome of one heartbeat firing.

    ``action`` is one of:
      - "submitted"                 — set_weights extrinsic was sent
      - "skipped_recent_activity"   — gap < threshold; no submission needed
      - "skipped_no_prior_weights"  — validator has no prior weights to assert
      - "skipped_not_in_metagraph"  — validator hotkey not registered on this netuid
      - "skipped_dry_run"           — --dry-run flag set; would have submitted
      - "failed"                    — set_weights extrinsic failed
    """

    action: str
    gap_blocks: int
    current_block: int
    last_update_block: int
    weights_count: int = 0
    submitted_block: Optional[int] = None
    error_message: Optional[str] = None


def _read_validator_state(
    subtensor,
    netuid: int,
    validator_hotkey_ss58: str,
) -> tuple[int, Optional[int], Optional[int], Optional[list[tuple[int, int]]]]:
    """Read the chain state needed to decide whether to heartbeat.

    Returns:
        (current_block, validator_uid_or_None, last_update_block_or_None,
         weights_or_None) — any of the trailing values can be None when the
        validator hotkey isn't in the metagraph or its storage entry is empty.
    """
    block = subtensor.substrate.get_block()
    current_block = int(block["header"]["number"] if isinstance(block, dict) else block.header.number)

    metagraph = subtensor.metagraph(netuid=netuid)
    if validator_hotkey_ss58 not in metagraph.hotkeys:
        return current_block, None, None, None

    uid = metagraph.hotkeys.index(validator_hotkey_ss58)

    # LastUpdate[netuid] is a Vec<BlockNumber> indexed by uid
    lu_raw = subtensor.substrate.query("SubtensorModule", "LastUpdate", [netuid])
    lu = lu_raw.value if hasattr(lu_raw, "value") else lu_raw
    last_update = int(lu[uid]) if isinstance(lu, list) and uid < len(lu) else None

    # Weights[netuid][uid] is a Vec<(uid_u16, weight_u16)>; empty if the
    # validator has never had a successful weights commit revealed.
    w_raw = subtensor.substrate.query("SubtensorModule", "Weights", [netuid, uid])
    w = w_raw.value if hasattr(w_raw, "value") else w_raw
    if w and isinstance(w, list):
        weights: list[tuple[int, int]] = [(int(t[0]), int(t[1])) for t in w]
    else:
        weights = None  # type: ignore[assignment]

    return current_block, uid, last_update, weights


def run_heartbeat(
    *,
    subtensor,
    validator_wallet,
    netuid: int,
    threshold_blocks: int = DEFAULT_THRESHOLD_BLOCKS,
    dry_run: bool = False,
) -> HeartbeatResult:
    """Read chain state; if a heartbeat is needed, re-assert latest weights.

    Args:
        subtensor: a ``bittensor.Subtensor`` instance (caller manages lifetime).
        validator_wallet: a ``bittensor.Wallet`` whose hotkey signs the extrinsic.
        netuid: subnet id (21 mainnet, 466 testnet).
        threshold_blocks: only submit if current_block - last_update >= this.
        dry_run: if True, log what would have been submitted but do NOT call
            ``set_weights``. Useful for the first week of cron operation.

    Returns:
        HeartbeatResult describing the action taken and the chain state observed.
    """
    val_ss58 = validator_wallet.hotkey.ss58_address
    current_block, uid, last_update, weights = _read_validator_state(
        subtensor, netuid, val_ss58,
    )

    if uid is None:
        logger.warning(
            "[heartbeat] validator hotkey %s... not in metagraph for netuid %d; "
            "no-op", val_ss58[:16], netuid,
        )
        return HeartbeatResult(
            action="skipped_not_in_metagraph",
            gap_blocks=0,
            current_block=current_block,
            last_update_block=0,
        )

    last = last_update if last_update is not None else 0
    gap = current_block - last

    if gap < threshold_blocks:
        logger.info(
            "[heartbeat] netuid=%d uid=%d gap=%d blocks < threshold=%d; "
            "skipped (recent activity present)",
            netuid, uid, gap, threshold_blocks,
        )
        return HeartbeatResult(
            action="skipped_recent_activity",
            gap_blocks=gap,
            current_block=current_block,
            last_update_block=last,
        )

    if not weights:
        logger.info(
            "[heartbeat] netuid=%d uid=%d gap=%d blocks ≥ threshold=%d but "
            "validator has no prior weights to re-assert (Weights storage "
            "empty — likely first deployment, or all prior commits unrevealed)",
            netuid, uid, gap, threshold_blocks,
        )
        return HeartbeatResult(
            action="skipped_no_prior_weights",
            gap_blocks=gap,
            current_block=current_block,
            last_update_block=last,
        )

    uids_to_send = [t[0] for t in weights]
    u16_weights = [t[1] for t in weights]
    # The chain stores u16 weights; the SDK accepts floats in [0,1] and
    # re-quantises. Convert by dividing by the u16 max (65535) — the round
    # trip is lossless for the values the chain itself just produced.
    normalized = [w / 65535.0 for w in u16_weights]

    logger.info(
        "[heartbeat] netuid=%d uid=%d gap=%d blocks ≥ threshold=%d; "
        "re-asserting %d weights%s",
        netuid, uid, gap, threshold_blocks, len(uids_to_send),
        " (DRY RUN)" if dry_run else "",
    )

    if dry_run:
        return HeartbeatResult(
            action="skipped_dry_run",
            gap_blocks=gap,
            current_block=current_block,
            last_update_block=last,
            weights_count=len(uids_to_send),
        )

    # Reuse the exact same commit path as the weekly scoring cron — same
    # `set_weights(commit_reveal_version=4, version_key=10002001)` semantics,
    # same retry logic, same block_hash binding behavior. This guarantees the
    # heartbeat cannot diverge from the scoring path's submission contract.
    result = commit_weights_layer_9c3(
        subtensor=subtensor,
        validator_wallet=validator_wallet,
        netuid=netuid,
        uids=uids_to_send,
        weights=normalized,
    )

    if result.success:
        logger.info(
            "[heartbeat] submitted ok; netuid=%d uids=%d block=%s",
            netuid, len(uids_to_send), result.block_number,
        )
        return HeartbeatResult(
            action="submitted",
            gap_blocks=gap,
            current_block=current_block,
            last_update_block=last,
            weights_count=len(uids_to_send),
            submitted_block=result.block_number,
        )

    logger.warning(
        "[heartbeat] submission FAILED netuid=%d uids=%d msg=%r",
        netuid, len(uids_to_send), result.message,
    )
    return HeartbeatResult(
        action="failed",
        gap_blocks=gap,
        current_block=current_block,
        last_update_block=last,
        weights_count=len(uids_to_send),
        error_message=result.message,
    )


def main() -> int:
    """CLI entry: ``hope-validator-heartbeat`` registered in pyproject.toml."""
    parser = argparse.ArgumentParser(
        description="SN21 validator activity-floor heartbeat (re-assert latest weights).",
    )
    parser.add_argument("--network", default="finney", choices=["test", "finney", "local"])
    parser.add_argument("--netuid", type=int,
                        default=int(os.environ.get("NETUID", "21")))
    parser.add_argument("--wallet-name", default=os.environ.get("WALLET_NAME", ""))
    parser.add_argument("--wallet-hotkey", default=os.environ.get("HOTKEY_NAME", "default"))
    parser.add_argument(
        "--threshold-blocks", type=int,
        default=int(os.environ.get("SN21_HEARTBEAT_THRESHOLD_BLOCKS",
                                   str(DEFAULT_THRESHOLD_BLOCKS))),
        help=f"Only submit if current_block - LastUpdate >= this (default {DEFAULT_THRESHOLD_BLOCKS})",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        default=os.environ.get("SN21_HEARTBEAT_DRY_RUN", "").lower() in ("1", "true", "yes"),
        help="Log what would have been submitted; do NOT call set_weights.",
    )

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if not args.wallet_name:
        logger.error("[heartbeat] WALLET_NAME (or --wallet-name) is required")
        return 2

    import bittensor as bt
    from hope.validator._subtensor import make_subtensor
    wallet = bt.Wallet(name=args.wallet_name, hotkey=args.wallet_hotkey)
    # Trigger an early-failure path if the hotkey is missing rather than
    # waiting until the extrinsic submission. Accessing .ss58_address forces
    # the SDK to load the on-disk key file.
    try:
        _ = wallet.hotkey.ss58_address
    except Exception as e:
        logger.error("[heartbeat] could not load wallet hotkey: %s", e)
        return 2

    with make_subtensor(args.network) as subtensor:
        result = run_heartbeat(
            subtensor=subtensor,
            validator_wallet=wallet,
            netuid=args.netuid,
            threshold_blocks=args.threshold_blocks,
            dry_run=args.dry_run,
        )

    # Exit code: 0 for any successful outcome (including skips), non-zero
    # only when the extrinsic submission failed.
    return 1 if result.action == "failed" else 0


if __name__ == "__main__":
    raise SystemExit(main())
