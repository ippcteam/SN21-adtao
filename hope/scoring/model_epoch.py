"""Which model a hotkey is running, and since when — for the standing.

Rule amendment 2026-09-14 ("current model, current form"): a hotkey that
replaces its model is ranked on the model that is running. Entries predicted
by the PREVIOUS model count at a fraction of their weight once the current
model carries enough scored evidence of its own; before that they count in
full, so a fresh commit cannot shed a bad month before it has shown anything.

The boundary is the FIRST BASKET DAY on which the hotkey's current digest
ran, read from the shadow ledger (the record of which image produced which
day's predictions): every prediction from that day on came from the current
model, every earlier one from a model it replaced. The map is written by the
executor each shadow day and published in the allocation audit
(`standing_method.model_since`) so the discount is recomputable from public
documents: the receipts give every entry's prediction day, the audit gives
every hotkey's boundary.

Not the intake gate's verdict date: the registry stamps every active model
with the day it is being run (found in the first dry run, 14 September 2026,
when every boundary read "today" and nothing was discounted).
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import date, timedelta

from hope.scoring.episode_average import ScoredEpisode

MODEL_SINCE_FILE = "model_since.json"


def model_since_path(root: str) -> str:
    return os.path.join(root, MODEL_SINCE_FILE)


# The boundary is the LATEST model change, but a change counts only if it
# comes at least MODEL_CHANGE_MIN_GAP_DAYS after the previous counted change:
# re-committing every few days after a poor stretch cannot keep restarting
# the discount, while a genuine improvement every week or two is ranked on
# the model actually running. 0 = every change counts. Judged inside the
# standing window plus the gap.
MODEL_SINCE_WINDOW_DAYS = 42
MODEL_CHANGE_MIN_GAP_DAYS = 7
MODEL_CHANGE_MIN_GAP_ENV = "SN21_MODEL_CHANGE_MIN_GAP_DAYS"


def model_change_min_gap_days(environ=os.environ) -> int:
    try:
        v = int((environ.get(MODEL_CHANGE_MIN_GAP_ENV) or "").strip())
        return v if v >= 0 else MODEL_CHANGE_MIN_GAP_DAYS
    except (TypeError, ValueError):
        return MODEL_CHANGE_MIN_GAP_DAYS


def _digest_on(root: str, day: str, hotkey: str) -> str | None:
    """The image digest that produced `hotkey`'s predictions on shadow day
    `day` (the operative, last record), or None if it did not run."""
    from hope.backtest.shadow import last_record
    rec = last_record(root, day, hotkey)
    if not rec:
        return None
    digest = rec.get("image_digest")
    return str(digest) if digest else None


def model_since_from_shadow(root: str, previous: Mapping | None = None,
                            window_days: int = MODEL_SINCE_WINDOW_DAYS,
                            as_of: date | None = None,
                            min_gap_days: int | None = None) -> dict:
    """{hotkey: {"digest", "since", "changes"}} from the shadow ledger.

    For every hotkey that ran on the latest shadow day: the digest it ran,
    and its boundary — the LATEST basket day on which its digest differed
    from the day before, counting only changes that come at least
    `min_gap_days` after the previously counted change (the first change in
    the span always counts). A hotkey whose digest never changed in the span
    has no boundary inside it (`since` is the first day of its run) and
    nothing to discount. `changes` counts the changes seen. A hotkey that did
    not run on the latest day keeps its `previous` entry, if any.
    """
    from hope.backtest.shadow import shadow_days
    if min_gap_days is None:
        min_gap_days = model_change_min_gap_days()
    days = shadow_days(root)
    if not days:
        return dict(previous or {})
    latest = days[-1]
    as_of = as_of or date.fromisoformat(latest)
    cutoff = (as_of - timedelta(days=window_days + min_gap_days)).isoformat()
    in_span = [d for d in days if d >= cutoff]
    latest_dir = os.path.join(root, "shadow", latest)
    hotkeys = sorted(fn[:-6] for fn in os.listdir(latest_dir) if fn.endswith(".jsonl"))
    out: dict = dict(previous or {})
    for hk in hotkeys:
        current = _digest_on(root, latest, hk)
        if not current:
            continue
        seq = [(d, _digest_on(root, d, hk)) for d in in_span]
        seq = [(d, g) for d, g in seq if g]
        changes: list[str] = [d1 for (d0, g0), (d1, g1) in zip(seq, seq[1:]) if g1 != g0]
        counted: list[str] = []
        for d in changes:
            if not counted or (date.fromisoformat(d) - date.fromisoformat(counted[-1])).days >= min_gap_days:
                counted.append(d)
        since = counted[-1] if counted else (seq[0][0] if seq else latest)
        out[hk] = {"digest": current, "since": since, "changes": len(changes)}
    return out


def load_model_since_raw(root: str) -> dict:
    try:
        with open(model_since_path(root)) as f:
            raw = json.load(f)
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def write_model_since(root: str, mapping: Mapping) -> int:
    """Persist {hotkey: {"digest", "since"}}. Atomic; returns how many
    hotkeys were written."""
    out = {str(hk): {"digest": str((rec or {}).get("digest") or ""),
                     "since": str((rec or {}).get("since"))[:10],
                     "changes": int((rec or {}).get("changes") or 0)}
           for hk, rec in mapping.items() if isinstance(rec, dict) and rec.get("since")}
    os.makedirs(root, exist_ok=True)
    tmp = model_since_path(root) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, indent=1, sort_keys=True)
    os.replace(tmp, model_since_path(root))
    return len(out)


def load_model_since(root: str) -> dict[str, date]:
    """{hotkey: date the current model was admitted}. Empty when the file is
    missing or unreadable — then no entry is discounted, which is the
    fail-open direction: a boundary we do not know must not shrink anybody's
    evidence."""
    try:
        with open(model_since_path(root)) as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return {}
    out: dict[str, date] = {}
    for hk, rec in (raw or {}).items():
        since = rec.get("since") if isinstance(rec, dict) else rec
        try:
            out[str(hk)] = date.fromisoformat(str(since)[:10])
        except (TypeError, ValueError):
            continue
    return out


def apply_previous_model_discount(
    entries: Mapping[str, list[ScoredEpisode]],
    model_since: Mapping[str, date],
    as_of: date,
    window_days: int,
    weight: float,
    threshold_mass: float,
) -> tuple[dict[str, list[ScoredEpisode]], dict]:
    """Discount the entries predicted before a hotkey's current model, in
    proportion to how much evidence the current model has shown. Pure.

    The discount is a factor on the entry's contribution to the MEAN
    (`ScoredEpisode.discount`); its `weight` — the evidence mass the prior,
    the placement floor and tenure count — is untouched. So a switch to an
    equally good model leaves the standing unchanged, a better one lifts it
    as its rows arrive, a worse one lowers it, and switching as such costs
    nothing (miner review, 16 September 2026).

    The factor falls linearly from 1.0 at zero current-model mass to `weight`
    at `threshold_mass` (and stays there above it): a commit that has shown
    nothing sheds nothing, a model with half the floor's evidence is half-way
    there, and there is no day on which a standing jumps. Miner feedback of
    14 September 2026 on the first cut, which held the old entries at full
    weight until the threshold and then cut them in one step.

    An entry's prediction day is `predicted_on` (set by the receipt loader);
    an entry without one is left alone. Hotkeys
    absent from `model_since` are left alone. Returns the new mapping and a
    stats block for the audit, with the factor applied per discounted hotkey.
    """
    out: dict[str, list[ScoredEpisode]] = {}
    discounted_hotkeys: list[str] = []
    factors: dict[str, float] = {}
    discounted_entries = 0
    for hk, eps in entries.items():
        since = model_since.get(hk)
        if since is None:
            out[hk] = list(eps)
            continue
        current_mass = 0.0
        previous: list[int] = []
        for i, ep in enumerate(eps):
            pday = ep.predicted_on or ep.aged_from
            if pday is None:
                continue                      # unknown prediction day: left alone
            age = (as_of - ep.age_day).days
            if age < 0 or age > window_days:
                continue
            if pday >= since:
                current_mass += ep.weight
            else:
                previous.append(i)
        progress = (min(1.0, current_mass / threshold_mass)
                    if threshold_mass > 0 else 1.0)
        factor = 1.0 - (1.0 - weight) * progress
        if not previous or factor >= 1.0:
            out[hk] = list(eps)
            continue
        scaled = list(eps)
        for i in previous:
            ep = eps[i]
            scaled[i] = ScoredEpisode(score=ep.score, scored_on=ep.scored_on,
                                      weight=ep.weight,
                                      predicted_on=ep.predicted_on,
                                      aged_from=ep.aged_from,
                                      discount=ep.discount * factor)
        out[hk] = scaled
        discounted_hotkeys.append(hk)
        factors[hk] = round(factor, 4)
        discounted_entries += len(previous)
    return out, {"weight": weight, "threshold_mass": threshold_mass,
                 "shape": "linear from 1.0 at zero current-model mass to "
                          "`weight` at `threshold_mass`",
                 "hotkeys_discounted": sorted(discounted_hotkeys),
                 "factor": {hk: factors[hk] for hk in sorted(factors)},
                 "entries_discounted": discounted_entries}
