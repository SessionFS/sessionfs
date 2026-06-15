# Binding Design — Multi-Repo Projects

**Status:** ✅ Codex VERIFIED-CLEAN for design handoff (R5, commit `daad7a7`, 2026-06-15) — approved for implementation pending the Sentinel security pass (§8). Review history: R1 (3 HIGH + 4 MED + 3 LOW) → R2 (0 HIGH, 4 MED + 1 LOW) → R3 (3 MED + 2 LOW) → R4 (1 MED + 1 LOW) → R5 CLEAN.
**Author:** Atlas (backend/data-model)
**Date:** 2026-06-15
**Companion:** `docs/design/multi-repo-projects-product.md` (Compass — linking UX + merge collision policy)
**Security gate:** Sentinel pre-build pass required before implementation (see §8)

## Revision History

| Rev | Date | Changes |
|-----|------|---------|
| R1 | 2026-06-15 | Codex R1: 3 HIGH (primary-demotion order, stranded personas, tombstone-aware resolvers) + 4 MED (exhaustive resolver list 11→16, ticket reassign-in-place, is_primary partial index, provider fields) + 3 LOW (JSONB→Text JSON, promote source rules when target none, CLI link-repo naming). CEO calls applied: ticket=reassign-in-place, CLI=link-repo. Per-table merge matrix added. |
| R2 | 2026-06-15 | Codex R2: 4 MED + 1 LOW. MED-1: dry-run writes zero DB rows (audit-row creation moved after dry-run return). MED-2: persona collision rename produces legal ASCII slug `{name}-{src8}` ≤50 chars (verified against `personas.py:45` regex `^[A-Za-z0-9_-]{1,50}$`); human-readable note in audit only; in-flight collision guard against other renamed personas. MED-3: wiki revision reassign uses `(project_id, page_slug)` not nonexistent `page_id` FK (verified `models.py:1054-1058`); revision-number uniqueness handled with offset numbering. MED-4: KnowledgeLink is map+dedup+reassign (not straight reassign) to avoid `uq_kl_link` violation (verified `models.py:1178`); entry-ID mapping from KnowledgeEntry dedup feeds link rewriting. LOW-5: `provider_repo_id` is server-derived/verified, not caller-trusted under unique constraint; added to Sentinel checklist. Residual: §10 test plan added. |
| R3 | 2026-06-15 | Codex R3: 3 MED + 2 LOW. MED-1: skip/merge_content collision policies now assign a legal archived unique name (`{name}-{src8}-archived`, ≤50 chars, `^[A-Za-z0-9_-]$`) BEFORE reassigning to target_id — no uq_persona_project_name violation, no tombstone stranding. MED-2: KnowledgeLink pseudocode rewritten: compute remapped key BEFORE mutation; duplicates are `db.delete()`'d; running set guards self-collision; no mutate-then-flush. MED-3: execute path writes an ATTEMPT audit row (status='started') in a separate session BEFORE mutation, then outcome-updates to 'completed'/'failed' in exception handler — survives rollback; dry-run stays zero-write; `status` column added to `project_merge_audit`. LOW-4: stale `(from <project>)` text replaced with `{name}-{src8}` in §7 and summary sections. LOW-5: companion wiki slug aligned to `{slug}-{src8}`; all `(from <source>)` suffixes swept and replaced. |
| R4 | 2026-06-15 | Codex R4: 1 MED + 1 LOW (audit-contract cleanup). MED-1: narrowed audit guarantee — precondition/authz rejections (404, cross-org, already-merged) are refused BEFORE the `started` audit row exists and are covered by standard request/access logging; only *validated* execute mutation attempts are merge-audited; §5.11 ordering now explicit (preconditions first, not merge-audited). LOW-2: added `skipped_link_ids TEXT NOT NULL DEFAULT '[]'` column to `project_merge_audit` (mirrors `skipped_ke_ids`); outcome-update now persists it. |

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

### 2.2 Resolvers That Assume 1 Remote → 1 Project (EXHAUSTIVE — 16 sites)

All verified by exhaustive grep of `git_remote_normalized` across `src/sessionfs/server`, `src/sessionfs/mcp`, `src/sessionfs/cli` on 2026-06-15.

**A. Direct project-resolution sites (use `Project.git_remote_normalized` in WHERE clause — must route through `project_repos`):**

| # | Resolver | File:Line | Pattern |
|---|----------|-----------|---------|
| A1 | `_resolve_project_id_for_session` | `routes/sessions.py:537-543` | `select(Project).where(git_remote_normalized == X).with_for_update()` → `.scalar_one_or_none()` |
| A2 | `GET /api/v1/projects/{git_remote_normalized:path}` | `routes/projects.py:376-378` | Same pattern, `.scalar_one_or_none()` |
| A3 | `PUT /api/v1/projects/{git_remote_normalized:path}/context` | `routes/projects.py:410` | `select(Project).where(git_remote_normalized == X)` → `.scalar_one_or_none()` |
| A4 | `POST /api/v1/projects/` (create) duplicate check | `routes/projects.py:261` | `select(Project).where(git_remote_normalized == body.git_remote_normalized)` — must also check `project_repos` |
| A5 | `validate_provenance_for_sender` | `handoff_helpers.py:155-161` | `select(Project).where(git_remote_normalized == session.git_remote_normalized)` → `.scalar_one_or_none()` |
| A6 | `validate_attachments` | `handoff_helpers.py:215-221` | `select(Project).where(git_remote_normalized == session.git_remote_normalized)` → `.scalar_one_or_none()` |
| A7 | `assert_service_key_handoff_boundary` (legacy fallback) | `handoff_helpers.py:521-546` | `select(Project).where(git_remote_normalized == session.git_remote_normalized)` → `.scalars().all()`, `len(matching) > 1` is 403 |
| A8 | `_get_project_or_404` (rules.py UUID/remote dual resolver) | `routes/rules.py:145-148` | `select(Project).where(Project.git_remote_normalized == project_id)` fallback after UUID try |
| A9 | Handoff create — attachment project_id resolution | `routes/handoffs.py:344-350` | `select(Project.id).where(git_remote_normalized == session.git_remote_normalized)` |
| A10 | Handoff claim — persona-only project_id resolution | `routes/handoffs.py:916-928` | `select(Project.id).where(git_remote_normalized == source_session.git_remote_normalized)` |

**B. Access-predicate sites (use `Session.git_remote_normalized` JOIN/WHERE against `Project.git_remote_normalized` — must switch to `Session.project_id` or join through `project_repos`):**

| # | Site | File:Line | Pattern |
|---|------|-----------|---------|
| B1 | `user_can_access_project` predicate #3 | `auth/project_access.py:55-65` | `Session.git_remote_normalized == project.git_remote_normalized` — checks if user has sessions on the project's remote |
| B2 | `_accessible_project_ids` path #3 | `handoff_helpers.py:116-126` | `select(Project.id).join(Session, Session.git_remote_normalized == Project.git_remote_normalized).where(user_id=X)` |
| B3 | `_get_project_or_404` access check (rules.py) | `routes/rules.py:153-158` | `Session.git_remote_normalized == project.git_remote_normalized` — same pattern as B1 |
| B4 | `list_projects` remote resolution | `routes/projects.py:124` | `Project.git_remote_normalized.in_(user_remotes)` — projects matched by session remote |
| B5 | `list_projects` session count aggregation | `routes/projects.py:134-145` | `select(Session.git_remote_normalized, count).where(git_remote_normalized.in_(remotes)).group_by(...)` |
| B6 | `_check_repo_access` | `routes/projects.py:74-82` | `Session.git_remote_normalized == git_remote` — grants access per-repo |

**C. Remote-propagated-through-session sites (no change needed — these write/read the Session row's denormalized `git_remote_normalized` tag, which survives as a per-session metadata column):**

| # | Site | File:Line | Why no change |
|---|------|-----------|---------------|
| C1 | Session sync POST + PUT — extracts workspace remote | `routes/sessions.py:790-795,1604-1609,1837,2184` | Passes through A1 for project resolution; session keeps its denormalized tag |
| C2 | Session sync — auto-extract-knowledge gating | `routes/sessions.py:1734-1736,1872-1874` | Uses `git_remote_normalized` as a boolean "has remote?" gate; session-level, not project-resolution |
| C3 | Handoff create — stores denormalized remote on handoff row | `routes/handoffs.py:851` | `git_remote_normalized=source_session.git_remote_normalized` — denormalized snapshot, unchanged |
| C4 | `_resolve_project_id_for_session` — session FK scan | `routes/sessions.py:1919` | `select(Project).where(git_remote_normalized == git_remote)` — already covered by A1's change |
| C5 | Webhook session matching (GitHub/GitLab PR events) | `routes/webhooks.py:142,344` | `Session.git_remote_normalized == normalized` — filters sessions by repo for PR matching; NOT project resolution. No change needed. |

**D. Client-side sites (HTTP callers — server-side route change covers them; error messages only):**

| # | Site | File:Line | Change |
|---|------|-----------|--------|
| D1 | MCP `_resolve_project_id` | `mcp/server.py:2603-2635` | HTTP `GET /api/v1/projects/{normalized}` — server covers resolution. Update 404 error message to mention `sfs project link-repo`. |
| D2 | MCP `CloudClient.get_project_context` | `mcp/cloud_client.py:93-98` | Same HTTP call pattern. No server-side change needed. |
| D3 | CLI `_resolve_project_id` | `cli/cmd_project.py:1086-1107` | Same HTTP call pattern. Update error message to mention `link-repo`. |
| D4 | CLI `cmd_rules._recent_tool_usage` | `cli/cmd_rules.py:301` | Queries LOCAL SQLite index by remote — session-scoped, not project resolution. No change needed. |

**E. Snapshot/display sites (read `project.git_remote_normalized` for audit/display — use primary remote helper):**

| # | Site | File:Line | Change |
|---|------|-----------|--------|
| E1 | Project transfer snapshot | `routes/project_transfers.py:320` | Read `get_primary_remote(project_id)` instead of `project.git_remote_normalized` |
| E2 | Org member project snapshot | `routes/org_members.py:828` | Same — use `get_primary_remote()` |

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
8. `project_rules` (UNIQUE on `project_id` — see merge matrix §5.12)
9. `knowledge_links`
10. `agent_personas`
11. `tickets`
12. `agent_runs`
13. `retrieval_audit_contexts`
14. `retrieval_audit_events`

### 2.6 The Ambiguity Guard That Becomes an Invariant

`assert_service_key_handoff_boundary` (`handoff_helpers.py:529-544`) currently treats `len(matching) > 1` as a 403 error. With the new global-unique constraint on `project_repos`, this case becomes **provably impossible** — the guard converts from a runtime ambiguity detector into a compile-time invariant backed by the database. The error is kept as defense-in-depth; if it ever fires, a database constraint has been violated.

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
    -- Provider identity for rename survival (v1):
    -- Stable across GitHub renames; NULL for self-hosted/unknown providers.
    provider        VARCHAR(20),          -- e.g. 'github', 'gitlab', 'bitbucket'
    provider_repo_id VARCHAR(100),        -- stable integer-as-string, e.g. GitHub repo ID
    is_primary      BOOLEAN NOT NULL DEFAULT FALSE,
    added_by_user_id VARCHAR(64)
                        REFERENCES users(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Each remote belongs to exactly one project (global uniqueness).
    CONSTRAINT uq_project_repos_remote UNIQUE (git_remote_normalized),

    -- Each provider repo_id belongs to exactly one project (rename survival).
    -- Partial: only enforced when provider_repo_id IS NOT NULL.
    CONSTRAINT uq_project_repos_provider_repo UNIQUE (provider, provider_repo_id)
        -- Implemented as partial unique index in Alembic (see below).
);

-- PostgreSQL:
--   CREATE UNIQUE INDEX uq_project_repos_primary
--       ON project_repos (project_id) WHERE is_primary IS TRUE;
--   CREATE UNIQUE INDEX uq_project_repos_provider_repo
--       ON project_repos (provider, provider_repo_id)
--       WHERE provider_repo_id IS NOT NULL;
--
-- SQLite 3.8.0+ supports partial indexes with the same syntax.
-- Alembic: op.create_index('uq_project_repos_primary', 'project_repos',
--   ['project_id'], unique=True, postgresql_where=text('is_primary IS TRUE'),
--   sqlite_where=text('is_primary IS TRUE'))

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
    provider: Mapped[str | None] = mapped_column(String(20), nullable=True)
    provider_repo_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    added_by_user_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
```

**Partial unique indexes in Alembic (migration 049):**

```python
# One primary per project (partial: WHERE is_primary IS TRUE)
op.create_index(
    "uq_project_repos_primary",
    "project_repos",
    ["project_id"],
    unique=True,
    postgresql_where=sa.text("is_primary IS TRUE"),
    sqlite_where=sa.text("is_primary IS TRUE"),
)

# One project per provider repo_id (partial: WHERE provider_repo_id IS NOT NULL)
op.create_index(
    "uq_project_repos_provider_repo",
    "project_repos",
    ["provider", "provider_repo_id"],
    unique=True,
    postgresql_where=sa.text("provider_repo_id IS NOT NULL"),
    sqlite_where=sa.text("provider_repo_id IS NOT NULL"),
)
```

**Application-level enforcement (defense-in-depth):** Before any `project_repos` write that sets `is_primary=True`, run a SELECT FOR UPDATE on the project's existing repo rows and swap `is_primary=FALSE` on the old primary in the same transaction. For `provider`+`provider_repo_id`, validate uniqueness with a pre-insert SELECT before relying on the partial index alone.

**Why NOT a plain `UNIQUE(project_id, is_primary)`:** That would allow only ONE non-primary repo per project (the tuple `(project_id, FALSE)` could appear once). We need N-1 non-primary repos. The partial index `WHERE is_primary IS TRUE` correctly enforces exactly one primary.

**Decision: Keep `projects.git_remote_normalized`.** This column remains as the project's **primary/display remote** for backward compatibility. It is denormalized from `project_repos WHERE is_primary IS TRUE`. During the backfill migration, every existing project's current `git_remote_normalized` becomes the primary repo. Old clients that query `GET /projects/{remote}` against a non-primary remote will still work via the new resolver (see §3.3). New clients should use project-by-ID endpoints where possible.

### 3.2 Migration (049 — Additive)

**File:** `src/sessionfs/server/db/migrations/versions/049_multi_repo_projects.py`

**Upgrade:**

1. Create `project_repos` table with all columns (including `provider`, `provider_repo_id`), constraints, and indexes
2. Create two partial unique indexes (primary, provider_repo_id) — use cross-DB-safe Alembic with `postgresql_where` + `sqlite_where`
3. Add `merged_into_project_id` (nullable FK to `projects.id`, `ON DELETE SET NULL`) + `merged_at` (nullable TIMESTAMPTZ) to `projects`
4. Create `project_merge_audit` table (see §5.10)
5. Backfill: one row per existing project from `projects.git_remote_normalized` with `is_primary = TRUE`
   ```sql
   INSERT INTO project_repos (id, project_id, git_remote_normalized, is_primary, created_at)
   SELECT gen_random_uuid()::text, id, git_remote_normalized, TRUE, NOW()
   FROM projects
   WHERE git_remote_normalized IS NOT NULL AND git_remote_normalized != '';
   ```
   (Use SQLite-compatible UUID generation via the existing `_gen_id()` utility.)

**Downgrade:** Drop `project_repos` table + `project_merge_audit` table. Drop `merged_into_project_id` + `merged_at` columns from `projects`. No data loss risk — the column on `projects` was never removed.

**Zero downtime:** Additive-only. Old code ignores the new table. New code queries the new table first, falls back to the old column.

### 3.3 Resolution Changes — Every Site

#### New Shared Helper

```python
# src/sessionfs/server/services/project_resolver.py (NEW)

async def resolve_project_by_remote(
    db: AsyncSession,
    git_remote_normalized: str,
    *,
    for_update: bool = False,
    follow_tombstone: bool = True,
) -> Project | None:
    """Resolve a project from a git remote via the project_repos join table.

    Dual-read: project_repos first, then fallback to projects.git_remote_normalized
    for backward compatibility. Tombstone-aware: if the resolved project has
    merged_into_project_id, follows the chain transparently (unless
    follow_tombstone=False, used only by merge endpoint itself).

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

    # Fallback: legacy projects.git_remote_normalized column
    if project is None:
        stmt = select(Project).where(
            Project.git_remote_normalized == git_remote_normalized
        )
        if for_update:
            stmt = stmt.with_for_update()
        result = await db.execute(stmt)
        project = result.scalar_one_or_none()

    # Tombstone redirect: follow merged_into_project_id chain
    if project is not None and follow_tombstone and project.merged_into_project_id:
        project = await db.get(Project, project.merged_into_project_id)
        # Chain-follow: if the target was itself merged, continue
        while project is not None and project.merged_into_project_id:
            project = await db.get(Project, project.merged_into_project_id)

    return project


async def get_primary_remote(db: AsyncSession, project_id: str) -> str | None:
    """Return the primary git_remote_normalized for a project."""
    result = await db.execute(
        select(ProjectRepo.git_remote_normalized)
        .where(ProjectRepo.project_id == project_id, ProjectRepo.is_primary == True)  # noqa: E712
        .limit(1)
    )
    row = result.scalar_one_or_none()
    return row if row else None


async def resolve_project_by_id(
    db: AsyncSession,
    project_id: str,
    *,
    follow_tombstone: bool = True,
) -> Project | None:
    """Get project by ID, optionally following tombstone chain."""
    project = await db.get(Project, project_id)
    if project is not None and follow_tombstone and project.merged_into_project_id:
        project = await db.get(Project, project.merged_into_project_id)
        while project is not None and project.merged_into_project_id:
            project = await db.get(Project, project.merged_into_project_id)
    return project
```

#### Site-by-Site Changes (All 16 Sites)

**Group A — Direct project-resolution sites (10 sites):**

| # | Site | Change |
|---|------|--------|
| A1 | `_resolve_project_id_for_session` (`routes/sessions.py:537-543`) | Replace `select(Project).where(git_remote_normalized == X).with_for_update()` → `await resolve_project_by_remote(db, X, for_update=True)` |
| A2 | `GET /api/v1/projects/{git_remote_normalized:path}` (`routes/projects.py:376-378`) | Replace `scalar_one_or_none()` → `await resolve_project_by_remote(db, X)`. Tombstone-aware: return `410 Gone` with `{"merged_into": target_id}` when resolved project is a tombstone reached via fallback (the primary path already follows the chain). |
| A3 | `PUT /api/v1/projects/{git_remote_normalized:path}/context` (`routes/projects.py:410`) | Same replacement as A2 |
| A4 | `POST /api/v1/projects/` duplicate check (`routes/projects.py:261`) | Extend check: also query `project_repos` for existing link: `select(ProjectRepo).where(git_remote_normalized == X)` |
| A5 | `validate_provenance_for_sender` (`handoff_helpers.py:155-161`) | Replace with `await resolve_project_by_remote(db, session.git_remote_normalized)`. **Also check `session.project_id` first** as authoritative anchor (mirrors A7's R6 pattern). |
| A6 | `validate_attachments` (`handoff_helpers.py:215-221`) | Replace with `await resolve_project_by_remote(db, session.git_remote_normalized)`. Prefer `session.project_id` first. |
| A7 | `assert_service_key_handoff_boundary` legacy fallback (`handoff_helpers.py:521-546`) | Replace lines 521-546 with `await resolve_project_by_remote(db, source_session.git_remote_normalized)`. The `len(matching) > 1` ambiguity check is now unreachable — keep as defense-in-depth assertion. |
| A8 | `_get_project_or_404` remote fallback (`routes/rules.py:145-148`) | Replace `select(Project).where(git_remote_normalized == project_id)` with `await resolve_project_by_remote(db, project_id)`. Tombstone-aware: 410 if resolved-to-tombstone. |
| A9 | Handoff create attachment resolution (`routes/handoffs.py:344-350`) | Replace with `await resolve_project_by_remote(db, session.git_remote_normalized)`. Prefer `session.project_id` first. |
| A10 | Handoff claim persona-only resolution (`routes/handoffs.py:916-928`) | Replace with `await resolve_project_by_remote(db, source_session.git_remote_normalized)`. Prefer `source_session.project_id` first. |

**Group B — Access-predicate sites (6 sites):**

| # | Site | Change |
|---|------|--------|
| B1 | `user_can_access_project` predicate #3 (`auth/project_access.py:55-65`) | Replace `Session.git_remote_normalized == project.git_remote_normalized` with `Session.project_id == project.id`. After multi-repo, `project.git_remote_normalized` is only the primary remote — users with sessions on non-primary repos would falsely fail the check. `Session.project_id` is set at sync time by A1 and is the correct project anchor. |
| B2 | `_accessible_project_ids` path #3 (`handoff_helpers.py:116-126`) | Replace the `join(Session, Session.git_remote_normalized == Project.git_remote_normalized)` with `select(Session.project_id).where(Session.user_id == X, Session.project_id.isnot(None))` — collect distinct `project_id`s the user has sessions for, then UNION with owner/org-member queries. |
| B3 | `_get_project_or_404` access check (`routes/rules.py:153-158`) | Replace `Session.git_remote_normalized == project.git_remote_normalized` with `Session.project_id == project.id` (same pattern as B1). |
| B4 | `list_projects` remote matching (`routes/projects.py:124`) | Replace `Project.git_remote_normalized.in_(user_remotes)` with a subquery through `project_repos`: `Project.id.in_(select(ProjectRepo.project_id).where(ProjectRepo.git_remote_normalized.in_(user_remotes)))`. Also keep the legacy `Project.git_remote_normalized.in_(user_remotes)` as fallback (OR condition). |
| B5 | `list_projects` session count (`routes/projects.py:134-145`) | Replace `select(Session.git_remote_normalized, count).group_by(Session.git_remote_normalized)` with `select(Session.project_id, count).group_by(Session.project_id)` filtered to sessions with non-null project_id. Match counts to projects by `project.id`, not `git_remote_normalized`. |
| B6 | `_check_repo_access` (`routes/projects.py:74-82`) | Replace `Session.git_remote_normalized == git_remote` with: (a) resolve project via `resolve_project_by_remote`, (b) check `Session.project_id == resolved_project.id` OR keep the git_remote match for sessions that predate project linkage. |

**Group C — No change needed (5 sites — see table in §2.2C).**

**Group D — Client-side error messages only (4 sites — see table in §2.2D).**

**Group E — Use `get_primary_remote()` (2 sites — see table in §2.2E).**

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
`provider` and `provider_repo_id` are **server-derived**, not caller-supplied. The server resolves them from the provided `git_remote` using the configured provider (e.g. GitHub App installation context already available server-side). This prevents reservation/DoS attacks where an attacker could link a forged `provider_repo_id` to block a legitimate repo from being linked later (LOW-5). If server-side resolution fails (e.g. self-hosted GitLab, unknown provider), the fields are stored as NULL — the `UNIQUE(git_remote_normalized)` constraint remains the primary uniqueness guard.

**Responses:**
- `201` — linked, returns `ProjectRepoResponse`
- `409` — repo already linked to another project: `{"error": "repo_already_linked", "existing_project_id": "...", "message": "This repo is already linked to project X. Unlink it there first, or merge the projects (see: sfs project merge)."}`
- `403` — not authorized
- `422` — last repo unlink denied (see §7 Q4)

#### `DELETE /api/v1/projects/{project_id}/repos/{repo_id}`
Unlink a repo.

**Authz:** Same as link. Cannot unlink the last repo (see §7 Q4).

#### `GET /api/v1/projects/{project_id}/repos`
List all repos for a project. Returns `ProjectRepoResponse[]` with `is_primary`, `provider`, `provider_repo_id` fields.

#### `POST /api/v1/projects/{project_id}/merge`
Merge another project into this one. See §5 for full design.

#### `GET /api/v1/projects/{git_remote_normalized:path}` (existing — updated)
No API surface change. The resolver now finds the project through `project_repos` (see §3.3 A2). Response still includes `git_remote_normalized` — now the primary remote. Tombstones return `410 Gone`.

### 4.2 CLI Commands

```bash
sfs project link-repo <remote> [--primary] [--project-id <id>]
    # Links a repo to a project. If --project-id is omitted, resolves
    # the current repo's project and links <remote> to it.
    # --primary makes this the display remote.
    # Example: sfs project link-repo github.com/acme/backend.git

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
2. **Dry-run first** — `dry_run=True` (default) performs all validation and reports what WOULD happen without writing. **Provably non-mutating: zero DB rows written.** No audit row, no locks beyond reads. `dry_run=False` executes.
3. **Audit-logged** — every *validated* execute mutation attempt is recorded in `project_merge_audit` (status `started` → `completed`/`failed`). Precondition and authorization rejections (404 not-found, cross-org, already-merged) are refused BEFORE the merge audit row is created and are covered by the standard request/access log, not the merge audit. An ATTEMPT row (status='started') is written in a SEPARATE transaction BEFORE any merge mutation. On success the row is outcome-updated to 'completed'; on failure the exception handler outcome-updates it to 'failed' via a fresh session — the audit row survives rollback of the merge transaction. Dry-run writes nothing. (Security-relevant denials such as cross-org merge attempts MAY additionally be recorded via the existing AdminAction audit path as a Sentinel-driven follow-up; not a v1 requirement.)
4. **NOT undoable automatically** — like `bulk_promote`, the merge is one-way. The dry-run report IS the undo plan.

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
- Cross-org merges are DENIED (data-stays-access-revoked invariant)
- Neither project may already be a tombstone (merged_into_project_id IS NULL on both)

### 5.3 Ticket Model: REASSIGN-IN-PLACE (CEO Decision)

Tickets are reassigned by updating `project_id` directly:

```sql
UPDATE tickets SET project_id = :target_id WHERE project_id = :source_id;
```

**Rationale:** Ticket IDs are globally unique (UUIDs). `depends_on` references are same-project at creation time (validated by `_validate_dependencies_same_project`). After reassignment, all tickets from both projects live under the same `project_id`, so all `depends_on` references remain valid. **No ID remapping, no `merged_from_ticket_id` provenance column, no copy semantics.** This is the simplest correct model.

### 5.4 Persona Handling

The `uq_persona_project_name` constraint (models.py:1220) means two personas with the same `name` cannot coexist in the target project.

**Overarching rule: EVERY source persona is reassigned to the target.** No persona is left on the tombstone project.

**Name constraint:** Persona names MUST match `^[A-Za-z0-9_-]{1,50}$` (`src/sessionfs/server/routes/personas.py:45`). Spaces, parentheses, and other characters are INVALID. The rename slug uses a legal ASCII-safe format.

```python
MAX_PERSONA_NAME = 50
SOURCE_SUFFIX = source_id[:8]  # 8 chars, fits within max

def _legal_rename(base_name: str, suffix: str, seen: set[str]) -> str:
    """Produce a legal, unique, ≤50-char rename. Truncates base if needed."""
    # Reserve space for '-' + suffix
    max_base = MAX_PERSONA_NAME - len(suffix) - 1
    truncated = base_name[:max_base]
    candidate = f"{truncated}-{suffix}"
    # If collision with existing (should be near-impossible with 8-char suffix),
    # append incrementing counter
    attempt = candidate
    i = 1
    while attempt in seen:
        attempt = f"{candidate[:MAX_PERSONA_NAME - 2]}-{i}"
        i += 1
    return attempt


async def _apply_persona_policy(db, source_id, target_id, policy):
    """Reassign ALL source personas to target, handling collisions per policy."""
    source_personas = (await db.execute(
        select(AgentPersona).where(AgentPersona.project_id == source_id)
    )).scalars().all()

    target_names = set((await db.execute(
        select(AgentPersona.name).where(AgentPersona.project_id == target_id)
    )).scalars().all())

    renames = []       # [{old_name, new_name, display_note}]
    for persona in source_personas:
        if persona.name in target_names:
            if policy == "rename":
                new_name = _legal_rename(persona.name, SOURCE_SUFFIX, target_names)
                renames.append({
                    "old_name": persona.name,
                    "new_name": new_name,
                    "display_note": f"Renamed from '{persona.name}' "
                                    f"(source project {source_id[:8]}) — "
                                    f"collided with target's persona of same name.",
                })
                persona.name = new_name
                target_names.add(new_name)  # guard against rename-rename collision
            elif policy == "skip":
                # Rename to legal archived unique name before reassign
                # to avoid uq_persona_project_name collision with target's
                # same-named persona. No tombstone stranding.
                archived_name = _legal_rename(
                    persona.name, f"{SOURCE_SUFFIX}-archived", target_names
                )
                renames.append({
                    "old_name": persona.name,
                    "new_name": archived_name,
                    "display_note": f"Archived '{persona.name}' "
                                    f"(source project {source_id[:8]}) — "
                                    f"skipped due to collision with target's "
                                    f"persona of same name.",
                })
                persona.name = archived_name
                persona.is_active = False
                target_names.add(archived_name)
            elif policy == "merge_content":
                target_p = (await db.execute(
                    select(AgentPersona).where(
                        AgentPersona.project_id == target_id,
                        AgentPersona.name == persona.name,
                    )
                )).scalar_one()
                target_p.content = (
                    f"{target_p.content}\n\n"
                    f"--- merged from project {source_id[:8]} ---\n"
                    f"{persona.content}"
                )
                # Rename source to legal archived unique name before reassign
                # to avoid uq_persona_project_name collision.
                archived_name = _legal_rename(
                    persona.name, f"{SOURCE_SUFFIX}-archived", target_names
                )
                renames.append({
                    "old_name": persona.name,
                    "new_name": archived_name,
                    "display_note": f"Archived '{persona.name}' "
                                    f"(source project {source_id[:8]}) — "
                                    f"content merged into target's persona "
                                    f"of same name.",
                })
                persona.name = archived_name
                persona.is_active = False
                target_names.add(archived_name)
        # REASSIGN — every source persona moves to target (with unique name)
        persona.project_id = target_id

    return renames
```

**Policy (Compass recommendation — apply as default):** Keep target's persona name. For `rename` policy, rename colliding source personas to `{name}-{src8}` (e.g. `prism-a1b2c3d4`), a legal slug ≤50 chars. For `skip` and `merge_content` policies, the source persona is renamed to `{name}-{src8}-archived` BEFORE reassign — this avoids the `uq_persona_project_name` violation (same-named persona already exists in target) while keeping the persona on the target project (no tombstone stranding). The human-readable explanation lives in the audit row's `persona_renames` JSON (`display_note` field). Users can override with the CLI `--interactive` flag at merge time. The merge audit records all renames.

### 5.5 Knowledge Entry Dedup + KnowledgeLink Rewriting

**KnowledgeEntry dedup:** Best-effort exact-match dedup (no LLM — house rules: no server-side LLM keys).

```python
async def _dedupe_knowledge_entries(db, source_id, target_id):
    """Build an entry-ID mapping, skip exact dupes, return the map.
    
    Returns a dict: source_entry_id → target_equivalent_id.
    Reassigned (non-dup) entries map to themselves (identity).
    """
    target_entries = (await db.execute(
        select(KnowledgeEntry.entry_type, KnowledgeEntry.content, KnowledgeEntry.id)
        .where(KnowledgeEntry.project_id == target_id)
    )).all()
    target_keys = {
        (e.entry_type, _normalize_content(e.content)): e.id
        for e in target_entries
    }

    source_entries = (await db.execute(
        select(KnowledgeEntry).where(KnowledgeEntry.project_id == source_id)
    )).scalars().all()

    entry_id_map = {}  # source_id → target_id (or self for reassigned)
    skipped_ids = []
    for entry in source_entries:
        key = (entry.entry_type, _normalize_content(entry.content))
        if key in target_keys:
            entry_id_map[entry.id] = target_keys[key]  # redirect to target equivalent
            skipped_ids.append(entry.id)
            # Do NOT reassign — it's a duplicate
        else:
            entry.project_id = target_id  # reassign unique entry
            entry_id_map[entry.id] = entry.id  # identity mapping (stable ID)

    return entry_id_map, skipped_ids
```

**KnowledgeLink handling (MED-4 fix):** `KnowledgeLink` has a uniqueness constraint `uq_kl_link` on `(project_id, source_type, source_id, target_type, target_id)` (`models.py:1178`). Straight reassign would violate this if the target already has the same link. And links referencing a skipped (deduped) source entry would dangle.

**Fix — map + dedup + reassign:**

```python
async def _reassign_knowledge_links(db, source_id, target_id, entry_id_map):
    """Reassign source links, rewriting IDs through the entry map and
    merging duplicates with target links.

    COMPUTE BEFORE MUTATE: the remapped key is computed FIRST from the
    entry_id_map WITHOUT mutating the ORM row. Duplicates are db.delete()'d.
    Only non-duplicate rows are mutated and flushed — this avoids the
    uq_kl_link violation that a mutate-then-check-then-flush pattern would
    cause (the ORM would still flush the already-mutated duplicate row).
    """
    source_links = (await db.execute(
        select(KnowledgeLink).where(KnowledgeLink.project_id == source_id)
    )).scalars().all()

    # Fetch existing target links for dedup
    target_links = (await db.execute(
        select(KnowledgeLink).where(KnowledgeLink.project_id == target_id)
    )).scalars().all()
    running_keys = {
        (lk.source_type, lk.source_id, lk.target_type, lk.target_id)
        for lk in target_links
    }

    skipped_link_ids = []
    for link in source_links:
        # Compute remapped key BEFORE mutating the row
        new_source_id = entry_id_map.get(link.source_id, link.source_id)
        new_target_id = entry_id_map.get(link.target_id, link.target_id)
        key = (link.source_type, new_source_id, link.target_type, new_target_id)

        if key in running_keys:
            # Duplicate — delete the source link, do NOT mutate+flush it
            skipped_link_ids.append(link.id)
            await db.delete(link)
        else:
            # Non-duplicate — safe to mutate and flush
            running_keys.add(key)
            link.project_id = target_id
            link.source_id = new_source_id
            link.target_id = new_target_id

    return skipped_link_ids
```

The entry-ID map is built during KnowledgeEntry dedup (§5.5 step) and fed to link reassignment. Links referencing skipped entries are rewritten to point at the target's equivalent entry; links that would duplicate an existing target link are dropped.

### 5.6 ProjectRules Conflict

`ProjectRules` has `unique=True` on `project_id` (models.py:1086). Only one rules row per project.

**Policy (with Codex LOW-9 fix):**

| Source has rules? | Target has rules? | Action |
|-------------------|-------------------|--------|
| YES | YES | **Conflict.** Target rules WIN. Source rules archived as `_merged_rules_{source_id[:8]}` wiki page. |
| YES | NO | **Promote.** Reassign source rules to target (`UPDATE project_rules SET project_id = :target_id WHERE project_id = :source_id`). |
| NO | YES | Nothing to do. Target already has rules. |
| NO | NO | Nothing to do. |

### 5.7 Wiki Page Slug Collisions

If both projects have a page at slug `architecture`, the source page is renamed to a legal slug (e.g. `architecture-{source_id[:8]}`). The merge audit records the rename.

**Page/revision relationship:** `KnowledgePage` and `WikiPageRevision` are linked by `(project_id, page_slug)` — there is NO `page_id` FK (`models.py:1054-1058`). When a slug is renamed, the revisions must follow atomically:

```python
async def _apply_slug_renames(db, source_id, target_id, slug_collisions):
    """Rename colliding source pages + reassign their revisions."""
    renames = []
    for old_slug, new_slug in slug_collisions:
        # 1. Rename the KnowledgePage row
        await db.execute(
            update(KnowledgePage)
            .where(KnowledgePage.project_id == source_id, KnowledgePage.slug == old_slug)
            .values(slug=new_slug, project_id=target_id)
        )
        # 2. Re-point ALL revisions for this slug (project_id + page_slug)
        await db.execute(
            update(WikiPageRevision)
            .where(WikiPageRevision.project_id == source_id,
                   WikiPageRevision.page_slug == old_slug)
            .values(project_id=target_id, page_slug=new_slug)
        )
        renames.append({"old_slug": old_slug, "new_slug": new_slug})

    # 3. Reassign NON-colliding pages + their revisions
    await db.execute(
        update(KnowledgePage)
        .where(KnowledgePage.project_id == source_id)
        .values(project_id=target_id)
    )
    await db.execute(
        update(WikiPageRevision)
        .where(WikiPageRevision.project_id == source_id)
        .values(project_id=target_id)
    )

    return renames
```

**Revision-number uniqueness:** `uq_wiki_revisions_number` is `(project_id, page_slug, revision_number)` (`models.py:1046-1050`). After reassigning source revisions to the target project with a NEW slug (via rename), there is zero risk of collision because the target project has no revisions for that slug yet. The source-only slug case (no collision) is safe because `revision_number` sequences are per-page and the source page didn't exist in the target. **No renumbering needed** — uniqueness is preserved by the slug being distinct.

### 5.8 Repo Reassignment (with HIGH-1 Fix — Primary Demotion Order)

**The problem:** If both projects have an `is_primary=true` repo row, reassigning source repos to target BEFORE demoting the source primary violates the partial unique index `WHERE is_primary IS TRUE`.

**The fix — ordered write sequence within the merge transaction:**

1. **Lock** both projects' `project_repos` rows with `SELECT ... FOR UPDATE`
2. **Demote source primary first:** `UPDATE project_repos SET is_primary = FALSE WHERE project_id = :source_id AND is_primary IS TRUE`
3. **Reassign all source repos:** `UPDATE project_repos SET project_id = :target_id WHERE project_id = :source_id`
   - If the target already has a primary, the source's ex-primary stays non-primary (demoted in step 2)
   - If the target has NO primary (edge case: all target repos unlinked?), the source's ex-primary becomes the target's effective primary. Promote it: pick the oldest reassigned repo and set `is_primary = TRUE`
4. **Single commit**

### 5.9 Source Project Tombstone

```python
# Add to Project model:
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
- Access to the tombstone is read-only for audit (original owner)
- Resolvers transparently follow the tombstone chain (see `resolve_project_by_remote`)

### 5.10 Merge Audit Table (LOW-8 Fix: cross-DB Text JSON)

```sql
CREATE TABLE project_merge_audit (
    id                  VARCHAR(64) PRIMARY KEY,
    source_project_id   VARCHAR(64),   -- nullable after project delete
    target_project_id   VARCHAR(64),
    initiated_by_user_id VARCHAR(64) REFERENCES users(id) ON DELETE SET NULL,
    dry_run             BOOLEAN NOT NULL DEFAULT TRUE,
    status              VARCHAR(20) NOT NULL DEFAULT 'completed',
                        -- 'started' | 'completed' | 'failed'
                        -- 'started' = merge in progress (attempt row written
                        --   in a separate tx before mutation begins).
                        -- 'completed' = merge succeeded (outcome update).
                        -- 'failed' = merge failed (outcome update from
                        --   exception handler; survives rollback).
    persona_policy      VARCHAR(20) NOT NULL,  -- 'rename' | 'skip' | 'merge_content'
    stats               TEXT NOT NULL DEFAULT '{}',          -- JSON object
    persona_renames     TEXT NOT NULL DEFAULT '[]',          -- JSON array
    slug_renames        TEXT NOT NULL DEFAULT '[]',          -- JSON array
    skipped_ke_ids      TEXT NOT NULL DEFAULT '[]',          -- JSON array
    skipped_link_ids    TEXT NOT NULL DEFAULT '[]',          -- JSON array
    rules_action        VARCHAR(20),  -- 'archived' | 'promoted' | 'none'
    error_message       TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

House convention: `Text` columns with `NOT NULL DEFAULT '{}'/'[]'` for JSON payloads, matching existing patterns (`agent_runs.findings`, `ticket_edits.old_value/new_value`).

### 5.11 Per-Table Merge Matrix

This is the single source of truth for the merge implementer. The transaction pseudocode in §5.12 follows this order exactly.

**Ordering is explicit:** Precondition checks (Phase 1 — project existence, org match, not-already-merged) come FIRST and are NOT merge-audited. These rejections (404/400) are covered by standard request/access logging. The `project_merge_audit` `started` row is written only AFTER preconditions pass and a real mutation is about to begin (Phase 2). The merge matrix steps below (1–15) execute within the merge transaction (Phase 3).

| Table | Mutation | Order | Uniqueness at risk | Collision behavior | Rollback/audit note |
|-------|----------|-------|--------------------|--------------------|---------------------|
| `project_repos` | **Demote primary** (source), then **reassign** all to target | 1 | `uq_project_repos_primary` partial index (source + target primaries collide) | Demote source primary BEFORE reassign. If target has no primary, promote oldest source repo. | Lock rows with FOR UPDATE first. |
| `agent_personas` | **Reassign** all to target | 2 | `uq_persona_project_name` (project_id, name) | Collision: rename source to `{name}-{src8}` (legal ASCII slug ≤50 chars; human-readable note in audit). Also: skip, merge_content. In-flight guard: each rename added to `target_names` set. | Records renames in `persona_renames` (old_name, new_name, display_note). |
| `project_rules` | **Promote** if target none; **archive** source + keep target if both exist | 3 | `project_rules.project_id` UNIQUE | Target wins on conflict. Source archived as `_merged_rules_{source_id[:8]}` wiki page. | Records action in `rules_action`. |
| `knowledge_pages` | **Reassign**; rename slug collisions to `{slug}-{src8}` | 4 | `knowledge_pages` slug uniqueness within project | Rename source slug. Revisions follow atomically in the same step (linked by `project_id`+`page_slug`, not a `page_id` FK). | Records renames in `slug_renames`. |
| `wiki_page_revisions` | **Reassign** (handled atomically WITH page slug rename — NOT a separate step) | 5 | `uq_wiki_revisions_number` (project_id, page_slug, revision_number) | No renumbering needed: renamed pages get a unique slug, non-colliding pages get unique project_id. | Audit via slug_renames; revisions are FK'd to pages by (project_id, page_slug). |
| `knowledge_entries` | **Reassign** unique; **skip** exact duplicates | 6 | None (entries are FK'd to project, no name uniqueness) | Exact (entry_type, normalized_content) match → skip. No LLM semantic dedup. | Records skipped IDs in `skipped_ke_ids`. |
| `knowledge_links` | **Map + dedup + reassign** (not straight reassign) | 7 | `uq_kl_link` (project_id, source_type, source_id, target_type, target_id) | Rewrite source_id/target_id through entry-ID map from KE dedup step. Skip links that duplicate an existing target link. In-flight guard prevents self-collision. | Records skipped IDs in `skipped_link_ids`. |
| `tickets` | **Reassign-in-place** | 8 | None (ticket IDs globally unique) | No collision possible. `depends_on` stays valid (same-project validation at creation). | CEO decision: reassign, not copy. |
| `agent_runs` | **Reassign** | 9 | None | Straight reassign. `persona_name`/`ticket_id` are plain strings, survive unchanged. | |
| `sessions` | **Reassign** | 10 | None (nullable FK, ON DELETE SET NULL) | Straight reassign. Sessions keep original `git_remote_normalized` tag (denormalized). | |
| `handoff_attachments` | **Reassign** | 11 | None | Straight reassign. | |
| `project_transfers` | **Reassign** | 12 | None | Straight reassign. | |
| `context_compilations` | **Reassign** | 13 | None | Straight reassign. | |
| `retrieval_audit_contexts` | **Reassign** | 14 | None | Straight reassign. | |
| `retrieval_audit_events` | **Reassign** | 15 | None | Straight reassign. | |

### 5.12 Transaction Pseudocode (matches merge matrix order)

```python
async def merge_projects(
    db: AsyncSession,           # primary session for reads + mutations
    source_id: str,
    target_id: str,
    user_id: str,
    dry_run: bool,
    persona_policy: str,
    session_factory,            # factory for fresh sessions (audit survival)
):
    # ==================================================================
    # Phase 1: READ — validate preconditions (all in same transaction)
    # ==================================================================
    source = await db.get(Project, source_id)
    target = await db.get(Project, target_id)
    if not source or not target:
        raise HTTPException(404)
    if source.org_id != target.org_id:
        raise HTTPException(400, "Cross-org merges are not supported. Transfer first.")
    if source.merged_into_project_id is not None:
        raise HTTPException(400, "Source project was already merged")
    if target.merged_into_project_id is not None:
        raise HTTPException(400, "Target project was already merged")

    # Pre-compute collisions
    persona_collisions = await _detect_persona_collisions(db, source_id, target_id)
    slug_collisions = await _detect_slug_collisions(db, source_id, target_id)
    ke_duplicates = await _detect_ke_duplicates(db, source_id, target_id)
    source_has_rules = await _has_rules(db, source_id)
    target_has_rules = await _has_rules(db, target_id)

    stats = await _compute_merge_stats(db, source_id, target_id,
        persona_collisions, slug_collisions, ke_duplicates,
        source_has_rules, target_has_rules)

    if dry_run:
        # Dry-run: ZERO DB writes. Validate + return stats only.
        # No audit row, no locks beyond reads.
        return {"dry_run": True, "stats": stats,
                "persona_collisions": persona_collisions,
                "slug_collisions": slug_collisions,
                "ke_duplicates": ke_duplicates}

    # ==================================================================
    # Phase 2: WRITE ATTEMPT AUDIT ROW (separate transaction — survives
    #          rollback of the merge transaction below)
    # ==================================================================
    audit_id = generate_id()
    async with session_factory() as audit_db:
        async with audit_db.begin():
            audit = ProjectMergeAudit(
                id=audit_id,
                source_project_id=source_id,
                target_project_id=target_id,
                initiated_by_user_id=user_id,
                dry_run=False,
                status="started",
                persona_policy=persona_policy,
                stats=json.dumps(stats),
                persona_renames="[]",   # populated on outcome
                slug_renames="[]",
                skipped_ke_ids="[]",
                skipped_link_ids="[]",
                rules_action=None,
            )
            audit_db.add(audit)
        # Committed immediately — durable before mutation begins

    # ==================================================================
    # Phase 3: MERGE MUTATIONS — single atomic transaction
    # ==================================================================
    persona_renames = []
    slug_renames = []
    skipped_ke_ids = []
    skipped_link_ids = []
    rules_action = "none"

    try:
        async with db.begin():
            # ORDER MATTERS — follows merge matrix exactly:

            # Step 1: PROJECT_REPOS — lock + demote source primary + reassign
            await db.execute(
                select(ProjectRepo).where(
                    ProjectRepo.project_id.in_([source_id, target_id])
                ).with_for_update()
            )
            await db.execute(
                update(ProjectRepo).where(
                    ProjectRepo.project_id == source_id,
                    ProjectRepo.is_primary == True  # noqa: E712
                ).values(is_primary=False)
            )
            await db.execute(
                update(ProjectRepo).where(ProjectRepo.project_id == source_id)
                .values(project_id=target_id)
            )
            target_primary = await db.execute(
                select(ProjectRepo).where(
                    ProjectRepo.project_id == target_id,
                    ProjectRepo.is_primary == True  # noqa: E712
                )
            ).scalar_one_or_none()
            if not target_primary:
                oldest = await db.execute(
                    select(ProjectRepo).where(ProjectRepo.project_id == target_id)
                    .order_by(ProjectRepo.created_at.asc()).limit(1)
                ).scalar_one_or_none()
                if oldest:
                    oldest.is_primary = True

            # Step 2: AGENT_PERSONAS — reassign ALL, dedupe collisions
            persona_renames = await _apply_persona_policy(
                db, source_id, target_id, persona_policy
            )

            # Step 3: PROJECT_RULES — promote or archive
            rules_action = "none"
            source_rules = await db.execute(
                select(ProjectRules).where(ProjectRules.project_id == source_id)
            ).scalar_one_or_none()
            if source_rules:
                if target_has_rules:
                    await _archive_rules_as_wiki_page(
                        db, source_rules, target_id, source_id
                    )
                    await db.delete(source_rules)
                    rules_action = "archived"
                else:
                    source_rules.project_id = target_id
                    rules_action = "promoted"

            # Step 4–5: KNOWLEDGE_PAGES + wiki_page_revisions (atomically)
            slug_renames = await _apply_slug_renames(
                db, source_id, target_id, slug_collisions
            )

            # Step 6: KNOWLEDGE_ENTRIES — dedup + build entry-ID map
            entry_id_map, skipped_ke_ids = await _dedupe_knowledge_entries(
                db, source_id, target_id
            )

            # Step 7: KNOWLEDGE_LINKS — map + dedup + reassign (compute before mutate)
            skipped_link_ids = await _reassign_knowledge_links(
                db, source_id, target_id, entry_id_map
            )

            # Steps 8-15: Remaining tables — straight reassign in order
            for model in [Ticket, AgentRun, SessionModel, HandoffAttachment,
                           ProjectTransfer, ContextCompilation,
                           RetrievalAuditContext, RetrievalAuditEvent]:
                await db.execute(
                    update(model).where(model.project_id == source_id)
                    .values(project_id=target_id)
                )

            # Step 16: Mark source as tombstone
            source.merged_into_project_id = target_id
            source.merged_at = func.now()

            # Step 17: Catch-up UPDATE for concurrent-sync race
            await db.execute(
                update(SessionModel).where(SessionModel.project_id == source_id)
                .values(project_id=target_id)
            )

        # Merge transaction committed — all-or-nothing success

        # ==================================================================
        # Phase 4: OUTCOME UPDATE — mark audit row 'completed' (fresh session)
        # ==================================================================
        async with session_factory() as audit_db:
            async with audit_db.begin():
                result = await audit_db.execute(
                    update(ProjectMergeAudit)
                    .where(ProjectMergeAudit.id == audit_id)
                    .values(
                        status="completed",
                        persona_renames=json.dumps(persona_renames),
                        slug_renames=json.dumps(slug_renames),
                        skipped_ke_ids=json.dumps(skipped_ke_ids),
                        skipped_link_ids=json.dumps(skipped_link_ids),
                        rules_action=rules_action,
                    )
                )
                if result.rowcount == 0:
                    # Audit row missing — should never happen, but don't crash
                    pass

        return {"dry_run": False, "stats": stats, "audit_id": audit_id}

    except Exception as exc:
        # ==================================================================
        # Phase 4 (failure path): OUTCOME UPDATE — mark audit row 'failed'
        # Fresh session survives the rolled-back merge transaction.
        # ==================================================================
        async with session_factory() as audit_db:
            async with audit_db.begin():
                await audit_db.execute(
                    update(ProjectMergeAudit)
                    .where(ProjectMergeAudit.id == audit_id)
                    .values(
                        status="failed",
                        error_message=f"{type(exc).__name__}: {exc}",
                    )
                )
        raise  # re-raise after audit update
```

### 5.13 Rollback + Race Guarantee

- **Atomicity:** Single `async with db.begin()` block. Any step failure → full rollback. Both projects untouched.
- **Audit survival (MED-3 fix):** The ATTEMPT audit row is written in a SEPARATE transaction (Phase 2) AFTER precondition validation passes but BEFORE the merge mutation begins. On success, the row is outcome-updated to `status='completed'` (Phase 4). On failure, the exception handler outcome-updates it to `status='failed'` via a FRESH session that is unaffected by the merge transaction's rollback. This guarantees every *validated* execute attempt — successful or failed — leaves a durable audit trail. Precondition and authorization rejections (404, cross-org, already-merged) are refused before the audit row exists and are covered by standard request/access logging. Dry-run writes nothing.
- **Concurrent-sync race (HIGH-3 fix):**
  - (a) Step 17 (catch-up UPDATE) catches sessions that landed on `source_id` during the merge window
  - (b) `resolve_project_by_remote()` is tombstone-aware — transparently follows `merged_into_project_id` chain, so any post-merge session sync that resolves via a source-project remote lands on the target
  - (c) `resolve_project_by_id()` (new helper, see §3.3) also follows the chain for project-by-ID lookups
  - (d) Residual window: a session synced AFTER step 17 but BEFORE the tombstone is committed (impossible — same transaction). If a session somehow has `project_id=source_id` after merge completes, the FK (`ON DELETE SET NULL`) does NOT fire because the source project is a tombstone, not deleted. The tombstone redirect in the resolver closes this on the NEXT lookup. This window is acceptably small and self-healing.
- **NOT relying on FK cleanup:** Appendix B.2's original claim that `ON DELETE SET NULL` would clean up stranded sessions was wrong — tombstones are never deleted. The catch-up UPDATE + tombstone-aware resolvers are the correct fix.

---

## 6. Authz / Security Implications

### 6.1 Who Can Link a Repo

1. **Project owner** — always
2. **Org admin** (for org-scoped projects) — always
3. **Repo-access check:** The linking user must have captured at least one session on the target remote, OR be the project owner / org admin.

### 6.2 Repo Hijacking Prevention

The global `UNIQUE(git_remote_normalized)` on `project_repos` + `UNIQUE(provider, provider_repo_id)` partial index prevent double-linking at the database level. `provider_repo_id` is server-derived (not caller-supplied), preventing reservation/DoS attacks where an attacker could link a forged ID to block a legitimate repo. The provider-ID constraint additionally catches rename-bypass attempts (renaming a repo on GitHub → different normalized remote → same provider_repo_id → UNIQUE violation).

### 6.3 Cross-Org Boundary

- A repo linked to an org-scoped project is "owned" by that org
- Linking the same repo to a different org's project is rejected (409)
- Merging projects across orgs is DENIED (400)
- Service keys: the `assert_service_key_can_access_project` check in `handoff_helpers.py:561-564` validates project-level access. With multi-repo, the project boundary is unchanged — service keys get access to ALL repos under a project.

### 6.4 Who Can Merge Projects

Both projects must be owned by the same entity:
- **Both personal:** Caller must own both
- **Both same org:** Caller must be org admin of that org
- **Mixed (personal + org):** DENIED. Use project transfer first, then merge.

### 6.5 Sentinel Pre-Build Security Pass

Before any implementation code is written, Sentinel must review:
1. Repo hijacking vector (linking a repo you don't control)
2. Cross-org repo linking bypass via `provider_repo_id`
3. Merge authz (can a non-owner merge two projects?)
4. Service key behavior post-merge (scoped `project_ids` — see Appendix B.1)
5. Audit trail completeness (can a malicious merge be detected post-hoc?)
6. Tombstone project access (can the old owner still read the tombstone?)
7. Tombstone redirect — does it leak the target project ID to unauthorized users on `GET /projects/{remote}`?

---

## 7. Open Decisions

### Q1: Each repo belongs to exactly one project — confirm?

**Recommendation: YES.** Enforced by `UNIQUE(git_remote_normalized)` + `UNIQUE(provider, provider_repo_id)` on `project_repos`.

### Q2: Persona collision policy + Rules collision policy

**Resolved by Compass companion (§3):** Keep target, rename source to `{name}-{src8}` (legal ASCII slug ≤50 chars; e.g. `prism-a1b2c3d4`). Human-readable "(from <project>)" context lives only in the audit row's `persona_renames.display_note` JSON. Rules: keep target, archive source as wiki page snapshot (promote source when target has none — LOW-9). User can override with `--interactive` at merge time.

### Q3: Is multi-repo / merge a tiered feature?

**RESOLVED — FREE for all tiers (CEO decision, 2026-06-15).** No tier plumbing. No `multi_repo_projects` feature gate. No `check_feature()` call. Ownership/org authz only.

### Q4: Must a project have at least one repo?

**Recommendation: YES.** Enforcement: `DELETE /repos/{id}` returns 422 on last repo. Merge is the exception — the source becomes a tombstone with zero repos (explicitly marked via `merged_into_project_id`).

### Q5: Should `projects.git_remote_normalized` ever be dropped?

**Recommendation: NOT YET.** Keep for at least two releases as a denormalized primary-remote cache. v0.12+ cleanup ticket.

---

## 8. Review Checklists

### 8.1 Codex Review Checklist

- [ ] Schema: `project_repos` table — are constraints correct? Partial unique indexes for both PG and SQLite?
- [ ] `provider` + `provider_repo_id`: nullable correctly? UNIQUE partial index handles NULLs?
- [ ] Migration: backfill correct? Any edge case with empty `git_remote_normalized`?
- [ ] Resolver: all 16 sites changed correctly? Any missed?
- [ ] Tombstone-aware resolvers: does `resolve_project_by_remote` follow chains correctly? Cycle detection? (A→B→A is prevented by precondition check, but defense-in-depth?)
- [ ] Dual-read fallback: `project_repos` checked FIRST (no shadowing window)
- [ ] Merge: write order follows merge matrix (primary demotion BEFORE reassign)
- [ ] Merge: ALL personas reassigned (HIGH-2 — no stranded personas)
- [ ] Merge: catch-up UPDATE for sessions (HIGH-3 — no sessions stranded on tombstone)
- [ ] Merge: ticket `depends_on` — reassign-in-place means no rewriting needed. Confirm same-project validation at creation guarantees this.
- [ ] Merge: `project_rules` promotion when target has none (LOW-9)
- [ ] Merge: persona dedup — what if both projects have same-named persona AND both are referenced by active tickets?
- [ ] Tombstone: `GET /projects/{id}` for merged project — 410 response doesn't leak target ID to unauthorized users?
- [ ] CLI: error messages guide users to `link-repo` when a remote isn't found?
- [ ] Service keys: scoped `project_ids` post-merge (Appendix B.1)
- [ ] Race: two users simultaneously linking same remote to different projects? (UNIQUE constraint + 409)
- [ ] Race: merge + concurrent session sync? (Catch-up UPDATE + tombstone redirect close the window.)
- [ ] Dry-run: provably zero DB writes? No audit row, no locks beyond reads? (MED-1)
- [ ] Persona rename: are generated names legal under `^[A-Za-z0-9_-]{1,50}$`? In-flight collision guard against rename-rename collision? (MED-2)
- [ ] Wiki revisions: are they reassigned by `(project_id, page_slug)` not a nonexistent `page_id` FK? Is `uq_wiki_revisions_number` preserved after reassign? (MED-3)
- [ ] KnowledgeLink: is entry-ID mapping built before link reassignment? Are duplicates detected and skipped? Does the in-flight dedup guard prevent self-collision within the batch? (MED-4)
- [ ] `provider_repo_id`: is it server-derived from git_remote, never trusted from caller input? (LOW-5)

### 8.2 Shield-SR Security Review Checklist

- [ ] Repo hijacking: can a user link a repo they don't have access to?
- [ ] Provider-repo-ID injection: is the server-derived path watertight? Can a caller forge provider metadata to DoS/reserve a legitimate repo? (LOW-5: server resolves provider_repo_id from git_remote; caller input is ignored.)
- [ ] Cross-org linking: UNIQUE constraint prevents DB-level double-link, but is app-layer check watertight?
- [ ] Merge authz: can a non-owner merge two projects? Org-membership edge cases?
- [ ] Service key project boundary: `assert_service_key_handoff_boundary` correct with multi-repo?
- [ ] Tombstone data leak: can a former org member read a tombstone they once had access to?
- [ ] Tombstone redirect: does resolving by a tombstone's remote leak the target project's existence/info?
- [ ] Audit completeness: every merge mutation recorded in `project_merge_audit`?
- [ ] Migration safety: backfill handles all existing rows? Downgrade clean?
- [ ] Circular merge prevention: preconditions enforce `merged_into_project_id IS NULL` on both source and target?

---

## 9. Test Plan (Design-Time — Implement at Build)

These tests MUST be written alongside the implementation, keyed to the merge matrix (§5.11). One test class per merge step.

### 9.1 Project Repos (Step 1)
- [ ] Primary demotion: source+target both have primary → demote fires first → reassign succeeds → only target primary remains
- [ ] Primary promotion: target has no primary → oldest source repo promoted to primary after reassign
- [ ] Last-repo guard: unlinking last repo → 422
- [ ] provider_repo_id uniqueness: linking same (provider, repo_id) where both non-null → IntegrityError / app-level guard

### 9.2 Personas (Step 2)
- [ ] Unique names: all source personas reassigned, no name collisions with target → success
- [ ] Name collision — rename: source "atlas" collides → renamed to `atlas-a1b2c3d4` (legal regex) → target now has both
- [ ] Name collision — truncation: persona name is 50 chars → truncated to 41 + `-` + 8-char suffix = 50 → passes regex
- [ ] Name collision — rename-rename: two source personas both collide AND would produce the same rename slug → suffix counter increments → both get unique names
- [ ] Name collision — skip: source persona is_active=False after merge, not visible in target
- [ ] Name collision — merge_content: source persona content appended to target persona content
- [ ] ALL source personas reassigned: zero personas remain on source project_id after merge

### 9.3 ProjectRules (Step 3)
- [ ] Source+target both have rules → source archived as wiki page, target kept
- [ ] Source has rules, target none → source reassigned (promoted)
- [ ] Neither has rules → no change

### 9.4 Knowledge Pages + Wiki Revisions (Steps 4-5)
- [ ] Slug collision: source "architecture" collides → renamed to `architecture-a1b2c3d4` → revisions follow with new `(project_id, page_slug)`
- [ ] No collision: all pages + revisions reassigned, `uq_wiki_revisions_number` preserved
- [ ] Revision numbering: source page with 5 revisions, no target conflict → all 5 reassigned, (project_id, page_slug, rev_N) tuples unique

### 9.5 Knowledge Entries (Step 6)
- [ ] Exact duplicate: same (entry_type, normalized_content) → source skipped, target equivalent ID recorded in entry_id_map
- [ ] Near-duplicate (not exact): different whitespace → both kept (no semantic dedup)
- [ ] Unique: source entry with no match → reassigned, identity-mapped in entry_id_map

### 9.6 Knowledge Links (Step 7)
- [ ] Reference to deduped entry: link.source_id is a skipped entry → rewritten to target's equivalent entry ID via entry_id_map
- [ ] Duplicate link: source link matches existing target link → skipped
- [ ] Self-collision guard: two source links would become duplicates of each other after rewrite → second one skipped
- [ ] Both source_id and target_id rewritten through map: verify both directions

### 9.7 Tickets (Step 8)
- [ ] All tickets reassigned: UPDATE tickets SET project_id=target → success
- [ ] depends_on validity: ticket with depends_on referencing another source ticket → both reassigned → reference still valid (same project now)
- [ ] Cross-project depends_on: confirm it CANNOT exist today (enforced at creation)

### 9.8 Sessions + Catch-up (Steps 10, 17)
- [ ] Concurrent session sync: simulate session landing on source_id during merge → step 17 catch-up catches it → session ends up on target
- [ ] Tombstone redirect: after merge, resolve_project_by_remote(source_remote) → returns target project (not tombstone)

### 9.9 Merge Transaction Integrity
- [ ] Atomic rollback: inject failure mid-merge → both projects untouched, zero rows changed
- [ ] Dry-run: call with dry_run=True → zero DB writes, no audit row, stats returned correctly
- [ ] Double-merge guard: attempt to merge already-merged project → 400

### 9.10 Endpoint Tests
- [ ] POST link-repo: success, 409 (already linked), 403 (not authorized), 422 (cross-org)
- [ ] DELETE unlink-repo: success, 422 (last repo)
- [ ] GET repos: returns list with is_primary, provider, provider_repo_id
- [ ] GET /projects/{remote}: resolves through project_repos, tombstone-aware (410 for merged)
- [ ] POST merge: dry-run returns stats, execute returns audit_id, 400 cross-org, 400 already-merged

---

## 10. Implementation Order

1. **Migration 049** — `project_repos` + partial indexes + `merged_into_project_id` on projects + `project_merge_audit`
2. **Shared helpers** — `resolve_project_by_remote()`, `resolve_project_by_id()`, `get_primary_remote()`
3. **Resolver changes** — all 16 sites in §3.3, in order: Group A first (project resolution), then Group B (access predicates), then Group E (snapshots)
4. **API endpoints** — link-repo, unlink-repo, list-repos
5. **API endpoint** — merge (dry-run first, then live)
6. **CLI commands** — link-repo, unlink-repo, repos, merge
7. **Backfill verification** — startup health check that every project has ≥1 `project_repos` row
8. **Deprecation plan** — file ticket for v0.12+ `projects.git_remote_normalized` cleanup
9. **Dashboard** — Prism: repo-manager panel
10. **Sentinel security pass** — before merge endpoint ships

---

## Appendix A: Files Touched (Summary)

| File | Change |
|------|--------|
| `src/sessionfs/server/db/models.py` | Add `ProjectRepo`, `ProjectMergeAudit` models; add `merged_into_project_id` + `merged_at` to `Project` |
| `src/sessionfs/server/db/migrations/versions/049_multi_repo_projects.py` | NEW — migration |
| `src/sessionfs/server/services/project_resolver.py` | NEW — `resolve_project_by_remote()`, `resolve_project_by_id()`, `get_primary_remote()` |
| `src/sessionfs/server/routes/sessions.py` | Replace `scalar_one_or_none()` with `resolve_project_by_remote()` (A1) |
| `src/sessionfs/server/routes/projects.py` | Replace resolver (A2, A3, A4) + add link/unlink/list/merge endpoints + fix B4, B5, B6 |
| `src/sessionfs/server/auth/project_access.py` | Replace `Session.git_remote_normalized` with `Session.project_id` (B1) |
| `src/sessionfs/server/services/handoff_helpers.py` | Replace direct queries (A5, A6, A7) + fix `_accessible_project_ids` (B2) |
| `src/sessionfs/server/routes/rules.py` | Replace resolver (A8) + fix access check (B3) |
| `src/sessionfs/server/routes/handoffs.py` | Replace direct queries (A9, A10) |
| `src/sessionfs/server/routes/project_transfers.py` | Use `get_primary_remote()` (E1) |
| `src/sessionfs/server/routes/org_members.py` | Use `get_primary_remote()` (E2) |
| `src/sessionfs/mcp/server.py` | Update error messages (D1) |
| `src/sessionfs/cli/cmd_project.py` | Add 4 commands + update error messages (D3) |

## Appendix B: Edge Cases

### B.1 Service Key `project_ids` Allowlist Post-Merge

If a service key is scoped to `project_ids: ["proj_A"]` and proj_A is merged into proj_B, should the key's scope auto-update? **Recommendation: DO NOT auto-update.** The key was explicitly scoped to A. Merging is a destructive operation requiring explicit key re-scoping. The merge audit records the event; the key admin must consciously decide whether to extend trust.

### B.2 Session `project_id` During Merge Race (HIGH-3 — CORRECTED)

**Original (wrong):** Claimed `ON DELETE SET NULL` would clean up stranded sessions. This was incorrect — tombstones are never deleted, so the FK never fires.

**Corrected:** (a) Step 17 catch-up UPDATE inside the merge transaction re-points sessions that landed on source_id during the merge; (b) `resolve_project_by_remote()` and `resolve_project_by_id()` are tombstone-aware and transparently follow the chain; (c) the residual window (session synced after merge completes) is self-healing on the next lookup. A session with `project_id=source_id` that is looked up via its git remote will resolve to the target via the tombstone redirect.

### B.3 Knowledge Entry entity_ref Cross-References

Some KB entries reference other entries by `entity_ref`. If dedup skips source entries, entity_refs pointing to them become dangling. Pre-existing behavior (entity_refs are best-effort string tags, not FKs). Future `sfs project repair-entity-refs` command.

### B.4 Merge with Active Project Transfer

If project A has a pending transfer, can it be merged? **Recommendation: NO.** Require transfers to be resolved (accepted/rejected/cancelled) first.

### B.5 Provider Repo Rename Survival

When a GitHub repo is renamed, `git_remote_normalized` changes but `provider_repo_id` stays the same. The `UNIQUE(provider, provider_repo_id)` partial index detects that the renamed repo is already linked and rejects a duplicate link. To handle the rename gracefully (update the old row rather than reject), a future `sfs project sync-repo-names` or GitHub-rename-webhook handler can match on `(provider, provider_repo_id)` and update `git_remote_normalized` in place. v1 ships the schema; the rename handler is a follow-up.
