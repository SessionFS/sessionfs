# Binding Design — Multi-Repo Projects

**Status:** Draft — awaiting CEO + Codex review
**Author:** Atlas (backend/data-model)
**Date:** 2026-06-15
**Companion:** `docs/design/multi-repo-projects-product.md` (Compass — linking UX + merge collision policy; not yet present at time of writing)
**Security gate:** Sentinel pre-build pass required before implementation (see §8)

---

## 1. Problem

SessionFS today is locked to **1 git repo = 1 project**. A real product often spans multiple repos (e.g. a frontend repo + a backend repo that are one logical effort). Because personas, knowledge, tickets, and rules are all scoped to a single-repo project, the same logical persona gets stored as duplicate rows under multiple projects whenever the repos are really one effort. The CEO wants a project to be able to own multiple repos so personas/KB/etc. are shared, not duplicated.

## 2. Current State — Verified Code Map

Every design decision below is grounded in these references. All line numbers verified against the actual codebase on 2026-06-15.

### 2.1 The 1:1 Hard Constraint

| Site | File:Line | What it does |
|------|-----------|--------------|
| UNIQUE constraint | `src/sessionfs/server/db/models.py:622-623` | `Project.git_remote_normalized` — `String(255), nullable=False, unique=True, index=True` |
| Migration origin | `src/sessionfs/server/db/migrations/versions/012_projects.py:19` | `sa.Column("git_remote_normalized", sa.String(255), nullable=False, unique=True)` |
| Session model | `src/sessionfs/server/db/models.py:170` | `Session.git_remote_normalized` — nullable, no unique constraint (sessions carry the tag, never enforce it) |
| Session→project FK | `src/sessionfs/server/db/models.py:193-197` | `Session.project_id` — nullable FK to `projects.id`, `ondelete="SET NULL"` |

### 2.2 Resolvers That Assume 1 Remote → 1 Project

| Resolver | File:Line | Pattern |
|----------|-----------|---------|
| `_resolve_project_id_for_session` | `src/sessionfs/server/routes/sessions.py:501-560` | `select(Project).where(git_remote_normalized == X).with_for_update()` → `.scalar_one_or_none()` at line 543 |
| `GET /api/v1/projects/{git_remote_normalized:path}` | `src/sessionfs/server/routes/projects.py:362-383` | Same pattern, `.scalar_one_or_none()` at line 378 |
| MCP `_resolve_project_id` | `src/sessionfs/mcp/server.py:2603-2635` | HTTP `GET /api/v1/projects/{normalized}`, expects exactly one JSON object |
| CLI `_resolve_project_id` | `src/sessionfs/cli/cmd_project.py:1086-1107` | HTTP `GET /api/v1/projects/{normalized}`, expects exactly one |
| `_check_repo_access` | `src/sessionfs/server/routes/projects.py:74-82` | `select(Session.id).where(user_id=X, git_remote_normalized=Y)` — grants access per-repo |
| `list_projects` | `src/sessionfs/server/routes/projects.py:85-119` | Resolves via `distinct(Session.git_remote_normalized)` for the user — projects surfaced by repo match |
| `validate_provenance_for_sender` | `src/sessionfs/server/services/handoff_helpers.py:155-161` | `select(Project).where(git_remote_normalized == session.git_remote_normalized)` → `.scalar_one_or_none()` |
| `assert_service_key_handoff_boundary` | `src/sessionfs/server/services/handoff_helpers.py:475-565` | Git-remote fallback at line 521-528; `len(matching) > 1` is a 403 `service_key_project_ambiguous` error at line 529-544 |
| Project transfer snapshot | `src/sessionfs/server/routes/project_transfers.py:320` | `project_git_remote_snapshot=project.git_remote_normalized` |
| Org member project snapshot | `src/sessionfs/server/routes/org_members.py:828` | `project_git_remote_snapshot=project.git_remote_normalized` |
| Session sync (POST + PUT) | `src/sessionfs/server/routes/sessions.py:790-840,1604-1702,1837,1919` | Extracts git_remote from workspace, passes to `_resolve_project_id_for_session`, also used in auto-extract-knowledge gating |

### 2.3 Normalization

`normalize_git_remote()` at `src/sessionfs/server/github_app.py:25-43` — strips to `owner/repo` (lowercase). All resolvers route through this before lookup.

### 2.4 Personas — The Pain Point

`AgentPersona` at `src/sessionfs/server/db/models.py:1207-1254`:
- FK `project_id` (CASCADE)
- `UniqueConstraint("project_id", "name")` at line 1220
- Two repos = two projects = two persona rows for the same logical role

### 2.5 The 14 Project-Scoped Tables (Unchanged by This Design)

These all carry FK `project_id` and are already scoped to the unit we want shared. **They need zero re-scoping** — the `project_id` becomes the shared unit across repos for free:

1. `sessions` (via `project_id`, nullable)
2. `handoff_attachments`
3. `project_transfers`
4. `knowledge_entries`
5. `context_compilations`
6. `knowledge_pages`
7. `wiki_page_revisions`
8. `project_rules` (UNIQUE on `project_id` — see merge issue §5.6)
9. `knowledge_links`
10. `agent_personas`
11. `tickets`
12. `agent_runs`
13. `retrieval_audit_contexts`
14. `retrieval_audit_events`

### 2.6 The Ambiguity Guard That Becomes an Invariant

`assert_service_key_handoff_boundary` (`handoff_helpers.py:529-544`) currently treats `len(matching) > 1` as a 403 error. With the new global-unique constraint on `project_repos`, this case becomes **provably impossible** — the guard converts from a runtime ambiguity detector into a compile-time invariant backed by the database. Document this.

---

## 3. Design: Make Repo↔Project Many-to-One

**Direction:** A project owns N repos. Each repo belongs to exactly ONE project. The 14 project-scoped tables are unchanged — they're already scoped to `project_id`, which becomes the shared unit across repos for free.

### 3.1 Schema: `project_repos` Join Table

```sql
CREATE TABLE project_repos (
    id              VARCHAR(64) PRIMARY KEY,
    project_id      VARCHAR(64) NOT NULL
                        REFERENCES projects(id) ON DELETE CASCADE,
    git_remote_normalized VARCHAR(255) NOT NULL,
    is_primary      BOOLEAN NOT NULL DEFAULT FALSE,
    added_by_user_id VARCHAR(64)
                        REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Each remote belongs to exactly one project (global uniqueness).
    CONSTRAINT uq_project_repos_remote UNIQUE (git_remote_normalized),

    -- One primary remote per project (for display / backward compat).
    -- Partial unique index: only one row per project_id where is_primary IS TRUE.
    -- Implemented as a conditional unique index.
    CONSTRAINT uq_project_repos_primary UNIQUE (project_id, is_primary)
        -- Note: in PostgreSQL this is a partial index:
        --   CREATE UNIQUE INDEX uq_project_repos_primary
        --       ON project_repos (project_id) WHERE is_primary IS TRUE;
        -- SQLite doesn't support partial indexes; enforce at application layer
        -- with SELECT FOR UPDATE + rowcount guard pattern.

    -- Index for lookup-by-remote (primary query path).
    -- Covered by uq_project_repos_remote's implicit index in PG;
    -- explicit in migration for SQLite clarity.
);

CREATE INDEX idx_project_repos_project ON project_repos (project_id);
```

**Model (SQLAlchemy):**

```python
class ProjectRepo(Base):
    __tablename__ = "project_repos"
    __table_args__ = (
        UniqueConstraint("git_remote_normalized", name="uq_project_repos_remote"),
        Index("idx_project_repos_project", "project_id"),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    git_remote_normalized: Mapped[str] = mapped_column(
        String(255), nullable=False
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    added_by_user_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

**Decision: Keep `projects.git_remote_normalized`.** This column remains as the project's **primary/display remote** for backward compatibility. It is denormalized from `project_repos WHERE is_primary IS TRUE`. During the backfill migration, every existing project's current `git_remote_normalized` becomes the primary repo. Old clients that query `GET /projects/{remote}` against a non-primary remote will still work via the new resolver (see §3.3). New clients should use project-by-ID endpoints where possible.

**Rationale for keeping the column:**
- Zero breakage for existing API consumers, CLI, MCP, and dashboard
- The column is used in 10+ response shapes (`ProjectResponse`, transfer snapshots, org member snapshots)
- Removing it would require a multi-release deprecation cycle with no benefit
- The join table is the source of truth; the column is a cached primary reference

### 3.2 Migration (049 — Additive)

**File:** `src/sessionfs/server/db/migrations/versions/049_multi_repo_projects.py`

**Upgrade:**

1. Create `project_repos` table with all columns, constraints, and indexes
2. Backfill: one row per existing project from `projects.git_remote_normalized` with `is_primary = TRUE`
   ```sql
   INSERT INTO project_repos (id, project_id, git_remote_normalized, is_primary, created_at)
   SELECT gen_random_uuid()::text, id, git_remote_normalized, TRUE, NOW()
   FROM projects
   WHERE git_remote_normalized IS NOT NULL AND git_remote_normalized != '';
   ```
   (Use SQLite-compatible UUID generation in the migration helper; the existing `_gen_id()` utility is available.)
3. No DROP or ALTER of `projects.git_remote_normalized` — retained for backward compat
4. No NOT NULL changes on existing columns

**Downgrade:** Drop `project_repos` table. (No data loss risk — the column on `projects` was never removed.)

**Zero downtime:** Additive-only. Old code ignores the new table. New code queries the new table first, falls back to the old column.

### 3.3 Resolution Changes — Every Site

The core pattern change: **every `select(Project).where(git_remote_normalized == X)` becomes a two-step lookup through `project_repos`.**

#### New Shared Helper

```python
# src/sessionfs/server/services/project_resolver.py (NEW)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sessionfs.server.db.models import Project, ProjectRepo

async def resolve_project_by_remote(
    db: AsyncSession,
    git_remote_normalized: str,
    *,
    for_update: bool = False,
) -> Project | None:
    """Resolve a project from a git remote via the project_repos join table.

    Dual-read: project_repos first, then fallback to projects.git_remote_normalized
    for backward compatibility during transition. Once all rows are backfilled and
    old clients are upgraded, the fallback can be removed (future cleanup ticket).

    Returns None when no project owns this remote.
    """
    if not git_remote_normalized:
        return None

    # Primary path: project_repos join table (source of truth)
    stmt = (
        select(Project)
        .join(ProjectRepo, ProjectRepo.project_id == Project.id)
        .where(ProjectRepo.git_remote_normalized == git_remote_normalized)
    )
    if for_update:
        stmt = stmt.with_for_update(of=Project)
    result = await db.execute(stmt)
    project = result.scalar_one_or_none()
    if project is not None:
        return project

    # Fallback: legacy projects.git_remote_normalized column
    # (for rows that haven't been backfilled or old direct writes)
    stmt = select(Project).where(
        Project.git_remote_normalized == git_remote_normalized
    )
    if for_update:
        stmt = stmt.with_for_update()
    result = await db.execute(stmt)
    return result.scalar_one_or_none()
```

#### Site-by-Site Changes

| # | Site | File:Line | Change |
|---|------|-----------|--------|
| 1 | `_resolve_project_id_for_session` | `routes/sessions.py:537-543` | Replace `select(Project).where(git_remote_normalized == X).with_for_update()` with `await resolve_project_by_remote(db, git_remote_normalized, for_update=True)` |
| 2 | `GET /api/v1/projects/{git_remote_normalized:path}` | `routes/projects.py:376-378` | Replace `select(Project).where(git_remote_normalized == X)` with `await resolve_project_by_remote(db, git_remote_normalized)` |
| 3 | MCP `_resolve_project_id` | `mcp/server.py:2625-2627` | No DB change needed (this is an HTTP client). The server-side route (#2) handles the resolution. However: the MCP tool's error message on 404 should mention multi-repo ("No project found for {normalized}. Run: sfs project init, or link this repo to an existing project with: sfs project link-repo") |
| 4 | CLI `_resolve_project_id` | `cli/cmd_project.py:1103-1107` | Same as #3 — server-side resolution covers this. Update error message to mention `link-repo`. |
| 5 | `_check_repo_access` | `routes/projects.py:74-82` | **Needs redesign:** currently checks `Session.git_remote_normalized == Y`. Must now find the project via `resolve_project_by_remote`, then check if user has sessions for ANY repo owned by that project. Or simpler: just check if user has sessions with `project_id == resolved_project.id` (the `sessions.project_id` FK, which is set at sync time by `_resolve_project_id_for_session`). |
| 6 | `list_projects` | `routes/projects.py:108-114` | Currently resolves via `distinct(Session.git_remote_normalized)`. Must change to: (a) find all session remotes, (b) resolve each to a project via `project_repos` + fallback, (c) UNION with org-membership and ownership queries. Dedup by project_id. |
| 7 | `validate_provenance_for_sender` | `handoff_helpers.py:155-161` | Replace `select(Project).where(git_remote_normalized == session.git_remote_normalized)` with `await resolve_project_by_remote(db, session.git_remote_normalized)`. **Also check `session.project_id` first** as the authoritative anchor (mirrors the handoff boundary guard's R6 fix pattern at `handoff_helpers.py:515-520`). |
| 8 | `assert_service_key_handoff_boundary` | `handoff_helpers.py:521-546` | Replace the legacy fallback block (lines 521-546) with `await resolve_project_by_remote(db, source_session.git_remote_normalized)`. The `len(matching) > 1` ambiguity error is now **provably unreachable** (the UNIQUE constraint on `project_repos.git_remote_normalized` guarantees at most one project per remote). Keep the error as a defense-in-depth invariant check — if it ever fires, the database constraint has been violated. |
| 9 | Session sync (POST + PUT) | `routes/sessions.py:790-795,1604-1609,1837,1919` | Already passes through `_resolve_project_id_for_session` (#1). The `git_remote_normalized` column on `Session` stays as a denormalized tag; `session.project_id` is also set from the resolver. **No change needed** — the resolver handles the indirection. |
| 10 | Project transfer snapshots | `routes/project_transfers.py:320` | `project_git_remote_snapshot` currently reads `project.git_remote_normalized`. After migration, this should read the PRIMARY remote from `project_repos`. Add a helper `get_primary_remote(db, project_id)` that returns the `is_primary=True` row's normalized remote. |
| 11 | Org member audit | `routes/org_members.py:828` | Same as #10 — use `get_primary_remote()`. |

### 3.4 The `is_primary` One-Per-Project Invariant

The primary remote is the one shown in `ProjectResponse.git_remote_normalized` and stored in `project_transfers.project_git_remote_snapshot`. Enforce exactly one primary per project:

- **On link:** if `is_primary=True` is requested, atomically swap: `UPDATE project_repos SET is_primary=FALSE WHERE project_id=X` then `INSERT ... is_primary=TRUE`. In a single transaction with SELECT FOR UPDATE on the project's existing repo rows.
- **On unlink:** if unlinking the primary, auto-promote the oldest remaining repo to primary. If it's the LAST repo, deny unlink (a project must have at least one repo — see Open Decision Q4).
- **Application-level guard:** Before any write, assert exactly one primary per project. The partial unique index in PostgreSQL enforces this at the DB level; SQLite enforces via the app-level SELECT FOR UPDATE serialization pattern.

---

## 4. API + CLI Surface

### 4.1 REST Endpoints

#### `POST /api/v1/projects/{project_id}/repos`
Link a repo to a project.

**Authz:** Project owner OR org admin (if org-scoped). User must also have captured at least one session on the target remote OR be the project owner (prevents hijacking repos you've never worked on).

**Request:**
```json
{
  "git_remote": "https://github.com/acme/backend.git",
  "is_primary": false
}
```

**Responses:**
- `201` — linked, returns `ProjectRepoResponse`
- `409` — repo already linked to another project: `{"error": "repo_already_linked", "existing_project_id": "...", "message": "This repo is already linked to project X. Unlink it there first, or merge the projects (see: sfs project merge)."}`
- `403` — not authorized
- `422` — last repo unlink denied (see Q4)

#### `DELETE /api/v1/projects/{project_id}/repos/{repo_id}`
Unlink a repo.

**Authz:** Same as link. Cannot unlink the last repo (see Q4).

#### `GET /api/v1/projects/{project_id}/repos`
List all repos for a project. Returns `ProjectRepoResponse[]` with `is_primary` flag.

#### `POST /api/v1/projects/{project_id}/merge`
Merge another project into this one. See §5 for full design.

#### `GET /api/v1/projects/{git_remote_normalized:path}` (existing — updated)
No API surface change. The resolver now finds the project through `project_repos` (see §3.3 #2). Response still includes `git_remote_normalized` — now the primary remote.

### 4.2 CLI Commands

```bash
sfs project link-repo <remote> [--primary] [--project-id <id>]
    # Links a repo to a project. If --project-id is omitted, resolves
    # the current repo's project and links <remote> to it.
    # --primary makes this the display remote.

sfs project unlink-repo <remote> [--project-id <id>]
    # Unlinks a repo. Refuses if it's the last repo.

sfs project repos [--project-id <id>]
    # Lists all repos for the project, with primary marked.

sfs project merge [--into <project-id>] [--dry-run]
    # Merges the current repo's project INTO the target project.
    # --dry-run is the DEFAULT (mirrors bulk-promote pattern).
    # --confirm to execute.
    # See §5.
```

### 4.3 Dashboard (Prism Follow-Up)

Prism will design a repo-manager panel on the Project detail page. Not designed here — this is the backend contract:
- The `GET /repos` endpoint above provides the data
- The link/unlink endpoints provide the mutations
- `ProjectResponse` gains an optional `repos: list[ProjectRepoResponse]` field (populated on detail requests, omitted on list to avoid N+1)

---

## 5. The Merge Migration

The hardest part. Users who already have split projects (same logical product, different repos) need a way to fold project B into project A.

### 5.1 Design Principles (From v0.10.13 `/rebuild` Incident)

The v0.10.13 incident (`tk_bc3c02a63e994717`) taught us that multi-commit destructive operations are dangerous. The merge MUST be:

1. **Single atomic transaction** — all or nothing. If anything fails mid-merge, the entire transaction rolls back and both projects are untouched.
2. **Dry-run first** — `dry_run=True` (default) performs all validation and reports what WOULD happen without writing. `dry_run=False` executes.
3. **Audit-logged** — a new `project_merge_audit` table records every merge attempt with before/after snapshots (project IDs, owner IDs, repo lists, entity counts per table).
4. **NOT undoable automatically** — like `bulk_promote`, the merge is one-way. The dry-run report IS the undo plan (the user can see exactly what will happen and decide).

### 5.2 Endpoint

`POST /api/v1/projects/{target_project_id}/merge`

**Request:**
```json
{
  "source_project_id": "proj_abc123",
  "dry_run": true
}
```

**Authz:**
- Caller must OWN both projects OR be org admin of both projects' orgs
- Both projects must be in the same org (or both personal)
- Cross-org merges are DENIED (data-stays-access-revoked invariant — you can't merge a personal project into an org project without explicit transfer first)

### 5.3 Entity Reassignment (14 Tables)

For each of the 14 project-scoped tables, reassign `project_id` from source → target:

```sql
-- Pattern repeated per table:
UPDATE <table> SET project_id = :target_id WHERE project_id = :source_id;
```

**Table-specific considerations:**

| Table | Considerations |
|-------|---------------|
| `sessions` | `project_id` is nullable FK with `ON DELETE SET NULL`. Straight UPDATE. Sessions keep their original `git_remote_normalized` tag (denormalized, unchanged). |
| `agent_personas` | **Dedup required.** See §5.4. |
| `project_rules` | **Conflict.** UNIQUE on `project_id`. Only one rules row survives per project. See §5.6. |
| `knowledge_entries` | **Dedup desired.** Entries with same `entry_type` + `content` hash may be duplicates. See §5.5. |
| `knowledge_pages` | **Slug collisions.** Same slug on both projects → rename source slug to `{slug}-merged-{source_project_id}` with a `merged_from_project_id` note in `wiki_page_revisions`. |
| `wiki_page_revisions` | Re-point `page_id` (after slug collision resolution above). |
| `tickets` | Preserve FSM state, `lease_epoch`, `depends_on`. Re-point `parent_ticket_id` if pointing within source project. `depends_on` entries referencing source-project tickets must be rewritten to the new IDs. |
| `agent_runs` | Straight reassign. `persona_name` + `ticket_id` are plain strings (no FK), survive unchanged. |
| `handoff_attachments` | Straight reassign. `project_id` is NOT NULL; FK handles cascade. |
| `context_compilations` | Straight reassign. |
| `retrieval_audit_contexts` | Straight reassign. |
| `retrieval_audit_events` | Straight reassign. |
| `knowledge_links` | Straight reassign. |
| `project_transfers` | Straight reassign. |

### 5.4 Persona Dedup

The `uq_persona_project_name` constraint (models.py:1220) means two personas with the same `name` cannot coexist in the target project.

**Policy (Open Decision Q2 — see §7):** The collision resolution policy is a Compass/CEO decision. The mechanism supports any of these:

**Option A — Rename source personas (recommended):** Source-project personas that collide with target-project personas get `name = "{original}-{source_project_id_suffix}"` (e.g., `atlas-proj_abc123`). The target persona wins. Audit log records the rename.

**Option B — Target wins, source dropped:** Source persona is NOT migrated. Sessions that referenced the source persona still carry `persona_name` as a plain string (no FK), so they survive. The persona row itself is soft-deleted (`is_active=False`).

**Option C — Merge content:** Combine both persona contents into one (append source content to target with a `--- merged from {source_project_id} ---` divider). Risky — persona content is markdown injected verbatim into context windows; concatenation could produce unbounded prompts.

**Mechanism (shared across options):**
```python
async def _dedupe_personas(db, source_id, target_id, policy: str):
    source_personas = await db.execute(
        select(AgentPersona).where(AgentPersona.project_id == source_id)
    )
    target_names = set(await db.execute(
        select(AgentPersona.name).where(AgentPersona.project_id == target_id)
    ))

    for persona in source_personas:
        if persona.name in target_names:
            if policy == "rename":
                persona.name = f"{persona.name}-{source_id[:8]}"
            elif policy == "skip":
                persona.is_active = False  # soft-delete
            elif policy == "merge_content":
                target = await db.execute(
                    select(AgentPersona).where(
                        AgentPersona.project_id == target_id,
                        AgentPersona.name == persona.name,
                    )
                ).scalar_one()
                target.content = f"{target.content}\n\n--- merged from {source_id} ---\n{persona.content}"
                persona.is_active = False
        # else: no collision, persona name unchanged
```

### 5.5 Knowledge Entry Dedup

Two projects may have the same knowledge entry (same concept, added independently). The merge should avoid literal duplicates.

**Mechanism:** For each source entry, check if a semantically equivalent entry exists in the target:
- Same `entry_type` AND `content` normalized (stripped whitespace, lowercased)
- If match found: skip the source entry, log in audit
- If no match: reassign

This is a **best-effort exact-match dedup**, not semantic dedup (which would require an LLM call — no server-side LLM keys per house rules). A future `sfs project dedup` command could do semantic dedup client-side.

### 5.6 ProjectRules Conflict

`ProjectRules` has `unique=True` on `project_id` (models.py:1086). Only one rules row per project.

**Policy:** Target rules WIN. Source rules are archived as a `wiki_page_revision` with slug `_merged_rules_{source_project_id}` so they're recoverable. The merge audit records the source rules hash.

**Rationale:** Rules are the most actively curated project artifact. Overwriting the target's rules with source rules would be surprising and destructive. Preserving source rules as a wiki page makes them searchable and copyable without breaking the target.

### 5.7 Wiki Page Slug Collisions

If both projects have a page at slug `architecture`, the source page is renamed to `architecture-merged-{source_id[:8]}`. Its revision history stays intact. A `merged_from_project_id` field on `WikiPageRevision` (new nullable column in migration 049) records the origin.

### 5.8 Repo Reassignment

All `project_repos` rows for the source project get `project_id = target_id`. The source project's primary remote becomes a non-primary repo under the target (unless the target has no primary set, in which case the source's primary becomes the target's primary). The source project itself is **NOT deleted** — it is marked with a new `merged_into_project_id` column (nullable FK to `projects.id`, `ON DELETE SET NULL`).

### 5.9 Source Project Tombstone

Add to `projects`:
```python
merged_into_project_id: Mapped[str | None] = mapped_column(
    String(64),
    ForeignKey("projects.id", ondelete="SET NULL"),
    nullable=True,
)
merged_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
)
```

A merged project:
- Returns `410 Gone` on `GET /api/v1/projects/{id}` with `{"merged_into": "<target_id>", "message": "This project was merged into <target_id>."}`
- Its repos now belong to the target
- Its `project_repos` rows are reassigned
- Access to the tombstone project is still governed by the original owner (read-only for audit)

### 5.10 Merge Audit Table

```sql
CREATE TABLE project_merge_audit (
    id              VARCHAR(64) PRIMARY KEY,
    source_project_id VARCHAR(64),  -- nullable after source project delete
    target_project_id VARCHAR(64),  -- nullable after target project delete
    initiated_by_user_id VARCHAR(64) REFERENCES users(id) ON DELETE SET NULL,
    dry_run         BOOLEAN NOT NULL DEFAULT TRUE,
    persona_policy  VARCHAR(20) NOT NULL,    -- 'rename' | 'skip' | 'merge_content'
    stats           JSONB NOT NULL DEFAULT '{}',  -- per-table counts before/after
    persona_renames JSONB NOT NULL DEFAULT '[]',  -- [{old_name, new_name}]
    slug_renames    JSONB NOT NULL DEFAULT '[]',  -- [{old_slug, new_slug}]
    rules_archived  BOOLEAN NOT NULL DEFAULT FALSE,
    error_message   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### 5.11 Transaction Structure

```python
async def merge_projects(db, source_id, target_id, user_id, dry_run, persona_policy):
    # Phase 1: READ — validate preconditions (all in the same transaction)
    source = await db.get(Project, source_id)
    target = await db.get(Project, target_id)
    if not source or not target:
        raise HTTPException(404)
    if source.org_id != target.org_id:
        raise HTTPException(400, "Cross-org merges are not supported")
    if source.merged_into_project_id is not None:
        raise HTTPException(400, "Source project was already merged")
    if target.merged_into_project_id is not None:
        raise HTTPException(400, "Target project was already merged")

    # Pre-compute all collision data
    persona_collisions = await _detect_persona_collisions(db, source_id, target_id)
    slug_collisions = await _detect_slug_collisions(db, source_id, target_id)
    ke_duplicates = await _detect_ke_duplicates(db, source_id, target_id)

    # Build stats for dry-run report
    stats = await _compute_merge_stats(db, source_id, target_id,
                                        persona_collisions, slug_collisions, ke_duplicates)

    if dry_run:
        return {"dry_run": True, "stats": stats, "persona_collisions": persona_collisions,
                "slug_collisions": slug_collisions, "ke_duplicates": ke_duplicates}

    # Phase 2: WRITE — single atomic transaction
    # (everything below is inside the same transaction as the reads above)

    # 1. Reassign repos
    await db.execute(
        update(ProjectRepo).where(ProjectRepo.project_id == source_id)
        .values(project_id=target_id)
    )

    # 2. Handle personas (dedup per policy)
    persona_renames = await _apply_persona_policy(db, source_id, target_id, persona_policy)

    # 3. Handle wiki page slugs
    slug_renames = await _apply_slug_renames(db, source_id, target_id, slug_collisions)

    # 4. Archive source rules as wiki page, reassign target rules only row
    source_rules = await db.execute(
        select(ProjectRules).where(ProjectRules.project_id == source_id)
    ).scalar_one_or_none()
    if source_rules:
        await _archive_rules_as_wiki_page(db, source_rules, target_id, source_id)
        await db.delete(source_rules)

    # 5. Reassign all 14 tables
    for table_model in PROJECT_SCOPED_TABLES:
        if table_model is AgentPersona:  # already handled above
            continue
        if table_model is ProjectRules:  # already handled above
            continue
        await db.execute(
            update(table_model).where(table_model.project_id == source_id)
            .values(project_id=target_id)
        )

    # 6. Mark source as merged tombstone
    source.merged_into_project_id = target_id
    source.merged_at = func.now()

    # 7. Write audit row
    audit = ProjectMergeAudit(
        id=generate_id(),
        source_project_id=source_id,
        target_project_id=target_id,
        initiated_by_user_id=user_id,
        dry_run=False,
        persona_policy=persona_policy,
        stats=stats,
        persona_renames=persona_renames,
        slug_renames=slug_renames,
        rules_archived=source_rules is not None,
    )
    db.add(audit)

    # Single commit for everything
    await db.commit()
    return {"dry_run": False, "stats": stats, "audit_id": audit.id}
```

### 5.12 Rollback Guarantee

If ANY step in Phase 2 fails (constraint violation, unexpected NULL, whatever), the entire transaction rolls back. Both projects are untouched. The error is logged to `project_merge_audit.error_message` in a separate transaction (the audit row from the failed attempt is written outside the merge txn using a nested transaction or a background task — but since we don't have background workers, we write the audit row FIRST in its own transaction, then attempt the merge in a second transaction).

---

## 6. Authz / Security Implications

### 6.1 Who Can Link a Repo

1. **Project owner** — always
2. **Org admin** (for org-scoped projects) — always
3. **Repo-access check:** The linking user must have captured at least one session on the target remote, OR be the project owner / org admin. This prevents hijacking — you can't link `facebook/react` to your personal project unless you've actually worked in that repo.

### 6.2 Repo Hijacking Prevention

The global `UNIQUE(git_remote_normalized)` on `project_repos` is the enforcement point:
- A repo can only belong to ONE project
- Linking a repo that's already linked returns 409 with the existing project ID
- The user must unlink first, or merge the projects

### 6.3 Cross-Org Boundary

- A repo linked to an org-scoped project is "owned" by that org
- Linking the same repo to a different org's project is rejected (409)
- Merging projects across orgs is DENIED (400)
- Service keys: the `assert_service_key_can_access_project` check in `handoff_helpers.py:561-564` already validates project-level access. With multi-repo, the project boundary is unchanged — service keys get access to ALL repos under a project, which is the desired behavior (service keys work at project granularity).

### 6.4 Who Can Merge Projects

Both projects must be owned by the same entity:
- **Both personal:** Caller must own both
- **Both same org:** Caller must be org admin of that org
- **Mixed (personal + org):** DENIED. Use project transfer first (`POST /api/v1/projects/{id}/transfer`), then merge.

### 6.5 Sentinel Pre-Build Security Pass

Before any implementation code is written, Sentinel must review:
1. Repo hijacking vector (linking a repo you don't control)
2. Cross-org repo linking
3. Merge authz (can a non-owner merge two projects?)
4. Service key behavior post-merge (do scoped project_ids get rewritten?)
5. Audit trail completeness (can a malicious merge be detected post-hoc?)
6. Tombstone project access (can the old owner still read the tombstone?)

---

## 7. Open Decisions

### Q1: Each repo belongs to exactly one project — confirm?

**Recommendation: YES.** This is the cornerstone invariant. Without it:
- Resolution is ambiguous (which project does `resolve_project_by_remote` return?)
- The handoff boundary guard (`assert_service_key_handoff_boundary`) can't anchor service keys
- Merging becomes an n:m reconciliation problem instead of simple reassignment

The global UNIQUE constraint on `project_repos.git_remote_normalized` enforces this at the database level. A repo is either unowned or owned by exactly one project.

### Q2: Persona collision policy + Rules collision policy

**Status:** Deferred to Compass companion doc (`docs/design/multi-repo-projects-product.md`). The mechanism supports all three options (rename, skip, merge_content).

**Atlas recommendation:** **Option A (rename source personas)** for personas, **target wins + archive source** for rules. Rationale:
- Personas: renaming preserves all data; the user can manually merge content afterward if desired. Skipping loses data. Merging content risks unbounded prompt injection.
- Rules: rules are the most actively curated artifact. Overwriting would be surprising. Archiving as a wiki page preserves the source rules for reference.

### Q3: Is multi-repo / merge a tiered feature?

**Recommendation: NO for multi-repo itself (linking repos). YES for merge.**

- **Multi-repo linking** should be available to all tiers (including Free). It's a fundamental data-model correction, not a premium feature. Gating it behind a tier would punish users who organically have multi-repo products.
- **Merge** could reasonably be Team+ (it's a one-time cleanup operation, not an ongoing need). However, the dry-run default makes it safe for all tiers — users can see what would happen without paying. Defer to Compass/CEO.

### Q4: Must a project have at least one repo?

**Recommendation: YES.** A project with zero repos is unreachable via the git-remote-based resolution paths (which is how the CLI, MCP, and session sync all discover projects). Without at least one repo, the project is an orphan accessible only by knowing its UUID.

**Enforcement:**
- `DELETE /repos/{id}` returns 422 if it would leave the project with zero repos
- `POST /merge` folds all repos into the target (source becomes a tombstone with zero repos — this is the ONE exception; the tombstone is explicitly marked as merged and returns 410)
- Project deletion (existing behavior) cascades through `project_repos` via `ON DELETE CASCADE`

### Q5: Should `projects.git_remote_normalized` ever be dropped?

**Recommendation: NOT YET.** Keep it for at least two releases as a denormalized primary-remote cache. Once all resolvers use `project_repos` and the fallback path has been exercised in production for a full release cycle, file a cleanup ticket to:
1. Make the column nullable (it's currently NOT NULL)
2. Drop it in a subsequent migration
3. Remove the fallback path from `resolve_project_by_remote`

This is a v0.12+ cleanup, not a v0.11 concern.

---

## 8. Review Checklists

### 8.1 Codex Review Checklist

- [ ] Schema: `project_repos` table — are constraints correct? Is the partial unique index for `is_primary` handled correctly for both PG and SQLite?
- [ ] Migration: backfill correct? Any edge case with empty `git_remote_normalized` on existing projects?
- [ ] Resolver: every `scalar_one_or_none()` site changed? Any missed?
- [ ] Dual-read fallback: does the fallback create a window where a newly-created project (with `project_repos` row) is shadowed by a stale `projects.git_remote_normalized`? (No — `project_repos` is checked FIRST.)
- [ ] Merge: atomic transaction boundaries correct? Audit row written BEFORE merge attempt?
- [ ] Merge: persona dedup — what happens if BOTH projects have personas with the same name AND both are referenced by active tickets?
- [ ] Merge: ticket `depends_on` rewriting — are self-references within the source project handled? What about cross-project `depends_on`?
- [ ] Merge: `project_rules` — if the target has NO rules but the source does, should source rules be promoted instead of archived?
- [ ] Merge: wiki page slug collision — if the renamed slug `{slug}-merged-{id}` also collides (unlikely but possible with truncated IDs)?
- [ ] Tombstone: `GET /projects/{id}` for a merged project — does the 410 response leak the target project ID to unauthorized users?
- [ ] CLI: error messages guide users to `link-repo` when a remote isn't found?
- [ ] Service keys: does a service key scoped to the source project's ID still work after merge? (No — project ID changes. Service key scopes would need rewriting or the key would lose access.)
- [ ] Race: two users simultaneously linking the same remote to different projects? (UNIQUE constraint + 409 on integrity error.)
- [ ] Race: merge + concurrent session sync? (Session sync resolves project_id at write time; merge is a separate transaction. A session synced during merge might land on either project_id. Acceptable — the session FK is best-effort metadata.)

### 8.2 Shield-SR Security Review Checklist

- [ ] Repo hijacking: can a user link a repo they don't have access to?
- [ ] Cross-org linking: can a repo be linked to two orgs simultaneously? (UNIQUE constraint prevents this at DB level, but is the app-layer check watertight?)
- [ ] Merge authz: can a non-owner merge two projects? Org-membership edge cases?
- [ ] Service key project boundary: does `assert_service_key_handoff_boundary` behave correctly when a project has multiple repos?
- [ ] Tombstone data leak: can a former org member still read a tombstone project they once had access to?
- [ ] Audit completeness: is every merge mutation recorded in `project_merge_audit`?
- [ ] Migration safety: does the backfill handle all existing rows correctly?
- [ ] Downgrade safety: can migration 049 be cleanly downgraded?
- [ ] `merged_into_project_id` — any risk of circular merges (A→B→A)? Enforced by pre-condition check.

---

## 9. Implementation Order (Recommended)

1. **Migration 049** — `project_repos` table + backfill + `merged_into_project_id` on projects + `project_merge_audit` table
2. **Shared helper** — `resolve_project_by_remote()` with dual-read fallback
3. **Resolver changes** — all 11 sites in §3.3, one at a time with targeted tests
4. **API endpoints** — link-repo, unlink-repo, list-repos
5. **API endpoint** — merge (dry-run first, then live)
6. **CLI commands** — link-repo, unlink-repo, repos, merge
7. **Backfill cleanup** — verify every existing project has a `project_repos` row; add a startup health check
8. **Deprecation plan** — file ticket to drop `projects.git_remote_normalized` in v0.12+
9. **Dashboard** — Prism designs and builds the repo-manager panel
10. **Sentinel security pass** — before merge endpoint ships to production

---

## Appendix A: Files Touched (Summary)

| File | Change |
|------|--------|
| `src/sessionfs/server/db/models.py` | Add `ProjectRepo`, `ProjectMergeAudit` models; add `merged_into_project_id` + `merged_at` to `Project` |
| `src/sessionfs/server/db/migrations/versions/049_multi_repo_projects.py` | NEW — migration |
| `src/sessionfs/server/services/project_resolver.py` | NEW — `resolve_project_by_remote()` helper |
| `src/sessionfs/server/routes/sessions.py` | Replace 3 `scalar_one_or_none()` sites with helper |
| `src/sessionfs/server/routes/projects.py` | Replace resolver + add link/unlink/list/merge endpoints |
| `src/sessionfs/server/services/handoff_helpers.py` | Replace 2 direct query sites with helper |
| `src/sessionfs/mcp/server.py` | Update error messages; no resolver change (HTTP client) |
| `src/sessionfs/cli/cmd_project.py` | Add 4 commands + update error messages |
| `src/sessionfs/server/routes/project_transfers.py` | Use `get_primary_remote()` for snapshot |
| `src/sessionfs/server/routes/org_members.py` | Use `get_primary_remote()` for snapshot |

## Appendix B: Edge Cases Not Yet Resolved

1. **Service key `project_ids` allowlist post-merge:** If a service key is scoped to `project_ids: ["proj_A"]` and proj_A is merged into proj_B, should the key's scope auto-update to `["proj_B"]`? Or should the key lose access? (Recommend: DO NOT auto-update — the key was explicitly scoped to A. Merging is a destructive operation that should require explicit key re-scoping.)

2. **Session `project_id` during merge race:** A session syncing concurrently with a merge may get `project_id = source_id` just before the merge transaction commits. After merge, `source_id` is a tombstone. The session's FK (`ON DELETE SET NULL`) will set it to NULL — the session becomes unlinked but not lost. Acceptable for v0.11; a future improvement could re-resolve orphan sessions on next sync.

3. **Knowledge entry entity_ref cross-references:** Some KB entries reference other entries by `entity_ref`. If those entries get new IDs during merge dedup, entity_refs become dangling. This is pre-existing behavior (entity_refs are best-effort string tags, not FKs). A future `sfs project repair-entity-refs` command could fix these.

4. **Merge with active project transfer:** If project A has a pending transfer, can it be merged? (Recommend: NO — require transfers to be resolved first.)
