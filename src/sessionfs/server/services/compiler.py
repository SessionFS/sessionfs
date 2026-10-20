"""Compile pending knowledge entries into project context via LLM."""

from __future__ import annotations

import json
import logging
import re
import secrets
import uuid
from collections import defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import delete, func, select, update

from sessionfs.server.db.models import (
    ContextCompilation,
    KnowledgeEntry,
    KnowledgeLink,
    KnowledgePage,
    Project,
    Session,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("sessionfs.compiler")

SECTION_MAP = {
    "decision": "## Key Decisions",
    "pattern": "## Patterns & Conventions",
    "discovery": "## Discoveries",
    "convention": "## Coding Conventions",
    "bug": "## Known Issues & Workarounds",
    "dependency": "## Dependencies & Integrations",
}

_COMPILE_SYSTEM_PROMPT = """\
You are a project context compiler. Given the current project context document \
and a set of new knowledge entries grouped by type, produce an updated context \
document that incorporates the new information.

Rules:
- Preserve the existing structure and headings
- Add new information under the appropriate section
- Remove duplicates (same fact stated differently)
- Keep it concise — this document is injected into every session
- Output ONLY the updated context document, nothing else"""


def _section_slug_for_entry_type(entry_type: str) -> str:
    heading = SECTION_MAP.get(entry_type, f"## {entry_type.title()}")
    raw_title = heading.lstrip("# ").strip().lower()
    return re.sub(r"[^a-z0-9]+", "_", raw_title).strip("_")


def _json_dt(value) -> str | None:
    return value.isoformat() if value is not None else None


def _build_source_manifest(
    entries: list[KnowledgeEntry],
    persona_by_session: dict[str, str | None] | None = None,
) -> str:
    """Map compiled context sections to the active KB claims feeding them.

    Per-entry fields support SoD disqualification:
    - `kb_entry_id` — primary key of the source claim
    - `created_by_user_id` — claim author (must-have for SoD)
    - `created_by_persona` — persona attribution resolved from the
      source session's persona_name. KnowledgeEntry doesn't carry a
      persona column directly, so we look it up via `session_id` at
      compile time. None when the source session was authored without
      an active persona bundle.
    - `promoted_at` — when the claim entered the compiled context

    `compile_id` is NOT persisted in each entry — it's denormalised at
    read time in `get_context_section` from the parent ContextCompilation
    row, so the compile and its manifest stay one atomic write.
    """
    persona_by_session = persona_by_session or {}
    manifest: dict[str, list[dict]] = defaultdict(list)
    for entry in entries:
        slug = _section_slug_for_entry_type(entry.entry_type)
        manifest[slug].append(
            {
                "kb_entry_id": entry.id,
                "created_by_user_id": entry.user_id,
                "created_by_persona": persona_by_session.get(entry.session_id),
                "promoted_at": _json_dt(entry.promoted_at),
            }
        )
    return json.dumps(manifest, sort_keys=True)


def _build_compile_prompt(context: str, grouped_entries: dict[str, list[str]]) -> str:
    """Build the user prompt for compilation."""
    parts = ["## Current Project Context", context or "(empty)", "", "## New Knowledge Entries"]
    for entry_type, contents in sorted(grouped_entries.items()):
        parts.append(f"\n### {entry_type.title()} ({len(contents)} entries)")
        for c in contents:
            parts.append(f"- {c}")
    parts.append("\n## Task")
    parts.append("Produce the updated project context document.")
    return "\n".join(parts)


async def _auto_supersede(project_id: str, db: AsyncSession) -> int:
    """Conservative auto-supersession during compile.

    Only supersedes when:
    - Same entity_ref (non-null) + same entry_type
    - Newer entry has confidence >= 0.8
    - High lexical overlap (>0.9) between old and new content

    If overlap is between 0.5 and 0.9, creates a 'contradicts' link instead.
    Returns the number of entries superseded.

    tk_09d8bdf4f6374a13 — prefetch existing entry→entry links once and skip
    add() when the 5-tuple already exists. Without this, every subsequent
    /compile re-creates the same 'contradicts' link (the supersedes path is
    self-gating via `superseded_by`, but contradicts has no such gate) and
    asyncpg raises UniqueViolationError on uq_kl_link at autoflush, which
    bubbles up uncaught and crashes /compile with the Starlette default
    21-byte 500.
    """
    from sessionfs.server.services.knowledge import word_overlap as _wo

    # Find entries with entity_ref set, grouped by (entity_ref, entry_type)
    result = await db.execute(
        select(KnowledgeEntry).where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.entity_ref.isnot(None),
            KnowledgeEntry.dismissed == False,  # noqa: E712
            KnowledgeEntry.superseded_by.is_(None),
        ).order_by(KnowledgeEntry.created_at.asc())
    )
    entries = list(result.scalars().all())

    groups: dict[tuple[str, str], list[KnowledgeEntry]] = defaultdict(list)
    for e in entries:
        if e.entity_ref:
            groups[(e.entity_ref, e.entry_type)].append(e)

    # Bulk-fetch existing entry→entry link pairs so we can skip duplicates.
    # uq_kl_link is (project_id, source_type, source_id, target_type, target_id)
    # — link_type is NOT part of the unique key (Codex R1 HIGH catch). With
    # project_id and source_type='entry' / target_type='entry' fixed, the
    # in-memory dedup key is just (source_id, target_id). If any entry→entry
    # link already exists for the pair (regardless of link_type), we must
    # skip — the DB constraint will reject a second insert even if the
    # link_type differs.
    existing_link_pairs: set[tuple[str, str]] = set()
    existing_result = await db.execute(
        select(
            KnowledgeLink.source_id,
            KnowledgeLink.target_id,
        ).where(
            KnowledgeLink.project_id == project_id,
            KnowledgeLink.source_type == "entry",
            KnowledgeLink.target_type == "entry",
        )
    )
    for src, tgt in existing_result.all():
        existing_link_pairs.add((src, tgt))

    superseded_count = 0
    for (_ref, _type), group in groups.items():
        if len(group) < 2:
            continue

        # Compare each pair: older vs newer
        for i in range(len(group)):
            older = group[i]
            if older.superseded_by is not None:
                continue
            for j in range(i + 1, len(group)):
                newer = group[j]
                if newer.superseded_by is not None:
                    continue
                if newer.confidence < 0.8:
                    continue

                overlap = _wo(older.content, newer.content)
                if overlap > 0.9:
                    # Supersede
                    older.superseded_by = newer.id
                    older.supersession_reason = "Auto-superseded: same entity, high overlap"
                    older.freshness_class = "superseded"
                    link_pair = (str(newer.id), str(older.id))
                    if link_pair not in existing_link_pairs:
                        link = KnowledgeLink(
                            project_id=project_id,
                            source_type="entry",
                            source_id=str(newer.id),
                            target_type="entry",
                            target_id=str(older.id),
                            link_type="supersedes",
                            confidence=overlap,
                        )
                        db.add(link)
                        existing_link_pairs.add(link_pair)
                    superseded_count += 1
                    break  # This older entry is done
                elif overlap > 0.5:
                    # Create contradicts link (don't supersede). Dedup is
                    # pair-only — if a supersedes (or any other) link already
                    # exists for this pair, skip; the DB constraint covers
                    # link_type-agnostic uniqueness.
                    link_pair = (str(newer.id), str(older.id))
                    if link_pair not in existing_link_pairs:
                        link = KnowledgeLink(
                            project_id=project_id,
                            source_type="entry",
                            source_id=str(newer.id),
                            target_type="entry",
                            target_id=str(older.id),
                            link_type="contradicts",
                            confidence=overlap,
                        )
                        db.add(link)
                        existing_link_pairs.add(link_pair)

    if superseded_count:
        await db.flush()
        logger.info(
            "Auto-superseded %d entries in project %s", superseded_count, project_id
        )
    return superseded_count


# How many times compile_project_context will run the LLM merge and find the
# inputs changed under it before giving up on the LLM output and falling back
# to the deterministic merge (GH #70 plan-then-revalidate).
_MAX_LLM_COMPILE_ATTEMPTS = 2


class _CompileInputs:
    """Inputs of one compile pass, gathered under the project scope lock."""

    def __init__(
        self,
        *,
        project: Project,
        pending: list[KnowledgeEntry],
        grouped: dict[str, list[str]],
        previous_context: str,
        compile_base_context: str,
        max_context_words: int,
        freshness_updated: int,
    ) -> None:
        self.project = project
        self.pending = pending
        self.grouped = grouped
        self.previous_context = previous_context
        self.compile_base_context = compile_base_context
        self.max_context_words = max_context_words
        self.freshness_updated = freshness_updated


def _expire_kb_objects(db: AsyncSession) -> None:
    """Expire knowledge rows held in the identity map.

    After a discarded planning pass (savepoint rolled back) the identity map
    can still carry attribute values that pass computed — e.g. entries the
    housekeeping loaded after a bulk UPDATE. Expiring them makes the next
    pass re-read the committed state instead of trusting those values.
    """
    for obj in list(db.identity_map.values()):
        if isinstance(obj, (KnowledgeEntry, KnowledgeLink, KnowledgePage)):
            db.expire(obj)


async def _run_compile_housekeeping(
    db: AsyncSession,
    project: Project,
    project_id: str,
    force_rebuild: bool,
) -> _CompileInputs:
    """Housekeeping writes + pending-claim read for one compile pass.

    Freshness refresh, auto-supersession, decay, retention, auto-promotion
    and (force_rebuild) the compiled_at reset, then the pending-claim query.
    Writes are NOT committed here — the caller either discards them (a
    planning pass) or commits them with the projection (the final pass).
    """
    # Phase 1 + Phase 2 housekeeping passes: freshness refresh,
    # auto-supersession, decay, retention, and auto-promotion of
    # evidence-class entries. These run inside the same transaction as
    # the projection write (no intermediate commit). If anything
    # downstream fails (LLM call, atomic swap), the entire phase rolls
    # back along with the projection — that's what tk_bc3c02a63e994717
    # fixes.
    from sessionfs.server.services.freshness import refresh_freshness_classes
    freshness_updated = await refresh_freshness_classes(project_id, db)

    # Auto-supersession: same entity_ref + same entry_type + newer + high confidence + high overlap
    await _auto_supersede(project_id, db)

    # Decay: reduce confidence of entries not referenced recently.
    # "Not recent" = last_relevant_at is NULL and created > 90 days ago,
    # OR last_relevant_at itself is > 90 days ago.
    from sqlalchemy import or_
    decay_cutoff = datetime.now(timezone.utc) - timedelta(days=90)
    await db.execute(
        update(KnowledgeEntry)
        .where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.dismissed == False,  # noqa: E712
            KnowledgeEntry.confidence > 0.1,
            or_(
                KnowledgeEntry.last_relevant_at.is_(None) & (KnowledgeEntry.created_at < decay_cutoff),
                KnowledgeEntry.last_relevant_at < decay_cutoff,
            ),
        )
        .values(confidence=KnowledgeEntry.confidence * 0.8)
        .execution_options(synchronize_session=False)
    )

    # Retention: auto-dismiss entries older than project retention setting
    retention_days = getattr(project, "kb_retention_days", 180) or 180
    retention_cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
    await db.execute(
        update(KnowledgeEntry)
        .where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.dismissed == False,  # noqa: E712
            KnowledgeEntry.confidence < 0.3,
            KnowledgeEntry.created_at < retention_cutoff,
        )
        .values(dismissed=True)
        .execution_options(synchronize_session=False)
    )

    # 2a. Auto-promote eligible evidence to claims so the extraction pipeline
    # feeds the compile path. Without this, synced sessions dead-end at
    # claim_class='evidence' and never appear in compiled views.
    # Criteria: confidence >= 0.5, content >= 30 chars, not dismissed.
    # Deliberately looser than the manual writeback gate (0.8/50) because
    # deterministic extraction already filters for meaningful content.
    await db.execute(
        update(KnowledgeEntry)
        .where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.claim_class == "evidence",
            KnowledgeEntry.dismissed == False,  # noqa: E712
            KnowledgeEntry.confidence >= 0.5,
            func.length(KnowledgeEntry.content) >= 30,
        )
        .values(claim_class="claim")
        .execution_options(synchronize_session=False)
    )

    # tk_bc3c02a63e994717 — when force_rebuild=True, NULL compiled_at on
    # all active claims so the pending query below picks up everything.
    # This joins the single final commit — if the LLM call or any other
    # downstream step fails, the wipe rolls back too.
    if force_rebuild:
        await db.execute(
            update(KnowledgeEntry)
            .where(
                KnowledgeEntry.project_id == project_id,
                KnowledgeEntry.claim_class == "claim",
                KnowledgeEntry.dismissed == False,  # noqa: E712
                KnowledgeEntry.superseded_by.is_(None),
            )
            .values(compiled_at=None)
            .execution_options(synchronize_session=False)
        )

    # 2b. Get pending entries — only active claims. populate_existing: the
    # bulk UPDATEs above don't synchronize the identity map, so re-read the
    # rows this pass actually sees.
    result = await db.execute(
        select(KnowledgeEntry)
        .where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.compiled_at.is_(None),
            KnowledgeEntry.dismissed == False,  # noqa: E712
            KnowledgeEntry.claim_class == "claim",
            KnowledgeEntry.freshness_class.in_(["current", "aging"]),
            KnowledgeEntry.superseded_by.is_(None),
        )
        .execution_options(populate_existing=True)
    )
    pending = list(result.scalars().all())

    # Budget priority: confidence DESC, last_relevant_at DESC (recent first),
    # entity_ref IS NOT NULL (prefer entity-bound), then trim lowest-priority.
    def _priority_key(e: KnowledgeEntry) -> tuple:
        rel_ts = (e.last_relevant_at or e.created_at).timestamp()
        has_entity = 1 if getattr(e, "entity_ref", None) else 0
        return (e.confidence, rel_ts, has_entity, e.id)

    pending.sort(key=_priority_key, reverse=True)

    # 3. Group by type
    grouped: dict[str, list[str]] = defaultdict(list)
    for entry in pending:
        grouped[entry.entry_type].append(entry.content)

    # Codex R1 HIGH on tk_879dbd5a5a034d0e — force_rebuild MUST compile
    # from an empty base, otherwise stale/dismissed/superseded text from
    # the prior context_document can survive a "rebuild" forever. We
    # preserve previous_context for the audit trail (ContextCompilation.
    # context_before) but feed the compile pipeline the empty string
    # when force_rebuild is set.
    previous_context = project.context_document or ""
    return _CompileInputs(
        project=project,
        pending=pending,
        grouped=dict(grouped),
        previous_context=previous_context,
        compile_base_context="" if force_rebuild else previous_context,
        max_context_words=getattr(project, "kb_max_context_words", 2000) or 2000,
        freshness_updated=freshness_updated,
    )


async def _llm_merge(
    prompt: str,
    *,
    api_key: str,
    model: str,
    provider: str | None,
    base_url: str | None,
) -> str | None:
    """Run the LLM merge. Returns None when the call fails (the caller then
    uses the deterministic merge). Runs with NO transaction or lock held."""
    from sessionfs.judge.providers import call_llm

    try:
        out = await call_llm(
            model=model,
            system=_COMPILE_SYSTEM_PROMPT,
            prompt=prompt,
            api_key=api_key,
            provider=provider,
            base_url=base_url,
        )
        return out.strip()
    except Exception:
        logger.warning("LLM compilation failed, falling back to simple compile", exc_info=True)
        return None


def _enforce_budget(context_after: str, max_context_words: int) -> str:
    """The LLM may ignore the budget hint in the prompt and return an
    over-budget document. Enforce the hard cap with the same trim logic
    _simple_compile uses."""
    if len(context_after.split()) > max_context_words:
        context_after = "\n".join(_trim_to_budget(context_after.splitlines(), max_context_words))
        if not context_after.endswith("\n"):
            context_after += "\n"
    return context_after


async def compile_project_context(
    project_id: str,
    user_id: str,
    db: AsyncSession,
    api_key: str | None = None,
    model: str = "claude-sonnet-4",
    provider: str | None = None,
    base_url: str | None = None,
    force_rebuild: bool = False,
    stats: dict | None = None,
) -> ContextCompilation | None:
    """Compile pending knowledge entries into project context via LLM.

    Phases (tk_bc3c02a63e994717, v0.10.13 fail-closed refactor; GH #70
    plan-then-revalidate):

    Plan (only with an LLM key) — under the project scope lock, run the
        housekeeping and read the pending claims inside a SAVEPOINT, build
        the LLM prompt, then roll the savepoint back and COMMIT the (now
        write-free) transaction. That releases the scope + row locks, so
        the LLM round-trip below blocks no other project writer (on SQLite
        the scope lock is the database-wide writer lock).

    LLM — call the LLM with no transaction or lock held.

    Apply — take the scope lock again, redo the housekeeping, rebuild the
        prompt and compare it with the one the LLM saw. Equal: use the LLM
        output. Different (a writer changed the pending claims or the
        context document meanwhile): discard and re-plan, at most
        _MAX_LLM_COMPILE_ATTEMPTS LLM calls, then fall back to the
        deterministic merge on the current inputs under the lock.

    Single atomic commit — housekeeping UPDATEs, the force_rebuild
        compiled_at reset, project.context_document, compiled_at on the
        pending batch, the ContextCompilation row and the section pages
        all commit ONCE. Any exception before that commit rolls back the
        entire pass, so the prior good projection survives a crash. A
        discarded planning pass writes nothing.

    No pending claims: the housekeeping writes commit on their own and the
    function returns None with the lock released, so a caller that goes on
    to concept generation (LLM) holds nothing.

    `force_rebuild=True` is the /rebuild semantics — "treat every active
    claim as pending and regenerate from scratch"; the destructive reset
    lives INSIDE the atomic pass, so the 2026-05-20 data-loss vector
    (proj_c0242b0fccbd48b4) cannot recur.

    `stats` (optional dict) receives `freshness_updated` from the pass
    that committed.
    """
    from sessionfs.server.services.scope_locks import (
        lock_project_scope_authorized,
    )

    project = await db.get(Project, project_id)
    if not project:
        logger.warning("Project %s not found for compilation", project_id)
        return None

    llm_plan: tuple[str, str | None] | None = None  # (prompt, LLM output)
    llm_calls = 0
    while True:
        # GH #70 — the project SCOPE lock comes first (global order: scope
        # locks, then row locks). It serializes this compile with project
        # merge / transfer and every other project-scoped writer,
        # re-authorizes the caller against the re-read project (a transfer
        # may have landed while we waited) and refuses (409 project_merged)
        # a merged-away project. On SQLite its UPDATE also opens the real
        # transaction, so the SAVEPOINT below is never the outermost one.
        await lock_project_scope_authorized(db, project, user_id=user_id)
        # Row lock so concurrent compile callers serialize (SQLite ignores
        # FOR UPDATE; its writer lock already serializes). Taken outside the
        # savepoint: a row lock taken inside a rolled-back savepoint is lost.
        result = await db.execute(
            select(Project)
            .where(Project.id == project_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        locked_project = result.scalar_one_or_none()
        if locked_project is None:
            logger.warning("Project %s not found for compilation", project_id)
            await db.rollback()
            return None
        project = locked_project

        savepoint = await db.begin_nested()
        try:
            inputs = await _run_compile_housekeeping(
                db, project, project_id, force_rebuild
            )
        except BaseException:
            await savepoint.rollback()
            raise

        if not inputs.pending:
            logger.info("No pending entries for project %s", project_id)
            # Persist the housekeeping and release the scope + row locks
            # now — the caller may go on to LLM concept generation.
            await savepoint.commit()
            await db.commit()
            if stats is not None:
                stats["freshness_updated"] = inputs.freshness_updated
            return None

        if not api_key:
            # Without an API key, do a simple append-based compilation.
            context_after = _simple_compile(
                inputs.compile_base_context, inputs.grouped,
                entries=inputs.pending, max_context_words=inputs.max_context_words,
            )
            break

        prompt = _build_compile_prompt(inputs.compile_base_context, inputs.grouped)
        if llm_plan is not None and llm_plan[0] == prompt:
            # Inputs unchanged since the LLM saw them — apply its output.
            raw = llm_plan[1]
            if raw is None:
                context_after = _simple_compile(
                    inputs.compile_base_context, inputs.grouped,
                    entries=inputs.pending, max_context_words=inputs.max_context_words,
                )
            else:
                context_after = _enforce_budget(raw, inputs.max_context_words)
            break

        if llm_calls >= _MAX_LLM_COMPILE_ATTEMPTS:
            logger.warning(
                "Project %s knowledge changed during %d LLM compile attempts; "
                "using the deterministic merge",
                project_id, llm_calls,
            )
            context_after = _simple_compile(
                inputs.compile_base_context, inputs.grouped,
                entries=inputs.pending, max_context_words=inputs.max_context_words,
            )
            break

        # Planning pass: discard its writes and release every lock before
        # the LLM round-trip.
        await savepoint.rollback()
        _expire_kb_objects(db)
        await db.commit()
        llm_calls += 1
        llm_plan = (
            prompt,
            await _llm_merge(
                prompt, api_key=api_key, model=model,
                provider=provider, base_url=base_url,
            ),
        )

    # Applying this pass: fold the savepoint into the outer transaction (the
    # scope + row locks stay held until the single commit below).
    await savepoint.commit()

    pending = inputs.pending
    context_before = inputs.previous_context
    if stats is not None:
        stats["freshness_updated"] = inputs.freshness_updated

    # Snapshot the active-claim inputs that feed the compiled context. This
    # is intentionally recorded with the compilation row so SoD/audit callers
    # can trace rendered sections back to KB authors outside the LLM output.
    source_result = await db.execute(
        select(KnowledgeEntry).where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.dismissed == False,  # noqa: E712
            KnowledgeEntry.claim_class == "claim",
            KnowledgeEntry.freshness_class.in_(["current", "aging"]),
            KnowledgeEntry.superseded_by.is_(None),
        )
    )
    source_entries_list = list(source_result.scalars().all())
    # Resolve persona attribution per source session in one batch query.
    # KnowledgeEntry has no persona column of its own; the persona that
    # was active when the claim was authored is recorded on the source
    # Session (set by the daemon's active-ticket annotation pipeline).
    session_ids = sorted({e.session_id for e in source_entries_list if e.session_id})
    persona_by_session: dict[str, str | None] = {}
    if session_ids:
        # Cross-project leak defense (Codex R1 MEDIUM): KnowledgeEntry.
        # session_id is a plain string, not a project-validated FK, so a
        # project-scoped claim can carry a session_id from another
        # project. Filter the persona-resolution lookup by Session.
        # project_id == this project AND not-deleted, so personas from
        # foreign or deleted sessions degrade to None instead of leaking.
        rows = (
            await db.execute(
                select(Session.id, Session.persona_name).where(
                    Session.id.in_(session_ids),
                    Session.project_id == project_id,
                    Session.is_deleted == False,  # noqa: E712
                )
            )
        ).all()
        persona_by_session = {sid: pn for sid, pn in rows}
    source_manifest = _build_source_manifest(
        source_entries_list, persona_by_session=persona_by_session
    )

    # 5. Save updated context
    now = datetime.now(timezone.utc)
    project.context_document = context_after
    project.updated_at = now

    # 6. Mark entries as compiled and update relevance + usage
    for entry in pending:
        entry.compiled_at = now
        entry.last_relevant_at = now
        entry.compiled_count = (getattr(entry, "compiled_count", 0) or 0) + 1

    # 7. Save compilation record
    compilation = ContextCompilation(
        project_id=project_id,
        user_id=user_id,
        entries_compiled=len(pending),
        context_before=context_before,
        context_after=context_after,
        source_manifest=source_manifest,
    )
    db.add(compilation)
    # tk_bc3c02a63e994717 — flush instead of commit so we get the
    # autogenerated id (needed for section page references / response
    # shape) without making the writes durable yet. The single all-or-
    # nothing commit happens at the end of this function after the
    # section page work completes. A crash between here and the final
    # commit rolls back EVERYTHING — projection + compilation row +
    # housekeeping writes + (when force_rebuild=True) the compiled_at
    # reset.
    await db.flush()
    await db.refresh(compilation)

    logger.info(
        "Compiled %d entries for project %s (compilation %d, uncommitted)",
        len(pending),
        project_id,
        compilation.id,
    )

    # 8. Create/update section wiki pages per entry type
    from sessionfs.server.db.models import KnowledgePage  # noqa: F811

    slug_map = {
        "decision": "key-decisions",
        "pattern": "patterns",
        "convention": "coding-conventions",
        "bug": "known-issues",
        "dependency": "dependencies",
        "discovery": "discoveries",
    }

    # Rebuild section pages for ALL known types, not just types in the
    # current pending batch. This ensures pages are true projections of
    # the active-claim state — if a claim goes stale or is superseded,
    # the page updates on the next compile even without new pending entries.
    for entry_type, slug in slug_map.items():
        section_title = SECTION_MAP.get(entry_type, f"## {entry_type.title()}").lstrip("# ")

        # Build page content from active claims of this type (not just pending)
        all_of_type = await db.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.project_id == project_id,
                KnowledgeEntry.entry_type == entry_type,
                KnowledgeEntry.dismissed == False,  # noqa: E712
                KnowledgeEntry.claim_class == "claim",
                KnowledgeEntry.freshness_class.in_(["current", "aging"]),
                KnowledgeEntry.superseded_by.is_(None),
            ).order_by(KnowledgeEntry.created_at.desc())
        )
        type_entries = list(all_of_type.scalars().all())

        # If zero active claims for this type, delete the section page
        # (if it exists) and move on. No empty pages.
        if not type_entries:
            await db.execute(
                delete(KnowledgePage).where(
                    KnowledgePage.project_id == project_id,
                    KnowledgePage.slug == slug,
                    KnowledgePage.page_type == "section",
                )
            )
            continue

        # Cap section pages: keep most recent + highest confidence
        section_limit = getattr(project, "kb_section_page_limit", 30) or 30
        original_count = len(type_entries)
        if original_count > section_limit:
            type_entries.sort(
                key=lambda e: (e.confidence, e.created_at.timestamp()),
                reverse=True,
            )
            type_entries = type_entries[:section_limit]

        page_content = f"# {section_title}\n\n"
        for e in type_entries:
            conf = " *(unverified)*" if e.confidence < 0.5 else ""
            page_content += f"- {e.content}{conf}\n"

        if original_count > section_limit:
            dropped = original_count - section_limit
            page_content += f"\n---\n*{dropped} older/lower-confidence entries not shown.*\n"

        # Upsert the section page
        existing_page = await db.execute(
            select(KnowledgePage).where(
                KnowledgePage.project_id == project_id,
                KnowledgePage.slug == slug,
            )
        )
        page = existing_page.scalar_one_or_none()

        if page:
            page.content = page_content
            page.word_count = len(page_content.split())
            page.entry_count = len(type_entries)
            page.updated_at = now
        else:
            db.add(KnowledgePage(
                id=f"kp_{secrets.token_hex(8)}",
                project_id=project_id,
                slug=slug,
                title=section_title,
                page_type="section",
                content=page_content,
                word_count=len(page_content.split()),
                entry_count=len(type_entries),
                auto_generated=True,
            ))

    # (Step 9 cleanup is now redundant — the loop above iterates ALL known
    # types and handles zero-claim deletion inline via the `continue` path.)

    # tk_bc3c02a63e994717 — THE single atomic commit. Everything written
    # since Phase 1's FOR UPDATE lock is in this transaction:
    #   - freshness / decay / retention / auto-promote UPDATEs
    #   - force_rebuild's compiled_at reset (if applicable)
    #   - project.context_document overwrite
    #   - compiled_at + last_relevant_at + compiled_count on the pending batch
    #   - ContextCompilation row insert
    #   - section page upserts + deletions
    # Any exception before this line rolls back ALL of it, so the prior
    # good projection survives a crash.
    await db.commit()
    logger.info(
        "Compile committed for project %s (compilation id=%d)",
        project_id,
        compilation.id,
    )

    return compilation


def _simple_compile(
    context: str,
    grouped: dict[str, list[str]],
    entries: list | None = None,
    max_context_words: int = 2000,
) -> str:
    """Simple append-based compilation without LLM.

    Uses SECTION_MAP for proper section names, merges similar entries,
    marks low-confidence entries as (unverified), and adds a Recent Changes
    section at the bottom.
    """
    # Split context into: verified part, unverified bullets, and strip
    # ## Recent Changes (rebuilt each compile from current batch).
    verified_lines: list[str] = []
    old_unverified: list[str] = []  # bare content (without marker) from old ## Unverified
    current_section = "verified"
    for line in (context or "").splitlines():
        stripped = line.strip()
        if stripped == "## Recent Changes":
            current_section = "recent"
            continue
        if stripped == "## Unverified":
            current_section = "unverified"
            continue
        if current_section != "verified" and stripped.startswith("## "):
            current_section = "verified"
        if current_section == "verified":
            verified_lines.append(line)
        elif current_section == "unverified" and stripped.startswith("- "):
            fact = stripped[2:].strip()
            if fact.lower().startswith("(unverified) "):
                fact = fact[13:].strip()
            old_unverified.append(fact)
    verified_context = "\n".join(verified_lines).rstrip()

    lines = [verified_context] if verified_context else ["# Project Context"]

    # Seed dedup set from verified context only (NOT unverified), so that
    # a newly verified fact can replace an older unverified marker.
    verified_facts: set[str] = set()
    for line in verified_context.splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            verified_facts.add(stripped[2:].strip().lower())

    # Build set of facts that are ONLY low-confidence in this batch.
    # If the same fact appears at both low and high confidence, the
    # high-confidence version wins and goes into the main section.
    low_conf_content: set[str] = set()
    high_conf_content: set[str] = set()
    if entries:
        for e in entries:
            normalized = e.content.strip().lower()
            if e.confidence >= 0.5:
                high_conf_content.add(normalized)
            else:
                low_conf_content.add(normalized)
        low_conf_content -= high_conf_content

    for entry_type, contents in sorted(grouped.items()):
        section_heading = SECTION_MAP.get(entry_type, f"## {entry_type.title()}")
        # Check if section already exists in the context
        if section_heading not in "\n".join(lines):
            lines.append(f"\n{section_heading}")

        # Deduplicate against verified context and current batch;
        # skip low-confidence entries (they go to ## Unverified only)
        for c in contents:
            normalized = c.strip().lower()
            if normalized in low_conf_content:
                continue
            if normalized not in verified_facts:
                verified_facts.add(normalized)
                lines.append(f"- {c}")

    # Rebuild ## Unverified: keep old unverified facts that haven't been
    # promoted to verified, and add new low-confidence entries.
    all_unverified: list[str] = []
    unverified_seen: set[str] = set()
    # Carry forward old unverified facts not now verified
    for fact in old_unverified:
        normalized = fact.strip().lower()
        if normalized not in verified_facts and normalized not in unverified_seen:
            unverified_seen.add(normalized)
            all_unverified.append(fact)
    # Add new low-confidence entries
    if entries:
        for e in [e for e in entries if e.confidence < 0.5]:
            normalized = e.content.strip().lower()
            if normalized not in verified_facts and normalized not in unverified_seen:
                unverified_seen.add(normalized)
                all_unverified.append(e.content)
    if all_unverified:
        lines.append("\n## Unverified")
        for fact in all_unverified:
            lines.append(f"- (unverified) {fact}")

    # Add Recent Changes section with dated changelog
    if entries:
        dated_entries: dict[str, list[str]] = defaultdict(list)
        for e in entries:
            date_str = e.created_at.strftime("%Y-%m-%d") if e.created_at else "unknown"
            dated_entries[date_str].append(f"[{e.entry_type}] {e.content}")

        if dated_entries:
            lines.append("\n## Recent Changes")
            for date_key in sorted(dated_entries.keys(), reverse=True):
                lines.append(f"\n### {date_key}")
                for item in dated_entries[date_key]:
                    lines.append(f"- {item}")

    # Budget enforcement: trim to max_context_words
    full_text = "\n".join(lines) + "\n"
    word_count = len(full_text.split())
    if word_count > max_context_words:
        lines = _trim_to_budget(lines, max_context_words)

    return "\n".join(lines) + "\n"


def _trim_to_budget(lines: list[str], max_words: int) -> list[str]:
    """Trim compiled context to stay within a word budget.

    Strategy: identify sections (## headings), count words per section,
    and remove oldest bullets (from bottom of each section) from the
    longest sections first, keeping at least 3 bullets per section.
    """
    # Parse lines into sections: list of (heading_line, bullet_lines)
    sections: list[tuple[str | None, list[str]]] = []
    current_heading: str | None = None
    current_bullets: list[str] = []

    for line in lines:
        stripped = line.strip()
        if stripped.startswith("## "):
            # Save previous section
            if current_heading is not None or current_bullets:
                sections.append((current_heading, current_bullets))
            current_heading = line
            current_bullets = []
        elif stripped.startswith("### "):
            # Sub-heading treated as a bullet (Recent Changes dates)
            current_bullets.append(line)
        else:
            current_bullets.append(line)

    # Don't forget the last section
    if current_heading is not None or current_bullets:
        sections.append((current_heading, current_bullets))

    def _total_words() -> int:
        total = 0
        for heading, bullets in sections:
            if heading:
                total += len(heading.split())
            total += sum(len(b.split()) for b in bullets)
        return total

    # Iteratively remove the last bullet from the largest section
    while _total_words() > max_words:
        # Find the section with the most bullets (that has more than 3)
        best_idx = -1
        best_count = 0
        for i, (heading, bullets) in enumerate(sections):
            # Count actual bullet lines (starting with "- ")
            bullet_count = sum(1 for b in bullets if b.strip().startswith("- "))
            if bullet_count > 3 and bullet_count > best_count:
                best_count = bullet_count
                best_idx = i

        if best_idx == -1:
            break  # All sections at minimum — can't trim further

        # Remove the last bullet line from this section
        heading, bullets = sections[best_idx]
        # Find last bullet line index
        for j in range(len(bullets) - 1, -1, -1):
            if bullets[j].strip().startswith("- "):
                bullets.pop(j)
                break
        sections[best_idx] = (heading, bullets)

    # Reconstruct lines
    result: list[str] = []
    for heading, bullets in sections:
        if heading is not None:
            result.append(heading)
        result.extend(bullets)
    return result


# ---------------------------------------------------------------------------
# Concept auto-generation
# ---------------------------------------------------------------------------

_CONCEPT_CLUSTER_SYSTEM = """\
You are a knowledge analyst. Given a list of knowledge entries for a software \
project, identify clusters of 5 or more related entries that share a common \
topic. Return ONLY a JSON array of objects with keys: topic (string), slug \
(string, lowercase-hyphenated), entry_count (int), summary (one sentence). \
Return an empty array [] if no clusters are found."""

_CONCEPT_ARTICLE_SYSTEM = """\
You are a technical writer. Write a concise project knowledge article \
(200-400 words, markdown format) about the given topic. Base the article \
strictly on the provided knowledge entries — do not add information that \
isn't supported by the entries. Use clear headings and bullet points where \
appropriate."""


_STOP_WORDS = frozenset({
    "the", "and", "for", "are", "but", "not", "you", "all", "can", "had",
    "her", "was", "one", "our", "out", "has", "have", "been", "from", "with",
    "they", "this", "that", "what", "when", "make", "like", "than", "each",
    "which", "their", "will", "other", "about", "many", "then", "them",
    "these", "some", "would", "into", "more", "also", "must", "should",
    "does", "only", "just", "where", "after", "before", "still", "every",
    "both", "same", "through", "using", "used", "uses", "instead", "already",
    "between", "because", "without", "during", "while", "however", "since",
    "being", "very", "most", "such", "well", "back", "even", "over",
    "need", "take", "come", "could", "good", "new", "now", "way", "may",
    "first", "also", "any", "those", "see", "how", "its", "two", "set",
    "get", "via", "per", "run", "let", "add", "use", "put", "try",
})


def _extract_phrases(content: str) -> list[str]:
    """Extract meaningful 2-3 word phrases from content for clustering.

    Filters stop words and requires at least one word >= 4 chars
    to avoid trivial bigrams like 'from the' or 'in the'.
    """
    words = re.findall(r"[a-z][a-z0-9_]+", content.lower())
    # Filter stop words
    meaningful = [w for w in words if w not in _STOP_WORDS and len(w) >= 3]
    phrases = []
    for i in range(len(meaningful) - 1):
        phrase = f"{meaningful[i]} {meaningful[i + 1]}"
        # Require at least one word >= 4 chars
        if any(len(w) >= 4 for w in (meaningful[i], meaningful[i + 1])):
            phrases.append(phrase)
        # Also try trigrams for more specific topics
        if i + 2 < len(meaningful):
            trigram = f"{meaningful[i]} {meaningful[i + 1]} {meaningful[i + 2]}"
            phrases.append(trigram)
    return phrases


async def check_concept_candidates(
    project_id: str,
    user_id: str,
    db: AsyncSession,
    api_key: str | None = None,
    model: str = "claude-sonnet-4",
    provider: str | None = None,
    base_url: str | None = None,
) -> list[dict]:
    """Find clusters of related entries that could become concept articles.

    Only runs if the project has >= 15 total entries. With an LLM API key,
    asks the model to find clusters. Without one, uses deterministic phrase
    matching (groups entries sharing a 2+ word phrase, requires 5+ entries).
    """
    # Check total entry count
    count_result = await db.execute(
        select(func.count(KnowledgeEntry.id)).where(
            KnowledgeEntry.project_id == project_id,
        )
    )
    total = count_result.scalar() or 0
    if total < 15:
        return []

    # Get active claims only
    result = await db.execute(
        select(KnowledgeEntry).where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.dismissed == False,  # noqa: E712
            KnowledgeEntry.claim_class == "claim",
            KnowledgeEntry.freshness_class.in_(["current", "aging"]),
            KnowledgeEntry.superseded_by.is_(None),
        )
    )
    entries = list(result.scalars().all())
    if len(entries) < 15:
        return []

    if api_key:
        return await _llm_concept_candidates(entries, api_key, model, provider, base_url)

    # Deterministic fallback: group by shared 2+ word phrases
    return _deterministic_concept_candidates(entries)


async def _llm_concept_candidates(
    entries: list[KnowledgeEntry],
    api_key: str,
    model: str,
    provider: str | None,
    base_url: str | None,
) -> list[dict]:
    """Use LLM to identify concept clusters."""
    import json

    from sessionfs.judge.providers import call_llm

    formatted = "\n".join(
        f"- [{e.entry_type}] {e.content}" for e in entries
    )
    prompt = f"Here are {len(entries)} knowledge entries:\n\n{formatted}"

    try:
        raw = await call_llm(
            model=model,
            system=_CONCEPT_CLUSTER_SYSTEM,
            prompt=prompt,
            api_key=api_key,
            provider=provider,
            base_url=base_url,
        )
        # Extract JSON from response (might be wrapped in markdown code block)
        raw = raw.strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)
        candidates = json.loads(raw)
        if not isinstance(candidates, list):
            return []
        # Validate structure
        valid = []
        for c in candidates:
            if all(k in c for k in ("topic", "slug", "entry_count", "summary")):
                valid.append({
                    "topic": str(c["topic"]),
                    "slug": str(c["slug"]),
                    "entry_count": int(c["entry_count"]),
                    "summary": str(c["summary"]),
                })
        return valid
    except Exception:
        logger.warning("LLM concept clustering failed, falling back to deterministic", exc_info=True)
        return _deterministic_concept_candidates(entries)


def _deterministic_concept_candidates(entries: list[KnowledgeEntry]) -> list[dict]:
    """Group entries by shared 2+ word phrases, requiring 5+ entries per group."""
    phrase_to_entries: dict[str, list[KnowledgeEntry]] = defaultdict(list)

    for entry in entries:
        phrases = _extract_phrases(entry.content)
        seen: set[str] = set()
        for phrase in phrases:
            if phrase not in seen:
                seen.add(phrase)
                phrase_to_entries[phrase].append(entry)

    candidates = []
    used_entry_ids: set[int] = set()

    # Sort by cluster size descending
    for phrase, cluster_entries in sorted(
        phrase_to_entries.items(), key=lambda x: len(x[1]), reverse=True
    ):
        if len(cluster_entries) < 5:
            continue
        # Skip if most entries already used
        new_entries = [e for e in cluster_entries if e.id not in used_entry_ids]
        if len(new_entries) < 5:
            continue

        slug = re.sub(r"[^a-z0-9]+", "-", phrase.lower()).strip("-")
        # Generate a meaningful topic name from the phrase
        topic = phrase.replace("_", " ").title()
        candidates.append({
            "topic": topic,
            "slug": slug,
            "entry_count": len(new_entries),
            "summary": f"{len(new_entries)} knowledge entries about {topic.lower()}.",
        })
        for e in new_entries:
            used_entry_ids.add(e.id)

    return candidates


async def generate_concept_article(
    topic: str,
    summary: str,
    entries: list[KnowledgeEntry],
    user_id: str,
    api_key: str | None = None,
    model: str = "claude-sonnet-4",
    provider: str | None = None,
    base_url: str | None = None,
) -> str:
    """Generate a concept article from related entries.

    Uses LLM if api_key is provided, otherwise falls back to a bulleted list.
    """
    formatted = "\n".join(
        f"- [{e.entry_type}] {e.content}" for e in entries
    )

    if api_key:
        from sessionfs.judge.providers import call_llm

        prompt = (
            f"## Topic: {topic}\n\n"
            f"## Summary: {summary}\n\n"
            f"## Related Knowledge Entries ({len(entries)}):\n{formatted}\n\n"
            f"Write the article now."
        )
        try:
            article = await call_llm(
                model=model,
                system=_CONCEPT_ARTICLE_SYSTEM,
                prompt=prompt,
                api_key=api_key,
                provider=provider,
                base_url=base_url,
            )
            return article.strip()
        except Exception:
            logger.warning("LLM article generation failed, using fallback", exc_info=True)

    # Fallback: simple bulleted list
    lines = [f"# {topic}", "", summary, ""]
    for entry in entries:
        lines.append(f"- **[{entry.entry_type}]** {entry.content}")
    return "\n".join(lines)


def _safe_entry_link_ids(
    links: Iterable[KnowledgeLink],
    *,
    page_slug: str | None = None,
) -> list[int]:
    """Cast entry-source KnowledgeLink.source_id to int, skipping malformed rows.

    KnowledgeLink.source_id is a String(64) column. Convention is
    str(KnowledgeEntry.id) when source_type='entry', but legacy rows may carry
    non-numeric values (concept slugs, UUIDs, malformed migration data). Without
    this guard a single bad row raises ValueError; the route's try/except
    catches it but the SQLAlchemy session is left poisoned from partial
    in-progress deletes, and subsequent db.execute calls die uncaught → Cloud
    Run worker kill → plain-text 500. Used across _prune_dead_concept_pages,
    the auto_generate_concepts dismissed-id prepass, and the per-existing-page
    deletion check.
    """
    ids: list[int] = []
    for lk in links:
        if lk.source_type != "entry":
            continue
        try:
            ids.append(int(lk.source_id))
        except (TypeError, ValueError):
            logger.warning(
                "Skipping malformed KnowledgeLink: source_type=entry "
                "but source_id=%r is not int-castable (page=%s)",
                lk.source_id, page_slug,
            )
    return ids


async def _prune_dead_concept_pages(project_id: str, db: AsyncSession) -> int:
    """Delete concept pages whose linked entries have ALL been dismissed.

    Returns the number of pages deleted. Called unconditionally from
    auto_generate_concepts() so dead pages are cleaned up even when no new
    concept candidates exist (e.g., project dropped below the 15-entry
    clustering threshold).

    GH #70 — does NOT commit. A commit here released the project scope
    and row locks halfway through auto_generate_concepts(); the deletes
    are now part of the caller's single final commit.
    """
    result = await db.execute(
        select(KnowledgePage).where(
            KnowledgePage.project_id == project_id,
            KnowledgePage.slug.like("concept/%"),
            KnowledgePage.page_type == "concept",
        )
    )
    concept_pages = list(result.scalars().all())
    if not concept_pages:
        return 0

    deleted = 0
    for page in concept_pages:
        linked_result = await db.execute(
            select(KnowledgeLink).where(
                KnowledgeLink.project_id == project_id,
                KnowledgeLink.target_id == page.id,
                KnowledgeLink.target_type == "page",
            )
        )
        links = list(linked_result.scalars().all())
        if not links:
            # No links at all — orphaned concept page, safe to delete
            await db.delete(page)
            deleted += 1
            logger.info("Pruned orphaned concept page %s", page.slug)
            continue

        linked_entry_ids = _safe_entry_link_ids(links, page_slug=page.slug)
        if not linked_entry_ids:
            continue

        dismissed_check = await db.execute(
            select(func.count(KnowledgeEntry.id)).where(
                KnowledgeEntry.id.in_(linked_entry_ids),
                KnowledgeEntry.dismissed == True,  # noqa: E712
            )
        )
        dismissed_count = dismissed_check.scalar() or 0
        if dismissed_count == len(linked_entry_ids):
            for lk in links:
                await db.delete(lk)
            await db.delete(page)
            deleted += 1
            logger.info("Pruned concept page %s (all %d linked entries dismissed)", page.slug, dismissed_count)

    if deleted:
        await db.flush()
    return deleted


async def auto_generate_concepts(
    project_id: str,
    user_id: str,
    db: AsyncSession,
    api_key: str | None = None,
    model: str = "claude-sonnet-4",
    provider: str | None = None,
    base_url: str | None = None,
) -> list[dict]:
    """Check for concept candidates and create pages for new ones.

    Called after compilation. Returns list of created/refreshed concept summaries.
    """
    # GH #70 — compute candidates and generate articles BEFORE taking the
    # project scope lock. On SQLite the scope lock IS the database-wide
    # writer lock (UPDATE projects SET id = id), and check_concept_candidates
    # / generate_concept_article round-trip to the LLM (network latency ×
    # number of candidates). Holding the writer lock across those calls would
    # block every write in the database for minutes (see services/
    # scope_locks.py). So the reads below are pre-lock snapshots; only the
    # prune + writes + single commit run under the lock, and the plan built
    # here is re-validated under it before anything is written.

    current = await db.get(Project, project_id)
    if current is None:
        # Project was deleted between compile and concept refresh — bail
        # gracefully rather than throwing.
        return []
    candidates = await check_concept_candidates(
        project_id, user_id, db, api_key, model, provider, base_url,
    )

    plans: list[dict] = []

    # ── Bulk prefetch (perf-1: candidates × active_claims → 1 + 1 + 1) ──
    # Fetch the active-claim set ONCE before the loop. Each candidate
    # filters this set in-memory by topic keywords, which is cheap.
    active_result = await db.execute(
        select(KnowledgeEntry).where(
            KnowledgeEntry.project_id == project_id,
            KnowledgeEntry.dismissed == False,  # noqa: E712
            KnowledgeEntry.claim_class == "claim",
            KnowledgeEntry.freshness_class.in_(["current", "aging"]),
            KnowledgeEntry.superseded_by.is_(None),
        )
    )
    active_claims: list[KnowledgeEntry] = list(active_result.scalars().all())

    # Prefetch ALL existing concept pages for the project, keyed by slug.
    candidate_slugs = [f"concept/{c['slug']}" for c in candidates]
    pages_by_slug: dict[str, KnowledgePage] = {}
    if candidate_slugs:
        pages_result = await db.execute(
            select(KnowledgePage).where(
                KnowledgePage.project_id == project_id,
                KnowledgePage.slug.in_(candidate_slugs),
            )
        )
        pages_by_slug = {p.slug: p for p in pages_result.scalars().all()}

    # Bulk-load existing links targeting any of those pages in ONE query
    # instead of N per-page lookups. Group by page_id for the loop.
    existing_page_ids = [p.id for p in pages_by_slug.values()]
    links_by_page: dict[str, list[KnowledgeLink]] = {pid: [] for pid in existing_page_ids}
    if existing_page_ids:
        links_result = await db.execute(
            select(KnowledgeLink).where(
                KnowledgeLink.project_id == project_id,
                KnowledgeLink.target_type == "page",
                KnowledgeLink.target_id.in_(existing_page_ids),
            )
        )
        for lk in links_result.scalars().all():
            links_by_page.setdefault(lk.target_id, []).append(lk)

    # Bulk-resolve which of the linked-entry ids are dismissed (across
    # all pages) so the per-page "all dismissed?" check is a dict lookup.
    all_linked_entry_ids: set[int] = set()
    for lks in links_by_page.values():
        all_linked_entry_ids.update(_safe_entry_link_ids(lks))
    dismissed_entry_ids: set[int] = set()
    if all_linked_entry_ids:
        dismissed_result = await db.execute(
            select(KnowledgeEntry.id).where(
                KnowledgeEntry.id.in_(all_linked_entry_ids),
                KnowledgeEntry.dismissed == True,  # noqa: E712
            )
        )
        dismissed_entry_ids = {row[0] for row in dismissed_result.all()}

    for candidate in candidates:
        concept_slug = f"concept/{candidate['slug']}"

        # Filter the prefetched active-claim set by topic keywords.
        topic_words = candidate["topic"].lower().split()
        matched_entries = [
            e for e in active_claims
            if any(w in e.content.lower() for w in topic_words if len(w) > 3)
        ]
        if not matched_entries:
            matched_entries = active_claims[:10]

        existing_page = pages_by_slug.get(concept_slug)

        if existing_page:
            # Use the prefetched links + dismissed-id set to decide whether
            # the page should be deleted (all linked entries dismissed). The
            # deletion itself happens under the lock in _prune_dead_concept_pages;
            # here we only skip article generation for a page that is going away.
            linked = links_by_page.get(existing_page.id, [])
            if linked:
                linked_entry_ids = _safe_entry_link_ids(linked, page_slug=concept_slug)
                if linked_entry_ids:
                    dismissed_count = sum(
                        1 for eid in linked_entry_ids if eid in dismissed_entry_ids
                    )
                    if dismissed_count == len(linked_entry_ids):
                        logger.info(
                            "Skipping concept page %s (all entries dismissed; "
                            "prune will delete it)",
                            concept_slug,
                        )
                        continue

            # Check if entry count grew >50% — regenerate
            old_count = existing_page.entry_count or 0
            if old_count > 0 and len(matched_entries) <= old_count * 1.5:
                continue  # Not enough growth to regenerate

        # Generate the article BEFORE the scope lock: the LLM round-trip must
        # not hold the SQLite DB-wide writer lock. The plan is re-validated
        # under the lock (source entries may have changed meanwhile).
        article = await generate_concept_article(
            topic=candidate["topic"],
            summary=candidate["summary"],
            entries=matched_entries,
            user_id=user_id,
            api_key=api_key,
            model=model,
            provider=provider,
            base_url=base_url,
        )

        plans.append({
            "action": "update" if existing_page else "create",
            "slug": concept_slug,
            "topic": candidate["topic"],
            "article": article,
            "page_id": existing_page.id if existing_page else None,
            # (id, content) snapshot for the under-lock re-validation.
            "matched": [(e.id, e.content) for e in matched_entries],
        })

    # GH #70 — take the project scope lock for the mutate phase only (prune
    # + writes + single commit). The LLM work above ran pre-lock because on
    # SQLite the scope lock IS the database-wide writer lock.
    from sessionfs.server.services.scope_locks import (
        lock_project_scope_authorized,
    )

    await lock_project_scope_authorized(db, current, user_id=user_id)

    # Re-check the project still exists under the lock (row lock on PG; the
    # scope lock already serialises SQLite).
    locked = await db.execute(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked.scalar_one_or_none() is None:
        return []

    # Always prune dead concept pages regardless of whether new candidates
    # exist (a page whose linked entries were ALL dismissed must go even when
    # the project dropped below the candidacy threshold).
    await _prune_dead_concept_pages(project_id, db)

    if not plans:
        await db.commit()
        return []

    # Re-validate each plan's source entries under the lock: an entry that was
    # dismissed, superseded, or edited since the pre-lock snapshot makes the
    # generated article stale, so drop that plan rather than write stale text.
    all_matched_ids = {eid for plan in plans for eid, _ in plan["matched"]}
    fresh: dict[int, str] = {}
    if all_matched_ids:
        fresh_rows = await db.execute(
            select(KnowledgeEntry.id, KnowledgeEntry.content).where(
                KnowledgeEntry.id.in_(all_matched_ids),
                KnowledgeEntry.dismissed == False,  # noqa: E712
                KnowledgeEntry.claim_class == "claim",
                KnowledgeEntry.freshness_class.in_(["current", "aging"]),
                KnowledgeEntry.superseded_by.is_(None),
            )
        )
        fresh = {row[0]: row[1] for row in fresh_rows.all()}

    now = datetime.now(timezone.utc)
    created: list[dict] = []

    for plan in plans:
        if not all(fresh.get(eid) == content for eid, content in plan["matched"]):
            logger.info(
                "Skipping concept page %s (source entries changed under lock)",
                plan["slug"],
            )
            continue

        if plan["action"] == "update":
            existing_page = (
                await db.execute(
                    select(KnowledgePage).where(
                        KnowledgePage.id == plan["page_id"]
                    )
                )
            ).scalar_one_or_none()
            if existing_page is None:
                # Pruned under the lock (all links dismissed) — skip.
                continue
            existing_page.content = plan["article"]
            existing_page.word_count = len(plan["article"].split())
            existing_page.entry_count = len(plan["matched"])
            existing_page.updated_at = now
            page_id = existing_page.id

            # Re-read the page's CURRENT links under the lock. The pre-lock
            # prefetch (links_by_page) is stale if another /compile committed
            # between the prefetch and the lock: it deleted the old links and
            # re-added the same (entry → page) pairs. Deleting the stale
            # prefetch would be a no-op and re-adding would then hit
            # uq_kl_link. Diff against the live set instead: drop links to
            # entries that left the contributor set, keep (and skip re-adding)
            # the ones already present so a concurrent writer's identical
            # links aren't churned into a unique-violation.
            current_links = (
                await db.execute(
                    select(KnowledgeLink).where(
                        KnowledgeLink.project_id == project_id,
                        KnowledgeLink.target_type == "page",
                        KnowledgeLink.target_id == page_id,
                    )
                )
            ).scalars().all()
            desired = {str(eid) for eid, _ in plan["matched"]}
            live_sources: set[str] = set()
            for lk in current_links:
                if lk.source_id in desired:
                    live_sources.add(lk.source_id)
                else:
                    await db.delete(lk)
        else:
            # Another caller may have created this page between our pre-lock
            # prefetch and the lock (the scope lock serialises writers, but
            # the read ran pre-lock). Skip rather than violate uq_kp_project_slug.
            dupe = (
                await db.execute(
                    select(KnowledgePage.id).where(
                        KnowledgePage.project_id == project_id,
                        KnowledgePage.slug == plan["slug"],
                    )
                )
            ).scalar_one_or_none()
            if dupe is not None:
                continue

            # Create knowledge page
            page_id = f"page_{uuid.uuid4().hex[:16]}"
            page = KnowledgePage(
                id=page_id,
                project_id=project_id,
                slug=plan["slug"],
                title=plan["topic"],
                page_type="concept",
                content=plan["article"],
                word_count=len(plan["article"].split()),
                entry_count=len(plan["matched"]),
                auto_generated=True,
                created_at=now,
                updated_at=now,
            )
            db.add(page)
            live_sources = set()

        # Create knowledge links from entries to concept page, skipping any
        # already present (the update branch diffs against the live set).
        for eid, _ in plan["matched"]:
            if str(eid) in live_sources:
                continue
            db.add(KnowledgeLink(
                project_id=project_id,
                source_type="entry",
                source_id=str(eid),
                target_type="page",
                target_id=page_id,
                link_type="contributes",
                confidence=1.0,
            ))

        created.append({
            "slug": plan["slug"],
            "topic": plan["topic"],
            "word_count": len(plan["article"].split()),
            "entry_count": len(plan["matched"]),
        })

    # One commit for the whole phase (pruning, deletions, creations) — the
    # project scope and row locks are held until here.
    await db.commit()
    if created:
        logger.info(
            "Auto-generated %d concept pages for project %s",
            len(created), project_id,
        )

    return created
