"""Project context CRUD routes."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import Project, Session, User
from sessionfs.server.tier_gate import UserContext, check_feature, get_user_context

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

DEFAULT_TEMPLATE = """\
# Project Context

## Overview
<!-- What is this project? One paragraph. -->

## Architecture
<!-- Tech stack, infrastructure, key services. -->

## Conventions
<!-- Coding standards, branch strategy, PR process. -->

## API Contracts
<!-- Key endpoints, request/response formats. -->

## Key Decisions
<!-- Important decisions that are locked and shouldn't be revisited. -->

## Team
<!-- Who works on what. -->
"""


class CreateProjectRequest(BaseModel):
    name: str
    git_remote_normalized: str
    # v0.10.0 Phase 5 — optional org scope on creation. If omitted, the
    # project is personal (org_id stays NULL). If provided, the creator
    # must be a member of that org (server validates).
    org_id: str | None = None


class UpdateContextRequest(BaseModel):
    context_document: str


class ProjectResponse(BaseModel):
    id: str
    name: str
    git_remote_normalized: str
    context_document: str
    owner_id: str
    created_at: datetime
    updated_at: datetime
    session_count: int = 0
    auto_narrative: bool = False
    kb_retention_days: int = 180
    kb_max_context_words: int = 2000
    kb_section_page_limit: int = 30


async def _check_repo_access(db: AsyncSession, user_id: str, git_remote: str) -> bool:
    """Check if user has sessions in this repo (grants read/write access).

    P2 (§3.3 B6): resolve the project for this remote first, then
    check by project_id (not git_remote_normalized).  After multi-repo,
    project.git_remote_normalized is only the primary remote; a user
    with sessions on non-primary repos should still have access.
    """
    from sessionfs.server.services.project_resolver import (
        resolve_project_by_remote,
    )
    project = await resolve_project_by_remote(db, git_remote)
    if project is None:
        return False
    stmt = (
        select(Session.id)
        .where(Session.user_id == user_id, Session.project_id == project.id)
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


@router.get("/", response_model=list[ProjectResponse])
async def list_projects(
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectResponse]:
    """List all projects the user has access to.

    Access predicate (mirrors `auth.project_access.user_can_access_project`):
      - owner
      - member of the project's org (v0.10.22 fix — tk_7a457574c5624e12;
        previously the listing ignored OrgMember entirely so new org
        members saw an empty `GET /api/v1/projects` for projects they
        had every right to read)
      - has captured a session on the project's git remote (legacy
        fallback so personal projects still surface for teammates who
        synced on the same repo)
    """
    from sqlalchemy import distinct, func, or_

    from sessionfs.server.db.models import OrgMember

    # Get git remotes from user's sessions
    session_remotes_stmt = select(distinct(Session.git_remote_normalized)).where(
        Session.user_id == user.id,
        Session.git_remote_normalized.isnot(None),
        Session.git_remote_normalized != "",
    )
    result = await db.execute(session_remotes_stmt)
    user_remotes = {r[0] for r in result.all()}

    # Get projects: owned by user OR scoped to an org the user is a
    # member of OR matching user's session remotes.
    # P2 (§3.3 B4): match through project_repos (any of the project's
    # repos matches a remote the user has sessions on) with legacy
    # primary-remote fallback.
    user_org_ids_stmt = select(OrgMember.org_id).where(OrgMember.user_id == user.id)
    conditions = [
        Project.owner_id == user.id,
        Project.org_id.in_(user_org_ids_stmt),
    ]
    if user_remotes:
        from sessionfs.server.db.models import ProjectRepo
        multi_repo_match = select(ProjectRepo.project_id).where(
            ProjectRepo.git_remote_normalized.in_(user_remotes)
        )
        conditions.append(or_(
            Project.git_remote_normalized.in_(user_remotes),  # legacy fallback
            Project.id.in_(multi_repo_match),                  # multi-repo path
        ))
    stmt = select(Project).where(or_(*conditions)).order_by(Project.updated_at.desc())
    result = await db.execute(stmt)
    projects: list[Project] = list(result.scalars().all())

    # P2 (§3.3 B5): count sessions by project_id, not git_remote_normalized.
    # After multi-repo, a user may have sessions on any of the project's
    # repos — all count toward the same project.
    if projects:
        project_ids = [p.id for p in projects]
        count_stmt = (
            select(
                Session.project_id,
                func.count(Session.id).label("cnt"),
            )
            .where(
                Session.user_id == user.id,
                Session.project_id.in_(project_ids),
            )
            .group_by(Session.project_id)
        )
        count_result = await db.execute(count_stmt)
        session_counts: dict[str, int] = {
            row.project_id: row.cnt for row in count_result
        }
    else:
        session_counts = {}

    return [
        ProjectResponse(
            id=p.id,
            name=p.name,
            git_remote_normalized=p.git_remote_normalized,
            context_document=p.context_document,
            owner_id=p.owner_id,
            created_at=p.created_at,
            updated_at=p.updated_at,
            session_count=session_counts.get(p.id, 0),
            auto_narrative=getattr(p, "auto_narrative", False),
            kb_retention_days=getattr(p, "kb_retention_days", 180),
            kb_max_context_words=getattr(p, "kb_max_context_words", 8000),
            kb_section_page_limit=getattr(p, "kb_section_page_limit", 30),
        )
        for p in projects
    ]


logger = logging.getLogger("sessionfs.api")


async def _backfill_knowledge_for_project(
    project_id: str, git_remote: str, user_id: str, blob_store: object,
) -> None:
    """Backfill knowledge entries from already-synced sessions for a new project.

    Reads blob archives to run the real summarizer, producing actual
    files_modified, key_decisions, errors, and packages for extraction.
    """
    import io
    import json
    import tarfile

    from sessionfs.server.db.engine import get_db as _get_db_gen
    from sessionfs.server.services.knowledge import extract_knowledge_entries
    from sessionfs.server.services.summarizer import summarize_session

    try:
        async for db in _get_db_gen():
            result = await db.execute(
                select(Session).where(
                    Session.git_remote_normalized == git_remote,
                    Session.is_deleted == False,  # noqa: E712
                ).order_by(Session.created_at.desc()).limit(20)
            )
            sessions = result.scalars().all()
            if not sessions:
                return

            extracted = 0
            for session in sessions:
                try:
                    if not session.blob_key:
                        continue
                    data = await blob_store.get(session.blob_key)
                    if not data:
                        continue

                    messages: list[dict] = []
                    manifest: dict = {}
                    workspace: dict = {}
                    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                        for member in tar.getmembers():
                            f = tar.extractfile(member)
                            if not f:
                                continue
                            content = f.read().decode("utf-8", errors="replace")
                            if member.name.endswith("messages.jsonl"):
                                for line in content.splitlines():
                                    line = line.strip()
                                    if line:
                                        messages.append(json.loads(line))
                            elif member.name.endswith("manifest.json"):
                                manifest = json.loads(content)
                            elif member.name.endswith("workspace.json"):
                                workspace = json.loads(content)

                    if not messages:
                        continue

                    summary = summarize_session(messages, manifest, workspace)
                    entries = await extract_knowledge_entries(
                        session.id, summary, project_id, session.user_id, db,
                    )
                    if entries:
                        extracted += len(entries)
                except Exception:
                    continue  # Best-effort per session

            if extracted:
                logger.info(
                    "Backfilled %d knowledge entries from %d sessions for project %s",
                    extracted, len(sessions), project_id,
                )
    except Exception:
        logger.warning("Knowledge backfill failed for project %s", project_id, exc_info=True)


@router.post("/", response_model=ProjectResponse, status_code=201)
async def create_project(
    body: CreateProjectRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    user: User = Depends(get_current_user),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """Create a project context for a repository."""
    check_feature(ctx, "project_context")
    # Check for existing project — both the legacy projects table column
    # AND the project_repos join table (multi-repo P2 A4).
    stmt = select(Project).where(Project.git_remote_normalized == body.git_remote_normalized)
    result = await db.execute(stmt)
    existing = result.scalar_one_or_none()
    if existing:
        raise HTTPException(409, "Project already exists for this repository")
    from sessionfs.server.db.models import ProjectRepo
    repo_check = await db.execute(
        select(ProjectRepo.id).where(
            ProjectRepo.git_remote_normalized == body.git_remote_normalized,
        ).limit(1)
    )
    if repo_check.scalar_one_or_none() is not None:
        raise HTTPException(409, "Project already exists for this repository")

    # v0.10.0 Phase 5 — validate org_id if provided. Caller must be a
    # member of the target org. Server is load-bearing here; CLI/dashboard
    # may pre-filter but cannot be trusted.
    org_general: dict = {}
    if body.org_id is not None:
        from sessionfs.server.db.models import OrgMember, Organization
        membership = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.user_id == user.id,
                    OrgMember.org_id == body.org_id,
                )
            )
        ).scalar_one_or_none()
        if membership is None:
            raise HTTPException(
                403,
                "You are not a member of the requested org",
            )

        # v0.10.0 Phase 6 Round 2 (KB entry 296) — pragmatic
        # inheritance: seed the new project's kb_* defaults from the
        # org's general settings at creation time. The compile/KB
        # runtime reads project columns directly and the columns are
        # NOT NULL, so "live inheritance" would require a schema
        # rewrite (nullable columns + effective-settings resolver at
        # every read site). Copying at creation gives org admins
        # control over the defaults their teammates start with while
        # keeping per-project overrides explicit and discoverable.
        org_row = (
            await db.execute(
                select(Organization).where(Organization.id == body.org_id)
            )
        ).scalar_one_or_none()
        if org_row is not None:
            try:
                settings_obj = (
                    json.loads(org_row.settings)
                    if isinstance(org_row.settings, str)
                    else (org_row.settings or {})
                )
            except (ValueError, TypeError):
                settings_obj = {}
            if isinstance(settings_obj, dict):
                general = settings_obj.get("general", {})
                if isinstance(general, dict):
                    org_general = general

    project_kwargs: dict = {
        "id": f"proj_{uuid.uuid4().hex[:16]}",
        "name": body.name,
        "git_remote_normalized": body.git_remote_normalized,
        "context_document": DEFAULT_TEMPLATE,
        "owner_id": user.id,
        "org_id": body.org_id,
    }
    # Apply org-default seeds only when the org has a non-null value
    # for each field. Server's hardcoded column defaults still apply
    # when the org didn't set a value, matching pre-Phase-6 behavior.
    for col in ("kb_retention_days", "kb_max_context_words", "kb_section_page_limit"):
        val = org_general.get(col)
        if isinstance(val, int):
            project_kwargs[col] = val

    project = Project(**project_kwargs)
    db.add(project)
    await db.commit()
    await db.refresh(project)

    # Backfill knowledge from sessions already synced before this project was created
    blob_store = getattr(request.app.state, "blob_store", None)
    if blob_store:
        background_tasks.add_task(
            _backfill_knowledge_for_project,
            project.id,
            body.git_remote_normalized,
            user.id,
            blob_store,
        )

    return ProjectResponse(
        id=project.id,
        name=project.name,
        git_remote_normalized=project.git_remote_normalized,
        context_document=project.context_document,
        owner_id=project.owner_id,
        created_at=project.created_at,
        updated_at=project.updated_at,
        auto_narrative=getattr(project, "auto_narrative", False),
        kb_retention_days=getattr(project, "kb_retention_days", 180),
        kb_max_context_words=getattr(project, "kb_max_context_words", 8000),
        kb_section_page_limit=getattr(project, "kb_section_page_limit", 30),
    )


@router.get("/{git_remote_normalized:path}", response_model=ProjectResponse)
async def get_project(
    git_remote_normalized: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """Get a project context by git remote.

    User must be the project owner, a member of the project's org
    (v0.10.22 — tk_7a457574c5624e12), or have captured at least one
    session on this git remote.

    Multi-repo aware (P2): resolves through project_repos join table.
    Tombstone-aware (F4): merged projects return 410 with merged_into
    target, gated by access check on the source first.  Unauthorized
    callers get opaque 404 — the target id is never leaked.
    """
    from sessionfs.server.auth.project_access import user_can_access_project
    from sessionfs.server.services.project_resolver import (
        ProjectResolutionLoopError,
        resolve_project_by_id,
        resolve_project_by_remote,
    )

    # Resolve without following tombstones first — we need to detect
    # the tombstone so we can run F4's access-on-source-before-disclose.
    try:
        project = await resolve_project_by_remote(
            db, git_remote_normalized, follow_tombstone=False,
        )
    except ProjectResolutionLoopError:
        raise HTTPException(409, {
            "error": "resolution_loop",
            "message": "Project resolution exceeded hop limit — possible data corruption.",
        })

    if not project:
        raise HTTPException(404, "No project context found")

    # F4 tombstone: source access check before disclosing merged_into.
    if project.merged_into_project_id:
        if not await user_can_access_project(db, user.id, project):
            # Unauthorized → opaque 404 (do not leak tombstone existence).
            raise HTTPException(404, "No project context found")
        # F5: re-authorize on the resolved/redirected target.
        target = await resolve_project_by_id(
            db, project.merged_into_project_id,
        )
        if target is None:
            raise HTTPException(404, "No project context found")
        if not await user_can_access_project(db, user.id, target):
            raise HTTPException(403, "No access to this project")
        raise HTTPException(
            status_code=410,
            detail={
                "error": "project_merged",
                "merged_into": target.id,
                "message": (
                    f"This project was merged into "
                    f"{target.name or target.id[:12]}."
                ),
            },
        )

    # Not a tombstone — run access check on the resolved project (F5:
    # resolver resolves, we authorize).
    if not await user_can_access_project(db, user.id, project):
        raise HTTPException(403, "No access to this project")

    return ProjectResponse(
        id=project.id,
        name=project.name,
        git_remote_normalized=project.git_remote_normalized,
        context_document=project.context_document,
        owner_id=project.owner_id,
        created_at=project.created_at,
        updated_at=project.updated_at,
        auto_narrative=getattr(project, "auto_narrative", False),
        kb_retention_days=getattr(project, "kb_retention_days", 180),
        kb_max_context_words=getattr(project, "kb_max_context_words", 8000),
        kb_section_page_limit=getattr(project, "kb_section_page_limit", 30),
    )


@router.put("/{git_remote_normalized:path}/context")
async def update_project_context(
    git_remote_normalized: str,
    body: UpdateContextRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Update the project context document.

    Multi-repo aware (P2): resolves through project_repos join table.
    F5: access check runs against the resolved project (resolver
    resolves, we authorize).
    """
    from sessionfs.server.auth.project_access import user_can_access_project
    from sessionfs.server.services.project_resolver import (
        ProjectResolutionLoopError,
        resolve_project_by_remote,
    )

    try:
        project = await resolve_project_by_remote(db, git_remote_normalized)
    except ProjectResolutionLoopError:
        raise HTTPException(409, {
            "error": "resolution_loop",
            "message": "Project resolution exceeded hop limit — possible data corruption.",
        })
    if not project:
        raise HTTPException(404, "No project context found")

    if not await user_can_access_project(db, user.id, project):
        raise HTTPException(403, "No access to this project")

    project.context_document = body.context_document
    project.updated_at = datetime.now(timezone.utc)
    await db.commit()

    return {"status": "updated", "size": len(body.context_document)}


@router.delete("/{project_id}")
async def delete_project(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete a project context. Only the owner or an admin can delete."""
    stmt = select(Project).where(Project.id == project_id)
    result = await db.execute(stmt)
    project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(404, "Project not found")

    if project.owner_id != user.id and user.tier != "admin":
        raise HTTPException(403, "Only the project owner or an admin can delete this project")

    await db.delete(project)
    await db.commit()

    return {"status": "deleted", "id": project_id}
