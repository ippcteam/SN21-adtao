"""Daily scoring RECEIPT — the document that makes a miner's score reproducible.

Governance ruling, 2026-08-05: miners must be able to verify (1) what they submitted is what
was scored, (2) the settled-outcome set was fixed before scoring and the same
for everyone, (3) they were scored on real data, (4) they can rerun and
reproduce the score.

Property (1) is the digest-commitment intake and already holds. This feed is
properties (2)–(4): for each day, ONE attested, hash-chained document carrying
everything a rerun needs —

    outcomes   every settled (episode, horizon) actual used that day, with the
               efficiency basis that was applied
    entries    every (episode, horizon, miner): the miner's own prediction
               verbatim, the four score components, and the final score
    formula    which formula version scored it and its published weights, so
               the verifier applies what actually ran rather than guessing

The receipt does not get its own on-chain anchor. The existing daily accuracy
document embeds `receipt_sha256`, and ITS sha256 is what the chain anchors —
so one anchor covers both: chain → accuracy doc → receipt_sha256 → receipt.
A tampered receipt breaks the middle link; a tampered accuracy doc breaks the
anchor. No new chain machinery, no second commitment slot.

Same rail as the accuracy feed: canonical-JSON hashing, ed25519 attestation,
per-feed hash chain, append-only storage. A day once published is never
rewritten; republishing raises rather than forks.

PRIVACY: outcomes are keyed by episode_id (a hash) and expose only the delta
fractions — the same shape already published per weekly epoch under
data/outcomes/. No account names, no customer ids, no spend levels.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from hope.publication.rail import attest, build_document
from hope.scoring.daily_score_flow import HorizonResult
from hope.scoring.settle_day_flow import (
    W_COVERAGE,
    W_DIRECTION,
    W_GOAL,
    W_QUANTILE,
    W_TOTAL,
    SettledHorizon,
    settle_scoring_v2_enabled,
)

RECEIPT_FEED_NAME = "daily_receipt"


def receipt_dir(root: str) -> str:
    return os.path.join(root, "receipts")


def _head_path(root: str) -> str:
    return os.path.join(receipt_dir(root), "_head.json")


def receipt_path(root: str, day: date | str) -> str:
    return os.path.join(receipt_dir(root), f"{day}.json")


def build_receipt_metrics(
    outcomes: Iterable[SettledHorizon],
    prediction_index: dict,
    results: Iterable[HorizonResult],
    components: dict,
    environ=None,
    censored: dict | None = None,
    transition_map: dict | None = None,
) -> dict:
    """The receipt payload. Deterministic: every list sorted on a total key,
    every float already rounded upstream — two validators building from the
    same inputs must byte-match, or the sha256 means nothing.

    `entries` carries the miner's prediction VERBATIM (the trio dict exactly
    as the shadow ledger recorded it). Reproduction must not depend on the
    validator's paraphrase of what the miner said — that would verify our
    copy, not their submission.
    """
    out_rows = []
    for o in sorted(outcomes, key=lambda o: (str(o.episode_id), o.horizon_days)):
        out_rows.append({
            "episode_id": str(o.episode_id),
            "horizon_days": int(o.horizon_days),
            "cost_delta_pct": o.cost_delta_pct,
            "conversions_delta_pct": o.conversions_delta_pct,
            "efficiency_delta_pct": o.efficiency_delta_pct,
            "efficiency_basis": getattr(o, "efficiency_basis", None),
            "finalized_on": str(o.finalized_on),
        })

    score_by_key = {(str(r.episode_id), int(r.horizon_days), r.miner): r
                    for r in results}
    entries = []
    for (eid, h, miner), r in sorted(score_by_key.items()):
        comp = components.get((eid, h, miner)) or components.get(
            (r.episode_id, r.horizon_days, miner))
        pred = ((prediction_index.get(eid) or {}).get(miner) or {}).get(str(h))
        entries.append({
            "episode_id": eid,
            "horizon_days": h,
            "miner": miner,
            "prediction": pred,           # verbatim; None is loud, not hidden
            "components": (None if comp is None else {
                "quantile": comp[0], "direction": comp[1],
                "coverage": comp[2], "goal": comp[3],
            }),
            "score": r.score,
            "finalized_on": str(r.finalized_on),
            # Which change type this entry scored (Rob, 21 Aug: miners must
            # see WHERE they win and lose, and the receipt is the surface
            # they already trust). Only present when the builder was given a
            # map — the keys come from the shadow-store payloads, the same
            # distributed data predictions come from, so validators sharing
            # the store still byte-match. UNKNOWN = payload had no key.
            **({"transition_key": transition_map.get(eid, "UNKNOWN")}
               if transition_map is not None else {}),
        })

    # The formula version must be read from the SAME environment the settle
    # step ran under — not os.environ. The rehearsal's first execution of the
    # miner rerun caught exactly this: scoring ran v2 off the injected flag
    # set, the receipt recorded v1 off the process env, and all 2,700
    # reproductions diverged. A receipt that records what the process env
    # says rather than what RAN is worse than no receipt.
    v2 = (settle_scoring_v2_enabled(environ) if environ is not None
          else settle_scoring_v2_enabled())
    return {
        "feed": RECEIPT_FEED_NAME,
        "formula": {
            "version": "v2" if v2 else "v1",
            # Published so the verifier applies what RAN, not what it guesses.
            "weights": ({"quantile": W_QUANTILE, "coverage": W_COVERAGE,
                         "direction": W_DIRECTION, "goal": W_GOAL,
                         "normaliser": W_TOTAL} if v2 else
                        {"quantile": 0.5, "direction": 0.5}),
        },
        "outcomes": out_rows,
        "entries": entries,
        "outcomes_total": len(out_rows),
        "entries_total": len(entries),
        "miners": len({e["miner"] for e in entries}),
        # Censored horizons are EXCLUDED from `outcomes` by the provider, so
        # without this they would vanish silently and a miner counting
        # episodes would find fewer than the basket held with no explanation.
        # the operator's attrition ruling says dropped horizons are recorded, never a
        # zero — recording them only in our database and not in the document
        # miners actually read would honour the letter and miss the point.
        # {} = nothing censored; None = this validator could not read the
        # censor state, which is a different statement and says so.
        "censored": (censored if censored is not None else {}),
        # Optional operator disclosure carried INTO the signed receipt when set,
        # so any note travels inside the signed document rather than as a
        # side-channel claim. Only present when SN21_RECEIPT_DISCLOSURE is set,
        # so normal receipts are unchanged and byte-identical across validators.
        **({"disclosure": _disclosure}
           if (_disclosure := ((environ or os.environ).get(
               "SN21_RECEIPT_DISCLOSURE") or "").strip()) else {}),
    }


@dataclass(frozen=True)
class ReceiptPublish:
    published: bool
    day: str
    sha256: str | None = None
    path: str | None = None
    skipped_reason: str | None = None


def _load_head(root: str) -> dict | None:
    p = _head_path(root)
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def run_daily_receipt(
    root: str,
    day: date,
    outcomes: Iterable[SettledHorizon],
    prediction_index: dict,
    results: list[HorizonResult],
    components: dict,
    signing_key: Ed25519PrivateKey,
    generated_at: str,
    environ=None,
    censored: dict | None = None,
    transition_map: dict | None = None,
) -> ReceiptPublish:
    """Publish the day's receipt. Append-only; a republished day raises.

    No results -> no receipt, and that is correct rather than lazy: a receipt
    exists to reproduce scores, and a day that scored nothing has nothing to
    reproduce. The accuracy feed's zero-day document already proves the gap
    was honest; carrying receipt_sha256=None there says "no receipt" in the
    anchored record itself.
    """
    day_s = str(day)
    if not results:
        return ReceiptPublish(False, day_s, skipped_reason="no scored results")

    path = receipt_path(root, day_s)
    if os.path.exists(path):
        raise FileExistsError(
            f"receipt for {day_s} already published at {path} — a day once "
            f"published is never rewritten (append-only rail)")

    head = _load_head(root)
    metrics = build_receipt_metrics(outcomes, prediction_index, results,
                                    components, environ=environ,
                                    censored=censored,
                                    transition_map=transition_map)
    doc = build_document(RECEIPT_FEED_NAME, day_s, metrics, generated_at,
                         prev_sha256=(head or {}).get("sha256"))
    att = attest(doc, signing_key)

    os.makedirs(receipt_dir(root), exist_ok=True)
    with open(path, "w") as f:
        json.dump({"document": att.document, "sha256": att.sha256,
                   "signature_hex": att.signature_hex,
                   "public_key_hex": att.public_key_hex}, f, indent=1,
                  default=str)
    with open(_head_path(root), "w") as f:
        json.dump({"day": day_s, "sha256": att.sha256}, f)
    return ReceiptPublish(True, day_s, sha256=att.sha256, path=path)
