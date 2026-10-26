"""Transaction-scoped locks on a project or an org (GH #70).

Several invariants span rows that a row lock cannot protect, because the
racing writer INSERTS a new row (a phantom) rather than updating one:

* case-insensitive persona-name uniqueness per project (concurrent creates,
  project merge renames);
* the project-transfer stranded-ticket guard vs. ticket assignment and
  persona create/delete in the project or its org;
* the org-persona delete guard vs. ticket assignment in the org's projects;
* a user's org memberships: every membership add or remove for a user
  (invite accept, domain auto-join, SSO JIT, activation, org create, admin
  create/delete, member removal) takes that USER's lock, so a user delete
  that snapshots the user's orgs cannot miss a membership inserted
  concurrently.

Every check-then-write path on those invariants takes the matching scope
lock first and holds it until its transaction commits or rolls back.

* PostgreSQL: ``pg_advisory_xact_lock`` on a stable 64-bit key derived from
  ("project"|"org", id). Re-entrant within a transaction, released at
  commit/rollback.
* SQLite: a no-op ``UPDATE <table> SET id = id WHERE id = :id``, which takes
  the database writer lock for the rest of the transaction (SQLite has a
  single writer, so this serializes the same paths).

GLOBAL LOCK ORDER (deadlock freedom — every path must follow it):

    1. project scope locks, sorted by project id
    2. user scope locks, sorted by user id
    3. org scope locks, sorted by org id
    4. row locks (SELECT ... FOR UPDATE, UPDATE/INSERT/DELETE of rows,
       including an autoflush of pending ORM changes)

Take every lock of a class in ONE `lock_scopes` call (it sorts), never
one at a time in discovery order.

On file-backed SQLite the scope lock IS the database writer lock, so a
path that must write from a SECOND session (merge's audit rows) does that
write before taking the scope lock, and rolls the primary session back
before writing again from the second session.

Never take a scope lock after a row lock in the same transaction: take the
scope lock before the first write or FOR UPDATE (e.g. update_ticket takes
it before its lease-epoch UPDATE). Paths that only touch org personas take
only the org lock and never a project lock afterwards. When a path must
read a project to learn its org, lock the project, re-read it, then lock
the org (lock_project_scope).
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable

from fastapi import HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Project

_TABLES = {"project": "projects", "user": "users", "org": "organizations"}


def advisory_key(kind: str, key: str) -> int:
    """Stable signed 64-bit key for pg_advisory_xact_lock."""
    digest = hashlib.blake2b(f"sfs:{kind}:{key}".encode(), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def _dialect(db: AsyncSession) -> str:
    return db.get_bind().dialect.name


async def _lock_one(db: AsyncSession, kind: str, key: str) -> None:
    if _dialect(db) == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:k)"), {"k": advisory_key(kind, key)}
        )
    else:
        table = _TABLES[kind]
        await db.execute(
            text(f"UPDATE {table} SET id = id WHERE id = :k"),  # noqa: S608 - fixed table names
            {"k": key},
        )


async def lock_scopes(
    db: AsyncSession,
    *,
    project_ids: Iterable[str | None] = (),
    user_ids: Iterable[str | None] = (),
    org_ids: Iterable[str | None] = (),
) -> None:
    """Take project locks, then user locks, then org locks (each sorted)."""
    for pid in sorted({p for p in project_ids if p}):
        await _lock_one(db, "project", pid)
    for uid in sorted({u for u in user_ids if u}):
        await _lock_one(db, "user", uid)
    for oid in sorted({o for o in org_ids if o}):
        await _lock_one(db, "org", oid)


async def lock_project_scope(
    db: AsyncSession,
    project: Project,
    *,
    extra_org_ids: Iterable[str | None] = (),
    require_live: bool = True,
) -> None:
    """Lock the project, re-read it (its org may have changed while we
    waited — e.g. a transfer committed), then lock its current org plus any
    extra orgs.

    With `require_live` (default), refuse (409 project_merged) when the
    project was merged into another one while we waited: merge relocates
    the project's rows under this same lock, so a write that proceeded
    would attach a late row to the tombstoned source.
    """
    await lock_scopes(db, project_ids=[project.id])
    # Re-SELECT under the lock instead of `db.refresh`: a concurrent hard
    # delete (delete_project does NOT take the scope lock) can commit while
    # we wait on the advisory lock, so `db.refresh` would re-SELECT the gone
    # row and raise ObjectDeletedError (an unhandled 500). Re-SELECTing lets
    # us surface a clean 404 — the same shape merge.py uses. populate_existing
    # refreshes the caller's ORM instance in place, so callers that re-run
    # access checks against `project` see the post-wait org/owner.
    refreshed = (
        await db.execute(
            select(Project)
            .where(Project.id == project.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if refreshed is None:
        raise HTTPException(status_code=404, detail="Project not found")
    project = refreshed
    if require_live and project.merged_into_project_id is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "project_merged",
                "message": (
                    "This project was merged into another project; write to "
                    "the merge target instead."
                ),
                "merged_into_project_id": project.merged_into_project_id,
            },
        )
    await lock_scopes(db, org_ids=[project.org_id, *extra_org_ids])


async def lock_project_scope_authorized(
    db: AsyncSession,
    project: Project,
    *,
    auth=None,
    user_id: str | None = None,
    ctx=None,
    feature: str | None = None,
    extra_org_ids: Iterable[str | None] = (),
    edit: bool = False,
) -> None:
    """`lock_project_scope`, then RE-AUTHORIZE against the refreshed project.

    The caller's earlier access/tier checks ran before it waited for the
    lock; a transfer (to an org the caller can't reach, or on a lower plan)
    may have committed meanwhile. Under the lock this re-runs, against the
    re-read project:

    * the project access check — for a service key the org/allowlist
      boundary (`assert_service_key_can_access_project`), otherwise the
      user-key predicate (`load_project_for_user`: owner, org member or
      captured session; 404 when no longer accessible) — or, with
      ``edit=True``, the editor predicate (`load_project_for_editor`:
      owner or org member; 403 ``project_edit_forbidden`` for a reader);
    * the feature/tier gate (`check_project_feature`) when `feature` is
      given (requires `ctx`).

    Pass `auth` (AuthContext) or `user_id`.
    """
    await lock_project_scope(db, project, extra_org_ids=extra_org_ids)

    from sessionfs.server.auth.dependencies import assert_service_key_can_access_project
    from sessionfs.server.auth.project_access import (
        load_project_for_editor,
        load_project_for_user,
    )

    if auth is not None and auth.key_kind == "service":
        await assert_service_key_can_access_project(db, auth, project)
    else:
        uid = auth.user.id if auth is not None else user_id
        if edit and uid is not None:
            await load_project_for_editor(project.id, db, uid)
        else:
            await load_project_for_user(project.id, db, uid)
    if feature is not None:
        from sessionfs.server.tier_gate import check_project_feature

        assert ctx is not None, "feature gate re-check needs the UserContext"
        await check_project_feature(ctx, feature, project, db)
