"""Production entrypoint for the SN21 archive server.

Reads config from env:
  SN21_ARCHIVE_HOST              (default 0.0.0.0)
  SN21_ARCHIVE_PORT              (default 8080)
  SN21_ARCHIVE_DIR               (default /var/lib/sn21-archive)
  SN21_ARCHIVE_REQUIRE_SIGNED    (true | false; default true for Tier-2)
  SN21_ARCHIVE_MAX_BODY_BYTES    (default 1048576 = 1 MiB)

Tier-2 deployments (HOPE shadow) MUST keep require_signed=true so a holder
of one hotkey cannot fill another's slot. Tier-3 self-archives may flip it
to false if the operator is comfortable accepting unauth uploads (the chain
SHA-256 commit is still the integrity anchor).
"""

from __future__ import annotations

import logging
import os
import sys

from hope.archive_server.app import build_app
from hope.archive_server.store import FilesystemStore


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log = logging.getLogger("entrypoint")

    host = os.environ.get("SN21_ARCHIVE_HOST", "0.0.0.0")
    port = int(os.environ.get("SN21_ARCHIVE_PORT", "8080"))
    base_dir = os.environ.get("SN21_ARCHIVE_DIR", "/var/lib/sn21-archive")
    require_signed = _bool_env("SN21_ARCHIVE_REQUIRE_SIGNED", True)
    max_body = int(os.environ.get("SN21_ARCHIVE_MAX_BODY_BYTES", str(1 * 1024 * 1024)))

    log.info(
        "starting archive server host=%s port=%d dir=%s signed_uploads=%s max_body=%dB",
        host, port, base_dir, require_signed, max_body,
    )

    store = FilesystemStore(base_dir)
    app = build_app(
        store=store,
        max_body_bytes=max_body,
        require_signed_uploads=require_signed,
    )

    import uvicorn

    uvicorn.run(app, host=host, port=port, log_config=None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
