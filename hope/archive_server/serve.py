"""SN21 archive server CLI — Tier-2 (operator) or Tier-3 (miner self) host.

Wraps `hope.archive_server.app.build_app` with a thin uvicorn launcher that
reads config from env vars + flags. Use as either:

  - **Tier-2** (operator): durable redundancy archive that miners and other
    validators can fall back to. Run with `--require-signed-uploads` so every
    POST carries a valid miner inner_sig.
  - **Tier-3** (miner self): the per-miner self-archive whose URL is
    announced on chain via the Layer 9.B Raw(URL) commit. Typically run
    with signed uploads off (`--require-signed-uploads false`) since the
    miner is the only writer.
"""

from __future__ import annotations

import logging
import os

import uvicorn

from hope.archive_server.app import build_app
from hope.archive_server.store import FilesystemStore, InMemoryStore

logger = logging.getLogger(__name__)


def main():
    """CLI entry point: `hope-archive-server`."""
    import argparse

    parser = argparse.ArgumentParser(description="SN21 archive server (Tier-2 / Tier-3)")
    parser.add_argument("--port", type=int,
                        default=int(os.environ.get("PORT", "8081")),
                        help="Port to bind (default 8081, or $PORT)")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind host (default 0.0.0.0)")
    parser.add_argument("--store", choices=["filesystem", "memory"], default="filesystem",
                        help="Storage backend (default filesystem; memory for tests)")
    parser.add_argument("--base-dir", default=os.environ.get("ARCHIVE_BASE_DIR", "/tmp/sn21-archive"),
                        help="Base directory for filesystem store")
    parser.add_argument("--require-signed-uploads", action="store_true",
                        help="Require X-Miner-Hotkey + X-Miner-Signature on POSTs (Tier-2 mode)")
    parser.add_argument("--max-body-mb", type=int, default=4,
                        help="Per-request body cap in MB (default 4)")

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.store == "filesystem":
        os.makedirs(args.base_dir, exist_ok=True)
        store = FilesystemStore(base_dir=args.base_dir)
        logger.info("FilesystemStore at %s", args.base_dir)
    else:
        store = InMemoryStore()
        logger.info("InMemoryStore (volatile)")

    app = build_app(
        store=store,
        max_body_bytes=args.max_body_mb * 1024 * 1024,
        require_signed_uploads=args.require_signed_uploads,
    )

    logger.info("Archive server starting on %s:%d (signed_uploads=%s)",
                args.host, args.port, args.require_signed_uploads)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
