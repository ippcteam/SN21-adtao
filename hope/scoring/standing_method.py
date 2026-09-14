"""Standing method — which entries the standing is computed from.

Rule amendment of 2026-09-04 (published before enabled, applied forward):

  SN21_STANDING_MODE = absolute          (default) each entry is the score
                                          itself, read from the standing
                                          ledger — the rule in force since
                                          launch, byte-for-byte.
  SN21_STANDING_MODE = episode_relative   each entry is the score MINUS the
                                          mean score of every miner scored on
                                          the same (episode, horizon), read
                                          from the published receipts.

WHY RELATIVE. The absolute mean rewards the mix of changes a miner happened
to be scored on as much as its accuracy: a model that is strong on the
commonest change type and answers everything else at the field level looks
best on a board whose evidence is mostly that type. Measured against the
field on the SAME change, difficulty and type mix cancel: the standing says
how far above or below everyone else a model was on identical episodes.

WHY FROM RECEIPTS. A ledger entry carries score, weight and day only. The
receipt for a settle day carries every miner's score on every (episode,
horizon) that settled that day — the field mean is one line of arithmetic
over the same public document a miner verifies with, so the relative
standing is reproducible from the mirror alone. Nothing in the ledger
changes; the absolute mode still reads it.

ABSENCE. An uncovered episode enters at the published floor (0.0). In
relative terms that is the floor minus the field: F below the field, where
F is the mean of the field means inside the window. Cancellations apply
exactly as in the ledger path (up to `missed` entries per day and hotkey).

Pure except for reading the ledger directory; parsing a window of receipts
is memoised per (root, as_of, window, files) because several steps of one
run read the standings.
"""
from __future__ import annotations

import json
import os
from collections import defaultdict
from collections.abc import Mapping
from datetime import date, timedelta

from hope.scoring import standing_ledger
from hope.scoring.daily_score_flow import horizon_entry_weight
from hope.scoring.episode_average import (
    DEFAULT_WINDOW_DAYS,
    settle_lag_days,
    ScoredEpisode,
    age_basis_effective_from,
    age_basis_in_force,
    amendment_in_force,
    half_life_in_force,
    prediction_basis_in_force,
    previous_model_threshold,
    previous_model_weight,
    prior_mass_in_force,
    window_from_env,
    window_in_force,
)

MODE_ENV = "SN21_STANDING_MODE"
MODE_ABSOLUTE = "absolute"
MODE_EPISODE_RELATIVE = "episode_relative"
MODES = (MODE_ABSOLUTE, MODE_EPISODE_RELATIVE)

# The published effective date. The rule is announced first and applied
# from this day forward; with the env set ahead of time the switch happens
# on the date the miners were told, not on the day an operator edits a
# variable. Unset = apply as soon as the mode is set.
EFFECTIVE_FROM_ENV = "SN21_STANDING_EFFECTIVE_FROM"


def effective_from(environ=os.environ) -> date | None:
    raw = (environ.get(EFFECTIVE_FROM_ENV) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None

PENALTY_ENTRY_WEIGHT = 1.0      # mirrors absence_penalty.PENALTY_ENTRY_WEIGHT
PENALTY_SCORE = 0.0             # the published floor

# Champion promotion margin under a relative standing. The published test
# "leads by at least 5% (relative)" has no meaning for a standing that sits
# near zero and can be negative, so the relative mode publishes an ABSOLUTE
# lead instead (challenger >= champion + margin). Unset = the relative test.
PROMOTION_MARGIN_ABS_ENV = "SN21_PROMOTION_MARGIN_ABS"


# The weight curve's score threshold. Published as 0.0 for the absolute
# standing ("at threshold or above earns"). A RELATIVE standing sits at 0.0
# when a miner matches the field, so reading the same 0.0 there would pay
# only miners above the field and cut the earning set below the published
# twenty. Under the relative mode the threshold is therefore not applied
# (-1.0, below any achievable relative standing): the top twenty by standing
# earn the published shares exactly as the rewards doc states. Explicit env
# wins in either mode.
CURVE_THRESHOLD_ENV = "SN21_CURVE_SCORE_THRESHOLD"
RELATIVE_CURVE_THRESHOLD = -1.0


# The weight curve's tail. Review finding of 7 Sept 2026 (SN21_CURVE_TAIL_REVIEW):
# under the published half-tail rank 1 takes 52.6% of a 20-miner field, ranks
# 11-20 share 0.08%, and ranks 17-20 round to ZERO on the chain's 16-bit
# weights — a seat that pays nothing while the alpha hold it requires steps to
# 1,000. Governance decision (9 Sept 2026): move the tail to 0.8. Same top
# three raw shares, same cap; every listed earner then pays on chain.
#
# Wired like the standing method: the value is announced first and applied
# from a published date forward. Unset = the published 0.5, so deploying this
# code changes nothing until the operator sets the variable; with the date set
# ahead of time the switch lands on the day miners were told, not on the day
# someone edits an environment variable. Out-of-range or unparseable values
# fall back to the published tail rather than paying an accidental curve.
CURVE_TAIL_DECAY_ENV = "SN21_CURVE_TAIL_DECAY"
CURVE_TAIL_EFFECTIVE_FROM_ENV = "SN21_CURVE_TAIL_EFFECTIVE_FROM"
PUBLISHED_TAIL_DECAY = 0.5


def curve_tail_effective_from(environ=os.environ) -> date | None:
    raw = (environ.get(CURVE_TAIL_EFFECTIVE_FROM_ENV) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        return None


def curve_tail_configured(environ=os.environ) -> float | None:
    """The tail the operator set, if it is a usable one; None otherwise."""
    raw = (environ.get(CURVE_TAIL_DECAY_ENV) or "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not (0.0 < value <= 1.0):
        return None
    return value


def curve_tail_decay(environ=os.environ, day: date | None = None) -> float:
    """The tail IN FORCE on `day` (today when None): the configured value once
    its effective date has arrived, else the published 0.5. Pure."""
    configured = curve_tail_configured(environ)
    if configured is None:
        return PUBLISHED_TAIL_DECAY
    start = curve_tail_effective_from(environ)
    if start is not None and (day or date.today()) < start:
        return PUBLISHED_TAIL_DECAY
    return configured


def curve_params_in_force(environ=os.environ, day: date | None = None):
    """The CurveParams the allocation pays with on `day`: published shares
    and cap, the threshold that follows the standing method, and the tail in
    force. The one place the curve is assembled from the environment."""
    from hope.scoring.weight_curve import CurveParams

    return CurveParams(score_threshold=curve_score_threshold(environ, day),
                       tail_decay=curve_tail_decay(environ, day))


def curve_score_threshold(environ=os.environ, day: date | None = None) -> float:
    raw = (environ.get(CURVE_THRESHOLD_ENV) or "").strip()
    if raw:
        try:
            return float(raw)
        except ValueError:
            pass
    return RELATIVE_CURVE_THRESHOLD if relative_enabled(environ, day) else 0.0


def promotion_margin_abs(environ=os.environ, day: date | None = None) -> float | None:
    """The absolute champion margin, in force only with the relative
    standing (it is the relative standing that makes the 5% test
    meaningless); before the effective date the published relative test
    applies unchanged."""
    if not relative_enabled(environ, day):
        return None
    try:
        v = float((environ.get(PROMOTION_MARGIN_ABS_ENV) or "").strip())
        return v if v > 0 else None
    except (TypeError, ValueError):
        return None


def standing_mode(environ=os.environ, day: date | None = None) -> str:
    """The mode IN FORCE on `day` (today when None): the configured mode,
    unless the effective date has not arrived, in which case absolute."""
    return MODE_EPISODE_RELATIVE if amendment_in_force(environ, day) else MODE_ABSOLUTE


def relative_enabled(environ=os.environ, day: date | None = None) -> bool:
    return standing_mode(environ, day) == MODE_EPISODE_RELATIVE


def method_params(environ=os.environ, day: date | None = None) -> dict:
    """The published parameters in force on `day`, for audits and reports."""
    return {
        "mode": standing_mode(environ, day),
        "configured_mode": (environ.get(MODE_ENV) or "").strip().lower() or MODE_ABSOLUTE,
        "effective_from": (effective_from(environ).isoformat() if effective_from(environ) else None),
        "half_life_days": half_life_in_force(environ, day),
        "prior_mass": prior_mass_in_force(environ, day),
        "window_days": window_in_force(environ, day),
        "promotion_margin_abs": promotion_margin_abs(environ, day),
        "curve_score_threshold": curve_score_threshold(environ, day),
        # The curve that paid this day, so the vector is recomputable from
        # the audit alone across a tail change.
        "curve_tail_decay": curve_tail_decay(environ, day),
        "curve_tail_configured": curve_tail_configured(environ),
        "curve_tail_effective_from": (curve_tail_effective_from(environ).isoformat()
                                      if curve_tail_effective_from(environ) else None),
        # Rule amendment 2026-09-14: which day an entry's age is measured
        # from, and how a replaced model's entries are weighted once the
        # current one has evidence. Settle-day values before the effective
        # date, so a reader of the audit knows which rule ranked the day.
        "age_basis": age_basis_in_force(environ, day),
        "age_basis_effective_from": (age_basis_effective_from(environ).isoformat()
                                     if age_basis_effective_from(environ) else None),
        "previous_model_weight": (previous_model_weight(environ)
                                  if prediction_basis_in_force(environ, day) else None),
        "previous_model_threshold": (previous_model_threshold(environ)
                                     if prediction_basis_in_force(environ, day) else None),
    }


# ---- receipts -> relative entries -------------------------------------------

_CACHE: dict = {}


def _receipt_files(root: str, as_of: date, window_days: int) -> list[tuple[date, str]]:
    d = os.path.join(root, "receipts")
    if not os.path.isdir(d):
        return []
    cutoff = as_of - timedelta(days=window_days)
    out = []
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json") or fn.startswith("_"):
            continue
        try:
            day = date.fromisoformat(fn[:-5])
        except ValueError:
            continue
        if cutoff <= day <= as_of:
            out.append((day, os.path.join(d, fn)))
    return out


def _entries_of(path: str) -> list[dict]:
    with open(path) as f:
        env = json.load(f)
    doc = env.get("document", env) if isinstance(env, dict) else {}
    return list((doc.get("metrics") or {}).get("entries") or [])


def field_means(entries: list[dict]) -> dict[tuple[str, int], float]:
    """Mean score per (episode, horizon) over every miner scored on it."""
    acc: dict[tuple[str, int], list[float]] = defaultdict(list)
    for e in entries:
        try:
            acc[(str(e["episode_id"]), int(e["horizon_days"]))].append(float(e["score"]))
        except (KeyError, TypeError, ValueError):
            continue
    return {k: sum(v) / len(v) for k, v in acc.items() if v}


def basket_days_from_tkeys(root: str) -> dict[str, date]:
    """episode_id -> basket day, from the per-basket map files the executor
    writes at resolve time (<root>/tkeys/BD-<day>.json). Exact where present;
    empty when the directory is missing, in which case the settle schedule
    derives the day."""
    d = os.path.join(root, "tkeys")
    out: dict[str, date] = {}
    if not os.path.isdir(d):
        return out
    for fn in sorted(os.listdir(d)):
        if not (fn.startswith("BD-") and fn.endswith(".json")):
            continue
        try:
            day = date.fromisoformat(fn[3:-5])
            with open(os.path.join(d, fn)) as f:
                m = json.load(f)
        except (ValueError, OSError):
            continue
        if isinstance(m, dict):
            for eid in m:
                out[str(eid)] = day
    return out


def _tkeys_signature(root: str):
    d = os.path.join(root, "tkeys")
    try:
        return (d, os.path.getmtime(d), len(os.listdir(d)))
    except OSError:
        return (d, None, 0)


def predicted_on_of(entry: dict, scored_on: date,
                    basket_days: Mapping | None = None,
                    environ=os.environ) -> date:
    """The day the entry's prediction was made. In order: the receipt's own
    `predicted_on` (receipts from the 2026-09-14 amendment on); the basket
    the episode was released in, where the reader holds that map; else the
    settle schedule — finalized_on = basket day + 1 + horizon + settling
    window — so prediction day = finalized_on − horizon − (1 + settling
    window), three days with the two-day window the platform runs."""
    raw = entry.get("predicted_on")
    if raw:
        try:
            return date.fromisoformat(str(raw)[:10])
        except ValueError:
            pass
    if basket_days:
        day = basket_days.get(str(entry.get("episode_id")))
        if isinstance(day, date):
            return day
    try:
        horizon = int(entry.get("horizon_days") or 0)
    except (TypeError, ValueError):
        horizon = 0
    return scored_on - timedelta(days=horizon + settle_lag_days(environ))


def load_relative_entries(root: str, as_of: date,
                          window_days: int | None = None,
                          environ=os.environ,
                          model_since: dict | None = None,
                          relative: bool = True,
                          stats: dict | None = None,
                          basket_days: Mapping | None = None,
                          ) -> dict[str, list[ScoredEpisode]]:
    """hotkey -> ScoredEpisodes in the window, from receipts plus the
    absence-penalty log net of cancellations. Relative to the field on the
    same (episode, horizon) by default; `relative=False` keeps the absolute
    scores (the board's headline accuracy) on the same entries, dating and
    weights.

    Under the prediction-day basis (rule amendment 2026-09-14) each entry is
    aged from the day it was predicted (`aged_from`), the window is applied
    to that day, and a hotkey's previous-model entries are discounted once
    its current model carries the threshold mass (hope.scoring.model_epoch;
    `model_since` = {hotkey: date the current model was admitted}).
    """
    if window_days is None:
        window_days = window_in_force(environ, as_of)
    by_prediction_day = prediction_basis_in_force(environ, as_of)
    if by_prediction_day and basket_days is None:
        basket_days = basket_days_from_tkeys(root)
    files = _receipt_files(root, as_of, window_days)
    since_key = tuple(sorted((hk, d.isoformat()) for hk, d in (model_since or {}).items()))
    sig = (root, as_of.isoformat(), window_days, by_prediction_day, relative,
           tuple((p, os.path.getmtime(p)) for _, p in files),
           _penalty_signature(root), since_key,
           _tkeys_signature(root) if by_prediction_day else None,
           settle_lag_days(environ))
    hit = _CACHE.get("k")
    if hit and hit[0] == sig:
        if stats is not None:
            stats.update(hit[2])
        return {hk: list(v) for hk, v in hit[1].items()}

    out: dict[str, list[ScoredEpisode]] = defaultdict(list)
    all_means: list[float] = []
    cutoff = as_of - timedelta(days=window_days)
    for day, path in files:
        entries = _entries_of(path)
        means = field_means(entries)
        all_means.extend(means.values())
        for e in entries:
            try:
                key = (str(e["episode_id"]), int(e["horizon_days"]))
                score = float(e["score"])
                miner = str(e["miner"])
            except (KeyError, TypeError, ValueError):
                continue
            scored_on = day
            fo = e.get("finalized_on")
            if fo:
                try:
                    scored_on = date.fromisoformat(str(fo)[:10])
                except ValueError:
                    pass
            aged_from = (predicted_on_of(e, scored_on, basket_days, environ)
                         if by_prediction_day else None)
            age_day = aged_from or scored_on
            if age_day < cutoff or age_day > as_of:
                continue
            # The receipt's own entry weight when it carries one (horizon
            # blend × episode weight, from the resolution gate); the plain
            # horizon share for receipts published before that field existed.
            try:
                w = float(e.get("weight"))
            except (TypeError, ValueError):
                w = 0.0
            if not w > 0:
                w = horizon_entry_weight(key[1])
            out[miner].append(ScoredEpisode(
                score=(score - means[key]) if relative else score,
                scored_on=scored_on, weight=w, aged_from=aged_from))
    field_level = (sum(all_means) / len(all_means)) if all_means else 0.0
    absence_value = (PENALTY_SCORE - field_level) if relative else PENALTY_SCORE
    # An uncovered basket day is dated by that day under both bases: the
    # prediction that was not made would have been made then.
    for hk, day, missed in _net_penalties(root, as_of, window_days):
        out[hk].extend(ScoredEpisode(score=absence_value, scored_on=day,
                                     weight=PENALTY_ENTRY_WEIGHT,
                                     aged_from=(day if by_prediction_day else None))
                       for _ in range(missed))
    result = {hk: v for hk, v in out.items() if v}
    info: dict = {"age_basis": age_basis_in_force(environ, as_of)}
    if by_prediction_day and model_since:
        from hope.scoring.model_epoch import apply_previous_model_discount
        result, discount = apply_previous_model_discount(
            result, model_since, as_of, window_days,
            previous_model_weight(environ), previous_model_threshold(environ))
        info["previous_model"] = discount
    _CACHE["k"] = (sig, {hk: list(v) for hk, v in result.items()}, info)
    if stats is not None:
        stats.update(info)
    return result


def _penalty_path(root: str) -> str:
    return os.path.join(standing_ledger.standing_dir(root), "_absence_penalties.jsonl")


def _penalty_signature(root: str):
    p = _penalty_path(root)
    c = standing_ledger._cancellations_path(root)
    return tuple((q, os.path.getmtime(q)) for q in (p, c) if os.path.exists(q))


def _net_penalties(root: str, as_of: date, window_days: int):
    """(hotkey, day, missed) per penalty record inside the window, net of
    cancellations for that (day, hotkey)."""
    p = _penalty_path(root)
    if not os.path.exists(p):
        return []
    cancelled: dict[tuple[str, str], int] = defaultdict(int)
    for c in standing_ledger.load_cancellations(root):
        cancelled[(str(c.get("day")), str(c.get("hotkey")))] += int(c.get("missed") or 0)
    cutoff = as_of - timedelta(days=window_days)
    out = []
    with open(p) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            try:
                day = date.fromisoformat(str(rec["day"]))
                hk = str(rec["hotkey"])
                missed = int(rec.get("missed") or 0)
            except (KeyError, TypeError, ValueError):
                continue
            if day < cutoff or day > as_of:
                continue
            key = (day.isoformat(), hk)
            take = min(missed, cancelled.get(key, 0))
            if take:
                cancelled[key] -= take
                missed -= take
            if missed > 0:
                out.append((hk, day, missed))
    return out


# Dry run of the prediction-day basis: compute what it WOULD rank, beside the
# rule in force, without applying it. Same pattern as the curve-tail review —
# the implementation runs on real days before its date is announced, so the
# switch, when it comes, changes nothing that has not already been seen.
PREVIEW_ENV = "SN21_STANDING_AGE_BASIS_PREVIEW"


def preview_enabled(environ=os.environ) -> bool:
    return (environ.get(PREVIEW_ENV) or "").strip().lower() in ("1", "true", "yes", "on")


def standing_preview(root: str, as_of: date, environ=os.environ,
                     model_since: dict | None = None,
                     placement_floor: float | None = None,
                     with_controls: bool = False,
                     current_alloc=None,
                     coldkey_of: dict | None = None,
                     alpha_of: dict | None = None,
                     alpha_floor: float = 0.0,
                     day_episode_volume: int = 0) -> dict:
    """The prediction-day standing computed as if in force today, beside the
    standing actually in force, per hotkey with a rank under each.

    `with_controls=True` runs the FULL allocation under both rules in dry-run
    form (allocation_from_ledger(persist=False): coldkey cap, duplicate and
    lineage controls, tenure, alpha hold, the curve) so the document also
    says who would be PAID under each — the comparison a miner actually
    wants. `current_alloc` reuses today's allocation when the caller already
    has it. Without controls it ranks placement-eligible standings only.

    Not a control: nothing here reaches weights, the audit or the report — it
    is written to the operator's store, and mirrored only when the operator
    publishes it.
    """
    from hope.scoring.episode_average import (
        AGE_BASIS_EFFECTIVE_FROM_ENV, AGE_BASIS_ENV, AGE_BASIS_PREDICTION,
        PLACEMENT_FLOOR_PREDICTIONS, episode_weighted_average,
        half_life_in_force, scored_prediction_count,
    )
    if placement_floor is None:
        placement_floor = PLACEMENT_FLOOR_PREDICTIONS
    forced = {k: v for k, v in dict(environ).items()
              if k != AGE_BASIS_EFFECTIVE_FROM_ENV}
    forced[AGE_BASIS_ENV] = AGE_BASIS_PREDICTION
    if model_since is None:
        from hope.scoring.model_epoch import load_model_since
        model_since = load_model_since(root)

    def params(env):
        return {"window_days": window_in_force(env, as_of),
                "half_life_days": half_life_in_force(env, as_of),
                "prior_mass": prior_mass_in_force(env, as_of)}

    def ranked_from_standings(vals):
        order = sorted(vals.items(), key=lambda kv: (-float(kv[1]), kv[0]))
        return {hk: {"relative": round(float(v), 6), "rank": i}
                for i, (hk, v) in enumerate(order, 1)}

    def ranked_pure(env, since):
        w = params(env)["window_days"]; hl = params(env)["half_life_days"]
        prior = params(env)["prior_mass"]
        stats: dict = {}
        entries = load_relative_entries(root, as_of, w, environ=env,
                                        model_since=since, stats=stats)
        vals = {}
        for hk, eps in entries.items():
            if scored_prediction_count(eps, as_of, w) < placement_floor:
                continue
            v = episode_weighted_average(eps, as_of, half_life_days=hl,
                                         window_days=w, prior_mass=prior)
            if v is not None:
                vals[hk] = v
        return ranked_from_standings(vals), stats, None

    def ranked_alloc(env, alloc):
        from hope.validator.daily_stream_weights import allocation_from_ledger
        # The day-volume hold ([D3]) withholds the vector on a thin day under
        # BOTH rules, which would make the paid comparison empty exactly when
        # it is wanted. The preview asks who would be paid if a vector were
        # set today, so the hold is not applied here; a held allocation the
        # caller passes in is recomputed the same way.
        if alloc is None or getattr(alloc, "gated", False):
            alloc = allocation_from_ledger(root, as_of, day_episode_volume,
                                           min_daily_episodes=0, environ=env,
                                           coldkey_of=coldkey_of, alpha_of=alpha_of,
                                           alpha_floor=alpha_floor, persist=False)
        stats = (alloc.collapse_audit.get("policies") or {}).get("standing_method") or {}
        pm = ((alloc.collapse_audit.get("policies") or {}).get("standing_method") or {}).get("previous_model")
        return (ranked_from_standings(alloc.standings),
                {"previous_model": pm} if pm else {}, alloc)

    if with_controls:
        current, _, alloc_now = ranked_alloc(environ, current_alloc)
        preview, preview_stats, alloc_new = ranked_alloc(forced, None)
        paid_now = sorted(hk for hk, w in (alloc_now.weights or {}).items() if w > 0)
        paid_new = sorted(hk for hk, w in (alloc_new.weights or {}).items() if w > 0)
        previous_model = ((alloc_new.collapse_audit.get("policies") or {})
                          .get("standing_method") or {}).get("previous_model")
    else:
        current, _, _ = ranked_pure(environ, None)
        preview, preview_stats, _ = ranked_pure(forced, model_since or None)
        paid_now = paid_new = None
        previous_model = preview_stats.get("previous_model")

    top_now = {hk for hk, r in current.items() if r["rank"] <= 20}
    top_new = {hk for hk, r in preview.items() if r["rank"] <= 20}
    rows = {}
    for hk in set(current) | set(preview):
        rows[hk] = {
            "current_rank": (current.get(hk) or {}).get("rank"),
            "current_relative": (current.get(hk) or {}).get("relative"),
            "preview_rank": (preview.get(hk) or {}).get("rank"),
            "preview_relative": (preview.get(hk) or {}).get("relative"),
            **({"paid_now": hk in set(paid_now), "paid_preview": hk in set(paid_new)}
               if paid_now is not None else {}),
        }
    summary = {
        "hotkeys_ranked_current": len(current),
        "hotkeys_ranked_preview": len(preview),
        "top20_seats_changed": len(top_new - top_now),
        "enter_top20": sorted(top_new - top_now),
        "leave_top20": sorted(top_now - top_new),
    }
    if paid_now is not None:
        summary.update({
            "paid_now": paid_now, "paid_preview": paid_new,
            "paid_seats_changed": len(set(paid_new) - set(paid_now)),
            "enter_paid": sorted(set(paid_new) - set(paid_now)),
            "leave_paid": sorted(set(paid_now) - set(paid_new)),
        })
    return {
        "as_of": as_of.isoformat(),
        "note": ("dry run: the prediction-day basis computed beside the rule in force; "
                 + ("standings and the paid set with every earning control applied, "
                    "ignoring the day-volume hold; "
                    if with_controls else "standings before earning controls; ")
                 + "nothing applied"),
        "with_controls": bool(with_controls),
        "current": params(environ),
        "preview": {**params(forced),
                    "age_basis": "prediction_day",
                    "previous_model_weight": previous_model_weight(forced),
                    "previous_model_threshold": previous_model_threshold(forced),
                    "model_since_hotkeys": len(model_since or {}),
                    # Each hotkey's boundary, so a miner can confirm theirs
                    # before it counts (miner request, 14 September 2026).
                    "model_since": {hk: d.isoformat()
                                    for hk, d in sorted((model_since or {}).items())},
                    "previous_model": previous_model},
        "summary": summary,
        "hotkeys": rows,
    }


def load_standing_entries(root: str, as_of: date, environ=os.environ,
                          window_days: int | None = None,
                          model_since: dict | None = None,
                          stats: dict | None = None,
                          ) -> dict[str, list[ScoredEpisode]]:
    """The entries every ranking consumer must read: ledger (absolute) or
    receipts (episode-relative), by the published mode. Under the
    prediction-day basis the executor's model_since map (written each shadow
    day) is read from the ledger root unless one is passed."""
    if window_days is None:
        window_days = window_in_force(environ, as_of)
    if relative_enabled(environ, as_of):
        if model_since is None and prediction_basis_in_force(environ, as_of):
            from hope.scoring.model_epoch import load_model_since
            model_since = load_model_since(root)
        return load_relative_entries(root, as_of, window_days, environ=environ,
                                     model_since=model_since, stats=stats)
    return standing_ledger.load_entries(root, as_of=as_of, window_days=window_days)
