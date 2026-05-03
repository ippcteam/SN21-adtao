"""Tests for the Indexed Merkle Tree."""

from __future__ import annotations

import hashlib

import pytest

from hope.commitment.imt import (
    DEFAULT_DEPTH,
    IMTLeaf,
    imt_leaf_hash,
    imt_node_hash,
    imt_proof_inclusion,
    imt_proof_non_inclusion,
    imt_root,
    make_leaves_with_pointers,
    verify_inclusion,
    verify_non_inclusion,
)


def _key(n: int) -> bytes:
    """Construct a 32-byte test key from an integer."""
    return n.to_bytes(32, "big")


def _value(n: int) -> bytes:
    return hashlib.blake2b(f"value-{n}".encode(), digest_size=32).digest()


class TestIMTLeaf:
    def test_constructor_validates_key_length(self):
        with pytest.raises(ValueError):
            IMTLeaf(key=b"\x00" * 31, value=b"\x00" * 32, next_key=b"\x00" * 32)

    def test_constructor_validates_value_length(self):
        with pytest.raises(ValueError):
            IMTLeaf(key=b"\x00" * 32, value=b"\x00" * 33, next_key=b"\x00" * 32)

    def test_constructor_validates_next_key_length(self):
        with pytest.raises(ValueError):
            IMTLeaf(key=b"\x00" * 32, value=b"\x00" * 32, next_key=b"\x00" * 30)


class TestHashHelpers:
    def test_imt_leaf_hash_is_32_bytes(self):
        leaf = IMTLeaf(key=_key(1), value=_value(1), next_key=_key(2))
        h = imt_leaf_hash(leaf)
        assert len(h) == 32

    def test_imt_leaf_hash_deterministic(self):
        leaf = IMTLeaf(key=_key(1), value=_value(1), next_key=_key(2))
        assert imt_leaf_hash(leaf) == imt_leaf_hash(leaf)

    def test_imt_leaf_hash_changes_with_inputs(self):
        a = imt_leaf_hash(IMTLeaf(_key(1), _value(1), _key(2)))
        b = imt_leaf_hash(IMTLeaf(_key(1), _value(2), _key(2)))
        assert a != b

    def test_imt_node_hash(self):
        h = imt_node_hash(b"\x00" * 32, b"\xff" * 32)
        assert len(h) == 32

    def test_imt_node_hash_validates_lengths(self):
        with pytest.raises(ValueError):
            imt_node_hash(b"\x00" * 31, b"\x00" * 32)


class TestMakeLeavesWithPointers:
    def test_empty_input(self):
        assert make_leaves_with_pointers([]) == []

    def test_single_leaf_has_max_next_key(self):
        leaves = make_leaves_with_pointers([(_key(1), _value(1))])
        assert len(leaves) == 1
        assert leaves[0].next_key == b"\xff" * 32

    def test_multi_leaf_pointers(self):
        pairs = [(_key(3), _value(3)), (_key(1), _value(1)), (_key(2), _value(2))]
        leaves = make_leaves_with_pointers(pairs)
        # Should be sorted by key
        assert leaves[0].key == _key(1)
        assert leaves[1].key == _key(2)
        assert leaves[2].key == _key(3)
        # Pointers should chain
        assert leaves[0].next_key == _key(2)
        assert leaves[1].next_key == _key(3)
        assert leaves[2].next_key == b"\xff" * 32

    def test_duplicate_key_rejected(self):
        with pytest.raises(ValueError, match="duplicate key"):
            make_leaves_with_pointers([(_key(1), _value(1)), (_key(1), _value(2))])

    def test_too_many_leaves_rejected(self):
        # depth=2 supports 4 leaves max
        leaves = [(_key(i), _value(i)) for i in range(5)]
        with pytest.raises(ValueError, match="Too many leaves"):
            make_leaves_with_pointers(leaves, depth=2)


class TestRoot:
    def test_empty_tree_root(self):
        # Should compute the root over all-empty leaves
        root = imt_root([])
        assert len(root) == 32

    def test_root_changes_with_leaves(self):
        a_root = imt_root(make_leaves_with_pointers([(_key(1), _value(1))]))
        b_root = imt_root(make_leaves_with_pointers([(_key(2), _value(2))]))
        assert a_root != b_root

    def test_root_deterministic(self):
        leaves = make_leaves_with_pointers([(_key(i), _value(i)) for i in range(8)])
        r1 = imt_root(leaves)
        r2 = imt_root(leaves)
        assert r1 == r2

    def test_root_invariant_under_input_order(self):
        # Different insertion orders to make_leaves_with_pointers should yield same root
        a = make_leaves_with_pointers([(_key(i), _value(i)) for i in [3, 1, 4, 2]])
        b = make_leaves_with_pointers([(_key(i), _value(i)) for i in [4, 3, 2, 1]])
        assert imt_root(a) == imt_root(b)

    def test_root_at_capacity(self):
        # depth=4 → 16 leaves
        leaves = make_leaves_with_pointers([(_key(i), _value(i)) for i in range(16)], depth=4)
        root = imt_root(leaves, depth=4)
        assert len(root) == 32


class TestInclusionProof:
    def test_inclusion_for_present_key(self):
        leaves = make_leaves_with_pointers([(_key(i), _value(i)) for i in range(8)])
        root = imt_root(leaves)
        proof = imt_proof_inclusion(leaves, _key(3))
        assert verify_inclusion(proof, root) is True

    def test_inclusion_proof_for_missing_key_raises(self):
        leaves = make_leaves_with_pointers([(_key(i), _value(i)) for i in range(8)])
        with pytest.raises(KeyError):
            imt_proof_inclusion(leaves, _key(99))

    def test_inclusion_fails_with_wrong_root(self):
        leaves = make_leaves_with_pointers([(_key(i), _value(i)) for i in range(8)])
        proof = imt_proof_inclusion(leaves, _key(3))
        wrong_root = b"\x42" * 32
        assert verify_inclusion(proof, wrong_root) is False

    def test_inclusion_fails_with_tampered_leaf(self):
        leaves = make_leaves_with_pointers([(_key(i), _value(i)) for i in range(8)])
        root = imt_root(leaves)
        proof = imt_proof_inclusion(leaves, _key(3))

        # Tamper with the leaf value
        from dataclasses import replace
        from hope.commitment.imt import InclusionProof

        bad_leaf = IMTLeaf(key=proof.leaf.key, value=b"\xff" * 32, next_key=proof.leaf.next_key)
        bad_proof = InclusionProof(
            leaf=bad_leaf, leaf_index=proof.leaf_index, siblings=proof.siblings
        )
        assert verify_inclusion(bad_proof, root) is False

    def test_inclusion_at_various_depths(self):
        for depth in [4, 8, 16]:
            n = min(8, 1 << depth)
            leaves = make_leaves_with_pointers(
                [(_key(i), _value(i)) for i in range(n)], depth=depth
            )
            root = imt_root(leaves, depth=depth)
            for i in range(n):
                proof = imt_proof_inclusion(leaves, _key(i), depth=depth)
                assert verify_inclusion(proof, root, depth=depth)


class TestNonInclusionProof:
    def test_non_inclusion_for_absent_middle_key(self):
        leaves = make_leaves_with_pointers([(_key(i * 2), _value(i)) for i in range(8)])
        root = imt_root(leaves)
        # _key(3) is between _key(2) and _key(4); not in tree
        proof = imt_proof_non_inclusion(leaves, _key(3))
        assert verify_non_inclusion(proof, root) is True

    def test_non_inclusion_for_present_key_raises(self):
        leaves = make_leaves_with_pointers([(_key(i), _value(i)) for i in range(8)])
        with pytest.raises(KeyError):
            imt_proof_non_inclusion(leaves, _key(3))

    def test_non_inclusion_fails_with_wrong_root(self):
        leaves = make_leaves_with_pointers([(_key(i * 2), _value(i)) for i in range(8)])
        proof = imt_proof_non_inclusion(leaves, _key(3))
        wrong_root = b"\x42" * 32
        assert verify_non_inclusion(proof, wrong_root) is False

    def test_non_inclusion_fails_with_tampered_target(self):
        leaves = make_leaves_with_pointers([(_key(i * 2), _value(i)) for i in range(8)])
        root = imt_root(leaves)
        proof = imt_proof_non_inclusion(leaves, _key(3))

        # Construct a forged proof claiming a different target_key was absent
        from hope.commitment.imt import NonInclusionProof

        # _key(2) IS in the tree, but proof's bracket is for the gap (2, 4)
        # so claiming target=_key(2) should fail (it's the bracket leaf itself, not absent)
        bad_proof = NonInclusionProof(
            target_key=_key(2),
            bracket_leaf_proof=proof.bracket_leaf_proof,
        )
        assert verify_non_inclusion(bad_proof, root) is False

    def test_non_inclusion_below_smallest(self):
        # If target is below all leaves, no bracket exists; should raise
        leaves = make_leaves_with_pointers([(_key(i * 2 + 5), _value(i)) for i in range(4)])
        with pytest.raises(ValueError, match="no bracket"):
            imt_proof_non_inclusion(leaves, _key(0))


class TestRealWorldScenario:
    """Simulate a real SN21 use case: 50 miner hotkeys, prediction commits."""

    def test_50_miners_inclusion_proofs(self):
        # 50 fake hotkeys
        miner_data = [
            (
                hashlib.blake2b(f"miner-{i}".encode(), digest_size=32).digest(),
                hashlib.blake2b(f"prediction-{i}".encode(), digest_size=32).digest(),
            )
            for i in range(50)
        ]
        leaves = make_leaves_with_pointers(miner_data)
        root = imt_root(leaves)

        # Every miner's prediction should have a valid inclusion proof
        for hk, _ in miner_data:
            proof = imt_proof_inclusion(leaves, hk)
            assert verify_inclusion(proof, root)

    def test_50_miners_non_inclusion_for_unregistered(self):
        miner_data = [
            (
                hashlib.blake2b(f"miner-{i}".encode(), digest_size=32).digest(),
                hashlib.blake2b(f"prediction-{i}".encode(), digest_size=32).digest(),
            )
            for i in range(50)
        ]
        leaves = make_leaves_with_pointers(miner_data)
        root = imt_root(leaves)

        # An unregistered hotkey should have a valid non-inclusion proof
        unregistered = hashlib.blake2b(b"unregistered-miner", digest_size=32).digest()
        # Make sure it's not actually in the tree
        if any(l.key == unregistered for l in leaves):
            pytest.skip("unlikely hash collision")
        try:
            proof = imt_proof_non_inclusion(leaves, unregistered)
            assert verify_non_inclusion(proof, root)
        except ValueError:
            # If unregistered is below smallest, that's OK for this test
            pass
