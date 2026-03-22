# CLI Reference

Complete reference for all `sfs` commands.

## Global Options

```
sfs [OPTIONS] COMMAND [ARGS]...
```

| Option | Description |
|--------|-------------|
| `--help` | Show help and exit |

---

## `sfs list`

List captured sessions.

```
sfs list [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--tool` | string | — | Filter by source tool (e.g., `claude-code`) |
| `--since` | string | — | Show sessions since time (`7d`, `24h`, or ISO date) |
| `--tag` | string | — | Filter by tag |
| `--sort` | string | `recent` | Sort order: `recent`, `oldest`, `messages`, `tokens` |
| `--json` | flag | `false` | Output as JSON |
| `--quiet`, `-q` | flag | `false` | Only print session IDs |

**Example:**

```bash
$ sfs list --since 7d --sort tokens

                       Sessions (5)
┌──────────────┬─────────────┬────────┬──────────┬───────────┐
│ ID           │ Tool        │ Model  │ Messages │ Title     │
├──────────────┼─────────────┼────────┼──────────┼───────────┤
│ a1b2c3d4e5f6 │ claude-code │ opus-4 │       23 │ Debug ... │
└──────────────┴─────────────┴────────┴──────────┴───────────┘
```

---

## `sfs show`

Show session details.

```
sfs show SESSION_ID [OPTIONS]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `SESSION_ID` | yes | Session ID or prefix (min 4 chars) |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--messages`, `-m` | flag | `false` | Show conversation messages |
| `--cost`, `-c` | flag | `false` | Show cost estimate |
| `--page-size` | int | `20` | Messages per page (with `--messages`) |

**Example:**

```bash
$ sfs show a1b2 --cost

╭──────────── Session Details ────────────╮
│ Session ID: a1b2c3d4-e5f6-...          │
│ Title: Debug auth flow                  │
│ Tool: claude-code 1.0.23               │
│ Model: claude-opus-4 (anthropic)       │
│ Messages: 23                            │
│ Input tokens: 34,200                    │
│ Output tokens: 12,800                   │
╰─────────────────────────────────────────╯
╭──────────── Cost Estimate ──────────────╮
│ Input cost: $0.5130                     │
│ Output cost: $0.9600                    │
│ Total: $1.4730                          │
╰─────────────────────────────────────────╯
```

---

## `sfs resume`

Resume a captured session in any supported AI tool.

```
sfs resume SESSION_ID [OPTIONS]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `SESSION_ID` | yes | Session ID or prefix |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--project` | path | — | Target project path (overrides workspace) |
| `--in` | string | `claude-code` | Target tool: `claude-code`, `codex`, `gemini`, or `cursor` |

Converts the session to the target tool's native format and injects it into that tool's session storage. Cursor is capture-only — use `--in` with another tool to resume Cursor sessions.

**Example:**

```bash
$ sfs resume a1b2 --project /Users/me/myproject

Session resumed successfully.
  CC Session ID: abc123-def456
  JSONL: /Users/me/.claude/projects/.../abc123-def456.jsonl
  Messages: 23

Open Claude Code in /Users/me/myproject to continue.
```

---

## `sfs checkpoint`

Create a named checkpoint of a session's current state.

```
sfs checkpoint SESSION_ID --name NAME
```

| Argument | Required | Description |
|----------|----------|-------------|
| `SESSION_ID` | yes | Session ID or prefix |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--name` | string | required | Checkpoint name |

**Example:**

```bash
$ sfs checkpoint a1b2 --name "before-refactor"

Checkpoint 'before-refactor' created for session a1b2c3d4e5f6.
```

---

## `sfs fork`

Fork a session into a new independent session.

```
sfs fork SESSION_ID --name NAME [OPTIONS]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `SESSION_ID` | yes | Session ID or prefix |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--name` | string | required | Title for the forked session |
| `--from-checkpoint` | string | — | Fork from a named checkpoint instead of current state |

**Example:**

```bash
$ sfs fork a1b2 --name "Try different approach"

Forked session created: f6e5d4c3b2a1
  Title: Try different approach
  Parent: a1b2c3d4e5f6
```

---

## `sfs export`

Export a session to a file.

```
sfs export SESSION_ID [OPTIONS]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `SESSION_ID` | yes | Session ID or prefix |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--format` | string | `sfs` | Export format: `sfs`, `markdown`, `claude-code` |
| `--output`, `-o` | path | `.` | Output directory |

**Example:**

```bash
$ sfs export a1b2 --format markdown -o ~/exports

Exported to /Users/me/exports/a1b2c3d4-e5f6-....md
```

---

## `sfs import`

Import sessions from external sources.

```
sfs import [FILE] [OPTIONS]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `FILE` | no | File to import (for file-based import) |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--from` | string | — | Import source: `claude-code` |
| `--format` | string | — | Input format (for file import) |

**Example:**

```bash
# Import all Claude Code sessions
$ sfs import --from claude-code

Found 47 Claude Code session(s).
Imported 47 new session(s).
```

---

## `sfs daemon start`

Start the SessionFS daemon in the background.

```
sfs daemon start [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--config` | path | — | Path to `config.toml` |
| `--log-level` | string | `INFO` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` |

**Example:**

```bash
$ sfs daemon start

Daemon started (PID 12345).
Logs: /Users/me/.sessionfs/daemon.log
```

---

## `sfs daemon stop`

Stop the running daemon.

```
sfs daemon stop
```

**Example:**

```bash
$ sfs daemon stop

Sent SIGTERM to daemon (PID 12345).
```

---

## `sfs daemon status`

Show daemon status and watcher health.

```
sfs daemon status
```

**Example:**

```bash
$ sfs daemon status

         SessionFS Daemon Status
┌──────────────────┬────────────────────────┐
│ Field            │ Value                  │
├──────────────────┼────────────────────────┤
│ PID              │ 12345                  │
│ Running          │ Yes                    │
│ Sessions         │ 47                     │
│ Watcher: cc      │ healthy (47 sessions)  │
└──────────────────┴────────────────────────┘
```

---

## `sfs daemon logs`

Show daemon log output.

```
sfs daemon logs [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--lines`, `-n` | int | `50` | Number of lines to show |
| `--follow`, `-f` | flag | `false` | Follow log output (like `tail -f`) |

**Example:**

```bash
$ sfs daemon logs -n 10

2026-03-20 14:30:00 sfsd INFO sfsd starting with 1 watcher(s)
2026-03-20 14:30:01 sfsd INFO sfsd running (PID 12345)
```

---

## `sfs config show`

Show the current configuration.

```
sfs config show
```

**Example:**

```bash
$ sfs config show

Config: /Users/me/.sessionfs/config.toml

log_level = "INFO"
scan_interval_s = 5.0

[claude_code]
enabled = true
```

---

## `sfs config set`

Set a configuration value.

```
sfs config set KEY VALUE
```

| Argument | Required | Description |
|----------|----------|-------------|
| `KEY` | yes | Config key (dotted path, e.g., `claude_code.enabled`) |
| `VALUE` | yes | Value to set |

**Example:**

```bash
$ sfs config set scan_interval_s 10

Set scan_interval_s = 10
```
