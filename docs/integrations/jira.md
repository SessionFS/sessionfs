# Jira Cloud integration

SessionFS promotes tickets to Jira Cloud stories. Promotion itself is
**one-way** — SessionFS creates or updates the Jira issue from the ticket and
reads its status on demand, never touching status, assignee or sprint.

With **two-way sync** enabled (see below), a linked ticket can also mirror
changes in both directions: status transitions, field edits and comments can
flow back into SessionFS through a signed inbound webhook, and SessionFS status
changes, opted-in comments and completion evidence can be pushed to Jira. The
ticket remains the source of truth — inbound transitions go through the same
ticket FSM as any other status change and are never forced past a legal step.

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
   optional template override, and an optional "done" status name. In the
   dashboard's **Add mapping** dialog both project pickers are searchable;
   typing part of a Jira project key or name asks Jira for matches, so every
   project on a large site is reachable.

3. **Promote tickets** from the ticket detail panel (**Promote to Jira** opens
   a preview you can edit before publishing; a linked ticket shows its issue
   key and an **Update Jira** button), with
   `sfs ticket jira preview|publish|status|unlink`, or through the API
   endpoints under `/api/v1/projects/{pid}/tickets/{tid}/jira`.

In the dashboard, org owners and admins manage the connection, project
mappings and integration health from the **Integrations** tab of the
Organization page.

## Two-way sync

Two-way sync is off by default. An org owner or admin turns it on in the Jira
card's **Two-way sync** section, or with
`PATCH /api/v1/orgs/{org_id}/integrations/jira/sync` and any of
`sync_status`, `sync_comments` (`off`, `inbound`, `outbound` or `both`) and
`status_map`. The PATCH merges: fields you leave out keep their value, and you
don't re-enter the API token. The connection `PUT` merges the same way, so
reconnecting or rotating the token never resets sync.

Every sync action is recorded in the `tracker_sync_events` ledger. The health
response reports `last_inbound_at`, `last_synced_at`, `pending_sync_events`,
`failed_sync_events_24h` (failed or refused events, in either direction) and
`webhook_auth_failures_24h`. The dashboard lists recent events.

### Inbound webhook

In Jira Cloud, create an admin webhook for the `jira:issue_updated` and
`comment_created` events, pointing at:

```
POST /api/v1/webhooks/jira/{org_id}
```

The Jira card shows the full URL for your org. The webhook lives under
`/api/v1` so the same URL works on the hosted service and on a Helm install,
whose dashboard proxy forwards only `/api`, `/mcp` and `/health`.

Generate a secret in the Jira card, or with
`POST /api/v1/orgs/{org_id}/integrations/jira/webhook-secret`. The secret is
returned **once**. The server stores it encrypted, because it has to
recompute each delivery's signature; only its last 4 characters are ever shown
again. Paste the secret into Jira's webhook settings. Jira signs each delivery
with `X-Hub-Signature: sha256=<hex>` over the raw body, and SessionFS checks it
in constant time. It fails closed:

- the org has no Jira connection → `404`;
- no secret has been generated → `503`;
- the signature is missing or wrong → `401`. Rejected deliveries are counted in
  a rolling 24-hour window on the connection, and at most one audit row per org
  per hour records them.

The body is read as a stream and refused with `413` once it passes 1 MiB, with
or without a `Content-Length`. A delivery for an issue with no SessionFS link
on the connection's site is ignored with `200`. If the org's plan no longer
includes the Jira integration, the delivery is ignored and an error event is
recorded.

**Idempotency.** Jira's `X-Atlassian-Webhook-Identifier` header is the
delivery key, so a retry is answered `{"status": "duplicate"}`. Without that
header the key falls back to Jira's comment or changelog id, prefixed with the
event type. That way a `comment_created` delivery is never mistaken for the
`jira:issue_updated` delivery Jira sends for the same comment.

**Status.** Only an `issue_updated` delivery whose changelog contains a
`status` change can move a ticket. Every delivery carries the issue's current
status, but an edit that doesn't change it never re-applies it. With
`sync_status` on, the new status category is looked up in the status map and
the action goes through the same ticket transitions as the ticket routes. Those
keep the FSM checks, `resolved_at`, dependent unblocking and enrichment, and
watcher notifications. The org's admin configured the map, so an inbound
transition acts on the org's behalf, attributed to "jira". It still can't do
anything the FSM forbids: only Tasks in `review` resolve to `done`, only Issues
in progress close. A refused transition is recorded as an error event and
counted in health; it is never forced.

**Echoes.** A delivery is dropped as an echo only when it matches something
SessionFS actually pushed. A status change is an echo when SessionFS pushed a
transition of the same name, for the same target status, in the last 5
minutes. A comment is an echo when its Jira id was recorded when SessionFS
posted it. As a fallback, for a post whose id was never recorded, it is also
an echo when it ends with the outbox reference SessionFS adds to every comment
it posts, and it comes from the connection's own account (or that account
isn't known yet). A reply that quotes or pastes a reference is still mirrored. SessionFS never pushes field edits, so those always apply.
The connection's own Atlassian `accountId` is only a hint: it can rule an echo
out (a different user made the change), never in.

A dedicated Jira integration account is recommended for the connection, so
SessionFS's own changes are easy to tell apart in Jira. A personal account
works too: your own transitions, edits and comments in Jira still sync back.

**Field edits.** A change to `summary`, `description` or `priority` posts a
system comment on the ticket: "Edited in Jira by <name>: <field> changed". It
includes at most a 500-character excerpt, which is DLP-redacted *before* it is
cut so a secret can't survive truncation. Only the redacted excerpt is stored.
Changes to other fields are ignored.

**Comments.** When `sync_comments` is `inbound` or `both`, a `comment_created`
delivery becomes a ticket comment with `source=jira`, and it is never trusted.
The text is DLP-redacted and capped at 10,000 characters. The author is shown
as "<name> (Jira)", with the name DLP-redacted and shortened to fit.

### Outbound pushes

A status change on a linked ticket queues an outbound push. A queued push
becomes a `pending` row in the sync ledger, written in the same transaction as
the ticket change. After the request commits, a background task delivers it,
so a Jira call never runs inside the ticket request and a Jira outage never
fails it. If the task is lost (for example an instance shutdown), the sweep
delivers the row later.

Only an actor with edit standing on the project, or a service key within its
org and project allowlist, drives Jira. A reader's lifecycle moves on tickets
they created stay inside SessionFS.

Each Jira call has a 30-second timeout and one retry after a timeout. A failed
delivery is retried with exponential backoff (1, 4, 16 and 64 minutes) through
`next_attempt_at`. After 5 attempts the row is marked `exhausted`. A row is
`skipped` with a reason when it no longer applies: the ticket was unlinked,
sync was turned off, the status is unmapped, the row was superseded by a newer
status, or the plan lost the integration.

Comments go to Jira only when the org's `sync_comments` is `outbound` or
`both`:

- **Send to Jira** on a comment is honoured for editors. A reader's flag is
  ignored. When an editor ticks it but the ticket is not linked or outbound
  comment sync is off, the comment is refused with `409 jira_comment_sync_off`
  and nothing is written.
- A trusted reviewer's verdict is forwarded only when it is `VERIFIED-CLEAN`,
  as read by the review-state parser. `CHANGES-REQUESTED` and plain comments
  are never forwarded.

Outbound comments carry a "via SessionFS —" prefix, are DLP-redacted and are
cut to fit the ADF budget. Every comment SessionFS posts (including completion
evidence) ends with a `[SessionFS ref tse_…]` line naming its outbox row.
Before posting, SessionFS looks for that reference among the issue's 20 newest
comments. A post that landed before a client timeout, or before a lost commit,
is therefore never posted twice. If the lookup itself fails, SessionFS posts
anyway rather than fail the delivery.

Transitions are idempotent in the same way. The target status comes from the
transition itself, because Jira reports which status each transition leads to.
That works even when the names differ, such as "Resolve" leading to
"Resolved". SessionFS records the target before posting. If Jira no longer
offers the transition, or a request timed out, SessionFS re-reads the issue.
When the issue is already at the target status, the push counts as delivered
instead of failing with "no transition named …".

### Sweep

`POST /api/v1/admin/sweeps/jira-sync` needs a platform-admin user key or a
`platform_sweeps:run` service key. It does two things:

- It delivers due rows: `pending`, fewer than 5 attempts, `next_attempt_at`
  reached. Terminal rows never block new failures.
- It deletes finished ledger rows older than 90 days.

Each row is claimed, delivered and committed on its own, and an exception on
one row is recorded on that row. Run it every few minutes. The optional body
`{"limit": 1-1000, "budget_seconds": 1-120}` bounds a run (defaults 100 and
50).

### Status map

`status_map` maps both directions:

- `transitions`: SessionFS status → Jira transition name (outbound). The keys
  are `open`, `in_progress`, `blocked`, `review`, `done`, `cancelled` and
  `closed`.
- `categories`: Jira status category (`new`, `indeterminate`, `done`) →
  SessionFS action (inbound): `resolve`, `start`, `reopen` or `none`.

`"none"`, an empty string or `null` means "not synced". The defaults cover
only what a stock Jira workflow has. Outbound, `open` → "To Do",
`in_progress` → "In Progress", and `done` and `closed` → "Done"; `blocked`,
`review` and `cancelled` are not synced. Inbound, only Done acts (`resolve`).
Moving an issue back to To Do or In Progress in Jira never rewinds SessionFS
unless you map it. Unknown statuses, categories or actions are rejected with
`422`. A transition your Jira workflow doesn't offer is recorded as a failed
event.

### Completion evidence

When a linked ticket reaches its completed status (`done` for a Task, `closed`
for an Issue) with status sync on, SessionFS posts one **evidence comment** to
the Jira issue. It contains:

- the latest agent-run verdict, the persona and a trusted-reviewer badge;
- per-file line counts from the sessions the ticket links (its resolver
  session and related sessions, in the ticket's own project and not deleted),
  capped at 20 files;
- the knowledge claims the ticket records (count plus up to 5 titles; live
  claims of the ticket's project only);
- links to the ticket and sessions when `SFS_DASHBOARD_URL` is set.

The comment is DLP-redacted and fits the ADF budget. It posts once per
completion: the evidence row is claimed under a unique key on the link and the
completion time before anything is sent. Concurrent jobs can't double-post,
and reopening and re-resolving a ticket posts new evidence. Evidence is its own
outbox row, so a failure building it can't undo the status push. The failure
is recorded and retried.

## Templates

A template is an ordered list of sections. Each section is either:

- **verbatim** — copied exactly from a ticket field (acceptance criteria,
  completion notes, linked sessions); never taken from the model; or
- **rewrite** — filled by joining raw source fields, then reshaped by the
  configured LLM key when one exists. Rewrite sections carry an optional
  guidance string that is passed to the model alongside the source fields.

Built-in templates: `agile` (Agile Standard), `compliance`
(Enterprise/Compliance), `sre` (Platform/SRE), `bug` (Bug Report), and
`design` (Design/UX — a user journey and hypothesis rewritten from the
description, a success metric copied verbatim from the acceptance criteria,
and context drawn from the description plus linked sessions). Built-ins are
read-only.

Org admins can create **custom templates** (up to 50 *active* templates per
org), each with a unique slug, a name, an optional description (≤ 1,000
characters), and up to 12 sections (headings ≤ 80 characters). Names,
headings, descriptions and guidance may not contain control or invisible
format characters (newlines and tabs are allowed in descriptions and
guidance). A section draws from the fixed source-field vocabulary, limited by
its mode:

| Mode | Allowed source fields |
|------|-----------------------|
| verbatim | `acceptance_criteria`, `completion_notes`, `linked_sessions` |
| rewrite | `title`, `description`, `acceptance_criteria`, `priority`, `kind`, `persona`, `completion_notes`, `linked_sessions` |

A field a mode can't render is rejected with a 422 naming
`sections[i].source_fields[j]`. Rewrite sections accept **guidance** (≤ 500
characters): a hint to the model about tone or emphasis for that section. It is
not a source — it is sent inside a delimited block the model is told to treat
as data, it is never applied to verbatim sections, and it can't add content:
every rewritten paragraph still needs a citation that resolves against the
ticket, or it is dropped.

Editing a custom template creates a new immutable version. A save carries the
`expected_version` the editor loaded; if someone else saved first the server
answers 409 `template_version_conflict` instead of overwriting their change.

**Archiving** (DELETE) hides a template from pickers as a *new* choice. It
never switches existing configuration: mappings, the connection default and
published links that use it keep resolving to it (re-saving a mapping or the
connection with its unchanged archived value is accepted), and health reports
them under `mappings_on_archived_templates`, `connection_default_archived` and
`archived_template_references`. `POST .../templates/{slug}/unarchive` restores
it (same slug and version). Archived templates don't count toward the 50-template
cap, but their slugs stay reserved: a new template can't reuse one. If a future
release adds a built-in whose slug an org already uses for a custom template,
the org's custom template keeps winning for that org.

A template is resolved in this order: the per-ticket choice in the promotion
dialog (`template` on preview/publish), the project mapping's template, then
the connection's default template. Members pick per ticket from
`GET /api/v1/projects/{pid}/tickets/{tid}/jira/templates`, which returns only
`{slug, name, version, builtin, archived}` for the built-ins and the org's
active custom templates, plus the inherited default and the link's pinned
slug/version. A published ticket keeps its template: if the template it is
pinned to was archived, that ticket (and only that ticket) still gets it as a
choice, marked `archived: true`, and can re-publish with it; other tickets get
a 422 `template_archived` for it. The picker starts on the pinned template,
else the inherited one.

Preview returns the resolved `template` `{slug, name, version}`; publish echoes
it back as `template` + `template_version`. If the resolution has changed
since the preview (the template was edited, say), publish refuses with 409
`template_changed` and you preview again — a story is never rendered or pinned
with a version nobody previewed. Each published link records the template slug
and (for custom templates) version it used, so a re-publish can show
"Published with <template> v<n>; current is v<m>" with a diff.

Every published story's description ends with a provenance footer:
`Generated by SessionFS · template: <name> v<n>` (built-ins have no version,
so just the name). When the dashboard URL is configured (`SFS_DASHBOARD_URL`,
or an explicitly set `SFS_APP_URL`) it continues ` · source ticket <url>`,
with the URL as a link. The footer counts toward the 32,000-character
description budget.

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
| GET | `/api/v1/orgs/{org_id}/integrations/jira/projects` | List Jira projects. `?q=<text>` searches key and name on Jira's side and `?limit=` (default 50, max 100) returns `{projects, has_more, query, limit}`; without either, the bare list of the first 500 with an `X-Jira-Projects-Truncated` header |
| GET/PUT/DELETE | `/api/v1/orgs/{org_id}/integrations/jira/mappings[/{project_id}]` | Read/upsert/remove mappings |
| GET | `/api/v1/orgs/{org_id}/integrations/jira/health` | Unmapped count, failures, stale links |
| PATCH | `/api/v1/orgs/{org_id}/integrations/jira/sync` | Change two-way sync settings (merge; no token needed) |
| POST | `/api/v1/orgs/{org_id}/integrations/jira/webhook-secret` | Generate/rotate the inbound webhook secret (returned once; stored encrypted, only the last 4 characters are shown again) |
| GET | `/api/v1/orgs/{org_id}/integrations/jira/sync-events` | Recent two-way sync events (admin). `?direction=inbound\|outbound` and `?limit=` (default 50, max 200) |
| GET/POST | `/api/v1/orgs/{org_id}/integrations/jira/templates` | List built-in + custom templates; create a custom template |
| GET/PUT/DELETE | `/api/v1/orgs/{org_id}/integrations/jira/templates/{slug}` | Read/update (with `expected_version`)/archive a custom template |
| POST | `/api/v1/orgs/{org_id}/integrations/jira/templates/{slug}/unarchive` | Restore an archived custom template |
| GET | `/api/v1/orgs/{org_id}/integrations/jira/templates/{slug}/versions` | Version history for a custom template |
| POST | `/api/v1/orgs/{org_id}/integrations/jira/templates/{slug}/preview` | Deterministic preview against a ticket from a mapped project (no model call); pass `sections` to preview an unsaved draft |
| GET | `/api/v1/projects/{pid}/tickets/{tid}/jira/templates` | Templates a member may pick for this ticket |
| POST | `/api/v1/projects/{pid}/tickets/{tid}/jira/preview` | Draft the story |
| POST | `/api/v1/projects/{pid}/tickets/{tid}/jira/publish` | Create/update the Jira issue |
| GET | `/api/v1/projects/{pid}/tickets/{tid}/jira` | Linked issue + cached status |
| DELETE | `/api/v1/projects/{pid}/tickets/{tid}/jira` | Unlink (project admin) |
| POST | `/api/v1/webhooks/jira/{org_id}` | Jira Cloud inbound webhook (HMAC-signed) for two-way sync |
| POST | `/api/v1/admin/sweeps/jira-sync` | Deliver due outbound sync events and expire old ones (`platform_sweeps:run`) |
| GET | `/api/v1/projects/{pid}/tickets/{tid}/jira-sync-events` | Sync events for a linked ticket |

Single-ticket reads (`GET /api/v1/projects/{pid}/tickets/{tid}` and the
`get_ticket` MCP tool) include an optional `jira` link object when the ticket
has been promoted.

## Limitations

- Jira is the only tracker in the MVP; the `trackers` package has a provider
  protocol so Linear / GitHub Projects / Azure DevOps can slot in later.
- Promotion push failures surface synchronously; there is no retry queue for
  a failed publish/update of an already-linked issue — it is recorded and
  listed under `last_push_failures` in the health response until the next
  successful publish. Two-way outbound sync, by contrast, is queued durably and
  retried by the admin sweep (up to 5 attempts with exponential backoff).
- Two-way sync is a SessionFS-side mirror, not a Jira plugin: status changes
  made in Jira arrive via webhook, so a Jira outage or misconfigured secret
  means inbound events are simply missed rather than queued for later replay.
  Events older than 90 days are expired from the sync ledger.
