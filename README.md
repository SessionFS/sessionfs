<p align="center">
  <img src="brand/logo-full.svg" alt="SessionFS" width="300">
</p>

# SessionFS

**Stop re-prompting. Start resuming.**

SessionFS captures your AI coding sessions and makes them portable across tools and teammates.

Start a session in Claude Code, resume it in Codex. Push a session to the cloud, your teammate pulls it with full context — conversation history, workspace state, tool configs, and token usage. No copy-pasting. No re-explaining.

## Supported Tools

| Tool | Capture | Resume |
|------|---------|--------|
| Claude Code | Yes | Yes |
| Codex CLI | Yes | Yes |
| Gemini CLI | Yes | Yes |
| Copilot CLI | Yes | Yes |
| Cursor IDE | Yes | Capture-only |
| Amp | Yes | Capture-only |
| Cline | Yes | Capture-only |
| Roo Code | Yes | Capture-only |
| Kilo Code | Yes | Capture-only |

## Quick Start

```bash
# 1. Install
pip install sessionfs

# 2. Start the daemon — it watches all 9 tools automatically
sfs daemon start

# 3. Use your AI tools normally — sessions are captured in the background

# 4. Browse captured sessions
sfs list

# 5. Resume a session (same tool or different)
sfs resume ses_abc123 --in codex
```

See the full [Quickstart Guide](docs/quickstart.md) for detailed steps.

## How It Works

The `sfsd` daemon uses filesystem events (fsevents on macOS, inotify on Linux) to watch native AI tool session storage. When it detects new or updated sessions, it converts them into the `.sfs` format — a portable directory containing `manifest.json`, `messages.jsonl`, `workspace.json`, and `tools.json`.

Each tool has its own watcher:
- **Claude Code** — watches `~/.claude/projects/` JSONL files
- **Codex CLI** — watches `~/.codex/sessions/` rollout files, reads SQLite index
- **Gemini CLI** — watches `~/.gemini/tmp/*/chats/` JSON sessions
- **Copilot CLI** — watches `~/.copilot/session-state/` event files
- **Cursor IDE** — reads `state.vscdb` SQLite database (capture-only)
- **Amp** — watches `~/.local/share/amp/threads/` JSON threads (capture-only)
- **Cline** — watches VS Code globalStorage task directories (capture-only)
- **Roo Code** — watches VS Code globalStorage task directories (capture-only)

Sessions are indexed locally for fast browsing via the CLI. Cloud sync is opt-in; the daemon defaults to local-only.

## Commands

| Command | Description |
|---------|-------------|
| `sfs list` | List captured sessions with filtering and sorting |
| `sfs show <id>` | Show session details, messages, and cost estimates |
| `sfs resume <id> [--in TOOL]` | Resume a session in any supported tool (auto-launches) |
| `sfs fork <id>` | Fork a session into a new independent session |
| `sfs checkpoint <id>` | Create a named checkpoint of a session |
| `sfs alias <id> <name>` | Set or clear a session alias |
| `sfs export <id>` | Export as `.sfs`, markdown, or Claude Code format |
| `sfs import` | Import sessions from any supported tool |
| `sfs search "query"` | Full-text search across all sessions |
| `sfs summary <id>` | Show session summary (files, tests, commands, packages) (or --today for daily overview) |
| `sfs audit <id>` | Audit a session for hallucinations with LLM-as-a-Judge |
| `sfs push <id>` | Push a session to the cloud |
| `sfs pull <id>` | Pull a session from the cloud |
| `sfs pull-handoff <hnd_id>` | Pull a session from a handoff link |
| `sfs list-remote` | List sessions on the cloud server |
| `sfs handoff <id> --to EMAIL` | Hand off a session to a teammate with email notification |
| `sfs sync` | Bidirectional sync (push + pull) |
| `sfs sync auto --mode MODE` | Set autosync mode: off, all, or selective |
| `sfs sync watch\|unwatch <id>` | Add/remove sessions from autosync watchlist |
| `sfs sync status` | Show autosync mode, counts, storage usage |
| `sfs project init\|edit\|show` | Manage shared project context for your team |
| `sfs project set-context FILE` | Set project context from a file |
| `sfs project get-context` | Output raw project context to stdout |
| `sfs project compile\|entries\|health\|dismiss` | Living Project Context — compile, browse, and manage knowledge |
| `sfs project ask\|pages\|page\|regenerate\|set` | Query knowledge, manage wiki pages, configure project |
| `sfs project link-repo\|unlink-repo\|repos` | Multi-repo projects — one project can own several git repos |
| `sfs project merge` | Consolidate two projects into one (dry-run by default) |
| `sfs rules init\|edit\|show\|compile` | Manage canonical project rules — compile once, drive every tool |
| `sfs rules push\|pull` | Sync canonical rules through the SessionFS API |
| `sfs persona list\|show\|create\|edit\|delete\|assume\|forget` | Manage agent personas; `assume`/`forget` toggle a persona-only local provenance bundle (Pro+) |
| `sfs ticket list\|show\|create\|start\|complete\|comment` | Manage agent tickets — start/complete writes a local provenance bundle so the daemon tags captured sessions (Team+) |
| `sfs ticket assign\|resolve\|escalate` | Re-route, close out (review→done with dep enrichment), or bump priority |
| `sfs ticket status\|block\|unblock\|reopen\|approve\|dismiss` | Ticket lifecycle transitions and active-bundle inspection |
| `sfs doctor` | Run 8 health checks with auto-repair |
| `sfs storage` | Show local disk usage and retention policy |
| `sfs storage prune` | Prune old sessions to free disk space |
| `sfs daemon start\|stop\|restart\|status\|logs` | Manage the background daemon |
| `sfs daemon rebuild-index` | Rebuild local session index from .sfs files on disk |
| `sfs watcher list\|enable\|disable` | Manage tool watchers |
| `sfs auth login\|signup\|status` | Manage cloud authentication |
| `sfs config show\|set` | Manage configuration |
| `sfs mcp serve` | Start MCP server (36 tools) for AI tool integration |
| `sfs mcp install --for TOOL` | Auto-configure MCP for all 8 supported tools |
| `sfs init` | Interactive setup wizard — auto-detects tools, optional sync |
| `sfs security scan\|fix` | Audit config permissions, API key exposure, dependencies |
| `sfs org create\|list\|show\|invite\|remove` | Manage organizations, members, and roles |
| `sfs admin reindex` | Re-extract metadata for all cloud sessions |
| `sfs admin create-trial\|create-license\|list\|extend\|revoke` | Manage self-hosted licenses |

See the full [CLI Reference](docs/cli-reference.md) for options and examples.

## Cross-Tool Resume

```bash
# Start in Claude Code, resume in Codex
sfs resume ses_abc123 --in codex

# Start in Gemini, resume in Claude Code
sfs resume ses_def456 --in claude-code

# Cursor sessions can be resumed in any bidirectional tool
sfs resume ses_ghi789 --in gemini
```

SessionFS converts between native formats automatically — message roles, tool calls, thinking blocks, and workspace state are mapped across tools. See [Compatibility](docs/compatibility.md) for details on which tools support resume and why some are capture-only.

## Cloud Sync (Optional)

```bash
# Create an account
sfs auth signup --url https://api.sessionfs.dev

# Push a session
sfs push <session_id>

# Pull on another machine
sfs pull <session_id>
sfs resume <session_id>
```

Free tier includes 14-day cloud retention with 1 device. See the [Sync Guide](docs/sync-guide.md) for setup, conflict handling, and self-hosted options.

## Session Search

```bash
# Search across all local sessions
sfs search "rate limiting middleware"

# MCP server lets AI tools search your past sessions
sfs mcp install --for claude-code
```

## Team Handoff

```bash
# Hand off a session to a teammate
sfs handoff ses_abc123 --to sarah@company.com

# Teammate pulls and resumes
sfs pull ses_abc123
sfs resume ses_abc123 --in codex
```

## Shared Project Context

Share architecture decisions, conventions, and team knowledge with every AI agent working on your codebase.

```bash
# Initialize project context (run from inside a git repo)
sfs project init
sfs project edit    # Opens in $EDITOR

# Any teammate with sessions in the repo can read it
sfs project show
```

AI agents connected via the MCP server can call `get_project_context` to read the document automatically. See [Project Context](docs/project-context.md) for details.

## Rules Portability

Maintain your project's AI instructions in one place. SessionFS compiles canonical rules into the tool-specific files each AI agent reads — `CLAUDE.md`, `codex.md`, `.cursorrules`, `.github/copilot-instructions.md`, `GEMINI.md` — so instructions stay consistent across every tool.

```bash
sfs rules init          # pick tools, seed canonical rules
sfs rules edit          # edit project preferences in $EDITOR
sfs rules compile       # write tool-specific files (commit them)
```

Compiled files are committed by default so fresh clones and teammates without SessionFS still get the same agent contract. Cross-tool resume preflights the target tool's rules file from current canonical rules (Case A/B/D write, Case C skip with warning). Each captured session records `rules_version`, `rules_hash`, and a full list of instruction artifacts so you always know what guided the agent. See [Rules Portability](docs/rules.md).

## Web Dashboard

A browser-based interface for browsing and managing synced sessions. Accessible at `http://localhost:8000` when running the self-hosted server, or at `app.sessionfs.dev` for cloud accounts.

## Self-Hosted Server

```bash
docker compose up -d
```

Starts the SessionFS API server, PostgreSQL, and web dashboard. See the [Sync Guide](docs/sync-guide.md#self-hosted) for full configuration.

## Session Format

Sessions are stored as `.sfs` directories:
- `manifest.json` — identity, provenance, model info, stats
- `messages.jsonl` — conversation history with content blocks
- `workspace.json` — git state, files, environment
- `tools.json` — tool definitions and shell context

All file paths are relative to workspace root. Sessions are append-only — conflict resolution appends both sides rather than merging.

## Status

**v0.13.1 — Public Beta.** Patch: fixes the SSO migration (056) so `alembic upgrade head` succeeds on PostgreSQL (055 declares the external-identity key as a `UniqueConstraint`, which PG backs with a constraint — `DROP INDEX` had to become a dialect-aware `DROP CONSTRAINT`; SQLite tests couldn't see it). Same test/migration/tool counts as v0.13.0.

**v0.13.0 — Public Beta.** 2669 backend tests + 397 dashboard tests passing. 56 database migrations. 68 MCP tools. **Organization SSO (OIDC).** Enterprise single sign-on: authorization-code + PKCE login (`/api/v1/auth/sso/start` + `/callback`), with anti-takeover account linking (auto-links only on a verified-email match to an already-verified account that is already a member of the IdP's org — identity keyed on `(org_idp_id, subject)`, never the mutable email), JIT provisioning (member-only, seat-capped), owner/admin provider config + DNS-TXT domain verification (client secret stored only as a reference), and org-wide enforcement with an owner break-glass — while service keys (CI / automation) stay categorically exempt. Hardened `id_token` validation + an SSRF-guarded fetch layer for all issuer-derived requests. New dependency `dnspython`; migrations 055 + 056. Available on all paid tiers.

**v0.12.1 — Public Beta.** 2525 backend tests + 397 dashboard tests passing. 54 database migrations. 68 MCP tools. **Multi-repo project fixes + autonomous work-queue reviewer path completed.** From a heavy user's multi-repo report: `sfs project repos` no longer crashes, projects can now be **renamed** (`PATCH /projects/{id}` + `sfs project set --name`), promoting a new primary repo demotes the old one instead of unlinking it, and `project merge` repo-linking is surfaced + proven. Plus the work-queue reviewer now posts trusted verdicts via the settle path (`complete_work_queue_step`), and org admins can register/revoke **trusted reviewers** (`/orgs/{id}/trusted-reviewers` + `sfs admin trusted-reviewers`) — completing the backend for an autonomous GPT-5.5/Codex reviewer. Additive only; no schema change (migrations 001–054 unchanged). All Codex-reviewed clean; Shield-SR CLEAN.

**v0.12.0 — Public Beta.** 2487 backend tests + 397 dashboard tests passing. 54 database migrations. 68 MCP tools. **Agent work queues + review-verdict trust hardening.** An agent (Claude / Codex / Gemini) can be pointed at a queue of tickets and repeatedly woken (via `/loop`, cron, or CI) to service, review, and close them using SessionFS as the source of truth — no human dispatcher. The server holds all loop state (it resumes with zero chat memory); `run_work_queue_step` returns one bounded directive at a time, `complete_work_queue_step` is the only durable advance, and a crash between them re-emits rather than loses or repeats work. Modes: `review_until_clean` (auto-finishes an item only on a server-verified, trusted, literal `VERIFIED-CLEAN`), `implement_until_done`, `triage`. Built-in safety envelope (attempt cap + 2m→5m→15m→60m backoff → `failed` for human reset, per-wake cap, cadence floor, dedicated rate limit). 6 new MCP tools (62 → 68); 2 new scopes (`work_queues:read`/`write`). **Security:** review verdicts now require a server-stamped `verdict_trusted` flag (set from the authenticated identity, never the request body), closing a spoof where a caller could forge a `VERIFIED-CLEAN`. Migrations 053–054. All tiers.

**v0.11.2 — Public Beta.** 2418 backend tests + 397 dashboard tests passing. 52 database migrations. 62 MCP tools. **Self-service licensing is now usable in the dashboard.** v0.11.0 shipped the licensing + org-management backend; this release adds the web UI: a guided **license-activation** flow (key → previewed org/tier → emailed single-use verification code → land as owner) for users not yet in an org, and **organization ownership transfer** (owner → admin, two-step with Accept/Decline + expiry). The owner role is shown in the members list and the owner row is protected. Also hardens `/me` and activation against a transient duplicate-membership race (no more 500s; clean 409). No schema change.

**v0.11.1 — Public Beta.** 2415 backend tests + 388 dashboard tests passing. 52 database migrations. 62 MCP tools. **Patch:** hardens the v0.11.0 entitlements backfill migration (a diagnostic statement could fail the upgrade on databases where the backfill recorded notes such as a plan-tier coercion or unmatched license) and restores a green CI. No schema change; no behavior change for databases already on v0.11.0.

**v0.11.0 — Public Beta.** 2415 backend tests + 388 dashboard tests passing. 52 database migrations. 62 MCP tools. **Licensing + organization-management redesign.** An organization's plan, seats, and storage now resolve from a single authoritative entitlements record instead of being scattered across user rows and license tables — which is what previously left enterprise admins unable to fully manage their own organizations. On that foundation: self-service license activation with **required email verification** (an org admin activates a key, then confirms a single-use, time-limited code sent to their address), two-step organization **ownership transfer** with an explicit owner role, and last-owner safety guards so an org can never be left without an owner. Existing orgs are backfilled deterministically, so no plan changes for anyone. Migrations 050–052 (entitlements foundation + ownership-transfer table + role/status integrity constraints). Backend + dashboard. Reviewed end-to-end across four phases — Codex + Sentinel design review, Codex code review, Shield-SR pre-release (all clean).

**v0.10.32 — Public Beta.** 2279 backend tests + 388 dashboard tests passing. 49 database migrations. 62 MCP tools. **Hotfix:** the Stripe `customer.subscription.updated` webhook no longer downgrades a paying customer to free (or clears their subscription pointer) on transient billing-health statuses like `past_due` — only a terminal `subscription.deleted` downgrades, which also restores the payment-recovery path. Prior feature — **Multi-repo projects** — a project can now own more than one git repo, so personas / knowledge / tickets / rules are shared across a product's repos instead of duplicated across split projects (migration 049 + `project_repos`). Verified-ownership repo linking (GitHub App installation proof, owner-attested fallback, verified-beats-unverified reclaim), `sfs project link-repo` / `unlink-repo` / `repos`, and `sfs project merge` to consolidate split projects (dry-run by default, atomic, audited) with a dashboard Repos tab + Merge surface. Free for all tiers. Also clears freshly-published CVEs (starlette/python-multipart/cryptography + site vite/astro). Design Codex-CLEAN + Sentinel-approved; implementation Codex code-review CLEAN + Shield-SR approved.

**v0.10.30 — Public Beta.** 2179 backend tests + 369 dashboard tests passing. 48 database migrations. 62 MCP tools. Complete dashboard visual redesign — dark-first OKLCH design-token foundation, a `ui/` primitive library (incl. a custom anchored keyboard-accessible Select/Dropdown), a grouped left sidebar, Sessions/Projects list-grid toggles, and a fully rebuilt captured-session conversation surface (tool-aware rendering instead of raw JSON, a real transcript with speaker hierarchy and themed markdown). Brand tagline → "Memory Layer For AI Agents" (dashboard + site). Frontend/site only — no backend, schema, MCP-tool, or auth changes. Codex-reviewed to VERIFIED-CLEAN across consolidated rounds; Shield-SR CLEAN (0 CRITICAL/HIGH).

**v0.10.29 — Public Beta.** 2179 backend tests + 198 dashboard tests passing. 48 database migrations. 62 MCP tools. Named auth profiles for multi-account on one device: `sfs auth login --profile` / `use` / `profiles` / `whoami`, one shared resolver behind every command (coordination + sync + daemon), precedence `SESSIONFS_API_KEY` > `SESSIONFS_PROFILE` > active profile > default, per-profile store isolation, daemon profile pinning, atomic 0600 key-file writes. Plus the cloud-only dashboard 428 fix: CORS now exposes `ETag` so the cross-origin dashboard can replay `If-Match` on rules saves. Codex-reviewed to VERIFIED-CLEAN; Shield-SR CLEAN (0 CRITICAL/HIGH/MEDIUM).

**v0.10.28 — Public Beta.** 2153 backend tests + 198 dashboard tests passing. 48 database migrations. 62 MCP tools. Bundles four R2-CLEAN tickets: `update_ticket` MCP/API/CLI verb (migration 048 `ticket_edits` audit table; per-field diff + system diff comment + lease_epoch fence + creator/admin authz), admin `POST /admin/users/{user_id}/api-keys` mint-on-behalf for lost-key recovery, org invite UPSERT over stale rows (declined/expired/orphan-accepted) with `SELECT FOR UPDATE`, and `/compile` single-pass `auto_generate_concepts` (halves LLM latency on real-compile). Shield-SR CLEAN (0 CRITICAL/HIGH/MEDIUM; PyJWT 2.12→2.13 for 4 CVEs).

**v0.10.27 — Public Beta.** 2119 backend tests + 198 dashboard tests passing. 47 database migrations. 61 MCP tools. Daemon liveness hotfix (`is_excluded` guard inside `_sync_sessions()`) + tier-aware per-file cap discovery (server exposes `max_member_bytes` in `GET /sync/settings`; CLI reads it with precedence env-var > server > 50 MB literal fallback). All three tickets reviewed clean by Codex (R2 VERIFIED-CLEAN) and Shield-SR pre-release security (0 CRITICAL/HIGH/MEDIUM). Additive only — no schema changes, no breaking changes; older CLIs ignore the new field harmlessly.

**v0.10.26 — Public Beta.** (v0.10.25's `click<8.4` pin didn't actually fix the CI test failures — typer 0.26 vendors its own `click`, so `BadParameter` no longer inherits from `click.exceptions.ClickException`. v0.10.26 catches both class hierarchies in `handle_errors` so Deploy API ships.) Underlying v0.10.24/0.10.25 feature set unchanged.

**v0.10.25 — Public Beta.** 2108 backend tests + 198 dashboard tests passing. 47 database migrations. 61 MCP tools. v0.10.25 is a one-line hotfix pinning `click<8.4` so the v0.10.24 `Deploy API` workflow ships — click 8.4 changed `BadParameter` exit code + class hierarchy, breaking 2 CLI tests that pass on local `.venv` but fail in fresh CI installs. Underlying feature set is identical to v0.10.24: Issue/Task rollup (migration 047 adds `kind` + `parent_ticket_id` to tickets; full Atlas + Prism dashboard surface), two enterprise blockers closed from GH #51 (`sfs org create` 500 + meaningful error envelope), GH #50 MCP persona update/delete parity, click-to-edit session title + alias inline, CORS preflight `If-Match` allow, Rules-page max-tokens UX, and a new `user_is_project_admin` actor-side authz helper. All Codex-reviewed clean.

### Session capture, resume, and search

- **Eight-tool capture** — Claude Code, Codex, Gemini, Cursor, Copilot CLI, Amp, Cline, Roo Code
- **Cross-tool resume** — start in Claude Code, resume in Codex (and vice-versa with Gemini / Copilot); auto-launches the native tool, full transcript via `--append-system-prompt-file` with 50-message trim, `sfs resume --model` to override the model
- **Narrative session summaries** — LLM-powered `what_happened`, `key_decisions`, `outcome`, `open_issues`
- **Full-text search** across all sessions (CLI, dashboard, API), tier-aware
- **Session browsing** — inspect, export, fork, checkpoint, compare; multi-select bulk delete + Find Duplicates in dashboard
- **Message pagination** — newest-first default, order toggle, sidechain/empty filtering

### Project knowledge

- **Shared project context** — one document per repo, shared across the team, readable via MCP, manageable from dashboard
- **Living Project Context** — auto-summarize on sync, knowledge entries (6 types), wiki pages with backlinks, structured compilation, concept auto-generation
- **Rules portability** — canonical project rules compiled into `CLAUDE.md`, `codex.md`, `.cursorrules`, `.github/copilot-instructions.md`, `GEMINI.md`; managed-file safety, deterministic output, optimistic-concurrency API; sessions persist `rules_version` + `rules_hash` + `instruction_artifacts` for full instruction provenance
- **Knowledge base lifecycle** — entry decay after 90 days unreferenced, auto-dismiss past retention, context-document budget, section page caps, concept auto-refresh, quality gates on contributions
- **LLM Judge** — confidence scores (0-100), CWE mapping, evidence linking, dismiss/confirm workflow

### Team and collaboration

- **Team handoff** — email notification, status stepper, session context card, smart workspace resolution
- **GitHub PR App** (signature enforcement) + **GitLab MR integration** (per-user webhook secrets, comment dedup) — auto-comment AI session context on pull requests and merge requests
- **RBAC** with admin and member roles; seat enforcement on invite accept

### Cloud, sync, and reliability

- **Cloud sync** with push/pull, email verification, ETag conflict detection, bounded per-user concurrency (5 client-side, 3 server-side), 429 + Retry-After backoff
- **Connection pool optimization** — configurable via `SFS_DATABASE_POOL_SIZE`/`MAX_OVERFLOW`/`POOL_TIMEOUT`/`POOL_RECYCLE`; sync_push splits into 3 phases so DB connections are held ~70ms (not ~5s)
- **Sync atomicity** — commit-then-promote blob invariant; temp blob preserved until second commit succeeds
- **Self-healing SQLite index** with auto-rebuild from `.sfs` files
- **`sfs doctor`** with 8 health checks and auto-repair
- **`handle_errors` decorator** on all CLI commands (no raw tracebacks)
- **Multi-provider email** (Resend, SMTP, or disabled for air-gapped)

### Agent personas and tickets (v0.10.1)

- **Agent personas** — portable AI roles per project (atlas, prism, scribe, ...), shared by humans and AI agents; CRUD via `sfs persona` (CLI) or 5 MCP tools (`list_personas`, `get_persona`, `create_persona`, `assume_persona`, `forget_persona`); ASCII name regex (1–50 chars), soft-delete preserves history. **Pro+** tier-gated. Dashboard management UI shipped in v0.10.3.
- **Agent tickets** — full ticket FSM (`suggested → open → in_progress → blocked → review → done`) with acceptance criteria, context refs, file refs, dependencies, comments; CRUD + lifecycle via `sfs ticket` (15 commands) or 9 MCP tools (`list_tickets`, `get_ticket`, `create_ticket`, `start_ticket`, `complete_ticket`, `resolve_ticket`, `assign_persona`, `escalate_ticket`, `add_ticket_comment`); atomic state transitions with rowcount-1 guard; agent-created tickets require ≥1 acceptance criterion + ≥20-char description (max 3/session); persona-delete refuses when non-terminal tickets reference it (`--force` bypass). **Team+** tier-gated. Dashboard UI shipped in v0.10.3.
- **Compiled persona+ticket context** — `start_ticket` returns markdown context (persona + ticket + criteria + file refs + active KB claims + completed-dep notes + recent comments) sized to the target tool's token budget (`?tool=claude-code|cursor|codex|gemini|copilot|generic`); same project_id + active-state filters applied across KB claims and dep notes.
- **Local provenance bundle** — `~/.sessionfs/active_ticket.json` written by `start_ticket` (CLI + MCP) and consumed by all 7 watchers (claude_code/codex/copilot/cursor/gemini/amp/cline); the bundle's `ticket_id` + `persona_name` flow into the manifest at capture time and through sync into the `sessions` table (migration 037).
- **Bundle ownership safety** — `complete_ticket` only unlinks the bundle when both `ticket_id` AND `project_id` match the completing ticket; bundles written by another tool/session are preserved.

### MCP and dashboard

- **MCP server** (local + remote) with 36 tools — sessions (search, context, recent, related, summary, audit, provenance), knowledge read (project context, context section, wiki page, search, list entries, get entry, health, ask), knowledge write (add_knowledge, update_wiki_page, list_wiki_pages, compile, dismiss_knowledge_entry), rules (get_rules, get_compiled_rules), **personas** (list_personas, get_persona, create_persona, assume_persona, forget_persona), and **tickets** (list_tickets, get_ticket, start_ticket, create_ticket, complete_ticket, resolve_ticket, assign_persona, escalate_ticket, add_ticket_comment)
- **`sfs mcp install --for <tool>`** for all 9 tools (stale registration repair, malformed config handling)
- **Web dashboard** with light/dark mode, resume-first layout, date-grouped sessions, lineage grouping, command palette (Cmd+K), mobile nav, accessibility (focus trapping, ARIA live regions), product identity
- **`/help` page** — MCP-first guidance, 9-tool installer with live terminal + copy button, agent prompt examples, curated CLI quick-reference

### Security, compliance, and billing

- **DLP / Secret Scrubbing** — 14 PHI patterns + 19 secret patterns, BLOCK/REDACT/WARN modes, server-side scan of all archive files, `sfs dlp scan/policy`, dashboard settings tab
- **`sfs init` wizard** with auto-detection of 9 tools and optional sync setup
- **`sfs security scan/fix`** for config permissions, API key exposure, dependency audit
- **Security pipeline** — pip-audit, Trivy (rendered Helm chart), Bandit, Dependabot, SECURITY.md; CRITICAL/HIGH blocks the pipeline
- **FSL licensing** with open-source core and enterprise extensions
- **Self-hosted license lifecycle** with grace periods, admin CLI, dashboard licenses tab
- **Server-side tier gating** — 6 tiers (free, starter, pro, team, business, enterprise), 30+ gated features
- **Hosted billing** with org-isolated checkout and subscription management
- **Organization management** (`sfs org` commands)

### Self-hosted deployment

- **Helm chart** (EKS / GKE / AKS tested) with license validation, single-ingress via nginx, seed job
- **Hardened security posture** — all containers run as non-root (UID 10001) with `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, all capabilities dropped, `seccompProfile: RuntimeDefault`; PostgreSQL container mounts `emptyDir` at `/tmp` and `/var/run/postgresql` so it works with read-only root; `helm test` hook pod matches the posture
- See [self-hosted docs](docs/self-hosted.md) for the full Security Posture section

### On the roadmap

- Session similarity (related-sessions ranking)
- VS Code extension
- Cost analytics dashboard

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, and PR guidelines.

## License

Apache 2.0 — Core. FSL (Functional Source License) — Enterprise extensions in `ee/`.
