# SN21 Grafana Dashboards

## sn21-archive-dashboard.json

Dashboard for the archive server's `/metrics` endpoint (Tier-2 / Tier-3).

### Panels

| Panel | Query intent |
|---|---|
| Uploads (last 5m) | `sum(increase(sn21_archive_requests_total{method="POST"}[5m]))` |
| Fetches (last 5m) | `sum(increase(sn21_archive_requests_total{method="GET"}[5m]))` |
| SHA mismatches (1h) | `increase(sn21_archive_sha_mismatch_total[1h])` — must stay 0 |
| 5xx rate (5m) | `sum(rate(sn21_archive_requests_total{status_group="5xx"}[5m]))` |
| Request rate by outcome | per-method × outcome timeseries |
| Latency p50/p95/p99 | `histogram_quantile(...)` over `request_seconds_bucket` |
| Upload body size | `request_bytes_bucket` over POSTs |
| Stored objects / min | `rate(store_objects_total[1m])` |

### Import

In Grafana: Dashboards → Import → Upload JSON file → select
`sn21-archive-dashboard.json`. Map the `Prometheus` data source.

### Alerting (suggested)

- **SHA mismatch ≥ 1 in 1h** → page on-call (likely malicious archive or bit-rot).
- **5xx rate ≥ 0.1 ops/s for 5m** → page on-call (server health).
- **p99 latency > 5s for 10m** → warn (Tier-2 capacity issue).
- **No POSTs for 30m during epoch window** → warn (miners not uploading).
