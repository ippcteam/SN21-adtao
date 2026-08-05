"""Shared ``bittensor.Subtensor`` factory.

Every validator-side binary that needs a chain connection must construct
its ``Subtensor`` through ``make_subtensor`` rather than calling
``bt.Subtensor(network=...)`` directly. Two reasons:

  1. **Single env-var knob.** ``SN21_SUBTENSOR_URL``, if set, pins the
     connection to that wss:// URL regardless of the named-network
     argument. Operators who run a private/archive subtensor node can
     set the env var once on the container and every binary in the
     stack — scoring runner, HTTP daemon, heartbeat, reg-index scan,
     diag tools — picks up the override consistently. Without this
     helper, each call site decides for itself, and historically only
     the runner's RPC-rotation path honored the override (see
     ``runner.py:_make_fresh_subtensor`` for the original site).

  2. **Single point of evolution.** When the rotation/failover strategy
     changes (additional URLs, retry policy, health probing), every
     call site benefits automatically.

The named-network fallback continues to accept the strings the
Bittensor SDK already understands (``"finney"``, ``"test"``, ``"local"``)
plus arbitrary wss:// URLs — the SDK normalises both.
"""

from __future__ import annotations

import argparse
import os

SUBTENSOR_URL_OVERRIDE_ENV = "SN21_SUBTENSOR_URL"

_NAMED_NETWORKS = ("test", "finney", "local")


def network_arg(value: str) -> str:
    """``argparse`` ``type=`` validator for the ``--network`` flag.

    Accepts the three Bittensor named networks (``test``, ``finney``,
    ``local``) plus arbitrary ``wss://`` / ``ws://`` URLs. The Bittensor
    SDK's ``Subtensor(network=...)`` already accepts both shapes; this
    function just teaches ``argparse`` the same vocabulary so operators
    can pass ``--network wss://archive.example:443`` directly instead of
    going through ``SN21_SUBTENSOR_URL``.

    Raises:
        argparse.ArgumentTypeError: when ``value`` is neither a known
            named network nor a ``wss://``/``ws://`` URL.
    """
    if value in _NAMED_NETWORKS:
        return value
    if value.startswith(("wss://", "ws://")):
        return value
    raise argparse.ArgumentTypeError(
        f"invalid network: {value!r} "
        f"(expected one of {', '.join(_NAMED_NETWORKS)}, or a wss:// URL)"
    )


def make_subtensor(network: str):
    """Construct a ``bittensor.Subtensor`` honoring the env override.

    Args:
        network: Named Bittensor network (``"finney"``, ``"test"``,
            ``"local"``) or a wss:// URL. Used only when
            ``SN21_SUBTENSOR_URL`` is unset or empty.

    Returns:
        A connected ``bittensor.Subtensor`` instance.
    """
    import bittensor as bt

    override = os.environ.get(SUBTENSOR_URL_OVERRIDE_ENV, "").strip()
    if override:
        return bt.Subtensor(network=override)
    return bt.Subtensor(network=network)
