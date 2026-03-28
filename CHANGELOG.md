# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.9.1] - 2026-03-28

### Fixed
- **Handoff resume crashes on receiver's machine** — falls back to CWD when sender's path doesn't exist instead of `FileNotFoundError`
- **SQLite "database locked" errors** — added `busy_timeout=5000ms` to session index and MCP search index
- **Audit modal ignores custom base URL** — `runAudit` now passes `base_url` through the full chain (API client, BackgroundTasks, server)
- **Model discovery fails after saving settings** — server-side `/judge/models` endpoint now uses saved encrypted API key as fallback
- **Re-audit page shows hardcoded models** — AuditModal now loads `base_url` from saved settings and runs model discovery with dropdown
- Added "Test Connection" button to AuditModal for custom base URLs

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
