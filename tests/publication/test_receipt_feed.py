"""The daily receipt and its verifier — Rob's rerun property, adversarially.

A verifier that passes on honest data proves nothing; these tests tamper with
each link and assert the right CHECK fails, so a failure is attributable.
"""

import json
import os
import sys

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from datetime import date

from hope.publication.daily_accuracy_runner import publish_day
from hope.publication.receipt_feed import (
    build_receipt_metrics,
    receipt_path,
    run_daily_receipt,
)
from hope.scoring.daily_score_flow import HorizonResult
from hope.scoring.settle_day_flow import SettledHorizon, score_entry_v2

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "scripts"))
from verify_day import verify_day  # noqa: E402

KEY = Ed25519PrivateKey.generate()
DAY = date(2026, 8, 18)
V2 = {"SN21_SETTLE_SCORING_V2": "true"}


def _pred(p50=0.4, spread=0.3):
    return {m: {"p10": p50 - spread, "p50": p50, "p90": p50 + spread}
            for m in ("cost_delta_pct", "conversions_delta_pct",
                      "efficiency_delta_pct")}


def _world(n_eps=4):
    outcomes = [SettledHorizon(f"ep{i}", 7, 0.4, 0.2, -0.1, DAY)
                for i in range(n_eps)]
    index = {f"ep{i}": {"alice": {"7": _pred()}, "bob": {"7": _pred(0.1)}}
             for i in range(n_eps)}
    results = []
    for o in outcomes:
        actual = {"cost_delta_pct": o.cost_delta_pct,
                  "conversions_delta_pct": o.conversions_delta_pct,
                  "efficiency_delta_pct": o.efficiency_delta_pct}
        for miner in ("alice", "bob"):
            pred = index[o.episode_id][miner]["7"]
            results.append(HorizonResult(o.episode_id, 7, miner,
                                         score_entry_v2(pred, actual), DAY))
    return outcomes, index, results, {}


def _publish(tmp_path):
    root = str(tmp_path)
    outcomes, index, results, comps = _world()
    rec = run_daily_receipt(root, DAY, outcomes, index, results, comps, KEY,
                            "2026-08-18T00:00:00Z", environ=V2)
    publish_day(root, DAY, results, KEY, "2026-08-18T00:00:00Z",
                receipt_sha256=rec.sha256)
    return root, rec


def test_honest_day_verifies_end_to_end(tmp_path):
    root, _ = _publish(tmp_path)
    v = verify_day(root, str(DAY))
    assert v["ok"], v
    assert v["checks"]["score_reproduction"]["entries_recomputed"] == 8


def test_tampered_score_fails_reproduction_and_names_the_entry(tmp_path):
    """THE POINT OF THE WHOLE FEED. A validator that inflates one score must
    be caught by the rerun, with the exact entry named."""
    root, _ = _publish(tmp_path)
    p = receipt_path(root, str(DAY))
    env = json.load(open(p))
    env["document"]["metrics"]["entries"][0]["score"] = 0.999999
    json.dump(env, open(p, "w"))
    v = verify_day(root, str(DAY))
    assert not v["ok"]
    # attestation breaks too (the doc changed under its sha) — but the
    # reproduction check must ALSO fail and name the entry: a miner needs
    # "which score is wrong", not just "something changed".
    assert v["checks"]["attestation"]["verdict"] == "FAIL"
    assert v["checks"]["score_reproduction"]["verdict"] == "FAIL"
    assert v["diffs"][0]["published"] == 0.999999


def test_tampered_outcome_fails_reproduction(tmp_path):
    """Scoring against different data than published (property 3): change one
    published actual and the scores on that episode stop reproducing."""
    root, _ = _publish(tmp_path)
    p = receipt_path(root, str(DAY))
    env = json.load(open(p))
    env["document"]["metrics"]["outcomes"][0]["cost_delta_pct"] = 5.0
    json.dump(env, open(p, "w"))
    v = verify_day(root, str(DAY))
    assert v["checks"]["score_reproduction"]["verdict"] == "FAIL"


def test_swapped_receipt_breaks_the_anchor_linkage(tmp_path):
    """Property 2: the receipt reachable from the anchor IS the dataset. A
    wholesale swap (re-signed, self-consistent) still fails, because the
    anchored accuracy document names the original sha."""
    root, _ = _publish(tmp_path)
    outcomes, index, results, comps = _world(n_eps=3)   # a different world
    os.unlink(receipt_path(root, str(DAY)))
    os.unlink(os.path.join(root, "receipts", "_head.json"))
    run_daily_receipt(root, DAY, outcomes, index, results, comps, KEY,
                      "2026-08-18T00:00:00Z", environ=V2)
    v = verify_day(root, str(DAY))
    assert v["checks"]["attestation"]["verdict"] == "PASS"      # self-consistent…
    assert v["checks"]["anchor_linkage"]["verdict"] == "FAIL"   # …but not anchored
    assert not v["ok"]


def test_expect_anchor_closes_the_loop_to_chain(tmp_path):
    """--expect-anchor is the FEED ROOT now, not the day's own hash: one
    commitment slot per hotkey means we anchor a rolling root over every
    published day, so the newest commitment still covers old days."""
    from hope.publication.feed_root import feed_root
    root, _ = _publish(tmp_path)
    assert verify_day(root, str(DAY), expect_anchor=feed_root(root))["ok"]
    v = verify_day(root, str(DAY), expect_anchor="ab" * 32)
    assert v["checks"]["feed_root"]["verdict"] == "FAIL"
    assert v["checks"]["feed_root"]["chain_root_matches"] is False


def test_no_results_no_receipt(tmp_path):
    rec = run_daily_receipt(str(tmp_path), DAY, [], {}, [], {}, KEY,
                            "2026-08-18T00:00:00Z", environ=V2)
    assert not rec.published and rec.sha256 is None


def test_receipt_is_append_only(tmp_path):
    root, _ = _publish(tmp_path)
    outcomes, index, results, comps = _world()
    with pytest.raises(FileExistsError):
        run_daily_receipt(root, DAY, outcomes, index, results, comps, KEY,
                          "2026-08-18T00:00:00Z", environ=V2)


def test_receipt_is_deterministic(tmp_path):
    """Two validators building from the same inputs must byte-match, or the
    sha means nothing."""
    outcomes, index, results, comps = _world()
    a = build_receipt_metrics(outcomes, index, results, comps, environ=V2)
    b = build_receipt_metrics(list(reversed(outcomes)),
                              dict(reversed(list(index.items()))),
                              list(reversed(results)), comps, environ=V2)
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def test_formula_version_comes_from_the_injected_environ(tmp_path):
    """The bug the rehearsal caught on first run: scoring ran v2 off injected
    flags while the receipt recorded v1 off os.environ, and all 2,700
    reproductions diverged. The receipt must record what RAN."""
    outcomes, index, results, comps = _world()
    assert build_receipt_metrics(outcomes, index, results, comps,
                                 environ=V2)["formula"]["version"] == "v2"
    assert build_receipt_metrics(outcomes, index, results, comps,
                                 environ={})["formula"]["version"] == "v1"


# ---- censored horizons must be VISIBLE in the document miners read ----------

def test_receipt_states_censored_counts(tmp_path):
    """Censored horizons are excluded from `outcomes` by the provider. Without
    this field a miner counting episodes finds fewer than the basket held and
    has no way to learn why. Rob's attrition ruling says dropped horizons are
    recorded — recording them only in our database misses the point."""
    outcomes, index, results, comps = _world()
    m = build_receipt_metrics(outcomes, index, results, comps, environ=V2,
                              censored={"left_system": 12, "spend_inactive": 3})
    assert m["censored"] == {"left_system": 12, "spend_inactive": 3}


def test_no_censoring_is_an_empty_dict_not_a_missing_key(tmp_path):
    """{} means 'nothing censored'. The key must always be present, or a
    reader cannot tell 'none' from 'this validator did not say'."""
    outcomes, index, results, comps = _world()
    m = build_receipt_metrics(outcomes, index, results, comps, environ=V2)
    assert m["censored"] == {}
    assert "censored" in m
