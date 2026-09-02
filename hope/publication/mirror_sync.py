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
import time
import urllib.error
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


def _sha256_of(body) -> str:
    """Digest of the exact bytes that would be posted.

    Canonicalised (sorted keys, no incidental whitespace) so an unchanged
    document produces the same digest run after run — otherwise dict ordering
    alone would make every document look modified and nothing would ever be
    skipped.
    """
    import hashlib
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _read_envelope(path: str) -> dict:
    with open(path) as fh:
        return json.load(fh)


def _feed_days(root: str, feed_dir: str) -> list[str]:
    d = os.path.join(root, feed_dir)
    if not os.path.isdir(d):
        return []
    return sorted(f[:-5] for f in os.listdir(d)
                  if f.endswith(".json") and not f.startswith("_"))


def build_mirror_items(ledger_root: str,
                       recent_days: int | None = None) -> list[dict]:
    """Every mirrored path and its exact response body, current as of now.

    recent_days limits which RECEIPT and ACCURACY bodies are included (the
    last N by day) — they are immutable once published, so the daily run
    has no reason to re-ship history it already shipped. Proofs, index,
    root, and the series are always rendered in full: they legitimately
    change whenever a new day publishes. None = everything (backfill).
    """
    from hope.publication.feed_root import (
        day_proof,
        feed_root,
        published_days,
    )

    items: list[dict] = []

    rec_days = _feed_days(ledger_root, "receipts")
    ship_rec = set(rec_days if recent_days is None else rec_days[-recent_days:])
    for day in rec_days:
        if day not in ship_rec:
            continue
        items.append({
            "path": f"/v1/daily/{day}/receipt",
            "body": _read_envelope(
                os.path.join(ledger_root, "receipts", f"{day}.json")),
        })

    # The allocation audit ships with the receipt it is derived from: a
    # grouping is only checkable next to the predictions it was computed
    # over, so mirroring one without the other publishes an assertion
    # instead of evidence.
    audit_days = _feed_days(ledger_root, "allocation_audit")
    ship_audit = set(audit_days if recent_days is None
                     else audit_days[-recent_days:])
    for day in audit_days:
        if day not in ship_audit:
            continue
        items.append({
            "path": f"/v1/daily/{day}/allocation-audit",
            "body": _read_envelope(
                os.path.join(ledger_root, "allocation_audit", f"{day}.json")),
        })

    acc_days = _feed_days(ledger_root, "accuracy")
    ship_acc = set(acc_days if recent_days is None else acc_days[-recent_days:])
    index_rows = []
    for day in acc_days:
        env = _read_envelope(
            os.path.join(ledger_root, "accuracy", f"{day}.json"))
        if day in ship_acc:
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

    # The cumulative Prediction Performance document — the latest one only,
    # at a stable path. Like the series it is a convenience surface derived
    # entirely from the receipts above, so a reader can recompute it.
    try:
        perf_dir = os.path.join(ledger_root, "prediction_performance")
        if os.path.isdir(perf_dir):
            perf_days = sorted(n[:-5] for n in os.listdir(perf_dir)
                               if n.endswith(".json"))
            if perf_days:
                with open(os.path.join(perf_dir,
                                       f"{perf_days[-1]}.json")) as fh:
                    items.append({"path": "/v1/daily/prediction-performance",
                                  "body": json.load(fh)})
    except Exception:
        pass

    # Absence penalties change standings, and standings must stay
    # reproducible from public documents alone — so the applied-penalty log
    # publishes beside the receipts. Empty log publishes as an empty list,
    # which is itself the statement "no penalties have been charged".
    try:
        from hope.scoring.absence_penalty import penalty_log
        from hope.scoring.standing_ledger import load_cancellations
        items.append({"path": "/v1/daily/absence-penalties",
                      "body": {"note": ("every applied absence penalty: one "
                                        "standing entry per missed episode "
                                        "at the published floor score. A "
                                        "charge caused by an operator-side "
                                        "failure is corrected by a record in "
                                        "`cancellations`, never deleted — "
                                        "standings exclude cancelled entries."),
                               "penalties": penalty_log(ledger_root),
                               "cancellations": load_cancellations(ledger_root)}})
    except Exception:
        pass

    return items


# Keep each POST comfortably inside proxy/worker request limits. The
# reconstruction-era receipts are tens of megabytes; one monolithic POST
# 502'd at the gateway on the first live run (21 Aug), which is how this
# number earned its existence.
MAX_POST_BYTES = 4_000_000


# Gateway statuses worth a retry: the operator backend can 502/503/504 on cold
# start or under load, and one blip must not leave the public mirror stale until
# the next hourly run (2026-08-24: a single 502 mid-sync did exactly that, and
# because index/root ship in the LAST batches, the whole feed read as of the
# previous day for an hour). 4xx (bad key, bad payload) never fixes on retry.
_RETRY_STATUSES = frozenset({502, 503, 504})


class MirrorSyncError(RuntimeError):
    """Raised when batches remain failed after retries. Carries the summary so
    the caller (and the heartbeat) can see what shipped and what did not."""

    def __init__(self, summary: dict):
        self.summary = summary
        failed_paths = [p for f in summary["failed_posts"] for p in f["paths"]]
        super().__init__(
            f"{len(summary['failed_posts'])} of {summary['posts']} mirror "
            f"batches failed after retries (stored {summary['stored']}; "
            f"index/root/absence-penalties were still attempted). "
            f"failed paths: {failed_paths[:8]}")


def _post(api_url: str, api_key: str, items: list[dict],
          timeout: int, retries: int = 3, backoff: float = 2.0) -> dict:
    data = json.dumps({"items": items}).encode()
    for attempt in range(retries + 1):
        req = urllib.request.Request(
            api_url.rstrip("/") + MIRROR_POST_PATH,
            data=data,
            method="POST",
            headers={"X-API-Key": api_key, "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            if e.code not in _RETRY_STATUSES or attempt == retries:
                raise
        except urllib.error.URLError:
            # socket timeout / connection reset / DNS blip — all transient.
            if attempt == retries:
                raise
        time.sleep(backoff * (2 ** attempt))   # 2s, 4s, 8s
    raise RuntimeError("unreachable: retry loop neither returned nor raised")


def _shipped_path(ledger_root: str) -> str:
    return os.path.join(ledger_root, "_mirror_shipped.json")


def _load_shipped(ledger_root: str) -> dict:
    try:
        with open(_shipped_path(ledger_root)) as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        # No record, or an unreadable one, means ship everything. Re-sending a
        # document the mirror already holds costs a request; skipping one it
        # does not hold costs a day nobody can verify.
        return {}


def _record_shipped(ledger_root: str, confirmed: dict) -> None:
    tmp = _shipped_path(ledger_root) + ".tmp"
    try:
        with open(tmp, "w") as fh:
            json.dump(confirmed, fh)
        os.replace(tmp, _shipped_path(ledger_root))
    except OSError:
        # Losing the record only costs a re-send next run.
        pass


def _mirror_has(api_url: str, path: str, timeout: int = 20) -> bool:
    """Whether the mirror already serves `path`, without fetching the body.

    Needed because the documents worth skipping are exactly the ones that
    could never be recorded: the three biggest receipts time out on upload,
    so a record built only from successful posts would never contain them and
    they would be retried for ever. The mirror has held them since the day
    they were first published — this asks, in a request that transfers no
    body, rather than pushing 35MB to find out.
    """
    req = urllib.request.Request(api_url.rstrip("/") + path, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 300
    except Exception:      # noqa: BLE001 — absent, unreachable, anything
        return False       # the safe answer is "send it"


def _is_immutable(path: str) -> bool:
    """Documents that never change once published.

    A receipt and an accuracy document are attested and hash-chained; the
    rail refuses to republish them, so their bytes are fixed for ever. Proofs,
    the root, the index and the accuracy series are NOT immutable — they are
    re-rendered every time a day publishes, because the root rolls — and must
    ship on every run.
    """
    return path.endswith("/receipt") or path.endswith("/accuracy")


def sync_mirror(ledger_root: str, api_url: str, api_key: str,
                timeout: int = 120,
                recent_days: int | None = None) -> dict:
    """Render and POST everything, batched by size. An oversized single
    item still ships alone — the endpoint has no per-item cap, only the
    gateway's request limit, and one item per request is as small as a
    request gets.

    Documents that cannot change are sent once. The feed re-sent every
    published receipt on every run — the three largest are 35MB, 29MB and
    13MB — and those uploads timed out, so a run that had published
    correctly reported a failed sync and turned the heartbeat red. Nothing
    was ever missing; the same frozen bytes were being pushed daily. A
    document is skipped only after the mirror has confirmed storing that
    exact sha256, so a changed document always ships.
    """
    items = build_mirror_items(ledger_root, recent_days=recent_days)

    shipped = _load_shipped(ledger_root)
    # Kept beside the items, never inside them: the ingest endpoint validates
    # the item shape, so an extra key would travel to the mirror and could be
    # rejected there.
    digest_of: dict[str, str] = {}
    skipped: list[str] = []
    to_send = []
    adopted: list[str] = []
    for it in items:
        path = it.get("path") or ""
        digest = _sha256_of(it.get("body"))
        digest_of[path] = digest
        if not _is_immutable(path):
            to_send.append(it)
            continue
        if shipped.get(path) == digest:
            skipped.append(path)
            continue
        # No record, but the mirror may already hold it from before this
        # bookkeeping existed — which is true of every receipt published so
        # far, including the ones too large to re-upload. Ask before sending.
        if _mirror_has(api_url, path):
            shipped[path] = digest
            adopted.append(path)
            skipped.append(path)
            continue
        to_send.append(it)
    items = to_send
    batches: list[list[dict]] = []
    batch: list[dict] = []
    batch_bytes = 0
    for it in items:
        size = len(json.dumps(it))
        if batch and batch_bytes + size > MAX_POST_BYTES:
            batches.append(batch)
            batch, batch_bytes = [], 0
        batch.append(it)
        batch_bytes += size
    if batch:
        batches.append(batch)

    stored = 0
    rejected: list = []
    failed: list = []
    confirmed = dict(shipped)
    for i, b in enumerate(batches):
        try:
            out = _post(api_url, api_key, b, timeout)
            stored += int(out.get("stored") or 0)
            batch_rejected = out.get("rejected") or []
            rejected.extend(batch_rejected)
            # Only what the mirror actually accepted. A rejected path must
            # not be recorded as shipped, or it is never sent again.
            bad = {r.get("path") if isinstance(r, dict) else r
                   for r in batch_rejected}
            for it in b:
                p = it.get("path") or ""
                if _is_immutable(p) and p not in bad and p in digest_of:
                    confirmed[p] = digest_of[p]
        except Exception as e:  # noqa: BLE001
            # Do NOT abort the loop: index, root and the absence-penalty log
            # ride in the FINAL batches, and a stale root is worse than a
            # missing receipt. Record the failure and keep shipping; surface
            # it after every batch has been attempted.
            failed.append({"batch": i,
                           "paths": [it.get("path") for it in b],
                           "error": str(e)})
    _record_shipped(ledger_root, confirmed)
    summary = {"success": not failed, "stored": stored, "rejected": rejected,
               "items_sent": len(items), "posts": len(batches),
               "skipped_unchanged": len(skipped),
               "adopted_from_mirror": len(adopted), "failed_posts": failed}
    if failed:
        raise MirrorSyncError(summary)
    return summary
