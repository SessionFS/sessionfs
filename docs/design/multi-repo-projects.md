# Binding Design — Multi-Repo Projects

**Status:** ✅ Codex VERIFIED-CLEAN (R5) + Sentinel APPROVED-WITH-CONDITIONS — conditions folded into design (S1 amendment); Codex re-review of S1 amendment fixed (S2). Review history: R1 (3 HIGH + 4 MED + 3 LOW) → R2 (0 HIGH, 4 MED + 1 LOW) → R3 (3 MED + 2 LOW) → R4 (1 MED + 1 LOW) → R5 CLEAN. S1: Sentinel amendment. S2: Codex re-review of S1 amendment (displacement coherence + resolver error + verified semantics).
**Author:** Atlas (backend/data-model)
**Date:** 2026-06-15
**Companion:** `docs/design/multi-repo-projects-product.md` (Compass — linking UX + merge collision policy)
**Security gate:** Sentinel pre-build pass completed 2026-06-15 — 6 must-fix conditions (1 HIGH + 5 MEDIUM) + 4 LOW folded into this document (§11). Codex re-review of S1 amendment completed 2026-06-15 — 1 HIGH + 2 MED + 1 LOW fixed (S2). Implementation re-review required before ship.

## Revision History

| Rev | Date | Changes |
|-----|------|---------|
| R1 | 2026-06-15 | Codex R1: 3 HIGH (primary-demotion order, stranded personas, tombstone-aware resolvers) + 4 MED (exhaustive resolver list 11→16, ticket reassign-in-place, is_primary partial index, provider fields) + 3 LOW (JSONB→Text JSON, promote source rules when target none, CLI link-repo naming). CEO calls applied: ticket=reassign-in-place, CLI=link-repo. Per-table merge matrix added. |
| R2 | 2026-06-15 | Codex R2: 4 MED + 1 LOW. MED-1: dry-run writes zero DB rows (audit-row creation moved after dry-run return). MED-2: persona collision rename produces legal ASCII slug `{name}-{src8}` ≤50 chars (verified against `personas.py:45` regex `^[A-Za-z0-9_-]{1,50}$`); human-readable note in audit only; in-flight collision guard against other renamed personas. MED-3: wiki revision reassign uses `(project_id, page_slug)` not nonexistent `page_id` FK (verified `models.py:1054-1058`); revision-number uniqueness handled with offset numbering. MED-4: KnowledgeLink is map+dedup+reassign (not straight reassign) to avoid `uq_kl_link` violation (verified `models.py:1178`); entry-ID mapping from KnowledgeEntry dedup feeds link rewriting. LOW-5: `provider_repo_id` is server-derived/verified, not caller-trusted under unique constraint; added to Sentinel checklist. Residual: §10 test plan added. |
| R3 | 2026-06-15 | Codex R3: 3 MED + 2 LOW. MED-1: skip/merge_content collision policies now assign a legal archived unique name (`{name}-{src8}-archived`, ≤50 chars, `^[A-Za-z0-9_-]$`) BEFORE reassigning to target_id — no uq_persona_project_name violation, no tombstone stranding. MED-2: KnowledgeLink pseudocode rewritten: compute remapped key BEFORE mutation; duplicates are `db.delete()`'d; running set guards self-collision; no mutate-then-flush. MED-3: execute path writes an ATTEMPT audit row (status='started') in a separate session BEFORE mutation, then outcome-updates to 'completed'/'failed' in exception handler — survives rollback; dry-run stays zero-write; `status` column added to `project_merge_audit`. LOW-4: stale `(from <project>)` text replaced with `{name}-{src8}` in §7 and summary sections. LOW-5: companion wiki slug aligned to `{slug}-{src8}`; all `(from <source>)` suffixes swept and replaced. |
| R4 | 2026-06-15 | Codex R4: 1 MED + 1 LOW (audit-contract cleanup). MED-1: narrowed audit guarantee — precondition/authz rejections (404, cross-org, already-merged) are refused BEFORE the `started` audit row exists and are covered by standard request/access logging; only *validated* execute mutation attempts are merge-audited; §5.11 ordering now explicit (preconditions first, not merge-audited). LOW-2: added `skipped_link_ids TEXT NOT NULL DEFAULT '[]'` column to `project_merge_audit` (mirrors `skipped_ke_ids`); outcome-update now persists it. |
| S1 | 2026-06-15 | **Sentinel amendment** (6 must-fix + 4 LOW from `docs/security/multi-repo-projects-security-review.md`). **F1 (HIGH):** verified-vs-unverified ownership model — added `verified` (bool, NOT NULL DEFAULT false) + `verification_method` (enum: `github_app`/`owner_attested`/`legacy_backfill`) to `project_repos`; github_app installation proof for GitHub remotes; owner_attested fallback for non-GitHub/self-hosted; verified-beats-unverified displacement with documented swap rules; legacy_backfill grandfathering for existing rows (migration 049). Noted pre-existing project-create squatting fix as §6 hardening follow-up. **F2 (MED):** de-credited `provider_repo_id` as hijack/DoS defense — documented as best-effort rename-survival nicety (frequently NULL); F1 verification is the load-bearing control. **F3 (MED):** 409 `repo_already_linked` gates `existing_project_id` behind `user_can_access_project` on the owning project; unauthorized callers get opaque 409. **F4 (MED):** tombstone 410 runs access check on source BEFORE disclosing `merged_into` target; unauthorized callers get opaque 404/403. **F5 (MED):** resolver redirect re-authorizes on the resolved/redirected target project at all 16 rewritten sites; added hop-cap ≤8 with logged error as defense-in-depth. **F6 (MED):** app-layer rate limits on link + merge (incl. dry-run); Forge ticket filed for durable edge/multi-replica rate limiting. **4 LOW:** audit denials (L1); `sfs security scan` parity (L2); block merge when transfer pending (L3, §5.11 Phase 1); attestation snapshot survives user deletion (L4, §5.1). New **§11 Security Conditions** cross-referencing the review + updated §8.2 checklist. Companion doc updated for GitHub app-install linking UX. |
| S2 | 2026-06-15 | **Codex re-review fixes of Sentinel amendment.** **HIGH-1 (displacement coherence):** displacement DELETEs the unverified row (never UPDATEs `project_id=NULL` — column is `NOT NULL`). Holder zero-repo after displacement → new `repo_reclaimed` orphaned state (distinct from merge tombstone; keeps its own KB/personas/tickets/rules with NO auto-import to claimant; owner can re-link to revive). Holder other-repos → promote new primary + refresh `git_remote_normalized`. Added `projects.repo_reclaimed_at` column (migration 049 additive). New **§3.4 Project Lifecycle States** (active/merged/repo_reclaimed with per-state resolver behavior). Q4 amended: zero-repo permitted ONLY in merged OR repo_reclaimed states. **MED-2 (resolver error):** hop-cap exceedance raises typed `ProjectResolutionLoopError` (never a silent Project return); both resolver paths fixed; route-level mapping to 409/500 documented. **MED-3 (verified semantics):** `verified=true` ONLY for `github_app`; `owner_attested` + `legacy_backfill` ALWAYS `verified=false`. Schema comment corrected; both docs swept. **LOW-4 (cross-org carve-out):** verified reclaim displaces cross-org unverified squatter; FINAL state in verified owner's org only. Normal cross-org linking still rejected. Binding §6.3 + companion §7.3 updated. **Sentinel build notes N1 (confused-deputy: linker's OWN installation token) + N2 (liveness: GitHub API call outside swap txn)** folded into §8.2 checklist + §11. |

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
    -- BEST-EFFORT only — frequently NULL. The load-bearing anti-hijack
    -- control is `verified` + `verification_method` (see F1/F2, §11).
    provider        VARCHAR(20),          -- e.g. 'github', 'gitlab', 'bitbucket'
    provider_repo_id VARCHAR(100),        -- stable integer-as-string, e.g. GitHub repo ID
    is_primary      BOOLEAN NOT NULL DEFAULT FALSE,
    -- Sentinel F1: repo-link anti-hijack ownership verification.
    --   verified=true  → ownership was proven (github_app ONLY).
    --                     Verified rows can displace unverified rows
    --                     on the same UNIQUE(git_remote_normalized).
    --   verified=false → caller claimed ownership but could not prove it
    --                     (owner_attested for non-GitHub/self-hosted/app-not-installed,
    --                     or legacy_backfill for pre-existing rows at migration).
    --   owner_attested and legacy_backfill are ALWAYS verified=false.
    --   Displacement rules documented in §6.2.
    verified        BOOLEAN NOT NULL DEFAULT FALSE,
    verification_method VARCHAR(20),      -- 'github_app' | 'owner_attested' | 'legacy_backfill'
    added_by_user_id VARCHAR(64)
                        REFERENCES users(id) ON DELETE SET NULL,
    -- Sentinel L4: link attestation survives user deletion via plain-string
    -- snapshot in the project_merge_audit / link audit row, not only the FK.
    -- The FK is ON DELETE SET NULL for hygiene; the audit row carries the
    -- linker's user_id + email snapshot at link time.
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    -- Each remote belongs to exactly one project (global uniqueness).
    -- Displacement: a verified link request can displace an unverified
    -- row (atomic swap with audit); see §6.2 displacement rules.
    CONSTRAINT uq_project_repos_remote UNIQUE (git_remote_normalized),

    -- Each provider repo_id belongs to exactly one project (rename survival).
    -- Partial: only enforced when provider_repo_id IS NOT NULL.
    -- NOTE: provider_repo_id is server-derived (caller input IGNORED) and
    -- frequently NULL (requires GitHub App installed on the repo).
    -- This is a rename-survival nicety, NOT a hijack/DoS defense (F2, §11).
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
    verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="false", nullable=False
    )
    verification_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
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

**`projects` table additions (migration 049):**

| Column | Type | Purpose |
|--------|------|---------|
| `merged_into_project_id` | VARCHAR(64) FK→projects(id) ON DELETE SET NULL, nullable | Tombstone marker — set when this project was merged into another. See §5.9. |
| `merged_at` | TIMESTAMPTZ, nullable | When the merge occurred. |
| `repo_reclaimed_at` | TIMESTAMPTZ, nullable | Set when ALL repos were reclaimed by verified owners via displacement (§6.2). The project enters the `repo_reclaimed` orphaned state — distinct from a merge tombstone. It retains all its own KB/personas/tickets/rules (NOT merged into the claimant). Hidden from the active project list but readable by its owner (audit trail). The owner may re-link a different repo to revive it (clear `repo_reclaimed_at`, transition back to `active`). See §3.4. |

### 3.2 Migration (049 — Additive)

**File:** `src/sessionfs/server/db/migrations/versions/049_multi_repo_projects.py`

**Upgrade:**

1. Create `project_repos` table with all columns (including `provider`, `provider_repo_id`), constraints, and indexes
2. Create two partial unique indexes (primary, provider_repo_id) — use cross-DB-safe Alembic with `postgresql_where` + `sqlite_where`
3. Add `merged_into_project_id` (nullable FK to `projects.id`, `ON DELETE SET NULL`) + `merged_at` (nullable TIMESTAMPTZ) + `repo_reclaimed_at` (nullable TIMESTAMPTZ) to `projects`
4. Create `project_merge_audit` table (see §5.10)
5. Backfill: one row per existing project from `projects.git_remote_normalized` with `is_primary = TRUE`, **`verified = FALSE, verification_method = 'legacy_backfill'`** (Sentinel F1: existing rows are grandfathered — NOT displaceable by a new *unverified* claim, but ARE displaceable by a *verified* claim so a real owner can reclaim a squatted legacy remote).
   ```sql
   INSERT INTO project_repos (id, project_id, git_remote_normalized, is_primary, verified, verification_method, created_at)
   SELECT gen_random_uuid()::text, id, git_remote_normalized, TRUE, FALSE, 'legacy_backfill', NOW()
   FROM projects
   WHERE git_remote_normalized IS NOT NULL AND git_remote_normalized != '';
   ```
   (Use SQLite-compatible UUID generation via the existing `_gen_id()` utility.)

**Downgrade:** Drop `project_repos` table + `project_merge_audit` table. Drop `merged_into_project_id` + `merged_at` + `repo_reclaimed_at` columns from `projects`. No data loss risk — the columns on `projects` were never removed.

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

    IMPORTANT (Sentinel F5): This function RESOLVES; it NEVER authorizes.
    Every caller MUST run its own access check against the RETURNED project
    (which may be a redirect target), not the input remote. Resolution is
    not authorization — a redirect through a tombstone must never grant
    access the caller would not have had on the target directly.

    Hop-cap: the tombstone chain is bounded at ≤8 hops (belt-and-suspenders;
    preconditions prevent cycles, but data corruption or future code paths
    could introduce one). Exceeding the hop cap raises a typed
    ProjectResolutionLoopError — never a silent normal Project return.
    Callers (routes) map it to a 409/500; no caller ever operates on a
    corrupt-chain project unknowingly.

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
    # with a hop cap to bound unbounded loops (defense-in-depth;
    # preconditions prevent A→B→A, but data corruption could create one).
    HOP_CAP = 8
    hops = 0
    if project is not None and follow_tombstone and project.merged_into_project_id:
        while project is not None and project.merged_into_project_id:
            hops += 1
            if hops > HOP_CAP:
                raise ProjectResolutionLoopError(
                    f"resolve_project_by_remote: tombstone hop cap ({HOP_CAP}) "
                    f"exceeded for remote {git_remote_normalized} — "
                    f"possible data corruption"
                )
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
    """Get project by ID, optionally following tombstone chain.

    Same hop-cap as resolve_project_by_remote (≤8). Raises
    ProjectResolutionLoopError on exceedance — never a silent return.
    """
    project = await db.get(Project, project_id)
    if project is not None and follow_tombstone and project.merged_into_project_id:
        hops = 0
        HOP_CAP = 8
        while project is not None and project.merged_into_project_id:
            hops += 1
            if hops > HOP_CAP:
                raise ProjectResolutionLoopError(
                    f"resolve_project_by_id: tombstone hop cap ({HOP_CAP}) "
                    f"exceeded for project {project_id} — "
                    f"possible data corruption"
                )
            project = await db.get(Project, project.merged_into_project_id)
    return project
```

**Route-level error mapping:** `ProjectResolutionLoopError` is a typed exception defined in `src/sessionfs/server/services/project_resolver.py`. Both `resolve_project_by_remote` and `resolve_project_by_id` raise it on hop-cap exceedance. Routes map it to `409 Conflict` with `{"error": "resolution_loop", "message": "..."}`, falling back to `500 Internal Server Error` if a route doesn't catch it explicitly. **Neither resolver path ever returns a normal `Project` on hop-cap exceedance** — callers cannot unknowingly operate on a corrupt-chain project.

#### Site-by-Site Changes (All 16 Sites)

**Group A — Direct project-resolution sites (10 sites):**

| # | Site | Change |
|---|------|--------|
| A1 | `_resolve_project_id_for_session` (`routes/sessions.py:537-543`) | Replace `select(Project).where(git_remote_normalized == X).with_for_update()` → `await resolve_project_by_remote(db, X, for_update=True)` |
| A2 | `GET /api/v1/projects/{git_remote_normalized:path}` (`routes/projects.py:376-378`) | Replace `scalar_one_or_none()` → `await resolve_project_by_remote(db, X)`. **Tombstone-aware (Sentinel F4):** resolve with `follow_tombstone=False` first. If the resolved project has `merged_into_project_id`, run `user_can_access_project` on the SOURCE (tombstone) BEFORE disclosing the target. Authorized → `410 Gone` with `{"merged_into": target_id}`. Unauthorized → opaque `404` (same as a non-existent project). If authorized, re-check access on the redirect target as well (F5). |
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

### 3.4 Project Lifecycle States

A project is in exactly one of three lifecycle states, derived from its columns:

| State | Condition | Resolver behavior | List visibility |
|-------|-----------|-------------------|-----------------|
| **`active`** | `merged_into_project_id IS NULL` AND `repo_reclaimed_at IS NULL` | Normal resolution. `resolve_project_by_remote` returns this project directly. | Visible in project list. |
| **`merged`** (tombstone) | `merged_into_project_id IS NOT NULL` | `resolve_project_by_remote` / `resolve_project_by_id` follow the tombstone chain transparently (hop-cap ≤8). Routes MUST re-authorize on the resolved target. `GET /projects/{id}` returns `410 Gone` with `merged_into` target (gated by access check on source first — §6.5 F4). | Hidden from project list. Accessible by direct ID lookup for audit. |
| **`repo_reclaimed`** (orphaned) | `repo_reclaimed_at IS NOT NULL` AND `merged_into_project_id IS NULL` | Treated as an orphaned project — **NOT redirected** like a tombstone. Resolves to itself normally (the project still exists and its data is intact). `GET /projects/{id}` returns the project with a `repo_reclaimed: true` flag. | Hidden from the active project list. Readable by its owner (audit trail). Dashboard surfaces a notice: "All repos were reclaimed by verified owners. Link a new repo to revive this project." |

**Transitions:**

- `active` → `merged`: merge execution (§5.12). Sets `merged_into_project_id` + `merged_at`. All repos reassigned to target.
- `active` → `repo_reclaimed`: displacement removes the LAST repo (§6.2). Sets `repo_reclaimed_at = NOW()`.
- `repo_reclaimed` → `active`: owner links a new repo to the project. Clears `repo_reclaimed_at = NULL`.
- `merged` → (no transition): merge is permanent. A merged tombstone cannot be revived.

**`repo_reclaimed` vs `merged` — critical distinction:**

| | `merged` (tombstone) | `repo_reclaimed` (orphaned) |
|---|---|---|
| Cause | User-initiated merge | Verified owner reclaims repo via displacement |
| Repos | All moved to target | All reclaimed by verified owners |
| KB / personas / tickets / rules | Reassigned to target project | **Kept intact** on this project (squatter-poisoning guard) |
| Resolver | Redirects to target | Returns this project (no redirect) |
| Revivable? | No (permanent) | Yes — owner can re-link a repo |
| Dashboard | "Merged into X" | "Repos reclaimed — link a repo to revive" |

---

## 4. API + CLI Surface

### 4.1 REST Endpoints

#### `POST /api/v1/projects/{project_id}/repos`
Link a repo to a project.

**Authz (Sentinel F1 — verified-vs-unverified ownership model):**

Linking a repo claims a globally-unique `git_remote_normalized`. The caller must have admin standing on the target project (owner OR org-admin — `user_is_project_admin`), AND must demonstrate repo ownership at one of two levels:

1. **GitHub App verification path (verified=true, verification_method='github_app'):** If the SessionFS GitHub App is installed on the target repo's owner, the server verifies control via the installation token — confirm the repo is in the installation's accessible-repo set (`GET /repos/{owner}/{repo}` succeeds with the installation token). On success: `verified=true`, `verification_method='github_app'`. This is the **authoritative** path. `provider` and `provider_repo_id` are populated from the installation response (e.g. GitHub repo ID as string). **Caller-supplied `provider_repo_id` is IGNORED** (F2: caller input is untrusted; only server-resolved values are stored).

2. **Owner-attested fallback (verified=false, verification_method='owner_attested'):** For non-GitHub remotes, self-hosted instances, or GitHub repos where the App is not installed, a project owner / org-admin MAY link with `verified=false`. The link is recorded as **owner-attested** — the caller asserts they control the repo but the server cannot verify it. The `added_by_user_id` FK records who attested. **The caller must still pass `user_is_project_admin` on the target project.** A forged session tag is NOT sufficient to link a global-unique remote.

**`provider` and `provider_repo_id` are server-derived (F2):** Caller-supplied values in the request body are **ignored**. The server populates them only when the GitHub App verification path succeeds. For all other remotes (GitHub without App, GitLab, Bitbucket, self-hosted), both fields are NULL. The `UNIQUE(provider, provider_repo_id)` partial index is a **best-effort rename-survival nicety**, NOT a hijack/DoS defense. The load-bearing anti-hijack control is F1's verification.

**Verified-beats-unverified displacement (Sentinel F1):** The global `UNIQUE(git_remote_normalized)` constraint is enforced with displacement rules:

| Requester | Holder | Result |
|-----------|--------|--------|
| Verified (`github_app`) | Unverified (`owner_attested` or `legacy_backfill`) | **Displace.** Atomically DELETE the unverified row from its project (audited; a `repo_reclaimed` event surfaces it to the displaced project). If the holder is left with zero repos, it enters the `repo_reclaimed` orphaned state (§3.4); if the displaced row was primary and other repos remain, promote a new primary. Then INSERT the verified row for the claimant. |
| Verified (`github_app`) | Verified (`github_app`) | **409 genuine conflict.** Both have proven ownership. Manual/support resolution required. |
| Unverified (`owner_attested`) | Any (verified or unverified) | **409.** Unverified claims cannot displace anyone. |

**Atomic displacement procedure:**
1. `SELECT ... FOR UPDATE` lock the existing (unverified) `project_repos` row AND the holder project row for FK safety.
2. **DELETE the unverified row** — frees the `UNIQUE(git_remote_normalized)` for the verified claimant's insert. The `SELECT FOR UPDATE` + UNIQUE constraint together act as the atomic backstop. **NEVER sets `project_id=NULL`** (the column is `NOT NULL`).
3. Write a `repo_reclaimed` audit event recording the displacement (old project_id, old `verified`/`verification_method`, displaced_at, displaced_by_user_id, the reclaimed remote).
4. **Handle the holder project's remaining repos:**
   - If the displaced row was `is_primary` and the holder has **other repos**: promote the oldest remaining repo to primary (`UPDATE project_repos SET is_primary = TRUE ... ORDER BY created_at ASC LIMIT 1`) and refresh `projects.git_remote_normalized` from the new primary.
   - If the displaced row was the holder's **only repo** (zero repos remain): the holder project enters the **`repo_reclaimed`** orphaned state — `UPDATE projects SET repo_reclaimed_at = NOW() WHERE id = :holder_id`. The holder keeps ALL its own project-scoped data (KB, personas, tickets, rules). **NEVER auto-import into the claimant** (squatter-poisoning guard — Codex explicit). The orphaned project is hidden from the active project list but readable by its owner (audit trail). A dashboard notice surfaces the state. The owner may re-link a different repo to revive it (transition back to `active`, clearing `repo_reclaimed_at`).
   - If the displaced row was non-primary and other repos remain: no structural change to the holder beyond the row deletion.
5. INSERT the new verified row into the claimant project.
6. Commit.

**Request:**
```json
{
  "git_remote": "https://github.com/acme/backend.git",
  "is_primary": false
}
```
`provider` and `provider_repo_id` are **IGNORED** if supplied — the server resolves its own values.

**Responses:**
- `201` — linked, returns `ProjectRepoResponse` with `verified` and `verification_method` fields
- `409` — repo already linked. **Sentinel F3:** `existing_project_id` is ONLY included when the caller passes `user_can_access_project` on the owning project. Unauthorized/cross-org callers receive an opaque `{"error": "repo_already_linked", "message": "This repo is already linked to another project."}` with NO project identifier. Authorized callers (owner/org-admin/member of the owning project) receive the full response with `existing_project_id` and unlink-or-merge guidance.
- `403` — not authorized (not project admin, or repo-access verification failed)
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
3. **Audit-logged** — every *validated* execute mutation attempt is recorded in `project_merge_audit` (status `started` → `completed`/`failed`). Precondition and authorization rejections (404 not-found, cross-org, already-merged, pending-transfer) are refused BEFORE the merge audit row is created and are covered by the standard request/access log, not the merge audit. An ATTEMPT row (status='started') is written in a SEPARATE transaction BEFORE any merge mutation. On success the row is outcome-updated to 'completed'; on failure the exception handler outcome-updates it to 'failed' via a fresh session — the audit row survives rollback of the merge transaction. Dry-run writes nothing. **Sentinel L1:** Security-relevant denials (cross-org merge attempts, unauthorized link attempts) SHOULD additionally be recorded via the existing `AdminAction` audit path to ensure SOC visibility of ownership-reassignment attacks — promoted from optional to recommended. **Sentinel L4:** Link attestation (`added_by_user_id`) MUST be snapshotted in the link audit row (plain-string user_id + email at link time, not only the nullable FK) so the record survives user deletion for forensics.
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
repo_reclaimed_at: Mapped[datetime | None] = mapped_column(
    DateTime(timezone=True), nullable=True
)
```

A merged project:
- Returns `410 Gone` on `GET /api/v1/projects/{id}` with `{"merged_into": "<target_id>", "message": "This project was merged into <target_id>."}` — **BUT ONLY after the access check on the SOURCE (tombstone) project passes** (Sentinel F4). Unauthorized callers (strangers, former org members who lost access) receive the same opaque `404`/`403` a non-tombstone inaccessible project returns — never `merged_into`. This prevents cross-tenant enumeration: an attacker probing a known source remote cannot learn the target project ID or confirm a merge occurred.
- Its repos now belong to the target
- Access to the tombstone is read-only for audit (original owner)
- Resolvers transparently follow the tombstone chain (see `resolve_project_by_remote`), but routes MUST run the access check against the resolved/redirected target, not the input remote (Sentinel F5 — see §3.3 resolver contract)
- **Distinct from `repo_reclaimed`:** a merged tombstone's KB/personas/tickets/rules were reassigned to the target. A `repo_reclaimed` project's data stays with the orphaned project (§3.4).

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
    # Sentinel L3: block merge if either project has a pending transfer.
    # A merge that strands a pending cross-scope transfer could move data
    # the transfer recipient was about to gain/lose rights to.
    source_transfer = await db.execute(
        select(ProjectTransfer).where(
            ProjectTransfer.project_id == source_id,
            ProjectTransfer.status == "pending",
        )
    ).scalar_one_or_none()
    if source_transfer:
        raise HTTPException(400, "Source project has a pending transfer. Resolve it first.")
    target_transfer = await db.execute(
        select(ProjectTransfer).where(
            ProjectTransfer.project_id == target_id,
            ProjectTransfer.status == "pending",
        )
    ).scalar_one_or_none()
    if target_transfer:
        raise HTTPException(400, "Target project has a pending transfer. Resolve it first.")

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

### 6.1 Who Can Link a Repo (Sentinel F1 — Verified-Ownership Model)

Linking a repo claims a globally-unique resource. The bar is higher than read access:

1. **Project admin standing required** (`user_is_project_admin` — owner OR org-admin of the project). A captured-session-on-the-remote is NOT sufficient for linking a new global-unique remote (Sentinel F1: `git_remote_normalized` is extracted from the client-supplied `workspace.json` inside the uploaded session archive at `routes/sessions.py:789-795`; it is attacker-controlled metadata and cannot be trusted as proof of repo ownership).

2. **Repo-ownership verification** — the server determines which path applies:
   - **GitHub App installed on the repo's owner:** Verify via installation token (`GET /repos/{owner}/{repo}` succeeds). Record `verified=true, verification_method='github_app'`. Populate `provider` + `provider_repo_id` from the installation response. This is the authoritative path.
   - **Otherwise (non-GitHub, self-hosted, or app-not-installed):** The admin MAY link with `verified=false, verification_method='owner_attested'`. The link is recorded as an explicit attestation; the `added_by_user_id` FK records who attested.

3. **No link without admin standing.** A forged `workspace.json` remote alone CANNOT reserve a global-unique remote.

### 6.2 Repo Hijacking Prevention — Verified-Beats-Unverified Displacement

The global `UNIQUE(git_remote_normalized)` on `project_repos` is the primary database-level guard. The displacement model resolves conflicts:

| Requester | Holder | Result |
|-----------|--------|--------|
| Verified (`github_app`) | Unverified (`owner_attested` or `legacy_backfill`) | **Displace.** Atomic DELETE of the unverified row (lock → delete → handle holder-project state → audit → insert verified row for claimant). If the holder is left with zero repos, it enters the `repo_reclaimed` orphaned state (distinct from merge tombstone; keeps its own KB/personas/tickets/rules — NEVER auto-imported). A `repo_reclaimed` event surfaces the displacement to the displaced project's admin. |
| Verified (`github_app`) | Verified (`github_app`) | **409 genuine conflict.** Both have proven ownership. Manual/support resolution required. |
| Unverified (`owner_attested`) | Any (verified or unverified) | **409.** Unverified claims cannot displace anyone. |

**Backfill (migration 049):** Existing projects' remotes are backfilled as `verified=false, verification_method='legacy_backfill'`. They are grandfathered: NOT displaceable by a new *unverified* claim, but ARE displaceable by a *verified* claim — so a real owner can reclaim a squatted legacy remote.

**Pre-existing squatting fix (noted for hardening):** The pre-existing `POST /projects/` create path also trusts the client-supplied `git_remote_normalized` without ownership verification. The same `verified` + `verification_method` stamping should be applied at project creation time (a follow-up within this Issue or a noted hardening). Creation and linking would then share one anti-hijack model. This is a §6 hardening item; not fully designed here.

**`provider_repo_id` is NOT the hijack defense (Sentinel F2):** The `UNIQUE(provider, provider_repo_id)` partial index only fires when the field is non-NULL, which requires the GitHub App to be installed on the repo. For the common case (app not installed, self-hosted, non-GitHub), the field is NULL and the index never fires. The design's anti-hijack control is F1's verified-ownership model. `provider_repo_id` is a best-effort rename-survival nicety. Caller-supplied values are **ignored**. Server derivation uses ONLY the linker's own installation token; cross-installation resolution is forbidden; failure to resolve → NULL.

### 6.3 Cross-Org Boundary

- A repo linked to an org-scoped project is "owned" by that org
- Linking the same repo to a different org's project is rejected (409) — **with one exception:** a verified owner reclaiming their repo via displacement (§6.2) places the repo in the verified owner's project/org and removes it from the unverified (possibly cross-org) holder. The FINAL state has the repo ONLY in the verified owner's project. Normal (non-reclaim) cross-org linking remains rejected.
- Merging projects across orgs is DENIED (400)
- Service keys: the `assert_service_key_can_access_project` check in `handoff_helpers.py:561-564` validates project-level access. With multi-repo, the project boundary is unchanged — service keys get access to ALL repos under a project.

### 6.4 Who Can Merge Projects

Both projects must be owned by the same entity:
- **Both personal:** Caller must own both
- **Both same org:** Caller must be org admin of that org
- **Mixed (personal + org):** DENIED. Use project transfer first, then merge.

### 6.5 Sentinel Pre-Build Security Pass — COMPLETED

Sentinel reviewed this design on 2026-06-15. Full report: `docs/security/multi-repo-projects-security-review.md`.

**Verdict:** APPROVED-WITH-CONDITIONS. All 6 must-fix conditions (F1–F6) + 4 LOW are folded into this document. See §11 for the cross-reference and implementation checklist.

### 6.6 Rate Limiting on Link + Merge (Sentinel F6)

The only existing limiter is `auth/rate_limit.py`'s `SlidingWindowRateLimiter` — in-memory, per-replica, insufficient for multi-replica Cloud Run. Link and merge are sensitive mutations that must be rate-limited:

**App-layer (implementation):**
- Apply per-user and per-project sliding-window caps on `POST …/repos` (link), `DELETE …/repos` (unlink), and `POST …/merge` (both dry-run and execute). Treat link as a sensitive mutation, not a read.
- Example thresholds: 20 links/hour per user, 10 merges (dry-run or execute)/hour per user. Tune based on real usage.
- Re-use the existing `SlidingWindowRateLimiter` pattern; add per-project keys in addition to per-user keys.

**Forge follow-up (durable edge rate limiting):**
- File a Forge ticket for durable edge/multi-replica rate limiting (Cloud Armor / API Gateway) on link + merge routes so the control survives horizontal scaling. The in-memory limiter is best-effort only.
- Reference the Forge ticket ID in the implementation commit and in §11.

---

## 7. Open Decisions

### Q1: Each repo belongs to exactly one project — confirm?

**Recommendation: YES.** Enforced by `UNIQUE(git_remote_normalized)` + `UNIQUE(provider, provider_repo_id)` on `project_repos`.

### Q2: Persona collision policy + Rules collision policy

**Resolved by Compass companion (§3):** Keep target, rename source to `{name}-{src8}` (legal ASCII slug ≤50 chars; e.g. `prism-a1b2c3d4`). Human-readable "(from <project>)" context lives only in the audit row's `persona_renames.display_note` JSON. Rules: keep target, archive source as wiki page snapshot (promote source when target has none — LOW-9). User can override with `--interactive` at merge time.

### Q3: Is multi-repo / merge a tiered feature?

**RESOLVED — FREE for all tiers (CEO decision, 2026-06-15).** No tier plumbing. No `multi_repo_projects` feature gate. No `check_feature()` call. Ownership/org authz only.

### Q4: Must a project have at least one repo?

**YES, with two explicit exceptions.** Enforcement: `DELETE /repos/{id}` returns 422 on last repo (normal unlink cannot orphan a project). Zero-repo projects are permitted ONLY in two lifecycle states:
- **`merged` tombstone** — source project after a merge; all repos reassigned to target (§5.9).
- **`repo_reclaimed` orphaned** — all repos reclaimed by verified owners via displacement (§6.2); project retains its own data.

Active projects (`merged_into_project_id IS NULL` AND `repo_reclaimed_at IS NULL`) must have ≥1 repo. See §3.4 for the full lifecycle state machine.

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
- [ ] **[F1] `verified` + `verification_method` columns:** migration 049 backfill correct? `legacy_backfill` for existing rows?
- [ ] **[F1] Displacement logic:** verified-vs-unverified, verified-vs-verified, unverified-vs-any rules correct? Atomic DELETE (never UPDATE project_id=NULL)? Holder zero-repo → `repo_reclaimed`? Holder other-repos → promote new primary?
- [ ] **[F1] `repo_reclaimed` orphaned state:** distinct from merge tombstone? Keeps its own KB/personas/tickets/rules (NEVER auto-imported)? Hidden from active list but readable by owner? Re-link revives it?
- [ ] **[F5] Resolver re-authorization:** do all 16 rewritten sites run access check on the resolved/redirected target, not the input remote?
- [ ] **[F5] Hop-cap:** does `resolve_project_by_remote` / `resolve_project_by_id` raise `ProjectResolutionLoopError` (never a silent Project return) on exceedance? Route-level mapping to 409/500?
- [ ] **[F4] Tombstone 410:** does the route run access check on the source BEFORE disclosing `merged_into`?
- [ ] **[MED-3] Verified semantics:** `verified=true` ONLY for `github_app`? `owner_attested` and `legacy_backfill` ALWAYS `verified=false`? Schema comment authoritative?
- [ ] **[LOW-4] Cross-org verified reclaim:** does verified displacement carve out cross-org rejection? FINAL state in verified owner's org only? Normal cross-org linking still rejected?

### 8.2 Shield-SR Security Review Checklist (Updated for Sentinel S1 + S2)

This checklist is aligned with the Binding Implementation Security Checklist in `docs/security/multi-repo-projects-security-review.md`. Each item gates implementation; "verified" requires a passing negative test where one is feasible. See §11 for the full cross-reference.

- [ ] **[F1] Repo-link does not trust forged session tags.** Link requires project admin (`user_is_project_admin`) AND repo-ownership verification (GitHub App installation proof → `verified=true, verification_method='github_app'`; or owner-attested fallback → `verified=false, verification_method='owner_attested'`). A fabricated `workspace.json` remote alone MUST NOT permit linking a global-unique remote. Verified-beats-unverified displacement rules implemented atomically with audit. *Tests:* user with forged session denied (403); verified displaces unverified; verified-vs-verified → 409; unverified-vs-any → 409.
- [ ] **[F1] Migration 049 backfill:** existing rows grandfathered as `verified=false, verification_method='legacy_backfill'`. Displaceable by verified claims, not by unverified claims.
- [ ] **[F1] Pre-existing squatting fix noted:** project CREATE hardening follow-up filed; creation and linking share one anti-hijack model.
- [ ] **[F2] Provider-repo-ID de-credited.** `provider_repo_id` is server-derived (caller input IGNORED), frequently NULL, and documented as a best-effort rename-survival nicety — NOT the hijack/DoS defense. Derivation uses ONLY the linker's own installation token; cross-installation resolution is forbidden. *Test:* caller-supplied `provider_repo_id` in the link body is ignored.
- [ ] **[F3] 409 `repo_already_linked` gates `existing_project_id`** behind `user_can_access_project` on the owning project. Unauthorized/cross-org callers get opaque 409 with no project identifier. *Tests:* org-A user probing org-B remote → no org-B project id; org-B member → full response.
- [ ] **[F4] Tombstone 410 `merged_into` gated by access check on source first.** Strangers/former-members get opaque 404/403, never the target id. *Tests:* source-owner → 410 + target; stranger → opaque.
- [ ] **[F5] Resolver authorizes nothing.** All 16 rewritten sites run access check against resolved/redirected target project. Tombstone-follow loop has hop-cap ≤8 with logged error. *Tests:* (a) following tombstone does not grant target access the caller lacked; (b) service key scoped to source ∉ target allowlist is denied after merge.
- [ ] **[F6] Link + merge (incl. dry-run) are rate-limited** at app layer. Forge ticket filed for durable edge/multi-replica rate limiting. *Test:* burst of link/merge-dry-run is throttled.
- [ ] **[Merge authz] BOTH source and target authorized independently** via owner-of-each OR `user_is_project_admin` of each; cross-org/personal-mix denied (400); both `merged_into_project_id IS NULL` precondition enforced; pending transfer on either side blocks merge (L3). *Tests:* non-owner denied; cross-org denied; double-merge denied; pending-transfer blocked.
- [ ] **[Service key] Multi-repo does not widen service-key scope.** Boundary remains the project; `assert_service_key_can_access_project` runs on resolved target. Post-merge, a `project_ids:[source]`-scoped key is denied on target. *Test:* present.
- [ ] **[Audit] Security-relevant denials recorded** (cross-org merge attempts, unauthorized link attempts) via AdminAction audit path (L1). Link attestation snapshot (user_id + email at link time) survives user deletion (L4).
- [ ] **[L2] `sfs security scan` parity** for any new local cache/path from link/merge (no new secret files expected in v1; note for parity).
- [ ] **[Dry-run] Provably zero DB writes** — no audit row, no row locks beyond reads. *Test:* row-count delta == 0.
- [ ] **[Regression] No raw secrets / cross-tenant identifiers in 4xx bodies, logs, or merge-audit stats.** `project_id` appears only to authorized callers (F3, F4).
- [ ] **[Migration] Backfill 049** creates exactly one `is_primary` repo per existing project with `verified=false, verification_method='legacy_backfill'`; empty/NULL `git_remote_normalized` projects produce no orphan rows. Dual-read fallback queries `project_repos` first (no shadowing). Downgrade clean.
- [ ] **[N1 — Sentinel build note] GitHub App verification uses the LINKER'S OWN installation token** — never a global/shared token. The installation lookup is scoped to the authenticated user's own installations; cross-installation resolution is forbidden. This prevents a confused-deputy attack where a user with App access on repo A uses that token to "verify" repo B that they don't control.
- [ ] **[N2 — Sentinel build note] GitHub API call is OUTSIDE the swap transaction.** The `GET /repos/{owner}/{repo}` verification call (installation token) is made BEFORE acquiring the `SELECT FOR UPDATE` lock on `project_repos`. The swap transaction (lock → DELETE → handle holder state → INSERT → commit) contains only DB writes — no live HTTP calls. This avoids holding row locks during network I/O (liveness). If the GitHub API call fails, the swap is never attempted.

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

When a GitHub repo is renamed, `git_remote_normalized` changes but `provider_repo_id` stays the same. The `UNIQUE(provider, provider_repo_id)` partial index detects that the renamed repo is already linked and rejects a duplicate link. To handle the rename gracefully (update the old row rather than reject), a future `sfs project sync-repo-names` or GitHub-rename-webhook handler can match on `(provider, provider_repo_id)` and update `git_remote_normalized` in place. v1 ships the schema; the rename handler is a follow-up. **Note (Sentinel F2):** `provider_repo_id` is frequently NULL (requires GitHub App installed on the repo); the partial index is a rename-survival nicety, not a hijack/DoS defense.

---

## 11. Security Conditions (Sentinel Pre-Build Pass)

**Source:** `docs/security/multi-repo-projects-security-review.md` (Sentinel, 2026-06-15)
**Verdict:** APPROVED-WITH-CONDITIONS
**Must-fix:** 6 (1 HIGH, 5 MEDIUM) + 4 LOW — all folded into this document as of the S1 amendment.

### F1 — HIGH — Verified-vs-Unverified Ownership Model (CEO-Decided)

**Problem:** `git_remote_normalized` is extracted from the *client-supplied* `workspace.json` inside the uploaded session archive (`routes/sessions.py:789-795` → `normalize_git_remote`). `normalize_git_remote` (`github_app.py:25-43`) only string-normalizes — it does NOT verify the caller owns or can push to the repo. The pre-existing `_check_repo_access` (`routes/projects.py:74-82`) and `user_can_access_project` predicate #3 (`auth/project_access.py:55-65`) both grant access purely on `Session.git_remote_normalized == <remote>` for a session the user owns. A free-tier attacker can fabricate a `.sfs` session claiming `git_remote = github.com/facebook/react`, sync it, then link the remote — claiming the globally-unique namespace.

**Resolution (this document):**
- `project_repos` gains `verified` (bool, NOT NULL DEFAULT false) + `verification_method` (enum: `github_app` | `owner_attested` | `legacy_backfill`) — §3.1
- GitHub App installation proof is the authoritative verification path: confirm the repo is in the installation's accessible set via installation token → `verified=true, verification_method='github_app'` — §4.1, §6.1
- Owner-attested fallback for non-GitHub/self-hosted/app-not-installed → `verified=false, verification_method='owner_attested'`; requires project admin (`user_is_project_admin`) — §4.1, §6.1
- Verified-beats-unverified displacement with documented swap rules (atomic lock+unlink+audit+insert) — §4.1, §6.2
- Migration 049 backfill: existing rows → `verified=false, verification_method='legacy_backfill'`, grandfathered (displaceable by verified, not by unverified) — §3.2
- Pre-existing project-create squatting fix noted as §6 hardening follow-up — §6.2

**Implementation gate:** Test that user with forged session on `victim/private` is denied link (403) and cannot reserve the global remote.

### F2 — MEDIUM — De-Credit `provider_repo_id` as Hijack/DoS Defense

**Problem:** `provider_repo_id` is "server-derived" but there is NO per-repo provider-ID mapping stored. `GitHubInstallation` (`models.py:526-540`) stores no repo list and no repo IDs. Deriving a `provider_repo_id` requires a live GitHub API call with an installation token — which only works if the App is installed on that repo. For the common case, `provider_repo_id` will be NULL so the partial `UNIQUE(provider, provider_repo_id)` index never fires. The design's stated mitigations leaning on provider_repo_id do not hold for the majority of links.

**Resolution (this document):**
- Caller-supplied `provider_repo_id` is IGNORED — §4.1
- Server derivation uses ONLY the linker's own installation token; cross-installation resolution is forbidden — §6.2
- Documented as best-effort rename-survival nicety, NOT hijack/DoS defense — §3.1, §6.2, Appendix B.5
- The load-bearing anti-hijack control is F1's verified-ownership model — §6.2

**Implementation gate:** Test that caller-supplied `provider_repo_id` in the link body is ignored.

### F3 — MEDIUM — 409 `repo_already_linked` Must Not Leak `existing_project_id`

**Problem:** Any authenticated free-tier user can probe any remote and learn the project_id that owns it from the 409 response, including projects in other orgs they have no access to — a cross-tenant enumeration oracle.

**Resolution (this document):**
- 409 response includes `existing_project_id` ONLY when the caller passes `user_can_access_project` on the owning project — §4.1
- Unauthorized/cross-org callers receive opaque `{"error": "repo_already_linked", "message": "This repo is already linked to another project."}` with NO project identifier — §4.1

**Implementation gate:** Test that org-A user probing an org-B remote sees no org-B project id.

### F4 — MEDIUM — Tombstone 410 Must Run Access Check on Source Before Disclosing Target

**Problem:** If the 410 tombstone response discloses `merged_into` (the target project ID) before running the access check, an unauthorized caller who knows a source remote learns the target project ID and confirms a merge occurred — a cross-tenant enumeration leak.

**Resolution (this document):**
- `GET /projects/{id}` resolves with `follow_tombstone=False` first; if tombstone detected, runs `user_can_access_project` on the SOURCE before disclosing `merged_into` — §5.9, §3.3 (A2)
- Unauthorized callers receive the same opaque 404/403 a non-tombstone inaccessible project returns — §5.9
- Mirror applies at the remote-based route (`GET /projects/{remote}`) — §3.3 (A2)

**Implementation gate:** Tests: source-owner → 410 + target; stranger → opaque 404, no target id.

### F5 — MEDIUM — Resolver Redirect Must Re-Authorize on Target + Hop-Cap

**Problem:** `resolve_project_by_remote` / `resolve_project_by_id` follow `merged_into_project_id` and return the *target* project. If a calling route trusts "I resolved a project, therefore the caller may use it," the tombstone redirect becomes a privilege-escalation bridge into the target's data.

**Resolution (this document):**
- Binding rule: `resolve_project_by_*` returns a project; it NEVER authorizes. Every Group-A site must keep its access check, and the check must run against the **resolved/redirected target** project — §3.3, §6.5
- Hop-cap ≤8 with logged error on both resolver helpers as defense-in-depth (preconditions prevent A→B→A, but data corruption could create a cycle) — §3.3
- Service-key corollary: `assert_service_key_can_access_project` must run on the resolved *target* id; a key scoped to source ∉ target allowlist → correctly denied — Appendix B.1, §3.3

**Implementation gate:** Tests: (a) following a tombstone does not grant target access the caller lacked; (b) service key scoped to source ∉ target allowlist is denied after merge.

### F6 — MEDIUM — Rate Limit Link + Merge (App-Layer + Forge Follow-Up)

**Problem:** The only limiter is `auth/rate_limit.py`'s `SlidingWindowRateLimiter` — in-memory, per-replica, insufficient for multi-replica Cloud Run. No rate limit on link or merge.

**Resolution (this document):**
- App-layer: per-user and per-project sliding-window caps on link, unlink, and merge (incl. dry-run) — §6.6
- Forge ticket: durable edge/multi-replica rate limiting (Cloud Armor / API Gateway) for link + merge routes — §6.6

**Implementation gate:** Test that burst of link/merge-dry-run is throttled. Forge ticket ID referenced in implementation commit.

### LOW — 4 Defense-in-Depth Items

| ID | Item | Resolution | Section |
|----|------|------------|---------|
| L1 | Audit security-relevant denials (cross-org merge, unauthorized link) via AdminAction | Promoted from optional to recommended in §5.1 design principles | §5.1 |
| L2 | `sfs security scan` parity for new perms/fields | Noted; no new secret files expected in v1 | §8.2 |
| L3 | Block merge when a project transfer is pending (either direction, either side) | Added to Phase 1 preconditions in merge pseudocode | §5.11, §5.12 |
| L4 | Link attestation snapshot survives user deletion | `added_by_user_id` FK is ON DELETE SET NULL for hygiene; audit row carries linker's user_id + email snapshot at link time | §5.1 |

### Sentinel Build Notes (Non-Blocking — Implementation Guidance)

These two notes from Sentinel's pre-build pass are folded here as implementation guidance for the engineer building the displacement and verification paths.

**N1 — GitHub App verification MUST use the linker's OWN installation token (confused-deputy defense).** The server resolves the GitHub App installation from the *authenticated user's* own installations — never from a global token or another user's installation. A user who installed the App on repo-A must not be able to use that installation's token to "verify" ownership of repo-B (which they don't control). Implementation: scope the installation lookup to `GET /user/installations` for the authenticated user, then use THAT installation's token for `GET /repos/{owner}/{repo}`. If the repo is not in the user's own installation set, verification fails → fall back to `owner_attested` (verified=false).

**N2 — GitHub API call is OUTSIDE the swap transaction (liveness).** The `GET /repos/{owner}/{repo}` verification call (installation token) is made BEFORE acquiring the `SELECT FOR UPDATE` lock on `project_repos`. The swap transaction (lock → DELETE → handle holder state → INSERT → commit) contains only DB writes — no live HTTP calls. Holding a row lock during a potentially slow network call to api.github.com would block concurrent link/merge operations and create a liveness risk. If the GitHub API call fails, the swap is never attempted.

### Residual Risk & Owners

- **Forged `git_remote_normalized` is a pre-existing platform weakness** (predates this feature; predicate #3 already trusts it for read access). This review hard-gates it for the *new* global-unique link/claim path (F1). A broader follow-up — verifying repo provenance at session-sync time, or down-weighting predicate #3 generally — is a separate Sentinel ticket. **Owner: Sentinel (follow-up ticket).**
- **Edge rate limiting** for link/merge — **Owner: Forge** (F6).
- **Design-doc amendments** for F1/F2/F3/F4/F5/F6 + LOW — **Owner: Atlas** (S1 amendment; complete).
- **Design-doc amendments for displacement coherence + resolver error + verified semantics** — **Owner: Atlas** (S2 amendment; this commit).
