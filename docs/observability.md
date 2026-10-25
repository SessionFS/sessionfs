# Operator metrics

SessionFS exposes a Prometheus-compatible metrics endpoint for operators:

```
GET /api/v1/admin/metrics
```

The response is Prometheus text exposition format `0.0.4`
(`Content-Type: text/plain; version=0.0.4`). Metrics are **per-process**, so in
a multi-replica deployment you scrape every replica and aggregate — see
[Aggregating across replicas](#aggregating-across-replicas) for which metrics
sum and which take `max()`.

## Authentication

The endpoint is **not anonymous**. It accepts exactly two credentials:

- A **platform-admin user key** (a user whose tier is `admin`), sent as the
  usual `Authorization: Bearer sk_sfs_…` header.
- A **service key** carrying the `admin_metrics:read` scope. As with
  `platform_sweeps:run`, the key only works while **both** the user it runs as
  and the platform admin who minted (or last rotated) it are still *active*
  platform admins — demote or deactivate either and the key stops on the next
  scrape. Only a platform admin may mint or rotate such a key.

Anything else gets `401` (anonymous) or `403` (a user or service key without
the scope). See [api-keys.md](api-keys.md) for the full scope table.

## Enabling scrape in Helm

The chart ships with scrape annotations **off** by default because the endpoint
requires a Bearer token:

```yaml
api:
  metrics:
    scrapeAnnotations: true
```

This adds `prometheus.io/scrape: "true"`, `prometheus.io/path` and
`prometheus.io/port` to the API pod template. Point Prometheus at the pods and
supply the token in your scrape job (e.g. via `authorization.credentials` in a
`scrape_config`, or an `extraScrapeConfigs` secret in the kube-prometheus-stack).

The annotations alone do **not** authenticate the scrape — a Prometheus scrape
that reaches the endpoint without a valid Bearer key will see `401` and record
no series. Configure the token out-of-band in your Prometheus deployment.

## Metric reference

| Metric | Type | Labels | Meaning |
|---|---|---|---|
| `http_request_duration_seconds` | histogram | `method`, `route`, `status_class` | Request duration. `route` is the matched route **template** (e.g. `/api/v1/sessions/{session_id}/sync`), `status_class` is `2xx`/`3xx`/`4xx`/`5xx`. Every 404 and 405 response is recorded with `route="unmatched"`, including a 404 raised by a matched handler (such as "Session not found"), which keeps the label set bounded. A request whose handler crashed is recorded as `5xx` with the route template when known. `method` is one of `GET`/`POST`/`PUT`/`PATCH`/`DELETE`/`HEAD`/`OPTIONS`, else `other`. No raw paths or ids are ever recorded. |
| `sessionfs_push_total` | counter | `outcome`, `size_bucket` | Sync pushes by outcome (`ok`, `conflict`, `rejected_quota`, `rejected_dlp`, `error`) and payload size (`<1MB`, `1-10MB`, `10-50MB`, `50-100MB`, `>100MB`). A 413 rejected on the Content-Length header alone is bucketed from that header, not `<1MB`. |
| `sessionfs_sync_pull_total` | counter | — | Successful session downloads. |
| `sessionfs_handoff_create_total` | counter | — | Handoffs created. |
| `sessionfs_handoff_claim_total` | counter | — | Handoffs claimed. |
| `sessionfs_dlp_findings_total` | counter | `severity`, `action` | DLP findings by severity (`critical`/`high`/`medium`/`low`, else `other`) and policy action (`block`/`redact`/`warn`, else `other`). |
| `sessionfs_auth_failures_total` | counter | `reason` | Auth failures by bounded reason (`missing_bearer`, `invalid_key`, `revoked`, `inactive_key`, `expired`, `inactive_user`). |
| `sessionfs_active_users` | gauge | `window` | Distinct active users with a sync in the window (`1h`, `1d`, `7d`). |
| `sessionfs_sessions` | gauge | — | Total non-deleted sessions. |
| `sessionfs_db_pool_size` | gauge | — | Configured database connection pool size. |
| `sessionfs_db_pool_checked_out` | gauge | — | Connections currently checked out. |
| `sessionfs_db_pool_overflow` | gauge | — | Overflow connections in the pool. |
| `sessionfs_metrics_series_dropped_total` | counter | — | Samples dropped by the per-metric series cap (cardinality guard). |

The DB-derived gauges (`active_users`, `sessions`, `db_pool_*`) are
computed at scrape time and cached for 30 seconds. The pool gauges are absent
entirely when there is no live pool (e.g. SQLite) rather than reporting zeros.

## Aggregating across replicas

Most metrics are **per-process**: each replica increments its own copy, so
aggregate with `sum()` (or `sum by (...)` for labeled series). A few are
**cluster-wide** — the same value is set on every replica from the database or
configuration, so summing them would over-count by the replica count. Take
`max()` (or `max by (...)`) for those instead:

| Aggregation | Metrics |
|---|---|
| `sum()` / `sum by (...)` | `http_request_duration_seconds`, `sessionfs_push_total`, `sessionfs_sync_pull_total`, `sessionfs_handoff_create_total`, `sessionfs_handoff_claim_total`, `sessionfs_dlp_findings_total`, `sessionfs_auth_failures_total`, `sessionfs_metrics_series_dropped_total`, `sessionfs_db_pool_checked_out`, `sessionfs_db_pool_overflow`, `sessionfs_db_pool_size` (each process has its own pool, so the cluster capacity is the sum) |
| `max()` / `max by (...)` | `sessionfs_active_users`, `sessionfs_sessions` |

## Streaming responses

`http_request_duration_seconds` records **time-to-first-headers** for a
streaming response (e.g. a `text/event-stream` route): `call_next` returns once
the response is started, so the duration is the time until headers were ready,
not until the stream finished. This applies to the session helper's streaming
route, `POST /api/v1/helper/turns`.

## Cardinality

Every label value is a bounded vocabulary chosen at the call site. A per-metric
series cap (10,000) drops observations past the limit as a backstop against an
accidental unbounded label value; nothing records raw paths, user ids, project
ids or session ids. Dropped samples are counted in
`sessionfs_metrics_series_dropped_total`.

## Dashboard

A ready-to-import Grafana dashboard is at
[`docs/observability/sessionfs-ops.json`](observability/sessionfs-ops.json). It
covers push outcomes, DLP findings, p50/p95 latency by route, active users and
DB pool saturation.
