# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
