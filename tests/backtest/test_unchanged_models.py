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
    r = U.update(U.empty(), {"hk": A}, "2026-10-01")
    st = U.standing(r["hotkeys"]["hk"], "2026-11-09")
    assert st == {"digest_since": "2026-10-01", "days_unchanged": 40, "removal_on": "2026-11-10"}
    assert U.to_remove(r, {"hk": _m(A)}, "2026-11-09") == {}
    assert U.to_remove(r, {"hk": _m(A)}, "2026-11-10") == {"hk": "unchanged"}


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
    assert out[0] == {"hotkey": "hk", "current_digest": "a" * 16, "model_since": "2026-10-01",
                      "days_unchanged": 35, "not_run_from": "2026-11-10"}
    assert "days_unchanged" not in out[1] and "days_unchanged" not in out[2]


# ---- a removed model re-added under a new digest -------------------------------

from hope.scoring.duplication import LineageParams  # noqa: E402

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
