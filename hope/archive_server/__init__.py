"""Archive HTTP server for SN21 — Tier-2 (operator shadow) and Tier-3 (miner self).

- store: pluggable storage backends (InMemory, Filesystem).
- app: FastAPI app factory.
"""

from hope.archive_server.app import (
    ARCHIVE_AUTH_DOMAIN,
    DEFAULT_MAX_BODY_BYTES,
    build_app,
)
from hope.archive_server.store import (
    ArchiveStore,
    FilesystemStore,
    InMemoryStore,
)

__all__ = [
    "ARCHIVE_AUTH_DOMAIN",
    "DEFAULT_MAX_BODY_BYTES",
    "ArchiveStore",
    "FilesystemStore",
    "InMemoryStore",
    "build_app",
]
