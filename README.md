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
| Cursor IDE | Yes | Capture-only |

## Quick Install

```bash
pip install sessionfs
```

Requires Python 3.10+. Installs two commands: `sfs` (CLI) and `sfsd` (daemon).

## Quick Start

```bash
# Start the daemon — it watches your tools automatically
sfs daemon start

# Use Claude Code, Codex, Gemini, or Cursor normally
# Sessions are captured in the background

# List captured sessions across all tools
sfs list

# Resume a Claude Code session in Codex
sfs resume ses_abc123 --in codex

# Or hand it off to a teammate
sfs push ses_abc123
```

See the full [Quickstart Guide](docs/quickstart.md) for detailed steps.

## How It Works

The `sfsd` daemon uses filesystem events (fsevents on macOS, inotify on Linux) to watch native AI tool session storage. When it detects new or updated sessions, it converts them into the `.sfs` format — a portable directory containing `manifest.json`, `messages.jsonl`, `workspace.json`, and `tools.json`.

Each tool has its own watcher:
- **Claude Code** — watches `~/.claude/projects/` JSONL files
- **Codex CLI** — watches `~/.codex/sessions/` rollout files, reads SQLite index
- **Gemini CLI** — watches `~/.gemini/tmp/*/chats/` JSON sessions
- **Cursor IDE** — reads `state.vscdb` SQLite database (capture-only, no write-back)

Sessions are indexed locally for fast browsing via the CLI. Cloud sync is opt-in; the daemon defaults to local-only.

## Commands

| Command | Description |
|---------|-------------|
| `sfs list` | List captured sessions with filtering and sorting |
| `sfs show <id>` | Show session details, messages, and cost estimates |
| `sfs resume <id> [--in TOOL]` | Resume a session in any supported tool |
| `sfs fork <id>` | Fork a session into a new independent session |
| `sfs checkpoint <id>` | Create a named checkpoint of a session |
| `sfs export <id>` | Export as `.sfs`, markdown, or Claude Code format |
| `sfs import` | Import sessions from any supported tool |
| `sfs push <id>` | Push a session to the cloud |
| `sfs pull <id>` | Pull a session from the cloud |
| `sfs daemon start\|stop\|status\|logs` | Manage the background daemon |
| `sfs config show\|set` | Manage configuration |
| `sfs admin reindex` | Re-extract metadata for all cloud sessions |

See the full [CLI Reference](docs/cli-reference.md) for options and examples.

## Cross-Tool Resume

```bash
# Start in Claude Code, resume in Codex
sfs resume ses_abc123 --in codex

# Start in Gemini, resume in Claude Code
sfs resume ses_def456 --in claude-code

# Cursor sessions can be resumed in any other tool
sfs resume ses_ghi789 --in gemini
```

SessionFS converts between native formats automatically — message roles, tool calls, thinking blocks, and workspace state are mapped across tools.

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

See the [Sync Guide](docs/sync-guide.md) for setup, conflict handling, and self-hosted options.

## Self-Hosted Server

```bash
docker compose up -d
```

Starts the SessionFS API server, PostgreSQL, and web dashboard. See the [Sync Guide](docs/sync-guide.md#self-hosted) for full configuration.

## Web Dashboard

A browser-based interface for browsing and managing synced sessions. Accessible at `http://localhost:8000` when running the self-hosted server.

## Session Format

Sessions are stored as `.sfs` directories:
- `manifest.json` — identity, provenance, model info, stats
- `messages.jsonl` — conversation history with content blocks
- `workspace.json` — git state, files, environment
- `tools.json` — tool definitions and shell context

All file paths are relative to workspace root. Sessions are append-only — conflict resolution appends both sides rather than merging.

## Status

**v0.1.0 — Public Beta.** 429 tests passing.

What works today:
- Four-tool session capture (Claude Code, Codex, Gemini, Cursor)
- Cross-tool resume between Claude Code, Codex, and Gemini
- Browse, inspect, export, fork, and checkpoint sessions
- Cloud sync with push/pull and ETag conflict detection
- Self-hosted API server with auth, PostgreSQL, and S3/local storage
- Web dashboard for session management
- 12 security controls including secret detection, path traversal protection, and audit logging

On the roadmap:
- Team handoff workflows with notifications
- VS Code extension
- Additional tool watchers

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, and PR guidelines.

## License

Apache 2.0
