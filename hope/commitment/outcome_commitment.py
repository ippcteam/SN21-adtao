"""On-chain outcome commitment (P4b) — pin ONE canonical outcome set per epoch.

After the submission deadline the operator commits, on chain, the canonical hash
of an epoch's outcome set under the outcome-signer identity:

    b"sn21-outcomes-v1:" + sha256_digest(32) + epoch_id

A validator fetching outcomes recomputes the digest from the package it received
and checks it against the on-chain-committed digest, rejecting on mismatch. This
guarantees every validator scores against the *same*, immutably-anchored outcome
set — the operator cannot serve different outcomes to different validators or
quietly change them after the fact. (The signature on the package already proves
attribution + integrity; this adds an immutable, one-set-for-all anchor.)

The digest is bound to the epoch_id so a commitment cannot be replayed across
epochs, and is computed over a canonical (sorted, CBOR) form so the operator and
every validator derive the identical value from the same package.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from hope.commitment.canonical import canonical_cbor_dumps

OUTCOME_COMMIT_PREFIX = b"sn21-outcomes-v1:"
_DIGEST_LEN = 32


def outcomes_digest(epoch_id: str, package: dict) -> bytes:
    """Canonical sha256 over an epoch's outcome set, bound to the epoch_id.

    Extracts each episode's `validator_only_outcomes` from the operator package
    (the outcome data the validator scores against), sorts by episode_id, and
    hashes the canonical-CBOR encoding together with the epoch_id. Operator and
    validator compute this identically from the same package response.
    """
    pairs = []
    for ep in package.get("episodes", []):
        voo = ep.get("validator_only_outcomes")
        if voo is None:
            continue
        pairs.append((str(ep.get("episode_id", "")), voo))
    pairs.sort(key=lambda p: p[0])
    blob = canonical_cbor_dumps({"epoch_id": str(epoch_id), "outcomes": pairs})
    return hashlib.sha256(blob).digest()


def build_outcome_commitment(epoch_id: str, digest: bytes) -> bytes:
    """Assemble the on-chain Raw commitment payload."""
    if len(digest) != _DIGEST_LEN:
        raise ValueError(f"digest must be {_DIGEST_LEN} bytes, got {len(digest)}")
    return OUTCOME_COMMIT_PREFIX + digest + str(epoch_id).encode("utf-8")


def parse_outcome_commitment(raw: bytes) -> Optional[tuple[str, bytes]]:
    """Parse a Raw commitment into (epoch_id, digest), or None if not ours."""
    if not isinstance(raw, (bytes, bytearray)) or not raw.startswith(OUTCOME_COMMIT_PREFIX):
        return None
    body = bytes(raw)[len(OUTCOME_COMMIT_PREFIX):]
    if len(body) < _DIGEST_LEN:
        return None
    digest = body[:_DIGEST_LEN]
    try:
        epoch_id = body[_DIGEST_LEN:].decode("utf-8")
    except UnicodeDecodeError:
        return None
    return epoch_id, digest


def verify_package_against_commitment(epoch_id: str, package: dict, raw: bytes) -> bool:
    """True iff `raw` is an outcome commitment for `epoch_id` whose digest
    matches the canonical digest recomputed from `package`."""
    parsed = parse_outcome_commitment(raw)
    if parsed is None:
        return False
    committed_epoch, committed_digest = parsed
    if committed_epoch != str(epoch_id):
        return False
    return committed_digest == outcomes_digest(epoch_id, package)


def read_outcome_commitment(subtensor, netuid: int, signer_ss58: str,
                            *, block_hash: Optional[str] = None) -> Optional[bytes]:
    """Read the `sn21-outcomes-v1` Raw commit published by `signer_ss58`, or None.

    Reads `Commitments.CommitmentOf[netuid][signer_ss58]` (single-slot,
    last-write-wins) and returns the first field whose bytes carry our prefix.
    """
    from hope.commitment.chain_reader import read_commitment_of
    fields = read_commitment_of(subtensor, netuid, signer_ss58, block_hash=block_hash)
    if not fields:
        return None
    for f in fields:
        raw = getattr(f, "bytes_", f if isinstance(f, (bytes, bytearray)) else None)
        if isinstance(raw, (bytes, bytearray)) and bytes(raw).startswith(OUTCOME_COMMIT_PREFIX):
            return bytes(raw)
    return None


def verify_outcome_commitment(subtensor, netuid: int, signer_ss58: str,
                              epoch_id: str, package: dict,
                              *, block_hash: Optional[str] = None) -> tuple[bool, str]:
    """Read the on-chain outcome commitment for `signer_ss58` and verify the
    `package` matches it. Returns (ok, reason)."""
    raw = read_outcome_commitment(subtensor, netuid, signer_ss58, block_hash=block_hash)
    if raw is None:
        return False, "no sn21-outcomes-v1 commitment on chain for signer"
    if verify_package_against_commitment(epoch_id, package, raw):
        return True, "ok"
    return False, "fetched outcomes do not match the on-chain committed digest"
