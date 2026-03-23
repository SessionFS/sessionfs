# Connecting SessionFS to Claude.ai

Use your past coding sessions as context in any Claude.ai conversation.

## Setup (2 minutes)

### 1. Push sessions to the cloud

```bash
sfs sync
```

### 2. Add SessionFS to Claude.ai

1. Go to [claude.ai](https://claude.ai) → Settings → Connectors
2. Click "Add MCP Server"
3. Enter the server URL: `https://mcp.sessionfs.dev/sse`
4. When prompted for authentication, enter your SessionFS API key

Find your key with:
```bash
sfs config show
```

### 3. Start using it

In any Claude.ai conversation, ask about your past sessions:

> "Search my past sessions for authentication errors"

> "Have I seen this CORS error before?"

> "Show me the session where I worked on the database migration"

## What it can do

| Tool | What it does |
|------|-------------|
| `search_sessions` | Full-text search across all your sessions |
| `get_session_context` | Retrieve the full conversation from a session |
| `list_recent_sessions` | Browse your recent sessions |
| `find_related_sessions` | Find sessions that touched a file or hit an error |

## Also works with

- **Claude Code** — `sfs mcp install --for claude-code`
- **Cursor** — `sfs mcp install --for cursor`
- **Copilot CLI** — `sfs mcp install --for copilot`

These use the local MCP server (stdio). The remote server at `mcp.sessionfs.dev` is for web-based clients like Claude.ai.

## Privacy

- Sessions are only accessible with your API key
- The MCP server is a proxy — it queries the SessionFS API on your behalf
- No session data is cached on the MCP server
- Scale-to-zero: the server shuts down when not in use
