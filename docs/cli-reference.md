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
| `--in` | string | `claude-code` | Target tool: `claude-code`, `codex`, `copilot`, or `gemini`. Capture-only tools (`cursor`, `amp`, `cline`, `roo-code`, `kilo-code`, `antigravity`) are rejected with a pointer to a bidirectional tool. |
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

If pushes for a session keep failing (server errors, rate limits, network), the daemon backs off instead of retrying every cycle: after 1 minute, 5 minutes, 15 minutes, then hourly. After a long failing streak (12 attempts over at least six hours) it pauses that session until the session changes, the daemon restarts, or you run `sfs daemon retry-sync`. `sfs daemon status` lists these sessions with their failure count and last error, and `sfs doctor` reports them under "Sync retries".

---

## `sfs daemon retry-sync`

Retry sessions whose sync is backing off or paused after failed pushes, on the daemon's next sync cycle.

```
sfs daemon retry-sync [SESSION_ID]
```

| Argument / Option | Description |
|----------|-------------|
| `SESSION_ID` | Retry just this session (a unique prefix of its ID also works). Omit to retry every session that is backing off or paused. |
| `--config PATH` | The config file the daemon was started with, if you started it with `sfs daemon start --config`. |

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

## `sfs config default-org`

*v0.10.0+ — multi-org users only.* Show, set, or clear your default org. The
default org is consulted by `sfs project init` to pick the scope for a new
project when neither `--org` nor `--personal` is passed; sessions captured in
workspaces with no matching `Project` row stay personal regardless of this
setting (server-side session-routing keys on git remote → project lookup, not
on `default_org_id`). The value is stored server-side (User.default_org_id)
and validated against your membership — you cannot set a default for an org
you don't belong to.

```
sfs config default-org [ORG_ID] [--clear]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `ORG_ID` | no | Org id to set as default. Omit (and pass no flags) to show. |

| Option | Description |
|--------|-------------|
| `--clear` | Remove the default org preference (fall back to personal scope) |

**Examples:**

```bash
# Show current default
$ sfs config default-org
Default org: org_acme_4f3d

# Set default
$ sfs config default-org org_acme_4f3d
Default org set to org_acme_4f3d.

# Clear default
$ sfs config default-org --clear
Default org cleared.
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
sfs push SESSION_ID [--yes] [--timeout SECONDS]
```

| Option | Description |
|--------|-------------|
| `--yes`, `-y` | Skip interactive confirmations (DLP findings, cloud-undelete). Findings are still shown. |
| `--timeout SECONDS` | How long to wait for the server to accept the upload (1–3600). Defaults to `SFS_UPLOAD_TIMEOUT`, then `upload_timeout` under `[sync]` in `config.toml`, then 300. Raise it for very large sessions on servers that run DLP scans. |

If the server doesn't answer in time, the push stops with a message saying so instead of retrying the same upload.

---

## `sfs pull`

Pull a session from the cloud.

```
sfs pull SESSION_ID
```

---

## Per-file size cap discovery (v0.10.27+)

`sfs push`, `sfs pull`, `sfs sync`, `sfs handoff`, and `sfs pull-handoff` all enforce a per-file size cap inside the sync archive. The CLI discovers the cap with the following precedence:

1. **`SFS_MAX_SYNC_MEMBER_BYTES_PAID` env var** — explicit operator override. Always wins; lets customers experiment ahead of a server upgrade.
2. **Server-supplied `max_member_bytes`** from `GET /api/v1/sync/settings` — tier-aware (`10 MB` free / `50 MB` paid by default); reflects whatever the operator has set on the server.
3. **`50 MB` literal final fallback** — only used when the env var is unset AND server discovery fails (network error, older server, missing field). Logged with a warning so the misconfiguration is visible.

The value is cached per CLI invocation — one `/sync/settings` call per `sfs sync` / `sfs push` / `sfs pull` / `sfs handoff` run. Older servers that pre-date the `max_member_bytes` field fall through to the literal cleanly without crashing.

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

Name exactly one recipient: `--to`, `--to-user-id` or `--to-team-id`.

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--to` | string | — | Recipient email |
| `--to-user-id` | string | — | Recipient user ID (direct account match) |
| `--to-team-id` | string | — | Recipient team ID (Team and Enterprise) |
| `--message`, `-m` | string | — | Message to include in the email |
| `--ticket` | string | — | Carry an active ticket through to the recipient |
| `--persona` | string | — | Carry a persona name through to the recipient |
| `--expires-hours` | int | `168` | Expiry in hours (capped by plan at 30 or 90 days) |
| `--attach` | string | — | Attach a project reference, repeatable. Format `kind:ref_id`, where kind is `kb_entry`, `wiki_page` or `ticket` (e.g. `--attach kb_entry:42`) |

---

## `sfs handoffs`

View and act on handoffs you sent or received.

| Command | Description |
|---------|-------------|
| `sfs handoffs inbox` | List handoffs sent to you |
| `sfs handoffs sent` | List handoffs you sent |
| `sfs handoffs get HANDOFF_ID` | Show full detail: status, attachments, comments and events |
| `sfs handoffs revoke HANDOFF_ID --reason TEXT` | Sender revokes a pending handoff (reason required, 1–500 characters); the recipient is notified |
| `sfs handoffs decline HANDOFF_ID [--reason TEXT]` | Recipient declines a pending handoff (optional reason, up to 500 characters); the sender is notified |
| `sfs handoffs comment HANDOFF_ID --message TEXT` | Post a comment on the handoff thread (1–10,000 characters); the other party is notified |
| `sfs handoffs comments HANDOFF_ID [--limit N]` | List comments, oldest first (`--limit` 1–200, default 200) |
| `sfs handoffs events HANDOFF_ID [--limit N]` | Show the handoff's audit log, oldest first (`--limit` 1–200, default 200) |

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

## `sfs recapture`

Manually re-run the watcher capture for a session, even if `.sfs` already
exists locally. Useful when a tool's native log file got compressed or
trimmed and the existing `.sfs` is now stale relative to it.

```
sfs recapture SESSION_ID [OPTIONS]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `SESSION_ID` | yes | Session ID or prefix |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--force` | flag | `false` | Re-capture even when the source has fewer messages than the existing `.sfs` (the compression-safe guard normally blocks this). |

**Behaviour:**
- Refuses to write a smaller `.sfs` over a larger one unless `--force`. The
  shared `should_recapture()` guard prevents accidental data loss when a
  source log has been compressed or trimmed.
- Refuses to recapture sessions on the deleted-sessions exclusion list — a
  re-captured session must not silently re-appear after a deliberate delete.
- Cursor-specific: raises `CursorComposerPurgedError` when the source
  composer has been purged, with guidance to use `--force` only if you
  understand the resulting `.sfs` will be empty.

**Examples:**

```bash
# Standard recapture — guard active
$ sfs recapture ses_abc123

# Force recapture (smaller source allowed)
$ sfs recapture ses_abc123 --force
```

---

## `sfs project`

Manage shared project context — a single document shared across the team via MCP.

### `sfs project init`

Create a project context for the current repo (matched by git remote).

```
sfs project init [--org ORG_ID | --personal]
```

| Option | Description |
|--------|-------------|
| `--org ORG_ID` | Scope the project to the given org. You must be a member. |
| `--personal` | Force personal scope, overriding any `default_org_id`. |

*v0.10.0+:* if neither flag is passed, the project inherits scope from your
server-side `default_org_id` (set with `sfs config default-org`). If you have
no default, the project is personal.

New org-scoped projects also inherit their org's KB creation defaults
(`kb_retention_days` / `kb_max_context_words` / `kb_section_page_limit`) from
the org settings panel at creation time.

### `sfs project link-repo` / `unlink-repo` / `repos`

*v0.10.31+.* A project can own more than one git repo, so personas, knowledge,
tickets, and rules are shared across all of a product's repos instead of being
split across separate projects.

```
sfs project link-repo <remote> [--primary] [--project-id ID]
sfs project unlink-repo <remote> [--project-id ID]
sfs project repos [--project-id ID]
```

| Command | Description |
|---------|-------------|
| `link-repo <remote>` | Link a git remote to a project. Requires project-admin standing. A GitHub repo is ownership-verified against your own App installation; otherwise it is owner-attested. `--primary` makes it the project's display remote. |
| `unlink-repo <remote>` | Remove a repo from the project. Refused if it is the project's last repo. |
| `repos` | List the project's linked repos, with the primary and verification status marked. |

Each repo belongs to exactly one project. Linking a repo already owned elsewhere
returns a conflict; a verified owner can reclaim a repo held by an unverified link.

### `sfs project merge`

*v0.10.31+.* Consolidate two projects that are really one product (e.g. you ended
up with separate frontend and backend projects). Folds the source project into the
target — repos, sessions, knowledge, tickets, personas, and rules — and leaves the
source as a tombstone that redirects to the target.

```
sfs project merge <source-project-id> --into <target-project-id> [--confirm] [--interactive] [--persona-policy rename|skip|merge_content]
```

| Option | Description |
|--------|-------------|
| (default) | **Dry run** — prints the full merge plan (what reassigns, what collides, the resolved names) and writes nothing. |
| `--confirm` | Execute the merge (single atomic transaction; audited). |
| `--interactive` | Resolve each name collision (persona/rules/wiki) interactively. |
| `--persona-policy` | Default collision policy for personas (`rename`, the default; `skip`; or `merge_content`). |

Both projects must be in the same scope (same org, or both personal); cross-org
merges are refused. Free on all tiers.

### `sfs project transfer`

*v0.10.0+.* Initiate or act on a project transfer. Exactly one of `--to`,
`--accept`, `--reject`, or `--cancel` must be passed.

```
sfs project transfer (--to DEST | --accept ID | --reject ID | --cancel ID) [--force]
```

| Option | Description |
|--------|-------------|
| `--to DEST` | Initiate a transfer. `DEST` is `personal` or an org id. Run from the project's repo. |
| `--accept ID` | Target user accepts a pending incoming transfer. |
| `--reject ID` | Target user rejects a pending incoming transfer. |
| `--cancel ID` | Initiator cancels a pending outgoing transfer. |
| `--force` | With `--to` (auto-accepted) or `--accept`: move the project even if open tickets rely on org personas the destination lacks. |

State machine: pending → accepted | rejected | cancelled. Audit row survives
the transition for compliance. When the initiator IS the target (personal →
own org you belong to), the server auto-accepts at create time.

**Org personas and transfers (GH #70):** moving a project into a different org or out of one is refused (409 `personas_stranded`) when open tickets are assigned to org personas that will not exist in the destination; the error lists each persona and its ticket ids. Project personas move with the project, so a same-named project persona avoids the refusal. Nothing is copied automatically: reassign the tickets, create the personas in the destination, or pass `--force` (on `--to` when the transfer auto-accepts, or on `--accept`).

**Examples:**

```bash
# Move this project into an org you belong to
$ sfs project transfer --to org_acme_4f3d

# Make a project personal again (admin initiates from org)
$ sfs project transfer --to personal

# Target accepts a pending incoming transfer
$ sfs project transfer --accept xfer_a1b2c3d4

# Initiator cancels before the target acts
$ sfs project transfer --cancel xfer_a1b2c3d4
```

### `sfs project transfers`

*v0.10.0+.* List your project transfers.

```
sfs project transfers [-d incoming|outgoing] [--state STATE]
```

| Option | Default | Description |
|--------|---------|-------------|
| `--direction`, `-d` | `incoming` | `incoming` (waiting on you) or `outgoing` (you initiated). |
| `--state` | — | Filter: `pending` / `accepted` / `rejected` / `cancelled`. |

### `sfs project show`

Display the current project context with metadata.

```
sfs project show
```

### `sfs project set`

Update project settings. Rename the project with `--name`, toggle
auto-narrative-on-sync with `--auto-narrative` / `--no-auto-narrative`.
Either or both may be supplied in one call.

```
sfs project set --name "Acme Platform"
sfs project set --auto-narrative
sfs project set --name "Acme Platform" --no-auto-narrative
```

Renaming requires project-admin standing (you must be the project owner
or an admin of the project's org). The new name is 1–255 characters;
leading/trailing whitespace is trimmed and HTML tags are stripped.
`sfs project show` reflects the new name immediately. This renames the
**project** — it is distinct from `sfs session rename`, which renames a
captured session.

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

### `sfs project promote-eligible`

Promote eligible knowledge-base notes to claims in bulk. Dry-run by default: it lists what would be promoted and writes nothing until you pass `--confirm`. A note is eligible when it is not dismissed or superseded, meets the length and confidence thresholds, and is not a near-duplicate of an active claim (or of another note promoted in the same run).

```
sfs project promote-eligible [OPTIONS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--min-length` | int | `50` | Skip entries shorter than this |
| `--min-confidence` | float | `0.8` | Skip entries below this confidence (ignored when `--confidence` is set) |
| `--confidence` | float | — | Set each candidate's confidence to this value before the duplicate check |
| `--entry-type` | string | — | Only promote this entry type (`decision`, `pattern`, …) |
| `--confirm` | flag | off | Apply the promotions |

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

### `sfs rules emit`

Print the latest compiled rules for a tool to stdout. Reads from the local rule cache populated by `sfs rules compile` and `sfs rules pull` — never hits the network.

```
sfs rules emit --tool TOOL [--format hook|file]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--tool` | string | required | Target tool (`claude-code`, `codex`, `cursor`, `copilot`, `gemini`) |
| `--format` | string | `hook` | `hook` for Claude Code's hook JSON spec, `file` for the plain compiled body |

`--format hook` is the format the SessionStart hook installed by `sfs hooks install` consumes. `--format file` is useful for piping the compiled body into another tool or inspecting it from the shell. If the local cache is empty, the command prints an empty payload and exits `0` — it never breaks Claude Code startup.

**Example:**

```bash
$ sfs rules emit --tool claude-code --format hook
{
  "hookSpecificOutput": {
    "hookEventName": "SessionStart",
    "additionalContext": "..."
  }
}
```

---

## `sfs hooks`

Manage SessionFS hooks for tools with native hook support. v0.9.9.6 supports Claude Code only. See [Hook-based injection](rules.md#hook-based-injection-claude-code) for the full reference.

### `sfs hooks install`

Wire the SessionFS `SessionStart` hook into the target tool's settings. The hook calls `sfs rules emit` on every session start and pipes the compiled rules into the system prompt. Idempotent — running twice does not duplicate the entry.

```
sfs hooks install --for TOOL [--user|--project] [--force]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--for` | string | required | Target tool (`claude-code` only in v0.9.9.6) |
| `--user` | flag | default | Install at user scope (`~/.claude/settings.json`) |
| `--project` | flag | `false` | Install at project scope (`.claude/settings.json` in repo root) |
| `--force` | flag | `false` | Skip the conflict warning when a managed `CLAUDE.md` already exists |

The hook block carries an `"sfs:managed": true` sentinel so `sfs hooks uninstall` can find and remove only the SessionFS entry. User-defined hooks are preserved.

**Example:**

```bash
$ sfs hooks install --for claude-code
Hook installed: ~/.claude/settings.json (SessionStart)
SessionFS will inject project rules at every Claude Code startup.
```

### `sfs hooks uninstall`

Remove the SessionFS-managed hook entry from the target tool's settings. Idempotent — no-op if not installed.

```
sfs hooks uninstall --for TOOL [--user|--project] [--force]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--for` | string | required | Target tool (`claude-code` only in v0.9.9.6) |
| `--user` | flag | default | Uninstall from user scope |
| `--project` | flag | `false` | Uninstall from project scope |
| `--force` | flag | `false` | Skip the confirmation prompt |

### `sfs hooks status`

Show which SessionFS hooks are installed across supported tools and scopes. Tools without native hook support appear as `N/A`.

```
sfs hooks status
```

**Example:**

```bash
$ sfs hooks status

SessionFS Hooks
───────────────
claude-code (user):     INSTALLED at ~/.claude/settings.json (SessionStart)
claude-code (project):  not installed
codex:                  N/A (no native hook system)
gemini:                 N/A
cursor:                 N/A
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

`TOOL` is one of `claude-code`, `codex`, `gemini`, `copilot`, `cursor`, `amp`, `cline`, `roo-code`, `kilo-code`, `antigravity`. The `antigravity` watcher covers the desktop app (`~/.gemini/antigravity`), the `agy` CLI (`~/.gemini/antigravity-cli`) and the IDE build (`~/.gemini/antigravity-ide`); each captured session records which app it came from.

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
| `--profile` | string | — | Save under a named profile for multi-account use (see below). Omit for the `default` profile. |

### Multi-account profiles (v0.10.29+)

Run more than one SessionFS account on the same device — e.g. your org's
operational account plus a separate account for external-org / client
work. One active profile drives **every** command in a shell (push,
pull, sync, ticket, persona, agent, the daemon) — they all resolve the
same identity.

```bash
sfs auth login --profile client-acme       # save a second account
sfs auth use client-acme                    # make it active (persisted)
sfs auth profiles                           # list profiles, * marks active
sfs auth whoami                             # show active profile + resolved email
sfs auth use default                        # switch back
```

**Resolution precedence** (highest first):

1. `SESSIONFS_API_KEY` (+ optional `SESSIONFS_API_URL`) — an ephemeral
   identity for CI / one-off use. Overrides any profile for that shell.
2. `SESSIONFS_PROFILE=<name>` — a per-shell active-profile override.
3. The persisted active profile (`sfs auth use <name>`).
4. The `default` profile (`~/.sessionfs/config.toml`).

Each named profile gets an isolated session store
(`~/.sessionfs/profiles/<name>/store`) so captures, the local index, and
the delete-exclusion list don't bleed across accounts. The `default`
profile keeps `~/.sessionfs` unchanged — existing single-account installs
need no migration. Profile files are written `chmod 600` (they hold raw
API keys).

> A long-running daemon pins the profile it started with; it does not
> flip identity mid-run. Restart the daemon after `sfs auth use` to pick
> up a new active profile.

#### `sfs auth use <name>`

Set the active profile. Persists to `~/.sessionfs/active_profile`; affects
all subsequent commands in any new shell until overridden.

#### `sfs auth profiles`

List known profiles and mark the active one.

#### `sfs auth whoami`

Show the active profile and the account it resolves to (one `/auth/me`
call), including which precedence source supplied the key.

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

### `sfs auth keys list`

List your personal API keys. Prints id, name, key prefix, expires_at, last_used_at, and active state.

```
sfs auth keys list
```

### `sfs auth keys create`

Mint a personal API key. The raw key is printed to stdout exactly once — capture it immediately.

```
sfs auth keys create --name NAME [--expires-days N] [--output-key]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--name`, `-n` | string | required | Human label (e.g. `macbook16-laptop`) |
| `--expires-days` | int | none | Key lifetime in days (1–730). Omit for no expiry. |
| `--output-key` | flag | false | Print ONLY the raw key to stdout (no decorations). Use `KEY=$(sfs auth keys create ... --output-key)` in CI. |

### `sfs auth keys revoke`

Revoke a personal API key. Soft delete with required audit reason.

```
sfs auth keys revoke KEY_ID --reason "..."
```

| Argument | Required | Description |
|----------|----------|-------------|
| `KEY_ID` | yes | Personal key id (from `sfs auth keys list`) |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--reason`, `-r` | string | required | Why this key is being revoked (audit trail) |

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

Create a new organization (you become its owner). Requires Team tier.

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

### `sfs org set-role`

Change a member's role (admin only). Accepts a user ID or email. Ownership
transfer is a separate flow and is not allowed here.

```
sfs org set-role USER_ID_OR_EMAIL admin|member
```

| Argument | Required | Description |
|----------|----------|-------------|
| `USER_ID_OR_EMAIL` | yes | User ID or email of the member |
| `ROLE` | yes | New role: `admin` or `member` |

**Example:**

```bash
$ sfs org set-role alice@example.com admin

Role for alice@example.com changed to admin.
```

### `sfs org remove`

Remove a member from the organization (admin only). Accepts a user ID or
email. Cannot remove yourself, the owner, or the last admin.

```
sfs org remove USER_ID_OR_EMAIL [OPTIONS]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `USER_ID_OR_EMAIL` | yes | User ID or email of the member to remove |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--bulk` | string | — | File with one user ID/email per line (`#` comments and blank lines ignored) |
| `--yes` | flag | — | Skip the confirmation prompt for bulk removal |

**Example:**

```bash
$ sfs org remove usr_b0b123

Member usr_b0b123 removed.
```

**Bulk removal** resolves every entry up front and aborts before removing
anything if any entry can't be resolved, then removes one at a time and
continues past individual failures:

```bash
$ sfs org remove --bulk members.txt --yes

About to remove:
  - usr_b0b123
  - alice@example.com
Removed 2 of 2 member(s).
```

### `sfs org auto-join`

Manage auto-join by verified email domain. When a user verifies an email on
one of these domains (and is not yet in any org), they are added to the org
as a member. Requires a verified domain and a paid plan (OIDC SSO).

```
sfs org auto-join list
sfs org auto-join add DOMAIN
sfs org auto-join remove DOMAIN
```

| Subcommand | Description |
|------------|-------------|
| `list` | Show the domains currently configured for auto-join |
| `add DOMAIN` | Enable auto-join for a verified email domain |
| `remove DOMAIN` | Disable auto-join for a domain |

**Example:**

```bash
$ sfs org auto-join list
example.com

$ sfs org auto-join add example.com
Enabled auto-join for example.com.

$ sfs org auto-join remove example.com
Disabled auto-join for example.com.
```

---

## `sfs mcp serve`

Start the MCP server on stdio transport.

```
sfs mcp serve
```

Tools exposed (80):

- **Sessions:** `search_sessions`, `get_session_context`, `list_recent_sessions`, `find_related_sessions`, `get_session_summary`, `get_audit_report`, `get_session_provenance`, `get_session_retrieval_log`, `rename_session`, `checkpoint_session`, `list_checkpoints`, `fork_session`
- **Knowledge (read):** `get_project_context`, `get_context_section`, `get_wiki_page`, `get_wiki_page_history`, `search_project_knowledge`, `list_knowledge_entries`, `get_knowledge_entry`, `get_knowledge_health`, `ask_project`
- **Knowledge (write):** `add_knowledge`, `update_wiki_page`, `list_wiki_pages`, `compile_knowledge_base`, `dismiss_knowledge_entry`, `update_entry_confidence`, `promote_entry`, `promote_eligible_entries`
- **Rules (read-only):** `get_rules`, `get_compiled_rules`
- **Personas:** `list_personas`, `get_persona`, `create_persona`, `update_persona`, `delete_persona`, `assume_persona`, `forget_persona`
- **Tickets:** `list_tickets`, `get_ticket`, `create_ticket`, `update_ticket`, `start_ticket`, `complete_ticket`, `resolve_ticket`, `approve_ticket`, `assign_persona`, `escalate_ticket`, `add_ticket_comment`, `list_ticket_comments`, `get_ticket_review_state`
- **Agent runs and feedback:** `create_agent_run`, `complete_agent_run`, `list_agent_runs`, `record_agent_feedback`, `list_agent_feedback`, `get_agent_feedback_summary`
- **Work queues:** `create_work_queue`, `list_work_queues`, `get_work_queue`, `set_work_queue_status`, `run_work_queue_step`, `complete_work_queue_step`
- **Handoffs:** `create_handoff`, `get_handoff`, `list_inbox_handoffs`, `list_sent_handoffs`, `claim_handoff`, `decline_handoff`, `revoke_handoff`, `add_handoff_comment`
- **Marketplace (never writes files or enables executable content):** `search_skill_marketplace`, `get_marketplace_package`, `diff_marketplace_versions`, `preview_marketplace_skill`, `load_marketplace_skill`, `request_skill_install`, `list_skill_install_requests`, `get_skill_install_request`, `cancel_skill_install_request`

If you are an agent or have the MCP server installed, prefer the MCP tools over the CLI equivalents listed in this reference — they are faster, run in-process, and avoid hitting API rate limits.

---

## `sfs mcp install`

Auto-configure MCP for an AI tool.

```
sfs mcp install --for TOOL
```

| Option | Type | Description |
|--------|------|-------------|
| `--for` | string | Target tool: `claude-code`, `codex`, `gemini`, `copilot`, `cursor`, `amp`, `cline`, `roo-code`, `kilo-code`, `antigravity` |

For `antigravity`, SessionFS always writes the current config, `~/.gemini/config/mcp_config.json` (agy CLI and current desktop builds; created if missing), and also updates `~/.gemini/antigravity/mcp_config.json` (older desktop builds) when that file exists. Other servers in those files are kept, and a file that is not valid JSON is left untouched.

For every tool that uses a JSON config file, `sfs mcp install` and `uninstall` check all target files first (valid JSON, writable, not a symlink to something you don't own) and change nothing unless every check passes. Each file is then written on its own: the previous version is saved next to it as `<file>.sessionfs-backup-<UTC time>` (the three newest are kept), and the new content replaces the file atomically with its permissions kept, so the config is never missing or half-written. A symlinked config is edited at its target, so the link stays in place. If another program changes the file at the same moment, SessionFS re-reads it and tries again. Each file's result is reported; if one file could not be changed, the others keep their update and the command says which one failed.

---

## `sfs mcp uninstall`

Remove the SessionFS MCP server registration from an AI tool. Other servers in the tool's config are kept.

```
sfs mcp uninstall --for TOOL
```

| Option | Type | Description |
|--------|------|-------------|
| `--for` | string | Target tool: `claude-code`, `codex`, `gemini`, `copilot`, `cursor`, `amp`, `cline`, `roo-code`, `kilo-code`, `antigravity` |

---

## `sfs mcp index`

Rebuild the local full-text search index from all local sessions.

```
sfs mcp index
```

---

## `sfs admin reindex`

Re-extract metadata for all cloud sessions (admin only).

```
sfs admin reindex
```

---

## `sfs admin create-trial`

Create a trial license key for a self-hosted deployment (admin only).

```
sfs admin create-trial ORG EMAIL [OPTIONS]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `ORG` | yes | Organization name |
| `EMAIL` | yes | Contact email |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--days` | int | `14` | Trial duration in days |
| `--seats` | int | `25` | Seat limit |

---

## `sfs admin create-license`

Create a paid license key with no expiry for a self-hosted deployment (admin only).

```
sfs admin create-license ORG EMAIL [OPTIONS]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `ORG` | yes | Organization name |
| `EMAIL` | yes | Contact email |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--tier` | string | `enterprise` | License tier |
| `--seats` | int | `25` | Seat limit |

---

## `sfs admin list-licenses`

List license keys (admin only).

```
sfs admin list-licenses [--status STATUS]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--status` | string | `all` | Filter: `all`, `active`, `trial`, `expired`, `revoked` |

---

## `sfs admin extend-trial`

Extend a trial license by N days from now (admin only).

```
sfs admin extend-trial KEY [--days N]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--days` | int | `14` | Days to extend from now |

---

## `sfs admin revoke-license`

Revoke a license key (admin only). The next license validation is refused with 403.

```
sfs admin revoke-license KEY [--reason TEXT]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--reason` | string | — | Reason for revocation |

---

## `sfs admin service-keys`

*v0.10.11+ — org-scoped service key management. Org admin + Team-or-above tier required.*

See [API Keys](./api-keys.md) for the conceptual guide, scope vocabulary, and integration recipes.

### `sfs admin service-keys list`

List service keys in an org. Shows id, name, key prefix, scopes, project allowlist, expires_at, and active state.

```
sfs admin service-keys list --org ORG_ID
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--org`, `-o` | string | required | Organization id (e.g. `org_9e39b81833e6fdd5`) |

### `sfs admin service-keys create`

Mint a new scoped service key. The raw key is printed to stdout exactly once — capture it immediately.

```
sfs admin service-keys create --org ORG_ID --name NAME --scope SCOPE [--scope SCOPE]... [--expires-days N] [--project PID]... [--output-key]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--org`, `-o` | string | required | Organization id |
| `--name`, `-n` | string | required | Human label (e.g. `github-actions-review-bot`) |
| `--scope`, `-s` | string | required, repeatable | Capability scope. Run `sfs admin service-keys scopes` for the full vocab. Wildcard `*` is rejected — service keys must enumerate scopes. |
| `--expires-days` | int | none | Key lifetime in days (1–730). Omit for no expiry. |
| `--project`, `-p` | string | none, repeatable | Per-key project allowlist. Omit to allow all projects in the org. |
| `--output-key` | flag | false | Print ONLY the raw key to stdout (no decorations). CI-safe — use `KEY=$(sfs admin service-keys create ... --output-key)`. |

Local validation: unknown scopes and `*` exit with code 2 before the network call.

### `sfs admin service-keys rotate`

Issue a new raw secret for an existing service key. The old key is revoked atomically in the same transaction with `revoke_reason="rotated by <user>"`. Scopes, expiry policy, and project allowlist are preserved.

```
sfs admin service-keys rotate KEY_ID --org ORG_ID [--output-key]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `KEY_ID` | yes | Service key id |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--org`, `-o` | string | required | Organization id |
| `--output-key` | flag | false | Print ONLY the new raw key to stdout (no decorations) |

Rotating a key that's already revoked returns 409 with a friendly hint to `create` a fresh one.

### `sfs admin service-keys revoke`

Soft-delete a service key with a required audit reason.

```
sfs admin service-keys revoke KEY_ID --org ORG_ID --reason "..."
```

| Argument | Required | Description |
|----------|----------|-------------|
| `KEY_ID` | yes | Service key id |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--org`, `-o` | string | required | Organization id |
| `--reason`, `-r` | string | required | Why this key is being revoked (audit trail; non-blank) |

### `sfs admin service-keys scopes`

Print the live scope vocabulary (sourced from the server's `VALID_SCOPES` set so it always reflects the running version).

```
sfs admin service-keys scopes
```

## `sfs admin trusted-reviewers`

Manage the trusted-reviewer registry — the org-scoped allowlist of identities whose review verdicts the work-queue `review_until_clean` stop oracle will trust. Org-admin (or owner) only. Registering a trusted reviewer is what lets a dedicated automated reviewer key (e.g. a GPT-5.5/Codex service key) post a `VERIFIED-CLEAN` that can auto-finish a queued item.

### `sfs admin trusted-reviewers list`

```
sfs admin trusted-reviewers list --org ORG_ID [--include-revoked]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--org`, `-o` | string | required | Organization id |
| `--include-revoked` | flag | false | Also show revoked (soft-deleted) rows |

### `sfs admin trusted-reviewers add`

Bind an identity (a service key OR a user) to the reviewer persona it is authorized to post verdicts as. Provide exactly one identity; scope is org-wide unless `--project` is given.

```
sfs admin trusted-reviewers add --org ORG_ID [--service-key-id SK_ID | --user-id USER_ID] [--project PID] [--persona codex-reviewer]
```

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--org`, `-o` | string | required | Organization id |
| `--service-key-id` | string | — | Service key to trust (must belong to this org). |
| `--user-id` | string | — | User to trust (must be a member of this org). |
| `--project`, `-p` | string | none | Limit trust to one project; omit for org-wide. |
| `--persona` | string | `codex-reviewer` | Reviewer persona the identity may post verdicts as. |

### `sfs admin trusted-reviewers revoke`

Soft-delete (deactivate) a trusted-reviewer row; the audit history is preserved.

```
sfs admin trusted-reviewers revoke REVIEWER_ID --org ORG_ID [--reason "..."]
```

| Argument | Required | Description |
|----------|----------|-------------|
| `REVIEWER_ID` | yes | Trusted-reviewer row id |

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `--org`, `-o` | string | required | Organization id |
| `--reason`, `-r` | string | none | Optional audit reason |

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

## `sfs persona` (v0.10.1)

Manage agent personas for the current project. Personas are portable AI roles (e.g. `atlas`, `prism`, `scribe`) shared by humans and AI agents working on the same codebase. Personas can also be defined at the org level and shared by every project in the org — see [Org-level personas](#org-level-personas).

**Tier:** Pro+. Persona names must be ASCII (1–50 chars: letters, digits, `_`, `-`).

### `sfs persona list`

List active personas usable in the current project: the project's own personas plus its org's personas (shown with `Org` in the Scope column).

```bash
sfs persona list
sfs persona list --no-org            # project personas only
sfs persona list --org org_abc123    # one org's personas
```

### `sfs persona show`

Print a persona's role, specializations, and full markdown content.

```bash
sfs persona show atlas
sfs persona show atlas --raw  # don't render markdown
```

### `sfs persona create`

Create a new persona. Pass `--content` or `--file`, or omit both to open `$EDITOR`.

```bash
sfs persona create atlas --role "Backend Architect" --file ./atlas.md
sfs persona create prism --role "Frontend Lead" --content "# Prism\n\nReact/TypeScript focus."
sfs persona create scribe --role "Docs Lead"  # opens $EDITOR
```

Options: `--role/-r` (required), `--file/-f`, `--content`, `--spec` (repeatable).

### `sfs persona edit`

Open the persona's content in `$EDITOR` and update on save.

```bash
sfs persona edit atlas
```

### `sfs persona delete`

Soft-delete a persona. The row stays (`is_active=false`) so historical tickets still resolve the name.

```bash
sfs persona delete atlas
sfs persona delete atlas --yes --force
```

Refuses (409) when non-terminal tickets reference the persona unless `--force` is passed; the CLI prints how many tickets are affected and suggests `sfs ticket list --assigned-to <name>` for triage.

### `sfs persona assume`

Declare that you are working as a persona without starting a ticket. Writes `~/.sessionfs/active_ticket.json` (with `ticket_id=null`) so the daemon tags every captured session with the persona name.

```bash
sfs persona assume atlas
```

Pair with `sfs persona forget` when done.

### `sfs persona forget`

Clear the local persona bundle written by `sfs persona assume`.

```bash
sfs persona forget
```

### Org-level personas

A persona can also be defined once for a whole organization and used from every project in that org (GitHub #70). Pass `--org <org_id>` to `list`, `show`, `create`, `edit`, and `delete` to work on the org's personas instead of the current project's. Writes need the org **admin** or **owner** role; any org member can read them.

```bash
sfs persona create gcp-infra --role "Terraform engineer" --file ./gcp-infra.md --org org_abc123
sfs persona list --org org_abc123          # the org's own personas
sfs persona edit gcp-infra --org org_abc123
sfs persona delete gcp-infra --org org_abc123 --yes
```

How names resolve inside a project: the project's own persona wins, then the org's. A project persona with the same name as an org persona **overrides** it for that project (repo-specific tuning); deleting the override brings the org persona back. This applies everywhere a persona is looked up by name — `sfs ticket assign`, `sfs ticket start`, `sfs persona show/assume`, agent runs, and the MCP tools.

- `sfs persona list` shows project and org personas together, with a **Scope** column. `--no-org` lists project personas only.
- `sfs persona pull --all` also writes the org's personas to `.agents/` (same `<name>-<role-fragment>.md` naming; the file notes it is an org persona). When a project persona overrides an org one, only the project version is written. `--no-org` skips org personas.
- `sfs persona edit <name>` without `--org` refuses an org persona and prints the `--org` command to use.
- Org names are case-insensitive-unique per org. An org persona and a project persona may share a name. Name lookups are case-insensitive everywhere, and stored references (ticket assignment, agent runs, provenance, the local active-ticket bundle) use the persona's stored spelling.
- Org personas are only visible to current members of the project's current org (and to that org's service keys). Someone who can reach the project another way — its owner, or someone with an earlier captured session — sees only the project's own personas, for example after the project moves to an org they don't belong to.
- Deleting an org persona refuses (409) while non-terminal tickets in the org's projects are assigned to it and the project has no same-named project persona to fall back to; `--force` overrides.
- Service keys use the same `personas:read` / `personas:write` scopes and only reach their own org. A service key limited to specific projects can read org personas but not write them.
- Tier: the org's own plan must include personas (Team and Enterprise do).

---

## `sfs ticket` (v0.10.1)

Manage agent tickets for the current project. Tickets are self-contained units of work assigned to a persona.

**Tier:** Team+.

**Status FSM (Task, default):** `suggested → open → in_progress → blocked → review → done` (terminal). `suggested/open → cancelled` (terminal).
**Status FSM (Issue, v0.10.24):** `suggested → open → in_progress → closed` (terminal). Issues never enter review; they are PM rollups closed manually with `sfs ticket close`.

### `sfs ticket list`

List tickets with optional filters.

```bash
sfs ticket list
sfs ticket list --assigned-to atlas
sfs ticket list --status in_progress
sfs ticket list --priority high
sfs ticket list --kind issue              # only PM rollup containers
sfs ticket list --kind task               # only executor work (omit for both)
```

### `sfs ticket show`

Print full ticket details: description, acceptance criteria, file refs, dependencies, completion notes.

```bash
sfs ticket show tk_abc123
```

### `sfs ticket create`

Create a new ticket. Defaults to source=`human` (lands as `open`).

```bash
sfs ticket create --title "Fix rate limiter" \
  --description "KB search endpoint allows unbounded queries." \
  --assigned-to atlas --priority high \
  --criteria "Per-user limit" --criteria "Tier-aware" \
  --file src/sessionfs/server/routes/knowledge.py
```

Options: `--title/-t` (required), `--description/-d`, `--assigned-to/-a`, `--priority/-p` (critical/high/medium/low), `--criteria` (repeatable), `--file/-f` (repeatable), `--depends-on` (repeatable), `--kind` (`task` (default) or `issue` — PM rollup container; v0.10.24), `--parent` (parent Issue id, only valid when `--kind task`; child task rolls up into the Issue's Children view).

Pass `--kind issue` for high-level PM rollups (no executor; closed manually with `sfs ticket close`). Tasks attached to an Issue via `--parent tk_...` appear under that Issue's Children rollup in CLI + MCP + dashboard.

### `sfs ticket start`

Start working on a ticket. Writes `~/.sessionfs/active_ticket.json` (the local provenance bundle) so the daemon tags every session captured during this work with the persona + ticket. Prints the compiled persona + ticket context the agent should consume.

```bash
sfs ticket start tk_abc123
sfs ticket start tk_abc123 --force          # recover a stuck blocked ticket
sfs ticket start tk_abc123 --tool cursor    # size context to cursor's 4k-token budget
sfs ticket start tk_abc123 --no-print-context
```

### `sfs ticket complete`

Mark a ticket complete. Moves to `review`. Clears the active-ticket bundle only when it points at this ticket — bundles from another tool/session are preserved.

```bash
sfs ticket complete tk_abc123 --notes "Implemented per-user rate limit; tests added." \
  --file src/sessionfs/server/middleware.py \
  --kb-entry 142
```

### `sfs ticket update`

Change a ticket's editable fields. Every change is recorded in the ticket's edit history and posted as a system comment showing the diff; an update that changes nothing posts nothing. Status changes go through the lifecycle commands, and persona reassignment through `sfs ticket assign`.

```bash
sfs ticket update tk_abc123 --title "Rate-limit the export endpoint" --priority high
sfs ticket update tk_abc123 --description-file notes.md --acceptance-criteria-file ac.txt
```

| Option | Description |
|--------|-------------|
| `--title` | New title |
| `--description` / `--description-file` | New description, inline or read from a file |
| `--priority` | `low`, `medium`, `high` or `critical` |
| `--acceptance-criteria-file` | File with one acceptance criterion per line; replaces the whole list |
| `--lease-epoch` | The lease epoch from `sfs ticket show`; the update is refused (409) if another worker has since started the ticket |

Only the ticket's creator or a project admin can update it.

### `sfs ticket review-state`

Summarize a review ticket's findings — open and closed findings, severity counts and the last verdict — derived from its comment thread. Prints "no review state" for tickets without reviewer comments.

```bash
sfs ticket review-state tk_abc123
```

### `sfs ticket comment`

Add a comment (progress update, question, blocker). Comments are slack-like — each call creates a new row.

```bash
sfs ticket comment tk_abc123 --content "Spotted a related bug in the cache layer."
sfs ticket comment tk_abc123 --content "Speaking as atlas" --as atlas
```

### `sfs ticket comments`

List all comments on a ticket, chronological. Renders each comment in a titled panel (author + created_at) with Markdown body content.

```bash
sfs ticket comments tk_abc123
```

### `sfs ticket watch`

Poll a ticket's comments endpoint and render new comments as they appear. Initial poll renders all existing comments; subsequent polls only render NEW ones (diffed by comment id). Process-local seen-set — restart re-shows existing comments.

```bash
sfs ticket watch tk_abc123                               # default 30s cadence
sfs ticket watch tk_abc123 --interval 15                 # poll every 15s (clamped to [5, 300])
sfs ticket watch tk_abc123 --from-author codex-reviewer  # filter to one author
sfs ticket watch tk_abc123 --exit-on-new                 # exit 0 on first new comment (CI hookup)
sfs ticket watch tk_abc123 --notify                      # OS notification on new comments (macOS)
```

Each new comment triggers a terminal bell. `--notify` additionally calls `terminal-notifier` on macOS if installed (best-effort, no-op otherwise). Ctrl-C exits gracefully with a count of new comments seen during the session.

### `sfs ticket status`

Show which ticket the local provenance bundle currently points at.

```bash
sfs ticket status
```

### Lifecycle commands

```bash
sfs ticket block tk_abc123     # in_progress → blocked
sfs ticket unblock tk_abc123   # blocked → in_progress
sfs ticket reopen tk_abc123    # review → open (reporter requests changes)
sfs ticket approve tk_abc123   # suggested → open (approve an agent-created ticket)
sfs ticket dismiss tk_abc123   # suggested/open → cancelled
sfs ticket resolve tk_abc123   # review → done (Task terminator; runs dep enrichment)
sfs ticket close tk_abc123     # in_progress → closed (Issue terminator; manual, NOT auto-derived from child Task status)
```

Issues and Tasks have forked FSM terminators: Tasks finish via `complete` → `accept` → `resolve` (done); Issues finish via `close` (closed). `sfs ticket list --kind issue` filters to PM rollup containers; `sfs ticket list --kind task` (default omits filter) to executor work.

### `sfs ticket assign`

Assign or re-assign a ticket to a persona. FSM state is unaffected.

```bash
sfs ticket assign tk_abc123 --to atlas
```

### `sfs ticket escalate`

Bump a ticket's priority one level (low → medium → high → critical). Optional `--reason` is posted as an audit-trail comment.

```bash
sfs ticket escalate tk_abc123
sfs ticket escalate tk_abc123 --reason "Customer-facing outage in prod"
```

---

## `sfs agent` (v0.10.2)

Record auditable runs of an agent persona against a ticket, with policy enforcement at completion. CI-first design — every flag has a stdin/stdout contract suitable for shell scripting and `gh`/`gitlab-ci` workflows.

**Tier:** Team+.

**Status FSM:** `queued → running → passed | failed | errored | cancelled` (all terminal).

### `sfs agent run <persona>`

Create + start an AgentRun, emit the compiled persona + ticket context, and optionally poll until completion.

```
sfs agent run <persona> [--ticket TK_ID]
                        [--trigger-source ci|webhook|scheduled|manual|mcp|api]
                        [--trigger-ref <sha|branch|cron>]
                        [--ci-provider github|gitlab|bitbucket|...]
                        [--ci-run-url <url>]
                        [--tool generic|claude-code|codex|gemini|bedrock|vertex|...]
                        [--fail-on none|low|medium|high|critical]
                        [--context-file PATH]
                        [--timeout SECONDS]
                        [--format text|json|markdown]
                        [--output-id]
```

| Flag | Description |
|------|-------------|
| `--ticket` | Scope the run to a ticket (must be same project; cross-project ticket-id is rejected with 422). |
| `--trigger-source` | Where the run originated. CI runners pass `ci`. |
| `--trigger-ref` | Free-form ref (PR commit SHA, branch, cron expr). |
| `--ci-provider`, `--ci-run-url` | Deep-link the run back to your CI UI. |
| `--tool` | Token budget hint when compiling persona + ticket context. `claude-code`=16k, `codex/gemini/copilot/amp/antigravity/generic`=8k, `cursor/windsurf/cline/roo-code/kilo-code`=4k, `bedrock`=16k, `vertex`=8k. |
| `--fail-on` | Severity threshold for `policy_result = "fail"` at complete time. |
| `--context-file` | Write the compiled context to this file instead of stdout (use this with `--output-id`). |
| `--timeout` | Block and poll until the run reaches a terminal status or `SECONDS` elapse. |
| `--format` | Status output format when polling. `markdown` is GitHub/GitLab step-summary-compatible. |
| `--output-id` | Print **only** the run id on stdout (everything else routes to stderr). Pairs with `$(sfs agent run ... --output-id)` capture in CI. |

### `sfs agent complete <run_id>`

Record findings + severity, evaluate policy, exit per the stored `exit_code`.

```
sfs agent complete <run_id> --summary "..." --severity none|low|medium|high|critical
                            [--findings-file findings.json]
                            [--status passed|failed|errored]
                            [--session-id SES_ID]
                            [--enforce]
```

| Flag | Description |
|------|-------------|
| `--summary` | Human-readable summary of what the run did. Required. |
| `--severity` | Worst severity present in findings. `none` never trips a `fail_on` threshold. |
| `--findings-file` | Path to a JSON file with a list of objects (e.g. `[{"severity":"high","title":"..."}]`). |
| `--status` | Override the terminal status — useful for review tooling that crashed (`--status errored --severity none`). Failed/errored statuses always store `exit_code=1` regardless of severity, so `--enforce` fails CI on crash. |
| `--session-id` | Tag the run with the session that produced it. |
| `--enforce` | Exit with the stored `exit_code` (0 on pass / 1 on fail). Defense-in-depth: also exits non-zero for any `failed`/`errored` status even when stored `exit_code` is 0. |

### `sfs agent status <run_id>`

Show a run's detail.

```
sfs agent status <run_id> [--format text|json|markdown]
```

`--format markdown` emits a GitHub-Actions / GitLab step-summary-compatible block — pipe it to `$GITHUB_STEP_SUMMARY` to surface the result in the CI summary panel.

### `sfs agent list`

List recent runs in the project.

```
sfs agent list [--persona NAME] [--status STATUS]
               [--trigger-source SOURCE] [--ticket TK_ID]
               [--limit N]
```

Default sort: `created_at` descending. See `docs/integrations/github-actions-agent-run.yml` and `docs/integrations/gitlab-agent-run.yml` for full CI-workflow examples wired up against `sfs agent run/complete/status` + the `--output-id` capture pattern.

### `sfs agent feedback …` (v0.17.0)

Append-only structured feedback/outcome records on agent work. A record points at least one target (`--agent-run`, `--ticket`, or `--comment`), all within the same project, and classifies the signal by `--type` × `--verdict` × optional `--label`. **Tier:** Team+.

The label vocabulary is fixed per `--type`:

| `--type` | Valid `--label` values |
|----------|------------------------|
| `route` | `correct_route`, `wrong_route` |
| `summary` | `summary_good`, `summary_bad` |
| `actionability` | `actionable`, `not_actionable` |
| `duplicate` | `duplicate`, `new_issue` |
| `resolution` | `false_positive`, `flaky`, `resolved_by_fix`, `resolved_by_test_update` |

Reassigning an agent-created ticket to a different persona auto-records `route` / `negative` / `wrong_route` (source `auto`) — see `assign_persona`.

```
sfs agent feedback add --type route --verdict negative --label wrong_route \
    --ticket TK_ID --final-owner sentinel [--notes "..."] [--format text|json]
sfs agent feedback list [--agent-run ID] [--ticket ID] [--type T] [--verdict V]
                        [--label L] [--persona NAME] [--limit N] [--format text|json]
sfs agent feedback summary [--persona NAME] [--since ISO] [--format text|json]
```

| Flag | Description |
|------|-------------|
| `--type` | Feedback category (`route` / `summary` / `actionability` / `duplicate` / `resolution`). Required for `add`. |
| `--verdict` | `positive` / `negative` / `neutral`. Required for `add`. |
| `--label` | Fixed vocabulary word valid for `--type`. |
| `--final-owner` | Persona/owner the signal points at (≤100 chars). |
| `--notes` | Free-form note (≤4000 chars). |
| `--agent-run`, `--ticket`, `--comment` | Targets; at least one required for `add`. |
| `--persona` | Filter `list`/`summary` to records whose run or ticket belongs to this persona. |
| `--since` | ISO timestamp — only records after it (for `summary`; `list` paginates via cursor). |
| `--format` | `text` (Rich table) or `json` (machine-safe stdout). |

`summary` is pure SQL aggregation (grouped counts by type × verdict × label) — no LLM. `--format json` on `list`/`summary` emits parseable output to stdout for scripting.

---

## `sfs resident` (v0.14.0)

Run a **resident** — an operator-hosted, always-on agent that drives a work queue by calling your own LLM. Configured via a TOML file at `~/.sessionfs/residents/<name>.toml` (see `docs/operations/resident-operator-guide.md`). The reviewer posts trusted verdicts; the implementer proposes code in an isolated worktree and never merges or self-closes.

| Command | Description |
|---------|-------------|
| `sfs resident run --config <name>` | Start the resident loop (polls the queue, calls your LLM, acts). `--once` runs a single wake; `--cold` rebuilds the mind from durable sources; `--daily-token-budget` / `--per-wake-token-budget` bound LLM spend (fail-closed). |
| `sfs resident health --config <name>` | Read-only summary of the resident's config + today's LLM budget spend (does not contact the server or start the loop). |

The implementer resident runs inside a hardened sandbox (`deploy/resident-sandbox/`). Merging a resident-authored, resident-auto-closed change in a high-risk class follows the mandatory checklist in `docs/operations/high-risk-merge-checklist.md`.

## `sfs skill` / `sfs plugin` (v0.16.0)

Import, inspect, and install portable Agent Skills and Agent Plugins locally. SessionFS preserves standards payloads, pins Git sources, computes deterministic target plans, discloses omissions, and records package provenance on captured sessions. `sfs plugin` is an alias of `sfs skill`.

| Command | Description |
|---------|-------------|
| `sfs skill inspect SOURCE [--pin REF] [--subdir PATH] [--for TOOL] [--json]` | Validate and inspect a local path, Git source, hosted package, or named local marketplace package without installing, and print its install disclosure (see below) |
| `sfs skill add SOURCE [--for TOOL] [--scope project\|user]` | Plan and transactionally install across one or more tools |
| `sfs skill list` / `sfs skill show ID` | Inspect installed state, provenance, hashes, targets, and omissions |
| `sfs skill plan ID` / `sfs skill compile ID [--for TOOL] [--drop TOOL] [--yes] [--allow-hidden-unicode]` | Compute a side-effect-free plan or re-apply the pinned package. `--for` is additive (installed targets are always kept); only `--drop TOOL` removes a tool, after listing the managed files to be unlinked and confirming (`--yes` in non-interactive mode) |
| `sfs skill sync --locked [--approve-widening] [--allow-hidden-unicode]` | Reproduce exact project lockfile state without upgrading. Every installed version the lockfile replaces is classified like an update before anything changes; one refused transition refuses the whole sync |
| `sfs skill remove ID` / `sfs skill update ID [--approve-widening] [--allow-hidden-unicode]` | Remove or update a package while preserving unmanaged (dirty) files; `update` previews old → new hash, commit, plan, the update diff and its class, then applies the re-consent rules below |
| `sfs skill diff ID [--to VERSION] [--json]` | Show what an update would change without applying it: files added/removed/changed with bounded text diffs, capability changes, new scanner and hidden-character findings, and the update class (`text`, `code`, `widening`) |
| `sfs skill rollback ID --to HASH [--approve-widening] [--allow-hidden-unicode]` | Re-apply a cached immutable version; `--to` is required and must be an exact `sha256:<64 hex>` (or bare 64-hex) digest. The transition is classified and consented like an update |
| `sfs skill doctor` | Detect drift, missing files, unsafe paths, and state mismatches; also shows the size of the removal trash |
| `sfs skill marketplace add URL --name NAME --pin SHA` | Register one pinned local marketplace source |
| `sfs skill marketplace list\|refresh\|remove` | Manage multiple user/project registry aliases explicitly |
| `sfs skill search QUERY [--tool TOOL] [--limit N] [--local-only] [--json]` | Search registered local marketplaces **and** the hosted SessionFS Marketplace. Each row is labelled `local:<scope>` or `hosted` and shows the reference to install it with. Signed out or offline, the hosted part is skipped with a note. `--tool` filters hosted results by tool compatibility; `--marketplace ALIAS` and `--local-only` search local registries only |
| `sfs skill add sessionfs:NAMESPACE/NAME[@VERSION]` | Install a package from the hosted SessionFS Marketplace (requires `sfs auth login`). `sessionfs:spk_<id>` also works and is required when a namespace/name pair is ambiguous. Without `@VERSION` the package's current version is used; `--pin VERSION` is equivalent to `@VERSION` |
| `sfs skill pull [--for TOOL] [--device-id ID] [--yes] [--allow-* ...] [--approve-widening] [--allow-hidden-unicode] [--json]` | Apply your org's skill assignments and your approved install requests to this device (see below) |

### Install disclosure

`sfs skill inspect` and `sfs skill add` print the install disclosure before any consent decision, from the package bytes: per target tool the files written (`.claude/skills/<name>/...`, or `~/...` in user scope) and what is not installed; scripts and their interpreter; frontmatter hooks (event and matcher) and `allowed-tools` pre-approvals; MCP servers (local command, remote origin, environment variable names); network endpoints referenced; shell commands the instructions tell the agent to run (from fenced shell blocks); instruction-scanner findings; and hidden or bidirectional Unicode and control characters, shown as `⟦U+202E⟧` with file, line and column. `inspect --json` includes it as `disclosure`, the same JSON as `GET /api/v1/marketplace/versions/{id}/disclosure` and MCP `get_marketplace_package`. A local package containing the hidden-character classes the publish gate refuses needs `--allow-hidden-unicode` on `add` / `update`.

### Update re-consent

Each installed package records the capability set you consented to (its digest and access manifest). `sfs skill update`, `sfs skill pull` and the daemon classify an update against it:

| Class | Meaning | `sfs skill update` | `sfs skill pull` / daemon |
|---|---|---|---|
| `text` | Only instructions or assets changed | One confirmation; `--yes` works. Capabilities you consented to carry forward (no need to repeat `--allow-scripts`), recorded as carried-forward receipts | Applied |
| `code` | Script, hook, MCP-config or executable-extension bytes changed, no new access | The changed capability's flag again; `--yes` alone is refused | Held unless the changed capability's flag is given; the daemon holds it |
| `widening` | New access: a capability, hook event/matcher, MCP server or command, remote origin, `allowed-tools` grant, network endpoint, environment variable, persistence target, or a lost publisher verification | Always stops: `--approve-widening` (or yes to the interactive widening prompt) plus the capability flags; nothing carries forward | Held, and the installed version stays, until `--approve-widening` or an org admin approves that exact version (`/advance`, which records its capability digest on the assignment) |

The same rules apply to `sfs skill rollback` and `sfs skill sync --locked`. Any change to MCP configuration (portable `mcp.json`, vendor MCP files, `mcpServers` in manifests) is widening; a remote MCP server is identified by name, origin and transport. `hooks.json` hooks are compared by event and matcher like frontmatter hooks; a malformed `hooks.json` changes as a whole and counts as widening. Network endpoints come from every text file in the package, including single-label names and IP addresses. Security sets (scripts, hooks, MCP servers, tool grants, endpoints, environment variables, executable extensions) are compared in full; when one is too large to store it is compared by digest and any change is widening, and a package that could not be analyzed completely (for example a truncated scan) is widening as well. Critical hidden characters are decided on complete counts, and a truncated scan requires `--allow-hidden-unicode`.

Only consent receipts bound to the installed content hash, its capability digest and the current consent rule carry forward, and only for the same tool and scope (an unrestricted receipt covers any tool). Receipts written before this release never carry: give the capability flag once (for example `sfs skill compile <name> --allow-scripts`) to record current consent. Adding a tool to an installed version (`sfs skill compile --for`, `sfs skill sync --locked`, or a pull) goes through the hidden-character gate (`--allow-hidden-unicode`). `sfs skill pull --approve-widening` also permanently lifts a digest restriction recorded for an installed version.

An installed version whose consented access is unknown (installed before this release, with its cached copy gone) is treated as `widening`. Held items are listed by `sfs skill pull` as `held: <name> needs approval` and logged by the daemon. A widening update is applied without `--approve-widening` only for the tools of an assignment or approved request that bound exactly the new version's capability digest; approvals made before this release, or under an older consent rule, never count. `pull` and the daemon also hold any package with critical hidden characters (`sfs skill pull --allow-hidden-unicode` installs it).

Risky capabilities require explicit flags bound to the exact package hash: `--allow-scripts` for any non-inert skill asset (the policy is an inert allowlist, so `.rb`, `.bat`, `.exe`, `Makefile`, and suffix-less files all count), `--allow-hooks` for `SKILL.md` frontmatter `hooks:` / `allowed-tools:` grants, and `--allow-executable-extensions` for a `hooks.json` or `hooks/` directory shipped inside a skill. `--yes` alone is refused for these and the error names the hook or tool grant. Local MCP activation remains reserved: SessionFS does not silently enable it.

### Publishing to the hosted marketplace

`sfs skill validate` runs the hosted marketplace's own publish checks on your machine, and `sfs skill publish` validates the same way, then uploads. The server and the CLI share one validator, so a package that passes locally passes the server's package checks.

| Command | Description |
|---------|-------------|
| `sfs skill validate SOURCE [--visibility personal\|org\|public] [--json]` | Validate a package directory or `.tar.gz` exactly as the server would. Prints each problem with its file and line (when known), why it fails, and how to fix it, then exits 1. On success it prints the content hash, risk flags, sizes, and the checks that only the server can make |
| `sfs skill publish SOURCE [--org ORG_ID] [--visibility personal\|org\|public] [--namespace NS] [--collection NAME] [--yes] [--json]` | Validate locally, show a summary (name, version, license, risk flags, size, target), confirm, then upload. Prints the listing, package, and version IDs and the package URL |
| `sfs skill publish SOURCE --trusted-publishing --namespace NS [--package NAME] [--visibility public] [--audience AUD]` | Publish from a GitHub Actions job without an API key: reads the server's OIDC audience, requests the job's OIDC token, exchanges it for a single-use publish token, and uploads. See [Trusted publishing](#trusted-publishing-from-github-actions) |

How it works:

- A directory is packed into a deterministic ustar `tar.gz`: sorted entries, zeroed timestamps and owners, files `0644`. `.git`, `.hg`, `.svn`, `.DS_Store`, and `Thumbs.db` are left out. Validation runs on those exact bytes, and the same bytes are uploaded. An archive is validated and uploaded as it is.
- The checks are the server's: ustar/GNU archive format only, path safety, collisions by case or Unicode normalization, member/file/size caps, the Agent Plugin `plugin.json` schema, hardened `SKILL.md` frontmatter, MCP credential literals, instruction text limits, and the built-in secret/PHI DLP scan. With `--visibility org` or `public`, a declared `license` in `plugin.json` is also required. DLP problems list every finding by file and line, never the matched text. For a personal target, DLP findings block locally, since no organization policy applies. For an org or public target they are shown as warnings instead: the organization's DLP policy (its allowlist and custom patterns) runs on the server, which accepts or rejects the upload. Every other check blocks for every target.
- Validation stops at the first structural problem, as the server does. Fix it and run again.
- With no `--org`, `publish` uploads to your personal library (`POST /api/v1/users/me/skills/packages`). With `--org`, it uploads to that organization (`POST /api/v1/orgs/{org_id}/skills/packages`, visibility `org` by default). `--visibility public` is accepted from platform administrators, and from a verified publisher that SessionFS has enabled for public publishing, under that publisher's own namespace (personal, or with `--org` for an org publisher). A publisher's first public version, and any update that asks for more access, waits for review before it appears. Namespace and collection default to `personal` or `org`, as on the server.
- A namespace claimed by another publisher (or one indistinguishable from it) takes no new packages from you (`409 namespace_claimed`); packages you already had there keep receiving versions.
- The upload is named `<package-name>.tar.gz`. Publishing a new version of the same package therefore adds a version to the existing package instead of creating a new one. Re-publishing an identical version reuses it.
- `--yes` skips the confirmation. Without a terminal, `publish` exits with code 2 unless `--yes` is given.

Some checks need server state, so only the server makes them. The CLI explains each one when the server returns it:

| Server answer | Meaning |
|---------------|---------|
| `401` | The API key was rejected. Run `sfs auth login` or set `SESSIONFS_API_KEY` |
| `403 upgrade_required` (`marketplace_access`) | Personal publishing needs Starter or above |
| `403 upgrade_required` (`team_skill_library`) | The organization's plan does not include the team skill library |
| `403` (role or scope) | You must be an org owner or admin. The key needs `marketplace:write` (personal) or `skills:admin` (org) |
| `409 visibility_mismatch` | This exact version is already listed in that collection with another visibility. Use `--collection` |
| `413 storage_limit` / `package_too_large` | Over the storage quota or the upload size cap |
| `429` version limits | 50 stored personal versions, or 100 per package. Delete older versions |
| `422 invalid_skill_package` | Package rejected. The 422 body's `details.reason` matches the local reason code. If it is `dlp_findings` on an org publish, the organization's DLP policy did not allow content the CLI flagged as a warning, or one of its custom patterns matched |
| `408 publish_timeout` | The upload must arrive within two minutes. Retry on a faster connection |
| `503 publish_busy` | The server is busy, or you already have a publish in flight. Nothing was published. Retry after the `Retry-After` seconds |
| `409 namespace_claimed` | Another publisher owns that namespace. Choose another `--namespace` |
| `403 public_publishing_not_allowed` / `publisher_not_verified` / `public_publishing_not_enabled` | Public listings need your own verified publisher namespace, opted into public publishing by SessionFS |
| `409 package_name_confusable` | A new public package name imitates a reserved brand or a verified publisher |
| `403 publisher_suspended` | The publisher is suspended; nothing can be published under it |

### Publishers and verification

A publisher owns a globally unique marketplace namespace. Verification proves who the publisher is and shows as a badge on its listings, such as `verified: acme.com` or `verified: github.com/acme (org #1234567)`. Display names never carry a badge.

| Command | Description |
|---------|-------------|
| `sfs skill publisher claim NAMESPACE [--org ORG_ID] [--display-name NAME] [--json]` | Claim a namespace: 3-39 lowercase letters, digits or single hyphens. Personal publishers need Starter or above; org publishers need an org owner/admin and the team skill library. Names that imitate a reserved brand or a verified publisher are refused with `409 namespace_confusable`: lookalike letters from any script (the full Unicode UTS #39 confusables table, so Cyrillic, Greek or Cherokee spellings), leetspeak digits (`0 1 2 3 4 5 6 7 8 9`), `rn`/`m`, one or two typos, and added `-official`, `-team`, `-ai` and similar. A namespace another owner already publishes packages in is refused with `409 namespace_in_use` (ask SessionFS to review the claim). An owner can hold 5 publishers and claim 10 namespaces in total (retired ones count). Namespaces are never released, even after deletion |
| `sfs skill publisher list [--json]` | Publishers you manage, personal and org |
| `sfs skill publisher show PUBLISHER_ID [--json]` | Publisher details; managers also see verifications and trusted publishers |
| `sfs skill publisher verify PUBLISHER_ID --dns DOMAIN` | Start DNS verification. Prints a `sessionfs-verification=...` TXT record to add at the domain. If your organization already verified the domain for SSO, it is reused (and re-checked) immediately |
| `sfs skill publisher verify PUBLISHER_ID --github-org INSTALLATION_ID` / `--github-user INSTALLATION_ID` | Verify a GitHub organization or account through one of **your own** SessionFS GitHub App installations. The installation must be linked to your account through the verified GitHub sign-in flow; an installation linked only by its id is refused with `409 installation_claim_unverified`. The badge is bound to GitHub's numeric account id, so a renamed or re-registered login does not inherit it |
| `sfs skill publisher check PUBLISHER_ID VERIFICATION_ID` | Re-check a verification now |
| `sfs skill publisher trust PUBLISHER_ID --repo OWNER/REPO --workflow FILE [--environment ENV] [--ref-pattern REF] [--package NAME] [--reusable-workflow OWNER/REPO/.github/workflows/FILE] [--allow-workflow-run]` | Allow a GitHub Actions workflow to publish for this publisher (see below) |
| `sfs skill publisher untrust PUBLISHER_ID TRUSTED_ID` | Remove a trusted publisher. Publish tokens already issued under it stop working |

SessionFS re-checks verifications daily. A DNS record missing for seven consecutive days drops the badge (status `lapsed`) until the record is back. Uninstalling or suspending the GitHub App revokes a GitHub verification immediately.

### Trusted publishing from GitHub Actions

Trusted publishing lets a GitHub Actions workflow publish without a SessionFS API key stored in the repository.

1. Claim a publisher and run `sfs skill publisher trust` with the repository, the workflow file, and ideally a GitHub `--environment` with protection rules plus a `--ref-pattern` such as `refs/tags/v*`. The trust is bound to the repository's numeric id and owner id.
2. In the workflow, give the job `permissions: id-token: write` and run `sfs skill publish . --trusted-publishing --namespace NS`. A complete example is in [`docs/integrations/github-actions-skill-publish.yml`](integrations/github-actions-skill-publish.yml).
3. The CLI reads the server's audience from `GET /api/v1/marketplace/trusted-publishing/config` (the deployment's public API origin), requests an OIDC token for it and posts it to `POST /api/v1/marketplace/trusted-publishing/token`. The server verifies GitHub's signature and checks:
   - the repository id and owner id;
   - the workflow file as the **caller** (`workflow_ref`, the file the run started from). If that file calls a shared reusable workflow, the trust still names the caller; add `--reusable-workflow` to also pin the called file (`job_workflow_ref`);
   - the event: `push`, `release` or `workflow_dispatch` only (`workflow_run` only with `--allow-workflow-run`). `pull_request`, `pull_request_target` and other pull-request events never publish, whatever their ref;
   - the environment and the ref (branches and tags only).
   It returns a publish token that expires after 15 minutes, can be used once, and only for that namespace (and package, when the trust or `--package` names one). Each OIDC token can be exchanged once.
4. The upload (`POST /api/v1/marketplace/trusted-publishing/publish`) records where the version was **published from**: repository, workflow, event, ref, environment, run and the run's commit. The listing shows it under "Published from". This is publication provenance, not build provenance: nothing yet binds the archive to that commit, and your declared `source_commit` is left as you set it.

The audience defaults to the server's `SFS_PUBLIC_API_URL`; `SFS_TRUSTED_PUBLISHING_AUDIENCE` overrides it. Outside development a server without either has trusted publishing disabled, and one configured with a generic audience such as `sessionfs` refuses to start, so a token minted for one deployment cannot be replayed against another.

Build provenance (a Sigstore / PEP 740 attestation that binds the archive digest, verified against a transparency log) is not implemented yet.

### Hosted marketplace packages

`sfs skill add sessionfs:acme/review-loop` downloads the version's archive and checks its content hash three ways before installing anything: the catalog's `content_hash`, the download's `X-SessionFS-Content-Hash` header, and the hash recomputed locally from the extracted files must all match. It then uses the same plan, consent and journaled install as any other source. The lockfile records the source as `source_type: hosted`, with `locator: sessionfs:<package id>#<server origin>` (bound to the server it came from), `commit: <version id>` and `listing_name: <namespace>/<name>`. This means:

- `sfs skill sync --locked` re-downloads exactly the locked version and hash, even if the server's current version has moved on.
- `sfs skill update ID` moves to the package's current hosted version. `--pin VERSION` selects a specific version label or version id instead.
- **Bound to its server:** update, `sync --locked` and re-downloads only ever talk to the server recorded in the locator. If that is not the active profile, SessionFS uses another signed-in profile for that server, or refuses with instructions to sign in to it. A different server can never supply a package id it did not publish.
- **Older installs:** packages installed before the server was recorded are refused until you run `sfs skill update ID --rebind-origin`. Only do this when the active server is the one you installed the package from.
- `sfs skill rollback ID --to HASH` re-applies a cached earlier version.

### `sfs skill pull`

Reconciles two server-side lists to this device:

- **Org assignments** (`GET /api/v1/users/me/skills/assignments`, including device assignments when `--device-id` or `SESSIONFS_DEVICE_ID` is set). These install into **user** scope.
- **Your approved install requests** for the current project, matched by the repository's `origin` remote. These install into project scope, or into user scope for `user`/`device` requests.

What pull does with them:

- **Required** assignments and approved requests are installed.
- **Optional** assignments are listed. They are installed only with `--yes` or when you answer yes to a prompt. A yes covers exactly that version, its content and the tools it named; if the assignment later points at other content or names more tools, you are asked again. If you `sfs skill remove` an optional package that pull installed, that version is opted out: neither `pull --yes` nor the daemon reinstalls it (it is reported as `opted-out`) until you answer yes interactively or `sfs skill add` the package again. The opt-out belongs to the account, scope, origin kind and version, so deleting and recreating an identical assignment does not undo it. It is recorded before the package is removed, so an interrupted removal never loses it. Required assignments still reinstall. When several assignments name the same version, the package is downloaded once, but each assignment keeps its own mode and tools. Only the tools of required assignments are installed automatically. Each optional assignment's extra tools are offered on their own, and added only if you accept that assignment. The daemon never adds them.
- **Same version assigned more than once:** when several assignments or requests (for example from two orgs) name the same version, they become one install. Their target tools are combined, and every one of them is tracked.
- **Pull never removes skills or tools:** even when the right version is already installed, `pull` adds tools an assignment now asks for. It never removes one. When an assignment or request stops asking for a tool (its tool list shrinks, or the origin disappears), `pull` reports the tool as no longer assigned, prints the `sfs skill compile <id> --scope <scope> --drop <tool>` command that removes it, and leaves it installed; the daemon logs the same. The report lists these under `unassigned_tools`, and they are reported until you drop the tool or it is assigned again. Tools you added by hand are never reported. An assignment with no tool list means every default tool (`--for`, otherwise all supported tools). Additions go through the normal compile, so consent is checked again.
- **Version updates need approval for every installed tool:** a new version is installed only if every tool the package is currently installed for is approved for it (an assignment or request covering that tool, with its capabilities, plus your `--allow-*` consent). Otherwise nothing is updated and the reason is reported, for example "2.0.0 is approved only for codex; claude-code still has 1.0.0 - request approval for claude-code or remove it with `sfs skill compile <id> --scope user --drop claude-code`". A tool never gets a version it was not approved for.
- **Service keys:** a service key can run `pull`. It gets project-scoped install requests (`--project ID` if the server cannot resolve the project from the git remote for a service key); user- and device-scoped requests belong to a person and are skipped. For users, `target_user_id` is only checked on user- and device-scoped requests.
- **Failed listings are not empty listings:** if assignments or install requests cannot be read (an HTTP error, network problem, or unresolvable project), that source is left alone. Nothing it installed is reported as no longer assigned. The note is printed before any prompt. The same applies when any row in a listing cannot be interpreted safely, for example a row that is not an object, a missing or null tool list, an assignment without a `required` or `optional` mode, or a request that is not `approved` or has no capability list. It also applies when the server reports that it left out records it could not read (`"incomplete": true` on the listing). It also applies when a paged listing cannot be read to its last page, and when an older, unpaged server returns exactly its 100-row limit, since rows may have been cut off. If the listing changes while it is being paged (a new `generation`, or the server's `listing_changed`), it is read again from the first page, at most twice. A pull's time limit or a cancel also stops the paging, which leaves that source incomplete. The valid rows still apply.
- **Tools this sfs doesn't support:** an assignment or request whose tool list names only tools this version of sfs doesn't support is skipped with a note, and its source counts as incomplete. It is never widened to all tools. If only some of its tools are unsupported, the supported ones are installed. An empty tool list still means every default tool.
- **Removed assignments:** `pull` and the daemon never uninstall a package because its assignment or approval disappeared. When a package that pull installed is no longer referenced by any assignment or request in a complete listing, `pull` lists it as no longer assigned and prints the `sfs skill remove <id> --scope <scope>` command that removes it. `sfs skill doctor` lists it too. If it is assigned again, it is no longer listed. Packages you installed yourself are never listed and never replaced. Pull ownership belongs to one server and account, and to one exact installation, identified by an immutable install id. If you remove a pulled package and add it again, or explicitly `sfs skill add` or `sfs skill remove` a package pull manages, pull no longer updates it or changes its tools. Every current assignment or request for an installed version is recorded, including optional ones and ones for packages you installed yourself. If a package is removed while pull is recording it, pull installs it again (or defers it with a note) rather than recording a reference to a missing package. Blocked packages are still quarantined automatically. A cancelled or timed-out pull stops before starting another quarantine move.
- **Interrupted installs:** an install that a crash or kill interrupted is settled at the start of the next `pull` (or daemon run), before any network work. If settling it would need a capability nobody durably consented to, it is left as it is and reported instead, for example: "An interrupted install of pkg_x (ver_y) could not be settled because consent for skill-scripts is missing; run `sfs skill pull --allow-scripts` to settle it". The daemon logs the same line. Running `pull` with the named flag settles it. The report lists these under `recovery`. The line is shown as soon as it is found, so it still appears (on stderr with `--json`) when the rest of the pull then fails, for example offline. Recovery only acts on an installation the interrupted pull itself last changed, undoes only what that interrupted pull itself changed (an origin counts as recorded only when that pull recorded it), puts back the exact earlier version (source, version and provenance, not just the same bytes) when it rolls an update back, and never removes an installation another account has recorded meanwhile; in a project it acts only in that project.
- **Elevated capabilities** (scripts, hooks, executable extensions, DLP findings) still need the usual `--allow-*` flags. They are also authorized **per tool**: a tool is installed only if an assignment or request covering that tool authorizes every capability it needs. An assignment authorizes its own tools; an install request authorizes only the capabilities approved on it, for its own tools. Approvals are never combined across requests. Excluded tools are listed with the reason.
- Running `pull` again is idempotent: content already installed is left alone. "Installed" means this version of this package from this server. Identical content installed from another package, server or source is reported as a conflict and not installed a second time.

### Revocations

`pull` (and the daemon) read `GET /api/v1/marketplace/revocations?since=<cursor>`. Servers without the feed (HTTP 404) are skipped quietly.

The feed only shows revocations the signed-in account can see, so `~/.sessionfs/skills/hosted.json` keeps a separate cursor and separate flags for each account (API URL + user id, or the key's fingerprint for a service key):

- **Switching accounts or profiles** starts the new account from its own cursor, never from another account's.
- **Full replay:** an account's first sync, and one sync every 24 hours, replays the whole feed and rebuilds that account's flags. This picks up older revocations that became visible after you joined an org, and drops ones you can no longer see. The feed is downloaded without holding the local state lock; the result is merged afterwards, and the sync starts again if another sync updated the same account in between.
- **Other accounts' blocks:** only another account's **global** blocks **on the same server** apply (owners and revocations are never mixed between servers). They are matched by content hash alone and shown without that account's version ids or reasons.
- **Reading to the end:** the feed is read until the server reports `has_more: false` (older servers: until an empty page). If a safety limit or the daemon's time limit is reached first, nothing is saved; the previous cursor and flags stay as they were.
- **Content blocks:** a block on a content hash (`kind: "content"`) is stored separately from version and package flags, per hash, block scope and org. Restoring the hash clears only that block, not a version-specific one. Unknown states and kinds from newer servers are ignored.
- **Org-scoped blocks:** an org-scoped entry (`block_scope: "org"` with `scope_org_id`), whether for a version, a package or content, applies only to installed copies that org owns **and** that were installed from a non-public listing. An org block never reaches a public copy. SessionFS records the listing, `owner_org_id` and listing `visibility` on each installed copy, written together with the install. The same version installed in two scopes from two listings (say, an org's private listing and the public one) is judged per copy, and downloading or failing to install another copy never changes how an existing one is judged. For older installs that have no such record, `pull` looks the values up from that version's metadata. Global blocks apply everywhere. When the owner or visibility is unknown (older server or install), blocks apply as before. An org or owner-scoped **content** block (by content hash) is kept on the device until an explicit `restored` event arrives, even after you leave the org and can no longer see its events; `sfs skill doctor` and `pull` keep reporting and enforcing it. Org or owner-scoped **version and package** events stay with the account that can see them: they are rebuilt on each full replay and are never imported by another profile on the device.
- **Owner-layer yanks:** a yank or restore by the publisher itself arrives as `block_scope: "owner"` (`scope_org_id` is the owning org, or null for a personal package). It applies only to copies installed from a non-public listing of that package and version, and only to that org's copies when `scope_org_id` is set, so a copy installed from the public listing of the same version is not affected. It stays with the account that can see it. After you leave the org, the next full replay drops it. Platform yanks (no scope) and global blocks are unchanged.
- **Layers:** a version or package has independent platform (global or no scope) and owner (org or owner scope, per org) states. A restore clears only its own layer, and the version counts as yanked or blocked while any layer is.
- **Global restores:** global content blocks (by content hash) are shared by every account on the device. For each content hash and server the latest event is kept, including restores. A content block that any account on the device has seen restored is lifted for every account, and an older block replayed later cannot bring it back. Version and package events stay with the account that can see them and are rebuilt on each full replay. So after you leave an org, its platform block of a private package lapses once you can no longer see it, instead of staying stuck. An older server's blocks and restores that carry no `kind` are matched by content hash and are also shared by every account, whether their scope is absent or global. Its kindless yanks are not shared.
- **Incomplete feed:** if the revocation feed cannot be read to its end, `pull` says so, keeps the previously known blocks and yanks, and still quarantines blocked packages.
- **New or rotated keys:** version and package blocks and yanks are kept per account. A credential this device has not seen yet, such as a rotated API key, is matched to its account with one lookup on the server. This happens before any hosted install, rollback or `sfs skill sync`, and on `pull`. While offline, it cannot be matched, because user and service keys look the same, so SessionFS never guesses. Only the content blocks shared by every account on the device apply then. `sfs skill list` and `sfs skill doctor` warn "Identity unknown … run `sfs skill pull` while online". A fresh hosted download always goes through the server, which refuses a blocked version anyway.

| State | What happens locally |
|---|---|
| `blocked` | Every file of the package, including ones you modified, is moved out of every tool directory into `~/.sessionfs/skills/quarantine/<package>/<timestamp>/`, so you can recover your edits. The notice includes the reason and the quarantine path. If a file cannot be moved, the package stays recorded as installed and stays in `skills.lock.json` marked as pending quarantine. It keeps being reported and retried. `sfs skill sync` leaves such a package alone, neither reinstalling nor removing it, and `sfs skill remove` refuses it. Only a later quarantine pass (`sfs skill pull` or the daemon) finishes it. A managed file swapped for a symlink is moved as the link itself (never followed), and its target is left untouched. It can never be installed again, even from the local cache. |
| `yanked` | The installed copy is kept. `sfs skill list` and `sfs skill doctor` show a warning, and new installs or rollbacks to that version are refused. |
| `restored` | The local flag is cleared. |

`sfs skill doctor` exits non-zero while blocked content is still active:

- A blocked package is still installed (for example, a project-scope install made before the block). Run `sfs skill pull` in that project.
- A quarantined file is back in a tool directory. Delete it by hand.

`sfs skill remove` also moves each file out of the tool directory before checking whether you modified it. An edit saved at the last moment is never deleted: modified files go back in place, and are reported as preserved.

Removal never deletes a file directly, and neither does any other command that takes a file out of a tool directory: `sfs skill update`, `rollback`, `sync`, and a `compile` that drops a target. Each file is moved out first and only then checked. A file you modified is put back (an update that would replace it stops instead), and an unchanged one goes to the trash. A file whose content would not change is left in place. Unmodified files are moved into a trash folder, `~/.sessionfs/skills/trash/<package>/<timestamp>/`, laid out like the tool directories they came from. A later removal deletes trash entries older than 7 days. `sfs skill doctor` shows how many files the trash holds and its size. To get a removed file back within that window, copy it from the trash. Moves into the trash and the quarantine are renames, so the file itself is moved. Only when the destination is on another filesystem is the file copied, and the copy is synced to disk before the removal is recorded. An interrupted removal puts every file back, wherever it had got to.

SessionFS never replaces a file it did not expect to find. Files are placed with an atomic rename that fails if the name is taken. On Linux and macOS this uses the system's no-replace rename; elsewhere it uses a hard link. If a file appeared at the destination meanwhile, a file being put back is saved beside it as `<name>.sfs-restored-<id>`, and an install stops without overwriting anything. On a filesystem that supports neither method, installs create the file exclusively. Moving a symlink there is refused, and the link is left where it is.

Every change to a tool directory goes through directory handles, opened one level at a time from your home or project directory without following symlinks. If a folder inside a tool directory is replaced by a symlink while a command runs, the command stops, and nothing is created or moved outside the tool directories. On a platform that cannot open folders this way, such as Windows, installing and removing skills is refused.

### Daemon

When cloud sync is enabled, the daemon runs the same reconcile every 15 minutes, backing off to 2 hours after repeated failures. It does less than `pull`:

- It quarantines blocked packages in user scope.
- It installs **required** assignments into user scope.
- It never prompts, never installs optional items, never uninstalls anything (it only quarantines blocked packages and logs packages that are no longer assigned), never grants an elevated capability (those items are logged as skipped), and has no project context.
- It runs in its own background thread, one run at a time, with a 10-minute time limit, so it never holds up session capture. Unfinished work is picked up on the next run.
- It changes skill files through the same locks and journal as `sfs skill` commands, so it never overlaps a command you are running.

## Billing and Tier Enforcement

When any cloud command receives a `403` response with an `upgrade_required` error, the CLI displays a friendly message indicating the required tier and a URL to upgrade:

```bash
$ sfs org create "Acme Corp" acme-corp

This feature requires the Team tier.
  Your tier: Free
  Upgrade: https://sessionfs.dev/pricing
```

This applies to all commands that interact with the cloud API, including `sfs org`, `sfs push`, `sfs handoff`, and `sfs sync`.
