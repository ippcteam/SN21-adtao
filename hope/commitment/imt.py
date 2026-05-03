"""Indexed Merkle Tree (IMT) — sorted-leaf binary Merkle tree with low/high pointers.

Each leaf carries `(key, value, next_key)` where `next_key` points to the next-larger key
in sorted order (or 0xFF...FF if the leaf has the largest key). This linked structure
makes non-inclusion proofs cheap: to prove `target_key` is absent, exhibit a leaf whose
`key < target_key < next_key`.

Reference: Aztec's IMT design
(https://docs.aztec.network/aztec/concepts/storage/trees/indexed_merkle_tree).

This is a minimal in-tree implementation per Q7. Audit-friendly: ~250 LOC, no third-party
crypto. Hash function is BLAKE2b-256 throughout.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# 32-byte sentinel meaning "this is the largest key in the tree"
_MAX_KEY = b"\xff" * 32
# 32-byte sentinel for empty leaves (used during root padding)
_EMPTY_KEY = b"\x00" * 32
_EMPTY_VALUE = b"\x00" * 32

# Default tree depth. 2^16 = 65,536 leaf slots — well above SN21 miner population.
DEFAULT_DEPTH: int = 16


@dataclass(frozen=True)
class IMTLeaf:
    """A leaf in the indexed Merkle tree.

    key:       the indexed key (e.g., a 32-byte hotkey raw)
    value:     the committed value at that key (e.g., 32-byte content hash)
    next_key:  pointer to the next-larger key in the sorted leaf list,
               or _MAX_KEY if this is the largest leaf
    """

    key: bytes
    value: bytes
    next_key: bytes

    def __post_init__(self) -> None:
        if len(self.key) != 32:
            raise ValueError(f"IMTLeaf.key must be 32 bytes, got {len(self.key)}")
        if len(self.value) != 32:
            raise ValueError(f"IMTLeaf.value must be 32 bytes, got {len(self.value)}")
        if len(self.next_key) != 32:
            raise ValueError(f"IMTLeaf.next_key must be 32 bytes, got {len(self.next_key)}")


@dataclass(frozen=True)
class InclusionProof:
    """Proof that `leaf` is at index `leaf_index` in a tree with the given root."""

    leaf: IMTLeaf
    leaf_index: int
    siblings: list[bytes]  # bottom-up; len == tree depth


@dataclass(frozen=True)
class NonInclusionProof:
    """Proof that `target_key` is absent from a tree with the given root.

    Witness: a leaf whose key < target_key < next_key (i.e., the gap that would
    have contained target_key if it existed).
    """

    target_key: bytes
    bracket_leaf_proof: InclusionProof


# ---- Hash helpers ----


def imt_leaf_hash(leaf: IMTLeaf) -> bytes:
    """BLAKE2b-256 of (key || value || next_key) — 96 bytes input, 32 bytes output."""
    return hashlib.blake2b(
        leaf.key + leaf.value + leaf.next_key, digest_size=32
    ).digest()


def imt_node_hash(left: bytes, right: bytes) -> bytes:
    """BLAKE2b-256 of (left || right) — 64 bytes input, 32 bytes output."""
    if len(left) != 32 or len(right) != 32:
        raise ValueError("imt_node_hash inputs must be 32 bytes each")
    return hashlib.blake2b(left + right, digest_size=32).digest()


def _empty_leaf_hash() -> bytes:
    """Hash of the empty leaf (key=0, value=0, next_key=0). Cached."""
    return imt_leaf_hash(IMTLeaf(_EMPTY_KEY, _EMPTY_VALUE, _EMPTY_KEY))


# ---- Tree construction & traversal ----


def _build_tree_levels(leaf_hashes: list[bytes], depth: int) -> list[list[bytes]]:
    """Return all levels of the tree, leaf level at index 0, root at index `depth`.

    Pads to 2^depth leaves with empty-leaf hashes.
    """
    n_leaves = 1 << depth
    if len(leaf_hashes) > n_leaves:
        raise ValueError(f"Too many leaves: {len(leaf_hashes)} > {n_leaves}")

    empty_h = _empty_leaf_hash()
    level = list(leaf_hashes) + [empty_h] * (n_leaves - len(leaf_hashes))
    levels = [level]

    for _ in range(depth):
        next_level = []
        for i in range(0, len(level), 2):
            next_level.append(imt_node_hash(level[i], level[i + 1]))
        levels.append(next_level)
        level = next_level

    return levels


def imt_root(leaves: list[IMTLeaf], depth: int = DEFAULT_DEPTH) -> bytes:
    """Compute the IMT root over the given leaves.

    Leaves MUST be pre-sorted by key and have correct next_key pointers; this
    function does not normalize them. Use `make_leaves_with_pointers` first.
    """
    leaf_hashes = [imt_leaf_hash(l) for l in leaves]
    levels = _build_tree_levels(leaf_hashes, depth)
    return levels[depth][0]


def make_leaves_with_pointers(
    pairs: list[tuple[bytes, bytes]], depth: int = DEFAULT_DEPTH
) -> list[IMTLeaf]:
    """Construct sorted leaves with correct next_key pointers from (key, value) pairs.

    Sorts by key. Last leaf's next_key is set to _MAX_KEY (sentinel).
    """
    if not pairs:
        return []
    sorted_pairs = sorted(pairs, key=lambda p: p[0])
    # Validate keys are unique
    for i in range(1, len(sorted_pairs)):
        if sorted_pairs[i][0] == sorted_pairs[i - 1][0]:
            raise ValueError(f"duplicate key in IMT input: {sorted_pairs[i][0].hex()}")
    leaves = []
    for i, (k, v) in enumerate(sorted_pairs):
        next_key = sorted_pairs[i + 1][0] if i + 1 < len(sorted_pairs) else _MAX_KEY
        leaves.append(IMTLeaf(key=k, value=v, next_key=next_key))
    n_leaves = 1 << depth
    if len(leaves) > n_leaves:
        raise ValueError(f"Too many leaves: {len(leaves)} > {n_leaves}")
    return leaves


# ---- Inclusion proofs ----


def imt_proof_inclusion(
    leaves: list[IMTLeaf], target_key: bytes, depth: int = DEFAULT_DEPTH
) -> InclusionProof:
    """Construct an inclusion proof for `target_key` against tree formed by `leaves`.

    Raises KeyError if target_key is not in leaves.
    """
    leaf_idx = next((i for i, l in enumerate(leaves) if l.key == target_key), None)
    if leaf_idx is None:
        raise KeyError(f"target_key {target_key.hex()} not in tree")

    target_leaf = leaves[leaf_idx]
    leaf_hashes = [imt_leaf_hash(l) for l in leaves]
    levels = _build_tree_levels(leaf_hashes, depth)

    siblings: list[bytes] = []
    idx = leaf_idx
    for d in range(depth):
        sibling_idx = idx ^ 1
        siblings.append(levels[d][sibling_idx])
        idx //= 2

    return InclusionProof(leaf=target_leaf, leaf_index=leaf_idx, siblings=siblings)


def verify_inclusion(
    proof: InclusionProof, expected_root: bytes, depth: int = DEFAULT_DEPTH
) -> bool:
    """Verify an inclusion proof against the expected root."""
    if len(proof.siblings) != depth:
        return False
    if len(expected_root) != 32:
        return False

    h = imt_leaf_hash(proof.leaf)
    idx = proof.leaf_index
    for sibling in proof.siblings:
        if idx % 2 == 0:
            h = imt_node_hash(h, sibling)
        else:
            h = imt_node_hash(sibling, h)
        idx //= 2
    return h == expected_root


# ---- Non-inclusion proofs ----


def imt_proof_non_inclusion(
    leaves: list[IMTLeaf], target_key: bytes, depth: int = DEFAULT_DEPTH
) -> NonInclusionProof:
    """Construct a non-inclusion proof for `target_key`.

    Witness: a leaf whose `key < target_key < next_key`. Raises KeyError if
    target_key is actually in the tree (use inclusion proof instead).
    """
    if any(l.key == target_key for l in leaves):
        raise KeyError(f"target_key {target_key.hex()} IS in tree; use inclusion proof")

    bracket = next(
        (l for l in leaves if l.key < target_key < l.next_key),
        None,
    )
    if bracket is None:
        raise ValueError(
            f"no bracket leaf found for {target_key.hex()}; tree may be empty or "
            "target_key is below smallest leaf"
        )

    inclusion = imt_proof_inclusion(leaves, bracket.key, depth=depth)
    return NonInclusionProof(target_key=target_key, bracket_leaf_proof=inclusion)


def verify_non_inclusion(
    proof: NonInclusionProof, expected_root: bytes, depth: int = DEFAULT_DEPTH
) -> bool:
    """Verify a non-inclusion proof against the expected root.

    Two checks:
    1. The bracket leaf is in the tree (inclusion proof valid).
    2. target_key falls strictly between bracket.key and bracket.next_key.
    """
    if not verify_inclusion(proof.bracket_leaf_proof, expected_root, depth=depth):
        return False
    bracket = proof.bracket_leaf_proof.leaf
    return bracket.key < proof.target_key < bracket.next_key
