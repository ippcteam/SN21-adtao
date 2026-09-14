"""Which model a hotkey is running, and since when — for the standing.

Rule amendment 2026-09-14 ("current model, current form"): a hotkey that
replaces its model is ranked on the model that is running. Entries predicted
by the PREVIOUS model count at a fraction of their weight once the current
model carries enough scored evidence of its own; before that they count in
full, so a fresh commit cannot shed a bad month before it has shown anything.

The boundary is the day the current digest was admitted (the intake gate's
verdict date): no prediction made on or after it can have come from the
model it replaced. The map is written by the executor each shadow day from
the registry it runs the models from, and published in the allocation audit
(`standing_method.model_since`) so the discount is recomputable from public
documents: the receipts give every entry's prediction day, the audit gives
every hotkey's boundary.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from datetime import date

from hope.scoring.episode_average import ScoredEpisode

MODEL_SINCE_FILE = "model_since.json"


def model_since_path(root: str) -> str:
    return os.path.join(root, MODEL_SINCE_FILE)


def write_model_since(root: str, models: Iterable) -> int:
    """Persist {hotkey: {"digest", "since"}} from the models the executor
    runs today (anything with `hotkey`, `image_digest`, `admitted_at`).
    Atomic; returns how many hotkeys were written."""
    out: dict = {}
    for m in models:
        hk = getattr(m, "hotkey", None)
        since = getattr(m, "admitted_at", None)
        if not hk or not since:
            continue
        out[str(hk)] = {"digest": str(getattr(m, "image_digest", "") or ""),
                        "since": str(since)[:10]}
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
    """Scale the weight of entries predicted before a hotkey's current model,
    in proportion to how much evidence the current model has shown. Pure.

    The factor falls linearly from 1.0 at zero current-model mass to `weight`
    at `threshold_mass` (and stays there above it): a commit that has shown
    nothing sheds nothing, a model with half the floor's evidence is half-way
    there, and there is no day on which a standing jumps. Miner feedback of
    14 September 2026 on the first cut, which held the old entries at full
    weight until the threshold and then cut them in one step.

    An entry's prediction day is `aged_from` (set by the receipt loader under
    the prediction-day basis); an entry without one is left alone. Hotkeys
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
            day = ep.aged_from or ep.scored_on
            age = (as_of - day).days
            if age < 0 or age > window_days:
                continue
            if day >= since:
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
                                      weight=ep.weight * factor,
                                      aged_from=ep.aged_from)
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
