# API Keys

*v0.10.10+ — scoped service keys for cloud agents; v0.10.11+ — CLI surface.*

SessionFS supports two kinds of API keys:

| Kind | Created via | Scopes | Intended for |
|---|---|---|---|
| **Personal user key** | `sfs auth keys create` (or `POST /api/v1/auth/me/api-keys`) | Inherits the owning user's permissions (`scopes=["*"]`) | Interactive use, scripting, single-developer CI, anywhere a human's authority is the right ceiling. |
| **Scoped service key** | `sfs admin service-keys create` (or `POST /api/v1/orgs/{org_id}/service-keys`) | Enumerated capability scopes (e.g. `handoffs:write`, `tickets:read`) | Cloud agents (Bedrock, Vertex), CI runners (GitHub Actions, GitLab MR), Slack/PagerDuty bots — anywhere a non-human caller acts on behalf of an org. |

The CEO-mandated rule: **service keys are the recommended credential for any non-human caller**, even when a personal key would technically work. The scope vocabulary gives you a least-privilege envelope and audit trail that a wildcard-permissioned personal key cannot match.

---

## Service keys

### When to mint one

At v0.10.11 the live capabilities are **handoff lifecycle**, **CI agent runs**, and **ticket triage** (see [Scope vocabulary](#scope-vocabulary) for the current opt-in status):

- A cloud agent (Bedrock action group, Vertex function calling) that sends and claims handoffs on behalf of users — give it `handoffs:write`.
- A GitHub Actions or GitLab MR runner that reports build/review findings — give it `agent_runs:write`. The same key can also `POST /handoffs/{id}/comments` if you add `handoffs:write`.
- A triage or workflow bot that polls tickets, reads comments/review state, posts triage comments, and moves tickets through start/complete — give it `tickets:read` and `tickets:write`.

For workloads that depend on the remaining reserved scopes (KB writes, persona/rules updates, read-side handoffs/sessions/agent-runs), continue using personal user keys until their Phase 3 route opt-in lands. The bookkeeping is identical — `SESSIONFS_API_KEY` works for either kind — so the eventual migration is just one line per CI job (the mint command's `--scope` flags).

Service keys live on an organization, are minted by an org admin, and are enforced **deny-by-default**: a service key can only call routes that explicitly opted in via `require_scope(...)` and only when one of the route's required scopes is in the key's scope list. Every other route (read-side, dashboard, billing, ungated writes) rejects service keys with `service_key_not_allowed` (see [Errors](#errors) below).

### Scope vocabulary

The 14 capability scopes defined at v0.10.11. The `*` wildcard is **reserved for legacy personal user keys** and is rejected at create time for service keys.

**Route opt-in is incremental.** v0.10.10 shipped the auth machinery (`require_scope(...)` decorator + `AuthContext`) and converted a first wave of write routes. Read-side routes and the rest of the write surface stay on the legacy `get_current_user` dependency for now and reject service keys with `service_key_not_allowed`. The remaining scopes are reserved for the Phase 3 route opt-in (deferred from v0.10.10 to v0.10.11+ — see `tk_e0d7db15ff814c0a` and forward).

| Scope | Status today | Routes that accept it |
|---|---|---|
| `handoffs:write` | ✅ live | `POST /api/v1/handoffs`, `POST /api/v1/handoffs/{id}/claim`, `POST /api/v1/handoffs/{id}/revoke`, `POST /api/v1/handoffs/{id}/decline`, `POST /api/v1/handoffs/{id}/comments` |
| `agent_runs:write` | ✅ live | `POST /api/v1/projects/{project_id}/agent-runs`, `POST /api/v1/projects/{project_id}/agent-runs/{run_id}/complete` |
| `tickets:read` | ✅ live | `GET /api/v1/projects/{project_id}/tickets`, `GET /api/v1/projects/{project_id}/tickets/{ticket_id}`, `GET /api/v1/projects/{project_id}/tickets/{ticket_id}/comments`, `GET /api/v1/projects/{project_id}/tickets/{ticket_id}/review-state` |
| `tickets:write` | ✅ live | `POST /api/v1/projects/{project_id}/tickets/{ticket_id}/comments`, `POST /api/v1/projects/{project_id}/tickets/{ticket_id}/start`, `POST /api/v1/projects/{project_id}/tickets/{ticket_id}/complete` |
| `sessions:read` | reserved | — |
| `handoffs:read` | reserved | — |
| `personas:read` | reserved | — |
| `personas:write` | reserved | — |
| `knowledge:read` | reserved | — |
| `knowledge:write` | reserved | — |
| `rules:read` | reserved | — |
| `rules:write` | reserved | — |
| `agent_runs:read` | reserved | — |
| `retrieval_audit:read` | reserved | — |
| `admin:*` | reserved | — |

Practically, this means today's service keys are useful for **handoff lifecycle automation** (Bedrock/Vertex bots sending and claiming handoffs, GitHub Actions / GitLab MR runners posting handoff comments), **CI agent runs** (test runners reporting findings), and **ticket triage automation** (bots polling tickets, reading review state, posting comments, and moving assigned tickets through start/complete). Reserved scopes are safe to include on a key — they just won't unlock any routes until the Phase 3 opt-in lands. The key reject-by-default posture means a leak today exposes only the live scope surface.

List the live vocabulary from the CLI: `sfs admin service-keys scopes`.

### Mint a service key

Org admin role + Team-or-above tier required.

```bash
sfs admin service-keys create \
  --org org_9e39b81833e6fdd5 \
  --name "github-actions-review-bot" \
  --scope agent_runs:write \
  --scope handoffs:write \
  --expires-days 90
```

Output:

```
Service key created: id=a7e3dbe0-b3d2-4166-be57-1cc837a65205 name=github-actions-review-bot prefix=sk_sfs_584c9...

Raw key: sk_sfs_584c9d12e7f3a8b4c2... (full key)

Save this key now — it will not be shown again.
```

The raw key is returned **exactly once** — on `create` and on `rotate`. Every subsequent read response only exposes `key_prefix` (first 12 chars). The CEO-mandated rule for any non-human caller: capture the raw key into the CI secret store / vault on the same machine that minted it, then never echo it again.

For CI runners, use `--output-key` to emit only the raw key on stdout (no decorations, no warning):

```bash
KEY=$(sfs admin service-keys create \
  --org org_9e39b81833e6fdd5 \
  --name "bedrock-action-group" \
  --scope handoffs:write \
  --expires-days 365 \
  --output-key)

aws secretsmanager put-secret-value \
  --secret-id sessionfs/bedrock-key \
  --secret-string "$KEY"
```

### Optional: per-key project allowlist

By default, a service key can act on any project in its org. Restrict it further with `--project`:

```bash
sfs admin service-keys create \
  --org org_9e39b81833e6fdd5 \
  --name "frontend-team-ci" \
  --scope agent_runs:write --scope handoffs:write \
  --project proj_frontend_a \
  --project proj_frontend_b \
  --expires-days 365
```

The key will be rejected with `project_not_in_allowlist` if it tries to act on `proj_backend`. Useful for siloing CI jobs to a specific repo cluster within a multi-project org.

### Ticket triage example

```bash
TRIAGE_KEY=$(sfs admin service-keys create \
  --org org_9e39b81833e6fdd5 \
  --name "n8n-triage-agent" \
  --scope tickets:read \
  --scope tickets:write \
  --project proj_c0242b0fccbd48b4 \
  --expires-days 90 \
  --output-key)

curl -sS \
  -H "Authorization: Bearer $TRIAGE_KEY" \
  "https://api.sessionfs.dev/api/v1/projects/proj_c0242b0fccbd48b4/tickets?status=open"

curl -sS -X POST \
  -H "Authorization: Bearer $TRIAGE_KEY" \
  -H "Content-Type: application/json" \
  -d '{"content":"Queued by n8n triage.","author_persona":"n8n-triage"}' \
  "https://api.sessionfs.dev/api/v1/projects/proj_c0242b0fccbd48b4/tickets/tk_123/comments"
```

### List, rotate, revoke

```bash
# List all keys in the org (active + revoked)
sfs admin service-keys list --org org_9e39b81833e6fdd5

# Rotate — same scopes/expiry/allowlist, new raw secret
sfs admin service-keys rotate a7e3dbe0-b3d2-4166-be57-1cc837a65205 --org org_9e39b81833e6fdd5

# Revoke — required audit reason
sfs admin service-keys revoke a7e3dbe0-b3d2-4166-be57-1cc837a65205 \
  --org org_9e39b81833e6fdd5 \
  --reason "ci runner decommissioned, replaced by github-actions-review-bot"
```

`rotate` is atomic: the old key flips to `revoked` (with `revoke_reason="rotated by <user_email>"`) in the same transaction the new key is minted. There is no window where both are valid or both are invalid.

`revoke` is a soft delete — the row stays for the audit trail. Rotating a revoked key returns 409 (`cannot rotate revoked key`); create a fresh one instead.

### Rotation policy

The product surface does not enforce a rotation schedule. The CEO recommendation, informed by typical enterprise compliance windows:

- **Long-lived service keys** (Bedrock agents, integration partners): `--expires-days 365`, rotate at the 9-month mark.
- **CI runner keys** that bind to a specific job's lifecycle: `--expires-days 90`, rotate on job decommission.
- **Short-lived bot keys** that you can easily re-mint: `--expires-days 30`.
- **Test keys**: `--expires-days 1`.

A key that has crossed `expires_at` returns `api_key_expired` on every request — no grace period.

---

## Personal user keys

Personal keys are tied to a single user and inherit the wildcard scope (`["*"]`). They can call any route the owning user could call from a logged-in browser session. Use them for interactive scripting, single-developer dev work, and quick one-offs.

```bash
# List your own keys
sfs auth keys list

# Mint one
sfs auth keys create --name "macbook16-laptop" --expires-days 90

# Or for CI capture:
KEY=$(sfs auth keys create --name "personal-ci" --expires-days 30 --output-key)

# Revoke
sfs auth keys revoke c9276ef2-ac53-4f2a-b8d1-... --reason "rotated"
```

The same raw-key-once + `--output-key` semantics as service keys apply.

Why use a service key over a personal key for non-human callers? Three reasons:

1. **Scope reduction.** A service key with `tickets:read, handoffs:write` cannot escalate into your billing, your other orgs, or your KB writes. A personal user key carries the full ambient authority of the user it was minted by.
2. **Org-bound audit.** Live service-key writes today stamp `actor_type=service_key`, `service_key_id`, and `service_key_name` on `HandoffEvent` and `AgentRun` rows. `TicketComment`, `KnowledgeEntry`, and `RetrievalAuditEvent` already carry the same provenance columns ready for the Phase 3 route opt-in, but those write routes are not service-key opted in yet. Personal keys show up as the user across all of these.
3. **Cross-org safety.** Service keys are pinned to an `org_id`. A leaked service key cannot accidentally write into the wrong org.

---

## Errors

Most service-key authorization failures return structured JSON bodies with a stable `error` code field. The CLI surfaces these verbatim (`<code>: <message>`) so you can grep on the code without parsing prose. A few legacy paths (e.g. plain `Invalid API key`) still use FastAPI's unstructured `detail` string — these are called out explicitly in the table below.

| HTTP | Code | When | Remediation |
|---|---|---|---|
| 401 | `api_key_revoked` | Key was explicitly revoked. | Mint a new key (`create`); update your client. |
| 401 | `api_key_expired` | Key's `expires_at` has passed. | Rotate the key (`rotate`); update your client. |
| 401 | (none — `"Invalid API key"`) | Key string doesn't match any row. | Re-paste the key — check for whitespace / truncation. |
| 403 | `service_key_not_allowed` | A service key called a route that doesn't opt in via `require_scope(...)`. | The route is user-only. Use a personal user key, or file a ticket asking for the route to opt in. |
| 403 | `insufficient_scope` | The route requires a scope your key doesn't have. Response includes `required` (array) and `current` (array). | Rotate the key with a wider scope list — service keys can't add scopes in-place. |
| 403 | `cross_org_denied` | Service key tried to access a project in a different org. Response includes `key_org_id`, `project_org_id`. | The key is org-bound. Mint a separate key in the other org if you need cross-org access. |
| 403 | `project_not_in_allowlist` | Service key has a `--project` allowlist and the requested project isn't in it. Response includes `allowed_project_ids`, `requested_project_id`. | Either rotate the key without the allowlist, or expand the allowlist. |
| 403 | `service_key_project_required` | Handoff route needs to anchor on a specific project but couldn't infer one from the request. | Provide an explicit `project_id` in the request, or use a personal key. |
| 403 | `service_key_project_not_registered` | Handoff session anchored on a project that isn't in the org. | The handoff source session belongs to another org — service key cannot bridge orgs. |
| 403 | `service_key_project_ambiguous` | Handoff session could resolve to more than one project via `git_remote_normalized`. | Pre-resolve the project on the caller side and pass `project_id` explicitly. |

The `insufficient_scope` error specifically includes `required` and `current` arrays so a CI runner can diff them and fail with an actionable log line:

```json
{
  "detail": {
    "error": "insufficient_scope",
    "required": ["agent_runs:write"],
    "current": ["tickets:read", "handoffs:read"],
    "message": "Scope required: one of ['agent_runs:write']. Key has: ['tickets:read', 'handoffs:read']."
  }
}
```

---

## Cloud-agent integration recipes

For the v0.10.11 doc cycle, the CLI flow is documented above. The full integration recipes for each platform live in:

- **AWS Bedrock action groups** — `docs/integrations/bedrock-action-group.yaml` (action-group schema) + `docs/integrations/bedrock_lambda.py` (dispatcher Lambda with `OPERATIONS` allowlist)
- **GCP Vertex AI function calling** — `docs/integrations/vertex_tools.py` (function-calling schema + dispatcher)
- **GitHub Actions** — `docs/integrations/github-actions-agent-run.yml` (`SESSIONFS_API_KEY` from secrets context, `--enforce` flag for fail-on-finding gating)
- **GitLab MR** — `docs/integrations/gitlab-agent-run.yml` (per-user webhook secret, comment dedup pattern)
- **Site narrative** — [sessionfs.dev/integrations/cloud-agents](https://sessionfs.dev/integrations/cloud-agents) walks through the dispatcher pattern end-to-end

The migration path: each of those examples currently uses a personal user key with `scopes=["*"]`. They'll be re-cut to use scoped service keys in a separate v0.10.11 ticket — the breaking change is one line (the `--scope` flags on the mint command) plus an env-var rename if you want to make it explicit (`SESSIONFS_API_KEY` works for either kind).

---

## Curl fallback

The CLI wraps these REST endpoints. When debugging or in environments without `sfs`:

```bash
# Mint a service key
curl -X POST "https://api.sessionfs.dev/api/v1/orgs/$ORG_ID/service-keys" \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "github-actions-review-bot",
    "scopes": ["agent_runs:write", "handoffs:write"],
    "expires_in_days": 90
  }'

# List keys
curl "https://api.sessionfs.dev/api/v1/orgs/$ORG_ID/service-keys" \
  -H "Authorization: Bearer $ADMIN_KEY"

# Rotate
curl -X POST "https://api.sessionfs.dev/api/v1/orgs/$ORG_ID/service-keys/$KEY_ID/rotate" \
  -H "Authorization: Bearer $ADMIN_KEY"

# Revoke (DELETE-with-body — note the -X DELETE + JSON payload)
curl -X DELETE "https://api.sessionfs.dev/api/v1/orgs/$ORG_ID/service-keys/$KEY_ID" \
  -H "Authorization: Bearer $ADMIN_KEY" \
  -H "Content-Type: application/json" \
  -d '{"reason": "rotated"}'
```

Personal key endpoints follow the same shapes under `/api/v1/auth/me/api-keys`. See [REST API](https://sessionfs.dev/api/) for the full route list.

---

## See also

- [Org Admin Guide](./org-admin.md) — who can mint service keys, how org admin role works
- [CLI Reference](./cli-reference.md) — `sfs admin service-keys` + `sfs auth keys` complete option tables
- [sessionfs.dev/api](https://sessionfs.dev/api) — full REST API reference including the underlying service-key endpoints
- [sessionfs.dev/integrations/cloud-agents](https://sessionfs.dev/integrations/cloud-agents) — Bedrock + Vertex dispatcher walkthrough
