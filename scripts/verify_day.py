"""Rerun and reproduce a day's scores from the published receipt.

Rob's four properties (2026-08-05), and which check answers each:

  1. "What was submitted is what was scored"
         The receipt carries every prediction VERBATIM. A miner diffs the
         `prediction` field against what their own container emitted. (The
         image-level half is the digest commitment at intake — chain-checked,
         not this script's job.)
  2. "The dataset of settled outcomes was fixed prior to scoring and same
      for all"
         One receipt per day, hash-chained to the previous day, attested,
         and reachable from the ON-CHAIN anchor via the accuracy document's
         receipt_sha256. Every miner's entry in the same document scores
         against the same `outcomes` array — there is no per-miner dataset
         to vary.
  3. "They were scored on the real data"
         The receipt's outcomes are the settled actuals the scorer consumed;
         this script recomputes every score FROM them. If the published
         outcomes were not the scoring inputs, the recomputation diverges.
  4. "They can rerun and reproduce the score"
         This script IS the rerun: recompute every (episode, horizon, miner)
         score with the receipt's own recorded formula and diff against the
         published score. Any mismatch exits non-zero with a structured diff.

Verdicts are per check, not one bit: signature, hash chain, anchor linkage,
and score reproduction each pass or fail independently, so a failure is
attributable rather than just alarming.

Usage:
    python scripts/verify_day.py --root <ledger_root> --day 2026-08-18 \
        [--miner <hotkey>] [--expect-anchor <64hex>]

--expect-anchor is the value the miner read from chain themselves (the
accuracy document's anchored sha256). Supplying it closes the loop to the
chain; omitting it still verifies everything below the anchor.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

PASS, FAIL = "PASS", "FAIL"


def _load(path):
    with open(path) as f:
        return json.load(f)


def verify_day(root: str, day: str, miner: str | None = None,
               expect_anchor: str | None = None) -> dict:
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))
    from hope.publication.rail import AttestedDocument, document_sha256, verify
    from hope.publication.receipt_feed import receipt_path, receipt_dir
    from hope.scoring.settle_day_flow import (
        W_COVERAGE, W_DIRECTION, W_GOAL, W_QUANTILE, W_TOTAL,
        entry_components, entry_components_v2,
    )

    checks: dict[str, dict] = {}
    diffs: list[dict] = []

    # ---- load the receipt ----------------------------------------------------
    rpath = receipt_path(root, day)
    if not os.path.exists(rpath):
        return {"day": day, "fatal": f"no receipt at {rpath}", "ok": False}
    env = _load(rpath)
    doc = env["document"]
    metrics = doc["metrics"]

    # ---- 1. attestation: the sha is the document, the signature is the sha ---
    sha_ok = document_sha256(doc) == env["sha256"]
    att = AttestedDocument(document=doc, sha256=env["sha256"],
                           signature_hex=env["signature_hex"],
                           public_key_hex=env["public_key_hex"])
    sig_ok = verify(att)
    checks["attestation"] = {
        "verdict": PASS if (sha_ok and sig_ok) else FAIL,
        "sha256_matches_document": sha_ok, "signature_valid": sig_ok,
        "public_key": env["public_key_hex"],
    }

    # ---- 2. hash chain to the previous receipt --------------------------------
    prev = doc.get("prev_sha256")
    if prev is None:
        checks["chain"] = {"verdict": PASS, "note": "first receipt in the feed"}
    else:
        prior = sorted(d for d in os.listdir(receipt_dir(root))
                       if d.endswith(".json") and not d.startswith("_")
                       and d[:-5] < day)
        if not prior:
            checks["chain"] = {"verdict": FAIL,
                               "note": "prev_sha256 set but no earlier receipt "
                                       "on disk to check against"}
        else:
            prev_env = _load(os.path.join(receipt_dir(root), prior[-1]))
            ok = prev_env["sha256"] == prev
            checks["chain"] = {"verdict": PASS if ok else FAIL,
                               "prev_receipt": prior[-1],
                               "expected": prev, "found": prev_env["sha256"]}

    # ---- 3. anchor linkage: accuracy doc names this receipt -------------------
    apath = os.path.join(root, "accuracy", f"{day}.json")
    if not os.path.exists(apath):
        checks["anchor_linkage"] = {"verdict": FAIL,
                                    "note": f"no accuracy document at {apath}"}
    else:
        aenv = _load(apath)
        adoc = aenv["document"]
        linked = adoc["metrics"].get("receipt_sha256") == env["sha256"]
        a_sha_ok = document_sha256(adoc) == aenv["sha256"]
        res = {"verdict": PASS if (linked and a_sha_ok) else FAIL,
               "accuracy_doc_names_this_receipt": linked,
               "accuracy_sha256_matches": a_sha_ok,
               "anchored_sha256": aenv["sha256"]}
        if expect_anchor:
            res["chain_anchor_matches"] = (expect_anchor.lower() == aenv["sha256"].lower())
            if not res["chain_anchor_matches"]:
                res["verdict"] = FAIL
        checks["anchor_linkage"] = res

    # ---- 4. the rerun: recompute every score from the receipt itself ----------
    actual_by_key = {(o["episode_id"], o["horizon_days"]): {
        "cost_delta_pct": o["cost_delta_pct"],
        "conversions_delta_pct": o["conversions_delta_pct"],
        "efficiency_delta_pct": o["efficiency_delta_pct"],
    } for o in metrics["outcomes"]}

    formula = metrics["formula"]
    recomputed = skipped = 0
    for e in metrics["entries"]:
        if miner and e["miner"] != miner:
            continue
        if e["prediction"] is None:
            skipped += 1
            continue
        actual = actual_by_key.get((e["episode_id"], e["horizon_days"]))
        if actual is None:
            diffs.append({"entry": e, "problem": "outcome missing from receipt"})
            continue
        if formula["version"] == "v2":
            c = entry_components_v2(e["prediction"], actual)
            w = formula["weights"]
            score = (w["quantile"] * c["quantile"] + w["coverage"] * c["coverage"]
                     + w["direction"] * c["direction"] + w["goal"] * c["goal"]
                     ) / w["normaliser"]
        else:
            q, d = entry_components(e["prediction"], actual)
            w = formula["weights"]
            score = w["quantile"] * q + w["direction"] * d
        score = round(max(0.0, min(1.0, score)), 6)
        recomputed += 1
        if abs(score - e["score"]) > 1e-6:
            diffs.append({"episode_id": e["episode_id"],
                          "horizon_days": e["horizon_days"],
                          "miner": e["miner"],
                          "published": e["score"], "recomputed": score})

    checks["score_reproduction"] = {
        "verdict": PASS if not diffs else FAIL,
        "entries_recomputed": recomputed,
        "entries_without_prediction": skipped,
        "mismatches": len(diffs),
        "formula": formula["version"],
    }

    ok = all(c["verdict"] == PASS for c in checks.values())
    return {"day": day, "ok": ok, "checks": checks,
            "diffs": diffs[:50],
            "miner_filter": miner}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--day", required=True)
    ap.add_argument("--miner", default=None)
    ap.add_argument("--expect-anchor", default=None)
    a = ap.parse_args()
    out = verify_day(a.root, a.day, a.miner, a.expect_anchor)
    print(json.dumps(out, indent=1, default=str))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
