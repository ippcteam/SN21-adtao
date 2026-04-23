"""Bittensor Synapse definitions for HOPE SN21.

Synapses are lightweight signaling messages sent over the Bittensor network.
Actual data (episodes, predictions) moves over HTTP — episodes are 15-70KB
each, too large for Synapse transport.

Three Synapse types:
- EpochAnnouncement: Validator → Miner, new epoch available
- Heartbeat: Validator → Miner, connectivity check
- CommitmentReveal: Validator → Miner, epoch outcomes revealed
"""

from __future__ import annotations

from typing import Optional

import bittensor as bt


class EpochAnnouncement(bt.Synapse):
    """Validator announces a new epoch to miners.

    Sent at the start of each epoch. Miners use the api_endpoint
    to fetch episodes via HTTP.
    """

    epoch_id: str                  # e.g. "WR-2026-W17-PUB-E1"
    episode_count: int             # Number of episodes in this epoch
    schema_version: str = "v1.9"   # Episode payload schema version
    commitment_root: str           # Merkle root hex of outcome commitments
    deadline: str                  # ISO 8601 prediction submission deadline
    api_endpoint: str              # HTTP URL for episode fetching


class Heartbeat(bt.Synapse):
    """Validator checks miner connectivity and readiness.

    Sent periodically. Miner fills in its status fields.
    """

    validator_version: str
    # Miner fills these:
    miner_version: Optional[str] = None
    miner_status: Optional[str] = None   # "ready" | "busy" | "error"
    episodes_processed: Optional[int] = None


class CommitmentReveal(bt.Synapse):
    """Validator reveals epoch outcomes after scoring deadline.

    Miners can use the revealed salt and outcomes_url to verify
    that outcomes match the commitment_root from EpochAnnouncement.
    """

    epoch_id: str
    epoch_salt: str                # Random salt used in commitment
    outcomes_url: str              # HTTP URL to download revealed outcomes
    weights_json: str              # Revealed scoring weight vector (JSON)
    scores_url: Optional[str] = None  # HTTP URL for per-miner scores
