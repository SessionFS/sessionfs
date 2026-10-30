"""The project checklist that learns — Review Loop R3, Brief E1.

``loop_checklist_items`` is the durable list of review traps a project has
learned (design ``docs/design/review-loop-r3.md`` §4, §5). Project editors
manage it; every plan gets it (no tier gate — design §8). SessionFS ships a
small generic seed that a project accepts or skips, and review findings whose
``class`` is ``new`` auto-propose items (deduped by ``(project_id, slug)``).

Routes under ``/api/v1/projects/{project_id}``:

    GET   /loop-checklist?status=      — list (readers)
    GET   /loop-checklist.md            — markdown of ACTIVE items (briefs)
    POST  /loop-checklist               — create/propose (editor, manual)
    POST  /loop-checklist/seed          — offer the 6 generic seed items
    PATCH /loop-checklist/{item_id}     — accept / retire / edit (editor)

Access mirrors the spend surface: a user key with no read standing on the
project gets the byte-identical 404 (existence-oracle, review checklist #2);
a captured-session reader gets 200 on read but 403 ``project_edit_forbidden``
on write; service keys go through their org/allowlist boundary via
``assert_service_key_can_access_project``.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import (
    AuthContext,
    assert_service_key_can_access_project,
    require_scope,
)
from sessionfs.server.auth.project_access import (
    require_project_editor,
    user_can_access_project,
)
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import LoopChecklistItem, Project
from sessionfs.server.routes.knowledge import _get_project_for_auth
from sessionfs.server.sanitize import sanitize_archive_string

router = APIRouter(prefix="/api/v1/projects", tags=["loop-checklist"])

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_VALID_STATUSES: tuple[str, ...] = ("proposed", "active", "retired")
_MAX_TITLE = 200
_MAX_CHECK_TEXT = 2000
# Cap the provenance refs list so a hot trap can't grow the JSON column
# without bound (a re-hit appends a ref; keep only the most recent N).
_MAX_FINDING_REFS = 20
# Bound the proposal inbox: at most this many new rows per completion and at
# most this many proposed rows per project (design §7.3).
_MAX_NEW_PROPOSALS_PER_RUN = 20
_MAX_PROPOSED_PER_PROJECT = 200


# ── Seed (design §10.1 — six generic traps, offered as `proposed`) ──

SEED_ITEMS: tuple[tuple[str, str, str], ...] = (
    (
        "access-rechecked-after-slow-steps",
        "Access rechecked after slow steps",
        "Re-run the write authorization after any await that can take seconds "
        "(an LLM call, Jira, an upload). A permission check taken before a "
        "slow step is stale afterwards.",
    ),
    (
        "existence-oracles",
        "Existence oracles",
        "Every miss on one route returns the byte-identical 404 body. Never "
        "vary the status code or message for unknown / deleted / not-yours / "
        "other-org, and check plan and tier gates after visibility, never "
        "before.",
    ),
    (
        "service-identity-acting-as-its-minter",
        "Service identity acting as its minter",
        "Authorize a service key by its org and project allowlist, never by "
        "the minter's personal LLM key, personal standing or name; require the "
        "matching scope and record the service-key provenance.",
    ),
    (
        "every-user-field-through-dlp",
        "Every user field through DLP and normalisation",
        "Run DLP and sanitize_archive_string on every text field before "
        "storage — titles, names and descriptions, not just the body. NULs or "
        "lone surrogates in any field 500 on PostgreSQL.",
    ),
    (
        "read-check-write-without-a-guarded-update",
        "Read-check-write without a guarded UPDATE",
        "State changes use a rowcount-1 guarded UPDATE … WHERE status = … "
        "inside the same transaction as their side effects, and tests exercise "
        "the guard, not just two sequential calls.",
    ),
    (
        "usage-counted-on-every-path",
        "Usage counted on every path",
        "Record usage on every path that reached the provider: success, "
        "invalid output, exception, timeout and cancellation (shield the "
        "write).",
    ),
)

_SEED_SLUGS: frozenset[str] = frozenset(slug for slug, _t, _c in SEED_ITEMS)


# ── Text hygiene (review checklist #7) ──


def _sanitize_text(value: str) -> str:
    """Collapse whitespace, strip NULs/lone surrogates, then redact secrets.

    Runs of spaces/tabs/newlines collapse to single spaces first so brief
    markdown can't be restructured by multi-line input. Best-effort redaction
    (mirrors handoffs.py's preview path): the text is still stored if a scan
    raises, but the NUL/surrogate strip is mandatory because those values 500
    on PostgreSQL.
    """
    value = " ".join((value or "").split())
    value, _ = sanitize_archive_string(value)
    try:
        from sessionfs.server.dlp import redact_text, scan_dlp

        findings = scan_dlp(value, categories=["secrets"])
        if findings:
            value = redact_text(value, findings)
    except Exception:
        pass
    return value


def _slugify(title: str) -> str:
    """Derive a checklist slug from a finding title (design §4, §7.3).

    Lowercase, collapse runs of non-alphanumerics to a single hyphen, then
    clamp to the slug vocabulary. Returns "" when the title has no sluggable
    content (e.g. it was only punctuation/emoji).
    """
    slug = re.sub(r"[^a-z0-9]+", "-", (title or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    if not slug:
        return ""
    slug = slug[:63].rstrip("-")
    if not _SLUG_RE.match(slug):
        return ""
    return slug


# ── Scope resolution ──


async def _read_scope(
    project_id: str, auth: AuthContext, db: AsyncSession
) -> Project:
    """Authorize a read. Outsiders get a byte-identical 404 (existence oracle)."""
    if auth.key_kind == "service":
        project = await _get_project_for_auth(project_id, db, auth)
        await assert_service_key_can_access_project(db, auth, project)
        return project
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None or not await user_can_access_project(
        db, auth.user.id, project
    ):
        raise HTTPException(404, "Project not found")
    return project


async def _write_scope(
    project_id: str, auth: AuthContext, db: AsyncSession
) -> Project:
    """Authorize a write. Outsiders 404 (identical body); read-only captured
    session holders 403 ``project_edit_forbidden``; editors pass."""
    if auth.key_kind == "service":
        project = await _get_project_for_auth(project_id, db, auth)
        await assert_service_key_can_access_project(db, auth, project)
        return project
    project = (
        await db.execute(select(Project).where(Project.id == project_id))
    ).scalar_one_or_none()
    if project is None or not await user_can_access_project(
        db, auth.user.id, project
    ):
        raise HTTPException(404, "Project not found")
    await require_project_editor(db, auth.user.id, project)
    return project


def _stamp_user_id(auth: AuthContext) -> str | None:
    """The user id to record on a creator/acceptor stamp.

    Service keys leave it NULL — their provenance goes on the
    ``*_actor_type`` / ``*_service_key_id`` / ``*_service_key_name`` columns
    instead, so the minter never stands in for the key (review checklist #3).
    """
    return None if auth.key_kind == "service" else auth.user.id


# ── Serialization ──


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _decode_refs(value: str | None) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except (ValueError, TypeError):
        return []
    if not isinstance(decoded, list):
        return []
    return [r for r in decoded if isinstance(r, dict)]


def _item_to_dict(item: LoopChecklistItem) -> dict[str, Any]:
    return {
        "id": item.id,
        "project_id": item.project_id,
        "slug": item.slug,
        "title": item.title,
        "check_text": item.check_text,
        "status": item.status,
        "source": item.source,
        "source_finding_refs": _decode_refs(item.source_finding_refs),
        "times_found": item.times_found,
        "last_found_at": _iso(item.last_found_at),
        "created_by_user_id": item.created_by_user_id,
        "accepted_by_user_id": item.accepted_by_user_id,
        "created_actor_type": item.created_actor_type,
        "created_service_key_id": item.created_service_key_id,
        "created_service_key_name": item.created_service_key_name,
        "accepted_actor_type": item.accepted_actor_type,
        "accepted_service_key_id": item.accepted_service_key_id,
        "accepted_service_key_name": item.accepted_service_key_name,
        "accepted_at": _iso(item.accepted_at),
        "created_at": _iso(item.created_at),
        "updated_at": _iso(item.updated_at),
    }


# ── Pydantic bodies ──


class _ChecklistCreate(BaseModel):
    slug: str
    title: str
    check_text: str

    @field_validator("slug")
    @classmethod
    def _slug_shape(cls, v: str) -> str:
        v = (v or "").strip().lower()
        if not _SLUG_RE.match(v):
            raise ValueError(
                "slug must be a lowercase slug (letters, digits, hyphens)"
            )
        return v

    @field_validator("title")
    @classmethod
    def _title_shape(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("title cannot be blank")
        if len(v) > _MAX_TITLE:
            raise ValueError(f"title must be {_MAX_TITLE} characters or fewer")
        return v

    @field_validator("check_text")
    @classmethod
    def _check_text_shape(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("check_text cannot be blank")
        if len(v) > _MAX_CHECK_TEXT:
            raise ValueError(
                f"check_text must be {_MAX_CHECK_TEXT} characters or fewer"
            )
        return v


class _ChecklistPatch(BaseModel):
    status: str | None = None  # 'active' (accept) | 'retired'
    title: str | None = None
    check_text: str | None = None

    @field_validator("status")
    @classmethod
    def _status_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = (v or "").strip().lower()
        if v not in {"active", "retired"}:
            raise ValueError("status must be one of: active, retired")
        return v

    @field_validator("title")
    @classmethod
    def _title_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("title cannot be blank")
        if len(v) > _MAX_TITLE:
            raise ValueError(f"title must be {_MAX_TITLE} characters or fewer")
        return v

    @field_validator("check_text")
    @classmethod
    def _check_text_shape(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("check_text cannot be blank")
        if len(v) > _MAX_CHECK_TEXT:
            raise ValueError(
                f"check_text must be {_MAX_CHECK_TEXT} characters or fewer"
            )
        return v


# ── Routes ──


@router.get("/{project_id}/loop-checklist")
async def list_checklist(
    project_id: str,
    status: str | None = Query(
        None, description="Filter by status: proposed | active | retired"
    ),
    auth: AuthContext = Depends(require_scope("loops:read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """List a project's checklist items, optionally filtered by status."""
    project = await _read_scope(project_id, auth, db)
    if status is not None:
        status = status.strip().lower()
        if status not in _VALID_STATUSES:
            raise HTTPException(
                422, f"status must be one of: {list(_VALID_STATUSES)}"
            )
    stmt = select(LoopChecklistItem).where(
        LoopChecklistItem.project_id == project.id
    )
    if status is not None:
        stmt = stmt.where(LoopChecklistItem.status == status)
    stmt = stmt.order_by(LoopChecklistItem.created_at, LoopChecklistItem.id)
    rows = (await db.execute(stmt)).scalars().all()
    return [_item_to_dict(r) for r in rows]


@router.get("/{project_id}/loop-checklist.md")
async def checklist_markdown(
    project_id: str,
    auth: AuthContext = Depends(require_scope("loops:read")),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Render the ACTIVE checklist as markdown for briefs (design §4, §5).

    Retired and proposed items are deliberately omitted — only accepted traps
    go into every brief.
    """
    project = await _read_scope(project_id, auth, db)
    rows = (
        (
            await db.execute(
                select(LoopChecklistItem)
                .where(
                    LoopChecklistItem.project_id == project.id,
                    LoopChecklistItem.status == "active",
                )
                .order_by(LoopChecklistItem.created_at, LoopChecklistItem.id)
            )
        )
        .scalars()
        .all()
    )
    lines = ["# Checklist", ""]
    if not rows:
        lines.append("_No accepted checklist items yet._")
    for r in rows:
        lines.append(f"- **{r.title}** — {r.check_text}")
    lines.append("")
    return Response(content="\n".join(lines), media_type="text/markdown")


@router.post("/{project_id}/loop-checklist")
async def create_checklist_item(
    project_id: str,
    body: _ChecklistCreate,
    auth: AuthContext = Depends(require_scope("loops:write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Create (propose) a checklist item by hand (editor, source=manual).

    The item starts ``proposed`` so the project accepts or skips it, same as
    seed and finding-sourced proposals. A duplicate slug in this project is a
    409 (proposals dedupe by ``(project_id, slug)`` — design §7.3).
    """
    project = await _write_scope(project_id, auth, db)
    existing = (
        await db.execute(
            select(LoopChecklistItem).where(
                LoopChecklistItem.project_id == project.id,
                LoopChecklistItem.slug == body.slug,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            409,
            f"A checklist item with slug '{body.slug}' already exists in "
            "this project.",
        )

    item = LoopChecklistItem(
        id=f"lci_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        slug=body.slug,
        title=_sanitize_text(body.title),
        check_text=_sanitize_text(body.check_text),
        status="proposed",
        source="manual",
        source_finding_refs=None,
        times_found=0,
        last_found_at=None,
        created_by_user_id=_stamp_user_id(auth),
        created_actor_type=auth.actor_type,
        created_service_key_id=auth.service_key_id,
        created_service_key_name=auth.service_key_name,
        accepted_by_user_id=None,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    return _item_to_dict(item)


@router.post("/{project_id}/loop-checklist/seed")
async def seed_checklist(
    project_id: str,
    auth: AuthContext = Depends(require_scope("loops:write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Offer the six generic seed traps as ``proposed`` items (design §10.1).

    Idempotent: any seed slug that already exists is skipped, so re-seeding
    never duplicates a row or clobbers a project's edits.
    """
    project = await _write_scope(project_id, auth, db)
    existing = set(
        (
            await db.execute(
                select(LoopChecklistItem.slug).where(
                    LoopChecklistItem.project_id == project.id,
                    LoopChecklistItem.slug.in_(_SEED_SLUGS),
                )
            )
        ).scalars().all()
    )
    created: list[LoopChecklistItem] = []
    for slug, title, check_text in SEED_ITEMS:
        if slug in existing:
            continue
        item = LoopChecklistItem(
            id=f"lci_{uuid.uuid4().hex[:16]}",
            project_id=project.id,
            slug=slug,
            title=title,
            check_text=check_text,
            status="proposed",
            source="seed",
            source_finding_refs=None,
            times_found=0,
            last_found_at=None,
            created_by_user_id=_stamp_user_id(auth),
            created_actor_type=auth.actor_type,
            created_service_key_id=auth.service_key_id,
            created_service_key_name=auth.service_key_name,
            accepted_by_user_id=None,
        )
        db.add(item)
        created.append(item)
    await db.commit()
    return {
        "created": [_item_to_dict(i) for i in created],
        "skipped": sorted(existing),
    }


@router.patch("/{project_id}/loop-checklist/{item_id}")
async def patch_checklist_item(
    project_id: str,
    item_id: str,
    body: _ChecklistPatch,
    auth: AuthContext = Depends(require_scope("loops:write")),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Accept (→ active), retire (→ retired) or edit a checklist item.

    Accepting records ``accepted_by_user_id`` on the proposed→active
    transition only, preserving the original acceptor on a re-accept (same
    audit-first-transition convention as knowledge dismiss).
    """
    project = await _write_scope(project_id, auth, db)
    result = await db.execute(
        select(LoopChecklistItem)
        .where(
            LoopChecklistItem.id == item_id,
            LoopChecklistItem.project_id == project.id,
        )
        .with_for_update()
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise HTTPException(404, "Checklist item not found")

    if body.status == "active":
        if item.status != "active":
            item.accepted_by_user_id = _stamp_user_id(auth)
            item.accepted_actor_type = auth.actor_type
            item.accepted_service_key_id = auth.service_key_id
            item.accepted_service_key_name = auth.service_key_name
            item.accepted_at = datetime.now(timezone.utc)
        item.status = "active"
    elif body.status == "retired":
        item.status = "retired"

    if body.title is not None:
        item.title = _sanitize_text(body.title)
    if body.check_text is not None:
        item.check_text = _sanitize_text(body.check_text)

    await db.commit()
    await db.refresh(item)
    return _item_to_dict(item)


# ── Proposals from findings (called from the agent-run complete path) ──


async def propose_from_findings(
    db: AsyncSession,
    project_id: str,
    run_id: str,
    findings: list[dict[str, Any]],
    created_by_user_id: str | None,
    created_actor_type: str = "user",
    created_service_key_id: str | None = None,
    created_service_key_name: str | None = None,
) -> None:
    """Turn review findings into proposed checklist items (design §3 "learn").

    - A finding with ``class == "new"`` proposes a new item keyed by a slug
      derived from its title; if that slug already exists, the re-hit bumps
      ``times_found`` instead of duplicating (dedupe by ``(project_id, slug)``).
    - A finding whose ``class`` is an existing item's slug bumps that item.

    The batch is bounded: at most ``_MAX_NEW_PROPOSALS_PER_RUN`` new rows per
    completion and at most ``_MAX_PROPOSED_PER_PROJECT`` proposed rows per
    project (bumps are unaffected). Existing slugs are prefetched in one
    ``IN(...)`` query so the insert-vs-bump decision costs one round-trip.

    Best-effort and self-committing: the caller wraps this so a proposal
    failure never fails the agent-run completion. Items start ``proposed`` so
    a project accepts or skips each.
    """
    now = datetime.now(timezone.utc)
    new_findings: list[tuple[str, str, dict[str, Any]]] = []  # (slug, title, ref)
    bump_findings: list[tuple[str, dict[str, Any]]] = []  # (slug, ref)
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        class_ = finding.get("class")
        if class_ == "new":
            # Sanitize/redact BEFORE slugifying so a secret in the finding
            # title never lands in the slug (review checklist #7).
            title = _sanitize_text(str(finding.get("title") or "").strip())
            slug = _slugify(title)
            if not slug:
                continue
            # Keep only {run_id, title(redacted)} — never the raw finding dict.
            new_findings.append((slug, title, {"run_id": run_id, "title": title}))
        elif isinstance(class_, str) and _SLUG_RE.match(class_):
            ref = {
                "run_id": run_id,
                "title": _sanitize_text(str(finding.get("title") or "").strip()),
            }
            bump_findings.append((class_, ref))

    # One IN(...) query for the whole batch instead of a SELECT per finding.
    all_slugs = [s for s, _, _ in new_findings] + [s for s, _ in bump_findings]
    rows_by_slug = await _prefetch_by_slugs(db, project_id, all_slugs)
    known_slugs: set[str] = set(rows_by_slug)
    proposed_count = await _proposed_count(db, project_id)

    created = 0
    for slug, title, ref in new_findings:
        if created >= _MAX_NEW_PROPOSALS_PER_RUN:
            break
        if slug in known_slugs:
            row = rows_by_slug.get(slug)
            if row is not None:
                await _bump(db, row, ref, now)
            continue
        if proposed_count >= _MAX_PROPOSED_PER_PROJECT:
            break
        if await _propose_new(
            db,
            project_id,
            slug,
            title,
            ref,
            created_by_user_id,
            now,
            created_actor_type,
            created_service_key_id,
            created_service_key_name,
        ):
            created += 1
            proposed_count += 1
        known_slugs.add(slug)

    for slug, ref in bump_findings:
        row = rows_by_slug.get(slug)
        if row is None:
            # It may have been created by a ``class="new"`` finding earlier in
            # this same batch.
            row = await _select_item(db, project_id, slug)
        if row is not None:
            await _bump(db, row, ref, now)

    await db.commit()


async def _prefetch_by_slugs(
    db: AsyncSession, project_id: str, slugs: list[str]
) -> dict[str, LoopChecklistItem]:
    if not slugs:
        return {}
    rows = (
        (
            await db.execute(
                select(LoopChecklistItem).where(
                    LoopChecklistItem.project_id == project_id,
                    LoopChecklistItem.slug.in_(slugs),
                )
            )
        )
        .scalars()
        .all()
    )
    return {r.slug: r for r in rows}


async def _proposed_count(db: AsyncSession, project_id: str) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(LoopChecklistItem)
            .where(
                LoopChecklistItem.project_id == project_id,
                LoopChecklistItem.status == "proposed",
            )
        )
    ) or 0


async def _select_item(
    db: AsyncSession, project_id: str, slug: str
) -> LoopChecklistItem | None:
    return (
        await db.execute(
            select(LoopChecklistItem).where(
                LoopChecklistItem.project_id == project_id,
                LoopChecklistItem.slug == slug,
            )
        )
    ).scalar_one_or_none()


async def _propose_new(
    db: AsyncSession,
    project_id: str,
    slug: str,
    title: str,
    ref: dict[str, Any],
    created_by_user_id: str | None,
    now: datetime,
    created_actor_type: str = "user",
    created_service_key_id: str | None = None,
    created_service_key_name: str | None = None,
) -> bool:
    """Insert a new proposal; return True if created, False if a concurrent
    proposal won the slug and we bumped that row instead."""
    try:
        # A savepoint scopes just our INSERT so a unique-slug clash with a
        # concurrent proposal can't poison the surrounding transaction
        # (review checklist #11).
        async with db.begin_nested():
            db.add(
                LoopChecklistItem(
                    id=f"lci_{uuid.uuid4().hex[:16]}",
                    project_id=project_id,
                    slug=slug,
                    title=_sanitize_text(title)[:_MAX_TITLE],
                    check_text=_sanitize_text(title)[:_MAX_CHECK_TEXT],
                    status="proposed",
                    source="finding",
                    source_finding_refs=json.dumps([ref]),
                    times_found=1,
                    last_found_at=now,
                    created_by_user_id=created_by_user_id,
                    created_actor_type=created_actor_type,
                    created_service_key_id=created_service_key_id,
                    created_service_key_name=created_service_key_name,
                    accepted_by_user_id=None,
                )
            )
            await db.flush()
        return True
    except IntegrityError:
        # A rival proposal landed this slug between our prefetch and INSERT.
        # The savepoint rollback already dropped our losing row from the
        # session, so bump the winner instead of duplicating.
        existing = await _select_item(db, project_id, slug)
        if existing is None:
            raise
        await _bump(db, existing, ref, now)
        return False


async def _bump(
    db: AsyncSession, item: LoopChecklistItem, ref: dict[str, Any], now: datetime
) -> None:
    """Atomically bump the hit counter, then append the provenance ref.

    The counter increment is a single guarded UPDATE so concurrent re-hits
    never lose a count (review checklist #11); the refs list is appended on
    the already-selected row and capped to the most recent N.
    """
    await db.execute(
        update(LoopChecklistItem)
        .where(LoopChecklistItem.id == item.id)
        .values(
            times_found=LoopChecklistItem.times_found + 1,
            last_found_at=now,
        )
    )
    refs = _decode_refs(item.source_finding_refs)
    refs.append(ref)
    item.source_finding_refs = json.dumps(refs[-_MAX_FINDING_REFS:])
