"""POST one epoch's leaderboard report to the operator's CMS endpoint.

Reads a private `EpochArtifact` JSON file, runs the pure aggregator to
produce the public `EpochReportPayload`, and POSTs it. Idempotent per
`epoch_id` (the CMS uses the `Idempotency-Key` header to upsert
drafts; published rows are frozen — see contract §5.2).

Exit codes:
  0  Accepted (201 Created or 200 OK draft upsert).
  1  Misconfiguration (missing file, env var, CLI arg).
  2  Non-recoverable 4xx (schema mismatch, auth failure, conflict).
  3  Persistent 5xx after retries.

This script carries no payload-shape logic — it consumes the
aggregator's output verbatim. Touching this file should never change
what's posted.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx

from hope.reporting.aggregator import aggregate
from hope.reporting.epoch_artifact import EpochArtifact
from hope.reporting.payload import EpochReportPayload

logger = logging.getLogger("post_epoch_report")

DEFAULT_ENDPOINT = "https://cms.adtao.io/api/sn21-epoch-reports"
DEFAULT_TIMEOUT_SECONDS = 30.0
DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BACKOFF_INITIAL_SECONDS = 1.0
DEFAULT_BACKOFF_FACTOR = 2.0

# Exit codes
EXIT_OK = 0
EXIT_MISCONFIG = 1
EXIT_REJECTED = 2
EXIT_PERSISTENT_5XX = 3


def _mask_bearer(headers: dict[str, str]) -> dict[str, str]:
    """Redact the bearer token from a headers dict for safe logging."""
    safe = dict(headers)
    if "Authorization" in safe:
        safe["Authorization"] = "Bearer <redacted>"
    return safe


def build_request_headers(api_key: str, epoch_id: str) -> dict[str, str]:
    """Construct the headers per contract §5."""
    return {
        "Authorization": f"Bearer {api_key}",
        "Idempotency-Key": f"epoch-{epoch_id}",
        "Content-Type": "application/json",
    }


def post_payload(
    payload: EpochReportPayload,
    *,
    endpoint: str,
    api_key: str,
    client: httpx.Client | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    backoff_initial_seconds: float = DEFAULT_BACKOFF_INITIAL_SECONDS,
    backoff_factor: float = DEFAULT_BACKOFF_FACTOR,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> httpx.Response:
    """POST the payload, retrying on 5xx with exponential backoff.

    Returns the final response on terminal outcome (success, 4xx,
    or exhausted retries). Never raises on HTTP status — the caller
    inspects the response and decides exit semantics.

    `sleep_fn` is injectable for tests so they don't actually sleep.
    """
    body = payload.model_dump(mode="json")
    headers = build_request_headers(api_key, payload.epoch_id)

    own_client = False
    if client is None:
        client = httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS)
        own_client = True

    try:
        last_response: httpx.Response | None = None
        delay = backoff_initial_seconds
        for attempt in range(1, max_attempts + 1):
            logger.info(
                "POST attempt %d/%d epoch=%s endpoint=%s",
                attempt, max_attempts, payload.epoch_id, endpoint,
            )
            response = client.post(endpoint, json=body, headers=headers)
            last_response = response
            status = response.status_code

            # 2xx terminal-success
            if 200 <= status < 300:
                return response
            # 429 Too Many Requests → honour Retry-After and retry. The CMS
            # throttles rapid POSTs; the correction flow posts twice in quick
            # succession (original 409 → -COR-N), which can trip this.
            if status == 429:
                wait = _retry_after_seconds(response, default=delay)
                logger.warning(
                    "429 rate-limited on attempt %d/%d; sleeping %.1fs",
                    attempt, max_attempts, wait,
                )
                if attempt < max_attempts:
                    sleep_fn(wait)
                    delay *= backoff_factor
                continue
            # 5xx → retry with backoff
            if 500 <= status < 600:
                logger.warning(
                    "5xx on attempt %d/%d (status=%d); sleeping %.1fs",
                    attempt, max_attempts, status, delay,
                )
                if attempt < max_attempts:
                    sleep_fn(delay)
                    delay *= backoff_factor
                continue
            # other 4xx → terminal failure, no retry
            return response

        # Exhausted retries; last_response is the most recent 5xx.
        assert last_response is not None
        return last_response
    finally:
        if own_client:
            client.close()


def report_response(response: httpx.Response) -> int:
    """Log + map an HTTP response to an exit code."""
    status = response.status_code
    if 200 <= status < 300:
        try:
            body = response.json()
        except json.JSONDecodeError:
            body = {}
        review_url = body.get("review_url")
        report_id = body.get("id")
        report_status = body.get("status", "draft")
        logger.info(
            "ACCEPTED status=%d report_status=%s id=%s review_url=%s",
            status, report_status, report_id, review_url,
        )
        if review_url:
            print(f"review_url: {review_url}")
        return EXIT_OK

    if 500 <= status < 600:
        logger.error("PERSISTENT 5xx after retries: status=%d body=%s",
                     status, response.text[:200])
        return EXIT_PERSISTENT_5XX

    # 4xx
    detail: Any = response.text[:500]
    try:
        body = response.json()
        detail = body.get("detail", detail)
    except json.JSONDecodeError:
        pass
    logger.error("REJECTED status=%d detail=%s", status, detail)
    return EXIT_REJECTED


def _retry_after_seconds(response: httpx.Response, *, default: float) -> float:
    """Seconds to wait before retrying a 429, from the Retry-After header or
    a `"Retry after Ns"` body hint. Clamped to [1, 10] so a hostile header
    can't stall the cron."""
    raw = response.headers.get("Retry-After") or response.headers.get("retry-after")
    val: float | None = None
    if raw:
        try:
            val = float(raw)
        except ValueError:
            val = None
    if val is None:
        m = re.search(r"retry after\s+(\d+(?:\.\d+)?)\s*s", response.text, re.IGNORECASE)
        if m:
            val = float(m.group(1))
    if val is None:
        val = default
    return max(1.0, min(10.0, val))


MAX_CORRECTIONS = 9


def post_with_correction(
    artifact,
    payload,
    *,
    endpoint: str,
    api_key: str,
    commentary: str | None = None,
    already_a_correction: bool = False,
    max_corrections: int = MAX_CORRECTIONS,
):
    """POST a report, re-posting as `{epoch}-COR-N` if the epoch is frozen.

    `payload` is authoritative. The correction is that same payload re-labelled,
    so whatever the caller built it with travels into the correction unchanged.
    There is deliberately no way to ask this function to rebuild it: doing so
    published corrections that silently lacked the caller's enrichment.

    IA D-13: a published epoch is frozen and the CMS answers 409. The fix is
    not to overwrite it — the original keeps its permanent URL and gains a
    "corrected by" banner — but to publish a successor carrying a `supersedes`
    pointer, which the CMS then renders as canonical.

    THIS LIVES HERE, AND IS CALLED FROM BOTH PLACES, ON PURPOSE
        The flow existed only inside this file's `main()`, so it was reachable
        by hand and not by the daily pipeline, which calls `post_payload`
        directly. A day that needed correcting therefore could not be
        corrected by the thing that runs every day: the pipeline posted, saw
        409, recorded `published: false`, and left the superseded numbers on
        the public leaderboard. Re-running the day fixed the ledger and the
        chain vector while the dashboard kept showing the old figures.

    Returns (response, epoch_id_actually_posted).
    """
    response = post_payload(payload, endpoint=endpoint, api_key=api_key)
    if (
        already_a_correction
        or "-COR-" in artifact.epoch_id
        or not _is_frozen_409(response)
    ):
        return response, artifact.epoch_id

    for n in range(1, max_corrections + 1):
        cor_id = f"{artifact.epoch_id}-COR-{n}"
        logger.warning(
            "epoch %s is published/frozen; re-posting as correction %s",
            artifact.epoch_id, cor_id,
        )
        cor_commentary = commentary or (
            f"Correction of {artifact.epoch_id}: this entry supersedes the "
            f"original report."
        )
        # Re-label the payload we were given; do NOT rebuild it.
        #
        # This used to call aggregate() again from the artifact alone, which
        # silently dropped everything the caller had enriched the payload
        # with — the allocation audit and the day's earning set. A frozen day
        # therefore published a correction in which every miner was funded
        # and no row carried the control that had acted on it, while the
        # caller's own logs described the correct payload it had built and
        # then thrown away. The correction was a different report from the
        # one that was checked.
        #
        # A correction differs from its original in three fields. Copying is
        # the only way to guarantee it differs in exactly those three.
        payload = payload.model_copy(update={
            "epoch_id": cor_id,
            "supersedes": artifact.epoch_id,
            "commentary_markdown": cor_commentary,
        })
        response = post_payload(payload, endpoint=endpoint, api_key=api_key)
        if not _is_frozen_409(response):
            return response, cor_id  # accepted, or a different outcome

    return response, cor_id


def _is_frozen_409(response: httpx.Response) -> bool:
    """True if the CMS rejected because the epoch is published/frozen (IA D-13).

    The server returns 409 with a body like
    ``{"error": "Epoch ... is published and frozen (IA D-13). ..."}``.
    """
    if response.status_code != 409:
        return False
    text = response.text
    try:
        body = response.json()
        text = str(body.get("error") or body.get("detail") or text)
    except json.JSONDecodeError:
        pass
    low = text.lower()
    return "frozen" in low or "published" in low


_POSTED_LEDGER = ".posted_epochs.json"


def _ledger_path(ledger_dir: Path) -> Path:
    return ledger_dir / _POSTED_LEDGER


def _already_posted(ledger_dir: Path, epoch_id: str) -> bool:
    """True if epoch_id is recorded in the dir's posted-ledger."""
    p = _ledger_path(ledger_dir)
    if not p.exists():
        return False
    try:
        return epoch_id in set(json.loads(p.read_text()))
    except (json.JSONDecodeError, ValueError, OSError):
        return False  # unreadable ledger ⇒ treat as not-posted (safe: at worst re-posts)


def _record_posted(ledger_dir: Path, epoch_id: str) -> None:
    """Append epoch_id to the dir's posted-ledger (best-effort, idempotent)."""
    p = _ledger_path(ledger_dir)
    try:
        existing = json.loads(p.read_text()) if p.exists() else []
    except (json.JSONDecodeError, ValueError, OSError):
        existing = []
    if epoch_id not in existing:
        existing.append(epoch_id)
    try:
        p.write_text(json.dumps(existing))
    except OSError as e:
        logger.warning("could not update posted-ledger %s: %s", p, e)


def _load_artifact(artifact_path: Path) -> EpochArtifact:
    """Load an artifact by direct path (not by epoch_id resolution)."""
    if not artifact_path.exists():
        raise FileNotFoundError(f"artifact not found: {artifact_path}")
    with open(artifact_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return EpochArtifact(**data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="POST one epoch's leaderboard report to the CMS.",
    )
    parser.add_argument("--artifact", type=Path, default=None,
                        help="Path to the EpochArtifact JSON file.")
    parser.add_argument("--artifact-dir", type=Path, default=None,
                        help="Directory of epoch_*.json artifacts; posts the "
                             "NEWEST one. For autonomous/daemon use where the "
                             "exact epoch isn't known ahead of time. Exits 0 "
                             "(no-op) if the dir holds no artifact.")
    parser.add_argument("--skip-if-posted", action="store_true",
                        help="Skip if this epoch_id was already posted (tracked "
                             "in <dir>/.posted_epochs.json); record on success. "
                             "Makes repeated daemon invocations idempotent so a "
                             "published/frozen epoch isn't re-posted as a fresh "
                             "-COR correction every tick.")
    parser.add_argument("--endpoint", default=None,
                        help=f"Override $SN21_LEADERBOARD_ENDPOINT (default: {DEFAULT_ENDPOINT}).")
    parser.add_argument("--api-key", default=None,
                        help="Override $SN21_LEADERBOARD_API_KEY (default: env).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Validate + print payload JSON; do not POST.")
    parser.add_argument("--commentary", default=None,
                        help="Optional commentary_markdown override.")
    parser.add_argument("--supersedes", default=None,
                        help="Optional release_key of a published row this "
                             "POST corrects (e.g. 'WR-2026-W21-PUB-E1'). "
                             "Triggers the CMS correction flow: published "
                             "rows stay frozen; the corrected row appears "
                             "as a new entry with a `supersedes` pointer "
                             "back to the original. The dashboard renders "
                             "the most-recent correction as the canonical "
                             "view of an epoch slot.")
    parser.add_argument("--epoch-membership-uids", default=None,
                        help="Comma-separated uids eligible to be SCORED this epoch "
                             "(e.g. a re-run scoped to the original participants). A "
                             "miner that scored but whose uid is not listed is "
                             "re-labeled 'disqualified_not_in_epoch' (shown, not "
                             "credited). Omit to score everyone normally.")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

    membership_uids = None
    if args.epoch_membership_uids:
        membership_uids = {int(u) for u in args.epoch_membership_uids.replace(",", " ").split()}

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    # Resolve transport config
    endpoint = (
        args.endpoint
        or os.environ.get("SN21_LEADERBOARD_ENDPOINT")
        or DEFAULT_ENDPOINT
    )
    api_key = args.api_key or os.environ.get("SN21_LEADERBOARD_API_KEY", "")

    # Resolve the artifact path: explicit --artifact, or the newest in --artifact-dir.
    artifact_path = args.artifact
    ledger_dir = args.artifact_dir
    if args.artifact_dir is not None:
        candidates = sorted(args.artifact_dir.glob("epoch_*.json"),
                            key=lambda p: p.stat().st_mtime, reverse=True)
        if not candidates:
            logger.info("no epoch_*.json in %s; nothing to post (no-op)",
                        args.artifact_dir)
            return EXIT_OK
        artifact_path = candidates[0]
        logger.info("artifact-dir: selected newest artifact %s", artifact_path.name)
    if artifact_path is None:
        logger.error("one of --artifact or --artifact-dir is required")
        return EXIT_MISCONFIG
    if ledger_dir is None:
        ledger_dir = artifact_path.parent

    # Load + aggregate
    try:
        artifact = _load_artifact(artifact_path)
    except (FileNotFoundError, json.JSONDecodeError, TypeError) as e:
        logger.error("cannot load artifact: %s", e)
        return EXIT_MISCONFIG

    if args.skip_if_posted and _already_posted(ledger_dir, artifact.epoch_id):
        logger.info("epoch %s already posted (per %s ledger); skipping",
                    artifact.epoch_id, ledger_dir)
        return EXIT_OK

    payload = aggregate(
        artifact,
        commentary_markdown=args.commentary,
        supersedes=args.supersedes,
        epoch_membership_uids=membership_uids,
    )

    if args.dry_run:
        print(json.dumps(payload.model_dump(mode="json"), indent=2, sort_keys=True))
        logger.info("dry-run OK epoch=%s pool_size=%d",
                    payload.epoch_id, payload.pool_size)
        return EXIT_OK

    if not api_key:
        logger.error(
            "SN21_LEADERBOARD_API_KEY is required (or pass --api-key). "
            "Aborting before sending an unauthenticated request."
        )
        return EXIT_MISCONFIG

    response, _posted_as = post_with_correction(
        artifact, payload,
        endpoint=endpoint, api_key=api_key,
        commentary=args.commentary,
        already_a_correction=bool(args.supersedes),
    )

    rc = report_response(response)
    if args.skip_if_posted and rc == EXIT_OK:
        _record_posted(ledger_dir, artifact.epoch_id)
    return rc


if __name__ == "__main__":
    sys.exit(main())
