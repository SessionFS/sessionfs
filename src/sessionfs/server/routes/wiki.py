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
from sessionfs.server.db.models import (
    AgentPersona,
    KnowledgeLink,
    KnowledgePage,
    Project,
    RetrievalAuditContext,
    Ticket,
    User,
    WikiPageRevision,
)
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
    persona_name: str | None = None
    ticket_id: str | None = None


class WikiRevisionResponse(BaseModel):
    """v0.10.7 — single row from wiki_page_revisions history.

    `id` is the internal row id used as the keyset-pagination cursor by
    `GET .../history?cursor=<id>`. Surfaced on the response so callers
    can paginate without guessing.
    """

    id: int
    revision_number: int
    revised_at: datetime
    title: str
    word_count: int
    user_id: str | None
    persona_name: str | None
    ticket_id: str | None


class WikiHistoryResponse(BaseModel):
    """v0.10.7 — paginated revision history for a wiki page.

    `next_cursor` is the `id` of the OLDEST revision in this page when
    more rows are available; pass it back as `?cursor=<next_cursor>`
    to fetch the next older page. None when no more rows.
    """

    slug: str
    revisions: list[WikiRevisionResponse]
    count: int
    next_cursor: int | None = None


class ProjectSettingsRequest(BaseModel):
    auto_narrative: bool
    kb_retention_days: int | None = None
    kb_max_context_words: int | None = None
    kb_section_page_limit: int | None = None


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


@router.get(
    "/{project_id}/pages/{slug:path}/history",
    response_model=WikiHistoryResponse,
)
async def get_page_history(
    project_id: str,
    slug: str,
    limit: int = 50,
    cursor: int | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> WikiHistoryResponse:
    """v0.10.7 — return the wiki page's full revision history.

    Cross-project access blocked by `_get_project_or_404`. Pagination:
    pass `cursor` (the `id` of the last revision from the previous
    page) for keyset pagination. Sort is `revised_at DESC, id DESC` so
    same-timestamp ties resolve deterministically (mirrors v0.10.5
    ContextCompilation history ordering).

    Declared BEFORE the `{slug:path}` GET so FastAPI matches the
    `/history` suffix here rather than treating `slug/history` as a
    single page slug.
    """
    await _get_project_or_404(project_id, db, user.id)
    limit = max(1, min(limit, 200))
    conds = [
        WikiPageRevision.project_id == project_id,
        WikiPageRevision.page_slug == slug,
    ]
    if cursor is not None:
        conds.append(WikiPageRevision.id < cursor)
    # Fetch limit+1 so we can detect whether more rows exist without
    # a second COUNT query — same pattern as v0.9.9 list_knowledge_entries.
    rows = (
        await db.execute(
            select(WikiPageRevision)
            .where(*conds)
            .order_by(
                WikiPageRevision.revised_at.desc(),
                WikiPageRevision.id.desc(),
            )
            .limit(limit + 1)
        )
    ).scalars().all()
    has_more = len(rows) > limit
    if has_more:
        rows = rows[:limit]
    revisions = [
        WikiRevisionResponse(
            id=r.id,
            revision_number=r.revision_number,
            revised_at=r.revised_at,
            title=r.title,
            word_count=r.word_count,
            user_id=r.user_id,
            persona_name=r.persona_name,
            ticket_id=r.ticket_id,
        )
        for r in rows
    ]
    next_cursor = revisions[-1].id if has_more and revisions else None
    return WikiHistoryResponse(
        slug=slug,
        revisions=revisions,
        count=len(revisions),
        next_cursor=next_cursor,
    )


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


async def _validate_revision_provenance(
    project_id: str,
    user_id: str,
    persona_name: str | None,
    ticket_id: str | None,
    db: AsyncSession,
) -> None:
    """v0.10.7 — guard against forged provenance on wiki revisions.

    persona_name (if supplied) must exist in this project. ticket_id
    (if supplied) must belong to this project AND the writing user
    must own it through ONE of three roles:
      - ticket creator (`Ticket.created_by_user_id`)
      - current resolver (`Ticket.resolver_user_id`, set on complete)
      - active executor — a RetrievalAuditContext exists for this
        ticket, was created by this user via start_ticket, has the
        same `lease_epoch` as the ticket's current lease_epoch, AND
        the ticket is still `in_progress` (R4 hardening — closed_at
        is never set anywhere so it can't gate executor expiry;
        lease_epoch + status match is the real gate).

    Executor write rights expire when:
      - someone force-starts the ticket (lease_epoch bumps)
      - the ticket is completed (status → review)
      - the ticket is accepted (status → done)
      - the ticket is blocked/cancelled (status changes)

    Without the executor path, a team agent who STARTED a colleague's
    ticket and is now executing it cannot attribute wiki revisions
    to that ticket — which would defeat the agent-execution provenance
    use case. Mirrors the v0.10.4 retrieval-audit context-create
    validation + cb8a9da cross-project leak defense.
    """
    if persona_name:
        persona = (
            await db.execute(
                select(AgentPersona.id).where(
                    AgentPersona.project_id == project_id,
                    AgentPersona.name == persona_name,
                )
            )
        ).scalar_one_or_none()
        if persona is None:
            raise HTTPException(
                422,
                f"persona_name {persona_name!r} not found in this project",
            )
    if ticket_id:
        ticket_row = (
            await db.execute(
                select(
                    Ticket.created_by_user_id,
                    Ticket.resolver_user_id,
                    Ticket.lease_epoch,
                    Ticket.status,
                ).where(
                    Ticket.id == ticket_id,
                    Ticket.project_id == project_id,
                )
            )
        ).one_or_none()
        if ticket_row is None:
            raise HTTPException(
                422,
                f"ticket_id {ticket_id!r} not found in this project",
            )
        created_by, resolver_by, ticket_lease, ticket_status = ticket_row
        # v0.10.7 R3 — executor association: a user who STARTED this
        # ticket has a RetrievalAuditContext with matching lease_epoch
        # created by start_ticket(). v0.10.7 R4 hardening — the
        # earlier `closed_at IS NULL` gate was effectively a no-op
        # because nothing in the codebase ever SETS closed_at, so a
        # user who started a ticket once retained write rights forever
        # even after complete/accept/cancel/force-start. Tighter gate:
        # the RetrievalAuditContext.lease_epoch must match the
        # ticket's CURRENT lease_epoch (force-start bumps the ticket
        # epoch → old context is stale) AND the ticket must still be
        # in_progress (after complete/accept the executor stops being
        # the writer). Both conditions together expire executor write
        # rights cleanly without us having to backfill closed_at on
        # every transition.
        is_owner = user_id in {created_by, resolver_by}
        if not is_owner and ticket_status == "in_progress":
            executor_match = (
                await db.execute(
                    select(RetrievalAuditContext.id).where(
                        RetrievalAuditContext.ticket_id == ticket_id,
                        RetrievalAuditContext.project_id == project_id,
                        RetrievalAuditContext.created_by_user_id == user_id,
                        RetrievalAuditContext.lease_epoch == ticket_lease,
                    )
                )
            ).scalar_one_or_none()
            is_owner = executor_match is not None
        if not is_owner:
            raise HTTPException(
                422,
                (
                    f"ticket_id {ticket_id!r} is not owned by you "
                    "(must be the ticket creator, current resolver, "
                    "or the active executor with a matching lease "
                    "while the ticket is in_progress to attribute "
                    "a wiki revision to it)"
                ),
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
    await _validate_revision_provenance(
        project_id, user.id, body.persona_name, body.ticket_id, db
    )

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
        effective_title = page.title
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
        effective_title = title

    # v0.10.7 — append revision row. Computed monotone number scoped
    # to (project_id, page_slug) so concurrent writers from different
    # projects (or different pages) don't collide. The UNIQUE constraint
    # on (project_id, page_slug, revision_number) is the safety net:
    # a true race (two writers picking the same N+1) raises IntegrityError
    # which we translate to HTTP 409. Clients should retry on 409.
    # SELECT FOR UPDATE on the page row is not used because SQLite
    # doesn't support it; the catch+409 path works on both backends.
    # Numbering bypasses DELETEd history rows by design — gaps are
    # tolerated; uniqueness is the invariant.
    from sqlalchemy import func as sql_func
    from sqlalchemy.exc import IntegrityError

    max_rev = (
        await db.execute(
            select(sql_func.max(WikiPageRevision.revision_number)).where(
                WikiPageRevision.project_id == project_id,
                WikiPageRevision.page_slug == slug,
            )
        )
    ).scalar_one_or_none()
    revision = WikiPageRevision(
        project_id=project_id,
        page_slug=slug,
        revision_number=(max_rev or 0) + 1,
        title=effective_title,
        content_snapshot=body.content,
        word_count=word_count,
        user_id=user.id,
        persona_name=body.persona_name,
        ticket_id=body.ticket_id,
        revised_at=now,
    )
    db.add(revision)

    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            409,
            (
                "Concurrent wiki page write detected (revision_number "
                "race). Retry the request."
            ),
        )
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
    """Update project settings (auto_narrative, lifecycle settings)."""
    check_feature(ctx, "project_context")
    project = await _get_project_or_404(project_id, db, user.id)

    project.auto_narrative = body.auto_narrative
    if body.kb_retention_days is not None:
        project.kb_retention_days = body.kb_retention_days
    if body.kb_max_context_words is not None:
        project.kb_max_context_words = body.kb_max_context_words
    if body.kb_section_page_limit is not None:
        project.kb_section_page_limit = body.kb_section_page_limit
    project.updated_at = datetime.now(timezone.utc)
    await db.commit()

    return {
        "status": "updated",
        "auto_narrative": body.auto_narrative,
        "kb_retention_days": project.kb_retention_days,
        "kb_max_context_words": project.kb_max_context_words,
        "kb_section_page_limit": project.kb_section_page_limit,
    }
