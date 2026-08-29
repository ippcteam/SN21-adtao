"""The published allocation audit — the document that makes a grouping checkable.

WHY THIS EXISTS
    The rules promise every detected group is published with its working.
    What existed was the operator's own copy and a per-miner line on the
    leaderboard, which tells a miner they were excluded without letting them
    see the group, who kept the seat, or any group they are not in. A miner
    asked where to find the list; there was nowhere to send them.

    These tests pin the properties that make the document worth publishing:
    whole groups with the beneficiary named, a stated parameter version, and
    a shape that survives a control writing nothing.
"""

import json
import os
from datetime import date

from hope.publication.allocation_audit import (
    audit_path,
    build_document,
    write_allocation_audit,
)

DAY = date(2026, 8, 29)

AUDIT = {
    "suppressed": ["hk-copy-a", "hk-copy-b", "hk-copy-c"],
    "lineage": {"groups": [
        {"kind": "same_lineage", "payee": "hk-original",
         "eliminated": ["hk-copy-a", "hk-copy-b"], "evidence": "2 pairs"},
        {"kind": "same_predictions", "payee": "hk-second",
         "eliminated": ["hk-copy-c"], "evidence": "identical points"},
    ]},
    "coldkey_cap": {"applied": True, "dropped": 88},
    "tenure_gated": {"hotkeys": ["hk-young"], "stood_down": False},
    "policies": {"lineage": {"params_version": "lineage-v1",
                             "exemption_configured": False},
                 "tenure": {"min_days": 7}},
}


class TestTheGroupIsWhole:
    def test_the_seat_holder_is_named_beside_the_excluded(self):
        """A suppression whose beneficiary is hidden reads as arbitrary, and
        the precedence claim cannot be checked without it."""
        doc = build_document(DAY, AUDIT)
        big = doc["groups"][0]
        assert big["seat_held_by"] == "hk-original"
        assert big["excluded"] == ["hk-copy-a", "hk-copy-b"]
        assert big["size"] == 3

    def test_groups_are_largest_first(self):
        doc = build_document(DAY, AUDIT)
        assert [g["size"] for g in doc["groups"]] == [3, 2]

    def test_the_evidence_travels_with_the_group(self):
        doc = build_document(DAY, AUDIT)
        assert doc["groups"][0]["evidence"] == "2 pairs"

    def test_it_names_the_receipt_it_came_from(self):
        """The whole claim is that this recomputes from published data, so
        the document has to say which document."""
        doc = build_document(DAY, AUDIT)
        assert doc["generated_from"] == "/v1/daily/2026-08-29/receipt"
        assert "recompute" in doc["how_to_verify"].lower()

    def test_the_parameter_version_is_stated(self):
        """Published as promised in the threat model: the mechanism and the
        version, so a grouping can be checked against the calibration that
        made it."""
        assert build_document(DAY, AUDIT)["parameters_version"] == "lineage-v1"

    def test_whether_an_exemption_was_in_force_is_visible(self):
        doc = build_document(DAY, AUDIT)
        assert doc["reference_exemption_configured"] is False


class TestSummaryMatchesTheGroups:
    def test_the_counts_are_the_ones_a_reader_would_total(self):
        s = build_document(DAY, AUDIT)["summary"]
        assert s["groups"] == 2
        assert s["hotkeys_excluded_as_copies"] == 3
        assert s["hotkeys_excluded_by_coldkey_cap"] == 88
        assert s["hotkeys_below_tenure"] == 1


class TestCountsAreCountsWhicheverShapeArrives:
    """`coldkey_cap.dropped` is a count in the per-control status block and
    the list of hotkeys in the audit body. Publishing one where the other was
    expected put ninety addresses in a summary field."""

    def test_a_list_of_hotkeys_summarises_as_its_length(self):
        audit = {**AUDIT, "coldkey_cap": {"applied": True,
                                          "dropped": ["hk-a", "hk-b"]}}
        doc = build_document(DAY, audit)
        assert doc["summary"]["hotkeys_excluded_by_coldkey_cap"] == 2
        assert doc["one_coldkey_one_seat"]["excluded"] == ["hk-a", "hk-b"]

    def test_a_plain_count_still_works(self):
        doc = build_document(DAY, AUDIT)
        assert doc["summary"]["hotkeys_excluded_by_coldkey_cap"] == 88
        assert doc["one_coldkey_one_seat"]["excluded"] == []

    def test_a_missing_value_is_none_not_zero(self):
        """Zero excluded and "the control did not record" are different
        statements about the day."""
        doc = build_document(DAY, {"coldkey_cap": {}})
        assert doc["summary"]["hotkeys_excluded_by_coldkey_cap"] is None


class TestItSurvivesAThinDay:
    def test_a_day_with_no_audit_still_publishes(self):
        """A control that recorded nothing must publish as nothing, not take
        the document out — a missing audit is indistinguishable from a day
        the controls did not run."""
        doc = build_document(DAY, None)
        assert doc["groups"] == []
        assert doc["summary"]["groups"] == 0
        assert doc["day"] == "2026-08-29"

    def test_a_partial_audit_does_not_raise(self):
        doc = build_document(DAY, {"suppressed": ["hk-a"]})
        assert doc["summary"]["hotkeys_excluded_as_copies"] == 1
        assert doc["parameters_version"] is None


class TestWriting:
    def test_it_lands_where_the_route_and_mirror_look(self, tmp_path):
        root = str(tmp_path)
        out = write_allocation_audit(root, DAY, AUDIT)
        assert os.path.exists(audit_path(root, DAY))
        assert out["groups"] == 2
        with open(audit_path(root, DAY)) as fh:
            assert json.load(fh)["groups"][0]["seat_held_by"] == "hk-original"

    def test_a_rerun_replaces_it(self, tmp_path):
        """Unlike the receipt this is a derived view. A corrected allocation
        must not leave a stale audit standing beside it."""
        root = str(tmp_path)
        write_allocation_audit(root, DAY, AUDIT)
        write_allocation_audit(root, DAY, {"suppressed": [], "lineage":
                                           {"groups": []}})
        with open(audit_path(root, DAY)) as fh:
            assert json.load(fh)["groups"] == []

    def test_no_temp_file_is_left_behind(self, tmp_path):
        root = str(tmp_path)
        write_allocation_audit(root, DAY, AUDIT)
        leftovers = [f for f in os.listdir(os.path.join(root, "allocation_audit"))
                     if f.endswith(".tmp")]
        assert leftovers == []
