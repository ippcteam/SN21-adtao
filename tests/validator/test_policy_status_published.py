"""Every documented control says what it did, every day.

WHY THIS EXISTS
    The audit blocks are written only when a control has something to say, so
    silence meant three different things at once: the control was switched
    off, or it ran and found nothing, or it could not run at all. All three
    published a byte-identical record.

    That is not a reporting nicety. A transient chain fault can take the
    identity read down, and the run then publishes a vector that looks exactly
    like a day on which the cap found nothing to do. The cap normally removes
    a substantial share of the field, so the largest single-day change to who
    gets paid could leave no trace in the document miners are told to audit.

    SN21_REWARDS.md and SN21_THREAT_MODEL.md name these controls to miners.
    "Did it run today" therefore has to be answerable from the published
    record, not from our logs — which is what `policies` is for, and what
    these tests hold in place.
"""

from datetime import date

import pytest

from hope.scoring.champion_promotion import PromotionState
from hope.scoring.episode_average import ScoredEpisode
from hope.validator.daily_stream_weights import compute_daily_allocation

DAY = date(2026, 8, 28)


def _entries(**standings):
    return {hk: [ScoredEpisode(score=s, scored_on=DAY) for _ in range(300)]
            for hk, s in standings.items()}


def _alloc(**kw):
    standings = kw.pop("standings", {"a": 0.80, "b": 0.70})
    return compute_daily_allocation(
        _entries(**standings), DAY, day_episode_volume=500,
        promotion_state=PromotionState(), **kw)


def _policies(**kw):
    return _alloc(**kw).collapse_audit["policies"]


class TestAlwaysPresent:
    def test_a_quiet_day_still_reports_every_control(self):
        """The failure mode this closes: nothing to suppress reads the same as
        nothing switched on."""
        pol = _policies()
        assert set(pol) == {"coldkey_cap", "one_payer", "lineage", "tenure"}

    def test_it_is_present_even_when_no_control_is_configured(self):
        pol = _policies(one_payer_on=False, lineage_on=False, tenure_min=0)
        assert pol["one_payer"]["enabled"] is False
        assert pol["lineage"]["configured"] is False
        assert pol["tenure"]["enabled"] is False


class TestColdkeyCap:
    """The control that silently vanished."""

    def test_a_missing_identity_map_is_recorded_as_not_applied(self):
        pol = _policies(coldkey_of=None)
        assert pol["coldkey_cap"]["applied"] is False
        assert pol["coldkey_cap"]["reason"]

    def test_applying_it_records_what_it_saw_and_did(self):
        pol = _policies(standings={"farmA": 0.80, "farmB": 0.79, "honest": 0.70},
                        coldkey_of={"farmA": "ck1", "farmB": "ck1",
                                    "honest": "ck2"})
        assert pol["coldkey_cap"]["applied"] is True
        assert pol["coldkey_cap"]["identities"] == 3
        assert pol["coldkey_cap"]["dropped"] == 1

    def test_ran_and_dropped_nobody_differs_from_could_not_run(self):
        """THE test. These two days were indistinguishable in the published
        record, and one of them means a share of the field was paid that the
        cap would have removed."""
        clean = _policies(standings={"a": 0.80, "b": 0.70},
                          coldkey_of={"a": "ck1", "b": "ck2"})
        broken = _policies(standings={"a": 0.80, "b": 0.70}, coldkey_of=None)

        assert clean["coldkey_cap"]["applied"] is True
        assert clean["coldkey_cap"]["dropped"] == 0
        assert broken["coldkey_cap"]["applied"] is False
        assert clean["coldkey_cap"] != broken["coldkey_cap"]

    def test_an_empty_identity_map_counts_as_not_applied(self):
        """`{}` reaching this far means the read produced nothing; capping
        over nothing must not report as a control that ran."""
        pol = _policies(coldkey_of={})
        assert pol["coldkey_cap"]["applied"] is False


class TestOnePayerAndLineage:
    def test_switched_on_but_finding_nothing_is_visible(self):
        """One payer per model has been enabled since the rules were published
        and has never recorded a suppression. That may be correct, but it must
        be legible as 'on, found none' rather than inferred from an absent
        key."""
        pol = _policies(one_payer_on=True, copy_suppressed=frozenset())
        assert pol["one_payer"]["enabled"] is True
        assert pol["one_payer"]["suppressed"] == 0

    def test_no_receipts_is_not_the_same_as_no_copies(self):
        """The detector returns an empty set both when the field is clean and
        when it had nothing to read. Publishing only the verdict makes a
        broken control indistinguishable from a healthy subnet, so the INPUT
        is published beside it."""
        clean = _policies(one_payer_on=True, copy_suppressed=frozenset(),
                          one_payer_stats={"fingerprints_today": 117,
                                           "days_indexed": 12, "groups": 0})
        blind = _policies(one_payer_on=True, copy_suppressed=frozenset(),
                          one_payer_stats={"fingerprints_today": 0,
                                           "days_indexed": 0, "groups": 0})

        assert clean["one_payer"]["suppressed"] == blind["one_payer"]["suppressed"] == 0
        assert clean["one_payer"]["fingerprints"] == 117
        assert blind["one_payer"]["fingerprints"] == 0
        assert clean["one_payer"] != blind["one_payer"]

    def test_a_failed_detector_publishes_its_reason(self):
        """The subprocess fails empty by design. Empty must not read as a
        clean day."""
        pol = _policies(one_payer_on=True,
                        one_payer_stats={"ran": False,
                                         "reason": "child exited 137"})
        assert pol["one_payer"]["reason"] == "child exited 137"

    def test_a_suppression_is_counted(self):
        pol = _policies(standings={"a": 0.80, "b": 0.70},
                        one_payer_on=True, copy_suppressed=frozenset({"b"}))
        assert pol["one_payer"]["suppressed"] == 1

    def test_uncalibrated_lineage_is_reported_as_unconfigured(self):
        """The four lineage parameters are unset in production, so the control
        returns empty. Unconfigured and 'configured, found no copies' are
        different claims about the subnet."""
        pol = _policies(lineage_on=False)
        assert pol["lineage"]["configured"] is False
        assert pol["lineage"]["groups"] == 0

    def test_configured_lineage_reports_its_work(self):
        pol = _policies(lineage_on=True, lineage_audit={("a", "b"): {}})
        assert pol["lineage"]["configured"] is True
        assert pol["lineage"]["pairs_examined"] == 1

    def test_an_unstated_switch_is_unknown_not_assumed_off(self):
        """A caller that does not say must not be recorded as 'off' — that
        would publish a claim we never checked."""
        pol = _policies()
        assert pol["one_payer"]["enabled"] is None
        assert pol["lineage"]["configured"] is None


class TestTenure:
    def test_the_gate_reports_how_many_it_held_back(self):
        # The newcomer needs VOLUME to clear the cold-start floor and become
        # placement-eligible, but few distinct scored DAYS — that is exactly
        # the miner the tenure gate exists for: a standing computed over one
        # day, which carries no evidence of sustained accuracy.
        entries = {
            "veteran": [ScoredEpisode(score=0.9, scored_on=date(2026, 8, d))
                        for d in range(10, 28) for _ in range(20)],
            "newcomer": [ScoredEpisode(score=0.95, scored_on=DAY)
                         for _ in range(300)],
        }
        alloc = compute_daily_allocation(
            entries, DAY, day_episode_volume=500,
            promotion_state=PromotionState(), tenure_min=7)
        pol = alloc.collapse_audit["policies"]["tenure"]
        assert pol["enabled"] is True and pol["min_days"] == 7
        assert pol["gated"] >= 1

    def test_a_disabled_gate_says_so(self):
        pol = _policies(tenure_min=0)
        assert pol["tenure"]["enabled"] is False
        assert pol["tenure"]["gated"] == 0


class TestBackwardCompatibility:
    def test_the_existing_detail_blocks_are_unchanged(self):
        """verify_day --recheck-grouping reads `suppressed`, and the CMS reads
        the detail blocks. Adding a status summary must not move them."""
        alloc = _alloc(standings={"farmA": 0.80, "farmB": 0.79},
                       coldkey_of={"farmA": "ck1", "farmB": "ck1"},
                       copy_suppressed=frozenset({"farmB"}))
        assert alloc.collapse_audit["coldkey_cap"]["dropped"] == ["farmB"]
        assert alloc.collapse_audit["suppressed"] == ["farmB"]
