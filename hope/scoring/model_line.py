"""Who keeps the seat in a copy group: the hotkey whose line of this model ran first.

A copy group pays one member (SN21_REWARDS.md, one payer per model). Before
SN21_MODEL_SENIORITY_FROM the payee is the member that first appeared in the
receipts with any model. A miner's review of 12 Nov 2026 showed what that
allows: a hotkey that has been around for months with another model can commit
a copy of a newer miner's model and take its seat.

The model's own first day fixes that, but a rebuild gets a new digest, and the
40-day rule makes every author change digest. On the digest's first day
alone, an author who rebuilds would lose the seat to whoever copied the old
build. So precedence follows the model's LINE: the digest a hotkey was judged
under, and before it each earlier digest of the same hotkey while there is
evidence the behaviour carried on. The evidence is another member of today's
copy set, judged today under the same digest it was judged under then, that
was in a copy group with this hotkey while this hotkey was judged under the
earlier digest. Being the same hotkey is not evidence on its own, nor is a
member that has changed model too: an older hotkey (or several) that switches
to a copy starts the day its current model first ran.

WHICH DIGEST A MEMBER WAS JUDGED UNDER. A receipt carries rows settled that
day from predictions made up to weeks earlier, so a group can be formed on
rows from a model the hotkey has already left. Each member is recorded with the
digest its rows came from: for the lineage test the current-model digest it
compares rows under; for the exact tests the digest the hotkey ran on each
row's prediction day (the 40-day record's `runs`). A member whose rows came
from more than one digest is recorded without one, and is no evidence.

The copy sets are recorded once per day (a re-run never rewrites a day), and
seeded from the published allocation audits for the days before the record
began, with a member's digest only where it ran one digest for the whole
window a receipt's rows can come from.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterable, Mapping
from datetime import date, timedelta

NEVER = "9999-12-31"
RECORD = os.path.join("registry", "_copy_sets.json")
KEEP_DAYS = 180
# How far back a receipt's rows can have been predicted: the longest horizon
# (28 days), the 3 days to settle, and some slack.
ROW_WINDOW_DAYS = 35


def short(digest) -> str | None:
    """The 16-hex form the receipt publishes; None for nothing."""
    text = str(digest or "").strip()
    if ":" in text:
        text = text.rsplit(":", 1)[1]
    return text[:16].lower() or None


def timeline(runs: Mapping[str, Mapping[str, str]], hk: str) -> list[tuple[str, str]]:
    """[(first basket day, short digest)] of a hotkey, oldest first."""
    return sorted((first, short(dg)) for dg, first in (runs.get(hk) or {}).items())


def digest_on(tl, day: str) -> str | None:
    held = None
    for first, dg in tl:
        if first <= day:
            held = dg
    return held


def _path(root: str) -> str:
    return os.path.join(root, RECORD)


def _seed(root: str, runs: Mapping) -> dict:
    """{day: [{hotkey: digest or None}, ...]} from the published allocation audits."""
    from hope.publication.allocation_audit import FEED_DIR
    d = os.path.join(root, FEED_DIR)
    days: dict = {}
    for fn in sorted(os.listdir(d)) if os.path.isdir(d) else []:
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(d, fn)) as f:
                doc = json.load(f)
            day = fn[:-5]
            back = (date.fromisoformat(day) - timedelta(days=ROW_WINDOW_DAYS)).isoformat()
        except (OSError, ValueError):
            continue
        sets = []
        for g in doc.get("groups") or []:
            members = {m for m in [g.get("seat_held_by"), *(g.get("excluded") or [])] if m}
            if len(members) < 2:
                continue
            judged = {}
            for m in sorted(members):
                tl = timeline(runs, m)
                now = digest_on(tl, day)
                judged[m] = now if now and digest_on(tl, back) in (now, None) else None
            sets.append(judged)
        if sets:
            days[day] = sets
    return days


def load(root: str, runs: Mapping | None = None) -> dict:
    """{day: [{hotkey: digest or None}, ...]}: the record, or the seed when there
    is none yet."""
    try:
        with open(_path(root)) as f:
            return dict(json.load(f).get("days") or {})
    except (OSError, ValueError):
        return _seed(root, runs or {})


def single(value) -> str | None:
    """One digest, or None for none or several (a list of the row digests)."""
    if isinstance(value, (list, tuple, set, frozenset)):
        value = {v for v in value if v}
        return next(iter(value)) if len(value) == 1 else None
    return value or None


def record(root: str, day, sets: Iterable[Mapping],
           runs: Mapping | None = None, save_it: bool = True) -> dict:
    """Add `day`'s copy groups (each {hotkey: the digest(s) it was judged under}) if
    the day isn't recorded yet - the first write wins - and keep KEEP_DAYS."""
    days = load(root, runs)
    d = str(day)[:10]
    if d not in days:
        unique = {json.dumps({hk: single(v) for hk, v in sorted(s.items())}, sort_keys=True)
                  for s in sets if len(s) > 1}
        days[d] = [json.loads(u) for u in sorted(unique)]
    floor = (date.fromisoformat(d) - timedelta(days=KEEP_DAYS)).isoformat()
    days = {k: v for k, v in days.items() if k >= floor}
    if save_it:
        path = _path(root)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as f:
            json.dump({"days": days}, f, indent=1, sort_keys=True)
        os.replace(path + ".tmp", path)
    return days


def copy_sets(groups: Iterable[Iterable[str]]) -> list[set]:
    """Groups that share a hotkey joined into one set."""
    sets: list[set] = []
    for g in groups:
        g = {m for m in g if m}
        for s in [s for s in sets if s & g]:
            g |= s
            sets.remove(s)
        sets.append(g)
    return sets


def line_since(hk: str, members: set, judged: Mapping[str, str | None],
               runs: Mapping[str, Mapping[str, str]], days: Mapping[str, list],
               rank_digest: str | None = None) -> str:
    """The first day of `hk`'s line of the model it was judged under today,
    inside today's copy set `members`. `judged` holds each member's one digest
    (None when its rows came from several); `rank_digest` is the newest of
    `hk`'s, used when it has several."""
    tl = timeline(runs, hk)
    firsts = {dg: first for first, dg in tl}
    cur = judged.get(hk) or rank_digest
    if not cur or cur not in firsts:
        return NEVER
    since = firsts[cur]
    witnesses = {m: judged[m] for m in members - {hk} if judged.get(m)}
    for f, dg in reversed([(f, dg) for f, dg in tl if f < since]):
        if not any(s.get(hk) == dg and s.get(m) == then
                   for sets in days.values() for s in sets
                   for m, then in witnesses.items()):
            break
        since = f
    return since


def precedence(groups: Iterable[Mapping[str, str | None]],
               runs: Mapping[str, Mapping[str, str]], days: Mapping[str, list],
               first_receipt: Mapping[str, str],
               fallback: Mapping[str, str] | None = None) -> dict:
    """hotkey -> (line start, first receipt day): the order every copy test and
    the restore of an orphaned set use when the rule is in force. `groups` are
    today's, each {hotkey: the digest or digests it was judged under}. A member
    with none is ranked on `fallback` (the digest the 40-day record says it
    runs), which is never evidence for anyone's line."""
    groups = list(groups)
    rows: dict = {}
    for g in groups:
        for hk, v in g.items():
            vals = v if isinstance(v, (list, tuple, set, frozenset)) else [v]
            rows.setdefault(hk, set()).update(x for x in vals if x)
    # One digest across every group today, or None: no evidence either way.
    judged = {hk: single(v) for hk, v in rows.items()}
    newest = {}
    for hk, v in rows.items():
        firsts = {dg: f for f, dg in timeline(runs, hk)}
        known = [dg for dg in v if dg in firsts]
        newest[hk] = (max(known, key=lambda dg: firsts[dg]) if known
                      else short((fallback or {}).get(hk)))
    out = {}
    for members in copy_sets(g.keys() for g in groups):
        for hk in members:
            out[hk] = (line_since(hk, members, judged, runs, days, newest.get(hk)),
                       str(first_receipt.get(hk) or NEVER))
    return out


def ranked(members: Iterable[str], key: Mapping[str, tuple]) -> list[str]:
    return sorted(members, key=lambda hk: (tuple(key.get(hk, (NEVER, NEVER))), hk))


def row_digests(root: str, receipt_day: str, hotkeys: Iterable[str],
                runs: Mapping) -> dict:
    """hotkey -> sorted digests its rows on that receipt came from (empty when
    the 40-day record knows none of them)."""
    from hope.scoring.standing_method import _entries_of
    want = set(hotkeys)
    path = os.path.join(root, "receipts", f"{receipt_day}.json")
    seen: dict = {hk: set() for hk in want}
    try:
        entries = _entries_of(path)
    except (OSError, ValueError):
        return {hk: [] for hk in want}
    tls = {hk: timeline(runs, hk) for hk in want}
    for e in entries:
        hk = e.get("miner")
        if hk not in want:
            continue
        on = e.get("predicted_on")
        if not on:
            try:
                on = (date.fromisoformat(str(e.get("finalized_on"))[:10])
                      - timedelta(days=int(e.get("horizon_days") or 0) + 3)).isoformat()
            except (TypeError, ValueError):
                continue
        seen[hk].add(digest_on(tls[hk], str(on)[:10]))
    return {hk: sorted(d for d in s if d) for hk, s in seen.items()}
