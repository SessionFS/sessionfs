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
| `sessionfs_sweep_runs_total` | counter | `sweep`, `result` | Authorized sweep runs. `sweep` is the closed sweep name (`reminders-due`, `jira-sync`, …; see the site's Metrics Reference for all 17), `result` is `ok`/`rejected`/`error`. |
| `sessionfs_sweep_duration_seconds` | histogram | `sweep` | Wall-clock duration of authorized sweep runs (0.1 s – 30 min buckets). |
| `sessionfs_sweep_lag_seconds` | gauge | `sweep` | How long the oldest due item had waited when the sweep last started (`reminders-due`, `jira-sync`, `ticket-notifications`); 0 = nothing due. |
| `sessionfs_sweep_denied_total` | counter | `sweep`, `reason` | Sweep calls refused before running: `unauthenticated`, `rate_limited`, `auth_rejected`, `not_admin`, `insufficient_scope`, `owner_not_admin`, `minter_not_admin`, `minter_missing`, `other`. |
| `sessionfs_jira_sync_total` | counter | `direction`, `result` (+ tenant `org_id`) | Outbound outbox deliveries (`done`/`skipped`/`failed`/`exhausted`) and verified inbound events (`applied`/`ignored`/`duplicate`). |
| `sessionfs_jira_sync_lag_seconds` | histogram | `direction` (+ tenant `org_id`) | Outbound: queued → delivered. Inbound: Jira event timestamp → applied. |
| `sessionfs_confluence_publish_total` | counter | `result` (+ tenant) | `published`, `in_progress`, `drifted`, `conflict`, `too_large`, `timeout`, `rejected`, `error`. |
| `sessionfs_webhook_ingest_total` | counter | `source`, `result` | `source`: `github`, `gitlab`, `jira`, `delivery_github`, `delivery_gitlab`, `stripe`; `result`: `ok`, `unauthorized`, `not_found`, `too_large`, `rejected`, `unavailable`, `error`. Never tenant-labelled. |
| `sessionfs_agent_tokens_total` | counter | `token_kind`, `model_family` (+ tenant) | Tokens each usage report / completion / worker finish / review round adds. `model_family` is a provider-registry id or `other`. Tokens only — there is no dollar metric. |
| `sessionfs_agent_runs_total` | counter | `status`, `role` (+ tenant) | Runs reaching `passed`/`failed`/`errored`/`cancelled`, by role (`implementer`, `reviewer`, …, `none`, `other`). |
| `sessionfs_approval_requests` | gauge | `status` (+ tenant) | DB-derived: approval requests by state. |
| `sessionfs_approval_decision_seconds` | histogram | `decision` (+ tenant) | Request raised → `approved`/`rejected`. |
| `sessionfs_review_loop_items` | gauge | `status` (+ tenant `project_id`) | DB-derived: review-loop work-queue items by status, including `needs_human`. |
| `sessionfs_org_concurrency_rejected_total` | counter | `route_class` (+ tenant `org_id`) | Requests refused with 429 `org_concurrency_limit`: `extraction`, `helper`, `compile`, `export`. |
| `sessionfs_extraction_server_seconds` | histogram | `kind`, `result` | Meeting/chat extraction time spent in the server: wall time minus the provider call (`kind` `meeting`/`chat`, `result` `done`/`failed`). Feeds the extraction SLO. |

The full list of label values lives in the public [Metrics Reference](../site/src/content/docs/metrics.mdx)
(`https://sessionfs.dev/metrics/`).

The DB-derived gauges (`active_users`, `sessions`, `db_pool_*`,
`approval_requests`, `review_loop_items`) are computed at scrape time and
cached for 30 seconds. They are never kept as in-memory counts, because every
replica must report the same cluster-wide value. The pool gauges are absent
entirely when there is no live pool (e.g. SQLite) rather than reporting zeros.

## Aggregating across replicas

Most metrics are **per-process**: each replica increments its own copy, so
aggregate with `sum()` (or `sum by (...)` for labeled series). A few are
**cluster-wide** — the same value is set on every replica from the database or
configuration, so summing them would over-count by the replica count. Take
`max()` (or `max by (...)`) for those instead:

| Aggregation | Metrics |
|---|---|
| `sum()` / `sum by (...)` | `http_request_duration_seconds`, `sessionfs_push_total`, `sessionfs_sync_pull_total`, `sessionfs_handoff_create_total`, `sessionfs_handoff_claim_total`, `sessionfs_dlp_findings_total`, `sessionfs_auth_failures_total`, `sessionfs_metrics_series_dropped_total`, every `sessionfs_sweep_*` counter/histogram, `sessionfs_jira_sync_*`, `sessionfs_confluence_publish_total`, `sessionfs_webhook_ingest_total`, `sessionfs_agent_*`, `sessionfs_approval_decision_seconds`, `sessionfs_extraction_server_seconds`, `sessionfs_db_pool_checked_out`, `sessionfs_db_pool_overflow`, `sessionfs_db_pool_size` (each process has its own pool, so the cluster capacity is the sum) |
| `max()` / `max by (...)` | `sessionfs_active_users`, `sessionfs_sessions`, `sessionfs_approval_requests`, `sessionfs_review_loop_items`, `sessionfs_sweep_lag_seconds` (set by whichever replica ran the sweep last) |

## Streaming responses

`http_request_duration_seconds` records **time-to-first-headers** for a
streaming response (e.g. a `text/event-stream` route): `call_next` returns once
the response is started, so the duration is the time until headers were ready,
not until the stream finished. This applies to the session helper's streaming
route, `POST /api/v1/helper/turns`.

## Per-tenant labels

Set `SFS_METRICS_TENANT_LABELS=true` (Helm `api.metrics.tenantLabels: true`)
to add `org_id` / `project_id` labels to the families marked "+ tenant" above.
Ids only, never names; a value that isn't id-shaped is dropped. Default off:
the hosted service aggregates across tenants. It is a self-hosted opt-in for
per-team dashboards; series then grow with the number of orgs and projects.

## Cardinality

Every label value is a bounded vocabulary chosen at the call site (business
families clamp anything outside their vocabulary to `other`; a unit test
multiplies every family's vocabularies and asserts the worst case fits the
cap with tenant labels off). A per-metric
series cap (10,000) drops observations past the limit as a backstop against an
accidental unbounded label value; nothing records raw paths, user ids, project
ids or session ids. Dropped samples are counted in
`sessionfs_metrics_series_dropped_total`.

## Dashboard

A ready-to-import Grafana dashboard is at
[`docs/observability/sessionfs-ops.json`](observability/sessionfs-ops.json). It
covers push outcomes, DLP findings, p50/p95 latency by route, active users,
DB pool saturation, sweeps (runs, failures, refusals, lag, duration), Jira sync
and lag, Confluence publishes, webhook ingest, agent tokens and runs, approvals
and review-loop items, plus an SLO row (28-day value of each objective against
its target, error budget remaining, p95 of each SLO'd operation, reminder
delivery lag).

## Service level objectives

Four SLOs are defined on these metrics (28-day window): ticket comment writes
(95% within 500 ms), session pushes (95% within 5 s), meeting/chat extraction
server time excluding the provider (95% within 1 s) and reminder delivery
(99% of 5-minute slices with the `reminders-due` sweep running and no due
reminder older than 15 minutes). Targets, PromQL and alerting are documented
on the public [Service Level Objectives](../site/src/content/docs/slos.mdx)
page (`https://sessionfs.dev/slos/`).

The recording + multi-window burn-rate alerting rules ship as
[`docs/observability/sessionfs-slo-rules.yaml`](observability/sessionfs-slo-rules.yaml)
and, identically, in the chart at `charts/sessionfs/files/slo-rules.yaml`
(a unit test keeps the two in sync). With the Prometheus Operator, set
`api.metrics.prometheusRule.enabled: true` (default off) to render them as a
`PrometheusRule`; `api.metrics.prometheusRule.labels` adds selector labels and
`api.metrics.prometheusRule.reminderSweep: false` drops the reminder group
when the reminders-due sweep is not scheduled.

CI runs an SLO perf smoke (`pytest tests/server/perf -m slo_smoke`, its own
job; excluded from the default run) that drives each operation on the
in-process app and fails when the best-of-three-rounds p95 passes a fixed
budget (budgets and local numbers are in the test module's docstring).
