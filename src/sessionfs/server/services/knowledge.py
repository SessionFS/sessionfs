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


# -- Semantic dedup helpers ---------------------------------------------------


def word_overlap(a: str, b: str) -> float:
    """Compute Jaccard-min word overlap between two strings.

    Returns 1.0 when either string is a subset of the other, 0.0 when they
    share no words. Uses `min(|A|, |B|)` as the denominator so a short
    query fully contained in a longer doc still scores as a match.
    """
    words_a = set(a.lower().split())
    words_b = set(b.lower().split())
    if not words_a or not words_b:
        return 0.0
    return len(words_a & words_b) / min(len(words_a), len(words_b))


def is_near_duplicate(content: str, existing_contents: list[str], threshold: float = 0.85) -> bool:
    """Return True if `content` is semantically near-duplicate of any of
    `existing_contents` (word overlap > threshold).

    `existing_contents` should be a pre-fetched list of recent non-dismissed
    entry contents for the same project — per-call DB queries would be too
    slow for batch extraction paths, so we ask callers to fetch once.
    """
    for existing in existing_contents:
        if word_overlap(content, existing) > threshold:
            return True
    return False


async def _fetch_recent_project_contents(
    project_id: str,
    db: AsyncSession,
    limit: int = 100,
) -> list[str]:
    """Fetch content strings of the N most recent non-dismissed entries for
    a project. Used by extraction paths to run semantic dedup in-memory
    instead of doing one query per candidate.
    """
    from sqlalchemy import select as sa_select

    result = await db.execute(
        sa_select(KnowledgeEntry.content)
        .where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.dismissed == False,  # noqa: E712
        )
        .order_by(KnowledgeEntry.created_at.desc())
        .limit(limit)
    )
    return [row[0] for row in result.all()]

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

    Uses semantic dedup (word overlap > 0.85) against recent project entries
    so re-syncs of long-running sessions add genuinely new knowledge and
    near-duplicates — e.g., "File modified: src/foo.py" from yesterday vs
    "File created/modified: src/foo.py" from today — don't accumulate.
    """
    # Exact-content set for the same session (fast path — prevents the same
    # extraction run from adding the same line twice in a batch) PLUS a
    # broader project-wide set for semantic dedup across sessions.
    from sqlalchemy import select as sa_select

    existing_result = await db.execute(
        sa_select(KnowledgeEntry.content).where(
            KnowledgeEntry.session_id == session_id,
            KnowledgeEntry.project_id == project_id,
        )
    )
    existing_contents: set[str] = {row[0] for row in existing_result.all()}

    # Broader project window for semantic dedup. 100 most recent entries is
    # enough to catch near-duplicates at scale without slowing the extraction
    # pass down to per-entry SQL.
    project_window = await _fetch_recent_project_contents(project_id, db, limit=100)

    entries: list[KnowledgeEntry] = []

    # Files modified -> pattern entries (evidence — machine-extracted)
    for file_path in summary.files_modified:
        entry = KnowledgeEntry(
            project_id=project_id,
            session_id=session_id,
            user_id=user_id,
            entry_type="pattern",
            content=f"File created/modified: {file_path}",
            confidence=0.3,
            source_context=f"Session {session_id} modified {file_path}",
            claim_class="evidence",
        )
        entries.append(entry)

    # Tests failing -> bug entries (evidence — machine-extracted)
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
            claim_class="evidence",
        )
        entries.append(entry)

    # Packages installed -> dependency entries (evidence — machine-extracted)
    for package in summary.packages_installed:
        entry = KnowledgeEntry(
            project_id=project_id,
            session_id=session_id,
            user_id=user_id,
            entry_type="dependency",
            content=f"Package installed: {package}",
            confidence=0.9,
            source_context=f"Session {session_id} installed {package}",
            claim_class="evidence",
        )
        entries.append(entry)

    # key_decisions from narrative -> decision entries (evidence — machine-extracted)
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
                claim_class="evidence",
            )
            entries.append(entry)

    # open_issues from narrative -> bug entries (evidence — machine-extracted)
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
                claim_class="evidence",
            )
            entries.append(entry)

    # Persist only genuinely new entries: (a) exact-match dedup against
    # this session's prior extractions, (b) semantic dedup against the
    # project window, (c) intra-batch dedup so the same run can't add two
    # near-duplicates. The combined filter closes the "near-duplicates
    # accumulate across sessions" gap from the pre-release review.
    new_entries: list[KnowledgeEntry] = []
    batch_contents: list[str] = []
    for e in entries:
        if e.content in existing_contents:
            continue
        if is_near_duplicate(e.content, project_window):
            continue
        if is_near_duplicate(e.content, batch_contents):
            continue
        existing_contents.add(e.content)
        batch_contents.append(e.content)
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
    # Exact-match dedup against prior LLM extractions for this same session
    # PLUS semantic dedup against a broader project window. The two-layer
    # check closes the "near-duplicates accumulate across sessions" gap.
    existing_contents: set[str] = set()
    project_window: list[str] = []
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
        project_window = await _fetch_recent_project_contents(project_id, db, limit=100)

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
                claim_class="evidence",
            ))

        # Semantic dedup: exact-match for this session, word-overlap
        # against the project window, AND intra-batch dedup so the LLM
        # can't sneak near-duplicates past us inside one response.
        new_entries: list[KnowledgeEntry] = []
        batch_contents: list[str] = []
        for e in entries:
            if e.content in existing_contents:
                continue
            if is_near_duplicate(e.content, project_window):
                continue
            if is_near_duplicate(e.content, batch_contents):
                continue
            existing_contents.add(e.content)
            batch_contents.append(e.content)
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
