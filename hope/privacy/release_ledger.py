"""Closed and released model digests — the model-privacy rule as a state machine.

docs/SN21_MODEL_PRIVACY.md (planned, not in force): while a digest is in the
earning set its image stays closed; it is released, irreversibly, on the
FIRST of
  1. left_earning_set — the hotkey running it is out of the earning set for
     5 consecutive days (a day withheld under the anti-copy rule does not
     count, and does not reset the run either);
  2. dethroned       — a new champion is seated: the outgoing champion's
     digest is released at once, even if that hotkey still earns;
  3. max_embargo     — 45 days after `closed_since`.
`closed_since` is the first day the digest was both closed and earning; a new
digest starts its own clock; a released digest never closes again.

Pure: the caller hands in each day's observations (who earns with which
digest, who the anti-copy rule withheld, who holds the champion seat) and
persists the state between days. Nothing here pulls, mirrors or publishes;
reading a day from the ledger is `day_inputs`, the daily step is
release_step.py and the operator copy is mirror.py.

Days without a run record are not observations: they neither count toward
the 5 nor reset it, so our downtime never releases anyone's model.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import date, timedelta

LEFT_EARNING_SET = "left_earning_set"
DETHRONED = "dethroned"
MAX_EMBARGO = "max_embargo"
# Same-day ties are broken in the published table order.
_REASON_ORDER = (LEFT_EARNING_SET, DETHRONED, MAX_EMBARGO)


@dataclass(frozen=True)
class ReleaseParams:
    max_embargo_days: int = 45
    release_after_days_out: int = 5


@dataclass(frozen=True)
class DigestState:
    hotkeys: frozenset
    closed_since: date
    days_out: int = 0                  # consecutive counted days out
    released_on: date | None = None
    reason: str | None = None


@dataclass(frozen=True)
class ReleaseState:
    digests: Mapping[str, DigestState] = field(default_factory=dict)
    champion: str | None = None        # hotkey holding the seat
    champion_digest: str | None = None  # the digest that held the seat
    last_observed: date | None = None


def observe_day(state: ReleaseState, day: date, earning: Mapping[str, str],
                copy_withheld=frozenset(), champion: str | None = None,
                params: ReleaseParams = ReleaseParams()
                ) -> tuple[ReleaseState, list[dict]]:
    """One observed run day.

    earning: hotkey -> digest for every hotkey the day's vector pays.
    copy_withheld: hotkeys whose seat was withheld under the anti-copy rule
        that day (their days out are neither counted nor reset).
    champion: hotkey holding the champion seat after the day (None = empty).
    Returns the new state and the day's releases, as ledger rows.
    """
    if state.last_observed is not None and day <= state.last_observed:
        raise ValueError(f"days must move forward: {day} after {state.last_observed}")
    digests = dict(state.digests)
    earning_by_digest: dict[str, set] = {}
    for hk, dg in earning.items():
        if dg:
            earning_by_digest.setdefault(str(dg), set()).add(str(hk))
    withheld = {str(h) for h in copy_withheld}
    due: dict[str, str] = {}

    # Closed, or kept closed: every earning digest not already released.
    for dg, hks in earning_by_digest.items():
        cur = digests.get(dg)
        if cur is None:
            digests[dg] = DigestState(hotkeys=frozenset(hks), closed_since=day)
        elif cur.released_on is None:
            digests[dg] = replace(cur, hotkeys=cur.hotkeys | frozenset(hks), days_out=0)

    for dg, cur in list(digests.items()):
        if cur.released_on is not None:
            continue
        # 1. out of the earning set
        if dg not in earning_by_digest:
            if cur.hotkeys & withheld:
                pass                                   # anti-copy day: not counted
            else:
                cur = replace(cur, days_out=cur.days_out + 1)
                digests[dg] = cur
                if cur.days_out >= params.release_after_days_out:
                    due.setdefault(dg, LEFT_EARNING_SET)
        # 3. embargo cap
        if day >= cur.closed_since + timedelta(days=params.max_embargo_days):
            due.setdefault(dg, MAX_EMBARGO)

    # 2. a new champion is seated: the outgoing seat's digest is released
    seat_digest = earning.get(champion) if champion else None
    if (champion is not None and state.champion is not None
            and champion != state.champion and state.champion_digest):
        old = digests.get(state.champion_digest)
        if old is not None and old.released_on is None:
            prev = due.get(state.champion_digest)
            if prev is None or _REASON_ORDER.index(DETHRONED) < _REASON_ORDER.index(prev):
                due[state.champion_digest] = DETHRONED

    releases = []
    for dg in sorted(due):
        cur = digests[dg]
        digests[dg] = replace(cur, released_on=day, reason=due[dg])
        releases.append({"digest": dg, "hotkey": min(cur.hotkeys) if cur.hotkeys else None,
                         "hotkeys": sorted(cur.hotkeys), "released_on": day.isoformat(),
                         "reason": due[dg], "closed_since": cur.closed_since.isoformat()})

    if champion is None:
        new_champion, new_digest = state.champion, state.champion_digest   # empty seat keeps the last holder
    elif champion == state.champion:
        new_champion, new_digest = champion, (seat_digest or state.champion_digest)
    else:
        new_champion, new_digest = champion, seat_digest
    return ReleaseState(digests=digests, champion=new_champion,
                        champion_digest=new_digest, last_observed=day), releases


def is_closed(state: ReleaseState, digest: str) -> bool:
    cur = state.digests.get(digest)
    return cur is not None and cur.released_on is None


# ---------------------------------------------------------------------------
# reading one day from the operator ledger
# ---------------------------------------------------------------------------
# How far back to look for the run a hotkey's earning model came from. An
# earner that has not run for longer than this is paid on standing alone and
# has no digest to close.
DIGEST_LOOKBACK_DAYS = 45


def day_inputs(ledger_root: str, day: date) -> dict | None:
    """(earning, copy_withheld, champion, refs) for a run day, or None when
    the day has no weights record (no run: not an observation).

    earning     hotkeys with a positive weight in intended_weights_<day>.json,
                each with the digest it last ran BEFORE that day
    withheld    hotkeys the anti-copy grouping excluded (collapse_audit groups)
    champion    the seat holder recorded with the day's weights
    refs        digest -> the repository it was pulled from (for the mirror)

    The weights file is named by the RUN day, the shadow records by the
    BASKET day, which is the day before (a run scores yesterday's basket).
    Reading shadow/<run day> found nothing on the day itself (it is written
    by the next run) and so counted every earner as out; only days strictly
    before the run day are read, which gives the same answer live and on
    replay.
    """
    path = os.path.join(ledger_root, f"intended_weights_{day.isoformat()}.json")
    if not os.path.exists(path):
        return None
    with open(path) as f:
        rec = json.load(f)
    weights = rec.get("weights") or {}
    earning, refs = {}, {}
    for hk, w in weights.items():
        try:
            if float(w) <= 0:
                continue
        except (TypeError, ValueError):
            continue
        ref = _ref_before(ledger_root, day, hk)
        if ref:
            repo, _, dg = ref.rpartition("@")
            earning[hk] = dg
            if repo:
                refs[dg] = repo
    withheld = set()
    audit = rec.get("collapse_audit") or {}
    for g in audit.get("groups") or []:
        withheld |= set(g.get("excluded") or [])
    return {"earning": earning, "copy_withheld": withheld,
            "champion": rec.get("champion"), "refs": refs}


def _ref_before(ledger_root: str, day: date, hotkey: str) -> str | None:
    """`repo@sha256:...` (or a bare digest) a hotkey last ran on a basket day
    before `day`, from its shadow record; None if it has not run within the
    lookback."""
    for n in range(1, DIGEST_LOOKBACK_DAYS + 1):
        p = os.path.join(ledger_root, "shadow", (day - timedelta(days=n)).isoformat(),
                         f"{hotkey}.jsonl")
        if not os.path.exists(p):
            continue
        last = None
        with open(p) as f:
            for line in f:
                if line.strip():
                    last = json.loads(line)
        ref = (last or {}).get("image_digest") or ""
        return ref if "sha256:" in ref else None
    return None


def replay(ledger_root: str, start: date, end: date,
           params: ReleaseParams = ReleaseParams()) -> tuple[ReleaseState, list[dict], int]:
    """Run the rule over the ledger from `start` to `end` inclusive.
    Returns the final state, every release (with the `image_ref` its digest
    was pulled from, when known), and the number of observed days."""
    state, out, observed, refs = ReleaseState(), [], 0, {}
    d = start
    while d <= end:
        inp = day_inputs(ledger_root, d)
        if inp is not None:
            refs.update(inp["refs"])
            state, rel = observe_day(state, d, inp["earning"], inp["copy_withheld"],
                                     inp["champion"], params)
            out.extend(rel)
            observed += 1
        d += timedelta(days=1)
    for r in out:
        r["image_ref"] = refs.get(r["digest"])
    return state, out, observed
