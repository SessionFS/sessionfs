"""Confluence integration routes (GitHub #99).

Org-scoped connection + mapping + health under
``/api/v1/orgs/{org_id}/integrations/confluence``; page/context promotion under
``/api/v1/projects/{pid}/confluence``. Enterprise-only (feature flag
``confluence_integration``). One-way: SessionFS creates/updates Confluence
pages and reads them on demand; Confluence edits never write back (they surface
as drift, which the caller resolves explicitly).

The Confluence router must be registered before ``projects.router`` in
``app.py`` because the latter is a ``/{git_remote_normalized:path}`` catch-all.

Built-in templates only in the MVP (no org-custom Confluence templates), so
template resolution is a simple built-in slug check with the connection default
force-picked at connect time. Every published paragraph runs through the strict
Markdown → storage-format converter, which is the safety boundary: no raw
Confluence markup or macro can be injected from a wiki page or a model rewrite.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import (
    AuthContext,
    get_current_user,
    require_scope,
)
from sessionfs.server.config import dashboard_link_base
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    ContextCompilation,
    DocPageLink,
    DocSpaceMapping,
    KnowledgeEntry,
    KnowledgePage,
    OrgAuditEvent,
    OrgDocConnection,
    OrgMember,
    Organization,
    Project,
    Session,
    User,
)
from sessionfs.server.integrations.docs import (
    DEFAULT_TEMPLATE,
    ConfluenceClient,
    DocumentationError,
    DocumentationNotFound,
    Template,
    get_template,
    is_builtin_slug,
    list_templates,
)
from sessionfs.server.integrations.docs.confluence import (
    markdown_to_storage,
    sessionfs_label,
    storage_text_length,
    storage_to_text,
    validate_site_url,
)
from sessionfs.server.integrations.docs.promote import (
    ContentTooLarge,
    DocSources,
    build_preview,
    payload_hash,
    redact_for_publish,
    redact_sections_for_publish,
    sections_to_storage,
    source_hash,
)
from sessionfs.server.llm_keys import decrypt_secret, encrypt_secret
from sessionfs.server.roles import has_minimum_role
from sessionfs.server.services.rules import split_context_sections
from sessionfs.server.tier_gate import (
    UserContext,
    check_project_feature,
    get_user_context,
    resolve_org_plan_tier,
)
from sessionfs.server.tiers import get_features_for_tier, get_minimum_tier_for_feature

router = APIRouter(prefix="/api/v1/projects", tags=["confluence"])
org_router = APIRouter(prefix="/api/v1/orgs", tags=["confluence"])

logger = logging.getLogger("sessionfs.api")

_ORG_NOT_FOUND = "Organization not found"

# Confluence space keys are uppercase alphanumeric/underscore (``DEV``, ``TEAM_1``).
_SPACE_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,49}$")

# Space/page picker bounds, mirroring the Jira project picker.
_SEARCH_MAX_CHARS = 100
_SEARCH_DEFAULT_LIMIT = 50
_SEARCH_MAX_LIMIT = 100

# A pending publish claim older than this is treated as abandoned and can be
# reclaimed (the worker that made it is presumed dead).
_CLAIM_STALE_SECONDS = 300

# Confluence Cloud caps a page body near 2,000,000 characters; refuse anything
# over this conservative 1 MiB before any outbound call so we never send a body
# Confluence will reject.
_MAX_STORAGE_CHARS = 1_000_000

# A timed-out create's page can lag Confluence's search index, so after a
# label search comes up empty, wait this long and search once more before
# falling back to the title lookup (a just-created page is adopted instead of
# duplicated).
_ADOPT_RETRY_DELAY_SECONDS = 2.0

# The whole outbound publish sequence (drift check, create/adopt, label,
# update) shares ONE deadline, well inside the 5-minute claim reclaim window,
# so a slow Confluence can never hold a claim open past it.
_PUBLISH_DEADLINE_SECONDS = 60.0

# A page found by label/title is only adopted when it was created no earlier
# than this before the unconfirmed publish began (clock skew allowance).
_ADOPT_SKEW = timedelta(minutes=2)

# Drift 409 diff bounds.
_DIFF_MAX_LINES = 200
_DIFF_MAX_CHARS = 20_000


def _aware(dt: datetime | None) -> datetime | None:
    """Normalize a stored timestamp to aware UTC (SQLite drops tzinfo)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _claim_is_live(link: DocPageLink, now: datetime | None = None) -> bool:
    """True while a publish claim is held and younger than the stale window."""
    if link.claim_token is None:
        return False
    claimed = _aware(link.claimed_at)
    if claimed is None:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - claimed).total_seconds() < _CLAIM_STALE_SECONDS


# ── Request bodies ──


class ConfluenceConnectionPut(BaseModel):
    site_url: str = Field(max_length=255)
    account_email: str = Field(max_length=255)
    api_token: str = Field(max_length=2048)
    default_template: str = Field(max_length=50)
    batch_window_minutes: int = Field(15, ge=1, le=1440)


class ConfluenceTestBody(BaseModel):
    """Test a Confluence connection without persisting it."""

    site_url: str = Field(max_length=255)
    account_email: str = Field(max_length=255)
    api_token: str = Field(max_length=2048)


class ConfluenceMappingPut(BaseModel):
    space_key: str = Field(max_length=50)
    parent_page_id: str = Field(max_length=64)
    template: str | None = Field(None, max_length=50)
    publish_context: bool = False
    auto_publish_pages: bool = False

    @field_validator("space_key")
    @classmethod
    def _valid_space_key(cls, value: str) -> str:
        key = value.strip()
        if not _SPACE_KEY_RE.match(key):
            raise ValueError("space_key must look like DEV or TEAM_1")
        return key

    @field_validator("parent_page_id")
    @classmethod
    def _valid_parent_page_id(cls, value: str) -> str:
        page_id = value.strip()
        if not page_id:
            raise ValueError("parent_page_id is required")
        if not page_id.isdigit():
            raise ValueError("parent_page_id must be a numeric Confluence page id")
        return page_id


class ConfluenceParagraph(BaseModel):
    text: str = Field(max_length=10000)
    citations: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("citations")
    @classmethod
    def _citation_lengths(cls, value: list[str]) -> list[str]:
        if any(len(c) > 64 for c in value):
            raise ValueError("each citation must be at most 64 characters")
        return value


class ConfluenceSection(BaseModel):
    key: str = Field(max_length=64)
    heading: str = Field("", max_length=200)
    paragraphs: list[ConfluenceParagraph] = Field(default_factory=list, max_length=50)


class ConfluencePreviewBody(BaseModel):
    kind: Literal["wiki_page", "context"]
    slug: str | None = Field(None, max_length=100)
    # Explicit per-preview template choice (same meaning as publish's
    # ``template_override``).
    template_override: str | None = Field(None, max_length=50)
    # Deprecated alias of ``template_override`` (publish's ``template`` is an
    # echo, so the preview field was renamed for consistency).
    template: str | None = Field(None, max_length=50)


class ConfluencePublishBody(BaseModel):
    kind: Literal["wiki_page", "context"]
    slug: str | None = Field(None, max_length=100)
    title: str = Field(min_length=1, max_length=255)
    sections: list[ConfluenceSection] = Field(min_length=1, max_length=20)
    confirm: bool
    # Echo of the preview's ``template.slug`` / ``template.version`` (version
    # is ``null`` for built-ins). When either is sent and differs from the
    # current resolution (override → mapping → connection default), publish
    # refuses with 409 template_changed: the mapping or default moved since
    # the draft was previewed.
    template: str | None = Field(None, max_length=50)
    template_version: int | None = None
    # An explicit per-publish template choice (the caller picked it, it is
    # not an echo). An unknown slug is a strict 422.
    template_override: str | None = Field(None, max_length=50)


class ConfluenceResolveBody(BaseModel):
    action: Literal["overwrite", "unlink"]


def _resolve_source(kind: str, slug: str | None) -> tuple[str, str]:
    """Map the request's ``kind``/``slug`` to a (source_kind, source_key)."""
    if kind == "wiki_page":
        if not slug or not slug.strip():
            raise HTTPException(422, "slug is required for a wiki_page publish")
        return "wiki_page", slug.strip()
    if slug and slug.strip():
        return "context_section", slug.strip()
    return "context_root", "root"


# ── Org-scoped authorization ──


def _insufficient_role(current_role: str | None) -> dict:
    return {
        "error": "insufficient_role",
        "current_role": current_role or "none",
        "required_role": "admin",
        "message": "Managing the Confluence integration requires the admin role.",
    }


async def _authorize_org_confluence(
    db: AsyncSession,
    user: User,
    org_id: str,
) -> tuple[Organization, str]:
    """Return (org, caller_role) for an org owner/admin.

    Service keys are rejected upstream by ``get_current_user`` (the org-level
    integration holds the Atlassian credential, so only user keys may touch it).
    Reads are admin-only too, since connection details, mappings and health all
    expose credential/config state. Mirrors ``_authorize_org_jira``.
    """
    org = await db.get(Organization, org_id)
    role = (
        await db.execute(
            select(OrgMember.role).where(
                OrgMember.org_id == org_id,
                OrgMember.user_id == user.id,
            )
        )
    ).scalars().first()
    if org is None or role is None:
        raise HTTPException(404, _ORG_NOT_FOUND)
    if not has_minimum_role(role, "admin"):
        raise HTTPException(status_code=403, detail=_insufficient_role(role))

    plan = await resolve_org_plan_tier(org_id, db)
    if "confluence_integration" not in get_features_for_tier(plan):
        required = get_minimum_tier_for_feature("confluence_integration")
        raise HTTPException(
            status_code=403,
            detail={
                "error": "upgrade_required",
                "feature": "confluence_integration",
                "plan_scope": "org",
                "current_tier": plan.value,
                "required_tier": required,
                "upgrade_url": "https://sessionfs.dev/pricing",
                "message": (
                    f"This feature requires the org's plan to be {required} or above."
                ),
            },
        )
    return org, role


def _org_audit(
    db: AsyncSession,
    *,
    org: Organization,
    actor: User,
    actor_role: str | None,
    event_type: str,
    target_type: str,
    target_id: str,
    before: dict | None = None,
    after: dict | None = None,
    actor_type: str = "user",
) -> None:
    db.add(
        OrgAuditEvent(
            id=f"oae_{uuid.uuid4().hex[:24]}",
            org_id=org.id,
            org_name_snapshot=org.name,
            event_type=event_type,
            actor_user_id=actor.id,
            actor_email_snapshot=actor.email,
            actor_role_at_time=actor_role if actor_role else actor_type,
            target_type=target_type,
            target_id=target_id,
            before=json.dumps(before) if before is not None else None,
            after=json.dumps(after) if after is not None else None,
        )
    )


def _token_hint(encrypted: str) -> str:
    try:
        plain = decrypt_secret(encrypted)
    except Exception:
        return "••••"
    if len(plain) <= 4:
        return "••••"
    return "••••" + plain[-4:]


async def _get_connection(org_id: str, db: AsyncSession) -> OrgDocConnection | None:
    return (
        await db.execute(
            select(OrgDocConnection).where(OrgDocConnection.org_id == org_id)
        )
    ).scalar_one_or_none()


def _connection_response(conn: OrgDocConnection) -> dict:
    return {
        "connected": True,
        "provider": conn.provider,
        "site_url": conn.site_url,
        "account_email": conn.account_email,
        "has_token": True,
        "token_hint": _token_hint(conn.encrypted_api_token),
        "default_template": conn.default_template,
        "batch_window_minutes": conn.batch_window_minutes,
        "enabled": conn.enabled,
        "last_verified_at": conn.last_verified_at.isoformat() if conn.last_verified_at else None,
        "connected_by_user_id": conn.connected_by_user_id,
    }


async def _get_mapping(
    project_id: str, org_id: str, db: AsyncSession
) -> DocSpaceMapping | None:
    return (
        await db.execute(
            select(DocSpaceMapping).where(
                DocSpaceMapping.project_id == project_id,
                DocSpaceMapping.org_id == org_id,
            )
        )
    ).scalar_one_or_none()


def _mapping_response(m: DocSpaceMapping) -> dict:
    return {
        "project_id": m.project_id,
        "space_key": m.space_key,
        "space_id": m.space_id,
        "parent_page_id": m.parent_page_id,
        "template": m.template,
        "publish_context": m.publish_context,
        "auto_publish_pages": m.auto_publish_pages,
    }


async def _get_link(
    project_id: str,
    source_kind: str,
    source_key: str,
    db: AsyncSession,
    *,
    populate_existing: bool = False,
) -> DocPageLink | None:
    stmt = select(DocPageLink).where(
        DocPageLink.project_id == project_id,
        DocPageLink.source_kind == source_kind,
        DocPageLink.source_key == source_key,
    )
    if populate_existing:
        stmt = stmt.execution_options(populate_existing=True)
    return (await db.execute(stmt)).scalar_one_or_none()


def _link_is_current(link: DocPageLink, org_id: str | None, site_url: str) -> bool:
    return link.org_id == org_id and link.site_url == site_url


def _link_stale_detail() -> dict:
    return {
        "error": "confluence_link_stale",
        "message": (
            "This source's Confluence link belongs to another organization or "
            "site; publish, drift checks and resolve are blocked so an "
            "unrelated page is never touched. A project admin can unlink it "
            "to publish again."
        ),
    }


def _assert_link_current(link: DocPageLink, org_id: str | None, site_url: str) -> None:
    """409 ``confluence_link_stale`` when the link points at another org/site.

    Mirrors Jira's ``_assert_link_current``: a project transfer or a
    connection re-point to another site must never read or write a page on
    an unrelated site. Only unlink works on a stale link.
    """
    if not _link_is_current(link, org_id, site_url):
        raise HTTPException(status_code=409, detail=_link_stale_detail())


def _page_url(site_url: str, space_key: str, page_id: str | None) -> str | None:
    if not page_id:
        return None
    return f"{site_url}/wiki/spaces/{space_key}/pages/{page_id}"


def _link_response(
    link: DocPageLink, site_url: str, space_key: str, org_id: str | None = None
) -> dict:
    stale = not _link_is_current(link, org_id if org_id is not None else link.org_id, site_url)
    # The page lives in the space it was created in, which may differ from
    # the mapping's current space.
    link_space = link.space_key or space_key
    return {
        "id": link.id,
        "source_kind": link.source_kind,
        "source_key": link.source_key,
        "page_id": link.page_id,
        "page_url": None if stale else _page_url(site_url, link_space, link.page_id),
        "version": link.version,
        "status": link.status,
        "stale": stale,
        "publishing": _claim_is_live(link),
        "template_slug": link.template_slug,
        "template_version": link.template_version,
        "last_pushed_at": link.last_pushed_at.isoformat() if link.last_pushed_at else None,
        "last_push_failed_at": (
            link.last_push_failed_at.isoformat() if link.last_push_failed_at else None
        ),
        "last_push_failure": link.last_push_failure,
    }


def _resolve_doc_template_slug(inherited: str, override: str | None) -> str:
    """Effective built-in template slug (override → mapping → connection default).

    Built-ins only in the MVP, so the override is a strict built-in check; the
    connection default is force-picked from built-ins at connect time, so a
    non-built-in inherited slug can only arise from stale data and falls back.
    """
    slug = inherited if (override is None or override == inherited) else override
    if override is not None and override != inherited and not is_builtin_slug(override):
        raise HTTPException(422, f"unknown template: {override}")
    if not is_builtin_slug(slug):
        return DEFAULT_TEMPLATE
    return slug


# ── Source collection ──

_BACKLINK_RE = re.compile(r"\[\[([^\]\n]{1,200})\]\]")


def _extract_backlinks(text: str) -> list[str]:
    seen: list[str] = []
    for m in _BACKLINK_RE.finditer(text or ""):
        slug = m.group(1).strip()
        if slug and slug not in seen:
            seen.append(slug)
    return seen


def _active_claims_clause(project_id: str):
    """Active claims: class ``claim``, not dismissed, not superseded."""
    return (
        KnowledgeEntry.project_id == project_id,
        KnowledgeEntry.dismissed == False,  # noqa: E712
        KnowledgeEntry.superseded_by.is_(None),
        KnowledgeEntry.claim_class == "claim",
    )


async def _claims_by_type(project_id: str, db: AsyncSession) -> dict[str, list[str]]:
    rows = (
        await db.execute(
            select(KnowledgeEntry.entry_type, KnowledgeEntry.content)
            .where(
                *_active_claims_clause(project_id),
                KnowledgeEntry.freshness_class.in_(["current", "aging"]),
            )
            .order_by(
                KnowledgeEntry.entry_type,
                KnowledgeEntry.created_at.desc(),
                KnowledgeEntry.id.desc(),
            )
            .limit(40)
        )
    ).all()
    out: dict[str, list[str]] = {}
    for entry_type, content in rows:
        out.setdefault(entry_type, []).append(content)
    return out


async def _session_evidence(project: Project, db: AsyncSession) -> list[str]:
    """The ten most recent sessions explicitly attributed to the project.

    Only ``Session.project_id`` counts (as in Jira's ``collect_sources``):
    sessions that merely share one of the project's git remotes belong to
    users who may have no standing on the project (other orgs, or members
    without a grant in a restricted org), and their titles must never be
    published to a Confluence space.
    """
    rows = (
        await db.execute(
            select(Session.id, Session.title)
            .where(
                Session.project_id == project.id,
                Session.is_deleted == False,  # noqa: E712
            )
            .order_by(Session.created_at.desc(), Session.id.desc())
            .limit(10)
        )
    ).all()
    return [title or f"session {sid[:8]}" for sid, title in rows]


async def _compile_metadata(project_id: str, db: AsyncSession) -> str:
    row = (
        await db.execute(
            select(
                ContextCompilation.entries_compiled,
                ContextCompilation.compiled_at,
                ContextCompilation.trigger,
            )
            .where(ContextCompilation.project_id == project_id)
            .order_by(
                ContextCompilation.compiled_at.desc(),
                ContextCompilation.id.desc(),
            )
            .limit(1)
        )
    ).one_or_none()
    if row is None:
        return ""
    entries, compiled_at, trigger = row
    ts = compiled_at.isoformat() if compiled_at else "unknown"
    return f"last compiled {ts} · {entries} entries · {trigger}"


async def _wiki_slugs(project_id: str, db: AsyncSession) -> list[str]:
    return list(
        (
            await db.execute(
                select(KnowledgePage.slug)
                .where(KnowledgePage.project_id == project_id)
                .order_by(KnowledgePage.slug)
            )
        ).scalars().all()
    )


def _section_title(project: Project, section_key: str) -> str:
    # Prefixed with the project name so two projects mapped to one space
    # never collide on a page title.
    return f"{project.name} · {section_key.replace('_', ' ').strip().title()}"


async def _collect_sources(
    project: Project,
    source_kind: str,
    source_key: str,
    db: AsyncSession,
) -> tuple[str, DocSources]:
    """Collect the SessionFS source for a page/context publish into ``DocSources``."""
    if source_kind == "wiki_page":
        page = (
            await db.execute(
                select(KnowledgePage).where(
                    KnowledgePage.project_id == project.id,
                    KnowledgePage.slug == source_key,
                )
            )
        ).scalar_one_or_none()
        if page is None:
            raise HTTPException(404, "Wiki page not found")
        title = page.title
        # Two wiki pages of one project with the same title would collide on
        # one Confluence title under the same parent: disambiguate with the
        # (unique) slug so each source gets its own page.
        twin = (
            await db.execute(
                select(KnowledgePage.id).where(
                    KnowledgePage.project_id == project.id,
                    func.lower(KnowledgePage.title) == (page.title or "").lower(),
                    KnowledgePage.slug != page.slug,
                ).limit(1)
            )
        ).first()
        if twin is not None:
            title = f"{page.title} ({page.slug})"
        return title, DocSources(
            page_body=page.content or "",
            sections=[],
            claims_by_type={},
            session_evidence=[],
            compile_metadata="",
            backlinks=_extract_backlinks(page.content or ""),
        )

    doc = project.context_document or ""
    sections = split_context_sections(doc)
    if source_kind == "context_section":
        if source_key not in sections:
            raise HTTPException(
                404,
                {
                    "error": f"Section '{source_key}' not found",
                    "available_slugs": sorted(sections.keys()),
                },
            )
        body = sections[source_key]
        title = _section_title(project, source_key)
        section_pairs = [(source_key.replace("_", " ").strip().title(), body)]
        page_body = body
    else:
        title = f"{project.name} Context"
        section_pairs = [
            (slug.replace("_", " ").strip().title(), body)
            for slug, body in sections.items()
        ]
        page_body = doc

    return title, DocSources(
        page_body=page_body,
        sections=section_pairs,
        claims_by_type=await _claims_by_type(project.id, db),
        session_evidence=await _session_evidence(project, db),
        compile_metadata=await _compile_metadata(project.id, db),
        backlinks=await _wiki_slugs(project.id, db),
    )


# ── Evidence footer ──


async def _claim_freshness_counts(
    project_id: str, db: AsyncSession
) -> tuple[int, int, int]:
    """Active claims bucketed by freshness: (current, aging, stale).

    Only claims that can shape a page count — class ``claim``, not dismissed,
    not superseded — so the footer never inflates its number with notes,
    evidence or superseded entries.
    """
    rows = (
        await db.execute(
            select(KnowledgeEntry.freshness_class, func.count())
            .where(*_active_claims_clause(project_id))
            .group_by(KnowledgeEntry.freshness_class)
        )
    ).all()
    counts = {freshness: count for freshness, count in rows}
    return (
        counts.get("current", 0),
        counts.get("aging", 0),
        counts.get("stale", 0),
    )


async def _last_compile_ts(project_id: str, db: AsyncSession) -> str | None:
    compiled_at = await _last_compile_at(project_id, db)
    return compiled_at.isoformat() if compiled_at else None


async def _last_compile_at(project_id: str, db: AsyncSession) -> datetime | None:
    compiled_at = (
        await db.execute(
            select(ContextCompilation.compiled_at)
            .where(ContextCompilation.project_id == project_id)
            .order_by(
                ContextCompilation.compiled_at.desc(),
                ContextCompilation.id.desc(),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return _aware(compiled_at)


def _view_source_url(
    dashboard_url: str, project_id: str, source_kind: str, source_key: str
) -> str:
    if not dashboard_url:
        return ""
    pid = quote(project_id, safe="")
    key = quote(source_key, safe="")
    if source_kind == "wiki_page":
        return f"{dashboard_url}/projects/{pid}?tab=knowledge&sub=pages&page={key}"
    if source_kind == "context_section":
        return f"{dashboard_url}/projects/{pid}?tab=context&slug={key}"
    return f"{dashboard_url}/projects/{pid}?tab=context"


def _footer_text(
    *,
    template_name: str,
    version: int | None,
    claims_current: int,
    claims_aging: int,
    claims_stale: int,
    last_compile_ts: str | None,
    view_url: str,
) -> str:
    """Provenance footer, real data only. Rendered through the strict converter."""
    total = claims_current + claims_aging + claims_stale
    parts = [
        "Generated by SessionFS",
        (
            f"Shaped by {total} active claims ({claims_current} current, "
            f"{claims_aging} aging, {claims_stale} stale)"
        ),
    ]
    if last_compile_ts:
        parts.append(f"last compile {last_compile_ts}")
    label = template_name if version is None else f"{template_name} v{version}"
    parts.append(f"template {label}")
    if view_url:
        parts.append(f"[View source in SessionFS →]({view_url})")
    return " · ".join(parts)


def _backlink_resolver(links: dict[str, str]) -> Callable[[str], str | None]:
    def resolve(slug: str) -> str | None:
        return links.get(slug)

    return resolve


async def _backlink_urls(
    project_id: str,
    space_key: str,
    site_url: str,
    db: AsyncSession,
    org_id: str | None = None,
) -> dict[str, str]:
    """Confluence URLs of this project's linked wiki pages on the current site."""
    stmt = select(
        DocPageLink.source_key, DocPageLink.page_id, DocPageLink.space_key
    ).where(
        DocPageLink.project_id == project_id,
        DocPageLink.source_kind == "wiki_page",
        DocPageLink.status == "linked",
        DocPageLink.page_id.is_not(None),
        DocPageLink.site_url == site_url,
    )
    if org_id is not None:
        stmt = stmt.where(DocPageLink.org_id == org_id)
    rows = (await db.execute(stmt)).all()
    return {
        slug: f"{site_url}/wiki/spaces/{link_space or space_key}/pages/{page_id}"
        for slug, page_id, link_space in rows
    }


async def _build_footer(
    *,
    project: Project,
    source_kind: str,
    source_key: str,
    template_name: str,
    version: int | None,
    db: AsyncSession,
    dashboard_url: str,
) -> str:
    """Render the provenance footer into storage HTML (real data only)."""
    view_url = _view_source_url(dashboard_url, project.id, source_kind, source_key)
    current, aging, stale = await _claim_freshness_counts(project.id, db)
    last_compile = await _last_compile_ts(project.id, db)
    text = _footer_text(
        template_name=template_name,
        version=version,
        claims_current=current,
        claims_aging=aging,
        claims_stale=stale,
        last_compile_ts=last_compile,
        view_url=view_url,
    )
    return markdown_to_storage(text)


# ── Error envelopes ──


def _publish_in_progress() -> dict:
    return {
        "error": "publish_in_progress",
        "message": "Another publish for this source is already in progress.",
    }


def _drifted(
    link: DocPageLink,
    remote_version: int | None = None,
    diff: list[str] | None = None,
    diff_truncated: bool = False,
) -> dict:
    return {
        "error": "confluence_page_drifted",
        "link_id": link.id,
        "page_id": link.page_id,
        "stored_version": link.version,
        "remote_version": remote_version,
        "diff": diff or [],
        "diff_truncated": diff_truncated,
        "message": (
            "The Confluence page was edited since the last publish; resolve the "
            "conflict (overwrite or unlink) before publishing again."
        ),
    }


def _content_too_large(section: str) -> HTTPException:
    return HTTPException(
        422,
        {
            "error": "content_too_large",
            "section": section,
            "message": (
                f"The {section!r} section is too large to publish as a single "
                "Confluence page; nothing was dropped."
            ),
        },
    )


def _content_too_large_for_confluence() -> HTTPException:
    return HTTPException(
        422,
        {
            "error": "content_too_large_for_confluence",
            "message": (
                "The Confluence page body would exceed 1 MiB (Confluence "
                "Cloud's ~2,000,000 character limit); nothing was published."
            ),
        },
    )


def _unified_diff(old: str, new: str) -> list[str]:
    if old == new:
        return []
    return list(
        difflib.unified_diff(
            old.splitlines(),
            new.splitlines(),
            fromfile="published",
            tofile="confluence",
            lineterm="",
        )
    )


def _bounded_drift_diff(
    published_storage: str | None, remote_storage: str | None
) -> tuple[list[str], bool]:
    """What changed in Confluence since our last publish, bounded.

    Both sides are reduced to text with ``storage_to_text`` so Confluence's
    re-serialization of the storage markup does not show up as noise. Capped
    at ``_DIFF_MAX_LINES`` lines / ``_DIFF_MAX_CHARS`` characters.
    """
    if published_storage is None or remote_storage is None:
        return [], False
    lines = _unified_diff(
        storage_to_text(published_storage), storage_to_text(remote_storage)
    )
    out: list[str] = []
    used = 0
    for line in lines:
        line = line[:500]
        if len(out) >= _DIFF_MAX_LINES or used + len(line) > _DIFF_MAX_CHARS:
            return out, True
        out.append(line)
        used += len(line)
    return out, False


# ── Claim fencing ──


async def _acquire_publish_claim(
    db: AsyncSession,
    *,
    project_id: str,
    org_id: str,
    site_url: str,
    source_kind: str,
    source_key: str,
) -> tuple[DocPageLink, str, str]:
    """Claim the publish slot for a source; return ``(link, action, token)``.

    Every publish — create, adopt AND update — holds a claim: a fresh
    ``claim_token`` written with a compare-and-swap, so two publishes of the
    same source can never run outbound at once (a concurrent publish 409s
    ``publish_in_progress`` instead of seeing our own version bump as drift).

    ``action`` is ``"update"`` (linked row with a page), ``"create"`` (no row,
    or a severed link) or ``"adopt"`` (the outcome of an earlier publish is
    unconfirmed: the caller adopts the page by stored id, label or title, or
    creates). A claim older than ``_CLAIM_STALE_SECONDS`` is abandoned and is
    reclaimed with a CAS on the token it read. A link that belongs to another
    org/site is never claimed (409 ``confluence_link_stale``).
    """
    now = datetime.now(timezone.utc)
    row = await _get_link(project_id, source_kind, source_key, db, populate_existing=True)

    if row is None:
        token = secrets.token_hex(16)
        db.add(
            DocPageLink(
                id=f"dpl_{uuid.uuid4().hex[:24]}",
                org_id=org_id,
                project_id=project_id,
                site_url=site_url,
                source_kind=source_kind,
                source_key=source_key,
                # In-flight placeholder: the outcome is unknown until the
                # publish settles (a crashed worker leaves an adoptable row).
                status="unknown",
                claim_token=token,
                claimed_at=now,
                last_pushed_at=now,  # when this (possibly unconfirmed) publish began
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            row = await _get_link(project_id, source_kind, source_key, db, populate_existing=True)
            if row is None:
                raise HTTPException(409, _publish_in_progress())
        else:
            link = await _get_link(
                project_id, source_kind, source_key, db, populate_existing=True
            )
            assert link is not None
            return link, "create", token

    if row.status not in ("unlinked", "failed"):
        # Anything that may reference a remote page must be on this org/site.
        _assert_link_current(row, org_id, site_url)

    if row.claim_token is not None:
        if _claim_is_live(row, now):
            raise HTTPException(409, _publish_in_progress())
        # Abandoned claim: reclaim with a CAS on the token we read.
        if row.status == "linked" and row.page_id:
            action = "update"
        elif row.status == "drifted":
            raise HTTPException(409, _drifted(row))
        else:
            action = "adopt"
        token = secrets.token_hex(16)
        res = await db.execute(
            update(DocPageLink)
            .where(
                DocPageLink.id == row.id,
                DocPageLink.claim_token == row.claim_token,
            )
            .values(claim_token=token, claimed_at=now)
            .execution_options(synchronize_session=False)
        )
        if res.rowcount != 1:  # type: ignore[attr-defined]
            await db.rollback()
            raise HTTPException(409, _publish_in_progress())
        await db.commit()
        link = await _get_link(project_id, source_kind, source_key, db, populate_existing=True)
        assert link is not None
        return link, action, token

    if row.status == "drifted":
        raise HTTPException(409, _drifted(row))

    token = secrets.token_hex(16)
    if row.status == "linked" and row.page_id:
        values: dict = {"claim_token": token, "claimed_at": now}
        action = "update"
    elif row.status in ("unknown", "linked"):
        # ``unknown`` (or a linked row that somehow lost its page id): an
        # earlier publish may have created the page — adopt before creating.
        # ``last_pushed_at`` is left alone: it records when that publish began.
        values = {"claim_token": token, "claimed_at": now, "status": "unknown"}
        action = "adopt"
    else:
        # ``unlinked`` (severed by an admin, or the page was deleted in
        # Confluence) or ``failed`` (the last create was definitively refused,
        # so nothing exists): a fresh create on the current site, never a
        # label/title adoption.
        values = {
            "claim_token": token,
            "claimed_at": now,
            "status": "unknown",
            "page_id": None,
            "version": None,
            "last_content_hash": None,
            "last_published_storage": None,
            "site_url": site_url,
            "org_id": org_id,
            "last_pushed_at": now,
        }
        action = "create"
    res = await db.execute(
        update(DocPageLink)
        .where(
            DocPageLink.id == row.id,
            DocPageLink.status == row.status,
            DocPageLink.claim_token.is_(None),
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:  # type: ignore[attr-defined]
        await db.rollback()
        raise HTTPException(409, _publish_in_progress())
    await db.commit()
    link = await _get_link(project_id, source_kind, source_key, db, populate_existing=True)
    assert link is not None
    return link, action, token


async def _fenced_mark_linked(
    db: AsyncSession,
    *,
    link_id: str,
    claim_token: str,
    page_id: str,
    version: int,
    now: datetime,
    user_id: str,
    content_hash: str,
    storage: str,
    template_slug: str,
    site_url: str,
    org_id: str,
    space_key: str,
) -> bool:
    """Settle a successful publish (create, adopt or update), fenced on token."""
    res = await db.execute(
        update(DocPageLink)
        .where(
            DocPageLink.id == link_id,
            DocPageLink.claim_token == claim_token,
        )
        .values(
            status="linked",
            claim_token=None,
            claimed_at=None,
            page_id=page_id,
            version=version,
            last_content_hash=content_hash,
            last_published_storage=storage,
            template_slug=template_slug,
            template_version=None,
            site_url=site_url,
            space_key=space_key,
            org_id=org_id,
            last_pushed_at=now,
            last_pushed_by_user_id=user_id,
            last_push_failed_at=None,
            last_push_failure=None,
        )
        .execution_options(synchronize_session=False)
    )
    return res.rowcount == 1  # type: ignore[attr-defined]


async def _fenced_mark_unknown(
    db: AsyncSession,
    *,
    link_id: str,
    claim_token: str,
    page_id: str | None = None,
    failure: str | None = None,
) -> bool:
    """Keep the row as ``unknown`` (outcome unconfirmed), fenced on token.

    Never deletes: the page may exist. When the create response came back
    (``page_id``) it is stored so the next publish adopts it by id.
    """
    values: dict = {"status": "unknown", "claim_token": None, "claimed_at": None}
    if page_id:
        values["page_id"] = page_id
    if failure:
        values["last_push_failed_at"] = datetime.now(timezone.utc)
        values["last_push_failure"] = failure[:300]
    res = await db.execute(
        update(DocPageLink)
        .where(DocPageLink.id == link_id, DocPageLink.claim_token == claim_token)
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    return res.rowcount == 1  # type: ignore[attr-defined]


async def _fenced_mark_failed(
    db: AsyncSession, *, link_id: str, claim_token: str, failure: str
) -> bool:
    """A definitive refusal: nothing exists remotely for this source.

    Status ``failed`` makes the next publish create again without any
    label/title adoption; the failure stamp orders it last in the sweep.
    """
    res = await db.execute(
        update(DocPageLink)
        .where(DocPageLink.id == link_id, DocPageLink.claim_token == claim_token)
        .values(
            status="failed",
            claim_token=None,
            claimed_at=None,
            last_push_failed_at=datetime.now(timezone.utc),
            last_push_failure=failure[:300],
        )
        .execution_options(synchronize_session=False)
    )
    return res.rowcount == 1  # type: ignore[attr-defined]


async def _fenced_mark_missing(
    db: AsyncSession, *, link_id: str, claim_token: str, page_id: str | None
) -> bool:
    """Sever a link whose page is gone in Confluence, fenced on the token."""
    res = await db.execute(
        update(DocPageLink)
        .where(DocPageLink.id == link_id, DocPageLink.claim_token == claim_token)
        .values(
            status="unlinked",
            claim_token=None,
            claimed_at=None,
            page_id=None,
            version=None,
            last_content_hash=None,
            last_published_storage=None,
            last_push_failed_at=datetime.now(timezone.utc),
            last_push_failure=f"Confluence page {page_id} no longer exists"[:300],
        )
        .execution_options(synchronize_session=False)
    )
    return res.rowcount == 1  # type: ignore[attr-defined]


async def _fenced_release_update(
    db: AsyncSession, *, link_id: str, claim_token: str, failure: str
) -> bool:
    """Release an update claim (the link stays ``linked``) and record why."""
    res = await db.execute(
        update(DocPageLink)
        .where(DocPageLink.id == link_id, DocPageLink.claim_token == claim_token)
        .values(
            claim_token=None,
            claimed_at=None,
            last_push_failed_at=datetime.now(timezone.utc),
            last_push_failure=failure[:300],
        )
        .execution_options(synchronize_session=False)
    )
    return res.rowcount == 1  # type: ignore[attr-defined]


async def _fenced_mark_drifted(
    db: AsyncSession, *, link_id: str, claim_token: str
) -> bool:
    res = await db.execute(
        update(DocPageLink)
        .where(DocPageLink.id == link_id, DocPageLink.claim_token == claim_token)
        .values(status="drifted", claim_token=None, claimed_at=None)
        .execution_options(synchronize_session=False)
    )
    return res.rowcount == 1  # type: ignore[attr-defined]


# ── Shared publish engine (interactive route + scheduled sweep) ──


@dataclass
class PublishRequest:
    """Everything one publish needs; the sections are final and DLP-redacted."""

    project: Project
    conn: OrgDocConnection
    mapping: DocSpaceMapping
    org: Organization | None
    source_kind: str
    source_key: str
    title: str
    sections: list[dict]
    template_slug: str
    template_name: str
    content_hash: str
    actor: User
    actor_type: str
    dashboard_url: str


def _raw_chars(sections: list[dict]) -> int:
    total = 0
    for section in sections:
        if not isinstance(section, dict):
            continue
        total += len(str(section.get("heading") or ""))
        for para in section.get("paragraphs") or []:
            if isinstance(para, dict):
                total += len(str(para.get("text") or ""))
    return total


async def _publish_parent_id(
    db: AsyncSession, req: PublishRequest
) -> str:
    """Where a new page goes: a context section nests under the context root
    page when that page is linked on this site, else the mapping parent."""
    if req.source_kind == "context_section":
        root = await _get_link(req.project.id, "context_root", "root", db)
        if (
            root is not None
            and root.status == "linked"
            and root.page_id
            and _link_is_current(root, req.project.org_id, req.conn.site_url)
        ):
            return root.page_id
    return req.mapping.parent_page_id


def _parse_ts(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return _aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
    except ValueError:
        return None


def _adoption_block(
    page,
    *,
    space_id: str,
    account_id: str | None,
    started_at: datetime | None,
    parent_id: str | None,
) -> str | None:
    """Why a page found by label/title must not be adopted (None = adopt).

    A label or title match alone is not proof this connection created the
    page for this source: the page must be in the mapped space, authored by
    the connection's account, created no earlier than the unconfirmed publish
    began (minus clock skew) and — for a title match — under the expected
    parent.
    """
    if page.space_id and page.space_id != space_id:
        return "The matching Confluence page is in another space."
    if account_id and page.author_id and page.author_id != account_id:
        return "The matching Confluence page was not created by this connection's account."
    created = _parse_ts(page.created_at)
    if started_at is not None and created is not None and created < started_at - _ADOPT_SKEW:
        return "The matching Confluence page predates the unconfirmed publish."
    if parent_id is not None and page.parent_id and page.parent_id != parent_id:
        return "The matching Confluence page is under a different parent page."
    return None


class _AdoptRejected(Exception):
    pass


class _TitleConflict(Exception):
    def __init__(self, page_id: str) -> None:
        self.page_id = page_id
        super().__init__(page_id)


def _title_conflict_detail(page_id: str | None) -> dict:
    return {
        "error": "confluence_title_conflict",
        "page_id": page_id,
        "message": (
            "A Confluence page with this source's title already belongs to "
            "another SessionFS source, so it was not adopted. Rename the page "
            "or change the mapping's parent, then publish again."
        ),
    }


def _superseded_detail() -> dict:
    return {
        "error": "publish_superseded",
        "message": (
            "This publish's claim was superseded by a newer publish; no link "
            "was changed."
        ),
    }


def _definitive_refusal(exc: DocumentationError) -> bool:
    """A 4xx answer means Confluence did not create anything (408/429 aside)."""
    code = getattr(exc, "status_code", None)
    return isinstance(code, int) and 400 <= code < 500 and code not in (408, 429)


async def _page_linked_elsewhere(
    db: AsyncSession, *, site_url: str, page_id: str, link_id: str
) -> bool:
    row = (
        await db.execute(
            select(DocPageLink.id).where(
                DocPageLink.site_url == site_url,
                DocPageLink.page_id == page_id,
                DocPageLink.id != link_id,
            ).limit(1)
        )
    ).first()
    return row is not None


class _PageMissing(Exception):
    """The link's own page is gone (404) in Confluence."""


def _page_missing_detail(page_id: str | None) -> dict:
    return {
        "error": "confluence_page_missing",
        "page_id": page_id,
        "message": (
            "The linked Confluence page no longer exists (or this connection "
            "can no longer see it). The link was severed; publish again to "
            "create a fresh page."
        ),
    }


async def execute_publish(db: AsyncSession, req: PublishRequest) -> dict:
    """Create/adopt/update the Confluence page for one source.

    The claim is taken before any outbound call; the whole outbound sequence
    shares one ``_PUBLISH_DEADLINE_SECONDS`` deadline; every settle write is
    fenced on the claim token. A page that may exist is never orphaned: any
    failure after a create keeps the link as ``unknown`` with the page id.
    Raises ``HTTPException`` with the same error codes for both callers.
    """
    project, conn, mapping = req.project, req.conn, req.mapping
    org_id = project.org_id
    assert org_id is not None

    # Raw-size pre-check before the (CPU-bound) Markdown conversion: storage
    # is never shorter than the raw text, so anything over the cap can't fit.
    if _raw_chars(req.sections) > _MAX_STORAGE_CHARS:
        raise _content_too_large_for_confluence()

    backlinks = await _backlink_urls(
        project.id, mapping.space_key, conn.site_url, db, org_id=org_id
    )
    footer = await _build_footer(
        project=project,
        source_kind=req.source_kind,
        source_key=req.source_key,
        template_name=req.template_name,
        version=None,
        db=db,
        dashboard_url=req.dashboard_url,
    )
    try:
        storage = sections_to_storage(
            req.sections, _backlink_resolver(backlinks), footer=footer
        )
    except ContentTooLarge as exc:
        raise _content_too_large(exc.section) from exc
    if storage_text_length(storage) > _MAX_STORAGE_CHARS:
        raise _content_too_large_for_confluence()

    parent_id = await _publish_parent_id(db, req)

    link, action, claim_token = await _acquire_publish_claim(
        db,
        project_id=project.id,
        org_id=org_id,
        site_url=conn.site_url,
        source_kind=req.source_kind,
        source_key=req.source_key,
    )
    link_id = link.id
    stored_page_id = link.page_id
    stored_version = link.version or 0
    published_storage = link.last_published_storage
    started_at = _aware(link.last_pushed_at)
    label = sessionfs_label(project.id, req.source_kind, req.source_key)
    title = req.title
    progress: dict = {"page_id": None, "creating": False}

    client = ConfluenceClient(
        conn.site_url, conn.account_email, decrypt_secret(conn.encrypted_api_token)
    )

    loop = asyncio.get_running_loop()
    deadline = loop.time() + _PUBLISH_DEADLINE_SECONDS

    def remaining() -> float:
        return max(0.001, deadline - loop.time())

    async def locate() -> tuple[str, Any, str]:
        """Phase 1: read-only. Returns (step, page, space_id)."""
        space_id = mapping.space_id
        if not space_id:
            space_id = (await client.get_space(mapping.space_key)).id
        if action == "update":
            try:
                remote = await client.get_page(stored_page_id, include_body=True)
            except DocumentationNotFound as exc:
                raise _PageMissing() from exc
            if remote.version > stored_version:
                return "drift", remote, space_id
            return "update", remote, space_id

        if action == "adopt":
            # Only reached when an earlier outcome was unconfirmed (timeout,
            # lost response, 5xx) — never after a definitive refusal.
            if stored_page_id:
                # The create response came back before a later step failed:
                # this is our page.
                try:
                    existing = await client.get_page(stored_page_id)
                except DocumentationNotFound as exc:
                    raise _PageMissing() from exc
                if existing.space_id and existing.space_id != space_id:
                    raise _AdoptRejected("The linked Confluence page moved to another space.")
                return "adopt_trusted", existing, space_id
            candidate = await client.find_page_by_label(mapping.space_key, label)
            if candidate is None:
                # Search can lag a just-created page; look once more.
                await asyncio.sleep(_ADOPT_RETRY_DELAY_SECONDS)
                candidate = await client.find_page_by_label(mapping.space_key, label)
            by_title = False
            if candidate is None:
                # The create may have landed but the label never did.
                candidate = await client.find_page_by_title(space_id, title)
                by_title = candidate is not None
            if candidate is not None:
                existing = await client.get_page(candidate)
                who = await client.validate_connection()
                block = _adoption_block(
                    existing,
                    space_id=space_id,
                    account_id=who.get("account_id"),
                    started_at=started_at,
                    parent_id=parent_id if by_title else None,
                )
                if block is not None:
                    raise _AdoptRejected(block)
                return "adopt", existing, space_id
        return "create", None, space_id

    async def apply(step: str, found: Any, space_id: str) -> tuple[str, Any]:
        """Phase 2: the writes."""
        if step == "update":
            page = await client.update_page(stored_page_id, found.version, title, storage)
            return "updated", page
        if step in ("adopt", "adopt_trusted"):
            progress["page_id"] = found.id
            await client.add_label(found.id, label)
            page = await client.update_page(found.id, found.version, title, storage)
            return "adopted", page
        progress["creating"] = True
        created = await client.create_page(space_id, parent_id, title, storage)
        progress["page_id"] = created.id
        await client.add_label(created.id, label)
        return "created", created

    now = datetime.now(timezone.utc)
    try:
        step, found, space_id = await asyncio.wait_for(locate(), timeout=remaining())
        if step == "drift":
            outcome, result = "drift", found
        else:
            if step == "adopt" and await _page_linked_elsewhere(
                db, site_url=conn.site_url, page_id=found.id, link_id=link_id
            ):
                # A page found by label/title already belongs to another
                # source (same title under the same parent, or another project
                # in the same space). Never adopt it.
                raise _TitleConflict(found.id)
            outcome, result = await asyncio.wait_for(
                apply(step, found, space_id), timeout=remaining()
            )
    except asyncio.TimeoutError as exc:
        reason = "Confluence did not respond within the publish deadline"
        if action == "update":
            await _fenced_release_update(
                db, link_id=link_id, claim_token=claim_token, failure=reason
            )
        else:
            await _fenced_mark_unknown(
                db,
                link_id=link_id,
                claim_token=claim_token,
                page_id=progress["page_id"],
                failure=reason,
            )
        await db.commit()
        raise HTTPException(
            504,
            {
                "error": "confluence_timeout",
                "message": (
                    "Confluence did not respond within 60 s; the page may or may "
                    "not have been changed. Publishing again adopts it."
                ),
            },
        ) from exc
    except _PageMissing as exc:
        # The linked page was deleted in Confluence (or is no longer visible
        # to the connection). Sever the link and say so; the next publish
        # creates a fresh page, and the sweep stops retrying it (unlinked).
        await _fenced_mark_missing(
            db, link_id=link_id, claim_token=claim_token, page_id=stored_page_id
        )
        if req.org is not None:
            _org_audit(
                db,
                org=req.org,
                actor=req.actor,
                actor_role=None,
                actor_type=req.actor_type,
                event_type="confluence_page_missing",
                target_type="confluence_page",
                target_id=req.source_key,
                before={"page_id": stored_page_id, "source_kind": req.source_kind},
                after={"status": "unlinked"},
            )
        await db.commit()
        raise HTTPException(409, _page_missing_detail(stored_page_id)) from exc
    except _TitleConflict as exc:
        await _fenced_mark_failed(
            db,
            link_id=link_id,
            claim_token=claim_token,
            failure="The matching Confluence page belongs to another SessionFS source",
        )
        await db.commit()
        raise HTTPException(409, _title_conflict_detail(exc.page_id)) from exc
    except _AdoptRejected as exc:
        await _fenced_mark_unknown(db, link_id=link_id, claim_token=claim_token)
        await db.commit()
        raise HTTPException(
            409, {"error": "confluence_adopt_rejected", "message": str(exc)}
        ) from exc
    except DocumentationError as exc:
        failure = str(exc)[:300]
        if action == "update":
            await _fenced_release_update(
                db, link_id=link_id, claim_token=claim_token, failure=failure
            )
            await db.commit()
        elif (
            progress["creating"]
            and progress["page_id"] is None
            and _definitive_refusal(exc)
        ):
            # Confluence definitively refused the create (4xx, e.g. a page
            # with this title already exists): nothing was created. Record it
            # as ``failed`` so the next publish creates again and NEVER adopts
            # by label/title (that could take over another source's page).
            marked = await _fenced_mark_failed(
                db, link_id=link_id, claim_token=claim_token, failure=failure
            )
            await db.commit()
            if not marked:
                raise HTTPException(409, _superseded_detail()) from exc
            raise HTTPException(
                409,
                {
                    "error": "confluence_create_refused",
                    "message": (
                        f"Confluence refused to create the page ({failure}). A page "
                        "with this title may already exist in the space: rename "
                        "the source or change the mapping, then publish again."
                    ),
                },
            ) from exc
        else:
            # Keep the row as ``unknown`` with the failure recorded — never
            # delete it. A page may exist (adopt, a failure after the create,
            # or a 5xx/timeout that landed anyway), and the recorded failure
            # lets the sweep order this source behind the healthy ones.
            marked = await _fenced_mark_unknown(
                db,
                link_id=link_id,
                claim_token=claim_token,
                page_id=progress["page_id"],
                failure=failure,
            )
            await db.commit()
            if not marked:
                raise HTTPException(
                    409,
                    {
                        "error": "publish_superseded",
                        "message": (
                            "This publish's claim was superseded by a newer "
                            "publish; no link was changed."
                        ),
                    },
                ) from exc
        raise HTTPException(
            502, {"error": "confluence_upstream_error", "message": str(exc)}
        ) from exc
    finally:
        await client.aclose()

    if outcome == "drift":
        remote = result
        await _fenced_mark_drifted(db, link_id=link_id, claim_token=claim_token)
        await db.commit()
        diff, truncated = _bounded_drift_diff(
            published_storage, getattr(remote, "storage", None)
        )
        drifted_link = await db.get(DocPageLink, link_id, populate_existing=True)
        raise HTTPException(
            409,
            _drifted(
                drifted_link or link,
                getattr(remote, "version", None),
                diff,
                truncated,
            ),
        )

    page = result
    try:
        marked = await _fenced_mark_linked(
            db,
            link_id=link_id,
            claim_token=claim_token,
            page_id=page.id,  # type: ignore[attr-defined]
            version=page.version,  # type: ignore[attr-defined]
            now=now,
            user_id=req.actor.id,
            content_hash=req.content_hash,
            storage=storage,
            template_slug=req.template_slug,
            site_url=conn.site_url,
            org_id=org_id,
            space_key=mapping.space_key,
        )
    except IntegrityError as exc:
        # uq_doc_page_link_site_page: another source linked this page while
        # we were outbound. Never share a page between two sources.
        await db.rollback()
        await _fenced_mark_failed(
            db,
            link_id=link_id,
            claim_token=claim_token,
            failure="The Confluence page is linked to another SessionFS source",
        )
        await db.commit()
        raise HTTPException(409, _title_conflict_detail(page.id)) from exc  # type: ignore[attr-defined]
    if not marked:
        await db.rollback()
        raise HTTPException(
            409,
            {
                "error": "publish_superseded",
                "message": (
                    "This publish's claim was superseded by a newer publish; "
                    "the link was not changed."
                ),
                "page_id": page.id,  # type: ignore[attr-defined]
            },
        )

    page_url = _page_url(conn.site_url, mapping.space_key, page.id)  # type: ignore[attr-defined]
    event_type = "confluence_page_created" if outcome == "created" else "confluence_page_updated"
    if req.org is not None:
        _org_audit(
            db,
            org=req.org,
            actor=req.actor,
            actor_role=None,
            actor_type=req.actor_type,
            event_type=event_type,
            target_type="confluence_page",
            target_id=req.source_key,
            after={"page_id": page.id, "page_url": page_url},  # type: ignore[attr-defined]
        )
    await db.commit()

    return {
        "project_id": project.id,
        "provider": "confluence",
        "source_kind": req.source_kind,
        "source_key": req.source_key,
        "page_id": page.id,  # type: ignore[attr-defined]
        "page_url": page_url,
        "version": page.version,  # type: ignore[attr-defined]
        "outcome": outcome,
        "last_pushed_at": now.isoformat(),
        "payload_hash": payload_hash(req.sections),
        "content_hash": req.content_hash,
        "template": {"slug": req.template_slug, "name": req.template_name, "version": None},
    }


# ── Org routes: connection ──


@org_router.get("/{org_id}/integrations/confluence")
async def get_confluence_connection(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _authorize_org_confluence(db, user, org_id)
    conn = await _get_connection(org_id, db)
    if conn is None:
        return {"connected": False, "has_token": False}
    return _connection_response(conn)


@org_router.put("/{org_id}/integrations/confluence")
async def put_confluence_connection(
    org_id: str,
    body: ConfluenceConnectionPut,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org, role = await _authorize_org_confluence(db, user, org_id)

    if not body.account_email.strip():
        raise HTTPException(422, "account_email is required")
    if not body.api_token.strip():
        raise HTTPException(422, "api_token is required")
    # The connection default is force-picked from the built-in templates; an
    # unknown slug is a strict 422 (no custom docs templates exist in the MVP).
    if not is_builtin_slug(body.default_template):
        raise HTTPException(422, f"unknown template: {body.default_template}")

    try:
        site_url = validate_site_url(body.site_url)
    except DocumentationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "confluence_connection_failed", "message": str(exc)},
        ) from exc

    conn = await _get_connection(org_id, db)
    client = ConfluenceClient(site_url, body.account_email, body.api_token)
    try:
        await client.validate_connection()
    except DocumentationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "confluence_connection_failed", "message": str(exc)},
        ) from exc
    finally:
        await client.aclose()

    now = datetime.now(timezone.utc)
    before = _connection_response(conn) if conn else None
    site_changed = conn is not None and conn.site_url != site_url
    if conn is None:
        conn = OrgDocConnection(
            org_id=org_id,
            provider="confluence",
            site_url=site_url,
            account_email=body.account_email.strip(),
            encrypted_api_token=encrypt_secret(body.api_token),
            default_template=body.default_template,
            batch_window_minutes=body.batch_window_minutes,
            enabled=True,
            connected_by_user_id=user.id,
            last_verified_at=now,
        )
        db.add(conn)
    else:
        conn.site_url = site_url
        conn.account_email = body.account_email.strip()
        conn.encrypted_api_token = encrypt_secret(body.api_token)
        conn.default_template = body.default_template
        conn.batch_window_minutes = body.batch_window_minutes
        conn.enabled = True
        conn.connected_by_user_id = user.id
        conn.last_verified_at = now
    if site_changed:
        # Cached space ids belong to the old site; re-resolve on next use.
        await db.execute(
            update(DocSpaceMapping)
            .where(DocSpaceMapping.org_id == org_id)
            .values(space_id=None)
            .execution_options(synchronize_session=False)
        )

    _org_audit(
        db,
        org=org,
        actor=user,
        actor_role=role,
        event_type="confluence_connection_updated",
        target_type="confluence_connection",
        target_id=org_id,
        before=before,
        after=_connection_response(conn),
    )
    await db.commit()
    return _connection_response(conn)


@org_router.delete("/{org_id}/integrations/confluence", status_code=204)
async def delete_confluence_connection(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    org, role = await _authorize_org_confluence(db, user, org_id)
    conn = await _get_connection(org_id, db)
    if conn is None:
        raise HTTPException(404, "No Confluence connection")
    _org_audit(
        db,
        org=org,
        actor=user,
        actor_role=role,
        event_type="confluence_connection_deleted",
        target_type="confluence_connection",
        target_id=org_id,
        before=_connection_response(conn),
        after=None,
    )
    await db.delete(conn)
    await db.commit()


@org_router.post("/{org_id}/integrations/confluence/test")
async def test_confluence_connection(
    org_id: str,
    body: ConfluenceTestBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _authorize_org_confluence(db, user, org_id)
    try:
        site_url = validate_site_url(body.site_url)
    except DocumentationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "confluence_connection_failed", "message": str(exc)},
        ) from exc
    client = ConfluenceClient(site_url, body.account_email, body.api_token)
    try:
        who = await client.validate_connection()
    except DocumentationError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "confluence_connection_failed", "message": str(exc)},
        ) from exc
    finally:
        await client.aclose()
    return {"ok": True, "account_id": who.get("account_id"), "display_name": who.get("display_name")}


# ── Org routes: space/page pickers ──


@org_router.get("/{org_id}/integrations/confluence/spaces")
async def list_confluence_spaces(
    org_id: str,
    response: Response,
    q: str | None = Query(None, max_length=_SEARCH_MAX_CHARS),
    limit: int | None = Query(None, ge=1, le=_SEARCH_MAX_LIMIT),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict] | dict:
    await _authorize_org_confluence(db, user, org_id)
    query = (q or "").strip() or None
    paged = q is not None or limit is not None
    page_limit = (limit or _SEARCH_DEFAULT_LIMIT) if paged else _SEARCH_MAX_LIMIT
    conn = await _get_connection(org_id, db)
    if conn is None:
        raise HTTPException(404, "No Confluence connection")
    client = ConfluenceClient(
        conn.site_url, conn.account_email, decrypt_secret(conn.encrypted_api_token)
    )
    try:
        page = await client.search_spaces(query, page_limit)
    except DocumentationError as exc:
        raise HTTPException(502, {"error": "confluence_upstream_error", "message": str(exc)}) from exc
    finally:
        await client.aclose()
    spaces = [{"key": s.key, "name": s.name} for s in page.spaces]
    if not paged:
        response.headers["X-Confluence-Spaces-Truncated"] = "true" if page.has_more else "false"
        return spaces
    return {"spaces": spaces, "has_more": page.has_more, "query": query, "limit": page_limit}


@org_router.get("/{org_id}/integrations/confluence/spaces/{space_key}/pages")
async def list_confluence_space_pages(
    org_id: str,
    space_key: str,
    q: str | None = Query(None, max_length=_SEARCH_MAX_CHARS),
    limit: int | None = Query(None, ge=1, le=_SEARCH_MAX_LIMIT),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await _authorize_org_confluence(db, user, org_id)
    if not _SPACE_KEY_RE.match(space_key):
        raise HTTPException(422, "space_key must look like DEV or TEAM_1")
    conn = await _get_connection(org_id, db)
    if conn is None:
        raise HTTPException(404, "No Confluence connection")
    client = ConfluenceClient(
        conn.site_url, conn.account_email, decrypt_secret(conn.encrypted_api_token)
    )
    try:
        pages = await client.search_pages(
            space_key, (q or "").strip() or None, limit or _SEARCH_DEFAULT_LIMIT
        )
    except DocumentationError as exc:
        raise HTTPException(502, {"error": "confluence_upstream_error", "message": str(exc)}) from exc
    finally:
        await client.aclose()
    return [{"id": p.id, "title": p.title} for p in pages]


@org_router.get("/{org_id}/integrations/confluence/templates")
async def list_confluence_templates(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await _authorize_org_confluence(db, user, org_id)
    return [
        {
            "slug": t.slug,
            "name": t.name,
            "version": None,
            "builtin": True,
            "sections": [
                {
                    "key": s.key,
                    "heading": s.heading,
                    "mode": s.mode,
                    "source_fields": list(s.source_fields),
                    "guidance": s.guidance,
                }
                for s in t.sections
            ],
        }
        for t in list_templates()
    ]


# ── Org routes: mappings ──


@org_router.get("/{org_id}/integrations/confluence/mappings")
async def list_confluence_mappings(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await _authorize_org_confluence(db, user, org_id)
    rows = (
        await db.execute(
            select(DocSpaceMapping)
            .join(Project, Project.id == DocSpaceMapping.project_id)
            .where(
                DocSpaceMapping.org_id == org_id,
                Project.org_id == DocSpaceMapping.org_id,
            )
        )
    ).scalars().all()
    return [_mapping_response(m) for m in rows]


@org_router.get("/{org_id}/integrations/confluence/mappings/{project_id}")
async def get_confluence_mapping(
    org_id: str,
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _authorize_org_confluence(db, user, org_id)
    mapping = await _get_mapping(project_id, org_id, db)
    if mapping is None:
        raise HTTPException(404, "Mapping not found")
    return _mapping_response(mapping)


@org_router.put("/{org_id}/integrations/confluence/mappings/{project_id}")
async def put_confluence_mapping(
    org_id: str,
    project_id: str,
    body: ConfluenceMappingPut,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org, role = await _authorize_org_confluence(db, user, org_id)
    if body.template is not None and not is_builtin_slug(body.template):
        raise HTTPException(422, f"unknown template: {body.template}")

    project = (
        await db.execute(
            select(Project).where(Project.id == project_id, Project.org_id == org_id)
        )
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(404, "Project not found")

    conn = await _get_connection(org_id, db)
    if conn is None:
        raise HTTPException(404, "No Confluence connection")

    # Validate the space + parent before persisting: the parent must exist, be
    # in the mapped space, and never be the space root, so SessionFS pages are
    # always real children inside the space the admin picked.
    client = ConfluenceClient(
        conn.site_url, conn.account_email, decrypt_secret(conn.encrypted_api_token)
    )
    try:
        space = await client.get_space(body.space_key)
        parent = await client.get_page(body.parent_page_id)
    except DocumentationError as exc:
        raise HTTPException(502, {"error": "confluence_upstream_error", "message": str(exc)}) from exc
    finally:
        await client.aclose()
    if parent.space_id is None or parent.space_id != space.id:
        raise HTTPException(
            422,
            {
                "error": "parent_page_not_in_space",
                "message": (
                    f"parent_page_id must be a page in the {body.space_key} space."
                ),
            },
        )
    if space.homepage_id is not None and body.parent_page_id == space.homepage_id:
        raise HTTPException(
            422,
            {
                "error": "parent_page_is_space_root",
                "message": (
                    "parent_page_id must be a real child page, not the space's "
                    "home page."
                ),
            },
        )

    # One mapping per project (unique project_id). A row left behind under
    # another org (the project moved here) is stale: drop it so the write
    # below can never hit the unique constraint.
    existing_rows = (
        await db.execute(
            select(DocSpaceMapping).where(DocSpaceMapping.project_id == project_id)
        )
    ).scalars().all()
    mapping: DocSpaceMapping | None = None
    for row in existing_rows:
        if row.org_id == org_id:
            mapping = row
        else:
            await db.delete(row)
    if len(existing_rows) > (1 if mapping is not None else 0):
        await db.flush()

    before = _mapping_response(mapping) if mapping else None

    # Moving the mapping to another space: pages already published live in
    # the OLD space. Their link rows are deleted (audited below) so every
    # source — interactive or swept — creates a fresh page in the new space;
    # the old pages stay in Confluence untouched. ``unlinked`` rows (an
    # admin's explicit "stop syncing") are kept. The delete is conditional on
    # no live claim and its rowcount is checked, so an in-flight publish can't
    # be overwritten (409; the whole save rolls back).
    previous_key = mapping.space_key if mapping is not None else None
    link_rows = (
        await db.execute(
            select(DocPageLink).where(
                DocPageLink.project_id == project_id,
                DocPageLink.status != "unlinked",
            )
        )
    ).scalars().all()
    moved = [
        link
        for link in link_rows
        if (link.space_key or previous_key) not in (None, body.space_key)
    ]
    if moved:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=_CLAIM_STALE_SECONDS)
        moved_summary = [
            {"source_kind": link.source_kind, "source_key": link.source_key, "page_id": link.page_id}
            for link in moved
        ]
        res = await db.execute(
            delete(DocPageLink)
            .where(
                DocPageLink.id.in_([link.id for link in moved]),
                DocPageLink.status != "unlinked",
                or_(
                    DocPageLink.claim_token.is_(None),
                    DocPageLink.claimed_at < cutoff,
                ),
            )
            .execution_options(synchronize_session=False)
        )
        if res.rowcount != len(moved):  # type: ignore[attr-defined]
            await db.rollback()
            raise HTTPException(409, _publish_in_progress())
        _org_audit(
            db,
            org=org,
            actor=user,
            actor_role=role,
            event_type="confluence_links_dropped",
            target_type="confluence_mapping",
            target_id=project_id,
            before={"space_key": previous_key, "links": moved_summary},
            after={"space_key": body.space_key, "reason": "mapping_space_changed"},
        )

    if mapping is None:
        mapping = DocSpaceMapping(
            id=f"dsm_{uuid.uuid4().hex[:24]}",
            org_id=org_id,
            project_id=project_id,
            space_key=body.space_key,
            space_id=space.id,
            parent_page_id=body.parent_page_id,
            template=body.template,
            publish_context=body.publish_context,
            auto_publish_pages=body.auto_publish_pages,
            created_by_user_id=user.id,
        )
        db.add(mapping)
    else:
        mapping.space_key = body.space_key
        mapping.space_id = space.id
        mapping.parent_page_id = body.parent_page_id
        mapping.template = body.template
        mapping.publish_context = body.publish_context
        mapping.auto_publish_pages = body.auto_publish_pages
        # The admin who saves the mapping is who the sweep publishes as (and
        # re-saving is how a mapping whose creator left is re-attributed).
        mapping.created_by_user_id = user.id

    _org_audit(
        db,
        org=org,
        actor=user,
        actor_role=role,
        event_type="confluence_mapping_updated",
        target_type="confluence_mapping",
        target_id=project_id,
        before=before,
        after=_mapping_response(mapping),
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            409,
            {
                "error": "mapping_conflict",
                "message": "This project's mapping changed concurrently; try again.",
            },
        ) from exc
    return _mapping_response(mapping)


@org_router.delete("/{org_id}/integrations/confluence/mappings/{project_id}", status_code=204)
async def delete_confluence_mapping(
    org_id: str,
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    org, role = await _authorize_org_confluence(db, user, org_id)
    mapping = await _get_mapping(project_id, org_id, db)
    if mapping is None:
        raise HTTPException(404, "Mapping not found")
    _org_audit(
        db,
        org=org,
        actor=user,
        actor_role=role,
        event_type="confluence_mapping_deleted",
        target_type="confluence_mapping",
        target_id=project_id,
        before=_mapping_response(mapping),
        after=None,
    )
    await db.delete(mapping)
    await db.commit()


# ── Org route: health ──


_HEALTH_FAILURE_WINDOW = timedelta(hours=24)


@org_router.get("/{org_id}/integrations/confluence/health")
async def confluence_health(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _authorize_org_confluence(db, user, org_id)
    since = datetime.now(timezone.utc) - _HEALTH_FAILURE_WINDOW

    unmapped_count = (
        await db.execute(
            select(func.count()).select_from(Project).where(
                Project.org_id == org_id,
                Project.id.notin_(
                    select(DocSpaceMapping.project_id).where(
                        DocSpaceMapping.org_id == org_id
                    )
                ),
            )
        )
    ).scalar_one()

    failed_rows = (
        await db.execute(
            select(DocPageLink)
            .where(
                DocPageLink.org_id == org_id,
                DocPageLink.last_push_failed_at.is_not(None),
                DocPageLink.last_push_failed_at >= since,
            )
            .order_by(DocPageLink.last_push_failed_at.desc())
            .limit(100)
        )
    ).scalars().all()

    in_flight_rows = (
        await db.execute(
            select(DocPageLink).where(
                DocPageLink.org_id == org_id,
                DocPageLink.claim_token.is_not(None),
            )
        )
    ).scalars().all()

    unknown_rows = (
        await db.execute(
            select(DocPageLink).where(
                DocPageLink.org_id == org_id,
                DocPageLink.status == "unknown",
                DocPageLink.claim_token.is_(None),
            )
        )
    ).scalars().all()

    drifted_rows = (
        await db.execute(
            select(DocPageLink).where(
                DocPageLink.org_id == org_id,
                DocPageLink.status == "drifted",
            )
        )
    ).scalars().all()

    return {
        "unmapped_project_count": int(unmapped_count or 0),
        "last_push_failures": [
            {
                "project_id": link.project_id,
                "source_kind": link.source_kind,
                "source_key": link.source_key,
                "failed_at": (
                    link.last_push_failed_at.isoformat()
                    if link.last_push_failed_at
                    else None
                ),
                "reason": link.last_push_failure,
            }
            for link in failed_rows
        ],
        "publishes_in_progress": [
            {
                "project_id": link.project_id,
                "source_kind": link.source_kind,
                "source_key": link.source_key,
                "started_at": (
                    link.claimed_at.isoformat() if link.claimed_at else None
                ),
            }
            for link in in_flight_rows
        ],
        "publishes_unknown": [
            {
                "project_id": link.project_id,
                "source_kind": link.source_kind,
                "source_key": link.source_key,
            }
            for link in unknown_rows
        ],
        "drifted_links": [
            {
                "project_id": link.project_id,
                "source_kind": link.source_kind,
                "source_key": link.source_key,
                "page_id": link.page_id,
                "stored_version": link.version,
            }
            for link in drifted_rows
        ],
    }


# ── Project-scoped authorization ──


def _service_key_denied() -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={
            "error": "service_key_not_allowed",
            "message": (
                "Publishing through the org's Confluence connection requires a "
                "user key; scheduled publishing runs through the admin sweep."
            ),
        },
    )


async def _authorize_project_confluence(
    db: AsyncSession,
    auth: AuthContext,
    ctx: UserContext,
    project_id: str,
    *,
    require_editor: bool = False,
    project_admin: bool = False,
    allow_service_key: bool = False,
) -> tuple[Project, OrgDocConnection, DocSpaceMapping]:
    from sessionfs.server.auth.dependencies import assert_service_key_can_access_project
    from sessionfs.server.auth.project_access import (
        project_edit_forbidden,
        user_can_edit_project,
        user_is_project_admin,
    )
    from sessionfs.server.routes.knowledge import _get_project_for_auth

    if auth.key_kind == "service" and not allow_service_key:
        raise _service_key_denied()

    try:
        project = await _get_project_for_auth(project_id, db, auth)
    except HTTPException as exc:
        # A caller who cannot read the project (another org, or hidden by
        # restricted visibility) learns nothing about it: 404, never 403.
        if exc.status_code == 403:
            raise HTTPException(404, "Project not found") from exc
        raise
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "confluence_integration", project, db)

    if project.org_id is None:
        raise HTTPException(
            409,
            {
                "error": "confluence_not_connected",
                "message": "This project is not org-scoped.",
            },
        )
    # Project read access alone is not enough to act through the org's
    # Confluence credential: only org members may. Service keys are org-bound
    # by ``assert_service_key_can_access_project``.
    if auth.key_kind != "service":
        is_member = (
            await db.execute(
                select(OrgMember.id).where(
                    OrgMember.org_id == project.org_id,
                    OrgMember.user_id == auth.user.id,
                )
            )
        ).first()
        if is_member is None:
            raise HTTPException(
                403,
                {
                    "error": "org_membership_required",
                    "message": "Only members of this project's organization can use its Confluence connection.",
                },
            )
        # Publishing changes shared content: editor standing (owner, org
        # admin, plain member of an open org, or a group editor/admin grant
        # when the org restricts visibility).
        if (require_editor or project_admin) and not await user_can_edit_project(
            db, auth.user.id, project
        ):
            raise project_edit_forbidden()
        if project_admin and not await user_is_project_admin(db, auth.user.id, project):
            raise HTTPException(
                status_code=403,
                detail={
                    "error": "insufficient_role",
                    "current_role": "member",
                    "required_role": "admin",
                    "message": "Unlinking or resolving a Confluence page requires project-admin standing.",
                },
            )

    conn = await _get_connection(project.org_id, db)
    if conn is None or not conn.enabled:
        raise HTTPException(
            409,
            {
                "error": "confluence_not_connected",
                "message": "This organization has not connected Confluence.",
            },
        )
    mapping = await _get_mapping(project.id, project.org_id, db)
    if mapping is None:
        raise HTTPException(
            409,
            {
                "error": "confluence_mapping_required",
                "message": "Ask your admin to add a mapping",
            },
        )
    return project, conn, mapping


def _template_for(
    mapping: DocSpaceMapping, conn: OrgDocConnection, override: str | None
) -> tuple[str, Template]:
    inherited = mapping.template or conn.default_template
    slug = _resolve_doc_template_slug(inherited, override)
    template = get_template(slug)
    assert template is not None  # built-in resolution always returns a template
    return slug, template


# ── Project routes: preview/publish/links ──


@router.post("/{project_id}/confluence/preview")
async def preview_confluence(
    project_id: str,
    body: ConfluencePreviewBody,
    auth: AuthContext = Depends(require_scope("knowledge:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project, conn, mapping = await _authorize_project_confluence(
        db, auth, ctx, project_id, require_editor=True
    )
    source_kind, source_key = _resolve_source(body.kind, body.slug)
    title, sources = await _collect_sources(project, source_kind, source_key, db)

    slug, template = _template_for(
        mapping, conn, body.template_override or body.template
    )

    org = await db.get(Organization, project.org_id)  # type: ignore[arg-type]
    try:
        preview = await build_preview(
            title=title,
            template=template,
            sources=sources,
            org=org,  # type: ignore[arg-type]
            user=auth.user,
            db=db,
            allow_user_key=auth.key_kind != "service",
        )
    except ContentTooLarge as exc:
        raise _content_too_large(exc.section) from exc

    response = preview.to_dict()
    # Show the reviewer exactly what will be sent: org DLP applied.
    response["title"] = redact_for_publish(preview.title, org)
    response["sections"] = redact_sections_for_publish(preview.sections, org)
    response["project_id"] = project.id
    response["source_kind"] = source_kind
    response["source_key"] = source_key
    response["template"] = {"slug": slug, "name": template.name, "version": None}

    link = await _get_link(project.id, source_kind, source_key, db)
    response["link"] = (
        _link_response(link, conn.site_url, mapping.space_key, project.org_id)
        if link is not None
        else None
    )
    # A stale link (another org/site) is reported with ``stale: true`` so the
    # caller can unlink it, but nothing remote is rendered for it and publish
    # still refuses it (409 confluence_link_stale). No outbound call here.
    if (
        link is not None
        and link.status == "linked"
        and link.page_id
        and _link_is_current(link, project.org_id, conn.site_url)
    ):
        response["existing"] = {
            "page_id": link.page_id,
            "page_url": _page_url(
                conn.site_url, link.space_key or mapping.space_key, link.page_id
            ),
            "version": link.version,
            "template_slug": link.template_slug,
            "template_version": link.template_version,
        }
    return response


@router.post("/{project_id}/confluence/publish")
async def publish_confluence(
    project_id: str,
    body: ConfluencePublishBody,
    request: Request,
    auth: AuthContext = Depends(require_scope("knowledge:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project, conn, mapping = await _authorize_project_confluence(
        db, auth, ctx, project_id, require_editor=True
    )
    if not body.confirm:
        raise HTTPException(400, "Set confirm=true to publish to Confluence")

    source_kind, source_key = _resolve_source(body.kind, body.slug)

    # A linked row that points at another org/site blocks publish before we
    # even look at the body — a stale link must never be re-targeted.
    existing = await _get_link(project.id, source_kind, source_key, db)
    if existing is not None and existing.status != "unlinked":
        _assert_link_current(existing, project.org_id, conn.site_url)

    # Resolution: an explicit override, else mapping → connection default.
    # ``template`` is only an echo of what the preview resolved; it never
    # selects the template.
    slug, template = _template_for(mapping, conn, body.template_override)

    # The draft was previewed against a specific template; if the resolution
    # moved since (mapping/default changed), refuse rather than publish a
    # shape nobody previewed. Built-ins carry version None.
    if body.template is not None or "template_version" in body.model_fields_set:
        expected_slug = body.template if body.template is not None else slug
        if expected_slug != slug or body.template_version is not None:
            raise HTTPException(
                409,
                {
                    "error": "template_changed",
                    "message": (
                        "The page template changed since this draft was "
                        f"previewed (now {template.name}); preview again before "
                        "publishing."
                    ),
                    "template": {"slug": slug, "name": template.name, "version": None},
                },
            )

    # The client never chooses section rendering: the section keys must be the
    # template's own, validated before any outbound call.
    section_keys = {s.key for s in template.sections}
    unknown = [s.key for s in body.sections if s.key not in section_keys]
    if unknown:
        raise HTTPException(422, f"unknown section key(s): {', '.join(unknown)}")

    sections = [s.model_dump() for s in body.sections]
    if _raw_chars(sections) > _MAX_STORAGE_CHARS:
        raise _content_too_large_for_confluence()

    org = await db.get(Organization, project.org_id)  # type: ignore[arg-type]
    # Source hash (skip-if-unchanged for the sweep) is over the SessionFS
    # source, not the edited draft.
    source_title, sources = await _collect_sources(project, source_kind, source_key, db)
    content_hash = source_hash(source_title, sources, slug)

    return await execute_publish(
        db,
        PublishRequest(
            project=project,
            conn=conn,
            mapping=mapping,
            org=org,
            source_kind=source_kind,
            source_key=source_key,
            title=redact_for_publish(body.title, org),
            sections=redact_sections_for_publish(sections, org),
            template_slug=slug,
            template_name=template.name,
            content_hash=content_hash,
            actor=auth.user,
            actor_type=auth.actor_type,
            dashboard_url=dashboard_link_base(getattr(request.app.state, "config", None)),
        ),
    )


@router.get("/{project_id}/confluence/links")
async def list_confluence_links(
    project_id: str,
    auth: AuthContext = Depends(require_scope("knowledge:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project, conn, mapping = await _authorize_project_confluence(
        db, auth, ctx, project_id, allow_service_key=True
    )
    rows = (
        await db.execute(
            select(DocPageLink)
            .where(DocPageLink.project_id == project.id)
            .order_by(DocPageLink.source_kind, DocPageLink.source_key)
        )
    ).scalars().all()
    return {
        "links": [
            _link_response(link, conn.site_url, mapping.space_key, project.org_id)
            for link in rows
        ],
        "mapping": {
            "space_key": mapping.space_key,
            "template": mapping.template,
            "publish_context": mapping.publish_context,
            "auto_publish_pages": mapping.auto_publish_pages,
        },
    }


async def _load_link_for_admin(
    db: AsyncSession, project: Project, link_id: str
) -> DocPageLink:
    link = await db.get(DocPageLink, link_id, populate_existing=True)
    if link is None or link.project_id != project.id:
        raise HTTPException(404, "No Confluence link with that id")
    if _claim_is_live(link):
        raise HTTPException(409, _publish_in_progress())
    return link


def _unclaimed_clause(link: DocPageLink):
    """WHERE fragment: the row is unchanged and holds no live claim."""
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_CLAIM_STALE_SECONDS)
    return (
        DocPageLink.id == link.id,
        DocPageLink.status == link.status,
        or_(DocPageLink.claim_token.is_(None), DocPageLink.claimed_at < cutoff),
    )


def _audit_unlink(
    db: AsyncSession, org: Organization | None, auth: AuthContext, link: DocPageLink, via: str
) -> None:
    if org is None:
        return
    _org_audit(
        db,
        org=org,
        actor=auth.user,
        actor_role=None,
        actor_type=auth.actor_type,
        event_type="confluence_unlinked",
        target_type="confluence_page",
        target_id=link.source_key,
        before={"page_id": link.page_id, "source_kind": link.source_kind, "status": link.status},
        after={"via": via},
    )


@router.delete("/{project_id}/confluence/links/{link_id}", status_code=204)
async def unlink_confluence(
    project_id: str,
    link_id: str,
    auth: AuthContext = Depends(require_scope("knowledge:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> None:
    project, _conn, _mapping = await _authorize_project_confluence(
        db, auth, ctx, project_id, project_admin=True
    )
    link = await _load_link_for_admin(db, project, link_id)

    org = await db.get(Organization, project.org_id)  # type: ignore[arg-type]
    _audit_unlink(db, org, auth, link, "delete")
    res = await db.execute(
        delete(DocPageLink).where(*_unclaimed_clause(link))
    )
    if res.rowcount != 1:  # type: ignore[attr-defined]
        await db.rollback()
        raise HTTPException(409, _publish_in_progress())
    await db.commit()


@router.post("/{project_id}/confluence/links/{link_id}/resolve")
async def resolve_confluence(
    project_id: str,
    link_id: str,
    body: ConfluenceResolveBody,
    auth: AuthContext = Depends(require_scope("knowledge:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project, conn, _mapping = await _authorize_project_confluence(
        db, auth, ctx, project_id, project_admin=True
    )
    link = await _load_link_for_admin(db, project, link_id)
    _assert_link_current(link, project.org_id, conn.site_url)
    org = await db.get(Organization, project.org_id)  # type: ignore[arg-type]

    if body.action == "unlink":
        # Sever the link but keep the row (status='unlinked') so a later publish
        # creates a fresh page; clear the remote snapshot so no stale page URL
        # is reported. Audited exactly like DELETE unlink.
        _audit_unlink(db, org, auth, link, "resolve")
        res = await db.execute(
            update(DocPageLink)
            .where(*_unclaimed_clause(link))
            .values(
                status="unlinked",
                page_id=None,
                version=None,
                last_content_hash=None,
                last_published_storage=None,
                claim_token=None,
                claimed_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        if res.rowcount != 1:  # type: ignore[attr-defined]
            await db.rollback()
            raise HTTPException(409, _publish_in_progress())
        await db.commit()
        return {"id": link.id, "status": "unlinked", "page_id": None}

    if not link.page_id:
        raise HTTPException(
            409,
            {
                "error": "confluence_link_invalid",
                "message": "This link has no remote page to overwrite from.",
            },
        )
    client = ConfluenceClient(
        conn.site_url, conn.account_email, decrypt_secret(conn.encrypted_api_token)
    )
    try:
        remote = await asyncio.wait_for(
            client.get_page(link.page_id, include_body=True), timeout=20
        )
    except asyncio.TimeoutError as exc:
        raise HTTPException(
            504, {"error": "confluence_timeout", "message": "Confluence did not respond."}
        ) from exc
    except DocumentationNotFound as exc:
        # The page is gone in Confluence: sever the link (audited) instead of
        # leaving it stuck; the next publish creates a fresh page.
        missing_page = link.page_id
        res = await db.execute(
            update(DocPageLink)
            .where(*_unclaimed_clause(link))
            .values(
                status="unlinked",
                page_id=None,
                version=None,
                last_content_hash=None,
                last_published_storage=None,
                claim_token=None,
                claimed_at=None,
            )
            .execution_options(synchronize_session=False)
        )
        if res.rowcount != 1:  # type: ignore[attr-defined]
            await db.rollback()
            raise HTTPException(409, _publish_in_progress()) from exc
        if org is not None:
            _org_audit(
                db,
                org=org,
                actor=auth.user,
                actor_role=None,
                actor_type=auth.actor_type,
                event_type="confluence_page_missing",
                target_type="confluence_page",
                target_id=link.source_key,
                before={"page_id": missing_page, "source_kind": link.source_kind},
                after={"status": "unlinked"},
            )
        await db.commit()
        raise HTTPException(409, _page_missing_detail(missing_page)) from exc
    except DocumentationError as exc:
        raise HTTPException(
            502, {"error": "confluence_upstream_error", "message": str(exc)}
        ) from exc
    finally:
        await client.aclose()

    # Accept the remote version as our baseline so the next publish
    # overwrites the Confluence edit (fenced: no claim, row unchanged).
    before = {"status": link.status, "version": link.version}
    res = await db.execute(
        update(DocPageLink)
        .where(*_unclaimed_clause(link), DocPageLink.page_id == link.page_id)
        .values(
            version=remote.version,
            status="linked",
            # The accepted remote body is the new drift-diff baseline.
            last_published_storage=remote.storage,
            last_push_failed_at=None,
            last_push_failure=None,
        )
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:  # type: ignore[attr-defined]
        await db.rollback()
        raise HTTPException(409, _publish_in_progress())
    if org is not None:
        _org_audit(
            db,
            org=org,
            actor=auth.user,
            actor_role=None,
            actor_type=auth.actor_type,
            event_type="confluence_drift_resolved",
            target_type="confluence_page",
            target_id=link.source_key,
            before=before,
            after={"status": "linked", "version": remote.version},
        )
    await db.commit()
    return {"id": link.id, "status": "linked", "version": remote.version}
