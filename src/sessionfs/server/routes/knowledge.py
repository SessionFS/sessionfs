"""Knowledge entries and compilation routes."""

from __future__ import annotations

import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
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


async def _get_project_or_404(project_id: str, db: AsyncSession) -> Project:
    """Get project by ID or raise 404."""
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(404, "Project not found")
    return project


@router.get("/{project_id}/entries", response_model=list[KnowledgeEntryResponse])
async def list_entries(
    project_id: str,
    type: str | None = Query(None, description="Filter by entry type"),
    pending: bool | None = Query(None, description="Filter by pending status"),
    limit: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[KnowledgeEntryResponse]:
    """List knowledge entries for a project."""
    await _get_project_or_404(project_id, db)

    stmt = select(KnowledgeEntry).where(KnowledgeEntry.project_id == project_id)

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


@router.put("/{project_id}/entries/{entry_id}", response_model=KnowledgeEntryResponse)
async def dismiss_entry(
    project_id: str,
    entry_id: int,
    body: DismissRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> KnowledgeEntryResponse:
    """Dismiss or un-dismiss a knowledge entry."""
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


@router.post("/{project_id}/compile", response_model=CompilationResponse)
async def compile_context(
    project_id: str,
    body: CompileRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CompilationResponse:
    """Compile pending knowledge entries into project context."""
    await _get_project_or_404(project_id, db)

    from sessionfs.server.services.compiler import compile_project_context

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

    if not compilation:
        raise HTTPException(404, "No pending entries to compile")

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
    await _get_project_or_404(project_id, db)

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
    await _get_project_or_404(project_id, db)

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

    return HealthResponse(
        project_id=project_id,
        total_entries=total_entries,
        pending_entries=pending_entries,
        compiled_entries=compiled_entries,
        dismissed_entries=dismissed_entries,
        total_compilations=total_compilations,
        last_compilation_at=last_compilation_at,
    )
