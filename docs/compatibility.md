# Tool Compatibility

SessionFS captures sessions from eight AI coding tools. Four support bidirectional sync (capture and resume); four are capture-only.

## Bidirectional Tools

These tools support both capture and resume. You can start a session in any of them, capture it with SessionFS, and resume it in any other bidirectional tool.

| Tool | Capture | Resume | Storage Location |
|------|---------|--------|-----------------|
| Claude Code | Yes | Yes | `~/.claude/projects/` JSONL files |
| Codex CLI | Yes | Yes | `~/.codex/sessions/` rollout files + SQLite index |
| Gemini CLI | Yes | Yes | `~/.gemini/tmp/*/chats/` JSON sessions |
| Copilot CLI | Yes | Yes | `~/.copilot/session-state/` event files |

## Capture-Only Tools

These tools are captured by the daemon but do not support session injection (resume). Each has a specific technical reason.

| Tool | Capture | Resume | Reason |
|------|---------|--------|--------|
| Cursor IDE | Yes | No | Content-addressed hashing prevents session injection |
| Amp | Yes | No | Cloud-first architecture -- local files are a sync cache |
| Cline | Yes | No | VS Code extension state is too fragile for automated injection |
| Roo Code | Yes | No | VS Code extension state is too fragile for automated injection |

### Why not just write the files?

- **Cursor** stores sessions in a SQLite database (`state.vscdb`) that uses content-addressed hashing for integrity checks. Writing sessions directly would cause hash mismatches and corrupt the database.
- **Amp** syncs sessions from its cloud service to local JSON files. These files are a read cache, not the source of truth. Writing to them would be overwritten on the next sync.
- **Cline** and **Roo Code** store session state inside VS Code's `globalStorage` directory. This state is tightly coupled to the extension lifecycle -- modifying it while VS Code is running can cause data loss or extension crashes.

## What You Can Still Do With Capture-Only Sessions

Capture-only sessions are fully functional in SessionFS. You can:

- **Browse and inspect** them with `sfs list` and `sfs show`
- **Search** across them with `sfs search`
- **Export** them as markdown, `.sfs`, or other formats
- **Fork** them into new sessions with `sfs fork`
- **Push to cloud** and share with teammates via `sfs push`
- **Resume in any bidirectional tool** -- the session is portable even if the original tool is not

The only thing you cannot do is inject the session back into the original capture-only tool.

## Cross-Tool Resume Examples

```bash
# Cursor session -> resume in Claude Code
sfs resume ses_abc123 --in claude-code

# Amp session -> resume in Codex
sfs resume ses_def456 --in codex

# Cline session -> resume in Gemini CLI
sfs resume ses_ghi789 --in gemini

# Claude Code session -> resume in Copilot CLI
sfs resume ses_jkl012 --in copilot

# Codex session -> resume in Gemini CLI
sfs resume ses_mno345 --in gemini

# Any session -> hand off to a teammate
sfs push ses_abc123
# Teammate pulls and resumes in their preferred tool
sfs pull ses_abc123
sfs resume ses_abc123 --in codex
```

SessionFS converts between native formats automatically. Message roles, tool calls, thinking blocks, and workspace state are mapped across tools.
