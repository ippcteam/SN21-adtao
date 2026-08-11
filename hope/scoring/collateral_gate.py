"""The alpha-hold gate — enforcing the published stake floor on weights.

WHAT WAS WRONG

    SN21_TRANSITION_PLAN §Bridge eligibility says, of the period from 10
    August: "carried-over weekly weights are paid ONLY to miners who meet
    BOTH" of submission and hold, and "Fail the hold -> not paid, even if you
    submitted."

    Nothing enforced it. `collateral_floor` computed a compliance view and
    wrote it to an advisory JSON that never touched weights; the weight path
    imported that module only for `resolve_burn_fraction`. On 11 August, 30 of
    117 funded miners held less than the 150-alpha floor and were taking 12.2%
    of miner emissions between them.

WHAT COUNTS AS THE HOLD

    SN21_STAKING states the requirement two ways: you "hold a published amount
    of subnet alpha on your miner", and separately, "as your model earns,
    incentive can be directed into meeting the floor (capture / escrow path)".
    Both satisfy it, so the hold is the GREATER of alpha held on chain and
    alpha captured into the lock.

    Reading only the capture bookkeeping would be catastrophic today: nothing
    has earned through the daily path yet, so every locked balance is zero and
    enforcement would cut the entire subnet.

FAIL OPEN, LOUDLY

    A hotkey the chain did not answer for is NOT treated as failing. An
    unreadable metagraph is our outage, and confiscating seats over it would
    be a worse error than the one this gate fixes — the same rule the coldkey
    cap already follows.

NEVER EMPTY THE VECTOR

    If enforcement would leave nobody earning, it refuses and reports why. An
    empty vector pays no one and, committed on chain, would replace a working
    distribution with silence. A gate that can zero the subnet on a bad read
    is not a safety control.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

# Default OFF. The floor is miner-facing and switching it on stops payments,
# so activation is a deliberate operator act, never a deploy side effect.
ENFORCE_ENV = "SN21_COLLATERAL_ENFORCE"

# Below this many survivors the gate refuses to act at all.
MIN_SURVIVORS = 1


def enforcement_enabled(environ=os.environ) -> bool:
    return (environ.get(ENFORCE_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on")


@dataclass(frozen=True)
class GateResult:
    weights: dict            # post-gate, re-normalised over the same total
    excluded: dict = field(default_factory=dict)   # hotkey/uid -> reason
    unreadable: list = field(default_factory=list)  # kept, chain silent
    applied: bool = False
    refused_reason: str | None = None
    floor_alpha: float = 0.0

    def as_dict(self) -> dict:
        return {
            "applied": self.applied,
            "floor_alpha": self.floor_alpha,
            "excluded_count": len(self.excluded),
            "excluded": self.excluded,
            "unreadable_kept": self.unreadable,
            "refused_reason": self.refused_reason,
            "earning_set_size": len([w for w in self.weights.values() if w > 0]),
        }


def effective_hold(key, alpha_reader: Callable[[object], float | None],
                   locked: Mapping | None) -> float | None:
    """The greater of chain-held alpha and capture-locked alpha.

    None means the chain did not answer AND we hold no bookkeeping for the
    key — unknown, not zero.
    """
    on_chain = None
    try:
        on_chain = alpha_reader(key)
    except Exception:
        on_chain = None
    captured = None
    if locked is not None and key in locked:
        try:
            captured = float(locked[key])
        except (TypeError, ValueError):
            captured = None
    if on_chain is None and captured is None:
        return None
    return max(on_chain or 0.0, captured or 0.0)


def apply_hold(weights: Mapping, floor_alpha: float,
               alpha_reader: Callable[[object], float | None],
               locked: Mapping | None = None,
               environ=os.environ,
               force: bool = False,
               protected: frozenset | set | None = None) -> GateResult:
    """Drop miners below the floor and re-normalise the survivors.

    `protected` names destinations that are NOT miners and must keep their
    exact weight — above all the burn UID, which rides in the same vector.
    Without it, re-normalising over the whole vector inflates burn: excluding
    30 miners would have quietly raised burn from 45% to a higher number
    nobody published. The freed weight belongs to the miners who qualified.

    Among the survivors, re-normalisation preserves ratios, exactly as the
    rewards curve does when fewer earners qualify.

    `force` runs the gate regardless of the env flag, for dry runs.
    """
    protected = frozenset(protected or ())
    total = float(sum(w for w in weights.values() if w > 0)) or 0.0
    if not enforcement_enabled(environ) and not force:
        return GateResult(weights=dict(weights), applied=False,
                          refused_reason=f"{ENFORCE_ENV} off",
                          floor_alpha=floor_alpha)
    if floor_alpha <= 0:
        return GateResult(weights=dict(weights), applied=False,
                          refused_reason="floor is zero — nothing to enforce",
                          floor_alpha=floor_alpha)
    if total <= 0:
        return GateResult(weights=dict(weights), applied=False,
                          refused_reason="empty input vector",
                          floor_alpha=floor_alpha)

    kept, excluded, unreadable = {}, {}, []
    for key, weight in weights.items():
        if weight <= 0 or key in protected:
            kept[key] = weight
            continue
        hold = effective_hold(key, alpha_reader, locked)
        if hold is None:
            # Our silence, not their failure.
            unreadable.append(key)
            kept[key] = weight
            continue
        if hold < floor_alpha:
            excluded[key] = f"alpha_hold {hold:.4f} < {floor_alpha:.4f}"
            continue
        kept[key] = weight

    # The miner pool is what the gate redistributes within; protected
    # destinations sit outside it and keep exactly what they had.
    protected_total = float(sum(w for k, w in weights.items()
                                if k in protected and w > 0))
    miner_pool = total - protected_total
    survivors = [w for k, w in kept.items() if w > 0 and k not in protected]

    if len(survivors) < MIN_SURVIVORS:
        return GateResult(weights=dict(weights), applied=False,
                          excluded=excluded,
                          refused_reason=(
                              f"would leave {len(survivors)} earner(s); "
                              f"refusing to empty the vector"),
                          floor_alpha=floor_alpha)

    kept_total = float(sum(survivors)) or 0.0
    if kept_total <= 0 or miner_pool <= 0:
        return GateResult(weights=dict(weights), applied=False,
                          excluded=excluded,
                          refused_reason="survivors carry no weight",
                          floor_alpha=floor_alpha)

    scale = miner_pool / kept_total
    renormalised = {
        k: (w if (k in protected or w <= 0) else w * scale)
        for k, w in kept.items()
    }
    return GateResult(weights=renormalised, excluded=excluded,
                      unreadable=unreadable, applied=True,
                      floor_alpha=floor_alpha)


def metagraph_alpha_reader(metagraph, by_uid: bool = True):
    """alpha held per miner, read off a metagraph.

    Returns None for anything the metagraph cannot answer for, so an
    out-of-range or missing entry is kept rather than confiscated.
    """
    def read(key):
        try:
            index = int(key) if by_uid else list(metagraph.hotkeys).index(key)
        except (TypeError, ValueError):
            return None
        for name in ("alpha_stake", "S", "total_stake", "stake"):
            values = getattr(metagraph, name, None)
            if values is None:
                continue
            try:
                return float(values[index])
            except (IndexError, TypeError, ValueError):
                continue
        return None
    return read
