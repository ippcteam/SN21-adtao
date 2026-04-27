"""Weight Setter — normalize miner scores and set weights on-chain.

After scoring an epoch, converts miner scores to normalized weights
and publishes them to the Bittensor network via subtensor.set_weights().
"""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)

# EMA smoothing factor — how much previous weights influence new weights
EMA_ALPHA = 0.1


class WeightSetter:
    """Compute normalized weights from miner scores and set on-chain."""

    def __init__(self):
        self.previous_weights: dict[int, float] = {}

    def normalize_scores(
        self,
        scores: dict[str, float],
        uid_map: dict[str, int],
    ) -> tuple[list[int], list[float]]:
        """Convert miner_id → score to (uids, weights) for set_weights.

        Args:
            scores: miner_hotkey → final_score
            uid_map: miner_hotkey → uid on the metagraph

        Returns:
            (uids, weights) tuple ready for subtensor.set_weights()
        """
        uids = []
        raw_weights = []

        for hotkey, score in scores.items():
            uid = uid_map.get(hotkey)
            if uid is None:
                logger.warning(f"Hotkey {hotkey[:16]}... not found in metagraph, skipping")
                continue
            uids.append(uid)
            raw_weights.append(max(score, 0.0))

        if not uids:
            return [], []

        # Normalize to sum to 1.0
        total = sum(raw_weights)
        if total > 0:
            weights = [w / total for w in raw_weights]
        else:
            # All zeros — distribute equally
            weights = [1.0 / len(uids)] * len(uids)

        # Apply EMA smoothing against previous weights
        if self.previous_weights:
            smoothed = []
            for uid, w in zip(uids, weights):
                prev = self.previous_weights.get(uid, w)
                smoothed_w = EMA_ALPHA * w + (1 - EMA_ALPHA) * prev
                smoothed.append(smoothed_w)
            weights = smoothed

            # Re-normalize after smoothing
            total = sum(weights)
            if total > 0:
                weights = [w / total for w in weights]

        # Store for next round
        self.previous_weights = dict(zip(uids, weights))

        logger.info(f"Normalized {len(uids)} miners: max={max(weights):.4f} min={min(weights):.4f}")
        return uids, weights

    async def set_weights(
        self,
        subtensor,
        wallet,
        netuid: int,
        uids: list[int],
        weights: list[float],
    ) -> bool:
        """Set weights on-chain via subtensor.

        Args:
            subtensor: Bittensor subtensor instance
            wallet: Bittensor wallet (validator)
            netuid: Subnet network UID
            uids: List of miner UIDs
            weights: Normalized weights (sum to 1.0)

        Returns:
            True if weights were set successfully
        """
        if not uids:
            logger.warning("No UIDs to set weights for")
            return False

        try:
            # Convert to numpy arrays as required by bittensor
            uid_array = np.array(uids, dtype=np.int64)
            weight_array = np.array(weights, dtype=np.float32)

            logger.info(f"Setting weights for {len(uids)} miners on netuid {netuid}")

            result = subtensor.set_weights(
                wallet=wallet,
                netuid=netuid,
                uids=uid_array,
                weights=weight_array,
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
