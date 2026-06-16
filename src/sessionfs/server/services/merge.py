"""Project merge engine (§5.11–§5.12).

Atomic, dry-run-first, audit-logged merge of one project into another.
Follows the per-table merge matrix order exactly. The merge transaction
is a single `async with db.begin()` block — any failure rolls back
completely and both projects are untouched.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionfs.server.db.models import (
    AgentPersona,
    AgentRun,
    ContextCompilation,
    HandoffAttachment,
    KnowledgeEntry,
    KnowledgeLink,
    KnowledgePage,
    Project,
    ProjectMergeAudit,
    ProjectRepo,
    ProjectRules,
    ProjectTransfer,
    RetrievalAuditContext,
    RetrievalAuditEvent,
    Session,
    Ticket,
    WikiPageRevision,
)

logger = logging.getLogger("sessionfs.server.services.merge")

# ── constants ──────────────────────────────────────────────────────
_MAX_PERSONA_NAME = 50
_SOURCE_SUFFIX_LEN = 8  # first 8 chars of source project id
_PERSONA_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,50}$")

# Tables that get straight reassign (project_id column → target_id).
_STRAIGHT_REASSIGN_MODELS = [
    Ticket,
    AgentRun,
    Session,
    HandoffAttachment,
    ProjectTransfer,
    ContextCompilation,
    RetrievalAuditContext,
    RetrievalAuditEvent,
]


# ── helpers ────────────────────────────────────────────────────────

def _legal_rename(base_name: str, suffix: str, seen: set[str]) -> str:
    """Produce a legal, unique, ≤50-char rename slug.

    Truncates base if needed so f"{truncated}-{suffix}" ≤ 50 chars.
    If the result collides with an existing name in *seen*, appends an
    incrementing counter.  Callers MUST add the returned name to *seen*
    before the next call so in-flight rename-rename collisions are
    prevented.
    """
    max_base = _MAX_PERSONA_NAME - len(suffix) - 1  # reserve '-' + suffix
    truncated = base_name[:max_base]
    candidate = f"{truncated}-{suffix}"
    attempt = candidate
    i = 1
    while attempt in seen:
        attempt = f"{candidate[:_MAX_PERSONA_NAME - 2]}-{i}"
        i += 1
    return attempt


def _normalize_content(text: str | None) -> str:
    """Whitespace-normalize content for exact-match dedup."""
    if text is None:
        return ""
    return " ".join(text.split())


# ── precondition helpers ───────────────────────────────────────────

async def _validate_preconditions(
    db: AsyncSession,
    source_id: str,
    target_id: str,
) -> tuple[Project, Project]:
    """Raise HTTPException on any precondition failure.

    Returns (source, target) projects on success.  These rejections are
    NOT merge-audited (§5.11 Phase 1).
    """
    from fastapi import HTTPException

    source = await db.get(Project, source_id)
    target = await db.get(Project, target_id)
    if not source:
        raise HTTPException(404, "Source project not found")
    if not target:
        raise HTTPException(404, "Target project not found")

    # Cross-org / personal-mix check.
    if source.org_id != target.org_id:
        raise HTTPException(
            400,
            "Cross-org merges are not supported. "
            "Use project transfer to move one project into the other's org first.",
        )

    # Neither already merged.
    if source.merged_into_project_id is not None:
        raise HTTPException(400, "Source project was already merged")
    if target.merged_into_project_id is not None:
        raise HTTPException(400, "Target project was already merged")

    # Sentinel L3: block if either project has a pending transfer.
    for side, proj in [("source", source), ("target", target)]:
        pending = (
            await db.execute(
                select(ProjectTransfer).where(
                    ProjectTransfer.project_id == proj.id,
                    ProjectTransfer.state == "pending",
                )
            )
        ).scalar_one_or_none()
        if pending:
            raise HTTPException(
                400,
                f"{side.capitalize()} project has a pending transfer. "
                "Resolve it first.",
            )

    return source, target


# ── collision detection (dry-run + pre-compute) ────────────────────

async def _detect_persona_collisions(
    db: AsyncSession, source_id: str, target_id: str,
) -> list[dict]:
    """Return list of {source_name, policy, resolved_name} for colliding personas."""
    source_names = (
        await db.execute(
            select(AgentPersona.name)
            .where(AgentPersona.project_id == source_id)
        )
    ).scalars().all()
    target_names = set(
        (
            await db.execute(
                select(AgentPersona.name)
                .where(AgentPersona.project_id == target_id)
            )
        ).scalars().all()
    )
    collisions = []
    for name in source_names:
        if name in target_names:
            collisions.append({"source_name": name})
    return collisions


async def _detect_slug_collisions(
    db: AsyncSession, source_id: str, target_id: str,
) -> list[str]:
    """Return list of source slugs that collide with target slugs."""
    source_slugs = (
        await db.execute(
            select(KnowledgePage.slug)
            .where(KnowledgePage.project_id == source_id)
        )
    ).scalars().all()
    target_slugs = set(
        (
            await db.execute(
                select(KnowledgePage.slug)
                .where(KnowledgePage.project_id == target_id)
            )
        ).scalars().all()
    )
    return [s for s in source_slugs if s in target_slugs]


async def _detect_ke_duplicates(
    db: AsyncSession, source_id: str, target_id: str,
) -> list[dict]:
    """Return list of source KE ids + types that are exact duplicates in target."""
    target_entries = (
        await db.execute(
            select(KnowledgeEntry.entry_type, KnowledgeEntry.content, KnowledgeEntry.id)
            .where(KnowledgeEntry.project_id == target_id)
        )
    ).all()
    target_keys = {
        (e.entry_type, _normalize_content(e.content)): e.id
        for e in target_entries
    }
    source_entries = (
        await db.execute(
            select(KnowledgeEntry)
            .where(KnowledgeEntry.project_id == source_id)
        )
    ).scalars().all()
    dupes = []
    for entry in source_entries:
        key = (entry.entry_type, _normalize_content(entry.content))
        if key in target_keys:
            dupes.append({
                "source_id": entry.id,
                "entry_type": entry.entry_type,
                "target_equivalent_id": target_keys[key],
            })
    return dupes


async def _has_rules(db: AsyncSession, project_id: str) -> bool:
    row = (
        await db.execute(
            select(ProjectRules.id).where(ProjectRules.project_id == project_id)
        )
    ).scalar_one_or_none()
    return row is not None


async def _compute_merge_stats(
    db: AsyncSession,
    source_id: str,
    target_id: str,
) -> dict:
    """Count rows that will be moved / skipped."""
    stats: dict = {}

    for label, model in [
        ("repos", ProjectRepo),
        ("personas", AgentPersona),
        ("tickets", Ticket),
        ("agent_runs", AgentRun),
        ("sessions", Session),
        ("handoff_attachments", HandoffAttachment),
        ("project_transfers", ProjectTransfer),
        ("context_compilations", ContextCompilation),
        ("knowledge_entries", KnowledgeEntry),
        ("knowledge_links", KnowledgeLink),
        ("knowledge_pages", KnowledgePage),
        ("wiki_page_revisions", WikiPageRevision),
        ("retrieval_audit_contexts", RetrievalAuditContext),
        ("retrieval_audit_events", RetrievalAuditEvent),
    ]:
        count = (
            await db.execute(
                select(func.count()).select_from(model).where(
                    model.project_id == source_id
                )
            )
        ).scalar() or 0
        stats[label] = count

    # Persona collisions
    collisions = await _detect_persona_collisions(db, source_id, target_id)
    stats["persona_collisions"] = len(collisions)

    # Slug collisions
    slug_collisions = await _detect_slug_collisions(db, source_id, target_id)
    stats["slug_collisions"] = len(slug_collisions)

    # KE duplicates
    ke_dupes = await _detect_ke_duplicates(db, source_id, target_id)
    stats["ke_duplicates"] = len(ke_dupes)

    # Rules
    stats["source_has_rules"] = await _has_rules(db, source_id)
    stats["target_has_rules"] = await _has_rules(db, target_id)

    return stats


# ── step implementations (each corresponds to one merge-matrix row) ─

async def _step_repos(
    db: AsyncSession, source_id: str, target_id: str,
) -> None:
    """Step 1: Lock + demote source primary + reassign all source repos."""
    # Lock both projects' repos.
    await db.execute(
        select(ProjectRepo)
        .where(ProjectRepo.project_id.in_([source_id, target_id]))
        .with_for_update()
    )
    # Demote source primary.
    await db.execute(
        update(ProjectRepo)
        .where(
            ProjectRepo.project_id == source_id,
            ProjectRepo.is_primary == True,  # noqa: E712
        )
        .values(is_primary=False)
    )
    # Reassign all source repos.
    await db.execute(
        update(ProjectRepo)
        .where(ProjectRepo.project_id == source_id)
        .values(project_id=target_id)
    )
    # If target has no primary after reassign, promote oldest.
    target_primary = (
        await db.execute(
            select(ProjectRepo).where(
                ProjectRepo.project_id == target_id,
                ProjectRepo.is_primary == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if not target_primary:
        oldest = (
            await db.execute(
                select(ProjectRepo)
                .where(ProjectRepo.project_id == target_id)
                .order_by(ProjectRepo.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if oldest:
            oldest.is_primary = True


async def _step_personas(
    db: AsyncSession,
    source_id: str,
    target_id: str,
    persona_policy: str,
) -> list[dict]:
    """Step 2: Reassign ALL source personas, handling collisions per policy.

    Returns list of {old_name, new_name, display_note} for audit.
    Every source persona ends up on target with a unique name.
    """
    source_personas = (
        await db.execute(
            select(AgentPersona).where(AgentPersona.project_id == source_id)
        )
    ).scalars().all()

    target_names: set[str] = set(
        (
            await db.execute(
                select(AgentPersona.name).where(
                    AgentPersona.project_id == target_id
                )
            )
        ).scalars().all()
    )

    source_prefix = source_id[:_SOURCE_SUFFIX_LEN]
    renames: list[dict] = []

    for persona in source_personas:
        if persona.name in target_names:
            if persona_policy == "rename":
                new_name = _legal_rename(persona.name, source_prefix, target_names)
                renames.append({
                    "old_name": persona.name,
                    "new_name": new_name,
                    "display_note": (
                        f"Renamed from '{persona.name}' "
                        f"(source project {source_prefix}) — "
                        f"collided with target's persona of same name."
                    ),
                })
                persona.name = new_name
                target_names.add(new_name)
            elif persona_policy == "skip":
                archived_name = _legal_rename(
                    persona.name, f"{source_prefix}-archived", target_names
                )
                renames.append({
                    "old_name": persona.name,
                    "new_name": archived_name,
                    "display_note": (
                        f"Archived '{persona.name}' "
                        f"(source project {source_prefix}) — "
                        f"skipped due to collision with target's "
                        f"persona of same name."
                    ),
                })
                persona.name = archived_name
                persona.is_active = False
                target_names.add(archived_name)
            elif persona_policy == "merge_content":
                # Merge content into target persona.
                target_p = (
                    await db.execute(
                        select(AgentPersona).where(
                            AgentPersona.project_id == target_id,
                            AgentPersona.name == persona.name,
                        )
                    )
                ).scalar_one()
                target_p.content = (
                    f"{target_p.content}\n\n"
                    f"--- merged from project {source_prefix} ---\n"
                    f"{persona.content}"
                )
                # Archive source persona under unique name.
                archived_name = _legal_rename(
                    persona.name, f"{source_prefix}-archived", target_names
                )
                renames.append({
                    "old_name": persona.name,
                    "new_name": archived_name,
                    "display_note": (
                        f"Archived '{persona.name}' "
                        f"(source project {source_prefix}) — "
                        f"content merged into target's persona "
                        f"of same name."
                    ),
                })
                persona.name = archived_name
                persona.is_active = False
                target_names.add(archived_name)
        # REASSIGN — every source persona moves to target.
        persona.project_id = target_id

    return renames


async def _step_rules(
    db: AsyncSession,
    source_id: str,
    target_id: str,
    target_has_rules: bool,
    source_prefix: str,
) -> str:
    """Step 3: ProjectRules — promote if target none, archive if both exist.

    Returns rules_action: 'promoted' | 'archived' | 'none'.
    """
    source_rules = (
        await db.execute(
            select(ProjectRules).where(ProjectRules.project_id == source_id)
        )
    ).scalar_one_or_none()

    if source_rules is None:
        return "none"

    if target_has_rules:
        # Archive source rules as wiki page.
        await _archive_rules_as_wiki_page(
            db, source_rules, target_id, source_prefix
        )
        await db.delete(source_rules)
        return "archived"
    else:
        source_rules.project_id = target_id
        return "promoted"


async def _archive_rules_as_wiki_page(
    db: AsyncSession,
    source_rules: ProjectRules,
    target_id: str,
    source_prefix: str,
) -> None:
    """Create a wiki page snapshot of the source project's rules."""
    import uuid as _uuid

    slug = f"_merged_rules_{source_prefix}"
    now = datetime.now(timezone.utc)
    page = KnowledgePage(
        id=str(_uuid.uuid4()),
        project_id=target_id,
        slug=slug,
        title=f"Merged Rules (from {source_prefix})",
        page_type="user",
        created_at=now,
        updated_at=now,
    )
    db.add(page)

    rev = WikiPageRevision(
        project_id=target_id,
        page_slug=slug,
        revision_number=1,
        content_snapshot=source_rules.static_rules or "",
        title=f"Merged Rules (from {source_prefix})",
        revised_at=now,
    )
    db.add(rev)


async def _step_knowledge_pages(
    db: AsyncSession,
    source_id: str,
    target_id: str,
    slug_collisions: list[str],
) -> list[dict]:
    """Steps 4-5: Reassign pages + revisions, handling slug collisions.

    For colliding slugs: rename to {slug}-{src8}, revisions follow
    atomically. Non-colliding: straight reassign.

    Returns list of {old_slug, new_slug} for audit.
    """
    source_prefix = source_id[:_SOURCE_SUFFIX_LEN]
    slug_renames: list[dict] = []
    target_slugs: set[str] = set(
        (
            await db.execute(
                select(KnowledgePage.slug).where(
                    KnowledgePage.project_id == target_id
                )
            )
        ).scalars().all()
    )

    # Handle colliding slugs first (rename before reassign).
    for old_slug in slug_collisions:
        new_slug = _legal_rename(old_slug, source_prefix, target_slugs)
        target_slugs.add(new_slug)

        # Rename KnowledgePage.
        await db.execute(
            update(KnowledgePage)
            .where(
                KnowledgePage.project_id == source_id,
                KnowledgePage.slug == old_slug,
            )
            .values(slug=new_slug, project_id=target_id)
        )
        # Re-point ALL revisions for this slug.
        await db.execute(
            update(WikiPageRevision)
            .where(
                WikiPageRevision.project_id == source_id,
                WikiPageRevision.page_slug == old_slug,
            )
            .values(project_id=target_id, page_slug=new_slug)
        )
        slug_renames.append({"old_slug": old_slug, "new_slug": new_slug})

    # Reassign remaining (non-colliding) pages.
    await db.execute(
        update(KnowledgePage)
        .where(KnowledgePage.project_id == source_id)
        .values(project_id=target_id)
    )
    # Reassign remaining revisions.
    await db.execute(
        update(WikiPageRevision)
        .where(WikiPageRevision.project_id == source_id)
        .values(project_id=target_id)
    )

    return slug_renames


async def _step_knowledge_entries(
    db: AsyncSession,
    source_id: str,
    target_id: str,
) -> tuple[dict[str, str], list[str]]:
    """Step 6: Dedup + reassign knowledge entries.

    Returns (entry_id_map, skipped_ids).
    entry_id_map: source_entry_id → target_equivalent_id.
    Reassigned (non-dup) entries map to themselves (identity).
    """
    target_entries = (
        await db.execute(
            select(KnowledgeEntry.entry_type, KnowledgeEntry.content, KnowledgeEntry.id)
            .where(KnowledgeEntry.project_id == target_id)
        )
    ).all()
    target_keys = {
        (e.entry_type, _normalize_content(e.content)): e.id
        for e in target_entries
    }

    source_entries = (
        await db.execute(
            select(KnowledgeEntry).where(KnowledgeEntry.project_id == source_id)
        )
    ).scalars().all()

    entry_id_map: dict[str, str] = {}
    skipped_ids: list[str] = []

    for entry in source_entries:
        key = (entry.entry_type, _normalize_content(entry.content))
        if key in target_keys:
            # Skip — redirect references to target equivalent.
            entry_id_map[entry.id] = target_keys[key]
            skipped_ids.append(entry.id)
        else:
            # Reassign unique entry.
            entry.project_id = target_id
            entry_id_map[entry.id] = entry.id  # identity mapping

    return entry_id_map, skipped_ids


async def _step_knowledge_links(
    db: AsyncSession,
    source_id: str,
    target_id: str,
    entry_id_map: dict[str, str],
) -> list[str]:
    """Step 7: Map + dedup + reassign knowledge links.

    COMPUTE BEFORE MUTATE: the remapped key is computed FIRST from the
    entry_id_map WITHOUT mutating the ORM row.  Duplicates are
    db.delete()'d.  Only non-duplicate rows are mutated.

    Returns skipped_link_ids.
    """
    source_links = (
        await db.execute(
            select(KnowledgeLink).where(KnowledgeLink.project_id == source_id)
        )
    ).scalars().all()

    target_links = (
        await db.execute(
            select(KnowledgeLink).where(KnowledgeLink.project_id == target_id)
        )
    ).scalars().all()
    running_keys = {
        (lk.source_type, lk.source_id, lk.target_type, lk.target_id)
        for lk in target_links
    }

    skipped_link_ids: list[str] = []
    for link in source_links:
        # Compute remapped key BEFORE mutating the row.
        new_source_id = entry_id_map.get(link.source_id, link.source_id)
        new_target_id = entry_id_map.get(link.target_id, link.target_id)
        key = (link.source_type, new_source_id, link.target_type, new_target_id)

        if key in running_keys:
            # Duplicate — delete source link, do NOT mutate.
            skipped_link_ids.append(link.id)
            await db.delete(link)
        else:
            running_keys.add(key)
            link.project_id = target_id
            link.source_id = new_source_id
            link.target_id = new_target_id

    return skipped_link_ids


async def _step_straight_reassign(
    db: AsyncSession,
    source_id: str,
    target_id: str,
) -> None:
    """Steps 8-15: Straight reassign all remaining tables."""
    for model in _STRAIGHT_REASSIGN_MODELS:
        await db.execute(
            update(model)
            .where(model.project_id == source_id)
            .values(project_id=target_id)
        )


# ── main entry point ───────────────────────────────────────────────

async def merge_projects(
    db: AsyncSession,
    source_id: str,
    target_id: str,
    user_id: str,
    dry_run: bool,
    persona_policy: str,
    session_factory: async_sessionmaker,
) -> dict:
    """Merge source project into target project.

    Args:
        db: Primary session for reads + mutations.
        source_id: Project to merge (becomes tombstone).
        target_id: Project to merge into.
        user_id: Initiating user.
        dry_run: If True, validate + compute plan only (ZERO writes).
        persona_policy: 'rename' | 'skip' | 'merge_content'.
        session_factory: Factory for fresh sessions (audit survival).

    Returns:
        dict with keys: dry_run, stats, persona_collisions, slug_collisions,
        ke_duplicates, [audit_id for execute].
    """
    # =================================================================
    # Phase 1: Validate preconditions (same transaction, NOT audited).
    # =================================================================
    source, target = await _validate_preconditions(db, source_id, target_id)

    # Pre-compute collisions + stats.
    persona_collisions = await _detect_persona_collisions(db, source_id, target_id)
    slug_collisions = await _detect_slug_collisions(db, source_id, target_id)
    ke_duplicates = await _detect_ke_duplicates(db, source_id, target_id)
    target_has_rules_flag = await _has_rules(db, target_id)
    stats = await _compute_merge_stats(db, source_id, target_id)

    if dry_run:
        # Dry-run: ZERO DB writes. Validate + return plan only.
        # No audit row, no locks beyond reads.
        return {
            "dry_run": True,
            "stats": stats,
            "persona_collisions": persona_collisions,
            "slug_collisions": slug_collisions,
            "ke_duplicates": ke_duplicates,
        }

    # =================================================================
    # Phase 2: Write ATTEMPT audit row (separate transaction — survives
    #          rollback of the merge transaction below).
    # =================================================================
    import uuid as _uuid

    audit_id = f"pma_{_uuid.uuid4().hex[:16]}"
    async with session_factory() as audit_db:
        async with audit_db.begin():
            audit = ProjectMergeAudit(
                id=audit_id,
                source_project_id=source_id,
                target_project_id=target_id,
                initiated_by_user_id=user_id,
                dry_run=False,
                status="started",
                persona_policy=persona_policy,
                stats=json.dumps(stats),
                persona_renames="[]",
                slug_renames="[]",
                skipped_ke_ids="[]",
                skipped_link_ids="[]",
                rules_action=None,
            )
            audit_db.add(audit)
        # Committed immediately — durable before mutation begins.

    # =================================================================
    # Phase 3: Merge mutations — single atomic transaction.
    # =================================================================
    persona_renames: list[dict] = []
    slug_renames_list: list[dict] = []
    skipped_ke_ids: list[str] = []
    skipped_link_ids: list[str] = []
    rules_action: str = "none"

    try:
        source_prefix = source_id[:_SOURCE_SUFFIX_LEN]

        # Step 1: project_repos — lock + demote + reassign.
        await _step_repos(db, source_id, target_id)

        # Step 2: agent_personas — reassign ALL, handle collisions.
        persona_renames = await _step_personas(
            db, source_id, target_id, persona_policy
        )

        # Step 3: project_rules — promote or archive.
        rules_action = await _step_rules(
            db, source_id, target_id, target_has_rules_flag, source_prefix
        )

        # Steps 4-5: knowledge_pages + wiki_page_revisions (atomically).
        slug_renames_list = await _step_knowledge_pages(
            db, source_id, target_id, slug_collisions
        )

        # Step 6: knowledge_entries — dedup + build entry-ID map.
        entry_id_map, skipped_ke_ids = await _step_knowledge_entries(
            db, source_id, target_id
        )

        # Step 7: knowledge_links — map + dedup + reassign.
        skipped_link_ids = await _step_knowledge_links(
            db, source_id, target_id, entry_id_map
        )

        # Steps 8-15: Remaining tables — straight reassign.
        await _step_straight_reassign(db, source_id, target_id)

        # Step 16: Mark source as tombstone.
        source.merged_into_project_id = target_id
        source.merged_at = func.now()

        # Step 17: Catch-up UPDATE for concurrent-sync race.
        await db.execute(
            update(Session)
            .where(Session.project_id == source_id)
            .values(project_id=target_id)
        )

        # Merge changes committed — all-or-nothing success.
        await db.commit()

        # =============================================================
        # Phase 4: Outcome-update audit row → 'completed' (fresh session).
        # =============================================================
        async with session_factory() as audit_db:
            async with audit_db.begin():
                result = await audit_db.execute(
                    update(ProjectMergeAudit)
                    .where(ProjectMergeAudit.id == audit_id)
                    .values(
                        status="completed",
                        persona_renames=json.dumps(persona_renames),
                        slug_renames=json.dumps(slug_renames_list),
                        skipped_ke_ids=json.dumps(skipped_ke_ids),
                        skipped_link_ids=json.dumps(skipped_link_ids),
                        rules_action=rules_action,
                    )
                )
                if result.rowcount == 0:
                    logger.error(
                        "merge audit row %s missing on outcome update", audit_id
                    )

        return {
            "dry_run": False,
            "stats": stats,
            "audit_id": audit_id,
            "persona_renames": persona_renames,
            "slug_renames": slug_renames_list,
            "skipped_ke_ids": skipped_ke_ids,
            "skipped_link_ids": skipped_link_ids,
            "rules_action": rules_action,
        }

    except Exception as exc:
        # =============================================================
        # Phase 4 (failure path): Outcome-update audit row → 'failed'.
        # Fresh session survives the rolled-back merge transaction.
        # =============================================================
        async with session_factory() as audit_db:
            async with audit_db.begin():
                await audit_db.execute(
                    update(ProjectMergeAudit)
                    .where(ProjectMergeAudit.id == audit_id)
                    .values(
                        status="failed",
                        error_message=f"{type(exc).__name__}: {exc}",
                    )
                )
        raise  # re-raise after audit update
