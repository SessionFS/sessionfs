# Self-Hosted Deployment

Deploy SessionFS on your own Kubernetes cluster.

## Overview

SessionFS can be deployed to any Kubernetes cluster using the official Helm chart. The deployment includes:

- **API Server** -- FastAPI application handling session CRUD, sync, and authentication
- **MCP Server** -- Model Context Protocol bridge (optional, experimental: remote MCP for web clients is not yet supported, so you can leave it disabled with `mcp.enabled=false`; desktop tools use the local `sfs mcp serve`)
- **Web Dashboard** -- React management interface (optional)
- **PostgreSQL** -- Built-in or external database
- **Blob Storage** -- Local PVC, Amazon S3, or Google Cloud Storage

## Prerequisites

- Kubernetes 1.26 or later
- Helm 3.12 or later
- `kubectl` configured for your cluster
- A PersistentVolume provisioner (most managed clusters include one)

## Security Posture

SessionFS ships with a hardened default security posture — no action required on your part. The Helm chart and container images meet CIS Kubernetes benchmark and Pod Security Standard **restricted** profile.

### Container images

Both `sessionfs-api` and `sessionfs-mcp` Docker images:

- Run as **non-root user** (UID 10001, dedicated `sessionfs` system user)
- Ship with no shell or package manager for the runtime user
- Are scanned on every release via `trivy` (CRITICAL/HIGH findings block the pipeline)

### Kubernetes SecurityContext

Every pod declared by the chart — API, MCP, dashboard, PostgreSQL, and the `helm test` hook — runs with:

| Setting | Value |
|---------|-------|
| `runAsNonRoot` | `true` |
| `runAsUser` | `10001` (or `999` for PostgreSQL, matching upstream convention) |
| `readOnlyRootFilesystem` | `true` |
| `allowPrivilegeEscalation` | `false` |
| `capabilities.drop` | `[ALL]` |
| `seccompProfile.type` | `RuntimeDefault` |

The PostgreSQL container mounts `emptyDir` volumes at `/tmp` and `/var/run/postgresql` so it can write its socket directory and temp files even with a read-only root filesystem. The persistent data volume (`/var/lib/postgresql/data`) uses a standard PVC.

### Secrets

All secrets (database credentials, verification secret, encryption key, SMTP credentials, Resend API key) live in Kubernetes Secret objects, never in ConfigMaps. See [Secrets Management](#secrets-management) below.

### Verification

You can verify the security posture of a rendered chart with `trivy`:

```bash
helm template sessionfs sessionfs/sessionfs \
  --namespace sessionfs \
  > /tmp/rendered.yaml

trivy config /tmp/rendered.yaml --severity CRITICAL,HIGH
```

A clean scan should report zero CRITICAL or HIGH misconfigurations.

### Client-Side Privacy Toggle: Rules Provenance

When developer clients capture sessions, SessionFS records instruction provenance in the session manifest — hashes of the rule files and agent instructions that shaped each run. Project-local files (`CLAUDE.md`, `.cursorrules`, etc.) are always hash-only by default; no content is uploaded.

Global rule files (those in a user's home directory, e.g. `~/.claude/CLAUDE.md`) are also hash-only by default. To suppress global-rules hashing entirely for privacy-sensitive environments, set on the **client** (daemon / CLI host):

```bash
export SFS_CAPTURE_GLOBAL_RULES=off
```

This is a client-side env var — the SessionFS server never sees global rule file content either way. See [docs/environment-variables.md](./environment-variables.md#session-capture) for details.

## Installation

### 1. Add the Helm Repository

```bash
helm repo add sessionfs https://charts.sessionfs.dev
helm repo update
```

### 2. Create a Namespace

```bash
kubectl create namespace sessionfs
```

### 3. Choose Your Configuration

#### Minimal (development / evaluation)

Single replica, built-in PostgreSQL, no ingress:

```bash
helm install sessionfs sessionfs/sessionfs \
  --set mcp.enabled=false \
  -f values.minimal.yaml \
  --namespace sessionfs
```

Access via port-forward:

```bash
kubectl port-forward svc/sessionfs-api 8000:8000 -n sessionfs
```

#### Standard

Two API replicas, built-in PostgreSQL, ingress enabled:

```bash
helm install sessionfs sessionfs/sessionfs \
  --set mcp.enabled=false \
  --namespace sessionfs \
  --set ingress.hosts[0].host=sessionfs.yourdomain.com
```

#### Production

External database, cloud storage, autoscaling, network policies:

```bash
helm install sessionfs sessionfs/sessionfs \
  -f values.production.yaml \
  --namespace sessionfs \
  --set postgresql.enabled=false \
  --set externalDatabase.existingSecret=sessionfs-db \
  --set security.existingSecret=sessionfs-secrets \
  --set storage.type=s3 \
  --set storage.s3.bucket=my-sessionfs-bucket \
  --set ingress.enabled=true \
  --set ingress.className=nginx \
  --set ingress.hosts[0].host=sessionfs.yourdomain.com \
  --set ingress.hosts[0].paths.api=/api \
  --set ingress.hosts[0].paths.dashboard=/ \
  --set mcp.enabled=false
```

## Secrets Management

SessionFS requires several secrets for operation. You can either provide them inline in `values.yaml` (not recommended for production) or reference pre-existing Kubernetes secrets.

`verification-secret` and `encryption-key` encrypt stored LLM keys and GitLab/Jira tokens, so they must never change once set. If you leave them empty, the chart generates them on first install and keeps the same values on every later `helm upgrade`. ArgoCD and other `helm template` flows can't read the cluster, so for GitOps set both values or use `security.existingSecret`. Charts before 0.24.0 generated new values on every upgrade; if stored keys stopped working after an upgrade, re-enter them once.

The API refuses to store or read encrypted secrets when `SFS_VERIFICATION_SECRET` is unset or a placeholder such as `dev-secret` (503 `encryption_unavailable`), except in development (`SFS_ENVIRONMENT=development`, or a SQLite database).

### Create Secrets Manually

```bash
# Application secrets
kubectl create secret generic sessionfs-secrets \
  --namespace sessionfs \
  --from-literal=verification-secret="$(openssl rand -hex 32)" \
  --from-literal=encryption-key="$(openssl rand -hex 32)" \
  --from-literal=resend-api-key="re_your_key_here"

# External database (if not using built-in PostgreSQL)
kubectl create secret generic sessionfs-db \
  --namespace sessionfs \
  --from-literal=database-url="postgresql+asyncpg://user:pass@host:5432/sessionfs"
```

Then reference them in your Helm values:

```yaml
security:
  existingSecret: sessionfs-secrets
externalDatabase:
  existingSecret: sessionfs-db
```

## Storage Configuration

### Local (default)

Uses a PersistentVolumeClaim. Suitable for single-node clusters or evaluation.

```yaml
storage:
  type: local
  local:
    persistence:
      enabled: true
      size: 10Gi
```

### Amazon S3

```yaml
storage:
  type: s3
  s3:
    bucket: my-sessionfs-bucket     # Bucket name only — no slashes
    region: us-east-1
    prefix: ""                       # Optional key prefix (e.g. "sessionfs/")
```

**Using IRSA (IAM Roles for Service Accounts):**

If your EKS nodes use IRSA, no additional credentials are needed. Attach this IAM policy to the service account role:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ],
      "Resource": [
        "arn:aws:s3:::my-sessionfs-bucket",
        "arn:aws:s3:::my-sessionfs-bucket/*"
      ]
    }
  ]
}
```

Annotate the service account in your Helm values:

```yaml
serviceAccount:
  create: true
  annotations:
    eks.amazonaws.com/role-arn: arn:aws:iam::123456789012:role/sessionfs-s3-role
```

**Using static credentials:**

```bash
kubectl create secret generic aws-creds \
  --namespace sessionfs \
  --from-literal=aws-access-key-id=AKIA... \
  --from-literal=aws-secret-access-key=...
```

```yaml
storage:
  s3:
    existingSecret: aws-creds
```

### Google Cloud Storage

```yaml
storage:
  type: gcs
  gcs:
    bucket: my-sessionfs-bucket
```

If using Workload Identity, no additional credentials are needed. Otherwise:

```bash
kubectl create secret generic gcs-creds \
  --namespace sessionfs \
  --from-file=gcs-credentials-json=./sa-key.json
```

```yaml
storage:
  gcs:
    existingSecret: gcs-creds
```

## Database Configuration

### Built-in PostgreSQL (default)

The chart deploys a single-replica PostgreSQL StatefulSet. Suitable for small deployments.

```yaml
postgresql:
  enabled: true
  auth:
    username: sessionfs
    database: sessionfs
  persistence:
    size: 10Gi
```

### External Database

For production, use a managed PostgreSQL service (AWS RDS, GCP Cloud SQL, Azure Database for PostgreSQL).

```yaml
postgresql:
  enabled: false

externalDatabase:
  host: your-db-host.region.rds.amazonaws.com
  port: 5432
  username: sessionfs
  database: sessionfs
  existingSecret: sessionfs-db
```

**Important:** Do NOT add `sslMode` or `?sslmode=require` to the connection URL. SessionFS handles SSL negotiation automatically via asyncpg. For RDS and Cloud SQL, SSL is negotiated transparently for non-localhost connections.

## TLS / HTTPS

Configure TLS through your ingress controller. Example with cert-manager:

```yaml
ingress:
  enabled: true
  className: nginx
  annotations:
    cert-manager.io/cluster-issuer: letsencrypt-prod
  hosts:
    - host: sessionfs.yourdomain.com
      paths:
        api: /api
        dashboard: /
  tls:
    - secretName: sessionfs-tls
      hosts:
        - sessionfs.yourdomain.com
```

## Monitoring

Enable Prometheus ServiceMonitor (requires prometheus-operator):

```yaml
monitoring:
  serviceMonitor:
    enabled: true
    interval: 30s
    labels:
      release: prometheus
```

### Operator metrics

The API serves Prometheus metrics at `GET /api/v1/admin/metrics` (request latency by route, push outcomes, DLP findings, auth failures, active users, DB pool use). The endpoint is not anonymous: scrape it with a platform-admin user key or a service key with the `admin_metrics:read` scope. Set `api.metrics.scrapeAnnotations: true` to add `prometheus.io/*` scrape annotations to the API pods, and supply the Bearer token in your Prometheus scrape config. See [Operator metrics](observability.md) for the metric reference, how to aggregate across replicas, and a ready-to-import Grafana dashboard.

## Scheduled KB compile sweep

Projects keep a per-project compile schedule (`nightly` or `weekly`) and an
optional pending-claim threshold (`compile_after_claims`), set from the
dashboard's project config or with `sfs project compile-schedule`. The server
does not run its own scheduler: an operator cron calls the sweep endpoint,
which compiles every project that is due and does nothing for the rest.

```bash
curl -s -X POST https://your-api/api/v1/admin/sweeps/kb-compile \
  -H "Authorization: Bearer $SWEEP_KEY" \
  -H "Content-Type: application/json" \
  -d '{"limit": 100}'
```

- **Auth** — a platform-admin user key or a `platform_sweeps:run` service key
  (see [API keys](./api-keys.md#service-keys)). The service key is preferred
  for cron: it works only while the platform admin who minted it and the user
  it runs as are both still active platform admins.
- **Body** — `{"limit": N}` caps how many projects are compiled (default 100,
  max 1000).
- **Selection** — a project is due when it has at least one pending claim and
  either its schedule has elapsed since its last scheduled compile (a project
  never compiled is immediately due), or its pending-claim threshold is
  reached. The threshold fires regardless of schedule, so `off` still compiles
  once enough claims accumulate. Projects with no pending claims are skipped.
- **Grandfathered projects are not swept** — a project grandfathered under
  migration 065 (it used `project_context` before v0.17.2, recorded in
  `ProjectFeatureGrandfather`) is never auto-compiled. The exemption only lets
  a settings editor whose own plan includes the feature keep editing the
  schedule; the sweep resolves the project's own plan alone, so a
  grandfathered project whose plan lacks the feature stays out of the sweep.
- **Deterministic only** — the sweep always uses the deterministic compiler,
  never the LLM.
- **Isolation and idempotency** — each project compiles in its own transaction,
  so one failure does not stop the batch (it is reported in `errors` and the
  sweep returns `{selected, compiled, skipped_no_pending, failed, errors}`).
  Re-running is safe: a compiled project advances its
  `last_scheduled_compile_at` and is not selected again until its schedule
  lapses.

Run it on a schedule. For example, a Kubernetes CronJob every 15 minutes:

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: sessionfs-kb-compile
  namespace: sessionfs
spec:
  schedule: "*/15 * * * *"
  jobTemplate:
    spec:
      template:
        spec:
          restartPolicy: OnFailure
          containers:
            - name: sweep
              image: curlimages/curl:latest
              args:
                - -s
                - -X
                - POST
                - https://your-api/api/v1/admin/sweeps/kb-compile
                - -H
                - "Authorization: Bearer $(SWEEP_KEY)"
                - -H
                - "Content-Type: application/json"
                - -d
                - '{"limit": 100}'
              env:
                - name: SWEEP_KEY
                  valueFrom:
                    secretKeyRef:
                      name: sessionfs-sweep-key
                      key: token
```

The same mechanism drives the dashboard's "compile needed" banner: when a
project has a pending claim and its last compile is older than 24 hours (or 20
or more pending claims), the dashboard offers a one-click compile. The banner
is advisory — the sweep above is what actually keeps projects fresh.

## Scheduled helper retention sweep

Session-helper messages expire **per message**, not per conversation. A message
is deleted once it is older than the conversation org's retention window —
`general.helper_retention_days` in the org's settings JSON (default **90**,
allowed **1–3650**) — or the 90-day default for personal threads. Conversations
left with no messages are then purged. As with the KB compile sweep, the server
does not run its own scheduler: an operator cron calls the sweep endpoint.

```bash
curl -s -X POST https://your-api/api/v1/admin/sweeps/helper-retention \
  -H "Authorization: Bearer $SWEEP_KEY"
```

- **Auth** — a platform-admin user key or a `platform_sweeps:run` service key
  (see [API keys](./api-keys.md#service-keys)).
- **Retention window** — set per org via `PUT /api/v1/orgs/{org_id}/settings`
  with a flat body `{"helper_retention_days": 30}` (the route merges into the
  stored general settings, so fields you omit are preserved; send an explicit
  `"helper_retention_days": null` to clear the override and revert to the 90-day
  default). The dashboard does not edit this setting yet.
- **Batching** — deletes run in bounded batches of 1000 rows and page through
  candidates with a keyset, so a large backlog is drained without one unbounded
  transaction or loading every expired id into memory.

Run it on a schedule with a CronJob, mirroring the [KB compile sweep
example](#scheduled-kb-compile-sweep), or let the chart create one. Mint a
`platform_sweeps:run` service key, store it in a Secret, and enable the
built-in CronJob (off by default; it runs daily at 03:00 and, when network
policies are enabled, the chart allows it to reach the API):

```yaml
api:
  helperRetention:
    enabled: true
    cronJob:
      schedule: "0 3 * * *"
      existingSecret: sessionfs-sweep-key     # required when enabled
      secretKey: helper-retention-api-key
```

## LLM connection tests against private hosts

The judge / helper key forms have a **Test connection** button
(`POST /api/v1/settings/judge/test`, `POST /api/v1/orgs/{org_id}/llm-settings/test`).
With `SFS_LLM_ALLOW_PRIVATE_HOSTS` on (the Helm default, `api.llm.allowPrivateHosts`)
a user can point it at an in-cluster address, so results for targets that
resolve to a non-public address are deliberately coarse, to keep the test from
being used to map the internal network:

- `ok` and `invalid_key` are reported as usual (you need them to set up a
  self-hosted model).
- Timeouts, connect failures and "not an API" replies (an HTML page or a 404
  from some other internal service) all read `unreachable`.
- No snippet of the upstream error is returned, and `latency_ms` is null.
- Every failure takes at least 15 seconds, so timing can't tell a refused port
  from a filtered one.

Public targets get the detailed result. Metadata, link-local and unspecified
addresses stay blocked in every mode (`endpoint_blocked`).

## Upgrading

```bash
helm repo update
helm upgrade sessionfs sessionfs/sessionfs \
  --namespace sessionfs \
  --reuse-values
```

Database migrations run automatically as a Helm pre-upgrade hook.

## Troubleshooting

### Check pod status

```bash
kubectl get pods -n sessionfs
kubectl describe pod <pod-name> -n sessionfs
```

### View API logs

```bash
kubectl logs -n sessionfs -l app.kubernetes.io/component=api --tail=100
```

### Check migration job

```bash
kubectl get jobs -n sessionfs -l app.kubernetes.io/component=migration
kubectl logs -n sessionfs job/sessionfs-migrate-<revision>
```

### Run Helm tests

```bash
helm test sessionfs --namespace sessionfs
```

### Common Issues

**Pods stuck in Pending:** Check that your cluster has a PersistentVolume provisioner and sufficient resources.

**Database connection errors:** Verify the database URL and credentials. For external databases, ensure network connectivity (security groups, VPC peering).

**Ingress not working:** Confirm your ingress controller is installed and the ingress class name matches.

**Security context errors (runAsNonRoot):** SessionFS images currently run as root. Set security contexts in values.yaml:

```yaml
api:
  podSecurityContext:
    runAsNonRoot: false
```

**asyncpg SSL errors:** Do not add `?sslmode=require` to the database URL — SessionFS handles SSL parameter translation internally. For RDS/Cloud SQL, asyncpg negotiates SSL automatically for non-localhost connections.

## API Documentation

After deployment, the API is documented via OpenAPI:

- **Swagger UI:** `https://your-domain/api/docs`
- **ReDoc:** `https://your-domain/api/redoc`
- **OpenAPI JSON:** `https://your-domain/api/openapi.json`

Key endpoints:

| Endpoint | Description |
|----------|-------------|
| `POST /api/v1/auth/signup` | Create account |
| `POST /api/v1/auth/login` | Get token |
| `GET /api/v1/auth/me` | Current user profile |
| `GET /api/v1/sessions` | List sessions |
| `POST /api/v1/sessions/{id}/audit` | Run LLM Judge |
| `GET /health` | Health check |

## Email Configuration

SessionFS supports three email providers: Resend (SaaS), SMTP (enterprise), or none (air-gapped).

### Resend (default for cloud)

```bash
helm install sessionfs sessionfs/sessionfs \
  --set mcp.enabled=false \
  --set email.provider=resend \
  --set email.resend.apiKey=$RESEND_KEY
```

### SMTP (enterprise / internal)

```bash
helm install sessionfs sessionfs/sessionfs \
  --set mcp.enabled=false \
  --set email.provider=smtp \
  --set email.smtp.host=smtp.company.internal \
  --set email.smtp.port=587 \
  --set email.smtp.username=sessionfs \
  --set email.smtp.password=$SMTP_PASS \
  --set email.fromAddress=sessionfs@company.com
```

For implicit SSL (port 465):

```bash
  --set email.smtp.port=465 \
  --set email.smtp.ssl=true \
  --set email.smtp.tls=false
```

### No email (air-gapped)

```bash
helm install sessionfs sessionfs/sessionfs \
  --set mcp.enabled=false \
  --set email.provider=none \
  --set api.env.SFS_REQUIRE_EMAIL_VERIFICATION=false
```

Users will be auto-verified on signup. Email notifications (handoff, retention) will be logged but not sent.

### Weekly org activity digest

A weekly email summarizing each Team/Enterprise org's activity (sessions,
tickets, agent runs, KB claims, handoffs, admin actions) is sent to that
org's owners and admins. Each admin can opt out individually from the
dashboard (org settings → Notifications) or via the API. The digest is sent
by an external scheduled job that calls a key-authenticated sweep endpoint —
the server has no scheduler of its own:

```text
POST /api/v1/admin/sweeps/org-digest
```

Authenticate with a platform-admin user key, or a `platform_sweeps:run`
service key minted by a platform admin. The sweep is idempotent per org per
week (`org_digest_runs`), so a duplicate schedule tick is safe: a second run
in the same week sends nothing. But the digest always summarizes the most
recently *completed* week, so a missed weekly tick permanently loses that
week unless the sweep runs again before the following Monday. Run the job
daily (or more often) — it is idempotent, so the extra runs are free and
only the first successful run of a given week sends.

With `email.provider=none` the sweep is a no-op for delivery but still
records each org's run, so self-hosted air-gapped deployments can keep the
job enabled (and the audit ledger accurate) without an SMTP or Resend
provider. Digest links resolve to `SFS_DASHBOARD_URL` when set, falling back
to `SFS_APP_URL` (the same origin invite emails use), and are omitted when
neither is set, so self-hosted installs never link recipients to the hosted
dashboard.

### Ticket notification email digest

Users who follow tickets accumulate unread ticket notifications as those
tickets change state, change hands, or pick up comments. A separate sweep
emails each user one digest per run instead of an email per notification,
sent by an external scheduled job that calls a key-authenticated endpoint —
the server has no scheduler of its own:

```text
POST /api/v1/admin/sweeps/ticket-notifications
```

Authenticate with a platform-admin user key, or a `platform_sweeps:run`
service key minted by a platform admin. Each run selects unread, un-emailed
notifications older than 10 minutes (the delay lets a burst of changes
coalesce into one row first) and groups them by user, so a user receives a
single digest listing every pending notification. Delivery is idempotent via
an `emailed_at IS NULL` rowcount guard, so a duplicate schedule tick is safe;
a delivery that fails releases its reservation (`emailed_at` is reset) so a
later run retries it.

Each user's **ticket email** preference gates delivery, set from the
dashboard (**Settings → Preferences**) or with
`PUT /api/v1/me/notification-settings` (`{"ticket_email": ...}`):

- `off` — never email (notifications still appear in the dashboard inbox and
  `sfs ticket inbox`).
- `your_turn` — the default: email only when at least one pending
  notification is a "your turn".
- `all` — email every batch.

Access is re-checked per row against the ticket's current project, so a user
who lost access since the notification was written is never emailed its
content; those rows are marked emailed (never delivered) so they do not
resurface. With `email.provider=none` the sweep is a no-op for delivery but
still marks rows emailed and logs the suppressed send, mirroring the org
digest. Digest links resolve to `SFS_DASHBOARD_URL` when set, falling back to
`SFS_APP_URL`, and are omitted when neither is set.

### Jira sync and Confluence publish sweeps

The Jira and Confluence integrations (v0.26.0) each need a scheduled job.
Like the other sweeps, they are key-authenticated endpoints called by an
external scheduler; authenticate with a platform-admin user key or a
`platform_sweeps:run` service key.

```text
POST /api/v1/admin/sweeps/jira-sync            # every minute
POST /api/v1/admin/sweeps/confluence-publish   # every 15 minutes
```

**Jira sync.** Status moves, comments and completion evidence for linked
tickets are written to an outbox in the same transaction as the change and
delivered right after it commits. The sweep delivers whatever that first
attempt missed (a Jira outage, a restart mid-delivery) with backoff of 1, 4,
16 and 64 minutes, giving up after 5 attempts, and expires finished rows
after 90 days. Run it every minute so the first retry lands on time. Body
`{"limit": N, "budget_seconds": S}` bounds a run (defaults 100 events and
50 seconds; max 1000 and 120). Each event is claimed and committed on its
own, so overlapping runs never deliver twice. Without this job, a change
whose first delivery fails never reaches Jira.

**Confluence publish.** Republishes the space mappings that opted in (the
project context tree, and wiki pages marked for publishing) when their
content changed and the connection's batch window has passed. It never calls
an LLM, skips unchanged sources without contacting Confluence, and never
overwrites a page edited in Confluence (it reports drift instead). Body
`{"limit": N}` (default 100, max 1000). Every 15 minutes is enough; run it
more often only if you lower the batch window.

Both jobs are safe to run on installs that don't use the integration: with
no connections configured they return immediately.

### SMTP with Kubernetes Secrets

```bash
kubectl create secret generic smtp-creds \
  --namespace sessionfs \
  --from-literal=username=sessionfs \
  --from-literal=password=$SMTP_PASS

helm install sessionfs sessionfs/sessionfs \
  --set mcp.enabled=false \
  --set email.provider=smtp \
  --set email.smtp.host=smtp.company.internal \
  --set email.smtp.existingSecret=smtp-creds
```

## Database Migrations

### Automatic (Helm)

Migrations run automatically on `helm install` and `helm upgrade`. On upgrades the migration job is a pre-upgrade hook that finishes before new API pods start (hook weight `-5`). On a fresh install the job is created alongside the database and its credentials and retries until the database is ready; API pods report ready only once the schema is migrated, so new code never serves against an old schema.

### ArgoCD

If you deploy SessionFS with ArgoCD (through its Helm source, or by rendering the chart with `helm template`), the chart orders the rollout with ArgoCD sync waves: the database and its credentials come up first, then the migration job runs, then (in local license mode) the license is seeded, and only after those succeed do the API and MCP deployments roll to the new version.

Nothing to configure: the annotations ship in the chart. As a safety net, API pods only report ready (`/health/ready`) once the database schema has been migrated for their version, so they never take traffic against an old schema, and the API logs a clear error at startup if the schema does not match. `/health/ready` returns `200 {"status":"ready","schema":"current"}` (or `"ahead"` after an image rollback) and `503 {"status":"not_ready","schema":"behind"}` (or `"missing"`, or `"unknown"` while a new pod cannot reach the database). It is served on the API pod and, through the chart's ingress, at `https://<your-host>/health/ready`; `/health` on the ingress host is the dashboard's own liveness check.

If you ever run `alembic upgrade head` by hand, restart the API deployment afterwards (`kubectl rollout restart deployment/sessionfs-api`) so its database connections pick up the new schema.

### Manual

```bash
kubectl exec -it deploy/sessionfs-api -- alembic upgrade head
```

### Troubleshooting Migrations

If the migration job fails, check logs:

```bash
kubectl logs job/sessionfs-migrate-<revision> -n sessionfs
```

If the schema does not match the running code (for example, a migration was skipped), the API logs a clear error at startup naming both revisions and the fix. Check the API logs if authenticated routes start failing with "column ... does not exist".

## Auditing Operator Overrides

Operators sometimes need to act without the normal consent flows: move a user's personal project into the organization while the owner is unavailable, merge a duplicate account, or change a tier. Every platform-admin action the API performs writes a row to the `admin_actions` table with the admin, the time, an optional reason and, for overrides, the state before and after the change. Use the paved paths below so the audit trail answers "who changed this, when, and why" without reconstructing terminal history.

**Move a project into an organization** (the owner does not need to initiate or accept):

```bash
sfs admin override project-transfer proj_abc123 --to-org org_xyz --expect-from personal \
  --reason "Owner on leave; care team needs access (ticket OPS-412)"
```

The move, an accepted transfer record, the audit entry and organization audit events commit in one transaction. Merged projects are refused, open work assigned to org personas the destination lacks is refused unless `--force`, and any pending transfer for the project is cancelled. The owner of an organization project must be a member of that organization: if the current owner is not (for example, they have left), add `--new-owner <user-id>` with an active member, and ownership changes in the same transaction. `--expect-from` makes a retried command safe: it refuses if the project is no longer where you expected it.

**Merge a duplicate account.** For a duplicate signup (for example a typo in the email), preview and then apply the merge instead of reassigning rows by hand:

```bash
sfs admin override user-merge <duplicate-id> <kept-id> --reason "Typo in email domain (ticket OPS-77)"          # dry run
sfs admin override user-merge <duplicate-id> <kept-id> --reason "Typo in email domain (ticket OPS-77)" --apply
```

This calls `POST /api/v1/admin/users/{source_id}/merge-into/{target_id}`. The dry run lists the rows that would move; `--apply` reassigns them, deduplicates org memberships (higher role wins), revokes the duplicate's API keys, deactivates it and writes one `user_merge` audit entry, all in one transaction. It refuses when both accounts hold an active entitlement or the duplicate owns marketplace assets or has billing records; resolve those first. See the [CLI reference](./cli-reference.md#sfs-admin-override-user-merge).

**Merge duplicate projects.** When the same repository ended up as two projects, the owner can run `sfs project merge`. When the twins belong to different users, merge them as an operator:

```bash
sfs admin override project-merge <duplicate-project-id> <kept-project-id> --reason "Duplicate from an ssh:// remote (OPS-81)"          # dry run
sfs admin override project-merge <duplicate-project-id> <kept-project-id> --reason "Duplicate from an ssh:// remote (OPS-81)" --apply
```

Everything moves to the kept project and the duplicate becomes a redirect to it, with one `project_merge` audit entry. Since v0.22.0 the server stores one canonical remote for every URL form of a repository (`ssh://`, `git://`, scp remotes with any user, trailing slashes), so new duplicates of this kind no longer appear. Upgrading runs migration 083, which rewrites stored remotes to the canonical form and prints any project it left alone because its canonical remote already belongs to another project: those are the pairs to merge. See the [CLI reference](./cli-reference.md#sfs-admin-override-project-merge).

**Record a change made by hand.** When you have to change data directly in the database, record it afterwards:

```bash
sfs admin override record --action user_merge --target-type user --target-id <kept-user-id> \
  --reason "Duplicate signup with a typo in the email domain; 62 rows reassigned" \
  --before-file before.json --after-file after.json
```

Recorded entries are stored with source `operator_record`: the server attests who recorded them and when, not that the described change happened.

**Review the log:**

```bash
sfs admin audit-log --target-type project --target-id proj_abc123
sfs admin audit-log --source operator_record --json
```

`PUT /api/v1/admin/users/{id}/tier`, `PUT /api/v1/admin/orgs/{id}/tier` and `POST /api/v1/admin/orgs/{id}/force-transfer-owner` also accept an optional `reason` in their request body and record the before and after values. The audit log does not protect against someone with direct database access; restrict who holds database credentials, and use database-level logging if you need that guarantee.

## Rate Limiting

Rate limiting is per API key, defaulting to 120 requests per minute.

```yaml
api:
  rateLimitPerMinute: 120    # Requests per minute per API key
  # Set to 0 to disable rate limiting (recommended for internal deployments)
```

Or via environment variable:

```yaml
api:
  env:
    SFS_RATE_LIMIT_PER_MINUTE: "0"   # Disable for internal deployments
```

## Architecture

SessionFS uses a reverse proxy architecture. All external traffic routes through the dashboard nginx, which proxies `/api/` and `/mcp/` to internal ClusterIP services:

```
Internet -> Ingress -> Dashboard Nginx -> API Service (ClusterIP)
                                       -> MCP Service (ClusterIP)
                                       -> Static Files
```

The ingress template routes everything to the dashboard service. Do NOT create separate ingress rules for `/api` or `/mcp` — this causes ALB target groups to stay in "unused" state.

The session helper streams its answers as Server-Sent Events from `POST /api/v1/helper/turns`. The chart's dashboard nginx passes that route through unbuffered with a 600-second read timeout, so answers appear as they are written. If you put another proxy or load balancer in front of the chart, turn off response buffering for that path and allow long-lived responses there too (the API sends a keep-alive comment every 15 seconds).

### Upload size limit

The dashboard nginx has `client_max_body_size` configured (default 100MB). Override in values.yaml:

```yaml
dashboard:
  clientMaxBodySize: "200m"
```

### Upload time limit

Pushing a large session waits for the server to DLP-scan and store it. The dashboard nginx waits up to `apiProxyTimeout` (whole seconds, default `300s`) for the API, and the CLI and daemon wait up to `SFS_UPLOAD_TIMEOUT` seconds (default 300). If very large sessions time out, raise both together and keep any load balancer idle timeout in front of the chart at least as long:

```yaml
dashboard:
  apiProxyTimeout: "600s"
```

```bash
export SFS_UPLOAD_TIMEOUT=600   # or upload_timeout = 600 under [sync] in ~/.sessionfs/config.toml
```

The chart's Ingress carries matching `nginx.ingress.kubernetes.io/proxy-read-timeout`, `proxy-send-timeout` and `proxy-body-size` annotations by default, taken from `dashboard.apiProxyTimeout` and `dashboard.clientMaxBodySize`, so ingress-nginx doesn't stop at its own 60-second default. Anything you set under `ingress.annotations` takes precedence. Other ingress controllers ignore these annotations; set their timeout yourself. On an AWS ALB, for example:

```yaml
ingress:
  annotations:
    alb.ingress.kubernetes.io/load-balancer-attributes: idle_timeout.timeout_seconds=300
```

## GitLab Integration

SessionFS supports GitLab merge request comments (cloud and self-hosted instances).

### Setup

1. Create a GitLab personal or project access token with `api` scope
2. Add a webhook to your GitLab project:
   - URL: `https://your-sessionfs-domain/webhooks/gitlab`
   - Secret token: set in your Helm values or `SFS_GITLAB_WEBHOOK_SECRET` env var
   - Events: "Merge request events"
3. Save your access token and instance URL in the dashboard under **Settings → Integrations → GitLab** (the API alternative is `PUT /api/v1/settings/gitlab`), and set the webhook secret via Helm:

```yaml
api:
  env:
    SFS_GITLAB_WEBHOOK_SECRET: "your-webhook-secret"
```

GitLab MR comments include the same AI session context as GitHub PR comments, including audit findings when available.

## CI/CD Pipelines

The following GitHub Actions pipelines handle deployment:

| Workflow | Trigger | Target |
|----------|---------|--------|
| Deploy API | Push to main (server code changes) | Cloud Run |
| Deploy MCP Server | Manual (`workflow_dispatch`) while the hosted MCP is parked | Cloud Run |
| Deploy Dashboard | Push to main (`dashboard/` changes) | Vercel |
| Deploy Site | Push to main (`site/` changes) | Vercel |
| Publish Container Images | After Release workflow | GHCR |

## Environment Variables

See [Environment Variables Reference](environment-variables.md) for the complete list of `SFS_*` configuration options.

## Further Troubleshooting

See [Troubleshooting Guide](troubleshooting.md) for common errors, sync debugging, and Kubernetes-specific issues.
