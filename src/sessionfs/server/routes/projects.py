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
from sessionfs.server.db.models import AdminAction, OrgMember, Project, ProjectRepo, Session, User
from sessionfs.server.tier_gate import (
    UserContext,
    check_feature,
    get_user_context,
    raise_project_upgrade_required,
    resolve_org_plan_tier,
    resolve_project_plan_tier,
    resolve_project_plan_tiers,
)
from sessionfs.server.tiers import get_features_for_tier

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])

# Pre-migration-073 reclaimed projects were marked by rewriting their
# legacy remote to "reclaimed:<project-id>". That namespace was never
# reserved, so an attacker could squat any project's future placeholder
# (tk_b6c5e184ba8d4aa1). Reclaimed is now STRUCTURAL — a NULL legacy
# remote plus repo_reclaimed_at — and this prefix is rejected on every
# caller-controlled remote input so the old strings can never be
# reintroduced.
RECLAIMED_REMOTE_PREFIX = "reclaimed:"

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

    @field_validator("git_remote_normalized")
    @classmethod
    def validate_remote(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("git_remote_normalized cannot be empty")
        if v.lower().startswith(RECLAIMED_REMOTE_PREFIX):
            # Reserved namespace (structural reclaimed state lives in
            # repo_reclaimed_at, not in this column).
            raise ValueError("git_remote_normalized uses a reserved prefix")
        # Older clients may send a raw URL form; store the canonical value so
        # the same repo can't become two projects (GitHub #88).
        from sessionfs.git_remote import normalize_git_remote

        normalized = normalize_git_remote(v)
        if not normalized:
            raise ValueError("git_remote_normalized cannot be empty")
        return normalized


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
    compile_schedule: str = "nightly"
    compile_after_claims: int | None = None
    last_scheduled_compile_at: datetime | None = None
    # P3: populated on detail endpoints only (omitted on list to avoid N+1).
    repos: list[ProjectRepoResponse] | None = None
    # Empty string for a repo-reclaimed project (its column is NULL);
    # repo_reclaimed distinguishes that state from a real remote.
    repo_reclaimed: bool = False
    # The plan tier that pays for this project (resolve_project_plan_tier).
    # Absent (None) only when the server predates this field — the dashboard
    # falls back to the caller's own tier in that case.
    effective_tier: str | None = None
    # Whether the CALLER may change this project (owner or a member of its
    # org — user_can_edit_project). A reader whose only access is a captured
    # session on the project's repo sees False: they can read and add
    # knowledge/tickets/comments but not change settings, rules, wiki pages
    # or curate the knowledge base. None only from servers that predate it.
    can_edit: bool | None = None


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
    would_refuse: bool | None = None
    blocking_reason: str | None = None


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
    predicates = [Session.project_id == project.id]
    # A project whose legacy column is NULL (repo-reclaimed, or nulled
    # by migration 073 after losing a conflict) must never add the
    # remote equality: `== None` renders IS NULL and would match every
    # session with a NULL remote.
    if project.git_remote_normalized is not None:
        predicates.append(
            Session.git_remote_normalized == project.git_remote_normalized
        )
    predicates.append(
        Session.git_remote_normalized.in_(
            select(ProjectRepo.git_remote_normalized).where(
                ProjectRepo.project_id == project.id,
            )
        )
    )
    stmt = (
        select(Session.id)
        .where(
            Session.user_id == user_id,
            or_(*predicates),
        )
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none() is not None


def _project_response(
    project: Project,
    *,
    session_count: int = 0,
    repos: list[ProjectRepoResponse] | None = None,
    effective_tier: str | None = None,
    can_edit: bool | None = None,
) -> ProjectResponse:
    """Build a ProjectResponse, mapping the structural reclaimed state
    (NULL git_remote_normalized + repo_reclaimed_at) onto the wire
    shape: empty remote string + repo_reclaimed flag."""
    return ProjectResponse(
        id=project.id,
        name=project.name,
        git_remote_normalized=project.git_remote_normalized or "",
        context_document=project.context_document,
        owner_id=project.owner_id,
        created_at=project.created_at,
        updated_at=project.updated_at,
        session_count=session_count,
        auto_narrative=getattr(project, "auto_narrative", False),
        kb_retention_days=getattr(project, "kb_retention_days", 180),
        kb_max_context_words=getattr(project, "kb_max_context_words", 8000),
        kb_section_page_limit=getattr(project, "kb_section_page_limit", 30),
        compile_schedule=getattr(project, "compile_schedule", "nightly"),
        compile_after_claims=getattr(project, "compile_after_claims", None),
        last_scheduled_compile_at=getattr(project, "last_scheduled_compile_at", None),
        repos=repos,
        repo_reclaimed=project.repo_reclaimed_at is not None,
        effective_tier=effective_tier,
        can_edit=can_edit,
    )


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
    from sqlalchemy import func

    from sessionfs.server.auth.project_access import list_accessible_projects
    from sessionfs.server.db.models import ProjectRepo

    # Access predicate now lives in auth.project_access.list_accessible_projects
    # (shared with the session helper's list_projects tool so the two listings
    # can never drift): owner OR org member OR a session on the project's
    # primary/linked git remote.
    projects: list[Project] = await list_accessible_projects(db, user.id)

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
            # A repo-reclaimed project's legacy column is NULL — never a
            # session-remote key.
            if p.git_remote_normalized:
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

    # The project's own plan tier (not the caller's primary-org tier) gates
    # project-scoped Enterprise surfaces in the dashboard (tk_792504a140604e62).
    # Batch-resolve so a long list stays O(1) round-trips.
    plan_tiers = await resolve_project_plan_tiers(projects, db) if projects else {}

    # Editor standing per project (owner or org member) from one membership
    # query — same rule as user_can_edit_project.
    member_org_ids = set(
        (
            await db.execute(
                select(OrgMember.org_id).where(OrgMember.user_id == user.id)
            )
        ).scalars().all()
    )

    return [
        _project_response(
            p,
            session_count=session_counts.get(p.id, 0),
            effective_tier=plan_tiers[p.id].value if p.id in plan_tiers else None,
            can_edit=(
                p.owner_id == user.id
                or (p.org_id is not None and p.org_id in member_org_ids)
            ),
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
    """Create a project context for a repository.

    The new project is paid for by its owner: the requested org's plan,
    or the caller's own plan for a personal project (tk_a90e4015ad984cde).
    Membership is checked before the org's plan, so the plan of an org
    the caller is not in is never revealed.
    """
    # GH #70 — creation can race member removal / user deactivation, so take
    # the caller's user lock (+ the target org's lock) BEFORE checking
    # membership/plan/active-user state, then re-read the authorization
    # state under the locks (scope_locks.py global order: user then org).
    from sessionfs.server.services.scope_locks import lock_scopes

    await lock_scopes(
        db, user_ids=[user.id], org_ids=[body.org_id] if body.org_id else []
    )

    # Re-read the caller under the user lock: a concurrent delete may have
    # deactivated them while we waited. Never create for an inactive user.
    locked_user = (
        await db.execute(
            select(User)
            .where(User.id == user.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if locked_user is None or not locked_user.is_active:
        raise HTTPException(409, "Account is inactive or no longer exists")

    org_general: dict = {}
    # GitHub #93 review round 2 LOW: the compile schedule defaults to 'off'
    # and only flips to 'nightly' once the project's paying plan is known to
    # include project_context. The gates below already reject plans without
    # the feature, so 'off' is a defensive fallback that keeps the sweep from
    # ever compiling a project whose plan shouldn't.
    compile_schedule = "off"
    if body.org_id is not None:
        # Single consolidated membership check under the org lock (folds in
        # the former duplicate pre-insert check). Membership is checked
        # before the org's plan, so the plan of an org the caller is not in
        # is never revealed.
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
        plan = await resolve_org_plan_tier(body.org_id, db)
        if "project_context" not in get_features_for_tier(plan):
            raise_project_upgrade_required("project_context", plan, None)
        compile_schedule = "nightly"

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
    else:
        check_feature(ctx, "project_context")
        compile_schedule = "nightly"

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

    project_kwargs: dict = {
        "id": f"proj_{uuid.uuid4().hex[:16]}",
        "name": body.name,
        "git_remote_normalized": body.git_remote_normalized,
        "context_document": DEFAULT_TEMPLATE,
        "owner_id": user.id,
        "org_id": body.org_id,
        # New projects opt into the scheduled KB compile sweep; migration 089
        # backfills 'off' onto pre-existing rows so the sweep never surprises
        # an operator with compiles they did not opt in to.
        "compile_schedule": compile_schedule,
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
    # Every new project also carries its primary remote in project_repos,
    # atomically (tk_b6c5e184ba8d4aa1): a project that exists ONLY in the
    # legacy projects.git_remote_normalized column was an installation-
    # claim hijack path — a verified link of the same remote conflicted
    # with the legacy unique column instead of displacing the squatter.
    # The row is unverified ('legacy_backfill', matching the migration
    # 073 backfill): a verified owner can still displace it.
    db.add(ProjectRepo(
        id=f"repo_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        git_remote_normalized=body.git_remote_normalized,
        is_primary=True,
        verified=False,
        verification_method="legacy_backfill",
        added_by_user_id=user.id,
    ))
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

    effective_tier = (await resolve_project_plan_tier(project, db)).value
    return _project_response(project, effective_tier=effective_tier, can_edit=True)


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
    from sqlalchemy import case, func, update

    from sessionfs.server.auth.project_access import (
        user_can_access_project,
        user_is_project_admin,
    )
    from sessionfs.server.github_app import (
        github_owner_repo_from_remote,
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
    if body.git_remote.strip().lower().startswith(
        RECLAIMED_REMOTE_PREFIX
    ) or normalized.lower().startswith(RECLAIMED_REMOTE_PREFIX):
        # Reserved namespace: reclaimed state is structural
        # (repo_reclaimed_at), never a caller-supplied remote value. The
        # raw input is checked too: the normalizer reads "reclaimed:x" as an
        # scp-style remote and would strip the prefix.
        raise HTTPException(400, "git_remote uses a reserved prefix")

    # Nothing is written before verification: end this read transaction so
    # no connection or row lock is held across the GitHub calls below.
    await db.commit()

    # 3. GitHub ownership verification (N2: OUTSIDE the swap transaction).
    #    MUST run BEFORE the locked holder check so the displacement
    #    rules can distinguish verified-vs-verified from
    #    verified-vs-unverified.
    #    The normalized remote has no host, so owner/repo is taken from
    #    the RAW remote and only for a github.com remote with exactly two
    #    path segments: another host (GitLab, self-hosted) or a subgroup
    #    path (group/sub/repo) must never be checked against GitHub's
    #    sub/repo. Anything else stays owner_attested.
    verified: bool = False
    verification_method: str | None = "owner_attested"
    provider: str | None = None
    provider_repo_id: str | None = None

    github_ref = github_owner_repo_from_remote(body.git_remote)
    if github_ref is not None:
        owner, repo_name = github_ref
        verified, verification_method, provider, provider_repo_id = (
            await verify_repo_ownership(db, user.id, owner, repo_name)
        )

    # 3b. Re-check authorization against freshly LOCKED sources: standing
    #     may have changed while GitHub was being called. Lock order (same
    #     as membership changes): user -> org member -> project. FOR SHARE
    #     on the user and membership rows serializes this link with a
    #     concurrent deactivation / demotion / removal (which take FOR
    #     UPDATE): either they commit first and we see it, or they wait
    #     until this link has committed. The project rows themselves are
    #     locked below (4c) together with every holder project, in one
    #     deterministic id-ordered query.
    caller = (await db.execute(
        select(User.id, User.is_active)
        .where(User.id == user.id)
        .with_for_update(read=True)
    )).first()
    if caller is None or not caller.is_active:
        raise HTTPException(403, "Only a project admin can link repos")
    project_org_id = (await db.execute(
        select(Project.org_id).where(Project.id == project_id)
    )).scalar_one_or_none()
    if project_org_id is not None:
        await db.execute(
            select(OrgMember.user_id)
            .where(OrgMember.org_id == project_org_id, OrgMember.user_id == user.id)
            .with_for_update(read=True)
        )

    # 4. Holder PROBE, unlocked (N2: no live HTTP calls inside the lock —
    #    verification was done in step 3). Applies F3 access-gating and the
    #    displacement rules. The candidate rows are only identified here;
    #    they are locked by id in step 4c AFTER every affected project is
    #    locked in id order. Locking ProjectRepo before Project inverts the
    #    order unlink_repo / merge take (Project -> ProjectRepo) and would
    #    deadlock, so the probe must not take a row lock.
    remote_holder_probe = (await db.execute(
        select(ProjectRepo.id, ProjectRepo.project_id, ProjectRepo.verified).where(
            ProjectRepo.git_remote_normalized == normalized,
        )
    )).first()
    # A repository renamed on GitHub keeps its provider id: a row holding
    # the same (provider, provider_repo_id) under an OLD remote is the same
    # repository and goes through the same displacement rules, so a stale
    # holder can never block the real owner after a rename.
    id_holder_probe = None
    if provider and provider_repo_id:
        id_holder_probe = (await db.execute(
            select(ProjectRepo.id, ProjectRepo.project_id, ProjectRepo.verified).where(
                ProjectRepo.provider == provider,
                ProjectRepo.provider_repo_id == provider_repo_id,
                ProjectRepo.git_remote_normalized != normalized,
            )
        )).first()

    # 4b. Legacy holder: a project holding `normalized` ONLY in its
    #     legacy projects.git_remote_normalized column, with no
    #     project_repos row for it (pre-073 projects could exist in that
    #     state; the 073 backfill writes rows where it can). Such a
    #     squatter otherwise blocked the verified owner's link with a
    #     uniqueness conflict on the legacy column and stayed eligible
    #     for legacy access paths (tk_b6c5e184ba8d4aa1). It is resolved
    #     under the same rules as an unverified ProjectRepo holder.
    #     Merge tombstones (merged_into_project_id set) are excluded: they
    #     redirect to their target, and marking one repo_reclaimed would
    #     corrupt the merged-vs-reclaimed distinction migration 073 keeps
    #     separate. merge.py nulls the tombstone's legacy remote at merge
    #     time, so a tombstone no longer squats a remote at all.
    legacy_holder = (await db.execute(
        select(Project).where(
            Project.git_remote_normalized == normalized,
            Project.id != project_id,
            Project.merged_into_project_id.is_(None),
            ~select(ProjectRepo.id).where(
                ProjectRepo.project_id == Project.id,
                ProjectRepo.git_remote_normalized == normalized,
            ).exists(),
        )
    )).scalar_one_or_none()

    # 4c. Lock EVERY affected project (target + probed holders) FOR UPDATE
    #     in deterministic project-id order before any of its repos is
    #     deleted or counted (tk_b6c5e184ba8d4aa1). Locking the target and
    #     its holders in ONE ORDER BY Project.id query means two concurrent
    #     links acquire the same projects in the same order and cannot
    #     deadlock (one link's target is the other's holder). FOR UPDATE
    #     also conflicts with the FOR KEY SHARE a concurrent link takes
    #     when inserting a project_repos row that references the project,
    #     so the zero-repo count below can never miss a concurrently-linked
    #     repository and mark a project reclaimed while it still has one.
    holder_project_ids = sorted(
        {project_id}
        | {
            holder.project_id
            for holder in (remote_holder_probe, id_holder_probe)
            if holder is not None
        }
        | ({legacy_holder.id} if legacy_holder is not None else set())
    )
    await db.execute(
        select(Project)
        .where(Project.id.in_(holder_project_ids))
        .order_by(Project.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    # Re-select the probed ProjectRepo rows FOR UPDATE by id now that their
    # parent projects are locked, and re-validate their classification: the
    # row's remote/provider identity may have changed between the unlocked
    # probe and the lock, so a holder whose key no longer matches is dropped
    # (the unique indexes remain the final backstop for any late writer).
    remote_holder = None
    if remote_holder_probe is not None:
        remote_holder = (await db.execute(
            select(ProjectRepo)
            .where(ProjectRepo.id == remote_holder_probe.id)
            .with_for_update()
        )).scalar_one_or_none()
        if remote_holder is not None and remote_holder.git_remote_normalized != normalized:
            remote_holder = None
    id_holder = None
    if id_holder_probe is not None:
        id_holder = (await db.execute(
            select(ProjectRepo)
            .where(ProjectRepo.id == id_holder_probe.id)
            .with_for_update()
        )).scalar_one_or_none()
        if id_holder is not None and not (
            id_holder.provider == provider
            and id_holder.provider_repo_id == provider_repo_id
            and id_holder.git_remote_normalized != normalized
        ):
            id_holder = None

    # Classify both holders BEFORE changing anything. Rename within the same
    # project: this project already links the repository (same provider id)
    # under its OLD name. That row is renamed in place after the normal
    # rules have run against any OTHER row holding the new name (an
    # unverified one is displaced, a verified one is a genuine 409), all in
    # this one transaction.
    same_project_rename = (
        id_holder is not None
        and id_holder.project_id == project_id
        and verified
    )
    holders_to_resolve = (remote_holder,) if same_project_rename else (remote_holder, id_holder)
    # Re-load the target project under its lock and re-check that its org
    # did not change and the caller still administers it (standing may have
    # changed while GitHub was being called).
    project = (await db.execute(
        select(Project)
        .where(Project.id == project_id)
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if project is None:
        raise HTTPException(404, "Project not found")
    if project.org_id != project_org_id:
        raise HTTPException(409, "Project changed while linking; retry")
    if not await user_is_project_admin(db, user.id, project):
        raise HTTPException(403, "Only a project admin can link repos")

    # 3c. Repo-reclaimed revival: a project with zero repos (or
    #     repo_reclaimed_at set from a prior displacement) gets its
    #     first new link as the forced primary, clearing the orphaned
    #     state. The legacy column is NOT mirrored here: a legacy-only
    #     holder (4b/5b) may still occupy that unique value, and the
    #     mirror must happen only after it has been displaced — the
    #     forced primary makes step 6 do it.
    repo_count = (await db.execute(
        select(func.count(ProjectRepo.id)).where(
            ProjectRepo.project_id == project_id,
        )
    )).scalar() or 0
    if repo_count == 0 or project.repo_reclaimed_at is not None:
        body.is_primary = True
        project.repo_reclaimed_at = None

    # Re-validate the legacy holder under its lock: its column (or a
    # project_repos row for this remote) may have changed between the
    # unlocked probe above and the lock. ProjectRepo holders were locked
    # at selection time and cannot have moved.
    if legacy_holder is not None:
        still_legacy = legacy_holder.git_remote_normalized == normalized and (
            await db.execute(
                select(ProjectRepo.id).where(
                    ProjectRepo.project_id == legacy_holder.id,
                    ProjectRepo.git_remote_normalized == normalized,
                ).limit(1)
            )
        ).first() is None
        if not still_legacy:
            legacy_holder = None

    # A same-project re-verification is recorded here and returned after
    # the other holders (legacy squatter, rename holder) are resolved.
    upgraded_own_holder = None
    for holder in holders_to_resolve:
        if holder is None:
            continue
        # F3 access-gating for 409: only disclose existing_project_id
        # to callers who can access the holding project. The project row
        # was already locked FOR UPDATE + refreshed above (4c); this
        # fetch hits the identity map.
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
            if holder.project_id == project_id and not same_project_rename:
                # Same-project re-verification: this project already links
                # the repository unverified; proving ownership upgrades the
                # existing row in place instead of deleting it (deleting
                # would transiently mark the project reclaimed, null its
                # legacy remote, and drop its primary flag — tk_b6c5e184ba8d4aa1).
                # Skips the in-place upgrade when a same-project rename is in
                # flight: the new-name holder is a duplicate of the old-name
                # row (same provider+repo_id) and must be displaced, not
                # upgraded, or the partial-unique provider index would see
                # both rows at commit and 500 the rename.
                holder.verified = True
                holder.verification_method = verification_method
                holder.provider = provider
                holder.provider_repo_id = provider_repo_id
                if project.repo_reclaimed_at is not None:
                    project.repo_reclaimed_at = None
                # An explicit is_primary=true on a same-project re-verification
                # must be honored exactly like step 6: demote the current
                # primary (keep it linked), promote this row, and mirror the
                # legacy column. The early return below used to skip step 6
                # wholesale, silently dropping the caller's primary request.
                if body.is_primary and not holder.is_primary:
                    await db.execute(
                        update(ProjectRepo)
                        .where(
                            ProjectRepo.project_id == project_id,
                            ProjectRepo.is_primary == True,  # noqa: E712
                            ProjectRepo.id != holder.id,
                        )
                        .values(is_primary=False)
                    )
                    await db.flush()  # land the demote before the promote
                    holder.is_primary = True
                if holder.is_primary:
                    # projects.git_remote_normalized is globally unique; a
                    # displaced (repo-reclaimed) project may still carry this
                    # string. The project_repos row is authoritative for
                    # resolution, so only mirror when it is free (same guard
                    # as the rename path below).
                    taken = (await db.execute(
                        select(Project.id).where(
                            Project.git_remote_normalized == normalized,
                            Project.id != project_id,
                        )
                    )).first()
                    if taken is None:
                        project.git_remote_normalized = normalized
                upgraded_own_holder = holder
                continue
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
                # Reclaimed is structural: the legacy remote is nulled in
                # the same transaction, so legacy remote fallbacks can
                # never resolve to (or grant access to) the displaced
                # project, and the claimant can take the name. No
                # placeholder string — the 'reclaimed:' namespace was
                # attacker-claimable and is reserved.
                await db.execute(
                    update(Project)
                    .where(Project.id == holder_project_id)
                    .values(
                        repo_reclaimed_at=func.now(),
                        git_remote_normalized=None,
                    )
                )
                await db.flush()
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
                    "legacy_holder": False,
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

    # 5b. Resolve the legacy holder (a project holding `normalized` ONLY
    #     in its legacy column) under the same rules as an unverified
    #     ProjectRepo holder: an unverified claimant cannot displace
    #     (409); a verified owner displaces the squatted legacy value.
    if legacy_holder is not None:
        if not verified:
            # F3 access-gating on the existing_project_id disclosure.
            if await user_can_access_project(db, user.id, legacy_holder):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "repo_already_linked",
                        "message": (
                            "This repo is already linked to another "
                            "project. Unlink it from that project first, "
                            "or merge the projects."
                        ),
                        "existing_project_id": legacy_holder.id,
                    },
                )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "repo_already_linked",
                    "message": "This repo is already linked to another project.",
                },
            )
        # Verified → displace. The holder project is locked (4c), so
        # this count cannot miss a concurrently-linked repository.
        legacy_repo_count = (await db.execute(
            select(func.count(ProjectRepo.id)).where(
                ProjectRepo.project_id == legacy_holder.id,
            )
        )).scalar() or 0
        if legacy_repo_count == 0:
            # No repositories left: structurally reclaimed — NULL legacy
            # remote + repo_reclaimed_at, never a placeholder string. A
            # merge tombstone is a distinct state and must never be marked
            # reclaimed (migration 073 guards the same CASE), so the stamp
            # is conditional even though the probe above already excludes
            # tombstones — defense in depth.
            await db.execute(
                update(Project)
                .where(Project.id == legacy_holder.id)
                .values(
                    repo_reclaimed_at=case(
                        (Project.merged_into_project_id.is_(None), func.now()),
                        else_=Project.repo_reclaimed_at,
                    ),
                    git_remote_normalized=None,
                )
            )
        else:
            # The project keeps its real repositories; only the squatted
            # legacy value is displaced. Realign the column to the
            # project's primary (else oldest) repository.
            legacy_primary = (await db.execute(
                select(ProjectRepo)
                .where(ProjectRepo.project_id == legacy_holder.id)
                .order_by(
                    ProjectRepo.is_primary.desc(), ProjectRepo.created_at.asc()
                )
                .limit(1)
            )).scalar_one()
            await db.execute(
                update(Project)
                .where(Project.id == legacy_holder.id)
                .values(git_remote_normalized=legacy_primary.git_remote_normalized)
            )
        await db.flush()
        db.add(AdminAction(
            id=f"adm_{uuid.uuid4().hex[:16]}",
            admin_id=user.id,
            action="repo_displaced",
            target_type="project_repo",
            target_id=legacy_holder.id,
            details=json.dumps({
                "displaced_remote": normalized,
                "claimant_project_id": project_id,
                "claimant_user_id": user.id,
                "claimant_user_email": user.email,
                "old_verified": False,
                "old_verification_method": None,
                "old_is_primary": True,
                "legacy_holder": True,
                "legacy_repo_count": legacy_repo_count,
                "cross_org": bool(
                    project.org_id is not None
                    and legacy_holder.org_id is not None
                    and project.org_id != legacy_holder.org_id
                ),
                "actor_user_id": user.id,
                "actor_email": user.email,
            }),
        ))

    if upgraded_own_holder is not None:
        await db.commit()
        await db.refresh(upgraded_own_holder)
        return _repo_to_response(upgraded_own_holder)

    if same_project_rename:
        assert id_holder is not None
        await db.flush()  # any displaced new-name holder is gone first
        id_holder.git_remote_normalized = normalized
        id_holder.verified = verified
        id_holder.verification_method = verification_method
        id_holder.provider = provider
        id_holder.provider_repo_id = provider_repo_id
        if body.is_primary and not id_holder.is_primary:
            await db.execute(
                update(ProjectRepo)
                .where(
                    ProjectRepo.project_id == project_id,
                    ProjectRepo.is_primary == True,  # noqa: E712
                    ProjectRepo.id != id_holder.id,
                )
                .values(is_primary=False)
            )
            await db.flush()
            id_holder.is_primary = True
        if id_holder.is_primary:
            # projects.git_remote_normalized is globally unique; a displaced
            # (repo-reclaimed) project may still carry this string. The
            # project_repos row is authoritative for resolution, so only
            # mirror the new name when it is free.
            taken = (await db.execute(
                select(Project.id).where(
                    Project.git_remote_normalized == normalized,
                    Project.id != project_id,
                )
            )).first()
            if taken is None:
                project.git_remote_normalized = normalized
        await db.commit()
        await db.refresh(id_holder)
        return _repo_to_response(id_holder)

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

    # 1. Lock + authz the target project. unlink holds exactly one project
    #    lock, so it cannot join a deadlock cycle with link (which locks a
    #    set of projects in id order), but it MUST take FOR UPDATE so the
    #    repo count and the was_primary read below cannot race a concurrent
    #    displacement that re-promotes the repo being unlinked
    #    (tk_b6c5e184ba8d4aa1).
    project = (await db.execute(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
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


# Ids minted by create_project. A normalized git remote always contains a
# dot or slash, so it can never match this.
_PROJECT_ID_RE = re.compile(r"^proj_[0-9a-f]{16}$")


@router.get("/{git_remote_normalized:path}", response_model=ProjectResponse)
async def get_project(
    git_remote_normalized: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> ProjectResponse:
    """Get a project context by project id or git remote.

    A segment matching ``proj_<16 hex>`` is looked up by id; anything
    else is a normalized git remote.  Both share the access, tombstone
    and 404/403 handling below.

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
        if _PROJECT_ID_RE.match(git_remote_normalized):
            project = await resolve_project_by_id(
                db, git_remote_normalized, follow_tombstone=False,
            )
        else:
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

    from sessionfs.server.auth.project_access import user_can_edit_project

    effective_tier = (await resolve_project_plan_tier(project, db)).value
    return _project_response(
        project,
        repos=await _repos_for_project(db, project.id),
        effective_tier=effective_tier,
        can_edit=await user_can_edit_project(db, user.id, project),
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
    from sessionfs.server.auth.project_access import (
        require_project_editor,
        user_can_access_project,
    )
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
    # Overwriting the shared context document is an edit: owner or org
    # member only (a captured-session reader gets 403).
    await require_project_editor(db, user.id, project)

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

    effective_tier = (await resolve_project_plan_tier(project, db)).value
    return _project_response(
        project,
        repos=await _repos_for_project(db, project.id),
        effective_tier=effective_tier,
        can_edit=True,
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
