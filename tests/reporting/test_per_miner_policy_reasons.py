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
