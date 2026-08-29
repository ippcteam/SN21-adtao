"""The reference exemption must reach the lineage path, not only the exact one.

WHY THIS EXISTS
    Two detectors decide who is excluded for running someone else's model: the
    exact-fingerprint one and the behavioural (lineage) one. The published
    rules grant one exemption covering both — "running the reference unchanged
    is participation, not plagiarism" — but only the exact path ever read it.
    The lineage path returned its groups straight to a caller that suppresses
    everything it is handed.

    That gap was invisible while lineage was switched off, and would have
    become live pay the moment anyone set the four parameters: the reference
    cluster is the largest group on the board, so the first day of enforcement
    would have zeroed reference runners while the docs promised they were safe.

    These tests pin the exemption to the lineage path, and pin the two paths to
    ONE derivation so they cannot drift apart again.
"""

from datetime import date

import pytest

from hope.validator.daily_stream_weights import (
    copy_exempt_hotkeys,
    lineage_from_receipts,
)

DAY = date(2026, 8, 20)

# Configured lineage parameters. Values are irrelevant to the exemption, so
# they are set permissively: this file is about WHO is spared, not where the
# boundary sits.
LINEAGE_ON = {
    "SN21_LINEAGE_CORR_MIN": "0.98",
    "SN21_LINEAGE_SIGN_MIN": "0.95",
    "SN21_LINEAGE_DISTANCE_MAX": "0.05",
    "SN21_LINEAGE_DISAGREE_MAX": "0.10",
    "SN21_LINEAGE_PARAMS_VERSION": "test-v1",
}


def _ledger(tmp_path, entries, outcomes):
    import json
    import os

    d = os.path.join(str(tmp_path), "receipts")
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, f"{DAY}.json"), "w") as fh:
        json.dump({"document": {"metrics": {"entries": entries,
                                            "outcomes": outcomes}}}, fh)
    return str(tmp_path)


def _cluster(tmp_path, hotkeys):
    """A ledger where every named hotkey runs the same behaviour.

    Predictions track the actuals closely and vary across episodes — a flat
    or wrong answer would be excluded by the trivial-agreement guard rather
    than by the exemption, which would make these tests pass for the wrong
    reason. The row count clears MIN_OVERLAP_ROWS (30); below it every pair
    is unjudged and the groups come back empty for a reason that has nothing
    to do with what is under test.
    """
    actuals = [round(-0.20 + 0.01 * i, 4) for i in range(40)]
    entries, outcomes = [], []
    for i, actual in enumerate(actuals):
        outcomes.append({"episode_id": f"e{i}", "horizon_days": 7,
                         "cost_delta_pct": actual})
    for n, hk in enumerate(hotkeys):
        for i, actual in enumerate(actuals):
            # Same behaviour, differing in the last decimals — the shape the
            # exact detector cannot see and lineage exists to catch.
            entries.append({
                "miner": hk, "episode_id": f"e{i}", "horizon_days": 7,
                "prediction": {"cost_delta_pct":
                               {"p50": round(actual + 1e-9 * (n + 1), 12)}},
            })
    return _ledger(tmp_path, entries, outcomes)


class TestTheExemptionReachesLineage:
    def test_without_it_the_cluster_is_enforced(self, tmp_path):
        """Baseline: absent an exemption this really is one lineage."""
        root = _cluster(tmp_path, ["house-model", "runner-a", "runner-b"])
        groups, audit = lineage_from_receipts(root, DAY, dict(LINEAGE_ON))
        assert len(groups) == 1
        assert audit["exemption_configured"] is False

    def test_a_group_containing_the_house_hotkey_is_stood_down(self, tmp_path):
        root = _cluster(tmp_path, ["house-model", "runner-a", "runner-b"])
        groups, audit = lineage_from_receipts(
            root, DAY, {**LINEAGE_ON, "SN21_HOUSE_HOTKEY": "house-model"})
        assert groups == [], "reference runners must not be suppressed"
        assert audit["exempt_groups"][0]["payee"]
        assert sorted(audit["exempt_groups"][0]["stood_down"])

    def test_the_explicit_list_works_before_a_house_model_exists(self, tmp_path):
        """The house hotkey is a chain fact we may not have yet; the list is
        the lever that works today."""
        root = _cluster(tmp_path, ["ref-runner", "runner-a", "runner-b"])
        groups, _ = lineage_from_receipts(
            root, DAY, {**LINEAGE_ON, "SN21_COPY_EXEMPT_HOTKEYS": "ref-runner"})
        assert groups == []

    def test_an_unrelated_exempt_hotkey_spares_nobody(self, tmp_path):
        """The exemption is not a global off-switch."""
        root = _cluster(tmp_path, ["house-model", "runner-a", "runner-b"])
        groups, _ = lineage_from_receipts(
            root, DAY, {**LINEAGE_ON, "SN21_COPY_EXEMPT_HOTKEYS": "somebody-else"})
        assert len(groups) == 1


class TestExemptGroupsStayVisible:
    def test_a_stood_down_group_is_still_published(self, tmp_path):
        """'Evidence, not accusation' cuts both ways: a grouping we declined
        to act on is still a grouping we found, and hiding it would make the
        published audit disagree with the detector."""
        root = _cluster(tmp_path, ["house-model", "runner-a"])
        _, audit = lineage_from_receipts(
            root, DAY, {**LINEAGE_ON, "SN21_HOUSE_HOTKEY": "house-model"})
        assert audit["exempt_groups"], "the group must still appear"
        assert "reference" in audit["exempt_groups"][0]["reason"]

    def test_the_audit_records_whether_an_exemption_was_configured(self, tmp_path):
        """An operator reading the day's audit must be able to tell 'no
        reference runners today' apart from 'no exemption was set'."""
        root = _cluster(tmp_path, ["a", "b"])
        _, off = lineage_from_receipts(root, DAY, dict(LINEAGE_ON))
        _, on = lineage_from_receipts(
            root, DAY, {**LINEAGE_ON, "SN21_HOUSE_HOTKEY": "elsewhere"})
        assert off["exemption_configured"] is False
        assert on["exemption_configured"] is True


class TestBothDetectorsShareOneDerivation:
    """The two paths must read the same exemption from the same places."""

    @pytest.mark.parametrize("environ,expected", [
        ({}, set()),
        ({"SN21_HOUSE_HOTKEY": "hk-house"}, {"hk-house"}),
        ({"SN21_COPY_EXEMPT_HOTKEYS": "hk-a, hk-b"}, {"hk-a", "hk-b"}),
        ({"SN21_HOUSE_HOTKEY": "hk-house",
          "SN21_COPY_EXEMPT_HOTKEYS": "hk-a"}, {"hk-house", "hk-a"}),
        ({"SN21_COPY_EXEMPT_HOTKEYS": " , ,"}, set()),
    ])
    def test_both_sources_are_combined(self, environ, expected):
        assert set(copy_exempt_hotkeys(environ)) == expected

    def test_the_exact_path_uses_the_same_helper(self, tmp_path):
        """Pins the shared derivation: the exact detector must honour the
        explicit list too, which before this change only lineage-adjacent
        code knew about."""
        import json
        import os

        from hope.validator.daily_stream_weights import (
            one_payer_suppression_from_receipts,
        )

        d = os.path.join(str(tmp_path), "receipts")
        os.makedirs(d, exist_ok=True)
        entry = lambda hk: {"miner": hk, "episode_id": "e1",  # noqa: E731
                            "horizon_days": 7,
                            "prediction": {"cost_delta_pct": {"p50": -0.05}}}
        with open(os.path.join(d, f"{DAY}.json"), "w") as fh:
            json.dump({"document": {"metrics": {
                "entries": [entry("ref-runner"), entry("newcomer")]}}}, fh)

        assert one_payer_suppression_from_receipts(
            str(tmp_path), DAY,
            {"SN21_COPY_EXEMPT_HOTKEYS": "ref-runner"}) == frozenset()
        # Unexempted, one of the pair is suppressed. WHICH one is precedence,
        # not this test's business — with no history the ordering is lexical,
        # so asserting the specific hotkey here would pin unrelated behaviour.
        assert len(one_payer_suppression_from_receipts(
            str(tmp_path), DAY, {})) == 1
