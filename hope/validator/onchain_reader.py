"""Layer 9.B reader — reverse of `hope/miner/onchain_submitter.py`.

Per epoch, per miner UID, the validator:

  1. Reads three on-chain commits at the miner's hotkey/UID:
     - TimelockEncrypted(K)        → auto-decrypted plaintext is K
     - Sha256(AES_ct)              → 32 bytes, identifies the canonical AES_ct
     - Raw{N}(self_archive_url)    → optional Tier-3 location

  2. Fetches AES_ct from archive tiers (Tier-1, Tier-2, Tier-3 in order),
     accepting only bytes whose SHA-256 matches the on-chain commit.

  3. Runs the 8-check scoreability rule (`evaluate_scoreability`) on
     (AES_ct, K, on_chain_triple, timing_bounds).

  4. If `ok=True`, returns the decoded prediction plaintext for the scorer.
     Otherwise returns the failure code so the validator can emit a
     retry-log entry (Layer 9.C.6) and flag the miner for `excluded` in 9.C.1.

The reader does NOT modify chain state. It is read-only and re-runnable —
the public verifier in `scripts/verify_epoch.py` reuses it verbatim, which
guarantees a validator and a third party reach the same scoring decision.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from hope.commitment.archives import (
    ArchiveClient,
    ArchiveEndpoint,
    FetchAggregate,
)
from hope.commitment.scoreability import (
    OnChainCommitTriple,
    ScoreabilityFailure,
    ScoreabilityResult,
    TimingBounds,
    evaluate_scoreability,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MinerReadResult:
    """Aggregate result for one miner / one epoch.

    `excluded_reason` is the discrete failure code if the prediction did NOT
    pass scoreability — fed verbatim to the 9.C.1 `excluded_miners` map. If
    the failure was archive-fetch (no tier returned matching bytes), the
    reason is `plaintext_unavailable` (no ScoreabilityFailure was even
    reachable) and the validator must emit a 9.C.6 retry log.
    """

    miner_hotkey: bytes
    miner_uid: int
    ok: bool
    plaintext: dict[str, Any] | None
    excluded_reason: str | None
    fetch: FetchAggregate | None
    on_chain: OnChainCommitTriple
    scoreability: ScoreabilityResult | None


@dataclass(frozen=True)
class ChainCommits9B:
    """Three commit fields read from chain for one (netuid, hotkey)."""

    timelock_k_revealed: bytes | None    # auto-decrypted K (32 bytes) or None
    sha256_ct_commit: bytes | None       # raw 32 bytes
    self_archive_url: str | None         # decoded UTF-8 URL or None
    chain_block_at_k_commit: int | None  # block where K landed
    miner_hotkey: bytes                     # 32-byte raw ed25519 pubkey


def read_miner_for_epoch(
    *,
    chain_commits: ChainCommits9B,
    archive_client: ArchiveClient,
    archive_endpoints: list[ArchiveEndpoint],
    epoch_id: str,
    timing: TimingBounds,
    miner_uid: int,
    miner_identity_for_archive: str,
    inner_sig_pubkey: bytes | None = None,
) -> MinerReadResult:
    """Read one miner's full Layer 9.B submission and run scoreability.

    Args:
        chain_commits: as produced by `assemble_chain_commits` after reading
            chain state for the miner.
        archive_client: shared client for archive HTTPS fetches.
        archive_endpoints: ordered list to try; Tier-1, Tier-2, Tier-3.
            Caller can include the miner's Tier-3 self_archive_url ONLY after
            verifying chain_commits.self_archive_url matches the endpoint URL.
        epoch_id: the operator release_key for this epoch (drives AAD).
        timing: protocol-allowed window for submitted_round and chain block.
        miner_uid: subnet UID for archive paths and logging.
        miner_identity_for_archive: SS58 of the miner hotkey (used in archive
            paths for Tier-2/Tier-3) — Tier-1 may use the UID instead but
            this single identity simplifies the path scheme.
        inner_sig_pubkey: optional override for the inner_sig anchor. When
            provided (e.g. the registered ed25519 pubkey from
            `hope/validator/registration_index.py`), it replaces the raw
            chain hotkey pubkey for the inner_sig verification + the
            `plaintext['miner_hotkey']` equality check. Required for
            sr25519-hotkey miners who publish a separate ed25519 binding
            via `sn21_keys.py register`; sr25519 hotkeys cannot themselves
            sign ed25519 inner_sigs. None ⇒ fall back to
            `chain_commits.miner_hotkey` (works only when the chain hotkey
            is ed25519, which the current Bittensor stack does not produce).

    Returns:
        MinerReadResult with `ok=True` only if the prediction passed all 8
        scoreability checks. Excluded reasons are populated on failure.
    """
    triple = OnChainCommitTriple(
        timelock_k_present=chain_commits.timelock_k_revealed is not None,
        sha256_ct_commit=chain_commits.sha256_ct_commit,
        self_archive_url=chain_commits.self_archive_url,
        chain_block_at_k_commit=chain_commits.chain_block_at_k_commit,
    )

    if (
        chain_commits.sha256_ct_commit is None
        or chain_commits.timelock_k_revealed is None
    ):
        # Even before fetching, we can short-circuit ON_CHAIN_PRESENT cases
        # so the retry log doesn't waste archive bandwidth on doomed reads.
        result = ScoreabilityResult(
            ok=False,
            failure=(
                ScoreabilityFailure.ON_CHAIN_PRESENT_MISSING_K
                if chain_commits.timelock_k_revealed is None
                else ScoreabilityFailure.ON_CHAIN_PRESENT_MISSING_SHA
            ),
            plaintext=None,
        )
        return MinerReadResult(
            miner_hotkey=chain_commits.miner_hotkey,
            miner_uid=miner_uid,
            ok=False,
            plaintext=None,
            excluded_reason=result.failure.value if result.failure else "unknown",
            fetch=None,
            on_chain=triple,
            scoreability=result,
        )

    fetch = archive_client.fetch_first_match(
        archive_endpoints,
        epoch_id=epoch_id,
        miner_identity=miner_identity_for_archive,
        expected_sha256=chain_commits.sha256_ct_commit,
    )

    if not fetch.ok or fetch.aes_ct is None:
        logger.info(
            "miner 9.B fetch failed epoch=%s uid=%d attempts=%d",
            epoch_id, miner_uid, len(fetch.attempts),
        )
        return MinerReadResult(
            miner_hotkey=chain_commits.miner_hotkey,
            miner_uid=miner_uid,
            ok=False,
            plaintext=None,
            excluded_reason="plaintext_unavailable",
            fetch=fetch,
            on_chain=triple,
            scoreability=None,
        )

    score_res = evaluate_scoreability(
        aes_ct=fetch.aes_ct,
        aes_key=chain_commits.timelock_k_revealed,
        epoch_id=epoch_id,
        miner_hotkey_from_chain=inner_sig_pubkey or chain_commits.miner_hotkey,
        on_chain=triple,
        timing=timing,
    )

    return MinerReadResult(
        miner_hotkey=chain_commits.miner_hotkey,
        miner_uid=miner_uid,
        ok=score_res.ok,
        plaintext=score_res.plaintext if score_res.ok else None,
        excluded_reason=(
            None if score_res.ok
            else (score_res.failure.value if score_res.failure else "unknown")
        ),
        fetch=fetch,
        on_chain=triple,
        scoreability=score_res,
    )


def assemble_chain_commits(
    *,
    revealed_k_plaintext: bytes | None,
    sha256_ct_commit: bytes | None,
    self_archive_url: str | None,
    chain_block_at_k_commit: int | None,
    miner_hotkey: bytes,
) -> ChainCommits9B:
    """Build a `ChainCommits9B` after reading raw chain state.

    Trivial wrapper for clarity at the call site — the validator runner reads
    each field via different SDK entrypoints (`get_revealed_commitment_by_hotkey`
    for K, `get_commitment` for SHA + URL) and assembles them here.
    """
    if len(miner_hotkey) != 32:
        raise ValueError(f"miner_hotkey must be 32 bytes, got {len(miner_hotkey)}")
    if revealed_k_plaintext is not None and len(revealed_k_plaintext) != 32:
        # An auto-decrypted K must be 32 bytes; if the chain reveal returns a
        # different length the miner submitted garbage and the prediction
        # cannot decrypt. Treat as missing.
        revealed_k_plaintext = None
    if sha256_ct_commit is not None and len(sha256_ct_commit) != 32:
        sha256_ct_commit = None
    return ChainCommits9B(
        timelock_k_revealed=revealed_k_plaintext,
        sha256_ct_commit=sha256_ct_commit,
        self_archive_url=self_archive_url,
        chain_block_at_k_commit=chain_block_at_k_commit,
        miner_hotkey=miner_hotkey,
    )
