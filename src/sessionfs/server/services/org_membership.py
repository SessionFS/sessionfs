"""Multi-org membership helpers.

A person can belong to several organizations. When a request names an org
(path, project or scope) that org is used; when it doesn't, the user's
*default org* is resolved at read time: ``users.default_org_id`` while they
are still a member there, else their earliest-joined membership (``ORDER BY
joined_at, org_id``) — ``tier_gate.get_user_org_membership``.

Joining an org never writes ``default_org_id``; only the explicit
``PUT /api/v1/auth/me/default-org`` does, and member removal clears it when
it pointed at the removed org.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import OrgMember, Organization


async def list_memberships(db: AsyncSession, user_id: str) -> list[tuple[OrgMember, Organization]]:
    """Every membership of the user with its org, earliest joined first."""
    return [
        (m, o)
        for m, o in (
            await db.execute(
                select(OrgMember, Organization)
                .join(Organization, Organization.id == OrgMember.org_id)
                .where(OrgMember.user_id == user_id)
                .order_by(OrgMember.joined_at, OrgMember.org_id)
            )
        ).all()
    ]


async def member_org_ids(db: AsyncSession, user_id: str) -> list[str]:
    """Ids of every org the user belongs to (sorted)."""
    return sorted(
        (
            await db.execute(select(OrgMember.org_id).where(OrgMember.user_id == user_id))
        ).scalars().all()
    )
