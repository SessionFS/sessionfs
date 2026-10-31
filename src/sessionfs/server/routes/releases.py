"""Pull requests, releases, gates, deployments and the Ship Report.

Review Loop R3, track F1 (docs/design/review-loop-r3.md §4, §5, §6.4). Routes
under ``/api/v1/projects/{project_id}``:

    POST  /pull-requests                        — upsert a PR (+ auto-link)
    POST  /releases                             — record a release (+ link PRs)
    POST  /releases/{version}/gates             — record a quality gate
    POST  /releases/{version}/deployments       — record a deployment
    GET   /releases                             — keyset list, newest first
    GET   /releases/{version}/ship-report       — Ship Report for a release
    GET   /ship-report?month=YYYY-MM            — Ship Report for a month

Access mirrors the loop surface: writes need the editor role (or a service key
with ``loops:write`` on the project); reads need ``loops:read``. A caller with
no standing gets the byte-identical 404 (existence oracle, review checklist
#2); a missing release is a byte-identical "Release not found" 404. Dollar
figures in the Ship Report follow the spend rule — serialized only for org
owners/admins (or the owner of a personal project), never for service keys.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import AuthContext, require_scope
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import Release
from sessionfs.server.auth.dependencies import assert_service_key_can_access_project
from sessionfs.server.db.models import Project
from sessionfs.server.routes.loop_checklist import _sanitize_text, _stamp_user_id, _write_scope
from sessionfs.server.routes.spend import _scope as _spend_scope
from sessionfs.server.services import releases

router = APIRouter(prefix="/api/v1/projects", tags=["releases"])

# Byte-identical existence oracle (review checklist #2).
_RELEASE_NOT_FOUND = "Release not found"
_PROJECT_NOT_FOUND = "Project not found"


async def _visible_project(
    project_id: str, auth: AuthContext, db: AsyncSession
) -> Project:
    """Load a project the caller can see; every miss is one 404 body.

    Service keys outside the project's org or allowlist get the same
    ``Project not found`` as an unknown id (no cross-org 403 oracle)."""
    if auth.key_kind == "service":
        project = (
            await db.execute(select(Project).where(Project.id == project_id))
        ).scalar_one_or_none()
        if project is None:
            raise HTTPException(404, _PROJECT_NOT_FOUND)
        await assert_service_key_can_access_project(
            db, auth, project, opaque_404_detail=_PROJECT_NOT_FOUND
        )
        return project
    return (await _spend_scope(project_id, auth, db))[0]


async def _scope(project_id: str, auth: AuthContext, db: AsyncSession):
    """Read scope: (project, show_dollars, readable) with opaque 404s."""
    if auth.key_kind == "service":
        return await _visible_project(project_id, auth, db), False, None
    return await _spend_scope(project_id, auth, db)


async def _write_project(
    project_id: str, auth: AuthContext, db: AsyncSession
) -> Project:
    """Write scope: invisible projects 404 identically; visible-but-not-
    editable user projects keep their 403."""
    if auth.key_kind == "service":
        return await _visible_project(project_id, auth, db)
    return await _write_scope(project_id, auth, db)

_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")

_PR_PROVIDERS: frozenset[str] = frozenset({"github", "gitlab"})
_PR_STATES: frozenset[str] = frozenset({"open", "closed", "merged"})
_RELEASE_SOURCES: frozenset[str] = frozenset(
    {"github_release", "gitlab_release", "helm_sync", "manual"}
)
_GATE_STATUSES: frozenset[str] = frozenset({"passed", "failed", "skipped"})
_DEPLOY_SOURCES: frozenset[str] = frozenset({"argocd", "helm", "manual"})

_MAX_TITLE = 500
_MAX_REPO = 200
_MAX_BRANCH = 200
_MAX_EXTERNAL_ID = 200
_MAX_EXTERNAL_URL = 2000
_MAX_VERSION = 100
_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_MAX_SHA = 64
_MAX_ENV = 64
_MAX_KIND = 64


# ── Pydantic bodies ──


class _PullRequestBody(BaseModel):
    provider: str
    repo: str
    number: int
    title: str
    external_id: str | None = None
    state: str = "open"
    author_user_id: str | None = None
    branch: str = ""
    head_sha: str | None = None
    merged_at: datetime | None = None
    merge_commit_sha: str | None = None
    external_url: str | None = None
    body: str = Field("", max_length=65536)

    @field_validator("provider")
    @classmethod
    def _provider(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in _PR_PROVIDERS:
            raise ValueError("provider must be one of: github, gitlab")
        return v

    @field_validator("state")
    @classmethod
    def _state(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in _PR_STATES:
            raise ValueError("state must be one of: open, closed, merged")
        return v

    @field_validator("number")
    @classmethod
    def _number(cls, v: int) -> int:
        if v < 1 or v > releases.MAX_PR_NUMBER:
            raise ValueError("number must be between 1 and 2147483647")
        return v

    @field_validator("repo", "title", "branch")
    @classmethod
    def _strings(cls, v: str | None) -> str | None:
        return _sanitize_text(v) if v is not None else None

    @field_validator("external_url")
    @classmethod
    def _external_url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        url = releases.safe_url(v)
        if url is None:
            raise ValueError("external_url must be an http(s) URL up to 2048 characters")
        return url

    @field_validator("repo")
    @classmethod
    def _repo_len(cls, v: str) -> str:
        if len(v) > _MAX_REPO:
            raise ValueError(f"repo must be {_MAX_REPO} characters or fewer")
        return v

    @field_validator("title")
    @classmethod
    def _title_len(cls, v: str) -> str:
        if len(v) > _MAX_TITLE:
            raise ValueError(f"title must be {_MAX_TITLE} characters or fewer")
        return v

    @field_validator("branch")
    @classmethod
    def _branch_len(cls, v: str) -> str:
        if len(v) > _MAX_BRANCH:
            raise ValueError(f"branch must be {_MAX_BRANCH} characters or fewer")
        return v

    @field_validator("external_id")
    @classmethod
    def _external_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if len(v) > _MAX_EXTERNAL_ID:
            raise ValueError(
                f"external_id must be {_MAX_EXTERNAL_ID} characters or fewer"
            )
        return v or None

    @field_validator("head_sha", "merge_commit_sha")
    @classmethod
    def _sha(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if len(v) > _MAX_SHA:
            raise ValueError(f"SHA must be {_MAX_SHA} characters or fewer")
        return v or None


class _ReleaseBody(BaseModel):
    version: str
    tag_sha: str
    previous_tag_sha: str | None = None
    released_at: datetime | None = None
    source: str = "manual"
    pr_numbers: list[int] | None = None
    commit_shas: list[str] | None = None

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("version cannot be blank")
        if len(v) > _MAX_VERSION:
            raise ValueError(f"version must be {_MAX_VERSION} characters or fewer")
        return v

    @field_validator("tag_sha", "previous_tag_sha")
    @classmethod
    def _sha(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("tag SHA cannot be blank")
        v = v.lower()
        if not _FULL_SHA_RE.fullmatch(v):
            raise ValueError("tag SHA must be a full 40- or 64-character hex commit id")
        return v

    @field_validator("source")
    @classmethod
    def _source(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in _RELEASE_SOURCES:
            raise ValueError(
                "source must be one of: github_release, gitlab_release, "
                "helm_sync, manual"
            )
        return v

    @field_validator("pr_numbers")
    @classmethod
    def _pr_numbers(cls, v: list[int] | None) -> list[int] | None:
        if v is None:
            return None
        if len(v) > releases.MAX_PR_NUMBERS:
            raise ValueError(f"pr_numbers must have {releases.MAX_PR_NUMBERS} entries or fewer")
        if any(n < 1 or n > releases.MAX_PR_NUMBER for n in v):
            raise ValueError("pr_numbers entries must be between 1 and 2147483647")
        return v

    @field_validator("commit_shas")
    @classmethod
    def _commit_shas(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        return [s.strip() for s in v if s and s.strip()][:_MAX_SHA]


class _GateBody(BaseModel):
    kind: str
    status: str = "passed"
    run_id: str | None = None
    url: str | None = None

    @field_validator("kind")
    @classmethod
    def _kind(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("kind cannot be blank")
        if len(v) > _MAX_KIND:
            raise ValueError(f"kind must be {_MAX_KIND} characters or fewer")
        return v

    @field_validator("status")
    @classmethod
    def _status(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in _GATE_STATUSES:
            raise ValueError("status must be one of: passed, failed, skipped")
        return v

    @field_validator("run_id")
    @classmethod
    def _run_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v[:_MAX_SHA] or None

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        if v is None or not v.strip():
            return None
        url = releases.safe_url(v)
        if url is None:
            raise ValueError("url must be an http(s) URL up to 2048 characters")
        return url


class _DeploymentBody(BaseModel):
    environment: str
    deployed_at: datetime | None = None
    source: str = "manual"

    @field_validator("environment")
    @classmethod
    def _environment(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("environment cannot be blank")
        if len(v) > _MAX_ENV:
            raise ValueError(f"environment must be {_MAX_ENV} characters or fewer")
        return v

    @field_validator("source")
    @classmethod
    def _source(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if v not in _DEPLOY_SOURCES:
            raise ValueError("source must be one of: argocd, helm, manual")
        return v


# ── Helpers ──


async def _get_release_or_404(
    db: AsyncSession, project_id: str, version: str
) -> Release:
    release = (
        await db.execute(
            select(Release).where(
                Release.project_id == project_id,
                Release.version == version,
            )
        )
    ).scalar_one_or_none()
    if release is None:
        raise HTTPException(404, _RELEASE_NOT_FOUND)
    return release


# ── Routes ──


@router.post("/{project_id}/pull-requests")
async def upsert_pr(
    project_id: str,
    body: _PullRequestBody,
    auth: AuthContext = Depends(require_scope("loops:write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Upsert a PR and auto-link tickets from ``tk_…`` ids / loop branches."""
    project = await _write_project(project_id, auth, db)
    return await releases.upsert_pull_request(
        db,
        project_id=project.id,
        provider=body.provider,
        repo=body.repo,
        number=body.number,
        title=body.title,
        external_id=body.external_id,
        state=body.state,
        author_user_id=body.author_user_id,
        branch=body.branch or "",
        head_sha=body.head_sha,
        merged_at=body.merged_at,
        merge_commit_sha=body.merge_commit_sha,
        external_url=body.external_url,
        body=body.body,
    )


@router.post("/{project_id}/releases")
async def record_release(
    project_id: str,
    body: _ReleaseBody,
    auth: AuthContext = Depends(require_scope("loops:write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Record a release and link its PRs (idempotent per project + version)."""
    project = await _write_project(project_id, auth, db)
    return await releases.record_release(
        db,
        project_id=project.id,
        version=body.version,
        tag_sha=body.tag_sha,
        previous_tag_sha=body.previous_tag_sha,
        released_at=body.released_at,
        source=body.source,
        pr_numbers=body.pr_numbers,
        commit_shas=body.commit_shas,
        created_by_user_id=_stamp_user_id(auth),
        service_key_id=auth.service_key_id if auth.key_kind == "service" else None,
    )


@router.post("/{project_id}/releases/{version}/gates")
async def record_gate(
    project_id: str,
    version: str,
    body: _GateBody,
    auth: AuthContext = Depends(require_scope("loops:write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Record a quality gate against a release (upsert per release + kind)."""
    project = await _write_project(project_id, auth, db)
    release = await _get_release_or_404(db, project.id, version)
    return await releases.record_gate(
        db,
        release_id=release.id,
        kind=body.kind,
        status=body.status,
        run_id=body.run_id,
        url=body.url,
    )


@router.post("/{project_id}/releases/{version}/deployments")
async def record_deployment(
    project_id: str,
    version: str,
    body: _DeploymentBody,
    auth: AuthContext = Depends(require_scope("loops:write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Record a deployment of a release (append-only)."""
    project = await _write_project(project_id, auth, db)
    release = await _get_release_or_404(db, project.id, version)
    return await releases.record_deployment(
        db,
        release_id=release.id,
        environment=body.environment,
        deployed_at=body.deployed_at,
        source=body.source,
    )


@router.get("/{project_id}/releases")
async def list_releases(
    project_id: str,
    before: datetime | None = Query(None),
    before_id: str | None = Query(None, max_length=64),
    limit: int = Query(20, ge=1, le=100),
    auth: AuthContext = Depends(require_scope("loops:read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """List a project's releases, newest first (keyset pagination)."""
    project, _, _ = await _scope(project_id, auth, db)
    rows = await releases.list_releases(
        db,
        project_id=project.id,
        limit=limit,
        before=before,
        before_id=before_id,
    )
    return {"project_id": project.id, "releases": rows}


@router.get("/{project_id}/releases/{version}/ship-report")
async def ship_report_for_version(
    project_id: str,
    version: str,
    auth: AuthContext = Depends(require_scope("loops:read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The Ship Report for one release."""
    project, show_dollars, _ = await _scope(project_id, auth, db)
    report = await releases.ship_report(
        db, project_id=project.id, show_dollars=show_dollars, version=version
    )
    if report is None:
        raise HTTPException(404, _RELEASE_NOT_FOUND)
    return {"project_id": project.id, **report}


@router.get("/{project_id}/ship-report")
async def ship_report_for_month(
    project_id: str,
    month: str = Query(..., description="YYYY-MM"),
    auth: AuthContext = Depends(require_scope("loops:read")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The Ship Report aggregated over one month's releases."""
    if not _MONTH_RE.match(month or "") or not (
        1970 <= int(month[:4]) <= 9998 and 1 <= int(month[5:7]) <= 12
    ):
        raise HTTPException(422, "month must be YYYY-MM")
    project, show_dollars, _ = await _scope(project_id, auth, db)
    report = await releases.ship_report(
        db, project_id=project.id, show_dollars=show_dollars, month=month
    )
    return {"project_id": project.id, **report}
