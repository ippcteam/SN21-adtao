"""docs/SN21_MODEL_PRIVACY.md as a state machine: closed while earning,
released on the first of 5 days out / dethroned / 45 days."""
import json
from datetime import date, timedelta

import pytest

from hope.privacy import release_ledger as R

D0 = date(2026, 11, 1)


def run(days, start=D0, **kw):
    """days: list of (earning dict, withheld set, champion) per consecutive day."""
    state, out = R.ReleaseState(), []
    for i, (earning, withheld, champ) in enumerate(days):
        state, rel = R.observe_day(state, start + timedelta(days=i), earning, withheld, champ, **kw)
        out += rel
    return state, out


def test_closed_while_earning_with_closed_since():
    state, rel = run([({"hkA": "sha256:a"}, set(), None)] * 3)
    assert rel == [] and R.is_closed(state, "sha256:a")
    assert state.digests["sha256:a"].closed_since == D0


def test_five_consecutive_days_out_releases_one_bad_day_does_not():
    days = [({"hkA": "sha256:a"}, set(), None)] + [({}, set(), None)] * 4 + [({"hkA": "sha256:a"}, set(), None)]
    state, rel = run(days)
    assert rel == [] and state.digests["sha256:a"].days_out == 0          # back in: the run resets
    state, rel = run([({"hkA": "sha256:a"}, set(), None)] + [({}, set(), None)] * 5)
    assert [(r["reason"], r["released_on"]) for r in rel] == [("left_earning_set", (D0 + timedelta(days=5)).isoformat())]


def test_anti_copy_days_do_not_count_toward_the_five():
    days = [({"hkA": "sha256:a"}, set(), None)] + [({}, {"hkA"}, None)] * 10 + [({}, set(), None)] * 4
    state, rel = run(days)
    assert rel == [] and state.digests["sha256:a"].days_out == 4


def test_a_new_digest_starts_its_own_clock_and_the_old_one_leaves():
    days = [({"hkA": "sha256:a"}, set(), None)] * 2 + [({"hkA": "sha256:b"}, set(), None)] * 5
    state, rel = run(days)
    assert [r["digest"] for r in rel] == ["sha256:a"] and rel[0]["reason"] == "left_earning_set"
    assert state.digests["sha256:b"].closed_since == D0 + timedelta(days=2)


def test_dethroned_champion_released_even_if_still_earning():
    days = [({"hkA": "sha256:a", "hkB": "sha256:b"}, set(), "hkA")] * 3 + \
           [({"hkA": "sha256:a", "hkB": "sha256:b"}, set(), "hkB")]
    state, rel = run(days)
    assert [(r["digest"], r["reason"]) for r in rel] == [("sha256:a", "dethroned")]
    assert R.is_closed(state, "sha256:b")


def test_an_empty_seat_is_not_a_dethrone_but_the_next_champion_is():
    days = [({"hkA": "sha256:a", "hkB": "sha256:b"}, set(), "hkA"),
            ({"hkA": "sha256:a", "hkB": "sha256:b"}, set(), None),
            ({"hkA": "sha256:a", "hkB": "sha256:b"}, set(), "hkB")]
    _, rel = run(days)
    assert [(r["digest"], r["reason"], r["released_on"]) for r in rel] == \
           [("sha256:a", "dethroned", (D0 + timedelta(days=2)).isoformat())]


def test_max_embargo_at_45_days_even_for_the_champion():
    days = [({"hkA": "sha256:a"}, set(), "hkA")] * 46
    state, rel = run(days)
    assert [(r["reason"], r["released_on"]) for r in rel] == [("max_embargo", (D0 + timedelta(days=45)).isoformat())]


def test_released_never_closes_again():
    days = [({"hkA": "sha256:a"}, set(), None)] + [({}, set(), None)] * 5 + [({"hkA": "sha256:a"}, set(), None)] * 3
    state, rel = run(days)
    assert len(rel) == 1 and not R.is_closed(state, "sha256:a")


def test_days_must_move_forward():
    state, _ = R.observe_day(R.ReleaseState(), D0, {"h": "d"})
    with pytest.raises(ValueError):
        R.observe_day(state, D0, {"h": "d"})


def test_same_day_triggers_follow_the_published_table_order():
    # out for the 5th day AND dethroned the same day -> left_earning_set (#1 before #2)
    days = [({"hkA": "sha256:a"}, set(), "hkA")] + [({"hkB": "sha256:b"}, set(), "hkA")] * 4 + \
           [({"hkB": "sha256:b"}, set(), "hkB")]
    _, rel = run(days)
    assert [(r["digest"], r["reason"]) for r in rel] == [("sha256:a", "left_earning_set")]


def test_day_inputs_and_replay_read_the_ledger(tmp_path):
    day = D0
    (tmp_path / f"intended_weights_{day}.json").write_text(json.dumps({
        "weights": {"hkA": 0.6, "hkB": 0.0, "hkC": 0.4}, "champion": "hkA",
        "collapse_audit": {"groups": [{"excluded": ["hkD"]}]}}))
    sh = tmp_path / "shadow" / day.isoformat()
    sh.mkdir(parents=True)
    (sh / "hkA.jsonl").write_text(json.dumps({"image_digest": "repo/m@sha256:aaa"}) + "\n")
    (sh / "hkC.jsonl").write_text(json.dumps({"image_digest": "repo/m@sha256:ccc"}) + "\n")
    inp = R.day_inputs(str(tmp_path), day)
    assert inp == {"earning": {"hkA": "sha256:aaa", "hkC": "sha256:ccc"},
                   "copy_withheld": {"hkD"}, "champion": "hkA"}
    assert R.day_inputs(str(tmp_path), day + timedelta(days=1)) is None    # no run: not an observation
    state, rel, observed = R.replay(str(tmp_path), day, day + timedelta(days=3))
    assert observed == 1 and rel == [] and R.is_closed(state, "sha256:aaa")
