"""D11 publication rail — generic, attested metrics publication (in-house).

Rob's GAP-3 decision (2026-07-25): "ignore tensora — we're 100% in-house."
The rail is therefore ours: a generic path that takes ANY metrics document,
canonicalises it, hash-chains it to the previous document of the same feed,
signs it (ed25519, same primitive as the commitment layer), and yields the
anchor digest for the existing on-chain commitment machinery
(hope/commitment/on_chain.py submit_sha256_commit — invoked by the caller,
never from this module). v0.5 §13: "specced generic — any metrics file, not
just accuracy — so later feeds land on it without rework."

Pure module: no I/O, no chain calls, no clocks (timestamps injected).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

RAIL_SCHEMA_VERSION = "rail-v1"


def canonical_bytes(doc: dict) -> bytes:
    """Deterministic encoding: sorted keys, compact separators, UTF-8."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), default=str).encode()


def document_sha256(doc: dict) -> str:
    return hashlib.sha256(canonical_bytes(doc)).hexdigest()


def build_document(feed: str, as_of: str, metrics: dict,
                   generated_at: str, prev_sha256: Optional[str]) -> dict:
    """Assemble the publishable envelope. All time values injected as ISO
    strings by the caller (rail stays clock-free and replay-safe)."""
    return {
        "rail_schema": RAIL_SCHEMA_VERSION,
        "feed": feed,
        "as_of": as_of,
        "generated_at": generated_at,
        "prev_sha256": prev_sha256,
        "metrics": metrics,
    }


@dataclass(frozen=True)
class AttestedDocument:
    document: dict
    sha256: str
    signature_hex: str
    public_key_hex: str

    @property
    def anchor_digest(self) -> str:
        """What goes on-chain via submit_sha256_commit: the document hash."""
        return self.sha256


def attest(doc: dict, private_key: Ed25519PrivateKey) -> AttestedDocument:
    """Sign the canonical hash. Signature covers the sha256 hex bytes so a
    verifier needs only (document, signature, pubkey) — same discipline as
    the commitment layer's inner_sig."""
    sha = document_sha256(doc)
    sig = private_key.sign(sha.encode())
    pub = private_key.public_key().public_bytes_raw().hex()
    return AttestedDocument(document=doc, sha256=sha,
                            signature_hex=sig.hex(), public_key_hex=pub)


def verify(att: AttestedDocument) -> bool:
    """Full verification: hash matches document AND signature matches hash."""
    if document_sha256(att.document) != att.sha256:
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(att.public_key_hex))
        pub.verify(bytes.fromhex(att.signature_hex), att.sha256.encode())
        return True
    except Exception:
        return False


def chain_ok(docs: list[dict]) -> bool:
    """Validate a feed series: each document's prev_sha256 equals the
    previous document's canonical hash (first may be None)."""
    prev = None
    for d in docs:
        if d.get("prev_sha256") != prev:
            return False
        prev = document_sha256(d)
    return True
