"""Weight Setter — normalize miner scores, apply burn, and set weights on-chain.

After scoring an epoch, converts miner scores to normalized weights,
applies burn rate (weight to UID 0), and publishes to the Bittensor
network via subtensor.set_weights().

Burn rate explained:
- Burn assigns a fraction of total weight to UID 0 (the validator/subnet owner)
- This reduces miner emissions proportionally
- High burn (95%) at launch = low miner incentive = fewer exploiters
- Lower burn gradually as the system proves stable
- Implemented per Tensora guidance (not a protocol parameter)
"""

from __future__ import annotations

import logging

import numpy as np

logger = logging.getLogger(__name__)

# EMA smoothing factor — how much previous weights influence new weights
EMA_ALPHA = 0.1

# Default burn rate for launch (95% per Tensora recommendation)
DEFAULT_BURN_FRACTION = 0.95


class WeightSetter:
    """Compute normalized weights from miner scores and set on-chain."""

    def __init__(self, burn_fraction: float = DEFAULT_BURN_FRACTION):
        self.burn_fraction = burn_fraction
        self.previous_weights: dict[int, float] = {}
        # Track hotkey→uid mapping to detect deregistrations
        self._hotkey_at_uid: dict[int, str] = {}

    def apply_burn(self, weights: dict[int, float]) -> dict[int, float]:
        """Apply burn rate by assigning burn_fraction of weight to UID 0.

        Per Tensora/Jack: burn is implemented by setting a percentage of
        weight to UID 0 (subnet owner). The remaining weight is split
        among miners proportionally.

        Args:
            weights: uid → weight (before burn)

        Returns:
            uid → weight (with UID 0 burn applied)
        """
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

            # Detect deregistration: if the hotkey at this UID changed,
            # reset the EMA for this UID (per Tensora: new miner should
            # not inherit a deregistered miner's score)
            prev_hotkey = self._hotkey_at_uid.get(uid)
            if prev_hotkey and prev_hotkey != hotkey:
                logger.info(f"UID {uid} hotkey changed ({prev_hotkey[:12]}→{hotkey[:12]}), resetting EMA")
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

        # Apply EMA smoothing against previous weights
        # Non-submitters (score=0) stay at hard zero
        # Deregistered UIDs already had their EMA cleared above
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
                        smoothed_w = EMA_ALPHA * w + (1 - EMA_ALPHA) * prev
                        smoothed.append(smoothed_w)
            weights = smoothed

            # Re-normalize
            total = sum(weights)
            if total > 0:
                weights = [w / total for w in weights]

        # Store for next round
        self.previous_weights = dict(zip(uids, weights))

        # Apply burn — assigns burn_fraction to UID 0
        pre_burn = dict(zip(uids, weights))
        post_burn = self.apply_burn(pre_burn)

        # Convert back to sorted lists
        final_uids = sorted(post_burn.keys())
        final_weights = [post_burn[uid] for uid in final_uids]

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
        """Set weights on-chain using commit-reveal protocol.

        Per Tensora: use commit_weights/reveal_weights so the commitment
        hash is public and no other validator can influence it.

        Flow:
        1. commit_weights — publishes hash of weights on-chain
        2. reveal_weights — reveals actual weights after delay

        Falls back to set_weights if commit_reveal is not enabled.
        """
        if not uids:
            logger.warning("No UIDs to set weights for")
            return False

        try:
            uid_array = np.array(uids, dtype=np.int64)
            weight_array = np.array(weights, dtype=np.float32)

            # Generate salt for commit-reveal
            import secrets
            salt = [secrets.randbelow(2**16) for _ in range(len(uids))]
            salt_array = np.array(salt, dtype=np.int64)

            logger.info(f"Setting weights for {len(uids)} UIDs on netuid {netuid}")

            # Try commit-reveal first (preferred per Tensora)
            try:
                cr_enabled = subtensor.commit_reveal_enabled(netuid=netuid)
            except Exception:
                cr_enabled = False

            if cr_enabled:
                logger.info("Using commit-reveal protocol for weight setting")

                # Step 1: Commit
                commit_result = subtensor.commit_weights(
                    wallet=wallet,
                    netuid=netuid,
                    salt=salt_array,
                    uids=uid_array,
                    weights=weight_array,
                    wait_for_inclusion=True,
                    wait_for_finalization=False,
                )

                if commit_result:
                    logger.info("Weights committed on-chain (hash public)")
                else:
                    logger.error("Failed to commit weights")
                    return False

                # Step 2: Reveal (handled automatically by commit_weights
                # when wait_for_revealed_execution=True, which is the default)
                logger.info("Weights committed and will be revealed automatically")
                return True

            else:
                # Fallback: direct set_weights (for testnet or when CR not enabled)
                logger.info("Commit-reveal not enabled, using direct set_weights")
                result = subtensor.set_weights(
                    wallet=wallet,
                    netuid=netuid,
                    uids=uid_array,
                    weights=weight_array,
                    wait_for_inclusion=True,
                    wait_for_finalization=False,
                )

                if result:
                    logger.info("Weights set successfully on-chain (direct)")
                else:
                    logger.error("Failed to set weights on-chain")

                return bool(result)

        except Exception as e:
            logger.error(f"Error setting weights: {e}")
            return False
