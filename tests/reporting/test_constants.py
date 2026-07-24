"""Tests for the leaderboard-reporting constants in hope/constants.py."""

from __future__ import annotations

import re

from hope.constants import EPOCH_TYPE_TABLE, SCORING_FORMULA_VERSION


def test_scoring_formula_version_is_semver_like():
    """SCORING_FORMULA_VERSION mirrors the spec doc's Version row.

    Format is `MAJOR.MINOR.PATCH` — no leading `v`, no pre-release tag.
    """
    assert re.fullmatch(r"\d+\.\d+\.\d+", SCORING_FORMULA_VERSION), (
        f"SCORING_FORMULA_VERSION must be MAJOR.MINOR.PATCH, got {SCORING_FORMULA_VERSION!r}"
    )


def test_epoch_type_table_has_phase_one_row():
    """Phase 1 must have at least the SEARCH/CAMPAIGN entry — the launch default."""
    keys = {(row[0], row[1]) for row in EPOCH_TYPE_TABLE}
    assert ("SEARCH", "CAMPAIGN") in keys


def test_epoch_type_table_row_shape():
    """Every row carries (campaign_type, scope, type, subtype, multiplier).

    Types: str, str|None, str, str|None, float (> 0).
    """
    for row in EPOCH_TYPE_TABLE:
        assert len(row) == 5, f"row {row!r} has wrong arity"
        campaign_type, scope, etype, subtype, mult = row
        assert isinstance(campaign_type, str) and campaign_type
        assert scope is None or isinstance(scope, str)
        assert isinstance(etype, str) and etype
        assert subtype is None or isinstance(subtype, str)
        assert isinstance(mult, float) and mult > 0


def test_epoch_type_multipliers_match_spec():
    """The multipliers must match SN21_REWARD_MECHANISM.md §"Component 3".

    Spec table:
      Search — campaign-level | 1.0
      Search — sub-campaign   | 1.2
      PMax                    | 1.5
      Shopping                | 1.3
      Consolidation           | 2.0
      Championship            | 3.0
    """
    expected = {
        ("Search", "campaign-level"): 1.0,
        ("Search", "sub-campaign"): 1.2,
        ("PMax", None): 1.5,
        ("Shopping", None): 1.3,
        ("Consolidation", None): 2.0,
        ("Championship", None): 3.0,
    }
    actual = {(row[2], row[3]): row[4] for row in EPOCH_TYPE_TABLE}
    assert actual == expected
