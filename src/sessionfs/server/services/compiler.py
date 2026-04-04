"""Compile pending knowledge entries into project context via LLM."""

from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select

from sessionfs.server.db.models import ContextCompilation, KnowledgeEntry, Project

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("sessionfs.compiler")

_COMPILE_SYSTEM_PROMPT = """\
You are a project context compiler. Given the current project context document \
and a set of new knowledge entries grouped by type, produce an updated context \
document that incorporates the new information.

Rules:
- Preserve the existing structure and headings
- Add new information under the appropriate section
- Remove duplicates (same fact stated differently)
- Keep it concise — this document is injected into every session
- Output ONLY the updated context document, nothing else"""


def _build_compile_prompt(context: str, grouped_entries: dict[str, list[str]]) -> str:
    """Build the user prompt for compilation."""
    parts = ["## Current Project Context", context or "(empty)", "", "## New Knowledge Entries"]
    for entry_type, contents in sorted(grouped_entries.items()):
        parts.append(f"\n### {entry_type.title()} ({len(contents)} entries)")
        for c in contents:
            parts.append(f"- {c}")
    parts.append("\n## Task")
    parts.append("Produce the updated project context document.")
    return "\n".join(parts)


async def compile_project_context(
    project_id: str,
    user_id: str,
    db: AsyncSession,
    api_key: str | None = None,
    model: str = "claude-sonnet-4",
    provider: str | None = None,
    base_url: str | None = None,
) -> ContextCompilation | None:
    """Compile pending knowledge entries into project context via LLM.

    Steps:
    1. Get current project context
    2. Get pending entries (compiled_at IS NULL, dismissed = FALSE)
    3. Group by type
    4. Call LLM to merge into existing context
    5. Save updated context
    6. Mark entries as compiled
    7. Save compilation record (before/after snapshots)
    """
    # 1. Get current project context
    result = await db.execute(
        select(Project).where(Project.id == project_id)
    )
    project = result.scalar_one_or_none()
    if not project:
        logger.warning("Project %s not found for compilation", project_id)
        return None

    # 2. Get pending entries
    result = await db.execute(
        select(KnowledgeEntry).where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.compiled_at.is_(None),
            KnowledgeEntry.dismissed == False,  # noqa: E712
        )
    )
    pending = list(result.scalars().all())

    if not pending:
        logger.info("No pending entries for project %s", project_id)
        return None

    # 3. Group by type
    grouped: dict[str, list[str]] = defaultdict(list)
    for entry in pending:
        grouped[entry.entry_type].append(entry.content)

    # 4. Call LLM to merge
    context_before = project.context_document or ""

    if not api_key:
        # Without an API key, do a simple append-based compilation
        context_after = _simple_compile(context_before, grouped)
    else:
        from sessionfs.judge.providers import call_llm

        prompt = _build_compile_prompt(context_before, grouped)
        try:
            context_after = await call_llm(
                model=model,
                system=_COMPILE_SYSTEM_PROMPT,
                prompt=prompt,
                api_key=api_key,
                provider=provider,
                base_url=base_url,
            )
            context_after = context_after.strip()
        except Exception:
            logger.warning("LLM compilation failed, falling back to simple compile", exc_info=True)
            context_after = _simple_compile(context_before, grouped)

    # 5. Save updated context
    now = datetime.now(timezone.utc)
    project.context_document = context_after
    project.updated_at = now

    # 6. Mark entries as compiled
    for entry in pending:
        entry.compiled_at = now

    # 7. Save compilation record
    compilation = ContextCompilation(
        project_id=project_id,
        user_id=user_id,
        entries_compiled=len(pending),
        context_before=context_before,
        context_after=context_after,
    )
    db.add(compilation)
    await db.commit()
    await db.refresh(compilation)

    logger.info(
        "Compiled %d entries for project %s (compilation %d)",
        len(pending),
        project_id,
        compilation.id,
    )

    return compilation


def _simple_compile(context: str, grouped: dict[str, list[str]]) -> str:
    """Simple append-based compilation without LLM."""
    lines = [context.rstrip()] if context.strip() else ["# Project Context"]

    for entry_type, contents in sorted(grouped.items()):
        lines.append(f"\n## {entry_type.title()}")
        for c in contents:
            lines.append(f"- {c}")

    return "\n".join(lines) + "\n"
