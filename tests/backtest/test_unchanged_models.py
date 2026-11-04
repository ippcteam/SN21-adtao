"""Models unchanged for 40 days are not run from day 41 (Sir Rob, KzoNlmEF, 2 Nov 2026)."""

from types import SimpleNamespace

from hope.backtest import unchanged_models as U

A = "repo/a@sha256:" + "a" * 64
B = "repo/a@sha256:" + "b" * 64


def test_off_until_the_date():
    assert U.enabled("2026-11-16", {}) is False
    assert U.enabled("2026-11-15", {U.SWITCH_ENV: "2026-11-16"}) is False
    assert U.enabled("2026-11-16", {U.SWITCH_ENV: "2026-11-16"}) is True
    assert U.enabled("2026-11-16", {U.SWITCH_ENV: "soon"}) is False


def test_the_count_starts_at_the_first_run_and_a_new_digest_restarts_it():
    r = U.update({}, {"hk": A}, "2026-10-01")
    r = U.update(r, {"hk": A}, "2026-10-02")
    assert r["hk"] == {"digest": "sha256:" + "a" * 64, "since": "2026-10-01"}
    r = U.update(r, {"hk": B}, "2026-10-05")
    assert r["hk"]["since"] == "2026-10-05"


def test_a_missed_day_does_not_reset_the_count():
    r = U.update({}, {"hk": A}, "2026-10-01")
    r = U.update(r, {}, "2026-10-02")
    r = U.update(r, {"hk": A}, "2026-10-03")
    assert r["hk"]["since"] == "2026-10-01"


def test_day_40_runs_and_day_41_does_not():
    r = U.update({}, {"hk": A}, "2026-10-01")
    active = {"hk": SimpleNamespace(image_digest=A)}
    st = U.standing(r["hk"], "2026-11-09")
    assert st == {"digest_since": "2026-10-01", "days_unchanged": 40, "removal_on": "2026-11-10"}
    assert U.to_remove(r, active, "2026-11-09") == {}
    assert set(U.to_remove(r, active, "2026-11-10")) == {"hk"}


def test_a_new_model_not_yet_run_is_never_removed():
    r = U.update({}, {"hk": A}, "2026-08-01")
    assert U.to_remove(r, {"hk": SimpleNamespace(image_digest=B)}, "2026-11-16") == {}


def test_remember_seeds_saves_and_reloads(tmp_path, monkeypatch):
    monkeypatch.setattr(U, "seed_from_ledger", lambda root: {"old": {"digest": "sha256:x", "since": "2026-09-01"}})
    rec = U.remember(str(tmp_path), {"hk": A}, "2026-11-04")
    assert rec["old"]["since"] == "2026-09-01" and rec["hk"]["since"] == "2026-11-04"
    assert U.load(str(tmp_path)) == rec
