"""The validator's daily loop — the single entrypoint the host timer calls.

Audit 2026-07-29's dominant finding was wiring: settle-day scoring, the
D9 capture fold, the compliance view, and D11 publication were all
correct, tested libraries that NOTHING invoked. This module is the
invoker — one call, five steps, each fail-soft with its own summary so
one broken step never silences the others:

  1. settle   — run_settle_day: settled outcomes -> scores -> standing
                ledger (per-pair markers make re-runs cheap no-ops)
  1c. liveness — fold EXECUTION results (shadow ledger ok/error) into the
                chronic-failure state machine: strikes, eviction, re-entry.
                Always records; ENFORCEMENT is gated on
                SN21_CHRONIC_FAILURE_POLICY and lands in step 5. The days it
                folds come from the SHADOW LEDGER, not from `day`: the
                ledger is keyed by BASKET day (yesterday's, written by
                shadow_daily.sh) whereas `day` is the SETTLE day, so asking
                whether the subnet ran on `day` answered "no" every day
                forever. See liveness_days_to_observe. A shadow day that is
                genuinely absent is still skipped entirely — absence is our
                non-run, never a miner failure.
  2. capture  — [D9] fold the day's earned alpha through the capture
                path into persisted CaptureStates. Pre-M4 the injected
                earnings provider returns {} (nothing earns yet); the
                wiring exists so M4 needs nothing.
  3. advisory — compliance_view written to <ledger_root>/collateral/
                compliance_<day>.json (the review-pack artifact §8
                promises; never touches weights)
  4. publish  — [D11] publish_day over the results step 1 entered,
                hash-chained + attested; the anchor digest goes on-chain
                via the injected committer ONLY when SN21_ANCHOR_COMMITS
                is true (default off — chain spend is a deliberate act)
  5. weights  — when SN21_DAILY_STREAM_WEIGHTS is on, compute the day's
                allocation and WRITE it to <ledger_root>/intended_weights_
                <day>.json. Chain submission stays with the existing
                runner path (onchain_runner's daily block) — a second
                set_weights caller would race it; at M4 the runner reads
                the same ledger this loop maintains. Also writes
                day_volume.json (the [D3] gate's volume handoff — the
                env-var contract had no producer).

Idempotency: steps 1 and 4 are idempotent by their own machinery
(entered-markers; append-only feed). Steps 2/3/5 overwrite per-day
artifacts deterministically. Running the loop twice in a day is safe.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import asdict
from datetime import date, timedelta

from hope.scoring import standing_ledger
from hope.scoring.collateral_floor import (
    ANCHOR_FIRST_SETTLEMENT,
    CaptureState,
    active_floor,
    compliance_view,
    fold_day,
    ladder_anchor_from,
)
from hope.scoring.settle_day_flow import run_settle_day
from hope.validator.daily_stream_weights import (
    allocation_from_ledger,
    daily_stream_enabled,
)

ANCHOR_FLAG_ENV = "SN21_ANCHOR_COMMITS"

# How far back step 1c will reach for shadow days it has never observed.
# Bounds the cold-start fold (and any catch-up after an outage) without
# needing a high-water mark file: once the strike log exists, the last
# observed day is the floor instead.
LIVENESS_LOOKBACK_DAYS = 14


def liveness_days_to_observe(
    shadow_root: str,
    prior_events,
    day: date,
    lookback_days: int = LIVENESS_LOOKBACK_DAYS,
) -> list:
    """Which SHADOW days step 1c should fold when the loop runs on `day`.

    THE BUG THIS EXISTS TO KILL: the loop used to ask
    `subnet_ran(shadow_root, str(day))` — its own calendar day. The shadow
    ledger is keyed by BASKET day and scripts/shadow_daily.sh runs
    `BD-$(date -d yesterday)`, so the loop's day is NEVER a shadow day. Step
    1c therefore reported "subnet did not run this day" every single day,
    forever, while reading as an honest statement — and the whole flag-off
    design rests on the claim that the evidence base accumulates from day
    one. `day` is also the SETTLE day, a different namespace again (an
    episode predicted on X settles X+15/22/36), so a one-day offset would
    not have fixed it either.

    Rule: fold every shadow day on disk that (a) the subnet actually ran,
    (b) is not after `day`, and (c) is at or after the floor. The floor is
    the LAST day already observed in the strike log — re-folding exactly
    that day is what lets a same-day re-run retract a recorded strike
    (chronic_failure._observations) — or, on a cold ledger,
    `day - lookback_days`. Days strictly before the floor are never
    re-folded: re-observing an ancient clean day against today's state would
    spuriously reinstate a currently evicted miner.
    """
    from hope.backtest.shadow import shadow_days
    from hope.scoring.chronic_failure import OBSERVATION_KINDS

    observed = [e.day for e in prior_events if e.kind in OBSERVATION_KINDS]
    floor = max(observed) if observed else day - timedelta(days=int(lookback_days))
    out = []
    for name in shadow_days(shadow_root):
        try:
            d = date.fromisoformat(name)
        except ValueError:
            continue
        if floor <= d <= day:
            out.append(d)
    return out


# ---- capture-state persistence ([D9]) ----------------------------------------

def _collateral_dir(ledger_root: str) -> str:
    return os.path.join(ledger_root, "collateral")


def _states_path(ledger_root: str) -> str:
    return os.path.join(_collateral_dir(ledger_root), "capture_states.json")


def load_capture_states(ledger_root: str) -> dict[str, CaptureState]:
    path = _states_path(ledger_root)
    if not os.path.exists(path):
        return {}
    with open(path) as f:
        raw = json.load(f)
    return {hk: CaptureState(**rec) for hk, rec in raw.items()}


def save_capture_states(ledger_root: str, states: dict[str, CaptureState]) -> None:
    os.makedirs(_collateral_dir(ledger_root), exist_ok=True)
    tmp = _states_path(ledger_root) + ".tmp"
    with open(tmp, "w") as f:
        json.dump({hk: asdict(st) for hk, st in states.items()}, f, indent=1)
    os.replace(tmp, _states_path(ledger_root))


# ---- the loop ----------------------------------------------------------------

def run_daily_loop(
    shadow_root: str,
    ledger_root: str,
    day: date,
    outcomes_provider: Callable[[date], list],
    earnings_provider: Callable[[date], dict[str, float]] | None = None,
    floor_alpha: float | None = None,
    key_loader: Callable[[], object] | None = None,
    chain_committer: Callable[[bytes], object] | None = None,
    coldkey_reader: Callable[[], object] | None = None,
    chain_reader: Callable[[str], float | None] | None = None,
    day_volume_provider: Callable[[date], int] | None = None,
    vertical_map_provider: Callable[[list], dict] | None = None,
    transition_key_provider: Callable[[list], dict] | None = None,
    liveness_lookback_days: int = LIVENESS_LOOKBACK_DAYS,
    environ=os.environ,
) -> dict:
    """One day, five fail-soft steps. Returns a per-step summary dict."""
    summary: dict = {"day": str(day)}

    # 1. settle-day scoring -> standing ledger
    horizon_results = []
    settle_components: dict = {}
    settled_outcomes: list = []
    prediction_index: dict = {}
    censored_counts: dict = {}
    try:
        settle = run_settle_day(shadow_root, ledger_root, day,
                                outcomes_provider, return_results=True,
                                environ=environ)
        horizon_results = settle.pop("horizon_results", [])
        settle_components = settle.pop("components", {})
        settled_outcomes = settle.pop("settled_outcomes", [])
        prediction_index = settle.pop("prediction_index", {})
        censored_counts = settle.pop("censored_counts", {})
        summary["settle"] = settle
    except Exception as e:
        summary["settle"] = {"error": str(e)}

    # 1b. [D4 condition 2] vertical-error series — the operator's episode->vertical
    # map tags each newly settled entry at entry time (J3, 2026-07-29).
    # vertical_map_provider: Callable[[list[str]], dict[episode_id -> vertical]]
    # (the reference implementation runs sql/j3_episode_vertical_map.sql over
    # the same operator-platform connection the outcomes provider uses). Raw score
    # components are stored so the operator's pending formula decision recomputes the
    # series instead of rebuilding it. Skipped silently when no provider.
    if vertical_map_provider is not None and horizon_results:
        try:
            from hope.scoring.daily_score_flow import horizon_entry_weight
            from hope.scoring.vertical_error_series import (
                SeriesEntry,
            )
            from hope.scoring.vertical_error_series import (
                append_entries as append_series,
            )
            vmap = vertical_map_provider(
                sorted({r.episode_id for r in horizon_results}))
            series = []
            for r in horizon_results:
                comps = settle_components.get(
                    (r.episode_id, r.horizon_days, r.miner))
                series.append(SeriesEntry(
                    episode_id=r.episode_id,
                    horizon_days=r.horizon_days,
                    miner=r.miner,
                    vertical=vmap.get(r.episode_id, "untagged"),
                    score=r.score,
                    pinball_component=comps[0] if comps else r.score,
                    direction_component=comps[1] if comps else r.score,
                    entry_weight=horizon_entry_weight(r.horizon_days,
                                                      r.resolution),
                    settled_on=str(r.finalized_on),
                ))
            summary["vertical_series"] = {
                "entries_appended": append_series(ledger_root, series),
                "untagged": sum(1 for e in series
                                if e.vertical == "untagged"),
            }
        except Exception as e:
            summary["vertical_series"] = {"error": str(e)}

    # 1c. Accuracy by change type (Rob, 20 Aug) — the one aggregation behind
    # the public accuracy page (7/14/28 selector) and per-miner win/lose
    # feedback. Same provider pattern as 1b: the operator platform maps
    # episode ids to transition keys; a missing map row buckets as UNKNOWN
    # rather than thinning the page. Fail-soft like every other step.
    if transition_key_provider is not None and horizon_results:
        try:
            from hope.reporting.type_accuracy import (
                aggregate_type_accuracy,
                build_scored_entries,
            )
            tmap = transition_key_provider(
                sorted({str(r.episode_id) for r in horizon_results}))
            agg = aggregate_type_accuracy(
                build_scored_entries(horizon_results, tmap or {}))
            _ta_dir = os.path.join(ledger_root, "accuracy_by_type")
            os.makedirs(_ta_dir, exist_ok=True)
            _ta_path = os.path.join(_ta_dir, f"{day}.json")
            with open(_ta_path, "w") as _f:
                json.dump({"day": str(day), **agg}, _f, sort_keys=True)
            summary["type_accuracy"] = {
                "entries": agg["totals"]["entries"],
                "miners": agg["totals"]["miners"],
                "types": agg["totals"]["types"],
                "path": _ta_path,
            }
        except Exception as e:
            summary["type_accuracy"] = {"error": str(e)}

    # 1c. liveness — chronic-failure observation (strikes/eviction).
    # Reads the day's EXECUTION facts off the shadow ledger and folds them
    # into the liveness state machine. Runs whether or not
    # SN21_CHRONIC_FAILURE_POLICY is on: recording is free and builds the
    # evidence base the amendment's numbers need; only ENFORCEMENT (step 5,
    # via allocation_from_ledger) is gated.
    #
    # The skip below is the single most important safety property here: the
    # shadow day is driven by a manual script, so an absent day means WE did
    # not run, not that miners failed. No run, no observation — never a
    # strike (see hope.backtest.shadow.subnet_ran).
    #
    # WHICH day is observed comes from the SHADOW LEDGER, never from the
    # loop's own calendar day — see liveness_days_to_observe.
    try:
        from hope.backtest.shadow import day_run_status
        from hope.scoring import chronic_failure as cf

        params = cf.params_from_env(environ)
        prior_events = standing_ledger.load_strike_events(
            ledger_root, as_of=day,
            window_days=params.window_days + liveness_lookback_days)
        days = liveness_days_to_observe(shadow_root, prior_events, day,
                                        liveness_lookback_days)
        if not days:
            summary["liveness"] = {
                "skipped": f"no shadow day the subnet ran on or before {day} "
                           f"within the {liveness_lookback_days}-day reach",
                "strikes": 0,
            }
        else:
            states = standing_ledger.load_eviction_states(ledger_root)
            log = list(prior_events)
            emitted: list = []
            struck: list = []
            excluded: list = []
            evicted: list = []
            reinstated: list = []
            retracted: list = []
            withheld: list = []
            observed = 0
            for d in days:
                runs = day_run_status(shadow_root, str(d))
                observed += len(runs)
                decision = cf.observe_day(states, log, d, runs, params)
                states = decision.states
                log.extend(decision.events)
                emitted.extend(decision.events)
                struck.extend(decision.struck)
                excluded.extend(decision.excluded)
                evicted.extend(decision.evicted)
                reinstated.extend(decision.reinstated)
                retracted.extend(decision.retracted)
                withheld.extend(decision.withheld)
            # EVENTS BEFORE STATE, deliberately. retract_eviction reconstructs
            # evictions_total purely from the KIND_EVICTED lines in the log, so
            # a crash between these two writes must not leave an eviction in
            # the state with no line behind it — that miner could never have
            # the eviction retracted correctly, and his counters would be
            # unreconstructable. The append-only log is the source of truth;
            # state is a derived cache. Losing the state and keeping the events
            # is recoverable, the reverse is not, so the log goes first.
            written = standing_ledger.append_strike_events(ledger_root, emitted)
            standing_ledger.save_eviction_states(ledger_root, states)
            summary["liveness"] = {
                "shadow_days_observed": [str(d) for d in days],
                "models_observed": observed,
                "strikes": len(struck),
                "struck": struck,
                "excluded": excluded,
                "evicted": evicted,
                "reinstated": reinstated,
                "retracted": retracted,
                # the non-empty-field floor firing. MUST be surfaced: a withheld eviction
                # means a model that earned removal is still earning, and the
                # only reason it is still here is that removing it would have
                # emptied the subnet. Silent is exactly wrong — the operator
                # needs to see it every day it happens.
                "withheld": withheld,
                "events_written": written,
                "enforced": cf.chronic_failure_policy_enabled(environ),
                "currently_evicted": sorted(cf.evicted_hotkeys(states, day)),
            }
    except Exception as e:
        summary["liveness"] = {"error": str(e)}

    # THE FLOOR IN FORCE TODAY. An explicit floor_alpha still wins (tests, and
    # the review-restatement override the policy reserves), but the default is
    # now resolved from configuration: SN21_IM_LAUNCH_DATE drives the ladder,
    # and until a date is named it holds at week 0. This is what makes the
    # launch date a value we set rather than a code change and a deploy — the
    # ladder was previously pinned to LAUNCH_FLOOR_ALPHA here and could never
    # step, no matter what collateral_floor computed.
    # first_settlement is only READ when the anchor is configured to use it;
    # computing it unconditionally would scan the whole ledger every day for a
    # value the launch anchor never looks at.
    _first_settled = (standing_ledger.first_scored_day(ledger_root)
                      if ladder_anchor_from(environ) == ANCHOR_FIRST_SETTLEMENT
                      else None)
    effective_floor = (float(floor_alpha) if floor_alpha is not None
                       else active_floor(day, environ, _first_settled))
    summary["collateral_floor_alpha"] = effective_floor

    # 2. [D9] capture fold — persisted; pre-M4 the provider returns {}
    try:
        earned = (earnings_provider or (lambda d: {}))(day)
        states = load_capture_states(ledger_root)
        escrowed = paid = 0.0
        for hk, alpha in sorted(earned.items()):
            st = states.get(hk) or CaptureState(hotkey=hk)
            folded = fold_day(st, float(alpha), effective_floor)
            states[hk] = folded.state
            escrowed += folded.escrowed_alpha
            paid += folded.paid_alpha
        save_capture_states(ledger_root, states)
        summary["capture"] = {"miners_earned": len(earned),
                              "escrowed_alpha": round(escrowed, 6),
                              "paid_alpha": round(paid, 6),
                              "states_tracked": len(states)}
    except Exception as e:
        summary["capture"] = {"error": str(e)}

    # 3. advisory compliance view (never touches weights)
    try:
        states = load_capture_states(ledger_root)
        view = compliance_view(states, effective_floor, chain_reader=chain_reader)
        os.makedirs(_collateral_dir(ledger_root), exist_ok=True)
        out_path = os.path.join(_collateral_dir(ledger_root),
                                f"compliance_{day}.json")
        with open(out_path + ".tmp", "w") as f:
            json.dump(view, f, indent=1)
        os.replace(out_path + ".tmp", out_path)
        summary["advisory"] = {"path": out_path,
                               "floors_met": view["floors_met"],
                               "floors_total": view["floors_total"]}
    except Exception as e:
        summary["advisory"] = {"error": str(e)}

    # 4. [D11] publication + gated anchor
    try:
        if key_loader is None:
            summary["publish"] = {"skipped": "no key_loader (set "
                                  "SN21_ED25519_KEY_FILE and pass a loader)"}
        else:
            from hope.publication.daily_accuracy_runner import publish_day
            from hope.publication.receipt_feed import run_daily_receipt

            # RECEIPT FIRST, deliberately: the accuracy document embeds the
            # receipt's sha256 and the chain anchors the accuracy document,
            # so one anchor covers both (governance ruling 2026-08-05: miners must be able
            # to rerun and reproduce their score — the receipt carries their
            # predictions verbatim, the settled actuals, the components and
            # the formula weights; scripts/verify_day.py is the rerun).
            receipt = run_daily_receipt(
                ledger_root, day, settled_outcomes, prediction_index,
                horizon_results, settle_components, key_loader(),
                generated_at=f"{day}T00:00:00Z", environ=environ,
                censored=censored_counts,
            )
            summary["receipt"] = {"published": receipt.published,
                                  "sha256": receipt.sha256,
                                  "skipped_reason": receipt.skipped_reason}

            published = publish_day(
                ledger_root, day, horizon_results, key_loader(),
                generated_at=f"{day}T00:00:00Z",
                receipt_sha256=receipt.sha256,
            )
            pub: dict = {"published": published.published,
                         "zero_day": published.zero_day,
                         "skipped_reason": published.skipped_reason,
                         "anchor_sha256": published.anchor_sha256}
            if published.published and published.anchor_sha256:
                # ANCHOR THE ROLLING ROOT, NOT THIS DAY'S HASH (decided
                # 2026-08-05). Commitments::CommitmentOf holds ONE entry per
                # (netuid, hotkey) and every commit overwrites the previous, so
                # committing per-day hashes would erase yesterday's anchor and
                # leave only the newest day verifiable at chain head. The root
                # covers every published day, so one commit keeps the whole
                # history checkable from head with a small inclusion proof.
                from hope.publication.feed_root import feed_root
                root_hex = feed_root(ledger_root)
                pub["feed_root"] = root_hex
                anchor_on = environ.get(ANCHOR_FLAG_ENV, "").strip().lower() in (
                    "1", "true", "yes", "on")
                if not anchor_on:
                    pub["anchor"] = "off (SN21_ANCHOR_COMMITS unset)"
                elif chain_committer is None:
                    pub["anchor"] = "skipped_no_committer"
                elif not root_hex:
                    # Cannot happen alongside published=True, but committing a
                    # zero/absent root would read on chain as a real anchor over
                    # an empty history — refuse loudly instead.
                    pub["anchor"] = "skipped_no_feed_root"
                else:
                    res = chain_committer(bytes.fromhex(root_hex))
                    pub["anchor"] = f"committed: {res}"
            summary["publish"] = pub
    except FileExistsError:
        summary["publish"] = {"skipped": "already published for this day"}
    except Exception as e:
        summary["publish"] = {"error": str(e)}

    # 5. weights intent + [D3] volume handoff
    try:
        vol = None
        if day_volume_provider is not None:
            vol = int(day_volume_provider(day))
            tmp = os.path.join(ledger_root, "day_volume.json.tmp")
            with open(tmp, "w") as f:
                json.dump({"day": str(day), "volume": vol}, f)
            os.replace(tmp, os.path.join(ledger_root, "day_volume.json"))
        if daily_stream_enabled(environ):
            # One coldkey, one seat. Injected the same way the chain
            # committer is, so the loop stays testable without a chain and a
            # read failure cannot take the day's weights down with it.
            coldkey_of = None
            if coldkey_reader is not None:
                try:
                    coldkey_of = coldkey_reader()
                except Exception as exc:
                    # Fail OPEN, loudly. A metagraph we could not read is not
                    # evidence that anybody is farming, and confiscating seats
                    # over our own outage would be the worse error.
                    print(f"[coldkey-cap] metagraph read failed ({exc}) — "
                          f"the cap is NOT applied today", flush=True)
                    coldkey_of = None
                else:
                    print(f"[coldkey-cap] identities read for "
                          f"{len(coldkey_of or {})} hotkey(s)", flush=True)

            alloc = allocation_from_ledger(ledger_root, day, vol or 0,
                                           environ=environ,
                                           coldkey_of=coldkey_of)
            out_path = os.path.join(ledger_root, f"intended_weights_{day}.json")
            with open(out_path + ".tmp", "w") as f:
                json.dump({
                    "day": str(day), "gated": alloc.gated,
                    "day_episode_volume": alloc.day_episode_volume,
                    "weights": alloc.weights,
                    # Raw D13 standings (hotkey -> average), separate from the
                    # gated weight vector: the public leaderboard shows SCORES,
                    # which are facts even on a gated day when weights hold.
                    "standings": alloc.standings,
                    "earning_set_size": alloc.earning_set_size,
                    "champion": (alloc.promotion.state.champion
                                 if alloc.promotion else None),
                    "evicted": list(alloc.evicted),
                    # The anti-clone working: who lost a seat, to whom, under
                    # which parameter version, with the pairwise numbers. It
                    # was being computed and discarded, which left
                    # verify_day --recheck-grouping comparing against nothing.
                    "collapse_audit": alloc.collapse_audit,
                }, f, indent=1)
            os.replace(out_path + ".tmp", out_path)
            summary["weights"] = {"path": out_path, "gated": alloc.gated,
                                  "earning_set_size": alloc.earning_set_size,
                                  "evicted": list(alloc.evicted),
                    # The anti-clone working: who lost a seat, to whom, under
                    # which parameter version, with the pairwise numbers. It
                    # was being computed and discarded, which left
                    # verify_day --recheck-grouping comparing against nothing.
                    "collapse_audit": alloc.collapse_audit,
                                  "day_volume": vol}
        else:
            summary["weights"] = {"skipped": "SN21_DAILY_STREAM_WEIGHTS off",
                                  "day_volume": vol}
    except Exception as e:
        summary["weights"] = {"error": str(e)}

    return summary


def read_day_volume(ledger_root: str, day: date) -> int | None:
    """The [D3] volume handoff for the runner: day_volume.json, trusted
    only when it records THIS day (a stale file must fail closed, not
    smuggle yesterday's volume past the gate)."""
    path = os.path.join(ledger_root, "day_volume.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            rec = json.load(f)
        if rec.get("day") != str(day):
            return None
        return int(rec["volume"])
    except Exception:
        return None
