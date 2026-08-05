"""The entrypoint's anchor wiring.

This exists because of a real gap: `daily_loop` had every anchor guard in
place and `run_daily_loop.py` never constructed a committer, so turning
SN21_ANCHOR_COMMITS on would have changed one log line from "off" to
"skipped_no_committer" and anchored nothing. The loop's own tests could not
catch it — they inject a committer. These pin the entrypoint.
"""

import importlib.util
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _entrypoint():
    """Load the script by path — it is not an importable module."""
    path = os.path.join(REPO, "scripts", "run_daily_loop.py")
    spec = importlib.util.spec_from_file_location("_rdl_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["_rdl_under_test"] = module
    spec.loader.exec_module(module)
    return module


FULL_ENV = {
    "SN21_ANCHOR_COMMITS": "true",
    "SN21_WALLET_NAME": "validator",
    "SN21_WALLET_HOTKEY": "default",
    "SN21_BT_NETWORK": "test",
    "SN21_NETUID": "466",
}


def test_flag_off_builds_no_committer_and_touches_no_wallet(tmp_path):
    assert _entrypoint()._anchor_committer(str(tmp_path), {}) is None


@pytest.mark.parametrize("drop", ["SN21_WALLET_NAME", "SN21_BT_NETWORK", "SN21_NETUID"])
def test_a_half_configured_anchor_refuses_rather_than_guessing(tmp_path, drop, capsys):
    """Committing the right root from the wrong identity looks anchored and
    verifies against nothing, so a missing setting must stop the anchor."""
    env = {k: v for k, v in FULL_ENV.items() if k != drop}
    assert _entrypoint()._anchor_committer(str(tmp_path), env) is None
    assert drop in capsys.readouterr().out          # says which one, by name


def test_a_non_integer_netuid_refuses(tmp_path, capsys):
    env = dict(FULL_ENV, SN21_NETUID="four-six-six")
    assert _entrypoint()._anchor_committer(str(tmp_path), env) is None
    assert "not an integer" in capsys.readouterr().out


def test_fully_configured_builds_a_working_committer(tmp_path, monkeypatch):
    """The gap this file exists for: with the flag on and the wallet settings
    present, the entrypoint must hand the loop something that actually
    commits."""
    import types

    calls = {}

    class _Res:
        success, block_number, message = True, 7_123_456, "ok"

    fake_bt = types.ModuleType("bittensor")
    fake_bt.subtensor = lambda network: calls.setdefault("network", network) or "SUB"
    fake_bt.wallet = lambda name, hotkey: calls.setdefault(
        "wallet", (name, hotkey)) or "WALLET"
    monkeypatch.setitem(sys.modules, "bittensor", fake_bt)

    def fake_submit(subtensor, wallet, netuid, root_bytes, **kw):
        calls["submitted"] = (netuid, root_bytes)
        return _Res()

    monkeypatch.setattr("hope.commitment.on_chain.submit_sha256_commit", fake_submit)

    committer = _entrypoint()._anchor_committer(str(tmp_path), FULL_ENV)
    assert committer is not None

    out = committer(bytes(range(32)))

    assert out["ok"] is True and out["block"] == 7_123_456
    assert calls["network"] == "test"
    assert calls["wallet"] == ("validator", "default")
    assert calls["submitted"] == (466, bytes(range(32)))
