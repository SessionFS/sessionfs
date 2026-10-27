"""Scheduled Confluence publish sweep (GitHub #99, spec §8).

The operator endpoint ``POST /api/v1/admin/sweeps/confluence-publish`` works
through Confluence space mappings that opted in (``publish_context`` and/or
``auto_publish_pages``) for orgs with an enabled connection:

* **Fair rotation.** Mappings are ordered by ``last_attempted_at`` NULLS
  FIRST and each is stamped *before* any publish, so a mapping that fails or
  has many pages moves to the back and can never starve the others. Work is
  bounded per run (``limit`` publish attempts in total, at most
  ``_PER_MAPPING_MAX`` per mapping); pages beyond the bound wait for the next
  run.
* **Entitlement + attribution.** A mapping is skipped (and logged) unless the
  org still has the ``confluence_integration`` feature and the mapping's
  creator is still an active owner/admin of the mapping's org. Every publish
  is attributed to that creator — never to the operator who ran the sweep.
* **Coalescing.** A source is skipped when its content hash (title +
  SessionFS source + template) equals the hash stored at its last publish, or
  when it changed less than the connection's ``batch_window_minutes`` ago
  (several compiles in quick succession become one publish).
* **Failing sources last.** Within a mapping, sources whose last publish
  failed (``doc_page_links.last_push_failed_at``) are ordered after the
  healthy ones, oldest failure first, so always-failing sources can never use
  up the per-mapping budget.
* **Sources.** ``publish_context`` publishes the context tree (the context
  root page, then one page per context section nested under it);
  ``auto_publish_pages`` publishes wiki pages with ``publish = true``.

Each publish runs in its own session through the SAME claim-fenced
create/adopt/update/drift engine as the interactive route
(``routes.confluence.execute_publish``), with deterministic sections only
(never the model) and org DLP applied. A drifted, unlinked, stale or in-flight
source is counted and skipped; a failure is recorded and the batch continues.
Re-running the sweep with nothing changed is a no-op: every source is skipped
as unchanged without any Confluence call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionfs.server.db.models import (
    DocPageLink,
    DocSpaceMapping,
    KnowledgePage,
    OrgDocConnection,
    OrgMember,
    Organization,
    Project,
    User,
)
from sessionfs.server.integrations.docs.promote import (
    ContentTooLarge,
    _fit_sections_to_caps,
    build_deterministic,
    redact_for_publish,
    redact_sections_for_publish,
    source_hash,
)
from sessionfs.server.integrations.docs.templates import (
    DEFAULT_TEMPLATE,
    get_template,
    is_builtin_slug,
)
from sessionfs.server.roles import ADMIN_ROLES

logger = logging.getLogger("sessionfs.confluence_sweep")

# At most this many publish attempts per mapping per run; the rest of a large
# mapping waits for the next run (the mapping has been stamped, so it rotates
# to the back behind every other mapping).
_PER_MAPPING_MAX = 20

# Context sections published per mapping are bounded too.
_MAX_CONTEXT_SECTIONS = 30


@dataclass
class _Candidate:
    source_kind: str
    source_key: str
    changed_at: datetime | None


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


async def _due_mappings(db: AsyncSession, *, limit: int) -> list[tuple[str, str, str, str]]:
    """Opted-in mappings in rotation order: (mapping id, project, org, creator)."""
    rows = await db.execute(
        select(
            DocSpaceMapping.id,
            DocSpaceMapping.project_id,
            DocSpaceMapping.org_id,
            DocSpaceMapping.created_by_user_id,
        )
        .join(Project, Project.id == DocSpaceMapping.project_id)
        .join(OrgDocConnection, OrgDocConnection.org_id == DocSpaceMapping.org_id)
        .where(
            # A mapping only applies while the project is still in the
            # mapping's org (transfers/merges drop it; this is the backstop).
            Project.org_id == DocSpaceMapping.org_id,
            Project.merged_into_project_id.is_(None),
            OrgDocConnection.enabled.is_(True),
            OrgDocConnection.provider == "confluence",
            or_(
                DocSpaceMapping.publish_context.is_(True),
                DocSpaceMapping.auto_publish_pages.is_(True),
            ),
        )
        .order_by(
            DocSpaceMapping.last_attempted_at.is_(None).desc(),
            DocSpaceMapping.last_attempted_at.asc(),
            DocSpaceMapping.id.asc(),
        )
        .limit(limit)
    )
    return [tuple(r) for r in rows.all()]  # type: ignore[misc]


async def _org_entitled(db: AsyncSession, org_id: str) -> bool:
    from sessionfs.server.tier_gate import resolve_org_plan_tier
    from sessionfs.server.tiers import get_features_for_tier

    plan = await resolve_org_plan_tier(org_id, db)
    return "confluence_integration" in get_features_for_tier(plan)


async def _creator_is_admin(db: AsyncSession, org_id: str, user_id: str) -> User | None:
    """The mapping creator, when still an active owner/admin of the org."""
    user = await db.get(User, user_id)
    if user is None or not user.is_active:
        return None
    role = (
        await db.execute(
            select(OrgMember.role).where(
                OrgMember.org_id == org_id, OrgMember.user_id == user_id
            )
        )
    ).scalars().first()
    if role is None or role not in ADMIN_ROLES:
        return None
    return user


async def _candidates(
    db: AsyncSession, mapping: DocSpaceMapping, project: Project
) -> list[_Candidate]:
    from sessionfs.server.routes import confluence as cr
    from sessionfs.server.services.rules import split_context_sections

    out: list[_Candidate] = []
    if mapping.publish_context and (project.context_document or "").strip():
        compiled_at = await cr._last_compile_at(project.id, db)
        changed = compiled_at or _aware(project.updated_at)
        out.append(_Candidate("context_root", "root", changed))
        sections = list(split_context_sections(project.context_document or "").keys())
        for key in sections[:_MAX_CONTEXT_SECTIONS]:
            out.append(_Candidate("context_section", key, changed))
    if mapping.auto_publish_pages:
        rows = (
            await db.execute(
                select(KnowledgePage.slug, KnowledgePage.updated_at)
                .where(
                    KnowledgePage.project_id == project.id,
                    KnowledgePage.publish.is_(True),
                )
                .order_by(KnowledgePage.updated_at.asc(), KnowledgePage.slug.asc())
            )
        ).all()
        out.extend(_Candidate("wiki_page", slug, _aware(updated)) for slug, updated in rows)

    # Sources whose last publish failed go last (oldest failure first), so a
    # set of always-failing sources can never use up the per-mapping budget
    # ahead of the healthy ones. A success clears the failure stamp.
    failed_at = {
        (kind, key): _aware(when)
        for kind, key, when in (
            await db.execute(
                select(
                    DocPageLink.source_kind,
                    DocPageLink.source_key,
                    DocPageLink.last_push_failed_at,
                ).where(
                    DocPageLink.project_id == project.id,
                    DocPageLink.last_push_failed_at.is_not(None),
                )
            )
        ).all()
    }
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    indexed = list(enumerate(out))
    indexed.sort(
        key=lambda pair: (
            (pair[1].source_kind, pair[1].source_key) in failed_at,
            failed_at.get((pair[1].source_kind, pair[1].source_key)) or epoch,
            pair[0],
        )
    )
    return [candidate for _, candidate in indexed]


class _Skip(Exception):
    def __init__(self, counter: str) -> None:
        self.counter = counter
        super().__init__(counter)


async def _publish_one(
    db: AsyncSession,
    *,
    mapping_id: str,
    candidate: _Candidate,
    actor_id: str,
    dashboard_url: str,
    now: datetime,
) -> str:
    """Publish one source if due; return an outcome counter name.

    Raises ``_Skip`` for a source that is not due or must not be touched, and
    ``HTTPException`` (from the shared engine) or any other exception for a
    failure.
    """
    from sessionfs.server.routes import confluence as cr

    mapping = await db.get(DocSpaceMapping, mapping_id)
    if mapping is None:
        raise _Skip("skipped_unmapped")
    project = await db.get(Project, mapping.project_id)
    conn = await cr._get_connection(mapping.org_id, db)
    actor = await db.get(User, actor_id)
    if project is None or conn is None or not conn.enabled or actor is None:
        raise _Skip("skipped_unmapped")
    if project.org_id != mapping.org_id:
        raise _Skip("skipped_unmapped")
    org = await db.get(Organization, mapping.org_id)

    template_slug = mapping.template or conn.default_template
    if not is_builtin_slug(template_slug):
        template_slug = DEFAULT_TEMPLATE
    template = get_template(template_slug)
    assert template is not None

    try:
        title, sources = await cr._collect_sources(
            project, candidate.source_kind, candidate.source_key, db
        )
    except HTTPException:
        raise _Skip("skipped_missing_source")
    content_hash = source_hash(title, sources, template_slug)

    link = await cr._get_link(project.id, candidate.source_kind, candidate.source_key, db)
    if link is not None:
        if link.status == "unlinked":
            raise _Skip("skipped_unlinked")
        if link.status != "failed" and not cr._link_is_current(
            link, project.org_id, conn.site_url
        ):
            raise _Skip("skipped_stale")
        if link.status == "drifted":
            raise _Skip("drifted")
        if cr._claim_is_live(link, now):
            raise _Skip("skipped_in_progress")
        if link.status == "linked" and link.last_content_hash == content_hash:
            raise _Skip("skipped_unchanged")

    window = timedelta(minutes=max(0, conn.batch_window_minutes or 0))
    if candidate.changed_at is not None and now - candidate.changed_at < window:
        raise _Skip("skipped_recent")

    sections = _fit_sections_to_caps(build_deterministic(template, sources))
    await cr.execute_publish(
        db,
        cr.PublishRequest(
            project=project,
            conn=conn,
            mapping=mapping,
            org=org,
            source_kind=candidate.source_kind,
            source_key=candidate.source_key,
            title=redact_for_publish(title, org),
            sections=redact_sections_for_publish(sections, org),
            template_slug=template_slug,
            template_name=template.name,
            content_hash=content_hash,
            actor=actor,
            actor_type="sweep",
            dashboard_url=dashboard_url,
        ),
    )
    return "published"


def _new_result() -> dict:
    return {
        "selected_mappings": 0,
        "attempted": 0,
        "published": 0,
        "skipped_unchanged": 0,
        "skipped_recent": 0,
        "skipped_in_progress": 0,
        "skipped_unlinked": 0,
        "skipped_stale": 0,
        "skipped_missing_source": 0,
        "skipped_unmapped": 0,
        "skipped_unentitled": 0,
        "skipped_creator_not_admin": 0,
        "deferred": 0,
        "drifted": 0,
        "failed": 0,
        "errors": [],
        "drifted_pages": [],
    }


async def run_confluence_publish_sweep(
    db: AsyncSession,
    *,
    limit: int,
    session_factory: async_sessionmaker[AsyncSession],
    dashboard_url: str,
    now: datetime | None = None,
) -> dict:
    """Run one bounded, fair pass over the opted-in mappings.

    ``db`` is used only for the read-only mapping selection (materialized,
    then released); stamping and each publish run in fresh sessions from
    ``session_factory`` so one source's commit can never drag another's work
    in or out. ``limit`` bounds the publish attempts of the whole run.
    """
    now = now or datetime.now(timezone.utc)
    mappings = await _due_mappings(db, limit=max(1, limit))
    await db.rollback()

    result = _new_result()
    result["selected_mappings"] = len(mappings)
    budget = max(1, limit)

    for mapping_id, project_id, org_id, creator_id in mappings:
        if budget <= 0:
            break
        # Stamp BEFORE any work so this mapping rotates to the back even if
        # what follows fails or runs out of budget.
        async with session_factory() as stamp_db:
            await stamp_db.execute(
                update(DocSpaceMapping)
                .where(DocSpaceMapping.id == mapping_id)
                .values(last_attempted_at=now)
                .execution_options(synchronize_session=False)
            )
            await stamp_db.commit()

            if not await _org_entitled(stamp_db, org_id):
                logger.info(
                    "Confluence sweep: org %s no longer has confluence_integration; "
                    "skipping mapping %s",
                    org_id,
                    mapping_id,
                )
                result["skipped_unentitled"] += 1
                continue
            if await _creator_is_admin(stamp_db, org_id, creator_id) is None:
                logger.warning(
                    "Confluence sweep: mapping %s creator %s is no longer an admin "
                    "of org %s; skipping (re-save the mapping as a current admin)",
                    mapping_id,
                    creator_id,
                    org_id,
                )
                result["skipped_creator_not_admin"] += 1
                continue
            mapping = await stamp_db.get(DocSpaceMapping, mapping_id)
            project = await stamp_db.get(Project, project_id)
            if mapping is None or project is None:
                result["skipped_unmapped"] += 1
                continue
            candidates = await _candidates(stamp_db, mapping, project)
            await stamp_db.rollback()

        per_mapping = 0
        for candidate in candidates:
            if budget <= 0 or per_mapping >= _PER_MAPPING_MAX:
                result["deferred"] += 1
                continue
            try:
                async with session_factory() as page_db:
                    outcome = await _publish_one(
                        page_db,
                        mapping_id=mapping_id,
                        candidate=candidate,
                        actor_id=creator_id,
                        dashboard_url=dashboard_url,
                        now=now,
                    )
                result[outcome] += 1
                result["attempted"] += 1
                budget -= 1
                per_mapping += 1
            except _Skip as skip:
                result[skip.counter] += 1
                if skip.counter == "drifted":
                    result["drifted_pages"].append(
                        {
                            "project_id": project_id,
                            "source_kind": candidate.source_kind,
                            "source_key": candidate.source_key,
                        }
                    )
            except HTTPException as exc:
                result["attempted"] += 1
                budget -= 1
                per_mapping += 1
                detail = exc.detail if isinstance(exc.detail, dict) else {}
                code = detail.get("error")
                if code == "confluence_page_drifted":
                    result["drifted"] += 1
                    result["drifted_pages"].append(
                        {
                            "project_id": project_id,
                            "source_kind": candidate.source_kind,
                            "source_key": candidate.source_key,
                        }
                    )
                elif code == "publish_in_progress":
                    result["skipped_in_progress"] += 1
                else:
                    result["failed"] += 1
                    result["errors"].append(
                        {
                            "project_id": project_id,
                            "source_kind": candidate.source_kind,
                            "source_key": candidate.source_key,
                            "error": str(code or exc.status_code),
                            "message": str(detail.get("message") or "")[:200],
                        }
                    )
            except ContentTooLarge as exc:
                result["attempted"] += 1
                budget -= 1
                per_mapping += 1
                result["failed"] += 1
                result["errors"].append(
                    {
                        "project_id": project_id,
                        "source_kind": candidate.source_kind,
                        "source_key": candidate.source_key,
                        "error": "content_too_large",
                        "message": str(exc)[:200],
                    }
                )
            except Exception as exc:  # noqa: BLE001 - one failure must not stop the batch
                logger.warning(
                    "Confluence publish failed for %s/%s/%s",
                    project_id,
                    candidate.source_kind,
                    candidate.source_key,
                    exc_info=True,
                )
                result["attempted"] += 1
                budget -= 1
                per_mapping += 1
                result["failed"] += 1
                result["errors"].append(
                    {
                        "project_id": project_id,
                        "source_kind": candidate.source_kind,
                        "source_key": candidate.source_key,
                        "error": type(exc).__name__,
                        "message": str(exc)[:200],
                    }
                )
    return result
