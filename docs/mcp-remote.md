# SessionFS MCP Server

Use your past coding sessions as context in AI conversations.

## Local MCP (Recommended)

Works with Claude Code, Cursor, and Copilot CLI. Runs locally, no network latency, searches your local session index.

### Install

```bash
# Claude Code
sfs mcp install --for claude-code

# Cursor
sfs mcp install --for cursor

# Copilot CLI
sfs mcp install --for copilot
```

Restart your tool after installing. The MCP server starts automatically.

### Use it

In any conversation, ask about your past sessions:

> "Search my past sessions for authentication errors"

> "Have I seen this CORS error before?"

> "Show me the session where I worked on the database migration"

### Available tools

| Tool | What it does |
|------|-------------|
| `search_sessions` | Full-text search across all your sessions |
| `get_session_context` | Retrieve the full conversation from a session |
| `list_recent_sessions` | Browse your recent sessions |
| `find_related_sessions` | Find sessions that touched a file or hit an error |

## Remote MCP (Claude.ai Web)

A remote MCP server runs at `https://mcp.sessionfs.dev` for web-based clients.

### Setup

1. Push sessions to the cloud: `sfs sync`
2. Go to [claude.ai](https://claude.ai) → Settings → Connectors
3. Add MCP server: `https://mcp.sessionfs.dev`
4. Enter your API key when prompted (`sfs config show`)

### Known Limitations

Claude.ai's MCP connector has open bugs that affect all remote MCP servers, not just SessionFS:

- **Tools may not appear** — Claude.ai web sometimes skips `tools/list` after connecting ([anthropics/claude-ai-mcp#83](https://github.com/anthropics/claude-ai-mcp/issues/83))
- **Auth popup may not close** — The authorize window can stay open after approval ([anthropics/claude-code#30218](https://github.com/anthropics/claude-code/issues/30218))
- **Token may not be sent** — OAuth completes but Claude.ai never sends the Bearer token ([anthropics/claude-ai-mcp#62](https://github.com/anthropics/claude-ai-mcp/issues/62))

These are Anthropic-side bugs being tracked. The local MCP server (Claude Code, Cursor, Copilot) works reliably. We recommend using the local server until the Claude.ai connector stabilizes.

## Privacy

- Sessions are only accessible with your API key
- The remote MCP server is a stateless proxy — queries the SessionFS API on your behalf
- No session data is cached on the MCP server
