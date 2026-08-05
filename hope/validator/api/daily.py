"""Daily verifiability endpoints — the distribution half of Rob's four properties.

The receipt, the accuracy document and the hash-chain index have existed since
2026-08-05 but lived only in the validator's ledger root: `verify_day --root`
worked on OUR host and nowhere else. A receipt nobody can fetch verifies
nothing, so this router IS the feature — everything served here is already
attested and already anchored; none of it is privileged.

Three routes:

    GET /v1/daily/{day}/receipt    the full scoring record for that day
    GET /v1/daily/{day}/accuracy   the anchored aggregate document
    GET /v1/daily/index            the chain walk: [{day, sha256, prev}]

WHY 404s CARRY A REASON. "No receipt for 2026-08-10" has at least four
innocent causes and one alarming one, and a bare 404 makes a miner assume the
worst. The body distinguishes: not_yet_published (settle has not run),
pre_maturity (nothing has settled at all yet — before ~18 Aug that is every
day), no_scored_results (a real day that scored nothing, e.g. every horizon
censored), and out_of_range (a date the feed never covered).

PATH SAFETY. `day` arrives from a public URL and reaches a filesystem path.
It is validated as a strict ISO calendar date and then RE-SERIALISED from the
parsed date object, so the string that touches the filesystem is one this
module constructed — not one a caller supplied. `..%2f` and friends fail the
parse; even a date-shaped string with padding cannot survive the round-trip.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date

from fastapi import APIRouter, HTTPException, Request

logger = logging.getLogger(__name__)
router = APIRouter()

# Where the daily loop writes its feeds. Same value the loop is invoked with;
# unset means this validator does not serve daily artifacts (the endpoints say
# so explicitly rather than 500-ing on a missing directory).
LEDGER_ROOT_ENV = "SN21_LEDGER_ROOT"


def _ledger_root(request: Request) -> str:
    root = (request.app.state.validator.get("ledger_root")
            or os.environ.get(LEDGER_ROOT_ENV) or "").strip()
    if not root:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "daily artifacts are not served by this validator",
                "reason": "ledger_root_unset",
                "fix": f"set {LEDGER_ROOT_ENV} on the API process to the same "
                       f"ledger root the daily loop writes to",
            },
        )
    return root


def _safe_day(day: str) -> str:
    """Strict ISO date -> a string THIS module built. See PATH SAFETY above."""
    try:
        parsed = date.fromisoformat(day)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=400,
            detail={"error": "day must be an ISO calendar date (YYYY-MM-DD)",
                    "received": day[:32]},
        )
    return parsed.isoformat()


def _read_envelope(path: str):
    with open(path) as f:
        return json.load(f)


def _absence_reason(root: str, feed_dir: str, day: str) -> dict:
    """Why a day is missing — see the module docstring. Never a bare 404."""
    d = os.path.join(root, feed_dir)
    if not os.path.isdir(d):
        return {"reason": "pre_maturity",
                "detail": "this feed has never published; nothing has settled yet"}
    published = sorted(f[:-5] for f in os.listdir(d)
                       if f.endswith(".json") and not f.startswith("_"))
    if not published:
        return {"reason": "pre_maturity",
                "detail": "this feed has never published; nothing has settled yet"}
    if day < published[0] or day > published[-1]:
        return {"reason": "out_of_range",
                "detail": f"feed covers {published[0]} to {published[-1]}"}
    # Inside the covered range but absent: the day ran and produced no scored
    # results (every horizon censored, or a genuinely empty day). The accuracy
    # feed publishes a zero-day document for exactly this case, so if THAT is
    # present and the receipt is not, the distinction is real and worth stating.
    return {"reason": "no_scored_results",
            "detail": "the day was processed but scored nothing to reproduce; "
                      "the accuracy document for that day states the gap"}


@router.get("/{day}/receipt")
async def get_receipt(day: str, request: Request):
    """The day's full scoring record: outcomes used, per-miner predictions
    verbatim, score components, final scores, and the formula that ran.

    This is what `scripts/verify_day.py --url` consumes. Served verbatim —
    the response IS the attested document, so a miner can verify the signature
    against the bytes they received without trusting this endpoint.
    """
    root, safe = _ledger_root(request), _safe_day(day)
    path = os.path.join(root, "receipts", f"{safe}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404,
                            detail={"day": safe, "feed": "receipt",
                                    **_absence_reason(root, "receipts", safe)})
    return _read_envelope(path)


@router.get("/{day}/accuracy")
async def get_accuracy(day: str, request: Request):
    """The day's anchored aggregate document. Its sha256 is what goes on
    chain, and its `metrics.receipt_sha256` names the receipt above — so this
    is the link between the chain anchor and the full record."""
    root, safe = _ledger_root(request), _safe_day(day)
    path = os.path.join(root, "accuracy", f"{safe}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404,
                            detail={"day": safe, "feed": "accuracy",
                                    **_absence_reason(root, "accuracy", safe)})
    return _read_envelope(path)


@router.get("/{day}/miner/{hotkey}")
async def get_miner_day(day: str, hotkey: str, request: Request):
    """One miner's entries for a day: predictions, components, scores.

    A convenience FILTER over the public receipt, not a private view — the
    receipt already contains every miner's entries verbatim, so gating this
    would be security theatre while the same bytes sit one endpoint away.
    Named `/miner/{hotkey}` rather than `/my-predictions` for exactly that
    reason: the old name implied a privacy that never existed here.

    This is the surface a miner hits when told "check your score", and it
    returns the receipt's own sha256 so they can verify the whole document
    with scripts/verify_day.py rather than trusting this summary.
    """
    root, safe = _ledger_root(request), _safe_day(day)
    path = os.path.join(root, "receipts", f"{safe}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404,
                            detail={"day": safe, "feed": "receipt",
                                    **_absence_reason(root, "receipts", safe)})
    env = _read_envelope(path)
    metrics = env.get("document", {}).get("metrics", {})
    mine = [e for e in metrics.get("entries", []) if e.get("miner") == hotkey]
    if not mine:
        # Present-but-empty is a REAL answer, and a different one from 404:
        # the day exists and this hotkey scored nothing in it (delivered no
        # predictions, or everything it predicted was censored).
        return {"day": safe, "miner": hotkey, "entries": [], "entries_total": 0,
                "receipt_sha256": env.get("sha256"),
                "note": "no scored entries for this hotkey on this day — the "
                        "day WAS scored, this miner has no entries in it",
                "miners_scored_that_day": metrics.get("miners")}
    scores = [e["score"] for e in mine]
    return {
        "day": safe, "miner": hotkey,
        "entries": mine,
        "entries_total": len(mine),
        "mean_score": round(sum(scores) / len(scores), 6),
        "receipt_sha256": env.get("sha256"),
        "formula": metrics.get("formula"),
        "how_to_verify": (
            f"python scripts/verify_day.py --url <this validator> "
            f"--day {safe} --miner {hotkey}"
        ),
    }


@router.get("/{day}/scores")
async def get_day_scores(day: str, request: Request):
    """Aggregate score summary for a day — no hotkeys.

    Deliberately aggregate: the site's information architecture is
    aggregate-only (IA D-05) and this is the endpoint a public page consumes.
    Per-miner detail is one route away at /{day}/miner/{hotkey}, which is
    public too — the distinction is presentation, not secrecy.
    """
    root, safe = _ledger_root(request), _safe_day(day)
    path = os.path.join(root, "receipts", f"{safe}.json")
    if not os.path.exists(path):
        raise HTTPException(status_code=404,
                            detail={"day": safe, "feed": "receipt",
                                    **_absence_reason(root, "receipts", safe)})
    env = _read_envelope(path)
    metrics = env.get("document", {}).get("metrics", {})
    by_h: dict[str, list] = {}
    for e in metrics.get("entries", []):
        by_h.setdefault(str(e.get("horizon_days")), []).append(e["score"])
    horizons = {h: {"scored": len(v),
                    "mean_score": round(sum(v) / len(v), 6),
                    "min_score": round(min(v), 6),
                    "max_score": round(max(v), 6)}
                for h, v in sorted(by_h.items())}
    return {"day": safe,
            "miners_scored": metrics.get("miners"),
            "entries_total": metrics.get("entries_total"),
            "outcomes_total": metrics.get("outcomes_total"),
            "censored": metrics.get("censored", {}),
            "horizons": horizons,
            "formula": metrics.get("formula"),
            "receipt_sha256": env.get("sha256")}


@router.get("/index")
async def get_index(request: Request):
    """The hash chain, oldest first: [{day, sha256, prev_sha256, receipt_sha256}].

    Lets a verifier walk the whole feed and confirm continuity without
    fetching every full receipt — each row's prev_sha256 must equal the
    previous row's sha256. A break is visible from this one call.
    """
    root = _ledger_root(request)
    acc_dir = os.path.join(root, "accuracy")
    rec_dir = os.path.join(root, "receipts")
    if not os.path.isdir(acc_dir):
        return {"days": [], "count": 0,
                "note": "no daily documents published yet"}

    rows = []
    for fn in sorted(f for f in os.listdir(acc_dir)
                     if f.endswith(".json") and not f.startswith("_")):
        day = fn[:-5]
        try:
            acc = _read_envelope(os.path.join(acc_dir, fn))
        except (OSError, json.JSONDecodeError) as e:  # noqa: PERF203
            # A corrupt file is reported in-band, not skipped: a silently
            # shorter index reads as a shorter chain, which is the same shape
            # as tampering.
            rows.append({"day": day, "error": f"unreadable: {type(e).__name__}"})
            continue
        receipt_sha = (acc.get("document", {}).get("metrics", {})
                       .get("receipt_sha256"))
        rows.append({
            "day": day,
            "sha256": acc.get("sha256"),
            "prev_sha256": acc.get("document", {}).get("prev_sha256"),
            "receipt_sha256": receipt_sha,
            "receipt_available": bool(receipt_sha) and os.path.exists(
                os.path.join(rec_dir, f"{day}.json")),
        })
    return {"days": rows, "count": len(rows),
            "how_to_verify": (
                "each row's prev_sha256 must equal the previous row's sha256; "
                "fetch /v1/daily/{day}/receipt and run scripts/verify_day.py "
                "to reproduce that day's scores"
            )}
