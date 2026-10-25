# Session helper

*Enterprise feature `session_helper`. Requires a BYOK model key.*

The session helper answers questions over the sessions and project knowledge you
can already read — "find the session where we fixed the SSO redirect", "what did
Alice decide about Redis and why?", "summarize these three sessions". It is an
agent with tools, not a chat over a single summary, and every factual answer
cites the session ids (and message ids) it drew from.

It sees exactly what you can read today: your own sessions, projects you can
access, and org membership. An org admin does not gain a wider view through it.

## Requirements

- **Enterprise plan.** Every helper route enforces the `session_helper` feature
  against the conversation's plan (the project's org for a project-scoped turn,
  otherwise your own plan). Other tiers receive `upgrade_required`.
- **Your own key.** The model call always uses a BYOK key — never a
  SessionFS-owned key. Resolution order per request:
  1. your own judge settings (`UserJudgeSettings`; in the dashboard under
     **Settings → Data protection → Security review model**);
  2. the org-wide helper key of the conversation's org, when enabled and you are
     a member;
  3. otherwise HTTP 409 `helper_key_required`.

  Responses and audit rows record which source was used (`user` | `org`), never
  the key itself.

## Where to use it

- **Dashboard** — **Ask the helper** in the sidebar or the Cmd+K palette opens
  a side drawer; the same button on a project or session page scopes the
  question to it. Answers stream in with links to the cited sessions, and
  earlier threads are listed under **Conversations** (with **New** to start one).
- **CLI** — `sfs helper ask`, `sfs helper conversations list|show|delete` and
  `sfs helper status` (see the [CLI reference](cli-reference.md#sfs-helper-enterprise-byok)).
- **MCP** — the `ask_session_helper` tool (below).

## API

Auth is **user keys only** — service keys are rejected with
`service_key_not_allowed`. At most **2 in-flight turns per user** are allowed
(409 `helper_busy`). The in-flight limit is **per process**: each replica
counts its own slots, so a horizontally scaled deployment allows up to
`2 × replicas` concurrent turns per user.

### `POST /api/v1/helper/turns`

Run one turn. Request body:

```json
{
  "conversation_id": "hc_…",       // optional; omit to start a new thread
  "message": "…",
  "scope": {                       // optional; narrows the tools
    "project_id": "…",             // optional
    "session_ids": ["…"]           // optional, up to 100
  },
  "stream": true                   // default true
}
```

With `stream=true` (the default) or `Accept: text/event-stream`, the response is
Server-Sent Events (`text/event-stream`, `Cache-Control: no-store`,
`X-Accel-Buffering: no`). Events:

| event | data |
|---|---|
| `conversation` | `{conversation_id}` — always first |
| `text` | `{delta}` |
| `tool_call` | `{id, name, args}` — args sanitized |
| `tool_result` | `{id, name, ok, summary}` — a short summary, not raw excerpt text |
| `citation` | `{session_id, message_index, title}` |
| `done` | `{stop_reason, input_tokens, output_tokens, key_source}` |
| `error` | `{code, message}` — the stream then ends |

A `: ping` comment is sent every 15 s so proxies do not idle the connection
out. If the client disconnects, the model call is cancelled (no orphan spend)
and the partial answer is stored with `stop_reason="client_disconnected"`.

With `stream=false`, the response is a single JSON document:

```json
{
  "conversation_id": "hc_…",
  "answer": "…",
  "citations": [{"session_id": "…", "message_index": 0, "title": "…"}],
  "tool_calls": [{"id": "…", "name": "…", "args": {}}],
  "usage": {"input_tokens": 0, "output_tokens": 0},
  "stop_reason": "stop",
  "key_source": "user"
}
```

Provider failures return HTTP 502 (`provider_error`) with a sanitized message —
never the key.

### `GET /api/v1/helper/conversations`

List your threads (newest first): `[{id, org_id, title, created_at, updated_at}]`.

### `GET /api/v1/helper/conversations/{id}`

One thread with its ordered `messages` array. A thread you do not own is a 404.

### `DELETE /api/v1/helper/conversations/{id}`

Hard-delete the thread and its messages → `{"deleted": true}`.

### `GET /api/v1/helper/status`

```json
{"enabled": true, "key_source": "user", "provider": "openai", "model": "gpt-4o"}
```

`enabled` is false when your plan does not include the helper; `key_source` is
null when no key is configured — surfaces can show an "add a key" state before
the first question.

## MCP tool

`ask_session_helper` (exposed by `sfs mcp serve`) wraps `POST /api/v1/helper/turns`
with `stream=false` and returns the JSON answer, so agents can ask a question
without shelling out to `sfs helper ask`.

| argument | type | notes |
|---|---|---|
| `question` | string | required; non-empty, at most 20,000 characters (the helper route limit) |
| `project` | string | optional project id (`proj_…`) forwarded as `scope.project_id` |
| `session_ids` | array of strings | optional; at most 100 ids, blank entries dropped |
| `conversation_id` | string | optional; continue an existing thread (`hc_…`) |

The tool authenticates with your user key (it never resolves a git remote), and
its HTTP client uses a 600-second timeout so a long turn isn't cut off.

## Retention

Conversations are private to their user. Orgs can override the default **90-day**
retention with a flat `helper_retention_days` field on
`PUT /api/v1/orgs/{org_id}/settings` (min 1 day); the route merges into the
stored general settings, so other fields survive. Sending an explicit
`"helper_retention_days": null` clears the override and reverts to the 90-day
default. Personal threads (no org) use the 90-day default. The admin retention
sweeper hard-deletes expired messages, then purges conversations left empty and
untouched for over an hour.

## Data protection

Every transcript excerpt, summary, and KB text sent to the provider is redacted
with the org's DLP policy (default policy for personal scope). Each turn writes
one retrieval-audit context and one event per tool call (`source="helper"`) with
sanitized arguments and returned refs, so an org can answer "what did the helper
send to the model". Logs carry only ids, token counts, durations, and stop
reasons — never the key, prompt text, or excerpt text.
