"""The feed's rolling root and per-day proofs, derived from what is published.

The bridge between the accuracy feed on disk and the Merkle machinery: read
every published daily document in day order, build the root over their hashes,
and produce the inclusion proof for any one day.

DERIVED, NEVER STORED. The root is recomputed from the accuracy directory on
every call rather than kept in a state file. That costs a directory scan and
buys the property being sold: the root is a pure function of the published
documents, so anyone holding the same documents computes the same root. A
cached root is a second source of truth, and a second source of truth is a
thing that can disagree with the first.

ORDER IS BY DAY, NOT BY FILESYSTEM. Leaves are sorted on the ISO date in the
filename — lexicographic order over YYYY-MM-DD is chronological order, and
readdir order is not. A tree built in a different order is a different root.
"""

from __future__ import annotations

import json
import os
from typing import Optional

from hope.publication.merkle import build_root, inclusion_proof, verify_proof


def _accuracy_dir(root: str) -> str:
    return os.path.join(root, "accuracy")


def published_days(root: str) -> list[tuple[str, str]]:
    """[(day, document_sha256)] for every published accuracy document, oldest
    first. These are the Merkle leaves, in leaf order.

    A file that will not parse is a HARD failure, not a skip: silently
    dropping a leaf produces a root that omits a day, which is precisely the
    tampering shape this structure exists to detect.
    """
    d = _accuracy_dir(root)
    if not os.path.isdir(d):
        return []
    out = []
    for fn in sorted(f for f in os.listdir(d)
                     if f.endswith(".json") and not f.startswith("_")):
        path = os.path.join(d, fn)
        with open(path) as f:
            env = json.load(f)
        sha = env.get("sha256")
        if not sha:
            raise ValueError(
                f"{path} has no sha256 — cannot build a feed root over a "
                f"document whose own hash is missing")
        out.append((fn[:-5], sha))
    return out


def feed_root(root: str) -> Optional[str]:
    """The rolling Merkle root over every published day. None if nothing has
    published — this is what goes on chain, and an empty feed must not commit
    a zero hash that would read as a real anchor."""
    return build_root([sha for _day, sha in published_days(root)])


def day_proof(root: str, day: str) -> Optional[dict]:
    """Everything a miner needs to prove `day` is in the anchored root.

    Returns None when the day has not published — the caller distinguishes
    "not in the tree" from "tree is empty", which are different answers to a
    miner asking why their day will not verify.
    """
    days = published_days(root)
    index = {d: i for i, (d, _s) in enumerate(days)}
    if day not in index:
        return None
    i = index[day]
    leaves = [sha for _d, sha in days]
    return {
        "day": day,
        "document_sha256": leaves[i],
        "leaf_index": i,
        "leaf_count": len(leaves),
        "proof": inclusion_proof(leaves, i),
        "feed_root": build_root(leaves),
    }


def verify_day_in_root(document_sha256: str, proof: list, root_hex: str) -> bool:
    """Miner-side check, re-exported here so verifiers import one module."""
    return verify_proof(document_sha256, proof, root_hex)
