"""Admission verdicts, published — a miner can see admitted vs skipped.

Two miners asked the same question on the same day (2 Sept): "was my
model ever admitted, or was it skipped?" — and the public record could
not answer it. The verdicts exist (one per judged digest, admitted or
rejected with the gate trace); this module reduces them to their public
form for the mirror at /v1/daily/admission-verdicts.

Nothing here is private: the digest is already public in the miner's own
on-chain commitment, the hotkey is public by construction, and the
rejection detail is the miner's own container's failure text, trimmed at
source. A digest with no record here was never judged — which, per the
intake ordering, means it is still queued, distinct from rejected.
"""

from __future__ import annotations

import json
import os

from hope.backtest.intake_runner import verdict_dir

# The rejection detail is the first line of the miner's own gate trace —
# enough to act on ("exit=1 after the joblib serial-mode warning") without
# publishing a full stderr dump.
DETAIL_CHARS = 200


def build_verdicts_document(ledger_root: str) -> dict:
    """Every judged digest's public verdict. Deterministic order."""
    directory = verdict_dir(ledger_root)
    verdicts = []
    if os.path.isdir(directory):
        for name in sorted(os.listdir(directory)):
            if not name.endswith(".json") or name.startswith("_"):
                continue
            try:
                with open(os.path.join(directory, name)) as f:
                    envelope = json.load(f)
            except (OSError, ValueError):
                continue
            body = (envelope.get("document") or {}).get("metrics") or envelope
            digest = body.get("image_digest") or body.get("digest")
            status = body.get("status")
            hotkey = body.get("hotkey")
            if not digest or not status:
                continue
            rec = {"hotkey": hotkey, "digest": digest, "status": str(status)}
            detail = body.get("detail")
            if detail and str(status) != "admitted":
                rec["detail"] = str(detail)[:DETAIL_CHARS]
            for k in ("gated_at", "judged_at", "timestamp"):
                if body.get(k):
                    rec["judged_at"] = str(body[k])
                    break
            # The gate's own numbers, when the record carries them: the pooled
            # scores the verdict was decided on, the bar, and the same scores
            # per horizon — so "which horizon is weak" is readable here rather
            # than inferred from the receipts weeks later.
            gate = body.get("gate")
            if isinstance(gate, dict) and gate.get("model_gate_score") is not None:
                rec["gate"] = {k: gate.get(k) for k in (
                    "model_gate_score", "baseline_gate_score", "required_gate_score",
                    "margin", "coverage_ok") if gate.get(k) is not None}
                if isinstance(gate.get("by_horizon"), dict) and gate["by_horizon"]:
                    rec["gate"]["by_horizon"] = gate["by_horizon"]
            corpus = body.get("corpus")
            if isinstance(corpus, dict) and corpus.get("source"):
                rec["corpus"] = {k: corpus.get(k) for k in (
                    "source", "key", "sha256", "cutoff", "episodes", "outcome_rows")
                    if corpus.get(k) is not None}
            verdicts.append(rec)
    return {
        "feed": "sn21-admission-verdicts",
        "note": ("one record per judged image digest. `admitted` models "
                 "run on every daily basket; `rejected_gate` carries the "
                 "first line of the container's own failure. A committed "
                 "digest with no record here has not been judged yet — "
                 "still queued, not rejected. `gate` carries the pooled "
                 "scores the verdict was decided on and the same scores per "
                 "horizon, for records that kept them. `corpus` names the "
                 "episode set the verdict was judged on: `held-out` is the "
                 "operator's unpublished corpus (key, sha256 of the document, "
                 "cutoff after the published bundle's last window); "
                 "`public-bundle` is the published training bundle, the source "
                 "used before a held-out corpus was served. Records without "
                 "`corpus` predate the field and were judged on the bundle."),
        "verdicts": verdicts,
        "total": len(verdicts),
    }
