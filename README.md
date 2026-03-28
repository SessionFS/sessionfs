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
| `sfs export <id>` | Export as `.sfs`, markdown, or Claude Code format |
| `sfs import` | Import sessions from any supported tool |
| `sfs push <id>` | Push a session to the cloud |
| `sfs pull <id>` | Pull a session from the cloud |
| `sfs handoff <id> --to EMAIL` | Hand off a session to a teammate with email notification |
| `sfs search "query"` | Full-text search across all sessions |
| `sfs storage` | Show local disk usage and retention policy |
| `sfs storage prune` | Prune old sessions to free disk space |
| `sfs daemon start\|stop\|restart\|status\|logs` | Manage the background daemon |
| `sfs watcher list\|enable\|disable` | Manage tool watchers |
| `sfs audit <id>` | Audit a session for hallucinations with LLM-as-a-Judge |
| `sfs project init\|edit\|show` | Manage shared project context for your team |
| `sfs config show\|set` | Manage configuration |
| `sfs mcp serve` | Start MCP server for AI tool integration |
| `sfs mcp install --for TOOL` | Auto-configure MCP for Claude Code, Cursor, or Copilot |
| `sfs admin reindex` | Re-extract metadata for all cloud sessions |

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

**v0.9.1 — Public Beta.** 835 tests passing.

What works today:
- Eight-tool session capture (Claude Code, Codex, Gemini, Cursor, Copilot CLI, Amp, Cline, Roo Code)
- Cross-tool resume between Claude Code, Codex, Gemini, and Copilot CLI (auto-launches native tool)
- Shared project context — one document per repo, shared across the team, readable via MCP
- Local storage management with configurable retention, pruning, and disk warnings
- Full-text search across all sessions (CLI + dashboard + API)
- MCP server with 5 tools — search, context, recent, related, and project context
- LLM-as-a-Judge — audit sessions for hallucinations (BYOK, multi-provider, OpenRouter, custom base URL for LiteLLM/vLLM/Ollama)
- GitHub PR App — auto-comment AI session context on pull requests
- Team handoff with email notification and smart workspace resolution
- Multi-provider email (Resend, SMTP, or disabled for air-gapped)
- Browse, inspect, export, fork, and checkpoint sessions
- Cloud sync with push/pull, email verification, and ETag conflict detection
- Self-hosted deployment via Helm chart (EKS/GKE/AKS tested)
- Web dashboard with session management, search, handoffs, and audit

On the roadmap:
- Stripe billing integration
- Session similarity and duplicate detection
- Cost analytics dashboard
- VS Code extension

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, and PR guidelines.

## License

Apache 2.0
