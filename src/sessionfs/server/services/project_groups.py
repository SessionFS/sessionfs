"""Project-group lifecycle helpers shared by the routes and the services that
move projects or users (GitHub #101).

Every group mutation writes an ``OrgAuditEvent`` in the caller's
transaction. These helpers never commit.
"""

from __future__ import annotations

import json
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    OrgAuditEvent,
    Organization,
    ProjectGroup,
    ProjectGroupAccess,
    ProjectGroupMember,
)


def group_audit_event(
    *,
    org_id: str,
    org_name: str | None,
    actor_user_id: str | None,
    actor_email: str | None,
    actor_role: str | None,
    event_type: str,
    group_id: str,
    before: dict | None,
    after: dict | None,
) -> OrgAuditEvent:
    """An ``OrgAuditEvent`` row for a project-group mutation."""
    return OrgAuditEvent(
        id=f"oae_{uuid.uuid4().hex[:24]}",
        org_id=org_id,
        org_name_snapshot=org_name or "",
        event_type=event_type,
        actor_user_id=actor_user_id,
        actor_email_snapshot=actor_email,
        actor_role_at_time=actor_role,
        target_type="project_group",
        target_id=group_id,
        before=json.dumps(before, default=str) if before is not None else None,
        after=json.dumps(after, default=str) if after is not None else None,
    )


async def _org_names(db: AsyncSession, org_ids: set[str]) -> dict[str, str]:
    if not org_ids:
        return {}
    rows = await db.execute(
        select(Organization.id, Organization.name).where(Organization.id.in_(org_ids))
    )
    return {oid: name for oid, name in rows.all()}


async def remove_project_from_all_groups(
    db: AsyncSession,
    *,
    project_id: str,
    actor_user_id: str | None,
    actor_email: str | None,
    actor_role: str | None,
    reason: str,
) -> list[dict]:
    """Drop every group membership of ``project_id`` (e.g. it left its org),
    one ``project_group_project_removed`` audit row per membership."""
    rows = (
        await db.execute(
            select(ProjectGroupAccess.group_id, ProjectGroup.org_id)
            .join(ProjectGroup, ProjectGroup.id == ProjectGroupAccess.group_id)
            .where(ProjectGroupAccess.project_id == project_id)
            .order_by(ProjectGroupAccess.group_id)
        )
    ).all()
    if not rows:
        return []
    names = await _org_names(db, {org_id for _, org_id in rows})
    await db.execute(
        delete(ProjectGroupAccess).where(ProjectGroupAccess.project_id == project_id)
    )
    removed = []
    for group_id, org_id in rows:
        db.add(
            group_audit_event(
                org_id=org_id,
                org_name=names.get(org_id),
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                actor_role=actor_role,
                event_type="project_group_project_removed",
                group_id=group_id,
                before={"project_id": project_id},
                after={"project_id": None, "reason": reason},
            )
        )
        removed.append({"group_id": group_id, "org_id": org_id})
    return removed


async def revoke_user_group_grants(
    db: AsyncSession,
    *,
    user_id: str,
    org_id: str | None,
    actor_user_id: str | None,
    actor_email: str | None,
    actor_role: str | None,
    reason: str,
) -> list[dict]:
    """Delete ``user_id``'s group grants — in ``org_id``'s groups, or in every
    org when ``org_id`` is None — one ``project_group_member_revoked`` audit
    row per grant."""
    stmt = (
        select(ProjectGroupMember.group_id, ProjectGroupMember.role, ProjectGroup.org_id)
        .join(ProjectGroup, ProjectGroup.id == ProjectGroupMember.group_id)
        .where(ProjectGroupMember.user_id == user_id)
        .order_by(ProjectGroupMember.group_id)
    )
    if org_id is not None:
        stmt = stmt.where(ProjectGroup.org_id == org_id)
    rows = (await db.execute(stmt)).all()
    if not rows:
        return []
    names = await _org_names(db, {o for _, _, o in rows})
    await db.execute(
        delete(ProjectGroupMember).where(
            ProjectGroupMember.user_id == user_id,
            ProjectGroupMember.group_id.in_([g for g, _, _ in rows]),
        )
    )
    revoked = []
    for group_id, role, group_org_id in rows:
        db.add(
            group_audit_event(
                org_id=group_org_id,
                org_name=names.get(group_org_id),
                actor_user_id=actor_user_id,
                actor_email=actor_email,
                actor_role=actor_role,
                event_type="project_group_member_revoked",
                group_id=group_id,
                before={"user_id": user_id, "role": role},
                after={"user_id": user_id, "role": None, "reason": reason},
            )
        )
        revoked.append({"group_id": group_id, "role": role, "org_id": group_org_id})
    return revoked
