"""validator.adtao.io sends daily requests to the public feed (12 Oct 2026).

It served /v1/daily/* from an empty local ledger and answered "nothing
published" while the public feed had 58 days. The two must never disagree."""
from fastapi.testclient import TestClient

from hope.validator.api.server import create_app

FEED = "https://hope-bittensor-api.onrender.com"


def test_with_a_feed_url_every_daily_route_redirects_to_the_same_path(monkeypatch):
    monkeypatch.setenv("SN21_DAILY_FEED_URL", FEED + "/")
    c = TestClient(create_app(), follow_redirects=False)
    for path in ("/v1/daily/index", "/v1/daily/2026-10-11/receipt",
                 "/v1/daily/2026-10-11/miner/5C812dXTkes8bZhiABC", "/v1/daily/2026-09-14/standing-preview"):
        r = c.get(path)
        assert r.status_code == 307, path
        assert r.headers["location"] == FEED + path


def test_the_query_string_is_kept(monkeypatch):
    monkeypatch.setenv("SN21_DAILY_FEED_URL", FEED)
    r = TestClient(create_app(), follow_redirects=False).get("/v1/daily/index?limit=5")
    assert r.headers["location"] == FEED + "/v1/daily/index?limit=5"


def test_without_a_feed_url_the_local_ledger_answers_as_before(monkeypatch, tmp_path):
    monkeypatch.delenv("SN21_DAILY_FEED_URL", raising=False)
    monkeypatch.setenv("SN21_LEDGER_ROOT", str(tmp_path))
    r = TestClient(create_app(), follow_redirects=False).get("/v1/daily/index")
    assert r.status_code == 200 and r.json()["count"] == 0


def test_other_routes_are_not_redirected(monkeypatch):
    monkeypatch.setenv("SN21_DAILY_FEED_URL", FEED)
    r = TestClient(create_app(), follow_redirects=False).get("/health")
    assert r.status_code == 200
