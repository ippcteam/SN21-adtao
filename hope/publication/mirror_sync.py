"""Push the daily verification feeds to the operator API mirror.

The feeds (receipt, accuracy document, proofs, root, index) are written by
the daily loop to the EXECUTOR's ledger disk — a background worker with no
public HTTP. The public validator API serves its own host's ledger, which
the daily loop never writes. Found 21 Aug 2026: every feed endpoint
answered "never published" while attested documents sat on the executor.
A receipt nobody can fetch verifies nothing (daily.py says the same), so
this module closes the transport half: after the publish step, the
pipeline renders the exact route bodies the daily router would serve and
POSTs them to the operator API, which stores and serves them verbatim at
the same /v1/daily/... paths — so `scripts/verify_day.py --url` works
against the mirror unchanged.

Rendering reuses the same pure functions the router uses (day_proof,
feed_root, published_days, build_series_document) and serves envelope
files verbatim, so there is no second implementation of any hash or
proof — only a second place to read the same bytes. Proofs and the root
change every time a new day publishes (rolling root), which is why the
sync re-renders and re-POSTs them all on every run.
"""

from __future__ import annotations

import json
import os
import urllib.request

MIRROR_POST_PATH = "/internal/bittensor/v1/daily/mirror"

_PROOF_HOW = (
    "leaf = sha256(0x00 || document_sha256); walk `proof` hashing "
    "sha256(0x01 || left || right) per step; the result must equal "
    "`feed_root`, which must equal the sha256 committed on chain by the "
    "validator hotkey"
)

_INDEX_HOW = (
    "each row's prev_sha256 must equal the previous row's sha256; "
    "fetch /v1/daily/{day}/receipt and run scripts/verify_day.py "
    "to reproduce that day's scores"
)


def _read_envelope(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def _feed_days(root: str, feed_dir: str) -> list[str]:
    d = os.path.join(root, feed_dir)
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d)
                  if f.endswith(".json") and not f.startswith("_"))


def build_mirror_items(ledger_root: str) -> list[dict]:
    """Every mirrored path and its exact response body, current as of now."""
    from hope.publication.feed_root import (
        day_proof,
        feed_root,
        published_days,
    )

    items: list[dict] = []

    for day in _feed_days(ledger_root, "receipts"):
        items.append({
            "path": f"/v1/daily/{day}/receipt",
            "body": _read_envelope(
                os.path.join(ledger_root, "receipts", f"{day}.json")),
        })

    acc_days = _feed_days(ledger_root, "accuracy")
    index_rows = []
    for day in acc_days:
        env = _read_envelope(
            os.path.join(ledger_root, "accuracy", f"{day}.json"))
        items.append({"path": f"/v1/daily/{day}/accuracy", "body": env})
        receipt_sha = (env.get("document", {}).get("metrics", {})
                       .get("receipt_sha256"))
        index_rows.append({
            "day": day,
            "sha256": env.get("sha256"),
            "prev_sha256": env.get("document", {}).get("prev_sha256"),
            "receipt_sha256": receipt_sha,
            "receipt_available": bool(receipt_sha) and os.path.exists(
                os.path.join(ledger_root, "receipts", f"{day}.json")),
        })
        proof = day_proof(ledger_root, day)
        if proof is not None:
            proof = {**proof, "how_to_verify": _PROOF_HOW}
            items.append({"path": f"/v1/daily/{day}/proof", "body": proof})

    items.append({
        "path": "/v1/daily/index",
        "body": {"days": index_rows, "count": len(index_rows),
                 "how_to_verify": _INDEX_HOW}
        if index_rows else
        {"days": [], "count": 0, "note": "no daily documents published yet"},
    })

    days = published_days(ledger_root)
    items.append({
        "path": "/v1/daily/root",
        "body": {
            "feed_root": feed_root(ledger_root),
            "leaf_count": len(days),
            "covers": ({"first_day": days[0][0], "last_day": days[-1][0]}
                       if days else None),
            "note": ("null root means nothing has published yet — no anchor "
                     "should exist on chain either" if not days else
                     "compare against the sha256 committed on chain by the "
                     "validator hotkey"),
        },
    })

    try:
        from hope.publication.series_feed import build_series_document
        items.append({"path": "/v1/daily/accuracy-series",
                      "body": build_series_document(ledger_root)})
    except Exception:
        # The series is a convenience surface; its absence must not stop
        # receipts from reaching miners.
        pass

    return items


def sync_mirror(ledger_root: str, api_url: str, api_key: str,
                timeout: int = 120) -> dict:
    """Render and POST everything. Returns the API's summary response."""
    items = build_mirror_items(ledger_root)
    req = urllib.request.Request(
        api_url.rstrip("/") + MIRROR_POST_PATH,
        data=json.dumps({"items": items}).encode(),
        method="POST",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        out = json.loads(resp.read().decode())
    out["items_sent"] = len(items)
    return out
