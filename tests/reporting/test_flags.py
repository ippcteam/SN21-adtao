"""Tests for hope.reporting.flags.reporter_enabled."""

from __future__ import annotations

import pytest

from hope.reporting.flags import reporter_enabled


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "TRUE", "Yes", " 1 "])
def test_truthy_values_enable(monkeypatch, value):
    monkeypatch.setenv("SN21_LEADERBOARD_REPORTER", value)
    assert reporter_enabled() is True


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "FALSE", "anything-else"])
def test_falsy_values_disable(monkeypatch, value):
    monkeypatch.setenv("SN21_LEADERBOARD_REPORTER", value)
    assert reporter_enabled() is False


def test_unset_disables(monkeypatch):
    monkeypatch.delenv("SN21_LEADERBOARD_REPORTER", raising=False)
    assert reporter_enabled() is False
