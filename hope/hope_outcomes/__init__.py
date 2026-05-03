"""HOPE outcome signer — Layer 9.A commit-reveal modules.

- release_commit: T=0 release_commit_digest builder.
- reveal_blob: post-deadline reveal blob with chain-anchored SHA-256.
"""

from hope.hope_outcomes.release_commit import (
    RELEASE_COMMIT_VERSION,
    EpisodeRef,
    build_release_commit_plaintext,
    compute_episodes_root,
    compute_release_commit_digest,
)
from hope.hope_outcomes.reveal_blob import (
    REVEAL_BLOB_VERSION,
    EpisodeOutcome,
    HorizonOutcomeMeasured,
    build_reveal_blob,
    compute_reveal_blob_sha256,
    verify_reveal_blob,
)

__all__ = [
    "RELEASE_COMMIT_VERSION",
    "EpisodeRef",
    "build_release_commit_plaintext",
    "compute_episodes_root",
    "compute_release_commit_digest",
    "REVEAL_BLOB_VERSION",
    "EpisodeOutcome",
    "HorizonOutcomeMeasured",
    "build_reveal_blob",
    "compute_reveal_blob_sha256",
    "verify_reveal_blob",
]
