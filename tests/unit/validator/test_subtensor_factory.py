"""Unit tests for hope.validator._subtensor.make_subtensor."""

from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture
def captured_network(monkeypatch):
    """Patch bittensor.Subtensor so make_subtensor can be tested offline.

    Returns a dict whose ``["network"]`` key holds the value passed to the
    fake Subtensor constructor on the latest call.
    """
    captured: dict[str, str] = {}

    class FakeSubtensor:
        def __init__(self, network):
            captured["network"] = network

    fake_bt = types.ModuleType("bittensor")
    fake_bt.Subtensor = FakeSubtensor  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "bittensor", fake_bt)
    return captured


def test_env_override_takes_precedence(monkeypatch, captured_network):
    monkeypatch.setenv("SN21_SUBTENSOR_URL", "wss://archive.example:443")
    from hope.validator._subtensor import make_subtensor

    make_subtensor("finney")
    assert captured_network["network"] == "wss://archive.example:443"


def test_named_network_used_when_env_unset(monkeypatch, captured_network):
    monkeypatch.delenv("SN21_SUBTENSOR_URL", raising=False)
    from hope.validator._subtensor import make_subtensor

    make_subtensor("finney")
    assert captured_network["network"] == "finney"


def test_env_whitespace_treated_as_unset(monkeypatch, captured_network):
    monkeypatch.setenv("SN21_SUBTENSOR_URL", "   ")
    from hope.validator._subtensor import make_subtensor

    make_subtensor("test")
    assert captured_network["network"] == "test"
