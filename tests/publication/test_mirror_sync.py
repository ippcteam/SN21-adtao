"""mirror_sync — the mirror must carry the same bytes the router serves.

The mirror exists because the feeds' host has no public HTTP; if the
mirrored bodies drift from the router's responses, verify_day passes
against one and fails against the other, which is worse than no mirror.
So these tests publish a real day with the real rail and check the
rendered items against the envelope files and pure functions directly.
"""

import json
from datetime import date

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.publication.daily_accuracy_runner import publish_day
from hope.publication.mirror_sync import build_mirror_items
from hope.publication.receipt_feed import run_daily_receipt
from hope.scoring.daily_score_flow import HorizonResult
from hope.scoring.settle_day_flow import SettledHorizon, score_entry_v2

KEY = Ed25519PrivateKey.generate()
DAY = date(2026, 8, 18)
V2 = {"SN21_SETTLE_SCORING_V2": "true"}


def _pred(p50=0.4, spread=0.3):
    return {m: {"p10": p50 - spread, "p50": p50, "p90": p50 + spread}
            for m in ("cost_delta_pct", "conversions_delta_pct",
                      "efficiency_delta_pct")}


def _publish_real_day(root: str):
    outcomes = [SettledHorizon(f"ep{i}", 7, 0.4, 0.2, -0.1, DAY)
                for i in range(3)]
    index = {o.episode_id: {"alice": {"7": _pred()},
                            "bob": {"7": _pred(0.1)}} for o in outcomes}
    results = []
    for o in outcomes:
        actual = {"cost_delta_pct": o.cost_delta_pct,
                  "conversions_delta_pct": o.conversions_delta_pct,
                  "efficiency_delta_pct": o.efficiency_delta_pct}
        for miner in ("alice", "bob"):
            results.append(HorizonResult(
                o.episode_id, 7, miner,
                score_entry_v2(index[o.episode_id][miner]["7"], actual), DAY))
    rec = run_daily_receipt(root, DAY, outcomes, index, results, {}, KEY,
                            "2026-08-18T00:00:00Z", environ=V2)
    publish_day(root, DAY, results, KEY, "2026-08-18T00:00:00Z",
                receipt_sha256=rec.sha256)
    return rec


def _by_path(items):
    return {i["path"]: i["body"] for i in items}


def test_empty_ledger_still_yields_index_and_root(tmp_path):
    got = _by_path(build_mirror_items(str(tmp_path)))
    assert got["/v1/daily/index"]["count"] == 0
    assert got["/v1/daily/root"]["feed_root"] is None


def test_receipt_and_accuracy_served_verbatim(tmp_path):
    root = str(tmp_path)
    rec = _publish_real_day(root)
    got = _by_path(build_mirror_items(root))

    with open(f"{root}/receipts/{DAY}.json") as fh:
        assert got[f"/v1/daily/{DAY}/receipt"] == json.load(fh)
    with open(f"{root}/accuracy/{DAY}.json") as fh:
        acc = json.load(fh)
    assert got[f"/v1/daily/{DAY}/accuracy"] == acc
    # the anchor linkage the whole design hangs on
    assert (acc["document"]["metrics"]["receipt_sha256"] == rec.sha256)


def test_index_root_and_proof_are_consistent(tmp_path):
    root = str(tmp_path)
    _publish_real_day(root)
    got = _by_path(build_mirror_items(root))

    idx = got["/v1/daily/index"]
    assert idx["count"] == 1
    row = idx["days"][0]
    assert row["day"] == str(DAY) and row["receipt_available"] is True

    proof = got[f"/v1/daily/{DAY}/proof"]
    root_doc = got["/v1/daily/root"]
    assert proof["feed_root"] == root_doc["feed_root"]
    assert root_doc["leaf_count"] == 1
    assert "how_to_verify" in proof
