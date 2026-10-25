"""Tracker mapping cleanup when a project leaves its org.

A project transfer (owner/admin/auto-accept) or a project merge moves a
project away from the org that owns its Jira mapping. The mapping row is
deleted here so the moved project is no longer mapped to the old org's Jira
project. Any ``ticket_tracker_links`` the project already has are deliberately
left in place and simply go stale: the routes' org/site check turns them into
409 ``jira_link_stale`` instead of letting a publish (or a status refresh)
write to an unrelated issue on the new org/site.
"""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import TrackerProjectMapping


async def drop_project_tracker_mapping(db: AsyncSession, project_id: str) -> None:
    """Delete the project's tracker mapping; leave any links stale."""
    await db.execute(
        delete(TrackerProjectMapping).where(
            TrackerProjectMapping.project_id == project_id
        )
    )
