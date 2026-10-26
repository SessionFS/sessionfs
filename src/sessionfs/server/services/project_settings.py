"""Project settings mutations shared by the settings route and the helper.

``PUT /api/v1/projects/{id}/settings`` and the session helper's
``set_project_auto_narrative`` proposal + confirm path run the SAME rule, so
the two can never drift:

1. ``load_project_for_settings`` — only project editors (the owner, or a
   member of the project's org) may edit the project's settings.  Raises 404
   (unknown) / 403 (no access), and 403 ``project_edit_forbidden`` for a
   reader whose only access is a session on the project's repo.
2. ``check_auto_narrative_gate`` — flipping auto-narrative needs the
   ``project_context`` feature on the project's plan.  Raises 403
   ``upgrade_required``.
3. ``set_project_auto_narrative`` — gate + mutation.  The caller owns the
   commit.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.project_access import load_project_for_editor
from sessionfs.server.db.models import Project
from sessionfs.server.tier_gate import UserContext, check_project_feature

# The feature that pays for compiling a project's context (and therefore for
# its auto-narrative).
AUTO_NARRATIVE_FEATURE = "project_context"


async def load_project_for_settings(
    project_id: str, db: AsyncSession, user_id: str
) -> Project:
    """The project, if ``user_id`` may edit its settings (else 404/403)."""
    return await load_project_for_editor(project_id, db, user_id)


async def check_auto_narrative_gate(
    project: Project, *, ctx: UserContext, db: AsyncSession
) -> None:
    """Raise 403 ``upgrade_required`` unless the project may flip auto-narrative."""
    await check_project_feature(ctx, AUTO_NARRATIVE_FEATURE, project, db)


async def set_project_auto_narrative(
    project: Project,
    *,
    enabled: bool,
    ctx: UserContext,
    db: AsyncSession,
) -> None:
    """Gate + set ``project.auto_narrative`` (the shared mutation)."""
    await check_auto_narrative_gate(project, ctx=ctx, db=db)
    project.auto_narrative = enabled
    project.updated_at = datetime.now(timezone.utc)
