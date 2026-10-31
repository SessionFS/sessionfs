"""Loop share links (Review Loop R3, Brief C3).

Project editors mint revocable, expiring public links to a loop story (one
ticket's ordered event stream) or a ship report. The raw bearer token is
returned exactly once and only its SHA-256 is stored, so a DB leak can't mint
working links. The public view is unauthenticated and serves a DLP-redacted
story with dollars and every author identity stripped; every miss — unknown
token, revoked, expired, deleted project or ticket, or a ship-report link
before the F-track releases tables land — is one identical 404 body.

Routes:
    POST   /api/v1/projects/{id}/loop-share-links            — mint (editor)
    GET    /api/v1/projects/{id}/loop-share-links            — list (editor)
    DELETE /api/v1/projects/{id}/loop-share-links/{link_id}  — revoke (editor)
    GET    /api/v1/share/loop/{token}                        — public view
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import inspect, select, update
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
from sessionfs.server.db.models import LoopShareLink, Project, Ticket
from sessionfs.server.routes.knowledge import _get_project_for_auth
from sessionfs.server.routes.loop_checklist import _sanitize_text
from sessionfs.server.routes.tickets import _get_ticket_or_404
from sessionfs.server.services import releases, review_loop
from sessionfs.server.tier_gate import UserContext, check_project_feature, get_user_context

router = APIRouter(prefix="/api/v1/projects", tags=["loop-share-links"])
share_router = APIRouter()

_MAX_EXPIRES_DAYS = 90
_DEFAULT_EXPIRES_DAYS = 30
# The ship report is built from the releases tables track F adds (#119); until
# they land a ship_report link resolves to the constant 404.
_RELEASES_TABLE = "releases"
# Identity keys a public ship report never carries.
_PUBLIC_DROPPED_KEYS = frozenset(
    {"author_user_id", "created_by_user_id", "service_key_id", "service_key_name"}
)

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_MONTH_RE = re.compile(r"^\d{4}-\d{2}$")


def _not_found() -> HTTPException:
    """The single public-miss body (review checklist #2)."""
    return HTTPException(
        status_code=404,
        detail="Share link not found",
        headers={"Cache-Control": "no-store", "X-Robots-Tag": "noindex"},
    )


async def _editor_scope(
    project_id: str,
    auth: AuthContext,
    db: AsyncSession,
    *,
    opaque_cross_org: bool = False,
) -> Project:
    """Authorize an editor operation.

    Outsiders get a byte-identical 404 (existence oracle); read-only captured
    session holders get 403 ``project_edit_forbidden``; editors pass. Service
    keys keep their org/allowlist boundary via
    ``assert_service_key_can_access_project`` — the read/list path folds a
    cross-org or unallowlisted key into the same 404 (``opaque_cross_org``),
    while writes keep the 403 oracle (review checklist #2).
    """
    if auth.key_kind == "service":
        project = await _get_project_for_auth(project_id, db, auth)
        await assert_service_key_can_access_project(
            db,
            auth,
            project,
            opaque_404_detail="Project not found" if opaque_cross_org else None,
        )
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
    """The user id to record on a creator/revoker stamp.

    Service keys leave it NULL — their provenance goes on the actor columns
    instead, so the minter never stands in for the key (review checklist #3).
    """
    return None if auth.key_kind == "service" else auth.user.id


async def _table_exists(db: AsyncSession, name: str) -> bool:
    conn = await db.connection()

    def _has(sync_conn) -> bool:
        return inspect(sync_conn).has_table(name)

    return await conn.run_sync(_has)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


# ── Serialization ──


def _link_to_dict(link: LoopShareLink) -> dict[str, Any]:
    return {
        "id": link.id,
        "kind": link.kind,
        "ticket_id": link.ticket_id,
        "release_version": link.release_version,
        "month": link.month,
        "created_at": _iso(link.created_at),
        "expires_at": _iso(link.expires_at),
        "revoked_at": _iso(link.revoked_at),
    }


def _redact_public_text(value: str) -> str:
    """Collapse whitespace, strip NULs/surrogates, redact secrets and emails."""
    value = _sanitize_text(value)
    return _EMAIL_RE.sub("[REDACTED:email]", value)


def _redact_value(value: Any) -> Any:
    """Recursively redact every string in the public payload."""
    if isinstance(value, str):
        return _redact_public_text(value)
    if isinstance(value, dict):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def _redact_story(story: dict[str, Any]) -> dict[str, Any]:
    """Serve the story with dollars dropped and free text DLP-redacted.

    ``ticket_story`` already emits persona/role labels (``reviewer``,
    ``role``) and opaque ids (``ticket_id``, ``run_id``) rather than user
    identity; this pass strips any email/secrets that leaked into a title,
    verdict or result summary.
    """
    return _redact_value(story)


# ── Bodies ──


class _CreateShareLink(BaseModel):
    kind: str
    ticket_id: str | None = Field(None, max_length=64)
    release_version: str | None = Field(None, max_length=100)
    month: str | None = Field(None, max_length=7)
    expires_days: int = Field(
        _DEFAULT_EXPIRES_DAYS, ge=1, le=_MAX_EXPIRES_DAYS
    )

    @field_validator("kind")
    @classmethod
    def _kind_shape(cls, value: str) -> str:
        value = (value or "").strip().lower()
        if value not in ("story", "ship_report"):
            raise ValueError("kind must be one of: story, ship_report")
        return value

    @field_validator("month")
    @classmethod
    def _month_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if not _MONTH_RE.match(value):
            raise ValueError("month must be YYYY-MM")
        return value

    @model_validator(mode="after")
    def _shape(self) -> "_CreateShareLink":
        if self.kind == "story":
            if not self.ticket_id:
                raise ValueError("ticket_id is required for kind 'story'")
            if self.release_version or self.month:
                raise ValueError(
                    "release_version/month are only for kind 'ship_report'"
                )
        else:
            if self.ticket_id:
                raise ValueError("ticket_id is only for kind 'story'")
            if not self.release_version and not self.month:
                raise ValueError(
                    "release_version or month is required for kind 'ship_report'"
                )
        return self


# ── Editor routes ──


@router.post("/{project_id}/loop-share-links", status_code=201)
async def create_loop_share_link(
    project_id: str,
    body: _CreateShareLink,
    auth: AuthContext = Depends(require_scope("loops:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Mint a share link; the raw token is returned once and never stored."""
    project = await _editor_scope(project_id, auth, db)
    await check_project_feature(ctx, "agent_tickets", project, db)

    ticket_id: str | None = None
    if body.kind == "story":
        assert body.ticket_id is not None  # enforced by _CreateShareLink._shape
        ticket = await _get_ticket_or_404(project.id, body.ticket_id, db)
        ticket_id = ticket.id

    token = secrets.token_urlsafe(48)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(days=body.expires_days)

    link = LoopShareLink(
        id=f"lsl_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        kind=body.kind,
        ticket_id=ticket_id,
        release_version=(body.release_version or "").strip() or None,
        month=(body.month or "").strip() or None,
        token_hash=token_hash,
        created_by_user_id=_stamp_user_id(auth),
        created_actor_type=auth.actor_type,
        created_service_key_id=auth.service_key_id,
        created_service_key_name=auth.service_key_name,
        expires_at=expires_at,
    )
    db.add(link)
    await db.commit()
    await db.refresh(link)

    # The public page is served by the DASHBOARD app (mirrors the session
    # share link): SFS_DASHBOARD_URL, then SFS_APP_URL, then the hosted app.
    dashboard_url = (
        os.environ.get("SFS_DASHBOARD_URL")
        or os.environ.get("SFS_APP_URL")
        or "https://app.sessionfs.dev"
    ).rstrip("/")
    return {
        "id": link.id,
        "kind": link.kind,
        "ticket_id": link.ticket_id,
        "release_version": link.release_version,
        "month": link.month,
        "token": token,
        "url": f"{dashboard_url}/share/loop/{token}",
        "expires_at": _iso(link.expires_at),
    }


@router.get("/{project_id}/loop-share-links")
async def list_loop_share_links(
    project_id: str,
    auth: AuthContext = Depends(require_scope("loops:read")),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """List a project's share links (newest first); tokens are never returned."""
    project = await _editor_scope(project_id, auth, db, opaque_cross_org=True)
    rows = (
        (
            await db.execute(
                select(LoopShareLink)
                .where(LoopShareLink.project_id == project.id)
                .order_by(LoopShareLink.created_at.desc(), LoopShareLink.id.desc())
            )
        )
        .scalars()
        .all()
    )
    return [_link_to_dict(r) for r in rows]


@router.delete("/{project_id}/loop-share-links/{link_id}", status_code=204)
async def revoke_loop_share_link(
    project_id: str,
    link_id: str,
    auth: AuthContext = Depends(require_scope("loops:write")),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Revoke a share link (soft — the row is kept for audit)."""
    project = await _editor_scope(project_id, auth, db)
    result = await db.execute(
        update(LoopShareLink)
        .where(
            LoopShareLink.id == link_id,
            LoopShareLink.project_id == project.id,
            LoopShareLink.revoked_at.is_(None),
        )
        .values(
            revoked_at=datetime.now(timezone.utc),
            revoked_by_user_id=_stamp_user_id(auth),
            revoked_actor_type=auth.actor_type,
            revoked_service_key_id=auth.service_key_id,
            revoked_service_key_name=auth.service_key_name,
        )
    )
    if result.rowcount != 1:
        await db.rollback()
        raise _not_found()
    await db.commit()


# ── Public view ──


async def _ship_report(db: AsyncSession, link: LoopShareLink) -> dict[str, Any]:
    """Render a ship-report link: no dollars, no identities, redacted text.

    A schema without the ``releases`` tables, or a release that no longer
    exists, resolves to the same constant 404 as every other miss.
    """
    if not await _table_exists(db, _RELEASES_TABLE):
        raise _not_found()
    report = await releases.ship_report(
        db,
        project_id=link.project_id,
        show_dollars=False,
        version=link.release_version,
        month=None if link.release_version else link.month,
    )
    if report is None:
        raise _not_found()
    return _redact_value(_strip_identities(report))


def _strip_identities(value: Any) -> Any:
    """Drop user/service-key identity and any dollar keys from a payload."""
    if isinstance(value, dict):
        return {
            k: _strip_identities(v)
            for k, v in value.items()
            if k not in _PUBLIC_DROPPED_KEYS and "usd" not in k
        }
    if isinstance(value, list):
        return [_strip_identities(v) for v in value]
    return value


@share_router.get("/share/loop/{token}")
async def get_loop_share(
    token: str,
    response: Response,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Public, unauthenticated loop story / ship report.

    ``no-store`` + ``noindex`` so the payload is never cached or indexed.
    Unknown, revoked, expired and deleted-project/ticket links all return the
    byte-identical 404.
    """
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Robots-Tag"] = "noindex"

    token_hash = hashlib.sha256(token.encode()).hexdigest()
    link = (
        await db.execute(
            select(LoopShareLink).where(LoopShareLink.token_hash == token_hash)
        )
    ).scalar_one_or_none()
    if link is None:
        raise _not_found()

    if link.revoked_at is not None:
        raise _not_found()

    _now = datetime.now(timezone.utc).replace(tzinfo=None)
    if link.expires_at.replace(tzinfo=None) < _now:
        raise _not_found()

    project = (
        await db.execute(select(Project).where(Project.id == link.project_id))
    ).scalar_one_or_none()
    if project is None or project.merged_into_project_id is not None:
        raise _not_found()

    if link.kind == "story":
        if not link.ticket_id:
            raise _not_found()
        ticket = (
            await db.execute(
                select(Ticket).where(
                    Ticket.id == link.ticket_id,
                    Ticket.project_id == link.project_id,
                )
            )
        ).scalar_one_or_none()
        if ticket is None:
            raise _not_found()
        story = await review_loop.ticket_story(
            db, project_id=link.project_id, ticket=ticket, show_dollars=False
        )
        return _redact_story(story)

    if link.kind == "ship_report":
        return await _ship_report(db, link)

    raise _not_found()
