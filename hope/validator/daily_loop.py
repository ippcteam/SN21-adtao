"""The validator's daily loop — the single entrypoint the host timer calls.

Audit 2026-07-29's dominant finding was wiring: settle-day scoring, the
D9 capture fold, the compliance view, and D11 publication were all
correct, tested libraries that NOTHING invoked. This module is the
invoker — one call, five steps, each fail-soft with its own summary so
one broken step never silences the others:

  1. settle   — run_settle_day: settled outcomes -> scores -> standing
                ledger (per-pair markers make re-runs cheap no-ops)
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
from dataclasses import asdict
from datetime import date
from typing import Callable, Optional

from hope.scoring import standing_ledger
from hope.scoring.collateral_floor import (
    CaptureState,
    LAUNCH_FLOOR_ALPHA,
    compliance_view,
    fold_day,
)
from hope.scoring.settle_day_flow import run_settle_day
from hope.validator.daily_stream_weights import (
    allocation_from_ledger,
    daily_stream_enabled,
)

ANCHOR_FLAG_ENV = "SN21_ANCHOR_COMMITS"


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
    earnings_provider: Optional[Callable[[date], dict[str, float]]] = None,
    floor_alpha: float = LAUNCH_FLOOR_ALPHA,
    key_loader: Optional[Callable[[], object]] = None,
    chain_committer: Optional[Callable[[bytes], object]] = None,
    chain_reader: Optional[Callable[[str], Optional[float]]] = None,
    day_volume_provider: Optional[Callable[[date], int]] = None,
    vertical_map_provider: Optional[Callable[[list], dict]] = None,
    environ=os.environ,
) -> dict:
    """One day, five fail-soft steps. Returns a per-step summary dict."""
    summary: dict = {"day": str(day)}

    # 1. settle-day scoring -> standing ledger
    horizon_results = []
    settle_components: dict = {}
    try:
        settle = run_settle_day(shadow_root, ledger_root, day,
                                outcomes_provider, return_results=True)
        horizon_results = settle.pop("horizon_results", [])
        settle_components = settle.pop("components", {})
        summary["settle"] = settle
    except Exception as e:
        summary["settle"] = {"error": str(e)}

    # 1b. [D4 condition 2] vertical-error series — Jayesh's episode->vertical
    # map tags each newly settled entry at entry time (J3, 2026-07-29).
    # vertical_map_provider: Callable[[list[str]], dict[episode_id -> vertical]]
    # (the reference implementation runs sql/j3_episode_vertical_map.sql over
    # the same OBI connection the outcomes provider uses). Raw score
    # components are stored so Rob's pending formula decision recomputes the
    # series instead of rebuilding it. Skipped silently when no provider.
    if vertical_map_provider is not None and horizon_results:
        try:
            from hope.scoring.vertical_error_series import (
                SeriesEntry, append_entries as append_series,
            )
            from hope.scoring.daily_score_flow import horizon_entry_weight
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

    # 2. [D9] capture fold — persisted; pre-M4 the provider returns {}
    try:
        earned = (earnings_provider or (lambda d: {}))(day)
        states = load_capture_states(ledger_root)
        escrowed = paid = 0.0
        for hk, alpha in sorted(earned.items()):
            st = states.get(hk) or CaptureState(hotkey=hk)
            folded = fold_day(st, float(alpha), floor_alpha)
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
        view = compliance_view(states, floor_alpha, chain_reader=chain_reader)
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
            published = publish_day(
                ledger_root, day, horizon_results, key_loader(),
                generated_at=f"{day}T00:00:00Z",
            )
            pub: dict = {"published": published.published,
                         "zero_day": published.zero_day,
                         "skipped_reason": published.skipped_reason,
                         "anchor_sha256": published.anchor_sha256}
            if published.published and published.anchor_sha256:
                anchor_on = environ.get(ANCHOR_FLAG_ENV, "").strip().lower() in (
                    "1", "true", "yes", "on")
                if not anchor_on:
                    pub["anchor"] = "off (SN21_ANCHOR_COMMITS unset)"
                elif chain_committer is None:
                    pub["anchor"] = "skipped_no_committer"
                else:
                    res = chain_committer(bytes.fromhex(published.anchor_sha256))
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
            alloc = allocation_from_ledger(ledger_root, day, vol or 0)
            out_path = os.path.join(ledger_root, f"intended_weights_{day}.json")
            with open(out_path + ".tmp", "w") as f:
                json.dump({
                    "day": str(day), "gated": alloc.gated,
                    "day_episode_volume": alloc.day_episode_volume,
                    "weights": alloc.weights,
                    "earning_set_size": alloc.earning_set_size,
                    "champion": (alloc.promotion.state.champion
                                 if alloc.promotion else None),
                }, f, indent=1)
            os.replace(out_path + ".tmp", out_path)
            summary["weights"] = {"path": out_path, "gated": alloc.gated,
                                  "earning_set_size": alloc.earning_set_size,
                                  "day_volume": vol}
        else:
            summary["weights"] = {"skipped": "SN21_DAILY_STREAM_WEIGHTS off",
                                  "day_volume": vol}
    except Exception as e:
        summary["weights"] = {"error": str(e)}

    return summary


def read_day_volume(ledger_root: str, day: date) -> Optional[int]:
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
