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
import re
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

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
    client: Optional[httpx.Client] = None,
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
        last_response: Optional[httpx.Response] = None
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
    val: Optional[float] = None
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


def _load_artifact(artifact_path: Path) -> EpochArtifact:
    """Load an artifact by direct path (not by epoch_id resolution)."""
    if not artifact_path.exists():
        raise FileNotFoundError(f"artifact not found: {artifact_path}")
    with open(artifact_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return EpochArtifact(**data)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="POST one epoch's leaderboard report to the CMS.",
    )
    parser.add_argument("--artifact", required=True, type=Path,
                        help="Path to the EpochArtifact JSON file.")
    parser.add_argument("--endpoint", default=None,
                        help="Override $SN21_LEADERBOARD_ENDPOINT (default: %s)."
                             % DEFAULT_ENDPOINT)
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
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = parser.parse_args(argv)

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

    # Load + aggregate
    try:
        artifact = _load_artifact(args.artifact)
    except (FileNotFoundError, json.JSONDecodeError, TypeError) as e:
        logger.error("cannot load artifact: %s", e)
        return EXIT_MISCONFIG

    payload = aggregate(
        artifact,
        commentary_markdown=args.commentary,
        supersedes=args.supersedes,
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

    response = post_payload(payload, endpoint=endpoint, api_key=api_key)

    # IA D-13 correction flow: a published epoch is frozen (409 "published
    # and frozen"). If we weren't already posting a correction, automatically
    # re-post under a `{epoch}-COR-N` epoch_id with a `supersedes` pointer to
    # the original. The CMS renders the most-recent correction as canonical;
    # the original keeps its permanent URL with a "corrected by" banner.
    if (
        _is_frozen_409(response)
        and not args.supersedes
        and "-COR-" not in artifact.epoch_id
    ):
        for n in range(1, 10):
            cor_id = f"{artifact.epoch_id}-COR-{n}"
            logger.warning(
                "epoch %s is published/frozen; re-posting as correction %s",
                artifact.epoch_id, cor_id,
            )
            cor_commentary = args.commentary or (
                f"Correction of {artifact.epoch_id}: validator registration "
                f"index refreshed; this entry supersedes the original report."
            )
            payload = aggregate(
                artifact,
                commentary_markdown=cor_commentary,
                supersedes=artifact.epoch_id,
                epoch_id_override=cor_id,
            )
            response = post_payload(payload, endpoint=endpoint, api_key=api_key)
            if not _is_frozen_409(response):
                break  # accepted (or a different outcome worth surfacing)

    return report_response(response)


if __name__ == "__main__":
    sys.exit(main())
