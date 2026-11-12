"""Models unchanged for 40 days are not run from day 41 (Sir Rob, KzoNlmEF, 2 Nov 2026)."""

from types import SimpleNamespace

from hope.backtest import unchanged_models as U

A = "repo/a@sha256:" + "a" * 64
B = "repo/a@sha256:" + "b" * 64
ON = {U.SWITCH_ENV: "2026-11-16"}


def _m(ref):
    return SimpleNamespace(image_digest=ref)


def test_off_until_the_date():
    assert U.enabled("2026-11-16", {}) is False
    assert U.enabled("2026-11-15", ON) is False
    assert U.enabled("2026-11-16", ON) is True
    assert U.enabled("2026-11-16", {U.SWITCH_ENV: "soon"}) is False


def test_the_count_starts_at_the_first_run_and_a_new_digest_restarts_it():
    r = U.update(U.empty(), {"hk": A}, "2026-10-01")
    r = U.update(r, {"hk": A}, "2026-10-02")
    assert r["hotkeys"]["hk"] == {"digest": "sha256:" + "a" * 64, "since": "2026-10-01"}
    assert r["through"] == "2026-10-02"
    r = U.update(r, {"hk": B}, "2026-10-05")
    assert r["hotkeys"]["hk"]["since"] == "2026-10-05"


def test_a_missed_day_does_not_reset_the_count():
    r = U.update(U.empty(), {"hk": A}, "2026-10-01")
    r = U.update(r, {}, "2026-10-02")
    r = U.update(r, {"hk": A}, "2026-10-03")
    assert r["hotkeys"]["hk"]["since"] == "2026-10-01"


def test_day_40_runs_and_day_41_does_not():
    """The record holds the BASKET day a digest first delivered on (1 Oct); the run
    that served it is the next day (2 Oct), and that run is day 1 (SN21_REWARDS.md:
    the count starts on the first daily run). 12 Nov 2026: counted from the basket
    day, a model got 39 runs and its first day not run was a day early."""
    r = U.update(U.empty(), {"hk": A}, "2026-10-01")
    st = U.standing(r["hotkeys"]["hk"], "2026-11-10")
    assert st == {"digest_since": "2026-10-02", "days_unchanged": 40, "removal_on": "2026-11-11"}
    assert U.to_remove(r, {"hk": _m(A)}, "2026-11-10") == {}
    assert U.to_remove(r, {"hk": _m(A)}, "2026-11-11") == {"hk": "unchanged"}


def test_a_model_gets_exactly_40_runs():
    from datetime import date, timedelta
    r = U.update(U.empty(), {"hk": A}, "2026-10-01")
    runs = [(date(2026, 10, 2) + timedelta(days=n)).isoformat() for n in range(60)]
    ran = [d for d in runs if U.to_remove(r, {"hk": _m(A)}, d) == {}]
    assert len(ran) == 40 and ran[0] == "2026-10-02" and ran[-1] == "2026-11-10"


def test_a_new_model_not_yet_run_is_never_removed():
    r = U.update(U.empty(), {"hk": A}, "2026-08-01")
    assert U.to_remove(r, {"hk": _m(B)}, "2026-11-16") == {}


def _with_ledger(monkeypatch, days: dict):
    """A fake shadow ledger: day -> {hotkey: image ref}."""
    from hope.backtest import shadow
    monkeypatch.setattr(shadow, "shadow_days", lambda root: sorted(days))
    monkeypatch.setattr(U, "_ran_on", lambda root, day: days[day])


def test_off_nothing_is_removed_but_the_record_is_kept(tmp_path, monkeypatch):
    _with_ledger(monkeypatch, {"2026-08-01": {"hk": A}})
    assert U.apply(str(tmp_path), {"hk": _m(A)}, "2026-11-16", environ={}) == {}
    rec = U.load(str(tmp_path))
    assert rec["hotkeys"]["hk"]["since"] == "2026-08-01" and rec["removed"] == {}
    assert U.removed_hotkeys(str(tmp_path), "2026-11-16") == frozenset()


def test_on_removal_sticks_to_the_digest_for_every_hotkey(tmp_path, monkeypatch):
    _with_ledger(monkeypatch, {"2026-08-01": {"hk": A}})
    root = str(tmp_path)
    assert U.apply(root, {"hk": _m(A)}, "2026-11-16", environ=ON) == {"hk": "unchanged"}
    assert U.removed_hotkeys(root, "2026-11-16") == frozenset({"hk"})
    assert U.removed_hotkeys(root, "2026-11-15") == frozenset()
    # the same digest committed by another hotkey is refused, and that day it is not charged either
    out = U.apply(root, {"hk": _m(A), "other": _m(A)}, "2026-11-17", environ=ON)
    assert out == {"hk": "removed_digest", "other": "removed_digest"}
    assert "other" in U.removed_hotkeys(root, "2026-11-17")


def test_a_new_model_that_runs_brings_the_hotkey_back(tmp_path, monkeypatch):
    days = {"2026-08-01": {"hk": A}}
    _with_ledger(monkeypatch, days)
    root = str(tmp_path)
    U.apply(root, {"hk": _m(A)}, "2026-11-16", environ=ON)
    days["2026-11-20"] = {"hk": B}
    assert U.apply(root, {"hk": _m(B)}, "2026-11-21", environ=ON) == {}
    assert "hk" not in U.removed_hotkeys(root, "2026-11-21")


def test_the_status_feed_shows_each_miners_days():
    from hope.publication.registration_status_feed import add_unchanged_days
    rec = U.update(U.empty(), {"hk": A, "new": A}, "2026-10-01")
    rec["removed"] = {}
    rows = [{"hotkey": "hk", "current_digest": "a" * 16},
            {"hotkey": "new", "current_digest": "b" * 16},      # committed a model not run yet
            {"hotkey": "none", "current_digest": None}]
    out = add_unchanged_days(rows, rec, "2026-11-04")
    assert out[0] == {"hotkey": "hk", "current_digest": "a" * 16, "model_since": "2026-10-02",
                      "days_unchanged": 34, "not_run_from": "2026-11-11"}
    assert "days_unchanged" not in out[1] and "days_unchanged" not in out[2]


def test_the_feed_never_shows_a_day_before_the_rule_starts():
    """Shown from the notice day: a model past its 40 days stops on the rule's first day."""
    from hope.publication.registration_status_feed import add_unchanged_days
    rec = U.update(U.empty(), {"old": A, "young": B}, "2026-09-01")
    rec = U.update(rec, {"young": "repo/y@sha256:" + "c" * 64}, "2026-11-01")
    rows = [{"hotkey": "old", "current_digest": "a" * 16},
            {"hotkey": "young", "current_digest": "c" * 16}]
    out = add_unchanged_days(rows, rec, "2026-11-09", start="2026-11-16")
    assert out[0]["not_run_from"] == "2026-11-16" and out[0]["days_unchanged"] == 69
    assert out[1]["not_run_from"] == "2026-12-12"
    assert U.switch_date({U.SWITCH_ENV: "2026-11-16"}) == "2026-11-16"
    assert U.switch_date({U.SWITCH_ENV: "soon"}) is None and U.switch_date({}) is None


# ---- a removed model re-added under a new digest -------------------------------

from hope.scoring.duplication import LineageParams

PARAMS = LineageParams(corr_min=0.99, sign_agree_min=0.95, distance_max=0.02,
                       disagree_max=0.05, version="t")


def _preds(scale=1.0, n=40):
    return {f"e{i}": {"7": {"cost_delta_pct": {"p10": 0.0, "p50": scale * (i - 20) / 10, "p90": 1.0}}}
            for i in range(n)}


def _outcomes(n=40):
    from hope.backtest.gate import OutcomeRow
    return [OutcomeRow(episode_id=f"e{i}", horizon_days=7, cost_delta_pct=(i - 20) / 10,
                       conversions_delta_pct=None, efficiency_delta_pct=None) for i in range(n)]


def test_a_rebuilt_copy_matches_and_an_independent_model_does_not():
    probes = {"sha256:" + "a" * 64: _preds()}
    hit = U.same_as_removed(_preds(), _outcomes(), probes, PARAMS)
    assert hit["removed_digest"] == "sha256:" + "a" * 64 and hit["params_version"] == "t"
    other = {f"e{i}": {"7": {"cost_delta_pct": {"p50": ((i * 7) % 13) / 5 - 1}}} for i in range(40)}
    assert U.same_as_removed(other, _outcomes(), probes, PARAMS) is None
    # the copy check unset decides nothing
    assert U.same_as_removed(_preds(), _outcomes(), probes, LineageParams()) is None


def test_probes_are_kept_per_corpus_and_only_missing_ones_are_due(tmp_path):
    root = str(tmp_path)
    rec = U.empty()
    rec["removed"] = {"sha256:" + "a" * 64: {"hotkey": "x", "removed_on": "2026-11-16", "image_ref": "r@sha256:" + "a" * 64},
                      "sha256:" + "b" * 64: {"hotkey": "y", "removed_on": "2026-11-17", "image_ref": "r@sha256:" + "b" * 64}}
    U.save(root, rec)
    assert [d for d, _ in U.probes_due(root, "c1")] == ["sha256:" + "a" * 64, "sha256:" + "b" * 64]
    assert len(U.probes_due(root, "c1", {U.PROBES_PER_DAY_ENV: "1"})) == 1
    U.save_probe(root, "c1", "sha256:" + "a" * 64, _preds())
    assert [d for d, _ in U.probes_due(root, "c1")] == ["sha256:" + "b" * 64]
    assert set(U.load_probes(root, "c1")) == {"sha256:" + "a" * 64}
    assert len(U.probes_due(root, "c2")) == 2          # a new corpus needs new probes


def test_the_gate_refuses_a_model_the_check_matches_before_signing():
    from hope.backtest.container_runner import RunResult
    from hope.backtest.gate_service import gate_submission
    preds = _preds()

    def run(_i, eps, _t):
        return RunResult(ok=True, predictions=preds, episodes_in=len(eps), predictions_out=40)

    eps = [{"episode_id": f"e{i}"} for i in range(40)]
    ok = gate_submission("r@sha256:" + "1" * 64, eps, _outcomes(), generated_at="2026-11-17T08:00:00+00:00",
                         runner=run, determinism_sample=0)
    assert ok["verdict"]["admitted"] is True
    res = gate_submission("r@sha256:" + "1" * 64, eps, _outcomes(), generated_at="2026-11-17T08:00:00+00:00",
                          runner=run, determinism_sample=0,
                          post_check=lambda p: U.same_as_removed(p, _outcomes(), {"sha256:" + "a" * 64: preds}, PARAMS))
    assert res["verdict"]["admitted"] is False
    assert res["verdict"]["reason"] == "same_as_removed_model"


# ---- the count is per digest (12 Nov 2026) --------------------------------------------

def test_going_back_to_a_digest_keeps_its_first_day():
    """A hotkey that switches A -> B -> A does not restart A's count."""
    r = U.update(U.empty(), {"hk": A}, "2026-10-01")
    r = U.update(r, {"hk": B}, "2026-10-20")
    assert r["hotkeys"]["hk"] == {"digest": U.bare_digest(B), "since": "2026-10-20"}
    r = U.update(r, {"hk": A}, "2026-11-01")
    assert r["hotkeys"]["hk"] == {"digest": U.bare_digest(A), "since": "2026-10-01"}
    assert U.to_remove(r, {"hk": _m(A)}, "2026-11-11") == {"hk": "unchanged"}


def test_switching_between_two_digests_cannot_avoid_day_41():
    from datetime import date, timedelta
    r = U.empty()
    for n in range(60):
        day = (date(2026, 10, 1) + timedelta(days=n)).isoformat()
        r = U.update(r, {"hk": A if (n // 10) % 2 == 0 else B}, day)
    assert r["hotkeys"]["hk"]["since"] in ("2026-10-01", "2026-10-11")
    active = {"hk": _m(A if ((59 // 10) % 2 == 0) else B)}
    assert U.to_remove(r, active, "2026-11-30") == {"hk": "unchanged"}


def test_the_same_digest_on_another_hotkey_counts_from_its_first_run():
    r = U.update(U.empty(), {"hk": A}, "2026-10-01")
    r = U.update(r, {"other": A}, "2026-10-25")
    assert r["hotkeys"]["other"]["since"] == "2026-10-01"


def test_a_day_not_run_still_does_not_reset_the_count():
    r = U.update(U.empty(), {"hk": A}, "2026-10-01")
    r = U.update(r, {}, "2026-10-05")
    r = U.update(r, {"hk": A}, "2026-10-09")
    assert r["hotkeys"]["hk"]["since"] == "2026-10-01"


def test_an_old_record_is_rebuilt_from_the_shadow_days_keeping_removals(tmp_path, monkeypatch):
    """A record written before the count was per digest has no "digests": it is
    rebuilt from every shadow day, and its removals are kept."""
    _with_ledger(monkeypatch, {"2026-10-01": {"hk": A}, "2026-10-20": {"hk": B}, "2026-11-01": {"hk": A}})
    old = {"through": "2026-11-01", "hotkeys": {"hk": {"digest": U.bare_digest(A), "since": "2026-11-01"}},
           "removed": {"sha256:z": {"hotkey": "x", "removed_on": "2026-11-16"}},
           "by_day": {"2026-11-16": ["x"]}}
    U.save(str(tmp_path), old)
    r = U.refresh(str(tmp_path))
    assert r["hotkeys"]["hk"] == {"digest": U.bare_digest(A), "since": "2026-10-01"}
    assert r["removed"] == old["removed"] and r["by_day"] == old["by_day"]
    assert r["through"] == "2026-11-01" and "digests" in U.load(str(tmp_path))
