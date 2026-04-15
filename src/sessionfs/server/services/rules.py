"""Rules service — load canonical rules, filter knowledge, invoke compilers.

This sits between the API/CLI and the per-tool compilers. It handles:
- ProjectRules lookup / create
- Filtering knowledge entries to only *active* claims
- Extracting context sections from the project's context_document
- Writing rules_versions only when outputs materially change
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import case, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    KnowledgeEntry,
    Project,
    ProjectRules,
    RulesVersion,
)
from sessionfs.server.services.rules_compiler import (
    COMPILERS,
    SUPPORTED_TOOLS,
    CompileContext,
    CompileResult,
    aggregate_hash,
)
from sessionfs.server.services.rules_compiler.base import KnowledgeClaim

# Knowledge types allowed by default in the injected block. The rule is:
# `convention` + `decision` are factual/durable; bug/discovery/note are
# ephemeral and stay out of compiled rule files.
DEFAULT_KNOWLEDGE_TYPES = ("convention", "decision")
ALLOWED_KNOWLEDGE_TYPES = (
    "convention",
    "decision",
    "pattern",
    "dependency",
)
DEFAULT_CONTEXT_SECTIONS = ("overview", "architecture")

# Freshness classes considered "active" by the compiler. Anything outside
# these should never be injected.
ACTIVE_FRESHNESS = {"current", "aging"}

# A section heading regex for splitting the project context_document into
# keyed sections. Matches "## Overview", "## Architecture", etc.
_H2_RE = re.compile(r"^##\s+(?P<title>.+?)\s*$", re.MULTILINE)


@dataclass
class CompileOutcome:
    """What compile() returned."""

    results: list[CompileResult]
    aggregate_hash: str
    created_version: RulesVersion | None  # None when no content change
    knowledge_used: list[KnowledgeClaim]
    context_used: dict[str, str]


def _json_loads(raw: str | None, default):
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return default


async def get_or_create_rules(
    db: AsyncSession, project: Project, actor_user_id: str
) -> ProjectRules:
    """Return the project's rules record, creating a default one if missing.

    First-request creation is raceable: ``project_rules.project_id`` has a
    unique constraint, so two concurrent readers can both miss the SELECT
    and both try to INSERT — one wins, the other hits IntegrityError. We
    catch the collision and re-read so both callers return the winner's row.
    """
    result = await db.execute(
        select(ProjectRules).where(ProjectRules.project_id == project.id)
    )
    rules = result.scalar_one_or_none()
    if rules is not None:
        return rules

    rules = ProjectRules(
        id=f"rules_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        version=1,
        static_rules="",
        include_knowledge=True,
        knowledge_types=json.dumps(list(DEFAULT_KNOWLEDGE_TYPES)),
        knowledge_max_tokens=1500,
        include_context=True,
        context_sections=json.dumps(list(DEFAULT_CONTEXT_SECTIONS)),
        context_max_tokens=1500,
        tool_overrides="{}",
        enabled_tools="[]",
        created_by=actor_user_id,
    )
    db.add(rules)
    try:
        await db.commit()
    except IntegrityError:
        # Lost the race — another caller already inserted the default row.
        # Rollback and re-read; both parties get the same (winner's) result.
        await db.rollback()
        result = await db.execute(
            select(ProjectRules).where(ProjectRules.project_id == project.id)
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            # Extremely unlikely (integrity error without a live row). Re-raise
            # rather than loop: something else is wrong with the schema/state.
            raise
        return existing
    await db.refresh(rules)
    return rules


def compute_etag(rules: ProjectRules) -> str:
    """Deterministic ETag over the fields the client may edit."""
    h = hashlib.sha256()
    h.update(f"v={rules.version}|".encode())
    h.update((rules.static_rules or "").encode())
    h.update(b"|")
    h.update((rules.knowledge_types or "").encode())
    h.update(b"|")
    h.update((rules.context_sections or "").encode())
    h.update(b"|")
    h.update((rules.tool_overrides or "").encode())
    h.update(b"|")
    h.update((rules.enabled_tools or "").encode())
    h.update(b"|")
    h.update(str(rules.include_knowledge).encode())
    h.update(str(rules.include_context).encode())
    h.update(str(rules.knowledge_max_tokens).encode())
    h.update(str(rules.context_max_tokens).encode())
    updated = rules.updated_at.isoformat() if rules.updated_at else ""
    h.update(updated.encode())
    return f'W/"{h.hexdigest()[:32]}"'


def split_context_sections(document: str) -> dict[str, str]:
    """Split a markdown doc into {slug: body} using `## Heading` boundaries.

    Slugs are lowercase heading text with non-alphanumerics collapsed to `_`.
    """
    if not document:
        return {}
    sections: dict[str, str] = {}
    matches = list(_H2_RE.finditer(document))
    if not matches:
        return {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(document)
        raw_title = m.group("title").strip().lower()
        slug = re.sub(r"[^a-z0-9]+", "_", raw_title).strip("_")
        body = document[start:end].strip()
        # Skip HTML-comment-only placeholder bodies
        stripped = re.sub(r"<!--.*?-->", "", body, flags=re.DOTALL).strip()
        if not stripped:
            continue
        sections[slug] = body
    return sections


async def _collect_active_claims(
    db: AsyncSession,
    project_id: str,
    wanted_types: list[str],
    limit: int = 40,
) -> list[KnowledgeClaim]:
    """Pull active (non-dismissed, non-superseded, fresh) claims of wanted types.

    Falls back to *all* allowed types if the requested intersection is empty.
    """
    types = [t for t in wanted_types if t in ALLOWED_KNOWLEDGE_TYPES]
    if not types:
        types = list(DEFAULT_KNOWLEDGE_TYPES)

    # Type priority must be applied IN SQL — applying it after a Python
    # LIMIT lets older high-priority claims (decisions) get dropped behind
    # newer low-priority ones (conventions). The ORDER BY: priority ASC
    # (lower = more important), then created_at DESC newest-first within a
    # tier, then id DESC as a stable tie-breaker on identical timestamps.
    priority_expr = case(
        (KnowledgeEntry.entry_type == "decision", 0),
        (KnowledgeEntry.entry_type == "convention", 1),
        (KnowledgeEntry.entry_type == "pattern", 2),
        (KnowledgeEntry.entry_type == "dependency", 3),
        else_=99,
    )
    stmt = (
        select(KnowledgeEntry)
        .where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.entry_type.in_(types),
            KnowledgeEntry.dismissed == False,  # noqa: E712
            KnowledgeEntry.superseded_by.is_(None),
            KnowledgeEntry.claim_class == "claim",
            KnowledgeEntry.freshness_class.in_(list(ACTIVE_FRESHNESS)),
        )
        .order_by(
            priority_expr.asc(),
            KnowledgeEntry.created_at.desc(),
            KnowledgeEntry.id.desc(),
        )
        .limit(limit)
    )
    result = await db.execute(stmt)
    entries = list(result.scalars().all())
    return [
        KnowledgeClaim(
            entry_type=e.entry_type,
            content=e.content,
            entity_ref=e.entity_ref,
        )
        for e in entries
    ]


def _filter_context(sections: dict[str, str], wanted: list[str]) -> dict[str, str]:
    if not wanted:
        return sections
    out: dict[str, str] = {}
    for slug in wanted:
        if slug in sections:
            out[slug] = sections[slug]
    return out


async def build_compile_context(
    db: AsyncSession, project: Project, rules: ProjectRules
) -> CompileContext:
    """Assemble a CompileContext from persistent state."""
    knowledge_claims: list[KnowledgeClaim] = []
    if rules.include_knowledge:
        wanted_types = _json_loads(
            rules.knowledge_types, list(DEFAULT_KNOWLEDGE_TYPES)
        )
        knowledge_claims = await _collect_active_claims(
            db, project.id, wanted_types
        )

    context_sections: dict[str, str] = {}
    if rules.include_context:
        wanted = _json_loads(
            rules.context_sections, list(DEFAULT_CONTEXT_SECTIONS)
        )
        all_sections = split_context_sections(project.context_document or "")
        context_sections = _filter_context(all_sections, wanted)

    tool_overrides = _json_loads(rules.tool_overrides, {}) or {}

    return CompileContext(
        static_rules=rules.static_rules or "",
        knowledge_claims=knowledge_claims,
        context_sections=context_sections,
        tool_overrides=tool_overrides,
        version=rules.version,
    )


def compile_for_tools(
    ctx: CompileContext, tools: list[str]
) -> list[CompileResult]:
    """Run each tool's compiler over a shared CompileContext."""
    results: list[CompileResult] = []
    for tool in tools:
        if tool not in COMPILERS:
            continue
        results.append(COMPILERS[tool].compile(ctx))
    return results


async def compile_rules(
    db: AsyncSession,
    project: Project,
    rules: ProjectRules,
    actor_user_id: str,
    tools_override: list[str] | None = None,
    persist_version: bool = True,
) -> CompileOutcome:
    """Compile the current canonical rules.

    A new ``rules_versions`` row is persisted only when:
      1. ``persist_version`` is True (the default), AND
      2. ``tools_override`` is not a partial subset of the canonical
         ``enabled_tools`` (partial compiles never version — see below), AND
      3. the aggregate body hash differs from the latest stored version.

    The body hash excludes the managed marker, so version-number changes in
    the marker cannot retrigger a spurious no-op failure.

    Concurrency: the RulesVersion insert is wrapped in an idempotent retry
    loop; on unique-constraint collision we re-read the latest version and
    either return a no-op (if someone else's compile produced the same
    body hash) or bump past the winner (if it produced something different).
    """
    canonical_enabled = _json_loads(rules.enabled_tools, [])
    canonical_enabled = [t for t in canonical_enabled if t in SUPPORTED_TOOLS]

    if tools_override is not None:
        compile_tools = [t for t in tools_override if t in SUPPORTED_TOOLS]
        # Any explicit tools_override makes this a partial compile — including
        # the case where the override happens to equal canonical_enabled. The
        # rationale: callers passing `tools=[...]` are doing targeted work
        # (resume-time materialisation, `sfs rules compile --tool X`, preview),
        # never a canonical "I want history to advance" compile. The no-version
        # guarantee stays firm regardless of which tool set the override holds.
        is_partial = True
    else:
        compile_tools = canonical_enabled
        is_partial = False

    ctx = await build_compile_context(db, project, rules)
    results = compile_for_tools(ctx, compile_tools)
    agg = aggregate_hash(results) if results else hashlib.sha256(b"").hexdigest()

    if not persist_version or is_partial:
        return CompileOutcome(
            results=results,
            aggregate_hash=agg,
            created_version=None,
            knowledge_used=ctx.knowledge_claims,
            context_used=ctx.context_sections,
        )

    # Idempotent insert with retry on unique-constraint collision. A concurrent
    # compile racing on the same project will either produce the same body hash
    # (we short-circuit to no-op) or a different one (we bump past the winner).
    for attempt in range(5):
        existing = await db.execute(
            select(RulesVersion)
            .where(RulesVersion.rules_id == rules.id)
            .order_by(RulesVersion.version.desc())
            .limit(1)
        )
        latest: RulesVersion | None = existing.scalar_one_or_none()

        if latest is not None and latest.content_hash == agg:
            # No-op short-circuit. In a race-loser path, the winner already
            # committed a new version with the same body hash; our local
            # rules.version (and the rendered results' managed markers) are
            # stale. Align to the committed winner's version and re-render
            # before returning so the caller sees canonical version/content.
            if rules.version != latest.version:
                rules.version = latest.version
                ctx.version = latest.version
                results = compile_for_tools(ctx, compile_tools)
            return CompileOutcome(
                results=results,
                aggregate_hash=agg,
                created_version=None,
                knowledge_used=ctx.knowledge_claims,
                context_used=ctx.context_sections,
            )

        new_version = (latest.version + 1) if latest else 1

        compiled_outputs = {
            r.tool: {
                "filename": r.filename,
                "content": r.content,
                "hash": r.content_hash,
                "token_count": r.token_count,
            }
            for r in results
        }
        knowledge_snapshot = [
            {
                "entry_type": c.entry_type,
                "content": c.content,
                "entity_ref": c.entity_ref,
            }
            for c in ctx.knowledge_claims
        ]

        rv = RulesVersion(
            id=f"rv_{uuid.uuid4().hex[:16]}",
            rules_id=rules.id,
            version=new_version,
            static_rules=rules.static_rules or "",
            compiled_outputs=json.dumps(compiled_outputs),
            knowledge_snapshot=json.dumps(knowledge_snapshot),
            context_snapshot=json.dumps(ctx.context_sections),
            compiled_at=datetime.now(timezone.utc),
            compiled_by=actor_user_id,
            content_hash=agg,
        )
        db.add(rv)

        rules.version = new_version
        rules.updated_at = datetime.now(timezone.utc)
        # The marker embeds rules.version; re-render so the on-disk content
        # reflects the new version. content_hash is marker-independent so agg
        # stays valid across this re-render.
        ctx.version = new_version
        results = compile_for_tools(ctx, compile_tools)
        compiled_outputs = {
            r.tool: {
                "filename": r.filename,
                "content": r.content,
                "hash": r.content_hash,
                "token_count": r.token_count,
            }
            for r in results
        }
        rv.compiled_outputs = json.dumps(compiled_outputs)

        try:
            await db.commit()
        except IntegrityError:
            # Another request won the race for this version number. Roll
            # back and retry — the next loop iteration will re-read the
            # (now-newer) latest and either short-circuit or bump past it.
            await db.rollback()
            continue

        await db.refresh(rv)
        return CompileOutcome(
            results=results,
            aggregate_hash=agg,
            created_version=rv,
            knowledge_used=ctx.knowledge_claims,
            context_used=ctx.context_sections,
        )

    # Should be unreachable in practice — 5 retries covers any realistic
    # contention window. Surface as a conflict so the client can retry.
    raise RuntimeError("compile_rules: exhausted retries on version contention")
