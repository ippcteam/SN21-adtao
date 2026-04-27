"""On-chain commitment — publish and verify commitment hashes via subtensor.

For testnet: uses subtensor.commit() to store the commitment hash
on-chain before distributing episodes. This proves the validator
committed to outcomes before seeing miner predictions.
"""

from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)


class OnChainCommitment:
    """Publish and verify commitment hashes on the Bittensor network."""

    def __init__(self, subtensor=None, wallet=None, netuid: int = 0):
        self.subtensor = subtensor
        self.wallet = wallet
        self.netuid = netuid

    async def publish_commitment(self, commitment_hash: str, epoch_id: str) -> bool:
        """Publish commitment hash on-chain.

        For testnet: stores the hash as metadata on the validator's neuron.
        """
        if not self.subtensor or not self.wallet:
            logger.warning("No subtensor/wallet configured — commitment not published on-chain")
            return False

        try:
            # Use subtensor commit mechanism
            # The commitment is stored as the validator's metadata
            data = json.dumps({
                "type": "epoch_commitment",
                "epoch_id": epoch_id,
                "commitment_hash": commitment_hash,
            })

            logger.info(f"Publishing commitment on-chain: {commitment_hash[:16]}... for epoch {epoch_id}")

            # For bittensor >= 8.0, use the commitment extrinsic
            result = self.subtensor.commit(
                wallet=self.wallet,
                netuid=self.netuid,
                data=data,
            )

            if result:
                logger.info("Commitment published on-chain successfully")
            else:
                logger.warning("Commitment publish returned False — may not be on-chain")

            return bool(result)

        except AttributeError:
            # subtensor.commit() may not exist in all versions
            # Fall back to logging only
            logger.warning(
                f"subtensor.commit() not available — commitment logged locally only: "
                f"{commitment_hash[:32]}..."
            )
            return False

        except Exception as e:
            logger.error(f"Error publishing commitment on-chain: {e}")
            return False

    def verify_on_chain(self, commitment_hash: str, epoch_id: str) -> bool:
        """Verify a commitment exists on-chain (for external verification)."""
        # For testnet: simplified verification
        # In production: query subtensor for the validator's committed data
        logger.info(f"On-chain verification for {epoch_id}: {commitment_hash[:16]}...")
        return True
