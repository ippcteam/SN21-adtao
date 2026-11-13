"""Copy-group precedence by the model's line (SN21_MODEL_SENIORITY_FROM), 13 Nov 2026.

The model's own first day alone resets on every rebuild, and the 40-day rule
makes every author rebuild: the copier of the old build would take the seat.
The line carries an author's date across their own digest change when a member
of today's group, judged today under the same digest as then, was grouped with
them under the earlier digest. A receipt's rows can come from a model the
hotkey has already left, so each member is recorded with the digest its rows
came from (the prediction day's digest), never by the date the group was seen.
"""
import json
import os
from datetime import date, timedelta

from hope.backtest import unchanged_models as U
from hope.scoring import model_line as ML
from hope.scoring.duplication import CopyGroup
from hope.validator import daily_stream_weights as W


def ref(c):
    return "repo/m@sha256:" + c * 64


A1, A2, CP, CP2, X, X2 = ref("a"), ref("b"), ref("c"), ref("9"), ref("e"), ref("f")
s = ML.short
ON = {W.MODEL_SENIORITY_FROM_ENV: "2026-11-01"}
TODAY = "2026-11-20"


def _span(start, end):
    d, out = date.fromisoformat(start), []
    while d.isoformat() <= end:
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out


def _runs(*spans):
    """spans: (start, end, {hk: ref}) -> {basket day: {hk: ref}}"""
    days: dict = {}
    for start, end, ran in spans:
        for d in _span(start, end):
            days.setdefault(d, {}).update(ran)
    return days


def _root(tmp_path, monkeypatch, days, history=(), receipts=None):
    from hope.backtest import shadow
    root = str(tmp_path)
    monkeypatch.setattr(shadow, "shadow_days", lambda r: sorted(days))
    monkeypatch.setattr(U, "_ran_on", lambda r, day: days.get(day, {}))
    U.refresh(root)
    for day, sets in history:
        ML.record(root, day, sets)
    for day, rows in (receipts or {}).items():
        _receipt(root, day, rows)
    return root


def _receipt(root, day, rows):
    """rows: {hk: [prediction days]}"""
    os.makedirs(os.path.join(root, "receipts"), exist_ok=True)
    entries = [{"miner": hk, "episode_id": f"{hk}-{i}", "horizon_days": 7,
                "predicted_on": p, "finalized_on": day, "score": 0.5, "weight": 1.0}
               for hk, ps in rows.items() for i, p in enumerate(ps)]
    with open(os.path.join(root, "receipts", f"{day}.json"), "w") as f:
        json.dump({"document": {"metrics": {"entries": entries}}}, f)


def _stats(payee, *eliminated, day=TODAY):
    return {"receipt_day": day, "group_detail": [
        {"payee": payee, "eliminated": list(eliminated), "kind": "same_predictions", "evidence": "e"}]}


def _rank(root, stats, lineage=(), env=ON, first=None, persist=True, day=TODAY, compared=None):
    return W.rank_by_model_line(root, day, env, stats, list(lineage), first or {},
                                persist=persist, lineage_audit={"current_model": compared or {}})


def _payee(stats):
    return stats["group_detail"][0]["payee"]


# ---- 1. the author's rebuild keeps the seat -------------------------------------

def _rebuild(tmp_path, monkeypatch, history=True):
    days = _runs(("2026-10-01", "2026-11-09", {"author": A1}),
                 ("2026-10-05", TODAY, {"copier": CP}),
                 ("2026-11-10", TODAY, {"author": A2}))
    hist = [(d, [{"author": s(A1), "copier": s(CP)}]) for d in _span("2026-10-10", "2026-11-09")] \
        if history else []
    return _root(tmp_path, monkeypatch, days, hist,
                 {TODAY: {"author": ["2026-11-12"], "copier": ["2026-11-12"]}})


def test_an_author_who_rebuilds_keeps_the_seat_from_a_copier_of_the_old_build(tmp_path, monkeypatch):
    root = _rebuild(tmp_path, monkeypatch)
    # The model's own first day alone would seat the copier: 11-10 against 10-05.
    assert U.model_first_days(root) == {"author": "2026-11-10", "copier": "2026-10-05"}
    stats = _stats("copier", "author")
    _, suppressed, key = _rank(root, stats)
    assert _payee(stats) == "author" and suppressed == {"copier"}
    assert key["author"][0] == "2026-10-01" and key["copier"][0] == "2026-10-05"


def test_a_rebuild_grouped_by_the_lineage_test_keeps_the_seat_too(tmp_path, monkeypatch):
    root = _rebuild(tmp_path, monkeypatch)
    g = CopyGroup(kind="lineage", original="copier", copies=("author",), evidence="e")
    lineage, _, _ = _rank(root, {}, lineage=[g], compared={"author": s(A2), "copier": s(CP)})
    assert lineage[0].original == "author" and lineage[0].copies == ("copier",)


def test_without_history_from_before_the_rebuild_the_new_digest_starts_the_line(tmp_path, monkeypatch):
    root = _rebuild(tmp_path, monkeypatch, history=False)
    stats = _stats("author", "copier")
    _, _, key = _rank(root, stats)
    assert key["author"][0] == "2026-11-10" and _payee(stats) == "copier"


def test_a_copier_first_grouped_after_the_rebuild_on_old_rows_still_counts(tmp_path, monkeypatch):
    """The copier starts 3 days before the rebuild; the receipts that first put
    them together arrive after it, on rows the author's old build made."""
    days = _runs(("2026-10-01", "2026-11-09", {"author": A1}),
                 ("2026-11-07", TODAY, {"copier": CP}),
                 ("2026-11-10", TODAY, {"author": A2}))
    hist = [(d, [{"author": s(A1), "copier": s(CP)}]) for d in _span("2026-11-15", "2026-11-17")]
    root = _root(tmp_path, monkeypatch, days, hist,
                 {TODAY: {"author": ["2026-11-13"], "copier": ["2026-11-13"]}})
    stats = _stats("copier", "author")
    _, _, key = _rank(root, stats)
    assert _payee(stats) == "author" and key["author"][0] == "2026-10-01"


def test_rows_from_both_builds_rank_on_the_newer_and_are_no_evidence(tmp_path, monkeypatch):
    root = _rebuild(tmp_path, monkeypatch)
    _receipt(root, TODAY, {"author": ["2026-11-08", "2026-11-12"], "copier": ["2026-11-12"]})
    stats = _stats("copier", "author")
    _, _, key = _rank(root, stats)
    assert _payee(stats) == "author" and key["author"][0] == "2026-10-01"
    assert ML.load(root)[TODAY] == [{"author": None, "copier": s(CP)}]


def test_a_catch_up_of_an_earlier_day_judges_the_model_of_that_day(tmp_path, monkeypatch):
    root = _rebuild(tmp_path, monkeypatch)
    _receipt(root, "2026-11-08", {"author": ["2026-11-01"], "copier": ["2026-11-01"]})
    stats = _stats("copier", "author", day="2026-11-08")
    _, _, key = _rank(root, stats, day="2026-11-08")
    assert _payee(stats) == "author" and key["author"][0] == "2026-10-01"


# ---- 2. an older hotkey that switches to a copy doesn't (finding A) ------------

def test_an_older_hotkey_switching_to_a_copy_does_not_take_the_seat(tmp_path, monkeypatch):
    days = _runs(("2026-08-01", TODAY, {"old2": X}),
                 ("2026-08-01", "2026-11-04", {"old": X}),
                 ("2026-11-01", TODAY, {"author": A1}),
                 ("2026-11-05", TODAY, {"old": CP}))
    hist = [(d, [{"old": s(X), "old2": s(X)}]) for d in _span("2026-08-10", "2026-11-04")]
    root = _root(tmp_path, monkeypatch, days, hist,
                 {TODAY: {"old": ["2026-11-12"], "author": ["2026-11-12"]}})
    stats = _stats("old", "author")
    _, suppressed, key = _rank(root, stats, first={"old": "2026-08-02", "author": "2026-11-02"})
    assert _payee(stats) == "author" and suppressed == {"old"}
    assert key["old"][0] == "2026-11-05"
    off = _stats("old", "author")
    _, none, _ = _rank(root, off, env={})
    assert none is None and _payee(off) == "old"          # off: the receipt order as before


def test_two_older_hotkeys_switching_days_apart_carry_nothing_from_each_other(tmp_path, monkeypatch):
    """s1 switches on 11-05 and s2 on 11-08. Receipts of 11-05..07 still group
    them on s1's old rows; that is s1 under its old model, not its new one."""
    days = _runs(("2026-08-01", "2026-11-04", {"s1": X}),
                 ("2026-08-01", "2026-11-07", {"s2": X}),
                 ("2026-11-01", TODAY, {"author": A1}),
                 ("2026-11-05", TODAY, {"s1": CP}),
                 ("2026-11-08", TODAY, {"s2": CP}))
    hist = [(d, [{"s1": s(X), "s2": s(X)}]) for d in _span("2026-08-10", "2026-11-07")]
    root = _root(tmp_path, monkeypatch, days, hist,
                 {TODAY: {"s1": ["2026-11-12"], "s2": ["2026-11-12"], "author": ["2026-11-12"]}})
    stats = _stats("s1", "s2", "author")
    _, suppressed, key = _rank(root, stats, first={"s1": "2026-08-02", "s2": "2026-08-02",
                                                   "author": "2026-11-02"})
    assert _payee(stats) == "author" and suppressed == {"s1", "s2"}
    assert (key["s1"][0], key["s2"][0]) == ("2026-11-05", "2026-11-08")


def test_an_author_and_copier_who_both_rebuild_on_one_day_fall_to_the_receipt_order(tmp_path, monkeypatch):
    """No member kept its model, so neither line carries; the first receipt day
    decides, as before the rule (open: noted for the rule's wording)."""
    days = _runs(("2026-10-01", "2026-11-09", {"author": A1}),
                 ("2026-10-05", "2026-11-09", {"copier": CP}),
                 ("2026-11-10", TODAY, {"author": A2, "copier": CP2}))
    hist = [(d, [{"author": s(A1), "copier": s(CP)}]) for d in _span("2026-10-10", "2026-11-09")]
    root = _root(tmp_path, monkeypatch, days, hist,
                 {TODAY: {"author": ["2026-11-12"], "copier": ["2026-11-12"]}})
    stats = _stats("copier", "author")
    _, _, key = _rank(root, stats, first={"author": "2026-10-02", "copier": "2026-10-06"})
    assert key["author"][0] == key["copier"][0] == "2026-11-10"
    assert _payee(stats) == "author"


# ---- 3. one payee for one set, whichever test named it --------------------------

def test_the_exact_and_lineage_tests_name_the_same_payee_for_one_set(tmp_path, monkeypatch):
    root = _rebuild(tmp_path, monkeypatch)
    stats = _stats("copier", "author")
    g = CopyGroup(kind="lineage", original="copier", copies=("author",), evidence="e")
    lineage, suppressed, key = _rank(root, stats, lineage=[g],
                                     compared={"author": s(A2), "copier": s(CP)})
    assert _payee(stats) == lineage[0].original == "author"
    restored = W.orphaned_payees([("author", ["copier"]), ("author", ["copier"])],
                                 set(suppressed) | set(lineage[0].copies), seniority=key)
    assert restored == set()


def test_end_to_end_the_allocation_pays_the_author_and_publishes_the_rule(tmp_path, monkeypatch):
    from hope.scoring import standing_ledger as sl
    from hope.scoring.daily_score_flow import WeightedEntry
    day = date(2026, 11, 20)
    days = _runs(("2026-08-01", "2026-11-04", {"old": X}),
                 ("2026-11-01", TODAY, {"author": A1}),
                 ("2026-11-05", TODAY, {"old": CP}))
    root = _root(tmp_path, monkeypatch, days)
    pred = {"cost_delta_pct": {"p50": -0.07}}
    rows = {"2026-08-05": [{"miner": "old", "episode_id": "e0", "horizon_days": 7,
                            "prediction": {"cost_delta_pct": {"p50": 0.3}}, "predicted_on": "2026-08-01"}],
            TODAY: [{"miner": hk, "episode_id": "e1", "horizon_days": 7, "prediction": pred,
                     "predicted_on": "2026-11-12"} for hk in ("old", "author")]}
    for d, entries in rows.items():
        with open(os.path.join(root, "receipts", f"{d}.json") if os.path.isdir(os.path.join(root, "receipts"))
                  else _mk(root, d), "w") as f:
            json.dump({"document": {"metrics": {"entries": entries}}}, f)
    for hk in ("old", "author"):
        sl.append_entries(root, [WeightedEntry(miner=hk, episode_id=f"e{i}", horizon_days=7, score=0.7,
                                               weight=1.0, entered_on=day) for i in range(300)])
    env = {"SN21_ONE_PAYER_PER_MODEL": "1"}
    off = W.allocation_from_ledger(root, day, 500, environ=env, persist=False)
    assert off.weights.get("author", 0) == 0 and off.weights.get("old", 0) > 0
    on = W.allocation_from_ledger(root, day, 500, environ={**env, **ON}, persist=False)
    assert on.weights.get("old", 0) == 0 and on.weights.get("author", 0) > 0
    pol = on.collapse_audit["policies"]["one_payer"]["precedence"]
    assert pol["rule"] == "model_line" and pol["line_since"] == {"author": "2026-11-01", "old": "2026-11-05"}
    assert off.collapse_audit["policies"]["one_payer"]["precedence"] == {"rule": "receipt_order"}
    assert not os.path.exists(os.path.join(root, ML.RECORD))      # persist=False writes nothing


def _mk(root, d):
    os.makedirs(os.path.join(root, "receipts"), exist_ok=True)
    return os.path.join(root, "receipts", f"{d}.json")


# ---- 4. reruns, dry runs, switch off, the seed -----------------------------------

def test_a_day_is_recorded_once_and_a_dry_run_writes_nothing(tmp_path, monkeypatch):
    root = _rebuild(tmp_path, monkeypatch, history=False)
    _rank(root, _stats("author", "copier"), persist=False)
    assert not os.path.exists(os.path.join(root, ML.RECORD))
    _rank(root, _stats("author", "copier"))
    _receipt(root, TODAY, {"author": ["2026-11-12"], "someone": ["2026-11-12"]})
    _rank(root, _stats("author", "someone"))
    assert ML.load(root)[TODAY] == [{"author": s(A2), "copier": s(CP)}]


def test_the_day_itself_and_later_days_are_no_evidence(tmp_path, monkeypatch):
    root = _rebuild(tmp_path, monkeypatch, history=False)
    ML.record(root, TODAY, [{"author": s(A1), "copier": s(CP)}])
    ML.record(root, "2026-11-25", [{"author": s(A1), "copier": s(CP)}])
    stats = _stats("author", "copier")
    _rank(root, stats)
    assert _payee(stats) == "copier"


def test_off_records_the_groups_and_changes_nothing(tmp_path, monkeypatch):
    root = _rebuild(tmp_path, monkeypatch)
    stats = _stats("copier", "author")
    g = CopyGroup(kind="lineage", original="copier", copies=("author",), evidence="e")
    lineage, suppressed, key = _rank(root, stats, lineage=[g], env={},
                                     compared={"author": s(A2), "copier": s(CP)})
    assert (suppressed, key) == (None, None)
    assert lineage == [g] and _payee(stats) == "copier" and "precedence" not in stats
    assert ML.load(root)[TODAY] == [{"author": s(A2), "copier": s(CP)}]


def test_before_its_date_the_rule_is_off(tmp_path, monkeypatch):
    root = _rebuild(tmp_path, monkeypatch)
    stats = _stats("copier", "author")
    _rank(root, stats, env={W.MODEL_SENIORITY_FROM_ENV: "2026-11-21"})
    assert _payee(stats) == "copier"


def test_the_published_audits_seed_the_history_with_digests_only_where_steady(tmp_path, monkeypatch):
    root = _rebuild(tmp_path, monkeypatch, history=False)
    os.makedirs(os.path.join(root, "allocation_audit"))
    for d in ("2026-11-08", "2026-11-12"):
        with open(os.path.join(root, "allocation_audit", f"{d}.json"), "w") as f:
            json.dump({"groups": [{"seat_held_by": "author", "excluded": ["copier"]}]}, f)
    seeded = ML.load(root, (U.load(root) or {}).get("runs"))
    assert seeded["2026-11-08"] == [{"author": s(A1), "copier": s(CP)}]
    # 11-12: the author changed model within the rows' window, so no digest.
    assert seeded["2026-11-12"] == [{"author": None, "copier": s(CP)}]
    stats = _stats("copier", "author")
    _rank(root, stats)
    assert _payee(stats) == "author"


def test_the_audit_names_the_rule_and_each_line_start(tmp_path, monkeypatch):
    root = _rebuild(tmp_path, monkeypatch)
    stats = _stats("copier", "author")
    _rank(root, stats, first={"author": "2026-10-02", "copier": "2026-10-06"})
    assert stats["precedence"] == {
        "rule": "model_line",
        "line_since": {"author": "2026-10-01", "copier": "2026-10-05"},
        "first_receipt": {"author": "2026-10-02", "copier": "2026-10-06"}}


def test_a_hotkey_with_no_record_of_its_model_comes_last(tmp_path, monkeypatch):
    root = _rebuild(tmp_path, monkeypatch)
    _receipt(root, TODAY, {"author": ["2026-11-12"], "unknown": ["2026-11-12"]})
    stats = _stats("unknown", "author")
    _rank(root, stats)
    assert _payee(stats) == "author"
