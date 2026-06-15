# Multi-Repo Projects — Product Companion

**Author:** Compass (product lead)
**Status:** Design — feeds Atlas's `multi-repo-projects.md` Open Decisions. Sentinel S1 amendment folded in (verified-vs-unverified linking UX).
**Date:** 2026-06-15

## 1. Who Has This Problem & How Acute

### Segment by pain level

| Segment | Example | Pain | Adoption blocker? |
|---------|---------|------|-------------------|
| **Frontend + backend pairs** | React dashboard + FastAPI server | HIGH — same personas ("atlas", "prism") duplicated across two projects, same KB entries re-entered, tickets filed in one project invisible to the other | **Yes.** These teams are SessionFS's core ICP. They hit this on day 2. |
| **Multi-service teams** | 4–12 microservices, each a repo | HIGH — teams fragment identity/coordination across N projects, or abandon the product for a single "coordinator" repo and lose per-service granularity | **Yes, at scale.** The 4+ service case makes the product feel broken. |
| **Infra + app** | Terraform/Helm repo + application repo | MEDIUM — infra changes (deploys, config) live in a separate project from the app tickets they enable. Cross-linking is manual. | **Friction, not blocker.** These teams work around it with ticket cross-references. |
| **Monorepo-adjacent** | One monorepo + 1–2 side repos (shared libs, docs site) | LOW — the monorepo carries most of the weight; side repos are read-only or docs-only | **Nice to have.** Not the wedge. |
| **Solo developer** | Single repo, personal project | NONE | Not applicable. |

**Verdict:** This is a **real adoption blocker** for the core ICP (multi-service teams, frontend+backend pairs). It undermines the "Memory Layer For AI Agents" positioning because memory/identity/coordination are scoped to a single repo, so teams with N repos get N fragmented memory layers — the opposite of the promise.

### When do users hit this?
- **Day 1:** First repo works beautifully. `sfs init`, auto-create project, personas, KB — everything clicks.
- **Day 2:** Second repo. User runs `sfs init` in the backend repo. A new project is created. They now have two separate "atlas" personas, two KBs, two ticket boards. Confusion sets in.
- **Week 2:** Third repo. The team has stopped using personas/tickets because managing them across projects is more work than not having them. They use SessionFS as a session logger only — the coordination wedge is lost.

## 2. Linking UX & Mental Model

### Recommendation: Explicit link model

**The user explicitly links additional repos into an existing project.** We never auto-guess that two repos belong to the same project.

**Why not auto-guess:**
1. **Same repo name, different products.** `org/api` could be the backend for Product A or Product B. The git remote alone carries no product-boundary signal.
2. **Cost of false positive >> cost of false negative.** Incorrectly merging two unrelated repos' KBs, personas, and tickets is data-corrupting. Missing a link opportunity is a minor UX friction the user can fix with one command.
3. **Org boundaries.** Repos in different GitHub orgs may not belong to the same SessionFS org, or may require cross-org project semantics we haven't designed yet.
4. **User intent is the only reliable signal.** "These repos are one product" is a human judgment. We ask once, explicitly.

### How the user links repos

**CLI (primary path — developers are in the terminal):**

```bash
# In any directory, link a remote into an existing project:
sfs project link-repo <remote> [--project-id <id>]
```

This:
1. Normalizes the provided git remote URL.
2. Checks whether the repo is already linked to another project (one repo ∈ exactly one project). If so, the CLI reports whether the existing link is verified or unverified — a verified owner can displace an unverified link (see below).
3. Validates admin standing on the target project (owner or org-admin).
4. **Verifies repo ownership:** If the SessionFS GitHub App is installed on the repo's owner, the server proves control via the installation — the link is recorded as **verified** (`verified=true`). For non-GitHub remotes, self-hosted instances, or repos where the App isn't installed, the link is recorded as **owner-attested** (`verified=false`) — the admin asserts they control the repo, but the server cannot prove it. An owner-attested link can be displaced by a verified claim from the repo's real owner later.
5. Outputs: `Linked github.com/org/backend-repo → "Platform API" (proj_abc123). Verified via GitHub App.` (or `Owner-attested — install the SessionFS GitHub App for verified linking.`)

**GitHub App installation for verified linking:** To link a GitHub repo with full verification, the SessionFS GitHub App must be installed on the repo's owner (user or organization). Without the App, linking still works but is recorded as owner-attested. The dashboard Repos tab shows a "Verify" prompt next to owner-attested repos with a link to install the App. Sentinel F1 (security review) requires this distinction to prevent repo-name squatting.

**Dashboard (secondary path — project admin view):**

A "Repos" tab on the project detail page shows the linked repo set with each repo's verification status (verified ✓ / owner-attested ⚠). An "Add repo" button opens a modal where the user pastes a git remote URL (`github.com/org/repo-name`). Validation runs server-side. If the SessionFS GitHub App is installed on the repo's owner, the link auto-verifies; otherwise the dashboard prompts to install the App for verified linking. This is the admin/org-owner path — useful when a tech lead is setting up the project structure before the team onboards.

**`sfs project init` inside an already-linked repo:**
- If the current directory's remote is already linked to a project, `sfs project init` prints: `This repo is already part of project "Platform API" (proj_abc123).` and exits 0. It does NOT create a second project.
- If the remote is unlinked, `sfs project init` behaves exactly as today: creates a new project, auto-names it from the repo name. The user can later link more repos into it.

### Project naming when it's no longer "the repo"

**Default:** The project keeps the name of the **first repo linked** (the primary). The user can rename it at any time — `sfs project set --name "Platform API"`.

**Recommendation:** When a user links a second repo, the CLI prompts: `Project "backend" now spans 2 repos. Rename it? [Platform API]:` with the current name as default. This is a one-time nudge, not a forced rename — many projects already have good names from their primary repo.

### Repo display: primary vs linked

- The **first repo** in a project is the **primary**. It has no special privileges beyond being the default name source. It can be changed later (Atlas follow-up — not v1).
- All linked repos are equal for KB, persona, ticket, and rules access. There is no "primary repo can write KB, linked repos are read-only" — that defeats the purpose.
- The dashboard Repos tab shows the set as a flat list with the primary marked with a subtle star/home indicator, and each repo shows its verification status: **verified ✓** (GitHub App confirmed ownership) or **owner-attested ⚠** (admin asserted ownership; installable GitHub App for verification). No hierarchy.
- **Displacement:** If a verified owner later links a repo that was already linked as owner-attested (by a different project), the unverified link is displaced — the repo moves to the verified owner's project. The displaced project's admin receives a notification. This prevents repo-name squatting (Sentinel F1).

### Why not "projects contain many repos" from the start?

The single-repo default is correct for the **individual tier** (free forever, solo devs, one repo = one project = one person). Multi-repo is a team concept. The complexity should only surface when the user crosses into team coordination territory. This keeps the v1 simplicity for the free tier and only expands the model when the need is real.

## 3. Merge UX — Consolidating Existing Split Projects

This is the migration path for users who already have duplicated personas/KBs across projects that should be one.

### The flow: "Fold project B into project A"

```
sfs project merge <source-project-id> --into <target-project-id>
```

**Step 1: Dry-run (default, no `--execute` flag)**

```
$ sfs project merge proj_backend --into proj_frontend

Dry-run: folding "backend" (proj_backend) into "frontend" (proj_frontend)

Personas (3 to reassign, 1 collision):
  ✓ atlas          — unique, will be reassigned
  ✓ scribe         — unique, will be reassigned
  ⚠ prism          — COLLISION: both projects have "prism"
                     → keep target's "prism", rename source's to "prism-x5k2m9a1"
                     (legal ASCII slug; "(from backend)" saved as display note)

Tickets (5 to reassign):
  ✓ tk_001 "Add auth endpoint"     — reassigned to target
  ✓ tk_002 "Fix CORS"              — reassigned to target
  ... (3 more)

Knowledge entries (47 to reassign):
  All unique — will be reassigned

Rules:
  ⚠ COLLISION: both projects have compiled rules
    → keep target's rules, archive source's as "rules (from backend)" snapshot

Wiki pages (2 to merge):
  ✓ architecture.md  — unique

Summary: 57 items to reassign, 2 collisions (auto-resolved with defaults).
Run with --execute to apply.
```

**Step 2: Execute with optional overrides**

```bash
sfs project merge proj_backend --into proj_frontend --execute
# Or with per-collision control:
sfs project merge proj_backend --into proj_frontend --execute --interactive
```

`--interactive` prompts per collision:
```
Collision: persona "prism" exists in both projects.
[1] Keep target's "prism" (frontend) + rename source's to "prism-x5k2m9a1"
    (legal ASCII slug; display note records the human-readable origin)
[2] Overwrite target with source's "prism"
[3] Keep both separate (skip merge for this persona)
[4] Field-merge (take target's role/description, source's scopes)
Choose [1-4] (default: 1):
```

### Collision policy (the key decision Atlas defers to us)

**Recommendation: Keep target, rename source — with `--interactive` override.**

| Entity | Default collision behavior | Rationale |
|--------|---------------------------|-----------|
| **Personas** | Keep target's name. Rename source's to `{name}-{src8}` (e.g. `prism-a1b2c3d4`), a legal ASCII slug per the `^[A-Za-z0-9_-]{1,50}$` regex. The human-readable explanation ("Renamed from 'prism' in source project abc12345") is stored in the audit record's `persona_renames` JSON, NOT in the `name` field. | The target project is the "survivor" — the user explicitly chose A as the destination. A's personas keep their canonical names. B's personas are preserved (no data loss) but disambiguated with a machine-safe identifier. The user can manually reconcile afterward (perhaps rename to a friendlier but still-legal name via `sfs persona update`). |
| **Rules** | Keep target's compiled rules. Archive source's compiled rules as a snapshot wiki page `_merged_rules_{source_id[:8]}`. | Rules are the project's governance surface. Merging two rule sets automatically is unpredictable — different LLM tools, different team conventions. Preserving the source as a reference page lets the user merge manually with full context. |
| **Tickets** | All tickets are reassigned in place — `project_id` is updated from source to target. No new IDs, no copy semantics. Open tickets stay open. Closed tickets stay closed. | Ticket IDs are globally unique, so no collisions are possible. `depends_on` references remain valid because all tickets end up under the same project (dependencies are validated same-project at creation). This is the simplest correct model — no `merged_from` provenance needed. |
| **Knowledge entries** | All entries are reassigned. Entries that are exact duplicates (same `entry_type` + normalized content) are silently skipped to avoid literal duplicates. No LLM semantic dedup (house rule: no server-side LLM keys). | KB entries are the memory record. We err on the side of preserving data, but literal duplicates serve no one. A future client-side `sfs project dedup` could do semantic dedup. |
| **Wiki pages** | Wiki pages with identical slugs get a `{slug}-{src8}` suffix (e.g. `architecture-a1b2c3d4`). Matches the binding doc's `_apply_slug_renames` — no human-readable characters in the slug. | Same reasoning as KB entries. |

**Why "keep target" and not "keep newest" or "field-merge"?**
- **Keep target** is the simplest mental model: the user declared A as the winner. Predictable, no heuristics.
- **Keep newest** sounds fair but breaks when the source project was more actively maintained (its personas are newer — now the target loses its own history).
- **Field-merge** (e.g., persona A's role + persona B's scopes) is seductive but dangerous: two "atlas" personas may have different scopes for good reason (one backend, one frontend). Merging them creates a hybrid persona that has access to everything — a privilege escalation vector. Not safe as a default.

### Post-merge: what happens to the source project?

The source project is **soft-deleted** (not hard-deleted). Its sessions remain individually accessible but the project no longer appears in the project list or dashboard. This preserves the session history for audit while removing the duplicate project from the active surface.

A `merged_into_project_id` field on the source project records where it went, for audit trail and potential undo (Atlas follow-up — not v1, but the field should exist from day one).

## 4. What "Project" Means to the User Afterward

The project remains the **unit of shared memory, identity, coordination, and governance** — it just now spans multiple repos.

| Concept | Before | After |
|---------|--------|-------|
| **Scope** | One git repo | One or more git repos |
| **Memory (KB)** | Scoped to one repo's sessions | Scoped to all linked repos' sessions |
| **Identity (Personas)** | Scoped to one repo | Shared across all linked repos |
| **Coordination (Tickets)** | Scoped to one repo | Shared across all linked repos |
| **Governance (Rules)** | Scoped to one repo | Shared across all linked repos. Rules compilers (`sfs rules compile`) run from any linked repo and produce the same canonical rules — the project's rule set is singular. |
| **Sessions** | Each session belongs to one repo | Unchanged. Sessions remain per-repo. Their `project_id` already links them to the project. |
| **Dashboard** | Project page shows one repo | Project page gains a Repos tab listing the linked set. The project name is editable and no longer tied to any single repo's name. |

### Repo identity within a project

Each repo retains its own git remote identity. Sessions, captures, and the daemon still operate per-repo. The project is the **aggregation layer** — it doesn't replace the repo as the capture unit, it groups repos for the shared surfaces (KB, personas, tickets, rules).

This matters because: a user running `sfs resume` in repo A should not see sessions from repo B by default. The session list is still per-repo. But `sfs project ask "what's our auth pattern?"` searches the KB across ALL linked repos — that's the value.

### Dashboard follow-up (Prism — note, don't design pixels)

- New **Repos tab** on ProjectDetail: lists linked repos, shows primary, "Add repo" button.
- Project name is editable in the header (inline rename, not a separate settings page).
- The project list (`/projects`) shows repo count as a badge (`3 repos`) next to projects with multiple repos.
- Session provenance in the conversation view already shows the repo — no change needed.

## 5. Tier & Monetization Fit

### RESOLVED — FREE for all tiers (CEO decision, 2026-06-15)

**Multi-repo projects are NOT monetized.** Repo-linking AND project-merge are available to every tier, including Free. This supersedes the original Team-tier recommendation that appeared in an earlier draft of this section.

Rationale:
1. **It's a data-model correction, not a premium feature.** A project spanning multiple repos is the *correct* model; the single-repo limitation was an implementation constraint, not a deliberate value boundary. Charging to remove a limitation we imposed punishes users who organically have multi-repo products.
2. **Consistent with "free individual tier forever."** Many solo and small-team developers legitimately have a frontend + backend repo for one product. They should not hit a paywall to make SessionFS model their reality correctly.
3. **No upgrade-trigger, no error path.** Do NOT add a `multi_repo_projects` feature gate, a `check_feature()` call, or a `multi_repo_requires_team` error. The link/unlink/merge endpoints enforce ownership/org authz only.

### Implementation note for Atlas

No tier plumbing. The link-repo, unlink-repo, and merge endpoints check ownership + org boundary (§7.3) and nothing else. The dry-run default on merge is a safety affordance for everyone, not a paywall preview.

## 6. Rollout & Communications

### Target audience for rollout

**Primary:** Anyone — solo or team — with 2+ projects that are really one product (frontend + backend, infra + app, microservices). These are the users who feel the fragmentation pain daily, across every tier.

**Secondary:** New users onboarding a multi-repo product, who should discover linking early so they never split their memory/identity in the first place.

### Communication sequence

1. **Changelog entry** (release day): "Projects can now span multiple repos. Link your frontend, backend, and infra repos into one project — shared memory, shared personas, shared tickets. Free for everyone."
2. **In-dashboard banner** (release day, dismissible): "New: multi-repo projects. Link your repos →" linking to the Repos tab. Shown to users with 2+ projects on the same org.
3. **CLI hint on `sfs project show`** (release day): If the user has other projects in the same org, add a line: `Tip: link repos into one project with 'sfs project link-repo'.`
4. **Email to existing users** (release week): "You have N projects across M repos. Multi-repo projects let you consolidate them into shared workspaces. Here's how."
5. **Docs page** (release day): `docs/multi-repo-projects.md` — user-facing guide with examples.

### No forced migration

Existing split projects continue to work exactly as today. Users are not forced to merge. The merge tool is offered, not imposed. This is critical: some teams may have legitimate reasons for separate projects (different products, different teams, different compliance boundaries). We respect that.

### Risk of confusion

- **"Which project do I link into which?"** — Users with 3+ split projects may be unsure about the "correct" merge order. Mitigation: the `--dry-run` output shows the full merge plan before execution, so they can experiment safely.

## 7. Risks & Edge Cases (Product Lens)

### 7.1 Repo moving between projects

**Scenario:** A user removes repo R from project A and links it to project B.

**Risk:** Project A's KB contains entries sourced from sessions on repo R. After the move, those entries reference a repo that's no longer in the project. Tickets may reference sessions from repo R.

**Recommendation:** Repo unlink is allowed, but:
- Existing KB entries and tickets are NOT deleted or moved. They stay in project A with their session provenance intact. The data was created in project A's context and belongs there.
- New sessions on repo R flow into project B.
- If the user wants to move the KB entries too, that's a separate operation (`sfs project merge` with a scope filter — Atlas follow-up, not v1).
- The dashboard Repos tab shows a "formerly linked" section if there are entries referencing a removed repo.

### 7.2 A repo that legitimately belongs to two products

**Scenario:** A shared library repo (`org/shared-utils`) is used by both Product A and Product B. Both teams want it in their project for KB/ticket context.

**Recommendation:** **One repo ∈ exactly one project.** This is a hard constraint. Rationale:
- If repo R is in both projects, sessions from R would feed two separate KBs — which KB is authoritative?
- Tickets referencing R's sessions would have ambiguous project scope.
- The data model (Atlas's join table) enforces this with a unique constraint on `git_remote_normalized` (and a `provider`+`provider_repo_id` partial unique index for rename survival).

**Workaround for the user:** The shared library team should have its own project. Product A and Product B can cross-reference tickets. Future: project-to-project links / "shared KB sections" — but this is a v2 feature, not v1.

### 7.3 Org boundaries

**Scenario:** A user tries to link `github.com/org-a/frontend` into a project owned by `org-b`.

**Recommendation:** **All repos in a linked project must belong to the same SessionFS org as the project.** Cross-org repo linking is rejected with a clear error: `Repo "org-a/frontend" belongs to a different organization. Projects cannot span organizations.`

**Exception — verified reclaim:** When a verified owner reclaims their repo via displacement (binding doc §6.2), the FINAL state places the repo in the verified owner's project/org and removes it from the unverified (possibly cross-org) holder. Normal (non-reclaim) cross-org linking remains rejected. The displacement audit trail records the cross-org transition for forensics.

**Rationale:**
- Orgs are the billing and access-control boundary. Mixing orgs within one project creates ambiguous billing (who pays for the KB storage?) and access-control confusion (an org-b member sees org-a's repo data?).
- The `org_id` FK on the project already scopes it. Each linked repo's `git_remote_normalized` must resolve to the same SessionFS org.
- Cross-org collaboration is a future feature (organization federation / project sharing) — not v1.

### 7.4 What breaks in user expectations

| Expectation | Reality | Mitigation |
|-------------|---------|------------|
| "Linking repos = merging their sessions" | Sessions stay per-repo. The KB aggregates across repos, but the session list in `sfs sessions` is still repo-scoped. | Document clearly: "Sessions are per-repo. The KB, personas, tickets, and rules are shared." |
| "I can link any repo" | Org-boundary check rejects cross-org repos (exception: verified reclaim can displace an unverified cross-org squatter — the repo ends up in the verified owner's project). Additionally, link requires project admin standing (owner or org-admin), and verification depends on GitHub App installation. Non-GitHub/self-hosted repos are owner-attested. | Clear error messages for each case. Dashboard prompts App install for verified linking. |
| "Merge is instant and perfect" | Collisions require manual review. The dry-run shows everything upfront. | Default `--dry-run` with no side effects. |
| "My free-tier project should support multi-repo" | It does — multi-repo is free for all tiers. | No gating; works everywhere. |
| "I can delete the source project after merge" | It's auto-soft-deleted. Sessions remain accessible. | The merge output confirms this explicitly. |
| "Renaming a repo on GitHub breaks the link" | The git remote is the link key. Renaming a repo changes its normalized remote. | **Resolved in v1.** The `project_repos` schema includes `provider` + `provider_repo_id` (stable across renames). A rename-survival handler (follow-up: sync-repo-names / GitHub webhook) matches on `(provider, provider_repo_id)` and updates `git_remote_normalized` in place. Until that handler ships, users can manually update with `sfs project unlink-repo` + `sfs project link-repo`. |

### 7.5 Atlas Open Decisions this doc resolves

This section is the handoff to Atlas's `multi-repo-projects.md`:

1. **Merge collision policy for personas:** Keep target's name. Rename source to `{name}-{src8}` (legal ASCII slug ≤50 chars; e.g. `prism-a1b2c3d4`). Human-readable note lives in audit record. Field-merge is explicitly rejected as a default (privilege escalation risk). User can override with `--interactive`.
2. **Merge collision policy for rules:** Keep target. Archive source as a wiki page snapshot.
3. **Repo exclusivity:** One repo ∈ exactly one project. Unique constraint on the join table.
4. **Org boundary:** All repos in a project must be in the same SessionFS org.
5. **Tier gate:** NONE — free for all tiers (CEO decision). No `multi_repo_projects` feature, no `check_feature`, no upgrade-prompt error. Ownership/org authz only.
6. **Project naming:** No change to the data model. The existing `name` field on Project is already editable. Default stays as the primary (first-linked) repo name.

---

## Summary of Compass Recommendations

| Decision | Recommendation |
|----------|---------------|
| **Linking model** | Explicit link — user runs `sfs project link-repo <remote>`. Never auto-guess. |
| **Merge collision default** | Keep target, rename source to `{name}-{src8}` (legal ASCII slug ≤50 chars). No field-merge. |
| **Merge dry-run** | Mandatory default. `--execute` required to mutate. `--interactive` for per-collision control. |
| **Repo exclusivity** | One repo ∈ exactly one project. Hard constraint. |
| **Org boundary** | All repos must be in the same SessionFS org as the project. |
| **Tier** | FREE for all tiers (CEO decision). No tier gate. |
| **Source project post-merge** | Soft-deleted with `merged_into_project_id` provenance. |
| **Project name** | Existing editable `name` field. Nudge rename on second repo link. |
