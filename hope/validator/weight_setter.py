"""Weight Setter — normalize miner scores, apply burn, and set weights on-chain.

After scoring an epoch, converts miner scores to normalized weights,
applies burn rate (weight to UID 0), and publishes to the Bittensor
network via subtensor.set_weights().

Two allocation paths are supported:

* **Simple normalization (default).** Linear normalize raw scores,
  apply EMA smoothing across consecutive epochs, then apply burn.
  This is the launch fallback path that runs whether or not the
  tiered reward mechanism is wired in.
* **Tiered allocation.** When ``tiered_allocator`` is supplied,
  per-miner inputs (raw_score + baseline + coverage + per-bucket
  coverage) are passed to the
  :class:`hope.validator.tiered_weights.TieredAllocator` which
  applies the participation gate, four-epoch EMA tier placement,
  Elite/Competitive/Participating pool shares, and Elite-floor
  redistribution from the reward spec. Burn is applied on top of the
  tier weights.

Burn rate explained:
- Burn assigns a fraction of total weight to UID 0 (the validator/subnet owner)
- This reduces miner emissions proportionally
- High burn (95%) at launch = low miner incentive = fewer exploiters
- Lower burn gradually as the system proves stable
- Implemented as a subnet-level parameter (not a protocol parameter)
"""

from __future__ import annotations

import logging

from hope.validator.tiered_weights import (
    MinerEpochInputs,
    TierAllocationResult,
    TieredAllocator,
)

logger = logging.getLogger(__name__)

# EMA smoothing factor — how much previous weights influence new weights
EMA_ALPHA = 0.1

# Default burn rate for launch (95%)
DEFAULT_BURN_FRACTION = 0.95


class WeightSetter:
    """Compute normalized weights from miner scores and set on-chain."""

    def __init__(
        self,
        burn_fraction: float = DEFAULT_BURN_FRACTION,
        tiered_allocator: TieredAllocator | None = None,
    ):
        self.burn_fraction = burn_fraction
        self.previous_weights: dict[int, float] = {}
        self._hotkey_at_uid: dict[int, str] = {}
        self.tiered_allocator = tiered_allocator

    def apply_burn(self, weights: dict[int, float]) -> dict[int, float]:
        """Apply burn rate by assigning burn_fraction of weight to UID 0."""
        if not weights or self.burn_fraction >= 1.0:
            return {0: 1.0}

        miner_fraction = 1.0 - self.burn_fraction
        total = sum(weights.values())

        if total < 1e-12:
            return {0: 1.0}

        scale = miner_fraction / total
        result: dict[int, float] = {0: self.burn_fraction}

        for uid, w in weights.items():
            if uid == 0:
                continue
            scaled = w * scale
            if scaled > 1e-12:
                result[uid] = scaled

        return result

    def normalize_scores(
        self,
        scores: dict[str, float],
        uid_map: dict[str, int],
    ) -> tuple[list[int], list[float]]:
        """Convert miner_id → score to (uids, weights) for set_weights."""
        uids = []
        raw_weights = []

        for hotkey, score in scores.items():
            uid = uid_map.get(hotkey)
            if uid is None:
                logger.warning(f"Hotkey {hotkey[:16]}... not found in metagraph, skipping")
                continue
            uids.append(uid)
            raw_weights.append(max(score, 0.0))

            # Detect deregistration: reset EMA if hotkey changed at this UID
            prev_hotkey = self._hotkey_at_uid.get(uid)
            if prev_hotkey and prev_hotkey != hotkey:
                logger.info(f"UID {uid} hotkey changed, resetting EMA")
                self.previous_weights.pop(uid, None)
            self._hotkey_at_uid[uid] = hotkey

        if not uids:
            return [], []

        # Normalize to sum to 1.0
        # If all scores are zero, assign zero weight to all miners (burn gets 100%)
        # This prevents Sybil clusters from splitting emissions with junk submissions
        total = sum(raw_weights)
        if total > 0:
            weights = [w / total for w in raw_weights]
        else:
            logger.warning("All miner scores are zero — assigning zero weights (full burn)")
            return [0], [1.0]  # Only UID 0 (burn) gets weight

        # Apply EMA smoothing (non-submitters stay at zero)
        if self.previous_weights:
            smoothed = []
            for uid, w in zip(uids, weights):
                if w == 0.0:
                    smoothed.append(0.0)
                else:
                    prev = self.previous_weights.get(uid, w)
                    if prev == 0.0:
                        smoothed.append(w)
                    else:
                        smoothed.append(EMA_ALPHA * w + (1 - EMA_ALPHA) * prev)
            weights = smoothed

            total = sum(weights)
            if total > 0:
                weights = [w / total for w in weights]

        self.previous_weights = dict(zip(uids, weights))

        # Apply burn
        pre_burn = dict(zip(uids, weights))
        post_burn = self.apply_burn(pre_burn)

        # Return as Python native lists (avoid numpy types for subtensor compatibility)
        final_uids = [int(u) for u in sorted(post_burn.keys())]
        final_weights = [float(post_burn[uid]) for uid in final_uids]

        active_miners = sum(1 for u in final_uids if u != 0 and post_burn[u] > 0)
        logger.info(
            f"Weights: {active_miners} active miners, "
            f"burn={self.burn_fraction:.0%} (UID 0 = {post_burn.get(0, 0):.4f})"
        )

        return final_uids, final_weights

    @staticmethod
    def derive_u16_weights(
        scored: dict[int, float],
        burn_fraction: float,
    ) -> dict[int, int]:
        """Map a UID → score map to UID → u16 weight, with burn applied.

        The 9.C.2 verifier mirrors the on-chain weights commit by recomputing
        this mapping from the scoring artifact's score table. ``scored``
        carries pre-normalisation raw or post-tier scores; the result is the
        u16 quantisation Subtensor stores.
        """
        if not scored:
            return {0: 65_535}
        miner_total = sum(max(s, 0.0) for s in scored.values())
        if miner_total <= 0 or burn_fraction >= 1.0:
            return {0: 65_535}

        miner_share = 1.0 - burn_fraction
        normalised: dict[int, float] = {0: burn_fraction}
        for uid, score in scored.items():
            if uid == 0 or score <= 0:
                continue
            normalised[uid] = miner_share * (score / miner_total)

        u16: dict[int, int] = {}
        for uid, w in normalised.items():
            value = round(w * 65_535)
            if value > 0:
                u16[uid] = min(65_535, value)
        return u16

    def allocate_tiered(
        self,
        miner_inputs: list[MinerEpochInputs],
        uid_map: dict[str, int],
    ) -> tuple[list[int], list[float], TierAllocationResult]:
        """Run the tiered allocation path and apply burn.

        Returns ``(uids, weights, allocation_result)``. ``allocation_result``
        carries the tier breakdown so callers can log which miners landed
        where and emit the excluded-miners list for the 9.C.1 commit.
        """
        if self.tiered_allocator is None:
            raise ValueError(
                "WeightSetter has no tiered_allocator configured; "
                "instantiate with TieredAllocator() to use this path"
            )

        result = self.tiered_allocator.allocate(miner_inputs)

        if not result.weights:
            logger.warning(
                "Tiered allocator returned no qualifying miners "
                "(excluded=%d) — assigning full weight to burn UID",
                len(result.excluded),
            )
            return [0], [1.0], result

        pre_burn: dict[int, float] = {}
        for hotkey, weight in result.weights.items():
            uid = uid_map.get(hotkey)
            if uid is None:
                logger.warning("Hotkey %s... not in metagraph; skipping", hotkey[:16])
                continue
            pre_burn[uid] = pre_burn.get(uid, 0.0) + weight
            self._hotkey_at_uid[uid] = hotkey

        if not pre_burn:
            return [0], [1.0], result

        post_burn = self.apply_burn(pre_burn)
        final_uids = sorted(post_burn.keys())
        final_weights = [float(post_burn[uid]) for uid in final_uids]

        logger.info(
            "Tiered weights: elite=%d competitive=%d participating=%d excluded=%d "
            "burn=%.0f%% (UID 0=%.4f)",
            len(result.elite),
            len(result.competitive),
            len(result.participating),
            len(result.excluded),
            self.burn_fraction * 100,
            post_burn.get(0, 0.0),
        )
        return final_uids, final_weights, result

    async def set_weights(
        self,
        subtensor,
        wallet,
        netuid: int,
        uids: list[int],
        weights: list[float],
    ) -> bool:
        """Set weights on-chain.

        Uses subtensor.set_weights() which automatically handles
        commit-reveal if enabled on the subnet. Python native lists
        are used (not numpy) for subtensor compatibility.
        """
        if not uids:
            logger.warning("No UIDs to set weights for")
            return False

        try:
            logger.info(f"Setting weights for {len(uids)} UIDs on netuid {netuid}")

            result = subtensor.set_weights(
                wallet=wallet,
                netuid=int(netuid),
                uids=uids,
                weights=weights,
                wait_for_inclusion=True,
                wait_for_finalization=False,
            )

            if result:
                logger.info("Weights set successfully on-chain")
            else:
                logger.error("Failed to set weights on-chain")

            return bool(result)

        except Exception as e:
            logger.error(f"Error setting weights: {e}")
            return False
