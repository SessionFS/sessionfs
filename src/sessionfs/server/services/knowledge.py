"""Extract knowledge entries from session summaries."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from sessionfs.server.db.models import KnowledgeEntry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from sessionfs.server.services.summarizer import SessionSummary

logger = logging.getLogger("sessionfs.knowledge")


async def extract_knowledge_entries(
    session_id: str,
    summary: SessionSummary,
    project_id: str,
    user_id: str,
    db: AsyncSession,
) -> list[KnowledgeEntry]:
    """Extract knowledge entries from a session summary.

    Extraction rules:
    - Files created -> "pattern" entries (confidence 0.5)
    - Tests failing -> "bug" entries (confidence 0.7)
    - Packages installed -> "dependency" entries (confidence 0.9)
    - key_decisions from narrative -> "decision" entries (confidence 0.8)
    - open_issues from narrative -> "bug" entries (confidence 0.7)
    """
    entries: list[KnowledgeEntry] = []

    # Files modified -> pattern entries
    for file_path in summary.files_modified:
        entry = KnowledgeEntry(
            project_id=project_id,
            session_id=session_id,
            user_id=user_id,
            entry_type="pattern",
            content=f"File created/modified: {file_path}",
            confidence=0.5,
            source_context=f"Session {session_id} modified {file_path}",
        )
        entries.append(entry)

    # Tests failing -> bug entries
    if summary.tests_failed > 0:
        errors_text = "; ".join(summary.errors_encountered[:3]) if summary.errors_encountered else "unknown"
        entry = KnowledgeEntry(
            project_id=project_id,
            session_id=session_id,
            user_id=user_id,
            entry_type="bug",
            content=f"{summary.tests_failed} test(s) failing: {errors_text}",
            confidence=0.7,
            source_context=f"Session {session_id}: {summary.tests_run} tests run, {summary.tests_failed} failed",
        )
        entries.append(entry)

    # Packages installed -> dependency entries
    for package in summary.packages_installed:
        entry = KnowledgeEntry(
            project_id=project_id,
            session_id=session_id,
            user_id=user_id,
            entry_type="dependency",
            content=f"Package installed: {package}",
            confidence=0.9,
            source_context=f"Session {session_id} installed {package}",
        )
        entries.append(entry)

    # key_decisions from narrative -> decision entries
    if summary.key_decisions:
        for decision in summary.key_decisions:
            entry = KnowledgeEntry(
                project_id=project_id,
                session_id=session_id,
                user_id=user_id,
                entry_type="decision",
                content=decision,
                confidence=0.8,
                source_context=f"Session {session_id} narrative key_decisions",
            )
            entries.append(entry)

    # open_issues from narrative -> bug entries
    if summary.open_issues:
        for issue in summary.open_issues:
            entry = KnowledgeEntry(
                project_id=project_id,
                session_id=session_id,
                user_id=user_id,
                entry_type="bug",
                content=issue,
                confidence=0.7,
                source_context=f"Session {session_id} narrative open_issues",
            )
            entries.append(entry)

    # Persist all entries
    if entries:
        for entry in entries:
            db.add(entry)
        await db.commit()
        logger.info(
            "Extracted %d knowledge entries from session %s for project %s",
            len(entries),
            session_id,
            project_id,
        )

    return entries
