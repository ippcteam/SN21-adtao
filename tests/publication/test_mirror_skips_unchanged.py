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


class TestDocumentsTheMirrorAlreadyHolds:
    """The documents worth skipping are the ones that could never be
    recorded: the biggest receipts time out on upload, so a record built only
    from successful posts would never contain them and they would be retried
    for ever. The mirror has held them since they were first published."""

    ITEM = [{"path": "/v1/daily/2026-08-23/receipt", "body": {"a": 1}}]

    def _run(self, tmp_path, monkeypatch, present, sent):
        monkeypatch.setattr(mirror_sync, "build_mirror_items",
                            lambda r, recent_days=None: list(self.ITEM))
        monkeypatch.setattr(mirror_sync, "_mirror_has",
                            lambda url, path, timeout=20, expected_sha=None: present)
        monkeypatch.setattr(mirror_sync, "_post",
                            lambda u, k, items, t: (
                                sent.append([i["path"] for i in items]),
                                {"stored": len(items), "rejected": []})[1])
        return mirror_sync.sync_mirror(str(tmp_path), "http://ops", "k")

    def test_a_document_already_there_is_not_uploaded_again(self, tmp_path, monkeypatch):
        sent = []
        out = self._run(tmp_path, monkeypatch, present=True, sent=sent)
        assert sent == []
        assert out["adopted_from_mirror"] == 1

    def test_a_document_the_mirror_lacks_is_still_sent(self, tmp_path, monkeypatch):
        """The failure to avoid: skipping something absent leaves a day
        nobody can verify, and says nothing at all."""
        sent = []
        out = self._run(tmp_path, monkeypatch, present=False, sent=sent)
        assert sent and "/v1/daily/2026-08-23/receipt" in sent[0]
        assert out["adopted_from_mirror"] == 0

    def test_an_unreachable_mirror_means_send(self, tmp_path, monkeypatch):
        """_mirror_has swallows every error and answers False, so a network
        problem during the probe can only cost an upload."""
        sent = []
        monkeypatch.setattr(mirror_sync, "build_mirror_items",
                            lambda r, recent_days=None: list(self.ITEM))
        monkeypatch.setattr(mirror_sync, "urllib", mirror_sync.urllib)
        monkeypatch.setattr(mirror_sync.urllib.request, "urlopen",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
        monkeypatch.setattr(mirror_sync, "_post",
                            lambda u, k, items, t: (
                                sent.append([i["path"] for i in items]),
                                {"stored": 1, "rejected": []})[1])
        mirror_sync.sync_mirror(str(tmp_path), "http://ops", "k")
        assert sent and "/v1/daily/2026-08-23/receipt" in sent[0]

    def test_adoption_is_remembered_so_it_is_asked_once(self, tmp_path, monkeypatch):
        calls = []
        monkeypatch.setattr(mirror_sync, "build_mirror_items",
                            lambda r, recent_days=None: list(self.ITEM))
        monkeypatch.setattr(mirror_sync, "_mirror_has",
                            lambda url, path, timeout=20, expected_sha=None: calls.append(path) or True)
        monkeypatch.setattr(mirror_sync, "_post",
                            lambda u, k, items, t: {"stored": 0, "rejected": []})
        mirror_sync.sync_mirror(str(tmp_path), "http://ops", "k")
        mirror_sync.sync_mirror(str(tmp_path), "http://ops", "k")
        assert len(calls) == 1, "the probe must not repeat once recorded"


class TestAdoptionComparesTheEnvelopeHash:
    """A path that exists on the mirror is adopted only when the mirror holds
    THIS document: a re-signed receipt at the same path must ship."""

    ITEM = [{"path": "/v1/daily/2026-08-24/receipt",
             "body": {"document": {"day": "2026-08-24"}, "sha256": "new-sha"}}]

    def _run(self, tmp_path, monkeypatch, held_sha, sent):
        monkeypatch.setattr(mirror_sync, "build_mirror_items",
                            lambda r, recent_days=None: list(self.ITEM))

        class Resp:
            status = 200
            headers = {mirror_sync.ENVELOPE_SHA_HEADER: held_sha} if held_sha else {}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(mirror_sync.urllib.request, "urlopen",
                            lambda req, timeout=20: Resp())
        monkeypatch.setattr(mirror_sync, "_post",
                            lambda u, k, items, t: (
                                sent.append([i["path"] for i in items]),
                                {"stored": len(items), "rejected": []})[1])
        return mirror_sync.sync_mirror(str(tmp_path), "http://ops", "k")

    def test_same_hash_is_adopted_not_sent(self, tmp_path, monkeypatch):
        sent = []
        self._run(tmp_path, monkeypatch, held_sha="new-sha", sent=sent)
        assert sent == []

    def test_different_hash_is_sent(self, tmp_path, monkeypatch):
        sent = []
        self._run(tmp_path, monkeypatch, held_sha="old-sha", sent=sent)
        assert sent == [["/v1/daily/2026-08-24/receipt"]]

    def test_mirror_without_hash_header_is_sent(self, tmp_path, monkeypatch):
        sent = []
        self._run(tmp_path, monkeypatch, held_sha=None, sent=sent)
        assert sent == [["/v1/daily/2026-08-24/receipt"]]
