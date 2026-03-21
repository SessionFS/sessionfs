# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

SessionFS — Dropbox for AI agent sessions. Captures, syncs, and hands off conversations across tools and teammates.

## Architecture

- **Daemon (sfsd):** Background process using fsevents/inotify (not polling) to watch native AI tool session storage (Claude Code, Codex, Cursor) and capture sessions into canonical `.sfs` format
- **CLI (sfs):** Command-line tool for browsing, pulling, resuming, forking, and handing off sessions
- **API Server:** FastAPI + PostgreSQL + S3/GCS for cloud session storage and team features

### Session Format (.sfs)

A `.sfs` session is a directory containing: `manifest.json`, `messages.jsonl`, `workspace.json`, `tools.json`. All file paths within are relative to workspace root. Sessions are append-only — conflict resolution appends both sides rather than merging.

## Key Decisions

- NO WebSockets, NO Redis, NO real-time sync. HTTP + ETags only.
- NO server-side LLM API keys. All LLM calls are client-side.
- Daemon defaults to local-only. Cloud sync is explicit opt-in.
- All file paths in .sfs format are relative to workspace root.
- Sessions are append-only. Never modify messages in place.
