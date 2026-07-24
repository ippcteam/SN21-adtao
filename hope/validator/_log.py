"""Make a validator tool's logs visible despite bittensor's logging hijack.

`import bittensor` installs loguru AND calls `logging.disable(...)`, a GLOBAL
suppressor that overrides any per-handler level — so `logging.basicConfig(...)`
/ `setLevel(...)` alone do NOT restore output (the decision lines a validator
tool emits go silent in production cron/worker logs). The reliable fix, applied
AFTER bittensor is imported:

  1. clear the global disable (`logging.disable(logging.NOTSET)`), and
  2. attach a DEDICATED stdout handler to the tool's own logger with
     `propagate = False`, so it is independent of whatever bittensor does to
     the root logger.

This is what makes the consolidated daemon's subprocesses auditable.
"""
from __future__ import annotations

import logging
import sys


def configure_logging(logger: logging.Logger, level: str = "INFO") -> None:
    """Restore visible logging for `logger` after `import bittensor`."""
    lvl = getattr(logging, str(level).upper(), logging.INFO)
    logging.disable(logging.NOTSET)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    )
    logger.handlers[:] = [handler]
    logger.setLevel(lvl)
    logger.propagate = False
