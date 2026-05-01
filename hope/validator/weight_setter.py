"""Weight Setter — normalize miner scores, apply burn, and set weights on-chain.

After scoring an epoch, converts miner scores to normalized weights,
applies burn rate (weight to UID 0), and publishes to the Bittensor
network via subtensor.set_weights().

Burn rate explained:
- Burn assigns a fraction of total weight to UID 0 (the validator/subnet owner)
- This reduces miner emissions proportionally
- High burn (95%) at launch = low miner incentive = fewer exploiters
- Lower burn gradually as the system proves stable
- Implemented as a subnet-level parameter (not a protocol parameter)
"""

from __future__ import annotations

import logging


logger = logging.getLogger(__name__)

# EMA smoothing factor — how much previous weights influence new weights
EMA_ALPHA = 0.1

# Default burn rate for launch (95%)
DEFAULT_BURN_FRACTION = 0.95


class WeightSetter:
    """Compute normalized weights from miner scores and set on-chain."""

    def __init__(self, burn_fraction: float = DEFAULT_BURN_FRACTION):
        self.burn_fraction = burn_fraction
        self.previous_weights: dict[int, float] = {}
        self._hotkey_at_uid: dict[int, str] = {}

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
        total = sum(raw_weights)
        if total > 0:
            weights = [w / total for w in raw_weights]
        else:
            weights = [1.0 / len(uids)] * len(uids)

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
