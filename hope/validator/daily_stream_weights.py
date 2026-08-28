"""Daily-stream weight allocation — E1 + D13 + D7 + D8 behind one flag.

This is the wiring layer the design leaves last (v0.5 §12: "daily weight
updates behind the [D3] volume gate"). Everything it composes is already
built and tested pure:

    daily_score_flow (E1)     — per-(episode,horizon) entries as horizons finalise
    episode_average (D13)     — age-weighted standing, cold-start gates
    weight_curve (D7)         — published rank curve, ceiling 20
    champion_promotion (D8)   — separate promotion state machine + log
    standing_ledger           — persistence for entries + promotion state

Flag: SN21_DAILY_STREAM_WEIGHTS (off by default — the operator's switch, M4).
Flag: SN21_CHRONIC_FAILURE_POLICY (off by default — the liveness/eviction
amendment). Off, evicted miners still earn; the state machine observes and
records regardless (chronic_failure module docstring).
ON, the unstated blast radius is this: evicting every placement-eligible
model empties the WHOLE weight vector, and on this subnet today that is one
model (reference-v1, ours — the only model that has ever run in shadow). The
failure is soft (onchain_runner retains the previous epoch vector) but no
weight moves at all. That is a governance question, not something the code
should quietly floor around, so allocation_from_ledger logs a
refusal-level warning and sets DailyAllocation.
enforcement_emptied_earning_set instead of inventing a survivor.
[D3] gate: SN21_D3_MIN_DAILY_EPISODES (0 = gate disabled until the operator sets
the published threshold against the verified ~330/weekday figure). On a
gated day the standings still update (scores are facts); only the WEIGHT
UPDATE is withheld — callers keep the previous vector, which is the
design's "thin day" behaviour: no weight movement on unrepresentative
volume, no data thrown away.

Pure core (compute_daily_allocation) + a thin I/O convenience
(allocation_from_ledger) mirroring the shadow-module pattern.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date

from hope.scoring import chronic_failure, standing_ledger
from hope.scoring.tenure import (
    short_tenure_hotkeys,
    tenure_gate_enabled,
    tenure_min_days,
)
from hope.scoring.champion_promotion import (
    PromotionDecision,
    PromotionParams,
    PromotionState,
    observe_day,
    vacate_seat,
)
from hope.scoring.chronic_failure import (
    chronic_failure_policy_enabled,
    evicted_hotkeys,
)
from hope.scoring.coldkey_cap import apply_coldkey_cap
from hope.scoring.duplication import (
    DuplicationReport,
    LineageParams,
    fingerprints_from_receipt,
    first_seen_fingerprints,
    lineage_collisions,
    one_payer_enabled,
    prediction_collisions,
    suppressed_copies,
)
from hope.scoring.episode_average import ScoredEpisode, standing
from hope.scoring.weight_curve import CurveParams, curve_weights

logger = logging.getLogger(__name__)

FLAG_ENV = "SN21_DAILY_STREAM_WEIGHTS"
D3_MIN_ENV = "SN21_D3_MIN_DAILY_EPISODES"

# [D3] ratified 2026-07-28 (The ruling: "agree 150"): the published minimum daily
# episode volume. The ENV VAR governs at runtime — unset keeps the gate off
# until the M4 cutover sets SN21_D3_MIN_DAILY_EPISODES=150 (cutover
# checklist step 1) — but the ratified number lives here so code, checklist
# and amendment cannot drift apart.
DEFAULT_D3_MIN_DAILY_EPISODES = 150


def daily_stream_enabled(environ=os.environ) -> bool:
    return environ.get(FLAG_ENV, "").strip().lower() in ("1", "true", "yes", "on")


def d3_min_daily_episodes(environ=os.environ) -> int:
    """[D3] published minimum; 0 disables the gate (pre-ratification)."""
    raw = environ.get(D3_MIN_ENV, "0").strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


@dataclass(frozen=True)
class DailyAllocation:
    day: date
    gated: bool                      # [D3]: True → hold previous weights
    day_episode_volume: int
    standings: dict = field(default_factory=dict)        # hotkey → D13 average
    weights: dict = field(default_factory=dict)          # hotkey → curve weight
    earning_set_size: int = 0
    promotion: PromotionDecision | None = None        # [D8], never gated
    evicted: tuple = ()                                  # liveness exclusions
    # True when eviction ENFORCEMENT is what emptied the earning set: the
    # same day would have paid somebody with the flag off. Not a patched
    # behaviour — a declared blast radius. See allocation_from_ledger.
    enforcement_emptied_earning_set: bool = False
    # The anti-clone working: which hotkeys lost a seat, to which principal,
    # under which parameter version, and the pairwise numbers behind every
    # grouping. Published in the receipt so a suppression can be recomputed
    # and argued with rather than merely announced.
    collapse_audit: dict = field(default_factory=dict)


def allocation_from_api(base_url: str, api_key: str, timeout_s: int = 60) -> "DailyAllocation":
    """The committer half of the M4 bridge: fetch the intended daily vector the
    operator (executor) published and return it as a DailyAllocation.

    The executor settles standings and computes the vector on its keyless disk;
    the committer holds the wallet but not the disk, so it reads the vector here
    over the same keyed internal API that serves baskets and outcomes. Fetches
    the LATEST published vector (the executor's settle and the committer's
    scoring run on independent clocks — 'latest' avoids committing nothing just
    because today's vector is not up yet).

    Fails SAFE: any read failure, a missing vector (404), or an unparseable body
    returns a GATED allocation, which the caller reads as "hold the previous
    vector" — it never fabricates or empties weights.
    """
    import json as _json
    import urllib.error
    import urllib.request
    from datetime import date as _date

    url = f"{base_url.rstrip('/')}/internal/bittensor/v1/daily/weights?day=latest"
    try:
        req = urllib.request.Request(url, headers={"X-API-Key": api_key})
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = _json.loads(resp.read().decode())
    except Exception as exc:  # noqa: BLE001 — 404/network/parse all fail safe
        logger.warning("[daily-stream] weight-vector fetch failed (%s) — "
                       "holding previous vector", exc)
        return DailyAllocation(day=_date.today(), gated=True, day_episode_volume=0)

    raw = payload.get("weights") or {}
    try:
        weights = {str(hk): float(w) for hk, w in raw.items()}
    except (TypeError, ValueError):
        logger.warning("[daily-stream] published vector had non-numeric weights "
                       "— holding previous vector")
        return DailyAllocation(day=_date.today(), gated=True, day_episode_volume=0)

    try:
        day = _date.fromisoformat(str(payload.get("day"))[:10])
    except ValueError:
        day = _date.today()
    return DailyAllocation(
        day=day,
        gated=bool(payload.get("gated", False)),
        day_episode_volume=int(payload.get("day_episode_volume") or 0),
        weights=weights,
        earning_set_size=int(payload.get("earning_set_size") or 0),
    )


def compute_daily_allocation(
    entries: dict[str, list[ScoredEpisode]],
    day: date,
    day_episode_volume: int,
    promotion_state: PromotionState,
    min_daily_episodes: int = 0,
    curve_params: CurveParams = CurveParams(),
    promotion_params: PromotionParams = PromotionParams(),
    evicted: frozenset = frozenset(),
    copy_suppressed: frozenset = frozenset(),
    participation: Mapping[str, float] | None = None,
    coldkey_of: Mapping[str, str] | None = None,
    lineage_groups: list | None = None,
    lineage_audit: Mapping | None = None,
    commit_block: Mapping[str, int] | None = None,
    tenure_min: int = 0,
    tenure_exempt: frozenset = frozenset(),
    one_payer_on: bool | None = None,
    lineage_on: bool | None = None,
) -> DailyAllocation:
    """One day's standings → weights (D7) + promotion observation (D8).

    [D3] gates the WEIGHT UPDATE only. Promotion observation still runs on
    a gated day: [D8]'s consecutive-hold clock is calendar-real and its
    inputs (standings) are facts regardless of whether weights move.
    Placement uses only placement-eligible miners (cold-start floor,
    GAP-2 v2 §3.4); their D13 average drives both curve rank and the
    promotion margin.

    `evicted` is the chronic-failure exclusion set (hope.scoring.
    chronic_failure). It is applied HERE, at the one place that feeds both
    the D7 curve and the D8 promotion margin, so money and champion move
    together. The curve renormalises over whoever is left
    (weight_curve.curve_weights), so removal needs no special-casing. Note
    what eviction does NOT do: ledger entries are untouched (scores are
    facts) and the container keeps being executed daily — that execution is
    how re-entry is observed.

    `copy_suppressed` is the one-payer-per-model exclusion set (ruled
    2026-08-06: identical models pay once, the first submission earns —
    hope.scoring.duplication.suppressed_copies). Same shape and same filter
    point as eviction, and the same limits: standings untouched, container
    still runs, and the exclusion lapses the moment the miner runs a model
    of their own. Unlike eviction it must NOT affect promotion — the
    promotion margin compares MODELS, and a copy's standing is the
    original's standing under another name, so hiding it from the margin
    would let a copy shield its original from a genuine challenger.
    Suppression is applied to the CURVE only.

    `participation` is the coverage gate (hope.scoring.participation), a
    hotkey -> multiplier in [0,1] from how much of each day's bundle the miner
    actually answered. It closes an attack that no anti-clone control can see:
    a skipped episode yields no ledger entry, so answering only the easy third
    of a basket makes a standing the mean of a best third. Cherry-picking is a
    genuinely independent model, so lineage, coldkey caps and precedence are
    all blind to it — coverage is the only thing that prices it.

    Applied to WEIGHTS, after the curve, then renormalised. Two consequences
    worth stating because they are easy to get wrong:

      * It must not touch standings or promotion. Coverage is a liveness-shaped
        fact, not an accuracy one, and the promotion margin compares models.
      * The penalty is RELATIVE, which is what a weight vector can express: the
        chain renormalises whatever we submit, so a multiplier that shrank
        every miner equally would be no penalty at all. It bites exactly when
        one miner covers less than another, which is the case that matters.
    """
    placements: dict[str, float] = {}
    for hotkey, eps in entries.items():
        if hotkey in evicted:
            continue
        st = standing(eps, as_of=day)
        if st["average"] is not None and st["placement_eligible"]:
            placements[hotkey] = st["average"]

    audit: dict = {}
    # Everything removed below still belongs on the leaderboard: scores are
    # facts and a hotkey that is not paid is not a hotkey that did not score.
    all_standings = dict(placements)

    # ---- Layer 1: one coldkey, one seat -----------------------------------
    # Applied BEFORE promotion, unlike copy suppression: a second hotkey on the
    # same coldkey is not a distinct principal at all, so it should never have
    # been a champion candidate either.
    coldkey_state: dict = {"applied": False, "identities": 0, "dropped": 0}
    if coldkey_of:
        cap = apply_coldkey_cap(placements, coldkey_of, commit_block=commit_block)
        placements = dict(cap.kept)
        coldkey_state = {"applied": True, "identities": len(coldkey_of),
                         "dropped": len(cap.dropped)}
        if cap.dropped:
            audit["coldkey_cap"] = cap.as_dict()
    else:
        # Without identities the cap cannot run, and it used to leave no trace
        # when that happened — no key in the audit, which reads exactly like a
        # day on which the cap ran and found nothing to do. The cap normally
        # removes a substantial share of the field, so those two days are very
        # different and must not publish the same record. A control that
        # cannot say "I did not run" cannot be audited.
        coldkey_state["reason"] = "no coldkey identities available"

    scored_days = standing_ledger.scored_day_counts(entries)
    promotion = observe_day(
        promotion_state, day, placements, scored_days, promotion_params
    )

    # ---- Layer 2: one lineage, one payee ----------------------------------
    # Dropped AFTER promotion observed the field, matching copy suppression.
    # NOTE: the design review asks for clone hotkeys to be ignored in the
    # promotion margin too. That is a one-line move of these two blocks and is
    # deliberately NOT made here — it contradicts the reasoning recorded on
    # copy_suppressed below, and which of the two is right is a ruling, not a
    # refactor.
    suppressed = set(copy_suppressed)
    if lineage_groups:
        groups_audit = []
        for i, group in enumerate(lineage_groups):
            suppressed.update(group.copies)
            groups_audit.append({
                "cluster": i,
                "payee": group.original,
                "eliminated": list(group.copies),
                "kind": group.kind,
                "evidence": group.evidence,
            })
        audit["lineage"] = {"groups": groups_audit}

    # One payer per model: the curve renormalises over genuine models only.
    if suppressed:
        placements = {hk: s for hk, s in placements.items()
                      if hk not in suppressed}
        audit["suppressed"] = sorted(suppressed)

    # Earning-set tenure (hope.scoring.tenure): the curve pays only hotkeys
    # with >= tenure_min distinct scored days. Same limits as copy
    # suppression: standings untouched, promotion untouched, curve only.
    # Stand-down guard: if gating would empty the curve, gate nobody — a
    # young field must not zero the whole vector.
    if tenure_min > 0:
        short = short_tenure_hotkeys(
            scored_days, placements.keys(), tenure_min, tenure_exempt)
        if short and len(short) < len(placements):
            placements = {hk: s for hk, s in placements.items()
                          if hk not in short}
            audit["tenure_gated"] = {
                "min_days": tenure_min, "hotkeys": sorted(short)}
        elif short:
            audit["tenure_gated"] = {
                "min_days": tenure_min, "stood_down": True,
                "reason": "gating would empty the curve",
                "hotkeys": sorted(short)}

    if lineage_audit:
        audit.setdefault("lineage", {})["pairwise"] = dict(lineage_audit)

    # ---- every documented control states its status, every day ------------
    # The detail blocks above are written only when a control has something to
    # SAY. That makes silence ambiguous three different ways — the control was
    # off, or it ran and found nothing, or it could not run at all — and all
    # three published an identical record, while meaning very different things
    # about who was paid.
    #
    # SN21_REWARDS.md and SN21_THREAT_MODEL.md name these controls to miners,
    # so "did it run today" has to be answerable from the published document
    # rather than from our logs. This block is that answer, and it is present
    # whether or not anything was suppressed.
    #
    # `None` means the caller did not tell us — recorded as unknown rather
    # than guessed, because a status we invented is worse than one we admit
    # we do not have.
    audit["policies"] = {
        "coldkey_cap": coldkey_state,
        "one_payer": {
            "enabled": one_payer_on,
            "suppressed": len(suppressed),
        },
        "lineage": {
            "configured": lineage_on,
            "groups": len(lineage_groups or []),
            "pairs_examined": len(lineage_audit or {}),
        },
        "tenure": {
            "enabled": tenure_min > 0,
            "min_days": tenure_min,
            "gated": len(audit.get("tenure_gated", {}).get("hotkeys", [])),
            "stood_down": bool(audit.get("tenure_gated", {}).get("stood_down")),
        },
    }

    gated = bool(min_daily_episodes) and day_episode_volume < min_daily_episodes
    weights = {} if gated else curve_weights(placements, curve_params)

    if weights and participation:
        # Unknown hotkey -> 1.0: a miner we have no coverage record for has not
        # been observed missing, and absence of a measurement must not cost
        # anyone their earnings.
        weights = {hk: w * participation.get(hk, 1.0) for hk, w in weights.items()}
        total = sum(weights.values())
        weights = ({hk: w / total for hk, w in weights.items()} if total > 0
                   else dict.fromkeys(weights, 0.0))
    return DailyAllocation(
        day=day,
        gated=gated,
        day_episode_volume=day_episode_volume,
        # Standing preserved, weight 0 — a suppressed hotkey stays visible
        # with the score it earned. Removing it would make a payment decision
        # look like a scoring one.
        standings=all_standings,
        weights=weights,
        collapse_audit=audit,
        earning_set_size=sum(1 for w in weights.values() if w > 0),
        promotion=promotion,
        evicted=tuple(sorted(evicted)),
    )


def allocation_from_ledger(
    root: str,
    day: date,
    day_episode_volume: int,
    min_daily_episodes: int | None = None,
    curve_params: CurveParams = CurveParams(),
    promotion_params: PromotionParams = PromotionParams(),
    environ=os.environ,
    coldkey_of: Mapping[str, str] | None = None,
    commit_block: Mapping[str, int] | None = None,
) -> DailyAllocation:
    """Load ledger + promotion state, compute, persist state + log events.

    The one impure entrypoint: everything it does beyond
    compute_daily_allocation is ledger I/O. Promotion state is persisted
    every call (last_observed advances even on uneventful days — the [D8]
    consecutive-day rule depends on it); events append to the audit log.

    Liveness enforcement is applied here and ONLY when
    SN21_CHRONIC_FAILURE_POLICY is on. With the flag off the state machine
    still runs and still records (daily_loop step 1c) — the evidence base
    accumulates from day one — but the weight vector is byte-identical to
    what it would have been, because eviction retracts a promise the spec
    has already published and must not reach miners before the amendment.
    """
    if min_daily_episodes is None:
        min_daily_episodes = d3_min_daily_episodes(environ)
    entries = standing_ledger.load_entries(root, as_of=day)
    state = standing_ledger.load_promotion_state(root)

    evicted: frozenset = frozenset()
    if chronic_failure_policy_enabled(environ):
        evicted = evicted_hotkeys(
            standing_ledger.load_eviction_states(root), day)
        if state.champion is not None and state.champion in evicted:
            # Force-vacate: omission alone would leave a crashing incumbent
            # seated for the full [D8] hold (champion_promotion.vacate_seat).
            vac = vacate_seat(state, day, "chronic_failure_eviction")
            state = vac.state
            if vac.event:
                standing_ledger.append_promotion_event(root, vac.event)

    copy_suppressed: frozenset = frozenset()
    if one_payer_enabled(environ):
        copy_suppressed = one_payer_suppression_subprocess(root, day, environ)

    tenure_min = tenure_min_days(environ) if tenure_gate_enabled(environ) else 0
    _house = chronic_failure.house_hotkey_from(environ)
    tenure_exempt = frozenset({_house}) if _house else frozenset()
    if tenure_min:
        logger.info("[tenure] earning-set gate active: min %d scored days",
                    tenure_min)

    participation: dict = {}
    if participation_gate_enabled(environ):
        participation = participation_multipliers(root, day, environ)
        thin = sorted(hk for hk, m in participation.items() if m < 1.0)
        if thin:
            logger.info("[participation] %d hotkey(s) below the coverage floor: %s",
                        len(thin), ", ".join(thin[:10]))

    # Layer 2 runs off the published receipts, so its verdicts are
    # recomputable by anyone holding them. Empty unless the parameters have
    # been calibrated and set.
    lineage_groups, lineage_audit = lineage_from_receipts(root, day, environ)
    if lineage_groups:
        logger.info("[lineage] %d group(s) collapsed under params@%s",
                    len(lineage_groups), lineage_audit.get("params_version"))

    alloc = compute_daily_allocation(
        entries, day, day_episode_volume, state,
        min_daily_episodes=min_daily_episodes,
        curve_params=curve_params,
        promotion_params=promotion_params,
        evicted=evicted,
        copy_suppressed=copy_suppressed,
        participation=participation,
        coldkey_of=coldkey_of,
        commit_block=commit_block,
        lineage_groups=lineage_groups,
        lineage_audit=lineage_audit,
        tenure_min=tenure_min,
        tenure_exempt=tenure_exempt,
        # Read from the environment HERE, where the switches actually live, so
        # the published status is what the run was configured with rather than
        # something inferred from an empty result. "Suppressed nobody" and
        # "was switched off" are different facts and must not share a record.
        one_payer_on=one_payer_enabled(environ),
        lineage_on=lineage_params_from_env(environ).configured(),
    )

    # BLAST RADIUS, declared not patched: with the flag on, evicting every
    # placement-eligible model empties the whole weight vector. On this
    # subnet today that is one model (reference-v1, ours), so a single
    # eviction stops all weight movement. Whether that is acceptable is a
    # GOVERNANCE call — a floor that keeps a non-running model earning is
    # exactly the punishment-free promise the amendment retracts — so the
    # behaviour is left alone and made LOUD instead. The counterfactual is
    # computed only on the empty-vector path, and only to prove enforcement
    # (rather than a thin field) is the cause.
    if evicted and not alloc.gated and alloc.earning_set_size == 0:
        counterfactual = compute_daily_allocation(
            entries, day, day_episode_volume, state,
            min_daily_episodes=min_daily_episodes,
            curve_params=curve_params,
            promotion_params=promotion_params,
            evicted=frozenset(),
        )
        if counterfactual.earning_set_size > 0:
            alloc = replace(alloc, enforcement_emptied_earning_set=True)
            logger.warning(
                "REFUSING NOTHING, BUT SAY IT LOUD: %s=on emptied the entire "
                "earning set on %s. Evicted %s; without enforcement %d model(s) "
                "would have earned (%s). No model is weighted today, so the "
                "runner will retain the previous epoch vector and no weight "
                "moves at all. This is the declared blast radius of enabling "
                "the chronic-failure policy on a subnet with this few "
                "placement-eligible models — not a bug to route around.",
                chronic_failure.FLAG_ENV, day, list(alloc.evicted),
                counterfactual.earning_set_size,
                sorted(counterfactual.weights),
            )

    if alloc.promotion is not None:
        standing_ledger.save_promotion_state(root, alloc.promotion.state)
        if alloc.promotion.event:
            standing_ledger.append_promotion_event(root, alloc.promotion.event)
    return alloc


def pairs_for_uids(
    weights: dict,
    uid_by_hotkey: dict,
    allowed_uids: set | None = None,
) -> list:
    """(uid, weight) pairs for the runner — allowlist-aware and pure.

    Audit 2026-07-29: the runner's daily block built pairs from the raw
    uid_by_hotkey map, silently bypassing SN21_WEIGHT_ALLOWLIST_UIDS.
    ``allowed_uids=None`` means no restriction; a set (even empty)
    restricts exactly. Deterministic order (hotkey asc) for stable
    vectors; zero/negative weights and unmapped hotkeys drop.
    """
    pairs = []
    for hk in sorted(weights):
        w = weights[hk]
        if w is None or w <= 0:
            continue
        uid = uid_by_hotkey.get(hk)
        if uid is None:
            continue
        if allowed_uids is not None and uid not in allowed_uids:
            continue
        pairs.append((uid, float(w)))
    return pairs


def _fingerprint_index_dir(root: str) -> str:
    return os.path.join(root, "fingerprints")


def _load_day_fingerprints(root: str, day_name: str) -> dict | None:
    """{hotkey: fingerprint} for one settle day — from the index if present.

    The index file is a few KB; the receipt it derives from can carry tens
    of thousands of entries. Reading receipts every day made the one-payer
    check cost ~1GB of transient memory and OOM-killed the 2G executor pod
    (kill loops on 26 and 27 Aug). Missing or unreadable index → derive from
    the receipt once and persist, so the heavy path runs at most once per
    day ever. None → no usable source; the caller skips the day.
    """
    ipath = os.path.join(_fingerprint_index_dir(root), f"{day_name}.json")
    try:
        with open(ipath) as f:
            loaded = json.load(f)
        if isinstance(loaded, dict):
            return loaded
    except (OSError, ValueError):
        pass
    rpath = os.path.join(root, "receipts", f"{day_name}.json")
    try:
        with open(rpath) as f:
            envelope = json.load(f)
    except (OSError, ValueError):
        return None
    entries = (envelope.get("document") or {}).get("metrics", {}).get("entries", [])
    prints = fingerprints_from_receipt(entries)
    try:
        os.makedirs(_fingerprint_index_dir(root), exist_ok=True)
        tmp = ipath + ".tmp"
        with open(tmp, "w") as f:
            json.dump(prints, f)
        os.replace(tmp, ipath)
    except OSError:
        pass    # index is an optimisation; the derived prints still serve
    return prints


def ensure_fingerprint_indexes(root: str, up_to_day=None) -> int:
    """Backfill fingerprint indexes for every receipt day lacking one.

    Loads receipts ONE AT A TIME and frees each before the next — call this
    while the process is still light (early in the pipeline), so the one-off
    heavy pass never lands on top of settle's peak. Returns how many were
    built; 0 on a warm ledger, making it free to call daily.
    """
    rdir = os.path.join(root, "receipts")
    if not os.path.isdir(rdir):
        return 0
    built = 0
    for name in sorted(os.listdir(rdir)):
        if not name.endswith(".json") or name.startswith("_"):
            continue
        day_name = name[:-5]
        if up_to_day is not None and day_name > str(up_to_day):
            continue
        ipath = os.path.join(_fingerprint_index_dir(root), name)
        if os.path.exists(ipath):
            continue
        if _load_day_fingerprints(root, day_name) is not None:
            built += 1
    return built


def one_payer_suppression_subprocess(
    root: str, day: date, environ, timeout_s: int = 600,
) -> frozenset:
    """one_payer_suppression_from_receipts behind a process boundary.

    Fingerprinting a ledger of receipts means json-loading documents with
    tens of thousands of entries; the transient allocation on top of a
    settle process already holding the day's ledger OOM-killed the 2G pod
    three times on 2026-08-26 — a kill loop, because the day had no run
    record yet. The subprocess caps the blast radius the same way the
    collector's batches do: the child pays the memory bill and dies alone.

    Fails EMPTY on any child failure — same direction as the in-process
    reader: missing evidence never suppresses anybody.
    """
    import subprocess
    import sys
    payload = json.dumps({
        "root": root, "day": str(day),
        "env": {k: v for k, v in dict(environ).items()
                if k.startswith(("SN21_", "HOPE_"))},
    })
    child = (
        "import json, sys, os\n"
        "from datetime import date\n"
        "spec = json.loads(sys.stdin.read())\n"
        "os.environ.update(spec['env'])\n"
        "from hope.validator.daily_stream_weights import "
        "one_payer_suppression_from_receipts\n"
        "out = one_payer_suppression_from_receipts(\n"
        "    spec['root'], date.fromisoformat(spec['day']), os.environ)\n"
        "print(json.dumps(sorted(out)))\n"
    )
    try:
        proc = subprocess.run(
            [sys.executable, "-c", child], input=payload,
            capture_output=True, text=True, timeout=timeout_s)
        if proc.returncode != 0:
            logger.warning(
                "one-payer child exited %d — suppressing NOBODY for %s "
                "(stderr tail: %s)", proc.returncode, day,
                (proc.stderr or "")[-200:])
            return frozenset()
        return frozenset(json.loads(proc.stdout.strip() or "[]"))
    except Exception:
        logger.exception(
            "one-payer subprocess failed — suppressing NOBODY for %s", day)
        return frozenset()


def one_payer_suppression_from_receipts(root: str, day: date, environ) -> frozenset:
    """The one-payer exclusion set for `day`, derived from published receipts.

    Behaviour-only, deliberately. Same-digest copies produce identical
    predictions once they are scored, and only scored models can earn — so
    for PAYMENT purposes the fingerprint detector subsumes the digest one,
    and this needs nothing but the ledger the loop already owns. (Digest
    collisions still matter earlier, at intake, where the registry-side
    report is built with chain access.)

    Exemptions available here: the house hotkey (SN21_HOUSE_HOTKEY) — a
    group containing the house model IS the reference behaviour that day,
    which exempts reference-runners before the reference image has ever
    been pushed digest-pinned. The digest list (SN21_COPY_EXEMPT_DIGESTS)
    additionally applies wherever active digests are known.

    Fails EMPTY, never loud: an unreadable receipt history must not zero
    anybody's weight. Missing evidence is a reason to not suppress, not a
    reason to suppress.
    """
    try:
        receipt_dir_path = os.path.join(root, "receipts")
        if not os.path.isdir(receipt_dir_path):
            return frozenset()
        days = sorted(
            name[:-5] for name in os.listdir(receipt_dir_path)
            if name.endswith(".json") and not name.startswith("_")
        )
        history_days = []
        today_fingerprints: dict = {}
        for day_name in days:
            if day_name > str(day):
                continue
            prints = _load_day_fingerprints(root, day_name)
            if prints is None:
                continue
            history_days.append((day_name, prints))
            if day_name == str(day):
                today_fingerprints = prints

        if not today_fingerprints:
            return frozenset()

        history = first_seen_fingerprints(history_days)
        groups = prediction_collisions(today_fingerprints, history=history)

        # Behavioural grouping is NOT done here. It lives in
        # lineage_from_receipts, which uses the four-signal test and publishes
        # its working. This path keeps the exact-fingerprint detector only:
        # byte-identical predictions are the cheapest, clearest evidence there
        # is, and unlike the behavioural test it needs no calibration — so it
        # is the one thing protecting the stream before parameters are set. It
        # is deliberately NOT the real control.
        if not groups:
            return frozenset()

        house = chronic_failure.house_hotkey_from(environ)
        exempt_hotkeys = frozenset({house}) if house else frozenset()
        return frozenset(suppressed_copies(
            DuplicationReport(groups=groups),
            exempt_hotkeys=exempt_hotkeys,
        ))
    except Exception:
        logger.exception(
            "one-payer suppression failed — suppressing NOBODY for %s", day)
        return frozenset()


def predictions_from_receipt(entries) -> dict:
    """{miner: {episode: {horizon: prediction}}} from the receipt entries."""
    out: dict = {}
    for entry in entries or []:
        miner = entry.get("miner")
        prediction = entry.get("prediction")
        if not miner or prediction is None:
            continue
        (out.setdefault(miner, {})
            .setdefault(str(entry.get("episode_id")), {})
            [str(entry.get("horizon_days"))]) = prediction
    return out


def actuals_from_receipt(outcomes) -> dict:
    """{(episode, horizon, metric): actual} — the scale behind each distance.

    The receipt already publishes every settled actual it scored against, so a
    grouping computed here can be recomputed by anyone holding the same
    document. That is what makes a suppression contestable instead of merely
    announced.
    """
    metrics = ("cost_delta_pct", "conversions_delta_pct", "efficiency_delta_pct")
    out: dict = {}
    for row in outcomes or []:
        episode = str(row.get("episode_id"))
        horizon = str(row.get("horizon_days"))
        for metric in metrics:
            value = row.get(metric)
            if isinstance(value, (int, float)):
                out[(episode, horizon, metric)] = float(value)
    return out


PARTICIPATION_ENABLED_ENV = "SN21_PARTICIPATION_GATE"
PARTICIPATION_WINDOW_DAYS = 7


def participation_gate_enabled(environ) -> bool:
    """Off by default. The gate changes who is paid, so it goes live when the
    coverage floor has been published to miners — not when the code lands."""
    return (environ.get(PARTICIPATION_ENABLED_ENV) or "").strip().lower() in (
        "1", "true", "yes", "on")


def participation_multipliers(
    root: str,
    day: date,
    environ,
    window_days: int = PARTICIPATION_WINDOW_DAYS,
) -> dict:
    """hotkey -> weight multiplier from recent bundle coverage.

    Reads the shadow ledger's own `episodes_in` / `predictions_out` records, so
    it needs no new plumbing and no new source of truth.

    A day the SUBNET did not run is dropped entirely — it neither rewards nor
    punishes, and it does not break a run of misses. On 2026-08-03 a worker
    died, no bundle shipped, and a naive gate would have marked every hotkey
    absent for OUR failure and propagated that to five mirroring validators
    within the hour. `day_verdict` already encodes this; the caller must supply
    an honest `subnet_ran`.
    """
    from datetime import timedelta

    from hope.backtest import shadow
    from hope.scoring import participation as P

    params = P.params_from_env(environ)
    days = [(day - timedelta(days=n)).isoformat()
            for n in range(window_days - 1, -1, -1)]   # chronological

    verdicts: dict = {}
    for d in days:
        ran = shadow.subnet_ran(root, d)
        coverage = shadow.day_coverage(root, d) if ran else {}
        if not ran:
            # Nothing to record: a day we did not ship tells us nothing about
            # anyone, so it is not appended for ANY hotkey.
            continue
        for hotkey, (episodes_in, predictions_out) in coverage.items():
            verdicts.setdefault(hotkey, []).append(
                P.day_verdict(episodes_in, predictions_out, True, params))

    return {hk: P.bridge_multiplier(v, params) for hk, v in verdicts.items()}


# ---------------------------------------------------------------------------
# Lineage parameters as published configuration.
#
# Every value is unset by default and `configured()` is False until ALL of them
# are supplied, which keeps the collapse OFF rather than running on numbers
# nobody calibrated. The ruling is explicit that these come from the operator's
# own red team against known copies and known-independent models, never from a
# participant's proposal — however well argued, and this one was.

LINEAGE_ENV = {
    "corr_min": "SN21_LINEAGE_CORR_MIN",
    "sign_agree_min": "SN21_LINEAGE_SIGN_MIN",
    "distance_max": "SN21_LINEAGE_DISTANCE_MAX",
    "disagree_max": "SN21_LINEAGE_DISAGREE_MAX",
}
LINEAGE_DELTA_ENV = "SN21_LINEAGE_DISAGREE_DELTA"
LINEAGE_VERSION_ENV = "SN21_LINEAGE_PARAMS_VERSION"


def lineage_params_from_env(environ) -> LineageParams:
    """Unset or malformed leaves the field None, which leaves the whole control
    off. A deploy typo must never quietly decide who is paid."""
    values: dict = {}
    for field_name, key in LINEAGE_ENV.items():
        raw = (environ.get(key) or "").strip()
        if not raw:
            continue
        try:
            values[field_name] = float(raw)
        except ValueError:
            logger.warning("%s=%r is not a number — lineage collapse stays OFF",
                           key, raw)
            return LineageParams()
    delta_raw = (environ.get(LINEAGE_DELTA_ENV) or "").strip()
    if delta_raw:
        try:
            values["disagree_delta"] = float(delta_raw)
        except ValueError:
            logger.warning("%s=%r is not a number — using the default delta",
                           LINEAGE_DELTA_ENV, delta_raw)
    values["version"] = (environ.get(LINEAGE_VERSION_ENV) or "unset").strip() or "unset"
    return LineageParams(**values)


def lineage_from_receipts(root: str, day: date, environ):
    """(groups, pairwise_audit) for `day`, from the published receipts.

    Reads the same documents a miner reads, so any grouping this produces can
    be recomputed by anyone holding them — which is what makes a suppression
    contestable rather than merely announced.

    Precedence inside a group is behaviour seniority: the earliest day on which
    a hotkey appears in the published receipts. Nobody can appear in the record
    before the model they copied existed, which is the property that makes this
    resistant to back-dated or empty commitments.
    """
    params = lineage_params_from_env(environ)
    if not params.configured():
        return [], {}

    receipt_dir_path = os.path.join(root, "receipts")
    try:
        days = sorted(fn[:-5] for fn in os.listdir(receipt_dir_path)
                      if fn.endswith(".json"))
    except OSError:
        return [], {}

    first_seen: dict = {}
    predictions: dict = {}
    actuals: dict = {}
    for rank, day_name in enumerate(d for d in days if d <= str(day)):
        try:
            with open(os.path.join(receipt_dir_path, f"{day_name}.json")) as fh:
                envelope = json.load(fh)
        except (OSError, ValueError):
            continue
        metrics = (envelope.get("document") or {}).get("metrics", {})
        entries = metrics.get("entries", [])
        for entry in entries:
            hotkey = entry.get("miner")
            if hotkey and hotkey not in first_seen:
                first_seen[hotkey] = rank
        if day_name == str(day):
            predictions = predictions_from_receipt(entries)
            actuals = actuals_from_receipt(metrics.get("outcomes", []))

    if not predictions:
        return [], {}

    groups, audit = lineage_collisions(predictions, actuals, params,
                                       precedence=first_seen)
    if groups:
        audit = dict(audit)
        audit["params_version"] = params.version
    return groups, audit
