"""User-key project access — the predicate the v0.10.0 Phase 5 design
documented and the v0.10.22 fix actually enforces.

A user key can reach a project iff ANY of these holds:

1. The user owns the project (`Project.owner_id == user_id`).
2. The project is org-scoped AND the user is a member of that org
   (`Project.org_id IS NOT NULL AND user_id IN
   OrgMember WHERE org_id = project.org_id`).
3. The user has captured at least one session on the project's git
   remote (`Session.user_id == user_id AND
   Session.git_remote_normalized == project.git_remote_normalized`).
   Kept as the legacy fallback so personal projects (org_id IS NULL)
   still grant access to teammates who synced on the same repo
   before the org-scoping work landed.

The third predicate is the one the codebase has always enforced.
Predicates 1 and 2 are what `db/models.py:187` documented and what
`routes/knowledge.py:280-298` + `routes/wiki.py:123-139` never got
around to implementing.

Service keys go through `assert_service_key_can_access_project` —
their boundary is `service_key.org_id`, NOT OrgMember. This helper
is user-key only.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import OrgMember, Project, Session


async def user_can_access_project(
    db: AsyncSession, user_id: str, project: Project
) -> bool:
    """Return True if the user is allowed to read this project."""
    if project.owner_id == user_id:
        return True

    if project.org_id is not None:
        member = (
            await db.execute(
                select(OrgMember.user_id).where(
                    OrgMember.org_id == project.org_id,
                    OrgMember.user_id == user_id,
                )
            )
        ).scalar_one_or_none()
        if member is not None:
            return True

    captured = (
        await db.execute(
            select(Session.id)
            .where(
                Session.user_id == user_id,
                Session.git_remote_normalized == project.git_remote_normalized,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return captured is not None


async def user_is_project_admin(
    db: AsyncSession, user_id: str, project: Project
) -> bool:
    """Return True if the user has admin standing on this project.

    Trusted-actor signal (v0.10.24 tk_dbccde26ed604b3c, Codex R1 MED #1):
    admin standing is the project owner OR — when the project is
    org-scoped — an `OrgMember.role == 'admin'` of that org. This is
    the actor-side check; do NOT authorize on mutable request input
    like `assigned_to`.

    Service keys are NOT admins via this helper. Service-key admin
    operations gate via the explicit `admin:*` scope.
    """
    if project.owner_id == user_id:
        return True
    if project.org_id is not None:
        admin = (
            await db.execute(
                select(OrgMember.user_id).where(
                    OrgMember.org_id == project.org_id,
                    OrgMember.user_id == user_id,
                    OrgMember.role == "admin",
                )
            )
        ).scalar_one_or_none()
        if admin is not None:
            return True
    return False


async def load_project_for_user(
    project_id: str, db: AsyncSession, user_id: str | None
) -> Project:
    """Fetch a project enforcing the user-key access predicate.

    Raises 404 if the project does not exist. Raises 403 if `user_id`
    is supplied and `user_can_access_project` returns False. Passing
    `user_id=None` skips the access check — only legitimate when the
    caller has its own boundary (e.g. service-key paths that already
    ran `assert_service_key_can_access_project`).
    """
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(404, "Project not found")

    if user_id is None:
        return project

    if not await user_can_access_project(db, user_id, project):
        raise HTTPException(403, "No access to this project")

    return project
