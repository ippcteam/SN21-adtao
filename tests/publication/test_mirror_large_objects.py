"""Large mirror documents travel through object storage, never in a batch.

A receipt grows with every horizon that matures. Pushed through the operator
API in a batch, a large one exhausted the receiving process, and the index,
root and allocation audit that followed it never arrived — so the public
record read as the previous day while the new weights were live.

These tests pin the replacement: the exact signed bytes go to object storage,
small documents ship first, every miner's slice ships with its receipt, and a
receipt is recorded as shipped only when all of it arrived.
"""

import hashlib
import inspect
import urllib.error

import pytest

from hope.publication import mirror_sync
from hope.publication.miner_day import miner_day_document, miner_day_documents
from hope.publication.rail import canonical_bytes

DAY = "2026-09-10"
RECEIPT = f"/v1/daily/{DAY}/receipt"
HOTKEYS = ["5" + c * 47 for c in "ABC"]
SLICES = {f"/v1/daily/{DAY}/miner/{hk}" for hk in HOTKEYS}


def _receipt():
    entries = [{"miner": hk, "score": round(0.1 * (i + 1) + 0.01 * j, 6),
                "episode": f"ep{j}", "horizon_days": 7}
               for i, hk in enumerate(HOTKEYS) for j in range(3)]
    entries.append({"miner": "not-a-hotkey", "score": 0.5, "episode": "x",
                    "horizon_days": 7})
    document = {"feed": "daily_receipt", "as_of": DAY,
                "metrics": {"entries": entries, "miners": 4, "formula": "f"}}
    return {"document": document,
            "sha256": hashlib.sha256(canonical_bytes(document)).hexdigest(),
            "signature_hex": "00", "public_key_hex": "11"}


def _items(receipt):
    return [{"path": RECEIPT, "body": receipt},
            {"path": f"/v1/daily/{DAY}/allocation-audit", "body": {"standings": {}}},
            {"path": "/v1/daily/index", "body": {"days": []}},
            {"path": "/v1/daily/root", "body": {"feed_root": "ab"}}]


class FakeMirror:
    def __init__(self):
        self.events = []
        self.posted = {}
        self.uploads = []
        self.puts = []
        self.fail_commit = False
        self.fail_slice_batches = False

    def post(self, api_url, api_key, items, timeout):
        paths = [it["path"] for it in items]
        self.events.append("post")
        if self.fail_slice_batches and any("/miner/" in p for p in paths):
            raise RuntimeError("gateway")
        for it in items:
            self.posted[it["path"]] = it["body"]
        return {"stored": len(items), "rejected": []}

    def api_json(self, api_url, api_key, route, payload, timeout,
                 retries=3, backoff=2.0):
        step = route.rsplit("/", 1)[-1]
        self.events.append(step)
        if step == "object-upload":
            self.uploads.append(payload)
            return {"upload_url": "https://storage.test/put?sig", "key": "k"}
        if self.fail_commit:
            raise RuntimeError("commit refused")
        return {"stored": 1}

    def put(self, url, data, timeout):
        self.events.append("put")
        self.puts.append((url, data))


@pytest.fixture
def mirror(monkeypatch, tmp_path):
    fake = FakeMirror()
    fake.receipt = _receipt()
    monkeypatch.setattr(mirror_sync, "LARGE_OBJECT_BYTES", 300)
    monkeypatch.setattr(mirror_sync, "_post", fake.post)
    monkeypatch.setattr(mirror_sync, "_api_json", fake.api_json)
    monkeypatch.setattr(mirror_sync, "_put_object", fake.put)
    monkeypatch.setattr(mirror_sync, "_mirror_has", lambda *a, **k: False)
    monkeypatch.setattr(mirror_sync, "build_mirror_items",
                        lambda root, recent_days=None, **_k: _items(fake.receipt))
    fake.run = lambda: mirror_sync.sync_mirror(str(tmp_path), "http://ops", "k")
    return fake


class TestTheReceiptGoesToObjectStorage:
    def test_it_never_travels_in_a_batch(self, mirror):
        mirror.run()
        assert RECEIPT not in mirror.posted
        assert [u["path"] for u in mirror.uploads] == [RECEIPT]

    def test_the_uploaded_bytes_are_the_signed_canonical_bytes(self, mirror):
        mirror.run()
        (url, data), = mirror.puts
        assert url == "https://storage.test/put?sig"
        assert data == canonical_bytes(mirror.receipt)
        spec = mirror.uploads[0]
        assert spec["size_bytes"] == len(data)
        assert spec["content_sha256"] == hashlib.sha256(data).hexdigest()
        assert spec["envelope_sha256"] == mirror.receipt["sha256"]

    def test_upload_then_put_then_commit(self, mirror):
        mirror.run()
        assert [e for e in mirror.events if e != "post"] == \
            ["object-upload", "put", "object-commit"]

    def test_the_summary_lists_the_hotkeys_with_entries(self, mirror):
        mirror.run()
        assert mirror.uploads[0]["summary"] == {"miners": 4, "hotkeys": sorted(HOTKEYS)}

    def test_the_run_reports_it(self, mirror):
        out = mirror.run()
        assert out["success"] and out["objects"] == 1


class TestSmallDocumentsNeverWaitOnAReceipt:
    def test_every_small_document_is_posted_before_the_upload(self, mirror):
        mirror.run()
        last_post = max(i for i, e in enumerate(mirror.events) if e == "post")
        assert last_post < mirror.events.index("object-upload")
        for path in (f"/v1/daily/{DAY}/allocation-audit", "/v1/daily/index", "/v1/daily/root"):
            assert path in mirror.posted

    def test_a_failed_receipt_still_ships_them_and_is_tried_again(self, mirror):
        mirror.fail_commit = True
        with pytest.raises(mirror_sync.MirrorSyncError):
            mirror.run()
        assert "/v1/daily/index" in mirror.posted

        mirror.fail_commit = False
        mirror.uploads.clear()
        mirror.run()
        assert [u["path"] for u in mirror.uploads] == [RECEIPT]


class TestEveryMinerGetsItsSlice:
    def test_one_slice_per_hotkey_with_entries(self, mirror):
        mirror.run()
        slices = {p: b for p, b in mirror.posted.items() if "/miner/" in p}
        assert set(slices) == SLICES
        for hk in HOTKEYS:
            assert slices[f"/v1/daily/{DAY}/miner/{hk}"] == \
                miner_day_document(mirror.receipt, DAY, hk)

    def test_a_second_run_sends_neither_receipt_nor_slices(self, mirror):
        mirror.run()
        mirror.posted.clear()
        mirror.uploads.clear()
        mirror.run()
        assert mirror.uploads == []
        assert not [p for p in mirror.posted if "/miner/" in p]

    def test_a_failed_slice_keeps_the_receipt_unrecorded(self, mirror):
        mirror.fail_slice_batches = True
        with pytest.raises(mirror_sync.MirrorSyncError):
            mirror.run()
        # the receipt itself was still published
        assert [u["path"] for u in mirror.uploads] == [RECEIPT]

        mirror.fail_slice_batches = False
        mirror.uploads.clear()
        mirror.run()
        assert [u["path"] for u in mirror.uploads] == [RECEIPT]
        assert {p for p in mirror.posted if "/miner/" in p} == SLICES


class TestSmallReceiptsAreUnchanged:
    def test_under_the_threshold_it_is_posted_inline_without_slices(self, mirror, monkeypatch):
        monkeypatch.setattr(mirror_sync, "LARGE_OBJECT_BYTES", 10 ** 9)
        mirror.run()
        assert RECEIPT in mirror.posted and mirror.uploads == []
        assert not [p for p in mirror.posted if "/miner/" in p]


class TestTheSliceIsTheValidatorRouteBody:
    def test_the_route_uses_the_shared_function(self):
        from hope.validator.api import daily

        assert "miner_day_document(" in inspect.getsource(daily.get_miner_day)

    def test_one_pass_equals_one_miner_at_a_time(self):
        receipt = _receipt()
        docs = miner_day_documents(receipt, DAY)
        assert set(docs) >= set(HOTKEYS)
        for hk, doc in docs.items():
            assert doc == miner_day_document(receipt, DAY, hk)

    def test_a_hotkey_without_entries_gets_the_no_entries_answer(self):
        doc = miner_day_document(_receipt(), DAY, "5" + "Z" * 47)
        assert doc["entries"] == [] and doc["miners_scored_that_day"] == 4


class _Resp:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self):
        return self.body


class TestTransport:
    def test_the_put_sends_the_bytes_with_the_signed_content_type(self, monkeypatch):
        seen = []
        monkeypatch.setattr(mirror_sync.urllib.request, "urlopen",
                            lambda req, timeout=None: (seen.append(req), _Resp(b""))[1])
        mirror_sync._put_object("https://storage.test/put?sig", b"{}", 30)
        (req,) = seen
        assert req.get_method() == "PUT" and req.data == b"{}"
        assert req.get_header("Content-type") == "application/json"

    def test_the_commit_retries_a_gateway_error(self, monkeypatch):
        monkeypatch.setattr(mirror_sync.time, "sleep", lambda s: None)
        timeouts = []

        def fake(req, timeout=None):
            timeouts.append(timeout)
            if len(timeouts) == 1:
                raise urllib.error.HTTPError(req.full_url, 502, "bad gateway", {}, None)
            return _Resp(b'{"stored": 1}')

        monkeypatch.setattr(mirror_sync.urllib.request, "urlopen", fake)
        out = mirror_sync._api_json("http://ops", "k", mirror_sync.MIRROR_OBJECT_COMMIT_PATH,
                                    {"path": RECEIPT}, 900)
        assert out == {"stored": 1} and timeouts == [900, 900]
