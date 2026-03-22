# SessionFS Session Format (.sfs) — Specification v0.1.0

## Overview

A `.sfs` session is a directory containing JSON files that represent a captured AI coding agent session. The format is a **canonical superset** of native formats from Claude Code, Codex CLI, and other AI coding tools — any native session can be losslessly converted into `.sfs`, and `.sfs` sessions can be resumed in their original tools.

Sessions are **append-only** — messages are never modified in place. Conflicts are resolved by appending both sides.

## Directory Structure

```
{session-id}/
├── manifest.json        # Required. Identity, provenance, stats, sync state.
├── messages.jsonl       # Required. Append-only conversation log.
├── workspace.json       # Optional. Git state, file refs, environment.
├── tools.json           # Optional. MCP servers, shell config, custom tools.
├── checkpoints/         # Optional. Named immutable snapshots.
│   └── {name}/          #   Each checkpoint is a copy of manifest + messages.
├── context/             # Optional. Compaction artifacts.
│   └── uncommitted.patch #  Workspace diffs, compressed history.
└── meta/                # Optional. Provenance and audit trail.
```

**Required files:** `manifest.json` and `messages.jsonl`.
**Optional files:** `workspace.json`, `tools.json`, and the `checkpoints/`, `context/`, `meta/` directories.

## Design Decisions

### Why a directory, not a single file?
- `messages.jsonl` can be appended without rewriting the entire session
- Large sessions (13+ MB observed in Claude Code) stay streamable
- `workspace.json` and `tools.json` can be captured independently
- Checkpoints are just subdirectories — no complex archive format needed

### Why JSONL for messages?
- Append-only: safe for concurrent access (copy-on-read for daemon watchers)
- Streamable: parse line-by-line without loading entire file
- Incremental: daemon can track byte offset and only parse new lines
- Matches both Claude Code and Codex native formats

### Why support both tree and flat message structures?
- Claude Code uses tree-structured messages (`parentUuid` for branching)
- Codex uses flat turn-based sequences (no branching)
- The `parent_msg_id` field is nullable — null means flat/sequential, non-null means tree

### Why a superset of native formats?
- Lossless conversion: no data loss when converting from native format
- Source-specific fields preserved in `metadata` objects
- Unknown content block types pass through via the `unknown` block type
- Tool names and inputs preserved verbatim — no normalization

---

## manifest.json

Session identity, provenance, and aggregate statistics.

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `sfs_version` | `string` | Yes | Format version. Currently `"0.1.0"`. |
| `session_id` | `string` | Yes | Globally unique session ID (UUID v4 or v7). |
| `title` | `string\|null` | No | Human-readable title. Derived from first user message. |
| `tags` | `string[]` | No | User-assigned tags for organization. Default `[]`. |
| `created_at` | `string` | Yes | ISO 8601 creation timestamp. |
| `updated_at` | `string\|null` | No | ISO 8601 last modification timestamp. |
| `source` | `object` | Yes | Provenance info. See [Source Info](#source-info). |
| `model` | `object\|null` | No | Primary model used. See [Model Info](#model-info). |
| `ownership` | `object\|null` | No | Owner and access control. See [Ownership Info](#ownership-info). |
| `stats` | `object\|null` | No | Aggregate statistics. See [Session Stats](#session-stats). |
| `sync` | `object\|null` | No | Cloud sync state. See [Sync State](#sync-state). |
| `parent_session_id` | `string\|null` | No | Parent session if forked/resumed from another. |
| `checkpoint_count` | `integer` | No | Number of checkpoints. Default `0`. |
| `sub_agents` | `object[]` | No | Sub-agent session references. Default `[]`. |
| `metadata` | `object` | No | Extensible key-value pairs. Default `{}`. |

### Source Info

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `tool` | `string` | Yes | Source tool: `"claude-code"`, `"codex"`, `"cursor"`, `"aider"`, etc. |
| `tool_version` | `string\|null` | No | Source tool version. Example: `"2.1.59"`. |
| `sfs_converter_version` | `string\|null` | No | SessionFS converter version. |
| `original_session_id` | `string\|null` | No | Session ID in the source tool's format. |
| `original_path` | `string\|null` | No | Filesystem path to the original session file. |
| `interface` | `string\|null` | No | How the session was initiated: `"cli"`, `"vscode"`, `"web"`, `"exec"`. |

### Model Info

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `provider` | `string` | Yes | Provider name: `"anthropic"`, `"openai"`. |
| `model_id` | `string` | Yes | Model identifier: `"claude-opus-4-6"`, `"o4-mini"`. |
| `reasoning_effort` | `string\|null` | No | Reasoning effort: `"low"`, `"medium"`, `"high"`. |

### Ownership Info

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `owner` | `string\|null` | No | Username or email of the session owner. |
| `team` | `string\|null` | No | Team identifier. |
| `collaborators` | `object[]` | No | Users with access: `[{"user": "...", "access": "read\|write\|admin"}]`. |

### Session Stats

| Field | Type | Description |
|-------|------|-------------|
| `message_count` | `integer` | Total messages. |
| `turn_count` | `integer` | Number of user→assistant turn pairs. |
| `tool_use_count` | `integer` | Number of tool invocations. |
| `total_input_tokens` | `integer` | Cumulative input tokens. |
| `total_output_tokens` | `integer` | Cumulative output tokens. |
| `estimated_cost_usd` | `number\|null` | Estimated API cost in USD. |
| `duration_ms` | `integer\|null` | Session duration in milliseconds. |

### Sync State

| Field | Type | Description |
|-------|------|-------------|
| `last_sync_at` | `string\|null` | ISO 8601 last sync timestamp. |
| `etag` | `string\|null` | Server ETag for optimistic concurrency. |
| `dirty` | `boolean` | True if local changes not yet synced. |
| `remote_url` | `string\|null` | URL on the SessionFS server. |

### Sub-Agent Reference

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | `string` | Sub-agent identifier. |
| `model` | `string\|null` | Model used by this sub-agent. |
| `message_count` | `integer` | Number of messages from this sub-agent. |

---

## messages.jsonl

Append-only message log. Each line is a JSON object representing one message.

### Message Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `msg_id` | `string` | Yes | Unique message ID (UUID). |
| `parent_msg_id` | `string\|null` | No | Parent message for tree structures. Null for flat sequences or root. |
| `role` | `string` | Yes | Author role: `"user"`, `"assistant"`, `"system"`, `"developer"`, `"tool"`. |
| `content` | `object[]` | Yes | Content blocks (min 1). See [Content Blocks](#content-blocks). |
| `timestamp` | `string` | Yes | ISO 8601 timestamp. |
| `turn_id` | `string\|null` | No | Groups messages into turns. |
| `model` | `string\|null` | No | Model ID (assistant messages). Example: `"claude-opus-4-6"`. |
| `provider` | `string\|null` | No | Provider name. Example: `"anthropic"`. |
| `stop_reason` | `string\|null` | No | Why model stopped: `"end_turn"`, `"tool_use"`, `"max_tokens"`, `"interrupted"`. |
| `usage` | `object\|null` | No | Token usage. See [Token Usage](#token-usage). |
| `is_sidechain` | `boolean` | No | True for sub-agent messages. Default `false`. |
| `is_meta` | `boolean` | No | True for system-injected messages. Default `false`. |
| `agent_id` | `string\|null` | No | Sub-agent identifier. |
| `metadata` | `object` | No | Extensible key-value pairs. Default `{}`. |

### Roles

| Role | Description | Source Mapping |
|------|-------------|---------------|
| `user` | Human user message | CC: `type: "user"`, Codex: `event_msg.user_message` |
| `assistant` | Model response | CC: `type: "assistant"`, Codex: `response_item.message role=assistant` |
| `system` | System-level events, summaries | CC: `type: "summary"` / `type: "system"` |
| `developer` | Tool-injected context (permissions, env) | Codex: `response_item.message role=developer` |
| `tool` | Tool execution results | CC: user messages containing `tool_result` blocks |

### Content Blocks

Each message contains an array of content blocks. The `type` field discriminates the variant.

#### `text`

Plain text content.

```json
{"type": "text", "text": "Hello, world!"}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"text"` | Yes | Block type discriminator. |
| `text` | `string` | Yes | The text content. |

#### `thinking`

Model reasoning/thinking content. May include a cryptographic signature for authenticity verification.

```json
{
  "type": "thinking",
  "text": "I need to read the file first...",
  "signature": "abc123...",
  "redacted": false,
  "summary": null
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"thinking"` | Yes | Block type discriminator. |
| `text` | `string` | Yes | Thinking content. Empty if redacted. |
| `signature` | `string\|null` | No | Cryptographic signature (Anthropic API). Preserve as-is. |
| `redacted` | `boolean` | No | True if content was encrypted/unavailable. Default `false`. |
| `summary` | `string\|null` | No | Plaintext summary if available (Codex reasoning). |

**Source mapping:**
- Claude Code: `thinking` blocks with `signature` field
- Codex: `reasoning` blocks → `redacted: true` if encrypted, `summary` from reasoning summaries

#### `tool_use`

Model requests a tool invocation.

```json
{
  "type": "tool_use",
  "tool_use_id": "toolu_abc123",
  "name": "Read",
  "input": {"file_path": "src/main.py"}
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"tool_use"` | Yes | Block type discriminator. |
| `tool_use_id` | `string` | Yes | Unique ID for matching with `tool_result`. |
| `name` | `string` | Yes | Tool name. |
| `input` | `object` | Yes | Tool input parameters. Structure varies by tool. |

**Source mapping:**
- Claude Code: `tool_use` content blocks → mapped directly
- Codex: `local_shell_call` → `name: "shell"`, `input: {"command": [...]}`, `function_call` → `name` + parsed `arguments`

#### `tool_result`

Result of a tool execution.

```json
{
  "type": "tool_result",
  "tool_use_id": "toolu_abc123",
  "content": "file contents here...",
  "is_error": false,
  "exit_code": 0,
  "duration_ms": 150
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"tool_result"` | Yes | Block type discriminator. |
| `tool_use_id` | `string` | Yes | ID of the corresponding `tool_use`. |
| `content` | `string` | No | Tool output text. Default `""`. |
| `is_error` | `boolean` | No | True if execution failed. Default `false`. |
| `exit_code` | `integer\|null` | No | Process exit code (shell tools). |
| `duration_ms` | `number\|null` | No | Execution time in milliseconds. |

#### `image`

Image content (input or output).

```json
{
  "type": "image",
  "source": {
    "type": "url",
    "data": "https://example.com/screenshot.png",
    "media_type": "image/png"
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"image"` | Yes | Block type discriminator. |
| `source.type` | `string` | Yes | `"url"`, `"base64"`, or `"path"` (workspace-relative). |
| `source.data` | `string` | Yes | URL, base64 data, or file path. |
| `source.media_type` | `string\|null` | No | MIME type (e.g., `"image/png"`). |

#### `summary`

Compacted conversation history summary.

```json
{
  "type": "summary",
  "text": "The user asked about authentication and we implemented JWT tokens.",
  "summarized_through_msg_id": "msg-042"
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `type` | `"summary"` | Yes | Block type discriminator. |
| `text` | `string` | Yes | Summary text. |
| `summarized_through_msg_id` | `string\|null` | No | Last message ID included in this summary. |

#### Unknown blocks

Content block types not in the spec are preserved as-is. The `type` field must not match any known type. All additional fields are preserved.

```json
{"type": "custom_viz", "chart_data": [...], "format": "svg"}
```

### Token Usage

| Field | Type | Description |
|-------|------|-------------|
| `input_tokens` | `integer` | Input tokens. Default `0`. |
| `output_tokens` | `integer` | Output tokens. Default `0`. |
| `cache_read_tokens` | `integer` | Tokens read from cache. Default `0`. |
| `cache_write_tokens` | `integer` | Tokens written to cache. Default `0`. |
| `reasoning_tokens` | `integer` | Reasoning/thinking output tokens. Default `0`. |

---

## workspace.json

Workspace context at session time. All file paths are relative to `root_path`.

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `root_path` | `string` | Yes | Absolute workspace root path. |
| `git` | `object\|null` | No | Git context. See [Git Context](#git-context). |
| `files` | `object[]` | No | File references. See [File Ref](#file-ref). Default `[]`. |
| `environment` | `object\|null` | No | Runtime environment. See [Environment](#environment). |
| `metadata` | `object` | No | Extensible. Default `{}`. |

### Git Context

| Field | Type | Description |
|-------|------|-------------|
| `remote_url` | `string\|null` | Git remote origin URL. |
| `branch` | `string\|null` | Active branch. |
| `commit_sha` | `string\|null` | HEAD commit SHA. |
| `commit_message` | `string\|null` | HEAD commit message (first line). |
| `dirty` | `boolean` | True if working tree had uncommitted changes. Default `false`. |
| `diff_path` | `string\|null` | Relative path to unified diff file (when dirty). |

**Non-git workspaces:** When the workspace is not a git repository, omit the `git` field entirely. File references can still be captured using SHA-256 hashes for validation.

### File Ref

| Field | Type | Description |
|-------|------|-------------|
| `path` | `string` | Workspace-relative file path. |
| `sha256` | `string\|null` | SHA-256 hash for content validation. |
| `size_bytes` | `integer\|null` | File size in bytes. |
| `last_modified` | `string\|null` | ISO 8601 modification time. |
| `role` | `string` | How the file was used: `"read"`, `"written"`, `"edited"`, `"created"`, `"deleted"`, `"referenced"`. Default `"referenced"`. |

### Environment

| Field | Type | Description |
|-------|------|-------------|
| `os` | `string\|null` | OS identifier: `"darwin"`, `"linux"`, `"windows"`. |
| `os_version` | `string\|null` | OS version string. |
| `shell` | `string\|null` | Default shell: `"bash"`, `"zsh"`, `"fish"`. |
| `languages` | `object` | Runtime versions: `{"python": "3.12.1", "node": "20.11.0"}`. |
| `venv_path` | `string\|null` | Virtual environment path (relative to workspace root). |

---

## tools.json

Tool configurations available during the session.

### Fields

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `mcp_servers` | `object[]` | No | MCP server connections. Default `[]`. |
| `shell` | `object\|null` | No | Shell execution config. |
| `custom_tools` | `object[]` | No | Custom tool definitions. Default `[]`. |
| `tools_used` | `string[]` | No | Names of tools actually invoked. Default `[]`. |
| `metadata` | `object` | No | Extensible. Default `{}`. |

### MCP Server

| Field | Type | Description |
|-------|------|-------------|
| `name` | `string` | Server name. |
| `uri` | `string\|null` | Connection URI (credentials redacted). |
| `transport` | `string\|null` | Transport: `"stdio"`, `"sse"`, `"http"`. |
| `tools_provided` | `string[]` | Tool names this server provides. |
| `auth_ref` | `string\|null` | Auth reference (NOT the credential): `"env:GITHUB_TOKEN"`. |

### Shell Config

| Field | Type | Description |
|-------|------|-------------|
| `default_shell` | `string\|null` | Shell for command execution. |
| `working_directory` | `string\|null` | Default working directory. |
| `sandbox_policy` | `string\|null` | Sandbox restrictions: `"read-only"`, `"unrestricted"`. |
| `approval_policy` | `string\|null` | Approval mode: `"never"`, `"on-request"`, `"always"`. |

### Custom Tool

| Field | Type | Description |
|-------|------|-------------|
| `name` | `string` | Tool name. |
| `description` | `string\|null` | Human-readable description. |
| `parameters_schema` | `object\|null` | JSON Schema for input parameters. |
| `source` | `string\|null` | Origin: `"mcp:filesystem"`, `"builtin"`, `"skill:openai-docs"`. |

---

## Extensibility Model

### Unknown fields
Every schema object includes a `metadata` property (type `object`, `additionalProperties: true`) for source-specific data that doesn't fit the canonical schema. Converters should preserve unknown fields here rather than dropping them.

### Unknown content block types
The message schema's content block union includes an `unknown` variant that matches any `type` value not in the known set. This allows new block types to be added by source tools without breaking validation.

### Version migration
The `sfs_version` field in `manifest.json` enables future migrations. When the spec changes:
1. Bump `sfs_version`
2. Write a migration function that transforms old format → new format
3. Readers should check `sfs_version` and apply migrations if needed

---

## Security Considerations

### No credentials in .sfs files
- `tools.json` stores auth **references** (`"env:GITHUB_TOKEN"`), never actual credentials
- LLM API keys are never stored — all LLM calls are client-side
- MCP server URIs should have credentials redacted

### Thinking block signatures
- Anthropic API thinking blocks include cryptographic signatures proving authenticity
- These must be preserved verbatim — never fabricated
- Codex reasoning may be encrypted — preserve `encrypted_content` as opaque data

### File path security
- All file paths in `workspace.json` are relative to workspace root
- Absolute paths must never appear in file references (only in `root_path`)
- Converters must strip absolute paths from tool inputs

---

## Conversion Reference

### Claude Code → .sfs

| CC Field | .sfs Field |
|----------|-----------|
| `uuid` | `msg_id` |
| `parentUuid` | `parent_msg_id` |
| `type: "user"` | `role: "user"` |
| `type: "assistant"` | `role: "assistant"` |
| `type: "summary"` | `role: "system"`, content: `summary` block |
| `message.content[].type: "text"` | `content[].type: "text"` |
| `message.content[].type: "thinking"` | `content[].type: "thinking"` |
| `message.content[].type: "tool_use"` | `content[].type: "tool_use"` |
| User message with `tool_result` blocks | `role: "tool"` |
| `message.model` | `model` |
| `message.usage` | `usage` (field name mapping) |
| `isSidechain` | `is_sidechain` |
| `isMeta` | `is_meta` |
| `cwd` | `workspace.root_path` (session-level), `metadata.cc_cwd` (per-message) |
| `gitBranch` | `workspace.git.branch` |
| `version` | `source.tool_version` |
| `sessionId` | `source.original_session_id` |

### Codex CLI → .sfs

| Codex Field | .sfs Field |
|-------------|-----------|
| `session_meta.id` | `source.original_session_id` |
| `session_meta.cwd` | `workspace.root_path` |
| `session_meta.cli_version` | `source.tool_version` |
| `session_meta.source` | `source.interface` |
| `session_meta.git.branch` | `workspace.git.branch` |
| `session_meta.git.commit_hash` | `workspace.git.commit_sha` |
| `session_meta.base_instructions` | `metadata.system_prompt` (on first system message) |
| `event_msg.user_message` | `role: "user"`, `content: [text]` |
| `event_msg.agent_message` | `role: "assistant"`, `content: [text]` |
| `response_item.message role=developer` | `role: "developer"` |
| `response_item.reasoning` | `content[].type: "thinking"`, `redacted: true` if encrypted |
| `response_item.local_shell_call` | `content[].type: "tool_use"`, `name: "shell"` |
| `response_item.function_call` | `content[].type: "tool_use"` |
| `response_item.function_call_output` | `content[].type: "tool_result"` |
| `turn_context.model` | `model` |
| `event_msg.token_count` | `usage` |
