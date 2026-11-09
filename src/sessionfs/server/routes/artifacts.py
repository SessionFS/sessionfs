"""Artifacts, lineage and promotion v1 (GitHub #122).

- ``GET/POST /api/v1/projects/{p}/artifacts``
- ``GET/DELETE /api/v1/projects/{p}/artifacts/{id}``
- ``PUT  /api/v1/projects/{p}/artifacts/{id}/tags``
- ``POST /api/v1/projects/{p}/artifacts/{id}/deprecate``
- ``GET  /api/v1/projects/{p}/artifacts/{id}/lineage?direction=&depth=``
- ``GET  /api/v1/projects/{p}/artifacts/{id}/promotion-check?to_stage=``
- ``POST /api/v1/projects/{p}/artifacts/{id}/promote``
- ``POST /api/v1/projects/{p}/artifact-edges``, ``DELETE .../artifact-edges/{edge}``
- ``GET  /api/v1/projects/{p}/artifact-families``
- ``GET/PUT /api/v1/projects/{p}/artifact-families/{kind}/{name}/requirements``
- ``GET  /api/v1/projects/{p}/artifact-families/{kind}/{name}/evals``

An artifact is a pointer: SessionFS validates the URI's shape and never
fetches it. Project readers read; project editors register, tag, link and
promote; family requirements and hard delete need a project admin. Service
keys use ``artifacts:read`` / ``artifacts:write`` in their org and project
allowlist; promote, requirements and delete are people-only (user keys).
Every write (register, tag, link, unlink, deprecate, promote, requirements,
delete) needs the project's plan to include ``evaluations``; reads stay open
after a downgrade. Every miss is one 404 body.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import AuthContext, require_scope
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    AgentRun,
    Artifact,
    ArtifactEdge,
    ArtifactFamilyRequirement,
    ArtifactTag,
    EvalRun,
    EvalSuite,
    Project,
)
from sessionfs.server.routes.evals import (
    _err,
    actor_fields,
    clean_line,
    clean_text,
    dlp_scanner,
    gated_write,
    nf,
    project_for_read,
    refuse_service_key,
    require_admin,
    require_editor,
    resolve_suite,
)
from sessionfs.server.services import artifacts as svc
from sessionfs.server.services import evals as ev
from sessionfs.server.tier_gate import UserContext, get_user_context

router = APIRouter(prefix="/api/v1/projects", tags=["artifacts"])

Kind = Literal["dataset", "model", "feature_set", "prompt", "checkpoint"]
Relation = Literal[
    "derived_from", "trained_on", "fine_tuned_from", "distilled_from", "evaluated_against", "uses"
]
_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+:@-]{0,63}$")
_DIGEST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._/+=-]{0,127}$")
_MAX_REQUIRED_SUITES = 20
_MAX_REQUIRED_TAGS = 16
_MATRIX_VERSIONS = 50


def _check_tags(tags: dict[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in tags.items():
        if not isinstance(key, str) or not svc.TAG_KEY_RE.fullmatch(key):
            raise ValueError(f"tag key {key!r} must be a lowercase slug (max 64)")
        if not isinstance(value, str) or len(value) > 256:
            raise ValueError(f"tag {key!r} needs a string value of at most 256 characters")
        out[key] = value
    return out


class ArtifactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=64)
    external_uri: str = Field(min_length=1, max_length=svc.MAX_URI)
    digest: str | None = Field(default=None, max_length=128)
    schema_summary: dict[str, Any] | list[Any] | None = Field(default=None)
    description: str = Field(default="", max_length=4096)
    tags: dict[str, str] = Field(default_factory=dict)
    produced_by_agent_run_id: str | None = Field(default=None, max_length=64)

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = " ".join(v.split())
        if not v:
            raise ValueError("name is required")
        return v

    @field_validator("version")
    @classmethod
    def _version(cls, v: str) -> str:
        v = v.strip()
        if not _VERSION_RE.fullmatch(v):
            raise ValueError("version must be letters, digits or . _ + : @ - (1-64, no spaces)")
        return v

    @field_validator("digest")
    @classmethod
    def _digest(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if v.lower().startswith("sha256:") and not re.fullmatch(r"sha256:[0-9a-f]{64}", v.lower()):
            raise ValueError("a sha256 digest is sha256: followed by 64 hex characters")
        if not _DIGEST_RE.fullmatch(v):
            raise ValueError("digest must be sha256:<hex> or a provider etag (no spaces)")
        return v.lower() if v.lower().startswith("sha256:") else v

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > svc.MAX_TAGS:
            raise ValueError(f"an artifact has at most {svc.MAX_TAGS} tags")
        return _check_tags(v)


class TagsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    set: dict[str, str] = Field(default_factory=dict)
    remove: list[str] = Field(default_factory=list, max_length=svc.MAX_TAGS)

    @field_validator("set")
    @classmethod
    def _set(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > svc.MAX_TAGS:
            raise ValueError(f"an artifact has at most {svc.MAX_TAGS} tags")
        return _check_tags(v)


class EdgeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: str = Field(min_length=1, max_length=64)
    child_id: str = Field(min_length=1, max_length=64)
    relation: Relation
    context_ref: str | None = Field(default=None, max_length=200)


class PromoteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    to_stage: Literal["candidate", "staging", "production", "archived"]


class RequirementsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suites: list[str] = Field(default_factory=list, max_length=_MAX_REQUIRED_SUITES)
    require_tags: dict[str, str] = Field(default_factory=dict)

    @field_validator("require_tags")
    @classmethod
    def _tags(cls, v: dict[str, str]) -> dict[str, str]:
        if len(v) > _MAX_REQUIRED_TAGS:
            raise ValueError(f"at most {_MAX_REQUIRED_TAGS} required tags")
        return _check_tags(v)


# ── Helpers ──


async def _artifact_or_404(
    db: AsyncSession, project: Project, artifact_id: str, *, lock: bool = False
) -> Artifact:
    """The artifact in this project (one 404 otherwise). ``lock=True`` takes
    a row lock and re-reads it; callers hold the project scope lock first."""
    if lock:
        row = (
            await db.execute(
                select(Artifact)
                .where(Artifact.id == artifact_id, Artifact.project_id == project.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
    else:
        row = await db.get(Artifact, artifact_id)
    if row is None or row.project_id != project.id:
        raise nf()
    return row


def _scan_json(scan, value: Any, depth: int = 0) -> Any:
    """DLP every string leaf (keys and values) of a schema summary."""
    if depth > 32:
        raise _err(422, "invalid_schema_summary", "schema_summary is nested too deeply")
    if isinstance(value, str):
        return clean_text(scan(value))
    if isinstance(value, list):
        return [_scan_json(scan, v, depth + 1) for v in value]
    if isinstance(value, dict):
        return {clean_line(scan(str(k)) or "", 200): _scan_json(scan, v, depth + 1) for k, v in value.items()}
    return value


async def _artifact_write(
    project_id: str, db: AsyncSession, auth: AuthContext, ctx: UserContext
) -> Project:
    project = await project_for_read(project_id, db, auth)
    await require_editor(db, auth, project)
    await gated_write(db, auth, ctx, project)
    return project


async def _full_view(db: AsyncSession, row: Artifact) -> dict[str, Any]:
    tags = (await svc.load_tags(db, [row.id])).get(row.id, {})
    return svc.artifact_view(row, tags)


# ── Artifacts ──


@router.get("/{project_id}/artifacts")
async def list_artifacts(
    project_id: str,
    kind: str | None = Query(default=None, max_length=20),
    name: str | None = Query(default=None, max_length=120),
    stage: str | None = Query(default=None, max_length=20),
    tag: str | None = Query(default=None, max_length=330, description="key or key=value"),
    include_deprecated: bool = Query(default=True),
    cursor: str | None = Query(default=None, max_length=200),
    limit: int = Query(default=50, ge=1, le=200),
    auth: AuthContext = Depends(require_scope("artifacts:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Artifacts newest first; ``cursor`` is the last page's ``next_cursor``."""
    project = await project_for_read(project_id, db, auth)
    stmt = select(Artifact).where(Artifact.project_id == project.id)
    if kind:
        stmt = stmt.where(Artifact.kind == kind)
    if name:
        stmt = stmt.where(Artifact.name == name)
    if stage:
        stmt = stmt.where(Artifact.stage == stage)
    if not include_deprecated:
        stmt = stmt.where(Artifact.deprecated_at.is_(None))
    if tag:
        key, sep, value = tag.partition("=")
        cond = [ArtifactTag.key == key.strip()]
        if sep:
            cond.append(ArtifactTag.value == value)
        stmt = stmt.where(Artifact.id.in_(select(ArtifactTag.artifact_id).where(*cond)))
    if cursor:
        ts, _, last_id = cursor.partition("|")
        try:
            when = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            raise _err(422, "invalid_cursor", "cursor is not a value this endpoint returned")
        stmt = stmt.where(
            or_(Artifact.created_at < when, and_(Artifact.created_at == when, Artifact.id < last_id))
        )
    rows = list(
        (
            await db.execute(
                stmt.order_by(Artifact.created_at.desc(), Artifact.id.desc()).limit(limit + 1)
            )
        ).scalars().all()
    )
    more = len(rows) > limit
    rows = rows[:limit]
    tags = await svc.load_tags(db, [r.id for r in rows])
    nxt = f"{rows[-1].created_at.isoformat()}|{rows[-1].id}" if more and rows else None
    return {"artifacts": [svc.artifact_view(r, tags.get(r.id)) for r in rows], "next_cursor": nxt}


@router.post("/{project_id}/artifacts", status_code=201)
async def register_artifact(
    project_id: str,
    body: ArtifactCreate,
    auth: AuthContext = Depends(require_scope("artifacts:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Register one artifact version (a pointer; the URI is never fetched)."""
    try:
        uri = svc.validate_uri(body.external_uri)
    except svc.ArtifactUriError as exc:
        raise _err(422, exc.code, str(exc))
    project = await _artifact_write(project_id, db, auth, ctx)
    scan = await dlp_scanner(db, auth, project)
    if scan(uri) != uri:
        raise _err(
            422,
            "uri_has_sensitive_data",
            "external_uri contains data the DLP policy flags; store a plain location.",
        )
    schema_text = None
    if body.schema_summary is not None:
        schema_text = json.dumps(_scan_json(scan, body.schema_summary), sort_keys=True)
        if len(schema_text.encode("utf-8")) > svc.MAX_SCHEMA_SUMMARY_BYTES:
            raise _err(422, "schema_summary_too_large", f"schema_summary must be at most {svc.MAX_SCHEMA_SUMMARY_BYTES} bytes")
    if body.produced_by_agent_run_id:
        run = await db.get(AgentRun, body.produced_by_agent_run_id)
        if run is None or run.project_id != project.id:
            raise _err(422, "invalid_agent_run", "produced_by_agent_run_id must be an agent run in this project.")
    if await svc.count_project_artifacts(db, project.id) >= svc.MAX_ARTIFACTS_PER_PROJECT:
        raise _err(409, "artifact_limit", f"A project has at most {svc.MAX_ARTIFACTS_PER_PROJECT} artifacts.")
    name = clean_line(scan(body.name) or "", 120)
    if not name:
        raise _err(422, "invalid_artifact", "name is required")
    exists = (
        await db.execute(
            select(Artifact.id).where(
                svc.family_clause(project.id, body.kind, name), Artifact.version == body.version
            )
        )
    ).scalar_one_or_none()
    if exists:
        raise _err(409, "artifact_version_exists", "This family already has that version.", artifact_id=exists)
    row = Artifact(
        id=f"art_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        kind=body.kind,
        name=name,
        version=body.version,
        external_uri=uri,
        digest=body.digest,
        schema_summary=schema_text,
        description=clean_text(scan(body.description) or "", 4096) or "",
        stage="candidate",
        produced_by_agent_run_id=body.produced_by_agent_run_id,
        created_at=datetime.now(timezone.utc),
        **actor_fields(auth),
    )
    db.add(row)
    await db.flush()
    for key, value in body.tags.items():
        db.add(ArtifactTag(artifact_id=row.id, key=key, value=clean_line(scan(value) or "", 256)))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _err(409, "artifact_version_exists", "This family already has that version.")
    await db.refresh(row)
    return await _full_view(db, row)


@router.get("/{project_id}/artifacts/{artifact_id}")
async def get_artifact(
    project_id: str,
    artifact_id: str,
    auth: AuthContext = Depends(require_scope("artifacts:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await project_for_read(project_id, db, auth)
    row = await _artifact_or_404(db, project, artifact_id)
    out = await _full_view(db, row)
    out["parents"] = (
        await db.execute(select(func.count()).select_from(ArtifactEdge).where(ArtifactEdge.child_id == row.id))
    ).scalar_one()
    out["children"] = (
        await db.execute(select(func.count()).select_from(ArtifactEdge).where(ArtifactEdge.parent_id == row.id))
    ).scalar_one()
    latest = await svc.latest_trusted_runs(db, [row.id])
    out["latest_trusted_runs"] = [
        {"suite_lineage_id": lin, "run_id": r.id, "verdict": r.verdict, "finished_at": r.finished_at.isoformat() if r.finished_at else None}
        for (_aid, lin), r in sorted(latest.items())
    ]
    return out


@router.put("/{project_id}/artifacts/{artifact_id}/tags")
async def update_tags(
    project_id: str,
    artifact_id: str,
    body: TagsBody,
    auth: AuthContext = Depends(require_scope("artifacts:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Set and remove tags (merge). At most 32 tags per artifact."""
    project = await _artifact_write(project_id, db, auth, ctx)
    row = await _artifact_or_404(db, project, artifact_id)
    scan = await dlp_scanner(db, auth, project)
    current = (await svc.load_tags(db, [row.id])).get(row.id, {})
    remove = {k for k in body.remove if k not in body.set}
    merged = {k: v for k, v in current.items() if k not in remove}
    merged.update(body.set)
    if len(merged) > svc.MAX_TAGS:
        raise _err(422, "tag_limit", f"An artifact has at most {svc.MAX_TAGS} tags.")
    await db.execute(
        delete(ArtifactTag).where(
            ArtifactTag.artifact_id == row.id,
            ArtifactTag.key.in_(list(remove | set(body.set))),
        )
    )
    for key, value in body.set.items():
        db.add(ArtifactTag(artifact_id=row.id, key=key, value=clean_line(scan(value) or "", 256)))
    await db.commit()
    return await _full_view(db, row)


@router.post("/{project_id}/artifacts/{artifact_id}/deprecate")
async def deprecate_artifact(
    project_id: str,
    artifact_id: str,
    auth: AuthContext = Depends(require_scope("artifacts:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Mark a version deprecated (kept, never deleted; can't be promoted).
    Plan-gated like every artifact write."""
    project = await _artifact_write(project_id, db, auth, ctx)
    row = await _artifact_or_404(db, project, artifact_id, lock=True)
    await db.execute(
        update(Artifact)
        .where(Artifact.id == row.id, Artifact.deprecated_at.is_(None))
        .values(deprecated_at=datetime.now(timezone.utc))
    )
    await db.commit()
    await db.refresh(row)
    return await _full_view(db, row)


@router.delete("/{project_id}/artifacts/{artifact_id}", status_code=204)
async def delete_artifact(
    project_id: str,
    artifact_id: str,
    auth: AuthContext = Depends(require_scope("artifacts:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Hard delete (project admin, user key; plan-gated). Refused while any
    edge or eval run references the artifact; deprecate instead."""
    project = await project_for_read(project_id, db, auth)
    await require_admin(db, auth, project, "Deleting artifacts")
    await gated_write(db, auth, ctx, project, admin_what="Deleting artifacts")
    row = await _artifact_or_404(db, project, artifact_id, lock=True)
    edge = (
        await db.execute(
            select(ArtifactEdge.id)
            .where(or_(ArtifactEdge.parent_id == row.id, ArtifactEdge.child_id == row.id))
            .limit(1)
        )
    ).scalar_one_or_none()
    run = (
        await db.execute(select(EvalRun.id).where(EvalRun.artifact_id == row.id).limit(1))
    ).scalar_one_or_none()
    bank = (
        await db.execute(
            select(EvalSuite.id).where(EvalSuite.case_bank_artifact_id == row.id).limit(1)
        )
    ).scalar_one_or_none()
    if edge or run or bank:
        raise _err(409, "artifact_in_use", "Lineage edges or eval runs reference this artifact; deprecate it instead.")
    await db.execute(delete(ArtifactTag).where(ArtifactTag.artifact_id == row.id))
    await db.execute(delete(Artifact).where(Artifact.id == row.id))
    await db.commit()
    return Response(status_code=204)


@router.get("/{project_id}/artifacts/{artifact_id}/lineage")
async def get_lineage(
    project_id: str,
    artifact_id: str,
    direction: Literal["up", "down", "both"] = Query(default="both"),
    depth: int = Query(default=svc.MAX_DEPTH, ge=1, le=svc.MAX_DEPTH),
    auth: AuthContext = Depends(require_scope("artifacts:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Bounded BFS (≤ 500 nodes, depth ≤ 25): nodes, edges and the latest
    trusted run per (suite lineage, node)."""
    project = await project_for_read(project_id, db, auth)
    root = await _artifact_or_404(db, project, artifact_id)
    graph = await svc.trace_lineage(db, project.id, root.id, direction=direction, depth=depth)
    rows = {
        r.id: r
        for r in (
            await db.execute(select(Artifact).where(Artifact.id.in_(graph.node_ids)))
        ).scalars().all()
    }
    tags = await svc.load_tags(db, list(rows))
    latest = await svc.latest_trusted_runs(db, list(rows))
    return {
        "root_id": root.id,
        "direction": direction,
        "depth": depth,
        "truncated": graph.truncated,
        "nodes": [svc.artifact_view(rows[i], tags.get(i)) for i in graph.node_ids if i in rows],
        "edges": [svc.edge_view(e) for e in graph.edges],
        "evals": [
            {
                "artifact_id": aid,
                "suite_lineage_id": lin,
                "run_id": r.id,
                "verdict": r.verdict,
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
            }
            for (aid, lin), r in sorted(latest.items())
        ],
    }


@router.get("/{project_id}/artifacts/{artifact_id}/promotion-check")
async def promotion_check(
    project_id: str,
    artifact_id: str,
    to_stage: Literal["candidate", "staging", "production", "archived"] = Query(...),
    auth: AuthContext = Depends(require_scope("artifacts:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Whether a promotion would be allowed now, and why not."""
    project = await project_for_read(project_id, db, auth)
    row = await _artifact_or_404(db, project, artifact_id)
    blockers = await svc.promotion_blockers(db, row, to_stage)
    return {"artifact_id": row.id, "from_stage": row.stage, "to_stage": to_stage, "allowed": not blockers, "blockers": blockers}


@router.post("/{project_id}/artifacts/{artifact_id}/promote")
async def promote_artifact(
    project_id: str,
    artifact_id: str,
    body: PromoteBody,
    auth: AuthContext = Depends(require_scope("artifacts:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Move a version to another stage (project editors, user keys only).

    Staging and production need a trusted ``passed`` run of every required
    suite on THIS version (409 ``promotion_blocked`` with reasons).
    Promoting to production archives the family's previous production
    version in the same transaction."""
    project = await project_for_read(project_id, db, auth)
    refuse_service_key(auth, "Promoting artifacts")
    await require_editor(db, auth, project)
    # Project scope lock first (void, deprecate and every other eval/artifact
    # write take it too), then the artifact's row lock; the gate below is
    # evaluated under both, so a concurrent void can't slip between the
    # check and the stage change.
    await gated_write(db, auth, ctx, project)
    row = await _artifact_or_404(db, project, artifact_id, lock=True)
    from_stage = row.stage
    blockers = await svc.promotion_blockers(db, row, body.to_stage)
    if blockers:
        raise _err(409, "promotion_blocked", "This version cannot be promoted yet.", blockers=blockers)
    res = await db.execute(
        update(Artifact)
        .where(Artifact.id == row.id, Artifact.stage == from_stage)
        .values(stage=body.to_stage)
    )
    if res.rowcount != 1:
        await db.rollback()
        raise _err(409, "stage_changed", "The artifact's stage changed; read it again.")
    archived: list[str] = []
    if body.to_stage == "production":
        archived = list(
            (
                await db.execute(
                    select(Artifact.id).where(
                        svc.family_clause(project.id, row.kind, row.name),
                        Artifact.stage == "production",
                        Artifact.id != row.id,
                    )
                )
            ).scalars().all()
        )
        if archived:
            await db.execute(
                update(Artifact)
                .where(Artifact.id.in_(archived), Artifact.stage == "production")
                .values(stage="archived")
            )
    await db.commit()
    await db.refresh(row)
    return {**(await _full_view(db, row)), "from_stage": from_stage, "archived_ids": archived}


# ── Edges ──


@router.post("/{project_id}/artifact-edges", status_code=201)
async def link_artifacts(
    project_id: str,
    body: EdgeCreate,
    auth: AuthContext = Depends(require_scope("artifacts:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Add a lineage edge parent -> child (both in this project). Refuses
    self-edges, cycles (checked under the project scope lock, depth ≤ 25)
    and more than 200 parents per child."""
    project = await _artifact_write(project_id, db, auth, ctx)
    parent = await _artifact_or_404(db, project, body.parent_id)
    child = await _artifact_or_404(db, project, body.child_id)
    if parent.id == child.id:
        raise _err(422, "self_edge", "An artifact cannot be its own parent.")
    existing = (
        await db.execute(
            select(ArtifactEdge.id).where(
                ArtifactEdge.parent_id == parent.id,
                ArtifactEdge.child_id == child.id,
                ArtifactEdge.relation == body.relation,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise _err(409, "edge_exists", "This edge already exists.", edge_id=existing)
    try:
        if await svc.would_create_cycle(db, parent.id, child.id):
            raise _err(422, "lineage_cycle", "This edge would make the lineage a cycle.")
    except svc.LineageTooDeep:
        raise _err(422, "lineage_too_deep", "The lineage below this child is too deep to check; the edge was not added.")
    if await svc.count_parents(db, child.id) >= svc.MAX_PARENTS:
        raise _err(409, "parent_limit", f"An artifact has at most {svc.MAX_PARENTS} parents.")
    scan = await dlp_scanner(db, auth, project)
    edge = ArtifactEdge(
        id=f"ae_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        parent_id=parent.id,
        child_id=child.id,
        relation=body.relation,
        context_ref=clean_line(scan(body.context_ref) or "", 200) or None,
        created_at=datetime.now(timezone.utc),
        **actor_fields(auth),
    )
    db.add(edge)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _err(409, "edge_exists", "This edge already exists.")
    await db.refresh(edge)
    return svc.edge_view(edge)


@router.delete("/{project_id}/artifact-edges/{edge_id}", status_code=204)
async def unlink_artifacts(
    project_id: str,
    edge_id: str,
    auth: AuthContext = Depends(require_scope("artifacts:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Remove a lineage edge (project editors; plan-gated)."""
    project = await _artifact_write(project_id, db, auth, ctx)
    edge = await db.get(ArtifactEdge, edge_id)
    if edge is None or edge.project_id != project.id:
        raise nf()
    await db.execute(delete(ArtifactEdge).where(ArtifactEdge.id == edge.id))
    await db.commit()
    return Response(status_code=204)


# ── Families ──


@router.get("/{project_id}/artifact-families")
async def list_families(
    project_id: str,
    kind: str | None = Query(default=None, max_length=20),
    auth: AuthContext = Depends(require_scope("artifacts:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Every (kind, name) family with its version count, newest version and
    production version."""
    project = await project_for_read(project_id, db, auth)
    stmt = (
        select(Artifact.kind, Artifact.name, func.count(), func.max(Artifact.created_at))
        .where(Artifact.project_id == project.id)
        .group_by(Artifact.kind, Artifact.name)
        .order_by(Artifact.kind, Artifact.name)
        .limit(1000)
    )
    if kind:
        stmt = stmt.where(Artifact.kind == kind)
    groups = (await db.execute(stmt)).all()
    prod = {
        (k, n): (aid, ver)
        for k, n, aid, ver in (
            await db.execute(
                select(Artifact.kind, Artifact.name, Artifact.id, Artifact.version).where(
                    Artifact.project_id == project.id, Artifact.stage == "production"
                )
            )
        ).all()
    }
    return {
        "families": [
            {
                "kind": k,
                "name": n,
                "versions": count,
                "latest_created_at": latest.isoformat() if latest else None,
                "production_artifact_id": prod.get((k, n), (None, None))[0],
                "production_version": prod.get((k, n), (None, None))[1],
            }
            for k, n, count, latest in groups
        ]
    }


@router.get("/{project_id}/artifact-families/{kind}/{name}/requirements")
async def get_requirements(
    project_id: str,
    kind: Kind,
    name: str,
    auth: AuthContext = Depends(require_scope("artifacts:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project = await project_for_read(project_id, db, auth)
    row = await svc.load_requirement(db, project.id, kind, name)
    return svc.requirement_view(row, kind, name)


@router.put("/{project_id}/artifact-families/{kind}/{name}/requirements")
async def put_requirements(
    project_id: str,
    kind: Kind,
    name: str,
    body: RequirementsBody,
    auth: AuthContext = Depends(require_scope("artifacts:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Set which suites must pass before a version of this family is
    promoted, and which tags its ``trained_on`` parents must carry. People
    only (project admin, user key); replaces the stored requirement."""
    project = await project_for_read(project_id, db, auth)
    if not name or len(name) > 120:
        raise nf()
    await require_admin(db, auth, project, "Setting promotion requirements")
    await gated_write(
        db, auth, ctx, project, admin_what="Setting promotion requirements"
    )
    # The family name and the required tag values are stored text: DLP them
    # under the same strictest-union, fail-closed scanner as artifact names
    # and tag values (registration scans those the same way, so a redacted
    # family name still matches its artifacts).
    scan = await dlp_scanner(db, auth, project)
    name = clean_line(scan(name) or "", 120)
    if not name:
        raise nf()
    require_tags = {
        key: clean_line(scan(value) or "", 256) for key, value in body.require_tags.items()
    }
    lineages: list[str] = []
    for ref in body.suites:
        suite = await resolve_suite(db, project, ref)
        if suite.lineage_id not in lineages:
            lineages.append(suite.lineage_id)
    row = await svc.load_requirement(db, project.id, kind, name)
    now = datetime.now(timezone.utc)
    if row is None:
        row = ArtifactFamilyRequirement(
            id=f"afr_{uuid.uuid4().hex[:16]}",
            project_id=project.id,
            kind=kind,
            name=name[:120],
        )
        db.add(row)
    row.suite_lineage_ids = json.dumps(lineages)
    row.require_tags = json.dumps(require_tags, sort_keys=True)
    row.updated_by_user_id = auth.user.id
    row.updated_at = now
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise _err(409, "requirements_changed", "Another save landed first; read the requirements again.")
    await db.refresh(row)
    return svc.requirement_view(row, kind, name)


@router.get("/{project_id}/artifact-families/{kind}/{name}/evals")
async def family_matrix(
    project_id: str,
    kind: Kind,
    name: str,
    auth: AuthContext = Depends(require_scope("artifacts:read", "evals:read")),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Versions (newest first, ≤ 50) × suite lineages: each cell is the
    latest trusted finished run's verdict, pass rate and id."""
    project = await project_for_read(project_id, db, auth)
    versions = list(
        (
            await db.execute(
                select(Artifact)
                .where(svc.family_clause(project.id, kind, name))
                .order_by(Artifact.created_at.desc(), Artifact.id.desc())
                .limit(_MATRIX_VERSIONS)
            )
        ).scalars().all()
    )
    req = svc.requirement_view(await svc.load_requirement(db, project.id, kind, name), kind, name)
    latest = await svc.latest_trusted_runs(db, [v.id for v in versions])
    lineages = list(req["suites"])
    for _aid, lin in sorted(latest):
        if lin not in lineages:
            lineages.append(lin)
    suites: dict[str, Any] = {}
    if lineages:
        for s in (
            await db.execute(
                select(EvalSuite)
                .where(EvalSuite.lineage_id.in_(lineages))
                .order_by(EvalSuite.version.desc())
            )
        ).scalars().all():
            suites.setdefault(s.lineage_id, s)
    rows = []
    for v in versions:
        cells: dict[str, Any] = {}
        for lin in lineages:
            run = latest.get((v.id, lin))
            if run is None:
                cells[lin] = None
                continue
            summary = ev.load_json(run.summary, {}) or {}
            cells[lin] = {"run_id": run.id, "verdict": run.verdict, "pass_rate": summary.get("pass_rate")}
        rows.append({"artifact_id": v.id, "version": v.version, "stage": v.stage, "deprecated": v.deprecated_at is not None, "cells": cells})
    return {
        "kind": kind,
        "name": name,
        "columns": [
            {
                "suite_lineage_id": lin,
                "slug": suites[lin].slug if lin in suites else None,
                "name": suites[lin].name if lin in suites else None,
                "required": lin in req["suites"],
            }
            for lin in lineages
        ],
        "rows": rows,
    }


__all__ = ["router"]
