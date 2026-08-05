"""Rerun and reproduce a day's scores from the published receipt.

the operator's four properties (2026-08-05), and which check answers each:

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

Usage (miner — the normal case):
    python scripts/verify_day.py --url https://validator.adtao.io \
        --day 2026-08-18 [--miner <hotkey>] [--expect-anchor <64hex>]

Usage (operator, reading the ledger directly):
    python scripts/verify_day.py --root <ledger_root> --day 2026-08-18

--url and --root are the SAME code path with a different loader. That is
deliberate: an operator-only verifier proves an operator-only property, and
the whole point is that a miner reaches the same verdict we do.

--expect-anchor is the value the miner read from chain themselves: the
validator's committed FEED ROOT (not any single day's hash — one commitment
slot per hotkey means we anchor a rolling root over every published day, so
the newest commitment still covers old days). Supplying it closes the loop to
the chain; omitting it verifies everything below the anchor, including that
this day is a leaf of the root this server serves.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

# Path set up at module scope, not inside verify_day: _Source needs the same
# helpers and a class body cannot wait for a function to fix sys.path.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                os.pardir))

from hope.publication.receipt_feed import receipt_dir, receipt_path  # noqa: E402

PASS, FAIL = "PASS", "FAIL"


class _Source:
    """Where documents come from. Two loaders, one verification path.

    The URL loader fetches the SAME attested envelopes the ledger holds, so
    every check below — signature, chain, anchor linkage, reproduction —
    operates on bytes the miner received over the wire rather than on
    anything this script derived locally.
    """

    def __init__(self, root=None, url=None):
        if bool(root) == bool(url):
            raise SystemExit("pass exactly one of --root or --url")
        self.root, self.url = root, (url.rstrip("/") if url else None)

    def _get_json(self, path):
        import urllib.error
        import urllib.request
        try:
            with urllib.request.urlopen(path, timeout=30) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                try:
                    return {"_absent": json.load(e)}
                except Exception:
                    return {"_absent": {"reason": "not_found"}}
            raise

    def receipt(self, day):
        if self.root:
            p = receipt_path(self.root, day)
            return _load(p) if os.path.exists(p) else None
        got = self._get_json(f"{self.url}/v1/daily/{day}/receipt")
        return None if "_absent" in got else got

    def accuracy(self, day):
        if self.root:
            p = os.path.join(self.root, "accuracy", f"{day}.json")
            return _load(p) if os.path.exists(p) else None
        got = self._get_json(f"{self.url}/v1/daily/{day}/accuracy")
        return None if "_absent" in got else got

    def proof(self, day):
        """The day's inclusion proof in the rolling feed root."""
        if self.root:
            from hope.publication.feed_root import day_proof
            return day_proof(self.root, day)
        got = self._get_json(f"{self.url}/v1/daily/{day}/proof")
        return None if "_absent" in got else got

    def prior_receipt_sha(self, day):
        """The sha of the receipt immediately before `day`, for the chain check."""
        if self.root:
            d = receipt_dir(self.root)
            prior = sorted(f for f in os.listdir(d)
                           if f.endswith(".json") and not f.startswith("_")
                           and f[:-5] < day)
            return _load(os.path.join(d, prior[-1]))["sha256"] if prior else None
        idx = self._get_json(f"{self.url}/v1/daily/index")
        rows = [r for r in idx.get("days", []) if r.get("day", "") < day
                and r.get("receipt_sha256")]
        return rows[-1]["receipt_sha256"] if rows else None


def _load(path):
    with open(path) as f:
        return json.load(f)


def verify_day(root: str | None = None, day: str = "", miner: str | None = None,
               expect_anchor: str | None = None, url: str | None = None) -> dict:
    from hope.publication.rail import AttestedDocument, document_sha256, verify
    from hope.scoring.settle_day_flow import (
        W_COVERAGE, W_DIRECTION, W_GOAL, W_QUANTILE, W_TOTAL,
        entry_components, entry_components_v2,
    )

    checks: dict[str, dict] = {}
    diffs: list[dict] = []
    src = _Source(root=root, url=url)

    # ---- load the receipt ----------------------------------------------------
    env = src.receipt(day)
    if env is None:
        return {"day": day, "ok": False,
                "fatal": f"no receipt available for {day} "
                         f"({'url ' + url if url else 'root ' + str(root)})"}
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
        found = src.prior_receipt_sha(day)
        if found is None:
            checks["chain"] = {"verdict": FAIL,
                               "note": "prev_sha256 is set but no earlier "
                                       "receipt is available to check against"}
        else:
            checks["chain"] = {"verdict": PASS if found == prev else FAIL,
                               "expected": prev, "found": found}

    # ---- 3. anchor linkage: accuracy doc names this receipt -------------------
    aenv = src.accuracy(day)
    if aenv is None:
        checks["anchor_linkage"] = {"verdict": FAIL,
                                    "note": "no accuracy document available "
                                            "for this day"}
    else:
        adoc = aenv["document"]
        linked = adoc["metrics"].get("receipt_sha256") == env["sha256"]
        a_sha_ok = document_sha256(adoc) == aenv["sha256"]
        res = {"verdict": PASS if (linked and a_sha_ok) else FAIL,
               "accuracy_doc_names_this_receipt": linked,
               "accuracy_sha256_matches": a_sha_ok,
               "anchored_sha256": aenv["sha256"]}
        checks["anchor_linkage"] = res

    # ---- 3b. feed root: is this day inside what the chain anchors? ------------
    # The top link. Commitments::CommitmentOf holds ONE entry per hotkey and
    # every commit overwrites the last, so the validator anchors a ROLLING ROOT
    # over every published day rather than each day's own hash. Without this
    # check a miner can prove the receipt is internally consistent and still
    # not know it is the history the chain committed to.
    proof_doc = src.proof(day)
    if proof_doc is None:
        checks["feed_root"] = {
            "verdict": FAIL,
            "note": "no inclusion proof available for this day — it is not a "
                    "leaf in the published feed",
        }
    else:
        from hope.publication.merkle import verify_proof
        acc_sha = (aenv or {}).get("sha256")
        claimed = proof_doc.get("document_sha256")
        in_root = verify_proof(claimed, proof_doc.get("proof") or [],
                               proof_doc.get("feed_root"))
        # the proof must be ABOUT this day's accuracy document, not merely a
        # valid proof of some other leaf
        same_doc = (acc_sha is not None and claimed == acc_sha)
        res = {"verdict": PASS if (in_root and same_doc) else FAIL,
               "leaf_in_root": in_root,
               "proof_is_for_this_day": same_doc,
               "feed_root": proof_doc.get("feed_root"),
               "leaf_index": proof_doc.get("leaf_index"),
               "leaf_count": proof_doc.get("leaf_count")}
        if expect_anchor:
            # expect_anchor is now the ROOT read from chain, not the day's hash
            res["chain_root_matches"] = (
                expect_anchor.lower() == str(proof_doc.get("feed_root")).lower())
            if not res["chain_root_matches"]:
                res["verdict"] = FAIL
                res["note"] = ("the root you read from chain is not the root "
                               "this server served — it is serving a different "
                               "history than it anchored")
        checks["feed_root"] = res

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
    ap.add_argument("--root", default=None,
                    help="ledger root (operator, local filesystem)")
    ap.add_argument("--url", default=None,
                    help="validator base URL (miner) e.g. https://validator.adtao.io")
    ap.add_argument("--day", required=True)
    ap.add_argument("--miner", default=None)
    ap.add_argument("--expect-anchor", default=None,
                    help="the FEED ROOT you read from chain yourself "
                         "(validator hotkey's committed sha256)")
    a = ap.parse_args()
    out = verify_day(a.root, a.day, a.miner, a.expect_anchor, url=a.url)
    print(json.dumps(out, indent=1, default=str))
    return 0 if out.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
