"""The performance page's funnel call gets enough time (14 Oct 2026)."""
import inspect

import scripts.run_daily_pipeline as P


def test_funnel_call_waits_longer_than_the_default():
    src = inspect.getsource(P.stage_publish_performance)
    assert "timeout_s=FUNNEL_TIMEOUT_S" in src
    assert P.FUNNEL_TIMEOUT_S >= 240
