"""The anti-clone layers where they actually decide money.

The layers were pure modules with green probes and NO caller — the same
flag-without-consumer shape that has now bitten this repo five times (anchor,
intake, one-payer, bridge, the coverage gate). These tests pin the wiring
itself: that the seam applies L1 then L2, that suppressed hotkeys keep their
standings, that the receipt carries the working, and that an uncalibrated
deployment suppresses nobody.
"""

import json
import os
from datetime import date

import pytest

from hope.scoring.champion_promotion import PromotionState
from hope.scoring.duplication import CopyGroup
from hope.scoring.episode_average import ScoredEpisode
from hope.validator.daily_stream_weights import (
    compute_daily_allocation,
    lineage_from_receipts,
    lineage_params_from_env,
)

DAY = date(2026, 8, 20)

CALIBRATED = {
    "SN21_LINEAGE_CORR_MIN": "0.98",
    "SN21_LINEAGE_SIGN_MIN": "0.95",
    "SN21_LINEAGE_DISTANCE_MAX": "0.05",
    "SN21_LINEAGE_DISAGREE_MAX": "0.10",
    "SN21_LINEAGE_PARAMS_VERSION": "redteam-v1",
}


def _entries(**standings):
    return {hk: [ScoredEpisode(score=s, scored_on=DAY) for _ in range(300)]
            for hk, s in standings.items()}


def _alloc(**kw):
    return compute_daily_allocation(
        _entries(**kw.pop("standings")), DAY, day_episode_volume=500,
        promotion_state=PromotionState(), **kw)


# ---- Layer 1 at the seam ---------------------------------------------------

def test_one_coldkey_takes_one_seat():
    alloc = _alloc(standings={"farmA": 0.80, "farmB": 0.79, "honest": 0.70},
                   coldkey_of={"farmA": "ck1", "farmB": "ck1", "honest": "ck2"})
    assert "farmB" not in alloc.weights
    assert alloc.weights["farmA"] > 0 and alloc.weights["honest"] > 0


def test_the_dropped_hotkey_keeps_its_standing():
    """Scores are facts. A hotkey that is not paid is not a hotkey that did
    not score, and removing it would make a payment decision look like a
    scoring one."""
    alloc = _alloc(standings={"farmA": 0.80, "farmB": 0.79},
                   coldkey_of={"farmA": "ck1", "farmB": "ck1"})
    assert abs(alloc.standings["farmB"] - 0.79) < 1e-9
    assert alloc.weights.get("farmB", 0.0) == 0.0


def test_the_coldkey_drop_is_published():
    alloc = _alloc(standings={"farmA": 0.80, "farmB": 0.79},
                   coldkey_of={"farmA": "ck1", "farmB": "ck1"})
    assert alloc.collapse_audit["coldkey_cap"]["dropped"] == ["farmB"]
    assert alloc.collapse_audit["coldkey_cap"]["contested"]["ck1"] == ["farmA", "farmB"]


def test_no_coldkey_map_changes_nothing():
    """Fail-safe: if we could not read identities, we do not confiscate seats."""
    assert _alloc(standings={"a": 0.8, "b": 0.7}).weights == \
           _alloc(standings={"a": 0.8, "b": 0.7}, coldkey_of=None).weights


# ---- Layer 2 at the seam ---------------------------------------------------

def test_a_lineage_group_pays_one_principal():
    group = CopyGroup(kind="same_lineage", original="author",
                      copies=("clone1", "clone2"), evidence="test")
    alloc = _alloc(standings={"author": 0.80, "clone1": 0.80, "clone2": 0.80},
                   lineage_groups=[group])
    assert alloc.weights["author"] == pytest.approx(1.0)
    assert alloc.weights.get("clone1", 0.0) == 0.0
    assert alloc.weights.get("clone2", 0.0) == 0.0


def test_the_copies_keep_their_standings_and_appear_in_the_audit():
    group = CopyGroup(kind="same_lineage", original="author",
                      copies=("clone1",), evidence="within tau")
    alloc = _alloc(standings={"author": 0.80, "clone1": 0.80},
                   lineage_groups=[group])
    assert abs(alloc.standings["clone1"] - 0.80) < 1e-9
    published = alloc.collapse_audit["lineage"]["groups"][0]
    assert published["payee"] == "author"
    assert published["eliminated"] == ["clone1"]
    assert published["evidence"] == "within tau"


def test_the_pairwise_numbers_are_published_for_recomputation():
    """A suppression a miner cannot recheck is an announcement, not evidence."""
    group = CopyGroup(kind="same_lineage", original="a", copies=("b",),
                      evidence="e")
    alloc = _alloc(standings={"a": 0.8, "b": 0.8}, lineage_groups=[group],
                   lineage_audit={"a|b": {"correlation": 0.999,
                                          "distance": 0.001},
                                  "params_version": "redteam-v1"})
    pairwise = alloc.collapse_audit["lineage"]["pairwise"]
    assert pairwise["a|b"]["correlation"] == 0.999
    assert pairwise["params_version"] == "redteam-v1"


# ---- the order the review specified ----------------------------------------

def test_layer_one_runs_before_layer_two():
    """standings -> coldkey cap -> lineage collapse -> curve. A hotkey the cap
    already removed must not also need catching by lineage."""
    group = CopyGroup(kind="same_lineage", original="farmA",
                      copies=("honest",), evidence="e")
    alloc = _alloc(standings={"farmA": 0.80, "farmB": 0.79, "honest": 0.70},
                   coldkey_of={"farmA": "ck1", "farmB": "ck1", "honest": "ck2"},
                   lineage_groups=[group])
    # farmB dropped by L1, honest dropped by L2, farmA is the only payee.
    assert {hk for hk, w in alloc.weights.items() if w > 0} == {"farmA"}
    assert "coldkey_cap" in alloc.collapse_audit
    assert "lineage" in alloc.collapse_audit


# ---- uncalibrated means off ------------------------------------------------

def test_an_uncalibrated_deployment_suppresses_nobody(tmp_path):
    """No red-teamed numbers means the control does not run — never a guess."""
    assert lineage_params_from_env({}).configured() is False
    assert lineage_from_receipts(str(tmp_path), DAY, {}) == ([], {})


def test_partial_configuration_is_also_off(tmp_path):
    partial = dict(CALIBRATED)
    partial.pop("SN21_LINEAGE_DISTANCE_MAX")
    assert lineage_params_from_env(partial).configured() is False
    assert lineage_from_receipts(str(tmp_path), DAY, partial) == ([], {})


def test_a_calibrated_deployment_groups_from_the_receipt(tmp_path):
    """End to end from the published document a miner also holds."""
    root = str(tmp_path)
    d = os.path.join(root, "receipts")
    os.makedirs(d, exist_ok=True)

    entries, outcomes = [], []
    for i in range(60):
        base = ((i * 37) % 100) / 100.0 - 0.5
        outcomes.append({"episode_id": f"e{i}", "horizon_days": 7,
                         "cost_delta_pct": 1.0})
        for hk, val in (("author", base), ("clone", base + 1e-6)):
            entries.append({"miner": hk, "episode_id": f"e{i}",
                            "horizon_days": 7,
                            "prediction": {"cost_delta_pct": {"p50": val}}})
    with open(os.path.join(d, f"{DAY}.json"), "w") as fh:
        json.dump({"document": {"metrics": {"entries": entries,
                                            "outcomes": outcomes}}}, fh)

    groups, audit = lineage_from_receipts(root, DAY, CALIBRATED)
    assert len(groups) == 1
    assert set(groups[0].members) == {"author", "clone"}
    assert audit["params_version"] == "redteam-v1"


# ---- a third party can recheck a suppression -------------------------------

def _receipt_with(root, suppressed, tmpday=DAY):
    d = os.path.join(root, "receipts")
    os.makedirs(d, exist_ok=True)
    entries, outcomes = [], []
    for i in range(60):
        base = ((i * 37) % 100) / 100.0 - 0.5
        outcomes.append({"episode_id": f"e{i}", "horizon_days": 7,
                         "cost_delta_pct": 1.0})
        for hk, v in (("author", base), ("clone", base + 1e-6),
                      ("independent", -base * 0.7 + 0.31)):
            entries.append({"miner": hk, "episode_id": f"e{i}",
                            "horizon_days": 7,
                            "prediction": {"cost_delta_pct": {"p50": v}}})
    with open(os.path.join(d, f"{tmpday}.json"), "w") as fh:
        json.dump({"document": {"metrics": {"entries": entries,
                                            "outcomes": outcomes},
                                "collapse_audit": {"suppressed": suppressed}}}, fh)
    return root


def _recheck(root, params="0.98,0.95,0.05,0.10"):
    import sys
    sys.path.insert(0, ".")
    from scripts.verify_day import recheck_grouping
    return recheck_grouping(root, str(DAY), params)


def test_a_miner_can_confirm_an_honest_suppression(tmp_path):
    out = _recheck(_receipt_with(str(tmp_path), ["clone"]))
    assert out["ok"] and out["matches"]
    assert out["recomputed_suppressed"] == ["clone"]


def test_an_independent_model_is_not_swept_in(tmp_path):
    out = _recheck(_receipt_with(str(tmp_path), ["clone"]))
    assert "independent" not in out["recomputed_suppressed"]


def test_a_wrongly_suppressed_miner_can_PROVE_it(tmp_path):
    """The case the whole mechanism exists for. If the subnet suppressed a
    hotkey the published parameters do not justify, recomputation disagrees —
    and the disagreement is the miner's evidence."""
    out = _recheck(_receipt_with(str(tmp_path), ["clone", "independent"]))
    assert out["matches"] is False
    assert out["recomputed_suppressed"] == ["clone"]
    assert out["published_suppressed"] == ["clone", "independent"]


def test_recheck_refuses_without_published_parameters(tmp_path):
    """A recomputation with invented numbers proves nothing."""
    out = _recheck(_receipt_with(str(tmp_path), ["clone"]), params=None)
    assert out["ok"] is False
    assert "--params" in out["error"]
