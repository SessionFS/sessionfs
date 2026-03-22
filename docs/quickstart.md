# Quickstart: Capture Your First Session

**Time:** ~3 minutes
**Prerequisites:** Python 3.10+, at least one supported AI coding tool (Claude Code, Codex, Gemini CLI, or Cursor)

## Step 1: Install SessionFS (30 seconds)

```bash
pip install sessionfs
```

Verify the install:

```bash
sfs --help
```

Expected output:

```
Usage: sfs [OPTIONS] COMMAND [ARGS]...

  SessionFS — Portable AI coding sessions.

Options:
  --help  Show this message and exit.

Commands:
  checkpoint  Create a named checkpoint of a session's current state.
  config      Manage SessionFS configuration.
  daemon      Manage the SessionFS daemon.
  export      Export a session.
  fork        Fork a session into a new independent session.
  import      Import sessions from external sources.
  list        List captured sessions.
  resume      Resume a session in Claude Code.
  show        Show session details.
```

## Step 2: Start the Daemon (10 seconds)

```bash
sfs daemon start
```

Expected output:

```
Daemon started (PID 12345).
Logs: /Users/you/.sessionfs/daemon.log
```

Check that it's running:

```bash
sfs daemon status
```

Expected output:

```
         SessionFS Daemon Status
┌──────────────────┬────────────────────────┐
│ Field            │ Value                  │
├──────────────────┼────────────────────────┤
│ PID              │ 12345                  │
│ Running          │ Yes                    │
│ Sessions         │ 0                      │
│ Watcher: cc      │ healthy (0 sessions)   │
└──────────────────┴────────────────────────┘
```

The daemon is now watching your Claude Code sessions directory. It uses filesystem events (not polling) so there's negligible CPU overhead.

## Step 3: Use Claude Code Normally

Just use Claude Code as you always do. SessionFS captures sessions in the background — no changes to your workflow.

If you don't have any recent Claude Code sessions, start one now:

```bash
claude "What is the capital of France?"
```

Wait a few seconds for the daemon to pick up the session.

## Step 4: Browse Your Sessions (10 seconds)

```bash
sfs list
```

Expected output:

```
                       Sessions (3)
┌──────────────┬─────────────┬────────┬──────────┬───────────┐
│ ID           │ Tool        │ Model  │ Messages │ Title     │
├──────────────┼─────────────┼────────┼──────────┼───────────┤
│ a1b2c3d4e5f6 │ claude-code │ opus-4 │       23 │ Debug ... │
│ f6e5d4c3b2a1 │ claude-code │ son4.5 │        8 │ Add fe... │
│ 112233445566 │ claude-code │ opus-4 │        2 │ Quick ... │
└──────────────┴─────────────┴────────┴──────────┴───────────┘
```

Filter and sort:

```bash
# Sessions from the last 24 hours
sfs list --since 24h

# Only Claude Code sessions, sorted by token count
sfs list --tool claude-code --sort tokens
```

## Step 5: Inspect a Session

```bash
sfs show a1b2c3d4e5f6
```

Expected output:

```
╭──────────── Session Details ────────────╮
│ Session ID: a1b2c3d4-e5f6-...          │
│ Title: Debug auth flow                  │
│ Tool: claude-code 1.0.23               │
│ Model: claude-opus-4 (anthropic)       │
│ Created: 2026-03-20T14:30:00           │
│                                         │
│ Messages: 23                            │
│ Turns: 12                               │
│ Tool uses: 8                            │
│ Input tokens: 34,200                    │
│ Output tokens: 12,800                   │
╰─────────────────────────────────────────╯
```

View the conversation:

```bash
sfs show a1b2c3d4e5f6 --messages
```

See cost estimates:

```bash
sfs show a1b2c3d4e5f6 --cost
```

## Step 6: Import Existing Sessions (Optional)

If you have existing Claude Code sessions from before you installed SessionFS:

```bash
sfs import --from claude-code
```

Expected output:

```
Found 47 Claude Code session(s).
Imported 47 new session(s).
```

## What's Next

- **Resume a session on another machine:**
  ```bash
  sfs resume <session_id> --project /path/to/project
  ```

- **Export as markdown:**
  ```bash
  sfs export <session_id> --format markdown
  ```

- **Fork a session to try a different approach:**
  ```bash
  sfs fork <session_id> --name "Alternative approach"
  ```

- **Enable cloud sync:** See the [Sync Guide](sync-guide.md)

- **Hand off to a teammate:** Push a session with `sfs push`, have your teammate `sfs pull` it

## Troubleshooting

**"No sessions found" after `sfs list`**

Make sure Claude Code has been used at least once and the daemon is running:
```bash
sfs daemon status
```

If the daemon shows 0 sessions, try importing existing sessions:
```bash
sfs import --from claude-code
```

**"Daemon not running" or daemon won't start**

Check the logs for errors:
```bash
sfs daemon logs
```

Common causes:
- Another `sfsd` process is already running (check with `ps aux | grep sfsd`)
- The `~/.sessionfs/` directory doesn't exist or isn't writable

**"Permission denied" errors**

Ensure the SessionFS data directory has correct permissions:
```bash
chmod 700 ~/.sessionfs
```

**Daemon is running but not detecting sessions**

Check watcher health:
```bash
sfs daemon status
```

If a watcher shows `degraded` or `broken`, check the daemon logs:
```bash
sfs daemon logs --lines 100
```
