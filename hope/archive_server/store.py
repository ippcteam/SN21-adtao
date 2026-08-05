"""Storage backends for archive servers (Tier-2 operator shadow, Tier-3 miner self).

Two implementations:

- InMemoryStore — keeps every uploaded blob in a dict. Use for tests and
  for short-lived Tier-3 self-archives that don't need durability.
- FilesystemStore — writes to disk under a base directory. Use for the operator
  Tier-2 deployments that need 90+ day retention.

Both stores key by the SHA-256 digest of the bytes; the URL path component
also embeds the digest, so a successful PUT means the path digest matched the
body digest. Stores never store something they can't reproduce that hash for.
"""

from __future__ import annotations

import hashlib
import threading
from abc import ABC, abstractmethod
from pathlib import Path


class ArchiveStore(ABC):
    """Abstract storage interface used by the FastAPI app."""

    @abstractmethod
    def put(self, *, epoch_id: str, miner_identity: str, sha256: bytes, data: bytes) -> None:
        ...

    @abstractmethod
    def get(self, *, epoch_id: str, miner_identity: str, sha256: bytes) -> bytes | None:
        ...

    @abstractmethod
    def exists(self, *, epoch_id: str, miner_identity: str, sha256: bytes) -> bool:
        ...


def _validate_components(epoch_id: str, miner_identity: str, sha256: bytes) -> None:
    if not (1 <= len(epoch_id) <= 80):
        raise ValueError(f"epoch_id length out of [1, 80]: {len(epoch_id)}")
    if "/" in epoch_id or ".." in epoch_id:
        raise ValueError(f"epoch_id contains invalid characters: {epoch_id!r}")
    if not (1 <= len(miner_identity) <= 80):
        raise ValueError(f"miner_identity length out of [1, 80]: {len(miner_identity)}")
    if "/" in miner_identity or ".." in miner_identity:
        raise ValueError(f"miner_identity contains invalid characters: {miner_identity!r}")
    if len(sha256) != 32:
        raise ValueError(f"sha256 must be 32 bytes, got {len(sha256)}")


class InMemoryStore(ArchiveStore):
    """Thread-safe dict-backed store. Used for tests and Tier-3 short retention."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[tuple[str, str, bytes], bytes] = {}

    def _key(self, epoch_id: str, miner_identity: str, sha256: bytes):
        _validate_components(epoch_id, miner_identity, sha256)
        return (epoch_id, miner_identity, sha256)

    def put(self, *, epoch_id, miner_identity, sha256, data):
        _validate_components(epoch_id, miner_identity, sha256)
        actual = hashlib.sha256(data).digest()
        if actual != sha256:
            raise ValueError("sha256 mismatch with body")
        with self._lock:
            self._data[(epoch_id, miner_identity, sha256)] = data

    def get(self, *, epoch_id, miner_identity, sha256):
        with self._lock:
            return self._data.get(self._key(epoch_id, miner_identity, sha256))

    def exists(self, *, epoch_id, miner_identity, sha256):
        with self._lock:
            return self._key(epoch_id, miner_identity, sha256) in self._data


class FilesystemStore(ArchiveStore):
    """Disk-backed store under `base_dir/{epoch_id}/{miner_identity}/{sha_hex}`.

    Atomic writes via tempfile-then-rename so concurrent puts don't see torn
    bytes. Reads are direct.
    """

    def __init__(self, base_dir: Path | str) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, epoch_id: str, miner_identity: str, sha256: bytes) -> Path:
        _validate_components(epoch_id, miner_identity, sha256)
        return self.base_dir / epoch_id / miner_identity / sha256.hex()

    def put(self, *, epoch_id, miner_identity, sha256, data):
        _validate_components(epoch_id, miner_identity, sha256)
        actual = hashlib.sha256(data).digest()
        if actual != sha256:
            raise ValueError("sha256 mismatch with body")
        target = self._path(epoch_id, miner_identity, sha256)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(target)

    def get(self, *, epoch_id, miner_identity, sha256):
        target = self._path(epoch_id, miner_identity, sha256)
        if not target.exists():
            return None
        return target.read_bytes()

    def exists(self, *, epoch_id, miner_identity, sha256):
        return self._path(epoch_id, miner_identity, sha256).exists()
