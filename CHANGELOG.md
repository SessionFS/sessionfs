# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
