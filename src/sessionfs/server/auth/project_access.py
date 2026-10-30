"""User-key project access — the predicate the v0.10.0 Phase 5 design
documented and the v0.10.22 fix actually enforces.

A user key can reach a project iff ANY of these holds:

1. The user owns the project (`Project.owner_id == user_id`).
2. The project is org-scoped AND the user is a member of that org
   (`Project.org_id IS NOT NULL AND user_id IN
   OrgMember WHERE org_id = project.org_id`).
3. The user has captured at least one session linked to this project
   (`Session.user_id == user_id AND
   Session.project_id == project.id`), or on one of its repositories.
   P2 (§3.3 B1): switched from remote-based matching
   (Session.git_remote_normalized == Project.git_remote_normalized)
   to project_id-based matching.  After multi-repo,
   Project.git_remote_normalized is only the primary remote — users
   with sessions on non-primary repos would falsely fail the old check.

Those three branches are the READ tier (plus append-only CONTRIBUTE
actions such as adding knowledge entries, tickets and comments). The
EDIT tier — changing settings, rules, wiki pages, the context document,
curating the knowledge base, personas, work queues — is the first two
branches only: `user_can_edit_project` / `load_project_for_editor`
(tk_25872885e3274a7d). A captured-session-only reader gets 403
`project_edit_forbidden` there. `user_is_project_admin` (owner or org
admin) is the stricter tier above that for rename/link/merge/close.

Project groups + restricted visibility (GitHub #101): an org may opt
into `general.restrict_project_visibility` in its settings. When OFF
(the default) the rules above are unchanged. When ON, for that org's
projects:

  - READ: owner, org admin/owner, or a current org member holding a
    grant (reader/editor/admin) on a live group of the same org that
    contains the project. The captured-session branch (3) grants
    nothing in a restricted org — not to members, not to non-members.
  - EDIT: owner, org admin/owner, or a group grant of editor/admin.
  - A group grant never confers project-ADMIN standing
    (`user_is_project_admin` stays owner or org admin).
  - Unreadable org settings count as restricted (fail closed).

Every group-grant check joins the grantee's current `OrgMember` row in
the group's org, requires the group to be live (`deleted_at IS NULL`)
and the project to still belong to the group's org, so an orphaned or
stale grant grants nothing.

The set forms — `accessible_project_ids_subquery`,
`editable_project_ids_subquery` and `org_editor_user_ids_subquery` —
are the single source of truth for every batch check (project lists,
handoff attachments, retrieval audit, persona rollups, compile and
work-queue editor sets).

Service keys go through `assert_service_key_can_access_project` —
their boundary is `service_key.org_id`, NOT OrgMember. This helper
is user-key only.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql import Select

from sessionfs.server.db.models import (
    OrgMember,
    Organization,
    Project,
    ProjectGroup,
    ProjectGroupAccess,
    ProjectGroupMember,
    ProjectRepo,
    Session,
)
from sessionfs.server.roles import ADMIN_ROLES

# Group roles. ``admin`` manages the group; ``editor``/``admin`` confer edit
# standing on the group's projects; ``reader`` confers read standing.
GROUP_ROLE_READER = "reader"
GROUP_ROLE_EDITOR = "editor"
GROUP_ROLE_ADMIN = "admin"
GROUP_ROLES: tuple[str, ...] = (GROUP_ROLE_READER, GROUP_ROLE_EDITOR, GROUP_ROLE_ADMIN)
GROUP_EDIT_ROLES: tuple[str, ...] = (GROUP_ROLE_EDITOR, GROUP_ROLE_ADMIN)
_GROUP_ROLE_LEVEL = {GROUP_ROLE_READER: 1, GROUP_ROLE_EDITOR: 2, GROUP_ROLE_ADMIN: 3}


def group_role_level(role: str | None) -> int:
    """Rank of a group role (0 for none/unknown) — for keep-the-higher merges."""
    return _GROUP_ROLE_LEVEL.get(role or "", 0)


# ── Restriction resolution ──


def settings_restrict_visibility(settings: Any) -> bool:
    """Whether a stored org ``settings`` value restricts project visibility.

    Fails closed: settings that cannot be read (malformed JSON, a non-object
    document or ``general`` block, or a non-boolean flag) count as
    restricted. Empty/absent settings and an explicit ``false``/``null``
    flag are unrestricted."""
    if settings is None or settings == "":
        return False
    if isinstance(settings, str):
        try:
            raw = json.loads(settings)
        except (ValueError, TypeError):
            return True
    else:
        raw = settings
    if raw is None:
        return False
    if not isinstance(raw, dict):
        return True
    general = raw.get("general")
    if general is None:
        return False
    if not isinstance(general, dict):
        return True
    flag = general.get("restrict_project_visibility")
    if flag is None or flag is False:
        return False
    return True


def _restricts_visibility(org: Organization | None) -> bool:
    """True when the org's settings opt into restricted project visibility."""
    if org is None:
        return False
    return settings_restrict_visibility(org.settings)


async def org_restricts_visibility(db: AsyncSession, org_id: str) -> bool:
    """True when ``org_id`` opts into restricted project visibility."""
    org = await db.get(Organization, org_id)
    return _restricts_visibility(org)


async def project_restricts_visibility(db: AsyncSession, project: Project) -> bool:
    """True when the project's org (if any) restricts visibility."""
    if project.org_id is None:
        return False
    return await org_restricts_visibility(db, project.org_id)


async def restricted_org_ids(db: AsyncSession, org_ids: Iterable[str | None]) -> set[str]:
    """The subset of ``org_ids`` that restrict project visibility (one query)."""
    ids = {o for o in org_ids if o}
    if not ids:
        return set()
    rows = await db.execute(
        select(Organization.id, Organization.settings).where(Organization.id.in_(ids))
    )
    return {oid for oid, settings in rows.all() if settings_restrict_visibility(settings)}


async def _org_member_row(
    db: AsyncSession, org_id: str, user_id: str
) -> OrgMember | None:
    return (
        await db.execute(
            select(OrgMember).where(
                OrgMember.org_id == org_id, OrgMember.user_id == user_id
            )
        )
    ).scalar_one_or_none()


# ── Shared subqueries (the set forms of the predicates) ──


def group_grant_project_ids_subquery(
    user_id: str, roles: Iterable[str] | None = None
) -> Select:
    """Project ids the user reaches through a group grant.

    Only counts a grant when the group is live, the grantee is a CURRENT
    member of the group's org, and the project still belongs to that org.
    ``roles`` narrows to those grant roles (e.g. ``GROUP_EDIT_ROLES``)."""
    grant_project = aliased(Project)
    grantee_member = aliased(OrgMember)
    stmt = (
        select(ProjectGroupAccess.project_id)
        .join(ProjectGroup, ProjectGroup.id == ProjectGroupAccess.group_id)
        .join(
            ProjectGroupMember,
            ProjectGroupMember.group_id == ProjectGroupAccess.group_id,
        )
        .join(grant_project, grant_project.id == ProjectGroupAccess.project_id)
        .join(
            grantee_member,
            and_(
                grantee_member.org_id == ProjectGroup.org_id,
                grantee_member.user_id == ProjectGroupMember.user_id,
            ),
        )
        .where(
            ProjectGroupMember.user_id == user_id,
            ProjectGroup.deleted_at.is_(None),
            grant_project.org_id == ProjectGroup.org_id,
        )
    )
    if roles is not None:
        stmt = stmt.where(ProjectGroupMember.role.in_(tuple(roles)))
    return stmt.correlate(None)


async def _member_full_access_org_ids(
    db: AsyncSession, user_id: str
) -> tuple[list[str], set[str]]:
    """(orgs whose every project the user reads/edits by membership, all
    member org ids). Full access = admin/owner, or an unrestricted org."""
    rows = (
        await db.execute(
            select(OrgMember.org_id, OrgMember.role, Organization.settings)
            .join(Organization, Organization.id == OrgMember.org_id)
            .where(OrgMember.user_id == user_id)
        )
    ).all()
    full = sorted(
        org_id
        for org_id, role, settings in rows
        if role in ADMIN_ROLES or not settings_restrict_visibility(settings)
    )
    return full, {r[0] for r in rows}


async def accessible_project_ids_subquery(
    db: AsyncSession, user_id: str, *, include_captured: bool = True
) -> Select:
    """SELECT of every project id the user may READ (``user_can_access_project``
    as a set). Use as ``Project.id.in_(...)`` / ``X.project_id.in_(...)``.

    ``include_captured=False`` leaves out the captured-session branch for
    surfaces that never admitted it (retrieval audit)."""
    p = aliased(Project)
    full_orgs, _ = await _member_full_access_org_ids(db, user_id)
    conditions = [
        p.owner_id == user_id,
        p.id.in_(group_grant_project_ids_subquery(user_id)),
    ]
    if full_orgs:
        conditions.append(p.org_id.in_(full_orgs))

    if include_captured:
        captured = _captured_condition(p, user_id)
        # Restricted orgs never admit captured-session readers (GitHub #101):
        # resolve which of the candidate projects' orgs restrict.
        candidate_orgs = (
            await db.execute(
                select(p.org_id).where(captured, p.org_id.is_not(None)).distinct()
            )
        ).scalars().all()
        restricted = await restricted_org_ids(db, candidate_orgs)
        if restricted:
            captured = and_(
                captured,
                or_(p.org_id.is_(None), p.org_id.not_in(sorted(restricted))),
            )
        conditions.append(captured)
    return select(p.id).where(or_(*conditions)).correlate(None)


def _captured_condition(p, user_id: str):
    """Captured-session branch over the aliased project ``p``: a session the
    user owns is linked to it, or sits on its primary/linked repository.
    A repo-reclaimed project never matches by remote."""
    user_remotes = (
        select(Session.git_remote_normalized)
        .where(
            Session.user_id == user_id,
            Session.git_remote_normalized.is_not(None),
            Session.git_remote_normalized != "",
        )
        .correlate(None)
    )
    linked = (
        select(Session.project_id)
        .where(Session.user_id == user_id, Session.project_id.is_not(None))
        .correlate(None)
    )
    via_repo = (
        select(ProjectRepo.project_id)
        .where(ProjectRepo.git_remote_normalized.in_(user_remotes))
        .correlate(None)
    )
    return or_(
        p.id.in_(linked),
        and_(
            p.repo_reclaimed_at.is_(None),
            or_(
                p.git_remote_normalized.in_(user_remotes),
                p.id.in_(via_repo),
            ),
        ),
    )


async def editable_project_ids_subquery(db: AsyncSession, user_id: str) -> Select:
    """SELECT of every project id the user may EDIT (``user_can_edit_project``
    as a set): owner, member of an unrestricted org, org admin/owner, or a
    group editor/admin grant."""
    p = aliased(Project)
    full_orgs, _ = await _member_full_access_org_ids(db, user_id)
    conditions = [
        p.owner_id == user_id,
        p.id.in_(group_grant_project_ids_subquery(user_id, GROUP_EDIT_ROLES)),
    ]
    if full_orgs:
        conditions.append(p.org_id.in_(full_orgs))
    return select(p.id).where(or_(*conditions)).correlate(None)


def org_editor_user_ids_subquery(project: Project, *, restricted: bool) -> Select | None:
    """SELECT of the user ids that edit ``project`` through its org (the
    owner is separate). ``None`` for a personal project.

    Unrestricted: every org member. Restricted: org admins/owners plus
    current members holding an editor/admin grant on a live group of the
    org that contains the project."""
    if project.org_id is None:
        return None
    if not restricted:
        return (
            select(OrgMember.user_id)
            .where(OrgMember.org_id == project.org_id)
            .correlate(None)
        )
    grant_users = (
        select(ProjectGroupMember.user_id)
        .join(
            ProjectGroupAccess,
            ProjectGroupAccess.group_id == ProjectGroupMember.group_id,
        )
        .join(ProjectGroup, ProjectGroup.id == ProjectGroupAccess.group_id)
        .where(
            ProjectGroupAccess.project_id == project.id,
            ProjectGroupMember.role.in_(GROUP_EDIT_ROLES),
            ProjectGroup.deleted_at.is_(None),
            ProjectGroup.org_id == project.org_id,
        )
        .correlate(None)
    )
    # Filtering OrgMember rows of the project's org (== the group's org)
    # is the grantee-membership join: an orphan grant matches no row.
    return (
        select(OrgMember.user_id)
        .where(
            OrgMember.org_id == project.org_id,
            or_(
                OrgMember.role.in_(ADMIN_ROLES),
                OrgMember.user_id.in_(grant_users),
            ),
        )
        .correlate(None)
    )


# ── Single-project predicates ──


async def user_group_roles_for_project(
    db: AsyncSession, user_id: str, project: Project
) -> set[str]:
    """Every group role (reader/editor/admin) the user holds on a live group
    that contains ``project``, or the empty set. Empty for a personal project
    and for a user who is not a current member of the project's org."""
    if project.org_id is None:
        return set()
    rows = await db.execute(
        select(ProjectGroupMember.role)
        .join(
            ProjectGroupAccess,
            ProjectGroupAccess.group_id == ProjectGroupMember.group_id,
        )
        .join(ProjectGroup, ProjectGroup.id == ProjectGroupAccess.group_id)
        .join(
            OrgMember,
            and_(
                OrgMember.org_id == ProjectGroup.org_id,
                OrgMember.user_id == ProjectGroupMember.user_id,
            ),
        )
        .where(
            ProjectGroupAccess.project_id == project.id,
            ProjectGroupMember.user_id == user_id,
            ProjectGroup.org_id == project.org_id,
            ProjectGroup.deleted_at.is_(None),
        )
    )
    return set(rows.scalars().all())


async def user_group_role(
    db: AsyncSession, user_id: str, group: ProjectGroup
) -> str | None:
    """The user's effective role in ``group``.

    Org admins/owners have admin standing on every group in their org.
    Otherwise the explicit grant role applies — only while the user is a
    current member of the group's org. ``None`` means no standing."""
    member = await _org_member_row(db, group.org_id, user_id)
    if member is None:
        return None
    if member.role in ADMIN_ROLES:
        return GROUP_ROLE_ADMIN
    row = (
        await db.execute(
            select(ProjectGroupMember.role).where(
                ProjectGroupMember.group_id == group.id,
                ProjectGroupMember.user_id == user_id,
            )
        )
    ).scalar_one_or_none()
    return row


async def user_group_editor_project_ids(db: AsyncSession, user_id: str) -> set[str]:
    """Project ids the user can EDIT via an editor/admin group grant."""
    rows = await db.execute(group_grant_project_ids_subquery(user_id, GROUP_EDIT_ROLES))
    return set(rows.scalars().all())


async def user_has_read_standing(
    db: AsyncSession, user_id: str, project: Project
) -> bool:
    """Read standing WITHOUT the captured-session branch: owner, org member
    of an unrestricted org, org admin/owner, or a group grant."""
    if project.owner_id == user_id:
        return True
    if project.org_id is None:
        return False
    member = await _org_member_row(db, project.org_id, user_id)
    if member is None:
        return False
    if member.role in ADMIN_ROLES:
        return True
    if not await org_restricts_visibility(db, project.org_id):
        return True
    return bool(await user_group_roles_for_project(db, user_id, project))


def project_session_clause(project: Project, *, legacy_only: bool = False):
    """The SQL predicate for "a captured session belongs to this project".

    A session belongs to a project when its ``project_id`` points at the
    project, OR — for legacy sessions synced before project attribution —
    when its normalized git remote matches the project's primary remote or
    any of its ``ProjectRepo`` remotes. A repo-reclaimed project lost its
    repository to a verified owner: sessions on that remote no longer
    belong to it (only sessions explicitly attributed to it do). Its
    legacy column is also NULL — the equality predicate must never be
    added for a NULL remote, where SQLAlchemy would render it as `IS NULL`
    and match every session with a NULL remote.

    This is the single source of truth for both ``user_can_access_project``
    and the session helper's project-scoped session tools, so the two can
    never drift (v0.10.22 P2 §3.3 B1 multi-repo fix).

    ``legacy_only=True`` limits the remote branches to sessions with NO
    project attribution (``project_id IS NULL``): a session explicitly
    attributed to project B is never pulled into project A just because it
    sits on one of A's remotes.  The session helper uses this mode so a
    project-scoped conversation only reads that project's sessions; the
    access predicate keeps the wider default (any session on the remote
    grants captured-session read access, as before).
    """
    predicates = [Session.project_id == project.id]
    if project.repo_reclaimed_at is None and project.git_remote_normalized is not None:
        remote_match = or_(
            Session.git_remote_normalized == project.git_remote_normalized,
            Session.git_remote_normalized.in_(
                select(ProjectRepo.git_remote_normalized).where(
                    ProjectRepo.project_id == project.id,
                )
            ),
        )
        if legacy_only:
            remote_match = and_(Session.project_id.is_(None), remote_match)
        predicates.append(remote_match)
    return or_(*predicates)


async def user_can_access_project(
    db: AsyncSession, user_id: str, project: Project
) -> bool:
    """Return True if the user is allowed to read this project."""
    if await user_has_read_standing(db, user_id, project):
        return True
    # GitHub #101: in a restricted org the captured-session branch grants
    # nothing — members need standing, non-members get nothing.
    if await project_restricts_visibility(db, project):
        return False

    captured = (
        await db.execute(
            select(Session.id)
            .where(Session.user_id == user_id, project_session_clause(project))
            .limit(1)
        )
    ).scalar_one_or_none()
    return captured is not None


async def user_can_edit_project(
    db: AsyncSession, user_id: str, project: Project
) -> bool:
    """Return True if the user may change this project (edit/admin tier).

    Editors are the first two branches of ``user_can_access_project``:
    the project owner, or — for an org-scoped project — a member of
    that org (an ``OrgMember`` row is active membership; removal deletes
    the row, so there is no separate status column). Admins and owners
    of the org are members too.

    When the org restricts visibility, a plain member is no longer an
    editor by membership alone: only org admins/owners and group grants
    of editor/admin may change the project.

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
    member = await _org_member_row(db, project.org_id, user_id)
    if member is None:
        return False
    if member.role in ADMIN_ROLES:
        return True
    if not await org_restricts_visibility(db, project.org_id):
        return True
    roles = await user_group_roles_for_project(db, user_id, project)
    return bool(roles & set(GROUP_EDIT_ROLES))


async def project_editor_user_ids(db: AsyncSession, project: Project) -> set[str]:
    """Every user id with editor standing on the project — the set form of
    ``user_can_edit_project`` for batch checks.

    Owner + org members when the org is unrestricted; owner + org
    admins/owners + group editor/admins when it restricts visibility."""
    ids = {project.owner_id}
    sub = org_editor_user_ids_subquery(
        project, restricted=await project_restricts_visibility(db, project)
    )
    if sub is not None:
        ids.update((await db.execute(sub)).scalars().all())
    return ids


def editor_authored_clause(
    user_id_col, project: Project, actor_type_col=None, *, restricted: bool
):
    """SQL predicate: the row was written by an editor of ``project`` or by a
    service key (org-bound by its own boundary).

    Used to keep content contributed by captured-session readers out of the
    automatic paths (compile-time promotion / supersession, work-queue
    adoption) — an editor has to act on it explicitly first.

    ``restricted`` is required: callers resolve it with
    ``project_restricts_visibility`` so the editor set always follows the
    org's current setting (a plain member is a reader, not an editor, when
    the org restricts visibility).
    """
    conditions = [user_id_col == project.owner_id]
    if actor_type_col is not None:
        conditions.append(actor_type_col == "service_key")
    sub = org_editor_user_ids_subquery(project, restricted=restricted)
    if sub is not None:
        conditions.append(user_id_col.in_(sub))
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

    A group grant (including group admin) does NOT confer project-ADMIN
    standing (GitHub #101).

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


async def user_can_view_spend_dollars(
    db: AsyncSession, user_id: str, project: Project
) -> bool:
    """Return True if the user may see dollar figures on agent spend.

    Review Loop R3 (agent-run spend): dollars are visible to org
    owners/admins of the project's org, or to the owner of a personal
    (org-less) project. Everyone else with read access sees token totals
    only. Narrower than ``user_is_project_admin`` — the owner of an
    org-scoped project does NOT see dollars unless they also hold an
    org admin/owner role.
    """
    if project.org_id is None:
        return project.owner_id == user_id
    admin = (
        await db.execute(
            select(OrgMember.user_id).where(
                OrgMember.org_id == project.org_id,
                OrgMember.user_id == user_id,
                OrgMember.role.in_(ADMIN_ROLES),
            )
        )
    ).scalar_one_or_none()
    return admin is not None


async def list_accessible_projects(
    db: AsyncSession, user_id: str
) -> list[Project]:
    """Return every project the user-key access predicate admits, in the
    projects-list route's ordering (most recently updated first).

    This is the single source of truth for both ``GET /api/v1/projects``
    and the session helper's ``list_projects`` tool, so the two listings
    can never drift. It is ``accessible_project_ids_subquery`` — the set
    form of ``user_can_access_project`` — minus merge tombstones
    (GitHub #111). A merged source stays readable (direct GETs answer 410
    with ``merged_into``, and its audit history stays reachable), but it is
    no longer a project anyone can pick, so listings leave it out.
    """
    stmt = (
        select(Project)
        .where(
            Project.id.in_(await accessible_project_ids_subquery(db, user_id)),
            Project.merged_into_project_id.is_(None),
        )
        .order_by(Project.updated_at.desc())
    )
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
