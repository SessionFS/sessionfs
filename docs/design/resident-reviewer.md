# Design: The Resident Reviewer Runner — a persistent, SessionFS-resident review mind

- **Ticket:** (to be cut on approval) — "Design the resident reviewer runner (the work-queue review loop's client-side runtime)"
- **Author:** Compass (product intent + acceptance boundaries)
- **Status:** 🟢 DRAFT (R3, Sentinel APPROVED-WITH-CONDITIONS folded) — Compass product+design draft. **Sentinel review = APPROVED-WITH-CONDITIONS:** reviewer-only phases (**R0/R1/R2 clear to build**); the **implementer phases (R3+) are conditionally gated** on the R0 server work landing Sentinel's blockers (F1/F4/F6/F7/F9) and passing an R0 re-review. CEO ruled the high-risk gate stays fully uniform + two binding conditions (§3.7.2). This revision folds all conditions into binding design + the phase plan. Routes back to **Sentinel** (R0 re-review of F1/F4/F6/F7/F9), and to **Atlas** (trust forging, cross-org/cross-resident memory leak, client-side credential boundary, runaway cost, **the NEW implementer repo-mutation + self-review surface** — §9), **Atlas** (the NEW server component: migration 057 + `routes/residents.py` + the self-review-prohibition close rule — §3.6, §3.7, §11), **Forge** (the sandbox/worktree runner — §3.7.1), and **Ledger** (the free/paid line — §7.1) before any implementation ticket is cut. Same gate discipline as `docs/design/sso-oidc.md`.
- **Audience:** This doc defines the *what* and the *acceptance boundaries* of the runner. Atlas owns the server *how*; Sentinel owns the threat-model sign-off; the runtime is **client-side** (§2, §5) but v1 now has a **server component** — the resident-memory primitive (§3.6) — so R1's "pure client build / no new migration" assumption is **superseded** (§0 R2, §3.6, §10).
- **Visibility:** INTERNAL. `docs/design/` is tracked on `develop` and stripped from public `main` by the release sanitizer. References internal architecture freely.
- **Grounded against:** codebase at `develop` as of 2026-07-04 (migrations 001–**056** — SSO shipped 055 [OIDC foundation] + 056 [P1-fix] this cycle, so the resident-memory primitive is the next migration, **057**; 68 MCP tools). Shipped components the runner builds **on** (do NOT redesign): `services/work_queues.py` + `routes/work_queues.py` (work queues v1, design `docs/design/agent-work-queues.md`), the `review_until_clean` server-side stop oracle (`services/review_state.py`), trusted-verdict provenance (`db/models.py:TrustedReviewer` + `routes/tickets.py:is_registered_trusted_reviewer` @ 2059, `ticket_comments.verdict_trusted`), the settle-path verdict (`complete_work_queue_step(verdict_content=…)` in `services/work_queues.py`), the admin trusted-reviewers registry (`routes/trusted_reviewers.py`), and scoped service API keys (`key_kind='service'`, v0.10.10).

---

## §0 — Revision History

- **R3 — 2026-07-04 (Compass — Sentinel APPROVED-WITH-CONDITIONS folded; reviewer phases clear, implementer phases conditionally gated):** Sentinel reviewed R2.1 and approved with conditions. **CEO ruling on the high-risk gate:** HOLD **fully uniform** (no per-class human-required auto-close) **+ two binding conditions** — (a) every resident-auto-closed implement item carries a server-set, queryable, human-visible **`resident-reviewed, NOT human-reviewed` marker** so the merger is never misled by a green item; (b) high-risk-class merges require a **mandatory documented non-rubber-stamp merge checklist** (Scribe doc + Shield governance gate). The human merge is load-bearing for the weakest class (DB migrations — two prod incidents this cycle). Folded into §3.7.2. **Sentinel blockers folded as R0/pre-R3 requirements (they gate the implementer build; Sentinel re-reviews the R0 impl):** **F1 (HIGH)** the self-review prohibition was unenforceable — `work_queue_items` has no implementer-identity column; R0 now (a) records `implementer_service_key_id`/`implementer_user_id` on the item at claim/settle, (b) makes implement-side close **re-derive `review_state` over `verdict_trusted=true` comments** (not the agent's claimed outcome), (c) rejects auto-close where the closing trusted identity == the recorded implementer identity, (d) **FAIL-CLOSED — no auto-close when the implementer identity is unknown/unregistered**; headline R0 deliverable + full negative-test matrix (§3.7.2, §3.6.1, §9 hook 7, §10 R0). **F9 (HIGH)** `implement_until_done` currently self-closes on the agent's claimed outcome (pre-existing hole, `work_queues.py` ~1274) — HARD sequencing gate: no implementer resident may point at an `implement_until_done` queue until R0's re-derive+self-review rule ships (§3.7.2, §10). **F2 (HIGH coherence)** the reviewer resident is **co-located with the implementer and reads the worktree/diff LOCALLY** — C7 holds, the server never gets repo contents; "diff-ref" defined as pointer/metadata only; co-location is now a binding constraint **C8** (§2, §3.7.1, §3.7.4). **F4 (MED)** register-trusted-reviewer AND resident-register now server-**reject** binding a `service_key_id` already bound to the opposite role on the same project/org (real SoD, not just prose) (§3.6.2, §4.1). **F5 (MED)** corrected: `--cold` clears only the WARM cache — **durable-memory poisoning survives re-hydration**; added implementer-mind provenance + a **quarantine signal**, and un-deferred owner-visibility for implementer minds specifically (§3.4, §6.2, §6.3, §3.6.2). **F6 (LOW→MED)** server hard cap on un-compacted `resident_memory` entries (reject / force server-side supersession of oldest) + a per-wake write cap — no reliance on client compaction discipline (§3.6.1, §3.6.2). **F7 (MED)** defined resident-memory **rebinding on service-key rotation** + rotated-out key denied at `require_scope` immediately on revoke, no window (§3.6.2). **F8 (LOW)** stated honestly: "cannot push/merge" is an **operator responsibility** (Forge hardened profile + Scribe docs), not a server-enforced product guarantee (§3.7.1, §9 hook 8). Build-time notes carried: reviewed content is framed **data-not-instructions** in the prompt scaffold; the forged-`resident_id`/`org_id`-in-body case + retention-sweeper isolation are required tests (§9). Phase plan updated: **R0 owns F1/F4/F6/F7/F9 + the F1 negative-test matrix; R3+ gated on Sentinel's R0 re-review** (§10).
- **R2.1 — 2026-07-04 (Compass — finalization, the two R2 security forks SETTLED into binding design):** CEO ruled on the two open security forks; folded as binding so Sentinel reviews a settled doc. **(1) Code custody = git stays HOST-LOCAL** — new binding constraint **C7**: SessionFS the server NEVER holds or transports repo contents; it only ever sees tickets/comments/diff-refs; all resident git ops (worktree/branch/diff/build) live on the operator host; the server is categorically out of the code-custody blast radius (resolves old §8.3 Q4). **(2) High-risk change classes = UNIFORM gate** — NO per-class human-required carve-out; the same model (implementer "done" → `waiting_review` only → authoritative close via the server stop-oracle over an INDEPENDENT trusted reviewer → human merge) applies to ALL changes (removes old §8.3 Q6). Honest residual retained for Sentinel: under a uniform gate a high-risk change (e.g. a DB migration — the exact class behind two prod incidents this cycle where automated review passed but PG-only bugs slipped) can auto-close the queue ITEM on a resident-reviewer `VERIFIED-CLEAN`, leaving the **human MERGE as the sole remaining human touchpoint** — so the merge backstop is **load-bearing** and merge-time review must not be a rubber-stamp (accepted CEO tradeoff: uniform simplicity over per-class gating). **Also settled:** merge gate is **human-only for v1** (no automated merge); free-tier memory **retention floor = 30 days** for compacted entries (active digest always kept), tunable; **owner-only audited mind-export DEFERRED** to a paid/v2 decision (privacy-vs-compliance tension noted, not designed now). **Factual fix:** the memory-primitive migration is **057 (down_revision=056)**, not 055 — SSO shipped 055+056 this cycle. No re-litigation of R2 decisions.
- **R2 — 2026-07-04 (Compass — CEO rulings folded, all three forks taken ambitiously):** CEO ruled on all five R1 open questions. **Confirmed as recommended:** operator-hosted always-on runtime (§3.1); one BYO OpenAI-compatible/Codex reference adapter, no SessionFS-default model (§5). **DECISION 1 — first-class resident-memory PRIMITIVE** (NOT session-capture reuse): new `residents` + `resident_memory_entries` tables, migration 057, `routes/residents.py`, `resident_memory:read/write` scopes (catalog 16→18), server-enforced single-org **and resident-private** isolation (§3.6). This **supersedes R1's "no new migration / pure client build"** — v1 now has a server component (§10, §11). **DECISION 2 — the implementer resident is a first-class v1 citizen** (NOT deferred): a worktree-isolated, sandboxed code-writing resident that can only PROPOSE (never push/merge/self-close); the authoritative implement-side gate is an **independent** trusted-reviewer verdict + a **human merge gate**, enforced by a NEW **server-side self-review prohibition** (§3.7, §4.4). Materially larger attack surface — new §9 hooks 7–12. **DECISION 3 — free local runner (review AND implement), paid managed features** (fleet mgmt, dashboards/observability, multi-queue-per-resident, hosted memory beyond a retention floor, governance/SSO ties): new §7.1 packaging + a work-queue §13 tier-line correction. **Carried forward:** the §9-hook-6 reviewer prompt-injection→genuine-but-wrong-`VERIFIED-CLEAN` risk, PLUS its worse implementer analog (a poisoned mind / injection → genuine-but-HARMFUL code reaching a merge/close state), whose containment is the independent gate + human merge + worktree isolation (§6.3, §9 hook 10). **What still holds from R1:** binding constraints C1–C4, the ledger/mind split, the settle-path verdict-trust model, crash-recovery via heartbeat re-emit, cost bounding. **What changed:** v1 is no longer client-only; v1 scope now includes the implementer resident + the memory primitive; two new binding constraints C5 (memory isolation) + C6 (implementer containment & authoritative gate).
- **R1 — 2026-06-29 (Compass):** Initial binding-intent draft. Frames the runner as a **persistent resident with living context in SessionFS** (the CEO vision: *the queue is the durable task LEDGER; the resident is the durable MIND*). Architecture (§3): client-side long-running process; hydrate → wake on the work-queue heartbeat → call the operator's own LLM → post verdicts via the settle-path → write durable memory back → checkpoint + compact. Trust/auth model (§4): scoped service key bound to a `trusted_reviewers` identity; single-org isolation as a hard boundary. Client-side LLM credential boundary (§5). Failure modes + safety envelope inherited from work queues + resident-specific additions (§6). v1/deferred (§7), CEO open questions (§8), Sentinel security-review hooks (§9), phased build plan (§10), handoff tickets (§11). No server schema change is *required* by v1 (the settle-path, trust registry, and service-key model already exist); any new server seam is flagged for Atlas.

---

## §1 — Problem Statement / Why a Resident (vs the existing stateless loop)

### 1.1 What already exists, and what it deliberately is NOT

Work queues v1 (v0.12.0) gave us a **server-authoritative, crash-safe task ledger** for autonomous review loops. The heartbeat `run_work_queue_step` is **deliberately stateless from the caller's perspective**: the server holds all loop state (`work_queue_items` cursor, seen-vs-acked split, directive lease, backoff, attempt cap), re-derives `review_state` server-side over `verdict_trusted=true` comments only, and returns **one bounded directive at a time**. A caller with **zero chat memory** — a Claude `/loop` prompt that evaporates between runs, a cron job, a CI step — can drive the loop correctly. That statelessness is a *correctness* feature: it is why a crashed wake never loses or double-posts a review (the directive re-emits), and why a forged `author_persona` can never auto-stop a loop (`agent-work-queues.md` §5.0).

But statelessness has a standing cost: **every wake, the reviewer LLM is cold.** Each wake it receives only the bounded directive context (`review_state` + the comment delta) and must reason from a blank slate. It has no memory of:

- the project's architecture, conventions, and the recurring anti-patterns it flagged three tickets ago;
- its own prior review *reasoning* on the current ticket across earlier rounds (it sees the comment delta, not the private chain of thought that produced its last verdict);
- the session lineage / *why* a design decision was made that a diff now touches.

The loop therefore pays a re-grounding tax on every wake. There are only two ways to pay it inside a stateless loop, and both are bad at scale: either the LLM stays **under-grounded** (cheap directives, weaker project-specific reviews), or you **stuff a large context blob into every directive** (better reviews, but it violates the work-queue's "small delta, expand on demand" budget and grows unbounded with thread length and uptime).

### 1.2 The resident insight (the CEO vision — binding framing)

> **The queue is the durable task LEDGER. The resident is the durable MIND.**

SessionFS *is* the memory layer for AI agents. A reviewer that **dogfoods the product** does not pay the re-grounding tax every wake. Instead it:

1. **Hydrates** a warm, durable mind from SessionFS on start — compiled project context, the project KB (including its *own* prior findings), the review playbook wiki, and the lineage of its own prior reasoning (its last checkpoint).
2. **Stays warm** across wakes — the work-queue heartbeat still feeds it the bounded task delta, but the mind it reasons *with* persists.
3. **Writes durable learnings back** to SessionFS — recurring findings, project conventions, fix patterns become KB claims and wiki pages that survive the process and are shared with the rest of the team's agents.
4. **Checkpoints and compacts** so it survives crashes and stays **cost-bounded** over long uptime — the durable mind lives in SessionFS, not in an ever-growing RAM context window.

The resident is the **persistent runtime** the work-queue design listed as its single open follow-up (`agent-work-queues.md` §16 / project memory). It is **not** a portable, cold-each-wake process. It **adds** a persistent mind *on top of* the durable, server-authoritative ledger.

### 1.3 The non-negotiable: the mind ADVISES, the ledger + oracle DECIDE

The resident's living context exists to make reviews **better and cheaper** — it is an **optimization, never a replacement** for the server-authoritative stop oracle. A resident **MUST NOT** be able to self-certify `VERIFIED-CLEAN` outside the trusted settle-path (§4). The server, over `verdict_trusted=true` comments only, remains the sole authority for closing a queue item (`agent-work-queues.md` §5.0; `services/review_state.py`). Crash-safety, double-post protection, and stop-authority all stay where work queues v1 put them: in the server. The resident's mind is a correctness-irrelevant accelerator — if it is lost, stale, or wrong, the *worst* outcome is a lower-quality (or absent) review, never a falsely-closed ticket. This is the load-bearing safety property of the whole design.

### 1.4 Job-to-be-done

> *As an operator running an autonomous review loop, I want a persistent reviewer that remembers my project — its architecture, its conventions, the issues it has already flagged — so its reviews are as well-grounded as a teammate who has been on the project for months, while it still runs unattended, costs a bounded amount over long uptime, survives crashes, and can never rubber-stamp a ticket clean unless it genuinely is.*

### 1.5 Non-goals (v1)

- **Not a new stop oracle.** The resident does not invent an "is it done?" heuristic. Closure stays server-side and trusted-only, for BOTH modes (§1.3, §3.7.2).
- **(R2 — was a non-goal in R1, now IN v1) The implementer resident IS in scope.** CEO DECISION 2 makes the code-writing `implement_until_done` resident a first-class v1 citizen. It writes code but can only PROPOSE — worktree-isolated, sandboxed, never self-closing, never merging (§3.7). This is the largest new safety surface (§9).
- **Not a SessionFS-hosted LLM runtime.** The runner never runs on SessionFS infra with SessionFS-held LLM keys — a standing Key Decision (§2 C1). It is operator-hosted and single-org (§3.1, §4.2). (Note: R2 DOES add server-side *storage* for the resident-memory primitive — that is data, not an LLM key, and does not violate C1.)
- **Not real-time.** Wake stays poll-driven via the heartbeat (honoring "NO WebSockets / NO Redis"). Event-driven wake is the work-queue v2 sketch, deferred (§7).
- **Not a model we ship.** SessionFS provides the memory + task ledger + trust seam; the **operator brings the brain** (their own external LLM, §5).

---

## §2 — Binding Constraints (state these as the contract)

> **C1 — Client-side LLM, operator's own model, operator's own credentials (Key Decision, non-negotiable).** The reviewer LLM is the **operator's own external model** (the CEO uses Codex / GPT-5.5). It is **NOT** a SessionFS product LLM and **NOT** Anthropic/Claude. There are **NO server-side LLM API keys** anywhere in SessionFS (a standing Key Decision). The runner is therefore **client-side** and calls the operator's LLM with the operator's own credentials. The SessionFS server never sees the LLM, the prompt, or the LLM key. See §5.

> **C2 — Verdicts only count via the trusted settle-path.** A verdict influences the stop oracle only when `ticket_comments.verdict_trusted == true`. That flag is **server-stamped from the authenticated `AuthContext`**, never from a request body (`tk_d42170b4670f4448`). The resident obtains it **only** by (a) authenticating as a **registered** `trusted_reviewers` identity and (b) posting through `complete_work_queue_step(verdict_content=…)`, which creates the verdict comment **server-side** with `author_persona` from queue config and `verdict_trusted` from `is_registered_trusted_reviewer(authenticated actor)`. `assume_persona` is **NOT** a trust source. A request-body `author_persona` or `verdict_trusted` is **never** sufficient. See §4.

> **C3 — The heartbeat is the task source of truth; the mind is an addition, never a replacement.** The resident reuses `run_work_queue_step` / `complete_work_queue_step` for *all* task state and the server-side stop oracle for *all* closure decisions. The living context is purely an accelerator (§1.3). A resident can **never** short-circuit the server-authoritative oracle.

> **C4 — Single-tenant / single-org. The living context is a hard isolation boundary.** One resident process serves **exactly one org**, via one org-bound service key, one `trusted_reviewers`/resident identity, and one **org-scoped local mind store**. A resident's living context (KB / wiki / sessions / memory-primitive entries / warm digest) must **never** mix orgs or tenants. Two orgs ⇒ two separate processes, keys, and stores. See §4.2.

> **C5 — (R2) The resident-memory primitive is SERVER-isolated: single-org AND resident-private.** The durable mind lives in the new `resident_memory_entries` table (§3.6), and its isolation is enforced by the **server**, not by client convention: every read/write requires `resident.org_id == ctx.org_id` **AND** `resident.service_key_id == ctx.service_key_id` — a resident may read/write **only its own mind**, never another resident's, even within the same org. This is strictly stronger than the work-queue project-scope check. Client-side org-scoped disk storage (C4) remains as defense-in-depth for the warm cache only. See §3.6.3.

> **C6 — (R2) The implementer resident can only PROPOSE, never DISPOSE — and cannot self-approve.** A code-writing resident (`implement_until_done`) is worktree-isolated + sandboxed (§3.7.1); it may write code and open it for review but **cannot** push to a protected branch, merge, release, or move its own queue item to a closed/`done` state. The authoritative implement-side gate is a **server-re-derived trusted review verdict from a DIFFERENT trusted identity** (server-enforced **self-review prohibition**) followed by a **human merge gate** — never the implementer's own claim. The gate is **UNIFORM across all change classes** (no per-class human-required carve-out; §3.7.2). Code has **no** trust flag; its trust is earned downstream, never asserted. See §3.7.2, §3.7.3, §4.4.

> **C7 — (R2.1, SETTLED) Code custody stays HOST-LOCAL. The SessionFS server is categorically out of the code-custody blast radius.** All of the resident's git operations — worktree checkout, branch commits, diffs, builds, test runs — live **entirely on the operator's host** (§3.7.1). SessionFS the server **NEVER** holds, stores, or transports repo contents; it only ever sees **tickets, comments, and diff-refs** (pointers/metadata, not the code itself). A compromise of the SessionFS server therefore cannot exfiltrate or tamper with customer source. This is a hard boundary, not a convention. See §3.7.1, §3.7.3.

> **C8 — (R3, F2) The reviewer that closes an implementer's work is CO-LOCATED and reads the diff LOCALLY. C7 is never softened to transport code through SessionFS.** Sentinel flagged an R2.1 contradiction: an *independent* reviewer must see the implementer's code to review it, but C7 forbids the server holding code. Resolution (CEO's chosen boundary): the reviewer resident is **co-located on the same operator host** (or a host with local read access to the same worktree/branch) and reads the **worktree/diff directly from the local filesystem/git**, not from SessionFS. A **`diff-ref`** is strictly a **pointer/metadata** handle (branch name, commit SHA, changed-path list, PR URL) — **never** the diff *contents*. SessionFS transports diff-refs; the host transports the diff. Independence is by **identity** (a different trusted `service_key_id`, §3.7.2) and, where the operator wants stronger separation, a different host with its own local checkout — but **in no case** does code flow through the SessionFS server. See §3.7.1, §3.7.4.

---

## §3 — Architecture

### 3.1 Where the runner sits

The resident is a **client-side, long-running process** — a "resident" in the daemon sense, not a one-shot CLI invocation. It is **operator-hosted**: an always-on process on a machine the operator controls (the CEO's workstation, a dedicated small VM, or a container/systemd unit the operator runs). It is **not** hosted by SessionFS, because hosting it would require SessionFS to hold the operator's LLM key (violates **C1**).

It talks to two systems, with two single-purpose credentials that **never cross** (§5):

```
   ┌─────────────────────────── operator-hosted, single-org ───────────────────────────┐
   │                                                                                    │
   │   ┌────────────────────────────────────────────────────────────┐                  │
   │   │  RESIDENT REVIEWER (long-running process)                    │                  │
   │   │                                                              │                  │
   │   │  ┌─────────────┐   ┌──────────────────┐   ┌──────────────┐   │                  │
   │   │  │ Heartbeat    │  │  Warm living      │  │ LLM adapter   │   │                  │
   │   │  │ driver       │  │  context (mind)   │  │ (pluggable)   │   │                  │
   │   │  │ (work-queue) │  │  bounded digest   │  │               │   │                  │
   │   │  └──────┬───────┘  └────────┬──────────┘  └──────┬───────┘   │                  │
   │   └─────────┼───────────────────┼────────────────────┼──────────┘                  │
   │             │ SessionFS API      │ SessionFS API      │ operator's LLM endpoint     │
   │             │ (service key,       │ (service key,      │ (operator's LLM key,        │
   │             │  org-bound)         │  org-bound)        │  LOCAL ONLY)                │
   └─────────────┼────────────────────┼────────────────────┼─────────────────────────────┘
                 ▼                    ▼                     ▼
        ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐
        │ work_queues +     │  │ KB / wiki /       │  │ Operator LLM      │
        │ stop oracle       │  │ sessions /        │  │ (Codex / GPT-5.5  │
        │ (the LEDGER)      │  │ checkpoints       │  │  / any OIDC-compat)│
        │  SERVER-AUTH      │  │ (the MIND store)  │  │  NEVER seen by srv │
        └──────────────────┘  └──────────────────┘  └──────────────────┘
```

A **degraded fallback** exists for solo/free use: a Claude `/loop` prompt driving the heartbeat with *no* persistent mind (exactly the work-queue v1 story today). The resident is the **warm, persistent upgrade** of that fallback — same task contract, added mind.

### 3.2 The control loop (how it talks to the heartbeat + posts settle-path verdicts)

The resident is a supervisor loop. Steady state, per wake, for a `review_until_clean` queue:

```
boot:                                    # §3.3 hydration — ONCE per process start / cold restart
  mind = hydrate_living_context(org, project, persona='codex-reviewer')

loop forever (cadence-gated by the server; the resident sleeps ≥ cadence_seconds):
  resp = run_work_queue_step(work_queue_id, wake_source='resident', wake_ref=<resident id>)
  if resp.stop:                          # stop_reason in {all_clean, paused, completed, ...}
      log + (optionally) idle until next poll; continue
  for directive in resp.directives:      # bounded — max_tickets_per_run (default 1, hard cap 5)
      # directive.intent == 'post_review'; context = {review_state delta, new_comments, lease_epoch}
      prompt = assemble(directive.context, mind.warm_digest)     # bounded; §3.5
      verdict_text, reasoning = operator_llm.review(prompt)      # CLIENT-SIDE, operator key (C1)
      # SETTLE PATH — server creates the verdict comment, stamps trust (C2):
      complete_work_queue_step(
          work_queue_id, item_id=directive.item_id,
          directive_id=directive.directive_id,                  # idempotency / lease settle
          ticket_lease_epoch=directive.ticket_lease_epoch,      # fenced; 409 → re-run
          outcome='posted_review',
          verdict_content=verdict_text,                          # "Codex R<N> review on tk_X: VERIFIED-CLEAN" | findings
      )
      # the resident NEVER sends author_persona; the server derives it + verdict_trusted
      writeback_durable_learnings(reasoning, mind)               # §3.3 — add_knowledge / update_wiki_page
  maybe_checkpoint_and_compact(mind)                             # §3.4/§3.5 — periodic
```

Key properties, all inherited from the shipped contract:

- **Task state is never in the mind.** The cursor, the "is it the reviewer's turn?" check, the attempt count, the backoff — all live in `work_queue_items` server-side. The resident reads them via the directive. This is the clean ledger/mind separation (**C3**): a wiped mind costs grounding, never task position.
- **The verdict goes through the settle-path, not `add_ticket_comment`.** This is **C2**. A reviewer posting via the generic comment path lands `author_persona=null → verdict_trusted=false` and can never stop the loop (this is documented in `services/work_queues.py`'s directive contract). The resident **must** use `complete_work_queue_step(verdict_content=…)`.
- **Lease-fenced.** Every settle passes `ticket_lease_epoch`; a 409 means the ticket moved under it → re-run the step (the directive re-emits). The resident never forces a write.
- **Crash between LLM call and settle re-emits, never double-posts** — the directive lease (`open_directive_id`) is the server's job (`agent-work-queues.md` §4.4/§4.5). The resident inherits it for free.

### 3.3 Living context in SessionFS — hydration + writeback + compaction

This is the dogfooding crux. The resident's mind is split into **shared durable memory** (lives in normal SessionFS surfaces, readable by the whole team's agents) and **private rolling memory** (the resident's own reasoning continuity).

**Hydration on boot** (all via existing read tools, all org-scoped by the service key):

| Mind layer | SessionFS source | Tool |
|---|---|---|
| Project architecture / conventions | compiled project context + sections | `get_project_context`, `get_context_section` |
| Its own prior findings + project KB | KB claims, **filtered to its own persona** | `search_project_knowledge`, `list_knowledge_entries` (the MCP `add_knowledge` note already supports `GET /entries?persona_name=<name>` precisely "so autonomous agents can retrieve their own prior findings") |
| Review playbook / standards | a dedicated wiki page (e.g. `review-playbook`) | `list_wiki_pages`, `get_wiki_page` |
| **Its own prior reasoning continuity (R2)** | the **resident-memory primitive** — latest `digest` + recent `reasoning` entries | `GET .../residents/{id}/memory/hydrate` (§3.6) |
| Ticket-specific lineage (on demand) | related sessions + summaries for the ticket under review | `find_related_sessions`, `get_session_summary`, `get_session_provenance` |

**Where the PRIVATE rolling mind lives (R2 — CEO DECISION 1, resolved):** R1 proposed running the resident *as its own captured `.sfs` session* + `checkpoint_session`. **CEO DECISION 1 rejects that overload** in favor of a **first-class resident-memory primitive** (§3.6). The split is now crisp: **shared durable memory → KB + wiki** (team-readable, cross-agent); **private rolling mind → the resident-memory primitive** (resident-private, server-isolated per C5). Neither overloads sessions, and the private-mind isolation is now a **server guarantee**, not a client convention.

**Writeback** (per the work-queue §10 writeback rules — unchanged, the resident just honors them):

- **`add_knowledge`** when the review surfaces something durable (a recurring anti-pattern, a project convention, a fix pattern). `persona_name='codex-reviewer'`, `author_class='agent'` (service keys are server-forced to `agent` — cannot spoof `human`). De-dup against active claims first.
- **`update_wiki_page`** for substantial findings (a review post-mortem, an update to the review playbook). Provenance-validated.
- The **verdict comment** itself is written by the server via the settle-path (not a resident writeback) — that is the point of **C2**.
- **Writeback discipline:** no-op writes are forbidden (matches the work-queue / `update_ticket` audit-pollution rule). A wake that learns nothing durable writes nothing to KB.

**Compaction (the cost-bounding mechanism — §3.5).** The resident maintains a **bounded warm digest**: a token-capped summary of its durable mind (compiled context highlights + its top KB claims + the review playbook + its latest `digest` entry), refreshed from the durable sources, **not** the full history. Periodically (every N wakes or M minutes) it calls `POST .../residents/{id}/memory/compact` — the resident client-side summarizes its recent `reasoning` entries into a new `digest`, the server stores it and marks the summarized entries superseded (§3.6.4). The durable sources (KB / wiki / memory-primitive digests / compiled context) remain the source of truth; the warm digest is a re-expandable cache, never the system of record.

### 3.4 Crash recovery

Two independent state domains, recovered independently:

- **Task state — recovered by the server, for free.** On restart the resident just calls `run_work_queue_step`. The seen-vs-acked cursor + directive lease mean any directive that was emitted but not settled **re-emits** (`agent-work-queues.md` R2). No review is lost or double-posted across a crash. The resident contributes nothing to task-state recovery beyond "call the heartbeat again."
- **Mind state — recovered by re-hydration.** On restart the resident re-runs `hydrate_living_context` (§3.3): durable KB/wiki/compiled-context survive in SessionFS; the **memory-primitive `hydrate`** (latest `digest` + recent `reasoning`) restores rolling reasoning continuity. The only thing lost is in-RAM reasoning since the last durable `POST .../memory` — which is **correctness-irrelevant** (§1.3): the worst consequence is the next review is slightly less warm.
- **`--cold` clears only the WARM (in-RAM/on-disk) cache — it does NOT cure durable-memory poisoning (R3, F5, honest correction).** R2.1 overstated `--cold` as a poisoning remedy. A `--cold` restart discards the local warm cache and re-reads from SessionFS, **but if the *durable* `resident_memory_entries` were poisoned, re-hydration faithfully reloads the poison.** The remedy for a suspected-poisoned mind is the **quarantine signal** (§3.6.2 `POST .../memory/quarantine`): it pauses the resident and forces the next start to treat prior `digest`/`reasoning` as untrusted and re-derive from the durable *shared* sources (compiled context + KB + wiki), not the resident's private entries. For **implementer** minds, owner visibility into the durable entries is available in v1 (§3.6.2, F5) so a poisoning is inspectable. The durable store is authoritative over the in-RAM mind — which is exactly why a poisoned durable store needs an explicit quarantine, not a cache flush.

**Invariant:** a crashed/restarted resident can **never** (a) leave a ticket falsely closed (server stop oracle), nor (b) lose a pending review (directive lease re-emit). Verdict integrity is independent of the resident's liveness.

### 3.5 Cost bounding

Living context must not mean unbounded LLM spend over long uptime. Bounds, layered:

- **Per-wake bound (server):** `max_tickets_per_run` (default 1, hard cap 5) + the bounded directive (`review_state` + comment delta, never the whole thread).
- **Wake-frequency bound (server):** `cadence_seconds` floor 120s / default 300s + the backoff curve (2m→5m→15m→60m) + the dedicated `/step` rate-limit class (app quota + Cloud Armor deny-429).
- **Mind bound (resident):** the **warm digest is token-capped** at `residents.mind_token_budget` (§3.6.1); full mind is re-expanded on demand only (same "small delta, expand on demand" philosophy as the directive). Periodic `compact` keeps the memory-primitive footprint bounded regardless of uptime; the retention sweeper (§3.6.2) hard-deletes superseded entries after a floor window.
- **LLM-spend bound (resident, new):** a local **per-wake token budget** and a **daily/total spend ceiling** in resident config. On budget exhaustion the resident parks (no-op wakes) rather than spending unbounded — and because no verdict is posted, the queue simply backs off. Fail-closed, never fail-clean.

### 3.6 The resident-memory primitive (R2 — DECISION 1; a first-class primitive, NOT session-capture reuse)

R1 persisted the resident's private mind by running it *as a captured `.sfs` session* and snapshotting via `checkpoint_session`. **CEO DECISION 1 rejects that overload.** The resident mind is a **first-class primitive**: sessions are transcripts of *tool work*, whereas a resident mind is *durable rolling reasoning + a compacted digest*. A dedicated primitive keeps the resident out of the operator's Sessions UI + storage quota and — critically — lets the **server enforce single-org AND resident-private isolation** on the mind (a client convention cannot). **This supersedes R1's "no new migration / pure client build" assumption:** v1 now has a server component (migration 057, two tables, `routes/residents.py`, two scopes). Dependencies enumerated in §11.

#### 3.6.1 Schema (migration 057 — strictly additive, `down_revision='056'`)

Two new tables **plus additive columns on the existing `work_queue_items`** (for F1 implementer-identity + the auto-close marker), following every existing convention (`String(64)` PKs `res_<hex>`/`rme_<hex>`, `ondelete=CASCADE`, JSON-as-Text, **inline `CheckConstraint` in `create_table`** for SQLite-applicability, no `lastrowid`, service-key provenance triple, direct SQLite `upgrade()`/`downgrade()` test).

**`residents` — the durable resident identity**

| Column | Type | Notes |
|---|---|---|
| `id` | String(64) PK | `res_<hex>` |
| `org_id` | FK→organizations CASCADE, NOT NULL, indexed | **the hard isolation anchor (C5)** |
| `project_id` | FK→projects CASCADE, NOT NULL, indexed | single-project scope in v1 |
| `kind` | String(20) NOT NULL | `reviewer` \| `implementer` (CHECK) |
| `persona_name` | String(50) NOT NULL | persona it acts as (validated against `agent_personas`) |
| `service_key_id` | String(36) NOT NULL | the bound credential the resident authenticates as; `uq_resident_service_key UNIQUE (service_key_id)` — one service key drives at most one resident |
| `work_queue_id` | String(64), nullable | the queue it drives |
| `status` | String(20) NOT NULL DEFAULT `active` | `active` \| `paused` \| `retired` (CHECK) — the lifecycle kill switch |
| `mind_token_budget` | Integer NOT NULL DEFAULT 8000 | cap on the hydrated warm-digest size (cost bound, §3.5) |
| `max_uncompacted_entries` | Integer NOT NULL DEFAULT 500 | **(R3, F6)** server hard cap on live (un-superseded) `reasoning`/`observation` entries for this resident; writes above it are rejected OR force server-side supersession of the oldest (§3.6.2) |
| `created_by_user_id` | String(64) NOT NULL | provenance |
| `actor_type` / `service_key_name` | provenance | who registered it |
| `created_at` / `updated_at` | DateTime(tz) | standard |

Index: `idx_resident_org_project (org_id, project_id)`.

**`resident_memory_entries` — the durable mind (append-only)**

| Column | Type | Notes |
|---|---|---|
| `id` | String(64) PK | `rme_<hex>` |
| `resident_id` | FK→residents CASCADE, NOT NULL, indexed | owner |
| `org_id` | FK→organizations CASCADE, NOT NULL, indexed | **DENORMALIZED isolation predicate** — every read filters `org_id == ctx.org_id`, belt-and-suspenders on top of the resident join (C5) |
| `kind` | String(20) NOT NULL | `reasoning` (rolling private thought) \| `digest` (compacted warm snapshot) \| `observation` (a durable note not yet promoted to KB) (CHECK) |
| `seq` | Integer NOT NULL | monotonic per resident; ordering + cursor; `uq_resident_memory_seq UNIQUE (resident_id, seq)` |
| `content` | Text NOT NULL | payload; hard size cap (e.g. 64 KB), rejected above |
| `token_estimate` | Integer NOT NULL DEFAULT 0 | for digest budgeting (§3.5) |
| `superseded_by` | String(64), nullable | when a `digest` compacts entries, they point to the digest that replaced them (compaction lineage) |
| `compacted_at` | DateTime(tz), nullable | set when an entry is folded into a digest |
| `created_at` | DateTime(tz) NOT NULL | |

Indexes: `idx_rme_resident_kind_seq (resident_id, kind, seq)`, `idx_rme_resident_created (resident_id, created_at)`.

**Additive columns on `work_queue_items` (R3, F1 — makes the self-review prohibition enforceable + carries the auto-close marker)**

Sentinel F1 (HIGH): the self-review rule was unenforceable because `work_queue_items` had **no implementer-identity column** to compare the closing verdict against. Migration 057 adds:

| Column | Type | Notes |
|---|---|---|
| `implementer_service_key_id` | String(36), nullable | **(F1)** the service-key identity that produced the code for this item; recorded at implement-side **claim** and re-affirmed at **settle**. Null until an implementer acts. |
| `implementer_user_id` | String(64), nullable | **(F1)** the user identity, if the implementer acted under a user key (belt-and-suspenders for identity comparison). |
| `auto_close_review_kind` | String(20), nullable | **(CEO condition a)** set when the item auto-closes: `resident_trusted` (a resident-reviewer verdict closed it — **"resident-reviewed, NOT human-reviewed"**) vs `human` (a human verdict closed it). Queryable + surfaced wherever a human sees the item. Null while open. |
| `closed_by_service_key_id` / `closed_by_user_id` | String(36)/String(64), nullable | the trusted identity whose `verdict_trusted=true` comment closed the item — the value the F1 self-review check compares against `implementer_*`. |

The self-review check (§3.7.2) is then a real server-side comparison: reject auto-close where `closed_by_* == implementer_*`; **fail-closed** when `implementer_*` is null/unknown for an `implement_until_done` item.

#### 3.6.2 API surface (register / write / hydrate / compact / read)

New `routes/residents.py`, all project-scoped, all enforcing the §3.6.3 isolation predicate on every call:

- `POST /api/v1/projects/{pid}/residents` — **register** a resident (org-admin gated; binds `service_key_id` + `persona_name` + `kind` + `work_queue_id`). Validates the service key belongs to the org and is not already bound (`uq_resident_service_key`). **(R3, F4 — SoD mutual-exclusion, server-enforced):** registering an `implementer` resident **REJECTS** a `service_key_id` that is already a `trusted_reviewers` identity for the same project/org, and registering a `reviewer` (or `register_trusted_reviewer`, §4.1) **REJECTS** a `service_key_id` already bound to an `implementer` resident for the same project/org. One credential can never hold both roles — the self-review prohibition (§3.7.2) is thereby enforced at bind time, not only at close time.
- `GET /api/v1/projects/{pid}/residents` / `GET .../{id}` — inspect (admin/observability).
- `POST .../residents/{id}/status` — pause / retire (the lifecycle kill switch; §6).
- `POST .../residents/{id}/rotate-key` — **(R3, F7) rebind on service-key rotation.** Admin re-points `residents.service_key_id` to a freshly-minted key (same org, same SoD checks as register). The **rotated-out key is denied at `require_scope` immediately on revoke — there is NO window** (revocation is already checked on every request via `api_keys.revoked_at`; the resident simply presents the new key on its next wake). The resident's durable mind is unaffected (it is keyed by `resident_id`, not by the service key). **Tests:** old key denied the instant it is revoked; new key rebinds and can read the same mind.
- `POST .../residents/{id}/memory` — **write** an entry (`reasoning`/`observation`). Scope `resident_memory:write`. Server assigns `seq`. **(R3, F6 — server-enforced caps, NOT client discipline):** the write is rejected (or forces server-side supersession of the oldest live entry) when the resident already has `max_uncompacted_entries` live entries; and a **per-wake write cap** bounds how many memory writes one `directive_id`/wake may make. This makes a runaway or hostile resident unable to storage-DoS its own org.
- `GET .../residents/{id}/memory/hydrate` — **hydrate**: latest `digest` + the most recent K uncompacted `reasoning` entries, bounded by `mind_token_budget`. Scope `resident_memory:read`. **Replaces** R1's `fork_session`-from-checkpoint.
- `POST .../residents/{id}/memory/compact` — **compact**: atomically inserts a new `digest` and marks the summarized prior entries `superseded_by`+`compacted_at`. The digest content is produced **client-side** (resident's own LLM or a deterministic summarizer) — **the server only stores + supersedes, never summarizes** (no server-side LLM, **C1**).
- `POST .../residents/{id}/memory/quarantine` — **(R3, F5) quarantine signal.** An org owner/admin (or an automated integrity check) can flag a resident's mind as **suspected-poisoned**: the resident is paused and its next start must `--cold`-rebuild AND treat prior `digest`/`reasoning` as untrusted (re-derive from durable shared sources: compiled context + KB + wiki), because **`--cold` alone clears only the WARM cache and durable-memory poisoning survives re-hydration** (§3.4, §6.2). For **implementer** minds specifically, the quarantine + owner-visibility is **not deferred** (see below); the general owner-only *export* stays a v2 governance decision (§8.3).
- **(R3, F5) Implementer-mind provenance + owner visibility.** Because an implementer's mind influences code, its `resident_memory_entries` carry provenance (which ticket/directive produced each entry) and are **owner-inspectable in v1** via an owner-gated `GET .../residents/{id}/memory` (audited) — a deliberate, narrow exception to C5's resident-private default, limited to `kind='implementer'` residents and org-owner callers. Reviewer minds stay fully resident-private (export deferred, §8.3).
- Admin **retention sweeper**: hard-deletes `compacted` (superseded) entries older than the retention floor. **(R2.1, SETTLED) Free-tier floor = 30 days** for compacted entries; the **active `digest` and un-superseded `reasoning` are always kept** (the mind is never wiped out from under a running resident). The 30-day value is a **tunable default** (operator/tier-overridable within caps); paid tiers get longer retention (§7.1).

New scopes `resident_memory:read` / `resident_memory:write` added to the catalog (16→18). Thin inspect-only MCP tools for humans/agents are v1.1; the resident runtime uses REST directly (it is a bespoke long-running process, not an MCP agent).

#### 3.6.3 Single-org + resident-private isolation — SERVER-ENFORCED (hard boundary, C5)

Every memory read/write enforces, server-side, in one predicate:
1. `assert_service_key_can_access_project(ctx, resident.project_id)` (v0.10.10 helper), **AND**
2. `resident.org_id == ctx.org_id` **AND** the entry's denormalized `org_id == ctx.org_id`, **AND**
3. **`resident.service_key_id == ctx.service_key_id`** — a resident may read/write **only its own mind**, not another resident's, *even within the same org*. The mind is **resident-private**, not merely org-private.

This is strictly stronger than the work-queue project-scope check and is enforced by the *server*, not client-side profile convention: a misconfigured, buggy, or compromised resident **cannot** read another org's — or another resident's — mind. Client-side org-scoped disk storage (§4.2) remains defense-in-depth for the *warm cache on disk* only. **This is DECISION 1's core security requirement (C5) and a headline Sentinel must-pass (§9 hook 9).**

#### 3.6.4 How hydrate → writeback → compact map onto the primitive

| R1 (session-capture) | R2 (primitive) |
|---|---|
| `fork_session` from last checkpoint | `GET .../memory/hydrate` → latest `digest` + recent `reasoning` |
| write rolling reasoning to the session transcript | `POST .../memory` (`kind='reasoning'`) |
| `checkpoint_session` + summarize-and-trim | `POST .../memory/compact` (client summarizes → server stores `digest`, supersedes old entries) |
| durable shareable claims | **unchanged** — still `add_knowledge`/`update_wiki_page` (team-readable; the memory primitive is the resident's *private* mind, KB is the *shared* mind) |

### 3.7 The implementer resident (R2 — DECISION 2; a first-class v1 citizen, and the security crux)

R1 deferred the code-writing resident. **CEO DECISION 2 makes `implement_until_done` a first-class v1 citizen.** A resident that autonomously *writes code* is a materially larger safety surface than one that posts verdicts, so the containment + the authoritative gate are the crux. The load-bearing property (§1.3) — **the mind ADVISES, the ledger + server DECIDE** — must hold even when the resident writes code.

#### 3.7.1 Repo-mutation boundary — where it runs, what it can touch (C6, C7)

- **Code custody is HOST-LOCAL (C7, binding).** Every git operation — worktree checkout, branch commits, diffs, builds, test runs — lives **entirely on the operator's host**. **SessionFS the server NEVER holds, stores, or transports repo contents;** it only ever sees **tickets, comments, and diff-refs** (pointers/metadata, never the code). The server is categorically out of the code-custody blast radius — a SessionFS compromise cannot reach customer source.
- **Worktree-isolated.** The implementer operates in a **dedicated git worktree on a resident-owned branch** (`resident/<queue>/<ticket_id>`), **never** the operator's primary working tree, **never** `develop`/`main`/any protected branch. File writes + commits are confined to that worktree + branch (mirrors the harness's own `isolation: "worktree"` model).
- **Sandboxed execution.** Any code it runs (tests, builds, linters) runs in a **contained sandbox** (container/jail), least-privilege, **no access to operator secrets** beyond an explicit minimal grant the operator configures locally. Blast radius = worktree + feature branch + sandbox.
- **Propose, never dispose.** The resident may create/modify files, commit to its resident branch, and **open a change for review** (a PR / a posted diff-ref + ticket transition to `review`). It **CANNOT** push to a protected branch, **CANNOT** merge, **CANNOT** release/deploy. Human-gated merge stays (CLAUDE.md branch policy + work-queue §1.1 "no automatic merge/deploy"). **Code never reaches `main` via a resident.**
- **"Cannot push/merge" is an OPERATOR responsibility, not a server-enforced product guarantee (R3, F8 — honest framing).** SessionFS the server never brokers git (C7), so it *cannot* enforce push/merge restrictions — those live entirely in the operator's host/git/CI permission model. The product provides the **Forge hardened sandbox profile** (git remotes read-only or absent, no protected-branch credentials, no merge tokens) and **Scribe operator docs** as the mechanism; the operator is responsible for deploying the resident inside that boundary. We state this plainly rather than implying the server polices code mutation.
- **The reviewer that closes this work is CO-LOCATED and reads the diff LOCALLY (C8).** The independent reviewer resident sees the implementer's change by reading the local worktree/branch/diff on the same host (or a host with local read access), **never** by the server transporting the diff. A `diff-ref` on a ticket/comment is a pointer (branch, SHA, changed-path list, PR URL) — metadata only. See §3.7.4, C8.

#### 3.7.2 The authoritative implement-side gate (the equivalent of the stop-oracle)

For `review_until_clean`, closure authority is the server stop-oracle re-deriving `review_state` over `verdict_trusted=true` comments. For `implement_until_done` the analogous question is: **what stops a resident self-approving its own code into a closed/merged state?** The answer, preserving §1.3:

1. **An implementer's own "done" is NEVER a close (F9 — a CHANGE from current behavior).** The implementer resident can only transition its queue item to `waiting_review`. **Today `implement_until_done` self-closes on the agent's *claimed* outcome** (pre-existing hole, `work_queues.py` ~1274) — R0 **must** replace that with the re-derive path below. Until it ships, **no implementer resident may point at an `implement_until_done` queue** (hard sequencing gate, §10).
2. **The authoritative close RE-DERIVES `review_state` server-side over `verdict_trusted=true` comments (F1b) — never the agent's claimed outcome.** An `implement_until_done` item reaches terminal `done` **only** when the server, re-deriving exactly as review mode does, finds strict `VERIFIED-CLEAN` + no open findings from a `verdict_trusted=true` comment posted by a **different** trusted identity. The implement-side `complete_work_queue_step` treats the agent's "done" as a hint, identical to how the review side treats a claimed verdict.
3. **Server-enforced self-review prohibition — now genuinely enforceable (F1, binding — C6).** R2.1 stated this rule but Sentinel found it **unenforceable**: `work_queue_items` had no implementer-identity column to compare against. R0 fixes it (migration 057, §3.6.1): (a) **record `implementer_service_key_id`/`implementer_user_id`** on the item at implement-side claim + settle; (b) the close path compares the closing `verdict_trusted=true` identity (`closed_by_service_key_id`/`closed_by_user_id`) against the recorded `implementer_*`; (c) **reject auto-close where they match**; (d) **FAIL-CLOSED — no auto-close at all when `implementer_*` is null/unknown/unregistered** for an `implement_until_done` item (an unattributable item never auto-closes; it waits for a human). Belt-and-suspenders at bind time: the F4 mutual-exclusion (§3.6.2/§4.1) forbids one `service_key_id` holding both roles, so the runtime comparison is defense-in-depth on top of a registration-time guarantee.
4. **Merge stays HUMAN-ONLY for v1.** Even a clean, independently-reviewed change only marks the item `done` + the ticket ready-to-merge. **No resident merges, and v1 ships with NO automated-merge path at all** (an authorized non-resident automation merge is a possible v2, out of scope now). The furthest state a resident can drive a ticket to is "reviewed clean, awaiting **human** merge."

**The gate is UNIFORM across all change classes (R2.1→R3, CEO SETTLED — no per-class carve-out).** There is deliberately **no** special "human-required" auto-close tier for auth/crypto/migrations/CI/dependency changes: the same four-step model (implementer "done" → `waiting_review` → independent trusted-reviewer re-derived close → human merge) governs **every** change. Rationale (CEO): uniform simplicity over per-class gating; a per-class allowlist is itself an error-prone security surface. **The CEO attached two NON-NEGOTIABLE, binding conditions to the uniform gate (R3):**

- **(Condition a — the item is honestly labeled, binding).** Every item auto-closed by a **resident** reviewer is stamped `auto_close_review_kind = 'resident_trusted'` (§3.6.1) — a server-set, **queryable, human-visible `"resident-reviewed, NOT human-reviewed"` marker**, surfaced everywhere a human sees the item (ticket, queue view, PR/merge surface). A human closing by their own verdict stamps `'human'`. **The merger is never misled by a green item** — they can always see whether a human ever reviewed it.
- **(Condition b — high-risk merges follow a documented non-rubber-stamp checklist, binding).** Merging a resident-auto-closed change in a high-risk class (DB migrations, auth, crypto, CI/CD, dependency bumps) requires a **mandatory documented merge checklist** that is explicitly a **non-rubber-stamp** gate. Owned by **Scribe** (the checklist doc) + **Shield** (the governance gate that requires it). This is the operationalization of "the human merge is load-bearing."

> **Honest residual (retained for Sentinel, §9 hook 10).** Under the uniform gate, a **high-risk change** — e.g. a **DB migration**, the exact class behind **two prod incidents this cycle** where automated review passed but PG-only bugs slipped through — can auto-close the queue **ITEM** on a resident-reviewer `VERIFIED-CLEAN`. Auto-closing the item does **not** merge or deploy (C6/C7); it only marks the ticket "reviewed clean (**resident-reviewed, NOT human-reviewed**, condition a), awaiting human merge." So the **human MERGE is the sole remaining human touchpoint**, which makes the merge backstop **LOAD-BEARING** — hence condition (b)'s mandatory non-rubber-stamp checklist for high-risk classes. This is an **accepted CEO tradeoff** (uniform model + the two conditions, over per-class auto-close gating).

Safety chain for the implementer: **host-local code custody → worktree isolation → propose-only → independent re-derived trusted review (self-review prohibited, fail-closed on unknown identity) → honest `resident-reviewed` marker → human merge gate (checklist-gated for high-risk).** Six independent barriers, none self-satisfiable; the human merge is the last and load-bearing one.

#### 3.7.3 Trust boundary for CODE vs verdicts

Verdicts have a trust flag (`verdict_trusted`). **Code has no "trusted" flag — code is inherently untrusted until independently reviewed and human-merged.** There is deliberately **no** `code_trusted` analog; the implementer's output earns trust *downstream* via review + human merge, never by assertion. Consequences:

- The implementer resident does **not** hold reviewer trust and does **not** register in `trusted_reviewers` for its project (§3.7.2 rule 3).
- Its service-key scopes are implement-appropriate (`tickets:write`, `work_queues:write`, `agent_runs:write`, `knowledge:read/write`, `resident_memory:read/write`, `sessions:read`) — **no** reviewer capability, and **no** push/merge capability (that lives in git/host permissions the operator does **not** grant the resident's environment).

#### 3.7.4 Reviewer resident ↔ implementer resident relationship

- **SEPARATE residents, processes, service keys, personas, and memory primitives** — never one process. Separation is **required** by §3.7.2's self-review prohibition: the identity that closes must differ from the identity that implemented.
- They **compose** exactly like the shipped work-queue pairing (`agent-work-queues.md` §6): an `implement_until_done` queue (persona e.g. `atlas`) and a `review_until_clean` queue (persona `codex-reviewer`) over the **same ticket set**. The implementer's `waiting_review` items become the reviewer's work; the reviewer's `changes` verdicts become the implementer's next `fix_findings` directive — all mediated by `TicketComment` + `review_state`, **no shared mind**. Each resident hydrates its **own** org-scoped, resident-private memory (§3.6.3).
- The reviewer that closes an implementer's work is, by construction, a **different trusted reviewer** (a reviewer resident, the polling Codex, or a human) — exactly the anti-self-review property the CEO asked for. (§9 hook 12 flags the residual: two residents driven by the *same operator* are identity-separated but not adversarially independent — the human merge gate is the backstop.)

---

## §4 — Trust & Auth Model

### 4.1 Service key ↔ `trusted_reviewers`

The resident authenticates to the SessionFS API as a **scoped service key** (`key_kind='service'`, v0.10.10): org-bound (`api_keys.org_id`), project-allowlisted, least-privilege scopes:

| Scope | Why |
|---|---|
| `work_queues:read` / `work_queues:write` | drive the heartbeat + settle |
| `tickets:read` / `tickets:write` | the settle-path creates the verdict comment server-side; read tickets/comments for context |
| `agent_runs:write` | per-wake execution audit (optional, if the wake opens an `AgentRun`) |
| `knowledge:read` / `knowledge:write` | hydrate + write back durable learnings |
| `sessions:read` | session lineage hydration; read its own checkpoints |

To make its verdicts **count**, the resident's **`service_key_id` is registered in `trusted_reviewers`** (via the admin registry, `routes/trusted_reviewers.py` / `sfs admin trusted-reviewers add`), bound to `reviewer_persona = <the queue's assigned_persona>` (e.g. `codex-reviewer`), scoped project-wide or org-wide. Then, on each settle-path verdict:

1. The resident calls `complete_work_queue_step(verdict_content=…)` with its service key.
2. The server creates the verdict `TicketComment` **itself**, stamping `author_persona` from `queue.assigned_persona` and `verdict_trusted` from `is_registered_trusted_reviewer(db, project_id, org_id, user_id, service_key_id, claimed_persona)` (`routes/tickets.py:2059`).
3. `is_registered_trusted_reviewer` matches a **service-key request ONLY to a `service_key_id` row** — a service key running "as" a user can never inherit a human's reviewer trust (strict identity isolation, already enforced).
4. The server-side stop oracle (`compute_review_state` over `verdict_trusted=true` only) then — and only then — can auto-finish the item on a **strict** `VERIFIED-CLEAN` + no open findings.

**(R3, F4) SoD mutual-exclusion at registration (server-enforced, both directions).** `register_trusted_reviewer` **REJECTS** a `service_key_id` that is already bound to an `implementer` resident on the same project/org; symmetrically, registering an `implementer` resident (§3.6.2) **REJECTS** a `service_key_id` already registered as a `trusted_reviewers` identity for the same project/org. One credential can never simultaneously be an implementer and a trusted reviewer — so the §3.7.2 self-review prohibition is guaranteed at *bind* time, and the runtime identity comparison (§3.7.2 rule 3) is defense-in-depth, not the sole control. This is Sentinel F4 made real prose.

**Kill switch:** revoking the registry row (`is_active=false` / `revoked_at`) makes the resident's *future* verdicts land `verdict_trusted=false` — they render but contribute **nothing** to closure. Revoking the service key stops the resident authenticating at all (checked on every request; no window — §3.6.2 F7). Neither rewrites settled verdicts. This is the operator's instant "stop trusting this resident" control.

### 4.2 Single-tenant isolation — no cross-org leak (hard boundary, **C4**)

**One resident process = one org = one service key = one `trusted_reviewers` identity = one org-scoped local mind store.** Rationale and enforcement:

- **Server-side (already enforced):** every API read/write the resident makes re-asserts `assert_service_key_can_access_project(ctx, project_id)` + the key's project allowlist + `queue.project_id == ticket.project_id` (work-queue R5/R10). A resident's service key **cannot** read another org's KB, sessions, wiki, or queue. The boundary is enforced on every call, not just at config time.
- **Durable mind (R2 — server-isolated, C5):** the authoritative durable mind lives in the resident-memory primitive (§3.6), whose single-org + resident-private isolation is a **server** guarantee (§3.6.3) — not a client convention. This is the strongest layer.
- **Client-side warm cache (defense-in-depth):** the resident's **local warm-digest cache must still be org-scoped on disk** so a compromised process cannot read a *cached* mind from another org. Reuse the named-auth-profiles isolation (v0.10.29 `profiles.py`): a per-org profile with an isolated `store_dir` + atomic `0600` writes for the service key. The warm cache lives under the org's profile dir; the durable mind is fetched from the server per §3.6.
- **Why NOT a shared multi-tenant resident:** a single process hydrating two orgs' contexts into one LLM context window is *exactly* the cross-tenant leak we forbid (and it would also need server-side LLM keys, **C1**). Multi-org operation is **explicitly out** (§7) — run N processes.

### 4.3 No trust forging

The only path to `verdict_trusted=true` is §4.1 (registered identity + settle-path). Restated as a closed list of what is **NOT** a trust source:

- a request-body `author_persona` string — server ignores it on the settle-path (server-derived from queue config);
- a request-body `verdict_trusted` — server-stamped only, never accepted as input;
- `assume_persona` — explicitly **not** a trust source (a resident "assuming" the reviewer persona confers no trust);
- the generic `add_ticket_comment` path — lands `verdict_trusted=false` for a reviewer (only the settle-path stamps trust);
- a service key inheriting a human's registry row — blocked by the strict service-key/user-key identity isolation in `is_registered_trusted_reviewer`.

The resident's *only* lever on the oracle is: be registered, and post genuine verdict text through the settle-path. It cannot self-certify.

### 4.4 Code vs verdict trust; separation of duties (R2 — DECISION 2, C6)

The implementer resident (§3.7) introduces a second trust question — *code* trust — which is deliberately answered differently from *verdict* trust. Restated as auth rules:

- **Verdict trust** = `verdict_trusted`, via a registered `trusted_reviewers` identity + the settle-path (§4.1). Only reviewer residents obtain it.
- **Code has NO trust flag.** An implementer resident asserts nothing; its output's trust is earned downstream by independent review + human merge (§3.7.3).
- **Self-review prohibition (server-enforced).** The server rejects an `implement_until_done` auto-close whose closing `verdict_trusted=true` identity == the implementer's identity on the same item (§3.7.2 rule 3). This is the SoD teeth.
- **Least-privilege, non-overlapping scopes.** An implementer service key carries no reviewer capability and is not a `trusted_reviewers` row for its project; a reviewer service key carries no push/merge/implement capability. No single credential can both write code and close its own review.
- **No resident holds git push/merge capability** — that lives in host/git permissions the operator does not grant the resident's environment (§3.7.1).

---

## §5 — The Operator's Own LLM (client-side credential boundary)

Per **C1**, the resident calls the **operator's own external model** with the operator's own credentials, **client-side**. **(R2 — CEO confirmed:** one BYO OpenAI-compatible/Codex reference adapter, **no SessionFS default model**.) Design:

- **Pluggable LLM adapter.** The resident has a thin "reviewer LLM" interface: *given a bounded review context, return a verdict + reasoning.* The operator configures their model endpoint + key in **local resident config** (env / local TOML, mirroring the `GOOGLE_GEMINI_BASE_URL` / `GEMINI_API_KEY` proxy pattern in `CLAUDE.md`'s Multi-LLM Review). v1 ships **one reference adapter** for an OpenAI-compatible / Codex endpoint (what the CEO already uses). SessionFS ships **no default model** — bring-your-own is the contract.
- **Two single-purpose credentials that never cross:**
  - the **operator LLM key** lives **only** in local resident config, used **only** to call the operator's LLM endpoint. It is **never** sent to the SessionFS server, never embedded in a service key, never logged.
  - the **SessionFS service key** is used **only** for SessionFS API auth. It is never sent to the LLM provider.
  The resident is the trust junction, but each credential is single-purpose and one-directional.
- **The server never sees the brain.** SessionFS receives only the *outputs* of LLM reasoning (the verdict text via the settle-path, KB/wiki writeback). The prompt, the LLM endpoint, and the LLM key are wholly client-side. This preserves the standing "NO server-side LLM API keys / all LLM calls client-side" Key Decision verbatim.
- **Model-agnostic.** Any operator LLM that can read a review context and emit a verdict + reasoning works. SessionFS supplies memory + ledger + trust seam; the operator supplies the brain.

---

## §6 — Failure Modes + Safety Envelope

### 6.1 Inherited from work queues v1 (unchanged — the resident gains nothing and loses nothing)

Attempt cap (`max_attempts_per_item` default 3, counting emitted directives only); backoff 2m→5m→15m→60m; per-wake cap (`max_tickets_per_run`); dedicated `/step` rate-limit class (app quota + Cloud Armor deny-429); **strict `VERIFIED-CLEAN`-only** auto-stop (aliases never auto-close); server-side re-derive of `review_state` over `verdict_trusted=true` only at settle. A misbehaving resident is bounded by all of these on the *server* side regardless of what the client does.

### 6.2 Resident-specific failure modes

| Failure | Behavior / mitigation |
|---|---|
| **Operator LLM outage / error** | **Fail-closed.** No verdict is posted; the directive lease stays open and re-emits next wake; the resident records a no-op/errored wake. **Never** a default-clean verdict. A resident must never post a verdict it did not get from the LLM. |
| **LLM budget exhausted** | Resident parks (no-op wakes) until the budget window resets; queue backs off. Bounded spend (§3.5). |
| **Stale mind** | Re-hydrate from durable SessionFS (authoritative over in-RAM). `--cold` discards the local **warm cache**. Mere staleness is correctness-irrelevant (§1.3) — it degrades review *quality*, not safety. |
| **POISONED durable mind (R3, F5 — distinct from stale)** | `--cold` does **NOT** fix this — re-hydration reloads poisoned durable `resident_memory_entries`. Remedy = the **quarantine signal** (§3.6.2): pause + force re-derivation from durable *shared* sources (compiled context + KB + wiki), ignoring the resident's private entries. Implementer minds are **owner-inspectable in v1** (§3.6.2) so poisoning is visible. Correctness backstop stands regardless: a poisoned reviewer mind still can't self-certify (C2), a poisoned implementer mind still can't self-close/self-merge (C6). |
| **Resident crash** | Heartbeat re-emit + re-hydrate (§3.4). No lost/double review; no false close. |
| **Revoked trust** | `trusted_reviewers` `is_active=false` → future verdicts `verdict_trusted=false` → cannot auto-stop (kill switch, §4.1). Service-key revoke → cannot authenticate. |
| **Prompt-injection via reviewed content (reviewer)** | The resident reads untrusted content (ticket bodies, implementer comments, diffs) into its prompt. An injection that coerces the LLM to emit `VERIFIED-CLEAN` **would** produce a (genuine, trust-stamped, but wrong) verdict — a **review-QUALITY** risk, not a trust-forging risk, and it is real. Mitigations: treat all reviewed content as **data, not instructions** in the prompt scaffold; keep a human spot-check / sampling expectation on auto-closed items; the strict-`VERIFIED-CLEAN` + no-open-findings gate still requires the LLM to actively assert clean. Flagged for Sentinel (§9 hook 6). |

### 6.3 Implementer-resident failure modes (R2 — DECISION 2, the larger surface)

| Failure | Behavior / mitigation |
|---|---|
| **Prompt-injection / poisoned mind → genuine-but-HARMFUL code (the worse analog of §6.2 injection)** | An injection in a ticket/comment/diff, or a systematically-poisoned mind, could make the implementer write harmful code (a backdoor, a data-exfil path, a deleted safety check). Unlike the reviewer case, the artifact is executable. **Containment (C6/C7):** code custody is host-local (server can't touch it); the code is worktree-isolated + sandboxed (blast radius bounded); it **cannot self-close** (independent trusted review, §3.7.2), **cannot self-merge / cannot reach `main`** (human merge gate). So harmful code must ALSO defeat the independent reviewer AND the human merge — defense in depth. **Residual (honest, R2.1 — uniform gate, accepted CEO tradeoff):** there is NO per-class elevated gate; a high-risk change (e.g. a DB migration) can auto-close the queue ITEM on a resident-reviewer `VERIFIED-CLEAN`, leaving the **human MERGE as the sole remaining human touchpoint** — the merge backstop is **load-bearing** and must not be a rubber-stamp. Sentinel to weigh sufficiency. Flagged §9 hook 10. |
| **Sandbox escape / secret access** | The implementer's execution sandbox is least-privilege with no operator secrets beyond an explicit grant (§3.7.1). A sandbox escape is the highest-severity failure; Forge owns the containment design. Kill switch: retire the resident + revoke the service key + discard the worktree/branch. |
| **Uncommitted / abandoned work** | Worst uncontained artifact is an **un-merged resident branch** — cheap to discard. Nothing reaches protected branches. |
| **Self-review attempt (R3, F1 — now enforceable)** | Server records `implementer_*` on the item (§3.6.1) and rejects an auto-close where `closed_by_* == implementer_*`; **fail-closed** when the implementer identity is unknown/unregistered (no auto-close, waits for a human). Registration-time F4 mutual-exclusion means one key can't hold both roles. |
| **Unattributable implement item (R3, F1d)** | An `implement_until_done` item with no recorded implementer identity **never auto-closes** — it parks for human review. Prevents a close-without-attribution bypass. |
| **Resident crash mid-implementation** | Task state re-emits via the heartbeat (§3.4); the worktree is re-derivable from the resident branch; mind re-hydrates. No false close (server oracle). |

---

## §7 — v1 vs Deferred

| Capability | v1 | Deferred (v2+) |
|---|---|---|
| Reviewer resident (`review_until_clean`) | ✅ | — |
| **Implementer resident (`implement_until_done` — writes code) (R2)** | ✅ (worktree-isolated, propose-only, self-review-prohibited, **uniform** gate, human-merge-only) | + auto-open-PR integrations; authorized-automation merge (needs its own design) |
| **Resident-memory PRIMITIVE (residents + resident_memory_entries, migration 057) (R2)** | ✅ (server-isolated hydrate/write/compact) | + longer retention tiers; inspect MCP tools |
| Hydrate / writeback / compact living context | ✅ | — |
| Operator's-own-LLM pluggable adapter + 1 reference (OpenAI-compatible/Codex) | ✅ | + certified adapters for more providers |
| Service key + `trusted_reviewers` settle-path verdicts | ✅ (all shipped) | — |
| Single-org, one-process-one-org isolation | ✅ | — |
| Crash recovery via heartbeat re-emit + re-hydrate | ✅ | — |
| Single queue per resident | ✅ | multiple concurrent queues per resident (paid, §7.1) |
| Event-driven wake (webhook → trigger, no polling) | ❌ | ✅ (work-queue v2; needs no-realtime exception sign-off) |
| Managed/SessionFS-hosted LLM runtime | ❌ (collides with C1) | only via a customer-supplied-key escrow — separate hard design, likely never |
| Multi-org single process | ❌ (forbidden, C4/C5) | ❌ (stays forbidden — run N processes) |
| Org-wide resident FLEET management + health dashboard (Prism) | ❌ | ✅ (paid, §7.1) |
| Fine-grained mind-sharing across a team of residents | ❌ (KB gives coarse sharing today) | ✅ |

### 7.1 Packaging & tiering (R2 — CEO DECISION 3: free local runner + paid managed features)

CEO DECISION 3: **the operator-hosted runner itself — BOTH reviewer and implementer residents — is FREE to all tiers.** The core "a resident on your machine that remembers your project" is the adoption wedge and the flagship "memory layer for AI agents" demo; gating it would blunt the wedge. The **higher-touch / managed / at-scale** pieces are Team+/Enterprise. Explicit line:

| Capability | Free (all tiers) | Paid (Team+/Enterprise) |
|---|---|---|
| Operator-hosted runner (reviewer AND implementer) | ✅ | ✅ |
| BYO-LLM adapter, local kill switch, worktree/sandbox isolation | ✅ | ✅ |
| **One** resident, **one** queue, single-project | ✅ | — |
| Resident-memory primitive storage | ✅ **30-day** compacted-entry retention floor (active digest always kept); tunable | longer retention + higher volume |
| Multiple residents / **multi-queue-per-resident** / cross-project | — | ✅ |
| **Org-wide resident fleet management** (register/rotate/retire many residents across projects+members) | — | ✅ |
| **Dashboards / observability** (resident health, fleet view, spend) | — (CLI readout only) | ✅ (Prism) |
| **Governance / SSO ties** (who may register a trusted reviewer / a resident; audit exports) | — | ✅ (ties to the OIDC-SSO + org control plane) |
| Service-key CI/cron wake at scale | inherits the work-queue tier line | ✅ |

**Work-queue §13 tier-line correction (binding):** the work-queues design (`agent-work-queues.md` §13) recommended "free = 1 *manual*-cadence queue, no service-key/CI wake." R2 **relaxes** that for the resident: a free operator may run **one service-key-driven resident** (reviewer or implementer) over **one queue** locally — the service-key wake is what makes a persistent resident possible, and it is now part of the free wedge. Fleet, multi-queue, observability, and governance remain paid. Ledger owns the final gate; this section is the Compass recommendation the CEO's DECISION 3 endorses.

---

## §8 — Open Questions for the CEO

### 8.1 R1 questions — RESOLVED by CEO (R2)

1. Runtime placement → **operator-hosted always-on** (confirmed as recommended). §3.1.
2. Private-mind surface → **first-class resident-memory primitive**, NOT session reuse (DECISION 1). §3.6.
3. Packaging → **free local runner (review+implement) + paid managed features** (DECISION 3). §7.1.
4. LLM adapter → **one BYO OpenAI-compatible/Codex reference, no default model** (confirmed). §5.
5. v1 scope → **include the implementer resident** (DECISION 2). §3.7.

### 8.2 R2 questions — SETTLED by CEO (R2.1) and folded into binding design

The DECISION-1/DECISION-2 questions R2 raised are now decided and live in the binding sections (no longer open):

1. **Memory retention floor + volume** → **SETTLED: 30-day** compacted-entry floor, active digest always kept, tunable; longer on paid (§3.6.2, §7.1).
2. **Sandbox/worktree host + provisioning** → **SETTLED: co-located on the operator host** for v1 via the worktree model; Forge owns a hardened container profile + the minimal secret grant (§3.7.1, §11 Forge).
3. **Git custody** → **SETTLED (binding C7): entirely host-local**; SessionFS never brokers/stores repo contents, only tickets/comments/diff-refs (§2 C7, §3.7.1).
4. **Merge gate** → **SETTLED: human-only for v1**, no automated-merge path (§3.7.2 rule 4).
5. **High-risk change classes** → **SETTLED: UNIFORM gate, no per-class carve-out** (§3.7.2); the accepted-tradeoff residual (human merge is load-bearing) is retained for Sentinel (§9 hook 10).

### 8.3 Deferred (not a v1 blocker) — one item for a later paid/governance decision

- **Owner-only audited mind-export (governance visibility of the resident-private mind).** Resident-memory is resident-private by design (C5) — not even org admins read it via the normal API. A compliance customer may later want a **break-glass, org-owner-only, audited** export of a resident's reasoning (esp. the implementer's, since it influences code). **DEFERRED to a paid/v2 decision** — the privacy-vs-compliance tension is noted but **not designed now**; resident-private stays absolute in v1 (never a silent admin read).

---

## §9 — Security Review Hooks (what Sentinel must scrutinize)

This section is the **Sentinel review surface**. Each hook maps to an existing or proposed control.

1. **Trust forging.** Verify the settle-path is the **only** route to `verdict_trusted=true`; that request-body `author_persona`/`verdict_trusted` and `assume_persona` are never trust sources; that `is_registered_trusted_reviewer` keeps strict service-key/user-key identity isolation (a service key cannot inherit a human's reviewer row); and that the registry kill switch (`is_active=false`) genuinely stops *future* verdicts from counting. **Must-pass test:** a resident whose registry row is revoked posts `VERIFIED-CLEAN` → `verdict_trusted=false` → the oracle does **not** auto-close.
2. **Cross-org context leak (the hard boundary, C4).** Verify one-process-one-org; that the resident's **local mind store is org-scoped on disk** (profile isolation, `0600`); that **every** server read/write re-asserts `assert_service_key_can_access_project` + project allowlist + `queue.project_id == ticket.project_id`; that no API path lets the resident's service key read another org's KB / sessions / wiki / queue; and that the warm digest cache cannot be poisoned with another org's data. **Must-pass test:** a resident's service key scoped to org A is denied on every org-B resource; the on-disk mind store for org A is unreadable from an org-B resident config.
3. **Client-side credential boundary (C1, §5).** Verify the operator LLM key **never** reaches the SessionFS server (not in any API payload, not in a service key, not in logs); the SessionFS service key **never** reaches the LLM provider; both are stored with `0600` perms (mirror `profiles.py` atomic-private-write); the service-key scopes are **least-privilege** (the list in §4.1, nothing broader).
4. **Runaway cost / loops.** Verify the **server** safety envelope (attempt cap, backoff, per-wake cap, `/step` rate-limit) holds against a misbehaving or hostile resident — i.e. the resident cannot bypass the cadence floor or attempt cap by re-registering, re-minting keys, or spamming `/step`. Verify the resident is **fail-closed** on LLM error/budget exhaustion (no default-clean verdict) and that its local per-wake + daily LLM-spend ceilings are enforced.
5. **Stop-oracle integrity under a resident (C3).** Verify the resident's living mind can **never** short-circuit the server-side stop oracle: closure requires server-re-derived strict `VERIFIED-CLEAN` + `verdict_trusted=true` + no open findings; the resident's claimed verdict is a hint, the server re-derives. **Must-pass test:** a resident that *asserts* clean on the settle-path while findings are open does **not** close the item.
6. **Prompt-injection → genuine-but-wrong `VERIFIED-CLEAN` (reviewer, review-quality risk, honest call-out).** The resident ingests untrusted reviewed content (ticket bodies, comments, diffs) into its prompt. An injection that coerces a (genuine, trust-stamped) wrong `VERIFIED-CLEAN` is **not** a trust-forging hole (the verdict is real, from a registered reviewer) but IS a real review-quality / auto-close-safety risk. Evaluate: reviewed-content-as-data prompt scaffolding, human spot-check/sampling on auto-closed items, whether strict auto-close needs a corroborating-signal bar for unattended operation.

### 9.1 R2/R3 hooks — the implementer + memory-primitive surface (DECISION 1 + 2; Sentinel F1–F9)

**These hooks are the R0 re-review scope.** Hooks 7 (F1), 14 (F4), 15 (F6), 16 (F7) verify the R0 server work; the implementer phases (R3+) are gated on Sentinel signing off the R0 implementation against them (§10).

7. **Self-review prohibition — F1, the headline R0 re-review item (C6, must-pass, FULL NEGATIVE MATRIX).** Verify R0 (a) records `implementer_service_key_id`/`implementer_user_id` on `work_queue_items` at implement claim + settle; (b) the implement-side close **re-derives `review_state` over `verdict_trusted=true` comments** (never the agent's claimed outcome — this replaces the F9 self-close hole at `work_queues.py` ~1274); (c) **rejects auto-close where `closed_by_* == implementer_*`**; (d) **fails closed — no auto-close when `implementer_*` is null/unknown/unregistered**. **Negative-test matrix (all must-pass):** (i) implementer posts its own clean verdict on its own item → NOT closed; (ii) same `service_key_id` implements + reviews → blocked at registration (F4) AND at close (identity match); (iii) `implementer_*` null → item never auto-closes, parks for human; (iv) independent trusted reviewer clean → closes; (v) agent claims "done"/clean while `review_state` re-derive shows open findings → NOT closed; (vi) `implement_until_done` queue with NO R0 rule present → implementer resident refused (F9 sequencing gate).
8. **Repo-mutation containment (C6/C7, F8 — honest scope).** Verify worktree isolation (writes confined to `resident/<queue>/<ticket>`, never protected branches) **as an operator-host property**. **State honestly (F8):** "cannot push/merge" is **NOT** a server-enforced product guarantee — SessionFS never brokers git (C7), so it *cannot* police code mutation; the guarantee is the **Forge hardened sandbox profile** (read-only/absent remotes, no protected-branch creds, no merge tokens) + **Scribe operator docs**. Sentinel verifies the *product's* boundary (server never receives repo contents) and the *operator profile* recommendation; it must NOT credit the server with enforcing push/merge. **Test:** the server rejects/has-no-path-for any repo-content payload; the Forge profile blocks push to `develop`/`main`.
9. **Resident-memory isolation (C5, must-pass).** Verify **every** memory read/write enforces `resident.org_id == ctx.org_id` AND the denormalized entry `org_id == ctx.org_id` AND **`resident.service_key_id == ctx.service_key_id`** — a resident reads/writes only **its own** mind, not another resident's (even same-org), never another org's. Verify the retention sweeper respects isolation. Verify migration 057 is a clean additive SQLite `upgrade()`/`downgrade()`. **Test:** resident A's service key is denied on resident B's memory endpoints (same org and cross-org); **a forged `resident_id`/`org_id` in the request body is ignored (server-derived from the resident row + AuthContext)**; the sweeper never deletes across residents/orgs.
10. **Implementer prompt-injection / poisoned mind → harmful code (the WORSE analog of hook 6).** An injection or systematically-poisoned mind produces genuine-but-harmful code. **The gate is UNIFORM (CEO-settled, no per-class carve-out) + two binding conditions (§3.7.2):** (a) the item is stamped `auto_close_review_kind='resident_trusted'` — a server-set, human-visible **"resident-reviewed, NOT human-reviewed"** marker so the merger is never misled; (b) high-risk merges require a **mandatory documented non-rubber-stamp checklist** (Scribe doc + Shield gate). All changes go implementer→`waiting_review`→independent re-derived close→human merge, code custody host-local (C7). Sentinel weighs whether marker + checklist + human merge are sufficient given the residual: a high-risk change (DB migration — two prod incidents this cycle) can auto-close the ITEM on a resident verdict, leaving the **load-bearing human MERGE** as the sole human touchpoint. Per-class auto-close gating was **rejected by the CEO** in favor of the uniform+conditions model — Sentinel may re-raise if it judges the conditions insufficient.
11. **Memory integrity + POISONING (F5, corrected).** Verify entries are append-only, server-assigned `seq`, resident-private (no cross-resident forge/inject). **State honestly: `--cold` clears only the WARM cache — a poisoned *durable* mind survives re-hydration.** Verify the **quarantine signal** (§3.6.2) pauses the resident and forces re-derivation from durable *shared* sources; verify **implementer-mind owner-visibility** (owner-gated audited `GET .../memory` for `implementer` residents) so poisoning is inspectable. The correctness backstop stands regardless (poisoned mind still can't self-certify / self-close).
12. **Separation-of-duties collusion (honest call-out).** Two residents (implementer + reviewer) driven by the **same operator** are *identity*-separated (F1/F4 hold) but not adversarially independent — one operator controls both LLMs, and per C8 they may be **co-located**. Sentinel to rule whether certain change classes need a **human** or **cross-operator** reviewer. Backstops: the human merge gate + condition-(a) marker + condition-(b) checklist.
13. **Co-location / C7-vs-reviewer coherence (F2, must-pass).** Verify the reviewer reads the diff **locally** and that **no code ever flows through the SessionFS server** — a `diff-ref` is pointer/metadata only (branch/SHA/paths/PR URL), never diff contents. **Test:** no API accepts or stores diff/file contents; the reviewer's independence is by identity (C8), not by server-mediated code transport.
14. **SoD mutual-exclusion at registration (F4, must-pass).** Verify `register_trusted_reviewer` rejects a `service_key_id` already bound to an `implementer` resident (same project/org) and vice-versa. **Test:** both bind orders are rejected; an existing dual-bound row (if any legacy) is surfaced, not silently trusted.
15. **Memory storage/cost DoS (F6, must-pass).** Verify the **server** hard cap on live (un-compacted) `resident_memory` entries (`max_uncompacted_entries`) and the per-wake write cap — enforced server-side, NOT reliant on client compaction discipline. **Test:** a resident spamming writes is rejected / oldest force-superseded; it cannot storage-DoS its own org.
16. **Service-key rotation (F7, must-pass).** Verify `rotate-key` rebinds `residents.service_key_id` (with the F4 SoD checks) and that the **rotated-out key is denied at `require_scope` immediately on revoke — no window**. **Test:** old key denied the instant it is revoked; new key rebinds and reads the same durable mind (keyed by `resident_id`).

---

## §10 — Phased Build Plan (R3 — Sentinel-gated)

> **Sequencing note (R3, Sentinel-gated):** the review-loop task/trust seam already shipped (work queues v1, stop oracle, trusted-verdict provenance migration 053, settle-path verdict, admin trusted-reviewers registry, scoped service keys). **Reviewer phases R0/R1/R2 are CLEAR to build** (Sentinel APPROVED). **Phase R0 (server) gates everything**, and its implementer-security deliverables (F1/F4/F6/F7/F9) get a **dedicated Sentinel R0 re-review** before the **implementer phases (R3+) may build**. **F9 HARD GATE:** no implementer resident may point at an `implement_until_done` queue until R0's re-derive + self-review rule replaces the current claimed-outcome self-close (`work_queues.py` ~1274).

### Phase R0 — Server: resident-memory primitive + implementer-security gate (Atlas) — GATES ALL, then Sentinel R0 re-review
- Migration 057 (`residents`, `resident_memory_entries`, **+ additive `work_queue_items` columns** `implementer_service_key_id`/`implementer_user_id`/`auto_close_review_kind`/`closed_by_*` per F1; strictly additive, `down_revision='056'`, inline CHECKs, direct SQLite up/down test).
- `routes/residents.py`: register/inspect/status/**rotate-key**/**quarantine** + memory write/hydrate/compact + retention sweeper, all enforcing the §3.6.3 isolation predicate (org + resident-private).
- Scopes `resident_memory:read/write` added to the catalog (16→18).
- **F1 (headline):** record `implementer_*` at implement claim/settle; implement-side close **re-derives `review_state` over `verdict_trusted=true`** (replaces the F9 claimed-outcome self-close); reject auto-close on identity match; **fail-closed on unknown implementer identity**. + the **F1 full negative-test matrix** (§9 hook 7 i–vi).
- **F4:** register-trusted-reviewer / resident-register mutual-exclusion (one key ≠ both roles, both directions).
- **F6:** server hard cap on un-compacted memory entries (`max_uncompacted_entries`) + per-wake write cap.
- **F7:** `rotate-key` rebind + rotated-out-key denied at `require_scope` immediately on revoke (no window).
- **Condition (a):** server-set `auto_close_review_kind` marker ("resident-reviewed, NOT human-reviewed") queryable + surfaced.
- **Tests:** §9 hooks 7, 9, 11, 13, 14, 15, 16 (incl. forged-`resident_id`/`org_id`-ignored + sweeper isolation); migration up/down.
- **→ Sentinel R0 re-review of F1/F4/F6/F7/F9 before R3 opens.**

### Phase R1 — Reviewer-runner skeleton (client, the wedge)
- A long-running client process (proposed surface: `sfs resident run --queue <id> --org-profile <name>`, or a standalone `sessionfs-resident` entrypoint — Atlas/Prism to confirm shape).
- Heartbeat driver: `run_work_queue_step` → for each `post_review` directive, call the LLM adapter, settle via `complete_work_queue_step(verdict_content=…)` (lease-fenced). **No mind yet** — proves loop + settle-path + trust against a registered service key.
- Org-scoped local warm-cache store via `profiles.py` isolation (service key `0600`).
- **Tests:** registered service key → strict `VERIFIED-CLEAN` auto-closes; revoked registry row → no auto-close; LLM error → fail-closed; cross-org service key denied.

### Phase R2 — Living context via the memory primitive (client)
- `hydrate_living_context` (§3.3): compiled context + persona-filtered KB + review-playbook wiki + `GET .../memory/hydrate`.
- Writeback: `add_knowledge` (de-duped, `persona='codex-reviewer'`, `author_class=agent`) + `update_wiki_page`; rolling `POST .../memory` + periodic `POST .../memory/compact`.
- Bounded **warm digest** assembly (token-capped at `mind_token_budget`; expand-on-demand).
- **Tests:** hydration org+resident-scoped (no cross read); warm digest under cap; compact supersedes correctly; writeback honors no-op discipline.

### Phase R3 — Implementer resident (client + sandbox, Forge) — GATED on the Sentinel R0 re-review (F1/F4/F6/F7/F9)
- **Precondition:** R0's F1 re-derive+self-review rule has SHIPPED and passed Sentinel R0 re-review; the F9 gate is satisfied (no implementer resident before that).
- Worktree isolation + sandboxed execution (§3.7.1); `implement`/`fix_findings` directive handling; commit-to-resident-branch + open-for-review (diff-**ref** only, C7/C8); **propose-only** (no push/merge granted — Forge profile, F8).
- Item can only reach `waiting_review`; close is the independent re-derived-reviewer path (Phase R0), self-review-prohibited + fail-closed on unknown identity; auto-close stamps the condition-(a) marker.
- **Tests:** §9 hooks 8 + 10 + 12; implementer cannot push/merge/self-close; the UNIFORM gate + two conditions hold for all change classes (marker stamped; high-risk merge checklist referenced); code custody host-local (server never receives repo contents, C7); co-located reviewer reads diff locally (C8, hook 13).

### Phase R4 — Crash-recovery + compaction + cost bounding (both residents)
- Re-hydrate on restart (heartbeat re-emit + memory hydrate); `--cold` rebuild; per-wake + daily LLM-spend ceilings; fail-closed budget parking.
- **Tests:** kill+restart mid-work → re-emit, no double-post, mind re-hydrates; long-uptime soak → memory footprint bounded (compact + sweeper); budget exhaustion → parks.

### Phase R5 (v1.1) — Operability + governance docs + tier enforcement
- Operator setup docs (Scribe): service-key mint, `trusted_reviewers` registration, resident register, LLM-adapter config, org-profile isolation, sandbox setup, the kill switch + quarantine.
- **Condition (b): the mandatory non-rubber-stamp high-risk merge checklist (Scribe doc + Shield governance gate).** Ships alongside the implementer surface — the human merge is load-bearing.
- CLI resident-health readout (last wake, last verdict, budget, **`auto_close_review_kind` marker on items**). §7.1 free/paid enforcement.
- Optional inspect-only memory MCP tools.

### Deferred (v2+) — see §7
Fleet management + health dashboard (Prism, paid); multi-queue-per-resident (paid); event-driven wake; cross-resident mind-sharing; owner-only audited **reviewer**-memory export (§8.3 — implementer-mind visibility is already v1 per F5); managed hosted runtime (only via customer-key escrow).

---

## §11 — Handoff Tickets (on approval)

- **CEO** — all R1/R2/R3 forks resolved and folded (§8.1, §8.2; the high-risk gate is uniform + two binding conditions, §3.7.2). Nothing outstanding for v1; the only deferred item is owner-only audited **reviewer**-mind export (§8.3, paid/v2).
- **Sentinel** — R2.1 APPROVED-WITH-CONDITIONS; reviewer phases cleared. **Remaining: the R0 re-review** of the implementer-security deliverables against §9 hooks 7 (F1 + full negative matrix), 13 (F2/C8), 14 (F4), 15 (F6), 16 (F7), plus 11 (F5 poisoning/quarantine) — sign-off **gates R3+**. Rulings still wanted on hooks 10 (uniform gate + conditions sufficiency) and 12 (same-operator collusion).
- **Atlas** — **Phase R0 (GATES ALL):** migration 057 (`residents` + `resident_memory_entries` + additive `work_queue_items` F1 columns), `routes/residents.py` (register/status/**rotate-key**/**quarantine** + memory write·hydrate·compact + sweeper) with the §3.6.3 isolation predicate + `resident_memory:read/write` scopes; the **F1 re-derive + self-review + fail-closed** close rule (replacing the F9 self-close hole); **F4** registration mutual-exclusion; **F6** memory caps; **F7** rotation + immediate revoke-deny; the **condition-(a) marker**. Plus the runner CLI/process entrypoint shape + `profiles.py` org-scoped warm-cache store.
- **Forge** — the implementer **sandbox/worktree runner** (§3.7.1, F8): hardened container profile (read-only/absent remotes, no protected-branch creds, no merge tokens), least-privilege secret grant, worktree lifecycle; uphold C7/C8 (code custody host-local; co-located reviewer reads diff locally; server never receives repo contents). **Note (F8): the push/merge boundary is this profile, not a server guarantee.**
- **Scribe** — operator setup + safety docs (Phase R5) **AND the condition-(b) mandatory non-rubber-stamp high-risk merge checklist** (DB migrations/auth/crypto/CI/deps); document the `auto_close_review_kind` "resident-reviewed, NOT human-reviewed" marker for mergers.
- **Shield** — **governance owner of the condition-(b) merge gate** (require the checklist for high-risk-class merges of resident-auto-closed changes); the implementer-mind owner-visibility audit trail (F5); revisit reviewer-mind export governance (§8.3, v2).
- **Prism** — (paid, v2) resident **fleet management + health dashboard** (§7.1) surfacing the marker; optional inspect-only memory view.
- **Ledger** — confirm the §7.1 free/paid line + the work-queue §13 tier-line correction (free = one service-key-driven resident/one queue; fleet/multi-queue/observability/governance paid).

---

## §12 — Recommended Primary Design (summary)

Ship **client-side, operator-hosted, single-org residents** — a **reviewer** and (R2) an **implementer** — each a long-running process that **hydrates a warm, durable "mind"** from SessionFS (compiled project context + its own persona-filtered KB + a review-playbook wiki + a first-class **resident-memory primitive**), drives the **already-shipped work-queue heartbeat** for all task state, calls the **operator's own external LLM client-side** (BYO OpenAI-compatible/Codex, operator's own key, never seen by the server), writes durable learnings **back**, and **compacts** so it survives crashes and stays cost-bounded. The reviewer posts verdicts **only** through the **settle-path** so the server stamps `verdict_trusted` from a **registered `trusted_reviewers` service-key identity**; the implementer is **worktree-isolated + sandboxed, propose-only** — it can never push, merge, or self-close, and its work reaches a closed state **only** when the implement-side close **re-derives `review_state` over `verdict_trusted=true` comments from an INDEPENDENT identity** (R3/F1: `work_queue_items` now records the implementer identity; auto-close is rejected on identity match and **fails closed on unknown identity**; F4 forbids one key holding both roles) + a **human merge gate**. The **queue stays the durable task LEDGER and the server stays the sole closure authority**; residents are the durable MIND that makes work better/cheaper but can **never** self-certify. **Isolation is a hard boundary:** single-org AND resident-private, server-enforced on the memory primitive (C5); **code custody stays host-local (C7) and the closing reviewer reads the diff locally, co-located (C8) — code never flows through the server.** **Sentinel APPROVED-WITH-CONDITIONS (R3):** reviewer phases R0/R1/R2 clear to build; the implementer phases (R3+) are gated on a **Sentinel R0 re-review** of F1/F4/F6/F7/F9. The high-risk gate is **fully uniform + two binding conditions** — (a) a server-set human-visible **"resident-reviewed, NOT human-reviewed"** marker on resident-auto-closed items, (b) a mandatory non-rubber-stamp high-risk merge checklist (Scribe doc + Shield gate) — because the **human merge is load-bearing**. v1 adds migration **057** (`residents` + `resident_memory_entries` + F1 `work_queue_items` columns) + `routes/residents.py`, built on the v0.12.0/v0.12.1 autonomous-reviewer backend + scoped service keys. **DECISION 3:** the operator-hosted runner (review AND implement) is **free**; fleet/observability/multi-queue/hosted-memory-beyond-floor/governance are **paid** (§7.1).
