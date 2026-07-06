# Resident operator guide — setup, isolation, and the kill switch

A **resident** is a long-running, operator-hosted process that drives a
SessionFS work queue by calling **your own** LLM. Two kinds:

- **Reviewer resident** (`review_until_clean`): reads tickets, posts trusted
  verdicts. Its verdict can auto-close a queue item.
- **Implementer resident** (`implement_until_done`): writes code in an isolated
  worktree, commits to a `resident/<queue>/<ticket>` branch, and posts a
  diff-ref for an **independent** reviewer to close. It can only PROPOSE — see
  the sandbox runbook (`resident-sandbox.md`) and the merge checklist
  (`high-risk-merge-checklist.md`).

## Free vs paid

The **local runner is FREE** — review AND implement, run on your own host with
your own LLM key. SessionFS never holds an LLM key and never brokers your git.

**Paid/managed** (v2, not the local runner): fleet management, health
dashboards, multi-queue-per-resident, hosted memory beyond the free retention
floor (compacted memory kept ≥ 30 days on free; longer on paid), and
governance/SSO ties. Running one resident locally needs none of these.

## Step 1 — Mint a scoped service key (per role)

Each resident authenticates as its own **scoped service key** (never a human
key). Mint one per role so identities are separable:

```bash
# --scope is REPEATABLE (one per capability); `sfs admin service-keys scopes`
# lists the full vocabulary.
sfs admin service-keys create --org <org_id> --name codex-reviewer \
  --scope work_queues:read --scope work_queues:write \
  --scope tickets:read --scope tickets:write \
  --scope knowledge:read --scope knowledge:write \
  --scope resident_memory:read --scope resident_memory:write \
  --scope sessions:read --scope agent_runs:write
# ...and a SEPARATE key for the implementer (persona atlas), same scopes.
sfs admin service-keys create --org <org_id> --name atlas-implementer \
  --scope work_queues:read --scope work_queues:write \
  --scope tickets:read --scope tickets:write \
  --scope knowledge:read --scope knowledge:write \
  --scope resident_memory:read --scope resident_memory:write \
  --scope sessions:read --scope agent_runs:write
```

The raw key is shown **once** — capture it for the resident's org profile
(`sfs auth login --profile <name>` stores it locally). See `resident-sandbox.md`
for the full implementer scope rationale (it needs `tickets:read` to hydrate).

## Step 2 — Register the resident (server memory primitive)

```bash
# Reviewer + implementer are SEPARATE residents (separate keys, personas, minds):
curl -X POST "$API/api/v1/orgs/<org_id>/residents" -H "Authorization: Bearer <admin key>" -H "Content-Type: application/json" \
  -d '{"kind":"reviewer","persona_name":"codex-reviewer",
       "service_key_id":"<reviewer key id>","project_id":"proj_..."}'
curl -X POST "$API/api/v1/orgs/<org_id>/residents" -H "Authorization: Bearer <admin key>" -H "Content-Type: application/json" \
  -d '{"kind":"implementer","persona_name":"atlas",
       "service_key_id":"<implementer key id>","project_id":"proj_..."}'
```

Each resident gets an org-scoped, resident-**private** memory. Note the returned
`res_...` ids for the config (`resident_id`).

## Step 3 — Make the reviewer's verdicts count (trusted_reviewers)

```bash
sfs admin trusted-reviewers add --org <org_id> \
  --service-key-id <reviewer key id> --persona codex-reviewer
```

**Separation of duties (F4, server-enforced):** you CANNOT register the
implementer's key as a trusted reviewer, and you cannot register a trusted
reviewer's key as an implementer, on the same project/org. This guarantees the
self-review prohibition at bind time — a resident can never approve its own code.

## Step 4 — Write the resident config

`~/.sessionfs/residents/<name>.toml`:

```toml
[resident]
queue_id = "wq_..."
project  = "proj_..."          # service-key residents use a project id, not a git remote
org_profile = "reviewer-org"   # named auth profile holding the SERVICE key (Step 1)
resident_id = "res_..."        # from Step 2
org_id = "org_..."
persona = "codex-reviewer"     # implementer: "atlas" (a NON-reviewer persona)
mode = "review"                # or "implement" (+ worktree_path, base_branch)
poll_interval_seconds = 30
mind_token_budget = 8000
# Cost bounding (both optional; a daily budget REQUIRES a per-wake cap):
daily_token_budget = 500000
per_wake_token_budget = 60000

[llm]                          # top-level section — YOUR OpenAI-compatible endpoint (never sent to SessionFS)
base_url = "https://api.openai.com/v1"
model = "gpt-5.1"
api_key_is_env = true          # resolve from RESIDENT_LLM_API_KEY (don't inline the key)
max_tokens = 4096
```

**Credential boundary:** the LLM key stays local (env or config); the SessionFS
service key comes from `org_profile` and is used only for the API. They never
cross.

## Step 5 — Run

```bash
export RESIDENT_LLM_API_KEY=sk-...      # your LLM key, local only
sfs resident run --config reviewer
# The implementer runs inside the hardened sandbox — see resident-sandbox.md.
```

Check health any time — a read-only summary of the config + today's LLM
budget spend (it doesn't contact the server or start the loop):

```bash
sfs resident health --config reviewer
```

## Kill switch + quarantine

Three levels of control, escalating:

- **Pause (reversible, queue-only):** set the queue status to `paused` — via the
  `set_work_queue_status` MCP tool, or `POST
  /api/v1/projects/<project>/work-queues/<queue>/status` with
  `{"status":"paused"}`. The resident keeps polling but does no work.
- **Retire (hard stop):** an org owner/admin retires the resident via the
  residents API (`.../residents/<id>/status` → retired), which **revokes its
  service key server-side** — it can no longer authenticate for anything, with
  no window (F7). In-flight items are re-pointed on key rotation.
- **Quarantine a poisoned mind (owner/admin only):** if an implementer's durable
  memory looks poisoned (a `--cold` restart clears only the warm cache;
  durable-memory poisoning survives re-hydration), quarantine it via the
  residents memory API so it stops feeding the LLM, then investigate.

The **human merge gate** (see `high-risk-merge-checklist.md`) is the final,
load-bearing backstop — no resident-authored code reaches a protected branch
without a named human merging it.
