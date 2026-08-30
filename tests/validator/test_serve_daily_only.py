"""The API must serve the daily feeds without a weekly release.

WHY THIS EXISTS
    A release is a weekly-era concept and the weekly stream has wound down.
    `hope-validator-api` nevertheless treated an OMITTED --release exactly
    like `--release auto`: it called the operator data backend for a weekly
    release listing and, when that failed, exited via parser.error.

    So a node that only wanted to serve the public /v1/daily feeds — which
    read the ledger on disk and need no credentials at all — could not start
    without weekly API access it has no use for. An external validator hit
    exactly this and asked whether the flag was leftover or whether their key
    needed widening; the honest answer was neither, the code was wrong.

    docs/validator_setup.md already described the intended behaviour
    ("Optional for daily receipt serving"). These tests make the code match
    the documentation rather than the other way round.
"""

import sys
import types

import pytest

import hope.validator.serve as serve


@pytest.fixture
def served(monkeypatch):
    """Captures create_app/uvicorn instead of binding a port."""
    seen = {}

    def fake_create_app(state=None):
        seen["state"] = state
        return "APP"

    def fake_run(app, **kw):
        seen["ran"] = app
        seen["kw"] = kw

    monkeypatch.setattr(serve, "create_app", fake_create_app)
    monkeypatch.setattr(serve.uvicorn, "run", fake_run)
    monkeypatch.setenv("SN21_LEDGER_ROOT", "/var/data/sn21/ledger")
    return seen


def _argv(*extra):
    return ["hope-validator-api", *extra]


class TestNoReleaseServesDaily:
    def test_it_starts_with_no_release_and_no_data_api(self, served, monkeypatch):
        monkeypatch.setattr(sys, "argv", _argv())
        # Any attempt to reach the operator backend is a failure of the test's
        # premise, not a network error to be tolerated.
        monkeypatch.setattr(
            serve, "_build_state",
            lambda **kw: pytest.fail("daily-only serving must not fetch episodes"))
        serve.main()
        assert served["ran"] == "APP"
        assert served["state"] is None, (
            "daily routes read the ledger; they need no validator state")

    def test_it_does_not_read_the_chain(self, served, monkeypatch):
        """No metagraph, no wallet — the daily feeds are public documents."""
        monkeypatch.setattr(sys, "argv", _argv())
        monkeypatch.setattr(
            serve, "_build_state",
            lambda **kw: pytest.fail("daily-only serving must not read chain"))
        serve.main()
        assert "ran" in served

    def test_the_port_is_still_honoured(self, served, monkeypatch):
        monkeypatch.setattr(sys, "argv", _argv("--port", "9111"))
        monkeypatch.setattr(serve, "_build_state",
                            lambda **kw: pytest.fail("should not be called"))
        serve.main()
        assert served["kw"]["port"] == 9111


class TestAutoIsNotFatal:
    """`auto` cannot succeed once the weekly listing is spent. Refusing to
    boot over it takes the daily feeds down for an unrelated reason."""

    def test_a_failing_auto_falls_back_to_daily(self, served, monkeypatch):
        monkeypatch.setattr(sys, "argv", _argv("--release", "auto"))

        class _Client:
            def __init__(self):
                raise RuntimeError("401 Unauthorized")

        monkeypatch.setattr("hope.validator.data_client.HopeDataClient", _Client)
        monkeypatch.setattr(serve, "_build_state",
                            lambda **kw: pytest.fail("should not be called"))
        serve.main()
        assert served["state"] is None

    def test_a_successful_auto_still_serves_that_release(self, served, monkeypatch):
        monkeypatch.setattr(sys, "argv", _argv("--release", "auto"))

        class _Client:
            base_url = "http://ops"

            async def discover_latest_release(self):
                return "WR-2026-W19-PUB-E1"

        monkeypatch.setattr("hope.validator.data_client.HopeDataClient", _Client)
        captured = {}

        def fake_build_state(**kw):
            captured.update(kw)
            return {}

        monkeypatch.setattr(serve, "_build_state", fake_build_state)
        serve.main()
        assert captured["release_key"] == "WR-2026-W19-PUB-E1"


class TestAnExplicitReleaseIsUnchanged:
    def test_weekly_serving_still_builds_full_state(self, served, monkeypatch):
        """Historical verification must keep working exactly as before."""
        monkeypatch.setattr(sys, "argv",
                            _argv("--release", "WR-2026-W19-PUB-E1"))
        captured = {}

        def fake_build_state(**kw):
            captured.update(kw)
            return {}

        monkeypatch.setattr(serve, "_build_state", fake_build_state)
        serve.main()
        assert captured["release_key"] == "WR-2026-W19-PUB-E1"
        assert served["state"] is not None
        assert "_metagraph_refresh_coro" in served["state"]
