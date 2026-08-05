"""Closing the loop between a receipt and the baskets it scored.

The receipt proves the scores follow from the outcomes and the predictions.
It does NOT, by itself, prove those episodes were the ones the subnet
actually revealed — that binding lives in the basket manifest, on the other
side of the repository boundary. This module is that check.

THE MAPPING IS PER HORIZON, NOT PER DAY
    It reads naturally as "a settle day's receipt covers that day's basket".
    It does not. An episode settles at

        action_window_end + 1 + horizon_days + 7-day settling window

    so a receipt for settle day D holds 7-day entries from the basket of
    D-15, 14-day entries from D-22 and 28-day entries from D-36 — three
    different baskets in one document. A check written against "the day's
    basket" would compare the wrong sets and either pass vacuously or fail
    on healthy data.

    Deriving each entry's basket day from its own horizon is strictly better
    than the simpler version: it validates the settle clock at the same time.
    An entry whose derived day has no manifest is either a clock bug or a
    basket that was never built, and both are worth knowing.

WHAT COUNTS AS A FAILURE
    Scored but NOT in its basket's manifest — we scored an episode the
    subnet did not reveal. That is the property this check exists to defend
    and the only condition it calls a violation.

    In the manifest but NOT scored is NORMAL and is not reported as a fault:
    censored horizons are dropped by design, and a miner who submitted
    nothing produces no entry. Treating absence as failure would flag every
    healthy day.

    A manifest that cannot be read is UNCHECKED, never "passed". The whole
    point is a claim about episodes we did not choose ourselves; quietly
    downgrading an unavailable manifest to a pass would leave the strongest
    claim in the feature resting on nothing.

The manifest source is injected. Where manifests get served from is an open
decision, and this check works the same against any of the options.

Pure module: no I/O, no clock.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import date, timedelta

# settle = action_window_end + 1 + horizon + 7  →  the offset back is 8 + horizon
SETTLE_OFFSET_DAYS = 8


def basket_day_for(finalized_on: date, horizon_days: int) -> date:
    """The basket date an entry came from, derived from its own horizon."""
    return finalized_on - timedelta(days=SETTLE_OFFSET_DAYS + int(horizon_days))


def episodes_by_basket_day(entries: Iterable[Mapping]) -> dict[date, set[str]]:
    """Group a receipt's scored episode ids by the basket day they came from."""
    out: dict[date, set[str]] = {}
    for entry in entries:
        raw_day = entry.get("finalized_on")
        episode = entry.get("episode_id")
        horizon = entry.get("horizon_days")
        if not raw_day or episode is None or horizon is None:
            continue
        day = basket_day_for(date.fromisoformat(str(raw_day)), int(horizon))
        out.setdefault(day, set()).add(str(episode))
    return out


def check_receipt(
    receipt: Mapping,
    manifest_for: Callable[[date], Iterable[str] | None],
) -> dict:
    """Every scored episode must appear in its own basket's manifest.

    `manifest_for(day)` returns that basket's episode ids, or None when the
    manifest cannot be read — which is reported as unchecked, not as a pass.
    """
    grouped = episodes_by_basket_day(receipt.get("entries", ()))

    baskets: list[dict] = []
    violations: list[dict] = []
    unchecked: list[str] = []
    scored_total = 0
    checked_total = 0

    for day in sorted(grouped):
        scored = grouped[day]
        scored_total += len(scored)
        manifest = manifest_for(day)
        if manifest is None:
            unchecked.append(str(day))
            baskets.append({"basket_day": str(day), "scored": len(scored),
                            "status": "unchecked_manifest_unavailable"})
            continue

        known = {str(e) for e in manifest}
        missing = sorted(scored - known)
        checked_total += len(scored)
        baskets.append({
            "basket_day": str(day),
            "scored": len(scored),
            "in_manifest": len(scored) - len(missing),
            # Present in the basket but unscored is expected — censoring and
            # non-submission both produce it — so it is reported, not faulted.
            "unscored_in_basket": len(known - scored),
            "status": "ok" if not missing else "scored_episodes_not_in_basket",
        })
        if missing:
            violations.append({"basket_day": str(day), "episode_ids": missing})

    return {
        "ok": not violations and not unchecked,
        "checked": not unchecked,
        "scored_episodes": scored_total,
        "verified_episodes": checked_total,
        "baskets": baskets,
        "violations": violations,
        "unchecked_days": unchecked,
    }
