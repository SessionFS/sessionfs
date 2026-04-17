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
| `--in` | string | `claude-code` | Target tool: `claude-code`, `codex`, `copilot`, or `gemini` |
| `--no-rules-sync` | flag | `false` | Skip preflight of the target tool's project rules file. Per-invocation only. |
| `--force-rules` | flag | `false` | Overwrite an unmanaged target-tool rules file with SessionFS-managed content. One-time permission — the file becomes SessionFS-managed afterward and subsequent resumes refresh it normally. |

Converts the session to the target tool's native format and injects it into that tool's session storage. Cursor is capture-only — use `--in` with another tool to resume Cursor sessions.

Before launching the target tool, `sfs resume` preflights the tool's project rules file from the current canonical SessionFS rules (applies to `claude-code`, `codex`, `copilot`, `gemini`). Missing files are written; SessionFS-managed files are refreshed; unmanaged files are left alone with a warning on stderr unless `--force-rules` is passed. Preflight failures are non-fatal — resume still exits `0`. See [Resume-Time Rules Sync](rules.md#resume-time-rules-sync) for the full policy.

**Example:**

```bash
$ sfs resume ses_abc123 --in codex

Source session used rules v3 (sessionfs).
Current project rules are v5.
Synced codex.md from SessionFS rules v5.
Launching codex resume ...

Session resumed successfully.
  CC Session ID: abc123-def456
  JSONL: /Users/me/.claude/projects/.../abc123-def456.jsonl
  Messages: 23

Open Claude Code in /Users/me/myproject to continue.
```

Skip preflight for a one-off resume:

```bash
$ sfs resume ses_abc123 --in codex --no-rules-sync
```

Take ownership of a hand-written `codex.md`:

```bash
$ sfs resume ses_abc123 --in codex --force-rules
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

---

## `sfs alias`

Set or clear a session alias for easy reference.

```
sfs alias SESSION_ID [ALIAS]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `SESSION_ID` | yes | Session ID or prefix |
| `ALIAS` | no | Alias name (omit to clear) |

**Example:**

```bash
$ sfs alias ses_a1b2 auth-debug
Alias set: auth-debug -> ses_a1b2c3d4e5f6

$ sfs show auth-debug   # Now works with alias
```

---

## `sfs search`

Full-text search across all local sessions.

```
sfs search QUERY [OPTIONS]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `QUERY` | yes | Search text |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--tool` | string | — | Filter by source tool |
| `--cloud` | flag | `false` | Search cloud sessions instead of local |
| `--json` | flag | `false` | Output as JSON |

**Example:**

```bash
$ sfs search "rate limiting middleware"

2 results:
  ses_a1b2  claude-code  "...added rate limiting middleware to..."
  ses_c3d4  codex        "...the rate limiter should handle..."
```

---

## `sfs summary`

Show a session summary — files changed, tests run, commands executed.

```
sfs summary SESSION_ID [OPTIONS]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `SESSION_ID` | yes | Session ID or prefix |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--format` | string | — | Export format: `md` for markdown |
| `--today` | flag | `false` | Show summary table of all sessions from today |

**Example:**

```bash
$ sfs summary ses_a1b2

Debug auth middleware
2.3h | 327 msgs | 28 tool calls | Claude Code
Branch: feature/auth-fix @ a1b2c3d

Files modified (3):
  src/auth/middleware.py
  src/auth/tokens.py
  tests/test_auth.py

Commands: 34
Tests: 6 runs (5 passed, 1 failed)
Packages: pyjwt, redis
```

---

## `sfs audit`

Audit a session for hallucinations using LLM-as-a-Judge.

```
sfs audit SESSION_ID [OPTIONS]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `SESSION_ID` | yes | Session ID or prefix |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--model` | string | `claude-sonnet-4` | Judge LLM model |
| `--api-key` | string | — | LLM API key (or use config/env) |
| `--provider` | string | auto-detect | Provider: anthropic, openai, google, openrouter |
| `--base-url` | string | — | Custom OpenAI-compatible endpoint (LiteLLM, vLLM, Ollama) |
| `--consensus` | flag | `false` | Run 3 passes, report where 2+ agree (3x cost) |
| `--report` | flag | `false` | Show existing report only |
| `--json` | flag | `false` | Output as JSON |
| `--format` | string | — | Export: `json`, `markdown`, `csv` |

**Example:**

```bash
$ sfs audit ses_a1b2 --model gpt-4o --base-url https://litellm.internal/v1

Trust Score: 74%
3 contradictions | 9 unverified | 42 verified

CRITICAL  test_result   msg #34  "Test passes" -> exit code 1
HIGH      file_existence msg #12  "Created validator.py" -> No Write call
```

---

## `sfs delete`

Delete a session from the cloud, the local device, or both. Requires an explicit scope flag — there is no default.

```
sfs delete SESSION_ID [OPTIONS]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `SESSION_ID` | yes | Session ID or prefix |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--cloud` | flag | — | Delete from server, keep local copy |
| `--local` | flag | — | Remove local copy, keep cloud copy |
| `--everywhere` | flag | — | Delete from both server and local |
| `--force` | flag | `false` | Skip confirmation prompt |

If no scope flag is provided, the command prints an error and exits.

**Examples:**

```bash
# Remove from cloud only (local copy stays)
$ sfs delete ses_abc123 --cloud
Delete ses_abc123 from cloud? Local copy will be kept. [y/N] y
Deleted from cloud. Recoverable for 30 days.

# Remove from this device only
$ sfs delete ses_abc123 --local
Delete ses_abc123 from this device? Cloud copy is unaffected. [y/N] y
Removed local copy.

# Delete everywhere (recoverable for 30 days)
$ sfs delete ses_abc123 --everywhere --force
Deleted from cloud and local device. Recoverable for 30 days.
```

See [Delete Lifecycle](delete-lifecycle.md) for full details on retention, recovery, and sync behavior.

---

## `sfs trash`

List soft-deleted sessions in the retention window.

```
sfs trash
```

**Example:**

```bash
$ sfs trash

Trash (3 sessions — purge after 30 days)

ID           Deleted       Scope        Purge after
ses_abc123   2 days ago    cloud        2026-05-14
ses_def456   5 days ago    everywhere   2026-05-11
ses_ghi789   12 days ago   everywhere   2026-04-28
```

---

## `sfs restore`

Undo a soft-delete. Clears the server-side deletion flag and removes the session from the local exclusion list.

```
sfs restore SESSION_ID
```

| Argument | Required | Description |
|----------|----------|-------------|
| `SESSION_ID` | yes | Session ID or prefix |

If the local copy was also removed (scope was `everywhere`), run `sfs pull <id>` afterward to re-download it.

**Example:**

```bash
$ sfs restore ses_abc123
Session restored. Run 'sfs pull ses_abc123' to re-download locally.
```

---

## `sfs push`

Push a session to the cloud.

```
sfs push SESSION_ID
```

---

## `sfs pull`

Pull a session from the cloud.

```
sfs pull SESSION_ID
```

---

## `sfs pull-handoff`

Pull a session from a handoff link.

```
sfs pull-handoff HANDOFF_ID
```

**Example:**

```bash
$ sfs pull-handoff hnd_x7k9

Session pulled. 47 messages.
Run: sfs resume ses_abc --in claude-code
```

---

## `sfs list-remote`

List sessions stored on the cloud server.

```
sfs list-remote [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--page` | int | `1` | Page number |
| `--page-size` | int | `20` | Results per page |

---

## `sfs handoff`

Hand off a session to a teammate with email notification.

```
sfs handoff SESSION_ID --to EMAIL [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--to` | string | required | Recipient email |
| `--message` | string | — | Message to include in the email |

---

## `sfs sync`

Bidirectional sync and autosync management.

### `sfs sync` (default)

Run bidirectional sync — push local changes, pull remote-only sessions.

```
sfs sync
```

### `sfs sync auto`

Set autosync mode.

```
sfs sync auto --mode MODE
```

| Mode | Behavior |
|------|----------|
| `off` | No autosync (default). Manual `sfs push` only. |
| `all` | Every new or updated session auto-pushes to cloud. |
| `selective` | Only sessions in the watchlist auto-push. |

### `sfs sync watch`

Add sessions to the autosync watchlist (selective mode).

```
sfs sync watch SESSION_ID [SESSION_ID...]
```

### `sfs sync unwatch`

Remove sessions from the autosync watchlist.

```
sfs sync unwatch SESSION_ID [SESSION_ID...]
```

### `sfs sync watchlist`

Show all sessions in the autosync watchlist.

```
sfs sync watchlist
```

### `sfs sync status`

Show current autosync mode, counts, and storage usage.

```
sfs sync status
```

---

## `sfs project`

Manage shared project context — a single document shared across the team via MCP.

### `sfs project init`

Create a project context for the current repo (matched by git remote).

```
sfs project init
```

### `sfs project show`

Display the current project context with metadata.

```
sfs project show
```

### `sfs project edit`

Open the context document in `$EDITOR`. Changes upload on save.

```
sfs project edit
```

### `sfs project set-context`

Set project context from a file.

```
sfs project set-context FILE
```

### `sfs project get-context`

Output raw project context markdown to stdout.

```
sfs project get-context
```

### `sfs project rebuild`

Force a full rebuild of the project's compiled context document from all active claims. Resets `compiled_at` on every active claim and clears the existing `context_document`, so the next compile pass produces a fresh document. Useful after significantly editing the knowledge base or when a settled project's context has drifted from current reality.

```
sfs project rebuild
```

---

## `sfs rules`

Manage canonical project rules and compile them into the tool-specific files each AI agent reads (`CLAUDE.md`, `codex.md`, `.cursorrules`, `.github/copilot-instructions.md`, `GEMINI.md`). See [Rules Portability](rules.md) for the full reference.

### `sfs rules init`

Seed canonical rules for the current project. Detects the git remote, preselects enabled tools from existing rule files + recent tool usage, and optionally imports a single unmanaged rule file as the canonical seed.

```
sfs rules init [--local-only]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--local-only` | flag | `false` | Gitignore the compiled rule files instead of committing them |

Only the five v0.9.9-supported tools are preselected: `claude-code`, `codex`, `cursor`, `copilot`, `gemini`. The picker shows the reason for each preselection (file present, recent usage, or manual pick).

**Example:**

```bash
$ sfs rules init

Detected rule files:
  CLAUDE.md       (reason: file present)
  .cursorrules    (reason: file present)

Recent tool usage (last 90 days):
  codex           (reason: recent usage)

Enable these tools? [Y/n]
Seeded canonical rules for myorg/my-project.
Run 'sfs rules edit' to edit preferences, then 'sfs rules compile'.
```

### `sfs rules edit`

Open the canonical `static_rules` document in `$EDITOR`.

```
sfs rules edit
```

### `sfs rules show`

Show current canonical version, enabled tools, knowledge/context injection config, and whether compiled outputs are in sync.

```
sfs rules show
```

**Example:**

```bash
$ sfs rules show

Project: myorg/my-project
Canonical version: 4
Enabled tools: claude-code, codex, cursor, gemini

Knowledge injection: on
  Types: convention, decision
  Budget: 2000 tokens

Context injection: on
  Sections: overview, architecture
  Budget: 2000 tokens

Compiled outputs: in sync (last compile: 2026-04-12)
```

### `sfs rules compile`

Compile canonical rules into tool-specific files. Deterministic — no new version is created unless a compiled output changes by hash.

```
sfs rules compile [--tool TOOL] [--dry-run] [--force]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--tool` | string | — | Compile for a single tool only |
| `--dry-run` | flag | `false` | Show what would be written without touching disk |
| `--force` | flag | `false` | Overwrite files not managed by SessionFS |

**Example:**

```bash
$ sfs rules compile

Compiling canonical rules v4...
  CLAUDE.md                               written  (sha256:9a1c…)
  codex.md                                written  (sha256:4e2f…)
  .cursorrules                            written  (sha256:b7d1…)
  .github/copilot-instructions.md         written  (sha256:c3a8…)
  GEMINI.md                               written  (sha256:5f90…)

New rules version: 5
```

SessionFS refuses to overwrite a rule file that is not managed (no SessionFS marker and not created by `sfs rules init`). Use `sfs rules init` to import it, or pass `--force`.

### `sfs rules push`

Push the canonical record and latest compiled version to the SessionFS API. Uses optimistic concurrency — a stale write returns `409 Conflict`.

```
sfs rules push
```

### `sfs rules pull`

Pull canonical rules from the SessionFS API. Run `sfs rules compile` afterwards to regenerate tool files.

```
sfs rules pull
```

---

## `sfs storage`

Manage local session storage.

### `sfs storage` (default)

Show local disk usage, session counts, and retention policy.

```
sfs storage
```

### `sfs storage prune`

Prune old sessions to free disk space.

```
sfs storage prune [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--dry-run` | flag | `false` | Show what would be pruned without deleting |
| `--force` | flag | `false` | Skip confirmation prompt |

---

## `sfs daemon restart`

Restart the daemon (stop + start).

```
sfs daemon restart
```

---

## `sfs daemon rebuild-index`

Rebuild the local session index from .sfs files on disk. Backfills missing `source_tool` from tracked sessions.

```
sfs daemon rebuild-index
```

Use this when the index is corrupted or sessions appear missing despite files existing on disk.

---

## `sfs watcher`

Manage tool watchers.

### `sfs watcher list`

List all tool watchers and their status.

```
sfs watcher list
```

### `sfs watcher enable`

Enable a tool watcher.

```
sfs watcher enable TOOL
```

### `sfs watcher disable`

Disable a tool watcher.

```
sfs watcher disable TOOL
```

---

## `sfs auth`

Manage cloud authentication.

### `sfs auth login`

Authenticate with the cloud server.

```
sfs auth login [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--url` | string | `https://api.sessionfs.dev` | Server URL |
| `--key` | string | — | API key |

### `sfs auth signup`

Create a new account.

```
sfs auth signup [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--url` | string | `https://api.sessionfs.dev` | Server URL |

### `sfs auth status`

Show current authentication status.

```
sfs auth status
```

---

## `sfs org`

Manage your organization — create, invite members, and view team info. Requires cloud authentication (`sfs auth login`).

### `sfs org info`

Show organization info and member count.

```
sfs org info
```

**Example:**

```bash
$ sfs org info

Organization: Acme Corp
  Slug: acme-corp
  Tier: Team
  Members: 5
  Created: 2026-01-15
```

### `sfs org create`

Create a new organization (you become admin). Requires Team tier.

```
sfs org create NAME SLUG
```

| Argument | Required | Description |
|----------|----------|-------------|
| `NAME` | yes | Display name for the organization |
| `SLUG` | yes | URL-friendly identifier (lowercase, hyphens) |

**Example:**

```bash
$ sfs org create "Acme Corp" acme-corp

Organization created: Acme Corp (acme-corp)
  You are now admin.
```

### `sfs org invite`

Invite a user to your organization (admin only). Invite expires in 7 days.

```
sfs org invite EMAIL [OPTIONS]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `EMAIL` | yes | Email address of the user to invite |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--role` | string | `member` | Role to assign: `member` or `admin` |

**Example:**

```bash
$ sfs org invite alice@example.com --role admin

Invitation sent to alice@example.com (role: admin).
  Expires: 2026-04-06
```

### `sfs org members`

List all members in your organization with roles and join dates.

```
sfs org members
```

**Example:**

```bash
$ sfs org members

                   Members (3)
┌───────────────────────┬────────┬────────────┐
│ Email                 │ Role   │ Joined     │
├───────────────────────┼────────┼────────────┤
│ you@example.com       │ admin  │ 2026-01-15 │
│ alice@example.com     │ admin  │ 2026-02-01 │
│ bob@example.com       │ member │ 2026-03-10 │
└───────────────────────┴────────┴────────────┘
```

### `sfs org remove`

Remove a member from the organization (admin only). Cannot remove yourself.

```
sfs org remove USER_ID
```

| Argument | Required | Description |
|----------|----------|-------------|
| `USER_ID` | yes | User ID of the member to remove |

**Example:**

```bash
$ sfs org remove usr_b0b123

Removed usr_b0b123 from Acme Corp.
```

---

## `sfs mcp serve`

Start the MCP server on stdio transport.

```
sfs mcp serve
```

Tools exposed (12): `search_sessions`, `get_session_context`, `list_recent_sessions`, `find_related_sessions`, `get_project_context`, `get_session_summary`, `get_audit_report`, `add_knowledge`, `update_wiki_page`, `list_wiki_pages`, `search_project_knowledge`, `ask_project`.

---

## `sfs mcp install`

Auto-configure MCP for an AI tool.

```
sfs mcp install --for TOOL
```

| Option | Type | Description |
|--------|------|-------------|
| `--for` | string | Target tool: `claude-code`, `codex`, `gemini`, `copilot`, `cursor`, `amp`, `cline`, `roo-code` |

---

## `sfs admin reindex`

Re-extract metadata for all cloud sessions (admin only).

```
sfs admin reindex
```

---

## `sfs admin create-trial`

Create a trial license for self-hosted deployments (admin only).

```
sfs admin create-trial [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--org` | string | — | Organization slug |
| `--days` | int | `14` | Trial duration in days |

---

## `sfs admin create-license`

Create a full license for self-hosted deployments (admin only).

```
sfs admin create-license [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--org` | string | required | Organization slug |
| `--tier` | string | required | License tier (team, enterprise) |
| `--seats` | int | — | Seat limit |
| `--expires` | string | — | Expiry date (ISO format) |

---

## `sfs admin list`

List all self-hosted licenses (admin only).

```
sfs admin list [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--status` | string | — | Filter by status: active, expired, revoked |

---

## `sfs admin extend`

Extend an existing license expiry (admin only).

```
sfs admin extend LICENSE_ID --days DAYS
```

| Argument | Required | Description |
|----------|----------|-------------|
| `LICENSE_ID` | yes | License ID |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--days` | int | required | Number of days to extend |

---

## `sfs admin revoke`

Revoke a self-hosted license (admin only).

```
sfs admin revoke LICENSE_ID
```

| Argument | Required | Description |
|----------|----------|-------------|
| `LICENSE_ID` | yes | License ID to revoke |

---

## `sfs doctor`

Run health checks on the local SessionFS installation with auto-repair for common issues.

```
sfs doctor
```

Checks performed (8): daemon running, index integrity, watcher health, config validity, disk space, MCP config, auth status, session format.

**Example:**

```bash
$ sfs doctor

SessionFS Health Check
  ✓ Daemon running (PID 12345)
  ✓ Index integrity OK (47 sessions)
  ✓ Watchers healthy (4/4)
  ✓ Config valid
  ✓ Disk space OK (2.1 GB free)
  ✗ MCP config missing for codex — auto-repaired
  ✓ Auth status OK
  ✓ Session format OK

7/8 passed, 1 auto-repaired.
```

---

## `sfs project compile`

Compile project knowledge entries into a structured context document with section pages.

```
sfs project compile
```

---

## `sfs project entries`

List knowledge entries for the current project.

```
sfs project entries [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--type` | string | — | Filter by entry type |
| `--json` | flag | `false` | Output as JSON |

---

## `sfs project health`

Check project context health — pending entries, stale compilations, missing pages.

```
sfs project health
```

---

## `sfs project dismiss`

Dismiss a pending knowledge entry.

```
sfs project dismiss ENTRY_ID
```

| Argument | Required | Description |
|----------|----------|-------------|
| `ENTRY_ID` | yes | Knowledge entry ID to dismiss |

---

## `sfs project ask`

Ask a question about the project using compiled knowledge.

```
sfs project ask QUESTION
```

| Argument | Required | Description |
|----------|----------|-------------|
| `QUESTION` | yes | Question to ask about the project |

---

## `sfs project pages`

List wiki pages for the current project.

```
sfs project pages
```

---

## `sfs project page`

Show a specific wiki page by slug.

```
sfs project page SLUG
```

| Argument | Required | Description |
|----------|----------|-------------|
| `SLUG` | yes | Wiki page slug |

---

## `sfs project regenerate`

Regenerate the compiled project context from current knowledge entries.

```
sfs project regenerate
```

---

## `sfs project set`

Set a project configuration value (e.g., auto-narrative toggle).

```
sfs project set KEY VALUE
```

| Argument | Required | Description |
|----------|----------|-------------|
| `KEY` | yes | Setting key (e.g., `auto_narrative`) |
| `VALUE` | yes | Setting value |

---

## `sfs init`

Interactive setup wizard for first-time users. Auto-detects installed AI tools and configures watchers. Optionally sets up cloud sync.

```
sfs init
```

**Example:**

```bash
$ sfs init

Detected tools:
  ✓ Claude Code
  ✓ Codex CLI
  ✓ Gemini CLI
  ✗ Cursor (not installed)
  ✓ Copilot CLI
  ✗ Amp (not installed)
  ✗ Cline (not installed)
  ✗ Roo Code (not installed)

Enabling watchers for 4 detected tools...
Set up cloud sync now? [y/N]:
```

---

## `sfs dlp`

Scan sessions for secrets and PHI, manage organization-wide DLP policy.

SessionFS ships with 22 secret patterns (API keys, access tokens, private keys, database URLs) and 14 PHI patterns (SSN, phone, credit card, medical IDs) based on industry standards. Policies run in one of three modes — `warn`, `redact`, or `block`.

### `sfs dlp scan`

Scan one or more local sessions for secrets and PHI. Produces a JSON report with findings, positions, and severity.

```
sfs dlp scan [SESSION_ID ...] [--mode MODE] [--format FORMAT]
```

**Options:**

| Flag | Description |
|------|-------------|
| `--mode warn\|redact\|block` | Default action when a finding is detected (default: `warn`) |
| `--format json\|text` | Output format (default: `text`) |
| `--verbose` | Show matched substrings (redacted by default) |

**Examples:**

```bash
# Scan the most recent session
$ sfs dlp scan

# Scan a specific session with JSON output
$ sfs dlp scan ses_abc123 --format json

# Scan all dirty sessions before syncing
$ sfs dlp scan --dirty
```

### `sfs dlp policy`

View or update the DLP policy. For organizations, this writes to the server-side org settings and applies to all members.

```
sfs dlp policy [--get|--set-mode MODE|--enable|--disable]
```

**Options:**

| Flag | Description |
|------|-------------|
| `--get` | Show the current DLP policy |
| `--set-mode warn\|redact\|block` | Update the policy mode |
| `--enable` | Enable DLP scanning on the org |
| `--disable` | Disable DLP scanning on the org |

**Examples:**

```bash
# View current policy
$ sfs dlp policy --get
{
  "enabled": true,
  "mode": "redact",
  "redact_patterns": ["secret", "phi"]
}

# Switch to block mode (rejects uploads containing findings)
$ sfs dlp policy --set-mode block
```

When org policy is set to `block`, any `sfs push` / `sfs sync` that contains a detected secret or PHI finding is rejected by the server with a `403` and a report of the offending patterns. When set to `redact`, the server automatically redacts the matches and stores the redacted archive.

---

## `sfs security`

Audit and fix security configuration.

### `sfs security scan`

Scan for security issues — config file permissions, API key exposure in config, and dependency vulnerabilities.

```
sfs security scan
```

**Example:**

```bash
$ sfs security scan

Config permissions .......... OK (600)
API key in config.toml ...... WARNING (plaintext key found)
pip-audit ................... OK (0 vulnerabilities)

1 issue found. Run 'sfs security fix' to remediate.
```

### `sfs security fix`

Auto-fix security issues found by `sfs security scan`.

```
sfs security fix
```

---

## Billing and Tier Enforcement

When any cloud command receives a `403` response with an `upgrade_required` error, the CLI displays a friendly message indicating the required tier and a URL to upgrade:

```bash
$ sfs org create "Acme Corp" acme-corp

This feature requires the Team tier.
  Your tier: Free
  Upgrade: https://sessionfs.dev/pricing
```

This applies to all commands that interact with the cloud API, including `sfs org`, `sfs push`, `sfs handoff`, and `sfs sync`.
