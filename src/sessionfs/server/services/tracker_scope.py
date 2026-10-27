"""Integration mapping cleanup when a project leaves its org.

A project transfer (owner/admin/auto-accept) or a project merge moves a
project away from the org that owns its integration mappings. Both the Jira
mapping and the Confluence space mapping are deleted here so the moved
project is no longer mapped to the old org's Jira project or Confluence
space.

Jira ``ticket_tracker_links`` are deliberately left in place and simply go
stale: the routes' org/site check turns them into 409 ``jira_link_stale``
instead of letting a publish (or a status refresh) write to an unrelated
issue on the new org/site.

Confluence ``doc_page_links`` are deleted with the mapping: they point at
pages in the OLD org's space, which the moved project has no business
updating, and an in-flight publish that still holds a claim loses its fenced
settle write (it reports ``publish_superseded`` instead of re-linking). A new
mapping in the new org starts from fresh pages.
"""

from __future__ import annotations

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import DocPageLink, DocSpaceMapping, TrackerProjectMapping


async def drop_project_integration_mappings(db: AsyncSession, project_id: str) -> None:
    """Delete the project's Jira and Confluence mappings.

    Jira links are left stale (409 ``jira_link_stale``); Confluence page
    links are dropped with their mapping.
    """
    await db.execute(
        delete(TrackerProjectMapping).where(
            TrackerProjectMapping.project_id == project_id
        )
    )
    await drop_project_doc_mapping(db, project_id)


async def drop_project_doc_mapping(db: AsyncSession, project_id: str) -> None:
    """Delete the project's Confluence space mapping and its page links."""
    await db.execute(
        delete(DocPageLink).where(DocPageLink.project_id == project_id)
    )
    await db.execute(
        delete(DocSpaceMapping).where(DocSpaceMapping.project_id == project_id)
    )
