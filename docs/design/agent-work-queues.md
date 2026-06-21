# Binding Design — MCP-First Agent Work Queues (autonomous ticket-closing loops)

**Status:** 🟡 DRAFT — Compass product+design draft for review. Routes to Atlas (backend/data-model/API contract), Sentinel (concurrency + tenant-boundary + runaway-loop threat model), and CEO (scope/sequencing approval) before any implementation ticket is cut. NOT cleared to build.
**Ticket:** `tk_c2ed6093acde4d55` — "Design MCP-first Agent Work Queues for autonomous ticket closing loops"
**Author:** Compass (product intent + acceptance boundaries)
**Co-owners on accept:** Atlas (how — schema/routes/MCP), Sentinel (security conditions), Prism (v1.1 dashboard)
**Date:** 2026-06-20
**Grounded against:** codebase at `develop` as of 2026-06-20 (migrations 001–052; 62 MCP tools; `src/sessionfs/server/routes/tickets.py`, `routes/agent_runs.py`, `services/review_state.py`, `mcp/server.py`, `db/models.py`, `active_ticket.py` all read and cited below by real path + line).

---

## Revision History

| Rev | Date | Changes |
|-----|------|---------|
| D1 | 2026-06-20 | Initial Compass draft. Full coverage of all 12 acceptance criteria + problem statement, data-model sketch (`work_queues` / `work_queue_items` / `work_queue_runs`, migration 053), MCP tool surface + JSON shapes, REVIEW-loop + IMPLEMENTATION-loop step algorithms, durable cursor model, wake-mechanism integration (/loop, cron, CI), token/cost controls, writeback rules, CLI parity expectations, security/concurrency threat list, staged plan, and the explicit `review-until-clean` mode spec built on `services/review_state.py`'s `VERIFIED-CLEAN` closure rule. |

---

## 1. Problem statement — why now

Today the team runs an **autonomous-looking** loop that is actually **human-dispatched**. The shape (from `CLAUDE.md` "Multi-LLM Review" + the `feedback_polling_codex` / `feedback_codex_review` memories + `services/review_state.py`'s own docstring) is:

1. An implementer agent (Atlas/Prism/etc. persona via Claude or DeepSeek) does work on a ticket and posts a closure comment.
2. A human **pastes the ticket ID** to the polling Codex reviewer (`author_persona="codex-reviewer"`).
3. Codex posts `Codex R{N} review on tk_X: CHANGES REQUESTED` + findings, or `VERIFIED-CLEAN`.
4. If findings: the implementer fixes, posts a new closure comment, and the human **re-pastes** to Codex.
5. Repeat until `VERIFIED-CLEAN`.

The human is the **dispatcher and the cursor**. They remember which ticket is mid-review, which round it's on, which agent acts next, and when the loop is done. That memory lives in a chat window — it evaporates between sessions, can't run on a schedule, and doesn't scale past one human babysitting one loop.

SessionFS already has every **primitive** to remove the human from the dispatch seat:
- `Ticket` FSM with atomic `UPDATE ... WHERE status=X` rowcount-1 guards and `lease_epoch` fencing (`routes/tickets.py:61-72`).
- `TicketComment` append-only thread with `since`/`since_id` cursor polling (`mcp/server.py:885-928`).
- `get_ticket_review_state` deriving open/closed findings + `last_verdict` + the `VERIFIED-CLEAN` closure rule **without an LLM** (`services/review_state.py`).
- `AgentRun` as the per-execution audit/policy record (`routes/agent_runs.py`, `db/models.py:1596`).
- `start_ticket` returning compiled persona+ticket context and writing the `active_ticket.json` provenance bundle (`active_ticket.py`).

What's **missing** is a **durable, MCP-addressable spec of "the loop itself"** — the list/filter of tickets to service, where the cursor is on each, what the stop condition is, the cadence, and the run budget — so that a stateless agent can wake (via Claude `/loop`, cron, CI, or a future event), call **one MCP tool**, do a small bounded chunk of work, persist its progress server-side, and exit — with the next wake picking up exactly where it left off. **That object is the WorkQueue.**

**Why now:** the manual loop is the proven, daily workflow. We are not inventing a workflow — we are formalizing one we already trust into a server-side object so it survives chat memory, runs unattended, and can be observed/audited.

### 1.1 Non-goals (explicit deferrals)

- **WorkQueue does not spawn models.** Like `AgentRun` (`db/models.py:1599-1603`: "It does NOT spawn the model"), the queue returns *instructions + bounded context* for the caller's runtime to execute. SessionFS stays true to "NO server-side LLM API keys" (CLAUDE.md Key Decisions).
- **No real-time push in v1/v1.1.** Honors "NO WebSockets, NO Redis, NO real-time sync. HTTP + ETags only." Wake is poll-driven (`/loop`, cron, CI). Event-driven wake is a v2 design sketch only (§14).
- **No cross-project queues in v1.** A queue is scoped to exactly one project (one `projects.id`), matching every existing project-scoped resource. Cross-project orchestration is out of scope.
- **No automatic merge/deploy.** The queue closes the *ticket review loop*; it never pushes, merges, or releases. Those stay human-gated per the branch policy.

---

## 2. WorkQueue — definition and why it is distinct

A **WorkQueue** is a *durable, named, project-scoped plan for an agent to repeatedly service a set of tickets without a human dispatcher.* It owns:
- **selection** — a filter (status/kind/assigned_to/priority/explicit-ID list) describing which tickets are in scope,
- **mode** — what "service" means (`review_until_clean` | `implement_until_done` | `triage`),
- **per-ticket cursor state** — where each ticket is in its loop (`work_queue_items`),
- **stop condition + budget** — when to stop and how much to do per wake,
- **cadence** — the intended wake interval (advisory; the wake mechanism enforces it),
- **lineage** — which persona acts, and the running history of wakes (`work_queue_runs`).

### 2.1 Distinct from existing objects

| Object | What it is | Why WorkQueue is not it |
|--------|-----------|--------------------------|
| **`Ticket`** (`db/models.py:1370`) | A single unit of work with an FSM and a lease. | A queue is a *plan over many tickets* + a durable cursor on each. A ticket has no concept of "next wake," "stop when clean," or "max per run." The queue **drives** ticket FSM transitions; it is not one. |
| **`AgentPersona`** (`db/models.py:1320`) | A reusable role/identity (markdown injected into context). | A persona is *who acts*. A queue is *what to act on, in what order, until when*. A queue **references** a persona (`assigned_persona`) but is orthogonal to it — the same persona can drive many queues. |
| **`AgentRun`** (`db/models.py:1596`) | The audit/policy record of **one** execution of one persona, optionally against one ticket. Status FSM `queued→running→passed/failed/...`. | An AgentRun is a *single shot*; a WorkQueue is the *durable loop that emits many AgentRuns over time*. Each wake of a queue may create one or more `AgentRun` rows (one per ticket worked). AgentRun answers "what happened in this run"; WorkQueue answers "what is the standing loop and where is its cursor." A `work_queue_runs` row may link the `AgentRun` it produced (`agent_run_id`). |
| **`Session`** (`db/models.py:141`) | A captured `.sfs` transcript of a tool session. | A session is *the record of work that happened in a tool*. A queue is *the dispatcher that decides work should happen*. A queue step may reference a captured session (checkpoint/fork) for context continuity, but a session never schedules or selects. |
| **Handoff** (`db/models.py:299` family) | A point-to-point transfer of a session/ticket from sender to one recipient. | A handoff is *one push to one recipient, claimed once*. A queue is *a self-driven pull loop with no human recipient*. They compose (a queue could be fed by handoffs) but are not the same shape. |

**One-line test:** if you can answer "where is the cursor, what's the stop condition, and when is the next wake?" the object is a WorkQueue. Nothing existing answers all three.

---

## 3. Data-model sketch (migration 053 — strictly additive)

Three new tables. All follow existing conventions: `String(64)` IDs (`wq_<hex>`, `wqi_<hex>`, `wqr_<hex>`), `ondelete="CASCADE"` from `projects.id`, JSON-as-Text (not native JSONB) for cross-DB SQLite/PG compatibility (matching `Ticket.context_refs`, `AgentRun.findings`), plain-String denormalized refs for audit-row survival, and service-key provenance columns (`actor_type`/`service_key_id`/`service_key_name`) per the v0.10.10 convention (`db/models.py:1541-1549`).

### 3.1 `work_queues` — the durable loop definition

| Column | Type | Notes |
|--------|------|-------|
| `id` | String(64) PK | `wq_<hex>` |
| `project_id` | String(64) FK→projects.id CASCADE, NOT NULL | tenant scope |
| `name` | String(100) NOT NULL | human label; `uq_work_queue_project_name` (project_id, name) |
| `mode` | String(20) NOT NULL | `review_until_clean` \| `implement_until_done` \| `triage` (CHECK constraint, migration 052 style) |
| `assigned_persona` | String(50), nullable | persona name that acts (plain string like `Ticket.assigned_to`; validated at run time, not FK) |
| `selector` | Text NOT NULL DEFAULT `'{}'` | JSON filter — see §3.4 |
| `stop_condition` | String(30) NOT NULL DEFAULT `'queue_empty'` | `queue_empty` \| `all_clean` \| `max_tickets` \| `manual` (CHECK) |
| `cadence_seconds` | Integer, nullable | advisory wake interval (informational; wake mechanism enforces) |
| `max_tickets_per_run` | Integer NOT NULL DEFAULT 1 | budget per wake (§9) |
| `status` | String(20) NOT NULL DEFAULT `'active'` | `active` \| `paused` \| `completed` \| `archived` (CHECK) |
| `lease_epoch` | Integer NOT NULL DEFAULT 0 | fences concurrent *queue-level* mutation (same pattern as `Ticket.lease_epoch`) |
| `created_by_user_id` | String(64) NOT NULL | reporter provenance |
| `actor_type` / `service_key_id` / `service_key_name` | provenance triple | who created/owns the queue |
| `created_at` / `updated_at` | DateTime(tz) | standard |

Indexes: `idx_work_queue_project_status (project_id, status)`.

### 3.2 `work_queue_items` — the per-ticket cursor (the durable state)

One row per (queue, ticket). This is the cursor the wake mechanism reads/writes. **It is the answer to acceptance-criterion #5.**

| Column | Type | Notes |
|--------|------|-------|
| `id` | String(64) PK | `wqi_<hex>` |
| `work_queue_id` | String(64) FK→work_queues.id CASCADE, NOT NULL | parent loop |
| `ticket_id` | String(64) NOT NULL | plain string (ticket may be hard-deleted; row survives — same rationale as `AgentRun.ticket_id`, `db/models.py:1610-1613`). `uq_work_queue_item (work_queue_id, ticket_id)` |
| `item_status` | String(20) NOT NULL DEFAULT `'pending'` | per-ticket queue status: `pending` \| `active` \| `waiting_review` \| `waiting_implementation` \| `done` \| `failed` \| `skipped` (CHECK) — **distinct from `Ticket.status`**; this is the queue's view of where the *loop* is, not the ticket's FSM |
| `last_comment_cursor_at` | DateTime(tz), nullable | the `since` value for `list_ticket_comments` polling (§ criterion 5) |
| `last_comment_cursor_id` | String(64), nullable | the `since_id` tiebreaker (pairs with above — `mcp/server.py:912-919`) |
| `last_agent_run_id` | String(64), nullable | the most recent `AgentRun` this loop produced for this ticket |
| `last_review_round` | Integer, nullable | highest Codex round seen (from `ReviewState.rounds`) |
| `last_verdict` | String(20), nullable | snapshot of `ReviewState.last_verdict` at last wake (`VERIFIED-CLEAN` / `CHANGES_REQUESTED`) |
| `last_ticket_lease_epoch` | Integer, nullable | the ticket's `lease_epoch` the loop last acted under (fencing — §11) |
| `attempts` | Integer NOT NULL DEFAULT 0 | wake count for this item (runaway-loop guard — §11) |
| `next_eligible_at` | DateTime(tz), nullable | earliest the item should be re-picked (backoff) |
| `note` | Text, nullable | last human-readable status (`"R3 CHANGES_REQUESTED — 1 HIGH open"`) |
| `created_at` / `updated_at` | DateTime(tz) | standard |

Indexes: `idx_wqi_queue_status (work_queue_id, item_status)`, `idx_wqi_ticket (ticket_id)`, `idx_wqi_next_eligible (work_queue_id, next_eligible_at)`.

### 3.3 `work_queue_runs` — append-only wake audit

One row per **wake** (per call to the run-step tool). Makes the loop observable and bounds runaway behavior.

| Column | Type | Notes |
|--------|------|-------|
| `id` | String(64) PK | `wqr_<hex>` |
| `work_queue_id` | String(64) FK→work_queues.id CASCADE, NOT NULL | |
| `wake_source` | String(30) NOT NULL | `loop` \| `cron` \| `ci` \| `manual` \| `event` (free-string, mirrors `AgentRun.trigger_source`) |
| `wake_ref` | String(200), nullable | CI run URL / cron id / loop tag (mirrors `AgentRun.trigger_ref`) |
| `item_id` | String(64), nullable | the `work_queue_items.id` serviced this wake (null = no-op wake / queue idle) |
| `ticket_id` | String(64), nullable | denormalized for audit |
| `agent_run_id` | String(64), nullable | the `AgentRun` produced this wake, if any |
| `action` | String(30) NOT NULL | `picked` \| `posted_review` \| `posted_progress` \| `completed_ticket` \| `waited` \| `stopped` \| `noop` \| `errored` |
| `outcome_summary` | Text, nullable | short human summary written by the agent |
| `actor_type` / `service_key_id` / `service_key_name` | provenance triple | who woke the loop |
| `created_at` | DateTime(tz) NOT NULL | |

Index: `idx_wqr_queue_created (work_queue_id, created_at)`.

> **Atlas decision point:** whether `work_queue_runs` should reuse/extend `AgentRun` instead of a new table. Compass recommendation: keep them separate. `AgentRun` is "one persona execution"; a wake can be a no-op or a pure poll that produces no `AgentRun`. Linking via `agent_run_id` keeps the audit trail joined without overloading `AgentRun`'s FSM.

---

## 4. MCP-first tool surface (criterion 2 + 10)

**MCP is the primary agent surface.** Agents call MCP tools far more reliably than CLI (every existing ticket/agent-run MCP tool description ends with *"Always use this MCP tool instead of running `sfs ... ` CLI"* — e.g. `mcp/server.py:848-849, 977-978`). CLI is a **secondary admin/parity surface** (§10).

Proposed new MCP tools (additive; current count 62 → 67). All take `git_remote` (auto-detected) and route through the existing `_resolve_project_id` resolver used by every project-scoped MCP tool.

### 4.1 `create_work_queue` — define the loop

Request:
```json
{
  "name": "review-backlog-clean",
  "mode": "review_until_clean",
  "assigned_persona": "codex-reviewer",
  "selector": {
    "status": "review",
    "kind": "task",
    "assigned_to": null,
    "priority": null,
    "ticket_ids": ["tk_aaa", "tk_bbb"]
  },
  "stop_condition": "all_clean",
  "cadence_seconds": 600,
  "max_tickets_per_run": 1,
  "git_remote": ""
}
```
- `selector.ticket_ids` (explicit list) OR a filter (status/kind/assigned_to/priority) OR both. If `ticket_ids` is set, those are seeded as `work_queue_items` immediately; filter-only queues materialize items lazily at run-step time (§7 hydration).
- Returns the created queue + `lease_epoch` + the materialized `items` list (id, ticket_id, item_status).

Response:
```json
{
  "work_queue": { "id": "wq_1a2b", "name": "...", "mode": "review_until_clean",
                  "status": "active", "lease_epoch": 0, "max_tickets_per_run": 1 },
  "items": [ {"id": "wqi_01", "ticket_id": "tk_aaa", "item_status": "pending"} ]
}
```

### 4.2 `get_work_queue` — inspect (cheap, no work done)

Request: `{ "work_queue_id": "wq_1a2b", "include_items": true, "git_remote": "" }`
Returns the queue config + summarized item cursors + a `progress` rollup `{pending, active, waiting_review, waiting_implementation, done, failed}`. This is the **inspect-without-acting** call — safe to poll, does not advance the cursor.

### 4.3 `list_work_queues` — discovery

Request: `{ "status": "active", "git_remote": "" }` → list of queues with progress rollups. Mirrors `list_tickets` shape.

### 4.4 `run_work_queue_step` — **the heartbeat** (criterion 2, 3, 4, 6)

The single tool a wake mechanism calls. It does **bounded** work: picks up to `max_tickets_per_run` eligible items, runs the mode-appropriate algorithm (§6, §7) for each, persists cursor state, and returns a **directive** telling the calling agent exactly what to do next this wake.

Request:
```json
{
  "work_queue_id": "wq_1a2b",
  "wake_source": "loop",
  "wake_ref": "loop-5m",
  "max_tickets": 1,
  "git_remote": ""
}
```

Response — a **work directive** (this is the key contract). The server does the *bookkeeping*; the agent does the *thinking*:
```json
{
  "queue_status": "active",
  "stop": false,
  "stop_reason": null,
  "directives": [
    {
      "item_id": "wqi_01",
      "ticket_id": "tk_aaa",
      "ticket_lease_epoch": 7,
      "intent": "post_review",                 // see §6/§7 intent vocabulary
      "context": {
        "ticket": { "title": "...", "status": "review", "kind": "task" },
        "review_state": { "last_verdict": "CHANGES_REQUESTED",
                          "open_findings": [{"severity":"HIGH","text":"...","round":2}],
                          "severity_counts": {"CRITICAL":0,"HIGH":1,"MEDIUM":0,"LOW":0},
                          "last_implementer_comment_id": "cm_77" },
        "new_comments": [ {"id":"cm_77","author_persona":"atlas","content":"R3 closure — fixed in abc1234","created_at":"..."} ],
        "persona_ref": "codex-reviewer",
        "expand_hints": ["get_ticket", "get_context_section", "get_session_summary"]
      },
      "writeback_contract": {
        "post_via": "add_ticket_comment",
        "required_lease_epoch": 7,
        "on_clean": "complete_work_queue_step(verdict=clean)",
        "on_changes": "complete_work_queue_step(verdict=changes)"
      }
    }
  ],
  "next_eligible_at": "2026-06-20T20:10:00Z"
}
```

Key properties:
- The server has already **advanced the cursor** (read `review_state`, fetched the comment delta since `last_comment_cursor_*`, updated `last_*` snapshots). The agent gets a **small delta**, not the whole thread (criterion 8).
- The server returns `ticket_lease_epoch` so the agent's subsequent writes are **fenced** (criterion 11).
- `intent` tells the agent the one thing to do. `expand_hints` lists the MCP tools to call **only if** it needs more context (criterion 8 — expand on demand).
- If nothing is eligible → `directives: []`, and `stop`/`stop_reason` reflect the stop condition (e.g. `stop:true, stop_reason:"all_clean"`).

### 4.5 `complete_work_queue_step` — record the outcome of a directive

After the agent acts on a directive (posts a review, posts progress, completes a ticket), it calls this to persist the result and let the cursor advance on the **next** wake.

Request:
```json
{
  "work_queue_id": "wq_1a2b",
  "item_id": "wqi_01",
  "ticket_id": "tk_aaa",
  "ticket_lease_epoch": 7,
  "outcome": "posted_review",            // matches the directive intent's verbs
  "verdict": "changes",                  // review modes only: "clean" | "changes"
  "comment_id": "cm_91",                 // the comment the agent just posted
  "agent_run_id": "run_55",              // optional — if the agent opened an AgentRun
  "summary": "Posted R3 CHANGES_REQUESTED (1 HIGH)",
  "git_remote": ""
}
```
- Fences on `ticket_lease_epoch` (409 on mismatch — same as `update_ticket`, `routes/tickets.py:1252-1262`).
- Updates `work_queue_items` cursor (`item_status`, `last_verdict`, `last_review_round`, `attempts++`, `next_eligible_at` backoff) and appends a `work_queue_runs` row.
- Returns the updated item + whether the **item is now terminal** (`item_status in {done, failed, skipped}`).

### 4.6 `pause_work_queue` / `resume_work_queue` / `complete_work_queue`

Lifecycle control (criterion 2). `pause` sets `status='paused'` → `run_work_queue_step` returns `stop:true, stop_reason:"paused"` and does no work. `complete` sets `status='completed'` (terminal; manual stop_condition). All fence on the queue-level `lease_epoch`.

### 4.7 Reused (NOT new) tools the loop composes

The loop is **assembled from existing tools** — this is what makes it MCP-first and low-risk. `run_work_queue_step` is the orchestrator; the agent still uses these directly to act:

| Existing tool | Path | Role in loop |
|---------------|------|--------------|
| `get_ticket_review_state` | `mcp/server.py:929` | the stop-condition oracle (§5) |
| `list_ticket_comments` (since/since_id) | `mcp/server.py:885` | comment delta when the agent expands |
| `start_ticket` | `mcp/server.py:960` | implementation loop: claim + load context + write `active_ticket.json` |
| `complete_ticket` | `mcp/server.py:1052` | implementation loop: review → done |
| `resolve_ticket` | `mcp/server.py:1252` | implementation loop terminal |
| `add_ticket_comment` | `mcp/server.py:1082` | both loops: post review / progress |
| `create_agent_run` / `complete_agent_run` | `mcp/server.py:1379/1423` | per-ticket execution audit |
| `add_knowledge` / `update_wiki_page` | (existing) | memory writeback (§9) |
| `get_ticket` / `get_context_section` / `get_session_summary` | (existing) | on-demand context expansion |

---

## 5. The `review_until_clean` mode spec (explicit, criterion-3 + the requested stop-condition spec)

This mode **is the manual loop, automated.** Its stop condition is computed by the existing, LLM-free `services/review_state.py`.

**Stop oracle.** On each pick, the server calls `compute_review_state(comments)` (`services/review_state.py:219`) for the item's ticket. The `VERIFIED-CLEAN` closure rule (`review_state.py:33-38, 269-294`) is the contract:
> *Findings raised in round N are closed when ANY subsequent Codex round has verdict `VERIFIED-CLEAN`.*

The item is **clean / terminal** when:
```
review_state is not None
AND review_state.last_verdict == "VERIFIED-CLEAN"
AND len(review_state.open_findings) == 0
```
(Belt-and-suspenders: a clean last verdict implies no open findings under the closure rule, but we assert both so a malformed thread can't false-positive a stop.)

**Per-wake algorithm (REVIEW agent, e.g. `codex-reviewer`):**

```
run_work_queue_step(queue):
  for item in pick_eligible(queue, limit=max_tickets):           # FOR UPDATE SKIP LOCKED (§11)
    rs = compute_review_state(comments_of(item.ticket_id))

    # 1. STOP CHECK
    if rs and rs.last_verdict == "VERIFIED-CLEAN" and not rs.open_findings:
        mark item.item_status = "done"; append run(action="stopped"); continue

    # 2. WAIT CHECK — is it the reviewer's turn?
    #    The reviewer acts only when the implementer has posted since the
    #    last review comment. Compare last_implementer_comment_id against
    #    the cursor. If no new implementer comment since our last review:
    if not implementer_posted_since(item.last_comment_cursor_id, rs):
        mark item.item_status = "waiting_implementation"
        set next_eligible_at = now + backoff
        append run(action="waited"); continue

    # 3. THE REVIEWER'S TURN — emit a directive
    delta = list_ticket_comments(item.ticket_id, since=item.last_comment_cursor_at,
                                 since_id=item.last_comment_cursor_id)
    advance cursor to newest comment in delta
    emit directive(intent="post_review", context={ticket, review_state, new_comments: delta,
                   ticket_lease_epoch}, writeback_contract={post_via:add_ticket_comment})
    mark item.item_status = "waiting_review"   # we have asked the agent to review
```

The agent then: inspects the fix (expands via `get_ticket`/`get_context_section`/diffs as needed), posts `Codex R{N+1} review on tk_X: <verdict>` via `add_ticket_comment` (fenced with `ticket_lease_epoch`), and calls `complete_work_queue_step(verdict=clean|changes, comment_id=...)`.

- `verdict=clean` → item becomes `done` on the **next** stop check (or immediately if `complete_work_queue_step` re-derives review_state and confirms — Atlas choice; Compass prefers re-derive so the server, not the agent, is the source of truth for "clean").
- `verdict=changes` → item becomes `waiting_implementation`; the loop will not re-pick it for review until a new implementer comment appears.

**This guarantees the loop terminates** on a `VERIFIED-CLEAN` exactly as the human does today, with the same parser the team already trusts — no new "is it done?" heuristic invented.

---

## 6. The `implement_until_done` mode spec (criterion 4)

Drives the **implementer** side: pick the next eligible ticket, claim it, work, post progress, complete, and react to review findings.

**Per-wake algorithm (IMPLEMENTER agent, e.g. `atlas`):**

```
run_work_queue_step(queue):
  for item in pick_eligible(queue, limit=max_tickets):
    t = get_ticket(item.ticket_id)

    # A. If the ticket has open review findings against us, fix them.
    rs = compute_review_state(comments_of(item.ticket_id))
    if rs and rs.open_findings:
        emit directive(intent="fix_findings",
                       context={open_findings: rs.open_findings, new_comments: delta,
                                ticket_lease_epoch, persona_ref: queue.assigned_persona},
                       writeback_contract={post_via:add_ticket_comment,
                                           then:"complete_work_queue_step(outcome=posted_progress)"})
        continue

    # B. Fresh ticket — claim it atomically and load context.
    if t.status == "open":
        bundle = start_ticket(item.ticket_id, tool=...)   # atomic UPDATE WHERE status='open'
        # 409 → another worker won the race → mark skipped/backoff, continue
        emit directive(intent="implement",
                       context={persona_and_ticket_md: bundle.context, ticket_lease_epoch: bundle.lease_epoch,
                                acceptance_criteria: t.acceptance_criteria},
                       writeback_contract={progress_via:add_ticket_comment,
                                           finish_via:"complete_ticket(review→done)",
                                           then:"complete_work_queue_step(outcome=completed_ticket)"})
        mark item.item_status="active"; continue

    # C. In review and clean → done.
    if t.status == "review" and rs and rs.last_verdict=="VERIFIED-CLEAN":
        emit directive(intent="finalize", writeback_contract={via:"resolve_ticket / complete_ticket"})
```

The agent does the actual edits in its own runtime, posts progress with `add_ticket_comment` (fenced), transitions the ticket via `start_ticket`/`complete_ticket`/`resolve_ticket` (all already atomic + lease-fenced), optionally opens/closes an `AgentRun` for the execution audit, and calls `complete_work_queue_step`.

**Pairing.** A `review_until_clean` queue (persona `codex-reviewer`) and an `implement_until_done` queue (persona `atlas`) over the **same ticket set** are the automated two-agent loop. Neither needs the other's chat memory — both read state from the server. The implementer's `waiting_implementation` items become the reviewer's `waiting_review` items and vice versa, mediated entirely by `TicketComment` + `review_state`.

### 6.1 `triage` mode (thin, v1-optional)

Picks `suggested`/`open` unassigned tickets and emits `intent="triage"` directives (suggest assignee/priority/acceptance-criteria via `update_ticket`). Lowest priority; can ship in v1.1.

---

## 7. Durable cursor / state model (criterion 5) — consolidated

Everything the loop needs to resume lives in `work_queue_items` (§3.2). **No chat memory is required to resume a loop** — this is the headline guarantee. Mapping to criterion 5's required fields:

| Criterion-5 field | Where it lives |
|-------------------|----------------|
| `current_ticket_id` | `work_queue_items.ticket_id` of `item_status='active'`/`waiting_*` rows |
| `last_comment_cursor` | `work_queue_items.last_comment_cursor_at` + `last_comment_cursor_id` (the `since`/`since_id` pair) |
| `last_agent_run_id` | `work_queue_items.last_agent_run_id` |
| per-ticket status | `work_queue_items.item_status` (queue-loop view, distinct from `Ticket.status`) |
| `stop_condition` | `work_queues.stop_condition` |
| `cadence` | `work_queues.cadence_seconds` |
| `max_tickets_per_run` | `work_queues.max_tickets_per_run` |

**Hydration (filter-only queues):** at each `run_work_queue_step`, before picking, the server runs the `selector` filter against `tickets` and **upserts** missing `work_queue_items` (idempotent on `uq_work_queue_item`). So a queue defined as "all `status=review` tasks" picks up newly-arrived review tickets automatically without re-creating the queue. Explicit `ticket_ids` queues skip hydration.

---

## 8. Wake mechanisms — calling the step without chat memory (criterion 6)

`run_work_queue_step` is **stateless from the caller's perspective**: all state is server-side. Any of these can drive it:

**A. Claude `/loop`.** A slash command / prompt run on an interval (the `loop` skill). The prompt is literally: *"Call `run_work_queue_step(work_queue_id=wq_1a2b, wake_source='loop')`. For each directive, do the one `intent`, write back per `writeback_contract` (fence with the returned `ticket_lease_epoch`), then call `complete_work_queue_step`. If `stop:true`, report the `stop_reason` and stop."* No memory of prior rounds needed — the server returns the delta.

**B. cron (`schedule`/`CronCreate`).** A scheduled cloud/local agent runs the same prompt on a crontab. `cadence_seconds` is the queue's advisory interval; the cron's schedule is the enforcer. `wake_source="cron"`, `wake_ref=<cron id>`.

**C. CI.** A GitHub Actions / GitLab CI job (same shape as the existing `docs/integrations/github-actions-agent-run.yml`) authenticates with a **scoped service key** (scopes `tickets:read/write`, `agent_runs:write`, plus new `work_queues:read/write` — §11), calls `run_work_queue_step`, acts, writes back. `wake_source="ci"`, `wake_ref=<ci_run_url>`. This is the post-merge "review the new commit" trigger.

**D. (v2) event-driven.** A webhook (new TicketComment by the implementer, a CI completion) flips `next_eligible_at` to now and optionally fires a `RemoteTrigger`/push to a runner. Design-only in v1/v1.1 (§14) — honors no-realtime in v1.

In **all** cases the contract is identical: **one MCP call in, a bounded directive out, write back, optionally call complete, exit.** The wake mechanism is interchangeable.

---

## 9. Context preservation + token/cost controls (criteria 7 + 8)

**Context preservation (criterion 7).** Each directive's `context` block assembles, from existing sources:
- **persona** — `persona_ref` (name); the agent expands via `get_persona` / `start_ticket`'s compiled bundle only when it actually needs the full markdown.
- **ticket context** — title/status/kind inline; full description/acceptance/file_refs via `get_ticket` on demand.
- **KB/wiki refs** — `expand_hints` point at `get_context_section`, `search_project_knowledge`, `get_wiki_page`.
- **review history** — `review_state` (compact, LLM-free) inline; raw thread via `list_ticket_comments` delta only.
- **AgentRun summaries** — `last_agent_run_id` + `list_agent_runs` for prior-execution `result_summary`/`findings`.
- **session checkpoint/fork refs** — optional. For long implementation loops the agent may `checkpoint_session` / `fork_session` (existing tools, `mcp/server.py`) and store the ref in `complete_work_queue_step.summary`; the next wake's directive surfaces it under `context.session_ref` so work continuity survives across wakes.

**Token/cost controls (criterion 8).** The whole design is "**small delta per wake, expand on demand**":
- Default directive carries only `review_state` (compact) + the comment **delta** since the cursor — never the whole thread. `review_state` is the explicit cheap substitute for re-reading comments (`mcp/server.py:936-938`: *"Far cheaper context than `list_ticket_comments` on a long thread"*).
- `max_tickets_per_run` (default **1**) bounds work per wake → bounds tokens per wake.
- `expand_hints` is a *menu*, not a payload — the agent pulls full context only when the delta is insufficient.
- Comment-delta and AgentRun list use `limit` caps (already 1–500 on `list_ticket_comments`).
- No directive embeds full session transcripts; only summaries/refs.

---

## 10. Memory writeback rules (criterion 9)

The loop must leave a durable trail in SessionFS's own memory layer (this is dogfooding the product). Rules:

| Action | When | Fencing / provenance |
|--------|------|----------------------|
| `add_ticket_comment` | every review verdict + every implementer progress/closure note | MUST pass `ticket_lease_epoch` from the directive. The auto-posted comment carries `author_persona = queue.assigned_persona` + `actor_type`/`service_key_*` so the audit shows it was a queue-driven agent, not a human. **Idempotency:** `complete_work_queue_step` records `comment_id`; a wake that already posted for the current `(item, lease_epoch)` must NOT post again (§11 duplicate-comment guard). |
| `complete_ticket` / `resolve_ticket` | implementer finishing a ticket | lease-fenced (existing). Writes `completion_notes` + `knowledge_entry_ids`. |
| `add_knowledge` | when the loop discovers something durable (a fix pattern, a recurring finding, a workaround) | per CLAUDE.md mandatory KB rule. `author_class`/`persona_name` set to the queue persona. Confidence honored (not clamped — v0.10.10 fix). De-dup against active claims before adding. |
| `update_wiki_page` | substantial findings (a review post-mortem, a design clarification) | provenance-validated (`persona_name`/`ticket_id` plumbed — v0.10.7 `_validate_revision_provenance`). |
| `complete_agent_run` | end of each per-ticket execution | writes `result_summary` + `findings` + `policy_result`; this is the per-execution audit the `work_queue_runs.agent_run_id` links to. |

**Writeback discipline:** no-op writes are forbidden (matches the `update_ticket` "no-op writes do NOT post — audit-pollution anti-pattern", CLAUDE.md v0.10.28). A wake that determines "nothing changed, still waiting" appends a `work_queue_runs` row with `action="waited"` but posts **no** ticket comment.

---

## 11. Security & concurrency risks + mitigations (criterion 11)

This section is the **Sentinel review surface**. Every risk maps to an existing or proposed control.

| # | Risk | Mitigation |
|---|------|-----------|
| R1 | **Double workers** (two cron jobs / a cron + a `/loop` on the same queue) double-process an item. | Item pick uses `SELECT ... FOR UPDATE SKIP LOCKED` on `work_queue_items` inside the run-step transaction; an item picked by one wake is invisible to a concurrent wake. The item's `item_status` flips to `active`/`waiting_*` inside the same txn. SQLite local-mode degrades to single-writer (acceptable; SKIP LOCKED is a PG optimization, not a correctness requirement given the status flip). |
| R2 | **Stale cursor** — agent acts on a directive built from an old `lease_epoch`; ticket changed underneath. | Every directive carries `ticket_lease_epoch`; every writeback (`add_ticket_comment`/`complete_ticket`/`complete_work_queue_step`) MUST pass it and is rejected 409 on mismatch (existing pattern `routes/tickets.py:1252-1262, 421-433`). A 409 → the agent re-runs `run_work_queue_step` to get a fresh directive. |
| R3 | **Duplicate comments** — a wake retries (network blip) and posts the same review twice. | (a) `complete_work_queue_step` is the commit point; it records `comment_id` + the `lease_epoch` it acted under on `work_queue_items`. (b) The next `run_work_queue_step` will NOT emit a fresh `post_review` directive for the same `(item, lease_epoch)` if `complete_*` already recorded one — it returns the recorded outcome as idempotent. (c) The ticket-write lease bump (`add_ticket_comment` advances `lease_epoch` when fenced) means a stale retry 409s. |
| R4 | **lease_epoch / fencing bypass** — agent omits the epoch. | Org setting `require_lease_epoch_on_ticket_writes` (existing, `routes/tickets.py:472`) forces 422 on omitted lease. Queues SHOULD set this on their org. `complete_work_queue_step` requires `ticket_lease_epoch` unconditionally (stricter than the ticket routes). |
| R5 | **Permission boundaries** — a service-key-driven CI loop touches tickets outside its org/project. | Reuse the v0.10.10 service-key model wholesale: `require_scope("work_queues:read"/"work_queues:write")` (2 new scopes added to the 14-scope catalog), `assert_service_key_can_access_project`, deny-by-default `get_current_user` rejects service keys, project-allowlist on the key. A queue is project-scoped (FK CASCADE); cross-project items are impossible by construction. |
| R6 | **Runaway loop** — a queue burns tokens/$$ forever (a finding that never closes; an agent stuck re-reviewing). | (a) `max_tickets_per_run` bounds per-wake. (b) `work_queue_items.attempts` increments each wake; a per-queue `max_attempts_per_item` (config, default e.g. 12) → item auto-flips to `failed` + `work_queue_runs(action="errored")` and stops being picked. (c) `next_eligible_at` backoff (exponential on repeated no-progress wakes) caps wake frequency on a stuck item. (d) Queue-level `status='completed'` on `all_clean`/`max_tickets` stop conditions. (e) Wake-source rate limiting at the edge (Cloud Armor, already live for activation/helm — extend the deny-429 path to `run_work_queue_step`). |
| R7 | **False watcher claims** — the loop marks a ticket `done`/clean when it isn't. | The stop oracle is `services/review_state.py`, the **same** LLM-free parser the team trusts, computed **server-side** at `complete_work_queue_step` (re-derive, don't trust the agent's `verdict` claim). The agent can *post* a verdict comment but cannot *declare the item done* — the server re-runs `compute_review_state` and only marks `done` if `last_verdict=="VERIFIED-CLEAN"` AND `open_findings==[]`. This is the single most important control: **the server, not the agent, decides "clean."** |
| R8 | **Confused-deputy / impersonation** — queue comments look like a human's. | `actor_type`/`service_key_id`/`service_key_name` provenance triple on every `work_queue_runs` row and every `TicketComment` the loop posts (existing convention). `author_persona` always set. Audit trail distinguishes queue-driven from human action. |
| R9 | **Queue-config race** — two admins edit a queue's selector/mode concurrently. | `work_queues.lease_epoch` + atomic `UPDATE WHERE lease_epoch=N` on every queue mutation (pause/resume/complete/edit), 409 on mismatch. |
| R10 | **Tenant data leak via selector** | Selector filters are applied within `project_id` scope only; hydration query is `WHERE tickets.project_id = queue.project_id`. No cross-project ticket can ever enter a queue. |

**Sentinel must rule on:** R6's default attempt cap + backoff curve; whether `run_work_queue_step` needs its own edge rate-limit class; whether `work_queues:*` scopes should be Team+ tier-gated (Compass recommends yes — see §13).

---

## 12. CLI as secondary surface (criterion 10)

CLI is **admin/observability + parity**, never the primary agent path. Proposed (mirrors `sfs ticket` / `sfs agent`):

| Command | Purpose |
|---------|---------|
| `sfs queue create --name --mode --persona --selector-file --stop-condition --cadence --max-per-run` | define a queue (admin) |
| `sfs queue list [--status]` | list queues + progress |
| `sfs queue show <id> [--items]` | inspect cursor state |
| `sfs queue step <id> [--wake-source manual]` | manually drive one wake (debugging) — prints the directive; does NOT auto-act (an agent acts; the CLI is for humans inspecting) |
| `sfs queue pause/resume/complete <id>` | lifecycle |

**Parity expectation:** CLI covers create/inspect/lifecycle (the human-admin surface). The *agent loop* (`run_work_queue_step` → act → `complete_work_queue_step`) is **MCP-only in v1** — the CLI `step` command is a read-only directive printer for humans, not an actor, because acting requires an LLM runtime which the CLI doesn't have. This is deliberate and matches the existing split (e.g. lifecycle FSM transitions stayed "CLI/MCP-only because they need the local active-ticket bundle", v0.10.3).

---

## 13. Packaging / tier (Compass recommendation — Ledger/CEO call)

- **Individual/free:** may create **one** `manual`-cadence queue, `max_tickets_per_run=1`, no service-key/CI wake (local `/loop` only). Lets a solo dev automate their own review loop — a strong free-tier hook that showcases the product.
- **Team+:** multiple queues, cron/CI wake via service keys, `work_queues:*` scopes, higher `max_tickets_per_run`. The autonomous multi-agent loop is a team-coordination capability → fits the "team handoff is the monetization wedge" thesis (CLAUDE.md Key Decisions).
- This is a **recommendation**, not a commitment. Ledger + CEO own the final gate.

---

## 14. Staged implementation plan (criterion 12)

### v1 — MCP-only minimum viable queue (the wedge)
- Migration 053: `work_queues`, `work_queue_items`, `work_queue_runs` (additive; CHECK constraints in 052 style).
- `services/work_queues.py`: hydration, `pick_eligible` (FOR UPDATE SKIP LOCKED), the two mode algorithms, server-side stop-oracle via `compute_review_state` (reused).
- Routes: `POST/GET/list /api/v1/projects/{pid}/work-queues`, `POST .../{id}/step`, `POST .../{id}/complete-step`, `POST .../{id}/{pause,resume,complete}`. Lease-fenced. Service-key scopes `work_queues:read/write` added to the catalog.
- 5 new MCP tools (§4.1–4.6): `create_work_queue`, `get_work_queue`, `list_work_queues`, `run_work_queue_step`, `complete_work_queue_step` (+ pause/resume/complete folded into one `set_work_queue_status` to keep tool count down → 62 → ~67).
- **Mode `review_until_clean` first** (it's the proven manual loop), then `implement_until_done`. `triage` deferred.
- Wake via Claude `/loop` only (no service-key CI in v1 if we want to ship faster; or include CI if service-key scopes land cleanly).
- Tests: hydration idempotency, SKIP LOCKED double-worker, lease-fence 409, the stop-oracle terminate-on-VERIFIED-CLEAN path (the test that proves the loop ends), runaway attempt-cap, duplicate-comment idempotency.

### v1.1 — CLI + dashboard
- `sfs queue *` CLI (§12).
- Prism dashboard: a **Queues** tab on ProjectDetail — list queues, per-item cursor table (item_status, last_verdict, attempts, next_eligible_at), a `work_queue_runs` timeline, pause/resume controls. Read-mostly; humans observe the autonomous loop.
- CI wake template (`docs/integrations/github-actions-work-queue.yml`) with scoped service key.

### v2 — event-driven wake (design sketch only here)
- A new TicketComment by the implementer / a CI completion flips `next_eligible_at=now` and (optionally) pushes a `RemoteTrigger` to a runner so the reviewer wakes immediately instead of on the next cron tick.
- Subscription model: a queue subscribes to ticket-event types. Still HTTP-driven (a webhook flips a flag; a poller/runner reacts) to honor "NO WebSockets/Redis" — or a deliberate Key-Decision amendment if true push is wanted. **Requires CEO sign-off on the no-realtime exception.**

---

## 15. Acceptance criteria coverage map

| # | Criterion | Section |
|---|-----------|---------|
| 1 | WorkQueue distinct from Ticket/Persona/AgentRun/Session | §2, §2.1 |
| 2 | MCP-first create/inspect/run/pause/complete tools + shapes | §4 |
| 3 | REVIEW-loop step algorithm | §5 |
| 4 | IMPLEMENTATION/closing step algorithm | §6 |
| 5 | Durable cursor/state model | §3.2, §7 |
| 6 | /loop, cron, CI, future wake call the step w/o chat memory | §8 |
| 7 | Context preservation (persona/ticket/KB/review/AgentRun/session) | §9 |
| 8 | Token/cost controls (small delta, expand on demand) | §9 |
| 9 | Memory writeback rules | §10 |
| 10 | CLI secondary surface only | §12 |
| 11 | Security/concurrency risks + mitigations | §11 |
| 12 | Staged plan v1/v1.1/v2 | §14 |
| + | Problem statement / why-now | §1 |
| + | Data-model sketch | §3 |
| + | review-until-clean spec using VERIFIED-CLEAN stop | §5 |

---

## 16. Open questions for CEO / Atlas / Sentinel

1. **(CEO) Tier gate** — is the §13 packaging (free=1 manual queue, Team+=CI/cron) the right monetization line? This determines whether `work_queues:*` scopes are Team-gated.
2. **(CEO) v2 no-realtime exception** — event-driven wake (§14) wants a webhook→trigger path. Acceptable within "HTTP + ETags only," or does it need an explicit Key-Decision amendment?
3. **(Atlas) `work_queue_runs` vs extend `AgentRun`** — separate table (Compass rec, §3.3) or overload AgentRun? Affects whether a no-op wake creates an audit row.
4. **(Atlas) Stop authority** — should `complete_work_queue_step` **re-derive** `review_state` server-side to confirm "clean" (Compass strong rec — R7), or trust the agent's `verdict`? Compass says re-derive; this is the false-watcher-claim control.
5. **(Atlas) Tool count** — fold pause/resume/complete into one `set_work_queue_status` (keeps us at ~67 tools) vs three separate tools (clearer descriptions)? Compass leans folded.
6. **(Sentinel) Runaway defaults** — `max_attempts_per_item` default and backoff curve (§11 R6); and whether `run_work_queue_step` needs a dedicated Cloud Armor deny-429 class like activation/helm-validate.
7. **(Sentinel) Re-review of `services/review_state.py` as a security boundary** — it now becomes a **stop-authority oracle**, not just a renderer. Its regex parser tolerates malformed input by returning `None`/open findings — confirm a malformed thread can never false-positive a `VERIFIED-CLEAN` stop (current logic requires `last_verdict=="VERIFIED-CLEAN"` AND `open_findings==[]`, which Compass believes is safe, but Sentinel should bless it).
8. **(Atlas/Compass) Hydration scope drift** — a filter-only queue (`status=review`) auto-adopts new review tickets. Is silent auto-adoption desired, or should new matches require an explicit confirm? Compass leans auto-adopt (that's the point of a queue) but flag for review.
9. **(Atlas) SQLite local-mode** — `FOR UPDATE SKIP LOCKED` is a no-op on SQLite; the `item_status` flip inside the txn is the correctness guard there. Confirm acceptable for self-hosted single-writer deployments.
```
