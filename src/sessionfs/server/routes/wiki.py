"""Wiki pages and knowledge links routes."""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import KnowledgeLink, KnowledgePage, Project, User
from sessionfs.server.tier_gate import UserContext, check_feature, get_user_context

logger = logging.getLogger("sessionfs.api")

router = APIRouter(prefix="/api/v1/projects", tags=["wiki"])


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class PageSummary(BaseModel):
    id: str
    slug: str
    title: str
    page_type: str
    word_count: int
    entry_count: int
    auto_generated: bool
    updated_at: datetime


class BacklinkItem(BaseModel):
    source_type: str
    source_id: str
    link_type: str
    confidence: float


class PageDetail(BaseModel):
    id: str
    project_id: str
    slug: str
    title: str
    page_type: str
    content: str
    word_count: int
    entry_count: int
    parent_slug: str | None = None
    auto_generated: bool
    created_at: datetime
    updated_at: datetime
    backlinks: list[BacklinkItem] = []


class PageWriteRequest(BaseModel):
    content: str
    title: str | None = None


class ProjectSettingsRequest(BaseModel):
    auto_narrative: bool


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _get_project_or_404(project_id: str, db: AsyncSession, user_id: str | None = None) -> Project:
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(404, "Project not found")

    if user_id and project.owner_id != user_id:
        from sessionfs.server.db.models import Session as SessionModel
        access = await db.execute(
            select(SessionModel.id)
            .where(SessionModel.user_id == user_id, SessionModel.git_remote_normalized == project.git_remote_normalized)
            .limit(1)
        )
        if access.scalar_one_or_none() is None:
            raise HTTPException(403, "No access to this project")

    return project


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/{project_id}/pages", response_model=list[PageSummary])
async def list_pages(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[PageSummary]:
    """List all wiki pages for a project."""
    await _get_project_or_404(project_id, db, user.id)

    result = await db.execute(
        select(KnowledgePage)
        .where(KnowledgePage.project_id == project_id)
        .order_by(KnowledgePage.updated_at.desc())
    )
    pages = list(result.scalars().all())

    return [
        PageSummary(
            id=p.id,
            slug=p.slug,
            title=p.title,
            page_type=p.page_type,
            word_count=p.word_count,
            entry_count=p.entry_count,
            auto_generated=p.auto_generated,
            updated_at=p.updated_at,
        )
        for p in pages
    ]


@router.get("/{project_id}/pages/{slug:path}", response_model=PageDetail)
async def get_page(
    project_id: str,
    slug: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> PageDetail:
    """Get a wiki page with backlinks."""
    await _get_project_or_404(project_id, db, user.id)

    result = await db.execute(
        select(KnowledgePage).where(
            KnowledgePage.project_id == project_id,
            KnowledgePage.slug == slug,
        )
    )
    page = result.scalar_one_or_none()
    if not page:
        raise HTTPException(404, "Page not found")

    # Fetch backlinks targeting this page
    links_result = await db.execute(
        select(KnowledgeLink).where(
            KnowledgeLink.project_id == project_id,
            KnowledgeLink.target_type == "page",
            KnowledgeLink.target_id == page.id,
        )
    )
    links = list(links_result.scalars().all())

    return PageDetail(
        id=page.id,
        project_id=page.project_id,
        slug=page.slug,
        title=page.title,
        page_type=page.page_type,
        content=page.content,
        word_count=page.word_count,
        entry_count=page.entry_count,
        parent_slug=page.parent_slug,
        auto_generated=page.auto_generated,
        created_at=page.created_at,
        updated_at=page.updated_at,
        backlinks=[
            BacklinkItem(
                source_type=lnk.source_type,
                source_id=lnk.source_id,
                link_type=lnk.link_type,
                confidence=lnk.confidence,
            )
            for lnk in links
        ],
    )


@router.put("/{project_id}/pages/{slug:path}", response_model=PageDetail)
async def create_or_update_page(
    project_id: str,
    slug: str,
    body: PageWriteRequest,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> PageDetail:
    """Create or update a wiki page."""
    check_feature(ctx, "project_context")
    await _get_project_or_404(project_id, db, user.id)

    result = await db.execute(
        select(KnowledgePage).where(
            KnowledgePage.project_id == project_id,
            KnowledgePage.slug == slug,
        )
    )
    page = result.scalar_one_or_none()

    now = datetime.now(timezone.utc)
    word_count = len(body.content.split()) if body.content.strip() else 0

    if page:
        page.content = body.content
        page.word_count = word_count
        page.updated_at = now
        if body.title is not None:
            page.title = body.title
    else:
        title = body.title or slug.replace("-", " ").title()
        page = KnowledgePage(
            id=f"page_{uuid.uuid4().hex[:16]}",
            project_id=project_id,
            slug=slug,
            title=title,
            page_type="section",
            content=body.content,
            word_count=word_count,
            created_at=now,
            updated_at=now,
        )
        db.add(page)

    await db.commit()
    await db.refresh(page)

    return PageDetail(
        id=page.id,
        project_id=page.project_id,
        slug=page.slug,
        title=page.title,
        page_type=page.page_type,
        content=page.content,
        word_count=page.word_count,
        entry_count=page.entry_count,
        parent_slug=page.parent_slug,
        auto_generated=page.auto_generated,
        created_at=page.created_at,
        updated_at=page.updated_at,
        backlinks=[],
    )


@router.delete("/{project_id}/pages/{slug:path}")
async def delete_page(
    project_id: str,
    slug: str,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete a wiki page."""
    check_feature(ctx, "project_context")
    await _get_project_or_404(project_id, db, user.id)

    result = await db.execute(
        select(KnowledgePage).where(
            KnowledgePage.project_id == project_id,
            KnowledgePage.slug == slug,
        )
    )
    page = result.scalar_one_or_none()
    if not page:
        raise HTTPException(404, "Page not found")

    await db.delete(page)
    await db.commit()

    return {"status": "deleted", "slug": slug}


class RegenerateRequest(BaseModel):
    llm_api_key: str | None = None
    model: str | None = None
    provider: str | None = None
    base_url: str | None = None


@router.post("/{project_id}/pages/{slug:path}/regenerate")
async def regenerate_page(
    project_id: str,
    slug: str,
    body: RegenerateRequest | None = None,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Regenerate an auto-generated concept page from latest entries."""
    check_feature(ctx, "project_context")
    await _get_project_or_404(project_id, db, user.id)

    result = await db.execute(
        select(KnowledgePage).where(
            KnowledgePage.project_id == project_id,
            KnowledgePage.slug == slug,
        )
    )
    page = result.scalar_one_or_none()
    if not page:
        raise HTTPException(404, "Page not found")

    if not page.auto_generated:
        raise HTTPException(400, "Only auto-generated pages can be regenerated")

    body = body or RegenerateRequest()

    # Get linked entries via knowledge_links
    from sessionfs.server.db.models import KnowledgeEntry, KnowledgeLink

    links_result = await db.execute(
        select(KnowledgeLink).where(
            KnowledgeLink.project_id == project_id,
            KnowledgeLink.source_type == "entry",
            KnowledgeLink.target_id == page.id,
        )
    )
    links = list(links_result.scalars().all())

    entries: list = []
    if links:
        entry_ids = [int(lnk.source_id) for lnk in links]
        entries_result = await db.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.id.in_(entry_ids),
            )
        )
        entries = list(entries_result.scalars().all())

    # If no linked entries, search by page title
    if not entries:
        title_words = page.title.lower().split()
        all_entries_result = await db.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.project_id == project_id,
                KnowledgeEntry.dismissed == False,  # noqa: E712
            )
        )
        all_entries = list(all_entries_result.scalars().all())
        entries = [
            e for e in all_entries
            if any(w in e.content.lower() for w in title_words if len(w) > 3)
        ]

    # Generate updated article
    from sessionfs.server.services.compiler import generate_concept_article

    content_before = page.content
    article = await generate_concept_article(
        topic=page.title,
        summary=f"Regenerated article about {page.title}",
        entries=entries,
        user_id=user.id,
        api_key=body.llm_api_key,
        model=body.model or "claude-sonnet-4",
        provider=body.provider,
        base_url=body.base_url,
    )

    # Store before/after in context_compilations
    from sessionfs.server.db.models import ContextCompilation

    compilation = ContextCompilation(
        project_id=project_id,
        user_id=user.id,
        entries_compiled=len(entries),
        context_before=content_before,
        context_after=article,
    )
    db.add(compilation)

    # Update the page
    now = datetime.now(timezone.utc)
    page.content = article
    page.word_count = len(article.split())
    page.entry_count = len(entries)
    page.updated_at = now

    await db.commit()

    return {
        "status": "regenerated",
        "slug": slug,
        "word_count": page.word_count,
        "entries_used": len(entries),
    }


@router.get("/{project_id}/links/{target_type}/{target_id}", response_model=list[BacklinkItem])
async def get_backlinks(
    project_id: str,
    target_type: str,
    target_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[BacklinkItem]:
    """Get backlinks for a target."""
    await _get_project_or_404(project_id, db, user.id)

    result = await db.execute(
        select(KnowledgeLink).where(
            KnowledgeLink.project_id == project_id,
            KnowledgeLink.target_type == target_type,
            KnowledgeLink.target_id == target_id,
        )
    )
    links = list(result.scalars().all())

    return [
        BacklinkItem(
            source_type=lnk.source_type,
            source_id=lnk.source_id,
            link_type=lnk.link_type,
            confidence=lnk.confidence,
        )
        for lnk in links
    ]


@router.put("/{project_id}/settings")
async def update_project_settings(
    project_id: str,
    body: ProjectSettingsRequest,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update project settings (auto_narrative)."""
    check_feature(ctx, "project_context")
    project = await _get_project_or_404(project_id, db, user.id)

    project.auto_narrative = body.auto_narrative
    project.updated_at = datetime.now(timezone.utc)
    await db.commit()

    return {"status": "updated", "auto_narrative": body.auto_narrative}
