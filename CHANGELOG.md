# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
