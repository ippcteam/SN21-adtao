"""A published "Funded" badge must mean the miner is actually being paid.

WHY THIS EXISTS
    The report's tiers are derived from the STANDINGS, and the earning
    controls deliberately never touch standings — a suppressed miner keeps
    its score, so it keeps its tier. The leaderboard renders any row with a
    tier as "Funded".

    Published as-is, a hotkey excluded for running someone else's model shows
    as "Funded · Elite" with a line underneath saying it was excluded. That is
    worse than showing nothing: the two halves of the same row disagree, and
    the miner asks which one is true.

    The weight vector is the only ground truth for who is paid, so it decides
    the tier. The score is untouched — it is a fact either way — and the
    policy note keeps carrying the reason.
"""

import pytest

from hope.reporting.aggregator import _build_miner_results


class _Artifact:
    """Minimal stand-in: only the fields _build_miner_results reads."""

    def __init__(self, scored, tiers):
        self.per_uid_scores = [
            {"uid": i, "hotkey": hk, "raw_score": 0.5, "met_baseline": True}
            for i, hk in enumerate(scored)
        ]
        self.tier_result = {"elite": list(tiers), "excluded": {}}


# Real-shaped SS58 addresses: MinerResult validates the format, so a short
# placeholder fails on the schema rather than on what is under test.
PAID = "5C5kbkWC7YmbS1Uh6FjAYtU3e8N1aHV4vK8emZt9ivNaBZGi"
SUPPRESSED = "5CzE7cNrvZ6fHH4fZcNrWG5Rx7tz1xAvDzBZkYi9uxgnJkEk"


def _rows(earning_set, collapse_audit=None):
    artifact = _Artifact([PAID, SUPPRESSED], [PAID, SUPPRESSED])
    results = _build_miner_results(
        artifact, tier_split_active=True, earning_set=earning_set,
        collapse_audit=collapse_audit)
    return {r.hotkey: r for r in results}


class TestTierFollowsTheVector:
    def test_a_suppressed_miner_is_not_shown_as_funded(self):
        rows = _rows({PAID})
        assert rows[PAID].tier == "elite"
        assert rows[SUPPRESSED].tier is None, (
            "a hotkey outside the weight vector must not render as Funded")

    def test_its_score_is_still_published(self):
        """Suppression removes a seat, not a fact. The score is what the
        miner earned on accuracy and it stays visible."""
        rows = _rows({PAID})
        assert rows[SUPPRESSED].score == pytest.approx(0.5)
        assert rows[SUPPRESSED].status == "scored"

    def test_no_vector_leaves_every_tier_alone(self):
        """A gated day publishes no weights. Treating that as 'nobody is
        funded' would report the whole field unfunded on a day when the
        weights merely held."""
        rows = _rows(None)
        assert rows[PAID].tier == "elite"
        assert rows[SUPPRESSED].tier == "elite"

    def test_an_empty_vector_is_not_confused_with_no_vector(self):
        """Passed explicitly, an empty set means nobody was paid."""
        rows = _rows(set())
        assert rows[PAID].tier is None
        assert rows[SUPPRESSED].tier is None


class TestTheReasonTravelsWithTheRow:
    def test_the_row_carries_both_the_verdict_and_the_reason(self):
        """The two must agree: not funded, and here is which control did it.
        Either alone is what sends a miner to chat."""
        audit = {"suppressed": [SUPPRESSED],
                 "lineage": {"groups": [{"payee": PAID,
                                         "eliminated": [SUPPRESSED]}]}}
        rows = _rows({PAID}, collapse_audit=audit)
        row = rows[SUPPRESSED]
        assert row.tier is None
        assert row.policies, "an excluded miner must carry its reason"
        assert any(p.control in ("one_payer", "lineage") for p in row.policies)

    def test_the_paid_miner_is_not_given_a_reason(self):
        """The controls name only the excluded party. Marking the payee too
        would read as an accusation against the person who did nothing."""
        audit = {"suppressed": [SUPPRESSED],
                 "lineage": {"groups": [{"payee": PAID,
                                         "eliminated": [SUPPRESSED]}]}}
        rows = _rows({PAID}, collapse_audit=audit)
        assert not rows[PAID].policies
