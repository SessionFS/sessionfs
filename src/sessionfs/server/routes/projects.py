"""Project context CRUD routes."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone

import logging

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import AdminAction, Project, ProjectRepo, Session, User
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


class UpdateProjectRequest(BaseModel):
    """Rename a project (tk_9b5fd8c3e2604254).

    Only `name` is settable — the Project model has no display_name or
    slug column (git_remote_normalized is the stable identifier). The
    validator mirrors session rename (sessions.SessionMetadataUpdate):
    reject null bytes, strip HTML tags, trim, reject empty/whitespace,
    cap at 255 chars (the column width).
    """

    name: str

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if "\x00" in v:
            raise ValueError("Null bytes not allowed in name")
        if len(v) > 255:
            raise ValueError("Name must be 255 characters or fewer")
        v = re.sub(r"<[^>]*>", "", v).strip()
        if not v:
            raise ValueError("Name cannot be empty")
        return v


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
    # P3: populated on detail endpoints only (omitted on list to avoid N+1).
    repos: list[ProjectRepoResponse] | None = None


class LinkRepoRequest(BaseModel):
    """Request to link a repo to a project.

    provider_repo_id is deliberately NOT a field — it is server-derived
    from the GitHub App installation response. Caller-supplied values
    are ignored (Sentinel F2).
    """

    git_remote: str
    is_primary: bool = False


class ProjectRepoResponse(BaseModel):
    id: str
    project_id: str
    git_remote_normalized: str
    provider: str | None = None
    provider_repo_id: str | None = None
    is_primary: bool = False
    verified: bool = False
    verification_method: str | None = None
    added_by_user_id: str | None = None
    created_at: datetime | None = None


class MergeRequest(BaseModel):
    """Request to merge one project into another (§5.2).

    dry_run defaults to True — performs full validation and returns
    a merge plan without writing anything. Set to False to execute.
    persona_policy controls collision handling: 'rename' (default),
    'skip', or 'merge_content'.
    """

    source_project_id: str
    dry_run: bool = True
    persona_policy: str = "rename"


class MergeResponse(BaseModel):
    dry_run: bool
    stats: dict
    persona_collisions: list[dict] | None = None
    slug_collisions: list[str] | None = None
    ke_duplicates: list[dict] | None = None
    audit_id: str | None = None
    persona_renames: list[dict] | None = None
    slug_renames: list[dict] | None = None
    skipped_ke_ids: list[str] | None = None
    skipped_link_ids: list[str] | None = None
    rules_action: str | None = None


async def _check_repo_access(db: AsyncSession, user_id: str, git_remote: str) -> bool:
    """Check if user has sessions in this repo (grants read/write access).

    P2 (§3.3 B6): resolve the project for this remote first, then
    check by project_id OR remote-match through project_repos.
    After multi-repo, a user with sessions on non-primary repos or
    legacy sessions with NULL project_id should still have access.
    """
    from sqlalchemy import or_

    from sessionfs.server.services.project_resolver import (
        resolve_project_by_remote,
    )
    project = await resolve_project_by_remote(db, git_remote)
    if project is None:
        return False
    stmt = (
        select(Session.id)
        .where(
            Session.user_id == user_id,
            or_(
                Session.project_id == project.id,
                Session.git_remote_normalized == project.git_remote_normalized,
                Session.git_remote_normalized.in_(
                    select(ProjectRepo.git_remote_normalized).where(
                        ProjectRepo.project_id == project.id,
                    )
                ),
            ),
        )
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

    from sessionfs.server.db.models import OrgMember, ProjectRepo

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

    # P2 (§3.3 B5): count sessions by project_id, primary remote,
    # AND through project_repos for legacy sessions with NULL
    # project_id.  After multi-repo, a user may have sessions on
    # any of the project's repos — all count toward the same project.
    if projects:
        project_ids = [p.id for p in projects]
        # Explicit project_id matches.
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
        # Build remote→project_id mapping for legacy session counting.
        # Primary remotes (from Project) + linked remotes (from ProjectRepo).
        remote_to_pids: dict[str, set[str]] = {}
        for p in projects:
            remote_to_pids.setdefault(p.git_remote_normalized, set()).add(p.id)
        repos_stmt = select(
            ProjectRepo.project_id,
            ProjectRepo.git_remote_normalized,
        ).where(ProjectRepo.project_id.in_(project_ids))
        repos_result = await db.execute(repos_stmt)
        for row in repos_result:
            remote_to_pids.setdefault(row.git_remote_normalized, set()).add(
                row.project_id
            )
        if remote_to_pids:
            legacy_count_stmt = (
                select(
                    Session.git_remote_normalized,
                    func.count(Session.id).label("cnt"),
                )
                .where(
                    Session.user_id == user.id,
                    Session.project_id.is_(None),
                    Session.git_remote_normalized.in_(remote_to_pids.keys()),
                )
                .group_by(Session.git_remote_normalized)
            )
            legacy_result = await db.execute(legacy_count_stmt)
            for row in legacy_result:
                for pid in remote_to_pids.get(row.git_remote_normalized, set()):
                    session_counts[pid] = (
                        session_counts.get(pid, 0) + row.cnt
                    )
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


# ── app-layer rate limiters (F6) ─────────────────────────────────────
# Per-user sliding-window caps on repo link/unlink/merge.
# Durable multi-replica / edge limiting is the Forge follow-up
# (ticket tk_d2646c1a38174eec) — these are the best-effort in-process
# guards.  Single-process deployments (local dev, single-replica
# Cloud Run) get full protection; multi-replica deployments need the
# durable layer.
_link_unlink_limiter = SlidingWindowRateLimiter(max_requests=30, window_seconds=60)
_merge_limiter = SlidingWindowRateLimiter(max_requests=10, window_seconds=60)


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


# ── P3: Multi-Repo Link / Unlink / List ──────────────────────────────────
# These MUST be registered BEFORE the greedy /{git_remote_normalized:path}
# route below, otherwise FastAPI matches /{project_id}/repos as a remote.


def _repo_to_response(repo: ProjectRepo) -> ProjectRepoResponse:
    """Map a ProjectRepo ORM row to the response schema."""
    return ProjectRepoResponse(
        id=repo.id,
        project_id=repo.project_id,
        git_remote_normalized=repo.git_remote_normalized,
        provider=repo.provider,
        provider_repo_id=repo.provider_repo_id,
        is_primary=repo.is_primary if repo.is_primary else False,
        verified=repo.verified if repo.verified else False,
        verification_method=repo.verification_method,
        added_by_user_id=repo.added_by_user_id,
        created_at=repo.created_at,
    )


async def _repos_for_project(
    db: AsyncSession, project_id: str,
) -> list[ProjectRepoResponse]:
    """Fetch all repo rows for a project, primary first."""
    result = await db.execute(
        select(ProjectRepo)
        .where(ProjectRepo.project_id == project_id)
        .order_by(ProjectRepo.is_primary.desc(), ProjectRepo.created_at.asc())
    )
    return [_repo_to_response(r) for r in result.scalars().all()]


@router.get("/{project_id}/repos", response_model=list[ProjectRepoResponse])
async def list_project_repos(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[ProjectRepoResponse]:
    """List all repos linked to a project. Access-gated."""
    from sessionfs.server.auth.project_access import (
        load_project_for_user,
    )

    await load_project_for_user(project_id, db, user.id)
    return await _repos_for_project(db, project_id)


@router.post(
    "/{project_id}/repos",
    response_model=ProjectRepoResponse,
    status_code=201,
)
async def link_repo(
    project_id: str,
    body: LinkRepoRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectRepoResponse:
    """Link a git repo to a project.

    Claims a globally-unique git_remote_normalized. Requires project
    admin standing (user_is_project_admin) AND repo-ownership
    verification (Sentinel F1). Verified rows can displace unverified
    holders atomically (§6.2). No tier gate — free for all tiers (Q3).
    """
    from sqlalchemy import func, update

    from sessionfs.server.auth.project_access import (
        user_can_access_project,
        user_is_project_admin,
    )
    from sessionfs.server.github_app import (
        normalize_git_remote,
        verify_repo_ownership,
    )

    # 0. Rate limit check (F6).
    if not _link_unlink_limiter.is_allowed(f"link:{user.id}"):
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limit",
                "message": "Too many link requests. Please wait and try again.",
            },
        )

    # 1. Load + authz the target project.
    project = (await db.execute(
        select(Project).where(Project.id == project_id)
    )).scalar_one_or_none()
    if project is None:
        raise HTTPException(404, "Project not found")
    if not await user_is_project_admin(db, user.id, project):
        raise HTTPException(403, "Only a project admin can link repos")

    # 2. Normalize the remote.
    normalized = normalize_git_remote(body.git_remote)
    if not normalized:
        raise HTTPException(400, "Could not parse git remote URL")

    # 2b. Repo-reclaimed revival: a project with zero repos (or
    #     repo_reclaimed_at set from a prior displacement) gets its
    #     first new link as the forced primary, clearing the orphaned
    #     state and repopulating the project's primary remote.
    repo_count = (await db.execute(
        select(func.count(ProjectRepo.id)).where(
            ProjectRepo.project_id == project_id,
        )
    )).scalar() or 0
    if repo_count == 0 or project.repo_reclaimed_at is not None:
        body.is_primary = True
        project.repo_reclaimed_at = None
        project.git_remote_normalized = normalized

    # 3. GitHub ownership verification (N2: OUTSIDE the swap transaction).
    #    MUST run BEFORE the locked holder check so the displacement
    #    rules can distinguish verified-vs-verified from
    #    verified-vs-unverified.
    #    Extract owner/repo: for bare format github.com/owner/repo (3 parts)
    #    the last two are owner+repo; for owner/repo (2 parts) use as-is.
    parts = normalized.split("/")
    verified: bool = False
    verification_method: str | None = "owner_attested"
    provider: str | None = None
    provider_repo_id: str | None = None

    owner: str | None = None
    repo_name: str | None = None
    if len(parts) == 2:
        owner, repo_name = parts[0], parts[1]
    elif len(parts) == 3:
        owner, repo_name = parts[1], parts[2]

    if owner and repo_name:
        verified, verification_method, provider, provider_repo_id = (
            await verify_repo_ownership(db, user.id, owner, repo_name)
        )

    # 4. Holder check with FOR UPDATE (N2: no live HTTP calls inside
    #    the lock — verification was done in step 3).  Applies F3
    #    access-gating and the displacement rules.
    holder = (await db.execute(
        select(ProjectRepo).where(
            ProjectRepo.git_remote_normalized == normalized,
        ).with_for_update()
    )).scalar_one_or_none()

    if holder is not None:
        # F3 access-gating for 409: only disclose existing_project_id
        # to callers who can access the holding project.
        holding_project = (await db.execute(
            select(Project).where(Project.id == holder.project_id)
        )).scalar_one_or_none()
        caller_can_see_holder = (
            holding_project is not None
            and await user_can_access_project(db, user.id, holding_project)
        )

        if holder.verified:
            # verified-vs-verified → 409 genuine conflict.
            # Apply F3 gating on the existing_project_id disclosure.
            if caller_can_see_holder:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "repo_already_linked",
                        "message": (
                            "This repo is linked to a verified project. "
                            "Both sides have proven ownership — manual "
                            "resolution required."
                        ),
                        "existing_project_id": holder.project_id,
                    },
                )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "repo_already_linked",
                    "message": (
                        "This repo is linked to a verified project. "
                        "Both sides have proven ownership — manual "
                        "resolution required."
                    ),
                },
            )
        elif not verified:
            # unverified-vs-any → 409 (unverified can't displace).
            # Apply F3 gating on the existing_project_id disclosure.
            if caller_can_see_holder:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "repo_already_linked",
                        "message": (
                            "This repo is already linked to another "
                            "project. Unlink it from that project first, "
                            "or merge the projects."
                        ),
                        "existing_project_id": holder.project_id,
                    },
                )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "repo_already_linked",
                    "message": "This repo is already linked to another project.",
                },
            )
        else:
            # verified beats unverified → DISPLACE (§6.2)
            holder_project_id = holder.project_id
            holder_was_primary = holder.is_primary

            # Cross-org boundary (LOW-4 carve-out: verified reclaim
            # displaces cross-org unverified squatter — final state in
            # the verified owner's org only).
            if (
                holding_project
                and project.org_id is not None
                and holding_project.org_id is not None
                and project.org_id != holding_project.org_id
            ):
                # Verified reclaim across orgs — permitted.
                # The holder loses the repo; final state is in claimant's org.
                pass

            # Atomic displacement: DELETE the unverified row.
            await db.delete(holder)
            await db.flush()  # free the UNIQUE slot before insert

            # Handle holder project state.
            remaining_count = (await db.execute(
                select(func.count(ProjectRepo.id)).where(
                    ProjectRepo.project_id == holder_project_id,
                )
            )).scalar() or 0

            if remaining_count == 0:
                # Holder has NO repos left → repo_reclaimed orphaned state.
                # Data stays with the holder (NEVER auto-imported).
                await db.execute(
                    update(Project)
                    .where(Project.id == holder_project_id)
                    .values(repo_reclaimed_at=func.now())
                )
            elif holder_was_primary:
                # Holder has other repos — promote oldest to primary.
                oldest = (await db.execute(
                    select(ProjectRepo)
                    .where(ProjectRepo.project_id == holder_project_id)
                    .order_by(ProjectRepo.created_at.asc())
                    .limit(1)
                )).scalar_one()
                oldest.is_primary = True
                # Refresh project.git_remote_normalized from new primary.
                await db.execute(
                    update(Project)
                    .where(Project.id == holder_project_id)
                    .values(git_remote_normalized=oldest.git_remote_normalized)
                )

            # Durable displacement audit (Shield L1 + L4).
            # Records the ownership-reassignment event with plain-string
            # actor snapshot (user_id + email) that survives user deletion.
            displacement_audit = AdminAction(
                id=f"adm_{uuid.uuid4().hex[:16]}",
                admin_id=user.id,
                action="repo_displaced",
                target_type="project_repo",
                target_id=holder_project_id,
                details=json.dumps({
                    "displaced_remote": normalized,
                    "claimant_project_id": project_id,
                    "claimant_user_id": user.id,
                    "claimant_user_email": user.email,
                    "old_verified": holder.verified,
                    "old_verification_method": holder.verification_method,
                    "old_is_primary": holder_was_primary,
                    "cross_org": bool(
                        holding_project
                        and project.org_id is not None
                        and holding_project.org_id is not None
                        and project.org_id != holding_project.org_id
                    ),
                    # L4: plain-string actor snapshot survives user deletion.
                    "actor_user_id": user.id,
                    "actor_email": user.email,
                }),
            )
            db.add(displacement_audit)

    # 6. Handle is_primary: DEMOTE the existing primary (keep the row),
    #    do NOT unlink it (tk_b3fc4a81446544ff). The previous primary
    #    stays linked and remains resolvable by its own remote — only
    #    its is_primary flag flips to false. The demote is a Core UPDATE
    #    (executes immediately against the connection) and the new row is
    #    inserted via the unit-of-work at commit, so the demote always
    #    lands first; the explicit flush() is a defensive barrier that
    #    keeps the partial-unique index uq_project_repos_primary (one
    #    primary per project) from ever observing two primaries even if
    #    the demote is later refactored into an ORM mutation.
    if body.is_primary:
        await db.execute(
            update(ProjectRepo)
            .where(
                ProjectRepo.project_id == project_id,
                ProjectRepo.is_primary == True,  # noqa: E712
            )
            .values(is_primary=False)
        )
        await db.flush()  # land the demote before the new-primary insert
        # Also update the project's git_remote_normalized.
        project.git_remote_normalized = normalized

    # 7. Insert the new repo row.
    new_repo = ProjectRepo(
        id=f"repo_{uuid.uuid4().hex[:16]}",
        project_id=project_id,
        git_remote_normalized=normalized,
        provider=provider,
        provider_repo_id=provider_repo_id,
        is_primary=body.is_primary,
        verified=verified,
        verification_method=verification_method,
        added_by_user_id=user.id,
    )
    db.add(new_repo)
    await db.commit()
    await db.refresh(new_repo)

    return _repo_to_response(new_repo)


@router.delete("/{project_id}/repos/{repo_id}")
async def unlink_repo(
    project_id: str,
    repo_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Unlink a repo from a project.

    Refuses if this would leave an active project with zero repos (422).
    If unlinking the primary, auto-promotes the oldest remaining repo.
    """
    from sqlalchemy import func, update

    from sessionfs.server.auth.project_access import (
        user_is_project_admin,
    )

    # 0. Rate limit check (F6).
    if not _link_unlink_limiter.is_allowed(f"unlink:{user.id}"):
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limit",
                "message": "Too many unlink requests. Please wait and try again.",
            },
        )

    # 1. Load + authz the target project.
    project = (await db.execute(
        select(Project).where(Project.id == project_id)
    )).scalar_one_or_none()
    if project is None:
        raise HTTPException(404, "Project not found")
    if not await user_is_project_admin(db, user.id, project):
        raise HTTPException(403, "Only a project admin can unlink repos")

    # 2. Find the repo row.
    repo = (await db.execute(
        select(ProjectRepo).where(
            ProjectRepo.id == repo_id,
            ProjectRepo.project_id == project_id,
        )
    )).scalar_one_or_none()
    if repo is None:
        raise HTTPException(404, "Repo not found on this project")

    # 3. Last-repo guard (Q4): active projects must have ≥1 repo.
    #    merged and repo_reclaimed states are exempt.
    if (
        project.merged_into_project_id is None
        and project.repo_reclaimed_at is None
    ):
        repo_count = (await db.execute(
            select(func.count(ProjectRepo.id)).where(
                ProjectRepo.project_id == project_id,
            )
        )).scalar() or 0
        if repo_count <= 1:
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "last_repo",
                    "message": (
                        "Cannot unlink the last repo of an active project. "
                        "Delete the project instead, or merge it into "
                        "another project."
                    ),
                },
            )

    # 4. If unlinking the primary, auto-promote the oldest remaining.
    was_primary = repo.is_primary
    await db.delete(repo)
    await db.flush()

    if was_primary:
        new_primary = (await db.execute(
            select(ProjectRepo)
            .where(ProjectRepo.project_id == project_id)
            .order_by(ProjectRepo.created_at.asc())
            .limit(1)
        )).scalar_one_or_none()
        if new_primary:
            new_primary.is_primary = True
            await db.execute(
                update(Project)
                .where(Project.id == project_id)
                .values(git_remote_normalized=new_primary.git_remote_normalized)
            )

    await db.commit()
    return {"status": "unlinked", "repo_id": repo_id}


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
        repos=await _repos_for_project(db, project.id),
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


@router.post("/{project_id}/merge", response_model=MergeResponse)
async def merge_project(
    project_id: str,
    body: MergeRequest,
    request: Request = None,  # noqa: ARG001
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> MergeResponse:
    """Merge another project into this one (§5).

    Atomic, dry-run-first, audit-logged.  Dry-run (default) validates
    and returns a merge plan with ZERO database writes.  Execute
    (dry_run=False) runs the full merge within a single atomic
    transaction — any failure rolls back completely and both projects
    are untouched.

    Authz (§6.4): caller must own BOTH projects or be org-admin of
    both projects' org.  Same-org only (or both personal).  Cross-org /
    personal-mix is denied (400).  Neither project may already be merged.
    Pending project transfers block the merge (400).
    """
    from sessionfs.server.auth.project_access import user_is_project_admin
    from sessionfs.server.db.engine import _session_factory
    from sessionfs.server.services.merge import merge_projects
    from sessionfs.server.services.project_resolver import (
        ProjectResolutionLoopError,
    )

    target_id = project_id
    source_id = body.source_project_id

    if target_id == source_id:
        raise HTTPException(400, "Cannot merge a project into itself")

    # Validate persona_policy.
    if body.persona_policy not in ("rename", "skip", "merge_content"):
        raise HTTPException(
            400,
            f"Invalid persona_policy: {body.persona_policy}. "
            "Must be 'rename', 'skip', or 'merge_content'.",
        )

    # 0. Rate limit check (F6).
    if not _merge_limiter.is_allowed(f"merge:{user.id}"):
        raise HTTPException(
            status_code=429,
            detail={
                "code": "rate_limit",
                "message": "Too many merge requests. Please wait and try again.",
            },
        )

    # Authz: caller must own BOTH or be org-admin of BOTH.
    source = await db.get(Project, source_id)
    target = await db.get(Project, target_id)
    if not source:
        raise HTTPException(404, "Source project not found")
    if not target:
        raise HTTPException(404, "Target project not found")

    source_admin = await user_is_project_admin(db, user.id, source)
    target_admin = await user_is_project_admin(db, user.id, target)
    if not source_admin:
        raise HTTPException(403, "Not authorized on source project")
    if not target_admin:
        raise HTTPException(403, "Not authorized on target project")

    # Cross-org denial audit (Shield L1): record the rejection before
    # merge_projects does, so the denial is durable even though the
    # merge itself never runs.  _validate_preconditions also blocks
    # cross-org as defense-in-depth.
    #
    # Write through a FRESH short-lived session that COMMITS before the
    # raise, so the denial audit survives the failed request's rollback
    # (mirrors the merge-audit separate-session pattern in merge.py).
    if source.org_id != target.org_id:
        denial_audit = AdminAction(
            id=f"adm_{uuid.uuid4().hex[:16]}",
            admin_id=user.id,
            action="merge_denied_cross_org",
            target_type="project",
            target_id=source_id,
            details=json.dumps({
                "target_project_id": target_id,
                "source_org_id": source.org_id,
                "target_org_id": target.org_id,
                "reason": "Cross-org merges are not supported",
                # L4: plain-string actor snapshot survives user deletion.
                "actor_user_id": user.id,
                "actor_email": user.email,
            }),
        )
        if _session_factory is not None:
            async with _session_factory() as audit_db:
                async with audit_db.begin():
                    audit_db.add(denial_audit)
        raise HTTPException(
            400,
            "Cross-org merges are not supported. "
            "Use project transfer to move one project into the other's org first.",
        )

    if _session_factory is None:
        raise HTTPException(500, "Database not initialized")

    try:
        result = await merge_projects(
            db=db,
            source_id=source_id,
            target_id=target_id,
            user_id=user.id,
            dry_run=body.dry_run,
            persona_policy=body.persona_policy,
            session_factory=_session_factory,
        )
    except ProjectResolutionLoopError:
        raise HTTPException(409, {
            "error": "resolution_loop",
            "message": "Project resolution exceeded hop limit — possible data corruption.",
        })

    return MergeResponse(**result)


@router.patch("/{project_id}", response_model=ProjectResponse)
async def update_project(
    project_id: str,
    body: UpdateProjectRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """Rename a project (tk_9b5fd8c3e2604254).

    Project-admin gated (owner or org-admin of the project's org via
    user_is_project_admin). Only `name` is settable — the model has no
    display_name/slug. Validation mirrors session rename: empty/
    whitespace → 422, null bytes rejected, HTML stripped, 255-char cap.

    Returns the updated ProjectResponse (with repos) so callers —
    `sfs project show`, the dashboard — reflect the new name without a
    second fetch. Distinct from session rename (PATCH /sessions/{id}).
    """
    from sessionfs.server.auth.project_access import user_is_project_admin

    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(404, "Project not found")

    if not await user_is_project_admin(db, user.id, project):
        raise HTTPException(403, "Only a project admin can rename this project")

    project.name = body.name
    project.updated_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(project)

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
        repos=await _repos_for_project(db, project.id),
    )


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
