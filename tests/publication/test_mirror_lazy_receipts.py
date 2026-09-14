"""Receipts are read only when they are about to be sent.

A receipt is tens of megabytes of JSON and several times that as objects.
The sync used to load every receipt in its window before deciding that
nearly all of them had already shipped — ten at once was the executor's
memory peak on a run that then skipped nine. Now a file-backed document
whose size and mtime match the record, and whose digest the mirror has
confirmed, is skipped without being opened; a new or rewritten file is read,
digested and sent as before.
"""

import json
import os
import time

import pytest

from hope.publication import mirror_sync

DAY = "2026-09-01"


def _write_receipt(root, day, payload):
    d = os.path.join(root, "receipts")
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, f"{day}.json")
    with open(p, "w") as fh:
        json.dump({"sha256": "a" * 64, "document": {"day": day, "metrics": payload}}, fh)
    return p


@pytest.fixture
def mirror(tmp_path, monkeypatch):
    sent: list[list[str]] = []
    reads: list[str] = []
    real_read = mirror_sync._read_envelope

    def fake_post(api_url, api_key, items, timeout):
        sent.append([it["path"] for it in items])
        return {"stored": len(items), "rejected": []}

    def counting_read(path):
        reads.append(path)
        return real_read(path)

    monkeypatch.setattr(mirror_sync, "_post", fake_post)
    monkeypatch.setattr(mirror_sync, "_read_envelope", counting_read)
    monkeypatch.setattr(mirror_sync, "_mirror_has", lambda *a, **k: False)
    # the registration-status document syncs the metagraph over the network
    # (20 s); this test is about files on disk
    from hope.publication import registration_status_feed
    monkeypatch.setattr(registration_status_feed, "build_registration_status_document",
                        lambda root: {"stub": True})

    def run():
        sent.clear(); reads.clear()
        out = mirror_sync.sync_mirror(str(tmp_path), "http://ops", "k")
        return [p for b in sent for p in b], list(reads), out

    return run


def test_lazy_items_carry_the_file_not_the_body(tmp_path):
    p = _write_receipt(str(tmp_path), DAY, {"entries": []})
    items = mirror_sync.build_mirror_items(str(tmp_path), lazy=True)
    rec = next(i for i in items if i["path"].endswith("/receipt"))
    assert rec == {"path": f"/v1/daily/{DAY}/receipt", "file": p}
    eager = next(i for i in mirror_sync.build_mirror_items(str(tmp_path))
                 if i["path"].endswith("/receipt"))
    assert "body" in eager and "file" not in eager


def test_a_new_receipt_is_read_once_and_sent(tmp_path, mirror):
    p = _write_receipt(str(tmp_path), DAY, {"entries": []})
    sent, reads, out = mirror()
    assert f"/v1/daily/{DAY}/receipt" in sent
    assert reads.count(p) == 1


def test_an_unchanged_shipped_receipt_is_skipped_without_being_opened(tmp_path, mirror):
    p = _write_receipt(str(tmp_path), DAY, {"entries": []})
    mirror()
    sent, reads, out = mirror()
    assert f"/v1/daily/{DAY}/receipt" not in sent
    assert p not in reads, "the skip must not cost a parse of the receipt"
    assert f"/v1/daily/{DAY}/receipt" not in sent and out["skipped_unchanged"] >= 1


def test_a_rewritten_receipt_is_read_and_sent_again(tmp_path, mirror):
    p = _write_receipt(str(tmp_path), DAY, {"entries": []})
    mirror()
    time.sleep(1.1)                       # a new mtime second
    _write_receipt(str(tmp_path), DAY, {"entries": [{"miner": "x"}]})
    sent, reads, out = mirror()
    assert f"/v1/daily/{DAY}/receipt" in sent
    assert p in reads


def test_the_file_record_never_travels_as_a_mirrored_path(tmp_path, mirror):
    _write_receipt(str(tmp_path), DAY, {"entries": []})
    sent, _, _ = mirror()
    assert not any(p == mirror_sync._FILES_KEY or p.endswith("_files") for p in sent)
    with open(os.path.join(str(tmp_path), "_mirror_shipped.json")) as fh:
        rec = json.load(fh)
    assert mirror_sync._FILES_KEY in rec
    assert f"/v1/daily/{DAY}/receipt" in rec[mirror_sync._FILES_KEY]


def test_the_standing_preview_is_mirrored_only_when_the_operator_says_so(tmp_path, monkeypatch):
    import json as _json
    import os as _os
    from hope.publication.mirror_sync import build_mirror_items
    root = str(tmp_path)
    d = _os.path.join(root, "standing_preview"); _os.makedirs(d)
    with open(_os.path.join(d, "2026-09-15.json"), "w") as f:
        _json.dump({"as_of": "2026-09-15"}, f)
    from hope.publication import registration_status_feed
    monkeypatch.setattr(registration_status_feed, "build_registration_status_document",
                        lambda *_a, **_k: {"feed": "stub"})
    monkeypatch.delenv("SN21_STANDING_PREVIEW_PUBLISH", raising=False)
    paths = {it["path"] for it in build_mirror_items(root, lazy=True)}
    assert "/v1/daily/2026-09-15/standing-preview" not in paths
    monkeypatch.setenv("SN21_STANDING_PREVIEW_PUBLISH", "1")
    items = {it["path"]: it for it in build_mirror_items(root, lazy=True)}
    assert items["/v1/daily/2026-09-15/standing-preview"]["file"].endswith("standing_preview/2026-09-15.json")
