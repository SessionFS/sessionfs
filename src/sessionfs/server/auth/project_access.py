"""User-key project access — the predicate the v0.10.0 Phase 5 design
documented and the v0.10.22 fix actually enforces.

A user key can reach a project iff ANY of these holds:

1. The user owns the project (`Project.owner_id == user_id`).
2. The project is org-scoped AND the user is a member of that org
   (`Project.org_id IS NOT NULL AND user_id IN
   OrgMember WHERE org_id = project.org_id`).
3. The user has captured at least one session linked to this project
   (`Session.user_id == user_id AND
   Session.project_id == project.id`).
   P2 (§3.3 B1): switched from remote-based matching
   (Session.git_remote_normalized == Project.git_remote_normalized)
   to project_id-based matching.  After multi-repo,
   Project.git_remote_normalized is only the primary remote — users
   with sessions on non-primary repos would falsely fail the old check.

The third predicate is the one the codebase has always enforced.
Predicates 1 and 2 are what `db/models.py:187` documented and what
`routes/knowledge.py:280-298` + `routes/wiki.py:123-139` never got
around to implementing.

Those three branches are the READ tier (plus append-only CONTRIBUTE
actions such as adding knowledge entries, tickets and comments). The
EDIT tier — changing settings, rules, wiki pages, the context document,
curating the knowledge base, personas, work queues — is the first two
branches only: `user_can_edit_project` / `load_project_for_editor`
(tk_25872885e3274a7d). A captured-session-only reader gets 403
`project_edit_forbidden` there. `user_is_project_admin` (owner or org
admin) is the stricter tier above that for rename/link/merge/close.

Service keys go through `assert_service_key_can_access_project` —
their boundary is `service_key.org_id`, NOT OrgMember. This helper
is user-key only.
"""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import distinct, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import OrgMember, Project, ProjectRepo, Session
from sessionfs.server.roles import ADMIN_ROLES


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

    # P2 (§3.3 B1): captured-session access via project_id OR
    # primary-remote match OR remote-match through project_repos.
    # A user with a session on any of the project's linked repos
    # — including legacy sessions with NULL project_id — should
    # have access.  The primary remote may not have a ProjectRepo
    # row yet, so Project.git_remote_normalized is included as a
    # direct fallback alongside the multi-repo join table.
    # A repo-reclaimed project lost its repository to a verified owner:
    # sessions on that remote no longer grant access to it (only sessions
    # explicitly attributed to it do). Its legacy column is also NULL —
    # the equality predicate must never be added for a NULL remote, where
    # SQLAlchemy would render it as `IS NULL` and match every session with
    # a NULL remote.
    predicates = [Session.project_id == project.id]
    if project.repo_reclaimed_at is None and project.git_remote_normalized is not None:
        predicates += [
            Session.git_remote_normalized == project.git_remote_normalized,
            Session.git_remote_normalized.in_(
                select(ProjectRepo.git_remote_normalized).where(
                    ProjectRepo.project_id == project.id,
                )
            ),
        ]
    captured = (
        await db.execute(
            select(Session.id)
            .where(Session.user_id == user_id, or_(*predicates))
            .limit(1)
        )
    ).scalar_one_or_none()
    return captured is not None


async def user_can_edit_project(
    db: AsyncSession, user_id: str, project: Project
) -> bool:
    """Return True if the user may change this project (edit/admin tier).

    Editors are the first two branches of ``user_can_access_project``:
    the project owner, or — for an org-scoped project — any member of
    that org (an ``OrgMember`` row is active membership; removal deletes
    the row, so there is no separate status column). Admins and owners
    of the org are members too.

    Captured-session access (branch 3) is READ + append-only CONTRIBUTE:
    anyone who syncs a session from the same repository can read the
    project and add to it (knowledge entries, tickets, comments, audit
    rows), but must not change its configuration or overwrite/delete
    shared content. ``user_is_project_admin`` is the stricter tier above
    this one (owner or org admin).
    """
    if project.owner_id == user_id:
        return True
    if project.org_id is None:
        return False
    member = (
        await db.execute(
            select(OrgMember.user_id).where(
                OrgMember.org_id == project.org_id,
                OrgMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    return member is not None


async def project_editor_user_ids(db: AsyncSession, project: Project) -> set[str]:
    """Every user id with editor standing on the project (owner + org
    members) — the set form of ``user_can_edit_project`` for batch checks."""
    ids = {project.owner_id}
    if project.org_id is not None:
        rows = await db.execute(
            select(OrgMember.user_id).where(OrgMember.org_id == project.org_id)
        )
        ids.update(r[0] for r in rows.all())
    return ids


def editor_authored_clause(user_id_col, project: Project, actor_type_col=None):
    """SQL predicate: the row was written by an editor of ``project`` (owner
    or org member) or by a service key (org-bound by its own boundary).

    Used to keep content contributed by captured-session readers out of the
    automatic paths (compile-time promotion / supersession, work-queue
    adoption) — an editor has to act on it explicitly first.
    """
    conditions = [user_id_col == project.owner_id]
    if actor_type_col is not None:
        conditions.append(actor_type_col == "service_key")
    if project.org_id is not None:
        conditions.append(
            user_id_col.in_(
                select(OrgMember.user_id).where(OrgMember.org_id == project.org_id)
            )
        )
    return or_(*conditions)


async def user_is_project_admin(
    db: AsyncSession, user_id: str, project: Project
) -> bool:
    """Return True if the user has admin standing on this project.

    Trusted-actor signal (v0.10.24 tk_dbccde26ed604b3c, Codex R1 MED #1):
    admin standing is the project owner OR — when the project is
    org-scoped — an org member at admin level or above (admin or owner;
    owners rank above admins since v0.11.0) of that org. This is
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
                    OrgMember.role.in_(ADMIN_ROLES),
                )
            )
        ).scalar_one_or_none()
        if admin is not None:
            return True
    return False


async def list_accessible_projects(
    db: AsyncSession, user_id: str
) -> list[Project]:
    """Return every project the user-key access predicate admits, in the
    projects-list route's ordering (most recently updated first).

    This is the single source of truth for both ``GET /api/v1/projects``
    and the session helper's ``list_projects`` tool, so the two listings
    can never drift. Predicate:
      - owner
      - member of the project's org (v0.10.22 fix)
      - has captured a session on the project's primary or linked git
        remote (legacy fallback so personal projects surface for
        teammates who synced on the same repo)
    """
    session_remotes_stmt = select(distinct(Session.git_remote_normalized)).where(
        Session.user_id == user_id,
        Session.git_remote_normalized.isnot(None),
        Session.git_remote_normalized != "",
    )
    result = await db.execute(session_remotes_stmt)
    user_remotes = {r[0] for r in result.all()}

    user_org_ids_stmt = select(OrgMember.org_id).where(OrgMember.user_id == user_id)
    conditions = [
        Project.owner_id == user_id,
        Project.org_id.in_(user_org_ids_stmt),
    ]
    if user_remotes:
        multi_repo_match = select(ProjectRepo.project_id).where(
            ProjectRepo.git_remote_normalized.in_(user_remotes)
        )
        conditions.append(or_(
            Project.git_remote_normalized.in_(user_remotes),  # legacy fallback
            Project.id.in_(multi_repo_match),                  # multi-repo path
        ))
    stmt = select(Project).where(or_(*conditions)).order_by(Project.updated_at.desc())
    result = await db.execute(stmt)
    return list(result.scalars().all())


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


PROJECT_EDIT_FORBIDDEN_MESSAGE = (
    "You can view this project but not change it. Only the project owner "
    "or a member of the project's organization can change it."
)


def project_edit_forbidden() -> HTTPException:
    """The 403 raised when a reader who is not an editor tries to change a
    project. Structured so the error envelope carries
    ``code=project_edit_forbidden``."""
    return HTTPException(
        403,
        {"code": "project_edit_forbidden", "message": PROJECT_EDIT_FORBIDDEN_MESSAGE},
    )


async def require_project_editor(
    db: AsyncSession, user_id: str, project: Project
) -> None:
    """Raise ``project_edit_forbidden`` unless the user is an editor."""
    if not await user_can_edit_project(db, user_id, project):
        raise project_edit_forbidden()


async def load_project_for_editor(
    project_id: str, db: AsyncSession, user_id: str
) -> Project:
    """Fetch a project for a change (edit/admin tier).

    Same not-found / no-access responses as ``load_project_for_user``
    (so a caller who cannot even read the project learns nothing new),
    then 403 ``project_edit_forbidden`` when the caller can read the
    project only through a captured session.
    """
    project = await load_project_for_user(project_id, db, user_id)
    await require_project_editor(db, user_id, project)
    return project
