"""Extract knowledge entries from session summaries."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from sessionfs.server.db.models import KnowledgeEntry

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from sessionfs.server.services.summarizer import SessionSummary

logger = logging.getLogger("sessionfs.knowledge")

_EXTRACTION_PROMPT = """\
You are analyzing an AI coding session to extract knowledge for a project wiki.

SESSION MESSAGES (last 30 assistant messages):
{messages_text}

Extract the most important knowledge from this session. Focus on:
1. Architecture/design decisions made
2. Code patterns discovered or established
3. Bugs found and how they were fixed
4. Conventions established or followed
5. Dependencies added or configured
6. Surprising discoveries

Return a JSON array of entries. Each entry has:
- "content": 1-2 sentences describing what was learned (be specific, not vague)
- "entry_type": one of "decision", "pattern", "bug", "convention", "dependency", "discovery"
- "confidence": 0.5-1.0

Only include genuinely useful knowledge — skip routine file edits and obvious actions.
Max 8 entries. Return ONLY the JSON array, nothing else.
If nothing significant was learned, return [].
"""


async def extract_knowledge_entries(
    session_id: str,
    summary: SessionSummary,
    project_id: str,
    user_id: str,
    db: AsyncSession,
) -> list[KnowledgeEntry]:
    """Extract knowledge entries from a session summary.

    Uses content-level dedup so re-syncs of long-running sessions can
    add new decisions/files/bugs without duplicating existing entries.
    """
    from sqlalchemy import select as sa_select

    # Fetch existing entry contents for this session to dedup at content level
    existing_result = await db.execute(
        sa_select(KnowledgeEntry.content).where(
            KnowledgeEntry.session_id == session_id,
            KnowledgeEntry.project_id == project_id,
        )
    )
    existing_contents: set[str] = {row[0] for row in existing_result.all()}

    entries: list[KnowledgeEntry] = []

    # Files modified -> pattern entries
    for file_path in summary.files_modified:
        entry = KnowledgeEntry(
            project_id=project_id,
            session_id=session_id,
            user_id=user_id,
            entry_type="pattern",
            content=f"File created/modified: {file_path}",
            confidence=0.3,
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

    # Persist only entries whose content is new (dedup against DB and intra-batch)
    new_entries: list[KnowledgeEntry] = []
    for e in entries:
        if e.content not in existing_contents:
            existing_contents.add(e.content)
            new_entries.append(e)
    if new_entries:
        for entry in new_entries:
            db.add(entry)
        await db.commit()
        logger.info(
            "Extracted %d new knowledge entries from session %s for project %s (skipped %d existing)",
            len(new_entries),
            session_id,
            project_id,
            len(entries) - len(new_entries),
        )

    return new_entries


async def extract_knowledge_with_llm(
    session_id: str,
    messages: list[dict],
    project_id: str,
    user_id: str,
    api_key: str,
    model: str = "claude-sonnet-4",
    provider: str | None = None,
    base_url: str | None = None,
    db: AsyncSession | None = None,
) -> list[KnowledgeEntry]:
    """Extract high-quality knowledge entries from session messages using LLM.

    Uses content-level dedup so re-syncs of long-running sessions can
    discover new patterns without duplicating existing LLM entries.
    This catches patterns, decisions, and discoveries that deterministic
    extraction misses. Runs automatically on sync when auto_narrative is
    enabled and the user has LLM configured.
    """
    # Fetch existing LLM-extracted content for content-level dedup
    existing_contents: set[str] = set()
    if db:
        from sqlalchemy import select as sa_select
        existing_result = await db.execute(
            sa_select(KnowledgeEntry.content).where(
                KnowledgeEntry.session_id == session_id,
                KnowledgeEntry.project_id == project_id,
                KnowledgeEntry.source_context.like("LLM-extracted%"),
            )
        )
        existing_contents = {row[0] for row in existing_result.all()}

    # Extract text from last 30 assistant messages
    assistant_texts: list[str] = []
    for msg in reversed(messages):
        if msg.get("role") == "assistant":
            content = msg.get("content", [])
            if isinstance(content, str):
                assistant_texts.append(content[:500])
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        assistant_texts.append(block.get("text", "")[:500])
            if len(assistant_texts) >= 30:
                break

    if not assistant_texts:
        return []

    messages_text = "\n---\n".join(reversed(assistant_texts))
    prompt = _EXTRACTION_PROMPT.format(messages_text=messages_text[:15000])

    try:
        from sessionfs.judge.providers import call_llm

        response = await call_llm(
            model=model,
            system="You extract knowledge from AI coding sessions. Return only valid JSON.",
            prompt=prompt,
            api_key=api_key,
            provider=provider,
            base_url=base_url,
        )

        # Parse JSON response
        text = response.strip()
        if text.startswith("```"):
            lines = text.split("\n")
            lines = [ln for ln in lines if not ln.strip().startswith("```")]
            text = "\n".join(lines)

        raw_entries = json.loads(text)
        if not isinstance(raw_entries, list):
            return []

        entries: list[KnowledgeEntry] = []
        valid_types = {"decision", "pattern", "bug", "convention", "dependency", "discovery"}

        for item in raw_entries[:8]:
            content = item.get("content", "").strip()
            entry_type = item.get("entry_type", "discovery")
            confidence = item.get("confidence", 0.7)

            if not content or entry_type not in valid_types:
                continue

            entries.append(KnowledgeEntry(
                project_id=project_id,
                session_id=session_id,
                user_id=user_id,
                entry_type=entry_type,
                content=content,
                confidence=min(max(confidence, 0.0), 1.0),
                source_context=f"LLM-extracted from session {session_id}",
            ))

        # Content-level dedup: against DB and intra-batch
        new_entries: list[KnowledgeEntry] = []
        for e in entries:
            if e.content not in existing_contents:
                existing_contents.add(e.content)
                new_entries.append(e)
        if new_entries and db:
            for entry in new_entries:
                db.add(entry)
            await db.commit()
            logger.info(
                "LLM extracted %d new knowledge entries from session %s (skipped %d existing)",
                len(new_entries),
                session_id,
                len(entries) - len(new_entries),
            )

        return new_entries

    except Exception:
        logger.warning("LLM knowledge extraction failed for %s", session_id, exc_info=True)
        return []
