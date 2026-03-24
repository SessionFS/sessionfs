# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
