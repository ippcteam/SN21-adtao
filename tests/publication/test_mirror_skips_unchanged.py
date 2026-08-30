"""Immutable documents ship once, everything else ships every run.

WHY THIS EXISTS
    The sync re-sent every published receipt on every run. The three largest
    are 35MB, 29MB and 13MB, and those uploads timed out, so a run that had
    published perfectly well reported a failed sync and turned the heartbeat
    red. Nothing was ever missing — the same frozen bytes were being pushed
    daily — but a real failure and this one looked identical, which is how a
    health signal stops being one.

    The danger in fixing it is the opposite mistake: skipping a document the
    mirror does NOT hold leaves a day nobody can verify, and unlike a slow
    upload that failure is silent. So the tests below lean on the skip being
    earned — confirmed by the mirror, for that exact content — and on
    everything that legitimately changes still shipping.
"""

import json
import os

import pytest

from hope.publication import mirror_sync


def _items():
    return [
        {"path": "/v1/daily/2026-08-23/receipt", "body": {"a": 1}},
        {"path": "/v1/daily/2026-08-23/accuracy", "body": {"b": 2}},
        {"path": "/v1/daily/2026-08-23/proof", "body": {"c": 3}},
        {"path": "/v1/daily/root", "body": {"d": 4}},
        {"path": "/v1/daily/index", "body": {"e": 5}},
    ]


@pytest.fixture
def synced(tmp_path, monkeypatch):
    """Runs sync_mirror against a fake mirror; returns a callable that
    reports which paths were POSTed each time."""
    sent: list[list[str]] = []

    def fake_post(api_url, api_key, items, timeout):
        sent.append([it["path"] for it in items])
        return {"stored": len(items), "rejected": []}

    monkeypatch.setattr(mirror_sync, "_post", fake_post)
    monkeypatch.setattr(mirror_sync, "build_mirror_items",
                        lambda root, recent_days=None: _items())

    def run():
        sent.clear()
        out = mirror_sync.sync_mirror(str(tmp_path), "http://ops", "k")
        return [p for batch in sent for p in batch], out

    return run


class TestTheFirstRunShipsEverything:
    def test_nothing_is_skipped_without_a_record(self, synced):
        paths, out = synced()
        assert len(paths) == 5
        assert out["skipped_unchanged"] == 0


class TestTheSecondRunSkipsOnlyTheFrozenOnes:
    def test_receipts_and_accuracy_are_not_re_sent(self, synced):
        synced()
        paths, out = synced()
        assert "/v1/daily/2026-08-23/receipt" not in paths
        assert "/v1/daily/2026-08-23/accuracy" not in paths
        assert out["skipped_unchanged"] == 2

    def test_proof_root_and_index_always_ship(self, synced):
        """The root rolls every time a day publishes, so the proofs and the
        index derived from it are different documents each run. Skipping
        those would serve a stale root — worse than a slow upload."""
        synced()
        paths, _ = synced()
        assert "/v1/daily/2026-08-23/proof" in paths
        assert "/v1/daily/root" in paths
        assert "/v1/daily/index" in paths


class TestASkipHasToBeEarned:
    def test_changed_content_ships_again(self, tmp_path, monkeypatch):
        """The record is keyed on content, not just the path. A receipt that
        somehow differs must not be masked by an older confirmation."""
        sent = []
        monkeypatch.setattr(mirror_sync, "_post",
                            lambda u, k, items, t: (
                                sent.append([i["path"] for i in items]),
                                {"stored": len(items), "rejected": []})[1])
        first = [{"path": "/v1/daily/2026-08-23/receipt", "body": {"a": 1}}]
        second = [{"path": "/v1/daily/2026-08-23/receipt", "body": {"a": 999}}]
        monkeypatch.setattr(mirror_sync, "build_mirror_items",
                            lambda r, recent_days=None: first)
        mirror_sync.sync_mirror(str(tmp_path), "http://ops", "k")
        monkeypatch.setattr(mirror_sync, "build_mirror_items",
                            lambda r, recent_days=None: second)
        sent.clear()
        mirror_sync.sync_mirror(str(tmp_path), "http://ops", "k")
        assert sent and "/v1/daily/2026-08-23/receipt" in sent[0]

    def test_a_rejected_item_is_never_recorded_as_shipped(self, tmp_path, monkeypatch):
        """Recording a rejection as success would mean the mirror never
        receives that day again — a silent hole."""
        monkeypatch.setattr(mirror_sync, "build_mirror_items",
                            lambda r, recent_days=None: [
                                {"path": "/v1/daily/2026-08-23/receipt",
                                 "body": {"a": 1}}])
        monkeypatch.setattr(mirror_sync, "_post", lambda u, k, items, t: {
            "stored": 0,
            "rejected": [{"path": "/v1/daily/2026-08-23/receipt",
                          "error": "too big"}]})
        mirror_sync.sync_mirror(str(tmp_path), "http://ops", "k")

        sent = []
        monkeypatch.setattr(mirror_sync, "_post",
                            lambda u, k, items, t: (
                                sent.append([i["path"] for i in items]),
                                {"stored": 1, "rejected": []})[1])
        mirror_sync.sync_mirror(str(tmp_path), "http://ops", "k")
        assert sent and "/v1/daily/2026-08-23/receipt" in sent[0]

    def test_a_failed_post_is_never_recorded_as_shipped(self, tmp_path, monkeypatch):
        monkeypatch.setattr(mirror_sync, "build_mirror_items",
                            lambda r, recent_days=None: [
                                {"path": "/v1/daily/2026-08-23/receipt",
                                 "body": {"a": 1}}])

        def boom(*a, **k):
            raise TimeoutError("gateway")

        monkeypatch.setattr(mirror_sync, "_post", boom)
        with pytest.raises(mirror_sync.MirrorSyncError):
            mirror_sync.sync_mirror(str(tmp_path), "http://ops", "k")

        sent = []
        monkeypatch.setattr(mirror_sync, "_post",
                            lambda u, k, items, t: (
                                sent.append([i["path"] for i in items]),
                                {"stored": 1, "rejected": []})[1])
        mirror_sync.sync_mirror(str(tmp_path), "http://ops", "k")
        assert sent and "/v1/daily/2026-08-23/receipt" in sent[0]

    def test_a_lost_record_ships_everything_again(self, tmp_path, synced):
        """Fail-safe direction: an unreadable record costs a re-send, not a
        missing day."""
        synced()
        os.remove(mirror_sync._shipped_path(str(tmp_path)))
        paths, out = synced()
        assert len(paths) == 5
        assert out["skipped_unchanged"] == 0

    def test_the_record_is_not_posted_to_the_mirror(self, synced, tmp_path):
        """The digest is bookkeeping, not feed content. It is kept beside the
        items because the ingest endpoint validates their shape."""
        synced()
        _paths, _out = synced()
        with open(mirror_sync._shipped_path(str(tmp_path))) as fh:
            rec = json.load(fh)
        assert set(rec) == {"/v1/daily/2026-08-23/receipt",
                            "/v1/daily/2026-08-23/accuracy"}
