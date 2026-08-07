"""Layer 1 — one coldkey, one seat in the earning set.

Ruled 2026-08-07. The design principle the anti-clone work is built around:

    one unit of predictive behaviour -> at most one curve seat
                                     -> paid to one on-chain principal

This module enforces the second half, which is the cheap, deterministic half.
A coldkey is the closest thing the chain has to an identity, and running many
hotkeys under one is the least expensive way to turn one model into many
paydays. Capping it costs an attacker a registration and an alpha hold per
extra seat instead of a re-tag.

WHY K=1 AND NOT K=3

    "K=3 is already a negotiated farm size. Attackers will run exactly K."
    Any K above one publishes the number of seats a farm should build. One
    coldkey, one seat, is the only value that does not name a target.

WHAT THIS DOES NOT CLOSE

    Many coldkeys running the same model. That is a lineage problem, not an
    identity problem, and it needs the behavioural collapse layer. This module
    is deliberately narrow: it is the layer that cannot be argued with,
    shipped first because it needs no calibration and no thresholds.

KEPT SEAT, DROPPED SEATS

    The highest standing keeps the seat. Ties break on the earlier model
    commit, then the hotkey itself, so the outcome is deterministic and does
    not depend on dict ordering. Dropped hotkeys keep their standings — scores
    are facts — they simply do not reach the curve.

Pure module: no chain calls. The coldkey mapping arrives as data.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

DEFAULT_SEATS_PER_COLDKEY = 1


@dataclass(frozen=True)
class ColdkeyCapResult:
    """What the cap did, in terms the receipt can publish."""
    kept: dict = field(default_factory=dict)      # hotkey -> standing
    dropped: tuple = ()                           # hotkeys that lost a seat
    # coldkey -> the hotkeys it had in contention, so a miner can check the
    # decision rather than be told it.
    contested: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "dropped": list(self.dropped),
            "contested": {ck: list(hks) for ck, hks in self.contested.items()},
            "kept_count": len(self.kept),
        }


def apply_coldkey_cap(
    standings: Mapping[str, float],
    coldkey_of: Mapping[str, str],
    k: int = DEFAULT_SEATS_PER_COLDKEY,
    commit_block: Mapping[str, int] | None = None,
) -> ColdkeyCapResult:
    """Keep at most `k` hotkeys per coldkey, by standing.

    `coldkey_of` maps hotkey -> coldkey ss58. A hotkey with NO known coldkey
    is kept and never grouped: the fail-safe direction for "we could not read
    this identity" is to not confiscate a seat over it. That is deliberate and
    it is a known soft edge — an attacker who can make their coldkey
    unreadable evades this layer, which is one more reason the lineage layer
    exists behind it.
    """
    commit_block = commit_block or {}
    by_coldkey: dict[str, list[str]] = {}
    unknown: list[str] = []

    for hotkey in standings:
        coldkey = coldkey_of.get(hotkey)
        if not coldkey:
            unknown.append(hotkey)
            continue
        by_coldkey.setdefault(coldkey, []).append(hotkey)

    kept: dict[str, float] = {hk: standings[hk] for hk in unknown}
    dropped: list[str] = []
    contested: dict[str, list[str]] = {}

    for coldkey, hotkeys in sorted(by_coldkey.items()):
        if len(hotkeys) > 1:
            contested[coldkey] = sorted(hotkeys)
        ordered = sorted(
            hotkeys,
            key=lambda hk: (
                -standings[hk],                             # best standing first
                commit_block.get(hk, float("inf")),         # then earlier commit
                hk,                                         # then deterministic
            ),
        )
        for hotkey in ordered[:k]:
            kept[hotkey] = standings[hotkey]
        dropped.extend(ordered[k:])

    return ColdkeyCapResult(kept=kept, dropped=tuple(sorted(dropped)),
                            contested=contested)
