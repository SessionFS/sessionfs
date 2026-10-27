# Session helper

*Enterprise feature `session_helper`. Requires a BYOK model key.*

The session helper answers questions over the sessions and project knowledge you
can already read — "find the session where we fixed the SSO redirect", "what did
Alice decide about Redis and why?", "summarize these three sessions". It is an
agent with tools, not a chat over a single summary, and every factual answer
cites the session ids (and message ids) it drew from.

It sees exactly what you can read today: your own sessions, projects you can
access, and org membership. An org admin does not gain a wider view through it.

For questions about SessionFS itself — how to set up Jira, what Auto Narrative
does, which commands and settings exist — it searches the official docs
(sessionfs.dev) instead of guessing. Those answers cite the docs pages and
sections they drew from alongside the session citations, and each link opens
the page in a new tab.

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

- **Supported providers.** The same provider list powers the judge, the helper,
  summaries, and Jira drafting:

  | Provider | Id | Endpoint |
  |---|---|---|
  | Anthropic | `anthropic` | api.anthropic.com |
  | OpenAI | `openai` | api.openai.com |
  | Google Gemini | `google` | generativelanguage.googleapis.com |
  | OpenRouter | `openrouter` | openrouter.ai |
  | DeepSeek | `deepseek` | api.deepseek.com |
  | Kimi (Moonshot) | `kimi` | api.moonshot.ai |
  | xAI (Grok) | `xai` | api.x.ai |
  | Mistral | `mistral` | api.mistral.ai |
  | Z.ai (GLM) | `zai` | api.z.ai (international) |
  | Qwen (Alibaba Cloud) | `qwen` | dashscope-intl.aliyuncs.com (Singapore) |
  | MiniMax | `minimax` | api.minimax.io (international) |
  | Groq | `groq` | api.groq.com |
  | Together AI | `together` | api.together.xyz |
  | Fireworks AI | `fireworks` | api.fireworks.ai |

  Pick the provider, paste your key, and choose a model — **Fetch models** lists
  the models your key can use, and any model id can be typed in. A custom
  OpenAI-compatible endpoint URL (LiteLLM, vLLM, Ollama, a regional endpoint such
  as mainland-China GLM or another Qwen region) replaces the provider default.
  A saved key is only ever sent to the provider or endpoint it was saved with.

- **Test connection.** Both key forms (personal and org) have a **Test
  connection** button: one tiny chat request ("ping", a few output tokens,
  15 s timeout, 20 s total) through the same code path real helper turns use,
  showing the endpoint, model and latency or a readable failure. API:
  `POST /api/v1/settings/judge/test` and
  `POST /api/v1/orgs/{org_id}/llm-settings/test` (owner/admin, Enterprise),
  body `{provider?, model?, base_url?, api_key?}`, response
  `{ok, code, message, provider, model, endpoint_host, latency_ms}`. Without
  `api_key` the saved key is used only for the exact provider/base URL it was
  saved with, otherwise `400 key_required`. 10 tests per user per minute per
  process (`429 test_rate_limited`).

  Failure codes: `endpoint_not_api` (404/405/HTML from a base URL — a
  provider's console or website, not its API), `invalid_key` (401, or a
  400/403 that says the key is invalid), `access_denied` (any other 403:
  region, model access or org policy — never blocks a save),
  `model_not_found`, `rate_limited`, `insufficient_balance`,
  `endpoint_blocked` (address checks), `timeout`, `unreachable`,
  `provider_error`.

- **Base URL troubleshooting.** Leave it empty for a listed provider. It must
  be an API address: `https://platform.deepseek.com` (DeepSeek's console)
  answers `405 Method Not Allowed`; use `https://api.deepseek.com` or leave the
  field empty. Saving a new base URL (or a new key with one) runs the test
  first. `invalid_key` and `endpoint_blocked` refuse the save
  (`422 {code, message}`), and so does `endpoint_not_api` when the base URL
  changed and the reply is definitive (405, a web page on 2xx/404/405, a
  redirect, or a known console host). Everything else saves and is returned
  in `warnings`: outages (a proxy's 502/503 HTML page is `provider_error`,
  not "wrong address"), rate limits, and a bare JSON 404 from an unknown
  host. A key-only change on an unchanged base URL is refused only for
  `endpoint_blocked` or a certain `invalid_key` (a 401, or a 400 saying the
  key is invalid); a 403 there is always a warning. A 403 that says the key
  is invalid refuses a save only when the base URL also changed. Private
  targets (self-hosted mode) get coarse, time-padded results — see
  docs/self-hosted.md. Only the provider's own JSON error
  field (`error.message`, `error`, `message`, `detail`) is ever shown as a
  snippet; HTML and plain-text bodies are not echoed. When private hosts are
  allowed and the target resolves to a non-public address, timeouts and
  connect failures both read `unreachable` and `latency_ms` is null.

  Known limit: the 10-per-minute test limit is per API process (in memory),
  so with N replicas a user can run up to 10 x N tests a minute. The durable
  limit belongs at the edge (a Cloud Armor rule on `/settings/judge/test`
  and `/orgs/*/llm-settings/test`).

  Implementation note: `saved_judge_key_matches` treats an empty base URL as
  "fall back to the saved endpoint". Any route where an empty base URL means
  "the provider's own API" (the connection tests) must also require the
  strict `services.llm_connection.saved_key_bound_to`. At runtime the same cause surfaces as `reason: endpoint_not_api`
  on the helper's `provider_error` with a message naming the API address.

## Where to use it

- **Dashboard** — **Ask the helper** in the sidebar or the Cmd+K palette opens
  a side drawer; the same button on a project or session page scopes the
  question to it. Answers stream in with links to the cited sessions, and
  earlier threads are listed under **Conversations** (with **New** to start one).
  Ids in an answer are clickable: a ticket (`tk_…`) opens a preview over the
  conversation (status, priority, assignee, project, description, latest
  comments, **Open ticket**), while project (`proj_…`) and session (`ses_…`)
  ids open their pages. When the scoped project has a single GitHub-verified
  repo, `PR #123` and commit SHAs link to GitHub. Ids inside code blocks are
  left as text. The preview reads `GET /api/v1/tickets/{ticket_id}`, which
  answers 404 for a ticket you can't read, the same as for one that doesn't exist.
- **CLI** — `sfs helper ask`, `sfs helper conversations list|show|delete` and
  `sfs helper status` (see the [CLI reference](cli-reference.md#sfs-helper-enterprise-byok)).
- **MCP** — the `ask_session_helper` tool (below).

## Configure the assistant

Model settings exist at two scopes — your own (`GET/PUT /api/v1/settings/helper`)
and the org's (`GET/PUT /api/v1/orgs/{org_id}/helper-settings`; owners/admins
write, members read the effective values) — and each turn resolves an effective
set from them. Every field is optional; anything unset falls back to the default.

| Setting | Range | Default |
|---|---|---|
| `max_output_tokens` | 256–32000 (integer) | 4096 |
| `temperature` | 0–2, or `null` for the provider default | 0 |
| `top_p` | 0–1 | unset (not sent) |
| `max_tool_steps` | 1–20 (integer) | 8 |
| `reasoning_effort` | `off` · `low` · `medium` · `high` | unset |
| `answer_style` | `concise` · `detailed` | `concise` |

`max_tool_steps` counts **tool rounds**: after its last allowed round the model
always gets one more step to answer, so `max_tool_steps: 1` means one round of
tool calls followed by the answer. Integer fields reject booleans. Providers
that don't accept sampling parameters never receive them; on Anthropic, setting
`top_p` drops `temperature` (the API refuses both together).

Configuration is stored on its own, apart from the LLM key rows: saving helper
settings never creates a key, and deleting or rotating a key (`DELETE
/api/v1/settings/judge`, `DELETE /api/v1/orgs/{org_id}/llm-settings`) never
touches the settings, instructions or budget.

### Plan

Both personal routes need the `session_helper` feature on your effective plan
(the same check `/helper/status` makes for a personal scope); otherwise they
return `403 upgrade_required`. The org `PUT` needs the org's plan to include the
helper (Enterprise). The org `GET` works on any plan, like the org key, and
reports `plan_includes_helper`, so a downgraded org can still see what is stored.

### Updating (merge semantics)

`PUT` merges into what is stored:

- A field you leave out keeps its value; an explicit `null` clears it
  (`"instructions": null`, `"monthly_token_budget": null`, `"settings": null`
  resets every setting).
- Inside `settings` each key merges the same way. An explicit `null` returns
  `max_output_tokens`, `max_tool_steps` or `answer_style` to its default; for
  `temperature`, `top_p`, `reasoning_effort` and the ceilings, `null` is itself
  the value (provider default, not sent, no ceiling).
- Unknown keys are rejected with 422.

So `{"monthly_token_budget": 2000}` changes only the budget, and
`{"instructions": "…"}` leaves the budget and settings alone.

### Precedence

A turn using **your own key** uses your personal settings, defaults for anything
unset. A turn using the **org key** uses the org's settings, and the org can
additionally set two ceilings (`ceiling_max_output_tokens`, `ceiling_max_tool_steps`)
that cap how far those org settings push a call. Ceilings only apply on the
org-key path — a personal-key turn is governed solely by your own settings. The
range bounds above are absolute hard caps on every path. `GET /api/v1/helper/status`
reports the effective settings for the key a turn would use.

### Custom instructions

Both scopes accept custom instructions: org instructions up to 4000 characters,
personal instructions up to 2000.

**Which apply to a turn:**

- **Org instructions** apply when the turn uses that org's key, and when the
  conversation is scoped to that org's project (or to sessions in that org's
  projects) — whichever key is used. An org's block appears once even when both
  hold.
- Instructions from an org you are not an active member of are never applied.
- **Personal instructions** always apply, after the org block.

Instructions are not secret. Any member can ask the helper to repeat them, and when a
member uses their own key the org's instructions are sent to that member's provider.
Never put credentials or confidential information in them.

**How they are framed.** The prompt always starts with the helper's fixed rules,
then the answer style, then the org block, then the personal block. Each block
sits between `<org-instructions>` / `<personal-instructions>` tags under a
header stating that the instructions cannot override the rules above: access
limits, scope, citations, treating tool output as data, and confirming before
any action.

**On save** the text is control-stripped, refused with `422
instructions_invalid` if it contains the delimiter tags, DLP-redacted, then
length-checked — the limit applies to the redacted text (`422
instructions_too_long`). If the DLP scan itself fails the save is refused with
`422 instructions_redaction_failed`. **On every turn** the stored text is
redacted again under that turn's DLP policy (which may be stricter than the one
it was saved under), and any delimiter-like tag is neutralised, so instruction
text can never close its own block.

### Usage and budget

Token usage is recorded in an append-only ledger: one row per turn that reported
any tokens — including turns that end with a client disconnect, a provider
error, the deadline or the step limit before any text. The monthly budget and
every usage figure read only this ledger, so deleting a conversation or the
retention sweep purging old messages never changes them. Ledger rows are kept
13 months (see [Retention](#retention)).

An org admin can set `monthly_token_budget` and read a per-member usage summary
for the current **UTC** calendar month; members see only their own usage. Only
turns that use the org's key count against its budget — a personal-key turn is
never charged to it. When this month's org-key usage has reached the budget, the
next turn fails with `429 helper_budget_exceeded` before the model is called.
The check runs before each turn, so turns already running (or started at the
same moment) can push usage past the budget by up to roughly one turn's maximum
tokens each. `GET /api/v1/settings/helper` reports your own month's usage across
both keys.

Every settings change writes an audit row (`user_helper_settings_updated` /
`org_helper_settings_updated`) carrying the instruction text's **length and
sha256 only** — never the text.

### Settings API

`GET /api/v1/settings/helper`:

```json
{
  "settings": {"max_output_tokens": 4096, "temperature": 0, "top_p": null,
               "max_tool_steps": 8, "reasoning_effort": null, "answer_style": "concise"},
  "instructions": "Address me as Alex.",
  "effective_settings": {"max_output_tokens": 4096, "temperature": 0, "top_p": null,
                         "max_tool_steps": 8, "reasoning_effort": null, "answer_style": "concise"},
  "usage": {"input_tokens": 1200, "output_tokens": 300, "total_tokens": 1500}
}
```

`PUT` takes `{"settings": {…partial…}, "instructions": "…"}` and returns the same
shape. `effective_settings` is for the key a turn would use (null without a key).

`GET /api/v1/orgs/{org_id}/helper-settings` (admin view; `PUT` takes `settings`
incl. the ceilings, `instructions` and `monthly_token_budget`):

```json
{
  "role": "admin",
  "plan_includes_helper": true,
  "effective_settings": {"max_output_tokens": 3000, "…": "…"},
  "has_instructions": true,
  "settings": {"max_output_tokens": 4096, "ceiling_max_output_tokens": 3000, "…": "…"},
  "instructions": "Cite the newest session first.",
  "monthly_token_budget": 2000000,
  "usage": {"input_tokens": 9000, "output_tokens": 2000, "total_tokens": 11000,
            "per_member": [{"user_id": "…", "input_tokens": 9000,
                            "output_tokens": 2000, "total_tokens": 11000}]}
}
```

Members get `role`, `plan_includes_helper`, `effective_settings`,
`has_instructions`, `usage` with only their own totals, and the budget status:
`budget_reached` (whether org-key turns are currently refused) and
`budget_used_pct` (whole percent, capped at 100; `null` without a budget) —
never the budget amount or anyone else's usage. The percentage is rounded down, so it reads
100 only once turns are refused. A member who uses most of the org key can still
estimate the budget from their own total and the percentage.

## Actions

Besides answering, the helper can propose five changes for you:

- **Turn Auto Narrative on or off** for one or more projects (for example
  "turn on narratives for all my projects").
- **Move sessions to Trash** (for example "clean up my scratch sessions from
  last month" or "trash every session that used DeepSeek").
- **Create a ticket** with a title, description, acceptance criteria, priority
  and optional assignee (for example "file a task for the webhook retry").
- **Assign a ticket** to a persona (for example "assign tk_… to Atlas").
- **Comment on a ticket** with a plain user comment (for example "note on
  tk_… that the fix is verified").

The helper never makes a change itself. Each proposal appears in the answer as
a card listing every target and what will happen to it — *will change*,
*unchanged*, or *skipped* with the reason (no access, the project's plan
doesn't include project context, already in Trash, outside the conversation's
scope). Who may turn Auto Narrative on or off follows the project settings
page exactly: anyone with access to the project, when the project's plan
includes project context. Nothing changes until you
press **Confirm** (or **Move to Trash**); **Cancel** drops it. A proposal
expires 15 minutes after it is made, and at most 5 can wait at once — older
ones expire first. Each proposal covers at most 100 targets.

Confirming applies exactly the targets on the card, with **your own
permissions**: access is checked again for each target when you confirm, so a
target you lost access to in the meantime fails on its own and the card shows
which ones applied and which failed. Every decision — confirmed, cancelled or
expired — is recorded in the retrieval audit log with the per-target outcome.
Proposals follow the org's helper retention window
(`general.helper_retention_days`): the retention sweep deletes decided ones
once the decision is older than the window, and unconfirmed ones once they
have expired and the expiry is older than the window.

Ticket cards are editable before you confirm: the create form's title,
description, acceptance criteria, priority and assignee are pre-filled from the
helper's draft and can be changed, and the comment form's body can be edited
the same way. Confirm creates the ticket or comment under your own name, with
a provenance line appended to the description or comment, and links the new
ticket from the card. A ticket a captured-session reader creates starts
*suggested* so a project editor can approve it; a helper comment is always a
plain comment (`verdict_trusted` is always false), never a trusted review
verdict — a body with any line that starts with a review verdict marker is
refused. Assignment follows the ticket route's rules: only the ticket's creator
or a project admin can reassign, and someone who can read but not edit the
project can't reassign a ticket that is in progress or held by a work queue
(reassigning an approved, not-yet-started ticket sends it back to
*suggested*). A draft longer than the ticket limits is refused rather than
cut short, and confirm is refused if the ticket moved to another project
after the proposal.

`trash_sessions` takes either `session_ids` the read tools returned, or
filters — `model`, `provider`, `project_id`, `source_tool`, `days`, `since`,
`until` — that the server resolves to the caller's own, non-deleted sessions
inside the conversation's scope (never both). Whether filters were given is
decided after parsing, so blank values (`" "`, `[""]`, an empty date) don't
count and a call with nothing else is refused. The resolved ids are ordered by
last activity, newest first (id breaks ties), so the same filters always
propose the same batch. At most 100 are proposed; when more match, the result
says "N sessions match …; this proposal covers the first 100" and the card
summary reads "Move 100 session(s) to Trash (first 100 of N matching)". The
filters are stored on the proposal as `params.match` (with the `matched` count)
for the audit trail; confirm acts on `params.session_ids` exactly, re-checking
ownership per target. The helper never trashes sessions it picked from text
mentions of a model.

Cleanup is reversible: sessions go to **Trash**, not away. Restore them from
**Sessions → Trash** within 30 days; after that they can be purged for good.

The confirm endpoints are `POST /api/v1/helper/actions/{id}/confirm` and
`POST /api/v1/helper/actions/{id}/cancel`; `GET /api/v1/helper/actions/{id}`
returns the current status (`pending`, `confirmed`, `failed`, `cancelled` or
`expired`). Confirming an expired action returns 409 `action_expired`, a
cancelled one 409 `action_cancelled`; confirming or cancelling an action that
was already decided otherwise returns its stored state unchanged.

## What it can read

### Sessions by model

`Session.model_id` / `model_provider` come from the capture manifest, so the
model is the one the capturing tool reported (a Claude Code session pointed at
DeepSeek shows a DeepSeek model). The unknown-bucket rules and the filters
live in `services/session_models.py`, shared with `GET /api/v1/sessions` and
`GET /api/v1/sessions/models`.

- `list_session_models` (`project_id`, `source_tool`, `days`, `since`,
  `until`) — model buckets across the caller's in-scope sessions, grouped by
  model id and provider, with session counts, capturing tools and first/last
  dates. NULL, `""`, `"unknown"` and `"<synthetic>"` model ids are one
  "unknown" bucket (whatever their provider), as in the dashboard menu.
- `search_sessions`, `list_recent_sessions`, `sessions_touching_path` and
  `trash_sessions` accept `model` and `provider` (a list; a bare string is
  accepted too, at most 20 values of 100 characters). Matching is a
  case-insensitive substring (`ILIKE '%value%' ESCAPE '\'`, wildcards in the
  value escaped), so `deepseek` matches `deepseek-v4-pro` and
  `DeepSeek-Flash`; values OR together. `unknown` selects the unknown bucket
  (for `provider`: no recorded provider); a family value never matches an
  unknown-bucket row.
- `days` / `since` / `until` (`YYYY-MM-DD`, UTC) bound `updated_at`; `until`
  is inclusive of that day.
- `search_sessions` needs `query` only when no metadata filter is given;
  without one it lists every matching session. `list_recent_sessions` and
  `search_sessions` take `offset` and allow up to 100 per page (a `limit`
  above 20 with a text query is refused). Their result starts with
  `Showing 21–40 of 57 sessions (model ~ deepseek). Next page: offset=40.`
  Titles are clipped to 120 characters, and a page stops early rather than
  run past the 40,000-character result cap; the note and the audit refs count
  only the lines returned. Text-search pages order by rank, then recency, then
  id, so tied rows never repeat or skip across pages.
- In a project-scoped conversation, a `project_id` for another project is
  refused ("This conversation is limited to project A; start an unscoped
  conversation to work with project B.") by every session tool and the trash
  proposal, instead of being swapped for the scope project.

Every session line carries the model next to the tool:
`[session:ses_…] Title [claude-code] model: deepseek-v4-pro …` (the provider
is added in parentheses when the id doesn't name it; control characters and
brackets in the client-supplied values are blanked).

The provider's unknown bucket (NULL, `""`, `unknown`) is compared after
trimming and lower-casing, in SQL and in Python alike.

The system prompt (v7) tells the model to call `list_session_models` first for
any question about models or LLMs, to filter by model or provider rather than
text-search for model names, that the capturing tool reports the model, and
to mention unknown-model sessions when they matter, and which tools page
(`list_recent_sessions`, `search_sessions`) and how.

### Project work items

Beyond sessions and project knowledge, the helper reads the project's own work
items and people — still only what you can already see:

- **Tickets** — `list_tickets` / `get_ticket`. Work items (ids like `tk_…`),
  with status, priority, assignee, project, acceptance criteria, dependencies
  and the last 10 comments.
- **Wiki pages** — `list_wiki_pages` / `get_wiki_page`. A project's knowledge
  pages, with the body capped at 20k characters.
- **Agent runs** — `list_agent_runs` / `get_agent_run`. CI review runs, with
  persona, status, severity and findings.
- **Personas** — `list_personas`. The project's own personas and the org
  personas visible from it, with role and active state.

The tools apply the same plan gates as the routes (`check_project_feature`):
tickets need `agent_tickets`, agent runs `agent_runs`, personas
`agent_personas`; a gated project yields "not available on this project's
plan" (or not-found for a single ticket) instead of data. The wiki read routes
have no plan gate, so neither do the wiki tools. Ticket descriptions, each
acceptance criterion, run summaries and finding messages are capped with
"…[truncated]", and `execute` caps any one tool result at 40,000 characters
after redaction.

In a project-scoped conversation nothing from another project comes back. The
session tools use `project_session_clause(project, legacy_only=True)`: sessions
attributed to the project, plus sessions with NO project on one of its remotes.
A session attributed to project B on project A's remote stays out of A's
scope. (The access predicate keeps the wider form.)

`[ticket:…]` / `[wiki:…]` citations are accepted only for ids a tool returned
this turn, and only in projects the caller can read inside the turn's scope.
A ticket id becomes citable only after the tool resolved it to a readable,
in-scope ticket: parent, dependency and agent-run ticket ids that don't
resolve are not shown as citable markers.

Every line a tool returns carries a source date as `YYYY-MM-DD (Nd ago)`. When
the newest source for a claim is more than 7 days old, the helper says so with
"As of \<date\>, ..." so you can see the information may be stale.

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
  "stream": true,                  // default true
  "retry": false                   // re-asking after a failed answer
}
```

`retry: true` re-asks a question whose answer failed. If the thread's last
question is still unanswered and matches this one after redaction, the stored
question is reused instead of being saved twice; otherwise the turn runs as
usual.

Citations look like `[session:<id> msg:<n>]`, where `n` is the 0-based line
number of the message in the session's `messages.jsonl`. The dashboard opens
them at `/sessions/<id>?msg=<n>`, backed by
`GET /api/v1/sessions/{id}/messages?around=<n>`, which returns the page holding
that line plus a `target` object (`found`, `hidden` when the view filters the
message out, or `missing`, with the nearest message to show instead).

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
{
  "enabled": true,
  "key_source": "user",
  "provider": "openai",
  "model": "gpt-4o",
  "effective_settings": {
    "max_output_tokens": 4096,
    "temperature": 0,
    "top_p": null,
    "max_tool_steps": 8,
    "reasoning_effort": null,
    "answer_style": "concise"
  },
  "has_org_instructions": false,
  "has_personal_instructions": true
}
```

`enabled` is false when your plan does not include the helper; `key_source` is
null when no key is configured — surfaces can show an "add a key" state before
the first question. `effective_settings` is the resolved settings for the key a
turn would use (null when no key is set); `has_org_instructions` /
`has_personal_instructions` report whether instructions would apply to a turn
in that scope (see [Custom instructions](#custom-instructions)) without
revealing their text.

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

The token-usage ledger has its own fixed policy: rows are kept **13 months**.
The same sweep deletes ledger rows older than 400 days (reported as
`usage_rows_deleted`), whatever the org's message window; deleting conversations
or messages never deletes ledger rows.

## Data protection

Every transcript excerpt, summary, and KB text sent to the provider is redacted
with the org's DLP policy (default policy for personal scope). Each turn writes
one retrieval-audit context and, per tool call, one event per project the call
read from (`source="helper"`), each with the sanitized arguments and returned
refs, so an org can answer "what did the helper send to the model". The
per-project fan-out is deliberate: each project's audit trail lists what was
read from it. Tools name a project only when it returned rows, so an unscoped
`list_tickets` over five projects where two have matches writes two events. An
identical repeated call in the same turn is answered from the turn's cache and
writes no event. Logs carry only ids, token counts, durations, and stop
reasons — never the key, prompt text, or excerpt text.
