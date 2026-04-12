"""Knowledge entries and compilation routes."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import ContextCompilation, KnowledgeEntry, Project, User

logger = logging.getLogger("sessionfs.api")

router = APIRouter(prefix="/api/v1/projects", tags=["knowledge"])


class KnowledgeEntryResponse(BaseModel):
    id: int
    project_id: str
    session_id: str
    user_id: str
    entry_type: str
    content: str
    confidence: float
    source_context: str | None = None
    created_at: datetime
    compiled_at: datetime | None = None
    dismissed: bool = False


class CompilationResponse(BaseModel):
    id: int
    project_id: str
    user_id: str
    entries_compiled: int
    context_before: str | None = None
    context_after: str | None = None
    compiled_at: datetime


class CompileRequest(BaseModel):
    llm_api_key: str | None = None
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None


class AddEntryRequest(BaseModel):
    content: str
    entry_type: str = "discovery"
    session_id: str | None = None
    confidence: float = 1.0
    source_context: str | None = None


class DismissRequest(BaseModel):
    dismissed: bool = True


class HealthResponse(BaseModel):
    project_id: str
    total_entries: int
    pending_entries: int
    compiled_entries: int
    dismissed_entries: int
    total_compilations: int
    last_compilation_at: datetime | None = None
    word_count: int = 0
    section_count: int = 0
    last_compiled: datetime | None = None
    potentially_stale: bool = False
    recommendations: list[str] = []
    stale_entry_count: int = 0
    low_confidence_count: int = 0
    decayed_count: int = 0


from sessionfs.server.services.knowledge import word_overlap as _word_overlap  # noqa: E402


async def _get_project_or_404(project_id: str, db: AsyncSession, user_id: str | None = None) -> Project:
    """Get project by ID, verify access, or raise 404/403."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(404, "Project not found")

    # Enforce access control if user_id provided
    if user_id and project.owner_id != user_id:
        from sessionfs.server.db.models import Session
        access = await db.execute(
            select(Session.id)
            .where(Session.user_id == user_id, Session.git_remote_normalized == project.git_remote_normalized)
            .limit(1)
        )
        if access.scalar_one_or_none() is None:
            raise HTTPException(403, "No access to this project")

    return project


@router.get("/{project_id}/entries", response_model=list[KnowledgeEntryResponse])
async def list_entries(
    project_id: str,
    type: str | None = Query(None, description="Filter by entry type"),
    pending: bool | None = Query(None, description="Filter by pending status"),
    search: str | None = Query(None, description="Search content (case-insensitive substring)"),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[KnowledgeEntryResponse]:
    """List knowledge entries for a project."""
    await _get_project_or_404(project_id, db, user.id)

    stmt = select(KnowledgeEntry).where(KnowledgeEntry.project_id == project_id)

    if search is not None:
        stmt = stmt.where(KnowledgeEntry.content.ilike(f"%{search}%"))
    if type is not None:
        stmt = stmt.where(KnowledgeEntry.entry_type == type)
    if pending is True:
        stmt = stmt.where(
            KnowledgeEntry.compiled_at.is_(None),
            KnowledgeEntry.dismissed == False,  # noqa: E712
        )
    elif pending is False:
        stmt = stmt.where(KnowledgeEntry.compiled_at.isnot(None))

    stmt = stmt.order_by(KnowledgeEntry.created_at.desc()).limit(limit)
    result = await db.execute(stmt)
    entries = list(result.scalars().all())

    # Track relevance when entries are returned via search
    if search and entries:
        entry_ids = [e.id for e in entries]
        await db.execute(
            update(KnowledgeEntry)
            .where(KnowledgeEntry.id.in_(entry_ids))
            .values(
                last_relevant_at=datetime.now(timezone.utc),
                reference_count=KnowledgeEntry.reference_count + 1,
            )
        )
        await db.commit()

    return [
        KnowledgeEntryResponse(
            id=e.id,
            project_id=e.project_id,
            session_id=e.session_id,
            user_id=e.user_id,
            entry_type=e.entry_type,
            content=e.content,
            confidence=e.confidence,
            source_context=e.source_context,
            created_at=e.created_at,
            compiled_at=e.compiled_at,
            dismissed=e.dismissed,
        )
        for e in entries
    ]


@router.post("/{project_id}/entries/add", response_model=KnowledgeEntryResponse, status_code=201)
async def add_entry(
    project_id: str,
    body: AddEntryRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeEntryResponse:
    """Create a single knowledge entry (used by MCP tools and external clients)."""
    await _get_project_or_404(project_id, db, user.id)

    valid_types = {"decision", "pattern", "discovery", "convention", "bug", "dependency"}
    if body.entry_type not in valid_types:
        from fastapi import HTTPException as _HTTPException
        raise _HTTPException(422, f"Invalid entry_type. Must be one of: {', '.join(sorted(valid_types))}")

    # Gate 1: Minimum content length
    if len(body.content) < 20:
        raise HTTPException(422, "Content too short — minimum 20 characters required")

    session_id = body.session_id or "manual"

    # Gate 2: Rate limit — max 20 entries per session_id+project in the last hour
    one_hour_ago = datetime.now(timezone.utc) - timedelta(hours=1)
    rate_result = await db.execute(
        select(func.count(KnowledgeEntry.id)).where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.session_id == session_id,
            KnowledgeEntry.created_at >= one_hour_ago,
        )
    )
    recent_count = rate_result.scalar() or 0
    if recent_count >= 20:
        raise HTTPException(429, "Rate limit exceeded — max 20 entries per session per hour")

    # Gate 3: Similarity check against recent non-dismissed entries
    recent_result = await db.execute(
        select(KnowledgeEntry.content)
        .where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.dismissed == False,  # noqa: E712
        )
        .order_by(KnowledgeEntry.created_at.desc())
        .limit(50)
    )
    for (existing_content,) in recent_result.all():
        if _word_overlap(body.content, existing_content) > 0.85:
            raise HTTPException(409, "Similar entry already exists")

    # Gate 4: Lower confidence for cli-ask/manual sources
    confidence = body.confidence
    if session_id in ("cli-ask", "manual"):
        confidence = min(confidence, 0.7)

    entry = KnowledgeEntry(
        project_id=project_id,
        session_id=session_id,
        user_id=user.id,
        entry_type=body.entry_type,
        content=body.content,
        confidence=confidence,
        source_context=body.source_context,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)

    return KnowledgeEntryResponse(
        id=entry.id,
        project_id=entry.project_id,
        session_id=entry.session_id,
        user_id=entry.user_id,
        entry_type=entry.entry_type,
        content=entry.content,
        confidence=entry.confidence,
        source_context=entry.source_context,
        created_at=entry.created_at,
        compiled_at=entry.compiled_at,
        dismissed=entry.dismissed,
    )


@router.put("/{project_id}/entries/{entry_id}", response_model=KnowledgeEntryResponse)
async def dismiss_entry(
    project_id: str,
    entry_id: int,
    body: DismissRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeEntryResponse:
    """Dismiss or un-dismiss a knowledge entry."""
    await _get_project_or_404(project_id, db, user.id)

    result = await db.execute(
        select(KnowledgeEntry).where(
            KnowledgeEntry.id == entry_id,
            KnowledgeEntry.project_id == project_id,
        )
    )
    entry = result.scalar_one_or_none()
    if not entry:
        raise HTTPException(404, "Entry not found")

    entry.dismissed = body.dismissed
    await db.commit()
    await db.refresh(entry)

    return KnowledgeEntryResponse(
        id=entry.id,
        project_id=entry.project_id,
        session_id=entry.session_id,
        user_id=entry.user_id,
        entry_type=entry.entry_type,
        content=entry.content,
        confidence=entry.confidence,
        source_context=entry.source_context,
        created_at=entry.created_at,
        compiled_at=entry.compiled_at,
        dismissed=entry.dismissed,
    )


@router.post("/{project_id}/entries/dismiss-stale")
async def dismiss_stale_entries(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Bulk-dismiss stale entries (> 90 days unreferenced + low confidence).

    Returns the count of entries dismissed so the dashboard can update its
    health banner without a full page refresh.
    """
    await _get_project_or_404(project_id, db, user.id)

    from sqlalchemy import or_
    ninety_days_ago = datetime.now(timezone.utc) - timedelta(days=90)

    # Only dismiss entries that are BOTH stale (old/unreferenced) AND
    # low-confidence (< 0.5). High-confidence stale entries are likely
    # real decisions/patterns that just haven't been referenced recently
    # — they should decay naturally via the compile-time 0.8x multiplier,
    # not be bulk-dismissed.
    result = await db.execute(
        update(KnowledgeEntry)
        .where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.dismissed == False,  # noqa: E712
            KnowledgeEntry.confidence < 0.5,
            or_(
                KnowledgeEntry.last_relevant_at.is_(None) & (KnowledgeEntry.created_at < ninety_days_ago),
                KnowledgeEntry.last_relevant_at < ninety_days_ago,
            ),
        )
        .values(dismissed=True)
        .execution_options(synchronize_session=False)
    )
    count = result.rowcount
    await db.commit()

    return {"dismissed_count": count}


@router.post("/{project_id}/compile", response_model=CompilationResponse)
async def compile_context(
    project_id: str,
    body: CompileRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CompilationResponse:
    """Compile pending knowledge entries into project context."""
    await _get_project_or_404(project_id, db, user.id)

    from sessionfs.server.services.compiler import (
        auto_generate_concepts,
        compile_project_context,
    )

    body = body or CompileRequest()
    compilation = await compile_project_context(
        project_id=project_id,
        user_id=user.id,
        db=db,
        api_key=body.llm_api_key,
        model=body.model or "claude-sonnet-4",
        provider=body.provider,
        base_url=body.base_url,
    )

    # Always run concept page refresh — even when there were no pending
    # entries to compile (compilation is None). Concept pages may need
    # cleanup because entries were dismissed or clusters shrank below the
    # threshold since the last compile. Previously, returning early when
    # compilation is None skipped this entirely, leaving dead concept
    # pages lingering until the next compile that happened to have real
    # entries.
    try:
        await auto_generate_concepts(
            project_id=project_id,
            user_id=user.id,
            db=db,
            api_key=body.llm_api_key,
            model=body.model or "claude-sonnet-4",
            provider=body.provider,
            base_url=body.base_url,
        )
    except Exception:
        logger.warning("Concept auto-generation/cleanup failed (non-fatal)", exc_info=True)

    if not compilation:
        return CompilationResponse(
            id=0,
            project_id=project_id,
            user_id=user.id,
            entries_compiled=0,
            context_before=None,
            context_after=None,
            compiled_at=datetime.now(timezone.utc),
        )

    # (Legacy) concept generation also runs after real compilation
    # for compatibility — the above call covers the cleanup-only path.
    try:
        await auto_generate_concepts(
            project_id=project_id,
            user_id=user.id,
            db=db,
            api_key=body.llm_api_key,
            model=body.model or "claude-sonnet-4",
            provider=body.provider,
            base_url=body.base_url,
        )
    except Exception:
        logger.warning("Concept auto-generation failed (non-fatal)", exc_info=True)

    return CompilationResponse(
        id=compilation.id,
        project_id=compilation.project_id,
        user_id=compilation.user_id,
        entries_compiled=compilation.entries_compiled,
        context_before=compilation.context_before,
        context_after=compilation.context_after,
        compiled_at=compilation.compiled_at,
    )


@router.get("/{project_id}/compilations", response_model=list[CompilationResponse])
async def list_compilations(
    project_id: str,
    limit: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[CompilationResponse]:
    """List compilation history for a project."""
    await _get_project_or_404(project_id, db, user.id)

    result = await db.execute(
        select(ContextCompilation)
        .where(ContextCompilation.project_id == project_id)
        .order_by(ContextCompilation.compiled_at.desc())
        .limit(limit)
    )
    compilations = list(result.scalars().all())

    return [
        CompilationResponse(
            id=c.id,
            project_id=c.project_id,
            user_id=c.user_id,
            entries_compiled=c.entries_compiled,
            context_before=c.context_before,
            context_after=c.context_after,
            compiled_at=c.compiled_at,
        )
        for c in compilations
    ]


@router.get("/{project_id}/health", response_model=HealthResponse)
async def project_health(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> HealthResponse:
    """Get knowledge health status for a project."""
    await _get_project_or_404(project_id, db, user.id)

    # Total entries
    total_result = await db.execute(
        select(func.count(KnowledgeEntry.id)).where(
            KnowledgeEntry.project_id == project_id
        )
    )
    total_entries = total_result.scalar() or 0

    # Pending entries
    pending_result = await db.execute(
        select(func.count(KnowledgeEntry.id)).where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.compiled_at.is_(None),
            KnowledgeEntry.dismissed == False,  # noqa: E712
        )
    )
    pending_entries = pending_result.scalar() or 0

    # Compiled entries
    compiled_result = await db.execute(
        select(func.count(KnowledgeEntry.id)).where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.compiled_at.isnot(None),
        )
    )
    compiled_entries = compiled_result.scalar() or 0

    # Dismissed entries
    dismissed_result = await db.execute(
        select(func.count(KnowledgeEntry.id)).where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.dismissed == True,  # noqa: E712
        )
    )
    dismissed_entries = dismissed_result.scalar() or 0

    # Compilation stats
    compilation_count_result = await db.execute(
        select(func.count(ContextCompilation.id)).where(
            ContextCompilation.project_id == project_id
        )
    )
    total_compilations = compilation_count_result.scalar() or 0

    last_compilation_result = await db.execute(
        select(ContextCompilation.compiled_at)
        .where(ContextCompilation.project_id == project_id)
        .order_by(ContextCompilation.compiled_at.desc())
        .limit(1)
    )
    last_compilation_at = last_compilation_result.scalar_one_or_none()

    # Context document analysis
    project = await _get_project_or_404(project_id, db, user.id)
    context_doc = project.context_document or ""
    word_count = len(context_doc.split()) if context_doc.strip() else 0
    section_count = sum(1 for line in context_doc.splitlines() if line.startswith("## "))

    # Staleness detection: check if pending entries mention numbers/terms not in the doc
    potentially_stale = False
    if pending_entries > 0 and context_doc.strip():
        pending_stmt = select(KnowledgeEntry.content).where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.compiled_at.is_(None),
            KnowledgeEntry.dismissed == False,  # noqa: E712
        )
        pending_result_entries = await db.execute(pending_stmt)
        pending_contents = [row[0] for row in pending_result_entries.all()]
        # Flag stale if any pending entry content is not found in the document
        for content in pending_contents:
            # Extract key terms (words longer than 4 chars) from entry
            terms = [w for w in content.split() if len(w) > 4]
            if terms and not any(term.lower() in context_doc.lower() for term in terms[:3]):
                potentially_stale = True
                break

    # Actionable metrics
    ninety_days_ago = datetime.now(timezone.utc) - timedelta(days=90)

    # Stale = never referenced AND created > 90 days ago, OR referenced but
    # last_relevant_at is itself older than 90 days. This matches the decay
    # logic in the compiler which decays both flavors. Previously only the
    # first case (last_relevant_at IS NULL) was counted, underreporting
    # entries that were referenced once and then forgotten.
    from sqlalchemy import or_
    stale_result = await db.execute(
        select(func.count(KnowledgeEntry.id)).where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.dismissed == False,  # noqa: E712
            or_(
                KnowledgeEntry.last_relevant_at.is_(None) & (KnowledgeEntry.created_at < ninety_days_ago),
                KnowledgeEntry.last_relevant_at < ninety_days_ago,
            ),
        )
    )
    stale_entry_count = stale_result.scalar() or 0

    low_conf_result = await db.execute(
        select(func.count(KnowledgeEntry.id)).where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.dismissed == False,  # noqa: E712
            KnowledgeEntry.confidence < 0.3,
        )
    )
    low_confidence_count = low_conf_result.scalar() or 0

    decayed_result = await db.execute(
        select(func.count(KnowledgeEntry.id)).where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.dismissed == False,  # noqa: E712
            KnowledgeEntry.confidence >= 0.1,
            KnowledgeEntry.confidence <= 0.5,
            KnowledgeEntry.created_at < ninety_days_ago,
        )
    )
    decayed_count = decayed_result.scalar() or 0

    # Build recommendations
    recommendations: list[str] = []
    if stale_entry_count > 10:
        recommendations.append(
            f"Consider compiling to trigger decay on {stale_entry_count} stale entries"
        )
    if low_confidence_count > 5:
        recommendations.append(
            f"{low_confidence_count} low-confidence entries may be auto-dismissed on next compile"
        )
    if pending_entries > 20:
        recommendations.append(
            f"Run compile to process {pending_entries} pending entries"
        )
    max_budget = getattr(project, "kb_max_context_words", 8000) or 8000
    if word_count > int(max_budget * 0.75):
        recommendations.append(
            f"Context document is {word_count} words — approaching {max_budget:,} word budget"
        )
    if total_entries > 0 and total_compilations == 0:
        recommendations.append(
            "No compilations yet — run compile to build context"
        )

    return HealthResponse(
        project_id=project_id,
        total_entries=total_entries,
        pending_entries=pending_entries,
        compiled_entries=compiled_entries,
        dismissed_entries=dismissed_entries,
        total_compilations=total_compilations,
        last_compilation_at=last_compilation_at,
        word_count=word_count,
        section_count=section_count,
        last_compiled=last_compilation_at,
        potentially_stale=potentially_stale,
        recommendations=recommendations,
        stale_entry_count=stale_entry_count,
        low_confidence_count=low_confidence_count,
        decayed_count=decayed_count,
    )
