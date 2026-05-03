"""Unit tests for hope/archive_server/store.py."""

from __future__ import annotations

import hashlib

import pytest

from hope.archive_server.store import FilesystemStore, InMemoryStore


@pytest.fixture
def payload() -> bytes:
    return b"the quick brown fox jumps over the lazy dog"


@pytest.fixture
def digest(payload) -> bytes:
    return hashlib.sha256(payload).digest()


@pytest.mark.parametrize(
    "store_factory",
    [
        lambda tmp: InMemoryStore(),
        lambda tmp: FilesystemStore(tmp),
    ],
    ids=["mem", "fs"],
)
class TestStoreContract:
    def test_put_then_get(self, tmp_path, store_factory, payload, digest):
        store = store_factory(tmp_path)
        store.put(epoch_id="EP", miner_identity="M", sha256=digest, data=payload)
        assert store.get(epoch_id="EP", miner_identity="M", sha256=digest) == payload
        assert store.exists(epoch_id="EP", miner_identity="M", sha256=digest)

    def test_get_missing_returns_none(self, tmp_path, store_factory, digest):
        store = store_factory(tmp_path)
        assert store.get(epoch_id="EP", miner_identity="M", sha256=digest) is None

    def test_put_rejects_sha_mismatch(self, tmp_path, store_factory, payload):
        store = store_factory(tmp_path)
        wrong = b"\x00" * 32
        with pytest.raises(ValueError, match="sha256 mismatch"):
            store.put(epoch_id="EP", miner_identity="M", sha256=wrong, data=payload)

    def test_invalid_epoch_id_rejected(self, tmp_path, store_factory, payload, digest):
        store = store_factory(tmp_path)
        with pytest.raises(ValueError, match="invalid"):
            store.put(epoch_id="../escape", miner_identity="M", sha256=digest, data=payload)

    def test_invalid_miner_identity_rejected(self, tmp_path, store_factory, payload, digest):
        store = store_factory(tmp_path)
        with pytest.raises(ValueError, match="invalid"):
            store.put(epoch_id="EP", miner_identity="m/y", sha256=digest, data=payload)

    def test_overwrite_same_digest(self, tmp_path, store_factory, payload, digest):
        store = store_factory(tmp_path)
        store.put(epoch_id="EP", miner_identity="M", sha256=digest, data=payload)
        store.put(epoch_id="EP", miner_identity="M", sha256=digest, data=payload)
        assert store.get(epoch_id="EP", miner_identity="M", sha256=digest) == payload

    def test_invalid_sha_length(self, tmp_path, store_factory, payload):
        store = store_factory(tmp_path)
        with pytest.raises(ValueError, match="32 bytes"):
            store.put(
                epoch_id="EP", miner_identity="M",
                sha256=b"\x00" * 31, data=payload,
            )


class TestFilesystemSpecific:
    def test_atomic_write(self, tmp_path):
        store = FilesystemStore(tmp_path)
        data = b"hello"
        digest = hashlib.sha256(data).digest()
        store.put(epoch_id="EP", miner_identity="M", sha256=digest, data=data)
        target = tmp_path / "EP" / "M" / digest.hex()
        assert target.exists()
        assert target.read_bytes() == data
        # No leftover .tmp file
        assert not list(target.parent.glob("*.tmp"))

    def test_separate_keys_separate_paths(self, tmp_path):
        store = FilesystemStore(tmp_path)
        d1, d2 = b"a", b"b"
        s1 = hashlib.sha256(d1).digest()
        s2 = hashlib.sha256(d2).digest()
        store.put(epoch_id="EP", miner_identity="M1", sha256=s1, data=d1)
        store.put(epoch_id="EP", miner_identity="M2", sha256=s2, data=d2)
        assert store.get(epoch_id="EP", miner_identity="M1", sha256=s1) == d1
        assert store.get(epoch_id="EP", miner_identity="M2", sha256=s2) == d2
