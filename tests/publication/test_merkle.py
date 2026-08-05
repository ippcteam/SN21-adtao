"""The rolling Merkle root — including the attacks it exists to stop.

A tree that verifies honest proofs proves little; these tests forge proofs,
swap leaves and replay internal nodes, and assert the root refuses them.
"""

import pytest

from hope.publication.merkle import (
    build_root,
    inclusion_proof,
    leaf_hash,
    node_hash,
    verify_proof,
)

DAYS = [f"{i:064x}" for i in range(1, 12)]   # 11 fake document hashes


# ---- the property the whole design rests on ---------------------------------

@pytest.mark.parametrize("n", range(1, 12))
def test_every_day_proves_against_the_current_root(n):
    """THE POINT OF OPTION B: after n days, EVERY day so far — not just the
    newest — verifies against the single root the chain holds."""
    leaves = DAYS[:n]
    root = build_root(leaves)
    for i, day in enumerate(leaves):
        assert verify_proof(day, inclusion_proof(leaves, i), root), \
            f"day {i} of {n} failed"


def test_the_root_moves_as_days_are_added():
    """Each day's commit must be a NEW root, or the anchor stops meaning
    'everything so far'."""
    roots = [build_root(DAYS[:n]) for n in range(1, 6)]
    assert len(set(roots)) == 5


def test_an_old_day_still_proves_after_later_days_land():
    """The failure Option A had: verifying a three-day-old receipt. Here the
    old day proves against the CURRENT root, no archive node."""
    root_now = build_root(DAYS)          # 11 days later
    assert verify_proof(DAYS[0], inclusion_proof(DAYS, 0), root_now)
    assert verify_proof(DAYS[3], inclusion_proof(DAYS, 3), root_now)


# ---- forgery ------------------------------------------------------------------

def test_a_day_that_was_never_published_does_not_prove():
    root = build_root(DAYS)
    forged = "de" * 32
    assert not verify_proof(forged, inclusion_proof(DAYS, 0), root)


def test_a_tampered_proof_step_fails():
    root = build_root(DAYS)
    proof = inclusion_proof(DAYS, 5)
    proof[0] = {"hash": "ff" * 32, "side": proof[0]["side"]}
    assert not verify_proof(DAYS[5], proof, root)


def test_a_flipped_side_fails():
    """Order matters: swapping which side a sibling sits on changes the
    parent hash. If this passed, proofs would be malleable."""
    root = build_root(DAYS)
    proof = inclusion_proof(DAYS, 2)
    flipped = [{**s, "side": ("left" if s["side"] == "right" else "right")}
               for s in proof]
    assert not verify_proof(DAYS[2], flipped, root)


def test_proof_from_the_wrong_tree_fails():
    """A proof built against yesterday's tree must not verify against
    today's root — otherwise a stale proof would launder a removed day."""
    old_root = build_root(DAYS[:5])
    new_proof = inclusion_proof(DAYS, 1)
    assert not verify_proof(DAYS[1], new_proof, old_root)


def test_empty_proof_only_verifies_a_single_leaf_tree():
    assert verify_proof(DAYS[0], [], build_root([DAYS[0]]))
    assert not verify_proof(DAYS[0], [], build_root(DAYS))


# ---- the two classic Merkle attacks -----------------------------------------

def test_internal_nodes_cannot_be_replayed_as_leaves():
    """SECOND PREIMAGE. Without domain separation an attacker could take two
    adjacent leaf hashes, present their concatenation as one 'leaf', and forge
    inclusion for data never published. The 0x00/0x01 prefixes stop it: a leaf
    hash and a node hash of the same bytes are different values."""
    a, b = leaf_hash(DAYS[0]), leaf_hash(DAYS[1])
    parent = node_hash(a, b)
    assert leaf_hash(a + b) != parent
    assert leaf_hash(parent) != parent


def test_odd_levels_promote_so_two_leaf_sets_cannot_share_a_root():
    """CVE-2012-2459 shape. If a lone node were DUPLICATED and hashed with
    itself, [x, y, z] and [x, y, z, z] would produce the same root — a day
    could be swapped for a copy without moving the anchor."""
    three = build_root(DAYS[:3])
    three_plus_dup = build_root(DAYS[:3] + [DAYS[2]])
    assert three != three_plus_dup


# ---- boundaries ---------------------------------------------------------------

def test_empty_feed_has_no_root_rather_than_a_zero_one():
    """None, not a zero hash: committing zeros would read on chain as a real
    anchor over an empty history."""
    assert build_root([]) is None
    assert not verify_proof(DAYS[0], [], None)


def test_single_day_feed():
    root = build_root([DAYS[0]])
    assert root == leaf_hash(DAYS[0])
    assert verify_proof(DAYS[0], [], root)


def test_index_out_of_range_raises():
    with pytest.raises(IndexError):
        inclusion_proof(DAYS, len(DAYS))
    with pytest.raises(IndexError):
        inclusion_proof([], 0)


def test_malformed_proof_hash_is_rejected_not_crashed():
    root = build_root(DAYS)
    bad = [{"hash": "not-hex", "side": "left"}]
    assert not verify_proof(DAYS[0], bad, root)
    assert not verify_proof(DAYS[0], [{"hash": "aa" * 32}], root)   # no side
