"""Prometheus metrics for the SN21 archive server.

Exposes per-archive-server counters and histograms for operations
visibility: uploads vs fetches, sha256 mismatches (signal for malicious
archives or bit-rot), per-tier latency, body sizes.

Metrics are populated by the FastAPI middleware in `app.py`. The
`/metrics` endpoint exposes them in Prometheus text format.

Operators scrape `/metrics` with their existing Prometheus + Grafana
stack. Standard scrape config applies.

Cardinality is bounded:
  - method ∈ {POST, GET}                          (2 labels)
  - outcome ∈ {ok, sha_mismatch, body_too_large,
               not_found, auth_fail, other}        (~6 labels)
  - status_code group: 2xx / 4xx / 5xx             (3 labels)
Total label combos: ≤ 36 — well within reasonable scrape budget.
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)


def build_registry() -> tuple[CollectorRegistry, dict]:
    """Create a fresh registry + the metric handles bound to it.

    Returns (registry, metrics_dict) where metrics_dict has stable keys
    callers use to record events. Tests can build their own registry to
    avoid global state.
    """
    registry = CollectorRegistry()
    metrics = {
        "requests_total": Counter(
            "sn21_archive_requests_total",
            "Total archive HTTP requests by method/outcome.",
            labelnames=("method", "outcome", "status_group"),
            registry=registry,
        ),
        "request_bytes": Histogram(
            "sn21_archive_request_bytes",
            "Per-request body size in bytes.",
            labelnames=("method",),
            buckets=(64, 128, 256, 512, 1024, 4096, 16384, 65536, 262144,
                     1048576),
            registry=registry,
        ),
        "request_seconds": Histogram(
            "sn21_archive_request_seconds",
            "Per-request elapsed time in seconds.",
            labelnames=("method", "outcome"),
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5,
                     5.0, 10.0),
            registry=registry,
        ),
        "store_objects": Counter(
            "sn21_archive_store_objects_total",
            "Objects written to the underlying store (excludes 200 dedup).",
            labelnames=("kind",),  # kind ∈ {put_new}
            registry=registry,
        ),
        "sha_mismatch_total": Counter(
            "sn21_archive_sha_mismatch_total",
            "Body SHA-256 disagreed with path component (suspicious).",
            registry=registry,
        ),
    }
    return registry, metrics


def status_group(code: int) -> str:
    if 200 <= code < 300:
        return "2xx"
    if 400 <= code < 500:
        return "4xx"
    if 500 <= code < 600:
        return "5xx"
    return "other"


def render_text(registry: CollectorRegistry) -> tuple[bytes, str]:
    """Render Prometheus text-format output. Returns (body, content_type)."""
    return generate_latest(registry), CONTENT_TYPE_LATEST
