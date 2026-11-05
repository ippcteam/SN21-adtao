"""5 Nov 2026: 79 of 150 models failed disk_low in seconds while a few large
images were unpacked, with 12 GB free before and after. Low disk while another
image is in flight is waited out; low disk with nothing in flight still fails."""

from hope.backtest import local_executor as le

GB = 1 << 30


def test_waits_for_in_flight_image_to_free_disk(monkeypatch):
    frees = iter([1 * GB, 1 * GB, 5 * GB])
    monkeypatch.setattr(le, "_free_disk_bytes", lambda p: next(frees))
    monkeypatch.setattr(le, "_IN_FLIGHT", 2)
    slept = []
    assert le._wait_for_disk("/x", sleep=slept.append, clock=lambda: 0.0) == 5 * GB
    assert len(slept) == 2


def test_nothing_in_flight_fails_at_once(monkeypatch):
    monkeypatch.setattr(le, "_free_disk_bytes", lambda p: 1 * GB)
    monkeypatch.setattr(le, "_IN_FLIGHT", 0)
    slept = []
    assert le._wait_for_disk("/x", sleep=slept.append) == 1 * GB
    assert slept == []


def test_gives_up_at_the_deadline(monkeypatch):
    monkeypatch.setattr(le, "_free_disk_bytes", lambda p: 1 * GB)
    monkeypatch.setattr(le, "_IN_FLIGHT", 1)
    monkeypatch.setenv(le.DISK_WAIT_ENV, "10")
    t = iter(range(0, 100, 4))
    slept = []
    assert le._wait_for_disk("/x", sleep=slept.append, clock=lambda: float(next(t))) == 1 * GB
    assert 1 <= len(slept) <= 3


def test_in_flight_count_returns_to_zero(monkeypatch, tmp_path):
    from hope.backtest.oci_pull import PullError

    def boom(*a, **k):
        assert le._IN_FLIGHT == 1
        raise PullError("nope")
    monkeypatch.setattr(le, "pull_and_unpack", boom)
    monkeypatch.setattr(le, "_free_disk_bytes", lambda p: 50 * GB)
    r = le.run_basket_local("repo", "sha256:" + "0" * 64, [{"episode_id": "e1"}], workdir_root=str(tmp_path))
    assert not r.ok and r.error.startswith("pull_failed")
    assert le._IN_FLIGHT == 0
