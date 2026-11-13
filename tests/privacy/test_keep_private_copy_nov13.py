"""The operator's private copy of each running model (hope.privacy.keep), 13 Nov 2026.

A removed model's probe and a release copy pulled from the miner's image, so a
miner who deleted or closed it escaped the re-add check and the release. Each
run now copies the models it ran into a private repository of ours, and both
read that copy first. Nothing is copied unless GitHub reports the repository
private, and our login is only sent for that repository.
"""
import io
import json
from datetime import date, timedelta

from hope.backtest import oci_pull as O
from hope.backtest import unchanged_models as U
from hope.privacy import keep as K
from hope.privacy import release_step as S

KEEP = "ghcr.io/adtaoai/sn21-kept"
PUBLIC = "ghcr.io/adtaoai/sn21-released"
AUTH = json.dumps({"username": "u", "password": "p"})
ENV = {K.KEEP_ENV: KEEP, S.MIRROR_ENV: PUBLIC, S.MIRROR_AUTH_ENV: AUTH}
DA, DB = "sha256:" + "a" * 64, "sha256:" + "b" * 64


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener(visibility):
    def open_(req, timeout=30):
        if visibility is None:
            raise O.urllib.error.URLError("no answer")
        assert req.full_url.endswith("/orgs/adtaoai/packages/container/sn21-kept")
        assert req.headers["Authorization"] == "Bearer p"
        return _Resp(json.dumps({"visibility": visibility}).encode())
    return open_


def _ran(root, day, ran, since=None):
    sh = root / "shadow" / day
    sh.mkdir(parents=True, exist_ok=True)
    for hk, ref in ran.items():
        (sh / f"{hk}.jsonl").write_text(json.dumps({"image_digest": ref, "predictions_out": 5}) + "\n")
    U.save(str(root), {"through": day, "hotkeys": {hk: {"digest": U.bare_digest(r), "since": (since or {}).get(hk, day)}
                                                  for hk, r in ran.items()}, "digests": {}, "runs": {}})


# ---- private or nothing ---------------------------------------------------------

def test_the_kept_repository_is_never_the_public_one_or_inside_it():
    assert K.target(ENV) == KEEP
    for bad in (PUBLIC, PUBLIC + "/kept", "ghcr.io/adtaoai", KEEP + ":latest", KEEP + "@" + DA, ""):
        assert K.target({**ENV, K.KEEP_ENV: bad}) is None, bad


def test_nothing_is_copied_unless_github_says_private(tmp_path):
    _ran(tmp_path, "2026-11-12", {"hk": f"ghcr.io/miner/m@{DA}"})
    calls = []

    def mirror(*a, **k):
        calls.append(a)
        return {"target": f"{KEEP}@{a[1]}"}

    for vis in ("public", "internal", None):
        s = K.run_keep_step(str(tmp_path), ENV, mirror_fn=mirror, opener=opener(vis))
        assert "skipped" in s and "not private" in s["skipped"]
    assert K.run_keep_step(str(tmp_path), {S.MIRROR_ENV: PUBLIC}, mirror_fn=mirror)["skipped"]
    assert K.run_keep_step(str(tmp_path), {**ENV, K.KEEP_ENV: PUBLIC}, mirror_fn=mirror,
                           opener=opener("private"))["skipped"]
    assert K.run_keep_step(str(tmp_path), {K.KEEP_ENV: KEEP}, mirror_fn=mirror,
                           opener=opener("private"))["skipped"] == "no credentials for the kept repository"
    assert calls == [] and not (tmp_path / K.KEPT_FILE).exists()


def test_another_registry_is_never_treated_as_private():
    assert K._github_visibility("docker.io/adtaoai/kept", "p", opener("private")) is None


# ---- the copies -----------------------------------------------------------------

def test_a_private_repository_gets_the_models_closest_to_40_days_first(tmp_path):
    _ran(tmp_path, "2026-11-12", {"new": f"ghcr.io/miner/n@{DB}", "old": f"ghcr.io/miner/o@{DA}"},
         since={"new": "2026-11-10", "old": "2026-10-08"})
    calls = []

    def mirror(src, dg, target, source_credentials=None, target_credentials=None):
        calls.append((src, dg, target, source_credentials, target_credentials))
        return {"target": f"{target}@{dg}"}

    s = K.run_keep_step(str(tmp_path), {**ENV, K.PER_DAY_ENV: "1"}, mirror_fn=mirror, opener=opener("private"))
    # The miner's image is read anonymously; only our repository gets our login.
    assert calls == [("ghcr.io/miner/o", DA, KEEP, (), ("u", "p"))]
    assert s["copied"] == 1 and s["left"] == 1
    assert K.kept_ref(str(tmp_path), DA, ENV) == f"{KEEP}@{DA}"
    K.run_keep_step(str(tmp_path), ENV, mirror_fn=mirror, opener=opener("private"))
    assert [c[1] for c in calls] == [DA, DB]                 # each digest once


def test_a_failed_copy_is_retried_and_the_step_never_raises(tmp_path):
    _ran(tmp_path, "2026-11-12", {"hk": f"ghcr.io/miner/m@{DA}"})

    def broken(*a, **k):
        raise O.PullError("gone")

    s = K.run_keep_step(str(tmp_path), ENV, mirror_fn=broken, opener=opener("private"))
    assert s["copied"] == 0 and s["failed"][0]["digest"] == DA
    assert K.due(str(tmp_path)) == [(DA, f"ghcr.io/miner/m@{DA}")]


def test_the_time_limit_stops_the_copies(tmp_path):
    _ran(tmp_path, "2026-11-12", {"a": f"ghcr.io/m/a@{DA}", "b": f"ghcr.io/m/b@{DB}"})
    ticks = iter([0, 0, 10_000, 10_000])
    s = K.run_keep_step(str(tmp_path), {**ENV, K.MINUTES_ENV: "1"},
                        mirror_fn=lambda *a, **k: {"target": f"{KEEP}@{a[1]}"},
                        opener=opener("private"), clock=lambda: next(ticks))
    assert s["copied"] == 1 and s["left"] == 1


def test_a_kept_ref_needs_the_repository_in_force(tmp_path):
    (tmp_path / "privacy").mkdir()
    (tmp_path / K.KEPT_FILE).write_text(json.dumps({DA: {"target": f"{KEEP}@{DA}"}}))
    assert K.kept_ref(str(tmp_path), DA, ENV) == f"{KEEP}@{DA}"
    assert K.kept_ref(str(tmp_path), DA, {**ENV, K.KEEP_ENV: "ghcr.io/adtaoai/other"}) is None
    assert K.kept_ref(str(tmp_path), DB, ENV) is None


# ---- our login goes to our repository only --------------------------------------

def test_the_puller_sends_our_login_only_to_the_kept_repository(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv(O.PULL_AUTH_ENV, raising=False)
    assert O._Registry("ghcr.io", "adtaoai/sn21-kept").credentials == ("u", "p")
    assert O._Registry("ghcr.io", "miner/m").credentials is None
    assert O._Registry("ghcr.io", "adtaoai/sn21-released").credentials is None
    assert O._Registry("ghcr.io", "adtaoai/sn21-kept", credentials=()).credentials is None


# ---- the probe and the release read our copy first -------------------------------

def test_a_removed_model_is_probed_from_our_copy_when_we_hold_one(tmp_path, monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)
    U.save(str(tmp_path), {"hotkeys": {}, "removed": {DA: {"removed_on": "2026-11-16", "image_ref": f"ghcr.io/m/a@{DA}"},
                                       DB: {"removed_on": "2026-11-16", "image_ref": f"ghcr.io/m/b@{DB}"}}})
    (tmp_path / "privacy").mkdir()
    (tmp_path / K.KEPT_FILE).write_text(json.dumps({DA: {"target": f"{KEEP}@{DA}"}}))
    assert U.probes_due(str(tmp_path), "corpus") == [(DA, f"{KEEP}@{DA}"), (DB, f"ghcr.io/m/b@{DB}")]


def test_a_release_copies_from_our_copy_with_our_login(tmp_path):
    d0 = date(2026, 11, 2)

    def day(d, weights, ran):
        (tmp_path / f"intended_weights_{d}.json").write_text(json.dumps({"weights": weights}))
        sh = tmp_path / "shadow" / (d - timedelta(days=1)).isoformat()
        sh.mkdir(parents=True, exist_ok=True)
        for hk, ref in ran.items():
            (sh / f"{hk}.jsonl").write_text(json.dumps({"image_digest": ref}) + "\n")

    day(d0, {"hkA": 1.0}, {"hkA": "ghcr.io/x/a@sha256:aaa"})
    for i in range(1, 6):
        day(d0 + timedelta(days=i), {"hkB": 1.0}, {"hkB": "ghcr.io/y/b@sha256:bbb"})
    (tmp_path / "privacy").mkdir()
    (tmp_path / K.KEPT_FILE).write_text(json.dumps({"sha256:aaa": {"target": f"{KEEP}@sha256:aaa"}}))
    calls = []

    def mirror(src, dg, target, source_credentials=None, target_credentials=None):
        calls.append((src, dg, target, source_credentials, target_credentials))
        return {"target": f"{target}@{dg}"}

    env = {**ENV, S.RELEASE_FROM_ENV: d0.isoformat()}
    s = S.run_release_step(str(tmp_path), d0 + timedelta(days=5), environ=env, mirror_fn=mirror)
    assert s["mirror"]["copied"] == 1
    assert calls == [(KEEP, "sha256:aaa", PUBLIC, ("u", "p"), ("u", "p"))]
    mirrored = json.loads((tmp_path / S.MIRRORED_FILE).read_text())
    assert mirrored["sha256:aaa"]["copied_from"] == "kept copy"
