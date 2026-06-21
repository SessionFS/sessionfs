# Binding Design — MCP-First Agent Work Queues (autonomous ticket-closing loops)

**Status:** 🟡 DRAFT (R2) — Compass product+design draft, revised to address the Atlas + Sentinel R1 design reviews (both NEEDS-CHANGES). Routes back to Atlas (backend/data-model/API contract), Sentinel (concurrency + tenant-boundary + runaway-loop + trusted-provenance threat model), and CEO (scope/sequencing approval) before any implementation ticket is cut. NOT cleared to build.
**Ticket:** `tk_c2ed6093acde4d55` — "Design MCP-first Agent Work Queues for autonomous ticket closing loops"
**Author:** Compass (product intent + acceptance boundaries)
**Co-owners on accept:** Atlas (how — schema/routes/MCP), Sentinel (security conditions), Prism (v1.1 dashboard)
**Date:** 2026-06-20
**Blocking dependency:** `tk_4849c5db3dab47c9` — **trusted-verdict-provenance boundary** (Sentinel R1 BLOCKER). The stop oracle MUST NOT trust caller-supplied `author_persona`. This ticket MUST land before `review_until_clean` ships. See §5.0, §11 R7/R11.
**Grounded against:** codebase at `develop` as of 2026-06-20 (migrations 001–052; 62 MCP tools; `src/sessionfs/server/routes/tickets.py`, `routes/agent_runs.py`, `services/review_state.py`, `mcp/server.py`, `db/models.py`, `active_ticket.py` all read and cited below by real path + line).

---

## Revision History

| Rev | Date | Changes |
|-----|------|---------|
| D1 | 2026-06-20 | Initial Compass draft. Full coverage of all 12 acceptance criteria + problem statement, data-model sketch (`work_queues` / `work_queue_items` / `work_queue_runs`, migration 053), MCP tool surface + JSON shapes, REVIEW-loop + IMPLEMENTATION-loop step algorithms, durable cursor model, wake-mechanism integration (/loop, cron, CI), token/cost controls, writeback rules, CLI parity expectations, security/concurrency threat list, staged plan, and the explicit `review-until-clean` mode spec built on `services/review_state.py`'s `VERIFIED-CLEAN` closure rule. |
| R2 | 2026-06-20 | Revised for Atlas + Sentinel R1 reviews (both NEEDS-CHANGES). **Sentinel BLOCKER:** added §5.0 trusted-verdict-provenance boundary — the stop oracle never trusts caller-supplied `author_persona`; verdicts count only when provenance is server-trusted; queue-posted comments derive `author_persona` server-side from queue config; gated on blocking dep `tk_4849c5db3dab47c9` (also R7/R11). **Atlas HIGH:** split the comment cursor into `last_seen_*` (server-shown) vs `last_acked_*` (durably reviewed) — only `complete_work_queue_step` advances the ACKED cursor after validating the write landed; added a re-emittable **directive lease** (`work_queue_run_id`/`directive_id`) so a crash between directive and writeback replays rather than loses (§3.2, §3.3, §4.4, §4.5, §5, §7, §11 R2/R3). **Atlas MED:** atomic claim is now `UPDATE ... WHERE item_status IN (...) AND next_eligible_at<=now` rowcount==1 as the cross-DB correctness primitive; `SKIP LOCKED` demoted to a PG throughput note (§5, §11 R1, §14). Adopted Atlas answers: `work_queue_runs` stays separate (link via `agent_run_id`); one `set_work_queue_status` verb with server-side allowed transitions replaces pause/resume/complete; `auto_adopt` is now explicit queue config with per-wake caps + every adopted ticket materialized before any action; migration 053 strictly additive (down_revision=052), inline CheckConstraint, RETURNING/explicit IDs (no lastrowid), direct SQLite up/down tests, added claim index `(work_queue_id, item_status, next_eligible_at)`. **Sentinel MED:** strict `VERIFIED-CLEAN`-only auto-stop (aliases NOT sufficient, §5.0); concrete v1 runaway contract (§9.1); dedicated rate-limit class for `run_work_queue_step` as a REQUIREMENT (§11 R6); directive-token idempotency on writeback (§11 R3); permission boundary `work_queues:read` to inspect, `work_queues:write` + `tickets:write`/`agent_runs:write` to act, with `assert_service_key_can_access_project` AND `queue.project_id==ticket.project_id` on every query (§11 R5/R10). §15 coverage map kept accurate; new §5.0 row added. |

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

**(R2) Migration mechanics — Atlas requirements (matches migration 052's known-good shape):**
- **Strictly additive, `down_revision = '052'`** — no edits to migrations 001–052; already-migrated DBs unaffected.
- **Inline `CheckConstraint` declared inside `op.create_table(...)`** (NOT a follow-up `op.create_check_constraint`) so the chain stays SQLite-applicable — the same pattern v0.11.0's 052 used after the SQLite inline-CHECK lesson (KB v0.11.0 P1).
- **No `lastrowid`** — these tables use server-generated `String(64)` PKs assigned in app code (or PG `RETURNING` / explicit IDs where a generated id is read back). Never rely on integer autoincrement / `cursor.lastrowid`.
- **Direct `upgrade()` / `downgrade()` SQLite tests** — a unit test runs the migration's `upgrade()` then `downgrade()` against an in-memory SQLite DB and asserts table create/drop + CHECK enforcement, so the local `/release` flow catches a broken chain before tag (the v0.10.7 SQLite-chain lesson).
- Indexes include the **(R2) claim index** `idx_wqi_claim (work_queue_id, item_status, next_eligible_at)` covering the atomic-claim predicate (§5).

### 3.1 `work_queues` — the durable loop definition

| Column | Type | Notes |
|--------|------|-------|
| `id` | String(64) PK | `wq_<hex>` |
| `project_id` | String(64) FK→projects.id CASCADE, NOT NULL | tenant scope |
| `name` | String(100) NOT NULL | human label; `uq_work_queue_project_name` (project_id, name) |
| `mode` | String(20) NOT NULL | `review_until_clean` \| `implement_until_done` \| `triage` (CHECK constraint, migration 052 style) |
| `assigned_persona` | String(50), nullable | persona name that acts (plain string like `Ticket.assigned_to`; validated at run time, not FK) |
| `selector` | Text NOT NULL DEFAULT `'{}'` | JSON filter — see §3.4 |
| `auto_adopt` | Boolean NOT NULL DEFAULT `false` | **(R2, Atlas)** when true, the selector filter is re-run each wake and newly-matching tickets are **materialized as `work_queue_items` rows before any action** (§7 hydration). When false, only `selector.ticket_ids` seed items and no silent adoption happens. **Explicit, not implicit.** |
| `max_adopt_per_wake` | Integer NOT NULL DEFAULT 5 | **(R2, Atlas)** per-wake cap on how many newly-matched tickets `auto_adopt` may materialize in one wake (prevents a 500-ticket filter from flooding the queue in a single step). |
| `stop_condition` | String(30) NOT NULL DEFAULT `'queue_empty'` | `queue_empty` \| `all_clean` \| `max_tickets` \| `manual` (CHECK) |
| `cadence_seconds` | Integer NOT NULL DEFAULT 300 | advisory wake interval (informational; wake mechanism enforces). **(R2)** floor 120s, default 300s (§9.1). |
| `max_tickets_per_run` | Integer NOT NULL DEFAULT 1 | budget per wake (§9). **(R2)** hard max 5 (§9.1). |
| `max_attempts_per_item` | Integer NOT NULL DEFAULT 3 | **(R2)** after this many EMITTED directives/action attempts with no progress, the item flips to `failed` and requires human reset (§9.1, §11 R6). |
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
| `last_seen_comment_at` | DateTime(tz), nullable | **(R2)** newest comment the server has *shown* in a directive (the `since` floor for the next delta fetch). Advanced when a directive is emitted. NOT proof the agent acted. |
| `last_seen_comment_id` | String(64), nullable | **(R2)** `since_id` tiebreaker for `last_seen_comment_at` (pairs with above — `mcp/server.py:912-919`). |
| `last_acked_comment_at` | DateTime(tz), nullable | **(R2)** newest comment that was *durably reviewed* — advanced ONLY by `complete_work_queue_step` after the agent's writeback is validated/committed. The stop oracle and "is it the reviewer's turn?" check read the ACKED cursor, never the SEEN cursor. A crash between directive and writeback leaves SEEN ahead of ACKED → the directive re-emits (no review lost/replayed). See §4.4/§4.5/§5/§11 R2. |
| `last_acked_comment_id` | String(64), nullable | **(R2)** `since_id` tiebreaker for `last_acked_comment_at`. |
| `open_directive_id` | String(64), nullable | **(R2)** directive lease — the `directive_id` of the currently-outstanding directive for this item (null when none open). Set when `run_work_queue_step` emits; cleared by `complete_work_queue_step`. While set, the same directive is **re-emittable** (idempotent) rather than producing a new one — this is the at-least-once-emit / exactly-once-ack contract. |
| `open_directive_run_id` | String(64), nullable | **(R2)** the `work_queue_runs.id` (`work_queue_run_id`) that emitted the open directive — joins the lease to its audit row. |
| `last_agent_run_id` | String(64), nullable | the most recent `AgentRun` this loop produced for this ticket |
| `last_review_round` | Integer, nullable | highest Codex round seen (from `ReviewState.rounds`) |
| `last_verdict` | String(20), nullable | snapshot of `ReviewState.last_verdict` at last ack (strict `VERIFIED-CLEAN` / `CHANGES_REQUESTED`) |
| `last_ticket_lease_epoch` | Integer, nullable | the ticket's `lease_epoch` the loop last acted under (fencing — §11) |
| `attempts` | Integer NOT NULL DEFAULT 0 | **count of EMITTED directives / action attempts** for this item (runaway-loop guard — §9.1, §11). Passive waits do NOT increment it. |
| `next_eligible_at` | DateTime(tz), nullable | earliest the item should be re-picked (backoff). Part of the atomic claim predicate (§5). |
| `note` | Text, nullable | last human-readable status (`"R3 CHANGES_REQUESTED — 1 HIGH open"`) |
| `created_at` / `updated_at` | DateTime(tz) | standard |

Indexes: `idx_wqi_queue_status (work_queue_id, item_status)`, `idx_wqi_ticket (ticket_id)`, and the **(R2) claim index** `idx_wqi_claim (work_queue_id, item_status, next_eligible_at)` (covers the atomic-claim predicate in §5; replaces the prior `idx_wqi_next_eligible`).

### 3.3 `work_queue_runs` — append-only wake audit

One row per **wake** (per call to the run-step tool). Makes the loop observable and bounds runaway behavior.

| Column | Type | Notes |
|--------|------|-------|
| `id` | String(64) PK | `wqr_<hex>`. This id IS the `work_queue_run_id` returned to the caller (the directive-lease handle — §4.4). |
| `work_queue_id` | String(64) FK→work_queues.id CASCADE, NOT NULL | |
| `directive_id` | String(64), nullable | **(R2)** the directive emitted by this wake (null for no-op/waited/stopped wakes that emit nothing). Stamped onto `work_queue_items.open_directive_id`; the idempotency key for writeback (§11 R3). |
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

> **(R2 — Atlas DECIDED, was decision point):** `work_queue_runs` stays a **separate table**, NOT a reuse/extension of `AgentRun`. `AgentRun` is "one persona execution"; a wake can be a no-op or a pure poll that produces no `AgentRun`. When a wake *does* produce a persona execution, the `work_queue_runs` row links it via `agent_run_id`. This keeps the audit trail joined without overloading `AgentRun`'s FSM. (Resolves Atlas answer #1 and former open question §16.3.)

---

## 4. MCP-first tool surface (criterion 2 + 10)

**MCP is the primary agent surface.** Agents call MCP tools far more reliably than CLI (every existing ticket/agent-run MCP tool description ends with *"Always use this MCP tool instead of running `sfs ... ` CLI"* — e.g. `mcp/server.py:848-849, 977-978`). CLI is a **secondary admin/parity surface** (§10).

Proposed new MCP tools (additive; current count 62 → **68**: `create_work_queue`, `get_work_queue`, `list_work_queues`, `run_work_queue_step`, `complete_work_queue_step`, `set_work_queue_status`). All take `git_remote` (auto-detected) and route through the existing `_resolve_project_id` resolver used by every project-scoped MCP tool.

**(R2) Permission boundary (Sentinel #9), enforced on every tool:**
- **Inspect / step-read** (`get_work_queue`, `list_work_queues`) require `work_queues:read`.
- **Act** (`create_work_queue`, `run_work_queue_step`, `complete_work_queue_step`, `set_work_queue_status`) require `work_queues:write` **AND** the downstream write scopes the action exercises — `tickets:write` (queue posts comments / drives FSM) and, where the wake opens an execution audit, `agent_runs:write`.
- Every item/hydration/directive query enforces BOTH `assert_service_key_can_access_project(ctx, queue.project_id)` (v0.10.10 helper) **AND** `queue.project_id == ticket.project_id` — a queue can never touch a ticket outside its own project (defense-in-depth on top of the FK CASCADE, §11 R10).
- Every queue-posted `TicketComment` and every `work_queue_runs` row stamps `actor_type`/`service_key_id`/`service_key_name` so a queue action is never indistinguishable from a human (§11 R8).

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

The single tool a wake mechanism calls. It does **bounded** work: picks up to `max_tickets_per_run` eligible items via the **atomic claim** (§5, not a passive read), runs the mode-appropriate algorithm (§6, §7) for each, advances the **SEEN** cursor (never the ACKED cursor), and returns a **directive** — with a re-emittable **directive lease** — telling the calling agent exactly what to do this wake.

**(R2) This is NOT a passive read — it has its own rate-limit class (Sentinel #7, REQUIREMENT not "mention").** It claims work, emits directives, and drives ticket writes + token spend. Both an app-level quota AND a Cloud Armor deny-429 path are REQUIRED, keyed by `org_id` / `project_id` / `service_key_id` / `work_queue_id` / source IP (the same edge pattern already live for `activate`/`helm-validate`, CLAUDE.md v0.11.0). See §11 R6.

**(R2) Directive lease / idempotency.** The response carries a `work_queue_run_id` (the `work_queue_runs.id`) and, when a directive is emitted, a `directive_id`. While an item has an `open_directive_id` set and unacked, re-calling `run_work_queue_step` **re-emits the same directive** (same `directive_id`) rather than minting a new one — so a crash between directive and writeback replays the same instruction instead of losing or double-counting it. The directive is settled only by `complete_work_queue_step` (§4.5), which is the sole writer of the ACKED cursor.

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
  "work_queue_run_id": "wqr_9f",              // (R2) the audit row + directive-lease handle for this wake
  "stop": false,
  "stop_reason": null,
  "directives": [
    {
      "directive_id": "dir_4c",               // (R2) lease handle; re-emittable until acked; idempotency key for writeback
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
        "directive_id": "dir_4c",            // (R2) MUST be passed back on complete/add_comment — idempotency key
        "post_via": "add_ticket_comment",
        "required_lease_epoch": 7,
        "author_persona_is_server_derived": true,  // (R2) the loop NEVER sends author_persona; the server stamps it from queue config (§5.0)
        "on_clean": "complete_work_queue_step(directive_id=dir_4c, verdict=clean)",
        "on_changes": "complete_work_queue_step(directive_id=dir_4c, verdict=changes)"
      }
    }
  ],
  "next_eligible_at": "2026-06-20T20:10:00Z"
}
```

Key properties:
- **(R2) The SEEN cursor — NOT the ACKED cursor — is advanced here.** This wake reads `review_state` against the ACKED cursor, fetches the comment delta, and advances `last_seen_comment_*` so the agent gets a **small delta**, not the whole thread (criterion 8). It does **NOT** advance `last_acked_comment_*` — that happens only in `complete_work_queue_step` after the writeback lands. A crash now leaves SEEN > ACKED → the next wake re-emits the same `directive_id` (no review lost, none replayed). This closes Atlas R1 HIGH #1.
- The server returns `ticket_lease_epoch` so the agent's subsequent writes are **fenced** (criterion 11).
- `intent` tells the agent the one thing to do. `expand_hints` lists the MCP tools to call **only if** it needs more context (criterion 8 — expand on demand).
- **(R2) The agent never supplies `author_persona`.** When it posts via `add_ticket_comment` under this directive, the server derives the author identity from the queue config (§5.0). A `directive_id` accompanies the write so the server can attach trusted provenance and reject duplicates (§11 R3).
- If nothing is eligible → `directives: []`, and `stop`/`stop_reason` reflect the stop condition (e.g. `stop:true, stop_reason:"all_clean"`).

### 4.5 `complete_work_queue_step` — record the outcome of a directive

After the agent acts on a directive (posts a review, posts progress, completes a ticket), it calls this to **settle the directive lease and advance the ACKED cursor**. This is the single commit point of the loop — the only place `last_acked_comment_*` moves.

Request:
```json
{
  "work_queue_id": "wq_1a2b",
  "item_id": "wqi_01",
  "directive_id": "dir_4c",              // (R2) REQUIRED — the lease being settled; duplicate settle is idempotent
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
- **(R2) `directive_id` is REQUIRED** and must equal the item's `open_directive_id`. If the lease is already settled (a retry of the same `directive_id`) the call is **idempotent** — it returns the recorded outcome and does NOT advance the cursor again or post again. A mismatched `directive_id` (stale/forged) is rejected. This closes Atlas R1 HIGH #1 and Sentinel #8 (directive-token idempotency).
- **(R2) Validates the write landed before advancing.** The server confirms the agent's claimed `comment_id` exists on `ticket_id`, was posted under the current `(item, lease_epoch)`, and carries the queue's server-derived trusted provenance (§5.0) — only then does it advance `last_acked_comment_*` to the newest comment the directive showed. If validation fails, the lease stays open and the directive re-emits next wake.
- Fences on `ticket_lease_epoch` (409 on mismatch — same as `update_ticket`, `routes/tickets.py:1252-1262`).
- **(R2) For `verdict` in review modes the server RE-DERIVES `review_state`** server-side (does not trust the agent's `verdict`) and only marks the item `done` when the **strict** stop condition holds (§5.0/§5). The agent's `verdict` is a hint for bookkeeping, never the authority for closure.
- Updates `work_queue_items` cursor (`item_status`, `last_verdict`, `last_review_round`, `attempts` — incremented only for emitted-directive settles, never passive waits, §9.1, `next_eligible_at` backoff), clears `open_directive_id`/`open_directive_run_id`, and stamps the settling `work_queue_runs` row with the `directive_id`.
- Returns the updated item + whether the **item is now terminal** (`item_status in {done, failed, skipped}`).

### 4.6 `set_work_queue_status` — one lifecycle verb (R2, Atlas answer #3)

Lifecycle control (criterion 2). **One** tool replaces the former separate `pause_work_queue`/`resume_work_queue`/`complete_work_queue` verbs. Request: `{ "work_queue_id": "wq_1a2b", "status": "paused", "lease_epoch": 3, "git_remote": "" }`.

- Target `status` is one of `active` \| `paused` \| `completed` \| `archived`.
- **Server-side allowed-transition table** (the server, not the caller, enforces legality):

| From \ To | active | paused | completed | archived |
|-----------|:------:|:------:|:---------:|:--------:|
| active    | —      | ✓      | ✓         | ✓        |
| paused    | ✓ (resume) | — | ✓         | ✓        |
| completed | ✓ (reopen) | ✗ | —         | ✓        |
| archived  | ✗      | ✗      | ✗         | — (terminal) |

- `paused` → `run_work_queue_step` returns `stop:true, stop_reason:"paused"` and does no work. `completed`/`archived` are terminal stops.
- Fences on the queue-level `lease_epoch` (atomic `UPDATE WHERE lease_epoch=N`, 409 on mismatch — §11 R9). An illegal transition is rejected with a structured `invalid_status_transition` error.

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

This mode **is the manual loop, automated.** Its stop condition is computed by the existing, LLM-free `services/review_state.py` — **but only over comments whose verdict provenance the server trusts** (§5.0).

### 5.0 TRUSTED-VERDICT-PROVENANCE BOUNDARY (Sentinel R1 BLOCKER — `tk_4849c5db3dab47c9`)

**This is the blocking condition for the entire mode. It MUST land before `review_until_clean` ships.**

The stop oracle MUST NOT trust caller-supplied `author_persona`. Today `author_persona` is **request-body data** on the comment-create path (`routes/tickets.py:336` and `:2077`), and `services/review_state.py` treats `author_persona == 'codex-reviewer'` (`'codex'`) as an **authoritative reviewer verdict**. That means **any `tickets:write` caller can forge a `VERIFIED-CLEAN`** and auto-close a ticket / stop a queue. An autonomous loop that trusts this is a self-driving rubber stamp.

R2 requires a **trusted-verdict-provenance boundary** before any auto-stop can read a verdict:

1. **The stop oracle counts a comment as a verdict ONLY when its provenance is server-trusted** — i.e. the verdict-bearing comment carries a server-set trust marker, established by ONE of: (a) **service-key binding** (`service_key_id` of an allowlisted reviewer identity), (b) an **allowlisted reviewer identity** configured on the queue/org, or (c) **queue-stamped provenance** (the comment was posted *by this queue* with the server-derived author — see #2). The raw `author_persona` string is **never** sufficient on its own.
2. **Queue-posted comments derive `author_persona` server-side from the queue config**, never from caller input. The loop calls `add_ticket_comment` under a `directive_id`; the server ignores any caller `author_persona` and stamps the queue's configured reviewer identity + the `actor_type`/`service_key_*` triple. This is why §4.4's `writeback_contract` carries `author_persona_is_server_derived: true`.
3. **`services/review_state.py` is a deterministic REDUCER over already-trusted comments — it is NOT itself a security boundary** (Sentinel #5). It must run *after* the trust filter, never as the trust filter. The design states this explicitly so no future change mistakes the parser for the gate.

This boundary is tracked as the BLOCKING dependency ticket **`tk_4849c5db3dab47c9`** and is also recorded as risk R7/R11 in §11. Until it lands, `review_until_clean` auto-stop is **not buildable**.

**Stop oracle (over trusted comments only).** After the trust filter (§5.0), the server calls `compute_review_state(trusted_comments)` (`services/review_state.py:219`) for the item's ticket. The `VERIFIED-CLEAN` closure rule (`review_state.py:33-38, 269-294`) is the contract:
> *Findings raised in round N are closed when ANY subsequent Codex round has verdict `VERIFIED-CLEAN`.*

**(R2) STRICT phrase only (Sentinel #5).** `review_state.py` aliases `APPROVED` / `NO CHANGES NEEDED` → `VERIFIED-CLEAN` for its renderer. For an **autonomous auto-stop** those aliases are **NOT sufficient** — only the exact, strict `VERIFIED-CLEAN` phrase may end a loop unattended. A directive's stop check ignores the aliases. (Rationale: a loop closing tickets without a human in the seat must demand the canonical verdict the team uses for "done"; the looser aliases are a renderer convenience, not an autonomy contract.) The aliases-not-accepted behavior is itself a required test (§14).

The item is **clean / terminal** when:
```
review_state is not None
AND review_state.last_verdict == "VERIFIED-CLEAN"   # STRICT — aliases do NOT qualify for auto-stop
AND len(review_state.open_findings) == 0
AND the VERIFIED-CLEAN-bearing comment passed the §5.0 trust filter
```
(Belt-and-suspenders: a clean last verdict implies no open findings under the closure rule, but we assert all conditions so neither a malformed thread nor a forged-provenance comment can false-positive a stop.)

**Per-wake algorithm (REVIEW agent, e.g. `codex-reviewer`):**

```
run_work_queue_step(queue):
  rid = mint work_queue_runs.id            # this wake's audit/lease handle (work_queue_run_id)
  # ATOMIC CLAIM (§11 R1) — the cross-DB correctness primitive (works on PG AND SQLite):
  #   UPDATE work_queue_items
  #      SET item_status='active', open_directive_run_id=:rid, updated_at=now
  #    WHERE id=:id AND item_status IN ('pending','waiting_review','waiting_implementation')
  #      AND (next_eligible_at IS NULL OR next_eligible_at <= now)
  #   -- proceed only if rowcount == 1; rowcount 0 means another wake won the item.
  #   (On PG, SELECT ... FOR UPDATE SKIP LOCKED is an OPTIONAL throughput optimization to
  #    avoid lock waits — it is NOT the correctness guard. The rowcount==1 status flip is.)
  for item in claim_eligible(queue, limit=max_tickets, run_id=rid):

    # (R2) If this item already has an OPEN directive (lease unsettled), RE-EMIT it
    # verbatim (same directive_id) — do not mint a new one, do not advance cursors.
    if item.open_directive_id is not None:
        re-emit recorded directive(item.open_directive_id); continue

    trusted = trust_filter(comments_of(item.ticket_id))   # §5.0 — drop untrusted-provenance verdicts
    rs = compute_review_state(trusted)

    # 1. STOP CHECK — reads the ACKED state; strict VERIFIED-CLEAN only (§5.0)
    if rs and rs.last_verdict == "VERIFIED-CLEAN" and not rs.open_findings:
        mark item.item_status = "done"; append run(action="stopped"); continue

    # 2. WAIT CHECK — is it the reviewer's turn? Compare against the ACKED cursor
    #    (last_acked_comment_*), NOT the seen cursor. The reviewer acts only when a
    #    new trusted implementer comment exists past what was last durably reviewed.
    if not implementer_posted_since(item.last_acked_comment_id, rs):
        mark item.item_status = "waiting_implementation"
        set next_eligible_at = now + backoff   # passive wait — attempts NOT incremented (§9.1)
        append run(action="waited"); continue

    # 3. THE REVIEWER'S TURN — emit a directive (advances SEEN, never ACKED)
    delta = list_ticket_comments(item.ticket_id, since=item.last_acked_comment_at,
                                 since_id=item.last_acked_comment_id)
    advance last_seen_comment_* to newest comment in delta   # SEEN only
    did = mint directive_id                                   # rid already minted for this wake
    persist work_queue_runs(id=rid, directive_id=did, item_id, action="picked")
    set item.open_directive_id = did   # open_directive_run_id was set by the claim
    item.attempts += 1                                       # EMITTED directive — counts (§9.1)
    emit directive(directive_id=did, intent="post_review",
                   context={ticket, review_state: rs, new_comments: delta, ticket_lease_epoch},
                   writeback_contract={directive_id:did, post_via:add_ticket_comment,
                                       author_persona_is_server_derived:true})
    mark item.item_status = "waiting_review"   # we have asked the agent to review
```

The agent then: inspects the fix (expands via `get_ticket`/`get_context_section`/diffs as needed), posts `Codex R{N+1} review on tk_X: <verdict>` via `add_ticket_comment` (fenced with `ticket_lease_epoch`; **the server stamps the author identity from queue config — the agent does NOT send `author_persona`**, §5.0), and calls `complete_work_queue_step(directive_id=..., verdict=clean|changes, comment_id=...)`.

- **(R2)** `complete_work_queue_step` settles the lease, validates the comment landed with trusted provenance, advances the **ACKED** cursor, and **re-derives `review_state` server-side over trusted comments** — the server, not the agent, is the source of truth for "clean."
- `verdict=clean` → item becomes `done` **only if** the server's re-derive confirms strict `VERIFIED-CLEAN` + no open findings + trusted provenance (§5.0). The agent cannot declare the item done by asserting a verdict.
- `verdict=changes` → item becomes `waiting_implementation`; the loop will not re-pick it for review until a new **trusted** implementer comment appears past the ACKED cursor.

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
| `last_comment_cursor` | **(R2)** split into `last_seen_comment_*` (server-shown floor) and `last_acked_comment_*` (durably-reviewed). The resumable, authoritative cursor is the **ACKED** pair; SEEN exists only so a replay re-shows the same delta. The directive lease (`open_directive_id`/`open_directive_run_id`) makes resume crash-safe. |
| `last_agent_run_id` | `work_queue_items.last_agent_run_id` |
| per-ticket status | `work_queue_items.item_status` (queue-loop view, distinct from `Ticket.status`) |
| `stop_condition` | `work_queues.stop_condition` |
| `cadence` | `work_queues.cadence_seconds` |
| `max_tickets_per_run` | `work_queues.max_tickets_per_run` |

**Hydration (R2 — explicit `auto_adopt`, Atlas answer #3):** auto-adoption is **opt-in queue config, never silent**. Only when `work_queues.auto_adopt = true` does `run_work_queue_step`, **before any action**, re-run the `selector` filter against `tickets` and **materialize** newly-matching tickets as `work_queue_items` rows (idempotent upsert on `uq_work_queue_item`), capped at `max_adopt_per_wake` (default 5) per wake. **Every adopted ticket exists as a materialized `work_queue_items` row before the loop acts on it** — there is no "act on a ticket that isn't yet a row." When `auto_adopt = false` (the default), only `selector.ticket_ids` seed items and the queue never silently grows. Hydration runs inside the step's transaction and is subject to the same `queue.project_id == ticket.project_id` scope as every other query (§11 R10). This replaces former open question §16.8 (silent auto-adopt → now an explicit, capped, materialize-first config).

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

### 9.1 Concrete v1 runaway / budget contract (R2 — Sentinel #6, replaces the hand-wavy "e.g. 12")

The v1 product contract (defaults + hard caps; operator-overridable within caps where noted):

| Knob | v1 default | Hard cap / floor | Notes |
|------|-----------|------------------|-------|
| `max_tickets_per_run` | **1** | **hard max 5** | tickets serviced per wake (§3.1) |
| `cadence_seconds` | **300** | **floor 120s** | advisory wake interval; a cadence below the floor is rejected at create/edit |
| `max_attempts_per_item` | **3** | — | counts **only EMITTED directives / action attempts**; **passive waits (`action="waited"`) do NOT increment** `attempts` (§3.2). |
| backoff on no-progress | **2m → 5m → 15m → 60m (cap)** | cap 60m | exponential `next_eligible_at` step on repeated no-progress wakes for the same item |
| item exhaustion | **after `max_attempts_per_item`**, item → `item_status='failed'` + `work_queue_runs(action="errored")` | — | a `failed` item is **not re-picked** and **requires explicit human reset** (re-queue / set_work_queue_status flow); no silent auto-retry |

The exhaustion rule is the hard floor against an infinite spend loop: a finding that never closes, or an agent that can't make progress, burns at most `max_attempts_per_item` emitted directives (not waits) before the item parks in `failed` for a human. Combined with `max_tickets_per_run` (per-wake bound), the backoff curve (frequency bound), and the dedicated rate-limit class (§11 R6), per-wake and per-item spend are both bounded.

---

## 10. Memory writeback rules (criterion 9)

The loop must leave a durable trail in SessionFS's own memory layer (this is dogfooding the product). Rules:

| Action | When | Fencing / provenance |
|--------|------|----------------------|
| `add_ticket_comment` | every review verdict + every implementer progress/closure note | MUST pass `ticket_lease_epoch` AND the `directive_id` from the directive. **(R2)** `author_persona` is **server-derived from queue config — the agent NEVER supplies it** (§5.0 trusted provenance); the server stamps the queue's reviewer identity + `actor_type`/`service_key_*` so the audit shows a queue-driven agent, not a human, and so the verdict is trust-markable by the stop oracle. **Idempotency:** the write is keyed by `directive_id`; a duplicate write under the same `directive_id` is rejected/deduped (§11 R3), and `complete_work_queue_step` records `comment_id`. A wake that already posted for the current `(item, directive_id)` must NOT post again. |
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
| R1 | **Double workers** (two cron jobs / a cron + a `/loop` on the same queue) double-process an item. | **(R2 — Atlas MED)** The correctness primitive is the **atomic claim**: `UPDATE work_queue_items SET item_status='active', open_directive_run_id=:rid WHERE id=:id AND item_status IN ('pending','waiting_review','waiting_implementation') AND (next_eligible_at IS NULL OR next_eligible_at<=now)` with **`rowcount == 1`** as the guard — this works on **both PG and SQLite**. An item claimed by one wake fails the predicate for a concurrent wake (rowcount 0). `SELECT ... FOR UPDATE SKIP LOCKED` is a **PG-only throughput optimization** to avoid lock waits — explicitly NOT the correctness mechanism. SQLite single-writer is fully correct here because the atomic UPDATE, not row locking, is the guard. |
| R2 | **Stale cursor / lost-or-replayed review** — agent acts on a directive then crashes before writeback; or acts on a directive built from an old `lease_epoch`. | **(R2 — Atlas HIGH #1, the headline fix)** The comment cursor is **split**: `last_seen_comment_*` (advanced when a directive is emitted) vs `last_acked_comment_*` (advanced ONLY by `complete_work_queue_step` after the writeback is validated/committed). The stop oracle and the reviewer-turn check read the **ACKED** cursor. A crash between directive and writeback leaves SEEN > ACKED with an **open directive lease** (`open_directive_id`) → the next wake **re-emits the same `directive_id`** (no review lost, none double-counted). Independently, every directive carries `ticket_lease_epoch`; every writeback MUST pass it and is rejected 409 on mismatch (`routes/tickets.py:1252-1262, 421-433`). A 409 → the agent re-runs `run_work_queue_step`. |
| R3 | **Duplicate comments / replay** — a wake retries (network blip) and posts the same review twice. | **(R2 — Sentinel #8, server-side directive token)** Every directive carries a `directive_id`; `add_ticket_comment` (queue path) and `complete_work_queue_step` are **keyed by `directive_id`** and **reject duplicate writes for the same `directive_id`** (idempotent settle). The directive lease (`open_directive_id`) means a retried `run_work_queue_step` re-emits the SAME `directive_id` rather than minting a new directive, so a retry can never produce a second distinct review. Plus: `complete_work_queue_step` is the single commit point recording `comment_id`; and the ticket-write lease bump means a stale retry 409s. This closes both the double-post and the stale-cursor replay. |
| R4 | **lease_epoch / fencing bypass** — agent omits the epoch. | Org setting `require_lease_epoch_on_ticket_writes` (existing, `routes/tickets.py:472`) forces 422 on omitted lease. Queues SHOULD set this on their org. `complete_work_queue_step` requires `ticket_lease_epoch` unconditionally (stricter than the ticket routes). |
| R5 | **Permission boundaries** — a service-key-driven CI loop touches tickets outside its org/project. | **(R2 — Sentinel #9, explicit)** Reuse the v0.10.10 service-key model: **inspect/step-read require `work_queues:read`; act requires `work_queues:write` AND `tickets:write` (and `agent_runs:write` where a wake opens an execution audit)** — 2 new scopes added to the 14-scope catalog. Every item/hydration/directive query enforces **BOTH** `assert_service_key_can_access_project(ctx, queue.project_id)` **AND** `queue.project_id == ticket.project_id`. Deny-by-default `get_current_user` rejects service keys; project-allowlist on the key. A queue is project-scoped (FK CASCADE); cross-project items are impossible by construction AND re-checked per query. |
| R6 | **Runaway loop** — a queue burns tokens/$$ forever (a finding that never closes; an agent stuck re-reviewing). | **(R2 — concrete contract §9.1 + dedicated rate-limit class, Sentinel #6/#7)** (a) `max_tickets_per_run` default 1 / hard max 5 bounds per-wake. (b) `max_attempts_per_item` **default 3**, counting **only EMITTED directives/action attempts** (passive `waited` wakes do NOT increment) → item auto-flips to `failed` + `work_queue_runs(action="errored")`, is not re-picked, and **requires explicit human reset**. (c) backoff **2m→5m→15m→60m cap** on no-progress wakes. (d) cadence **floor 120s / default 300s**. (e) **REQUIRED dedicated rate-limit class for `run_work_queue_step`** — it is NOT a passive read (claims work, emits directives, drives ticket writes + token spend): an app-level quota **AND** a Cloud Armor deny-429 path, keyed by `org_id`/`project_id`/`service_key_id`/`work_queue_id`/IP (same edge pattern as `activate`/`helm-validate`). This is a v1 REQUIREMENT, not a follow-up. |
| R7 | **False watcher claims / forged verdict** — the loop marks a ticket `done`/clean when it isn't, OR a caller forges a `VERIFIED-CLEAN`. | **(R2 — Sentinel R1 BLOCKER, `tk_4849c5db3dab47c9`)** TWO controls, both required. (i) **Trusted-verdict-provenance boundary (§5.0):** the stop oracle counts a comment as a verdict ONLY when its provenance is server-trusted (service-key binding / allowlisted reviewer identity / queue-stamped provenance) — **never** the caller-supplied `author_persona` string (forgeable today via `routes/tickets.py:336/2077`). Queue-posted comments derive `author_persona` server-side. (ii) **Server-side re-derive:** the stop oracle is `services/review_state.py` (the same LLM-free parser the team trusts) run **server-side over trusted comments only** at `complete_work_queue_step`; the agent's `verdict` is a hint, not authority. The server marks `done` only on **strict** `VERIFIED-CLEAN` + `open_findings==[]` + trusted provenance. `review_state.py` is a deterministic REDUCER, **not** the trust boundary — the trust filter runs first. **This blocking dependency MUST land before `review_until_clean` ships.** |
| R8 | **Confused-deputy / impersonation** — queue comments look like a human's. | `actor_type`/`service_key_id`/`service_key_name` provenance triple on every `work_queue_runs` row and every `TicketComment` the loop posts (existing convention). `author_persona` always set. Audit trail distinguishes queue-driven from human action. |
| R9 | **Queue-config race** — two admins edit a queue's selector/mode concurrently. | `work_queues.lease_epoch` + atomic `UPDATE WHERE lease_epoch=N` on every queue mutation (pause/resume/complete/edit), 409 on mismatch. |
| R10 | **Tenant data leak via selector / hydration** | **(R2)** Selector filters are applied within `project_id` scope only; the hydration query is `WHERE tickets.project_id = queue.project_id`, AND every item/directive query independently re-asserts `queue.project_id == ticket.project_id` plus `assert_service_key_can_access_project` (R5). No cross-project ticket can enter a queue by construction or by query. |
| R11 | **Verdict-provenance forgery (the core autonomy risk)** — `author_persona` is request-body data; `review_state.py` treats `author_persona=='codex-reviewer'` as an authoritative verdict, so any `tickets:write` caller can forge a `VERIFIED-CLEAN` and auto-close a ticket / stop a loop. | **(R2 — Sentinel R1 BLOCKER)** See §5.0 and R7. The stop oracle trusts a verdict ONLY via server-set provenance (service-key binding / allowlisted reviewer / queue-stamped); queue comments derive `author_persona` server-side; strict `VERIFIED-CLEAN` only. Tracked as BLOCKING dependency **`tk_4849c5db3dab47c9`** — **MUST land before `review_until_clean` ships**. |

**Sentinel R1 findings — resolution status:**
- **[HIGH/BLOCKER] Trusted-verdict provenance** → §5.0 + R7/R11; blocking dep `tk_4849c5db3dab47c9` declared in the header and §14 sequencing. **RESOLVED in design.**
- **[MED] `review_state.py` is a reducer, not a boundary; strict-vs-alias** → §5.0 #3 (reducer-not-gate stated) + strict `VERIFIED-CLEAN`-only auto-stop (aliases documented as NOT sufficient + required test §14). **RESOLVED.**
- **[MED] Concrete runaway defaults** → §9.1 product contract (1/max5, 120s floor/300s default, max_attempts 3 counting emitted only, 2m→60m backoff, `failed`+human-reset). **RESOLVED.**
- **[MED] Dedicated rate-limit class for `run_work_queue_step`** → R6 (app quota + Cloud Armor deny-429, keyed by org/project/service_key/queue/IP) as a v1 REQUIREMENT. **RESOLVED.**
- **[MED] Directive-token idempotency** → R3 + §4.4/§4.5 directive lease (`work_queue_run_id`/`directive_id`). **RESOLVED.**
- **[#9] Permission boundary** → R5 + §4 intro (read to inspect / write+tickets:write+agent_runs:write to act; `assert_service_key_can_access_project` AND `queue.project_id==ticket.project_id` per query; actor_type/service_key stamping). **RESOLVED.**

**Remaining Sentinel sign-off asks:** whether `work_queues:*` scopes should be Team+ tier-gated (Compass recommends yes — §13); final blessing of the §5.0 trust-marker mechanism choice (which of service-key-binding vs allowlisted-identity vs queue-stamp is canonical for v1) as part of `tk_4849c5db3dab47c9`.

---

## 12. CLI as secondary surface (criterion 10)

CLI is **admin/observability + parity**, never the primary agent path. Proposed (mirrors `sfs ticket` / `sfs agent`):

| Command | Purpose |
|---------|---------|
| `sfs queue create --name --mode --persona --selector-file --stop-condition --cadence --max-per-run` | define a queue (admin) |
| `sfs queue list [--status]` | list queues + progress |
| `sfs queue show <id> [--items]` | inspect cursor state |
| `sfs queue step <id> [--wake-source manual]` | manually drive one wake (debugging) — prints the directive; does NOT auto-act (an agent acts; the CLI is for humans inspecting) |
| `sfs queue set-status <id> --status active\|paused\|completed\|archived` | **(R2)** lifecycle (wraps the single `set_work_queue_status` verb; server enforces allowed transitions) |

**Parity expectation:** CLI covers create/inspect/lifecycle (the human-admin surface). The *agent loop* (`run_work_queue_step` → act → `complete_work_queue_step`) is **MCP-only in v1** — the CLI `step` command is a read-only directive printer for humans, not an actor, because acting requires an LLM runtime which the CLI doesn't have. This is deliberate and matches the existing split (e.g. lifecycle FSM transitions stayed "CLI/MCP-only because they need the local active-ticket bundle", v0.10.3).

---

## 13. Packaging / tier (Compass recommendation — Ledger/CEO call)

- **Individual/free:** may create **one** `manual`-cadence queue, `max_tickets_per_run=1`, no service-key/CI wake (local `/loop` only). Lets a solo dev automate their own review loop — a strong free-tier hook that showcases the product.
- **Team+:** multiple queues, cron/CI wake via service keys, `work_queues:*` scopes, higher `max_tickets_per_run`. The autonomous multi-agent loop is a team-coordination capability → fits the "team handoff is the monetization wedge" thesis (CLAUDE.md Key Decisions).
- This is a **recommendation**, not a commitment. Ledger + CEO own the final gate.

---

## 14. Staged implementation plan (criterion 12)

**(R2) Sequencing gate:** the **trusted-verdict-provenance boundary `tk_4849c5db3dab47c9` MUST land before the `review_until_clean` auto-stop ships** (§5.0, §11 R7/R11). v1 may build the queue scaffolding + `implement_until_done` first; `review_until_clean` auto-closing is gated on that dependency.

### v1 — MCP-only minimum viable queue (the wedge)
- **Blocking dependency first:** `tk_4849c5db3dab47c9` — trusted-verdict-provenance boundary (server-derived `author_persona` on queue posts + trust-marked verdicts in `review_state` consumption). Without it, `review_until_clean` auto-stop is not buildable.
- Migration 053: `work_queues`, `work_queue_items`, `work_queue_runs` — **(R2)** strictly additive, `down_revision='052'`, **inline `CheckConstraint` in `create_table`** (052 style), **no `lastrowid`** (server-generated `String(64)` PKs / `RETURNING`), and the **claim index `idx_wqi_claim (work_queue_id, item_status, next_eligible_at)`**.
- `services/work_queues.py`: explicit-`auto_adopt` hydration (materialize-first, `max_adopt_per_wake` cap), **`claim_eligible` via the atomic `UPDATE ... WHERE item_status IN (...) AND next_eligible_at<=now` rowcount==1 primitive** (PG `SKIP LOCKED` as an optional throughput add-on only), the directive-lease emit/re-emit logic, the two mode algorithms, the §5.0 **trust filter** + server-side stop-oracle via `compute_review_state` (reused) with **strict `VERIFIED-CLEAN` only**.
- Routes: `POST/GET/list /api/v1/projects/{pid}/work-queues`, `POST .../{id}/step`, `POST .../{id}/complete-step`, `POST .../{id}/status` (single `set_work_queue_status`, server-enforced transitions). Lease-fenced. Service-key scopes `work_queues:read/write` added to the catalog; act-paths also require `tickets:write`/`agent_runs:write`. **Dedicated rate-limit class** on `/step` (app quota + Cloud Armor deny-429).
- 6 new MCP tools (§4.1–4.6): `create_work_queue`, `get_work_queue`, `list_work_queues`, `run_work_queue_step`, `complete_work_queue_step`, **`set_work_queue_status`** (one verb, replacing pause/resume/complete) → 62 → **68**.
- **Mode `review_until_clean` first** (it's the proven manual loop, **after** the blocking dep), then `implement_until_done`. `triage` deferred.
- Wake via Claude `/loop` only (no service-key CI in v1 if we want to ship faster; or include CI if service-key scopes land cleanly).
- Tests (R2-expanded): **direct SQLite migration `upgrade()`/`downgrade()`** (+ CHECK enforcement); hydration idempotency + `auto_adopt`-off-means-no-adoption + `max_adopt_per_wake` cap; **atomic-claim rowcount==1 double-worker** (two concurrent claims, exactly one wins); lease-fence 409; **seen-vs-acked cursor crash-replay** (crash after directive, before ack → same `directive_id` re-emits, no lost/replayed review); **directive-token duplicate-write rejection**; **forged-`author_persona` is NOT trusted as a verdict** (the §5.0/R11 test); **alias (`APPROVED`/`NO CHANGES NEEDED`) does NOT auto-stop** (strict-only test); the stop-oracle terminate-on-strict-`VERIFIED-CLEAN` path (the test that proves the loop ends); runaway `max_attempts_per_item=3` → `failed` (counting emitted directives, not waits) + human-reset-required; cross-project `queue.project_id==ticket.project_id` denial.

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
| 5 | Durable cursor/state model (seen-vs-acked split + directive lease) | §3.2, §3.3, §7 |
| 6 | /loop, cron, CI, future wake call the step w/o chat memory | §8 |
| 7 | Context preservation (persona/ticket/KB/review/AgentRun/session) | §9 |
| 8 | Token/cost controls (small delta, expand on demand) | §9 |
| 9 | Memory writeback rules | §10 |
| 10 | CLI secondary surface only | §12 |
| 11 | Security/concurrency risks + mitigations | §11 |
| 12 | Staged plan v1/v1.1/v2 | §14 |
| + | Problem statement / why-now | §1 |
| + | Data-model sketch (migration 053 mechanics) | §3 |
| + | review-until-clean spec using STRICT VERIFIED-CLEAN stop | §5 |
| + | **(R2) Trusted-verdict-provenance boundary (Sentinel BLOCKER, `tk_4849c5db3dab47c9`)** | §5.0, §11 R7/R11 |
| + | **(R2) Concrete runaway/budget contract** | §9.1, §11 R6 |
| + | **(R2) Permission boundary (read/write scopes + per-query project scope)** | §4 intro, §11 R5/R10 |

---

## 16. Open questions for CEO / Atlas / Sentinel

**Still open (CEO / sign-off):**
1. **(CEO) Tier gate** — is the §13 packaging (free=1 manual queue, Team+=CI/cron) the right monetization line? This determines whether `work_queues:*` scopes are Team-gated.
2. **(CEO) v2 no-realtime exception** — event-driven wake (§14) wants a webhook→trigger path. Acceptable within "HTTP + ETags only," or does it need an explicit Key-Decision amendment?
3. **(Sentinel) Trust-marker mechanism** — §5.0 names three acceptable trust markers (service-key binding / allowlisted reviewer identity / queue-stamped). Which is canonical for v1? To be settled as part of blocking dep `tk_4849c5db3dab47c9`.

**Resolved in R2 (were D1 open questions):**
- ~~`work_queue_runs` vs extend `AgentRun`~~ → **DECIDED separate** (Atlas answer #1, §3.3).
- ~~Stop authority re-derive vs trust agent~~ → **DECIDED server re-derive over trusted comments** (§4.5, §5, §11 R7).
- ~~Tool count: fold lifecycle~~ → **DECIDED one `set_work_queue_status` verb** with server-side allowed transitions (§4.6, Atlas answer #3).
- ~~Runaway defaults / dedicated rate-limit class~~ → **DECIDED concrete §9.1 contract + required rate-limit class** (§11 R6, Sentinel #6/#7).
- ~~`review_state.py` as security boundary~~ → **CLARIFIED: it is a deterministic REDUCER, not a boundary**; the §5.0 trust filter is the boundary; strict `VERIFIED-CLEAN` only (Sentinel #5).
- ~~Hydration silent auto-adopt~~ → **DECIDED explicit `auto_adopt` config, materialize-first, `max_adopt_per_wake` capped** (§7, Atlas answer #3).
- ~~SQLite `SKIP LOCKED` correctness~~ → **CLARIFIED: the atomic `UPDATE ... rowcount==1` is the cross-DB correctness primitive; `SKIP LOCKED` is a PG-only throughput optimization** (§5, §11 R1, Atlas MED).
```
