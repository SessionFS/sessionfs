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

The MCP server exposes 36 tools across five categories (sessions, knowledge read/write, rules, personas, tickets). Full parameter reference at [sessionfs.dev/docs/mcp/](https://sessionfs.dev/docs/mcp/).

**Session tools**

| Tool | What it does |
|------|-------------|
| `search_sessions` | Full-text search across all your sessions |
| `get_session_context` | Retrieve the full conversation from a session |
| `list_recent_sessions` | Browse your recent sessions |
| `find_related_sessions` | Find sessions that touched a file or hit an error |
| `get_session_summary` | Structured summary of a session |
| `get_audit_report` | LLM Judge findings for a session |
| `get_session_provenance` | Rules version, hash, source, and instruction artifacts that shaped a session |

**Knowledge (read)**

| Tool | What it does |
|------|-------------|
| `get_project_context` | Full compiled wiki: overview + pages + concepts |
| `get_context_section` | One section of the project context document (cheaper than the full doc) |
| `get_wiki_page` | One wiki page's content plus backlinks |
| `search_project_knowledge` | Search knowledge entries by query |
| `list_knowledge_entries` | Filtered list of entries (type, claim class, freshness, session, dismissed) with pagination |
| `get_knowledge_entry` | One entry's full record, including `last_relevant_at` |
| `get_knowledge_health` | Pending / compiled / dismissed counts, stale and low-confidence flags, recommendations |
| `ask_project` | Q&A against the knowledge base and recent sessions |

**Knowledge (write)**

| Tool | What it does |
|------|-------------|
| `add_knowledge` | Contribute a discovery during a session (claim / evidence / note) |
| `update_wiki_page` | Create or update a wiki page |
| `list_wiki_pages` | Browse the wiki structure |
| `compile_knowledge_base` | Trigger a compile pass; returns counts of entries compiled and pages updated |
| `dismiss_knowledge_entry` | Retire a wrong/stale entry with audited reason; idempotent; `undismiss=true` reverses |

**Rules (read)**

| Tool | What it does |
|------|-------------|
| `get_rules` | Canonical project rules and compilation config |
| `get_compiled_rules` | Compiled rule text for a tool (CLAUDE.md / codex.md / .cursorrules / copilot-instructions.md / GEMINI.md) |

**Personas** (v0.10.1)

| Tool | What it does |
|------|-------------|
| `list_personas` | List active agent personas for the project |
| `get_persona` | Full content + role + specializations for one persona |
| `create_persona` | Create a new persona (ASCII name 1-50 chars, role, content, specializations) |
| `assume_persona` | Declare you are working as a persona without a ticket — writes a persona-only provenance bundle |
| `forget_persona` | Clear the local persona bundle written by `assume_persona` |

**Tickets** (v0.10.1)

| Tool | What it does |
|------|-------------|
| `list_tickets` | List tickets with optional `assigned_to` / `status` / `priority` filters |
| `get_ticket` | Full ticket detail including dependencies and comments |
| `create_ticket` | Create a new ticket (human or agent source; FSM-validated) |
| `start_ticket` | Atomic open→in_progress transition; returns compiled persona+ticket context sized to the target tool; writes local provenance bundle |
| `complete_ticket` | Atomic in_progress→review transition with completion notes + changed files; clears the local bundle iff owned |
| `resolve_ticket` | Atomic review→done transition; runs dependency enrichment + auto-unblock for dependents |
| `assign_persona` | Set or change `ticket.assigned_to` (FSM state unchanged) |
| `escalate_ticket` | Bump priority one level (low → medium → high → critical); optional rationale posted as a comment |
| `add_ticket_comment` | Slack-like comment with optional persona attribution |

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
