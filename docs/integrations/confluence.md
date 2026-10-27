# Confluence Cloud integration

SessionFS syncs a project's wiki pages and context document to Confluence Cloud
as polished, real pages. The link is strictly **one-way**: SessionFS creates or
updates the Confluence page from the SessionFS source, and reads the page's
version back for drift detection. Edits made in Confluence never write back
into the project — the wiki page and context document stay the source of truth.

The integration is **Enterprise** only, behind the `confluence_integration`
feature flag. It is available from the dashboard, the CLI and the API.

## What it does

A member of a project's organization who can edit the project can:

- **Preview** a Confluence page built from a wiki page or the project context,
  without sending anything to Confluence.
- **Publish** it. The first publish creates the page; later publishes update
  its content. The page is rendered through a strict Markdown → Confluence
  storage-format converter — never raw markup.
- **Unlink** or **resolve drift** (project admins): if Confluence was edited
  since the last push, the link is marked `drifted` and must be explicitly
  overwritten or unlinked before publishing again.
- **Opt into the scheduled sweep**, which batch-pushes opted-in wiki pages and
  (when the mapping has `publish_context`) the context tree on a schedule,
  without a model call.

Each source (a wiki page slug, or the context document) links to **at most one**
Confluence page (`doc_page_links`), so publishing again updates the same page
rather than creating a duplicate.

Publishing is safe to retry. Every publish (create, adopt or update) first
takes a claim on the source's link row; a second publish of the same source
while the claim is live gets `409 publish_in_progress` (never a false drift),
and a claim older than 5 minutes is treated as abandoned and reclaimed. The
whole outbound sequence of one publish shares a single 60-second deadline.

Each created page gets a global label
`sessionfs-<project>-<kind>-<key>-<hash>` (lowercase, label-safe, with a short
hash so two sources never share a label). Confluence's v2 create call cannot
carry labels, so the label is added right after the create. If anything fails
or times out once the page may exist — a timeout, a lost response, a 5xx, or a
failure after the create answered — the link is **kept** in the `unknown`
state, never deleted, together with the page id whenever the create response
came back. Only then does the next publish adopt instead of creating:

1. by the stored page id, when known;
2. otherwise by CQL `type = page AND space = "KEY" AND label = "<label>"`
   (searched twice, since the index can lag);
3. otherwise by the exact page title in the space.

A page found by label or title is adopted only when it is in the mapped
space, was authored by the connection's account, was created no earlier than
the unconfirmed publish began (2-minute skew), and — for a title match — sits
under the expected parent; anything else is refused with
`409 confluence_adopt_rejected` and the link stays `unknown`. A page found by
label or title that another SessionFS source already links (same title under
the same parent, or another project mapped to the same space) is never
adopted: `409 confluence_title_conflict`, and the link becomes `failed`. A
unique index on `(site_url, page_id)` backs this up, so two sources can never
share a page.

When Confluence **definitively refuses** a create (a 4xx other than 408/429 —
typically "a page with this title already exists"), nothing was created: the
link becomes `failed` with the failure recorded, the publish returns
`409 confluence_create_refused`, and the next publish creates again **without**
any label/title adoption. Two wiki pages of one project that share a title get
distinct Confluence titles (`<title> (<slug>)`); the same title in two projects
mapped to the same space and parent ends in that clean 409 for the second one.
A body over the
1 MiB cap is refused before anything is sent
(`422 content_too_large_for_confluence`); the raw text is checked before the
Markdown conversion even runs.

## Setup

1. **Connect the org** (org owners and admins) from the Organization page's
   **Integrations** tab, or:

   ```bash
   sfs org confluence connect \
     --site-url https://acme.atlassian.net \
     --email docs@acme.com \
     --template reference
   ```

   The credentials are validated against Confluence
   (`GET /wiki/rest/api/user/current`) and the API token is stored Fernet-encrypted at rest — it is never returned
   by any route or written to audit rows.

2. **Map projects** to a Confluence space and parent page via
   `PUT /api/v1/orgs/{org_id}/integrations/confluence/mappings/{project_id}`, or
   `sfs org confluence map`. Each mapping carries a `space_key`, a
   numeric `parent_page_id` (which must be a page **in that space** and a real
   child page, never the space's home page), an optional template override,
   and two opt-in flags:
   `publish_context` (show the context publish button and let the sweep
   publish the context tree) and `auto_publish_pages` (include the project's
   opted-in wiki pages in the scheduled sweep). Saving resolves the space key
   to its numeric space id (v2 API calls take the id) and caches it on the
   mapping. In the dashboard's **Add mapping** dialog the space and parent
   pickers are searchable against the real Confluence site: spaces are listed
   (v2 has no space text search) and filtered by key or name, bounded to the
   first 1,000 spaces; pages are searched with CQL `title ~ "..."`.

3. **Publish pages.** From a wiki page or the context tab, **Publish to
   Confluence** opens a preview you can inspect before publishing. From the
   CLI:

   ```bash
   sfs project confluence publish my-page --preview
   sfs project confluence publish my-page
   sfs project confluence publish --context
   ```

`GET /api/v1/orgs/{org_id}/integrations/confluence/health` (and
`sfs org confluence status`) reports unmapped projects, drifted links,
publishes in progress, unconfirmed publishes, and push failures from the last
24 hours.

A wiki page opts into the sweep with its **Publish** switch (editors only),
which calls `PATCH /api/v1/projects/{pid}/pages/{slug}` with
`{"publish": true|false}`. The toggle changes only the flag — no content
write, no revision, and `updated_at` is untouched.

## Templates

Five built-in templates: **Reference** (`reference`), **Architecture**
(`architecture`), **Runbook** (`runbook`), **Onboarding** (`onboarding`), and
**Concept** (`concept`). The org picks a default when connecting, and a project
mapping can override it. Custom Confluence templates are out of the MVP — only
the built-ins exist.

Each template is an ordered list of sections. Every section is either:

- **verbatim** — copied exactly from a source field (claims by type, session
  evidence, backlinks, steps); never taken from the model; or
- **rewrite** — filled by joining raw source fields, then reshaped by the
  configured LLM key when one exists. When no key is configured, a rewrite
  section falls back to the same verbatim source text, so publishing always
  works.

A section draws from the fixed source-field vocabulary:

| Field | Meaning |
|-------|---------|
| `page_body` | The wiki page body, or the context document (or one context section) |
| `sections` | The context document's sections (empty for a wiki page) |
| `claims_by_type` | The project's knowledge claims grouped by freshness class |
| `session_evidence` | Evidence distilled from captured sessions |
| `compile_metadata` | Metadata from the last context compilation |
| `backlinks` | Other wiki-page slugs (resolved to linked pages when they exist) |

Preview takes the same `template_override` field (`template` is accepted as a
deprecated alias). Template resolution order: an explicit per-publish
`template_override`, the
project mapping's template, then the connection default. Publish echoes the
preview's `template.slug` and `template.version` in `template` /
`template_version`; the echo never selects the template. When the echo no
longer matches the resolution — the mapping's template or the connection
default changed since the preview, or a version is echoed for a built-in
(built-ins have none) — publish is refused with `409 template_changed` and
nothing is sent; preview again.

Every published page ends with a provenance footer such as
`Generated by SessionFS · Shaped by 12 active claims (9 current, 2 aging, 1
stale) · last compile 2026-10-26T10:00:00Z · template Reference`. Only active
claims count (claim class `claim`, not dismissed, not superseded), bucketed by
their freshness class. When the
dashboard URL is configured (`SFS_DASHBOARD_URL`, or an explicitly set
`SFS_APP_URL`) it continues ` · View source in SessionFS →` with a link back to
the source page. The footer is rendered through the strict converter, never raw
markup.

## Rendering and the citation rule

The source is the source of truth. Markdown from the wiki page or context
document is converted to Confluence storage format through a strict allowlist:

- headings, paragraphs, emphasis and inline code;
- ordered and unordered lists;
- fenced code blocks (rendered as `ac:structured-macro` code blocks with
  CDATA-wrapped, CDATA-safe bodies);
- tables;
- links limited to `http(s)` and `mailto:` schemes — anything else is emitted
  as plain text;
- `[[slug]]` backlinks resolved to the linked Confluence page when it exists,
  and emitted as plain text otherwise.

Raw HTML and markup are escaped, not injected. The deterministic draft fills
every section from its source fields. When the org or user has an LLM key, only
the `rewrite` sections are sent to the model; model output is untrusted, so
every rewritten paragraph must carry a citation that resolves against a real
source field or it is dropped, and a section with nothing left falls back to
the deterministic draft. Verbatim sections are never taken from the model.

## Drift and resolution

Before an update, SessionFS reads the remote page version. If it is newer than
the version recorded at the last push, the link is marked `drifted` and the
publish refuses with `409 confluence_page_drifted` rather than silently
clobbering the Confluence edit. A project admin resolves the conflict with
`POST /api/v1/projects/{pid}/confluence/links/{link_id}/resolve` and
`{"action": "overwrite"}` (accept the remote version as the new baseline;
the next publish overwrites the Confluence edit) or `{"action": "unlink"}`
(keep the row but sever the link; the next publish creates a fresh page). The
drift `409` carries `link_id`, both versions and a bounded unified `diff` of
what changed in Confluence since the last publish (`diff_truncated` when cut).
Unlink and resolve refuse with `409 publish_in_progress` while a publish claim
is live. Resolve `unlink` writes the same `confluence_unlinked` audit event as
`DELETE`. The scheduled sweep counts drifted pages separately and never
touches them.

An update whose outcome is unknown (it timed out) can surface as drift on the
next publish if Confluence applied it; resolve it with `overwrite`.

**Deleted pages.** When Confluence answers 404 for the page a linked (or
`unknown`) link points at, the publish (or resolve `overwrite`) severs the
link (status `unlinked`, audited as `confluence_page_missing`) and returns
`409 confluence_page_missing` instead of silently creating a replacement. The
next interactive publish creates a fresh page. The sweep deliberately leaves
it alone (it treats `unlinked` as "stop syncing"), so a page someone deleted in
Confluence is not resurrected by automation; publish it by hand to bring it
back.

**Troubleshooting a 404.** Confluence also answers 404 when the page still
exists but the connection's account lost permission to see it (a page
restriction or a space permission change). SessionFS cannot tell the two
apart, so it treats both as missing and severs the link. If the page does
still exist, restore the account's view permission and unlink/republish, or
expect the next publish to be refused with `409 confluence_create_refused`
(the title is still taken) rather than overwriting anything.

**Changing a mapping's space.** Saving a mapping with a different space key
deletes the project's link rows for pages in the old space (one
`confluence_links_dropped` audit event lists them with their page ids), so
the next publish **and the sweep** create fresh pages in the new space; the
old pages stay in Confluence untouched. Links an admin explicitly stopped
syncing (`unlinked`) are kept. The delete is conditional on no live claim and
its row count is checked; while a publish of the project holds a live claim
the whole save is refused with `409 publish_in_progress`. Each link records the space its page lives in,
and page URLs are built from that, not the mapping's current space.

**Re-attribution.** Saving a mapping makes the saving admin its owner for the
sweep (`created_by_user_id`), which is how a mapping whose creator left is
re-attributed.

## Stale links (transfer, merge, site change)

A page link is bound to the org and Confluence site it was created against.
When a link's org or site differs from the current connection (for example the
admin re-pointed the connection to another site), publish, drift checks and
resolve refuse with `409 confluence_link_stale` and nothing is sent to either
site. Preview still answers (no Confluence call) and reports the link with
`stale: true` and no page URL, so the dashboard can offer Unlink; only
`DELETE .../links/{id}` (unlink) works, after which
the next publish creates a fresh page on the current site. Transferring a
project to another scope, or merging it into another project, deletes its
Confluence mapping and page links together with its Jira mapping.

## Security

- **Token encryption** — the API token is Fernet-encrypted at rest using the
  same derivation as judge settings. No route, response, or audit row ever
  contains the token.
- **SSRF control** — the site URL must match the strict allowlist
  `https://<site>.atlassian.net` (lowercase alphanumerics and hyphens only, no
  ports/paths/userinfo), redirects are never followed, and requests time out
  after 20 seconds (60 seconds for a page create/update).
- **Error sanitization** — upstream error bodies are scrubbed (the token and
  request headers are never logged or returned) before leaving the client.
- **DLP redaction** — source text is redacted before any model call, and the
  final title and sections are redacted again with the org's DLP policy
  (secrets always) before rendering, on both the interactive and sweep paths.
  The preview shows the redacted text.
- **Authorization** — org-scoped routes check the caller is an org owner or
  admin and gate on the org's plan; project routes gate on the project's org,
  require org membership and edit standing on the project (in an org that
  restricts visibility: org admins and group editors/admins), and reuse
  `knowledge:read` / `knowledge:write` scopes. A caller who cannot read the
  project gets 404. Service keys are refused on preview, publish, unlink and
  resolve (`403 service_key_not_allowed`); scheduled publishing goes through
  the sweep. Unlink and resolve require project-admin standing.

## API surface

| Method | Path | Purpose |
|--------|------|---------|
| GET/PUT/DELETE | `/api/v1/orgs/{org_id}/integrations/confluence` | Read/upsert/remove the connection |
| POST | `/api/v1/orgs/{org_id}/integrations/confluence/test` | Validate credentials without saving |
| GET | `/api/v1/orgs/{org_id}/integrations/confluence/spaces` | List/search spaces |
| GET | `/api/v1/orgs/{org_id}/integrations/confluence/spaces/{key}/pages` | List/search pages in a space |
| GET | `/api/v1/orgs/{org_id}/integrations/confluence/templates` | List the built-in templates |
| GET/PUT/DELETE | `/api/v1/orgs/{org_id}/integrations/confluence/mappings[/{project_id}]` | Read/upsert/remove mappings |
| GET | `/api/v1/orgs/{org_id}/integrations/confluence/health` | Unmapped count, failures, drifted links |
| POST | `/api/v1/projects/{pid}/confluence/preview` | Draft a page (wiki page or context) |
| POST | `/api/v1/projects/{pid}/confluence/publish` | Create/update the Confluence page |
| GET | `/api/v1/projects/{pid}/confluence/links` | List the project's page links (+ `stale`) and the mapping |
| DELETE | `/api/v1/projects/{pid}/confluence/links/{link_id}` | Unlink (project admin) |
| POST | `/api/v1/projects/{pid}/confluence/links/{link_id}/resolve` | Resolve drift: overwrite or unlink |

## Scheduled sweep

Operators run `POST /api/v1/admin/sweeps/confluence-publish` (a platform-admin
user key or a `platform_sweeps:run` service key; optional `{"limit": N}`,
default 100, max 1000 publish attempts per run). The sweep is deterministic —
it never calls the model — and applies org DLP. Per run it:

- works through mappings with `publish_context` and/or `auto_publish_pages`
  whose project is still in the mapping's org and whose org connection is
  enabled, ordered by `last_attempted_at` (never-attempted first). Each
  mapping is stamped **before** any work, so a failing or very large mapping
  rotates to the back and cannot starve the others; at most 20 publishes per
  mapping per run (the rest are reported as `deferred`). Within a mapping,
  sources whose last publish failed (`last_push_failed_at`) go after the
  healthy ones, oldest failure first, so always-failing sources cannot use up
  that budget;
- skips a mapping (and logs it) when the org no longer has the
  `confluence_integration` feature, or when the mapping's creator is no
  longer an active owner/admin of the org. Every publish is attributed to the
  mapping creator, never to the operator;
- publishes the context tree (context root page, then one page per context
  section nested under it) when `publish_context` is set, and wiki pages with
  `publish = true` when `auto_publish_pages` is set;
- skips a source whose content hash (title + SessionFS source + template) is
  unchanged since its last publish, or which changed less than the
  connection's `batch_window_minutes` ago (several compiles coalesce into one
  publish); drifted, unlinked, stale and in-flight sources are counted and
  left alone.

Re-running the sweep with nothing changed is a no-op: every source is skipped
as unchanged without any Confluence call.

## Limitations

- Confluence Cloud is the only documentation provider in the MVP; the
  `integrations/docs` package has a provider protocol so Notion / Google Docs
  can slot in later.
- Custom templates are out of the MVP — only the five built-ins exist.
- Drift is detected on publish only; webhook-driven drift detection and
  "accept the Confluence edit into SessionFS" are post-MVP.
- Push failures surface synchronously; there is no async retry queue. A failed
  update of an already-linked page is recorded and listed under
  `last_push_failures` in the health response until the next successful
  publish.
