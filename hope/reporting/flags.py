"""Feature-flag helpers for the leaderboard reporter.

The reporter is gated by `SN21_LEADERBOARD_REPORTER=1` so the validator
hot path is unaffected until an operator opts in. All reporting code
that touches `run_epoch_scoring` reads this flag first; if disabled,
the reporter is a no-op.

The flag accepts truthy values `1`, `true`, `yes`, `on` (case-insensitive).
Anything else (unset, empty, `0`, `false`, …) leaves the reporter dormant.
"""

from __future__ import annotations

import os

_REPORTER_ENV_VAR = "SN21_LEADERBOARD_REPORTER"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def reporter_enabled() -> bool:
    """Return True when the reporter is opted in via env var."""
    raw = os.environ.get(_REPORTER_ENV_VAR, "")
    return raw.strip().lower() in _TRUTHY
