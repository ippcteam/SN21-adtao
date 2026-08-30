"""Each miner's row carries the reason a control acted on it.

WHY
    The allocation audit publishes each control as its own fleet-level list —
    who the coldkey cap dropped, who tenure held back, who was suppressed as
    a copy. That shape is right for verifying the RULE and wrong for
    answering the question a miner actually has, which is "why am I not being
    paid today". Today they must search several arrays to find themselves,
    and a reader of the public leaderboard cannot see any of it.

    A rule enforced where nobody can see it applied is, from the miner's
    side, indistinguishable from a rule applied arbitrarily. So the reason
    travels in the miner's own row.
"""

from hope.reporting.aggregator import policies_by_hotkey

AUDIT = {
    "coldkey_cap": {
        "dropped": ["farmB", "farmC"],
        "contested": {"ck1": ["farmA", "farmB", "farmC"]},
    },
    "suppressed": ["copycat"],
    "lineage": {
        "groups": [{"payee": "author", "eliminated": ["cloner"],
                    "kind": "same_lineage"}],
    },
    "tenure_gated": {"min_days": 7, "hotkeys": ["newcomer"]},
}


def _controls(notes, hotkey):
    return [n.control for n in notes.get(hotkey, [])]


class TestEachControlExplainsItself:
    def test_a_capped_hotkey_is_told_who_holds_the_seat(self):
        notes = policies_by_hotkey(AUDIT)
        [note] = notes["farmB"]
        assert note.control == "coldkey_cap"
        assert note.counterparty == "farmA", (
            "naming the holder is the difference between a reason and an "
            "assertion")
        assert "one earning seat" in note.detail

    def test_every_loser_of_a_contest_is_told_the_same_holder(self):
        notes = policies_by_hotkey(AUDIT)
        assert notes["farmC"][0].counterparty == "farmA"

    def test_the_hotkey_that_kept_the_seat_gets_no_note(self):
        """Only miners a control ACTED ON are annotated. The winner of a
        contest was not penalised and must not read as though it was."""
        assert "farmA" not in policies_by_hotkey(AUDIT)

    def test_the_holder_is_derived_from_who_survived_not_from_list_order(self):
        """`contested` is sorted ALPHABETICALLY, not by rank — the ordering
        that decided the seat is not published. Reading the first entry as
        the winner names the wrong hotkey, and here would name one that was
        itself dropped."""
        notes = policies_by_hotkey({
            "coldkey_cap": {
                "dropped": ["aaa", "bbb"],
                "contested": {"ck": ["aaa", "bbb", "zzz"]},
            }})
        assert notes["aaa"][0].counterparty == "zzz"
        assert notes["bbb"][0].counterparty == "zzz"

    def test_an_ambiguous_group_names_nobody(self):
        """If more than one member survived, or the audit is partial, saying
        nothing beats picking one arbitrarily."""
        notes = policies_by_hotkey({
            "coldkey_cap": {
                "dropped": ["a"],
                "contested": {"ck": ["a", "b", "c"]},   # two survivors
            }})
        assert notes["a"][0].counterparty is None

    def test_a_suppressed_copy_is_told_why(self):
        [note] = policies_by_hotkey(AUDIT)["copycat"]
        assert note.control == "one_payer"
        assert "pay once" in note.detail

    def test_a_lineage_elimination_names_the_payee(self):
        [note] = policies_by_hotkey(AUDIT)["cloner"]
        assert note.control == "lineage"
        assert note.counterparty == "author"

    def test_tenure_states_the_bar_and_that_scores_still_count(self):
        """A miner held back by tenure has not been punished — the most
        useful thing the row can say is that showing up is the remedy."""
        [note] = policies_by_hotkey(AUDIT)["newcomer"]
        assert note.control == "tenure"
        assert "7 scored days" in note.detail
        assert "scores still count" in note.detail.lower()

    def test_a_stood_down_tenure_gate_says_so(self):
        notes = policies_by_hotkey({
            "tenure_gated": {"min_days": 7, "hotkeys": ["a"],
                             "stood_down": True}})
        assert "stood down" in notes["a"][0].detail.lower()


class TestSeveralControlsAtOnce:
    def test_a_miner_hit_by_two_controls_carries_both_reasons(self):
        """Telling someone only the first reason they were excluded invites
        them to fix it and be excluded again."""
        notes = policies_by_hotkey({
            "coldkey_cap": {"dropped": ["x"], "contested": {"ck": ["y", "x"]}},
            "tenure_gated": {"min_days": 7, "hotkeys": ["x"]},
        })
        assert set(_controls(notes, "x")) == {"coldkey_cap", "tenure"}


class TestItInventsNothing:
    def test_an_empty_audit_produces_no_reasons(self):
        assert policies_by_hotkey({}) == {}

    def test_a_missing_audit_produces_no_reasons(self):
        assert policies_by_hotkey(None) == {}

    def test_junk_is_survived_rather_than_guessed_at(self):
        """A malformed audit must not become an invented accusation against
        a miner."""
        for junk in ("not a dict", [], 7,
                     {"coldkey_cap": "nope"},
                     {"lineage": {"groups": ["nope"]}},
                     {"tenure_gated": None}):
            assert policies_by_hotkey(junk) == {} or isinstance(
                policies_by_hotkey(junk), dict)

    def test_a_cap_without_contest_detail_still_explains_itself(self):
        """Missing counterparty must degrade to a reason without a name, not
        to no reason at all."""
        notes = policies_by_hotkey({"coldkey_cap": {"dropped": ["solo"]}})
        [note] = notes["solo"]
        assert note.control == "coldkey_cap"
        assert note.counterparty is None
        assert note.detail


class TestItReachesTheRow:
    def test_the_publish_stage_sends_the_audit_with_the_report(self):
        import inspect

        import scripts.run_daily_pipeline as pipeline
        src = inspect.getsource(pipeline.stage_publish_report)
        assert "collapse_audit=" in src, (
            "without the audit the report cannot say why anyone was excluded")

    def test_rows_default_to_null_not_to_an_empty_claim(self):
        """Null means 'this report predates per-miner reasons'; an empty list
        means 'the controls ran and none touched you'. Collapsing the two
        would let an old report read as a clean bill of health."""
        from hope.reporting.payload import MinerResult

        row = MinerResult(uid=1, hotkey="5" + "a" * 47, score=0.5,
                          status="scored")
        assert row.policies is None


class TestTenureSaysHowManyDays:
    """"Fewer than 7 scored days" made a miner count for themselves, and the
    obvious way to count — receipts you appear in — gives a HIGHER number,
    because one receipt can carry entries settled on more than one date.
    Three miners read 7 off the feed while the gate saw 6, and asked why they
    were being held back. The row now states the figure the gate used."""

    HK = "5GTGB9P8tc3gmFrDPqLpqPPFPtCF1mVwvfPYnQE6ss3AMRRb"

    def test_it_states_the_count_the_gate_used(self):
        notes = policies_by_hotkey({
            "tenure_gated": {"min_days": 7, "hotkeys": [self.HK],
                             "scored_days": {self.HK: 6}},
        })
        detail = notes[self.HK][0].detail
        assert "Scored on 6 of the 7 days needed" in detail
        assert "Fewer than" not in detail

    def test_scores_still_count_is_still_said(self):
        notes = policies_by_hotkey({
            "tenure_gated": {"min_days": 7, "hotkeys": [self.HK],
                             "scored_days": {self.HK: 6}},
        })
        assert "Scores still count" in notes[self.HK][0].detail

    def test_an_audit_without_counts_keeps_the_old_wording(self):
        """Older days carry no counts. Printing 0 there would tell a miner
        they have never scored, which is a different and untrue claim."""
        notes = policies_by_hotkey({
            "tenure_gated": {"min_days": 7, "hotkeys": [self.HK]},
        })
        detail = notes[self.HK][0].detail
        assert "Fewer than 7 scored days" in detail
        assert "0 of the 7" not in detail

    def test_a_stood_down_gate_still_says_so(self):
        notes = policies_by_hotkey({
            "tenure_gated": {"min_days": 7, "hotkeys": [self.HK],
                             "scored_days": {self.HK: 2}, "stood_down": True},
        })
        detail = notes[self.HK][0].detail
        assert "Scored on 2 of the 7 days needed" in detail
        assert "stood down" in detail


class TestADuplicateRowNamesWhoEarns:
    """"This model is already earning under an earlier submission" named
    nobody. A miner could not check that and could only deny it, which is the
    argument it produced. The row now names the hotkey holding the seat."""

    COPY = "5CzE7cNrvZ6fHH4fZcNrWG5Rx7tz1xAvDzBZkYi9uxgnJkEk"
    PAYEE = "5C5kbkWC7YmbS1Uh6FjAYtU3e8N1aHV4vK8emZt9ivNaBZGi"

    def _audit(self, **over):
        base = {"suppressed": [self.COPY],
                "one_payer_groups": [{"payee": self.PAYEE,
                                      "eliminated": [self.COPY],
                                      "kind": "same_predictions",
                                      "evidence": "identical point estimates"}]}
        base.update(over)
        return base

    def test_the_seat_holder_is_named(self):
        note = policies_by_hotkey(self._audit())[self.COPY][0]
        assert note.control == "one_payer"
        assert note.counterparty == self.PAYEE

    def test_the_payee_is_not_accused_of_anything(self):
        """The group has two members and only one of them is excluded."""
        notes = policies_by_hotkey(self._audit())
        assert self.PAYEE not in notes

    def test_an_audit_without_groups_still_produces_the_row(self):
        """Older days carry no group detail. The exclusion is still real, so
        the note stands — it just cannot name the counterparty."""
        note = policies_by_hotkey({"suppressed": [self.COPY]})[self.COPY][0]
        assert note.control == "one_payer"
        assert note.counterparty is None
        assert "already earning" in note.detail


class TestTheSeatRowNamesTheOwner:
    """"Another hotkey with the same owner holds it" asserts a relationship
    and offers no evidence for it, so a miner can only take it on trust. The
    coldkey and the number of hotkeys it runs are the evidence, and both were
    already in the audit — they simply were not surfaced."""

    OWNER = "5CVEj1BDwPqQ3qKZ9pRk9c9jV4vK8emZt9ivNaBZGiXXXXXX"
    KEPT = "5C5kbkWC7YmbS1Uh6FjAYtU3e8N1aHV4vK8emZt9ivNaBZGi"
    DROPPED = "5CSkpHaCJdgRL5BeSb8gpAuiJ5f9r5ZZw3Hg6jeupRo2U8yv"

    def _audit(self):
        return {"coldkey_cap": {
            "dropped": [self.DROPPED],
            "contested": {self.OWNER: [self.KEPT, self.DROPPED]},
        }}

    def test_it_names_the_coldkey_and_how_many_hotkeys_it_runs(self):
        note = policies_by_hotkey(self._audit())[self.DROPPED][0]
        assert self.OWNER in note.detail
        assert "runs 2 hotkeys" in note.detail

    def test_it_still_names_the_hotkey_holding_the_seat(self):
        note = policies_by_hotkey(self._audit())[self.DROPPED][0]
        assert note.counterparty == self.KEPT

    def test_the_hotkey_that_kept_the_seat_is_not_flagged(self):
        assert self.KEPT not in policies_by_hotkey(self._audit())

    def test_without_a_contested_map_it_falls_back_rather_than_inventing(self):
        """Older audits carry no owner map. The exclusion is still real; it
        just cannot cite the coldkey."""
        note = policies_by_hotkey(
            {"coldkey_cap": {"dropped": [self.DROPPED]}})[self.DROPPED][0]
        assert "same owner" in note.detail
        assert note.counterparty is None
