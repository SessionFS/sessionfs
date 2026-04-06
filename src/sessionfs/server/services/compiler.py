"""Compile pending knowledge entries into project context via LLM."""

from __future__ import annotations

import logging
import re
import secrets
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import func, select

from sessionfs.server.db.models import (
    ContextCompilation,
    KnowledgeEntry,
    KnowledgeLink,
    KnowledgePage,
    Project,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("sessionfs.compiler")

SECTION_MAP = {
    "decision": "## Key Decisions",
    "pattern": "## Patterns & Conventions",
    "discovery": "## Discoveries",
    "convention": "## Coding Conventions",
    "bug": "## Known Issues & Workarounds",
    "dependency": "## Dependencies & Integrations",
}

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
        context_after = _simple_compile(context_before, grouped, entries=pending)
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
            context_after = _simple_compile(context_before, grouped, entries=pending)

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

    # 8. Create/update section wiki pages per entry type
    from sessionfs.server.db.models import KnowledgePage

    slug_map = {
        "decision": "key-decisions",
        "pattern": "patterns",
        "convention": "coding-conventions",
        "bug": "known-issues",
        "dependency": "dependencies",
        "discovery": "discoveries",
    }

    for entry_type, contents in grouped.items():
        slug = slug_map.get(entry_type, entry_type)
        section_title = SECTION_MAP.get(entry_type, f"## {entry_type.title()}").lstrip("# ")

        # Build page content from all entries of this type (not just pending)
        all_of_type = await db.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.project_id == project_id,
                KnowledgeEntry.entry_type == entry_type,
                KnowledgeEntry.dismissed == False,  # noqa: E712
            ).order_by(KnowledgeEntry.created_at.desc())
        )
        all_entries = all_of_type.scalars().all()

        page_content = f"# {section_title}\n\n"
        for e in all_entries:
            conf = " *(unverified)*" if e.confidence < 0.5 else ""
            page_content += f"- {e.content}{conf}\n"

        # Upsert the section page
        existing_page = await db.execute(
            select(KnowledgePage).where(
                KnowledgePage.project_id == project_id,
                KnowledgePage.slug == slug,
            )
        )
        page = existing_page.scalar_one_or_none()

        if page:
            page.content = page_content
            page.word_count = len(page_content.split())
            page.entry_count = len(all_entries)
            page.updated_at = now
        else:
            db.add(KnowledgePage(
                id=f"kp_{secrets.token_hex(8)}",
                project_id=project_id,
                slug=slug,
                title=section_title,
                page_type="section",
                content=page_content,
                word_count=len(page_content.split()),
                entry_count=len(all_entries),
                auto_generated=True,
            ))

    await db.commit()

    return compilation


def _simple_compile(
    context: str,
    grouped: dict[str, list[str]],
    entries: list | None = None,
) -> str:
    """Simple append-based compilation without LLM.

    Uses SECTION_MAP for proper section names, merges similar entries,
    marks low-confidence entries as (unverified), and adds a Recent Changes
    section at the bottom.
    """
    # Split context into: verified part, unverified bullets, and strip
    # ## Recent Changes (rebuilt each compile from current batch).
    verified_lines: list[str] = []
    old_unverified: list[str] = []  # bare content (without marker) from old ## Unverified
    current_section = "verified"
    for line in (context or "").splitlines():
        stripped = line.strip()
        if stripped == "## Recent Changes":
            current_section = "recent"
            continue
        if stripped == "## Unverified":
            current_section = "unverified"
            continue
        if current_section != "verified" and stripped.startswith("## "):
            current_section = "verified"
        if current_section == "verified":
            verified_lines.append(line)
        elif current_section == "unverified" and stripped.startswith("- "):
            fact = stripped[2:].strip()
            if fact.lower().startswith("(unverified) "):
                fact = fact[13:].strip()
            old_unverified.append(fact)
    verified_context = "\n".join(verified_lines).rstrip()

    lines = [verified_context] if verified_context else ["# Project Context"]

    # Seed dedup set from verified context only (NOT unverified), so that
    # a newly verified fact can replace an older unverified marker.
    verified_facts: set[str] = set()
    for line in verified_context.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            verified_facts.add(stripped[2:].strip().lower())

    # Build set of facts that are ONLY low-confidence in this batch.
    # If the same fact appears at both low and high confidence, the
    # high-confidence version wins and goes into the main section.
    low_conf_content: set[str] = set()
    high_conf_content: set[str] = set()
    if entries:
        for e in entries:
            normalized = e.content.strip().lower()
            if e.confidence >= 0.5:
                high_conf_content.add(normalized)
            else:
                low_conf_content.add(normalized)
        low_conf_content -= high_conf_content

    for entry_type, contents in sorted(grouped.items()):
        section_heading = SECTION_MAP.get(entry_type, f"## {entry_type.title()}")
        # Check if section already exists in the context
        if section_heading not in "\n".join(lines):
            lines.append(f"\n{section_heading}")

        # Deduplicate against verified context and current batch;
        # skip low-confidence entries (they go to ## Unverified only)
        for c in contents:
            normalized = c.strip().lower()
            if normalized in low_conf_content:
                continue
            if normalized not in verified_facts:
                verified_facts.add(normalized)
                lines.append(f"- {c}")

    # Rebuild ## Unverified: keep old unverified facts that haven't been
    # promoted to verified, and add new low-confidence entries.
    all_unverified: list[str] = []
    unverified_seen: set[str] = set()
    # Carry forward old unverified facts not now verified
    for fact in old_unverified:
        normalized = fact.strip().lower()
        if normalized not in verified_facts and normalized not in unverified_seen:
            unverified_seen.add(normalized)
            all_unverified.append(fact)
    # Add new low-confidence entries
    if entries:
        for e in [e for e in entries if e.confidence < 0.5]:
            normalized = e.content.strip().lower()
            if normalized not in verified_facts and normalized not in unverified_seen:
                unverified_seen.add(normalized)
                all_unverified.append(e.content)
    if all_unverified:
        lines.append("\n## Unverified")
        for fact in all_unverified:
            lines.append(f"- (unverified) {fact}")

    # Add Recent Changes section with dated changelog
    if entries:
        dated_entries: dict[str, list[str]] = defaultdict(list)
        for e in entries:
            date_str = e.created_at.strftime("%Y-%m-%d") if e.created_at else "unknown"
            dated_entries[date_str].append(f"[{e.entry_type}] {e.content}")

        if dated_entries:
            lines.append("\n## Recent Changes")
            for date_key in sorted(dated_entries.keys(), reverse=True):
                lines.append(f"\n### {date_key}")
                for item in dated_entries[date_key]:
                    lines.append(f"- {item}")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Concept auto-generation
# ---------------------------------------------------------------------------

_CONCEPT_CLUSTER_SYSTEM = """\
You are a knowledge analyst. Given a list of knowledge entries for a software \
project, identify clusters of 5 or more related entries that share a common \
topic. Return ONLY a JSON array of objects with keys: topic (string), slug \
(string, lowercase-hyphenated), entry_count (int), summary (one sentence). \
Return an empty array [] if no clusters are found."""

_CONCEPT_ARTICLE_SYSTEM = """\
You are a technical writer. Write a concise project knowledge article \
(200-400 words, markdown format) about the given topic. Base the article \
strictly on the provided knowledge entries — do not add information that \
isn't supported by the entries. Use clear headings and bullet points where \
appropriate."""


def _extract_phrases(content: str) -> list[str]:
    """Extract 2+ word lowercase phrases from content for deterministic clustering."""
    words = re.findall(r"[a-z][a-z0-9_]+", content.lower())
    phrases = []
    for i in range(len(words) - 1):
        phrase = f"{words[i]} {words[i + 1]}"
        if len(phrase) >= 5:
            phrases.append(phrase)
    return phrases


async def check_concept_candidates(
    project_id: str,
    user_id: str,
    db: AsyncSession,
    api_key: str | None = None,
    model: str = "claude-sonnet-4",
    provider: str | None = None,
    base_url: str | None = None,
) -> list[dict]:
    """Find clusters of related entries that could become concept articles.

    Only runs if the project has >= 15 total entries. With an LLM API key,
    asks the model to find clusters. Without one, uses deterministic phrase
    matching (groups entries sharing a 2+ word phrase, requires 5+ entries).
    """
    # Check total entry count
    count_result = await db.execute(
        select(func.count(KnowledgeEntry.id)).where(
            KnowledgeEntry.project_id == project_id,
        )
    )
    total = count_result.scalar() or 0
    if total < 15:
        return []

    # Get non-dismissed entries
    result = await db.execute(
        select(KnowledgeEntry).where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.dismissed == False,  # noqa: E712
        )
    )
    entries = list(result.scalars().all())
    if len(entries) < 15:
        return []

    if api_key:
        return await _llm_concept_candidates(entries, api_key, model, provider, base_url)

    # Deterministic fallback: group by shared 2+ word phrases
    return _deterministic_concept_candidates(entries)


async def _llm_concept_candidates(
    entries: list[KnowledgeEntry],
    api_key: str,
    model: str,
    provider: str | None,
    base_url: str | None,
) -> list[dict]:
    """Use LLM to identify concept clusters."""
    import json

    from sessionfs.judge.providers import call_llm

    formatted = "\n".join(
        f"- [{e.entry_type}] {e.content}" for e in entries
    )
    prompt = f"Here are {len(entries)} knowledge entries:\n\n{formatted}"

    try:
        raw = await call_llm(
            model=model,
            system=_CONCEPT_CLUSTER_SYSTEM,
            prompt=prompt,
            api_key=api_key,
            provider=provider,
            base_url=base_url,
        )
        # Extract JSON from response (might be wrapped in markdown code block)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        candidates = json.loads(raw)
        if not isinstance(candidates, list):
            return []
        # Validate structure
        valid = []
        for c in candidates:
            if all(k in c for k in ("topic", "slug", "entry_count", "summary")):
                valid.append({
                    "topic": str(c["topic"]),
                    "slug": str(c["slug"]),
                    "entry_count": int(c["entry_count"]),
                    "summary": str(c["summary"]),
                })
        return valid
    except Exception:
        logger.warning("LLM concept clustering failed, falling back to deterministic", exc_info=True)
        return _deterministic_concept_candidates(entries)


def _deterministic_concept_candidates(entries: list[KnowledgeEntry]) -> list[dict]:
    """Group entries by shared 2+ word phrases, requiring 5+ entries per group."""
    phrase_to_entries: dict[str, list[KnowledgeEntry]] = defaultdict(list)

    for entry in entries:
        phrases = _extract_phrases(entry.content)
        seen: set[str] = set()
        for phrase in phrases:
            if phrase not in seen:
                seen.add(phrase)
                phrase_to_entries[phrase].append(entry)

    candidates = []
    used_entry_ids: set[int] = set()

    # Sort by cluster size descending
    for phrase, cluster_entries in sorted(
        phrase_to_entries.items(), key=lambda x: len(x[1]), reverse=True
    ):
        if len(cluster_entries) < 5:
            continue
        # Skip if most entries already used
        new_entries = [e for e in cluster_entries if e.id not in used_entry_ids]
        if len(new_entries) < 5:
            continue

        slug = re.sub(r"[^a-z0-9]+", "-", phrase.lower()).strip("-")
        candidates.append({
            "topic": phrase.title(),
            "slug": slug,
            "entry_count": len(new_entries),
            "summary": f"Cluster of {len(new_entries)} entries related to '{phrase}'.",
        })
        for e in new_entries:
            used_entry_ids.add(e.id)

    return candidates


async def generate_concept_article(
    topic: str,
    summary: str,
    entries: list[KnowledgeEntry],
    user_id: str,
    api_key: str | None = None,
    model: str = "claude-sonnet-4",
    provider: str | None = None,
    base_url: str | None = None,
) -> str:
    """Generate a concept article from related entries.

    Uses LLM if api_key is provided, otherwise falls back to a bulleted list.
    """
    formatted = "\n".join(
        f"- [{e.entry_type}] {e.content}" for e in entries
    )

    if api_key:
        from sessionfs.judge.providers import call_llm

        prompt = (
            f"## Topic: {topic}\n\n"
            f"## Summary: {summary}\n\n"
            f"## Related Knowledge Entries ({len(entries)}):\n{formatted}\n\n"
            f"Write the article now."
        )
        try:
            article = await call_llm(
                model=model,
                system=_CONCEPT_ARTICLE_SYSTEM,
                prompt=prompt,
                api_key=api_key,
                provider=provider,
                base_url=base_url,
            )
            return article.strip()
        except Exception:
            logger.warning("LLM article generation failed, using fallback", exc_info=True)

    # Fallback: simple bulleted list
    lines = [f"# {topic}", "", summary, ""]
    for entry in entries:
        lines.append(f"- **[{entry.entry_type}]** {entry.content}")
    return "\n".join(lines)


async def auto_generate_concepts(
    project_id: str,
    user_id: str,
    db: AsyncSession,
    api_key: str | None = None,
    model: str = "claude-sonnet-4",
    provider: str | None = None,
    base_url: str | None = None,
) -> list[dict]:
    """Check for concept candidates and create pages for new ones.

    Called after compilation. Returns list of created concept summaries.
    """
    candidates = await check_concept_candidates(
        project_id, user_id, db, api_key, model, provider, base_url,
    )
    if not candidates:
        return []

    created = []
    now = datetime.now(timezone.utc)

    for candidate in candidates:
        concept_slug = f"concept/{candidate['slug']}"

        # Check if page already exists
        existing = await db.execute(
            select(KnowledgePage).where(
                KnowledgePage.project_id == project_id,
                KnowledgePage.slug == concept_slug,
            )
        )
        if existing.scalar_one_or_none():
            continue

        # Get entries for this concept (search by topic keywords)
        topic_words = candidate["topic"].lower().split()
        result = await db.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.project_id == project_id,
                KnowledgeEntry.dismissed == False,  # noqa: E712
            )
        )
        all_entries = list(result.scalars().all())
        # Filter entries containing topic words
        matched_entries = [
            e for e in all_entries
            if any(w in e.content.lower() for w in topic_words if len(w) > 3)
        ]
        if not matched_entries:
            matched_entries = all_entries[:10]

        # Generate article
        article = await generate_concept_article(
            topic=candidate["topic"],
            summary=candidate["summary"],
            entries=matched_entries,
            user_id=user_id,
            api_key=api_key,
            model=model,
            provider=provider,
            base_url=base_url,
        )

        # Create knowledge page
        page_id = f"page_{uuid.uuid4().hex[:16]}"
        page = KnowledgePage(
            id=page_id,
            project_id=project_id,
            slug=concept_slug,
            title=candidate["topic"],
            page_type="concept",
            content=article,
            word_count=len(article.split()),
            entry_count=len(matched_entries),
            auto_generated=True,
            created_at=now,
            updated_at=now,
        )
        db.add(page)

        # Create knowledge links from entries to concept page
        for entry in matched_entries:
            link = KnowledgeLink(
                project_id=project_id,
                source_type="entry",
                source_id=str(entry.id),
                target_type="page",
                target_id=page_id,
                link_type="contributes",
                confidence=1.0,
            )
            db.add(link)

        created.append({
            "slug": concept_slug,
            "topic": candidate["topic"],
            "word_count": len(article.split()),
            "entry_count": len(matched_entries),
        })

    if created:
        await db.commit()
        logger.info(
            "Auto-generated %d concept pages for project %s",
            len(created), project_id,
        )

    return created
