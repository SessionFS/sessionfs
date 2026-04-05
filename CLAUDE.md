# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

SessionFS — Portable session layer for AI coding tools.

## Agent Team

Project-specific agent personas are in `.agents/`. Load the relevant agent for your task before starting work. See `.agents/README.md` for the full assignment matrix.

| Agent | File | Domain |
|-------|------|--------|
| Atlas | `.agents/atlas-backend.md` | Backend, daemon, API, CLI |
| Sentinel | `.agents/sentinel-security.md` | Security, auth, audit |
| Forge | `.agents/forge-devops.md` | CI/CD, Docker, distribution |
| Prism | `.agents/prism-frontend.md` | Web dashboard, VS Code extension |
| Scribe | `.agents/scribe-docs.md` | Documentation |
| Ledger | `.agents/ledger-revenue.md` | Revenue, billing, Stripe, pricing |
| Shield | `.agents/shield-compliance.md` | Compliance, data governance, certifications |
| Shield-SR | `.agents/shield-security-review.md` | Pre-release security review (MANDATORY before every release) |
| Scribe-Site | `.agents/scribe-site-sync.md` | Pre-release site sync (MANDATORY — no stale site content) |
| Vault | `.agents/vault-licensing.md` | Licensing, open source, IP protection |

## Architecture

- **Daemon (sfsd):** Background process using fsevents/inotify (not polling) to watch native AI tool session storage (Claude Code, Codex, Gemini CLI, Cursor) and capture sessions into canonical `.sfs` format
- **CLI (sfs):** Command-line tool for browsing, pulling, resuming, forking, and handing off sessions
- **API Server:** FastAPI + PostgreSQL + S3/GCS for cloud session storage and team features
- **Web Dashboard:** React management interface (NOT a chat UI — users interact with their native AI tools)

### Session Format (.sfs)

A `.sfs` session is a directory containing: `manifest.json`, `messages.jsonl`, `workspace.json`, `tools.json`. All file paths within are relative to workspace root. Sessions are append-only — conflict resolution appends both sides rather than merging.

## Commit Rules

- **All commits must use author `sessionfsbot <bot@sessionfs.dev>`.** Use `--author="sessionfsbot <bot@sessionfs.dev>"` on every git commit.
- **NEVER include "Co-Authored-By" lines referencing Claude, Anthropic, or any AI assistant.**
- **NEVER mention Claude Code, Claude, AI, LLM, or any AI tooling in commit messages.**
- Commit messages should read as if written by a human developer. Focus on what changed and why.

## Git Branch Policy

- **`develop` is LOCAL ONLY.** NEVER push develop to origin. The public repo must only have `main`. Develop contains internal files (.agents/, src/spikes/, docs/security/, DOGFOOD.md, brand/, .release/, CLAUDE.md) that must never be public.
- **All work happens on `develop` locally.** Commits go here. Tests run here.
- **Public releases: merge to `main`, sanitize, push.** Use `.release/private-files.txt` to strip internal files before pushing main. The `/release` skill handles this.
- **NEVER run `git push origin develop`.** This is a security breach — it exposes internal strategy, agent personas, threat models, and business docs.
- **If develop is accidentally pushed:** delete immediately with `git push origin --delete develop`.

## Multi-LLM Review

Before major releases, use a second LLM (e.g., Gemini CLI) to review specific areas. This catches blind spots and provides architectural critique.

```bash
export GOOGLE_GEMINI_BASE_URL="http://100.96.105.123:4000"
export GEMINI_API_KEY=sk-training-2025
gemini --model gemini-3-flash-preview --yolo -p "Review [area] in the codebase and suggest improvements..."
```

**When to use:** UI/UX review, architecture critique, missed edge cases, security audit of new features.
**Not for:** Writing code. Use it for reviewing what was built, not generating implementations.
**Models available:** gemini-3-flash-preview, gpt-5, claude-opus-4.1, deepseek-r1 (via proxy at 100.96.105.123:4000)

## Release Process

- **ALWAYS use the `/release` skill** (at `.claude/commands/release.md`) for every release. No ad-hoc releases.
- The skill covers: version bump, changelog, docs update, ruff lint, tests, deploy landing + dashboard, merge to main with sanitization, tag, verify pipelines, update memories.
- **Run `ruff check src/` before committing.** Lint failures break CI and are embarrassing.
- **Run tests before every release.** No exceptions.

## Key Decisions (Do Not Violate)

- NO WebSockets, NO Redis, NO real-time sync. HTTP + ETags only.
- NO server-side LLM API keys. All LLM calls are client-side.
- Daemon defaults to local-only. Cloud sync is explicit opt-in.
- All file paths in .sfs format are relative to workspace root.
- Sessions are append-only. Never modify messages in place.
- Team handoff is the core monetization wedge. Individual tier is free forever.
- **All GCP infrastructure must be created via Terraform.** No `gcloud` imperative commands for resource creation. Secrets stored in GCP Secret Manager only — never in code, env files, or CI configs.

## Current Phase

**Phase 6 complete — Living Project Context & Wiki**

993 tests passing. DB migrations: 001–019. Eight-tool capture + four-tool resume (auto-launch, cross-tool transcript via --append-system-prompt-file, 50-message trim). Session summarization (deterministic + narrative LLM summaries). Autosync (off/all/selective modes, debounce, watchlist). Local storage management (pruning, retention, disk warnings). Multi-provider email (Resend, SMTP, none). Team handoff with email + session copy on claim + status stepper + session context card. LLM Judge with confidence scores (0-100), CWE mapping, evidence linking, dismiss/confirm findings. Shared project context (CLI + API + MCP tool + dashboard page with markdown editor). Living Project Context (auto-summarize on sync, knowledge entries with 6 types, structured compilation, section pages, concept auto-generation, regenerate, wiki pages with backlinks, auto-narrative toggle). MCP server (local + remote, 12 tools including add_knowledge, update_wiki_page, list_wiki_pages, search_project_knowledge, ask_project). MCP install for all 8 tools. Full-text search. GitHub PR App + GitLab MR integration. Admin dashboard. Helm chart (EKS validated, single-ingress via nginx, license validation, seed job with retry + cache fallback). Self-hosted license lifecycle (migration 017, grace periods, admin CLI + API, dashboard licenses tab). Cursor tool call extraction from agentKv layer. Tier-based sync limits (50MB free, 300MB paid). FSL licensing (ee/ directory). Server-side tier gating (5 tiers, 30+ features). RBAC (admin/member roles). Stripe billing integration. Organization management. Telemetry endpoint. Client version tracking. Dashboard redesign (light/dark mode, resume-first layout, date-grouped sessions, lineage grouping, left rail nav, page transitions, toast notifications, skeleton loading). `sfs init` wizard (auto-detects 8 tools, optional sync). `sfs security scan/fix` (config permissions, API key exposure, dependency audit). Security pipeline (GitHub Action: pip-audit, Trivy, Bandit + Dependabot + SECURITY.md). Skill/slash command detection across all converters. Tool call capture for Gemini CLI (toolCalls array) and Amp (tool_use/tool_result blocks). Multi-select bulk delete + Find Duplicates in dashboard. Session deduplication (Codex watcher skips sessionfs_import). Search tier check uses effective org tier. Self-healing SQLite index (auto-rebuild from .sfs files). `sfs doctor` (8 health checks with auto-repair). `handle_errors` decorator on all CLI commands. Message pagination (newest-first, order toggle, sidechain/empty filtering).


<!-- sessionfs-knowledge-start -->
## SessionFS Knowledge Base — MANDATORY

**IMPORTANT: You MUST call `add_knowledge()` via MCP when you discover something significant about this codebase.** This is not optional — it is a core requirement of working in this repo, just like following commit conventions.

Every discovery you make helps the next AI agent that works here. If you learn it, log it.

### You MUST contribute when you:
- Make or discover an architecture/design decision → `add_knowledge("description", "decision")`
- Find a code pattern or convention → `add_knowledge("description", "pattern")`
- Discover a bug or workaround → `add_knowledge("description", "bug")`
- Learn about an external dependency → `add_knowledge("description", "dependency")`
- Establish a coding convention → `add_knowledge("description", "convention")`
- Learn anything useful for future sessions → `add_knowledge("description", "discovery")`

### How to contribute
- Quick facts: `add_knowledge("what you learned", "type")` — call this multiple times per session
- Full articles: `update_wiki_page("slug", "markdown content")` — for substantial findings
- Search first: `search_project_knowledge("topic")` — avoid duplicates
- Browse: `list_wiki_pages()` — see what exists

### Examples of when to call add_knowledge:
- "The auth middleware resolves effective tier from the org, not the user record" → decision
- "All converters follow the pattern: parse native format → canonical .sfs → write" → pattern
- "SQLite WAL mode can corrupt on power loss — index.py has self-healing" → bug
- "react-markdown is used for rendering project context, not a custom parser" → dependency

**Do NOT end a session without contributing at least one knowledge entry if you learned something new.**

<!-- sessionfs-knowledge-end -->
