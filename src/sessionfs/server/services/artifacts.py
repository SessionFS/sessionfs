"""Artifact pointers, lineage and the v1 promotion gate (GitHub #122).

Record-only: an artifact is a pointer (URI, digest, schema summary, tags).
SessionFS validates the URI's shape and never fetches it. Lineage edges stay
inside one project. Promotion to staging or production requires a trusted
``passed`` eval run on this exact artifact version for every required suite
of the family (``artifact_family_requirements``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.artifact_uri import (  # noqa: F401  (re-exported)
    ALLOWED_SCHEMES,
    MAX_URI,
    ArtifactUriError,
    link_url,
    validate_uri,
)
from sessionfs.server.db.models import (
    Artifact,
    ArtifactEdge,
    ArtifactFamilyRequirement,
    ArtifactTag,
    EvalRun,
    EvalSuite,
)

MAX_TAGS = 32
MAX_PARENTS = 200
MAX_ARTIFACTS_PER_PROJECT = 10_000
MAX_DEPTH = 25
MAX_NODES = 500
MAX_SCHEMA_SUMMARY_BYTES = 16 * 1024

TAG_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:-]{0,63}$")

# Stage moves people may make. Gated targets need the eval gate.
_ALLOWED_FROM: dict[str, frozenset[str]] = {
    "staging": frozenset({"candidate"}),
    "production": frozenset({"candidate", "staging"}),
    "archived": frozenset({"candidate", "staging", "production"}),
    "candidate": frozenset({"staging", "archived"}),
}
GATED_STAGES = frozenset({"staging", "production"})


# ── Views ──


def tags_by_artifact(rows: Iterable[ArtifactTag]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        out.setdefault(row.artifact_id, {})[row.key] = row.value
    return out


async def load_tags(db: AsyncSession, artifact_ids: list[str]) -> dict[str, dict[str, str]]:
    if not artifact_ids:
        return {}
    rows = (
        await db.execute(select(ArtifactTag).where(ArtifactTag.artifact_id.in_(artifact_ids)))
    ).scalars().all()
    return tags_by_artifact(rows)


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def artifact_view(row: Artifact, tags: dict[str, str] | None = None) -> dict[str, Any]:
    schema: Any = None
    if row.schema_summary:
        try:
            schema = json.loads(row.schema_summary)
        except ValueError:
            schema = None
    return {
        "id": row.id,
        "project_id": row.project_id,
        "kind": row.kind,
        "name": row.name,
        "version": row.version,
        "external_uri": row.external_uri,
        "link_url": link_url(row.external_uri),
        "digest": row.digest,
        "schema_summary": schema,
        "description": row.description or "",
        "stage": row.stage,
        "produced_by_agent_run_id": row.produced_by_agent_run_id,
        "tags": dict(sorted((tags or {}).items())),
        "created_by_user_id": row.created_by_user_id,
        "actor_type": row.actor_type,
        "service_key_name": row.service_key_name,
        "created_at": _iso(row.created_at),
        "deprecated_at": _iso(row.deprecated_at),
    }


def edge_view(row: ArtifactEdge) -> dict[str, Any]:
    return {
        "id": row.id,
        "parent_id": row.parent_id,
        "child_id": row.child_id,
        "relation": row.relation,
        "context_ref": row.context_ref,
        "actor_type": row.actor_type,
        "service_key_name": row.service_key_name,
        "created_at": _iso(row.created_at),
    }


# ── Lineage ──


class LineageTooDeep(Exception):
    pass


async def would_create_cycle(db: AsyncSession, parent_id: str, child_id: str) -> bool:
    """True when ``parent -> child`` would close a cycle: ``parent`` is
    already reachable from ``child`` going down. Bounded at ``MAX_DEPTH``
    levels and ``MAX_NODES`` nodes; past either bound raises
    ``LineageTooDeep`` (the caller refuses, failing closed). Run under the
    project scope lock so two concurrent links can't close a cycle together.
    """
    if parent_id == child_id:
        return True
    seen = {child_id}
    frontier = [child_id]
    for _depth in range(MAX_DEPTH):
        if not frontier:
            return False
        children = (
            await db.execute(
                select(ArtifactEdge.child_id).where(ArtifactEdge.parent_id.in_(frontier))
            )
        ).scalars().all()
        nxt: list[str] = []
        for c in children:
            if c == parent_id:
                return True
            if c not in seen:
                seen.add(c)
                nxt.append(c)
        if len(seen) > MAX_NODES:
            raise LineageTooDeep()
        frontier = nxt
    if frontier:
        raise LineageTooDeep()
    return False


@dataclass
class LineageGraph:
    node_ids: list[str] = field(default_factory=list)
    edges: list[ArtifactEdge] = field(default_factory=list)
    truncated: bool = False


async def trace_lineage(
    db: AsyncSession, project_id: str, root_id: str, *, direction: str, depth: int
) -> LineageGraph:
    """Bounded BFS from ``root_id`` (up = parents, down = children, both).
    Stops at ``MAX_NODES`` nodes (``truncated``)."""
    depth = max(1, min(depth, MAX_DEPTH))
    graph = LineageGraph(node_ids=[root_id])
    seen = {root_id}
    edge_ids: set[str] = set()
    frontier: list[str] = [root_id]
    level = 0
    while frontier and level < depth:
        conds = []
        if direction in ("up", "both"):
            conds.append(ArtifactEdge.child_id.in_(frontier))
        if direction in ("down", "both"):
            conds.append(ArtifactEdge.parent_id.in_(frontier))
        rows = (
            await db.execute(
                select(ArtifactEdge)
                .where(ArtifactEdge.project_id == project_id, or_(*conds))
                .order_by(ArtifactEdge.created_at, ArtifactEdge.id)
            )
        ).scalars().all()
        front_set = set(frontier)
        nxt: list[str] = []
        for edge in rows:
            ends = []
            if direction in ("up", "both") and edge.child_id in front_set:
                ends.append(edge.parent_id)
            if direction in ("down", "both") and edge.parent_id in front_set:
                ends.append(edge.child_id)
            new_ends = [e for e in ends if e not in seen]
            if len(seen) + len(new_ends) > MAX_NODES:
                graph.truncated = True
                break
            if edge.id not in edge_ids:
                edge_ids.add(edge.id)
                graph.edges.append(edge)
            for e in new_ends:
                seen.add(e)
                graph.node_ids.append(e)
                nxt.append(e)
        if graph.truncated:
            break
        frontier = nxt
        level += 1
    if frontier and level >= depth and not graph.truncated:
        # More levels exist beyond the requested depth only if a frontier
        # node still has an edge in the traced direction.
        conds = []
        if direction in ("up", "both"):
            conds.append(ArtifactEdge.child_id.in_(frontier))
        if direction in ("down", "both"):
            conds.append(ArtifactEdge.parent_id.in_(frontier))
        more = (
            await db.execute(select(ArtifactEdge.id).where(or_(*conds)).limit(1))
        ).scalar_one_or_none()
        graph.truncated = more is not None
    # Keep only edges whose both ends are in the returned node set.
    nodes = set(graph.node_ids)
    graph.edges = [e for e in graph.edges if e.parent_id in nodes and e.child_id in nodes]
    return graph


# Upper bound on rows ``latest_trusted_runs`` returns: one per (artifact,
# suite lineage). Callers pass at most MAX_NODES artifacts (lineage) or 50
# versions (family matrix); 20 suite lineages each is far beyond real use.
MAX_LATEST_RUNS = 10_000


async def latest_trusted_runs(
    db: AsyncSession, artifact_ids: list[str]
) -> dict[tuple[str, str], EvalRun]:
    """Latest trusted finished run per (artifact, suite lineage).

    Only the newest row of each (artifact_id, suite_lineage_id) partition is
    loaded (``row_number()`` window, SQLite and PostgreSQL), never the whole
    run history, and the result is bounded by ``MAX_LATEST_RUNS``."""
    if not artifact_ids:
        return {}
    ids = list(dict.fromkeys(artifact_ids))[: MAX_NODES * 2]
    ranked = (
        select(
            EvalRun.id.label("id"),
            func.row_number()
            .over(
                partition_by=(EvalRun.artifact_id, EvalRun.suite_lineage_id),
                order_by=(EvalRun.finished_at.desc(), EvalRun.id.desc()),
            )
            .label("rn"),
        )
        .where(
            EvalRun.artifact_id.in_(ids),
            EvalRun.state == "finished",
            EvalRun.trusted.is_(True),
        )
        .subquery()
    )
    rows = (
        await db.execute(
            select(EvalRun)
            .join(ranked, EvalRun.id == ranked.c.id)
            .where(ranked.c.rn == 1)
            .order_by(EvalRun.artifact_id, EvalRun.suite_lineage_id)
            .limit(MAX_LATEST_RUNS)
        )
    ).scalars().all()
    out: dict[tuple[str, str], EvalRun] = {}
    for run in rows:
        out.setdefault((run.artifact_id or "", run.suite_lineage_id), run)
    return out


# ── Requirements and promotion ──


def requirement_view(row: ArtifactFamilyRequirement | None, kind: str, name: str) -> dict[str, Any]:
    suites: list[str] = []
    tags: dict[str, str] = {}
    if row is not None:
        try:
            suites = [s for s in json.loads(row.suite_lineage_ids or "[]") if isinstance(s, str)]
        except ValueError:
            suites = []
        try:
            raw = json.loads(row.require_tags or "{}")
            tags = {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}
        except ValueError:
            tags = {}
    return {
        "kind": kind,
        "name": name,
        "suites": suites,
        "require_tags": tags,
        "updated_by_user_id": row.updated_by_user_id if row else None,
        "updated_at": _iso(row.updated_at) if row else None,
    }


async def load_requirement(
    db: AsyncSession, project_id: str, kind: str, name: str
) -> ArtifactFamilyRequirement | None:
    return (
        await db.execute(
            select(ArtifactFamilyRequirement).where(
                ArtifactFamilyRequirement.project_id == project_id,
                ArtifactFamilyRequirement.kind == kind,
                ArtifactFamilyRequirement.name == name,
            )
        )
    ).scalar_one_or_none()


async def promotion_blockers(
    db: AsyncSession, artifact: Artifact, to_stage: str
) -> list[dict[str, Any]]:
    """Why ``artifact`` cannot move to ``to_stage`` (empty = allowed).

    Staging and production need, for every required suite lineage of the
    family, a finished, trusted, ``passed`` run on THIS artifact version
    (voided and expired runs never count), and every required tag on each
    ``trained_on`` parent. A family with required tags but no ``trained_on``
    parent is blocked (fail closed)."""
    reasons: list[dict[str, Any]] = []
    if artifact.deprecated_at is not None:
        reasons.append({"code": "artifact_deprecated", "message": "A deprecated artifact cannot be promoted."})
    if to_stage == artifact.stage:
        reasons.append({"code": "same_stage", "message": f"The artifact is already in {to_stage}."})
        return reasons
    allowed = _ALLOWED_FROM.get(to_stage, frozenset())
    if artifact.stage not in allowed:
        reasons.append(
            {
                "code": "invalid_transition",
                "message": f"Cannot move from {artifact.stage} to {to_stage}.",
            }
        )
        return reasons
    if to_stage not in GATED_STAGES:
        return reasons

    req = requirement_view(
        await load_requirement(db, artifact.project_id, artifact.kind, artifact.name),
        artifact.kind,
        artifact.name,
    )
    for lineage_id in req["suites"]:
        passed = (
            await db.execute(
                select(EvalRun.id)
                .where(
                    EvalRun.project_id == artifact.project_id,
                    EvalRun.suite_lineage_id == lineage_id,
                    EvalRun.artifact_id == artifact.id,
                    EvalRun.state == "finished",
                    EvalRun.verdict == "passed",
                    EvalRun.trusted.is_(True),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if passed is None:
            suite = (
                await db.execute(
                    select(EvalSuite.slug, EvalSuite.name)
                    .where(EvalSuite.lineage_id == lineage_id)
                    .order_by(EvalSuite.version.desc())
                    .limit(1)
                )
            ).first()
            reasons.append(
                {
                    "code": "suite_not_passed",
                    "suite_lineage_id": lineage_id,
                    "suite_slug": suite[0] if suite else None,
                    "message": (
                        f"No trusted passing run of {suite[1] if suite else lineage_id} "
                        f"on {artifact.name} {artifact.version}."
                    ),
                }
            )
    if req["require_tags"]:
        parent_ids = list(
            (
                await db.execute(
                    select(ArtifactEdge.parent_id).where(
                        ArtifactEdge.child_id == artifact.id,
                        ArtifactEdge.relation == "trained_on",
                    )
                )
            ).scalars().all()
        )
        if not parent_ids:
            reasons.append(
                {
                    "code": "no_trained_on_parents",
                    "message": "The family requires tags on trained_on parents, and this version has none.",
                }
            )
        else:
            tags = await load_tags(db, parent_ids)
            for pid in parent_ids:
                have = tags.get(pid, {})
                for key, value in req["require_tags"].items():
                    if have.get(key) != value:
                        reasons.append(
                            {
                                "code": "parent_tag_missing",
                                "parent_id": pid,
                                "tag": key,
                                "required_value": value,
                                "message": f"trained_on parent {pid} needs tag {key}={value}.",
                            }
                        )
    return reasons


async def count_project_artifacts(db: AsyncSession, project_id: str) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(Artifact).where(Artifact.project_id == project_id)
        )
    ).scalar_one()


async def count_parents(db: AsyncSession, child_id: str) -> int:
    return (
        await db.execute(
            select(func.count()).select_from(ArtifactEdge).where(ArtifactEdge.child_id == child_id)
        )
    ).scalar_one()


def family_clause(project_id: str, kind: str, name: str):
    return and_(Artifact.project_id == project_id, Artifact.kind == kind, Artifact.name == name)
