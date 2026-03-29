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

**Phase 3 complete — Team Intelligence + Trust Verification**

848 tests passing. Eight-tool capture + four-tool resume (auto-launch). Session summarization (files, tests, commands, packages — deterministic). Autosync (off/all/selective modes, debounce, watchlist). Local storage management (pruning, retention, disk warnings). Multi-provider email (Resend, SMTP, none). Team handoff with email + session copy on claim. LLM Judge V2 (severity classification, category detection, audit history DB, auto-audit trigger, custom base URL, model discovery). Shared project context (CLI + API + MCP tool). MCP server (local + remote, 5 tools). Full-text search. GitHub PR App + GitLab MR integration. Admin dashboard. Helm chart (EKS validated, single-ingress via nginx). Cursor tool call extraction from agentKv layer. Tier-based sync limits (50MB free, 300MB paid).
