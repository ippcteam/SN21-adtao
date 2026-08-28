"""A re-run day must reach the public leaderboard, not just the ledger.

WHY THIS EXISTS
    A published epoch is frozen (IA D-13) and the CMS answers 409. The
    successor flow — re-post as `{epoch}-COR-N` carrying a `supersedes`
    pointer, which the CMS then renders as canonical — existed only inside
    post_epoch_report's `main()`. The daily pipeline called `post_payload`
    directly, so it had no way to reach it.

    The consequence is worse than a missing feature. Re-running a day
    corrected the ledger and the on-chain vector, the run exited 0, and the
    public leaderboard kept serving the superseded figures — the one surface
    everybody actually looks at was the only one that did not get the
    correction, and the run reported success either way.

    So the flow now lives in one function that both callers use, and these
    tests pin that the PIPELINE reaches it.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts import post_epoch_report as por


class _Artifact:
    def __init__(self, epoch_id="BD-2026-08-28"):
        self.epoch_id = epoch_id


class _Payload:
    def __init__(self, epoch_id="BD-2026-08-28"):
        self.epoch_id = epoch_id
        self.miner_results = []


FROZEN = {"error": "Epoch BD-2026-08-28 is published and frozen (IA D-13). "
                   "Publish a correction as a new epoch entry."}


@pytest.fixture
def posts(monkeypatch):
    """Records every POST and replays scripted responses."""
    sent = []

    def fake_post(payload, *, endpoint, api_key, **kw):
        sent.append(getattr(payload, "epoch_id", None))
        return sent_responses.pop(0)

    sent_responses = []
    monkeypatch.setattr(por, "post_payload", fake_post)
    monkeypatch.setattr(por, "aggregate",
                        lambda artifact, **kw: _Payload(
                            kw.get("epoch_id_override") or artifact.epoch_id))

    class Handle:
        calls = sent

        @staticmethod
        def respond(*responses):
            sent_responses.extend(responses)

    return Handle


class TestCorrectionSuccession:
    def test_a_frozen_epoch_is_re_posted_as_the_first_correction(self, posts):
        posts.respond(httpx.Response(409, json=FROZEN),
                      httpx.Response(201, json={"id": "x"}))
        resp, posted_as = por.post_with_correction(
            _Artifact(), _Payload(), endpoint="http://cms", api_key="k")

        assert posted_as == "BD-2026-08-28-COR-1"
        assert resp.status_code == 201
        assert posts.calls == ["BD-2026-08-28", "BD-2026-08-28-COR-1"]

    def test_it_walks_to_the_next_number_when_a_correction_is_also_frozen(self, posts):
        """Re-running a day twice must not fight over one correction slot."""
        posts.respond(httpx.Response(409, json=FROZEN),
                      httpx.Response(409, json=FROZEN),
                      httpx.Response(201, json={"id": "x"}))
        _resp, posted_as = por.post_with_correction(
            _Artifact(), _Payload(), endpoint="http://cms", api_key="k")
        assert posted_as == "BD-2026-08-28-COR-2"

    def test_a_clean_post_is_left_alone(self):
        """No correction machinery on a normal day."""
        calls = []

        def fake_post(payload, **kw):
            calls.append(payload.epoch_id)
            return httpx.Response(201, json={"id": "x"})

        import scripts.post_epoch_report as m
        original = m.post_payload
        m.post_payload = fake_post
        try:
            resp, posted_as = m.post_with_correction(
                _Artifact(), _Payload(), endpoint="http://cms", api_key="k")
        finally:
            m.post_payload = original

        assert posted_as == "BD-2026-08-28"
        assert resp.status_code == 201
        assert calls == ["BD-2026-08-28"]

    def test_a_correction_is_never_corrected_recursively(self, posts):
        """Posting a COR that is itself frozen must stop, not spiral into
        COR-1-COR-1."""
        posts.respond(httpx.Response(409, json=FROZEN))
        _resp, posted_as = por.post_with_correction(
            _Artifact("BD-2026-08-28-COR-1"), _Payload("BD-2026-08-28-COR-1"),
            endpoint="http://cms", api_key="k")
        assert posted_as == "BD-2026-08-28-COR-1"
        assert posts.calls == ["BD-2026-08-28-COR-1"], (
            "a frozen correction must be posted once and left alone")

    def test_a_non_frozen_conflict_is_not_papered_over(self, posts):
        """Only IA D-13 freezing earns a successor. Any other 409 is a real
        failure and must surface as one."""
        posts.respond(httpx.Response(409, json={"error": "schema mismatch"}))
        resp, posted_as = por.post_with_correction(
            _Artifact(), _Payload(), endpoint="http://cms", api_key="k")
        assert posted_as == "BD-2026-08-28"
        assert resp.status_code == 409
        assert posts.calls == ["BD-2026-08-28"]

    def test_it_gives_up_rather_than_looping_forever(self, posts):
        posts.respond(*[httpx.Response(409, json=FROZEN)] * 4)
        _resp, posted_as = por.post_with_correction(
            _Artifact(), _Payload(), endpoint="http://cms", api_key="k",
            max_corrections=3)
        assert posted_as == "BD-2026-08-28-COR-3"
        assert len(posts.calls) == 4          # original + three attempts


class TestTheDailyPipelineUsesIt:
    """The wiring, which is the part that was missing."""

    def test_the_pipeline_calls_the_correction_flow_not_a_bare_post(self):
        import inspect

        import scripts.run_daily_pipeline as pipeline
        src = inspect.getsource(pipeline.stage_publish_report)
        assert "post_with_correction" in src, (
            "the daily path must reach the successor flow, or a re-run "
            "corrects everything except the public leaderboard")
        assert "post_payload(" not in src

    def test_the_stage_reports_the_epoch_it_actually_posted(self):
        """Reporting the ORIGINAL id after posting a successor would make the
        run record disagree with the CMS."""
        import inspect

        import scripts.run_daily_pipeline as pipeline
        src = inspect.getsource(pipeline.stage_publish_report)
        assert '"epoch_id": posted_as' in src
        assert '"supersedes"' in src
