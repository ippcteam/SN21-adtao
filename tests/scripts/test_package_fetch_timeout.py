"""The basket package fetch waits long enough for the largest baskets (12 Nov 2026).

The backend builds the package on the request at about 0.2-0.35 s per episode. With
600 s the two largest baskets timed out on their first fetch (BD-2026-11-03, 2,915
payloads; BD-2026-11-07, 2,417, built in 680 s) and each run lost 15 minutes.
"""
import scripts.run_daily_pipeline as rdp

SLOW_S_PER_EPISODE = 0.35


def test_the_limit_covers_the_largest_basket_seen_at_the_slow_rate():
    assert rdp.PACKAGE_TIMEOUT_S >= 2915 * SLOW_S_PER_EPISODE * 1.5


def test_the_package_fetch_uses_that_limit(monkeypatch):
    seen = {}

    def fake_get(path, timeout_s=120):
        seen["path"], seen["timeout_s"] = path, timeout_s
        return {"episodes": [{"episode_id": 7, "payload": {"account_state": {}}}]}

    monkeypatch.setattr(rdp, "_api_get", fake_get)
    out = rdp.fetch_basket_payloads("BD-2026-11-11")
    assert seen == {"path": "releases/BD-2026-11-11/package", "timeout_s": rdp.PACKAGE_TIMEOUT_S}
    assert out == [{"account_state": {}, "episode_id": "7"}]
