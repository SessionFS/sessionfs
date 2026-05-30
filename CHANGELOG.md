# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.10.24] - 2026-05-30

**Bundle release — Issue/Task rollup + 2 enterprise unblocks (GH #50 + GH #51) + dashboard surface.** Migration 047 adds `kind` + `parent_ticket_id` to tickets so a single user-reported problem can roll up multiple executor workstreams. Two enterprise customer bugs filed by `najitestech` close: `sfs org create` 500 (latent FK ordering since v0.10.0, blocked every manual-license enterprise) and missing MCP `update_persona` / `delete_persona` parity. Plus structured `IntegrityError` envelopes across CLI + dashboard so future opaque 500s become actionable, Rules-page max-tokens UX, session rename, CORS If-Match preflight unblock, and the Prism-side dashboard surface for the rollup. 2108 backend tests + 198 dashboard tests passing (+85 backend / +11 dashboard from v0.10.23 baseline). 1 strictly additive migration.

### Added

**Issue/Task rollup** (`tk_dbccde26ed604b3c` + `tk_5d14f10e489d4361` Prism, commits `0cae114` + `09d0735` + `98d3154`). Compass-scoped Option A from `tk_23f523c1bdd94fc5`: extend `tickets` with `kind` String(10) NOT NULL default 'task' + `parent_ticket_id` String(64) FK ON DELETE SET NULL + composite index on `(project_id, parent_ticket_id)`. NOT a reuse of `TicketDependency` (DAG ordering ≠ container relationship). Forked FSM: Tasks keep the executor lifecycle (suggested → open → in_progress → blocked → review → done); Issues use a simpler manager FSM (open → in_progress → closed + cancelled escape). Issue status is **manually closed by Compass** — NOT auto-derived from child Task status, per CEO direction.

- New `POST /tickets/{tid}/close` Issue terminator. Rejects Tasks with 400 + clear error.
- `POST /tickets` accepts `kind` + `parent_ticket_id` with full validation: parent must exist + be same-project + be `kind='issue'`; no Issue-under-Issue (single-level only in v1); cross-project parent → 422.
- `GET /tickets` accepts `kind` query filter (`issue` | `task` | omitted); detail returns `child_ticket_ids: list[str]` for Issues (single SELECT, indexed, project-scoped).
- **Actor-based authorization** via new `user_is_project_admin(db, user_id, project)` helper in `src/sessionfs/server/auth/project_access.py`. Returns True for project owner OR (org-scoped) `OrgMember.role='admin'`. Drops the `assigned_to=='compass'` user-input-trusting branch entirely (Codex R1 MEDIUM #1 — anyone could pass it). Issue creation AND `/close` both gated on this trusted-actor signal.
- Compiled persona context now emits `Kind: {kind}`, `Parent Issue: {id} — {title} (status: {status})` when set, and `## Child Tasks (N)` section listing every child with id + status + title (Codex R1 MEDIUM #3 — agents reading the markdown can now see the rollup).
- MCP `create_ticket` + `list_tickets` tools gain `kind` + `parent_ticket_id` params with docstrings explaining the distinction. `get_ticket` + `start_ticket` compiled_context surface the rollup automatically.
- CLI: `sfs ticket create --kind issue|task --parent <tk_>`; `sfs ticket list --kind issue|task`; `sfs ticket show` displays kind + parent Issue + child task list; `sfs ticket close <tk_>` Issue terminator. `sfs ticket watch --until-closed` terminal set extended with `closed`.

**Dashboard Issue/Task surface** (Prism, `98d3154`). Single-surface design — no separate IssuesTab. `TicketsTab.tsx` extended with: `KIND_FILTERS` (All / Issues / Tasks) alongside existing status filter; indigo left-edge bar + `KindBadge` on each row for at-a-glance distinction; Issue expand shows a Children section listing child Tasks (id/title/status/assigned_to) as clickable buttons that re-target `selected` in place; Task expand with non-null `parent_ticket_id` shows a Back-to-parent Issue breadcrumb; New Ticket modal gains a kind selector + conditional Parent Issue autocomplete (Task only); Close Issue button on `in_progress` Issues calling the new `closeTicket()` route; STATUS_FILTERS gains `closed` row with emerald STATUS_TONE. `useTickets` hook gains `kind` filter; new `useTicketChildren` parallel-fetches children via `useQueries` with shared `['ticket', projectId, id]` cache keys so direct expand reuses the entry; new `useCloseTicket` mutation. KB entry **#611** documents the UI pattern.

**Session rename** (`tk_cf9f1691091d4e8e`, commits `2ef0335` + `7bb93cd`). Click-to-edit title + alias inline on session detail view, side-by-side per CEO. Title + alias persist via the existing PATCH /sessions/{id} endpoint (hardened: empty-title 422 + `aliases_cloud` tier gate + all-None body 400). New MCP tool `rename_session(session_id, new_title?, new_alias?)` — empty `new_alias=""` routes through DELETE /alias and captures canonical id from the DELETE response so a caller passing the about-to-be-cleared alias as the identifier doesn't 404 on the follow-up (Codex R1 MEDIUM). Three-layer plumbing: server (route hardening), MCP (new tool), dashboard (click-to-edit affordance with side-by-side form + `MoreMenu` "Rename (Title & Alias)" entry, replacing the old alias-only edit).

**Generic `IntegrityError` envelope + structured 5xx hardening** (`tk_e7da4c4508d94bac` / GH #51 ask #2, commits `8a2cb85` + `3576245`). Closes the customer-facing UX failure mode shared by every multi-INSERT bug: bare "Internal Server Error" with no actionable body. New FastAPI exception handler for `sqlalchemy.exc.IntegrityError` classifies by PostgreSQL SQLSTATE (`pgcode`) first, falls back to SQLite message string-matching. Unique violation → 409 `duplicate_resource`; FK violation → 500 `foreign_key_violation` (server-bug class, structured body so triage doesn't lose three releases like 2026-05-20); NOT NULL → 422 `missing_required_field`; CHECK → 422 `check_constraint_violation`; catch-all → 500 `integrity_error`. Handler logs raw DBAPI text at ERROR with request method + path; client envelope intentionally strips column names and row values to avoid PII leak. New CLI helper `format_api_error(body, status)` parses v0.10.x envelope first, falls back to legacy `detail` string + dict shapes, then `str(body)` — wired into `handle_api_response` (the `sfs org create` triggering path), `_api_request` helpers in `cmd_project.py` + `cmd_rules.py`, and all 12 inline "API error" prints in `cmd_ticket.py`. Dashboard `ApiError` class now extracts `code` + `details` + clean `message` from the envelope at construction time, with `raw` preserved for diagnostics; new `parseApiErrorBody` helper exports the same parse for non-throw paths.

**MCP `update_persona` + `delete_persona`** (`tk_32abb6d0d4744c5d` / GH #50, commits `f55fc85` + `030230b` + `fde0f4e`). Closes the 6-of-8 persona-tool gap najitestech surfaced. MCP tool count 59 → 61. `update_persona` wraps PUT /personas/{name} — body sends ONLY fields the caller passed (omitted fields stay unchanged; no silent overwrite); rejects locally if no mutable field provided. `delete_persona` wraps DELETE /personas/{name}?force=... — soft-delete with strict force-parsing (literal `True` OR case-insensitive `{"true", "1", "yes"}` only; `bool("false") == True` Python footgun closed per Codex R1 MEDIUM). 409 surfaces the structured envelope so callers can route on `error.code` + `error.details.open_ticket_count` to decide whether to retry with force=true. 404 surfaces the persona name in a clean error. Tool descriptions reference the v0.10.23 case-insensitive-unique contract and the honest reactivation guidance (R2 fix): "Reactivation is NOT exposed via MCP today — operator must use the HTTP API directly with `is_active=true`."

**Rules page max-tokens UX** (`tk_d4a13a68b6724ba6`, commits `c4c8d02` + `0d9ccf9`). Companion to the v0.10.23 CORS If-Match unblock on the same surface. Dashboard `DebouncedTokenInput` gains `max={20000}` (browser arrows respect the cap) + `n > 20000` debounce gate (defends against paste-bypass since `<input type="number" max=>` accepts pasted out-of-range by spec). Server's both `knowledge_max_tokens` + `context_max_tokens` over-cap branches now raise structured envelope `code: 'max_tokens_exceeded'` with `field`, `min`, `max`, `current` — replaces bare "knowledge_max_tokens out of range".

### Fixed

**`sfs org create` 500 for manual-license enterprise customers** (`tk_17b39010f9a64cba` / GH #51, commit `31cec52`). One-line `await db.flush()` between `db.add(org)` and `db.add(member)` in `routes/org.py:139-160`. Latent since v0.10.0 because SQLAlchemy's unit-of-work doesn't reliably topologically sort two pending INSERTs by FK dependency without an intervening flush. For Stripe-paying users the implicit autoflush triggered by `update(User)` masked the bug; users with no Stripe fields (every manual-license enterprise customer, every user upgraded via admin action without going through Stripe checkout) hit the FK violation → 500. Inline comment cites GH #51 + the autoflush mechanics so a future reader doesn't strip the flush as "unnecessary". New regression `test_create_org_enterprise_user_no_stripe` uses `PRAGMA foreign_keys=ON` on the test session so SQLite enforces FKs like PostgreSQL (otherwise the bug doesn't reproduce in tests).

**CORS preflight `If-Match` allowed** (`febd732`, no parent ticket — direct enterprise repro). Dashboard's `PUT /rules` sends `If-Match: <etag>` for ETag-based optimistic concurrency. Browser preflight requests `Access-Control-Request-Headers: ...if-match` but `CORSMiddleware.allow_headers` was `["Content-Type", "Authorization"]` — `If-Match` not listed → preflight 400 → PUT never fires → dashboard toggle reverts with no error surface. Live-verified via curl on `api.sessionfs.dev`. Added `If-Match` + `If-None-Match` to allow_headers + drift-guard regression test pinning the audited list. Affects every ETag-protected write surfaced via the dashboard.

### Verification

- pytest tests/ -x -q → **2108 passed**, 2 xfailed (pre-existing migration-003 chain)
- ruff check src/ → clean
- helm lint charts/sessionfs → clean
- dashboard npm test -- --run → **198 passed** (was 186)
- dashboard npm run build → tsc + vite clean
- pip-audit → 0 vulnerabilities
- npm audit (dashboard + site) → 0 vulnerabilities
- bandit → 0 HIGH; 17 MEDIUM pre-existing baseline (none in v0.10.24 files)
- Shield-SR independent pre-release review → APPROVED 0 CRITICAL / 0 HIGH / 0 NEW MEDIUM

### Codex review cycles

All 8 Atlas+Prism tickets implemented directly by Claude per CEO's standing rule (no Codex CLI spawn). Polling Codex (`author_persona="codex"`) reviewed:
- tk_cf9f1691091d4e8e: R1 1 MED → R2 CLEAN
- tk_dbccde26ed604b3c: R1 3 MED + 2 LOW → R2 CLEAN
- tk_17b39010f9a64cba: R1 CLEAN first round
- tk_e7da4c4508d94bac: R1 2 MED + 1 LOW → R2 CLEAN
- tk_d4a13a68b6724ba6: R1 1 LOW → R2 CLEAN
- tk_32abb6d0d4744c5d: R1 1 MED + 2 LOW → R2 1 LOW → R3 CLEAN
- tk_5d14f10e489d4361 (Prism): not Codex-reviewed (Compass-scoped, CEO-shipped)

## [0.10.23] - 2026-05-26

**Bundle release — Scout state-cache fix + ChatGPT MCP hotfix + persona case-sensitivity + release-skill hardening.** Five commits across two operational fixes (entity_ref upsert closing Scout's silent cache loss, MCP `_ConsumedResponse` sentinel restoring tool discovery on ChatGPT/Claude.ai) and three release-hygiene improvements (persona case-insensitive lookup, Scribe-Site gate, merge-conflict docs). No migrations. No new endpoints. 2023 backend tests + 187 dashboard tests passing (+16 backend from v0.10.22 baseline).

### Added

**Opt-in entity_ref upsert on `/entries/add`** (`tk_49db8d2b6c424d35`, commits `7a98f27` + `393b25f` + `a78728d`). New optional `upsert: bool = False` field on `AddEntryRequest`. When `True` AND `entity_ref` is provided, the route looks up the prior active entry by `(project_id, entity_ref, dismissed_at IS NULL, superseded_by_id IS NULL)` and supersedes-in-place rather than running the similarity-based dedup that returns 409 on cache rotations. Closes Scout's state-cache silent loss path (3 duplicate tickets filed in 18 hours on a 6%-delta cache body). Existing callers unaffected — feature is fully opt-in. Cloud agent dispatchers (`bedrock-action-group.yaml`, `vertex_tools.py`) updated to expose `upsert` in their tool schemas. Scout n8n docs (`scout-n8n.md`) updated with the dismiss-then-create workaround pattern for clients pinned to v0.10.22 and the upsert recipe for v0.10.23+. Codex R1 MEDIUM (similarity gate must check intent before bypass) + R1 LOW (test coverage) → R2 VERIFIED-CLEAN.

**ChatGPT / Claude.ai MCP tool-discovery hotfix** (commit `f7fdcc1`). `_ConsumedResponse(Response)` sentinel class in `mcp/remote_server.py` so `handle_mcp` returns a no-op ASGI callable after `transport.handle_request()` consumes the underlying response. Starlette 1.x `Route` wrapper rejects `None` return with `TypeError: 'NoneType' object is not callable`, which truncated the wire response and caused remote MCP clients to see "no tools" after OAuth connect. Auth ordering preserved — Bearer-token validate runs before transport delegation. 4 new unit tests covering the sentinel shape + ASGI no-op semantics + auth gate ordering.

**Persona name case-insensitive lookup + normalize-at-write** (`tk_884b2321fdb74170`, commits `b39f9c9` + `df501b6`). New `_resolve_persona_name(db, project_id, name) -> str | None` helper in `routes/tickets.py` using `func.lower(AgentPersona.name) == name.lower()` with `.order_by(id).first()` (deterministic tiebreaker, won't raise `MultipleResultsFound`). `start_ticket` resolver, `create_ticket`, and `update_ticket` PUT all normalize `assigned_to` through it; unknown names pass through as free-text for back-compat. `list_tickets` filter switched to `func.lower(Ticket.assigned_to) == assigned_to.lower()` so the discovery path also surfaces legacy `Atlas`-cased rows when an agent queries `?assigned_to=atlas`. `create_persona` endpoint rejects case-insensitive duplicates with 409 + explicit "case-insensitive-unique" error message — resolves ambiguity at the source so the resolver tiebreaker stays defensive. Closes the v0.10.18 onboarding pain where `start_ticket` 400'd because `.agents/atlas-backend.md` capitalizes the persona name but `agent_personas.name` stores it lowercase. 6 regression tests. Restored gutted assertion block at the end of `test_review_state_cap_preserves_earliest_rounds` (Codex R1 LOW1 — accidentally removed during the initial edit). Codex R1 1 MED + 2 LOW → R2 VERIFIED-CLEAN.

**Release-skill Scribe-Site gate** (`tk_2cc5bcca97284a91`, commit `a1071de`). Release script step 6f now reads `git diff $(git describe --abbrev=0 --tags)..HEAD -- site/` and skips Scribe-Site invocation + site deploy when `wc -l` returns 0. Saves ~5-10 minutes on backend-only releases without weakening the "no stale site content" invariant (still mandatory when `site/` is touched).

**`.release/README.md` documenting expected develop→main merge conflicts** (`tk_7d3b6b1ac1714c60`, commit `12a58c1`). 71-line pure-docs addition explaining what `.release/` contains, which paths conflict at merge time (`.claude/commands/release.md`, `CLAUDE.md`, `.agents/**`), why we don't use a broad `merge=ours` `.gitattributes` rule (would silently drop legitimate develop-side changes), and the escape valve if conflict-then-sanitize pain returns (narrow per-path `.gitattributes`, never broad).

### Operational closes (no code)

- `tk_c72ad4f99dae404b` — housekeeping close (`sfs ticket watch` shipped earlier in v0.10.7)
- `tk_12e6d8775eb045a2` — v0.10.5 compile source manifest review (Codex R2 was clean on 2026-05-15; just never got the `resolve_ticket` call)
- `tk_a1144426a013413c` — v0.10.7 customer-ask provenance fields review (Codex R8 was VERIFIED-CLEAN on 2026-05-17; same)

### Verification

- pytest tests/ -x -q → **2023 passed**, 2 xfailed (pre-existing migration-003 chain)
- ruff check src/ → clean
- helm lint charts/sessionfs → clean
- pip-audit → 0 vulnerabilities
- npm audit (dashboard + site) → 0 vulnerabilities
- bandit → 0 HIGH; 11 MEDIUM pre-existing baseline (all in files NOT touched by v0.10.23)
- Shield-SR independent pre-release review → APPROVED 0 CRITICAL / 0 HIGH / 0 NEW MEDIUM

### Authoring + reviews

All 5 commits implemented directly by Claude (no Codex CLI spawn), per CEO's standing rule. Polling Codex (`author_persona="codex"`) reviewed:
- tk_49db8d2b6c424d35: R1 MED + R1 LOW → R2 VERIFIED-CLEAN
- tk_884b2321fdb74170: R1 MED + 2 LOW → R2 VERIFIED-CLEAN
- f7fdcc1, a1071de, 12a58c1: small targeted changes shipped without review (release-process tooling + ChatGPT outage hotfix)

## [0.10.22] - 2026-05-24

**Org-collaboration fixes.** Three tickets that close long-standing operational gaps on the org surface: new org members can finally read org-scoped artifacts without first cloning the repo; invite emails actually send; recipients have a dashboard `/invites` page to accept or decline; and Scout's n8n workflow gets the multi-source signal-shape contract that was deferred from v0.10.21. No platform breakage — one strictly-additive migration (046).

### Added

**Scout multi-source signal-shape contract** (`tk_918073e8aa4c4478`, commits `59812de` + `0589563` + `cb66312`). New `docs/integrations/scout-signal-shape.md` (~600 lines) defines the 9-field canonical envelope (`source`, `source_id`, `title`, `url`, `content`, `posted_at`, `author`, `signal_strength`, `raw`) that every upstream feeds through before Scout's reasoning loop runs. `source_id` is the dedup key and threads into v4's `source_context` format unchanged. Four reference Code-node templates ship in `docs/integrations/n8n-source-adapters/` (HN Algolia, GitHub Releases, Reddit, generic RSS) each with an inline FIXTURE block. PII scrubbing hard rules in §5 + §5.1 with the Reddit adapter as the reference implementation: drops `selftext` from `raw` entirely and rewrites `raw.author` to the `u/`-prefixed form before emit so even a misconfigured workflow can't leak. Mandatory `Code: strip raw` node in the n8n workflow diagram for token-budget discipline. Updates `scout-n8n.md` §8 to point at the new spec and adds `.agents/scout-competitive-intelligence.md` for persona ownership. Adding source N+1 = one normalizer + one Merge-node pin; reasoning, dedup, and write steps unchanged. Three rounds of polling-Codex review → VERIFIED-CLEAN.

**Org-member project access fix** (`tk_7a457574c5624e12`, commit `b469d8d`). New shared helper `src/sessionfs/server/auth/project_access.py` with `user_can_access_project(db, user_id, project) -> bool` returning True for the 3-branch user-key predicate: owner OR member of `project.org_id` OR captured-session-on-git-remote (legacy fallback). Closes the v0.10.0 Phase 5 design intent documented in `db/models.py:187` but never enforced — new org members were getting 403 on every org-scoped artifact (personas, KB, wiki, tickets, agent runs) until they cloned the repo and synced a session. Both `_get_project_or_404` copies (knowledge.py + wiki.py) collapsed to thin wrappers delegating to `load_project_for_user`; 30+ existing importers across tickets, agent_runs, retrieval_audit, personas pick up the fix without call-site changes. `routes/projects.py:list_projects` SQL gains the OrgMember subquery branch so org-scoped projects appear in `GET /api/v1/projects` for the right users. `get_project` and `update_project_context` route through the same helper. `delete_project` intentionally unchanged (owner-or-admin only). Service-key path untouched — `service_key.org_id` remains the only org boundary there. 9 regression tests covering all 6 affected route families + cross-org isolation + personal-project unchanged. Codex R1 first-round VERIFIED-CLEAN.

**Org invite email + dashboard `/invites` page** (`tk_6afbcfefe5804c1d`, commits `4888043` + `91d3522` + `ca30263`). Closes the 2026-05-23 onboarding incident where 4 new SessionFS signups had to be hand-messaged accept URLs because the invite endpoints never fired email.

- **Migration 046** strictly additive: `declined_at`, `decline_reason`, `last_emailed_at` on `org_invites`. No backfill.
- **`services/invite_helpers.dispatch_invite_email`** shared best-effort helper. Both invite endpoints (`routes/org.py` legacy + `routes/org_members.py` multi-org) + the new resend endpoint route through it. SMTP/Resend failure logs but does not 500 the route.
- **`email.EmailProvider.send_org_invite` + `email_templates.org_invite_email`** mirror the v0.10.9 handoff lifecycle shapes. Every user-controlled field is `_html.escape`'d.
- **`app.state.config`** wired in `server/app.py` lifespan so services can read `config.app_url` for dashboard URL building without re-importing `ServerConfig`.
- **New `POST /api/v1/orgs/{org_id}/invites/{invite_id}/resend`** — admin-only re-fire without creating a new invite row. 409 on already-accepted/declined/expired.
- **New `POST /api/v1/org/invite/{invite_id}/decline`** — recipient marks the invite refused with optional reason (1000-char cap). Atomic rowcount-1 guarded transition; subsequent `/accept` rejects 400.
- **New `GET /api/v1/org/invites/me`** — pending invites for the logged-in user, server-filtered to exclude accepted/declined/expired.
- **Atomic accept + decline FSM transitions** via rowcount-1 guarded conditional UPDATE on `accepted_at IS NULL AND declined_at IS NULL AND expires_at > now()` with `synchronize_session=False` to skip the ORM's client-side WHERE eval (SQLite vs PG datetime safety). On rowcount=0 the session rolls back (dropping any pending OrgMember add for the accept path) and returns 409. Codex R1 MEDIUM fix.
- **Active-invite predicate** (`accepted_at IS NULL AND declined_at IS NULL AND expires_at > now()`) now consistent across all 5 sites (duplicate-invite check at both creation paths, /invites/me, admin /invites, resend pre-flight). Closes Codex R1 MEDIUM + R2 LOW (predicate drift would otherwise block admin recovery after a decline + surface stale "Pending Invites" rows on the dashboard).
- **Dashboard `/invites` page** + nav link with pending-count badge in both desktop nav and mobile drawer (`dashboard/src/invites/InvitesPage.tsx` + `useInvites.ts`). Reads `?highlight=<invite_id>` from the email accept link to outline the matching row. Inline reason textarea on decline. Accept invalidates `['my-orgs']` so the org settings page refreshes.

13 regression tests covering both invite endpoints firing email, best-effort behavior under provider failure, resend without new row, resend admin-only + 409 on accepted, decline blocks subsequent accept, decline wrong-email denied, /invites/me filters to current user + hides accepted/declined, atomic accept-after-concurrent-decline returns 409 with no OrgMember leak, admin-can-reinvite-after-decline, admin pending list hides declined + expired. Three Codex review rounds → R3 VERIFIED-CLEAN.

### Verification

- pytest tests/ -x -q → **2007 passed**, 2 xfailed (pre-existing migration-003 chain)
- ruff check src/ → clean
- helm lint charts/sessionfs → clean
- dashboard tsc -b → clean
- dashboard npm test -- --run → **187 passed**
- dashboard npm run build → no warnings
- pip-audit → 0 vulnerabilities (105 deps)
- npm audit (dashboard + site) → 0 vulnerabilities
- bandit → 0 HIGH, 17 MEDIUM (all pre-existing baseline; 0 in v0.10.22 files)
- Shield-SR independent pre-release review → APPROVED 0 CRITICAL / 0 HIGH / 0 NEW MEDIUM

### Authoring + reviews

All 3 tickets implemented directly by Claude (no Codex CLI spawn), per CEO's standing rule. Polling Codex (`author_persona="codex"`) reviewed every commit:
- tk_918073e8aa4c4478: R1 MEDIUM (raw PII leak) + R1 LOW (GH prerelease policy) → R2 LOW (stale §5.1 prose) → R3 VERIFIED-CLEAN
- tk_7a457574c5624e12: R1 VERIFIED-CLEAN first round
- tk_6afbcfefe5804c1d: R1 HIGH (test assertion) + 2 MEDIUMs (reinvite-after-decline + accept/decline race) → R2 LOW (admin list filter) → R3 VERIFIED-CLEAN

### Operational unlock

After this deploys, every future org invite from either endpoint sends an email automatically; recipients see pending invites in `/invites` with a nav badge; admins can resend without re-inviting and recover from a decline by re-inviting; new org members can pull personas + read KB + see project listing without needing to clone the repo first as a workaround. The 2026-05-23 onboarding pain doesn't recur.

## [0.10.21] - 2026-05-22

**Phase 4a + the continuous-agent stack.** This release lands the agent-authored KB attribution model (Phase 4a), the MCP write-path equivalent, the full Scout v4 n8n integration contract, AgentRun provenance exposure, and an `agent_runs:read` service-key opt-in — five tickets that together unblock continuous autonomous agents on SessionFS for the first time. Plus a transitive starlette CVE pin caught by Shield-SR.

### Added

**`persona_name` + `author_class` on KnowledgeEntry** (`tk_f5ae3eea92934add`, commits `dfbd215` + `e8b9297`). Migration 045 strictly additive:
- `persona_name VARCHAR(64) NULL` — validated against `agent_personas` for the same project on write (mirrors v0.10.7 wiki page-write policy); unknown personas return 422.
- `author_class VARCHAR(16) NOT NULL DEFAULT 'human'` — every existing row backfills to `'human'` via the column default; new writes default to `'human'` for user keys, `'agent'` for service keys.
- Composite index `idx_knowledge_persona_recent (project_id, persona_name, created_at DESC)` for Scout v4's per-persona retrieval.

Anti-spoof invariant: `POST /entries/add` forces `author_class = "agent"` whenever `auth.actor_type == "service_key"`, regardless of request body. The DB-level regression `test_service_key_cannot_spoof_author_class_human` asserts at row level after `db_session.refresh(entry)`.

`GET /entries` gains three new query params with AND-composition against the existing filters:
- `persona_name` — exact match (max 64 chars)
- `author_class` — `Literal["human", "agent"]`
- `source_filter` — literal substring match via `.contains(value, autoescape=True)`; `%` and `_` in user input escaped so they match as data characters, not LIKE wildcards (Codex R1 LOW fix, commit `e8b9297`).

`KnowledgeEntryResponse` exposes both fields on every read path (list, get, dismiss, refresh, promote, supersede).

**MCP `add_knowledge` persona attribution** (`tk_8028c79963fe4dc7`, commits `73ee0ab` + `9c479c8`). MCP input schema gains `persona_name` (max 64) and `author_class` (`human|agent`). When `persona_name` is omitted AND the active-ticket bundle's `project_id` matches the resolved project (mirroring `update_wiki_page` v0.10.7), the handler auto-threads `bundle.persona_name` AND defaults `author_class` to `"agent"` — without this default the bundle path would land rows as `human` and miss the agent retrieval channel (Codex R1 MEDIUM). Explicit args win in both directions. The tool response surfaces the attribution that actually landed so callers can detect server-side overrides (anti-spoof). 8 regression tests.

**Scout v4 n8n workflow contract** (`tk_d8e02fb02d874b3f`, commits `9544c99` + `4f96e23` + `cbaf702` + `99e1fcd` + `20615b1`). New `docs/integrations/scout-n8n.md` (~620 lines) is the full contract for running Scout as a continuous analyst from n8n via the direct HTTP API. Covers the scope matrix, durable `trigger_ref` via a `Build trigger_ref` Set node using documented n8n primitives (`$exec.id` + `String.hash('sha256')`), persona preflight failure path (no AgentRun to complete on 404), POST /agent-runs flow with `/start` explicitly skipped (user-key only), KB writes with stable `source_context` format `scout:n8n:<workflow_id>:<exec_id>:<signal_id>`, dedupe + retry caps (`MAX_KB_WRITES_PER_RUN=20`, `MAX_TICKET_CREATES_PER_RUN=5`, `MAX_RETRY_PER_SIGNAL=1`), failure-branch severity matrix, and a run-N + run-N+1 smoke-test procedure that verifies the agent-memory loop end-to-end. No platform changes — uses existing v0.10.21 endpoints only. Three rounds of polling-Codex review + one n8n-engineer human review.

**`AgentRunResponse` exposes service-key audit triple** (`tk_a77b671fd86a42fb`, commit `e61d38b`). `actor_type`, `service_key_id`, `service_key_name` (v0.10.10 migration 042 columns) now serialize on every AgentRun read path (create, list, get, start, complete, cancel) via the shared `_row_to_response` helper. No DB migration. Scout n8n smoke-test (§6.1) now verifies provenance directly via `GET /agent-runs` instead of routing through the KB attribution fallback. 3 regression tests including the backward-compat invariant (user-key rows return `actor_type='user'` + null service-key fields).

**`agent_runs:read` service-key opt-in** (`tk_31b87575d5534d00`, commits `f4cded5` + `20615b1`). `GET /agent-runs` and `GET /agent-runs/{run_id}` converted from `get_current_user` to `require_scope("agent_runs:read")`, routed through `_get_project_for_auth` (v0.10.19 helper that branches on `auth.key_kind`) followed by `assert_service_key_can_access_project`. Write routes untouched — `agent_runs:write` remains required for create + complete; `/start` and `/cancel` stay user-key only by design. The scope catalog in `docs/api-keys.md` flips `agent_runs:read` from "reserved" to **✅ live**. 5 regression tests covering allow, deny, missing-scope, cross-project (`project_not_in_allowlist`), cross-org (`cross_org_denied`), and user-key backward compatibility.

### Security

**`starlette>=1.0.1` pin** (commit `3775ce0`) to close GHSA-86qp-5c8j-p5mr (host-header URL reconstruction → potential auth bypass). Shield-SR caught this during the initial v0.10.21 review; FastAPI 0.135+ requires only `starlette>=0.46.0` so the floor pin prevents the resolver from landing on a vulnerable transitive version on the next deploy.

### Verification

- `pytest tests/ -x -q` → **1985 passed + 2 xfailed** (was 1959 + 2 baseline at v0.10.20; +26 net new across the five tickets).
- `pytest tests/server/integration/test_scoped_service_keys.py tests/server/integration/test_agent_runs_api.py -q` → **78 passed** (was 42 + 18 = 60 at v0.10.20).
- `pytest tests/unit/test_mcp_server.py -q` → 104 passed (was 96; +8 for MCP add_knowledge attribution).
- Dashboard `npm test` → **187 passed** (unchanged — no frontend changes).
- `ruff check src/` → clean.
- `mypy src/sessionfs/server/routes/{knowledge,agent_runs,personas,tickets}.py src/sessionfs/mcp/server.py` → clean.
- `helm lint charts/sessionfs` → clean.
- `pip-audit` → **0 vulnerabilities** (after starlette pin).
- `npm audit` (dashboard + site) → **0 vulnerabilities**.
- bandit → 0 HIGH / 0 new MEDIUM (pre-existing MEDIUM not in v0.10.21 files).
- Migration smoke: isolated 044 → 045 → 044 SQLite upgrade/downgrade with legacy-row backfill regression → clean.
- Codex review threads — all VERIFIED-CLEAN after fixes:
  - `tk_f5ae3eea92934add` R1 LOW (source_filter wildcards) → resolved in `e8b9297`.
  - `tk_8028c79963fe4dc7` R1 MEDIUM (bundle author_class default) → resolved in `9c479c8`, R2 VERIFIED-CLEAN.
  - `tk_d8e02fb02d874b3f` R1 → R2 → R3 cycle (2 MEDIUM + 2 LOW, then 1 MEDIUM on n8n expression primitives) → R3 VERIFIED-CLEAN, plus a separate R1 LOW caught after `agent_runs:read` landed fixed in `20615b1`.
  - `tk_a77b671fd86a42fb` R1 VERIFIED-CLEAN first round.
  - `tk_31b87575d5534d00` R1 LOW (scope-coverage doc) → R2 VERIFIED-CLEAN.
- Shield-SR independent pre-release security review on the initial Phase 4a slice — APPROVED 0/0/0/0 (caught + fixed the starlette CVE).

### Scout v4 unblock

After v0.10.21 deploys, Scout v4 can run as a continuous autonomous analyst from n8n with a least-privilege service key. Each execution loads its own prior findings via `GET /entries?persona_name=scout&author_class=agent&limit=30`, writes new findings with `persona_name=scout` + a stable `source_context`, wraps everything in an `AgentRun` that surfaces service-key provenance in read responses, and runs without needing a CEO personal key for the runtime persona load. The same loop unblocks every future autonomous agent on the SessionFS fleet (Sentinel-watch, Ledger-monitor, Relay-listener).

## [0.10.20] - 2026-05-22

Phase 3.6 service-key opt-in: persona CRUD routes. Unblocks the n8n Scout agent's runtime persona load and every future autonomous agent that needs to fetch the actual persona doc as its system prompt at runtime.

### Added

**Service-key access to persona routes** (`tk_4d932478298b4e27`). 5 routes converted to `require_scope`, mirroring the v0.10.18 + v0.10.19 conversion pattern exactly:

READ (`require_scope("personas:read")`):
- `GET /projects/{pid}/personas` (list)
- `GET /projects/{pid}/personas/{name}` (detail — the route the n8n Scout's "Get Scout Persona" node calls)

WRITE (`require_scope("personas:write")`):
- `POST /projects/{pid}/personas` (create)
- `PUT /projects/{pid}/personas/{name}` (update)
- `DELETE /projects/{pid}/personas/{name}` (soft-delete via is_active=False)

Tier C (`assume_persona`, `forget_persona` — session-state mutators) intentionally NOT touched. Those mutate the caller's local provenance bundle, not a project row, and remain user-key only.

**AgentPersona audit-row columns (migration 044)**. Migration 042 (v0.10.10) added `actor_type` / `service_key_id` / `service_key_name` to 5 audit tables; v0.10.19 migration 043 added them to Ticket. AgentPersona was excluded. v0.10.20 closes that gap with a strictly-additive migration mirroring 043 exactly (nullable cols, no defaults, no constraints, no indexes, no FKs). create/update/delete persona writes now stamp the triple from `AuthContext` so the audit row records the service-key principal.

**Cross-route helper reuse**: persona routes import `_get_project_for_auth` from `knowledge.py` (mirrors `wiki._get_project_or_404` cross-route import). Service keys load by id only; user keys keep the legacy owner/session gate. `assert_service_key_can_access_project(db, auth, project)` enforces the org+allowlist boundary AFTER the helper returns at all 5 sites.

### Changed

**`docs/api-keys.md`**: `personas:read` and `personas:write` moved from "reserved" to **✅ live** with full endpoint lists. Scope-vocabulary count corrected from 14 → 15 (Codex R1 LOW fix). Scout pattern example extended to include the runtime persona-load step. New explicit note that `assume_persona`/`forget_persona` remain user-key only.

### Verification

- pytest tests/ -x -q → **1959 passed + 2 xfailed** (was 1952 + 2; +7 new regression tests)
- pytest tests/server/integration/test_scoped_service_keys.py -q → **42 passed** (was 35; +7 new)
- dashboard `npm test` → **187 passed** (unchanged)
- ruff check src/ → clean
- mypy src/sessionfs/server/routes/personas.py → clean
- helm lint charts/sessionfs → clean
- pip-audit → **0 vulnerabilities**
- npm audit (dashboard + site) → **0 vulnerabilities**
- bandit → 0 HIGH / 0 new MEDIUM (all existing MEDIUM are pre-existing)
- Migration smoke: isolated 043 → 044 → 043 SQLite upgrade/downgrade → clean
- Polling Codex review thread on `tk_4d932478298b4e27` — R1 1 LOW (scope-count typo) → **resolved** in commit `cbcd0c6`. No behavioral findings.
- Shield-SR independent pre-release security review — **APPROVED, 0 CRITICAL / 0 HIGH / 0 MEDIUM / 0 LOW**

### Scout agent unblock

After v0.10.20 deploys, the stopgap "CEO Personal Key" credential in the n8n Scout workflow can be removed and the "Get Scout Persona" node rewired back to the org-bound service key (now scoped `[personas:read, knowledge:read, knowledge:write, tickets:write]`). Every future n8n agent — Sentinel-watch, Ledger-monitor, Relay-listener — can now operate as a least-privilege service key with runtime persona load.

## [0.10.19] - 2026-05-21

Phase 3.5 service-key opt-in: ticket CREATE + knowledge routes. Unblocks the n8n Scout agent and every future autonomous discovery/research agent that gathers external signals and writes them back as KB entries + new tickets.

### Added

**Service-key access to ticket CREATE + knowledge routes** (`tk_65a096acf57946eb`). 8 new routes converted to `require_scope`, mirroring the v0.10.18 ticket-route conversion exactly:

WRITE (`require_scope("tickets:write")`):
- `POST /projects/{pid}/tickets` (ticket create — the entrypoint Scout uses to open follow-up tickets)

READ (`require_scope("knowledge:read")`):
- `GET /projects/{pid}/entries` (list/search)
- `GET /projects/{pid}/entries/{eid}` (detail)

WRITE (`require_scope("knowledge:write")`):
- `POST /projects/{pid}/entries/add`
- `PUT /projects/{pid}/entries/{eid}` (dismiss/update)
- `PUT /projects/{pid}/entries/{eid}/refresh`
- `PUT /projects/{pid}/entries/{eid}/promote`
- `PUT /projects/{pid}/entries/{eid}/supersede`

Tier C routes (`compile`, `rebuild`, `dismiss-stale`, `health`, `compilations`) deliberately NOT touched — they remain user-key only. A regression test (`test_service_key_still_denied_on_compile_rebuild_dismiss_stale`) pins the deny-by-default contract so a future Phase 4 must opt them in explicitly.

**Ticket audit-row columns (migration 043)**. Migration 042 (v0.10.10) added `actor_type` / `service_key_id` / `service_key_name` to 5 tables (TicketComment, KnowledgeEntry, AgentRun, RetrievalAuditEvent, HandoffEvent) but Ticket itself was excluded. v0.10.19 closes that audit gap with a strictly-additive migration mirroring 042 exactly (nullable cols, no defaults, no constraints, no indexes). `create_ticket` populates the columns from `AuthContext`. KnowledgeEntry mutators (dismiss/update/refresh/promote/supersede) now stamp the same triple on every write path so the audit row records the service-key principal.

**`_get_project_for_auth` helper** (Codex R1 MEDIUM 1). Service keys minted by an org admin against a project owned by a different org member previously 403'd on the legacy `_get_project_or_404(project_id, db, user.id)` user-owner / captured-session gate before the org/allowlist boundary could evaluate. The new helper branches on `auth.key_kind`: service keys load the project by id (404 only); user keys keep the legacy gate. Replaced across all 7 converted knowledge.py call sites + 8 converted tickets.py call sites. `assert_service_key_can_access_project(db, auth, project)` continues to enforce the org+allowlist boundary AFTER the helper returns.

**`knowledge:read` is now truly read-only for service keys** (Codex R1 HIGH 1). The `GET /entries` search side-effect path previously mutated `used_in_answer_count` / `last_relevant_at` / `retrieved_count` whenever the search matched. A service key with only `knowledge:read` could therefore alter freshness/decay state. v0.10.19 gates the telemetry UPDATE on `auth.key_kind == "service" and "knowledge:write" not in auth.scopes`: service keys with read-only scope no longer mutate counters; service keys with read+write keep the existing telemetry; user keys are unaffected (key_kind="user" short-circuits the AND).

### Changed

**`docs/api-keys.md`** — `knowledge:read` and `knowledge:write` moved from the "reserved for Phase 3" list to **✅ live** with full endpoint lists. New Scout-pattern example added (`search_project_knowledge` → `add_knowledge` → `create_ticket` curl flow). Audit-row narrative updated to include Ticket alongside the other 5 audit tables. Tier C remains explicitly called out as user-key only.

### Verification

- pytest tests/ -x -q → **1952 passed + 2 xfailed** (was 1950 + 2 xfailed; +2 new regression tests in this release)
- pytest tests/server/integration/test_scoped_service_keys.py -q → **35 passed** (was 33; +2 new — knowledge:read freshness gate + cross-owner project access)
- dashboard `npm test` → **187 passed**
- ruff check src/ → clean
- mypy src/sessionfs/server/routes/tickets.py src/sessionfs/server/routes/knowledge.py → clean
- helm lint charts/sessionfs → clean
- pip-audit → **0 vulnerabilities**
- npm audit (dashboard + site) → **0 vulnerabilities**
- Migration smoke: isolated 042 → 043 → 042 SQLite upgrade/downgrade → clean
- Codex independent review thread on `tk_65a096acf57946eb` — R1 NEEDS-FIXES (HIGH + MEDIUM) → R2 **VERIFIED-CLEAN** on commit `adf4955`
- Shield-SR independent pre-release security review — **APPROVED, 0 CRITICAL / 0 HIGH / 0 MEDIUM / 0 LOW**

### Scout agent unblock

After v0.10.19 deploys, the n8n Scout service key on `proj_c0242b0fccbd48b4` will be reminted with scopes `[tickets:read, tickets:write, knowledge:read, knowledge:write, agent_runs:read, agent_runs:write]` and Scout will start operating with a least-privilege org-bound key (no more CEO personal key for autonomous discovery agents).

## [0.10.18] - 2026-05-21

Opts ticket routes into service-key auth (v0.10.10 Phase 3). Unblocks the n8n triage agent and every future CI/cloud-agent integration that touches tickets.

### Added

**Service-key access to ticket routes** (`tk_1ea90b1d210d40a8`). 7 ticket routes converted to `require_scope` dependencies, mirroring the v0.10.10 agent_runs/handoffs pattern exactly:

READ (`require_scope("tickets:read")`):
- `GET /projects/{pid}/tickets` (list)
- `GET /projects/{pid}/tickets/{tid}`
- `GET /projects/{pid}/tickets/{tid}/comments`
- `GET /projects/{pid}/tickets/{tid}/review-state`

WRITE (`require_scope("tickets:write")`):
- `POST /projects/{pid}/tickets/{tid}/comments`
- `POST /projects/{pid}/tickets/{tid}/start`
- `POST /projects/{pid}/tickets/{tid}/complete`

For every converted route: `assert_service_key_can_access_project(db, auth, project)` runs before any DB work — enforces both the org boundary (`service_key.org_id == project.org_id`) and the optional per-key project allowlist. For write routes: `TicketComment` audit rows now populate `actor_type` / `service_key_id` / `service_key_name` from `AuthContext` so service keys never silently impersonate humans in provenance.

Lease-epoch fencing on `/comments` and `/complete` preserved under service-key callers. `_assert_lease_required_mode` accepts the new `AuthContext` and sources `require_lease_epoch_on_ticket_writes` from `auth.org_id` (the service key's bound org), falling back to `project.org_id` for legacy user-key paths. Stale-lease fence still uses the row-count atomic-UPDATE pattern (unchanged).

Resolve/escalate/approve/dismiss/create routes intentionally NOT converted — out of scope per the ticket. User keys with the `*` wildcard scope continue to work on all converted routes.

`docs/api-keys.md` updated: `tickets:read` + `tickets:write` moved from "reserved for Phase 3 route opt-in" to "✅ live" with full endpoint lists. New triage-bot persona example added to "When to mint one".

### Tests

7 new regression tests in `tests/server/integration/test_scoped_service_keys.py`:
- `test_service_key_can_list_tickets_with_read_scope`
- `test_service_key_can_read_ticket_detail_and_comments_and_review_state`
- `test_service_key_can_start_complete_and_comment_with_write_scope` (asserts `actor_type='service_key'` + key id/name populated on the comment row)
- `test_service_key_tickets_insufficient_scope_denied` (asserts `insufficient_scope` 403 with `required` + `current` arrays)
- `test_service_key_tickets_project_allowlist_denied_before_write` (asserts `project_not_in_allowlist` and pre-write denial with `COUNT(comments)` unchanged)
- `test_service_key_still_denied_on_unconverted_ticket_route` (POST /accept still rejects with `service_key_not_allowed`)
- `test_service_key_ticket_lease_required_mode_and_stale_fence` (422 on missing lease, 409 on stale lease, 200/201 on valid lease)
- `test_user_key_regression_on_converted_ticket_routes` (user keys still 200, audit row `actor_type='user'`)

### Notes

- **Tests:** 1942 backend + 186 dashboard passing (+7 net new). 2 xfail-strict pre-existing.
- **MCP tools:** 58 (unchanged).
- **Migrations:** 001–042 (no new — migration 042 already added the `actor_type` / `service_key_id` / `service_key_name` columns on `TicketComment`).
- **No new endpoints.** Pure auth-boundary widening on existing routes.
- **Compatibility:** Additive. User keys continue to authorize identically. Service keys gain access to the converted ticket routes; all other ticket routes (`resolve`, `escalate`, `approve`, `dismiss`, `accept`, `POST /tickets`) still reject service keys with `service_key_not_allowed`.

### Process notes

- **AgentRun pattern miss documented for follow-up.** The work shape (orchestrator spawns Codex CLI as Atlas with a ticket-scoped prompt → Codex edits + tests + reports) is exactly what v0.10.2's AgentRun + CI Integration was designed to capture. This release bypassed it (no `create_agent_run` record exists for the Codex session that did the implementation). Atlas/Forge follow-up: document the orchestrator pattern in `docs/integrations/cloud-agents.mdx` so the next external-agent spawn uses AgentRun for provenance.
- **Persona name case-sensitivity bug surfaced.** The ticket was assigned to `Atlas` (capitalized) but the active persona record is `atlas`. MCP `start_ticket` rejects with 400 because the match is case-sensitive. Worked around via `assign_persona` to `atlas`. Atlas to file a follow-up ticket: either match case-insensitively in the persona resolver or validate against the persona registry at ticket-create / assign-time.

## [0.10.17] - 2026-05-21

`knowledge_health` API + MCP fix. The `pending_entries` count was a generalized "uncompiled non-dismissed claims" — it counted superseded/stale claims (compile skips) and missed auto-promotable evidence (compile processes). Operators got "Run compile to process N pending entries" advice that lied in both directions: stayed flat after a successful compile, and reported 0 for projects with only eligible evidence.

### Fixed

**`knowledge_health.pending_entries` overcount + counterpart undercount** (`tk_935a4eb62be94676`):

- `pending_entries` now mirrors the compile pipeline's Phase 2b select EXACTLY: `claim_class='claim'` AND `compiled_at IS NULL` AND not dismissed AND `freshness_class IN ('current', 'aging')` AND `superseded_by IS NULL`.
- New `auto_promotable_evidence` field surfaces evidence rows the compiler's Phase 2a will auto-promote AND that survive Phase 2b post-promotion (`claim_class='evidence'` AND `confidence >= 0.5` AND `length(content) >= 30` AND not dismissed AND `compiled_at IS NULL` AND current/aging AND no superseder).
- New `uncompiled_notes` field counts notes that need `bulk_promote` first.
- `potentially_stale` flag uses the same compile-eligible predicate as `pending_entries` so notes / superseded claims with novel terms can't drive a false-positive "Context may be stale" warning.
- "Run compile" recommendation now drives off `compile_work_total = pending_entries + auto_promotable_evidence`. Structured breakdown below threshold: "3 pending claims + 1 auto-promotable evidence row — run compile". Single-line above threshold: "Run compile to process N entries (P pending claims, Q auto-promotable evidence)".
- "No compilations yet — run compile to build context" recommendation now gated on `compile_work_total > 0`. Notes-only fresh projects get "No compile-eligible entries yet — add claims (or promote evidence) before running compile".
- New "N uncompiled notes — call bulk_promote..." recommendation fires whenever notes exist.

MCP `get_knowledge_health` tool description rewritten to document each field with its exact filter clause so MCP agents read the same contract the route enforces.

5 new regression tests pin each surface:
- `test_health_pending_entries_matches_compile_filter`
- `test_health_counts_auto_promotable_evidence`
- `test_health_potentially_stale_ignores_notes_and_superseded`
- `test_health_auto_promotable_excludes_stale_and_superseded_evidence`
- `test_health_no_compile_advice_when_only_notes_exist`

Toggle-tested: temporarily disabling the new filter clauses produces the original overcount shape and the regression test fails with the exact pre-fix count.

### Notes

- **Tests**: 1935 backend + 186 dashboard passing (+5 net new). 2 xfail-strict pre-existing.
- **MCP tools**: 58 (unchanged — `get_knowledge_health` description rewritten, schema unchanged).
- **Migrations**: 001–042 (no new migrations).
- **API contract**: backward-compatible additive change. `uncompiled_notes` and `auto_promotable_evidence` are optional on the dashboard `ProjectHealthResponse` interface so older self-hosted servers still parse.
- **No site changes** — site already cloud-first as of yesterday's pivot.

## [0.10.16] - 2026-05-20

Follow-on hotfix. v0.10.15 closed one `uq_kl_link` violation site (`_auto_supersede`) but production exposed a SECOND site at `auto_generate_concepts`'s existing-page branch where `KnowledgeLink` rows are deleted-and-re-inserted with the same composite key in a single transaction. SQLAlchemy's UnitOfWork orders INSERTs ahead of DELETEs by default, so the INSERT fires while the old row is still present, violating `uq_kl_link`. The IntegrityError bubbles past the route's try/except, the session enters PendingRollback, and the next `_count_pages` call surfaces a Starlette text/plain 500.

### Fixed

**`/compile` 500 from delete+insert race in `auto_generate_concepts`** (`tk_09d8bdf4f6374a13`, R2 follow-up) — added explicit `await db.flush()` after the delete loop at `src/sessionfs/server/services/compiler.py:1346` and before the new-link add loop. This serialises the DELETEs to the open transaction (still rollbackable) so the INSERTs that follow see a clean slate. Plus belt-and-suspenders `await db.rollback()` in the `/compile` route's exception handler at `src/sessionfs/server/routes/knowledge.py:976-987` so a future commit failure in `auto_generate_concepts` no longer poisons the session for downstream calls like `_count_pages`.

New regression test `test_auto_generate_concepts_flushes_delete_before_insert` seeds a concept page + entry-link for the same pair, monkeypatches `check_concept_candidates` + `generate_concept_article` to force the regenerate branch, and asserts exactly one (entry, page) link survives the delete+re-add cycle.

### Notes

- **Tests:** 1930 backend + 186 dashboard passing (+1 net new regression test). 2 xfail-strict pre-existing.
- **MCP tools:** 58 (unchanged).
- **Migrations:** 001–042 (no new migrations).
- **v0.10.15 stays merged.** The pair-level dedup in `_auto_supersede` is correct and necessary. v0.10.16 closes the second site.
- **Cloud Run log diagnosis FTW.** The first attempt at v0.10.15 (commit `4d7f1f7` before the R1 fix) was based on a misdiagnosis. Reading the actual asyncpg traceback from `gcloud logging read` revealed the precise table, constraint, and stack — turning a "the worker is being killed somehow" theory into a deterministic fix at the right line.

## [0.10.15] - 2026-05-20

Hotfix release. Fixes the ACTUAL `/compile` 500 on proj_c0242b0fccbd48b4 — the v0.10.14 fix closed an audited crash class but turned out not to be the bug crashing prod.

### Fixed

**`/compile` 500 on projects with stable `contradicts` KnowledgeLinks** (`tk_09d8bdf4f6374a13`) — `_auto_supersede` in `src/sessionfs/server/services/compiler.py` was creating `KnowledgeLink` rows with `link_type='contradicts'` on every `/compile` pass without checking whether the same `(project_id, source_type, source_id, target_type, target_id)` tuple already existed. The `supersedes` path was self-gating via `older.superseded_by is not None: continue`, but `contradicts` had no such gate. Every subsequent `/compile` against a project with stable contradicts links queued a duplicate insert that SQLAlchemy autoflush surfaced on the next `db.execute()` (the decay UPDATE), raising `asyncpg.exceptions.UniqueViolationError` on `uq_kl_link`. The IntegrityError bubbled up uncaught past FastAPI's middleware, and Starlette returned its default `PlainTextResponse("Internal Server Error", status_code=500)` — exactly 21 bytes of text/plain. The v0.10.14 release notes misdiagnosed this response shape as a Cloud Run worker kill; the worker stays alive, only the request fails.

Root cause confirmed from Cloud Run logs (revision `sessionfs-api-00132-f2p`):

```
sqlalchemy.dialects.postgresql.asyncpg.AsyncAdapt_asyncpg_dbapi.IntegrityError:
<class 'asyncpg.exceptions.UniqueViolationError'>:
duplicate key value violates unique constraint "uq_kl_link"
```

Fix: prefetch all existing entry→entry link tuples `(source_id, target_id, link_type)` for the project ONCE at the top of `_auto_supersede`, then check the in-memory set before `db.add(link)` for both the supersedes and contradicts branches. In-run additions also join the set so two compiles in the same transaction can't double-add. New regression test `test_auto_supersede_idempotent_on_existing_contradicts_link` seeds the failure shape (two entries with same entity_ref + overlap in the contradicts band), calls `_auto_supersede` twice on fresh sessions, asserts no exception + flat link count across passes.

### Notes

- **Tests:** 1928 backend + 186 dashboard passing (+1 net regression test). 2 xfail-strict pre-existing.
- **MCP tools:** 58 (unchanged).
- **Migrations:** 001–042 (no new migrations).
- **v0.10.14 stays merged.** The `_safe_entry_link_ids` helper closes a real audited crash class for the malformed-link path. The two regression tests added for it remain valid.
- **My v0.10.14 root-cause writeup was wrong about "Cloud Run worker kill / GFE 21-byte text/plain 500".** The 21-byte body is Starlette's default `PlainTextResponse` for uncaught exceptions with `debug=False`. The worker stays alive between requests; only the failing request bubbles. Updated knowledge entry 408 in proj_c0242b0fccbd48b4 to reflect this.

## [0.10.14] - 2026-05-20

Hotfix release. Fixes the `/compile` availability bug that hit proj_c0242b0fccbd48b4 immediately after the v0.10.13 R5 metadata restore.

### Fixed

**`/compile` 500 on projects with malformed `KnowledgeLink` source_ids** (`tk_d92434fe63564c06`) — `_prune_dead_concept_pages` in `src/sessionfs/server/services/compiler.py` did `int(lk.source_id)` without a try/except when `source_type=='entry'`. `KnowledgeLink.source_id` is a `String(64)` column; the convention is `str(KnowledgeEntry.id)` but legacy rows can have non-numeric values (slugs, UUIDs, malformed migration data). A single bad row triggers `ValueError`. The `/compile` route's `try/except Exception` around `auto_generate_concepts` catches the raise — but earlier links in the same page's link list already had partial `await db.delete(lk)` calls. The SQLAlchemy session is left in a poisoned state. Subsequent `db.execute()` calls in the route then die with un-handled SQLAlchemy errors that bypass FastAPI's exception middleware → Cloud Run worker kill → 21-byte plain-text "Internal Server Error" from Google Frontend.

Diagnosed via: `bulk-promote` on the same project returned 200 while `/compile` returned 500 in 0.5s. The fast-fail signature ruled out timeout / OOM / LLM. Diffing `_prune_dead_concept_pages` against its sibling code in `auto_generate_concepts` (line ~1198) showed the latter has the exact `try/except (TypeError, ValueError) → continue` pattern this site was missing.

Fix shipped in two rounds:

- **R0** (`tk_d92434fe63564c06`) — mirror the sibling guard at `_prune_dead_concept_pages`. Malformed rows skip with a warning log instead of crashing.
- **R1** (Codex review `tk_e5185f5d432243f2`, HIGH) — Codex widened the audit and surfaced a remaining unguarded `int(lk.source_id)` at the per-existing-page deletion check in `auto_generate_concepts` (compiler.py:1247-1251 pre-fix), and a 4th site outside compiler.py at `src/sessionfs/server/routes/wiki.py:602` (page regenerate route). Refactored to extract `_safe_entry_link_ids(links, *, page_slug=None) -> list[int]` helper at module scope and route all 3 compiler sites through it; wiki.py:602 gets the matching inline guard. Final grep confirms only the helper's own (guarded) cast remains.

Two regression tests pin the failure shapes: `test_prune_dead_concept_pages_skips_malformed_links` (direct call path) and `test_auto_generate_concepts_existing_page_skips_malformed_links` (monkeypatches `check_concept_candidates` to force the existing-page branch where R1 HIGH lived).

This bug was masked before v0.10.13 by the destructive `/rebuild` data-loss bug (`tk_bc3c02a63e994717`): both ran together and the destructive resets ate the projection before the prune crash could be observed independently. v0.10.13's fail-closed contract isolated this one to "availability only" — zero DB drift on each failed `/compile`, confirmed in production.

### Notes

- **Tests:** 1927 backend + 186 dashboard passing (+2 net regression tests). 2 xfail-strict pre-existing.
- **MCP tools:** 58 (unchanged).
- **Migrations:** 001–042 (no new migrations).
- **No new endpoints.** Single-file backend fix.

## [0.10.13] - 2026-05-20

Incident-driven safety release. No new features. Forced by the 2026-05-20 incident on the SessionFS dev project where `/rebuild` wiped `project.context_document` and every active claim's `compiled_at` when the recompile crashed mid-flight. Two related fixes ship together.

### Fixed

**Fail-closed `/rebuild` and `/compile`** (`tk_bc3c02a63e994717`, CRITICAL) — `compile_project_context` previously committed twice inside the function (lines 419 + 522) and the `/rebuild` route added two more commits BEFORE calling it for the destructive resets. Four transaction boundaries through one logical compile, with the destructive resets in the first two — any crash after them was data-destructive. Refactored to a single atomic commit at the end of `compile_project_context`:

- Phase 1 (reads only): SELECT project FOR UPDATE + pending claims + active claims for source_manifest + sessions for persona lookup. No writes.
- Phase 2 (compute in memory, no DB writes during LLM call): build LLM prompt, call LLM (or `_simple_compile` fallback), enforce word budget, group entries, compute section page contents.
- Phase 3 (single atomic transaction): apply freshness / decay / retention / auto-promote UPDATEs; apply destructive reset if `force_rebuild=True`; update `project.context_document`; mark pending entries compiled; INSERT `ContextCompilation` (flush only — committed at end); upsert section pages; **commit once.** Any exception before the final commit rolls back everything.

New `force_rebuild: bool = False` parameter on `compile_project_context`. The `/rebuild` route is now a thin wrapper calling `compile_project_context(force_rebuild=True)`. The destructive reset (NULL `compiled_at` on every active claim, clear `context_document`) now runs INSIDE the same atomic transaction.

Codex R1 caught a related correctness bug: `force_rebuild=True` was nulling `compiled_at` but the compile still read `context_before = project.context_document` and merged new claims INTO that stale text — so dismissed/superseded/aging entries' content could survive a "rebuild" forever. Fixed by splitting the variable: `previous_context` preserves the prior document for the audit trail (`ContextCompilation.context_before` is unchanged), `compile_base_context` becomes `""` when `force_rebuild=True` so the merge genuinely starts from scratch.

3 new rollback regression tests in `tests/server/integration/test_knowledge.py`, including the headline `test_rebuild_rollback_on_compile_crash_preserves_prior_state` — the test that would have caught the 2026-05-20 incident if it had existed.

### Added

**Admin repair endpoint** (`tk_dd3ba7082ef0432e`, HIGH) — `POST /api/v1/admin/projects/{project_id}/restore-from-compilation` recovers a project's `context_document` AND the `compiled_at` metadata on participating entries from a chosen `ContextCompilation` snapshot. Body `{compilation_id: int, dry_run: bool = true}`. Reads the row, parses `source_manifest` for participating entry ids, restores both the document text and the per-entry `compiled_at` in a single atomic transaction. Admin-gated (`require_admin`); audit-logged via `AdminAction`. Codex R1 caught Python's bool-is-int trap (a body `{"compilation_id": true}` would coerce to `compilation_id=1`); fixed with explicit `isinstance(x, bool)` rejection on both `compilation_id` and `dry_run`.

Closes the metadata gap left by the v0.10.12 R1 emergency restore (which only repaired the document text via the public `PUT /context` endpoint). Provides a safe operator tool for future incidents.

9 integration tests in `tests/server/integration/test_admin.py` cover dry-run/apply, 404 on unknown compilation_id, 404 on cross-project compilation_id, non-admin 401/403, empty `context_after` 422, non-integer compilation_id 422, bool/string `dry_run` 422.

### Reviews

Codex single review thread `tk_879dbd5a5a034d0e`: R1 1 HIGH (force_rebuild empty-base) + 1 MEDIUM (strict bool rejection) → **R2 VERIFIED-CLEAN**.

Shield-SR pre-release security review: 0 critical / 0 high / 0 medium new findings. Release approved.

### Notes

- **Tests:** 1925 backend + 186 dashboard passing (+13 net over v0.10.12). 2 xfail-strict pre-existing migration-003 PG-syntax (`tk_7dc9e8764a5a4297`).
- **MCP tools:** 58 (unchanged from v0.10.12).
- **Migrations:** 001–042 (no new migrations).
- **New endpoint:** 1 (admin restore-from-compilation).
- **Non-blocking follow-ups deferred:** Duplicate freshness pass in `/rebuild` (cosmetic, can return count from `compile_project_context` later) and pre-existing duplicate concept generation in `/compile` route — both flagged by Codex as out of scope for the safety release.

## [0.10.12] - 2026-05-19

### Added

**Bulk-promote eligible KB notes to claims** (parent `tk_c64915570f4d4042`, Codex R1+R2 VERIFIED-CLEAN on `tk_03263e280f4b4732`) — the v0.10.10 confidence-clamp bug (fixed in v0.10.10) left many production KBs with hundreds of stuck note-class entries. The per-entry repair path (`PUT /entries/{id}/confidence` + `PUT /entries/{id}/promote` × N) doesn't scale past ~5 entries. SessionFS dev had 270 stuck notes; Baptist Health has the same problem. This release ships the practical bulk repair surface.

- New service `src/sessionfs/server/services/bulk_promote.py:promote_eligible_notes()` — pure-Python eligibility filter (class=note, not dismissed, not superseded, optional entry_type filter, ≥ `min_length` chars, ≥ `min_confidence` unless caller overrides via `set_confidence`, no near-duplicate against active claims). Pre-fetches active claims once; in-run promotions join the compare set so two near-duplicate notes can't both promote. Dry-run is the default and makes zero writes.
- New endpoint `POST /api/v1/projects/{pid}/entries/bulk-promote` — same auth as the single-entry promote (project membership via `_get_project_or_404`). `BulkPromoteRequest` validates `min_length∈[1, 10000]`, `min_confidence`/`set_confidence∈[0.0, 1.0]`, optional `entry_type` ≤ 64 chars. Returns `{promoted, skipped, reasons, promoted_ids, dry_run}` with a stable per-reason breakdown (`too_short`, `low_confidence`, `duplicate`, `dismissed`, `superseded`, `wrong_type`, `already_claim`).
- New MCP tool `promote_eligible_entries` (57 → 58 tools) — wraps the endpoint with local arg validation BEFORE the network call (boolean rejection on numeric fields, blank-string rejection on `entry_type`, range bounds on every numeric). 60s httpx timeout for the bulk operation.
- New CLI `sfs project promote-eligible --min-length N --min-confidence N --confidence N --entry-type X --confirm` — dry-run is the default; `--confirm` is required to mutate. Renders a verdict header + Skipped-by-reason rich table.

**Per-ticket review-state endpoint row cap** (`tk_33a25a12a5cf4dc3`, Shield-SR LOW follow-up from v0.10.11) — `GET /api/v1/projects/{pid}/tickets/{tid}/review-state` now caps `TicketComment` fetch at 500 rows (parity with `list_ticket_comments`). `ORDER BY (created_at, id) ASC` ensures the earliest review rounds always survive the cap on long threads — callers care about "what was raised first and is it closed", not the latest 500 noisy comments.

**Release-process hardening** — three improvements informed by the v0.10.11 post-mortem:

- **Pinned mypy >= 1.20** (`tk_cd196cf0421b4a6e`) in `pyproject.toml` dev extras. v0.10.11 CI on main failed because local mypy 1.19.1 missed 11 union-attr errors that CI's 1.20.2 caught. The pin is the narrowest fix for the entire class of "green local / red on main" surprises. Local now reports 1.20.2 on `pip install -e .[dev]`.
- **`.release/sanitize_main.py`** (`tk_b9b6eb47685e4916`) — deterministic Python helper for stripping private files during the develop → main merge. Reads `.release/private-files.txt`; supports `--dry-run` (default) and `--apply`; exits non-zero on residual leaks. Replaces the prior 14-chained-shell-command sweep. 8 unit tests in `tests/unit/test_sanitize_main.py` (parser + leak-finder); tests skip cleanly when `.release/` is stripped on main.
- **Post-PyPI smoke step** (`tk_4698db39fb0248b3`) — new release skill step 12b runs a fresh-venv `pip install sessionfs==X.Y.Z` + `sfs --help` shape check + new-command help check with 6×30s retry for PyPI index lag. Catches "wheel published but ships wrong contents" before users do.

**MCP handler dispatch tests** for `promote_eligible_entries` (`tk_97b693793c814f4d`, Codex R2 residual-risk follow-up) — 8 new tests in `tests/unit/test_mcp_server.py` mirroring the `update_entry_confidence` pattern: URL routing, body forwarding for all 5 args, range validation, boolean rejection on numeric fields, blank-string and non-string `entry_type` rejection, `dry_run`-must-be-bool, entry_type whitespace stripping.

### Fixed

**Codex R1 fixes on bulk-promote** (in `5a41085` on develop):
- **MEDIUM** — Dry-run/confirm asymmetry on in-run duplicates. The original implementation only appended eligible content to the compare set inside `if not dry_run`, so a dry-run with two near-duplicate notes reported both as promoted while the confirmed run correctly skipped one. Fix: append unconditionally; only the field writes stay gated on `not dry_run`. The "inspect-then-mutate" safety contract now holds. New regression test pins the parity.
- **LOW** — Boundary defaults disagreed with the service constant. The API/CLI/MCP defaulted to `min_confidence=0.85` but the single-entry promote gate and the service constant are 0.8 — entries at [0.80, 0.85) would promote one-by-one but skip in bulk. Lowered all three boundary defaults to 0.8 for parity.

### Security

- Auto-upgraded dependencies during Shield-SR audit: `cryptography` 46.0.5 → 48.0.0 (pin widened to `>=46.0.7`), `idna` 3.11 → 3.15, `mako` 1.3.10 → 1.3.12, `pygments` 2.19.2 → 2.20.0, `urllib3` 2.6.3 → 2.7.0, `pip` 26.0 → 26.1.1. Closes 9 HIGH/MEDIUM CVEs.

### Reviews

- Bulk-promote: `tk_03263e280f4b4732` R1 (1 MEDIUM + 1 LOW) → R2 **VERIFIED-CLEAN**
- v0.10.12 follow-ups (row cap + MCP dispatch tests): `tk_bf4c08059e1e42c1` — administratively closed per CEO (pure test-hardening + 1-line LIMIT guard; Codex itself pre-flagged as non-blocking residual-risk on the parent reviews)

Shield-SR pre-release security review: 0 critical / 0 high / 0 medium new findings. Release approved.

### Notes

- **Tests:** 1912 backend + 186 dashboard passing (+41 net over v0.10.11). 2 xfail-strict pre-existing migration-003 PG-syntax (`tk_7dc9e8764a5a4297`).
- **MCP tools:** 57 → 58 (+1: `promote_eligible_entries`).
- **Migrations:** 001–042 (no new migrations).

## [0.10.11] - 2026-05-19

### Added

**CLI for v0.10.10 scoped service keys** (parent tk_e0d7db15ff814c0a, Codex R1+R2 VERIFIED-CLEAN on tk_53e042ecee7e43ff) — closes the human-facing surface gap so issuing, listing, rotating, and revoking keys no longer requires curl.

- `sfs admin service-keys list|create|revoke|rotate|scopes` — org admin + Team+ tier required for mutations. Raw key prints to stdout exactly once on create + rotate with a `Save this — you won't see it again.` warning on stderr.
- `sfs auth keys list|create|revoke` — any logged-in user, personal-key surface.
- `--output-key` flag on create + rotate emits ONLY the raw key for `KEY=$(sfs admin service-keys create ... --output-key)` CI capture. Mirrors `--output-id` on `ticket create`.
- Local scope validation against `VALID_SCOPES` (imported from server schemas, single source of truth). Unknown scopes and `*` wildcard for service keys fail with exit 2 before any network call.
- Structured error codes from the server (insufficient_scope, cross_org_denied, etc.) are surfaced verbatim via `_parse_error` covering all 4 FastAPI envelope shapes ({"error": {...}}, {"detail": {...}}, {"detail": "string"}, {"detail": [validation_list]}).
- All commands inherit `SESSIONFS_API_KEY` / `SESSIONFS_API_URL` env-var auth via the shared `_get_api_config` helper.

**`docs/api-keys.md`** (tk_522991717c6446c9, Codex R1+R2 VERIFIED-CLEAN on tk_ee765a03b69045eb) — public reference covering both kinds of keys, the 14-scope vocabulary with current opt-in status (handoffs:write and agent_runs:write live today; remaining 12 scopes reserved for Phase 3 route opt-in), service-key lifecycle, cloud-agent integration recipes (Bedrock, Vertex, GitHub Actions, GitLab MR), rotation policy guidance, and complete structured error code reference. `docs/cli-reference.md` gains full option tables for `sfs auth keys` and `sfs admin service-keys`.

**Per-ticket review-state derivation** (tk_e025375272b84a95, Codex R1 VERIFIED-CLEAN on first round on tk_d7354a8032e8443b) — compact summary of open findings, closed findings, last verdict, and severity counts for long review threads. No new schema or stored state — computed at read time from existing TicketComment rows.

- `GET /api/v1/projects/{pid}/tickets/{tid}/review-state` — same auth as get_ticket (project membership + agent_tickets feature). Cross-project access denied by `_get_project_or_404`. Returns `review_state: null` for tickets without any codex-reviewer comments.
- `get_ticket_review_state` MCP tool (56 → 57 total tools) — wraps the endpoint. Description steers agents away from scraping comments themselves.
- `sfs ticket review-state <id>` CLI command — renders verdict + severity counts header, Rounds table, Open + Closed finding lists.
- Parser (`src/sessionfs/server/services/review_state.py`) extracts findings from Codex's consistent comment shape (`Codex R{N} review on tk_X: VERDICT` header + `• SEVERITY — text` bullets). Closure rule: findings raised in round N close when any subsequent round has VERIFIED-CLEAN verdict. Reopens not modeled (rare and fragile to text-match). 14 unit tests + 4 integration tests.

### Fixed

**`sfs ticket watch` DNS error** (tk_aeb8580706d84e2e, Codex R1 VERIFIED-CLEAN on first round on tk_d611718c24944110) — caught by Codex during v0.10.9 review when its sandbox got a raw `[Errno 8] nodename nor servname provided, or not known` traceback. The shared `_api_request` helper now catches `httpx.RequestError` (parent of ConnectError, TimeoutException, NetworkError, SSL errors — but NOT HTTPStatusError) and exits 1 with an actionable `Configure cloud auth first: sfs auth login --url ... or set SESSIONFS_API_URL / SESSIONFS_API_KEY` message. Every CLI command using `_api_request` benefits — not just ticket watch/comments.

**Shared `_api_request` helper** — two regression fixes from the service-keys CLI work:
- DELETE method now forwards `json_data` via `client.request("DELETE", ..., json=...)`. Httpx's `client.delete()` doesn't accept a `json` kwarg, so v0.10.10 revoke endpoints (which require a `RevokeKeyRequest` body) were dropping the reason and 422-ing.
- 204 No Content responses no longer crash `resp.json()` even when content-type is `application/json` — short-circuits to empty string when `resp.content` is empty.

**Manifest schema `retrieval_audit_id`** (tk_c19f7694009e4723) — `src/sessionfs/spec/schemas/manifest.schema.json` now allows the `retrieval_audit_id` field added in v0.10.4 (migration 039). `test_full_capture_pipeline` was failing because `additionalProperties: false` at root rejected valid manifests. Schema fixed inline before this release.

**Dashboard footer version** (commit e87953d, posted earlier) — Layout.tsx no longer hardcodes a version string. Vite reads from a new `dashboard/VERSION` file (or SFS_VERSION env / pyproject.toml fallbacks) at build time. `/release` skill now bumps `dashboard/VERSION` alongside pyproject and Chart.yaml.

### Reviews

Five separate Codex review threads, all VERIFIED-CLEAN:
- Service-keys CLI: tk_53e042ecee7e43ff (R1 1 MEDIUM + 1 LOW → R2 CLEAN)
- docs/api-keys.md: tk_ee765a03b69045eb (R1 1 MEDIUM + 3 LOW → R2 CLEAN)
- DNS error catch: tk_d611718c24944110 (R1 CLEAN first round)
- Review-state: tk_d7354a8032e8443b (R1 CLEAN first round)
- Update-entry-confidence + promote-entry MCP tools (tk_44bc8c8862304051) bundled from earlier in same cycle (R2 CLEAN)

Shield-SR pre-release security review: 0 critical / 0 high / 0 medium new findings — release approved. One LOW noted as Atlas follow-up (`tk_33a25a12a5cf4dc3`: cap row count on review-state endpoint).

### Notes

- **Tests:** 1871 backend + 186 dashboard passing (+52 net over v0.10.10). 2 xfail-strict pre-existing migration-003 PG-syntax (`tk_7dc9e8764a5a4297`).
- **MCP tools:** 54 → 57 (+3: `update_entry_confidence`, `promote_entry`, `get_ticket_review_state`).
- **Migrations:** 001–042 (no new migrations this release).
- **Phase 3 route opt-in for service keys still deferred** to v0.10.12+ — TicketComment / KnowledgeEntry / RetrievalAuditEvent write routes remain on plain `get_current_user` and reject service keys. Scopes are defined but reserved.

## [0.10.10] - 2026-05-18

### Added

**Scoped service API keys** (parent tk_2e030a85253143df, 7 Codex review rounds across two phase reviews) — replaces broad static user bearer tokens for cloud agents, CI runners, and integration partners with expirable, scope-restricted service keys. Static user tokens no longer required for Bedrock, Vertex, GitHub Actions, GitLab MR pipelines.

- **Migration 042** — additive. `ApiKey` gains `key_kind` ('user' | 'service'), `org_id` (FK organizations CASCADE, required for service keys), `scopes` (JSON list), `expires_at`, `revoked_at`, `revoke_reason`, `created_by_user_id` (FK users SET NULL), `last_used_ip` (45 chars, IPv6-safe), `service_key_name`, `project_ids` (optional project allowlist within org), `key_prefix` (real raw-key prefix captured at create/rotate for incident response). Existing user keys back-fill to `scopes='["*"]'` so every legacy token continues to authorize unchanged.
- **5 audit-row tables** (TicketComment, KnowledgeEntry, AgentRun, RetrievalAuditEvent, HandoffEvent) gain `actor_type` ('user' | 'service_key') + `service_key_id` + `service_key_name`. Service keys never silently impersonate humans in provenance.
- **Deny-by-default for service keys** (Codex Phase 2 R2 HIGH) — `get_current_user` rejects service keys with 403 `service_key_not_allowed`. The ONLY way a service key reaches a route handler is via `require_scope(*scopes)` or `require_any_scope(*scopes)` dependencies. Pre-route enforcement, not post-route middleware — side effects are impossible from unauthorized service keys.
- **Scope vocabulary** — 14 capability scopes (sessions:read, handoffs:read/write, tickets:read/write, personas:read/write, knowledge:read/write, rules:read/write, agent_runs:read/write, retrieval_audit:read, admin:*). `*` wildcard reserved for legacy user/admin keys; service keys must enumerate explicitly (rejected with 422 at create otherwise).
- **Cross-org boundary** — `assert_service_key_can_access_project` enforces that a service key's `org_id` matches the target project's `org_id` before any project-scoped state change. Optional per-key `project_ids` allowlist gives finer control. Routes that handle handoffs resolve the source session's project via `sessions.project_id` (authoritative since migration 036) — Codex Phase 2 R6 caught and fixed a `git_remote_normalized` fallback that originally "preferred the key's org" on ambiguity (bypass vector).
- **Org-scoped admin surface** — `POST/GET /api/v1/orgs/{org_id}/service-keys`, `DELETE /api/v1/orgs/{org_id}/service-keys/{id}`, `POST /api/v1/orgs/{org_id}/service-keys/{id}/rotate`. Org admin role + Team+ tier required for mutations. Cross-org 404 (not 403) for existence hiding. Personal user keys under `/api/v1/auth/me/api-keys`.
- **Structured error codes** — `api_key_revoked`, `api_key_expired`, `service_key_not_allowed`, `insufficient_scope` (with `required` + `current` arrays), `cross_org_denied` (with `key_org_id` + `project_org_id`), `service_key_project_required`, `service_key_project_not_registered`, `service_key_project_ambiguous`. Agents can distinguish rotate-needed from permission-needed without log-scraping.
- **Secret handling** — raw key returned exactly once on POST create + POST rotate. List/detail endpoints return only `key_prefix`. Tests assert raw key absent from log output on both paths.
- **Phase 2 route opt-in** — 5 handoff write routes (POST `/handoffs`, claim, revoke, decline, comments POST) + agent_runs create/complete now use `require_scope("handoffs:write" | "agent_runs:write")`. Service-key callers populate `actor_type='service_key'` on the resulting HandoffEvent and AgentRun audit rows.
- **18 new integration tests** covering deny-by-default, scope allow/deny, expiry, revocation, cross-org boundary, project-id authoritative resolution, orphan-handoff denial, raw-key-not-in-list, last_used_at/_ip updates, wildcard rejection on service keys, unknown-scope rejection.

**Knowledge base confidence fix** (tk_483cede83deb443b, Codex review tk_328006e4c6024dd8 R1-R3 VERIFIED-CLEAN) — restores the CEO-driven workflow for marking strategy/decision entries as high-confidence and promoting them into compiled context.

- **Root cause** — `POST /entries/add` silently clamped `confidence = min(confidence, 0.7)` for `session_id in ("cli-ask", "manual")`. Combined with MCP's default `session_id="manual"`, this lowered EVERY caller-supplied confidence on manual/MCP-sourced entries to 0.7, blocking promotion (gate is 0.8). Entries 403/404/405 stayed at 0.7 regardless of what the caller passed.
- **Fix** — `AddEntryRequest.confidence` is now `Optional[float]` with `Field(None, ge=0.0, le=1.0)`. When caller passes confidence explicitly, server honors it. When omitted, legacy 0.7-for-manual / 1.0-for-session-derived defaults still apply. MCP `_handle_add_knowledge` only forwards confidence when caller explicitly supplied it.
- **NEW endpoint** — `PUT /api/v1/projects/{pid}/entries/{id}/confidence` lets agents/dashboards update confidence on existing entries that got clamped before the fix landed. Repair path for legacy data.
- **Compile no-op consistency** — `POST /compile` no-op response now derives `context_words_before/after` from `project.context_document` (same source as `/health.word_count`) instead of returning 0 unconditionally. New `noop_reason` field surfaces actionable diagnosis: "No claims eligible to compile. N note(s) are uncompiled — notes do not auto-promote. Update confidence via PUT /entries/{id}/confidence then call PUT /entries/{id}/promote, which returns the specific gate failures...".
- **Shared serializer** — `_entry_to_response(entry)` helper centralizes the full `KnowledgeEntryResponse` shape so new routes can't omit fields (retrieved_count, used_in_answer_count, compiled_count, last_relevant_at, supersession_reason).

**`list_ticket_comments` MCP tool** (tk_32f3dacf1c9749bc, bundled follow-up from v0.10.9) — wraps `GET /api/v1/projects/{pid}/tickets/{id}/comments` with `since` (ISO timestamp) + `since_id` (cursor tiebreaker for same-millisecond ties) + `limit` (1-500, default 200). Unblocks autonomous Codex/Claude review polling loops over MCP.

### Reviews

- Scoped service keys foundation (Phase 1): Codex R1 scope → R2 schema → R3 implementation → R4 re-review VERIFIED-CLEAN (tk_9fefcc3832ac49da)
- Scoped service keys route opt-in (Phase 2): Codex R5 → R6 → R7 VERIFIED-CLEAN (tk_f664b480140f40c4)
- list_ticket_comments: Codex R2 CLEAN (tk_32f3dacf1c9749bc)
- KB confidence fix: Codex R1 → R2 → R3 VERIFIED-CLEAN (tk_328006e4c6024dd8). HIGH finding (MCP add_knowledge confidence clamp) caught by Codex during this review — Atlas had originally fixed only the symptom (/confidence repair endpoint) without addressing the write-path bug.
- Shield-SR independent pre-release security review: 0 CRITICAL / 0 HIGH / 0 MEDIUM. Release approved.

### Deferred to v0.10.11+

- TicketComment / KnowledgeEntry / RetrievalAuditEvent writer provenance (their routes still on plain `get_current_user` — service keys correctly rejected; full route opt-in is Phase 3)
- CLI surface for scoped service keys (`sfs admin service-keys list/create/revoke/rotate`, `sfs auth keys list/create/revoke`)
- Bedrock + Vertex + GitHub Actions + GitLab MR doc updates to use scoped service keys (`docs/integrations/*` examples)
- New `docs/api-keys.md`
- Optional `ApiKey` model_validator rejecting service keys with empty `scopes`

## [0.10.9] - 2026-05-17

### Added

**Comprehensive handoff redesign** — single-feature release that elevates handoffs from "copy a session blob to an email recipient" to a first-class coordination primitive on the same plane as tickets, personas, and agent runs.

- **Migration 041** — additive schema, zero existing data loss. 12 new columns on `handoffs` (recipient_user_id, recipient_team_id, ticket_id, persona_name, revoked_at, revoked_by_user_id, revoke_reason, handoff_kind, snapshot_persona_name, snapshot_ticket_title, sender_tier_snapshot, viewed_at) and 5 new tables: `teams`, `team_members`, `handoff_attachments` (with `project_id` for unambiguous wiki slug lookups), `handoff_comments`, `handoff_events`.
- **Exactly-one-recipient invariant** — every handoff specifies exactly one of `recipient_email` (any user by email), `recipient_user_id` (direct account match), or `recipient_team_id` (team handoff, Team+ tier). Enforced server-side via Pydantic `@model_validator(exactly_one_recipient)` with `strip_blank` field validators so whitespace-only IDs collapse to None before the count check.
- **Provenance carry-through** — sender attaches `ticket_id` + `persona_name`; on claim, the response includes an `active_ticket_payload` (ticket_id, persona_name, project_id, lease_epoch) that the recipient's CLI writes to `~/.sessionfs/active_ticket.json`. The next captured session is automatically tagged with the handed-off context. Persona-only handoffs derive `project_id` from the source session's git_remote so write_bundle has what it needs.
- **Sender curates context** — `attachments[]` list of `{kind: kb_entry|wiki_page|ticket, ref_id}` validated against the session's project at create time. At claim, attachments are re-validated against recipient's accessible projects; inaccessible refs are silently dropped and surfaced in `dropped_attachments` with structured reason (`not_accessible` | `deleted` | `invalid_id` | `unknown_kind`).
- **Lifecycle endpoints** — `POST /handoffs/{id}/revoke` (sender-only, atomic, required reason), `POST /handoffs/{id}/decline` (recipient-only, atomic, optional reason), `POST/GET /handoffs/{id}/comments` (sender + valid recipients, paged 200), `GET /handoffs/{id}/events` (audit log, paged 200).
- **Team management surface** — `routes/teams.py`: `POST/GET /teams`, `GET/DELETE /teams/{id}`, `POST/GET/DELETE /teams/{id}/members/...`. Org admin required for mutations; any org member can list. `team_handoff` feature added to Team + Enterprise tiers.
- **Existence-hiding (404-not-403)** — non-parties get 404 on `GET /handoffs/{id}`, `/claim`, `/decline`, `/comments`, `/events`, `/summary` (rewritten from 403 + email-only check). Eligibility is checked BEFORE any lazy-expire writes or status-specific responses so non-recipients can't distinguish pending vs claimed vs revoked via response codes.
- **Atomic claim race** — `UPDATE Handoff WHERE id=X AND status='pending'` runs FIRST. Race losers never write blobs or insert Session rows — eliminates orphan blob storage from concurrent team-member claims. `new_session_id` is pre-allocated and included in the atomic UPDATE.
- **Lazy expiry** — pending handoffs past `expires_at` flip to `expired` on read with audit event. Per-tier `expires_in_hours` clamping: 720h (30d) on Free/Pro/Team, 2160h (90d) on Enterprise.
- **viewed_at tracking** — first GET by a non-sender stamps `viewed_at` + emits a `viewed` event. Sender's later reads do not overwrite.
- **4 lifecycle email notifications** — `send_handoff_claimed` (to sender), `send_handoff_revoked` (to recipient), `send_handoff_declined` (to sender), `send_handoff_comment` (to other party). All templates `html.escape()` user-supplied fields. All sends are best-effort try/except — never fail the route.
- **8 new handoff MCP tools** — `create_handoff`, `claim_handoff`, `get_handoff`, `list_inbox_handoffs`, `list_sent_handoffs`, `revoke_handoff`, `decline_handoff`, `add_handoff_comment`. Alongside the `list_ticket_comments` follow-up, v0.10.9 exposes 54 tools total (was 45). All handoff tools use the shared `_handoff_api_config` helper (handoffs aren't project-scoped).
- **CLI extensions** — `sfs handoff` accepts `--to-user-id`, `--to-team-id`, `--ticket`, `--persona`, `--expires-hours`, `--attach kind:ref_id` (repeatable). New subcommands: `sfs handoffs get | revoke | decline | comment | comments | events`. `sfs pull-handoff` now persists `active_ticket_payload` to `~/.sessionfs/active_ticket.json` (the core v0.10.9 promise — without this the recipient session loses ticket+persona context).

### Review history

Full receipts across 5 rounds of Codex review on parent ticket `tk_89e90060e6314311`:
- R1 (scope): clean with 2 corrections (file paths `email.py`/`email_templates.py` not `email_service.py`; tier gating preserved at existing Pro+, not "Free=email")
- R2 (schema): 3 MEDIUM (recipient_team_id missing FK, _accessible_project_ids missed org-member projects, wiki slug ambiguity in attachment validation) + 1 LOW (blank-strip whitespace) — all fixed pre-implementation
- R3 (implementation): 2 HIGH (claim/decline existence leak via status-before-auth + pull_handoff dropping active_ticket_payload) + 4 MEDIUM (persona-only project_id, blob copy before atomic claim, /summary using old 403, attachment response missing project_id) + 2 LOW (MCP not dispatch-tested, no real team E2E test) — all fixed
- R4 (re-review): VERIFIED-CLEAN. Independent Shield-SR security review: zero critical/high findings. Release approved.

### Deferred to v0.10.10+
- Broadcast claim_policy + per-team-member fan-out
- `parent_handoff_id` relay chain (A→B→C)
- `Session.blob_refcount` for storage efficiency on unchanged claims
- Background expiry sweeper (currently lazy-on-read per Codex G)
- Team email fan-out for revoke/comment notifications

## [0.10.8] - 2026-05-16

### Fixed
- **CI test regression on v0.10.7.** `test_returns_kb_and_session_sources` passed locally but failed on the public CI runner. Root cause: `_fetch_kb_entries_raw` re-imports `load_config` from `sessionfs.daemon.config` inside the function body. The monkeypatch on `mcp_server.load_config` doesn't intercept that re-import; in CI without a real `~/.sessionfs/config.toml` on disk, `load_config` returned defaults (empty api_key) and the function early-returned `[]`, leaving `sources_cited` empty. Fix patches `sessionfs.daemon.config.load_config` directly alongside the existing `mcp_server.load_config` patch. Test now passes under CI-like conditions (no config file on disk).

No product code changes from v0.10.7; this release exists to unblock the Deploy API and Deploy MCP Server pipelines that failed on the v0.10.7 push because of the test failure. Cloud Run picks up v0.10.7's customer-ask provenance fields + `sfs ticket watch` CLI + migration 040 SQLite-compat fix here.

Version skipped 0.10.7.1 because Helm chart versions require strict SemVer (X.Y.Z) — 4-segment versions aren't valid.

## [0.10.7] - 2026-05-16

### Added
- **Customer-ask provenance fields.** Three read-side extensions to existing endpoints, all additive (no schema breaks). Same pattern as v0.10.5 `source_entries` work, extending the evidence trail to three more read endpoints.
  - **`sources_cited` on `ask_project`** — typed `list[{type: "kb"|"session", id}]` returned alongside the assembled research markdown. KB IDs come from a structured re-fetch (`_fetch_kb_entries_raw`) — no regex extraction from prose. Session IDs come from the local search index. Open for `{type: "section", slug}` once ask_project grows a compiled-section retrieval step.
  - **Wiki page revision history.** New `wiki_page_revisions` table (migration 040) stores every page edit with `revision_number`, `revised_at`, `user_id`, `persona_name`, `ticket_id`, full content snapshot. New `GET /api/v1/projects/{id}/pages/{slug}/history` endpoint with cursor pagination + `next_cursor` envelope. New `get_wiki_page_history` MCP tool (45 → 46 tools). Wiki PUT now accepts optional `persona_name` + `ticket_id`; MCP `update_wiki_page` auto-threads them from the active-ticket bundle when project matches.
  - **`personas_active` on session summaries.** New `session_summaries.personas_active` JSON list collected from manifest + per-message persona annotations. Refreshed on both deterministic and narrative regeneration paths. Documented as session-level overblocking (per-decision authorship requires summarizer prompt rework — separate ticket).
  - **Lease required-mode org setting.** Org admins can flip `Organization.settings.require_lease_epoch_on_ticket_writes = true`; complete/comment/accept ticket writes then return 422 if `lease_epoch` is omitted. Existing supplied-lease behavior unchanged. Personal projects (no org) bypass.
- **`sfs ticket watch <id>` CLI.** Polls the `GET /comments` endpoint and renders new comments live (Panel + Markdown). Flags: `--interval N` (clamped to [5, 300] seconds, default 30), `--from-author NAME` filter, `--exit-on-new` for CI scripting, `--notify` for macOS terminal-notifier. Pairs with `sfs ticket comments` (v0.10.5).

### Fixed
- **Migration 040 SQLite-incompat.** First implementation called `op.create_unique_constraint` after `op.create_table` — fails on SQLite (ALTER TABLE can't add constraints). Codex flagged across R2-R7. Fix: `sa.UniqueConstraint(...)` moved inside `op.create_table()` as a column-level argument; downgrade simplified to drop the table (constraint goes with it). An interim follow-up migration 041 was tried first but Codex correctly identified that a linear repair migration can't heal a chain that halts at the failed 040 — pivoted to in-place edit since 040 had never shipped.
- **Wiki revision provenance ownership check.** `_validate_revision_provenance` allows `ticket_id` only when the user owns the ticket through one of three roles: creator (`Ticket.created_by_user_id`), current resolver (`Ticket.resolver_user_id`), or active executor (open `RetrievalAuditContext` for this ticket created by the user via `start_ticket`, with matching `lease_epoch` AND ticket still `in_progress`). Without the executor path, agents executing colleagues' tickets couldn't attribute wiki revisions to them — defeating the agent-execution provenance use case. Lease+status gate replaces the original `closed_at IS NULL` check (which never expired because nothing in the codebase sets `closed_at`).
- **Test isolation flake.** `test_unknown_block_type_logged` was failing under full-suite runs because `tests/server/integration/test_migrations_sqlite.py` triggered alembic's `fileConfig()` which defaults to `disable_existing_loggers=True`, killing the `sfs.writeback` logger's propagation. Fixture now constructs alembic `Config` without the ini path so `fileConfig` is skipped.

### Tests
- 1727 → 1745 backend (+18: ask_project sources_cited, wiki history + revision provenance + executor-association + stale-lease + post-status, lease required-mode + personal-project bypass, `sfs ticket watch` clamping/filter/exit-on-new/404, migration smoke xfail). 186 dashboard unchanged.
- 2 xfail-strict: `test_migrations_sqlite.py` SQLite chain blocked by migration 003's `USING GIN` (broader fix tracked in `tk_7dc9e8764a5a4297`).

### Security
- Shield-SR independent pre-release review CLEAN — 0 CRITICAL / 0 HIGH / 0 MEDIUM. Codex review across 8 rounds (R1 scope + R2-R8 implementation): final R8 verdict VERIFIED-CLEAN.

## [0.10.6] - 2026-05-15

### Added
- **Kilo Code watcher (9th supported tool).** New capture-only watcher for the Kilo Code VS Code extension (`kilocode.Kilo-Code` on the marketplace, on-disk path `globalStorage/kilocode.kilo-code/`). Kilo Code is a fork of Roo Code (which is a fork of Cline) and uses the same per-task UUID storage layout with `api_conversation_history.json` and `cline_messages.json` files, so capture reuses the already-reviewed Cline parsing path. New `KiloCodeWatcher` is a thin subclass of `ClineWatcher` setting `tool="kilo-code"`. Wired into `sfs daemon`, `sfs init` auto-detect, `sfs watcher enable/disable/list`, `sfs mcp install/uninstall --for kilo-code`, `sfs recapture`, and the `sfs resume --in kilo-code` rejection path (capture-only contract matches Cline/Roo).

### Tests
- 1720 → 1727 backend (+7: parsing, UUID task ID, history_item metadata, fallback discovery, manifest tool name, watcher config default, platform-aware default storage path).

### Security
- Shield-SR independent pre-release review CLEAN — 0 CRITICAL / 0 HIGH / 0 MEDIUM. No new attack surface (storage_dir is admin-controlled TOML, same trust boundary as Cline/Roo; tool name added to closed allowlist enums on every routing site).

## [0.10.5] - 2026-05-15

### Added
- **Compile source manifest: `created_by_persona` + `compile_id`.** Each entry in `get_context_section.source_entries` now carries the resolving persona (from the source session's `persona_name`, set by the daemon's active-ticket annotation pipeline) and the parent `ContextCompilation.id`. Agent Runner SoD callers can disqualify by persona/tool/whole-compile cohort, not just by `created_by_user_id`. Persona attribution lookup is one batched SELECT per compile (bounded by distinct session_ids, not entry count); compile_id is denormalized at response time so the compile + manifest stay one atomic write.
- **Tier-aware archive unpack cap.** `sync/archive.py:validate_tar_archive` and `unpack_session` now accept `member_limit_bytes`. CLI `pull` / `sync` / `pull_handoff` thread `MAX_MEMBER_SIZE` (already reads `SFS_MAX_SYNC_MEMBER_BYTES_PAID`) into unpack. The old hardcoded 50 MB silently nullified paid-tier overrides above 50 MB — same class of bug DLP carried before v0.9.9.8. Default fallback is 100 MB, matching the server abuse cap in `_validate_tar_gz`. Self-hosted operators raising the paid-tier cap must set the same env var on every CLI host.
- **`sfs ticket comments <id>` CLI** — read-only client of the existing `GET /api/v1/projects/{id}/tickets/{id}/comments` endpoint. Closes the gap where cross-agent review threads could only be read through the dashboard. Renders each comment in a titled Panel with author + created_at; Markdown body content so code fences render legibly.

### Fixed
- **Cross-project persona leak on `source_entries`.** Persona attribution SELECT now constrains `Session.project_id == project_id AND Session.is_deleted == False`. A project-scoped KB entry pointing at a session from another project (KnowledgeEntry.session_id is plain text, not a project-validated FK) no longer leaks that session's `persona_name` into this project's `source_entries`. Deleted-session attribution degrades to `created_by_persona=null` without error. 2 regression tests added.
- **Nondeterministic `compile_id` in same-timestamp bucket.** `get_context_section` latest-compile lookup now orders by `(compiled_at DESC, id DESC)`. Since `compile_id` is part of the SoD evidence contract, identical-timestamp tiebreaks need to be deterministic. 1 regression test added.
- **Site `devalue` HIGH (GHSA-77vg-94rm-hx3p, CWE-770 DoS, CVSS 7.5).** `site/node_modules/devalue` 5.6.4 → 5.8.1 via `npm audit fix`. Astro stays on 6.3.1. No app code change.

### Tests
- 1711 → 1720 backend (+9: archive tier-aware regressions, cross-project persona leak regressions, same-timestamp compile tiebreak regression). 186 dashboard unchanged.

### Security
- Shield-SR independent pre-release review: 0 CRITICAL / 0 HIGH / 0 MEDIUM after `devalue` fix. Codex R2 on `tk_12e6d8775eb045a2` (compile source manifest): no findings. Codex review on archive tier-aware change (KB 396): no blocking issues, one informational note on env-parity for self-hosted operators (documented in `docs/environment-variables.md`).

## [0.10.4] - 2026-05-15

### Added
- **Migration 039 — ticket lease epochs + context source manifest + retrieval audit tables.** Adds `tickets.lease_epoch` (NOT NULL DEFAULT 0), `context_compilations.source_manifest` (TEXT NOT NULL DEFAULT '{}'), `sessions.retrieval_audit_id` (nullable, indexed), and two new tables: `retrieval_audit_contexts` (one row per ticket start, links to project + ticket + persona + lease_epoch + created_by_user) and `retrieval_audit_events` (events per context, with serialized arguments/returned_refs, source flag, caller_user_id).
- **Ticket lease fencing.** `start_ticket` now atomically increments `lease_epoch` via `UPDATE ... WHERE status IN (...) SET lease_epoch = lease_epoch + 1`. `complete_ticket`, `add_ticket_comment`, and `accept_ticket` accept optional `lease_epoch` in the request body and use it as an inline WHERE-clause predicate. Stale daemons get 409 with a clear current-vs-supplied message. Coordinated audit, not strict mutex — opt-in semantics documented on the MCP tool descriptions.
- **Compile source manifest.** `compile_project_context` snapshots the active KB claims feeding each section before LLM compilation and stores them on the compilation row. `get_context_section` returns `source_entries` so SoD/audit callers can trace rendered prose back to the claims that shaped it.
- **Retrieval audit log (server primary + local fallback).** Server: 4 new routes — `POST /api/v1/projects/{id}/retrieval-audit-contexts`, `POST /api/v1/projects/{id}/retrieval-audit-events`, `GET /api/v1/retrieval-audit-contexts/{ctx}/events`, `GET /api/v1/sessions/{id}/retrieval-log`. Local fallback: `~/.sessionfs/retrieval_logs/<id>.jsonl` for offline MCP clients. MCP records context-shaping retrievals (search_project_knowledge, get_wiki_page, get_persona, get_compiled_rules, get_context_section, find_related_sessions, get_session_context) when an active ticket bundle has `retrieval_audit_id`; sessions persist the id from `active_ticket` manifests so the audit chain survives capture.
- **MCP `get_session_retrieval_log` tool** (44 total). Server-success path and local-fallback path return identical top-level keys `{session_id, retrieval_audit_id, events, count}`; local rows are lifted into the server's `RetrievalAuditEventResponse` shape with `source="local"` so consumers parse one schema.
- **`sfs persona pull [<name>|--all]` CLI** — pulls server-side personas to local `.agents/*.md` files. Preserves the existing kebab-naming convention on re-pull (`<name>-<role-fragment>.md`); HTML-comment preamble only (no double H1 against the persona's own identity header). Release-only personas (`shield-security-review.md`, `scribe-site-sync.md`) untouched — no server equivalent.
- **Site messaging rewrite for v1.0 positioning.** Landing / features / enterprise / pricing reframed around Memory / Identity / Coordination / Governance pillars. "Your team today. AI agents tomorrow." hybrid-operator section on enterprise. Cloud-agent-ready strip (Bedrock, Vertex, custom API) phrased precisely as "via integration docs" — not a hosted gateway.

### Fixed
- **Site `/dlp/` 404 + stale secret-pattern count.** Landing DLP card was linking to a non-existent route; repointed to `/enterprise/` where the Compliance Built In pillar describes DLP. Secret-pattern count corrected from stale 22 → 19 to match `sessionfs.security.secrets.SECRET_PATTERNS`. Historical changelog entries untouched.
- **Retrieval-audit defenses (10 sentinel findings + 2 Codex follow-up findings closed).** Path traversal: `SAFE_AUDIT_ID_RE` regex validates audit/session IDs at every entry point (audit_session_id, audit_context_id, record_retrieval, read_retrieval_log, MCP `get_session_retrieval_log`). Session upload validates claimed `retrieval_audit_id` exists in `retrieval_audit_contexts` for the same project AND was created by the uploading user — silently drops the field with a warning otherwise. Context create validates supplied `ticket_id` and `persona_name` belong to the same project; rejects forged provenance with 422. Event creator must own the context (`_assert_context_owner`). Audit event payloads capped at 16 KiB with `_truncated` marker. Cross-project SELECT leak closed via `Project.id IN (accessible_projects_subquery)` filter in initial WHERE. Comment lease check is now atomic (`INSERT … SELECT WHERE lease_epoch = ?`). Local JSONL caps at 10 MiB. `sanitize_arguments` strips any key containing api_key/token/secret/password/auth/credential. `collect_returned_refs` walker has 50-level depth limit and no longer regex-extracts `KB N` / `Entry N` from arbitrary string fields.

### Tests
- 1626 → 1711 backend (+85: retrieval-audit API regression suite, ticket lease tests, compile source-manifest assertions, sentinel hardening regressions). 186 dashboard unchanged.

### Security
- Shield-SR independent pre-release review: 0 CRITICAL / 0 HIGH / 0 MEDIUM. One LOW noted: `_get_project_or_404` vs `_accessible_project_ids_subquery` give different visibility for context-create POST vs context-events GET (GET stricter). Direction is defensive, not permissive. Tracked as Atlas follow-up; not release-blocking.

## [0.10.3] - 2026-05-14

### Added
- **Dashboard UI: Personas, Tickets, AgentRuns tabs.** Three new ProjectDetail tabs surface the v0.10.1 personas + tickets and v0.10.2 AgentRun backends. Read-focused MVP — the moderation transitions a human reviewer wants ship; the FSM transitions tied to the local active-ticket bundle stay in CLI/MCP. Reviewed across 2 rounds of Codex review on ticket `tk_530dfba7f14446dd`; full receipts there.
  - **Personas tab** — list with role + specializations + last-updated; create modal with name-immutable + ASCII regex pattern + markdown content; edit modal; delete with `--force` toggle for the server's non-terminal-ticket guard.
  - **Tickets tab** — list filtered by status (all 7 FSM states + All); expand-in-place detail panel (description, acceptance criteria as ☐ boxes, dependency list, completion notes, comments); approve (suggested→open) + dismiss (suggested/open→cancelled) buttons gated by current status; inline comment composer; new-ticket modal.
  - **AgentRuns tab** — audit-trail read-only view (CI-driven on the backend); status + persona + trigger filters; 30s refetchInterval; expand to view tool / ticket / trigger ref / fail-on / duration / structured findings JSON. Clickable CI run URL goes through a `safeHttpUrl()` allowlist (http/https only) so a crafted `javascript:` or `data:` URL falls back to plain text.
- **API client + react-query hooks** for all three surfaces. 14 new methods on `ApiClient` (list/get/create/update/delete personas; list/get/create/approve/dismiss/comment tickets; list/get agent-runs). New typed interfaces mirror the server Pydantic responses field-for-field.

### Fixed
- **CLI `sfs agent complete --findings-file` now rejects non-object list elements locally.** The API model is `list[dict[str, Any]]` and rejects `[1]` or `["bad"]` with a 422 that leaves the run stuck in `running` for non-CI users. The CLI now enumerates elements and reports the index + type of the first non-object before the HTTP call.
- **`handle_errors` decorator preserves `typer.Exit(N)` exit codes.** Latent bug: `typer.Exit` is a `RuntimeError`, not a `SystemExit`, so the generic `except Exception` was silently downgrading every `typer.Exit(N)` (N != 0) to `SystemExit(1)` with `"Unexpected error: N"`. Affected 7 sites across cmd_agent / cmd_persona / cmd_config / cmd_project. The decorator now catches `click.exceptions.Exit` explicitly and re-raises as `SystemExit(exit_code)`.
- **Mypy union-attr error in `sfs agent run` timeout polling.** `r3.get('status')` was called inside an `'r3' in dir()` guard, but mypy correctly rejected `.get()` on the union return type. Replaced with `isinstance(r3, dict)` narrowing + pre-initialised `r3: dict[str, Any] | list[Any] | str = {}`. CI hotfix already on main; bundled here for the release commit.
- **Dashboard `addToast` signature.** `PersonasTab` + `TicketsTab` had 10 call sites using `addToast({ kind, message })`; the hook exports `addToast(type, message)`. `npm run build` was failing TS2554 at every site even though the vitest mocks were permissive enough to mask it. All 10 sites rewritten; test mocks now assert against the positional shape.
- **Dashboard `ci_run_url` rendered without scheme guard.** Operator-supplied URL was rendered as `<a href={url}>` with `rel="noreferrer"`. Now goes through `safeHttpUrl(raw)` which parses via `new URL()` and only allows `http:` / `https:`; everything else (`javascript:`, `data:`, malformed) falls back to plain text. `rel` upgraded to `"noopener noreferrer"`.
- **DeleteConfirmModal Esc close.** The persona-delete modal trapped focus via `useFocusTrap` but missed the `keydown` Esc handler that the create/edit and new-ticket modals already used. All three modals now share the same close-on-Escape behaviour.

### Tests
- +2 backend (1686 → 1688): findings-file non-object rejection + `handle_errors` Exit-preservation regression.
- +21 dashboard (165 → 186): three new tab test files covering happy + filter + expand + action-gating + toast surfacing + URL safety + Esc close.

### Security
- Shield-SR independent pre-release review CLEAN — zero CRITICAL/HIGH findings, zero pip-audit / npm audit vulnerabilities (104 Python + dashboard deps). Bandit clean (no new findings; pre-existing MEDIUMs in unchanged files). `dangerouslySetInnerHTML`: 0 occurrences in new dashboard code. Codex R1 HIGH on `ci_run_url` confirmed closed.

## [0.10.2] - 2026-05-14

### Added
- **AgentRun + CI Integration.** New layer on top of v0.10.1 personas + tickets: an AgentRun is one auditable execution of one persona, optionally against one ticket, with trigger metadata, severity, findings, and CI-friendly policy enforcement. Tracking + enforcement, not model auto-spawning — the CI script picks the LLM, SessionFS records the result. Cross-agent reviewed across 10 rounds (Codex R1–R10); full receipts on ticket `tk_f5381e113f144be5`.
- **Migration 038** — `agent_runs` table. Project-scoped FK; `persona_name` / `ticket_id` / `session_id` intentionally plain strings (no FK) for audit-row survival across persona-renames and session-deletes. Columns: status, severity, trigger_source/ref, ci_provider/run_url, findings JSON-as-text (`NOT NULL DEFAULT '[]'`), fail_on threshold, policy_result, exit_code, duration_seconds, started_at/completed_at. Five indexes: `idx_agent_run_project_status`, `idx_agent_run_ticket`, `idx_agent_run_project_persona`, `idx_agent_run_project_trigger`, `idx_agent_run_project_created`.
- **AgentRun REST API** — `POST/GET/POST start/POST complete/POST cancel` at `/api/v1/projects/{id}/agent-runs`. Atomic `UPDATE ... WHERE status IN (...)` rowcount-1 guards on every transition. Same-project ticket validation (cross-project ticket-id rejected with 422). Errored/failed final statuses force `exit_code=1` so `--enforce` always fails CI on crash. **Tier: Team+ (`agent_runs`).** Router registered before `projects.router` catch-all.
- **CLI `sfs agent`** — 4 subcommands: `run` (create + start + emit compiled context to `--context-file`), `complete` (record findings + severity + policy result with `--enforce` for CI exit-code propagation), `status` (text / json / markdown formats; markdown is GitHub/GitLab step-summary compatible), `list` (filter by persona/status/trigger_source/limit). Machine-safe `--output-id` flag prints only the run id; polling status output is routed to stderr in output-id mode. Defense-in-depth: `--enforce` exits non-zero for any `failed`/`errored` status even when stored `exit_code` is 0.
- **`sfs ticket create --output-id`** — companion machine-safe flag for CI scripting. Stdout is exactly the ticket id; the human "Created ticket …" confirmation goes to stderr.
- **`approve_ticket` MCP tool** — mirrors the `sfs ticket approve` CLI; moves a `suggested` ticket → `open`. 409 surfaces as a readable status-conflict error so agents can recover.
- **Session-ops MCP tools** — three new local-only tools that operate on `~/.sessionfs`: `checkpoint_session` (named snapshot of manifest + messages, regex-validated name `^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$` blocks path traversal), `list_checkpoints` (oldest first, returns name + created_at + message_count + on-disk path), `fork_session` (forks the live head or a named checkpoint into a new session with `parent_session_id` + optional `forked_from_checkpoint` lineage in the manifest). Shared pure helpers in `src/sessionfs/session_ops.py`; CLI `sfs checkpoint` / `sfs fork` refactored to call the same helpers (DRY).
- **MCP tools (7 new)** — `create_agent_run`, `complete_agent_run`, `list_agent_runs`, `approve_ticket`, `checkpoint_session`, `list_checkpoints`, `fork_session`. Total MCP tool count: 36 → 43.
- **Cloud Agent Control Plane** — Bedrock + Vertex AI integration. `docs/integrations/bedrock-action-group.yaml` + `docs/integrations/bedrock_lambda.py` (Bedrock action-group dispatcher with closed `OPERATIONS` table + `parse.quote(safe="")` on path params blocking traversal); `docs/integrations/vertex_tools.py` (function-calling schema + dispatcher). Public docs at `site/src/content/docs/integrations/cloud-agents.mdx`.
- **CI Integration docs page** — `site/src/content/docs/integrations/ci-integration.mdx` with policy matrix, crash-safety patterns, and the PR-injection hardening playbook (token scoping, `${{ }}` template-substitution avoidance, `persist-credentials: false`, separate `comment-on-pr` job) developed across the 10-round Codex review.
- **GitHub Actions + GitLab CI example workflows** — `docs/integrations/github-actions-agent-run.yml` + `docs/integrations/gitlab-agent-run.yml`. Defense layers: per-step `SESSIONFS_API_KEY` env scoping (off the PR-controlled review step), `persist-credentials: false` on `actions/checkout`, `permissions: contents: read` only, PR title/body via `env:` (never `${{ }}` template), `jq -e 'type == "array" and all(.[]; type == "object")'` findings-shape guard routing crash / missing / malformed / non-object-element to the `errored` complete path so runs never stick in `running`, workspace-relative `.sessionfs/` paths so `hashFiles()` works AND artifacts upload, commented-out separate `comment-on-pr` job demonstrating the correct GitHub-Actions pattern (workflow- or job-level `permissions:`, never step-level).
- **Env-var auth for CI** — `_get_api_config` now honors `SESSIONFS_API_KEY` / `SESSIONFS_API_URL` env vars before falling back to `~/.sessionfs` sync config, so a fresh CI runner with only the documented secret authenticates without `sfs auth login` first.
- **Manifest schema additions** — `persona_name`, `ticket_id`, `instruction_provenance`, `_resume_parent_id` properties added to `src/sessionfs/spec/schemas/manifest.schema.json`. Fixes a Phase-6 bug where active-ticket annotation collided with `additionalProperties: false`.
- **Tool-aware token budgets** — `_compile_persona_context` now recognises `bedrock` (16000 tokens) and `vertex` (8000 tokens) alongside the existing tool aliases.

### Tests
- +60 tests over v0.10.1 (1626 → 1686 backend tests). 27 AgentRun coverage (lifecycle, policy matrix, atomic concurrent-start race, cross-project guards, tier gate, errored/failed exit-code forcing); 9 `sfs agent` CLI tests (`--format markdown` step-summary, `--format json`, `--output-id` polling stream-routing, env-var auth precedence, `--enforce` defense-in-depth); 14 MCP coverage for the new tools (approve dispatch + 409 path, checkpoint/list/fork happy paths, name regex rejection, duplicate-name rejection, missing-session, missing-checkpoint, prefix resolution); plus Cloud Agent integration smoke tests.

### Security
- Independent Shield-SR pre-release review: zero CRITICAL/HIGH findings, zero pip-audit / npm audit vulnerabilities (104 Python + 390 npm deps), zero bandit HIGH, no hardcoded secrets. v0.10.2 surface follows v0.10.1 patterns: project-scoped FKs, atomic-UPDATE FSM guards, tier-gated routes, project-scoped lookups failing closed to 404. The CI YAML examples carry the hardening from 10 rounds of Codex review.

## [0.10.1] - 2026-05-13

### Added
- **Agent Personas + Ticketing System.** v0.10.1 is the first SessionFS release with first-class persona + ticket management. Personas are portable AI roles per project; tickets are self-contained units of work with a server-enforced FSM, dependency graph, comments, and an active-ticket provenance bundle that tags every captured session with `persona_name` + `ticket_id`. Built across 6 phases under the cross-agent review pattern; full receipts under KB `entity_ref=agent-personas-tickets-v0.10.1-phase-{1..6}`.
- **Migration 037** — 4 new tables: `agent_personas` (project-scoped, ASCII-name UNIQUE, soft-delete via `is_active`), `tickets` (FSM: suggested → open → in_progress → blocked → review → done | cancelled; reporter provenance split into `created_by_user_id`/`session_id`/`persona`; JSON-as-text columns for context_refs/file_refs/related_sessions/acceptance_criteria/changed_files/knowledge_entry_ids, all `NOT NULL DEFAULT '[]'`), `ticket_dependencies` (composite-PK edge table with `idx_ticket_deps_depends_on` for reverse lookup), `ticket_comments` (slack-like, non-idempotent). Also two new columns on `sessions`: `persona_name VARCHAR(50)` and `ticket_id VARCHAR(64)` with `idx_sessions_ticket_id`.
- **Persona CRUD API** — `GET/POST /api/v1/projects/{id}/personas`, `GET/PUT/DELETE /api/v1/projects/{id}/personas/{name}`. ASCII regex `^[A-Za-z0-9_-]{1,50}$` (no Unicode leak). Pre-check duplicate before insert with narrow IntegrityError catch on `uq_persona_project_name`. **Tier: Pro+ (`agent_personas`).**
- **Persona-delete guard** — `DELETE /api/v1/projects/{id}/personas/{name}` refuses 409 when non-terminal tickets (suggested/open/in_progress/blocked/review) reference the persona. `?force=true` overrides; terminal `done`/`cancelled` tickets never block. Protects MCP, CLI, and any future client equally.
- **Ticket CRUD + lifecycle API** — 13 routes under `/api/v1/projects/{id}/tickets`. Atomic `UPDATE ... WHERE status='X'` rowcount-1 guards on `start_ticket` and `accept` (no double-accept duplication). Cross-project dependency validation in `_validate_dependencies_same_project` + JOIN-filtered `_enrich_dependents` (belt + suspenders against legacy bad edges). Dependency cycle detection via BFS. `list(dict.fromkeys(...))` dedup on `depends_on` before validation/cycle-check/insert. Agent-created tickets (source='agent') require ≥1 acceptance criterion + ≥20-char description, max 3 per session_id. **Tier: Team+ (`agent_tickets`).**
- **Ticket comments API** — `GET/POST /api/v1/projects/{id}/tickets/{ticket_id}/comments`. Slack-like non-idempotent (each call creates a row). Project-gated via `_get_project_or_404` + `_get_ticket_or_404`. Inherit `agent_tickets` tier gate.
- **Compiled persona + ticket context** — `start_ticket` now returns `{ticket, compiled_context}`. Markdown assembled from persona content + ticket section (description, criteria as checkboxes, file refs, KB claims, completed-dep notes) + recent comments. Tool-aware truncation via `?tool=` query param (`claude-code`=16k, `codex/gemini/copilot/amp/generic`=8k, `cursor/windsurf/cline/roo-code`=4k tokens; char budget = tokens × 4 with explicit `[...truncated to fit tool token limit]` marker).
- **Cross-project leak defense** — `_compile_persona_context` filters every data-exposure SELECT by `project_id`: KB claims filtered by `KnowledgeEntry.project_id == ticket.project_id` AND `claim_class='claim'` AND `superseded_by IS NULL` AND `dismissed=False` AND `freshness_class IN (current, aging)`; dependency completion notes filtered by `Ticket.project_id == ticket.project_id` AND `status='done'`. `context_refs` cast to int with non-numeric drops (prevents driver-dependent coercion).
- **MCP tools (8 new)** — `list_personas`, `get_persona`, `list_tickets`, `get_ticket`, `start_ticket` (writes local provenance bundle), `create_ticket`, `complete_ticket` (clears owned bundle, attaches `provenance_warning` on write failure), `add_ticket_comment`. Total MCP tool count: 22 → 30.
- **Shared active-ticket bundle** — `src/sessionfs/active_ticket.py` exposes `bundle_path()`, `read_bundle()`, `write_bundle()` (returns bool, surfaces `provenance_warning` to MCP and yellow warning to CLI on OSError), `clear_bundle_if_owned()` (reads-before-unlink; only removes when both `ticket_id` AND `project_id` match the completing ticket — never deletes another tool's bundle).
- **CLI `sfs persona`** — 5 commands: `list`, `show` (`--raw` to skip markdown render), `create` (`--content` / `--file` / `$EDITOR`), `edit` (opens content in `$EDITOR`), `delete` (`--force` flag passes through to server guard; on 409 surfaces server message via `body.get("detail") or body.get("error", {}).get("message")` envelope-fallback).
- **CLI `sfs ticket`** — 12 commands: `list`, `show`, `create`, `start` (writes bundle, prints compiled context, `--tool` / `--force` / `--no-print-context`), `complete` (clears bundle iff owned), `comment` (`--as PERSONA`), `status` (Rich panel from bundle), `block`, `unblock`, `reopen`, `approve`, `dismiss`.
- **Daemon integration** — all 7 watchers (claude_code, codex, copilot, cursor, gemini, amp, cline) call `annotate_manifest_with_active_ticket(session_dir)` after capture, which reads the local bundle and adds `ticket_id` + `persona_name` to the session's `manifest.json`. The server-side `_extract_manifest_metadata` sanitizes (str.strip, 50/64-char truncation matching column widths) and the 4 Session-write sites populate the columns. Manifest is the source of truth, so re-syncing the same `.sfs` from a different machine preserves the original tagging.
- **Router precedence** — `personas.router` and `tickets.router` registered BEFORE `projects.router` in `app.py` so their `/{project_id}/(personas|tickets)/...` paths beat the catch-all `/{git_remote_normalized:path}` (same trick used by rules + project_transfers).

### Tests
- +124 tests over v0.10.0 (1502 → 1626 backend tests). 11 schema canaries (incl. AST-based migration source nullability check), 24 persona-CRUD tests, 28 ticket FSM + dependency + atomicity tests, 13 ticket comments + compiled context + cross-project leak regressions, 14 bundle module tests, 9 CLI smoke + error-envelope tests, 7 watcher annotation tests, 4 session-upload provenance roundtrip tests, plus updated MCP server tool-count + dispatcher tests for Phase 8 agent-workflow tools (16 new tests including +2 Round 2 regressions for forget_persona ticket-bundle refusal and escalate audit-comment warnings).

### Cross-agent review history
- **Phase 1** (schema): 3 rounds. MEDIUM nullable=False missing on 6 JSON columns; LOW missing reverse-lookup index; LOW nullability canary only ORM-shaped (added AST-based migration source assertion).
- **Phase 2** (persona CRUD): 2 rounds. LOW Unicode `str.isalnum()` leak (replaced with explicit ASCII regex); LOW broad `IntegrityError` masked FK failures (pre-check + narrow constraint-name catch).
- **Phase 3** (ticket CRUD + FSM): 3 rounds. MEDIUM cross-project dep validation; MEDIUM non-atomic accept (UPDATE...WHERE status='review' rowcount guard); LOW duplicate-deps composite-PK collision (dedup with `list(dict.fromkeys(...))`).
- **Phase 4** (comments + compiled context + MCP): 3 rounds. HIGH cross-project KB claim leak in compiled_context (project_id + dismissed + freshness filter); LOW provenance bundle ownership check (read-before-unlink); MEDIUM cross-project dep completion_notes leak (same project_id filter on done_deps).
- **Phase 5** (CLI + shared bundle): 3 rounds. MEDIUM persona-delete strands tickets (server-side guard + `?force=true`); LOW bundle write failures swallowed (bool return + CLI yellow warning + MCP `provenance_warning`); LOW CLI 409 envelope mismatch (read both `body["detail"]` and `body["error"]["message"]`).
- **Phase 6** (daemon integration): bundle propagation across all 7 watchers + manifest extractor + 4 server write-sites + sanitization at column widths.
- **Phase 7** (docs): docs/CLI-reference + README features + CHANGELOG narrative.
- **Phase 8** (agent workflow tools): 6 new MCP tools (`create_persona`, `assign_persona`, `assume_persona`, `forget_persona`, `resolve_ticket`, `escalate_ticket`) at customer request. Total MCP tool count 30 → 36. New `clear_bundle()` helper and persona-only bundles (`ticket_id=null` + `persona_name` set) so the daemon tags ad-hoc agent work that isn't tied to a ticket. CLI additions: `sfs persona assume|forget`, `sfs ticket assign|resolve|escalate`.

## [0.10.0] - 2026-05-13

### Added
- **Org Admin Console.** v0.10.0 is the first SessionFS release with full org-level administration. Org admins manage members, transfer projects between scopes, edit org defaults, and link captured sessions to org-scoped projects from a single Organization page and a parallel CLI surface. Built across 7 phases over ~5 hours of cross-agent review; full receipts under KB entry_ref `org-admin-console-v0.10.0-phase-{1..7}`.
- **Migration 035** — `projects.org_id` (nullable FK → organizations.id, ON DELETE SET NULL) gives projects an explicit org scope. `users.default_org_id` (same shape) stores each user's preferred org for multi-org routing. New `project_transfers` table provides a durable audit + state machine for cross-scope project moves. ON DELETE SET NULL on `project_id` so an audit row survives the project being hard-deleted; the `project_git_remote_snapshot` column (stable identity — git_remote_normalized is unique server-wide) keeps historical transfers identifiable.
- **Migration 036** — `sessions.project_id` (nullable FK → projects.id, ON DELETE SET NULL, indexed). Server resolves the linkage on every sync from the workspace's git remote so the org-scope of any captured session is recoverable.
- **Project transfer API.** `POST /api/v1/projects/{id}/transfer` initiates, `POST /api/v1/transfers/{xfer_id}/{accept,reject,cancel}` mutates state. Atomic `UPDATE ... WHERE state='pending'` with rowcount check prevents double-accept. Partial-unique index `idx_project_transfers_pending_unique` is the DB-level backstop for concurrent-initiate races. `GET /api/v1/transfers?direction=&state=` for inbox listing. Auto-accept when initiator == target (personal → own org). Standing is re-validated on accept/reject — a member demoted between initiate and accept loses the right to act on the transfer.
- **Multi-org member management API.** `GET /api/v1/orgs` lists the caller's memberships with role. `GET /api/v1/orgs/{org_id}/members` lists, `POST /api/v1/orgs/{org_id}/members/invite` invites, `PUT /api/v1/orgs/{org_id}/members/{user_id}/role` promotes/demotes, `DELETE /api/v1/orgs/{org_id}/members/{user_id}` removes. Removal preserves all member-authored data (CEO-mandated "data stays, access revoked" invariant) — sessions stay user-owned, org-scoped projects auto-transfer to the removing admin with a ProjectTransfer audit row, KB entries stay with authorship preserved, default_org_id cleared if it pointed at this org, and pending transfers tied to the removed user's standing here are cancelled. `SELECT ... FOR UPDATE` row-locks the admin rows before any last-admin guard so concurrent cross-demotion/cross-removal can't leave the org with zero admins.
- **Org settings API.** `GET/PUT /api/v1/orgs/{org_id}/settings` for the three KB creation defaults (`kb_retention_days`, `kb_max_context_words`, `kb_section_page_limit`). Stored in `Organization.settings` JSON under the `general` key (parallel to the existing DLP block under `dlp`). Admin-only PUT with range validation; any member can GET. The DLP block survives a general-settings PUT via structural merge. New org-scoped projects inherit the three kb_* defaults from the org at project-create time.
- **Default-org API.** `GET /api/v1/auth/me` now includes `default_org_id`. `PUT /api/v1/auth/me/default-org` sets it (membership validated; non-member 403); passing null clears.
- **Session project resolution.** Both upload surfaces (`POST /api/v1/sessions` and `PUT /api/v1/sessions/{id}/sync`) now resolve `session.project_id` from the workspace git remote via a shared `_resolve_project_id_for_session` helper. Resolution runs inside the write transaction with `SELECT ... FOR UPDATE` on both the Project and OrgMember rows so concurrent project deletes / membership removals can't leave a session row pointing at a project the user no longer has access to. Re-sync re-evaluates: pre-Phase-5 sessions retroactively pick up project_id when the project is created later; stale linkages clear when access is revoked.
- **Dashboard surfaces.** New `MembersTab` (org members management with CEO data-stays modal copy), `TransferInbox` (`/transfers` route, lists incoming + outgoing pending with Accept/Reject/Cancel), `TransferPanel` (per-project Transfer tab on the Project page with destination dropdown and pending-cancel branch), `OrgSettingsTab` (KB creation defaults form mounted on the Organization page). New nav link `/transfers` with a pending-incoming badge mirroring the Handoffs badge.
- **CLI.** `sfs project init --org <id>` and `--personal` for explicit project scope. `sfs project transfer --to|--accept|--reject|--cancel <id>` and `sfs project transfers --direction --state` for transfer ops. `sfs config default-org [<id>|--clear]` for default-org preference (server-canonical).

### Changed
- **`/api/v1/auth/me` response** now includes `default_org_id` (nullable string). All other fields unchanged.
- **`POST /api/v1/projects/`** request body gains optional `org_id`. Caller must be a member; non-members get 403. Personal scope (omitted) preserves the pre-v0.10.0 behavior.
- **`/api/v1/org/members/...` (legacy single-org)** routes delegate to the same `perform_member_removal` / `perform_role_change` services as the new multi-org routes, so both URL surfaces enforce the same data-stays invariants.

### Notes
- 1502 backend tests + 165 dashboard tests passing (was 1384 + 117 at v0.9.9.12). +118 backend (project transfers, org members, default-org routing, org general settings, creation-time inheritance). +48 dashboard (MembersTab, TransferInbox, TransferPanel, OrgSettingsTab, useTransfers hook-level cache invalidation).
- 5 new MCP tools? No — MCP unchanged. v0.10.0 is API + dashboard + CLI surface only.
- Migrations 035 and 036 both apply cleanly on a fresh PG and on upgrade from migration 034 with existing session data. Downgrade present and reverse-tested. All new columns are nullable; zero-downtime DDL.
- Codex review chain: 7 phases of cross-agent review across 30+ rounds. Each round's findings, fixes, and verification logged in the KB under `entity_ref=org-admin-console-v0.10.0-phase-{1..7}`.

## [0.9.9.12] - 2026-05-12

### Fixed
- **Daemon-reindex data loss when the SQLite index self-heals against any malformed manifest.** A user-reported symptom — "7-8 Codex sessions disappeared from `sfs list` after daemon restart following an Index-was-corrupted recovery" — traced to two distinct bugs in the rebuild loop:
  1. `src/sessionfs/store/index.py:upsert_session` used `manifest.get("source", {})` (and the same shape for `model` / `stats` / `tags`), which returns `None` (not the default) when the manifest has `"source": null`. The next `source.get(...)` raised `AttributeError`. `created_at` and `source.tool` (both NOT NULL columns) hit `sqlite3.IntegrityError` on the same kind of null-or-wrong-type input. Replaced with `_as_dict` / `_as_list` helpers that use `isinstance` checks — so any falsy OR truthy non-matching type (e.g. `"source": "codex"`, `"source": ["codex"]`, `"tags": "not-a-list"`) is normalized to an empty container instead of crashing.
  2. `_rebuild_index_from_disk` only caught `(json.JSONDecodeError, OSError)`, so an `AttributeError` from bug #1 — or any `sqlite3.IntegrityError` / `TypeError` — aborted the entire reindex loop. Every session sorted alphabetically AFTER the bad one was silently dropped. Broadened to `except Exception` with WARNING-level skip logging and a "N indexed, K skipped" summary line.
- **`sqlite3.IntegrityError` misinterpreted as index corruption.** `upsert_session_metadata`'s `except sqlite3.DatabaseError` branch caught IntegrityError (parent class) and ran the destructive recreate-and-retry recovery path per bad session — wasteful + noisy. Now catches `sqlite3.IntegrityError` first and re-raises so the per-session skip in the outer loop handles it cleanly. Same fix applied to `upsert_tracked_session`.
- **`sfs daemon rebuild-index` CLI command had its own copy of the same two bugs.** `cli/cmd_daemon.py:rebuild_index` duplicated the reindex loop with the same null-unsafe defaults and the same too-narrow except. Applied the matching `isinstance` guards and broadened the per-session except. Backfill block now repairs `"source": null` / `"source": "codex"` shapes in-place when a `tracked_sessions` row exists for the ID. Cross-reference comment added so future maintainers audit both loops together; the DRY refactor is queued for a v0.10.x cleanup.

### Notes
- No new database migrations (still 034). No new MCP tools.
- Helm chart version 0.9.14 → 0.9.15 (chart evolves independent of app). appVersion 0.9.9.12.
- 1384 backend tests + 117 dashboard tests passing (was 1376 + 117 at v0.9.9.11; +8 backend from new regression suite in `tests/unit/test_resilience.py`). Four Codex review rounds (KB entries 222 → 224 → 226 → 228 → 229 CLEAN) resolved before tag.

## [0.9.9.11] - 2026-05-12

### Fixed
- **Dashboard freeze when editing project rules → knowledge / context max tokens.** Both `<input type="number">` fields fired `patchRules()` on every keystroke, so typing `8000` issued four sequential `PUT /api/v1/projects/{id}/rules` calls — the first succeeded with a new ETag, the next three 409'd against the stale one, and the cascade of "refresh and try again" toasts plus react-query refetch storm rendered as a freeze. `RulesTab.tsx` now routes both inputs through a new `DebouncedTokenInput` helper: 600 ms local-draft debounce, flush-on-blur, flush-on-unmount (via empty-dep `useEffect` cleanup + `onCommitRef`), and a `flush(): Promise<boolean>` handle exposed via `forwardRef` + `useImperativeHandle` so the Compile button can drain pending edits before running. `patchRules` switched to `mutateAsync` and returns `Promise<boolean>` (never rejects — signals failure in-band so the existing 8 fire-and-forget checkbox callers don't generate unhandled-rejection warnings). `handleCompile` is now `async`, awaits both flushes, and short-circuits with a "Compile skipped — pending rules update failed" toast when any flush returned `false`. Compile button additionally disabled while `updateRules.isPending` as defense-in-depth against HTTP/2 multiplexing race. Six new regression tests in `RulesTab.test.tsx` cover: keystroke coalescing per pause, independent debounce per input, blur-flush, unmount-flush, compile-drains-pending (deferred-promise pattern proves compile is held until the patch promise settles), compile-skipped-on-409, compile-skipped-on-network/500.

### Notes
- No new database migrations (still 034). No server-side code changes.
- Helm chart version 0.9.13 → 0.9.14 (chart evolves independent of app). appVersion 0.9.9.11.
- 1376 backend tests + 117 dashboard tests passing (was 109 dashboard in v0.9.9.10; +8 from the RulesTab regression suite). Four Codex review rounds (entries 214 → 216 → 218 → 220 CLEAN) resolved before tag.

## [0.9.9.10] - 2026-05-12

### Added
- **`pg_trgm` GIN index on `knowledge_entries.content`** (migration 034). PostgreSQL only; SQLite is a no-op. Accelerates `ILIKE '%query%'` substring search used by `GET /api/v1/projects/{id}/entries?search=` and MCP `search_project_knowledge`. The route and the MCP wrapper now both enforce a 3-character minimum on `search` so every accepted query benefits from the trigram index — 1-2 char queries previously fell back to a sequential scan.
- **Shared ANSI-strip test helper** (`tests/utils/ansi.py`) with `strip_ansi()`, `assert_in_ansi()`, `assert_not_in_ansi()`. Replaces four inline `re.sub(r"\x1b\[...", ...)` sites in `test_autosync.py` and `test_hooks_installer.py` that previously protected those tests from the v0.9.9.8 CI-color flake class. New helper has 10 unit tests in `tests/unit/test_ansi_helper.py`; default case-insensitive matching is opt-out for tests where casing is part of the contract.
- **`confirm_or_exit()` CLI helper** (`src/sessionfs/cli/common.py`) — single source of truth for interactive confirmation prompts. Adds a non-TTY guard before `typer.confirm` so piped EOF returns a structured "stdin not a tty; pass --yes" exit instead of leaking an "Unexpected error" through `handle_errors`. Wired into the DLP scan-on-push prompt and the push-after-delete prompt in `cli/cmd_cloud.py`.

### Changed
- **`/api/v1/sync/status` collapsed from 5 queries to 2.** The watchlist counts (`watched`/`queued`/`failed`) now come from a single `GROUP BY status` SELECT against `tracked_sessions`; `watched` is the row sum across statuses, with `queued` and `failed` projected from the grouped result. The aggregate session counters remain on a single multi-aggregate SELECT. Behavior unchanged.
- **Admin `/api/v1/admin/orgs` no longer N+1s.** Member counts for the paginated org list are now loaded via a single `WHERE org_id IN (...) GROUP BY org_id` SELECT against `org_members`, then merged into the response. Empty-page guard prevents `IN ()`.
- **`sfs project ask` keyword extractor** rebuilt around `_extract_search_keywords()` in `cli/cmd_project.py`. The helper strips trailing punctuation, lowercases, drops stop words, enforces a 3-character minimum (mirrors the server gate), dedupes, and caps at 5 keywords. Previous behavior tokenized with `len(w) > 1` and would 422-abort on common 2-char tokens like `db`, `ai`, `ui` once the server-side floor landed. 8 regression tests in `tests/unit/test_ask_keywords.py` including a canary that fails if the CLI floor and server gate drift apart.
- **`/release` skill step 12 (post-deploy verification)** hardened against the v0.9.9.x detection-blind-spot class. Cache-busted probes (`?nocache=$(date +%s)`); strict version match via `grep -F "v${VERSION}"` against the changelog endpoint; `vercel inspect <live-alias>` checks the deployment that actually serves traffic; output captured to a variable before `grep` so `set -o pipefail` doesn't mask `inspect` failures.

### Fixed
- **`urllib3` bumped past CVE-2026-44431 / CVE-2026-44432** (fix in 2.7.0). Transitive dependency — no direct pin in `pyproject.toml`.
- **`click.exceptions.Abort` backstop in `handle_errors`** (`cli/common.py`). `Abort` does not inherit from `ClickException`; without an explicit catch it was bubbling up as "Unexpected error: aborted." for any cancelled `typer.confirm`.

### Notes
- Migration 034 added: `idx_ke_content_trgm` (`CREATE EXTENSION IF NOT EXISTS pg_trgm` + GIN with `gin_trgm_ops`). Idempotent on re-run; downgrade drops the index but leaves the extension installed (other tables / future migrations may depend on it).
- Helm chart version 0.9.12 → 0.9.13 (chart evolves independent of app). appVersion 0.9.9.10.
- 1376 backend tests + 109 dashboard tests passing (was 1367 in v0.9.9.9; +9 from B4/B6 + ask-keyword regression suites). Three Codex review rounds (entry 207 → 209 → 211 CLEAN) resolved before tag.

## [0.9.9.9] - 2026-05-11

### Fixed
- **CI / Deploy API gate.** Three tests asserted substrings (`--to`, `messages.jsonl`, `claude-code (user)`) directly against captured stdout/stderr. Rich's console renderer splits styled text across ANSI escape sequences when color is enabled (CI's wider terminal triggered it; local pytest had color disabled). v0.9.9.8's Deploy API workflow runs `pytest tests/ -v` as a deploy gate, so this blocked the release deploy. Fix: strip ANSI codes with a `\x1b\[[0-9;]*[a-zA-Z]` regex before substring assertions in `tests/unit/test_autosync.py` and `tests/unit/test_hooks_installer.py`. Verified by running the full backend suite under `FORCE_COLOR=1`.

### Changed
- **Dashboard audit polling consolidated.** `dashboard/src/hooks/useAudit.ts` previously exposed a `useRunAudit` hook that owned a parallel 5-second poll loop competing with `BackgroundTasksProvider` for the same audit lifecycle. `useRunAudit` had zero callers — pure dead code. Removed; `BackgroundTasksProvider` (driven by `AuditModal`) is now the single owner of run + poll.
- **Onboarding page stops polling on completion.** `dashboard/src/onboarding/GettingStartedPage.tsx` used `refetchInterval: 10_000` unconditionally on the `sessions` and `projects` queries, so the page kept hitting `/api/v1/sessions` and `/api/v1/projects` every 10 s indefinitely. Now each query stops polling once its own completion condition is met (`sessions.total > 0`, `projects.length > 0`) via the function-form `refetchInterval`.
- **Folder + inbox-handoff stale windows extended on the hottest screen.** `useFolders` staleTime 30 s → 300 s; `SessionList`'s inbox-handoff query 60 s → 300 s. Folder mutations (`create`/`update`/`delete`/`bookmark`) still invalidate the cache, so stale data isn't a correctness risk. The 5-min window cuts redundant refetches on every remount and tab focus — `SessionList` is the dashboard's hottest screen and these queries fired on every mount.

### Notes
- No new database migrations. No server-side code changes.
- Helm chart version 0.9.11 → 0.9.12 (chart evolves independent of app). appVersion 0.9.9.9.
- 1344 backend tests + 109 dashboard tests passing. Test fix verified under `FORCE_COLOR=1` to match CI behaviour.

## [0.9.9.8] - 2026-05-11

### Fixed
- **DLP per-member size cap is now tier-aware.** `redact_and_repack` accepts a `member_limit_bytes` parameter sourced from the same `_member_size_limit_for_tier` helper used by `_check_member_sizes`. Pre-fix, a hardcoded `50 * 1024 * 1024` in `server/dlp.py` silently nullified any `SFS_MAX_SYNC_MEMBER_BYTES_PAID` env override above 50 MB for orgs with DLP=REDACT mode enabled. New typed `DlpMemberTooLargeError` lets the route return the same structured 413 envelope as the non-DLP path. Post-redaction size validation added — replacement markers like `[REDACTED:openai_api_key]` (24 chars) can expand a payload past the cap.
- **`sfs handoff <handoff_id>` now redirects to `sfs pull-handoff` instead of "Missing option --to".** Recipients who pasted a handoff ID into the (sender) command hit a wall; redirect is gated on `not to` so session aliases shaped like `hnd_…` aren't hijacked.
- **`handle_errors` decorator no longer swallows Typer/Click validation errors.** `click.exceptions.ClickException` re-raises through the standard Typer error-box rendering instead of being caught as generic Exception and printed as "Unexpected error: ...".
- **`sfs sync` no longer silently skips sessions the daemon auto-excluded for transient reasons.** Hard deletes still respected. Per-session atomic clearing under `fcntl.flock` (new `acquire_for_retry()` helper) gates immediately before the network call so partial-failure paths preserve the exclusion. TOCTOU window between snapshot read and clear is closed: any concurrent writer that installs a hard delete during the sync run wins. All exclusion-list helpers (`is_excluded`, `get_entry`, `list_deleted`, `is_transient_exclusion`) now defensively filter non-dict entries from a hand-edited or corrupted `deleted.json`.
- **`sfs doctor` install-consistency check** detects "pip-installed sessionfs to user-site but PATH points at an older binary tied to a different Python interpreter" drift. Parses the on-PATH `sfs` binary's shebang and subprocesses that interpreter to read its `sessionfs.__version__`. Reports the divergence and points at the `python -m sessionfs.cli.main` fallback (made reachable via a new `__main__` guard in `cli/main.py`).
- **Helm chart version** bumped 0.9.10 → 0.9.11 (chart evolves independently of app version; appVersion 0.9.9.8).

### Notes
- No new database migrations. 031, 032, 033 from v0.9.9.7 remain authoritative.
- 1344 backend tests + 109 dashboard tests passing. Ten Codex review rounds resolved.

## [0.9.9.7] - 2026-05-10

### Added
- **Tier-aware per-member sync size cap** — `messages.jsonl` and other archive members can now reach **50 MB** for Pro/Team/Enterprise (was 10 MB across the board). Free/Starter stay at 10 MB. Override via `SFS_MAX_SYNC_MEMBER_BYTES_FREE` / `SFS_MAX_SYNC_MEMBER_BYTES_PAID`. Hard 100 MB abuse cap unchanged. The CLI pre-flight check uses the larger paid default; the server enforces the actual tier-resolved cap and returns a structured 413 with tier-aware suggestion text.
- **`dismiss_knowledge_entry` MCP tool (audited)** — write tool that records `dismissed_at`, `dismissed_by`, `dismissed_reason` on the entry. Idempotent (re-dismiss preserves the original timestamp + dismisser). Set `undismiss=true` to reverse a dismissal — clears the audit row. Length-capped at 500 chars; whitespace-only reasons normalize to NULL on the server. Audit triple is exposed on every entry-returning endpoint via `KnowledgeEntryResponse` so agents can confirm what was recorded.
- **Migration 031** — `dismissed_at`, `dismissed_by`, `dismissed_reason` columns on `knowledge_entries`.
- **Migration 032** — `recipient_email_normalized` column on `handoffs` plus a dedicated index for inbox lookups. Backfilled from existing rows via SQLAlchemy Core (cross-DB).
- **Migration 033** — composite indexes on `knowledge_entries` for the Tier A list paths: `(project_id, dismissed, claim_class, freshness_class)`, `(project_id, compiled_at, dismissed)`, and `(project_id, created_at)` for keyset cursor pagination.

### Changed
- **Concept compiler query collapse** — `auto_generate_concepts` now prefetches the active-claim set, all candidate concept pages, and their links once before the candidate loop, then resolves dismissed-entry membership in one bulk query. Previously did `candidates × active_claims` plus per-slug page + link lookups; now O(candidates) in-memory.
- **Handoff list batching** — `/api/v1/handoffs/inbox` and `/sent` batch-load referenced senders + sessions in two queries instead of N+1 per handoff. Inbox filters on `recipient_email_normalized` directly (raw-column index miss is fixed); a fallback OR clause keeps pre-migration rows reachable until backfill catches them.
- **Server-side dismiss reason normalization** — `DismissRequest.reason` strips whitespace and collapses empty/whitespace-only to `None` via Pydantic `field_validator`. Direct API callers can no longer persist `"   "` as the audit reason or clobber a real prior reason with whitespace.
- **`KnowledgeEntry` row lock on dismiss** — `PUT /entries/{id}` now `SELECT ... FOR UPDATE` the entry before branching on `entry.dismissed`, eliminating the audit-row race where two concurrent dismissals could both take the first-dismiss path. SQLite no-ops the lock; PostgreSQL serializes overlapping callers.

### Known Performance Items
The following items were identified during the v0.9.9.7 perf audit but
deferred — they are not blocking and can land in a later patch:
- Dashboard polling consolidation: `BackgroundTasks.tsx` and `useAudit.ts`
  run independent `setInterval` loops against overlapping endpoints. Move
  to a single query-driven path with backoff and visibility awareness.
- `GettingStartedPage.tsx` polls every 10 s indefinitely; should stop
  once `hasSession && hasProject`.
- `SessionList.tsx` always loads folders + inbox handoffs; lazy-load
  behind the folder/handoff UI or extend the stale window.
- `sync status` does five separate aggregates and `admin.py` org listing
  does per-org member-count queries; collapse into grouped aggregates.
- KB content search still uses `ILIKE %…%`; trigram / FTS index is the
  next scalability step once the composite indexes above bed in.

### Fixed
- **Sync error message uses server detail** — `SyncTooLargeError` prefers the structured detail body from the server (which carries tier-aware `suggestion` text) over the hardcoded message. Pre-v0.9.9.7 fallback text retained for older deployments.
- **Pip-audit MEDIUM bumps** — `python-multipart>=0.0.27`, mako and pip refreshed in the dev environment.
- **postcss XSS bump** — dashboard `postcss` 8.5.8 → 8.5.14 (dev-time tooling, not a production runtime exposure).

## [0.9.9.6] - 2026-05-10

### Added
- **MCP Tier A read surface (7 new tools)** — `get_knowledge_entry`, `list_knowledge_entries`, `get_wiki_page`, `get_knowledge_health`, `get_context_section`, `get_session_provenance`, `compile_knowledge_base`. Total MCP tool count goes 14 → 21. All tools enforce existing project membership / session ownership checks.
- **`list_knowledge_entries` rich filters** — `claim_class`, `freshness_class`, `dismissed`, `session_id` query params on `GET /api/v1/projects/{id}/entries`, plus three sort modes (`created_at_desc`, `last_relevant_at_desc`, `confidence_desc`) with stable `id` tiebreak so identical sort-key values can't reorder.
- **Keyset cursor pagination** — opt-in `?cursor=<id>` query param on `list_entries` (default sort only). Snapshot-stable across concurrent inserts/deletes — no skipped or duplicated rows. Server emits `X-Next-Cursor` response header on every default-sort page when more rows exist (works in both OFFSET and cursor modes), so callers can bootstrap keyset iteration from page 1 without inventing a sentinel.
- **Per-user tier-aware knowledge rate limits** — `KNOWLEDGE_RATE_LIMITS = {free:20, starter:50, pro:100, team:100, enterprise:200, admin:500}` requests/hour on `POST /entries/add`. Bucket key is `user_id` (not session_id) so MCP `manual` callers don't share buckets. `SFS_KNOWLEDGE_RATE_LIMIT_PER_HOUR` env override. Admin tier promoted before effective-tier resolution.
- **413 graceful failure** — `sfs push`/`sfs handoff`/`sfs sync` pre-scan archives for oversized members (10MB limit) and surface `SyncTooLargeError` with actionable guidance instead of an opaque server 413.
- **Compression-safe capture guard** — shared `should_recapture()` helper consulted by all 7 watchers before re-write. Checks the deleted-sessions exclusion list FIRST, then compares source JSONL message count against existing `.sfs` to prevent re-capturing a compressed session as empty.
- **`sfs recapture` command** — manual re-capture flow with `CursorComposerPurgedError` guard for purged-composer cases.
- **Cross-tool MCP-over-CLI nudge** — three-layer fix so agents prefer MCP tools over `sfs` shell commands: (1) MCP tool descriptions say "Always use this MCP tool instead of running `sfs ...`"; (2) `_SESSIONFS_MCP_GUIDANCE` block injected into every compiled rules file (CLAUDE.md / codex.md / .cursorrules / GEMINI.md / copilot-instructions.md) lists all 21 tools; (3) `sfs hooks install` writes a SessionStart hook for Claude Code that emits the cached compiled output before the first message.
- **Dashboard "Compile now" CTA** — workflow hint banner now carries an inline button so users don't navigate to the Entries tab to act. Server-side health recommendations fire at any pending count > 0 (was > 20).
- **Compile workflow guidance in compiled rules** — agents are instructed to check `get_knowledge_health` after contributing knowledge, prompt the user, and only call `compile_knowledge_base` on explicit consent.
- **Pre-upgrade Helm migration Job** — runs Alembic upgrade head before the API rolls out, so a self-hosted upgrade that drops a new migration cannot serve traffic against an unmigrated DB.

### Changed
- **Project-row lock on compile + concept generation** — `compile_project_context` and `auto_generate_concepts` both `SELECT … FOR UPDATE` the project row before reading pending claims / creating concept pages. Two compile callers serialize end-to-end on PostgreSQL; SQLite no-ops the lock harmlessly. Eliminates the duplicate-context-document and duplicate-concept-page races.
- **Compile MCP response trimmed** — `compile_knowledge_base` strips `context_before` / `context_after` (often thousands of words) from the MCP JSON payload to keep agent context small. Dashboard direct-API calls still receive the full payload.
- **Dashboard manualChunks** — explicit `vite.config.ts` chunk naming (`react-vendor`, `markdown`, `zod`, `react-router`, `react-query`). Main bundle drops from 212 KB to 30 KB (8× smaller); vendors cache separately across deploys.
- **`/api/v1/health` alias** — added alongside `/health` for self-hosted ingress paths that scope readiness to `/api/v1/*`.
- **DELETE `/sessions/{id}` backward-compat** — accepts `scope=` query param for older CLI clients.
- **Settings.json fcntl locking** — `sfs hooks install/uninstall` and rules emitter use `fcntl.flock` on a `.sfs-lock` file so concurrent invocations can't corrupt the JSON. Hook entry detection now requires command prefix `sfs rules emit ` (not just sentinel match) before treating an entry as managed.

### Fixed
- **Stable pagination ordering** — `KnowledgeEntry.id.desc()` is the absolute final tiebreak in every sort mode on `list_entries`. Without it, identical sort-key values reordered arbitrarily across pages.
- **Daemon transient errors no longer exclude sessions** — only `SyncTooLargeError` (413) counts toward the exclusion threshold; transient `SyncError` failures are retried instead of permanently excluded.
- **Admin tier reaches its 500/hr knowledge bucket** — admin is checked from raw `user.tier` before effective-tier resolution (which collapses admin → enterprise). Previously admins were capped at 200/hr.
- **`sfs handoff` 413 graceful path** — handoff now pre-checks oversized members and catches `SyncTooLargeError`, exiting with actionable guidance instead of an opaque server error.
- **Compile description honesty** — `compile_knowledge_base` MCP description now flags "HEAVY + MUTATING", explains the project lock, and points to the real `GET /api/v1/projects/{id}/compilations` collection (not a non-existent per-id route). Removes the misleading "cron-driven pass" claim — there is no scheduler.
- **Rules emit unmanaged-CLAUDE.md guard** — `sfs rules emit` no longer falls back to reading an unmanaged CLAUDE.md as if it were managed content.
- **KB health pending count** — only counts claims (excludes notes). Notes don't compile, so they shouldn't drive the pending banner.

## [0.9.9.5] - 2026-04-17

### Added
- **First-run onboarding** — signup auto-authenticates and navigates to `/getting-started`. Three-step onboarding page (install tool, capture session, create project) with live completion indicators. State-based redirect gate: 0 sessions + 0 projects → onboarding. API key shown in dismissible banner after signup. User-scoped dismissal via djb2 hash (not global to browser). Legacy key migration for upgrade path.
- **Sort direction toggle** — ascending/descending on all sort modes in the dashboard. CLI adds `--sort messages-asc` and `--sort tokens-asc` for finding small sessions.
- **Tool sort mode** — real "Sort: Tool" in the sessions list groups by tool label.
- **Tool filter alias normalization** — `gemini` / `gemini-cli` and `copilot` / `copilot-cli` treated as the same family across list, search, and admin endpoints.

### Changed
- **Cloud Run min-instances** — API deployment now sets `--min-instances 1` to eliminate cold-start latency.
- **OnboardingGate loading** — shows lightweight placeholder instead of blank screen or mounting SessionList prematurely (avoids extra folder/handoff queries for first-time users).
- **Publish Container Images workflow** — now supports `workflow_dispatch` for manual retrigger with configurable ref.

### Fixed
- **Unified 410 delete propagation** — structured `SyncDeletedError` replaces string-based detection. Shared `cleanup_deleted_session()` helper wired into all three sync paths (bulk `sfs sync`, explicit `sfs push`, daemon autosync). Dashboard-deleted sessions are auto-cleaned locally on next sync instead of showing red 410 errors.
- **Full local cleanup on server 410** — removes `.sfs` directory + SQLite index entry (both `sessions` and `tracked_sessions` tables) + adds to exclusion list. No more orphaned local copies.
- **Migration 030 cross-DB backfill** — replaced raw PostgreSQL-only `INTERVAL` syntax + `is_deleted = 1` with SQLAlchemy Core queries (works on both PostgreSQL and SQLite). Handles NULL `deleted_at` with sensible defaults.
- **`sfs delete` / `sfs restore` prefix resolution** — now resolves session ID prefixes via local store for all scopes (was only resolving for local/everywhere, not cloud).
- **`sfs rules init` TTY guard** — fails fast with clear message when stdin is not a TTY and `--yes` is not passed.
- **Sessions empty state** — now links to `/getting-started` and `/help` instead of bare `sfs push` command.

## [0.9.9.4] - 2026-04-16

### Added
- **Three-scope session delete** — `sfs delete <id>` with `--cloud` (server only, keep local), `--local` (device only, keep cloud), or `--everywhere` (both). No default — explicit choice required. Confirmation prompt with `--force` bypass for automation.
- **Sync-aware deletes** — autosync respects intentional deletes via `~/.sessionfs/deleted.json` exclusion list. Push and pull skip excluded sessions. `sync_push` un-delete path is gated behind `X-SessionFS-Undelete: true` header with ETag conflict check — autosync can never reverse a delete.
- **`sfs trash`** — lists soft-deleted sessions in the 30-day retention window with scope badges and purge dates.
- **`sfs restore <id>`** — reverses a soft-delete on the server, clears local tombstone, prints `sfs pull` guidance when local copy was removed.
- **Dashboard delete dialog** — replaces the single `confirm()` with a two-choice dialog: "Remove from cloud" or "Delete everywhere". One-line explanation per option.
- **Dashboard Trash view** — filter toggle on the session list showing soft-deleted sessions with scope badges, purge dates, restore buttons, and scope-aware restore guidance toast.
- **Admin purge endpoint** — `POST /api/v1/admin/purge-deleted` hard-deletes expired soft-deleted sessions and their blobs. Single-session or bulk. Returns purge count and bytes reclaimed.
- **Restore response guidance** — `POST /sessions/{id}/restore` returns `restored_from_scope` and `local_copy_may_be_missing` so clients can show accurate recovery guidance.
- **Migration 030** — `deleted_by`, `delete_scope`, `purge_after` columns on sessions table.

### Changed
- **DELETE endpoint** — now requires `?scope=cloud|everywhere` query parameter (was parameterless). Returns 200 with session record including `purge_after` (was 204). Old clients without `?scope=` get 400.
- **Storage quota** — soft-deleted sessions excluded from used-bytes calculation.
- **Share links** — return 410 Gone for deleted sessions (was 404).

### Fixed
- **Autosync un-delete bug** — the original soft-delete was immediately reversed by autosync pushing the local copy. Now gated by explicit intent header + ETag check.
- **Purge audit atomicity** — purge and audit log now committed in a single transaction (was two separate commits).

### Security
- **CVE-2026-40347** — python-multipart bumped from `>=0.0.9` to `>=0.0.26` (DoS via crafted multipart preamble/epilogue).
- **Purge endpoint hardening** — session_id format validation + atomic audit logging.

## [0.9.9.3] - 2026-04-16

### Added
- **End-to-end resume smoke tests** — 3 tests exercising the actual `resume()` command wiring with real file writes, mocked API boundary, and stubbed tool launch. Proves: missing target file gets created before launch, unmanaged file is skipped without `--force-rules`, preflight failure does not abort resume.
- **Recursive nested skills/agents provenance** — `instruction_provenance` capture now discovers files recursively under `.agents/`, `.claude/commands/`, `.claude/skills/`, etc. (was flat-only). Uses `os.walk` with deterministic sorted traversal, symlink skipping, and a 30-file-per-root bound.

### Fixed
- **Provenance docs misstatements** — docs said the source-provenance line is "omitted" when absent (it actually prints a fallback message), and said "meaningfully initialized" checks `rules_versions` rows (it only checks `enabled_tools` / `static_rules` / `tool_overrides`). Both corrected in `docs/rules.md` and `site/.../rules.mdx`.
- **Provenance capture summary table** — added to docs: 4-row table (managed / unmanaged / global / nested) showing what's captured and whether it's reconstructable.

### Documentation
- **Provenance reconstruction semantics** — docs now explicitly state that unmanaged/global artifacts are hash-only (not reconstructable), and that resume uses current canonical rules by default (not historical session rules). Historical replay deferred to v0.9.10.

## [0.9.9.2] - 2026-04-16

### Fixed
- **Session push 500 with instruction provenance** — `rules_hash` column was `String(64)` but captured hashes are `sha256:` + 64 hex = 71 chars. PostgreSQL enforced the length and raised on INSERT. Fix: strip the `sha256:` prefix before storing (hex digest alone is sufficient for identity matching) + migration 029 widens the column to `String(80)` for defense-in-depth.
- **`sfs push --yes`** — new flag to skip the interactive DLP confirmation prompt. Findings are still displayed but don't block the push. Required for non-interactive environments (CI, scripts, daemon autosync).

## [0.9.9.1] - 2026-04-15

### Fixed
- **Migration 028 boolean defaults** — `server_default=sa.text("1")` on `project_rules.include_knowledge` and `include_context` worked on SQLite but PostgreSQL rejects `1` as a boolean literal. Changed to `sa.text("true")` to match the project's Postgres-compatible migration pattern. v0.9.9 prod API deploy failed the Alembic migration step; v0.9.9.1 clears it. No data impact — the broken migration never committed a transaction.

## [0.9.9] - 2026-04-14

### Added

#### Rules Portability — canonical project rules compiled to five tool formats
- **Canonical rules per project** — new `project_rules` + `rules_versions` tables (migration 028) with 4 new columns on `sessions` for instruction provenance. Managed via `GET/PUT /api/v1/projects/{id}/rules`, compiled via `POST /api/v1/projects/{id}/rules/compile`.
- **Five tool compilers** — deterministic partial-compile-aware compilers for Claude Code (`CLAUDE.md`), Codex (`codex.md`), Cursor (`.cursorrules`), Copilot (`.github/copilot-instructions.md`), and Gemini (`GEMINI.md`). Each embeds a SessionFS managed marker at the top of the file.
- **Knowledge + context injection** — compilers pull active claims (default `convention` + `decision` types) and project context sections (default `overview` + `architecture`) with per-tool token ceilings and progressive condensation.
- **`sfs rules` CLI** — `init` (auto-detects existing rule files + recent session-history tool usage, preselects from the 5 supported tools), `edit`, `show` (in-sync state), `compile` (with `--tool X`, `--dry-run`, `--force`), `push`, `pull`. `--local-only` at init adds compiled files to `.gitignore`; the default is shared-in-repo.
- **Managed-file safety** — `sfs rules compile` refuses to overwrite a user-maintained rule file unless `--force` is set. Detection reads only the first 512 bytes so markers buried in hand-written content don't trigger false positives.
- **Session instruction provenance** — session manifests now carry `rules_version`, `rules_hash`, `rules_source` (`sessionfs` / `manual` / `mixed` / `none`), and `instruction_artifacts[]`. Managed files record version + hash only (content lives in `rules_versions`); unmanaged / global artifacts store path + hash + source + scope. `SFS_CAPTURE_GLOBAL_RULES=off` suppresses global hashing for privacy-sensitive environments.
- **MCP tools** — read-only `get_rules` and `get_compiled_rules` for agents. No agent self-modification of rules.
- **Dashboard Rules tab** — new `RulesTab` under ProjectDetail with version badge, static preferences editor, enabled tools checklist, knowledge/context injection settings, per-tool compiled output viewer, version history list, and compile action. ETag-based optimistic concurrency with 409 toast on stale saves.

#### Resume-Time Rules Sync — carry the behavior contract across tools
- **Preflight during `sfs resume`** — before launching the target tool, partial-compile only that tool's rule file from current canonical rules and write it with managed-file safety (Case A write / Case B refresh / Case C warn-skip / Case D `--force-rules` overwrite). Supported resume targets: claude-code, codex, copilot, gemini.
- **Source session provenance display** — shows what rules shaped the original session before launching the resumed session (e.g. `Source session used rules v3 (sessionfs). Current project rules are v5. Synced codex.md from SessionFS rules v5.`). Hash-based provenance survives environments where content snapshots were intentionally suppressed.
- **New flags on `sfs resume`** — `--no-rules-sync` skips the preflight entirely; `--force-rules` overwrites an unmanaged target rule file with SessionFS-managed content. `--force-rules` is a one-time permission — the file becomes SessionFS-managed afterward and subsequent resumes refresh it normally.
- **Meaningfully initialized** — preflight compiles and writes only when the project has curated content (`enabled_tools` non-empty, `static_rules` non-empty, or `tool_overrides` non-empty). Untouched default rules rows skip cleanly.
- **Non-fatal semantics** — rules sync failure never fails the resume itself. Warning to stderr, resume continues, exit 0.
- **Partial-compile guarantee** — any compile request with an explicit `tools` override is treated as partial and never bumps canonical history, regardless of whether the override matches `enabled_tools`.

### Fixed
- **Helm chart invalid semver** (`chart.metadata.version "0.9.8.6" is invalid`) — fixed by the v0.9.9 bump. Four-segment versions aren't valid semver; the chart now tracks the same 3-segment version as the Python package.
- **Compile hash included managed marker** — regression fix: `content_hash` is now the body hash only (marker-independent), so no-op detection doesn't break after a version bump. No more infinite-version-bump loops.
- **Atomic optimistic concurrency on PUT /rules** — `SELECT ... FOR UPDATE` row lock ensures two writers holding the same prior ETag can't both commit. Second one receives 409.
- **Concurrent compile race** — version-number contention now retries idempotently; colliding with a same-body-hash winner short-circuits to no-op and aligns local `rules.version` to the winner.
- **First-time rules creation race** — `get_or_create_rules` catches `IntegrityError` and returns the winner's row rather than raising 500.
- **Knowledge injection determinism** — claim ordering priority (decision > convention > pattern > dependency) moved into SQL `ORDER BY` so LIMIT respects it; deterministic `id DESC` tie-breaker on identical timestamps.
- **Path traversal defense** — added `_safe_target_path()` in `cli/cmd_rules.py` used by both `sfs rules compile` and resume preflight; validates target file against canonical `TOOL_FILES[tool]` and confirms the resolved path stays inside `git_root`.
- **Provenance logging visibility** — `watchers/provenance.py` failures now log at `warning` level (was `debug`) so breakage is visible in normal daemon operation. Still non-fatal to session capture.
- **Malformed compile payload hardening** — `preflight_target_tool_rules()` validates `outputs` is a list and `filename`/`content` are strings; returns structured `api-error` result instead of raising.

### Security
- **CVE-2025-71176** — pytest bumped from `>=8.0,<9.0` to `>=9.0.3,<10.0` (dev dependency only; /tmp DoS under certain test configurations).

## [0.9.8.6] - 2026-04-13

### Added
- **Knowledge Base v2 — claim model** — three-layer lifecycle: evidence (raw facts from sync) → claim (promoted active truth) → note (rejected or dismissed). New columns on `knowledge_entries`: `claim_class`, `entity_ref`, `entity_type`, `freshness_class`, `supersession_reason`, `promoted_at`, `promoted_by`, `retrieved_count`, `used_in_answer_count`, `compiled_count` (migration 027).
- **Per-type freshness decay** — new `src/sessionfs/server/services/freshness.py` applies per-type windows: bug 30d, dependency 60d, pattern/discovery 90d, convention 180d, decision 365d. Entries decay from `current` → `stale` → `archived` based on `last_relevant_at`.
- **Auto-promotion at compile** — evidence with `confidence >= 0.5` and `content >= 30 chars` is automatically promoted to `claim` at the start of each compile pass, so compile pulls from a stable active-claim pool.
- **Supersession** — `PUT /api/v1/projects/{id}/entries/{entry_id}/supersede` retires an old claim and links it to a superseding entry with a reason. Superseded entries remain readable for audit but are excluded from compile.
- **Refresh endpoint** — `PUT /api/v1/projects/{id}/entries/{entry_id}/refresh` updates `last_relevant_at` and resets `freshness_class` to `current`; replaces the old "Still valid" no-op (which called undismiss on non-dismissed entries).
- **Rebuild endpoint** — `POST /api/v1/projects/{id}/rebuild` resets `compiled_at=NULL` on all active claims and clears `context_document`, forcing a full compile on settled projects where `compile` would otherwise be a no-op.
- **`sfs project rebuild` CLI** — new command mirrors the rebuild endpoint.
- **Writeback gates for agent contributions** — `add_knowledge` (MCP + API) defaults to `claim_class="note"`. Auto-promotes to `claim` only if specificity gate, semantic dedup (Jaccard-min ≥ 0.85), and rate limit pass. Returns classification feedback so agents know when their entry was rejected vs accepted vs promoted.
- **Section pages as true projections** — compile iterates ALL known types from `slug_map`, not just types in the pending batch. Section pages with zero active claims are deleted inline (previously went stale indefinitely).
- **`used_in_answer_count` tracking** — MCP `ask_project` and API search expose a `_used_in_answer` flag that increments this counter on retrieval, feeding the freshness signal.
- **Dashboard `KnowledgeEntriesTab` v2** — health banner with stale review queue, claim/freshness badges, filter controls, provenance blocks (entity ref + promoted_by + retrieved/used counts), promote/supersede/refresh/dismiss actions, rebuild button.

### Changed
- **Compile default budget lowered to 2000 words** (from 8000) — active-truth-per-token principle produces sharper context documents. Migration 027 backfills existing projects with `kb_max_context_words = 8000 OR NULL` to the new default.
- **Search filters to active claims by default** — `GET /api/v1/projects/{id}/entries/search` excludes evidence, notes, and superseded entries unless `include_stale=true`.
- **MCP `search_project_knowledge`** — same default; returns active-claim results with provenance.
- **MCP `get_project_context`** — filters to active claims when constructing the compiled document.

### Fixed
- **Concept page prune filter** — compile filtered dead pages by `page_type == "auto"`, but concept pages are stored as `page_type == "concept"`. Dead pages were never pruned.
- **`dismiss-stale` safety guard** — added `confidence < 0.5` constraint to prevent bulk-dismissing medium-confidence entries that haven't been referenced recently.
- **Daemon startup order** — `_fetch_remote_settings()` now runs before `full_scan()`. Previously a slow settings fetch blocked watcher startup by ~95 seconds on fresh installs.
- **Codex watcher NoneType crash** — `payload.get("content") or []` pattern in 4 places (content, summary, action blocks); prior code crashed when Codex emitted null fields, halting the entire watcher loop.
- **Daemon PID resolution fallback** — `sfs daemon stop` now falls back to `daemon.json` when `sfsd.pid` is missing, fixing orphaned daemon processes after crashes.

### Documentation
- **`sfs dlp scan/policy` in site CLI docs** — added to `site/src/content/docs/cli.mdx`.

## [0.9.8.5] - 2026-04-12

### Added
- **Dashboard Help page** (`/help`) — MCP-first guidance with 8-tool installer (`sfs mcp install --for <tool>`), agent prompt examples, curated CLI reference, 12-tool MCP reference, external resource links.
- **Admin org back-office endpoints** — `GET/POST /api/v1/admin/orgs` + `PUT /api/v1/admin/orgs/{id}/tier` for internal provisioning, bypassing the Team+ subscription gate.
- **Bulk dismiss-stale endpoint** — `POST /api/v1/projects/{id}/entries/dismiss-stale` atomically dismisses old low-confidence entries (< 0.5 confidence + > 90 days unreferenced). Dashboard surfaces a "Dismiss N stale" button in the knowledge health banner.
- **Dashboard health banner** — `ProjectDetail` Entries tab surfaces stale/low-confidence/decayed counts + actionable recommendations from the health API.
- **Self-hosted Security Posture documentation** — new section in `docs/self-hosted.md` and `site/src/content/docs/self-hosted.mdx` covering non-root UIDs, read-only rootfs, capability drops, seccomp, and `trivy config` verification.
- **`sfs dlp` CLI documentation** — `docs/cli-reference.md` now documents `sfs dlp scan` and `sfs dlp policy` subcommands.

### Fixed
- **Dashboard signup broken on app.sessionfs.dev** — `VITE_API_URL` unset at Vercel build time; `baseUrl` defaulted to `window.location.origin` (static host), returning 405 on POST. Fixed three ways: env var set in Vercel, URL baked into bundle, `app.*` → `api.*` fallback in code.
- **Unguarded localStorage across dashboard** — `main.tsx`, `ThemeToggle`, `SettingsPage` assumed a full Storage interface. Now all call sites use `src/utils/storage.ts` helper with full guards.

### Security
- **GitLab webhook user binding** (HIGH) — webhook accepted any per-user secret match but discarded which user it belonged to, then loaded credentials from `sessions[0].user_id`. A forged MR payload could impersonate another user's GitLab token. Fixed: bind `auth_user_id` to the matching row, scope session lookup + credential load to that user.
- **GitHub installation claim IDOR** (HIGH) — `PUT /settings/github` blindly claimed the first unclaimed installation. Fixed: require `installation_id` from the client, time-windowed claim (15 min), 403/410 on violations, atomic conditional `UPDATE ... WHERE user_id IS NULL` to prevent race conditions.
- **Effective-tier leak** (MEDIUM) — `sync_push` upload limit and `/api/v1/sync/status` read `user.tier` directly instead of resolving the effective org tier. Enterprise org members with personal `free` tier got 50 MB limits. Fixed: both paths now use `get_effective_tier(user, db)`.
- **`pr_comments` unique index scoped** (MEDIUM) — migration 026 widens the unique index from `(repo_full_name, pr_number)` to `(installation_id, repo_full_name, pr_number)` to match runtime query scoping.
- **Security Scan workflow rebuilt** — replaced `aquasecurity/trivy-action` with raw `trivy` binary (`setup-trivy@v0.2.6`, `trivy v0.69.3`); renders Helm chart before misconfig scan; `--severity CRITICAL,HIGH` exit-code enforcement.
- **Dockerfile hardening** — `Dockerfile` and `Dockerfile.mcp` now create a non-root `sessionfs` user (UID 10001) and set `USER 10001` before CMD.
- **Helm chart hardening** — PostgreSQL StatefulSet + `helm test` hook pod now have full `securityContext` with `readOnlyRootFilesystem`, `allowPrivilegeEscalation: false`, `capabilities.drop: [ALL]`, `seccompProfile: RuntimeDefault`. Fixed pre-existing bug where fallback emptyDir volume was at StatefulSet spec level instead of pod spec level.
- **4 CVE patches** — vite 7.0.0→7.3.2 (3 CVEs), defu ≤6.1.4→6.1.5 (prototype pollution) in `site/package-lock.json`.

### Knowledge Base Lifecycle
- **LLM compile budget enforcement** — `_trim_to_budget()` now applied after LLM response, not just on the simple-compile path.
- **Semantic dedup on all extraction paths** — shared `word_overlap()` + `is_near_duplicate()` helpers; both deterministic and LLM extraction now run a 3-layer filter (exact-match, project-wide overlap, intra-batch).
- **Concept page pruning** — new `_prune_dead_concept_pages()` runs unconditionally before candidate check; fixed `page_type` filter (`"auto"` → `"concept"`).
- **Health stale-entry count** — includes entries with old `last_relevant_at`, not just `IS NULL`.
- **Bulk dismiss confidence guard** — only dismisses entries with `confidence < 0.5`.

### Daemon / Sync
- **Codex watcher crash** — `payload.get("content", [])` returns `None` when key exists with explicit null. Fixed in 4 places with `payload.get("content") or []` pattern.
- **Daemon startup delay** — `_fetch_remote_settings()` moved before watcher `full_scan()` so autosync mode is correct immediately (308 ms vs 96 s).
- **`sfs daemon stop` fallback** — new `_resolve_daemon_pid()` prefers `sfsd.pid`, falls back to `daemon.json`. Both stop and status use it. Also clears `daemon.json` on stale-PID cleanup.
- **DB pool config** — `SFS_DATABASE_POOL_SIZE`, `MAX_OVERFLOW`, `POOL_TIMEOUT`, `POOL_RECYCLE` env vars wired through `ServerConfig` → `init_engine()`.

### Tests
- Backend: 1052 → **1091** (+39 tests in 4 new files: billing webhooks, sync promotion failures, GitHub claim, knowledge lifecycle)
- Dashboard: 22 → **76** (+54 tests in 7 new files: BillingPage, GitHubIntegrationSection, HandoffDetail, ProjectDetail, SessionDetail, AdminDashboard, OrgPage)
- Codex null-content regression tests (2 new)

## [0.9.8.4] - 2026-04-10

### Added
- **Dashboard Help page** — new `/help` route with MCP-first guidance, an 8-tool installer (Claude Code, Codex, Gemini, Cursor, Copilot, Amp, Cline, Roo Code) with live terminal preview and copy-to-clipboard, example agent prompts by use-case, a curated 10-command CLI quick-reference, the full 12-tool MCP reference, and external resource links. Help icon sits between ThemeToggle and the avatar; a Help entry also appears in the mobile drawer.
- **Helm chart Security Posture documentation** — new "Security Posture" section in `docs/self-hosted` covering non-root UIDs, read-only root filesystems, dropped capabilities, RuntimeDefault seccomp, and how to verify with `trivy config` on a rendered chart.

### Fixed
- **Dashboard signup broken on app.sessionfs.dev** — `VITE_API_URL` was not set at Vercel build time, so the LoginPage `baseUrl` defaulted to `window.location.origin`, making signups POST to the static Vercel host and return 405. Fixed three ways: (1) set `VITE_API_URL=https://api.sessionfs.dev` in Vercel for all three environments, (2) bake the URL into the new production bundle, (3) derive `api.<domain>` from `app.<domain>` in the fallback chain so this cannot silently break again.
- **Unguarded localStorage across dashboard** — `main.tsx`, `ThemeToggle`, and `SettingsPage` assumed a full Storage interface. Now all call sites route through a new `src/utils/storage.ts` helper that guards against missing localStorage, plain-object localStorage (vitest 4 + jsdom), SecurityError on access, and QuotaExceededError on write. Shared vitest setup installs an in-memory stub so every test file gets a working localStorage.
- **Help page theme query stale** — resource links now subscribe to `document.documentElement[data-theme]` via `MutationObserver` so sessionfs.dev links update live when the user toggles theme on the Help page.

### Security (post-release hardening)
- **Helm chart — postgres StatefulSet** — container now has a full `securityContext` with `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `runAsNonRoot: true`, `capabilities.drop: [ALL]`, and `seccompProfile.type: RuntimeDefault`. Added `emptyDir` mounts at `/tmp` and `/var/run/postgresql` so postgres can still write its socket directory with a read-only root filesystem. Also fixed a pre-existing bug where the fallback `emptyDir` volume (when `persistence.enabled=false`) was declared at the StatefulSet spec level instead of the pod spec level, making it ineffective.
- **Helm chart — `helm test` hook** — the curl-based connection test pod now has pod and container `securityContext` with `runAsUser: 10001`, `readOnlyRootFilesystem: true`, dropped capabilities, and RuntimeDefault seccomp.
- **Dockerfile + Dockerfile.mcp** — both now create a non-root `sessionfs` user (UID 10001) and set `USER 10001` before `CMD`. Cloud Run and Kubernetes already enforced non-root at the orchestration layer, but the image itself is now compliant for self-hosted users who don't override.
- **Security Scan workflow rewrite** — replaced `aquasecurity/trivy-action` with raw `trivy` binary (via `setup-trivy@v0.2.6`, `trivy v0.69.3`) because the action's `severity:` input does not filter exit-code for `scan-type: config`. Now renders the Helm chart with `helm template` before running misconfig scan so Trivy evaluates real Kubernetes manifests instead of raw `{{- with .Values }}` templates (which produced 40+ false-positive findings on an already-hardened chart). Also enforces `--severity CRITICAL,HIGH` and excludes `node_modules` / `.venv` from the secret scan.
- **vite 7.0.0–7.3.1 → 7.3.2** — fixes three CVEs in `site/package-lock.json`: CVE-2026-39364 (server.fs.deny bypass), CVE-2026-39363 (WebSocket arbitrary file read), CVE-2026-39365 (path traversal in optimized deps). Build-time only; site is statically generated.
- **defu <=6.1.4 → 6.1.5** — fixes CVE-2026-35209 (prototype pollution via `__proto__` key in defaults argument). Transitive dep in the site build.

## [0.9.8.3] - 2026-04-09

### Fixed
- **Connection pool exhaustion** — client-side `asyncio.Semaphore(5)` limits concurrent uploads; server-side per-user semaphore (max 3) with 429+Retry-After; sync client retries 429 with backoff.
- **sync_push connection lifecycle** — split into 3 phases: DB reads → release connection → blob upload (no DB) → fresh session for writes. Connection held ~70ms instead of ~5s.
- **Sync race condition** — blob uploaded to temp key, row committed with temp key first (PK constraint for creates, FOR UPDATE for updates), blob promoted only after commit. Temp blob preserved on any post-commit failure.
- **Pool health endpoint** — `GET /health/pool` returns pool utilization metrics.
- **Sync summary** — shows error count alongside pushed/pulled/conflicts.

## [0.9.8.2] - 2026-04-09

### Fixed
- **Database pool exhaustion** — API pods were using SQLAlchemy defaults (pool_size=5, max_overflow=10) causing 500 errors under load. Now configurable via `SFS_DATABASE_POOL_SIZE` (default 20), `SFS_DATABASE_MAX_OVERFLOW` (default 40), `SFS_DATABASE_POOL_TIMEOUT` (default 60s), `SFS_DATABASE_POOL_RECYCLE` (default 1800s). Connections verified with `pool_pre_ping=True`.

## [0.9.8.1] - 2026-04-09

### Added
- **Knowledge base lifecycle** — entry decay (0.8x confidence after 90 days unreferenced), auto-dismiss past retention period, configurable per-project settings (`kb_retention_days`, `kb_max_context_words`, `kb_section_page_limit`).
- **Quality gates on knowledge entries** — minimum 20 char content, 20/hr rate limit per session, 85% similarity rejection, lower confidence cap for manual/CLI entries.
- **Context document budget** — 8,000 word default cap with priority-aware trimming (high-confidence entries kept first).
- **Section page caps** — 30 items default, "N older entries not shown" footer.
- **Concept page auto-refresh** — regenerate when cluster grows 50%+, auto-delete when all entries dismissed.
- **Actionable health endpoint** — returns recommendations, stale/low-confidence/decayed entry counts.
- **`sfs resume --model`** — specify model for target tool (Claude Code, Codex, Gemini supported; Copilot warns).
- **Concept auto-generation fix** — stop word filtering, meaningful phrase extraction, trigrams for better topic names.
- Database migration 025: lifecycle fields on knowledge_entries + project settings.

### Fixed
- Billing webhook: `_sync_billing_to_org` requires positive match on both customer_id AND subscription_id. Personal subscription events cannot mutate org state.
- Billing webhook: `_find_user_or_org_by_customer` uses subscription_id to disambiguate legacy same-customer data.
- Billing webhook: non-active downgrade clears `stripe_subscription_id`.
- DLP pre-scan only runs when org policy is enabled (not unconditionally).
- `pack_session()` snapshots files into memory before tarring (fixes active session push).
- Session list sorts by `updated_at` (not `created_at`).
- Concept auto-generation filters stop words — no more "From The" or "Instead Of" concepts.
- Dashboard billing page uses typed `BillingStatus` interface.
- `SessionDetail` type includes `dlp_scan_results`.

## [0.9.8] - 2026-04-09

### Added
- **DLP / Secret Scrubbing** — pre-sync content protection with 14 PHI patterns (SSN, MRN, DOB, NPI, patient name, etc.) and 22 secret patterns. Three enforcement modes: BLOCK, REDACT, WARN. Server-side scan of all archive files, org policy via settings JSON, custom patterns and allowlist support.
- **DLP CLI** — `sfs dlp scan <session_id>` for local scanning, `sfs dlp policy` to view org policy. `sfs push` shows DLP preview when org policy is enabled.
- **DLP Dashboard** — Settings > DLP tab for org admins (enable, mode, categories). Session detail shows DLP findings section with pattern types and action taken.
- **DLP API** — `GET/PUT /dlp/policy`, `POST /dlp/scan` (dry-run), `GET /dlp/stats`. Feature-gated to Pro+ tier.
- Database migration 024: `dlp_scan_results` column on sessions.
- 43 new DLP tests (PHI patterns, false negatives, category filtering, allowlist, custom patterns, redaction, policy validation).

### Changed
- Session list sorts by `updated_at` instead of `created_at` — most recently active sessions appear first.
- `pack_session()` snapshots file contents into memory before tarring — handles active sessions with concurrent daemon writes.

### Fixed
- **Billing webhook isolation** — `_find_user_or_org_by_customer()` uses `subscription_id` to disambiguate legacy same-customer data. `_sync_billing_to_org()` requires positive match on both `customer_id` AND `subscription_id` — personal subscription webhooks can never mutate org state.
- **Billing non-active downgrade** — `past_due`/`unpaid`/`paused`/`incomplete_expired` now clears `stripe_subscription_id` on both org and user rows.
- **DLP pre-scan** — only runs when org DLP policy is enabled, not unconditionally on every push.
- **Dashboard type safety** — `SessionDetail` type includes `dlp_scan_results`, `BillingPage` uses typed `BillingStatus` interface.

### Security
- DLP scanning runs server-side at the sync chokepoint — even modified clients cannot bypass it.
- Redacted content (`[REDACTED:TYPE]`) replaces matches in ALL `.json`/`.jsonl` archive files. Original text never stored.
- Org DLP policy changes are admin-only and merge (don't overwrite custom_patterns/allowlist).

## [0.9.7.4] - 2026-04-07

### Fixed
- **MCP workspace detection** — all 6 project-scoped tools now use MCP `roots/list` protocol to detect workspace directory from the AI tool client, with CWD fallback.
- **MCP explicit git_remote** — `search_project_knowledge`, `ask_project`, `add_knowledge`, `update_wiki_page`, and `list_wiki_pages` now accept an optional `git_remote` parameter, matching `get_project_context`. Fixes "No git repository detected" when roots protocol is unavailable.
- **0-byte index.db recovery** — truncated index files are now detected and deleted on startup, triggering automatic rebuild from .sfs files.
- **Auto-reindex on empty index** — if index has 0 sessions after schema creation, flags for full reindex.

## [0.9.7.3] - 2026-04-06

### Added
- **Knowledge base docs** — new page covering the full knowledge loop, entries, compile, wiki, MCP write tools, API endpoints, best practices.
- **Dashboard user guide** — new page covering all 9 dashboard pages and keyboard shortcuts.
- **Organizations docs** — new page covering org creation, invites, roles, seats, billing.
- **Billing & tiers docs** — new page covering 5 tiers, Stripe integration, beta mode.

### Changed
- **CLI reference** — added ~15 missing commands (project *, doctor, init, security, mcp install/uninstall).
- **MCP docs** — all 12 tools documented in 3 categories with full parameter tables.
- **API reference** — added 16 missing endpoints across auth, sessions, share links, handoffs, settings.
- **Quickstart** — added `sfs init` wizard, project knowledge setup, `sfs doctor`.
- **Project context docs** — compile workflow, auto-narrative, knowledge loop integration.
- **Environment vars** — added Stripe, URL, and storage/retention variables.
- **Docs landing** — "memory layer" tagline, knowledge base and dashboard cards, 70+ commands.
- **Site sidebar** — Knowledge Base in Features, new Platform section (Dashboard, Organizations, Billing).
- **Pro tier features** — added "Living knowledge base", "Agent write-back (MCP)", "Project context + wiki".

### Fixed
- `sfs project ask` URL-encodes search queries (spaces in questions broke API calls).
- `sfs project ask` uses keyword-based search instead of exact phrase matching.
- Site knowledge base page: corrected tool names (`update_wiki_page`, `list_wiki_pages`).
- Site footer tagline: "Portable AI coding sessions" → "Memory layer for AI coding agents".
- Docs: removed nonexistent `POST /auth/login` endpoint from self-hosted guide.
- Docs: DLP and enterprise features marked as planned/coming (not shipped).
- Docs: resume `--in` targets corrected to 4 bidirectional tools only.

## [0.9.7.2] - 2026-04-06

### Added
- **Command palette search** — Cmd/Ctrl+K shortcut, arrow key navigation, grouped results, ARIA combobox semantics.
- **Mobile nav drawer** — slide-in hamburger menu with all navigation links, backdrop, ARIA dialog.
- **Mobile search filters** — sheet-style filter panel with staged changes, apply/clear, active filter chips.
- **Focus trapping** — shared `useFocusTrap` hook on all 4 modal dialogs.
- **ARIA live regions** — toast notifications, background task status, handoff badges announce to screen readers.
- **Zod form validation** — create project, handoff, login/signup, judge settings forms validated on blur/submit.
- **Shared wordmark component** — consistent SessionFS branding across shell and login.
- **App-level UI tests** — Vitest + Testing Library setup with route-level tests for search, layout, login.

### Changed
- **Code splitting** — all routes lazy-loaded, main bundle 676KB → 262KB, no chunk size warning.
- **Sessions hero** — compact action-oriented layout with prominent Resume button, inline handoff/project chips.
- **Session rows** — stronger titles, dimmer metadata, solid Resume button on hover.
- **Project cards** — health badges (sessions, auto-narrative), name as title, skeleton loading.
- **Project detail tabs** — fade transitions, sliding underline, workflow hints, context tab as hero artifact.
- **Typography** — stronger light-mode contrast, 6-level type scale, tuned brand color.
- **Product identity** — site-aligned typography and tokens, updated shell and login branding.
- Deduplicated `formatBytes`, `getAvatarColor`, `TOOL_COLORS` into shared utils.

### Fixed
- MCP remote server "No read stream writer available" race condition — `asyncio.Event` ensures transport connected before handling requests.

## [0.9.7.1] - 2026-04-06

### Added
- **GitLab settings CRUD** — `GET/PUT/DELETE /api/v1/settings/gitlab` for bootstrapping GitLab MR integration.
- **Watchlist status API** — `PUT /sync/watch/{session_id}/{status}` for daemon sync lifecycle tracking.
- **Handoff metadata snapshots** — session title/tool/model/tokens frozen at handoff creation (migration 021).
- **Knowledge backfill** — creating a project backfills knowledge from already-synced sessions via blob archives.
- 5 new compiler integration tests (repeated compile dedup, unverified promotion, mixed confidence, cross-batch).

### Fixed
- **Billing isolation** — org Team checkout creates separate Stripe customer; webhook handlers use org-first branching; personal-vs-org subscription detection throughout portal/status/checkout.
- **Handoff security** — recipient verification on claim, auth required on detail/summary, claimed summary blocked, recipient session ID persisted and returned from all routes.
- **Knowledge compiler** — content-level dedup (not session-level bail), verified/unverified promotion across compiles, ephemeral section rebuild, low-confidence entries only in Unverified, intra-batch dedup.
- **Sync engine** — conflict re-dirtying, selective watchlist fetched from server, pagination for remote session list, cross-user session-ID reuse blocked, stale file cleanup on pull.
- **Admin dashboard** — user_id→id contract fix, activity tab endpoint/fields, self-demotion guard, search param, enterprise/starter tiers, org membership cleanup on delete.
- **MCP install** — stale registration repair, malformed config handling, timeout isolation, UnicodeDecodeError handling.
- **Share links** — passwords moved from GET query to POST body, PBKDF2 hashing with salt, configurable API URL, alias-aware revoke.
- **Webhooks** — GitHub signature enforcement when secret configured, GitLab route path fix (`/webhooks/gitlab` → `/gitlab`), GitLab MR comment dedup tracking, per-user webhook secret verification.
- **Dashboard** — compile refreshes all project/wiki queries, search shows aliases, quick preview uses newest-first, jump-to-message always forces oldest-first, message order resets on session change, mobile nav for Billing/Admin, accessible dialogs, keyboard support, billing error display.
- **Org management** — seat capacity re-checked on invite acceptance, invite email normalization, signup email normalization.
- Database migrations 020–023.

### Security
- Share-link passwords no longer exposed in URLs (moved to POST body).
- Share-link password storage upgraded from SHA-256 to PBKDF2-HMAC-SHA256 with random salt.
- GitHub webhooks reject missing signature when secret is configured.
- Handoff detail/summary endpoints require authentication; restrict access to sender/recipient.
- Claimed handoff summaries return 410 (no data leakage after claim).
- Cross-user session-ID reuse blocked on sync push.
- Same-user session-ID reuse expires pending handoffs and revokes share links.
- Org non-admin members cannot access org Stripe portal.

## [0.9.7] - 2026-04-04

### Added
- **Living Project Context** — auto-summarize on sync, knowledge entries (6 types), structured compilation, section pages, concept auto-generation, regenerate.
- **3 MCP write tools** — `add_knowledge`, `update_wiki_page`, `list_wiki_pages` (12 MCP tools total).
- **MCP search tools** — `search_project_knowledge`, `ask_project`.
- **Wiki pages** — `knowledge_pages` + `knowledge_links` tables, multi-document structure, backlinks.
- **Auto-narrative toggle** per project (runs LLM narrative on sync when enabled).
- **Self-healing SQLite index** — auto-deletes corrupted `index.db`, rebuilds from `.sfs` files.
- **`handle_errors` decorator** on all CLI commands (no raw tracebacks).
- **`sfs doctor`** — 8 health checks with auto-repair.
- **Message pagination** — newest-first default, order toggle, sidechain/empty filtering.
- **Multi-select bulk delete + Find Duplicates** in session list.
- **Delete session from session detail.**
- **CLI commands** — `sfs project compile`, `entries`, `health`, `dismiss`, `ask`, `pages`, `page`, `regenerate`, `set`.
- Database migration 018: `knowledge_entries` + `context_compilations`.
- Database migration 019: `knowledge_pages` + `knowledge_links` + `auto_narrative`.

### Changed
- `get_project_context` MCP tool includes wiki pages + pending entries + contribution instructions.
- Compilation creates separate section pages per entry type.
- Dashboard: Knowledge Entries, Pages, History tabs on project detail.
- Session titles skip system/developer prompts, filter "You are " prefix.
- Hide `<synthetic>` model and zero token counts from dashboard.
- Billing page handles API errors gracefully.
- Footer version v0.9.7.
- Project cards show session count.
- Project context renders as formatted markdown (ReactMarkdown).

### Fixed
- Search endpoint uses `check_feature()` not raw `user.tier` (Baptist Health fix).
- Cross-tool resume: full transcript via `--append-system-prompt-file`, CWD-based scope.
- Codex watcher skips `sessionfs_import` sessions.
- All Sessions count uses real API total (not folder count).
- Bulk delete reports partial failures.
- Stronger duplicate detection key (includes etag).
- Project access control on all knowledge + wiki routes (security fix).
- 6 API/dashboard contract mismatches fixed.
- Bookmark icon state wired through prop chain.

### Security
- Project-level access control on all 13 knowledge + wiki routes.
- `dismiss_entry` route access check added.

## [0.9.6] - 2026-03-30

### Added
- **Self-hosted license lifecycle** — migration 017, grace period state machine (valid → warning → degraded → 403), validation logging, admin CLI commands (`sfs admin create-trial`, `sfs admin create-license`, `sfs admin list`, `sfs admin extend`, `sfs admin revoke`), admin API (CRUD + history), Helm local mode (seed job with retry + cache fallback), dashboard licenses tab.
- **Dashboard full redesign** — light/dark mode with proper contrast, resume-first layout replacing analytics cards, date-grouped sessions, lineage grouping (collapsible follow-ups), left rail navigation, page transitions, toast notifications, skeleton loading.
- **Narrative session summaries** — `POST /summary/narrative` generates what_happened, key_decisions, outcome, open_issues from session data via LLM. Dashboard SummaryTab has "Generate Narrative" button. Pro+ tier gated.
- **Project Context dashboard page** — list, detail, markdown editor, create/delete from the web UI.
- **Cross-tool resume improvements** — full conversation transcript via `--append-system-prompt-file`, 50-message trim + handoff context for better continuity.
- **MCP install for all 8 tools** — was 3, now supports all tools. Codex uses `codex mcp add`, Gemini uses `gemini mcp add`.
- **`sfs init` wizard** — auto-detects all 8 tools, optional sync setup during first run.
- **`sfs security scan/fix`** — config permissions audit, API key exposure check, dependency audit.
- **Skill/slash command detection** — recognized across all converters for accurate tool call counts.
- **Multi-select bulk delete** — select and delete multiple sessions from dashboard.
- **Find Duplicates** — dashboard feature to identify duplicate sessions.
- **Delete session from detail** — remove a session directly from the session detail page.
- **Enterprise page** — rewritten for AI governance messaging.
- **Feature page** — `/features/project-context/` deep-dive.
- **Cross-site theme consistency** — dashboard passes `?theme=` parameter to marketing site.
- **Handoff UX** — status stepper and session context card for clearer handoff flow.

### Changed
- LLM Judge revamped — confidence scores (0-100) per finding, CWE mapping (CWE-393, CWE-684, CWE-1104, etc.), evidence linking, dismiss/confirm findings with reason tracking.
- Dashboard analytics section replaced with resume-first hero section.
- Tool call capture fixed for Gemini CLI (reads `toolCalls` array) and Amp (parses `tool_use`/`tool_result` blocks).
- Audit now warns when a session has 0 tool calls.
- Session deduplication — Codex watcher skips `sessionfs_import` sessions.
- Search endpoint fixed to use effective org tier instead of raw `user.tier`.
- 958 tests passing.

### Security
- **Security pipeline** — GitHub Action running pip-audit, Trivy container scanning, and Bandit static analysis on every push.
- **Dependabot** enabled for automated dependency update PRs.
- **SECURITY.md** — responsible disclosure policy published.

## [0.9.5] - 2026-03-30

### Added
- **FSL licensing** — Dual-license model: MIT core (`src/sessionfs/`) + FSL-1.1-Apache-2.0 enterprise (`ee/sessionfs_ee/`). Following PostHog/Sentry pattern.
- **Server-side tier gating** — 5 tiers (Free, Starter, Pro, Team, Enterprise) with 30+ gated features. `require_feature()` middleware on sync, handoff, audit, summary, and project endpoints. 403 responses include `upgrade_url` and `required_tier`.
- **RBAC** — Organizations with admin/member roles. Effective tier resolution (org tier for team users, user tier for solo). Permission checks on billing, invite, and settings endpoints.
- **Organization management** — `POST/GET /api/v1/org`, invite via email with 7-day expiry, accept/revoke invites, change roles, remove members, seat limit enforcement.
- **Stripe billing** — Checkout sessions, Customer Portal, webhook handler (checkout.completed, subscription.updated/deleted, invoice.payment_failed), idempotent event processing.
- **Helm license validation** — `POST /api/v1/helm/validate` endpoint. Init container validates license key on pod start. License tier determines feature availability.
- **Telemetry endpoint** — `POST /api/v1/telemetry` with PII rejection. Opt-in, anonymous usage data collection.
- **Client version tracking** — Sync client sends `X-Client-Version`, `X-Client-Platform`, `X-Client-Device` headers. Dashboard shows version with update prompt when outdated.
- **CLI org commands** — `sfs org info`, `sfs org create`, `sfs org invite`, `sfs org members`, `sfs org remove`.
- **CLI upgrade prompts** — Friendly messages when API returns 403 for tier/role/storage limits.
- **Dashboard billing page** — Tier comparison cards, Stripe Checkout redirect, Customer Portal link, storage usage meter.
- **Dashboard org management** — Members table, invite form, role switching, pending invites list. Admin-only controls hidden for members.
- **Dashboard version widget** — Settings page shows last synced package version, platform, device, and "Update available" badge when outdated.
- Database migration 016: users billing columns, organizations, org_members, org_invites, stripe_events, helm_licenses, telemetry_events.

- **LLM Judge revamp** — Confidence scores (0-100) per finding, evidence snippets with source references, CWE category mapping (CWE-393, CWE-684, CWE-1104, etc.), claim-evidence linking in judge prompt.
- **Audit dismiss/confirm** — Human-in-the-loop: dismiss findings as false positives with reason, un-dismiss, dismissed findings section in dashboard. `POST /audit/dismiss` endpoint persists to blob storage.
- **Narrative session summaries** — LLM-powered: `POST /summary/narrative` generates what_happened, key_decisions, outcome, open_issues from session data. Reuses judge provider system (BYOK). Pro+ tier gated. Dashboard SummaryTab has "Generate Narrative" button.
- **Dashboard analytics cards** — Session list shows: Sessions Today (with yesterday comparison), Tool Breakdown (color bar + top 3), Total Tokens, Peak Hours. Computed client-side from loaded data.
- **Resume preview** — Session detail shows last 3 messages, files touched, and "Resume in [tool]" copy button before tab content.
- **MCP tools expanded** — `get_session_summary` and `get_audit_report` tools added (now 7 total). Summary returns deterministic stats; audit returns trust score, findings, confidence.

### Changed
- Default test user tier set to "pro" (was "free") to reflect pre-gating behavior.
- Sync client sends platform and device info alongside version in request headers.
- `/api/v1/auth/me` returns `last_client_version`, `last_client_platform`, `last_client_device`, `last_sync_at`, `latest_version`.
- Judge prompt restructured: each claim paired with its relevant evidence instead of flat dump.
- AuditTab.tsx rewritten with unified FindingCard component, confidence bars, evidence viewer, dismiss buttons.
- Export formats (Markdown, CSV) include confidence and CWE columns.
- 921 tests passing.

## [0.9.1] - 2026-03-28

### Fixed
- **Handoff resume crashes on receiver's machine** — falls back to CWD when sender's path doesn't exist instead of `FileNotFoundError`
- **SQLite "database locked" errors** — added `busy_timeout=5000ms` to session index and MCP search index
- **Audit modal ignores custom base URL** — `runAudit` now passes `base_url` through the full chain (API client, BackgroundTasks, server)
- **Model discovery fails after saving settings** — server-side `/judge/models` endpoint now uses saved encrypted API key as fallback
- **Re-audit page shows hardcoded models** — AuditModal now loads `base_url` from saved settings and runs model discovery with dropdown
- Added "Test Connection" button to AuditModal for custom base URLs

## [0.9.4] - 2026-03-30

### Added
- **Session lineage** — `parent_session_id` tracked through resume, sync push, and API. `sfs show` displays parent/children. `sfs list --tree` groups by lineage. Dashboard shows fork banner and "↩ fork" badges.
- **Cursor tool call extraction** — reads `agentKv:blob:` layer from Cursor's SQLite DB. Sessions go from 0 to 900+ tool calls, making audit functional for Cursor.
- **Deploy pipelines** — GitHub Actions for Dashboard (Vercel) and Site (Vercel), triggered on push to main when respective dirs change.
- **Product site** — Astro + Starlight site with 26 pages: home, features (4 deep-dives), pricing, enterprise, changelog, blog placeholder, and 16 Starlight docs.
- `sfs daemon rebuild-index` — rebuilds local SQLite index from .sfs files, backfills `source_tool`.
- `sfs summary --today` — shows table of all sessions captured today.
- `GET /api/v1/summaries?since=&until=` — batch summary API for date range reporting.
- Auto-summarize trigger settings API (`GET/PUT /settings/summarize-trigger`).
- Signup rate limit (5 per IP per hour).

### Fixed
- **Claim extractor for Claude Code** — two-strategy extraction (tool-context + standalone regex). 0 claims → 54+.
- **Auto-audit wiring** — `on_sync` trigger now calls audit API after push. `on_pr` checks user setting in webhook handler.
- **GitLab webhook DB session** — uses `_session_factory` instead of non-existent `app.state.db_session_factory`.
- **Daemon settings poll** — async `httpx.AsyncClient` instead of blocking sync `httpx.get`.
- **SummaryTab crash** — moved `useState` hooks before conditional returns (React hooks rules).
- **Audit modal ignores base URL** — `runAudit` now passes `base_url` through full chain.
- **Model discovery with saved key** — server falls back to user's encrypted key when no explicit key provided.
- **Handoff resume path** — falls back to CWD when sender's path doesn't exist on receiver's machine.
- **SQLite "database locked"** — `busy_timeout=5000ms` on session index and MCP search index.
- **`sfs config show`** — Rich markup no longer swallows TOML `[section]` headers.
- **`sfs summary --today`** — checks both local and UTC dates, checks `updated_at` too.
- Evidence truncation raised from 200 to 2000 chars.
- Proper error responses: 422 for 0 claims with tool calls, 502 for LLM errors, 504 for timeouts.

### Security
- **Config file permissions** — `config.toml` and `daemon.json` now `chmod 600` after write (was world-readable).
- **pip-audit in CI** — Python dependency vulnerability scanning on every push.
- **Trivy on GHCR images** — all 3 published images (API, MCP, Dashboard) scanned for CRITICAL/HIGH CVEs.
- **Signup rate limit** — 5 signups per IP per hour prevents account enumeration.

### Changed
- Landing page replaced with Astro + Starlight product site (26 pages).
- README commands table expanded to 34 entries.
- CLI reference expanded to 37+ command sections.
- 848 tests passing.

## [0.9.3] - 2026-03-28

### Added
- **LLM Judge V2** — severity-classified findings (critical/high/low), category detection (test_result, file_existence, command_output, etc.), auto-assigned from category
- **Audit history** — every audit persisted in `audit_reports` table with model, provider, scores, findings
- **Audit history API** — `GET /api/v1/sessions/{id}/audits` (list), `GET /api/v1/audits/{id}` (detail)
- **Auto-audit trigger** — configurable: manual, on_sync, on_pr. Dashboard radio buttons + `audit_trigger` on users
- **GitLab webhook** — `POST /webhooks/gitlab` for merge request events, posts AI context comments on MRs
- **GitLab client** — cloud + self-hosted support, encrypted token storage
- **Session summarization** — deterministic extraction: files modified/read, commands, test results (pytest/jest/go test), packages, errors, duration
- `sfs summary <id>` CLI command with `--format md` for markdown export
- Dashboard Summary tab with metric cards, files, activity, errors sections
- `GET/POST /api/v1/sessions/{id}/summary` API endpoints
- PR/MR comments now include contradictions table when audit exists
- Updated judge prompt to return category alongside verdict
- `audit_reports`, `gitlab_settings`, `session_summaries` database tables (migrations 014-015)

### Fixed
- **Claim extractor for Claude Code sessions** — new two-strategy extraction (tool-context + standalone regex) produces 50+ claims from sessions that previously returned 0
- Evidence truncation raised from 200 to 2000 chars
- Proper error responses: 422 for 0 claims with tool calls, 502 for LLM HTTP errors, 504 for timeouts

### Changed
- Landing page updated for v0.9.3 — Judge V2 spotlight, Session Summary spotlight, Enterprise section, 8-card feature grid, fixed handoff command
- Judge prompt returns `category` instead of `severity` — severity auto-assigned deterministically
- Dashboard AuditTab redesigned — contradictions-first, metric cards, collapsible verified/unverified, audit history

## [0.9.0] - 2026-03-28

### Added
- **Autosync** — automatic session sync to cloud with three modes: off (default), all, or selective
- `sfs sync auto --mode all|selective|off` — set autosync mode
- `sfs sync watch <id>` / `sfs sync unwatch <id>` — manage selective watchlist
- `sfs sync watchlist` — show watched sessions with sync status
- `sfs sync status` — show mode, counts, storage, queued/failed
- API endpoints: `GET/PUT /api/v1/sync/settings`, `GET /api/v1/sync/watchlist`, `POST/DELETE /api/v1/sync/watch/{id}`, `GET /api/v1/sync/status`
- Dashboard autosync settings (radio buttons for off/all/selective)
- Dashboard API client methods for sync settings, watchlist, and status
- Daemon debounces session changes before pushing (30s default, configurable)
- Daemon polls API for settings changes every 60 seconds
- `sync_watchlist` database table for selective mode tracking
- `sync_mode` and `sync_debounce` columns on users table

### Fixed
- **Handoff claim now copies session data** — blob is duplicated in storage, new session record created for recipient (was only recording `claimed_at` without copying)
- **Ingress routes all traffic through dashboard nginx** — removed separate `/api` and `/mcp` ALB target groups that stayed in "unused" state on EKS
- **Nginx body size limit** — added `client_max_body_size 100m` (configurable via `dashboard.clientMaxBodySize`) to prevent 413 on large session uploads
- MCP proxy added to dashboard nginx for internal routing

## [0.8.3] - 2026-03-27

### Added
- `SFS_SMTP_VERIFY_SSL` — disable SSL certificate verification for internal SMTP relays with self-signed certs
- Model discovery endpoint `GET /api/v1/settings/judge/models` — queries OpenAI-compatible `/v1/models` to list available models
- Dashboard auto-discovers models when custom base URL is set (dropdown instead of hardcoded list)

### Fixed
- Handoff email showed wrong command (`sfs pull --handoff`) — corrected to `sfs pull-handoff`
- CLI handoff hint showed same wrong command
- LLM Judge returned 403 when using custom base URL without API key (Ollama, local vLLM) — API key is now optional when `base_url` is set
- SMTP email logger not visible in uvicorn container logs — propagated to root logger

## [0.8.2] - 2026-03-27

### Added
- **Custom base URL for LLM Judge** — `--base-url` flag, `SFS_JUDGE_BASE_URL` env var, and dashboard settings field for LiteLLM, vLLM, Ollama, Azure OpenAI, and any OpenAI-compatible gateway
- **Shared project context** — `sfs project init|edit|show|set-context|get-context` CLI commands, REST API, and MCP `get_project_context` tool for team-shared instructions
- Project context database migration (projects table)
- Judge base URL database migration (user_judge_settings.base_url column)
- `docs/project-context.md` guide

### Fixed
- **Codex resume broken** — rollout files used `source: "custom"` which Codex CLI rejects on resume; changed to `source: "cli"` with `originator: "codex_cli_rs"`
- Codex rollout missing `output`/`exit_code` on `local_shell_call` payloads and `status` on `function_call` payloads
- Codex rollout had extra fields (`id`, `end_turn`) on message payloads that native format doesn't use
- `sfs resume --in codex` hint said `codex --resume` (wrong flag) then `codex --thread` (wrong flag) — Codex uses `codex resume` subcommand

### Changed
- `sfs resume` now auto-launches the target tool instead of printing a copy-paste command:
  - Claude Code: `claude --resume <id>`
  - Codex: `codex resume <uuid>`
  - Gemini: `gemini --resume latest`

## [0.8.1] - 2026-03-26

### Fixed
- Session ID validation now accepts 8-40 character IDs (was 12-20) — fixes sync rejection for short-form IDs like `ses_ae7652a4`
- Rate limiter now respects `SFS_RATE_LIMIT_PER_MINUTE` environment variable — was hardcoded to 100, now reads config (default 120, set to 0 to disable)
- Removed `?sslmode=require` from Helm database URL template — asyncpg handles SSL internally
- MCP service port corrected from 3001 to 8080 in Helm chart — fixes pod crash-loops from failed health probes
- Dashboard no longer hardcodes `api.sessionfs.dev` — uses `VITE_API_URL` env var, falls back to `window.location.origin`
- Dashboard health endpoint returns JSON `{"status":"ok"}` instead of plain text
- S3 bucket names containing `/` (e.g. `my-bucket/prefix`) are split gracefully into bucket + prefix

### Added
- Request logging middleware — all 4xx responses logged at WARNING level with method, path, status, and client IP
- Structured logging for session ID validation failures
- Rate limit rejection logging with client IP and configured limit
- `SFS_S3_PREFIX` environment variable for S3 key prefixes
- `storage.s3.prefix` in Helm values.yaml
- Dashboard nginx ConfigMap in Helm chart — proxies `/api/` to API service for self-hosted deployments
- GHCR dashboard image now built with `VITE_API_URL=https://api.sessionfs.dev`
- `docs/troubleshooting.md` — error responses, session ID format, sync debugging, Kubernetes issues
- IRSA IAM policy example in `docs/self-hosted.md`
- Rate limit configuration docs in `docs/self-hosted.md`

### Changed
- Default rate limit increased from 100 to 120 requests per minute per API key
- Session ID regex changed from `[a-zA-Z0-9]{12,20}` to `[a-z0-9]{8,40}` (lowercase only, wider range)
- `externalDatabase.sslMode` removed from Helm values.yaml (was causing asyncpg errors)

## [0.8.0] - 2026-03-26

### Added
- **`sfs storage`** — shows local disk usage with progress bar, session counts, retention policy
- **`sfs storage prune`** — prune old sessions with `--dry-run` and `--force` flags
- **`sfs daemon restart`** — restart daemon in one command
- **Multi-provider email** — SMTP support alongside Resend, with auto-detection and null provider for air-gapped deployments
- **`SFS_REQUIRE_EMAIL_VERIFICATION`** — disable email verification for internal deployments
- **Gemini model extraction** — reads model name from `logs.json` per session (no more blank Model column)
- **Environment variables reference** — `docs/environment-variables.md` with all `SFS_*` vars
- Gemini 3.1 Pro, 3 Pro, 3 Flash model abbreviations in CLI

### Changed
- Daemon auto-prunes synced sessions hourly based on retention policy (90-day default, 30-day for synced)
- Daemon warns at 80% storage, pauses capture at 95%
- Default local storage cap: 2 GB
- Helm chart security contexts now configurable via values.yaml (no more hardcoded `runAsNonRoot: true`)
- Dashboard nginx runs as non-root on port 8080 (fixes permission errors on EKS)
- Migration job uses `SFS_DATABASE_URL` (was `DATABASE_URL`) and async driver
- Email section in Helm values.yaml supports Resend, SMTP, existing secrets, and provider selection
- `sfs watcher enable/disable` reuses `sfs daemon restart` instead of duplicating logic

### Fixed
- Migration job falling back to SQLite due to env var mismatch (`DATABASE_URL` vs `SFS_DATABASE_URL`)
- asyncpg crash on `?sslmode=require` in database URL — SSL params now handled internally
- Dashboard nginx `chown` permission errors on EKS (`/var/cache/nginx/client_temp`)
- Helm migration URL using sync `postgresql://` driver instead of `postgresql+asyncpg://`
- Gemini sessions showing blank model name in `sfs list`

## [0.7.1] - 2026-03-25

### Fixed
- Landing page rendering raw JavaScript — duplicate script block appended after `</html>`
- Container image publishing to GHCR never triggered — workflow used `release: published` event which doesn't fire from `GITHUB_TOKEN`; changed to `workflow_run` trigger

## [0.7.0] - 2026-03-25

### Added
- **`sfs watcher list`** — shows all 8 tools with enabled/disabled status and install detection
- **`sfs watcher enable/disable`** — toggle tool watchers with automatic daemon restart

### Changed
- Landing page scroll animations visible by default (progressive enhancement)
- Stars badge moved from nav to footer
- Waitlist CTA now functional (mailto)

### Fixed
- Content invisible on first load due to fade-up animation gating visibility
- GitHub App secrets wired to Cloud Run via Terraform

## [0.6.0] - 2026-03-25

### Added
- **GitHub PR AI Context App** — automatically comments on PRs with linked AI sessions, tools, trust scores
- **Webhook handler** at `/webhooks/github` with HMAC-SHA256 signature verification
- **Git metadata indexing** — sessions store normalized remote, branch, commit for fast PR matching
- **PR comment builder** — single and multi-session markdown with dashboard links
- **Installation management** — toggles for auto-comment, trust scores, session links
- **"What is this?" page** — `docs/github-app.md` conversion funnel for PR reviewers
- **Jump to message** in audit findings — switches to Messages tab at correct page
- **Bookmark folders** — create colored folders, bookmark sessions, filter by folder
- **Background audit** — large sessions return 202, floating toast indicator while processing
- **Numbered page navigation** for messages (1, 2, 3... not just Next)
- **Helm chart** for Kubernetes self-hosted deployment
- **GHCR image publish pipeline**

### Fixed
- Search SQL asyncpg ambiguous parameter types
- Cursor parser NULL bubble values
- Audit timeout on large sessions (10 claim-dense chunks max)
- Messages pagination exceeding server 100 limit
- Missing JSONResponse import in audit route

### Security
- `*.pem` added to gitignore
- `github-app-manifest.json` added to private files
- `.vercel/` directories blocked from commits

## [0.5.0] - 2026-03-25

### Added
- **Bookmark folders** — create colored folders, bookmark sessions, filter session list by folder
- **Background audit** — large sessions (500+ msgs) return 202 and run in background with floating toast indicator
- **Helm chart** — production Kubernetes deployment (API, MCP, Dashboard, PostgreSQL, migrations, ingress, network policies, HPA)
- **Dashboard Dockerfile** — containerized dashboard for self-hosted deploy
- **GHCR publish pipeline** — builds and pushes api/mcp/dashboard images on release
- **Audit status endpoint** — `GET /audit/status` for polling background audits
- **Self-hosted docs** — `docs/self-hosted.md` with AWS/GCP/generic K8s examples

### Fixed
- Search SQL ambiguous parameter types on PostgreSQL (asyncpg)
- Cursor parser crash on NULL bubble values in SQLite
- Audit timeout on large sessions (now capped at 10 claim-dense chunks)
- Audit modal closes immediately — no more blocking the UI

## [0.4.0] - 2026-03-24

### Added
- **Session aliases** — `sfs alias ses_d20e auth-debug` then use `auth-debug` everywhere (show, push, pull, resume, search)
- **Created date column** in dashboard session list
- **Server-side pagination** in dashboard (Previous/Next)
- **Landing page redesign** — 10 sections, glass-morphism, scroll animations, free-during-beta pricing
- **Logo PNGs** — portal mark at 512/256/128px

### Changed
- GitHub org migrated from `alwaysnix` to `SessionFS`
- Dashboard readability: brighter colors (#f0f3f6/#d0d7de), 16px base, #0d1117 background
- LLM providers: correct model IDs, reasoning model support, Google auth header
- develop branch is LOCAL ONLY with pre-push hook

### Fixed
- Cursor parser NULL bubble values
- Sync duplicate key race conditions (5 fixes)
- Daemon crash on status write
- OpenRouter response_format rejection
- Admin user list showing deleted users
- Dashboard pagination breaking after first page

### Security
- Deleted develop from public origin (was accidentally pushed)
- Pre-push hook prevents future develop pushes

## [0.3.3] - 2026-03-24

### Added
- **Landing page redesign** — 10 sections, asymmetric hero, glass-morphism, scroll animations
- **Free during beta** pricing — replaced 4-tier grid with beta message
- **Logo PNGs** — 512, 256, 128px versions of the portal mark

### Changed
- GitHub org migrated from `alwaysnix` to `SessionFS`
- All repo URLs, WIF bindings, landing page links, pyproject.toml updated
- Dashboard readability: background `#0d1117`, text `#f0f3f6`/`#d0d7de`, base 16px, all text-xs→text-sm
- Landing readability: body text `#c9d1d9`, terminal 14px min, feature descriptions 16px
- LLM providers: OpenAI `max_completion_tokens`, reasoning models use effort not temperature, Google `x-goog-api-key` header, correct model IDs
- `develop` branch is now LOCAL ONLY with pre-push hook protection

### Fixed
- OpenRouter 400: removed unsupported `response_format`
- OpenAI reasoning models (o3/o4-mini) rejecting `temperature` parameter

### Security
- Deleted `develop` from public origin (was accidentally pushed with internal files)
- Pre-push hook blocks future develop pushes

## [0.3.2] - 2026-03-24

### Added
- **Admin dashboard** — user management, tier control, system stats, audit log
- **Brand identity** — portal logo, color system (#4f9cf7/#3ddc84/#0a0c10), favicon, social preview
- **Tier-based sync limits** — 50MB free, 300MB paid/admin (was flat 10MB)

### Fixed
- Daemon crash on status file write (FileNotFoundError now caught)
- Daemon main loop catches all exceptions (no more crash on transient errors)
- Sync duplicate key race condition (concurrent push from daemon)
- Sync MissingGreenlet from broken async rollback (replaced with upfront check)
- Sync soft-deleted sessions blocking new pushes (reuse deleted rows)
- Sync missing If-Match header on first push (header now optional)
- Admin user list showing deactivated users (now filtered)
- Ruff lint errors for CI green

## [0.3.1] - 2026-03-24

### Added
- **Audit report export** — download as JSON, Markdown, or CSV (CLI `--format` + dashboard dropdown)
- **OpenRouter provider** — access 400+ models via single API key, auto-detected by `/` in model name
- **Stored judge API keys** — Fernet-encrypted at rest, Settings page in dashboard
- **Audit status indicators** — trust score badges on session list, detail sidebar, handoff emails, CLI list
- **`GET /api/v1/auth/me`** — returns user profile (email, tier, verified status)
- **Latest model dropdowns** — Opus 4.6, GPT-5.4, Gemini 3.1, DeepSeek V3.2/R1, o3, o4-mini

### Fixed
- Handoff inbox/sent routes returning 404 (wildcard route was catching them)
- Audit using hardcoded blob path instead of session.blob_key
- UTF-8 decode error when reading session archives with non-ASCII content
- AuditReport type not imported in dashboard AuditTab
- CLI `sfs auth status` now shows email, tier, and verification status

## [0.3.0] - 2026-03-23

### Added
- **Team handoff with email notifications** — `sfs handoff --to email --message` sends notification via Resend with session metadata, git context, and pull instructions
- **Smart workspace resolution** — when pulling a handoff, automatically finds the recipient's local clone by matching git remote URLs
- **Handoff dashboard** — inbox/sent tabs, detail page with claim button, handoff modal on session detail
- **LLM-as-a-Judge hallucination detection** — `sfs audit` evaluates AI responses against tool call evidence (BYOK — user provides their own API key)
- **Multi-provider judge** — supports Anthropic, OpenAI, Google via httpx (no SDK dependencies)
- **Consensus mode** — `sfs audit --consensus` runs 3 passes, reports only where 2+ agree
- **Trust score badges** — session list shows green/yellow/red audit badges
- **Audit dashboard tab** — expandable findings with verdict icons, severity badges, evidence
- **Compatibility guide** — `docs/compatibility.md` with full 8-tool matrix and technical reasons for capture-only
- **Remote MCP server** — `mcp.sessionfs.dev` with OAuth 2.1 PKCE + Dynamic Client Registration

### Changed
- Pricing: free tier changed from 25-session count to 14-day rolling retention
- Capture-only CLI messages now include tool-specific reasons and Copilot in alternatives
- Quickstart rewritten as 5-step hero workflow
- README restructured: hero workflow first, advanced features below
- Judge uses temperature=0 for deterministic output
- Judge verdict rules tightened: hallucination requires proof of contradiction, absence of evidence is unverified

### Fixed
- Dashboard auth persistence via sessionStorage (survives refresh)
- Vercel SPA routing (catch-all rewrite for direct URL access)
- Integer overflow on token counts (bigint migration 004)
- Duplicate search bar removed from session list
- All lint errors fixed (ruff clean, mypy clean)
- Node.js 24 opt-in for GitHub Actions
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-03-23

### Added
- **Copilot CLI support** — full capture and resume via events.jsonl injection
- **Amp support** — session capture from Sourcegraph Amp threads (capture-only)
- **Cline support** — session capture from VS Code extension storage (capture-only)
- **Roo Code support** — session capture from VS Code extension storage (capture-only)
- **MCP server** — 4 tools for AI tool integration (search, context, list, find related)
- **Full-text search** — PostgreSQL FTS for cloud, SQLite FTS5 for local CLI
- **Dashboard search** — search bar with instant results and full results page
- **Session search CLI** — `sfs search` with local and cloud modes
- **MCP install command** — `sfs mcp install --for claude-code|cursor|copilot`
- **Email verification** — gates cloud sync until email verified
- **Rolling retention** — free tier: 14-day cloud retention, Pro: unlimited
- **Share links** — 24h default expiry with optional password
- **10MB sync limit** — clear error with guidance to compact
- **GCS blob store** — Google Cloud Storage backend for production
- **Cloud Run deployment** — api.sessionfs.dev live on GCP
- **GitHub Actions CI/CD** — deploy pipeline with Trivy vulnerability scanning
- **Terraform infrastructure** — separate repo (sessionfs-infra) with plan-on-PR, apply-on-merge

### Changed
- Messaging overhaul: retired "Dropbox" analogy, new tagline "Stop re-prompting. Start resuming."
- Version sourced from pyproject.toml (single source of truth)
- SFS format version decoupled from package version

### Fixed
- Personal paths sanitized from spec examples
- .gitignore safety nets for internal files

## [0.1.0] - 2026-03-22

### Added
- Initial public release
- Claude Code session capture and resume
- Codex CLI session capture and resume
- Gemini CLI session capture and resume
- Cursor IDE session capture (capture-only)
- Background daemon with filesystem event watching
- CLI for browsing, exporting, forking, checkpointing sessions
- Cloud sync with push/pull and ETag conflict detection
- Self-hosted API server (FastAPI + PostgreSQL + S3)
- Web dashboard for session management
- Secret detection and path traversal protection
