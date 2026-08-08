"""Settled outcomes over HTTP — so the loop's host needs no database login.

The daily loop must know what actually happened to each advertising account
before it can score anything, and that lives in the operator's database. The
natural host for the loop is the public-facing validator, and putting database
credentials there to read a few columns is a poor trade.

The loop already takes its outcomes provider as an argument, so this is a
different provider rather than a different architecture. The validator holds an
API key to one read-only endpoint instead of a login to a database.
"""

import json
from datetime import date
from unittest.mock import patch

import pytest

from hope.scoring.settle_day_flow import SettledHorizon, http_outcomes_provider

PAYLOAD = {"outcomes": [
    {"episode_id": "28935", "horizon_days": 7,
     "cost_delta_pct": -0.0396, "conversions_delta_pct": 0.38,
     "efficiency_delta_pct": -0.304, "finalized_on": "2026-08-18",
     "goal_basis": "cpa"},
    {"episode_id": "28938", "horizon_days": 28,
     "cost_delta_pct": 0.12, "conversions_delta_pct": -0.05,
     "efficiency_delta_pct": 0.18, "finalized_on": "2026-09-08",
     "goal_basis": "conversion_value"},
]}


class _Resp:
    def __init__(self, body): self._b = json.dumps(body).encode()
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_it_returns_the_same_shape_as_the_database_provider():
    """The two must be interchangeable — the scoring path cannot be allowed to
    behave differently depending on where the truth came from."""
    with patch("urllib.request.urlopen", return_value=_Resp(PAYLOAD)):
        rows = http_outcomes_provider("https://api.example", "k")(date(2026, 8, 18))

    assert all(isinstance(r, SettledHorizon) for r in rows)
    assert rows[0].episode_id == "28935"
    assert rows[0].horizon_days == 7
    assert rows[0].finalized_on == date(2026, 8, 18)
    assert rows[0].goal_basis == "cpa"
    assert rows[1].horizon_days == 28
    assert rows[1].goal_basis == "conversion_value"


def test_the_api_key_is_sent_and_the_day_is_the_filter():
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["key"] = req.get_header("X-api-key")
        return _Resp(PAYLOAD)

    with patch("urllib.request.urlopen", fake_urlopen):
        http_outcomes_provider("https://api.example/", "secret")(date(2026, 8, 18))

    assert captured["key"] == "secret"
    assert "settled_on_or_before=2026-08-18" in captured["url"]
    assert captured["url"].startswith("https://api.example/internal/")


def test_a_missing_goal_basis_defaults_to_cpa():
    """Preserves the pre-v2 behaviour, where efficiency was always CPA."""
    thin = {"outcomes": [dict(PAYLOAD["outcomes"][0])]}
    thin["outcomes"][0].pop("goal_basis")
    with patch("urllib.request.urlopen", return_value=_Resp(thin)):
        rows = http_outcomes_provider("https://api.example", "k")(date(2026, 8, 18))
    assert rows[0].goal_basis == "cpa"


def test_an_empty_day_is_not_an_error():
    with patch("urllib.request.urlopen", return_value=_Resp({"outcomes": []})):
        assert http_outcomes_provider("https://api.example", "k")(date(2026, 8, 18)) == []


def test_a_failing_endpoint_raises_rather_than_scoring_a_partial_day():
    """Silently scoring whatever arrived would write receipts that never settle
    correctly, and receipts are not rewritable."""
    import urllib.error

    err = urllib.error.HTTPError("u", 503, "down", None, None)
    with patch("urllib.request.urlopen", side_effect=err), pytest.raises(RuntimeError, match="cannot score"):
        http_outcomes_provider("https://api.example", "k")(date(2026, 8, 18))
