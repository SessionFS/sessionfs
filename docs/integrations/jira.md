# Jira Cloud integration

SessionFS promotes tickets to Jira Cloud stories. The integration is strictly
**one-way**: SessionFS creates or updates the Jira issue from the ticket, and
reads the issue's status on demand. Edits made in Jira never write back into
the SessionFS ticket — the ticket remains the source of truth.

The integration is **Enterprise** only, behind the `jira_integration` feature
flag. It is available from the dashboard, the CLI and the API.

## What it does

A user with write access to a ticket can draft and publish a Jira story:

- **Preview** builds a draft story from the ticket without touching Jira.
- **Publish** creates the issue on first publish and updates its summary and
  description on later publishes. Status, assignee, and sprint are never
  changed.
- **Status** shows the linked issue key, URL, and current Jira status (cached
  for 60 seconds).
- **Unlink** removes the local link; the Jira issue itself is left untouched.

Each ticket links to **at most one** Jira issue (`ticket_id` is the primary key
of `ticket_tracker_links`). Publishing again updates the existing issue rather
than creating a duplicate.

Publishing is safe to retry. Each created issue carries a
`sessionfs-<ticket_id>` label. If Jira does not answer in time (`504
jira_timeout`), the link is kept in an *unknown* state and the next publish
searches Jira for that label and adopts the issue it finds before creating a
new one. Two publishes of the same ticket at once cannot both create an issue
(`409 publish_in_progress` / `publish_superseded`), and a description that
would exceed Jira's 32,000-character limit is refused before anything is sent
(`422 content_too_large_for_jira`). If the org reconnects to a different Jira
site, existing links are reported as stale (`409 jira_link_stale`) and must be
unlinked before the ticket can be published again.

## Setup

1. **Connect the org** (org admin). The credentials are validated against
   Jira (`GET /rest/api/3/myself`) and the API token is stored Fernet-encrypted
   at rest — it is never returned by any route or written to audit rows.

   ```bash
   sfs org jira connect \
     --site-url https://acme.atlassian.net \
     --email jira@acme.com \
     --template agile
   ```

2. **Map projects** to Jira projects via
   `PUT /api/v1/orgs/{org_id}/integrations/jira/mappings/{project_id}`. Each
   mapping carries the Jira project key, issue type (default `Story`), an
   optional template override, and an optional "done" status name.

3. **Promote tickets** from the ticket detail panel (**Promote to Jira** opens
   a preview you can edit before publishing; a linked ticket shows its issue
   key and an **Update Jira** button), with
   `sfs ticket jira preview|publish|status|unlink`, or through the API
   endpoints under `/api/v1/projects/{pid}/tickets/{tid}/jira`.

In the dashboard, org owners and admins manage the connection, project
mappings and integration health from the **Integrations** tab of the
Organization page.

## Templates

A template is an ordered list of sections. Each section is either:

- **verbatim** — copied exactly from a ticket field (acceptance criteria,
  completion notes, linked sessions); never taken from the model; or
- **rewrite** — filled by joining raw source fields, then reshaped by the
  configured LLM key when one exists.

Built-in templates: `agile` (Agile Standard), `compliance`
(Enterprise/Compliance), `sre` (Platform/SRE), `bug` (Bug Report). Custom
templates are a later piece. The org admin picks a default template when
connecting; a project mapping may override it.

## Promotion and the citation rule

The ticket is the source of truth. The deterministic draft fills every section
from its source fields. When the org or user has an LLM key, only the
`rewrite` sections are sent to the model, which must return strict JSON whose
paragraphs each carry a citation back to a real source span:

- `description:L3-L5` — 1-indexed, inclusive description line range;
- `acceptance_criteria[0]` — 0-indexed acceptance-criteria entry; or
- a named field: `title`, `priority`, `kind`, `persona`, `completion_notes`,
  `linked_sessions`.

Model output is untrusted. Unknown section keys are dropped, invalid JSON is
discarded, and any paragraph whose citation does not resolve to a real source
span is dropped. Verbatim sections are never taken from the model, and sections
with no surviving paragraphs fall back to the deterministic draft. Source text
is DLP-redacted against the org's DLP policy before it is sent to the model.

## Security

- **Token encryption** — the API token is Fernet-encrypted at rest using the
  same derivation as judge settings. No route, response, or audit row ever
  contains the token.
- **SSRF control** — the site URL must match the strict allowlist
  `https://<site>.atlassian.net` (lowercase alphanumerics and hyphens only,
  no ports/paths/userinfo), redirects are never followed, and requests time
  out after 20 seconds.
- **Error sanitization** — upstream error bodies are scrubbed (the token and
  request headers are never logged or returned) before leaving the client.
- **DLP redaction** — source text is redacted before any model call.
- **Authorization** — org-scoped routes check membership against the target
  org and gate on the org's plan; ticket routes gate on the project's org and
  reuse the existing `tickets:read` / `tickets:write` scopes.

## API surface

| Method | Path | Purpose |
|--------|------|---------|
| GET/PUT/DELETE | `/api/v1/orgs/{org_id}/integrations/jira` | Read/upsert/remove the connection |
| GET | `/api/v1/orgs/{org_id}/integrations/jira/projects` | List Jira projects + issue types |
| GET/PUT/DELETE | `/api/v1/orgs/{org_id}/integrations/jira/mappings[/{project_id}]` | Read/upsert/remove mappings |
| GET | `/api/v1/orgs/{org_id}/integrations/jira/health` | Unmapped count, failures, stale links |
| POST | `/api/v1/projects/{pid}/tickets/{tid}/jira/preview` | Draft the story |
| POST | `/api/v1/projects/{pid}/tickets/{tid}/jira/publish` | Create/update the Jira issue |
| GET | `/api/v1/projects/{pid}/tickets/{tid}/jira` | Linked issue + cached status |
| DELETE | `/api/v1/projects/{pid}/tickets/{tid}/jira` | Unlink (project admin) |

Single-ticket reads (`GET /api/v1/projects/{pid}/tickets/{tid}` and the
`get_ticket` MCP tool) include an optional `jira` link object when the ticket
has been promoted.

## Limitations

- Jira is the only tracker in the MVP; the `trackers` package has a provider
  protocol so Linear / GitHub Projects / Azure DevOps can slot in later.
- Push failures surface synchronously; there is no async retry queue. A
  failed update of an already-linked issue is recorded and listed under
  `last_push_failures` in the health response until the next successful
  publish.
