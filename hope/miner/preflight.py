"""Pre-flight checks the miner runs before doing any work.

Two checks are exposed:

1. ``check_registration(validator_url, hotkey, sign_headers)``
   Asks the validator's ``/v1/registration-status`` whether this hotkey
   has a verified sn21-reg-v1 binding. When the validator's gate is
   enabled and the hotkey is missing, raises ``PreflightError`` with an
   actionable message pointing at ``scripts/sn21_keys.py register``.
   When the gate is disabled (or the validator is unreachable / pre-
   v1.x), returns silently — the miner proceeds at its own risk.

2. ``check_submission_window(validator_url)``
   Reads ``/health`` and refuses to start when ``submission_open=false``
   or when the wall-clock deadline has passed. The error tells the
   miner the exact deadline timestamp and how many seconds late they
   are, so they immediately know to wait for the next weekly release
   rather than re-running their model code.

Both checks have a tight HTTP timeout (5s) and fall through to a
warning log on network errors — failing the miner because the validator
is briefly unreachable would be worse than letting the chain submission
proceed (and surface the same error post-hoc).
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

logger = logging.getLogger(__name__)

# Tight timeout — the pre-flight checks must not block the miner for
# long when the validator HTTP API is slow. 5s is generous for a single
# GET /health or /v1/registration-status; longer and we'd silently
# degrade the miner's start-up UX.
_PREFLIGHT_TIMEOUT_S = 5.0


class PreflightError(RuntimeError):
    """Raised when a pre-flight check determines the miner should not start.

    Carries the actionable message verbatim — the CLI prints
    ``str(err)`` directly to stderr so the miner reads exactly what
    the validator returned.
    """


def check_registration(
    validator_url: str,
    *,
    sign_headers: dict[str, str],
) -> None:
    """Verify the hotkey carried in sign_headers has a sn21-reg-v1 binding.

    Args:
        validator_url: base URL of the validator HTTP API.
        sign_headers: full set of auth headers (X-Miner-Hotkey, -Nonce,
            -Signature) bound to ``GET /v1/registration-status``. The
            caller builds these the same way it would for any other
            authenticated endpoint.

    Raises:
        PreflightError: when the validator confirms the hotkey is not
            in its loaded registration index.
    """
    url = f"{validator_url.rstrip('/')}/v1/registration-status"
    try:
        resp = httpx.get(url, headers=sign_headers, timeout=_PREFLIGHT_TIMEOUT_S)
    except httpx.HTTPError as exc:
        logger.warning(
            "preflight: registration check could not reach validator (%s); "
            "proceeding without verification", exc,
        )
        return
    if resp.status_code == 404:
        # Older validator-api without the registration-status endpoint.
        # Don't block the miner — this just means the gate hasn't been
        # rolled out on this validator yet.
        logger.warning(
            "preflight: validator has no /v1/registration-status endpoint; "
            "proceeding without verification",
        )
        return
    if resp.status_code >= 400:
        logger.warning(
            "preflight: registration check returned HTTP %d; proceeding "
            "without verification", resp.status_code,
        )
        return
    try:
        body = resp.json()
    except ValueError:
        logger.warning(
            "preflight: registration check returned unparseable body; "
            "proceeding without verification",
        )
        return

    gate_enabled = bool(body.get("gate_enabled"))
    registered = bool(body.get("registered"))
    hint = body.get("hint", "")

    if not gate_enabled:
        logger.info(
            "preflight: validator has no registration index loaded "
            "(submission gate inactive)",
        )
        return
    if registered:
        logger.info("preflight: hotkey is registered ✓")
        return

    raise PreflightError(
        f"Hotkey is NOT registered on chain for the miner role. "
        f"Submissions will be rejected. {hint}"
    )


def check_submission_window(validator_url: str) -> None:
    """Refuse to start when the current epoch's mining window is closed.

    Reads the unauthenticated ``/health`` endpoint, parses the deadline
    and ``submission_open`` flag. Raises ``PreflightError`` with a
    deadline-aware message when the window is closed. A network error
    or older validator without the new fields degrades to a warning —
    the chain-commit submission would surface the same problem.

    Raises:
        PreflightError: when the validator reports the window has
            already closed for this epoch.
    """
    url = f"{validator_url.rstrip('/')}/health"
    try:
        resp = httpx.get(url, timeout=_PREFLIGHT_TIMEOUT_S)
    except httpx.HTTPError as exc:
        logger.warning(
            "preflight: window check could not reach validator (%s); "
            "proceeding without verification", exc,
        )
        return
    if resp.status_code != 200:
        logger.warning(
            "preflight: window check returned HTTP %d; proceeding "
            "without verification", resp.status_code,
        )
        return
    try:
        body = resp.json()
    except ValueError:
        return

    deadline: Optional[str] = body.get("deadline_utc")
    seconds_until: Optional[int] = body.get("seconds_until_deadline")
    submission_open = body.get("submission_open")

    # Older validator without the new fields: nothing to check.
    if deadline is None and seconds_until is None and submission_open is None:
        return

    is_closed = (
        (submission_open is False)
        or (isinstance(seconds_until, int) and seconds_until <= 0)
    )
    if not is_closed:
        return

    epoch_id = body.get("current_epoch", "this epoch")
    seconds_late = -seconds_until if isinstance(seconds_until, int) else None
    raise PreflightError(
        f"Mining window for {epoch_id} has CLOSED. "
        f"Deadline (UTC): {deadline}. "
        + (f"Seconds late: {seconds_late}. " if seconds_late is not None else "")
        + "Submissions arriving after the deadline are not scored. "
        "Wait for the next weekly epoch to open; poll GET /health and watch "
        "for `current_epoch` to update."
    )
