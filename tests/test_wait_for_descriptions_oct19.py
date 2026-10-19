"""The scored run waits for the basket's change descriptions before fetching it (19 Oct 2026)."""
from datetime import date

from scripts import run_daily_pipeline as P

ON = {P.DESC_WAIT_ENV: "2026-10-30", P.DESC_WAIT_MAX_ENV: "600"}
DAY = date(2026, 10, 30)


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _api(seq):
    it = iter(seq)

    def get(path):
        assert path == "releases/BD-2026-10-29/descriptor-coverage"
        v = next(it)
        if isinstance(v, Exception):
            raise v
        return v
    return get


def test_off_before_the_switch_date():
    assert P.wait_for_descriptions("BD-2026-10-29", date(2026, 10, 29), environ=ON,
                                   api_get=lambda p: 1 / 0) == {"waited": False}
    assert P.wait_for_descriptions("BD-2026-10-29", DAY, environ={}) == {"waited": False}


def test_waits_until_the_basket_is_described():
    c = Clock()
    cov = [{"publishing": True, "described_share": 0.4, "context_share": 0.1},
           {"publishing": True, "described_share": 0.99, "context_share": 0.9},
           {"publishing": True, "described_share": 0.99, "context_share": 0.985}]
    out = P.wait_for_descriptions("BD-2026-10-29", DAY, api_get=_api(cov), sleep=c.sleep, clock=c, environ=ON)
    assert out["ready"] is True and out["waited_s"] == 120


def test_runs_past_the_limit_with_what_is_there():
    c = Clock()
    cov = [RuntimeError("502")] + [{"publishing": True, "described_share": 0.5, "context_share": 0.5}] * 20
    out = P.wait_for_descriptions("BD-2026-10-29", DAY, api_get=_api(cov), sleep=c.sleep, clock=c, environ=ON)
    assert out["ready"] is False and out["timed_out"] and out["waited_s"] == 600


def test_no_wait_when_descriptions_are_not_published():
    out = P.wait_for_descriptions("BD-2026-10-29", DAY, environ=ON,
                                  api_get=_api([{"publishing": False, "described_share": 0}]))
    assert out["reason"] == "not publishing" and out["waited_s"] == 0
