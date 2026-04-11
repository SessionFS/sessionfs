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

## Quick Start

```bash
# 1. Install
pip install sessionfs

# 2. Start the daemon — it watches all 8 tools automatically
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
| `sfs doctor` | Run 8 health checks with auto-repair |
| `sfs storage` | Show local disk usage and retention policy |
| `sfs storage prune` | Prune old sessions to free disk space |
| `sfs daemon start\|stop\|restart\|status\|logs` | Manage the background daemon |
| `sfs daemon rebuild-index` | Rebuild local session index from .sfs files on disk |
| `sfs watcher list\|enable\|disable` | Manage tool watchers |
| `sfs auth login\|signup\|status` | Manage cloud authentication |
| `sfs config show\|set` | Manage configuration |
| `sfs mcp serve` | Start MCP server (12 tools) for AI tool integration |
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

**v0.9.8.4 — Public Beta.** 1052 backend tests + 22 dashboard tests passing.

What works today:
- Eight-tool session capture (Claude Code, Codex, Gemini, Cursor, Copilot CLI, Amp, Cline, Roo Code)
- Cross-tool resume between Claude Code, Codex, Gemini, and Copilot CLI (auto-launches native tool, full transcript via --append-system-prompt-file with 50-message trim)
- Shared project context — one document per repo, shared across the team, readable via MCP, manageable from dashboard
- Living Project Context — auto-summarize on sync, knowledge entries (6 types), wiki pages, structured compilation
- Local storage management with configurable retention, pruning, and disk warnings
- Full-text search across all sessions (CLI + dashboard + API)
- Living Project Context — auto-summarize on sync, knowledge entries, wiki pages, structured compilation
- MCP server with 12 tools — search, context, recent, related, project context, summary, audit report, add_knowledge, update_wiki_page, list_wiki_pages, search_project_knowledge, ask_project
- MCP install for all 8 tools (codex mcp add, gemini mcp add)
- Self-healing SQLite index with auto-rebuild
- `sfs doctor` with 8 health checks and auto-repair
- LLM-as-a-Judge with confidence scores (0-100), CWE mapping, evidence linking, dismiss/confirm findings
- Narrative session summaries — LLM-powered what_happened, key_decisions, outcome, open_issues
- GitHub PR App + GitLab MR integration — auto-comment AI session context on pull requests and merge requests
- Team handoff with email notification, status stepper, session context card, and smart workspace resolution
- Multi-provider email (Resend, SMTP, or disabled for air-gapped)
- Browse, inspect, export, fork, and checkpoint sessions
- Cloud sync with push/pull, email verification, and ETag conflict detection
- FSL licensing with open-source core and enterprise extensions
- Self-hosted license lifecycle with grace periods, admin CLI, and dashboard licenses tab
- Server-side tier gating (5 tiers, 30+ gated features)
- RBAC with admin and member roles
- Stripe billing integration with subscription management
- Organization management (`sfs org` commands)
- Self-hosted deployment via Helm chart with license validation (EKS/GKE/AKS tested)
- Web dashboard with light/dark mode, resume-first layout, date-grouped sessions, lineage grouping, skeleton loading
- `sfs init` wizard with auto-detection of 8 tools and optional sync setup
- `sfs security scan/fix` for config permissions, API key exposure, dependency audit
- Security pipeline (GitHub Action with pip-audit, Trivy, Bandit), Dependabot, SECURITY.md
- Multi-select bulk delete + Find Duplicates in dashboard
- Skill/slash command detection across all converters
- Self-healing SQLite index with auto-rebuild from .sfs files
- `sfs doctor` with 8 health checks and auto-repair
- `handle_errors` decorator on all CLI commands (no raw tracebacks)
- Message pagination with newest-first default, order toggle, sidechain/empty filtering
- Database migrations 001–019

On the roadmap:
- Session similarity and duplicate detection
- Cost analytics dashboard
- VS Code extension

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, and PR guidelines.

## License

Apache 2.0 — Core. FSL (Functional Source License) — Enterprise extensions in `ee/`.
