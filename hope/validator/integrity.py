"""Integrity module — cryptographic proofs for the data pipeline.

Ensures every link in the chain is verifiable:

1. Episode commitment: SHA256 of episode bundle, published before miners see data
2. Prediction receipts: Miner signs prediction, validator returns signed receipt
3. Prediction Merkle tree: Root published BEFORE outcomes are fetched
4. Outcome verification: HOPE signs outcomes, validator verifies signature
5. Scoring commitment: Deterministic scoring output hash

The principle: an external observer with no trust in HOPE, the validator,
or any miner can independently verify correctness at every step.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class EpisodeCommitment:
    """Published before miners receive episode data."""

    episode_hash: str          # SHA256 of sorted episode bundle
    episode_count: int
    committed_at: str          # ISO timestamp
    epoch_id: str


@dataclass
class PredictionReceipt:
    """Returned to miner after prediction accepted."""

    miner_hotkey: str
    episode_id: str
    prediction_hash: str       # SHA256 of the prediction payload
    received_at: str           # ISO timestamp
    receipt_hash: str          # SHA256(miner_hotkey + prediction_hash + received_at)


@dataclass
class PredictionMerkleTree:
    """Merkle tree over all accepted predictions. Root published before scoring."""

    root: str
    leaf_count: int
    leaves: dict[str, str] = field(default_factory=dict)  # (hotkey:episode_id) -> hash


@dataclass
class IntegrityProof:
    """Complete integrity proof for an epoch."""

    episode_commitment: EpisodeCommitment | None = None
    prediction_merkle: PredictionMerkleTree | None = None
    scoring_hash: str = ""     # SHA256 of deterministic scoring output


def compute_episode_commitment(
    episodes: list,
    epoch_id: str,
    timestamp: str,
) -> EpisodeCommitment:
    """Compute a commitment hash over the episode bundle.

    This hash is published BEFORE miners receive episodes, proving
    episodes were not modified after distribution.
    """
    # Serialize episodes deterministically
    episode_dicts = []
    for ep in episodes:
        if hasattr(ep, "model_dump"):
            episode_dicts.append(ep.model_dump(mode="json"))
        elif isinstance(ep, dict):
            episode_dicts.append(ep)
        else:
            episode_dicts.append(str(ep))

    episode_json = json.dumps(episode_dicts, sort_keys=True, default=str)
    episode_hash = hashlib.sha256(episode_json.encode()).hexdigest()

    return EpisodeCommitment(
        episode_hash=episode_hash,
        episode_count=len(episodes),
        committed_at=timestamp,
        epoch_id=epoch_id,
    )


def compute_prediction_hash(prediction_payload: dict | str) -> str:
    """Compute SHA256 hash of a prediction payload."""
    if isinstance(prediction_payload, dict):
        payload_str = json.dumps(prediction_payload, sort_keys=True, default=str)
    else:
        payload_str = str(prediction_payload)
    return hashlib.sha256(payload_str.encode()).hexdigest()


def create_prediction_receipt(
    miner_hotkey: str,
    episode_id: str,
    prediction_hash: str,
    received_at: str,
) -> PredictionReceipt:
    """Create an integrity receipt for a submitted prediction."""
    receipt_input = f"{miner_hotkey}:{prediction_hash}:{received_at}"
    receipt_hash = hashlib.sha256(receipt_input.encode()).hexdigest()

    return PredictionReceipt(
        miner_hotkey=miner_hotkey,
        episode_id=episode_id,
        prediction_hash=prediction_hash,
        received_at=received_at,
        receipt_hash=receipt_hash,
    )


def build_prediction_merkle_tree(
    predictions: dict[str, dict],
) -> PredictionMerkleTree:
    """Build a Merkle tree over all accepted predictions.

    predictions: {hotkey: {episode_id: Prediction, ...}, ...}
    """
    leaves: dict[str, str] = {}

    for hotkey, miner_preds in sorted(predictions.items()):
        if isinstance(miner_preds, dict):
            pred_items = sorted(miner_preds.items())
        elif isinstance(miner_preds, list):
            pred_items = [(p.episode_id, p) for p in miner_preds]
        else:
            continue

        for episode_id, pred in pred_items:
            if hasattr(pred, "model_dump"):
                pred_json = json.dumps(pred.model_dump(mode="json"), sort_keys=True, default=str)
            elif isinstance(pred, dict):
                pred_json = json.dumps(pred, sort_keys=True, default=str)
            else:
                pred_json = str(pred)

            leaf_key = f"{hotkey}:{episode_id}"
            leaves[leaf_key] = hashlib.sha256(pred_json.encode()).hexdigest()

    if not leaves:
        empty_root = hashlib.sha256(b"empty_predictions").hexdigest()
        return PredictionMerkleTree(root=empty_root, leaf_count=0, leaves={})

    # Build Merkle tree
    leaf_hashes = [leaves[k] for k in sorted(leaves.keys())]
    root = _merkle_root(leaf_hashes)

    return PredictionMerkleTree(root=root, leaf_count=len(leaves), leaves=leaves)


def compute_merkle_inclusion_proof(
    tree: PredictionMerkleTree,
    hotkey: str,
    episode_id: str,
) -> dict | None:
    """Generate a full Merkle inclusion proof for a specific prediction.

    Returns the sibling hashes along the path from leaf to root,
    allowing independent verification that the leaf is in the tree.
    """
    leaf_key = f"{hotkey}:{episode_id}"
    if leaf_key not in tree.leaves:
        return None

    # Reconstruct the tree to find the path
    sorted_keys = sorted(tree.leaves.keys())
    leaf_index = sorted_keys.index(leaf_key)
    leaf_hashes = [tree.leaves[k] for k in sorted_keys]

    # Build proof: collect sibling at each level
    proof_siblings = []
    index = leaf_index
    level = list(leaf_hashes)

    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])

        # Sibling is the other node in the pair
        if index % 2 == 0:
            sibling = level[index + 1] if index + 1 < len(level) else level[index]
            proof_siblings.append({"position": "right", "hash": sibling})
        else:
            sibling = level[index - 1]
            proof_siblings.append({"position": "left", "hash": sibling})

        # Move to next level
        next_level = []
        for i in range(0, len(level), 2):
            combined = level[i] + level[i + 1]
            next_level.append(hashlib.sha256(combined.encode()).hexdigest())
        level = next_level
        index = index // 2

    return {
        "leaf_key": leaf_key,
        "leaf_hash": tree.leaves[leaf_key],
        "leaf_index": leaf_index,
        "merkle_root": tree.root,
        "total_predictions": tree.leaf_count,
        "proof": proof_siblings,
        "verification_instructions": (
            "To verify: start with leaf_hash. For each sibling in proof, "
            "if position='left', compute SHA256(sibling.hash + current), "
            "if position='right', compute SHA256(current + sibling.hash). "
            "Final result must equal merkle_root."
        ),
    }


def _merkle_root(hashes: list[str]) -> str:
    """Compute Merkle root from a list of leaf hashes."""
    if not hashes:
        return hashlib.sha256(b"empty").hexdigest()

    level = list(hashes)
    while len(level) > 1:
        if len(level) % 2 == 1:
            level.append(level[-1])
        next_level = []
        for i in range(0, len(level), 2):
            combined = level[i] + level[i + 1]
            next_level.append(hashlib.sha256(combined.encode()).hexdigest())
        level = next_level

    return level[0]


def compute_scoring_hash(scores: dict) -> str:
    """Deterministic hash of scoring output for verification."""
    score_data = {}
    for miner_id, score in sorted(scores.items()):
        if hasattr(score, "final_score"):
            score_data[miner_id] = {
                "raw_score": score.raw_score,
                "final_score": score.final_score,
                "episodes_scored": score.episodes_scored,
            }
        elif isinstance(score, dict):
            score_data[miner_id] = score

    score_json = json.dumps(score_data, sort_keys=True, default=str)
    return hashlib.sha256(score_json.encode()).hexdigest()
