# Quickstart

**Time:** Under 2 minutes.
**Prerequisites:** Python 3.10+

## 1. Install

```bash
pip install sessionfs
```

This installs two commands: `sfs` (CLI) and `sfsd` (daemon).

## 2. Start the Daemon

```bash
sfs daemon start
```

The daemon watches your AI tools in the background using filesystem events (not polling). Negligible CPU overhead.

It automatically detects and captures sessions from all eight supported tools:

- Claude Code
- Codex CLI
- Gemini CLI
- Copilot CLI
- Cursor IDE
- Amp
- Cline
- Roo Code

## 3. Use Your Tools Normally

No behavior change required. Just use your AI coding tools the way you always do. SessionFS captures sessions silently in the background.

## 4. Browse Your Sessions

```bash
sfs list
```

```
                     Sessions (12)
┌──────────────┬─────────────┬────────┬──────────┬───────────────────────┐
│ ID           │ Tool        │ Model  │ Messages │ Title                 │
├──────────────┼─────────────┼────────┼──────────┼───────────────────────┤
│ ses_a1b2c3d4 │ claude-code │ opus-4 │       47 │ Debug auth flow       │
│ ses_e5f6a7b8 │ gemini-cli  │ gem-2  │       23 │ Add rate limiting     │
│ ses_c9d0e1f2 │ codex-cli   │ codex  │       31 │ Refactor DB schema    │
│ ses_g3h4i5j6 │ cursor      │ son4.5 │        8 │ Fix CI pipeline       │
└──────────────┴─────────────┴────────┴──────────┴───────────────────────┘
```

Filter and sort:

```bash
sfs list --since 24h
sfs list --tool claude-code --sort tokens
```

## 5. Resume a Session

Resume in the same tool or a different one:

```bash
# Resume in Claude Code (default)
sfs resume ses_a1b2c3d4

# Resume a Cursor session in Codex
sfs resume ses_g3h4i5j6 --in codex

# Resume any session in Gemini CLI
sfs resume ses_e5f6a7b8 --in gemini
```

Four tools support resume: Claude Code, Codex, Gemini CLI, and Copilot CLI. Sessions from capture-only tools (Cursor, Amp, Cline, Roo Code) can be resumed in any of the four bidirectional tools. See [Compatibility](compatibility.md) for details.

---

## Troubleshooting

**"No sessions found" after `sfs list`**

Make sure the daemon is running and at least one AI tool has been used:

```bash
sfs daemon status
```

If the daemon shows 0 sessions, try importing existing sessions:

```bash
sfs import --from claude-code
```

**Daemon won't start**

Check the logs:

```bash
sfs daemon logs
```

Common causes:
- Another `sfsd` process is already running (`ps aux | grep sfsd`)
- The `~/.sessionfs/` directory doesn't exist or isn't writable

**Daemon is running but not detecting sessions**

```bash
sfs daemon status
```

If a watcher shows `degraded` or `broken`, check `sfs daemon logs --lines 100`.

## What's Next

Once you're capturing sessions, explore these features:

```bash
# Search across all sessions
sfs search "rate limiting"

# Get a quick summary of any session
sfs summary ses_abc

# Audit a session for hallucinations
sfs audit ses_abc --model gpt-4o

# Enable automatic cloud sync
sfs auth login
sfs sync auto --mode all

# Share project context with your team
sfs project init
sfs project edit

# Keep AI instructions consistent across tools (CLAUDE.md, codex.md, .cursorrules, …)
sfs rules init
sfs rules compile

# Hand off a session to a teammate
sfs handoff ses_abc --to colleague@company.com

# Install MCP so AI agents remember past sessions
sfs mcp install --for claude-code
```

See the full [CLI Reference](cli-reference.md) for all commands and options.
