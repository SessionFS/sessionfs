# Scout v4 — n8n workflow contract

Scout is an autonomous **continuous analyst** that runs from n8n, not as a
local MCP tool. Each execution must (a) load Scout's own prior findings
before it reasons, (b) write new findings with persona attribution, and
(c) wrap the run in a SessionFS `AgentRun` so the audit trail is always
closed. This document is the integration contract — copy the request
shapes verbatim into your n8n nodes.

The workflow uses the **direct HTTP API** with a service key. MCP
`add_knowledge` is the right path for local agents (Claude Code, Codex
CLI) but n8n nodes are HTTP-only, so this doc only references HTTP.

This contract assumes v0.10.21+ (Phase 4a). Earlier versions don't
support `persona_name` / `author_class` on knowledge entries and Scout
attribution will silently fall back to the user-row default.

**Scope of this contract (v4): continuity only.** v4 fixes the agent-
memory loop — Scout reads its own prior findings, writes attributed
entries, and wraps each execution in an AgentRun. Multi-source
ingestion (HN + GitHub + Reddit + pricing pages + Discord per
`.agents/scout`) is **NOT** in scope here. A future Phase 4c will
introduce a uniform signal-shape adapter so Scout doesn't need to be
rebuilt for each new source. Until then, the working assumption is
one source per workflow (Hacker News for the v4 build); the contract
shapes below apply equally to whichever single source you wire in.

---

## 1. Service-key scopes Scout needs

Mint one service key for the n8n Scout workflow with these scopes,
scoped to the project Scout watches:

| Scope | Why Scout needs it | Endpoints used |
|-------|--------------------|----------------|
| `personas:read` | Verify the `scout` persona exists at run start; load its prompt body | `GET /personas/{name}` |
| `knowledge:read` | Retrieve prior Scout findings as input context | `GET /entries?persona_name=scout&author_class=agent&limit=30` |
| `knowledge:write` | Persist new signals as KB entries attributed to `scout` | `POST /entries/add` |
| `tickets:write` | Open follow-up tickets when a signal warrants human review | `POST /tickets` |
| `agent_runs:write` | Open and close the `AgentRun` wrapping each execution | `POST /agent-runs`, `POST /agent-runs/{run_id}/complete` |

Mint via CLI:

```bash
SCOUT_KEY=$(sfs admin service-keys create \
  --org org_9e39b81833e6fdd5 \
  --name "n8n-scout-agent" \
  --scope personas:read \
  --scope knowledge:read \
  --scope knowledge:write \
  --scope tickets:write \
  --scope agent_runs:write \
  --project proj_c0242b0fccbd48b4 \
  --output-key)
```

Store the raw key in n8n's credential store as an `HTTP Header Auth`
credential with name `Authorization` and value `Bearer ${SCOUT_KEY}`.
The raw key is only visible at create / rotate time — there is no
recovery path.

**Scope-name clarification:** the catalog has only two agent-run
scopes — `agent_runs:write` (covers create + start + complete) and
`agent_runs:read` (covers list + get + status). There is no separate
`agent_runs:list`. If you see a reference to `:list` in older
proposals or AC drafts, it maps to `agent_runs:read`.

### Scopes Scout deliberately does NOT need

- `agent_runs:read` — service-key reads on `/agent-runs` are reserved
  (Phase 4+). The Scout workflow tracks its own execution state in n8n
  and never needs to list past runs from the API. If you later add a
  dashboard or admin surface that lists Scout runs, it should hit the
  API as a user key.
- `personas:write` — Scout doesn't create or mutate personas at
  runtime. The `scout` persona must already exist before the workflow
  runs (created once by a human via `sfs persona create` or via the
  Dashboard).
- `knowledge:write` extras (`/compile`, `/rebuild`, `/dismiss-stale`)
  — these are higher-trust dashboard surfaces that remain user-key
  only by design.

### Expected failure on missing scope

If you forget a scope, the API returns:

```json
{
  "error": {
    "code": "403",
    "message": "Scope required: one of ['<scope>']. Key has: [<key's scopes>].",
    "details": {
      "error": "insufficient_scope",
      "required": ["<scope>"],
      "current": ["<key's scopes>"]
    }
  }
}
```

The `required` + `current` arrays let the n8n node surface a clear
remediation in the failure branch.

---

## 2. Startup sequence (every execution)

Each n8n execution runs these nodes in order before any reasoning step:

### 2.1 Verify the `scout` persona exists

```http
GET /api/v1/projects/{project_id}/personas/scout
Authorization: Bearer ${SCOUT_KEY}
```

- **200** → persona exists; capture `content` as the persona prompt
  body (this is what feeds the LLM as its system message). Proceed
  to §2.2.
- **404** → persona missing. **Preflight failure — no AgentRun is
  created.** `POST /agent-runs` validates `persona_name` against
  `agent_personas` for the project before insert, so the create call
  would return 422 (not 201) and no `run_id` would exist to
  complete. Instead:
  1. Surface an ops-visible failure: write to n8n's execution log
     with severity `critical`, post a Slack/email alert via your
     standard ops channel, and **stop the workflow execution**.
  2. Do NOT call any `/agent-runs` endpoint. There's nothing to
     close.
  3. Open a follow-up ticket via `POST /tickets` (uses your
     existing `tickets:write` scope) tagged `priority=high`,
     `assigned_to=atlas`, `title="Scout persona missing on
     <project_id>"`, so a human registers the persona via
     `sfs persona create`.

  This pattern preserves the "every AgentRun reaches a terminal
  state" invariant from §5 (which only applies to runs that were
  successfully created) while still leaving a durable audit handle
  via the follow-up ticket. If a future Phase 4d wants to record
  failed preflights as `AgentRun` rows, it needs platform changes
  (relax `_validate_active_persona` or add a queued-without-persona
  state) — out of scope here.

### 2.2 Create the AgentRun (queued)

```http
POST /api/v1/projects/{project_id}/agent-runs
Authorization: Bearer ${SCOUT_KEY}
Content-Type: application/json

{
  "persona_name": "scout",
  "tool": "n8n",
  "trigger_source": "scheduled",
  "trigger_ref": "{{ $('Build trigger_ref').item.json.trigger_ref }}",
  "fail_on": "high",
  "triggered_by_persona": "scout"
}
```

- **Valid `trigger_source`**: `manual | ci | webhook | scheduled | mcp | api`.
  Use `scheduled` for cron-driven n8n triggers and `webhook` for
  inbound-webhook triggers. Anything else returns 422.
- **`trigger_ref`**: a stable, human-readable string tying this run
  back to its n8n execution. **Use a durable composite** so the
  pointer survives n8n's execution-history retention window
  (default ~10 days; configurable but rarely tuned):

  ```
  trigger_ref = "n8n:<workflow_id>:<iso_timestamp>:<short_hash>"
  ```

  - `workflow_id` from `$workflow.id` (durable; survives purge).
  - `iso_timestamp` rounded to seconds, stable across the
    execution.
  - `short_hash` is an 8-char hex digest of `(workflow_id +
    exec_id + iso_timestamp)` — enough entropy to disambiguate
    same-second triggers and to fingerprint a single execution
    without leaning on the soon-to-be-purged `exec_id`.

  **Don't try to inline the hash construction inside the HTTP
  Request JSON body** — the n8n expression engine doesn't expose
  Node's `$crypto.*` API. The reliable pattern is a preceding
  **Set** (or **Code**) node that pre-computes `trigger_ref` and
  the HTTP body just references it:

  In an n8n **Set** node named exactly `Build trigger_ref`, set a
  single field `trigger_ref` (type: string) to:

  ```
  ={{ "n8n:" + $workflow.id + ":" + $now.toISO() + ":"
       + ($workflow.id + $exec.id + $now.toISO())
           .hash('sha256').substring(0, 8) }}
  ```

  This uses two documented n8n expression primitives:
  `$exec.id` (current execution id) and `String.prototype.hash`
  (e.g. `String.hash('sha256')` returns a hex digest). The
  downstream HTTP Request body then references the pre-computed
  value via `{{ $('Build trigger_ref').item.json.trigger_ref }}`
  as shown above — clean, no exotic helpers, paste-ready.

  Avoid the bare `n8n:<workflow_id>:<exec_id>` shape — after
  n8n's retention window the `exec_id` becomes a dangling
  pointer and you lose the audit trail from `trigger_ref` back to
  the running history. Keep `exec_id` inside `source_context`
  on KB writes (§3.1) where it's only a dedupe handle and doesn't
  need to outlive the execution.
- **`fail_on`**: `none | low | medium | high | critical`. Use `high`
  for production Scout — runs with severity ≥ high get
  `policy_result=fail` even if the workflow itself completed cleanly.

Response is `201` with the AgentRun row — capture `id` (e.g.
`run_abc123...`) into a workflow variable. Every subsequent step
needs it.

**Note:** Scout does NOT call `POST /agent-runs/{run_id}/start`. That
endpoint accepts user keys only today (it returns a heavy compiled
persona+ticket context payload meant for IDE-side tooling). Service
keys can skip it: `complete` accepts both `queued` and `running` as
source states, so the run goes `queued → completed` directly when
Scout finishes. The audit trail still captures `persona_name`,
`trigger_source`, `trigger_ref`, `result_summary`, `severity`, and
the service-key actor on the row.

### 2.3 Fetch Scout's prior findings

```http
GET /api/v1/projects/{project_id}/entries
   ?persona_name=scout
   &author_class=agent
   &limit=30
   &sort=created_at_desc
Authorization: Bearer ${SCOUT_KEY}
```

**Do NOT filter by `claim_class=claim`.** Scout writes most findings
at `claim_class=note` until a future run promotes them. Restricting
to `claim` would hide Scout's own working memory from itself. The
retrieval should pull all classes (note + claim + evidence) so the
agent sees its full trajectory.

Feed the returned rows into the LLM input as a structured block
labeled "Scout's prior signals (last 30, most recent first)". Each
row already carries `content`, `entity_ref`, `source_context`,
`created_at`, `claim_class`, and `freshness_class` — that's enough
for the LLM to deduplicate, escalate, or retire signals.

### 2.4 Reason

The LLM now has the persona prompt (2.1) + prior findings (2.3) +
the current external signal (HN front page, RSS items, etc — whatever
your trigger pulls). It produces:

- Zero or more **new findings** to persist as KB entries.
- Zero or more **follow-up tickets** to open for human review.

---

## 3. Writing findings (steps 3.x run per finding)

### 3.1 Persist a finding as a KB entry

```http
POST /api/v1/projects/{project_id}/entries/add
Authorization: Bearer ${SCOUT_KEY}
Content-Type: application/json

{
  "entry_type": "discovery",
  "content": "<the finding text, ideally 100+ chars, structured>",
  "confidence": 0.7,
  "persona_name": "scout",
  "author_class": "agent",
  "source_context": "scout:n8n:{{ $workflow.id }}:{{ $exec.id }}:{{ $signal_id }}",
  "entity_ref": "<optional canonical id, e.g. hn:38291847>",
  "entity_type": "<optional, e.g. 'hn_story'>"
}
```

- **`persona_name: "scout"`** is mandatory. The server validates it
  against `agent_personas` for the project — typos return 422.
- **`author_class: "agent"`** can be passed explicitly. The MCP
  shortcut (active-ticket bundle auto-default to `agent`) is not
  available over HTTP — the request body is the only signal.
  Service-key anti-spoof remains server-authoritative: if you pass
  `author_class: "human"` with a service key, the server forces it
  back to `agent`. The response body carries the value that actually
  landed; assert on it in the n8n node if you want defense-in-depth.
- **`source_context`** is the dedupe + provenance handle:
  `scout:n8n:<workflow_id>:<execution_id>:<signal_id>`. Pick
  `<signal_id>` from the upstream source (e.g. HN story id, RSS
  GUID hash, Reddit post id). Two retries of the same execution
  reusing the same `signal_id` will have the SAME `source_context` —
  see §4 for dedupe.
- **`confidence`**: omit for Scout's default (0.7, note tier). Pass
  ≥ 0.8 explicitly only when the signal is independently
  corroborated and Scout itself is sure. Most Scout writes should
  stay below the 0.8 promotion gate so a future Scout run can
  validate or supersede them.

### 3.2 Optionally open a follow-up ticket

If a finding warrants human review (e.g. a competitor shipped a
feature that overlaps with our roadmap), open a ticket:

```http
POST /api/v1/projects/{project_id}/tickets
Authorization: Bearer ${SCOUT_KEY}
Content-Type: application/json

{
  "title": "Scout: <signal headline>",
  "description": "<the finding + why it matters>",
  "priority": "medium",
  "assigned_to": "atlas"
}
```

The ticket carries the same service-key audit triple
(`actor_type=service_key`, `service_key_id`, `service_key_name`) so
the dashboard can show "opened by Scout (n8n)" without ambiguity.

---

## 4. Dedupe + retry policy

n8n retries failed executions (default: 3 attempts per node). Scout
**must** survive retries without writing duplicate KB entries or
opening duplicate tickets.

### 4.1 Bounded entries per run

Hard caps enforced by the workflow (so two correct Scout
implementations don't diverge by an order of magnitude):

| Constant | Limit | Rationale |
|----------|-------|-----------|
| `MAX_KB_WRITES_PER_RUN` | **20** | If the LLM identifies more than 20 signals worth persisting, log the overflow and drop the lowest-signal-strength items. Anything beyond 20 is almost certainly bucket-spam that the KB's semantic dedup will collapse anyway. |
| `MAX_TICKET_CREATES_PER_RUN` | **5** | Tickets are higher-trust than KB notes — they enter humans' inboxes and trigger work. A run that wants to open more than 5 is almost always misclassifying signals as escalations. |
| `MAX_RETRY_PER_SIGNAL` | **1** | One retry on signal-level write failure (HTTP 5xx, timeout). Beyond that, the failure branch should record the signal as part of the AgentRun's `result_summary` and skip — not loop. |

When more than 20 signals look interesting, prefer **consolidation**:
one KB entry per upstream-source bucket (HN top stories, GitHub
trending, Reddit /r/programming) summarizing the day's signal in that
bucket, not one entry per item. The bucket entry's `source_context`
stays stable (`scout:n8n:<workflow_id>:<execution_id>:bucket:hn`) so
the next run can find and supersede it.

### 4.2 Stable `source_context` is the dedupe primitive

Because `source_context` includes `<execution_id>:<signal_id>`, a
node retry that reaches the same write step with the same upstream
signal will send the SAME `source_context`. Two strategies for the
write step in n8n:

1. **Pre-write dedupe (preferred)**: before POST, query
   `GET /entries?source_filter=scout:n8n:<workflow_id>:<execution_id>:<signal_id>&limit=1`.
   If a row exists, skip the write. The v0.10.21 `source_filter`
   uses literal substring matching with `%`/`_` escaped, so the
   string is matched as data.
2. **Idempotent retry tolerance**: skip the pre-check and let the
   server accept the duplicate. The KB has semantic dedup
   (content-overlap) that will fold near-duplicates at compile time,
   so transient retry storms produce at most 2–3 redundant rows per
   signal, which the next compile will collapse. This is simpler in
   n8n but produces noisier audit trails.

Either strategy is fine; pick (1) if storage / signal-volume
matters, (2) if simplicity matters.

### 4.3 AgentRun is created BEFORE the dedupe check

The AgentRun (§2.2) is created at the top of the execution, before
any retry-prone work. If the workflow retries the *whole* execution
(rare — only on cold-start failure before the first node runs), a
new AgentRun will be created. That's a feature: each retry generates
a fresh `execution_id`, so it's a different audit record. The actual
data writes are still deduped by `source_context` at the KB layer.

---

## 5. Failure branch (mandatory)

**Every workflow execution must complete its AgentRun**, regardless
of what failed. Without this, runs accumulate in the `queued` state
forever and the `agent_runs` table grows orphans.

In n8n: put a single failure-branch node downstream of every workflow
step that calls SessionFS APIs. Wire it to the `Stop and Error`
trigger of every upstream node. The failure branch runs:

```http
POST /api/v1/projects/{project_id}/agent-runs/{run_id}/complete
Authorization: Bearer ${SCOUT_KEY}
Content-Type: application/json

{
  "status": "errored",
  "severity": "<see severity matrix below>",
  "result_summary": "<one-line description of what failed>",
  "findings": []
}
```

### Severity matrix for n8n failures

| Failure class | `severity` | Rationale |
|---------------|------------|-----------|
| Persona missing (404 on §2.1) | `critical` | Scout cannot run at all; needs human to provision the persona. |
| Auth failure (401/403 on any call) | `high` | Service key revoked, rotated without n8n update, or scopes missing. |
| Upstream source unreachable (HN/RSS 5xx, timeout) | `low` | Transient; next scheduled run will likely succeed. |
| LLM API error / rate limit | `medium` | Recoverable but indicates capacity issues. |
| Internal SessionFS 5xx | `medium` | Surface to ops; not Scout's fault. |
| Unknown / unclassified exception | `medium` | Default for the catch-all branch. |

With `fail_on=high` set at AgentRun create, runs with
`severity ≥ high` end up as `policy_result=fail` and the dashboard
flags them red without further configuration.

### Success branch

```http
POST /api/v1/projects/{project_id}/agent-runs/{run_id}/complete
Authorization: Bearer ${SCOUT_KEY}
Content-Type: application/json

{
  "status": "passed",
  "severity": "none",
  "result_summary": "Reviewed <N> signals, wrote <M> KB entries, opened <K> tickets.",
  "findings": []
}
```

Findings array is reserved for structured CI-style outputs (e.g. test
failures). Scout's "findings" are KB entries + tickets, not items in
the `findings` JSON column. Pass `[]`.

---

## 6. Smoke-test procedure (live verification)

After deploying or modifying the n8n Scout workflow, run this manual
verification once to confirm the loop:

### 6.1 Run N — write findings

1. Trigger the Scout workflow manually in n8n.
2. After it completes, check the AgentRun:
   ```bash
   curl -sS -H "Authorization: Bearer $USER_KEY" \
     "https://api.sessionfs.dev/api/v1/projects/proj_c0242b0fccbd48b4/agent-runs?persona_name=scout&limit=1"
   ```
   - Expect: 1 row with `status=passed`, `trigger_source=scheduled`,
     `trigger_ref` matching the durable composite shape
     `n8n:<workflow_id>:<iso_timestamp>:<short_hash>` per §2.2,
     `actor_type=service_key`, and
     `service_key_name=n8n-scout-agent`. The audit triple is
     surfaced on every AgentRun read response as of v0.10.21
     (tk_a77b671fd86a42fb), so this single curl confirms both the
     run's terminal state and the service key that owned it.
3. Verify KB attribution:
   ```bash
   curl -sS -H "Authorization: Bearer $USER_KEY" \
     "https://api.sessionfs.dev/api/v1/projects/proj_c0242b0fccbd48b4/entries?persona_name=scout&author_class=agent&limit=5"
   ```
   - Expect: ≥ 1 row with `persona_name=scout`, `author_class=agent`,
     `source_context` matching the `scout:n8n:...` shape.
4. Verify `source_filter` dedupe handle:
   ```bash
   curl -sS -H "Authorization: Bearer $USER_KEY" \
     "https://api.sessionfs.dev/api/v1/projects/proj_c0242b0fccbd48b4/entries?source_filter=scout:n8n:&limit=20"
   ```
   - Every row's `source_context` should start with `scout:n8n:`.

### 6.2 Run N+1 — retrieve own memory

1. Trigger the Scout workflow again.
2. In the n8n execution log, inspect the response of the §2.3
   retrieval node.
   - Expect: the rows written in Run N are present in the input
     context. If they're absent, the agent-memory loop is broken
     (see §6.3).
3. Verify the LLM's reasoning step references prior findings (most
   easily checked by reading the new KB entries — they should
   reference, supersede, or extend prior signals rather than restate
   them).

### 6.3 Common loop-broken symptoms

- **No prior findings retrieved**: service-key writes always land
  as `author_class=agent` (the server forces this regardless of
  payload), so the symptom is almost never an `author_class`
  problem. The likely causes are, in order: (a) `persona_name`
  typo on the write — the server validates against
  `agent_personas` and returns 422 on mismatch, so check n8n's
  failure log for a recent 422 from `POST /entries/add`; (b) the
  retrieval query is hitting the wrong project (verify
  `project_id` in the URL); (c) the retrieval is filtering by
  `claim_class=claim` somewhere and missing the note-class rows
  Scout actually writes — re-read §2.3.
- **Findings present but agent ignores them**: the persona prompt
  (§2.1) is missing the "Always read 'Scout's prior signals'
  section before reasoning" instruction. Fix the persona body
  via `PUT /personas/scout` (requires `personas:write` on the
  service key OR a one-off user-key write — Scout itself does
  not need to mutate its own persona).
- **AgentRun stuck in `queued`**: the failure branch isn't
  wired. n8n executions that error out are leaving runs open.
  Audit: `GET /agent-runs?status=queued&persona_name=scout`.

---

## 7. Why the contract looks like this

A few decisions worth flagging:

- **Why HTTP, not MCP?** n8n's HTTP Request node is the only
  general-purpose call surface. MCP is for local agents that have
  an MCP runtime (Claude Code, Codex CLI). The MCP
  `add_knowledge` tool has additional ergonomics (active-ticket
  bundle auto-thread) that don't apply to a stateless n8n
  workflow.
- **Why skip `/start`?** It's user-key-only today and its only
  side effect (the compiled persona context payload) duplicates
  what Scout already loads via §2.1 + §2.3. A future Phase 4b
  may opt `/start` into `agent_runs:write` if a downstream
  consumer surfaces "running" status visibility — until then,
  Scout's `queued → terminal` lifecycle is correct.
- **Why include notes in retrieval?** Scout's lower-confidence
  signals stay at `claim_class=note` until a future run
  corroborates them and promotes. If retrieval filters
  `claim_class=claim`, Scout loses access to its own working
  memory. The Phase 4a default `GET /entries` returns all
  classes; don't add `claim_class=claim` to the query.
- **Why `source_context` matters more than `session_id`?** Scout
  is stateless across n8n executions — there's no SessionFS
  session row for its writes. `source_context` is the only
  durable handle linking a KB entry back to its origin
  (`<workflow_id>:<execution_id>:<signal_id>`), and the
  v0.10.21 `source_filter` query param makes it queryable.

---

## 8. Future work (NOT in scope here)

The following are deliberately deferred — Scout v4 works without
them today:

- **Phase 4c: multi-source signal adapter.** v4 covers exactly
  one upstream source per workflow. Phase 4c will introduce a
  uniform signal-shape contract (canonical `{source, signal_id,
  content, observed_at, ...}` envelope) so HN, GitHub trending,
  Reddit, pricing-page scrapers, and Discord can share the same
  Scout reasoning loop without per-source forks of this
  workflow. Until that lands, build one workflow per source and
  let the LLM read across them via the KB.
- **`agent_runs:read` for service keys**: would let Scout
  consult its own run history before reasoning ("did the last
  run already cover this signal?"). Today the same question
  is answered via the KB's `source_filter` query, which is
  cheaper and more direct.
- **Per-persona compile retrieval channel**: when `/compile`
  decides which entries enter the project context document,
  it could weight or exclude based on `author_class`. Today
  all classes are eligible.
- **n8n credential helper**: a future `sfs n8n bootstrap`
  command could provision the service key + write the n8n
  credential automatically. Manual mint + paste works fine for
  the first few agents.
- **First-class `n8n-engineer` persona**: the role that owns
  building and maintaining the n8n workflows (this doc, the
  workflow JSON files, the service-key rotation cycle) is
  currently implicit. Registering `n8n-engineer` as a project
  persona (`sfs persona create --name n8n-engineer
  --role "n8n Agent Engineering Lead"`) lets future KB writes,
  tickets, and review comments from that role land with the
  same Phase 4a attribution Scout itself uses.

Open a follow-up ticket if Scout's workflow hits a wall any of
these features would solve.
