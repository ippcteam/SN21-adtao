"""Rolling Merkle root over the daily feed — so ANY day stays verifiable.

WHY THIS EXISTS (decided 2026-08-05, Option B). The chain's
`Commitments::CommitmentOf` holds ONE entry per (netuid, hotkey) and every new
commit overwrites the previous, regardless of variant. Anchoring each day's
accuracy-document hash directly would therefore erase the day before: only the
newest day would be verifiable at chain head, and a miner checking a
three-day-old receipt would read today's anchor, get a mismatch, and see what
looks like fraud but is storage semantics. Verifying an older day would need
the commit's block hash plus an ARCHIVE node — a barrier most miners cannot
clear.

A rolling root fixes it with no new chain machinery: the leaves are every daily
accuracy-document hash so far, and the root is recommitted each day. The newest
on-chain commitment always covers the ENTIRE history, so a miner verifies any
day from chain head with a proof of a few dozen bytes.

THE ROOT IS A PURE FUNCTION OF THE PUBLISHED DOCUMENTS. It is recomputed from
the accuracy feed on disk rather than kept as incremental state, deliberately:
separate state is separate state to corrupt, and a root derived from the
published set is one anyone else can rederive from the same published set. That
is the whole property being sold.

DOMAIN SEPARATION (RFC 6962 style). Leaf hashes are prefixed 0x00, internal
nodes 0x01. Without this, an internal node can be replayed as a leaf — an
attacker who knows two adjacent leaf hashes could present their concatenation
as a single "leaf" and forge an inclusion proof for data that was never
published. The prefixes cost one byte and close that entirely.

ODD LEVELS PROMOTE, THEY DO NOT DUPLICATE. A lone node at the end of a level is
carried up unchanged rather than hashed with itself. Duplicating is the classic
CVE-2012-2459 shape: two different leaf sets can produce the same root, which
would let a day be swapped for a duplicate without changing the anchor.
"""

from __future__ import annotations

import hashlib
from typing import Sequence

LEAF_PREFIX = b"\x00"
NODE_PREFIX = b"\x01"


def leaf_hash(payload: str | bytes) -> str:
    """Hash one leaf. `payload` is a daily document's sha256 hex string."""
    if isinstance(payload, str):
        payload = payload.encode()
    return hashlib.sha256(LEAF_PREFIX + payload).hexdigest()


def node_hash(left: str, right: str) -> str:
    """Hash two children into their parent."""
    return hashlib.sha256(
        NODE_PREFIX + bytes.fromhex(left) + bytes.fromhex(right)
    ).hexdigest()


def _levels(leaves: Sequence[str]) -> list[list[str]]:
    """Every level bottom-up, [0] = leaf hashes. Empty input -> [[]]."""
    if not leaves:
        return [[]]
    level = [leaf_hash(x) for x in leaves]
    out = [level]
    while len(level) > 1:
        nxt = []
        for i in range(0, len(level) - 1, 2):
            nxt.append(node_hash(level[i], level[i + 1]))
        if len(level) % 2:
            nxt.append(level[-1])      # promote, never duplicate (see docstring)
        level = nxt
        out.append(level)
    return out


def build_root(leaves: Sequence[str]) -> str | None:
    """Merkle root over `leaves` in order. None for an empty feed — a root
    over nothing is not zero, it is absent, and callers must not commit a
    zero hash that would read as a real anchor."""
    if not leaves:
        return None
    return _levels(leaves)[-1][0]


def inclusion_proof(leaves: Sequence[str], index: int) -> list[dict]:
    """Sibling path proving `leaves[index]` is in the tree.

    Each step is {"hash": <hex>, "side": "left"|"right"} — the side the
    SIBLING sits on, so a verifier concatenates in the right order without
    knowing the tree shape. A promoted node has no sibling at that level and
    contributes no step, which is why proofs vary in length by a step or two.
    """
    if not leaves or not (0 <= index < len(leaves)):
        raise IndexError(f"index {index} outside 0..{len(leaves) - 1}")
    proof: list[dict] = []
    levels = _levels(leaves)
    idx = index
    for level in levels[:-1]:
        if len(level) == 1:
            break
        if idx == len(level) - 1 and len(level) % 2:
            # promoted: no sibling at this level, index halves (rounding down)
            idx //= 2
            continue
        if idx % 2:
            proof.append({"hash": level[idx - 1], "side": "left"})
        else:
            proof.append({"hash": level[idx + 1], "side": "right"})
        idx //= 2
    return proof


def verify_proof(leaf_payload: str, proof: Sequence[dict], root: str) -> bool:
    """Recompute the root from a leaf and its sibling path.

    This is the whole miner-side check: given their day's document hash, the
    proof served alongside it, and the root read from chain, does the
    arithmetic land on the same root? No trust in the server that served the
    proof — a wrong proof simply fails to reach the root.
    """
    if not root:
        return False
    cur = leaf_hash(leaf_payload)
    for step in proof:
        sib = step.get("hash", "")
        try:
            if step.get("side") == "left":
                cur = node_hash(sib, cur)
            elif step.get("side") == "right":
                cur = node_hash(cur, sib)
            else:
                return False
        except ValueError:
            return False           # non-hex sibling: malformed proof, not a match
    return cur == root
