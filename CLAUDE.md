# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

SessionFS — Dropbox for AI agent sessions. Captures, syncs, and hands off conversations across tools and teammates.

## Agent Team

Project-specific agent personas are in `.agents/`. Load the relevant agent for your task before starting work. See `.agents/README.md` for the full assignment matrix.

| Agent | File | Domain |
|-------|------|--------|
| Atlas | `.agents/atlas-backend.md` | Backend, daemon, API, CLI |
| Sentinel | `.agents/sentinel-security.md` | Security, auth, audit |
| Forge | `.agents/forge-devops.md` | CI/CD, Docker, distribution |
| Prism | `.agents/prism-frontend.md` | Web dashboard, VS Code extension |
| Scribe | `.agents/scribe-docs.md` | Documentation |

## Architecture

- **Daemon (sfsd):** Background process using fsevents/inotify (not polling) to watch native AI tool session storage (Claude Code, Codex, Cursor) and capture sessions into canonical `.sfs` format
- **CLI (sfs):** Command-line tool for browsing, pulling, resuming, forking, and handing off sessions
- **API Server:** FastAPI + PostgreSQL + S3/GCS for cloud session storage and team features
- **Web Dashboard:** React management interface (NOT a chat UI — users interact with their native AI tools)

### Session Format (.sfs)

A `.sfs` session is a directory containing: `manifest.json`, `messages.jsonl`, `workspace.json`, `tools.json`. All file paths within are relative to workspace root. Sessions are append-only — conflict resolution appends both sides rather than merging.

## Git Branch Policy

- **Default push target: `v0.1.0-release` (private).** All commits and pushes go here unless explicitly told otherwise. This branch contains internal files (.agents/, src/spikes/, docs/security/, DOGFOOD.md).
- **Public releases: `v0.1.0-public` only.** When the user says "go public" or requests a public release, cherry-pick or merge the relevant changes into the public branch. Strip all internal files before pushing — the .gitignore on that branch already excludes them.
- **Never push internal files to the public branch.** Agent personas, research spikes, threat models, dogfood logs, and business strategy must stay on the release branch only.

## Key Decisions (Do Not Violate)

- NO WebSockets, NO Redis, NO real-time sync. HTTP + ETags only.
- NO server-side LLM API keys. All LLM calls are client-side.
- Daemon defaults to local-only. Cloud sync is explicit opt-in.
- All file paths in .sfs format are relative to workspace root.
- Sessions are append-only. Never modify messages in place.
- Team handoff is the core monetization wedge. Individual tier is free forever.

## Current Phase

**Phase 1: Complete — Daemon + CLI + Server MVP**

327 tests passing. Dog-fooding complete. Ready for v0.1.0 public release.
