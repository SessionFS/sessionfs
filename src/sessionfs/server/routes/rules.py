"""Project rules API routes.

Endpoints:
- GET  /api/v1/projects/{id}/rules
- PUT  /api/v1/projects/{id}/rules              (ETag / If-Match required)
- POST /api/v1/projects/{id}/rules/compile
- GET  /api/v1/projects/{id}/rules/versions
- GET  /api/v1/projects/{id}/rules/versions/{version}

Access control: must be project owner OR have at least one session
with matching git_remote_normalized (same model as knowledge.py).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    Project,
    ProjectRules,
    RulesVersion,
    Session,
    User,
)
from sessionfs.server.services.rules import (
    compile_rules,
    compute_etag,
    get_or_create_rules,
)
from sessionfs.server.services.rules_compiler import SUPPORTED_TOOLS

logger = logging.getLogger("sessionfs.api")

router = APIRouter(prefix="/api/v1/projects", tags=["rules"])


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------


class RulesResponse(BaseModel):
    id: str
    project_id: str
    version: int
    static_rules: str
    include_knowledge: bool
    knowledge_types: list[str]
    knowledge_max_tokens: int
    include_context: bool
    context_sections: list[str]
    context_max_tokens: int
    tool_overrides: dict
    enabled_tools: list[str]
    supported_tools: list[str]
    created_at: datetime
    updated_at: datetime
    etag: str


class UpdateRulesRequest(BaseModel):
    static_rules: str | None = None
    include_knowledge: bool | None = None
    knowledge_types: list[str] | None = None
    knowledge_max_tokens: int | None = None
    include_context: bool | None = None
    context_sections: list[str] | None = None
    context_max_tokens: int | None = None
    tool_overrides: dict | None = None
    enabled_tools: list[str] | None = None


class CompileRulesRequest(BaseModel):
    tools: list[str] | None = Field(
        default=None,
        description="Optional override list; defaults to ProjectRules.enabled_tools",
    )


class CompiledOutput(BaseModel):
    tool: str
    filename: str
    content: str
    hash: str
    token_count: int


class CompileResponse(BaseModel):
    version: int
    created_new_version: bool
    aggregate_hash: str
    outputs: list[CompiledOutput]


class RulesVersionSummary(BaseModel):
    id: str
    version: int
    compiled_at: datetime
    compiled_by: str
    content_hash: str
    tools: list[str]


class RulesVersionDetail(BaseModel):
    id: str
    rules_id: str
    version: int
    static_rules: str
    compiled_outputs: dict
    knowledge_snapshot: list[dict]
    context_snapshot: dict
    compiled_at: datetime
    compiled_by: str
    content_hash: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def _load_project_or_404(
    project_id: str, db: AsyncSession, user_id: str
) -> Project:
    """Resolve the path `{project_id}` param to a Project row.

    Accepts either the project UUID (``proj_...``) or the git remote
    (``github.com/owner/repo``), matching the dual pattern used across the
    API (``projects.py`` keys by git remote; ``knowledge.py`` keys by UUID).
    The dashboard passes the git remote on every project-scoped request.
    """
    # Try UUID first, then fall back to git_remote_normalized.
    result = await db.execute(select(Project).where(Project.id == project_id))
    project = result.scalar_one_or_none()
    if project is None:
        result = await db.execute(
            select(Project).where(Project.git_remote_normalized == project_id)
        )
        project = result.scalar_one_or_none()
    if not project:
        raise HTTPException(404, "Project not found")
    if project.owner_id == user_id:
        return project
    access = await db.execute(
        select(Session.id).where(
            Session.user_id == user_id,
            Session.git_remote_normalized == project.git_remote_normalized,
        ).limit(1)
    )
    if access.scalar_one_or_none() is None:
        raise HTTPException(403, "No access to this project")
    return project


def _rules_to_response(rules: ProjectRules) -> RulesResponse:
    return RulesResponse(
        id=rules.id,
        project_id=rules.project_id,
        version=rules.version,
        static_rules=rules.static_rules or "",
        include_knowledge=rules.include_knowledge,
        knowledge_types=_as_list(rules.knowledge_types, []),
        knowledge_max_tokens=rules.knowledge_max_tokens,
        include_context=rules.include_context,
        context_sections=_as_list(rules.context_sections, []),
        context_max_tokens=rules.context_max_tokens,
        tool_overrides=_as_obj(rules.tool_overrides, {}),
        enabled_tools=_as_list(rules.enabled_tools, []),
        supported_tools=list(SUPPORTED_TOOLS),
        created_at=rules.created_at,
        updated_at=rules.updated_at,
        etag=compute_etag(rules),
    )


def _as_list(raw: str | None, default: list) -> list:
    if not raw:
        return list(default)
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else list(default)
    except json.JSONDecodeError:
        return list(default)


def _as_obj(raw: str | None, default: dict) -> dict:
    if not raw:
        return dict(default)
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else dict(default)
    except json.JSONDecodeError:
        return dict(default)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/{project_id}/rules", response_model=RulesResponse)
async def get_rules(
    project_id: str,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RulesResponse:
    """Fetch canonical rules for a project. Creates a default row on first read."""
    project = await _load_project_or_404(project_id, db, user.id)
    rules = await get_or_create_rules(db, project, user.id)
    body = _rules_to_response(rules)
    response.headers["ETag"] = body.etag
    return body


@router.put("/{project_id}/rules", response_model=RulesResponse)
async def update_rules(
    project_id: str,
    body: UpdateRulesRequest,
    request: Request,
    response: Response,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RulesResponse:
    """Update canonical rules with optimistic concurrency.

    Requires `If-Match` header with the current ETag; returns 409 on mismatch.
    """
    project = await _load_project_or_404(project_id, db, user.id)
    # Ensure the row exists first (creates default on first read).
    await get_or_create_rules(db, project, user.id)

    if_match = request.headers.get("If-Match")
    if not if_match:
        raise HTTPException(428, "If-Match header required")

    # Re-fetch under a row lock so the ETag check + mutation + commit are
    # atomic. Two concurrent writers holding the same prior ETag can't both
    # pass this check — the second one waits for the first to commit, sees
    # the new ETag, and returns 409. SQLite ignores FOR UPDATE (single-writer
    # semantics already serialise writes there).
    locked = await db.execute(
        select(ProjectRules)
        .where(ProjectRules.project_id == project.id)
        .with_for_update()
    )
    rules = locked.scalar_one()
    current_etag = compute_etag(rules)
    if if_match != current_etag:
        raise HTTPException(409, "Rules modified by another client — refresh and retry")

    changed = False

    if body.static_rules is not None and body.static_rules != rules.static_rules:
        rules.static_rules = body.static_rules
        changed = True
    if body.include_knowledge is not None and body.include_knowledge != rules.include_knowledge:
        rules.include_knowledge = body.include_knowledge
        changed = True
    if body.knowledge_types is not None:
        new = json.dumps(list(body.knowledge_types))
        if new != rules.knowledge_types:
            rules.knowledge_types = new
            changed = True
    if body.knowledge_max_tokens is not None and body.knowledge_max_tokens != rules.knowledge_max_tokens:
        if body.knowledge_max_tokens < 0 or body.knowledge_max_tokens > 20000:
            raise HTTPException(422, "knowledge_max_tokens out of range")
        rules.knowledge_max_tokens = body.knowledge_max_tokens
        changed = True
    if body.include_context is not None and body.include_context != rules.include_context:
        rules.include_context = body.include_context
        changed = True
    if body.context_sections is not None:
        new = json.dumps(list(body.context_sections))
        if new != rules.context_sections:
            rules.context_sections = new
            changed = True
    if body.context_max_tokens is not None and body.context_max_tokens != rules.context_max_tokens:
        if body.context_max_tokens < 0 or body.context_max_tokens > 20000:
            raise HTTPException(422, "context_max_tokens out of range")
        rules.context_max_tokens = body.context_max_tokens
        changed = True
    if body.tool_overrides is not None:
        new = json.dumps(body.tool_overrides)
        if new != rules.tool_overrides:
            rules.tool_overrides = new
            changed = True
    if body.enabled_tools is not None:
        # Validate tool slugs — reject unknown ones loudly so users don't end
        # up wondering why their enabled list silently dropped entries.
        unknown = [t for t in body.enabled_tools if t not in SUPPORTED_TOOLS]
        if unknown:
            raise HTTPException(
                422, f"Unsupported tool(s): {', '.join(unknown)}. "
                f"Supported: {', '.join(SUPPORTED_TOOLS)}"
            )
        new = json.dumps(list(body.enabled_tools))
        if new != rules.enabled_tools:
            rules.enabled_tools = new
            changed = True

    if changed:
        from datetime import timezone
        rules.updated_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(rules)

    body_out = _rules_to_response(rules)
    response.headers["ETag"] = body_out.etag
    return body_out


@router.post("/{project_id}/rules/compile", response_model=CompileResponse)
async def compile_rules_endpoint(
    project_id: str,
    body: CompileRulesRequest | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> CompileResponse:
    """Compile current canonical rules. Creates a new rules_versions row
    only if at least one compiled output's hash differs from the previous.
    """
    project = await _load_project_or_404(project_id, db, user.id)
    rules = await get_or_create_rules(db, project, user.id)

    tools_override = body.tools if (body and body.tools is not None) else None
    outcome = await compile_rules(db, project, rules, user.id, tools_override)

    return CompileResponse(
        version=rules.version,
        created_new_version=outcome.created_version is not None,
        aggregate_hash=outcome.aggregate_hash,
        outputs=[
            CompiledOutput(
                tool=r.tool,
                filename=r.filename,
                content=r.content,
                hash=r.content_hash,
                token_count=r.token_count,
            )
            for r in outcome.results
        ],
    )


@router.get("/{project_id}/rules/versions", response_model=list[RulesVersionSummary])
async def list_rules_versions(
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RulesVersionSummary]:
    """List rules versions (immutable compile history) newest-first."""
    project = await _load_project_or_404(project_id, db, user.id)
    rules = await get_or_create_rules(db, project, user.id)

    result = await db.execute(
        select(RulesVersion)
        .where(RulesVersion.rules_id == rules.id)
        .order_by(RulesVersion.version.desc())
    )
    versions = list(result.scalars().all())

    summaries = []
    for v in versions:
        outputs = _as_obj(v.compiled_outputs, {})
        summaries.append(
            RulesVersionSummary(
                id=v.id,
                version=v.version,
                compiled_at=v.compiled_at,
                compiled_by=v.compiled_by,
                content_hash=v.content_hash,
                tools=sorted(outputs.keys()),
            )
        )
    return summaries


@router.get(
    "/{project_id}/rules/versions/{version}", response_model=RulesVersionDetail
)
async def get_rules_version(
    project_id: str,
    version: int,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> RulesVersionDetail:
    project = await _load_project_or_404(project_id, db, user.id)
    rules = await get_or_create_rules(db, project, user.id)

    result = await db.execute(
        select(RulesVersion).where(
            RulesVersion.rules_id == rules.id, RulesVersion.version == version
        )
    )
    v = result.scalar_one_or_none()
    if v is None:
        raise HTTPException(404, f"Rules version {version} not found")

    return RulesVersionDetail(
        id=v.id,
        rules_id=v.rules_id,
        version=v.version,
        static_rules=v.static_rules,
        compiled_outputs=_as_obj(v.compiled_outputs, {}),
        knowledge_snapshot=_as_list(v.knowledge_snapshot, []),
        context_snapshot=_as_obj(v.context_snapshot, {}),
        compiled_at=v.compiled_at,
        compiled_by=v.compiled_by,
        content_hash=v.content_hash,
    )
