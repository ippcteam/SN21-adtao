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
    """Stands in for the pydantic payload, including `model_copy`.

    `miner_results` carries a marker so a test can tell the payload the
    caller built apart from one rebuilt inside the correction flow — which
    is the difference that decided what the public leaderboard showed.
    """

    def __init__(self, epoch_id="BD-2026-08-28", miner_results=None,
                 supersedes=None, commentary_markdown=None):
        self.epoch_id = epoch_id
        self.miner_results = ([] if miner_results is None else miner_results)
        self.supersedes = supersedes
        self.commentary_markdown = commentary_markdown

    def model_copy(self, update=None):
        clone = _Payload(self.epoch_id, list(self.miner_results),
                         self.supersedes, self.commentary_markdown)
        for key, value in (update or {}).items():
            setattr(clone, key, value)
        return clone


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


class TestTheCorrectionIsTheSameReport:
    """A correction must be the payload the caller built, re-labelled.

    It used to be rebuilt from the artifact alone inside this function, which
    dropped everything the caller had enriched the payload with — the day's
    allocation audit and its earning set. So a frozen day published a
    correction in which every miner appeared funded and no row carried the
    control that had acted on it, while the caller's own logs described the
    correct payload it had built and then discarded. Nothing reported a
    failure: the numbers checked before posting were not the numbers posted.
    """

    def _built_by_caller(self):
        return _Payload(miner_results=[
            {"uid": 1, "tier": None, "policies": [{"control": "one_payer"}]},
            {"uid": 2, "tier": "elite", "policies": []},
        ])

    def test_the_correction_carries_the_callers_rows(self, posts):
        posts.respond(httpx.Response(409, json=FROZEN),
                      httpx.Response(201, json={"id": "x"}))
        original = self._built_by_caller()
        sent = []
        import scripts.post_epoch_report as por_mod
        real = por_mod.post_payload

        def capture(payload, **kw):
            sent.append(payload)
            return real(payload, **kw)

        por_mod.post_payload = capture
        try:
            _resp, posted_as = por.post_with_correction(
                _Artifact(), original, endpoint="http://cms", api_key="k")
        finally:
            por_mod.post_payload = real

        assert posted_as == "BD-2026-08-28-COR-1"
        correction = sent[-1]
        assert correction.miner_results == original.miner_results, (
            "the correction must publish the rows that were built and "
            "checked, not a rebuild that silently lacks them")
        assert any(r["policies"] for r in correction.miner_results)

    def test_only_the_three_labelling_fields_change(self, posts):
        posts.respond(httpx.Response(409, json=FROZEN),
                      httpx.Response(201, json={"id": "x"}))
        original = self._built_by_caller()
        _resp, _posted = por.post_with_correction(
            _Artifact(), original, endpoint="http://cms", api_key="k")
        # The original object is left alone; the copy is what was posted.
        assert original.epoch_id == "BD-2026-08-28"
        assert original.supersedes is None

    def test_it_cannot_be_asked_to_rebuild_the_payload(self):
        """No membership_uids escape hatch: re-aggregating inside here is the
        defect, so the parameter that invited it is gone."""
        import inspect
        params = inspect.signature(por.post_with_correction).parameters
        assert "membership_uids" not in params
        # Strip comments first: the function DOCUMENTS why it no longer
        # rebuilds, and a naive grep matches that prose instead of the code —
        # a test that reads explanations rather than behaviour.
        src = "\n".join(
            line for line in
            inspect.getsource(por.post_with_correction).splitlines()
            if not line.strip().startswith("#"))
        assert "aggregate(" not in src


class TestTheCorrectionKeepsTheDaysExplanation:
    """Correcting a day must not delete why the day looked as it did.

    A held day carries a note saying no new weights were set and the
    previous allocation still pays. Correcting that day replaced the note
    with "this entry supersedes the original report", which tells a miner
    nothing about why nobody's rank moved — and a held day is exactly the
    kind that gets corrected.
    """

    def test_the_callers_commentary_survives(self, posts):
        posts.respond(httpx.Response(409, json=FROZEN),
                      httpx.Response(201, json={"id": "x"}))
        original = _Payload(commentary_markdown="The day was held.")
        sent = []
        import scripts.post_epoch_report as m
        real = m.post_payload
        m.post_payload = lambda p, **kw: (sent.append(p), real(p, **kw))[1]
        try:
            por.post_with_correction(_Artifact(), original,
                                     endpoint="http://cms", api_key="k")
        finally:
            m.post_payload = real
        body = sent[-1].commentary_markdown
        assert "The day was held." in body
        assert "supersedes" in body, "the correction notice is still added"

    def test_a_day_with_nothing_to_say_gets_only_the_notice(self, posts):
        posts.respond(httpx.Response(409, json=FROZEN),
                      httpx.Response(201, json={"id": "x"}))
        sent = []
        import scripts.post_epoch_report as m
        real = m.post_payload
        m.post_payload = lambda p, **kw: (sent.append(p), real(p, **kw))[1]
        try:
            por.post_with_correction(_Artifact(), _Payload(),
                                     endpoint="http://cms", api_key="k")
        finally:
            m.post_payload = real
        assert sent[-1].commentary_markdown.startswith("Correction of")
