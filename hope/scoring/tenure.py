"""Earning-set tenure — payment begins after a track record, not before.

A standing is an average, and an average over one settled day carries no
evidence of sustained skill: a miner can hold a top seat on a single good
day's mean while miners with weeks of full coverage rank below them. The
absence machinery corrects this over time (every further settle day dilutes
a small sample), but the curve pays TODAY. This gate closes that window:
a hotkey enters the paid curve only once it has scored entries on at least
`min_days` distinct settle days.

WHAT IT DOES NOT DO — same limits as the one-payer suppression:
  * standings are untouched (scores are facts, and they accrue from day one);
  * the container keeps being executed daily — tenure accrues by being
    scored, so entry into the curve is earned by simply keeping a model
    running while its horizons mature;
  * promotion is untouched — the margin compares models, not seats;
  * absence-penalty entries DO count as scored days (they live in the same
    standing ledger the day counts come from) — and that is safe: a miner
    sitting out accrues days only at full-weight zero scores, so aging into
    the curve on penalties arrives with a standing too low to earn.

DIRECTION OF ERROR — toward paying, like the participation gate:
  * unreadable or missing receipt history gates NOBODY;
  * if gating would empty the curve entirely (a young subnet, a repaired
    ledger), the gate stands down for that day and the audit says so. A gate
    that wrongly pays costs a share for a day; a gate that wrongly zeroes
    the whole vector strips everyone at once.

Pure module: no I/O, no chain calls. The receipt read lives in the caller.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

TENURE_FLAG_ENV = "SN21_TENURE_GATE"
MIN_DAYS_ENV = "SN21_TENURE_MIN_DAYS"
DEFAULT_MIN_DAYS = 7


def tenure_gate_enabled(environ) -> bool:
    return (environ.get(TENURE_FLAG_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on")


def tenure_min_days(environ) -> int:
    """Unset, blank or malformed keeps the default. A deploy typo must never
    silently loosen or tighten who is paid."""
    raw = (environ.get(MIN_DAYS_ENV) or "").strip()
    if not raw:
        return DEFAULT_MIN_DAYS
    try:
        val = int(raw)
    except ValueError:
        return DEFAULT_MIN_DAYS
    return val if val >= 1 else DEFAULT_MIN_DAYS


def scored_days_by_hotkey(
    day_entries: Iterable[tuple[str, Iterable[str]]],
) -> dict[str, int]:
    """Count distinct settle days on which each hotkey has a scored entry.

    `day_entries` is (day, hotkeys-with-entries-that-day) pairs; days must
    already be deduplicated by the caller (one receipt per day makes that
    natural). Order does not matter.
    """
    days_of: dict[str, set[str]] = {}
    for day, hotkeys in day_entries:
        for hk in hotkeys:
            days_of.setdefault(hk, set()).add(day)
    return {hk: len(days) for hk, days in days_of.items()}


def short_tenure_hotkeys(
    scored_days: Mapping[str, int],
    candidates: Iterable[str],
    min_days: int,
    exempt_hotkeys: frozenset = frozenset(),
) -> frozenset[str]:
    """The subset of `candidates` that has not yet reached `min_days`.

    A candidate absent from `scored_days` has zero scored days and is gated —
    that is the point of the rule, not an edge case. Exempt hotkeys (the
    house/reference model) are never gated.
    """
    out = set()
    for hk in candidates:
        if hk in exempt_hotkeys:
            continue
        if scored_days.get(hk, 0) < min_days:
            out.add(hk)
    return frozenset(out)
