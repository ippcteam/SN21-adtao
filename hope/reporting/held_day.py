"""Which allocation a day's report describes.

A day that publishes no new vector (gated, or otherwise held) keeps the
previous vector live on chain, so its report shows the miners that vector
pays. The reasons printed against the unpaid rows must come from the SAME
allocation: the controls are recomputed every day on fresh standings, and on
a held day today's groupings can differ from the ones that produced the
live vector. Pairing today's reasons with yesterday's paid set published rows
that were both "Funded" and "excluded — another hotkey earns instead"
(6 Sept 2026, five of twenty).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class LiveAllocation:
    earning_set: frozenset            # hotkeys the live vector pays
    collapse_audit: dict              # the audit that produced that vector
    held: bool                        # True when taken from an older day
    source_day: str | None            # the day the vector was published
    notes: tuple = field(default_factory=tuple)


def _paid(intent: dict) -> frozenset:
    weights = (intent or {}).get("weights") or {}
    out = set()
    for hk, w in weights.items():
        try:
            if float(w) > 0:
                out.add(str(hk))
        except (TypeError, ValueError):
            continue
    return frozenset(out)


def live_allocation(day: str, intent: dict, older: list[tuple[str, dict]]) -> LiveAllocation:
    """The allocation a report for `day` describes.

    `intent` is the day's own intended-weights document; `older` lists
    (day, intent) pairs for earlier days, newest first. The day's own vector
    wins when it pays anybody; otherwise the newest earlier vector that pays
    somebody is live, and its audit travels with it. With nothing to fall
    back on the report shows nobody funded and the day's own audit. Pure.
    """
    own = _paid(intent)
    own_audit = dict((intent or {}).get("collapse_audit") or {})
    if own:
        return LiveAllocation(own, own_audit, False, str(day))
    for prior_day, prior in older:
        if str(prior_day) >= str(day):
            continue
        paid = _paid(prior)
        if paid:
            return LiveAllocation(
                paid, dict((prior or {}).get("collapse_audit") or {}), True, str(prior_day),
                notes=(f"day held — reporting the live vector from {prior_day} ({len(paid)} earning)",))
    return LiveAllocation(frozenset(), own_audit, False, None)
