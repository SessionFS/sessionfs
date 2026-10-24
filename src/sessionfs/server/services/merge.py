"""Project merge engine (§5.11–§5.12).

Atomic, dry-run-first, audit-logged merge of one project into another.
Follows the per-table merge matrix order exactly. The merge transaction
is a single `async with db.begin()` block — any failure rolls back
completely and both projects are untouched.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionfs.server.db.models import (
    AgentFeedback,
    AgentPersona,
    AgentRun,
    Handoff,
    ContextCompilation,
    HandoffAttachment,
    KnowledgeEntry,
    KnowledgeLink,
    KnowledgePage,
    Project,
    ProjectFeatureGrandfather,
    ProjectMergeAudit,
    ProjectRepo,
    ProjectRules,
    ProjectTransfer,
    RetrievalAuditContext,
    RetrievalAuditEvent,
    Session,
    Ticket,
    User,
    WikiPageRevision,
)

logger = logging.getLogger("sessionfs.server.services.merge")

# ── constants ──────────────────────────────────────────────────────
_MAX_PERSONA_NAME = 50
_SOURCE_SUFFIX_LEN = 8  # first 8 chars of source project id
_PERSONA_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,50}$")

# Tables that get straight reassign (project_id column → target_id).
_STRAIGHT_REASSIGN_MODELS = [
    Ticket,
    AgentRun,
    AgentFeedback,
    Session,
    HandoffAttachment,
    ProjectTransfer,
    ContextCompilation,
    RetrievalAuditContext,
    RetrievalAuditEvent,
]


# ── helpers ────────────────────────────────────────────────────────

def _legal_rename(
    base_name: str,
    suffix: str,
    seen: set[str],
    *,
    case_insensitive: bool = False,
) -> str:
    """Produce a legal, unique, ≤50-char rename slug.

    Truncates base if needed so f"{truncated}-{suffix}" ≤ 50 chars.
    If the result collides with an existing name in *seen*, appends an
    incrementing counter.  Callers MUST add the returned name to *seen*
    before the next call so in-flight rename-rename collisions are
    prevented.

    `case_insensitive=True` (persona names, GH #70): *seen* holds
    lower-cased names and a candidate collides when its lower-case form is
    in it. Wiki slugs keep exact matching.
    """
    def _taken(name: str) -> bool:
        return (name.lower() if case_insensitive else name) in seen

    def _build(tail: str) -> str:
        # Build the full tail first, then truncate the base so the result
        # never exceeds the 50-char column, whatever the counter width.
        return f"{base_name[:max(_MAX_PERSONA_NAME - len(tail), 0)]}{tail}"[
            :_MAX_PERSONA_NAME
        ]

    attempt = _build(f"-{suffix}")
    i = 1
    while _taken(attempt):
        attempt = _build(f"-{suffix}-{i}")
        i += 1
    return attempt


def _normalize_content(text: str | None) -> str:
    """Whitespace-normalize content for exact-match dedup."""
    if text is None:
        return ""
    return " ".join(text.split())


# ── precondition helpers ───────────────────────────────────────────

async def _validate_preconditions(
    db: AsyncSession,
    source_id: str,
    target_id: str,
) -> tuple[Project, Project]:
    """Raise HTTPException on any precondition failure.

    Returns (source, target) projects on success.  These rejections are
    NOT merge-audited (§5.11 Phase 1).
    """
    from fastapi import HTTPException

    source = await db.get(Project, source_id)
    target = await db.get(Project, target_id)
    if not source:
        raise HTTPException(404, "Source project not found")
    if not target:
        raise HTTPException(404, "Target project not found")

    # Cross-org / personal-mix check.
    if source.org_id != target.org_id:
        raise HTTPException(
            400,
            "Cross-org merges are not supported. "
            "Use project transfer to move one project into the other's org first.",
        )

    # Neither already merged.
    if source.merged_into_project_id is not None:
        raise HTTPException(400, "Source project was already merged")
    if target.merged_into_project_id is not None:
        raise HTTPException(400, "Target project was already merged")

    # Sentinel L3: block if either project has a pending transfer.
    for side, proj in [("source", source), ("target", target)]:
        pending = (
            await db.execute(
                select(ProjectTransfer).where(
                    ProjectTransfer.project_id == proj.id,
                    ProjectTransfer.state == "pending",
                )
            )
        ).scalar_one_or_none()
        if pending:
            raise HTTPException(
                400,
                f"{side.capitalize()} project has a pending transfer. "
                "Resolve it first.",
            )

    return source, target


# ── collision detection (dry-run + pre-compute) ────────────────────

async def _org_persona_names(
    db: AsyncSession, org_id: str | None,
) -> tuple[set[str], set[str]]:
    """(lower-cased ACTIVE org persona names, lower-cased ALL org persona
    names) of `org_id`; both empty for a personal project."""
    if not org_id:
        return set(), set()
    rows = (
        await db.execute(
            select(AgentPersona.name, AgentPersona.is_active).where(
                AgentPersona.org_id == org_id
            )
        )
    ).all()
    return (
        {name.lower() for name, active in rows if active},
        {name.lower() for name, _ in rows},
    )


async def _source_live_persona_refs(
    db: AsyncSession, source_id: str, handoff_ids: list[str] | None,
) -> set[str]:
    """Distinct persona names the source's LIVE references carry — the
    same columns `_step_rewrite_persona_refs` rewrites. `handoff_ids`
    None: use the pending-handoff filter (pre-lock / dry-run planning)."""
    from sessionfs.server.services.personas import pending_handoffs_in_project

    handoff_scope = (
        pending_handoffs_in_project(source_id)
        if handoff_ids is None
        else (Handoff.status == "pending", Handoff.id.in_(handoff_ids))
    )
    refs: set[str] = set()
    for column, scope in (
        (Ticket.assigned_to, (Ticket.project_id == source_id,)),
        (AgentRun.persona_name, (AgentRun.project_id == source_id,)),
        (Handoff.persona_name, handoff_scope),
    ):
        refs.update(
            v
            for v in (
                await db.execute(
                    select(column).where(*scope, column.is_not(None)).distinct()
                )
            ).scalars().all()
            if v
        )
    return refs


async def _detect_org_rebinds(
    db: AsyncSession,
    source_id: str,
    target_id: str,
    org_id: str | None,
    handoff_ids: list[str] | None = None,
) -> list[AgentPersona]:
    """GH #70 — org personas the source's live references resolve to today
    that a target PROJECT persona of the same name would shadow after the
    move (the references would silently re-bind to the target's override).

    Returned sorted by name; the merge pins each one (a project copy under
    a fresh name in the target) and points the source references at it.
    """
    if not org_id:
        return []
    from sessionfs.server.services.personas import resolve_persona

    target_active = {
        n.lower()
        for n in (
            await db.execute(
                select(AgentPersona.name).where(
                    AgentPersona.project_id == target_id,
                    AgentPersona.is_active.is_(True),
                )
            )
        ).scalars().all()
    }
    if not target_active:
        return []
    pinned: dict[str, AgentPersona] = {}
    for ref in sorted(await _source_live_persona_refs(db, source_id, handoff_ids)):
        if ref.lower() not in target_active or ref.lower() in pinned:
            continue
        now = await resolve_persona(db, source_id, ref, org_id=org_id)
        if now is not None and now.org_id is not None:
            pinned[ref.lower()] = now
    return [pinned[k] for k in sorted(pinned)]


async def _detect_persona_collisions(
    db: AsyncSession,
    source_id: str,
    target_id: str,
    org_id: str | None = None,
    handoff_ids: list[str] | None = None,
) -> list[dict]:
    """Return list of {source_name} for colliding personas.

    Case-insensitive (GH #70): persona names are case-insensitive-unique
    per project, so `Atlas` in the source collides with `atlas` in the
    target. A second source persona that only differs in case from an
    earlier source persona (legacy rows) also collides once the first has
    moved.

    Org shadowing (GH #70, projects in an org): a project persona OVERRIDES
    the org persona of the same name, so the move can silently re-bind
    references in two ways — both are reported as collisions with a `kind`:

    * `shadows_org_persona` — a source persona overrides an org persona the
      target has no project persona for; moved as is, it would take over
      every target reference that resolves to the org persona today.
      The rename policy renames it (like a target collision).
    * `target_persona_shadows_org_persona` — source references resolve to
      an org persona today, but the target has an active project persona of
      that name; after the move they would bind to the target's override.
      The rename policy pins the org persona (a project copy under a fresh
      name) and points the source references at it.

    Only the rename policy keeps every reference resolving to the persona
    it resolves to today; `skip` / `merge_content` refuse to execute while
    an org-shadowing collision exists.
    """
    source_names = (
        await db.execute(
            select(AgentPersona.name)
            .where(AgentPersona.project_id == source_id)
            .order_by(AgentPersona.id)
        )
    ).scalars().all()
    taken = {
        n.lower()
        for n in (
            await db.execute(
                select(AgentPersona.name)
                .where(AgentPersona.project_id == target_id)
            )
        ).scalars().all()
    }
    org_active, _ = await _org_persona_names(db, org_id)
    collisions: list[dict] = []
    for name in source_names:
        if name.lower() in taken:
            collisions.append({"source_name": name})
        elif name.lower() in org_active:
            collisions.append({
                "source_name": name,
                "kind": "shadows_org_persona",
            })
        else:
            taken.add(name.lower())
    for org_persona in await _detect_org_rebinds(
        db, source_id, target_id, org_id, handoff_ids
    ):
        collisions.append({
            "source_name": org_persona.name,
            "kind": "target_persona_shadows_org_persona",
        })
    return collisions


def _org_shadow_collisions(collisions: list[dict]) -> list[dict]:
    return [c for c in collisions if c.get("kind")]


async def _detect_slug_collisions(
    db: AsyncSession, source_id: str, target_id: str,
) -> list[str]:
    """Return list of source slugs that collide with target slugs."""
    source_slugs = (
        await db.execute(
            select(KnowledgePage.slug)
            .where(KnowledgePage.project_id == source_id)
        )
    ).scalars().all()
    target_slugs = set(
        (
            await db.execute(
                select(KnowledgePage.slug)
                .where(KnowledgePage.project_id == target_id)
            )
        ).scalars().all()
    )
    return [s for s in source_slugs if s in target_slugs]


async def _detect_ke_duplicates(
    db: AsyncSession, source_id: str, target_id: str,
) -> list[dict]:
    """Return list of source KE ids + types that are exact duplicates in target."""
    target_entries = (
        await db.execute(
            select(KnowledgeEntry.entry_type, KnowledgeEntry.content, KnowledgeEntry.id)
            .where(KnowledgeEntry.project_id == target_id)
        )
    ).all()
    target_keys = {
        (e.entry_type, _normalize_content(e.content)): e.id
        for e in target_entries
    }
    source_entries = (
        await db.execute(
            select(KnowledgeEntry)
            .where(KnowledgeEntry.project_id == source_id)
        )
    ).scalars().all()
    dupes = []
    for entry in source_entries:
        key = (entry.entry_type, _normalize_content(entry.content))
        if key in target_keys:
            dupes.append({
                "source_id": entry.id,
                "entry_type": entry.entry_type,
                "target_equivalent_id": target_keys[key],
            })
    return dupes


async def _has_rules(db: AsyncSession, project_id: str) -> bool:
    row = (
        await db.execute(
            select(ProjectRules.id).where(ProjectRules.project_id == project_id)
        )
    ).scalar_one_or_none()
    return row is not None


async def _compute_merge_stats(
    db: AsyncSession,
    source_id: str,
    target_id: str,
    org_id: str | None = None,
) -> dict:
    """Count rows that will be moved / skipped."""
    stats: dict = {}

    for label, model in [
        ("repos", ProjectRepo),
        ("personas", AgentPersona),
        ("tickets", Ticket),
        ("agent_runs", AgentRun),
        ("sessions", Session),
        ("handoff_attachments", HandoffAttachment),
        ("project_transfers", ProjectTransfer),
        ("context_compilations", ContextCompilation),
        ("knowledge_entries", KnowledgeEntry),
        ("knowledge_links", KnowledgeLink),
        ("knowledge_pages", KnowledgePage),
        ("wiki_page_revisions", WikiPageRevision),
        ("retrieval_audit_contexts", RetrievalAuditContext),
        ("retrieval_audit_events", RetrievalAuditEvent),
    ]:
        count = (
            await db.execute(
                select(func.count()).select_from(model).where(
                    model.project_id == source_id
                )
            )
        ).scalar() or 0
        stats[label] = count

    # Persona collisions
    collisions = await _detect_persona_collisions(db, source_id, target_id, org_id)
    stats["persona_collisions"] = len(collisions)

    # Slug collisions
    slug_collisions = await _detect_slug_collisions(db, source_id, target_id)
    stats["slug_collisions"] = len(slug_collisions)

    # KE duplicates
    ke_dupes = await _detect_ke_duplicates(db, source_id, target_id)
    stats["ke_duplicates"] = len(ke_dupes)

    # Rules
    stats["source_has_rules"] = await _has_rules(db, source_id)
    stats["target_has_rules"] = await _has_rules(db, target_id)

    return stats


# ── step implementations (each corresponds to one merge-matrix row) ─

async def _step_repos(
    db: AsyncSession, source_id: str, target_id: str,
) -> None:
    """Step 1: Lock + demote source primary + reassign all source repos."""
    # Lock both projects' repos.
    await db.execute(
        select(ProjectRepo)
        .where(ProjectRepo.project_id.in_([source_id, target_id]))
        .with_for_update()
    )
    # Demote source primary.
    await db.execute(
        update(ProjectRepo)
        .where(
            ProjectRepo.project_id == source_id,
            ProjectRepo.is_primary == True,  # noqa: E712
        )
        .values(is_primary=False)
    )
    # Reassign all source repos.
    await db.execute(
        update(ProjectRepo)
        .where(ProjectRepo.project_id == source_id)
        .values(project_id=target_id)
    )
    # If target has no primary after reassign, promote oldest.
    target_primary = (
        await db.execute(
            select(ProjectRepo).where(
                ProjectRepo.project_id == target_id,
                ProjectRepo.is_primary == True,  # noqa: E712
            )
        )
    ).scalar_one_or_none()
    if not target_primary:
        oldest = (
            await db.execute(
                select(ProjectRepo)
                .where(ProjectRepo.project_id == target_id)
                .order_by(ProjectRepo.created_at.asc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if oldest:
            oldest.is_primary = True


async def _step_personas(
    db: AsyncSession,
    source_id: str,
    target_id: str,
    persona_policy: str,
    org_id: str | None = None,
    org_pins: list[AgentPersona] | None = None,
    user_id: str | None = None,
) -> list[dict]:
    """Step 2: Reassign ALL source personas, handling collisions per policy.

    Returns list of {old_name, new_name, display_note} for audit.
    Every source persona ends up on target with a unique name.

    GH #70 — in an org, a source persona whose name matches an ACTIVE org
    persona also collides (moved as is it would override the org persona
    for the target's references), and generated names avoid every org
    persona name. `org_pins` (rename policy only) are org personas the
    source's references must keep resolving to: each gets a project copy
    in the target under a fresh name (entry carries
    `pinned_from_org_persona_id`, no `persona_id`).
    """
    source_personas = (
        await db.execute(
            select(AgentPersona)
            .where(AgentPersona.project_id == source_id)
            .order_by(AgentPersona.id)
        )
    ).scalars().all()

    # GH #70 — lower-cased: persona names are case-insensitive-unique per
    # project, so collision detection and rename uniqueness ignore case.
    # `target_names` = names occupied in the target so far (collision test).
    target_names: set[str] = {
        n.lower()
        for n in (
            await db.execute(
                select(AgentPersona.name).where(
                    AgentPersona.project_id == target_id
                )
            )
        ).scalars().all()
    }
    # `reserved` = every name a generated rename must avoid: the target's
    # names, EVERY original source name (so a generated name can never equal
    # another source persona's original name — that is what made reference
    # rewrites cascade), and every name generated so far.
    org_active, org_all = await _org_persona_names(db, org_id)
    reserved: set[str] = set(target_names) | {
        p.name.lower() for p in source_personas
    } | org_all

    source_prefix = source_id[:_SOURCE_SUFFIX_LEN]
    renames: list[dict] = []

    for persona in source_personas:
        shadows_org = (
            persona.name.lower() not in target_names
            and persona.name.lower() in org_active
        )
        if shadows_org and persona_policy != "rename":
            # merge_projects refuses this before any write; defensive.
            raise ValueError(
                f"persona {persona.name!r} shadows an org persona; only the "
                "rename policy can merge it"
            )
        if persona.name.lower() in target_names or shadows_org:
            if persona_policy == "rename":
                new_name = _legal_rename(
                    persona.name, source_prefix, reserved, case_insensitive=True
                )
                reserved.add(new_name.lower())
                renames.append({
                    "persona_id": persona.id,
                    "old_name": persona.name,
                    "new_name": new_name,
                    "display_note": (
                        f"Renamed from '{persona.name}' "
                        f"(source project {source_prefix}) — "
                        + (
                            "overrode the org persona of the same name, "
                            "which the target's references resolve to."
                            if shadows_org
                            else "collided with target's persona of same name."
                        )
                    ),
                })
                persona.name = new_name
                target_names.add(new_name.lower())
            elif persona_policy == "skip":
                archived_name = _legal_rename(
                    persona.name, f"{source_prefix}-archived", reserved,
                    case_insensitive=True,
                )
                reserved.add(archived_name.lower())
                renames.append({
                    "persona_id": persona.id,
                    "old_name": persona.name,
                    "new_name": archived_name,
                    "display_note": (
                        f"Archived '{persona.name}' "
                        f"(source project {source_prefix}) — "
                        f"skipped due to collision with target's "
                        f"persona of same name."
                    ),
                })
                persona.name = archived_name
                persona.is_active = False
                target_names.add(archived_name.lower())
            elif persona_policy == "merge_content":
                # Merge content into target persona.
                target_p = (
                    await db.execute(
                        select(AgentPersona)
                        .where(
                            AgentPersona.project_id == target_id,
                            AgentPersona.id != persona.id,
                            func.lower(AgentPersona.name) == persona.name.lower(),
                        )
                        .order_by(
                            case((AgentPersona.name == persona.name, 0), else_=1),
                            AgentPersona.id,
                        )
                    )
                ).scalars().first()
                assert target_p is not None  # collision implies a target row
                target_p.content = (
                    f"{target_p.content}\n\n"
                    f"--- merged from project {source_prefix} ---\n"
                    f"{persona.content}"
                )
                # Archive source persona under unique name.
                archived_name = _legal_rename(
                    persona.name, f"{source_prefix}-archived", reserved,
                    case_insensitive=True,
                )
                reserved.add(archived_name.lower())
                renames.append({
                    "persona_id": persona.id,
                    "old_name": persona.name,
                    "new_name": archived_name,
                    "display_note": (
                        f"Archived '{persona.name}' "
                        f"(source project {source_prefix}) — "
                        f"content merged into target's persona "
                        f"of same name."
                    ),
                })
                persona.name = archived_name
                persona.is_active = False
                target_names.add(archived_name.lower())
        else:
            # Moves under its own name; later source rows that differ only
            # in case (legacy) now collide with it.
            target_names.add(persona.name.lower())
        # REASSIGN — every source persona moves to target.
        persona.project_id = target_id

    for org_persona in org_pins or []:
        import uuid as _uuid

        pin_name = _legal_rename(
            org_persona.name, source_prefix, reserved, case_insensitive=True
        )
        reserved.add(pin_name.lower())
        target_names.add(pin_name.lower())
        db.add(AgentPersona(
            id=f"per_{_uuid.uuid4().hex[:16]}",
            project_id=target_id,
            name=pin_name,
            role=org_persona.role,
            content=org_persona.content,
            specializations=org_persona.specializations,
            is_active=True,
            created_by=user_id or org_persona.created_by,
        ))
        renames.append({
            "pinned_from_org_persona_id": org_persona.id,
            "old_name": org_persona.name,
            "new_name": pin_name,
            "display_note": (
                f"Pinned org persona '{org_persona.name}' as '{pin_name}' "
                f"for references from source project {source_prefix} — the "
                f"target's own '{org_persona.name}' would otherwise take "
                f"them over."
            ),
        })

    return renames


async def _capture_pending_handoff_ids(
    db: AsyncSession,
    source_id: str,
) -> list[str]:
    """GH #70 — ids of PENDING handoffs whose provenance belongs to the
    source project, captured BEFORE `_step_repos` reassigns the repo rows.

    Three attribution shapes (mirrors `pending_handoffs_in_project` in
    services/personas.py plus the legacy-remote shape):

    1. The durable anchor (`Handoff.source_project_id`) points at the
       source — authoritative, set at create / backfilled by 072.
    2. Anchor NULL and the source session has project_id == source
       (rolling-deploy window: old code may still create unanchored rows).
    3. Anchor NULL and the source session is LEGACY (project_id NULL) but
       its git_remote_normalized belongs to the source — dual-read like
       `resolve_project_by_remote`: project_repos first, then the legacy
       projects.git_remote_normalized column. These are exactly the rows a
       repo reassign would silently detach, so the capture must run while
       the repo rows still name the source.

    Empty list is fine — `Handoff.id.in_([])` renders false downstream.
    """
    legacy_remote_sessions = select(Session.id).where(
        Session.project_id.is_(None),
        or_(
            Session.git_remote_normalized.in_(
                select(ProjectRepo.git_remote_normalized).where(
                    ProjectRepo.project_id == source_id
                )
            ),
            Session.git_remote_normalized.in_(
                select(Project.git_remote_normalized).where(
                    Project.id == source_id
                )
            ),
        ),
    )
    rows = (
        await db.execute(
            select(Handoff.id).where(
                Handoff.status == "pending",
                or_(
                    Handoff.source_project_id == source_id,
                    and_(
                        Handoff.source_project_id.is_(None),
                        or_(
                            Handoff.session_id.in_(
                                select(Session.id).where(
                                    Session.project_id == source_id
                                )
                            ),
                            Handoff.session_id.in_(legacy_remote_sessions),
                        ),
                    ),
                ),
            )
        )
    ).scalars().all()
    return list(rows)


async def _step_handoff_anchors(
    db: AsyncSession,
    handoff_ids: list[str],
    target_id: str,
) -> None:
    """GH #70 — move the durable anchor of the source's pending handoffs
    to the merge target, so post-merge stranding guards and the claim
    route attribute them to the live project (the source is tombstoned)."""
    if not handoff_ids:
        return
    await db.execute(
        update(Handoff)
        .where(Handoff.id.in_(handoff_ids))
        .values(source_project_id=target_id)
        .execution_options(synchronize_session=False)
    )


async def _step_rewrite_persona_refs(
    db: AsyncSession,
    source_id: str,
    source_snapshot: list[tuple[str, str]],
    renames: list[dict],
    handoff_ids: list[str] | None = None,
) -> None:
    """Step 2b (rename policy): point the source project's LIVE persona
    references at the renamed persona, so they keep resolving to the same
    persona after the move instead of to the target's same-named one.

    Rewritten (references used to resolve a persona later):
      * tickets.assigned_to
      * agent_runs.persona_name
      * handoffs.persona_name of PENDING handoffs from the source project's
        sessions (the claim writes it into the recipient's bundle)
    Left as written (audit / snapshot / attribution): sessions.persona_name,
    handoffs.snapshot_persona_name, tickets.created_by_persona,
    ticket_comments.author_persona, ticket_edits.edited_by_persona,
    agent_runs.triggered_by_persona, knowledge_entries.persona_name,
    wiki_page_revisions.persona_name, retrieval_audit_contexts.persona_name.

    `source_snapshot` is the source personas' (id, ORIGINAL name) list taken
    before any rename, filtered to ACTIVE personas only (an inactive
    duplicate must not win the match). Each distinct reference value is
    resolved ONCE against it, case-insensitively, exactly like the persona
    resolver: an exact-case match wins, else the lowest id among case-only
    duplicates. The result is one immutable {old value: new name} mapping
    applied with a single CASE update per table, so one rename can never
    feed another.
    """
    renamed_by_id = {
        r["persona_id"]: r["new_name"] for r in renames if r.get("persona_id")
    }
    # GH #70 — org personas pinned for the source's references (no active
    # source persona carries these names, else the reference resolved to it).
    pinned_by_lower = {
        r["old_name"].lower(): r["new_name"]
        for r in renames
        if r.get("pinned_from_org_persona_id")
    }
    if not renamed_by_id and not pinned_by_lower:
        return
    by_lower: dict[str, list[tuple[str, str]]] = {}
    for pid, name in sorted(source_snapshot, key=lambda t: t[0]):
        by_lower.setdefault(name.lower(), []).append((pid, name))

    def _target_for(value: str) -> str | None:
        candidates = by_lower.get(value.lower())
        if not candidates:
            return pinned_by_lower.get(value.lower())
        exact = [c for c in candidates if c[1] == value]
        pid = (exact or candidates)[0][0]
        return renamed_by_id.get(pid)

    # GH #70: scope the rewrite to the pending-handoff set captured
    # BEFORE `_step_repos` (legacy remote-anchored handoffs included);
    # a bare session.project_id match would miss them.
    handoff_scope = (
        Handoff.status == "pending",
        Handoff.id.in_(handoff_ids or []),
    )
    targets = (
        (Ticket, Ticket.assigned_to, (Ticket.project_id == source_id,)),
        (AgentRun, AgentRun.persona_name, (AgentRun.project_id == source_id,)),
        (Handoff, Handoff.persona_name, handoff_scope),
    )
    for model, column, scope in targets:
        values = (
            await db.execute(
                select(column).where(*scope, column.is_not(None)).distinct()
            )
        ).scalars().all()
        mapping = {
            v: new for v in values if (new := _target_for(v)) is not None and new != v
        }
        if not mapping:
            continue
        await db.execute(
            update(model)
            .where(*scope, column.in_(list(mapping)))
            .values({column.key: case(mapping, value=column, else_=column)})
            .execution_options(synchronize_session=False)
        )


async def _step_rules(
    db: AsyncSession,
    source_id: str,
    target_id: str,
    target_has_rules: bool,
    source_prefix: str,
) -> str:
    """Step 3: ProjectRules — promote if target none, archive if both exist.

    Returns rules_action: 'promoted' | 'archived' | 'none'.
    """
    source_rules = (
        await db.execute(
            select(ProjectRules).where(ProjectRules.project_id == source_id)
        )
    ).scalar_one_or_none()

    if source_rules is None:
        return "none"

    if target_has_rules:
        # Archive source rules as wiki page.
        await _archive_rules_as_wiki_page(
            db, source_rules, target_id, source_prefix
        )
        await db.delete(source_rules)
        return "archived"
    else:
        source_rules.project_id = target_id
        return "promoted"


async def _archive_rules_as_wiki_page(
    db: AsyncSession,
    source_rules: ProjectRules,
    target_id: str,
    source_prefix: str,
) -> None:
    """Create a wiki page snapshot of the source project's rules."""
    import uuid as _uuid

    slug = f"_merged_rules_{source_prefix}"
    now = datetime.now(timezone.utc)
    page = KnowledgePage(
        id=str(_uuid.uuid4()),
        project_id=target_id,
        slug=slug,
        title=f"Merged Rules (from {source_prefix})",
        page_type="user",
        created_at=now,
        updated_at=now,
    )
    db.add(page)

    rev = WikiPageRevision(
        project_id=target_id,
        page_slug=slug,
        revision_number=1,
        content_snapshot=source_rules.static_rules or "",
        title=f"Merged Rules (from {source_prefix})",
        revised_at=now,
    )
    db.add(rev)


async def _step_knowledge_pages(
    db: AsyncSession,
    source_id: str,
    target_id: str,
    slug_collisions: list[str],
) -> list[dict]:
    """Steps 4-5: Reassign pages + revisions, handling slug collisions.

    For colliding slugs: rename to {slug}-{src8}, revisions follow
    atomically. Non-colliding: straight reassign.

    Returns list of {old_slug, new_slug} for audit.
    """
    source_prefix = source_id[:_SOURCE_SUFFIX_LEN]
    slug_renames: list[dict] = []
    target_slugs: set[str] = set(
        (
            await db.execute(
                select(KnowledgePage.slug).where(
                    KnowledgePage.project_id == target_id
                )
            )
        ).scalars().all()
    )

    # Handle colliding slugs first (rename before reassign).
    for old_slug in slug_collisions:
        new_slug = _legal_rename(old_slug, source_prefix, target_slugs)
        target_slugs.add(new_slug)

        # Rename KnowledgePage.
        await db.execute(
            update(KnowledgePage)
            .where(
                KnowledgePage.project_id == source_id,
                KnowledgePage.slug == old_slug,
            )
            .values(slug=new_slug, project_id=target_id)
        )
        # Re-point ALL revisions for this slug.
        await db.execute(
            update(WikiPageRevision)
            .where(
                WikiPageRevision.project_id == source_id,
                WikiPageRevision.page_slug == old_slug,
            )
            .values(project_id=target_id, page_slug=new_slug)
        )
        slug_renames.append({"old_slug": old_slug, "new_slug": new_slug})

    # Reassign remaining (non-colliding) pages.
    await db.execute(
        update(KnowledgePage)
        .where(KnowledgePage.project_id == source_id)
        .values(project_id=target_id)
    )
    # Reassign remaining revisions.
    await db.execute(
        update(WikiPageRevision)
        .where(WikiPageRevision.project_id == source_id)
        .values(project_id=target_id)
    )

    return slug_renames


async def _step_knowledge_entries(
    db: AsyncSession,
    source_id: str,
    target_id: str,
) -> tuple[dict[str, str], list[str]]:
    """Step 6: Dedup + reassign knowledge entries.

    Returns (entry_id_map, skipped_ids).
    entry_id_map: source_entry_id → target_equivalent_id.
    Reassigned (non-dup) entries map to themselves (identity).
    """
    target_entries = (
        await db.execute(
            select(KnowledgeEntry.entry_type, KnowledgeEntry.content, KnowledgeEntry.id)
            .where(KnowledgeEntry.project_id == target_id)
        )
    ).all()
    target_keys = {
        (e.entry_type, _normalize_content(e.content)): e.id
        for e in target_entries
    }

    source_entries = (
        await db.execute(
            select(KnowledgeEntry).where(KnowledgeEntry.project_id == source_id)
        )
    ).scalars().all()

    entry_id_map: dict[str, str] = {}
    skipped_ids: list[str] = []

    for entry in source_entries:
        key = (entry.entry_type, _normalize_content(entry.content))
        if key in target_keys:
            # Skip — redirect references to target equivalent.
            entry_id_map[entry.id] = target_keys[key]
            skipped_ids.append(entry.id)
        else:
            # Reassign unique entry.
            entry.project_id = target_id
            entry_id_map[entry.id] = entry.id  # identity mapping

    return entry_id_map, skipped_ids


async def _step_knowledge_links(
    db: AsyncSession,
    source_id: str,
    target_id: str,
    entry_id_map: dict[str, str],
) -> list[str]:
    """Step 7: Map + dedup + reassign knowledge links.

    COMPUTE BEFORE MUTATE: the remapped key is computed FIRST from the
    entry_id_map WITHOUT mutating the ORM row.  Duplicates are
    db.delete()'d.  Only non-duplicate rows are mutated.

    Returns skipped_link_ids.
    """
    source_links = (
        await db.execute(
            select(KnowledgeLink).where(KnowledgeLink.project_id == source_id)
        )
    ).scalars().all()

    target_links = (
        await db.execute(
            select(KnowledgeLink).where(KnowledgeLink.project_id == target_id)
        )
    ).scalars().all()
    running_keys = {
        (lk.source_type, lk.source_id, lk.target_type, lk.target_id)
        for lk in target_links
    }

    skipped_link_ids: list[str] = []
    for link in source_links:
        # Compute remapped key BEFORE mutating the row.
        new_source_id = entry_id_map.get(link.source_id, link.source_id)
        new_target_id = entry_id_map.get(link.target_id, link.target_id)
        key = (link.source_type, new_source_id, link.target_type, new_target_id)

        if key in running_keys:
            # Duplicate — delete source link, do NOT mutate.
            skipped_link_ids.append(link.id)
            await db.delete(link)
        else:
            running_keys.add(key)
            link.project_id = target_id
            link.source_id = new_source_id
            link.target_id = new_target_id

    return skipped_link_ids


async def _step_straight_reassign(
    db: AsyncSession,
    source_id: str,
    target_id: str,
) -> None:
    """Steps 8-15: Straight reassign all remaining tables."""
    for model in _STRAIGHT_REASSIGN_MODELS:
        await db.execute(
            update(model)
            .where(model.project_id == source_id)
            .values(project_id=target_id)
        )


async def _step_feature_grandfathers(
    db: AsyncSession,
    source_id: str,
    target_id: str,
) -> None:
    """Carry the source's grandfathered features over to the target.

    The source's tickets, runs and personas move to the target, so the
    target inherits the source's pre-v0.17.2 usage (tk_a90e4015ad984cde).
    The source rows stay with the tombstone.
    """
    source_features = set(
        (
            await db.execute(
                select(ProjectFeatureGrandfather.feature).where(
                    ProjectFeatureGrandfather.project_id == source_id
                )
            )
        ).scalars()
    )
    target_features = set(
        (
            await db.execute(
                select(ProjectFeatureGrandfather.feature).where(
                    ProjectFeatureGrandfather.project_id == target_id
                )
            )
        ).scalars()
    )
    for feature in sorted(source_features - target_features):
        db.add(ProjectFeatureGrandfather(project_id=target_id, feature=feature))


# ── main entry point ───────────────────────────────────────────────

async def merge_projects(
    db: AsyncSession,
    source_id: str,
    target_id: str,
    user_id: str,
    dry_run: bool,
    persona_policy: str,
    session_factory: async_sessionmaker,
    *,
    platform_admin: bool = False,
    before_commit: Callable[[dict], None] | None = None,
) -> dict:
    """Merge source project into target project.

    Args:
        db: Primary session for reads + mutations.
        source_id: Project to merge (becomes tombstone).
        target_id: Project to merge into.
        user_id: Initiating user.
        dry_run: If True, validate + compute plan only (ZERO writes).
        persona_policy: 'rename' | 'skip' | 'merge_content'.
        session_factory: Factory for fresh sessions (audit survival).
        platform_admin: The caller is acting as a platform admin (admin
            override, GitHub #89): under the locks, re-check that they are
            still an active platform admin instead of a project admin.
        before_commit: Called with the final stats just before the merge
            commits, so the caller can add rows (an AdminAction) to ``db``
            that commit or roll back together with the merge.

    Returns:
        dict with keys: dry_run, stats, persona_collisions, slug_collisions,
        ke_duplicates, [audit_id for execute].
    """
    # =================================================================
    # Phase 1: Validate preconditions (same transaction, NOT audited).
    # =================================================================
    source, target = await _validate_preconditions(db, source_id, target_id)

    # Pre-compute collisions + stats.
    persona_collisions = await _detect_persona_collisions(
        db, source_id, target_id, source.org_id,
        await _capture_pending_handoff_ids(db, source_id) if dry_run else None,
    )
    slug_collisions = await _detect_slug_collisions(db, source_id, target_id)
    ke_duplicates = await _detect_ke_duplicates(db, source_id, target_id)
    target_has_rules_flag = await _has_rules(db, target_id)
    stats = await _compute_merge_stats(db, source_id, target_id, source.org_id)

    if dry_run:
        # Dry-run: ZERO DB writes. Validate + return plan only.
        # No audit row, no locks beyond reads. It predicts the execute-time
        # org-shadowing refusal so the plan never promises a merge that
        # --confirm would refuse.
        shadow = _org_shadow_collisions(persona_collisions)
        return {
            "dry_run": True,
            "would_refuse": bool(shadow) and persona_policy != "rename",
            "blocking_reason": (
                "persona_org_shadowing"
                if shadow and persona_policy != "rename" else None
            ),
            "stats": stats,
            "persona_collisions": persona_collisions,
            "slug_collisions": slug_collisions,
            "ke_duplicates": ke_duplicates,
        }

    from fastapi import HTTPException

    # =================================================================
    # Phase 2: Write the ATTEMPT audit row FIRST, from a separate session,
    #          before this request takes ANY scope or row lock.
    #
    # On file-backed SQLite the scope lock is the database writer lock:
    # once the primary session holds it, a second session cannot commit
    # ("database is locked"). So the durable 'started' row is written
    # now, and every later audit update happens only after the primary
    # session has committed or rolled back. Its stats are the pre-lock
    # plan; the completed/failed update records the final outcome.
    # =================================================================
    import uuid as _uuid

    audit_id = f"pma_{_uuid.uuid4().hex[:16]}"
    async with session_factory() as audit_db:
        async with audit_db.begin():
            audit_db.add(
                ProjectMergeAudit(
                    id=audit_id,
                    source_project_id=source_id,
                    target_project_id=target_id,
                    initiated_by_user_id=user_id,
                    dry_run=False,
                    status="started",
                    persona_policy=persona_policy,
                    stats=json.dumps(stats),
                    persona_renames="[]",
                    slug_renames="[]",
                    skipped_ke_ids="[]",
                    skipped_link_ids="[]",
                    rules_action=None,
                )
            )
        # Committed immediately — durable before mutation begins.

    persona_renames: list[dict] = []
    slug_renames_list: list[dict] = []
    skipped_ke_ids: list[str] = []
    skipped_link_ids: list[str] = []
    rules_action: str = "none"

    try:
        # =============================================================
        # Phase 1b (EXECUTE only): locks + re-validation, in the global
        # order (scope_locks.py):
        #   1. both PROJECT scope locks (sorted). Every writer of
        #      project.org_id (transfers) and every write that adds rows
        #      merge relocates (personas, tickets, agent runs, knowledge
        #      entries, wiki pages) takes the same lock, so after this the
        #      projects cannot change under us and no late row can land.
        #   2. re-read both projects and re-validate scope +
        #      authorization — a transfer may have landed since the
        #      caller's checks.
        #   3. the (refreshed) ORG scope lock.
        #   4. row locks (SELECT ... FOR UPDATE) last; re-check the
        #      merged/pending-transfer preconditions under them.
        #   5. recompute the execute plan (stats, rules flag, collision
        #      sets) from the locked state.
        # =============================================================
        from sessionfs.server.auth.project_access import user_is_project_admin
        from sessionfs.server.services.scope_locks import lock_scopes

        await lock_scopes(db, project_ids=[source_id, target_id])
        refreshed = (
            await db.execute(
                select(Project)
                .where(Project.id.in_([source_id, target_id]))
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
        by_id = {p.id: p for p in refreshed}
        locked_source = by_id.get(source_id)
        locked_target = by_id.get(target_id)
        if locked_source is None:
            raise HTTPException(404, "Source project not found")
        if locked_target is None:
            raise HTTPException(404, "Target project not found")
        source = locked_source
        target = locked_target
        if source.org_id != target.org_id:
            raise HTTPException(
                409,
                "Project scope changed concurrently (a transfer landed); the "
                "projects are no longer in the same org. Refresh and retry.",
            )
        # Org lock BEFORE the authorization re-read: membership/role
        # changes take the same org lock, so the standing we read is the
        # standing that holds until commit.
        await lock_scopes(db, org_ids=[source.org_id])
        if platform_admin:
            actor = (
                await db.execute(
                    select(User.tier, User.is_active).where(User.id == user_id)
                )
            ).first()
            if actor is None or actor.tier != "admin" or actor.is_active is False:
                raise HTTPException(403, "Platform admin access required")
        else:
            if not await user_is_project_admin(db, user_id, source):
                raise HTTPException(403, "Not authorized on source project")
            if not await user_is_project_admin(db, user_id, target):
                raise HTTPException(403, "Not authorized on target project")
        await db.execute(
            select(Project)
            .where(Project.id.in_([source_id, target_id]))
            .with_for_update()
            .execution_options(populate_existing=True)
        )

        # Re-check merged_into under the lock (concurrent-merge race guard).
        if source.merged_into_project_id is not None:
            raise HTTPException(
                400, "Source project was already merged (concurrent merge)"
            )
        if target.merged_into_project_id is not None:
            raise HTTPException(
                400, "Target project was already merged (concurrent merge)"
            )

        # Re-check pending transfers under the lock.
        for side, proj in [("source", source), ("target", target)]:
            pending = (
                await db.execute(
                    select(ProjectTransfer)
                    .where(
                        ProjectTransfer.project_id == proj.id,
                        ProjectTransfer.state == "pending",
                    )
                )
            ).scalar_one_or_none()
            if pending:
                raise HTTPException(
                    400,
                    f"{side.capitalize()} project has a pending transfer. "
                    "Resolve it first.",
                )

        # Recompute the execute plan from the locked state — the pre-lock
        # plan (including the slug collision set) may be stale.
        target_has_rules_flag = await _has_rules(db, target_id)
        stats = await _compute_merge_stats(db, source_id, target_id, source.org_id)
        slug_collisions = await _detect_slug_collisions(db, source_id, target_id)

        # =============================================================
        # Phase 3: Merge mutations — single atomic transaction.
        # =============================================================
        source_prefix = source_id[:_SOURCE_SUFFIX_LEN]

        # GH #70: capture the source's pending-handoff set BEFORE the
        # repo rows move — legacy handoffs anchored only through a repo
        # remote would be undetectable after Step 1.
        pending_handoff_ids = await _capture_pending_handoff_ids(db, source_id)

        # GH #70 — org shadowing, from the locked state (the org lock is
        # held, so org personas can't change under us). Only the rename
        # policy keeps every live reference bound to the persona it
        # resolves to today; refuse the others rather than re-bind silently.
        persona_collisions = await _detect_persona_collisions(
            db, source_id, target_id, source.org_id, pending_handoff_ids
        )
        shadow = _org_shadow_collisions(persona_collisions)
        if shadow and persona_policy != "rename":
            raise HTTPException(
                409,
                {
                    "code": "persona_org_shadowing",
                    "message": (
                        "Merging would change which persona some references "
                        "resolve to (an org persona and a project persona "
                        "share a name). Use persona_policy='rename'."
                    ),
                    "persona_collisions": shadow,
                },
            )
        org_pins = await _detect_org_rebinds(
            db, source_id, target_id, source.org_id, pending_handoff_ids
        )

        # Step 1: project_repos — lock + demote + reassign.
        await _step_repos(db, source_id, target_id)

        # Snapshot the source personas' ORIGINAL names before any rename
        # (the reference rewrite resolves against these, not the renamed).
        # GH #70 (Codex R6) — LIVE references must resolve only against
        # ACTIVE source personas: an inactive row that shares a name (legacy
        # case-distinct duplicate or a soft-deleted override) must not win
        # the exact-case/lowest-id match and silently leave the reference
        # bound to the wrong (target) persona after the rename.
        source_persona_snapshot = [
            (pid, name)
            for pid, name in (
                await db.execute(
                    select(AgentPersona.id, AgentPersona.name).where(
                        AgentPersona.project_id == source_id,
                        AgentPersona.is_active.is_(True),
                    )
                )
            ).all()
        ]

        # Step 2: agent_personas — reassign ALL, handle collisions.
        persona_renames = await _step_personas(
            db, source_id, target_id, persona_policy,
            org_id=source.org_id, org_pins=org_pins, user_id=user_id,
        )

        # Step 2b: live persona references follow the renames (same
        # transaction, before the straight reassignment moves the rows).
        if persona_policy == "rename":
            await _step_rewrite_persona_refs(
                db, source_id, source_persona_snapshot, persona_renames,
                handoff_ids=pending_handoff_ids,
            )

        # Step 3: project_rules — promote or archive.
        rules_action = await _step_rules(
            db, source_id, target_id, target_has_rules_flag, source_prefix
        )

        # Steps 4-5: knowledge_pages + wiki_page_revisions (atomically).
        slug_renames_list = await _step_knowledge_pages(
            db, source_id, target_id, slug_collisions
        )

        # Step 6: knowledge_entries — dedup + build entry-ID map.
        entry_id_map, skipped_ke_ids = await _step_knowledge_entries(
            db, source_id, target_id
        )

        # Step 7: knowledge_links — map + dedup + reassign.
        skipped_link_ids = await _step_knowledge_links(
            db, source_id, target_id, entry_id_map
        )

        # Steps 8-15: Remaining tables — straight reassign.
        await _step_straight_reassign(db, source_id, target_id)
        await _step_feature_grandfathers(db, source_id, target_id)

        # Step 16: Mark source as tombstone. Its repos were reassigned to the
        # target above, so null the legacy remote as well: a merge tombstone
        # is a distinct state from repo-reclaimed, and leaving the legacy
        # column pointing at the reassigned primary would recreate the
        # pre-073 "squatted legacy column" state (zero repos + a stale
        # remote) that the link_repo displacement path could then misread as
        # reclaimable.
        source.merged_into_project_id = target_id
        source.merged_at = func.now()
        source.git_remote_normalized = None

        # Step 17: Catch-up UPDATE for concurrent-sync race.
        await db.execute(
            update(Session)
            .where(Session.project_id == source_id)
            .values(project_id=target_id)
        )

        # Step 18 (GH #70): move the durable anchor of the source's
        # pending handoffs to the target — the stranding guards and the
        # claim route attribute them to the live project from now on.
        await _step_handoff_anchors(db, pending_handoff_ids, target_id)

        if before_commit is not None:
            before_commit(stats)

        # Merge changes committed — all-or-nothing success. This also
        # releases the scope locks (and the SQLite writer lock).
        await db.commit()

    except Exception as exc:
        # =============================================================
        # Failure path (validation under the locks, or a mutation error):
        # roll the primary session back FIRST — that releases its locks,
        # including the SQLite writer lock — then mark the audit row
        # 'failed' from a fresh session.
        # =============================================================
        await db.rollback()
        async with session_factory() as audit_db:
            async with audit_db.begin():
                await audit_db.execute(
                    update(ProjectMergeAudit)
                    .where(ProjectMergeAudit.id == audit_id)
                    .values(
                        status="failed",
                        error_message=f"{type(exc).__name__}: {exc}",
                    )
                )
        raise  # re-raise after audit update

    # =================================================================
    # Phase 4: Outcome-update audit row → 'completed' (fresh session,
    #          after the primary commit released its locks).
    # =================================================================
    async with session_factory() as audit_db:
        async with audit_db.begin():
            result = await audit_db.execute(
                update(ProjectMergeAudit)
                .where(ProjectMergeAudit.id == audit_id)
                .values(
                    status="completed",
                    stats=json.dumps(stats),
                    persona_renames=json.dumps(persona_renames),
                    slug_renames=json.dumps(slug_renames_list),
                    skipped_ke_ids=json.dumps(skipped_ke_ids),
                    skipped_link_ids=json.dumps(skipped_link_ids),
                    rules_action=rules_action,
                )
            )
            if result.rowcount == 0:
                logger.error(
                    "merge audit row %s missing on outcome update", audit_id
                )

    return {
        "dry_run": False,
        "stats": stats,
        "audit_id": audit_id,
        "persona_renames": persona_renames,
        "slug_renames": slug_renames_list,
        "skipped_ke_ids": skipped_ke_ids,
        "skipped_link_ids": skipped_link_ids,
        "rules_action": rules_action,
    }
