"""The public release list (hope/privacy/release_list.py): what is published, and only when it changes."""

import base64
import io
import json
import urllib.error

from hope.privacy import release_list as RL
from hope.privacy import release_step as RS

ON = {"SN21_RELEASE_LIST_REPO": "org/models", "SN21_RELEASE_LIST_TOKEN": "t"}
LEDGER = {"from": "2026-11-01", "as_of": "2026-11-20", "observed_days": 20, "closed": [{"digest": "sha256:c", "hotkeys": ["5C"]}],
          "releases": [
              {"digest": "sha256:b", "hotkey": "5B", "hotkeys": ["5B"], "released_on": "2026-11-19",
               "reason": "dethroned", "closed_since": "2026-11-01", "image_ref": "docker.io/m/b"},
              {"digest": "sha256:a", "hotkey": "5A", "hotkeys": ["5A", "5Z"], "released_on": "2026-11-12",
               "reason": "left_earning_set", "closed_since": "2026-11-01", "image_ref": "docker.io/m/a"}]}
MIRRORED = {"sha256:a": {"target": "ghcr.io/org/sn21-released@sha256:a"}}


class FakeGitHub:
    def __init__(self, files=None):
        self.files = dict(files or {})
        self.puts = []

    def __call__(self, path, token, method="GET", data=None):
        name = path.rsplit("/", 1)[-1]
        if method == "GET":
            if name not in self.files:
                raise urllib.error.HTTPError(path, 404, "Not Found", {}, io.BytesIO(b""))
            return {"sha": f"sha-{name}", "content": base64.b64encode(self.files[name].encode()).decode()}
        self.puts.append((name, data))
        self.files[name] = base64.b64decode(data["content"]).decode()
        return {}


def test_rows_carry_only_the_public_fields_and_the_copy():
    rows = RL.public_rows(LEDGER, MIRRORED)
    assert [r["digest"] for r in rows] == ["sha256:a", "sha256:b"]            # by release day
    assert set(rows[0]) == {"digest", "hotkey", "released_on", "reason", "closed_since", "copy"}
    assert rows[0]["copy"] == "ghcr.io/org/sn21-released@sha256:a" and rows[1]["copy"] is None


def test_closed_digests_and_source_repositories_are_not_published():
    gh = FakeGitHub()
    RL.publish_release_list(LEDGER, MIRRORED, ON, api=gh)
    text = gh.files["released.json"] + gh.files["README.md"]
    assert "sha256:c" not in text and "5C" not in text and "docker.io/m/" not in text and "5Z" not in text


def test_both_files_are_written_then_left_alone_when_nothing_changed():
    gh = FakeGitHub()
    out = RL.publish_release_list(LEDGER, MIRRORED, ON, api=gh)
    assert out == {"released": 2, "released.json": "written", "README.md": "written"}
    doc = json.loads(gh.files["released.json"])
    assert doc["as_of"] == "2026-11-20" and len(doc["released"]) == 2
    assert "| 2026-11-19 | dethroned | `5B` | `sha256:b` | pending |" in gh.files["README.md"]
    again = RL.publish_release_list(LEDGER, MIRRORED, ON, api=gh)
    assert again["released.json"] == "same" and again["README.md"] == "same" and len(gh.puts) == 2


def test_an_update_sends_the_current_sha():
    gh = FakeGitHub({"released.json": "old", "README.md": "old"})
    RL.publish_release_list(LEDGER, MIRRORED, ON, api=gh)
    assert all(data["sha"] == f"sha-{name}" for name, data in gh.puts)


def test_off_unless_both_switches_are_set():
    for env in ({}, {"SN21_RELEASE_LIST_REPO": "org/models"}, {"SN21_RELEASE_LIST_TOKEN": "t"}):
        assert "skipped" in RL.publish_release_list(LEDGER, MIRRORED, env, api=FakeGitHub())


def test_a_failed_write_is_reported_not_raised():
    def boom(*a, **k):
        raise urllib.error.HTTPError("x", 500, "Server Error", {}, io.BytesIO(b""))
    out = RL.publish_release_list(LEDGER, MIRRORED, ON, api=boom)
    assert out["released.json"].startswith("error") and out["README.md"].startswith("error")


def test_an_empty_list_says_so():
    gh = FakeGitHub()
    RL.publish_release_list({"from": "2026-11-01", "as_of": "2026-11-02", "releases": []}, {}, ON, api=gh)
    assert "(none yet)" in gh.files["README.md"] and json.loads(gh.files["released.json"])["released"] == []


def test_the_release_step_publishes_the_list_with_and_without_copies(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(RS, "build_ledger", lambda root, day, start: LEDGER)
    monkeypatch.setattr(RL, "publish_release_list", lambda l, m, e: calls.append(m) or {"ok": 1})
    from datetime import date
    env = {"SN21_MODEL_RELEASE_FROM": "2026-11-01"}
    out = RS.run_release_step(str(tmp_path), date(2026, 11, 20), env)
    assert out["public_list"] == {"ok": 1} and calls[-1] == {}
    env["SN21_RELEASE_MIRROR"] = "ghcr.io/org/sn21-released"
    out = RS.run_release_step(str(tmp_path), date(2026, 11, 20), env,
                              mirror_fn=lambda ref, dg, target, target_credentials=None: {"target": f"{target}@{dg}"})
    assert out["mirror"]["copied"] == 2 and set(calls[-1]) == {"sha256:a", "sha256:b"}


def test_the_release_step_stays_off_without_its_start_date(tmp_path, monkeypatch):
    monkeypatch.setattr(RL, "publish_release_list", lambda *a: (_ for _ in ()).throw(AssertionError("called")))
    from datetime import date
    assert "skipped" in RS.run_release_step(str(tmp_path), date(2026, 11, 20), {})
