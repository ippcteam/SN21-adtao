"""A hotkey whose new model waits or was rejected keeps running its last
admitted model (decided 18 Oct 2026, SN21_KEEP_LAST_ADMITTED_FROM).

On 16 Oct 8 of the 10 newly charged miners had pushed a new model that was
rejected at admission, so each lost the whole day's basket although its
previous model was admitted and running the day before."""
import json
import os

from hope.backtest import last_admitted as LA
from hope.backtest import shadow
from hope.backtest.model_registry import build_registry
from hope.publication.registration_status_feed import assemble

OLD = "sha256:" + "a" * 64
NEW = "sha256:" + "b" * 64
OTHER = "sha256:" + "c" * 64
REPO = "ghcr.io/miner/model"


def _reader(commits):
    return lambda hk: commits.get(hk, [])


def _registry(commits, admitted):
    return build_registry(list(commits), _reader(commits), admitted, "2026-10-30")


def test_switch_reads_the_date(monkeypatch):
    assert not LA.enabled("2026-10-30", {})
    assert not LA.enabled("2026-10-29", {LA.SWITCH_ENV: "2026-10-30"})
    assert LA.enabled("2026-10-30", {LA.SWITCH_ENV: "2026-10-30"})
    assert not LA.enabled("2026-10-30", {LA.SWITCH_ENV: "soon"})


def test_rejected_new_model_falls_back_to_the_last_admitted_one(tmp_path):
    root = str(tmp_path)
    admitted = {OLD}
    day1 = _registry({"hk": [(10, f"sn21-model:v1:{REPO}@{OLD}")]}, admitted)
    LA.remember(root, day1["active"], "2026-10-29", admitted)

    day2 = _registry({"hk": [(20, f"sn21-model:v1:{REPO}@{NEW}")]}, admitted)
    assert day2["active"] == {} and day2["pending_admission"] == {"hk": NEW}
    kept = LA.fallbacks(day2, LA.load(root), admitted, "2026-10-30")
    assert kept["hk"].image_digest == f"{REPO}@{OLD}"


def test_new_model_takes_over_once_admitted(tmp_path):
    root = str(tmp_path)
    LA.remember(root, _registry({"hk": [(10, f"sn21-model:v1:{REPO}@{OLD}")]}, {OLD})["active"],
                "2026-10-29", {OLD})
    reg = _registry({"hk": [(20, f"sn21-model:v1:{REPO}@{NEW}")]}, {OLD, NEW})
    assert reg["active"]["hk"].image_digest == f"{REPO}@{NEW}"
    assert LA.fallbacks(reg, LA.load(root), {OLD, NEW}, "2026-10-30") == {}
    rec = LA.remember(root, reg["active"], "2026-10-30", {OLD, NEW})
    assert rec["hk"]["image_digest"] == f"{REPO}@{NEW}"


def test_no_fallback_without_an_admitted_previous_model(tmp_path):
    reg = _registry({"hk": [(20, f"sn21-model:v1:{REPO}@{NEW}")]}, set())
    assert LA.fallbacks(reg, {}, set(), "2026-10-30") == {}
    # a remembered model that is no longer admitted is never run
    rec = {"hk": {"image_digest": f"{REPO}@{OTHER}", "since": "2026-10-01"}}
    assert LA.fallbacks(reg, rec, {OLD}, "2026-10-30") == {}


def test_a_hotkey_only_ever_falls_back_to_its_own_model(tmp_path):
    rec = {"other": {"image_digest": f"{REPO}@{OLD}", "since": "2026-10-29"}}
    reg = _registry({"hk": [(20, f"sn21-model:v1:{REPO}@{NEW}")]}, {OLD})
    assert LA.fallbacks(reg, rec, {OLD}, "2026-10-30") == {}


def test_record_seeds_from_the_shadow_ledger(tmp_path):
    root = str(tmp_path)
    for day, ref, preds in (("2026-10-15", f"{REPO}@{OLD}", 600),
                            ("2026-10-16", f"{REPO}@{NEW}", 0)):
        d = shadow.shadow_dir(root, day)
        os.makedirs(d)
        with open(os.path.join(d, "hk.jsonl"), "w") as f:
            f.write(json.dumps({"image_digest": ref, "ok": True, "predictions_out": preds}) + "\n")
        shadow.record_run_marker(root, day, 680, 1)
    rec = LA.remember(root, {}, "2026-10-17", {OLD, NEW})
    # newest day first, but a run that delivered nothing does not count
    assert rec == {"hk": {"image_digest": f"{REPO}@{OLD}", "since": "2026-10-15"}}
    assert os.path.exists(os.path.join(root, LA.RECORD))


def test_status_feed_names_the_model_still_running():
    reg = _registry({"hk": [(20, f"sn21-model:v1:{REPO}@{NEW}")]}, {OLD})
    reg["kept_last_admitted"] = LA.fallbacks(
        reg, {"hk": {"image_digest": f"{REPO}@{OLD}"}}, {OLD}, "2026-10-30")
    [row] = assemble(reg, {"hk": 7}, {"b" * 64: "rejected_gate"}, set(), {})
    assert row["status"] == "rejected"
    assert row["current_digest"] == "b" * 16 and row["running_digest"] == "a" * 16


def test_status_feed_unchanged_when_nothing_is_kept():
    reg = _registry({"hk": [(20, f"sn21-model:v1:{REPO}@{NEW}")]}, set())
    [row] = assemble(reg, {"hk": 7}, {}, set(), {})
    assert row["status"] == "pending" and "running_digest" not in row
