"""Registration-status join — the pure assembler that answers
"admitted but shows nothing" per hotkey."""

from types import SimpleNamespace

from hope.publication.registration_status_feed import assemble, _hex, _short


def _model(digest):
    return SimpleNamespace(image_digest=digest, hotkey="x", admitted_at="2026-09-04")


def test_active_admitted_unscored_shows_first_settle():
    reg = {"active": {"hkA": _model("repo@sha256:" + "a" * 64)},
           "pending_admission": {}}
    rows = assemble(reg, {"hkA": 103}, {}, set(), {"hkA": "2026-09-18"})
    r = rows[0]
    assert r["status"] == "active" and r["uid"] == 103
    assert r["current_digest"] == "a" * 16
    assert r["scored"] is False
    assert r["first_settle_date"] == "2026-09-18"   # settlement lag, not a fault


def test_active_scored_hides_first_settle():
    reg = {"active": {"hkA": _model("sha256:" + "b" * 64)},
           "pending_admission": {}}
    rows = assemble(reg, {"hkA": 5}, {}, {"hkA"}, {"hkA": "2026-09-18"})
    assert rows[0]["scored"] is True
    assert rows[0]["first_settle_date"] is None


def test_pending_vs_rejected_from_verdict():
    reg = {"active": {},
           "pending_admission": {"hkP": "sha256:" + "c" * 64,
                                 "hkR": "sha256:" + "d" * 64}}
    vmap = {"d" * 64: "rejected_gate"}
    rows = assemble(reg, {"hkP": 1, "hkR": 2}, vmap, set(), {})
    by = {r["hotkey"]: r for r in rows}
    assert by["hkP"]["status"] == "pending"    # committed, not yet judged
    assert by["hkR"]["status"] == "rejected"   # judged, did not pass


def test_hex_normalisation():
    assert _hex("repo/x@sha256:" + "e" * 64) == "e" * 64
    assert _hex("sha256:" + "F" * 64) == "f" * 64
    assert _short("sha256:" + "1" * 64) == "1" * 16
    assert _hex(None) is None


def test_sorted_by_uid():
    reg = {"active": {"hkB": _model("sha256:" + "2" * 64)},
           "pending_admission": {"hkA": "sha256:" + "3" * 64}}
    rows = assemble(reg, {"hkB": 9, "hkA": 2}, {}, set(), {})
    assert [r["uid"] for r in rows] == [2, 9]
