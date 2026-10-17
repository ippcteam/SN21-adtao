"""The curve-tail preview removes its ledger copies when it ends (17 Oct 2026:
the 9 Sep run left 6.1 GB of copies on the executor disk)."""
import os
from types import SimpleNamespace

from scripts import preview_curve_tail as P


def _ledger(root):
    os.makedirs(os.path.join(root, "standing"))
    open(os.path.join(root, "standing", "a.jsonl"), "w").write("{}\n")


def test_copies_are_removed_after_the_preview_and_after_a_failure(tmp_path, monkeypatch):
    led, work = str(tmp_path / "ledger"), str(tmp_path / "work")
    _ledger(led)
    seen = []

    def fake_alloc(copy, day, eps, environ=None):
        seen.append(os.path.isdir(copy))
        return SimpleNamespace(gated=False, earning_set_size=1, weights={"hk": 1.0}, standings={"hk": 0.5})
    monkeypatch.setattr(P, "allocation_from_ledger", fake_alloc)
    assert P.main(["--day", "2026-10-17", "--tails", "0.5,0.8", "--ledger-root", led, "--workdir", work]) == 0
    assert seen == [True, True] and not os.path.exists(os.path.join(work, "curve_preview"))

    monkeypatch.setattr(P, "allocation_from_ledger", lambda *a, **k: 1 / 0)
    try:
        P.main(["--day", "2026-10-17", "--tails", "0.5", "--ledger-root", led, "--workdir", work])
    except ZeroDivisionError:
        pass
    assert not os.path.exists(os.path.join(work, "curve_preview"))


def test_keep_leaves_them(tmp_path, monkeypatch):
    led, work = str(tmp_path / "ledger"), str(tmp_path / "work")
    _ledger(led)
    monkeypatch.setattr(P, "allocation_from_ledger",
                        lambda *a, **k: SimpleNamespace(gated=False, earning_set_size=1, weights={"hk": 1.0}, standings={}))
    P.main(["--day", "2026-10-17", "--tails", "0.5", "--ledger-root", led, "--workdir", work, "--keep"])
    assert os.path.isdir(os.path.join(work, "curve_preview", "ledger_0.5"))
