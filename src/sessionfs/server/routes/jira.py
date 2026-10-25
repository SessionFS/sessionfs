"""Jira integration routes (GitHub #98).

Org-scoped connection + mapping + health under
``/api/v1/orgs/{org_id}/integrations/jira``; ticket promotion under
``/api/v1/projects/{pid}/tickets/{tid}/jira``. Enterprise-only (feature flag
``jira_integration``). One-way: SessionFS creates/updates the Jira story and
reads its status on demand; Jira edits never write back.

The Jira router must be registered before ``projects.router`` in ``app.py``
because the latter is a ``/{git_remote_normalized:path}`` catch-all.
"""

from __future__ import annotations

import asyncio
import difflib
import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import (
    AuthContext,
    get_current_user,
    require_scope,
)
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    OrgAuditEvent,
    OrgMember,
    OrgTrackerConnection,
    Organization,
    Project,
    Ticket,
    TicketComment,
    TicketTrackerLink,
    TrackerProjectMapping,
    User,
)
from sessionfs.server.integrations.trackers import (
    DEFAULT_TEMPLATE,
    JiraClient,
    get_template,
)
from sessionfs.server.integrations.trackers.base import TrackerError, TrackerIssue
from sessionfs.server.integrations.trackers.jira import sessionfs_label, validate_site_url
from sessionfs.server.integrations.trackers.promote import (
    ContentTooLarge,
    adf_text_length,
    build_preview,
    payload_hash,
    sections_to_adf,
)
from sessionfs.server.llm_keys import decrypt_secret, encrypt_secret
from sessionfs.server.roles import has_minimum_role
from sessionfs.server.tier_gate import (
    UserContext,
    check_project_feature,
    get_user_context,
    resolve_org_plan_tier,
)
from sessionfs.server.tiers import get_features_for_tier, get_minimum_tier_for_feature

router = APIRouter(prefix="/api/v1/projects", tags=["jira"])
org_router = APIRouter(prefix="/api/v1/orgs", tags=["jira"])

logger = logging.getLogger("sessionfs.api")

_ORG_NOT_FOUND = "Organization not found"

# Jira project keys are all-uppercase (``ACME``, ``ACME_1``) — 2 to 10 chars.
_JIRA_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,9}$")

# A pending publish claim older than this is treated as abandoned and can be
# reclaimed (the worker that made it is presumed dead).
_CLAIM_STALE_SECONDS = 300

# Statuses Jira reports back are truncated to the link column's width before
# they are persisted, so a long custom status name can't overflow the column.
_LAST_STATUS_MAX = 50

# Jira Cloud caps a description field near 32,767 characters; refuse anything
# over this conservative 32,000 before any outbound call so we never send a
# body Jira will reject.
_MAX_ADF_DESCRIPTION_CHARS = 32000

# A timed-out create's issue can lag Jira's search index, so after a label
# search comes up empty, wait this long and search once more before creating
# (a just-created issue is adopted instead of duplicated). Injectable for tests.
_ADOPT_RETRY_DELAY_SECONDS = 2.0


def _aware(dt: datetime | None) -> datetime | None:
    """Normalize a stored timestamp to aware UTC (SQLite drops tzinfo)."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


# ── Request bodies ──


class JiraConnectionPut(BaseModel):
    # Caps mirror the org_tracker_connections column widths so an oversize
    # body is rejected with a 422 up front instead of overflowing the column
    # (and surfacing as a 500) on insert.
    site_url: str = Field(max_length=255)
    account_email: str = Field(max_length=255)
    api_token: str = Field(max_length=2048)
    default_template: str = Field(max_length=50)


class JiraMappingPut(BaseModel):
    jira_project_key: str = Field(max_length=10)
    issue_type: str = Field("Story", max_length=50)
    template: str | None = None
    done_status_name: str | None = Field(None, max_length=50)

    @field_validator("jira_project_key")
    @classmethod
    def _valid_project_key(cls, value: str) -> str:
        key = value.strip()
        if not _JIRA_PROJECT_KEY_RE.match(key):
            raise ValueError("jira_project_key must look like ACME or ACME_1")
        return key


class JiraParagraph(BaseModel):
    text: str = Field(max_length=10000)
    citations: list[str] = Field(default_factory=list, max_length=20)

    @field_validator("citations")
    @classmethod
    def _citation_lengths(cls, value: list[str]) -> list[str]:
        if any(len(c) > 64 for c in value):
            raise ValueError("each citation must be at most 64 characters")
        return value


class JiraSection(BaseModel):
    key: str = Field(max_length=64)
    heading: str = Field("", max_length=200)
    paragraphs: list[JiraParagraph] = Field(default_factory=list, max_length=50)


class JiraPublishBody(BaseModel):
    summary: str = Field(min_length=1, max_length=255)
    sections: list[JiraSection] = Field(min_length=1, max_length=20)
    confirm: bool


# ── Org-scoped authorization ──


def _insufficient_role(current_role: str | None) -> dict:
    return {
        "error": "insufficient_role",
        "current_role": current_role or "none",
        "required_role": "admin",
        "message": "Managing the Jira integration requires the admin role.",
    }


async def _authorize_org_jira(
    db: AsyncSession,
    user: User,
    org_id: str,
) -> tuple[Organization, str]:
    """Return (org, caller_role) for an org owner/admin.

    Service keys are rejected upstream by ``get_current_user`` (the org-level
    integration holds the Atlassian credential, so only user keys may touch
    it — same rule as ``org_llm_settings``). Reads are admin-only too, since
    connection details, mappings and health all expose credential/config state.

    Mirrors ``_authorize_org_personas``: membership is checked against
    ``{org_id}`` itself, every miss (unknown org, non-member) is the same 404,
    and the feature gate runs only after authorization so the plan lookup
    leaks nothing.
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
    if "jira_integration" not in get_features_for_tier(plan):
        required = get_minimum_tier_for_feature("jira_integration")
        raise HTTPException(
            status_code=403,
            detail={
                "error": "upgrade_required",
                "feature": "jira_integration",
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


# In-memory cache of a connection's Atlassian accountId (from /myself), keyed
# by org_id.  Invalidated when the connection is written or deleted so a
# reconnected account can never be shadowed by a stale id.
_account_id_cache: dict[str, str] = {}


async def _connection_account_id(client: JiraClient, *, org_id: str) -> str:
    """The connection's own accountId, fetched once and cached per org."""
    cached = _account_id_cache.get(org_id)
    if cached is not None:
        return cached
    account_id = await client.myself_account_id()
    _account_id_cache[org_id] = account_id
    return account_id


def _parse_jira_timestamp(value: str | None) -> datetime | None:
    """Parse a Jira ``created``/``updated`` timestamp to aware UTC, or None.

    Jira emits ``2026-10-25T12:00:00.000+0000`` (offset without a colon),
    which ``datetime.fromisoformat`` on Python 3.10 rejects; normalize the
    offset and a trailing ``Z`` first.
    """
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    match = re.search(r"[+-]\d{4}$", text)
    if match and ":" not in match.group(0)[-3:]:
        off = match.group(0)
        text = text[: match.start()] + off[:3] + ":" + off[3:]
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _adoption_block(
    adopted: TrackerIssue,
    mapping: TrackerProjectMapping,
    account_id: str,
    unknown_since: datetime | None,
) -> str | None:
    """Reason the label-found issue may NOT be adopted, or None if adoptable.

    Adoption requires the issue to be in the mapped Jira project, created by
    the connection's own account (creator or reporter), and created after the
    unknown publish began.  We fail closed: a missing project / creator /
    reporter / created timestamp is a rejection, not a pass.
    """
    if not adopted.project_key:
        return f"found Jira issue {adopted.key} without a project"
    if adopted.project_key != mapping.jira_project_key:
        return (
            f"found Jira issue {adopted.key} in project {adopted.project_key}, "
            f"not the mapped project {mapping.jira_project_key}"
        )
    owners = {a for a in (adopted.creator_account_id, adopted.reporter_account_id) if a}
    if not account_id or account_id not in owners:
        return f"found Jira issue {adopted.key} not created by this connection's Jira account"
    created = _parse_jira_timestamp(adopted.created_at)
    if created is None:
        return f"found Jira issue {adopted.key} without a parseable created timestamp"
    # Two minutes of slack for clock skew between Jira and this server; the
    # account check above is what stops a planted issue.
    if unknown_since is not None and created < unknown_since - timedelta(minutes=2):
        return f"found Jira issue {adopted.key} created before the unknown publish began"
    return None


async def _get_connection(org_id: str, db: AsyncSession) -> OrgTrackerConnection | None:
    return (
        await db.execute(
            select(OrgTrackerConnection).where(OrgTrackerConnection.org_id == org_id)
        )
    ).scalar_one_or_none()


def _connection_response(conn: OrgTrackerConnection) -> dict:
    return {
        "connected": True,
        "provider": conn.provider,
        "site_url": conn.site_url,
        "account_email": conn.account_email,
        "has_token": True,
        "token_hint": _token_hint(conn.encrypted_api_token),
        "default_template": conn.default_template,
        "enabled": conn.enabled,
        "last_verified_at": conn.last_verified_at.isoformat() if conn.last_verified_at else None,
        "connected_by_user_id": conn.connected_by_user_id,
    }


# ── Connection ──


@org_router.get("/{org_id}/integrations/jira")
async def get_jira_connection(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _authorize_org_jira(db, user, org_id)
    conn = await _get_connection(org_id, db)
    if conn is None:
        return {"connected": False, "has_token": False}
    return _connection_response(conn)


@org_router.put("/{org_id}/integrations/jira")
async def put_jira_connection(
    org_id: str,
    body: JiraConnectionPut,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org, role = await _authorize_org_jira(db, user, org_id)

    if not body.account_email.strip():
        raise HTTPException(422, "account_email is required")
    if not body.api_token.strip():
        raise HTTPException(422, "api_token is required")
    if get_template(body.default_template) is None:
        raise HTTPException(422, f"unknown template: {body.default_template}")

    try:
        site_url = validate_site_url(body.site_url)
    except TrackerError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "jira_connection_failed", "message": str(exc)},
        ) from exc

    conn = await _get_connection(org_id, db)
    client = JiraClient(site_url, body.account_email, body.api_token)
    try:
        await client.validate_connection()
    except TrackerError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "jira_connection_failed", "message": str(exc)},
        ) from exc
    finally:
        await client.aclose()

    now = datetime.now(timezone.utc)
    before = _connection_response(conn) if conn else None
    if conn is None:
        conn = OrgTrackerConnection(
            org_id=org_id,
            provider="jira",
            site_url=site_url,
            account_email=body.account_email.strip(),
            encrypted_api_token=encrypt_secret(body.api_token),
            default_template=body.default_template,
            enabled=True,
            connected_by_user_id=user.id,
            last_verified_at=now,
        )
        db.add(conn)
    else:
        conn.site_url = client.site_url
        conn.account_email = body.account_email.strip()
        conn.encrypted_api_token = encrypt_secret(body.api_token)
        conn.default_template = body.default_template
        conn.enabled = True
        conn.connected_by_user_id = user.id
        conn.last_verified_at = now

    _org_audit(
        db,
        org=org,
        actor=user,
        actor_role=role,
        event_type="jira_connection_updated",
        target_type="jira_connection",
        target_id=org_id,
        before=before,
        after=_connection_response(conn),
    )
    await db.commit()
    # The account may have changed (or been created); drop the cached
    # accountId so the next adoption check refetches it.
    _account_id_cache.pop(org_id, None)
    return _connection_response(conn)


@org_router.delete("/{org_id}/integrations/jira", status_code=204)
async def delete_jira_connection(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    org, role = await _authorize_org_jira(db, user, org_id)
    conn = await _get_connection(org_id, db)
    if conn is None:
        raise HTTPException(404, "No Jira connection")
    _org_audit(
        db,
        org=org,
        actor=user,
        actor_role=role,
        event_type="jira_connection_deleted",
        target_type="jira_connection",
        target_id=org_id,
        before=_connection_response(conn),
        after=None,
    )
    await db.delete(conn)
    await db.commit()
    _account_id_cache.pop(org_id, None)


# ── Projects (mapping UI) ──


@org_router.get("/{org_id}/integrations/jira/projects")
async def list_jira_projects(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await _authorize_org_jira(db, user, org_id)
    conn = await _get_connection(org_id, db)
    if conn is None:
        raise HTTPException(404, "No Jira connection")
    client = JiraClient(conn.site_url, conn.account_email, decrypt_secret(conn.encrypted_api_token))
    try:
        projects = await client.list_projects()
        # Issue types are fetched lazily via the per-project endpoint below, so
        # listing a large account doesn't issue one createmeta call per project.
        return [
            {"key": project.key, "name": project.name, "issue_types": []}
            for project in projects
        ]
    except TrackerError as exc:
        raise HTTPException(502, {"error": "jira_upstream_error", "message": str(exc)}) from exc
    finally:
        await client.aclose()


@org_router.get("/{org_id}/integrations/jira/projects/{project_key}/issue-types")
async def list_jira_issue_types(
    org_id: str,
    project_key: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await _authorize_org_jira(db, user, org_id)
    if not _JIRA_PROJECT_KEY_RE.match(project_key):
        raise HTTPException(422, "project_key must look like ACME or ACME_1")
    conn = await _get_connection(org_id, db)
    if conn is None:
        raise HTTPException(404, "No Jira connection")
    client = JiraClient(conn.site_url, conn.account_email, decrypt_secret(conn.encrypted_api_token))
    try:
        issue_types = await client.list_issue_types(project_key)
        return [{"name": it.name, "id": it.id} for it in issue_types]
    except TrackerError as exc:
        raise HTTPException(502, {"error": "jira_upstream_error", "message": str(exc)}) from exc
    finally:
        await client.aclose()


# ── Mappings ──


def _mapping_response(m: TrackerProjectMapping) -> dict:
    return {
        "project_id": m.project_id,
        "jira_project_key": m.jira_project_key,
        "issue_type": m.issue_type,
        "template": m.template,
        "done_status_name": m.done_status_name,
    }


async def _get_mapping(
    project_id: str, org_id: str, db: AsyncSession
) -> TrackerProjectMapping | None:
    return (
        await db.execute(
            select(TrackerProjectMapping).where(
                TrackerProjectMapping.project_id == project_id,
                TrackerProjectMapping.org_id == org_id,
            )
        )
    ).scalar_one_or_none()


@org_router.get("/{org_id}/integrations/jira/mappings")
async def list_jira_mappings(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await _authorize_org_jira(db, user, org_id)
    rows = (
        await db.execute(
            select(TrackerProjectMapping).where(TrackerProjectMapping.org_id == org_id)
        )
    ).scalars().all()
    return [_mapping_response(m) for m in rows]


@org_router.get("/{org_id}/integrations/jira/mappings/{project_id}")
async def get_jira_mapping(
    org_id: str,
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _authorize_org_jira(db, user, org_id)
    mapping = await _get_mapping(project_id, org_id, db)
    if mapping is None:
        raise HTTPException(404, "Mapping not found")
    return _mapping_response(mapping)


@org_router.put("/{org_id}/integrations/jira/mappings/{project_id}")
async def put_jira_mapping(
    org_id: str,
    project_id: str,
    body: JiraMappingPut,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org, role = await _authorize_org_jira(db, user, org_id)
    if not body.jira_project_key.strip():
        raise HTTPException(422, "jira_project_key is required")
    if body.template is not None and get_template(body.template) is None:
        raise HTTPException(422, f"unknown template: {body.template}")

    project = (
        await db.execute(
            select(Project).where(Project.id == project_id, Project.org_id == org_id)
        )
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(404, "Project not found")

    mapping = await _get_mapping(project_id, org_id, db)
    before = _mapping_response(mapping) if mapping else None
    if mapping is None:
        mapping = TrackerProjectMapping(
            id=f"tpm_{uuid.uuid4().hex[:16]}",
            org_id=org_id,
            project_id=project_id,
            jira_project_key=body.jira_project_key.strip(),
            issue_type=body.issue_type.strip() or "Story",
            template=body.template,
            done_status_name=body.done_status_name,
            created_by_user_id=user.id,
        )
        db.add(mapping)
    else:
        mapping.jira_project_key = body.jira_project_key.strip()
        mapping.issue_type = body.issue_type.strip() or "Story"
        mapping.template = body.template
        mapping.done_status_name = body.done_status_name

    _org_audit(
        db,
        org=org,
        actor=user,
        actor_role=role,
        event_type="jira_mapping_updated",
        target_type="jira_mapping",
        target_id=project_id,
        before=before,
        after=_mapping_response(mapping),
    )
    await db.commit()
    return _mapping_response(mapping)


@org_router.delete("/{org_id}/integrations/jira/mappings/{project_id}", status_code=204)
async def delete_jira_mapping(
    org_id: str,
    project_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    org, role = await _authorize_org_jira(db, user, org_id)
    mapping = await _get_mapping(project_id, org_id, db)
    if mapping is None:
        raise HTTPException(404, "Mapping not found")
    _org_audit(
        db,
        org=org,
        actor=user,
        actor_role=role,
        event_type="jira_mapping_deleted",
        target_type="jira_mapping",
        target_id=project_id,
        before=_mapping_response(mapping),
        after=None,
    )
    await db.delete(mapping)
    await db.commit()


# ── Health ──


@org_router.get("/{org_id}/integrations/jira/health")
async def jira_health(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _authorize_org_jira(db, user, org_id)

    unmapped_count = (
        await db.execute(
            select(Project.id).where(
                Project.org_id == org_id,
                Project.id.notin_(
                    select(TrackerProjectMapping.project_id).where(
                        TrackerProjectMapping.org_id == org_id
                    )
                ),
            )
        )
    ).scalars().all()

    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    # A pending row is an in-flight publish claim, not a stale/failed link — it
    # must never surface as a broken link. Only ``linked`` rows are links.
    stale_rows = (
        await db.execute(
            select(TicketTrackerLink).where(
                TicketTrackerLink.org_id == org_id,
                TicketTrackerLink.status == "linked",
                or_(
                    TicketTrackerLink.last_status_checked_at.is_(None),
                    TicketTrackerLink.last_status_checked_at < cutoff,
                ),
            )
        )
    ).scalars().all()

    # Real failures, not an empty list: the last push failure is persisted on
    # the ticket's link row and surfaced here (time + sanitized reason only).
    failed_rows = (
        await db.execute(
            select(TicketTrackerLink).where(
                TicketTrackerLink.org_id == org_id,
                TicketTrackerLink.status == "linked",
                TicketTrackerLink.last_push_failed_at.is_not(None),
            )
        )
    ).scalars().all()

    pending_rows = (
        await db.execute(
            select(TicketTrackerLink).where(
                TicketTrackerLink.org_id == org_id,
                TicketTrackerLink.status == "pending",
            )
        )
    ).scalars().all()

    unknown_rows = (
        await db.execute(
            select(TicketTrackerLink).where(
                TicketTrackerLink.org_id == org_id,
                TicketTrackerLink.status == "unknown",
            )
        )
    ).scalars().all()

    return {
        "unmapped_project_count": len(unmapped_count),
        "last_push_failures": [
            {
                "ticket_id": link.ticket_id,
                "issue_key": link.issue_key,
                "failed_at": (
                    link.last_push_failed_at.isoformat()
                    if link.last_push_failed_at
                    else None
                ),
                "reason": link.last_push_failure,
            }
            for link in failed_rows
        ],
        "stale_links": [
            {
                "ticket_id": link.ticket_id,
                "issue_key": link.issue_key,
                "last_status_checked_at": (
                    link.last_status_checked_at.isoformat()
                    if link.last_status_checked_at
                    else None
                ),
            }
            for link in stale_rows
        ],
        "publishes_in_progress": [
            {
                "ticket_id": link.ticket_id,
                "started_at": (
                    link.last_pushed_at.isoformat() if link.last_pushed_at else None
                ),
            }
            for link in pending_rows
        ],
        "publishes_unknown": [
            {
                "ticket_id": link.ticket_id,
                "started_at": (
                    link.last_pushed_at.isoformat() if link.last_pushed_at else None
                ),
            }
            for link in unknown_rows
        ],
    }


# ── Ticket promotion ──


async def _authorize_ticket_jira(
    db: AsyncSession,
    auth: AuthContext,
    ctx: UserContext,
    project_id: str,
    ticket_id: str,
    *,
    project_admin: bool = False,
) -> tuple[Project, Ticket, OrgTrackerConnection, TrackerProjectMapping]:
    from sessionfs.server.auth.dependencies import assert_service_key_can_access_project
    from sessionfs.server.auth.project_access import user_is_project_admin
    from sessionfs.server.routes.knowledge import _get_project_for_auth
    from sessionfs.server.routes.tickets import _get_ticket_or_404

    project = await _get_project_for_auth(project_id, db, auth)
    await assert_service_key_can_access_project(db, auth, project)
    await check_project_feature(ctx, "jira_integration", project, db)

    if project_admin and auth.key_kind == "service":
        # Unlinking needs a real human's project-admin standing; a service key
        # must not sever a promoted issue on the minter's behalf.
        raise HTTPException(
            status_code=403,
            detail={
                "error": "service_key_not_allowed",
                "message": "Unlinking a Jira issue requires a user key with project-admin standing.",
            },
        )
    if project_admin and not await user_is_project_admin(db, auth.user.id, project):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "insufficient_role",
                "current_role": "member",
                "required_role": "admin",
                "message": "Unlinking a Jira issue requires project-admin standing.",
            },
        )

    ticket = await _get_ticket_or_404(project_id, ticket_id, db)

    if project.org_id is None:
        raise HTTPException(
            409,
            {"error": "jira_not_connected", "message": "This project is not org-scoped."},
        )
    # Project read access alone is not enough to act through the org's Jira
    # credential: ``user_can_access_project`` also admits non-members whose
    # captured sessions match the project's remote (e.g. a removed member).
    # Service keys are org-bound by ``assert_service_key_can_access_project``.
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
                    "message": "Only members of this project's organization can use its Jira connection.",
                },
            )
    conn = await _get_connection(project.org_id, db)
    if conn is None or not conn.enabled:
        raise HTTPException(
            409,
            {"error": "jira_not_connected", "message": "This organization has not connected Jira."},
        )
    mapping = await _get_mapping(project.id, project.org_id, db)
    if mapping is None:
        raise HTTPException(
            409,
            {
                "error": "jira_project_unmapped",
                "message": "Ask your admin to add a mapping",
            },
        )
    return project, ticket, conn, mapping


def _template_for(mapping: TrackerProjectMapping, conn: OrgTrackerConnection):
    slug = mapping.template or conn.default_template
    return get_template(slug) or get_template(DEFAULT_TEMPLATE)


async def _get_link(ticket_id: str, db: AsyncSession) -> TicketTrackerLink | None:
    return (
        await db.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket_id)
        )
    ).scalar_one_or_none()


def _assert_link_current(link: TicketTrackerLink, org_id: str, site_url: str) -> None:
    """409 ``jira_link_stale`` when the link points at another org/site.

    A project transfer or a connection re-point leaves the old link in place;
    it must never write to (or read the status of) an unrelated issue on the
    new org/site.
    """
    if link.org_id != org_id or link.site_url != site_url:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "jira_link_stale",
                "message": (
                    "This ticket's Jira link belongs to another organization "
                    "or site; publish/status are blocked to avoid touching an "
                    "unrelated issue. a project admin can unlink it "
                    "(sfs ticket jira unlink) to promote again."
                ),
            },
        )


def _publish_in_progress() -> dict:
    return {
        "error": "publish_in_progress",
        "message": "Another publish for this ticket is already in progress.",
    }


def _content_too_large(section: str) -> HTTPException:
    """422 envelope for verbatim content that can't fit the publish caps."""
    return HTTPException(
        422,
        {
            "error": "content_too_large",
            "section": section,
            "message": (
                f"The {section!r} section is too large to publish as a single "
                "Jira description; nothing was dropped."
            ),
        },
    )


def _content_too_large_for_jira() -> HTTPException:
    """422 envelope for a description that would exceed Jira Cloud's size cap."""
    return HTTPException(
        422,
        {
            "error": "content_too_large_for_jira",
            "message": (
                "The Jira description would exceed 32,000 characters (Jira "
                "Cloud's ~32,767 limit); nothing was published."
            ),
        },
    )


def _published_text(summary: str, sections: list[dict]) -> str:
    """The canonical summary + description text of a published draft.

    Stored on the link at publish time and recomputed for a preview, so the
    re-publish diff compares like with like.
    """
    lines = [summary]
    for section in sections:
        heading = (section.get("heading") or section.get("key") or "").strip()
        if heading:
            lines.append("")
            lines.append(f"## {heading}")
        for para in section.get("paragraphs", []):
            text = (para.get("text") or "").strip()
            if text:
                lines.append(text)
    return "\n".join(lines)


def _unified_diff(old: str, new: str) -> list[str]:
    if old == new:
        return []
    return list(
        difflib.unified_diff(
            old.splitlines(), new.splitlines(), fromfile="published", tofile="draft", lineterm=""
        )
    )


async def _acquire_publish_claim(
    db: AsyncSession, *, ticket_id: str, org_id: str, site_url: str
) -> tuple[TicketTrackerLink, str, str | None]:
    """Claim the publish slot for a ticket; return ``(link, action, token)``.

    ``action`` is ``"update"`` (linked row), ``"create"`` (no row, or a stale
    pending claim we reclaimed) or ``"adopt"`` (a timed-out create left the row
    in ``unknown`` — the caller searches Jira by label and adopts-or-creates).
    A linked row is the update path (after the stale-org/site check) and has no
    claim token — its writes are PK-fenced by the 1:1 row. With no row we insert
    a *pending* row first and commit it, writing a fresh ``claim_token``. A
    concurrent request either 409s ``publish_in_progress`` (fresh claim) or
    atomically reclaims a stale (>5 min) one with a compare-and-swap on the
    token it read; every later write by this request is fenced on ``token`` so a
    reclaimed worker can't overwrite the new owner's link.
    """
    now = datetime.now(timezone.utc)
    row = await db.get(TicketTrackerLink, ticket_id, populate_existing=True)
    if row is None:
        token = secrets.token_hex(16)
        db.add(
            TicketTrackerLink(
                ticket_id=ticket_id,
                org_id=org_id,
                provider="jira",
                status="pending",
                site_url=site_url,
                claim_token=token,
                last_pushed_at=now,  # doubles as the claim timestamp while pending
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            row = await db.get(TicketTrackerLink, ticket_id, populate_existing=True)
            if row is None:
                raise HTTPException(409, _publish_in_progress())
        else:
            return (
                await db.get(TicketTrackerLink, ticket_id, populate_existing=True),
                "create",
                token,
            )

    if row.status == "linked":
        _assert_link_current(row, org_id, site_url)
        # Release the read transaction before the (bounded) outbound update so
        # an unlink during the call is seen by the fenced UPDATE below, and we
        # never sit idle-in-transaction on a 60 s HTTP round-trip.
        await db.commit()
        return row, "update", None

    if row.status == "unknown":
        # A prior publish timed out mid-create; the issue may exist remotely.
        # Reclaim the row as a fresh pending claim (fenced on status='unknown')
        # so the caller searches Jira by label and adopts-or-creates exactly once.
        # ``last_pushed_at`` is deliberately left alone: it records when the
        # original (unknown) publish began, which the adopt check uses to refuse
        # issues that predate it.
        token = secrets.token_hex(16)
        res = await db.execute(
            update(TicketTrackerLink)
            .where(
                TicketTrackerLink.ticket_id == ticket_id,
                TicketTrackerLink.status == "unknown",
            )
            .values(
                status="pending",
                claim_token=token,
                site_url=site_url,
                org_id=org_id,
            )
            .execution_options(synchronize_session=False)
        )
        if res.rowcount != 1:  # type: ignore[attr-defined]
            await db.rollback()
            raise HTTPException(409, _publish_in_progress())
        await db.commit()
        return (
            await db.get(TicketTrackerLink, ticket_id, populate_existing=True),
            "adopt",
            token,
        )

    # A pending row exists: either another request holds a fresh claim, or a
    # dead worker left it behind (>5 min) and we may reclaim it.
    pushed_at = _aware(row.last_pushed_at)
    age = (now - pushed_at).total_seconds() if pushed_at is not None else 0.0
    if age < _CLAIM_STALE_SECONDS:
        raise HTTPException(409, _publish_in_progress())
    token = secrets.token_hex(16)
    # Compare-and-swap: the reclaim only wins if the row still carries the
    # token this request read. If another worker reclaimed first, rowcount is
    # 0 and this request backs off.
    res = await db.execute(
        update(TicketTrackerLink)
        .where(
            TicketTrackerLink.ticket_id == ticket_id,
            TicketTrackerLink.status == "pending",
            TicketTrackerLink.claim_token == row.claim_token,
        )
        .values(
            claim_token=token,
            last_pushed_at=now,
            site_url=site_url,
            org_id=org_id,
        )
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:  # type: ignore[attr-defined]
        await db.rollback()
        raise HTTPException(409, _publish_in_progress())
    await db.commit()
    return (
        await db.get(TicketTrackerLink, ticket_id, populate_existing=True),
        "create",
        token,
    )


async def _fenced_mark_linked(
    db: AsyncSession,
    *,
    ticket_id: str,
    claim_token: str,
    issue: TrackerIssue,
    now: datetime,
    user_id: str,
    digest: str,
    published_text: str,
) -> bool:
    """Flip a pending claim to ``linked``, fenced on ``claim_token``.

    Returns True only when the UPDATE hit exactly one row (this request still
    owns the claim). A False return means the claim was reclaimed out from
    under us and the row must not be touched.
    """
    res = await db.execute(
        update(TicketTrackerLink)
        .where(
            TicketTrackerLink.ticket_id == ticket_id,
            TicketTrackerLink.claim_token == claim_token,
        )
        .values(
            status="linked",
            claim_token=None,
            issue_key=issue.key,
            issue_url=issue.url,
            issue_id=issue.id,
            last_pushed_at=now,
            last_pushed_by_user_id=user_id,
            last_known_status=None,
            last_status_checked_at=None,
            payload_hash=digest,
            last_published_text=published_text,
            last_push_failed_at=None,
            last_push_failure=None,
        )
        .execution_options(synchronize_session=False)
    )
    return res.rowcount == 1  # type: ignore[attr-defined]


async def _fenced_release_claim(
    db: AsyncSession, *, ticket_id: str, claim_token: str
) -> bool:
    """Delete a pending claim, fenced on ``claim_token``.

    Returns True only when this request still owned the claim (rowcount 1).
    Used to release the claim on a definitive create failure.
    """
    res = await db.execute(
        delete(TicketTrackerLink).where(
            TicketTrackerLink.ticket_id == ticket_id,
            TicketTrackerLink.claim_token == claim_token,
        )
    )
    return res.rowcount == 1  # type: ignore[attr-defined]


async def _fenced_mark_unknown(
    db: AsyncSession, *, ticket_id: str, claim_token: str
) -> bool:
    """Flip a pending claim to ``unknown`` (timed-out create), fenced on token.

    Returns True only when this request still owned the claim. Unlike
    ``_fenced_release_claim`` this keeps the row so the next publish can search
    Jira by label and adopt the possibly-created issue instead of duplicating it.
    """
    res = await db.execute(
        update(TicketTrackerLink)
        .where(
            TicketTrackerLink.ticket_id == ticket_id,
            TicketTrackerLink.status == "pending",
            TicketTrackerLink.claim_token == claim_token,
        )
        .values(status="unknown", claim_token=None)
        .execution_options(synchronize_session=False)
    )
    return res.rowcount == 1  # type: ignore[attr-defined]


async def _fenced_mark_updated(
    db: AsyncSession,
    *,
    ticket_id: str,
    issue_key: str,
    issue: TrackerIssue,
    now: datetime,
    user_id: str,
    digest: str,
    published_text: str,
) -> bool:
    """Update an already-linked row, fenced on status + issue_key.

    Returns True only when exactly one ``linked`` row still points at the same
    ``issue_key`` — i.e. the link wasn't unlinked or re-pointed while we were
    outbound. A False return means the caller must 409 ``link_changed`` instead
    of raising StaleDataError/500.
    """
    res = await db.execute(
        update(TicketTrackerLink)
        .where(
            TicketTrackerLink.ticket_id == ticket_id,
            TicketTrackerLink.status == "linked",
            TicketTrackerLink.issue_key == issue_key,
        )
        .values(
            issue_url=issue.url,
            issue_id=issue.id,
            last_pushed_at=now,
            last_pushed_by_user_id=user_id,
            last_known_status=None,
            last_status_checked_at=None,
            payload_hash=digest,
            last_published_text=published_text,
            last_push_failed_at=None,
            last_push_failure=None,
        )
        .execution_options(synchronize_session=False)
    )
    return res.rowcount == 1  # type: ignore[attr-defined]


@router.post("/{project_id}/tickets/{ticket_id}/jira/preview")
async def preview_jira(
    project_id: str,
    ticket_id: str,
    auth: AuthContext = Depends(require_scope("tickets:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project, ticket, conn, mapping = await _authorize_ticket_jira(
        db, auth, ctx, project_id, ticket_id
    )
    template = _template_for(mapping, conn)
    try:
        preview = await build_preview(
            ticket=ticket,
            template=template,
            issue_type=mapping.issue_type,
            jira_project_key=mapping.jira_project_key,
            org=(await db.get(Organization, project.org_id)),  # type: ignore[arg-type]
            user=auth.user,
            db=db,
            # A service key never spends its minter's personal LLM key on the
            # draft rewrite; it falls back to the org key (or the deterministic
            # template) only.
            allow_user_key=auth.key_kind != "service",
        )
    except ContentTooLarge as exc:
        raise _content_too_large(exc.section) from exc
    response = preview.to_dict()
    # The CLI writes this verbatim to ``preview --out``; ticket/project ids let
    # ``publish --from`` refuse a file generated for a different ticket.
    response["ticket_id"] = ticket.id
    response["project_id"] = project.id
    link = await _get_link(ticket.id, db)
    if link is not None and link.status == "linked":
        _assert_link_current(link, project.org_id, conn.site_url)
        response["existing"] = {
            "issue_key": link.issue_key,
            "issue_url": link.issue_url,
            "last_known_status": link.last_known_status,
        }
        if link.last_published_text:
            response["diff"] = _unified_diff(
                link.last_published_text,
                _published_text(preview.summary, preview.sections),
            )
    return response


@router.post("/{project_id}/tickets/{ticket_id}/jira/publish")
async def publish_jira(
    project_id: str,
    ticket_id: str,
    body: JiraPublishBody,
    auth: AuthContext = Depends(require_scope("tickets:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project, ticket, conn, mapping = await _authorize_ticket_jira(
        db, auth, ctx, project_id, ticket_id
    )
    if not body.confirm:
        raise HTTPException(400, "Set confirm=true to publish to Jira")

    # A linked row that points at another org/site blocks publish before we
    # even look at the body — a stale link must never be re-targeted.
    existing_link = await _get_link(ticket_id, db)
    if existing_link is not None and existing_link.status == "linked":
        _assert_link_current(existing_link, project.org_id, conn.site_url)

    # The client never chooses section rendering: mode is resolved from the
    # ticket's template by section key, and an unknown key is rejected before
    # any outbound call.
    template = _template_for(mapping, conn)
    mode_by_key = {s.key: s.mode for s in template.sections}
    unknown = [s.key for s in body.sections if s.key not in mode_by_key]
    if unknown:
        raise HTTPException(422, f"unknown section key(s): {', '.join(unknown)}")

    # Pydantic already validated the summary/sections shape and caps; this is
    # the publish-shape text used for storage and the re-publish diff.
    sections = [s.model_dump() for s in body.sections]
    # Refuse an oversize body on its raw text BEFORE the Markdown conversion
    # (CPU work on the event loop).  Markdown syntax and link URLs are not
    # counted by Jira, so allow twice the cap of raw text; anything beyond
    # that can never fit.
    raw_chars = sum(len(p.text or "") for s in body.sections for p in s.paragraphs)
    if raw_chars > 2 * _MAX_ADF_DESCRIPTION_CHARS:
        raise _content_too_large_for_jira()
    try:
        adf = sections_to_adf(sections, mode_by_key)
    except ContentTooLarge as exc:
        raise _content_too_large(exc.section) from exc

    # Pre-flight size guard before any outbound call: Jira Cloud caps a
    # description near 32,767 characters, so refuse a description that would
    # exceed it rather than sending a body Jira will reject.
    if adf_text_length(adf) > _MAX_ADF_DESCRIPTION_CHARS:
        raise _content_too_large_for_jira()

    # Claim BEFORE any outbound call so two concurrent publishes can't create
    # two Jira issues for the same ticket.
    link, action, claim_token = await _acquire_publish_claim(
        db, ticket_id=ticket_id, org_id=project.org_id, site_url=conn.site_url
    )

    client = JiraClient(
        conn.site_url, conn.account_email, decrypt_secret(conn.encrypted_api_token)
    )
    now = datetime.now(timezone.utc)
    digest = payload_hash(body.summary, sections)
    published_text = _published_text(body.summary, sections)
    try:
        # Bound each outbound call well below the 5-minute reclaim window so a
        # hung request can't hold the claim (and, on timeout, we mark the claim
        # unknown instead of deleting it).
        if action == "update":
            issue = await asyncio.wait_for(
                client.update_issue(link.issue_key, body.summary, adf),
                timeout=60,
            )
            event_type = "jira_issue_updated"
        else:
            # create/adopt: a fresh create (its label lets a later retry adopt
            # it) or a timed-out retry that searches for its prior issue first.
            adopted = None
            if action == "adopt":
                adopted = await asyncio.wait_for(
                    client.search_issue_by_label(
                        mapping.jira_project_key, sessionfs_label(ticket_id)
                    ),
                    timeout=60,
                )
                if adopted is None:
                    # Jira's search index can lag a just-created issue; wait a
                    # moment and look once more before creating, so a timed-out
                    # create is adopted instead of duplicated.
                    await asyncio.sleep(_ADOPT_RETRY_DELAY_SECONDS)
                    adopted = await asyncio.wait_for(
                        client.search_issue_by_label(
                            mapping.jira_project_key, sessionfs_label(ticket_id)
                        ),
                        timeout=60,
                    )
            if adopted is not None:
                if action == "adopt":
                    # Validate before adopting: a label match alone is not proof
                    # that THIS connection created the issue in the mapped
                    # project after the unknown publish began.  A planted issue
                    # (or one from a stale/duplicated connection) keeps the link
                    # 'unknown' and is refused with a clear 409.
                    account_id = await asyncio.wait_for(
                        _connection_account_id(client, org_id=project.org_id),
                        timeout=60,
                    )
                    block = _adoption_block(
                        adopted, mapping, account_id, _aware(link.last_pushed_at)
                    )
                    if block is not None:
                        marked_unknown = await _fenced_mark_unknown(
                            db, ticket_id=ticket_id, claim_token=claim_token
                        )
                        await db.commit()
                        raise HTTPException(
                            409,
                            {
                                "error": "jira_adopt_rejected",
                                "message": block,
                            },
                        )
                issue = await asyncio.wait_for(
                    client.update_issue(adopted.key, body.summary, adf),
                    timeout=60,
                )
                event_type = "jira_issue_updated"
            else:
                issue = await asyncio.wait_for(
                    client.create_issue(
                        mapping.jira_project_key,
                        mapping.issue_type,
                        body.summary,
                        adf,
                        labels=[sessionfs_label(ticket_id)],
                    ),
                    timeout=60,
                )
                event_type = "jira_issue_created"
    except asyncio.TimeoutError as exc:
        if action != "update":
            # Keep the claim row as ``unknown`` (fenced) so the next publish
            # searches by label and adopts-or-creates once; a 504 tells the
            # caller the remote outcome is unknown.
            marked_unknown = await _fenced_mark_unknown(
                db, ticket_id=ticket_id, claim_token=claim_token
            )
            if marked_unknown:
                await db.commit()
            else:
                await db.rollback()
        raise HTTPException(
            504,
            {
                "error": "jira_timeout",
                "message": (
                    "Jira did not respond within 60 s; the issue may or may not "
                    "have been created."
                ),
            },
        ) from exc
    except TrackerError as exc:
        if action != "update":
            # Release the pending claim (fenced) so a later publish can retry.
            released = await _fenced_release_claim(
                db, ticket_id=ticket_id, claim_token=claim_token
            )
            await db.commit()
            if not released:
                # We lost the claim mid-flight (it was reclaimed); nothing to
                # clean up on our side and the row belongs to its new owner.
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
        else:
            # Persist the sanitized failure so /health can surface it.
            link.last_push_failed_at = now
            link.last_push_failure = str(exc)[:300]
            await db.commit()
        raise HTTPException(502, {"error": "jira_upstream_error", "message": str(exc)}) from exc
    finally:
        await client.aclose()

    if action == "update":
        # Fenced UPDATE on status='linked' AND issue_key=<expected>. rowcount 0
        # means the link was unlinked or re-pointed while we were outbound; 409
        # link_changed instead of StaleDataError/500.
        marked = await _fenced_mark_updated(
            db,
            ticket_id=ticket_id,
            issue_key=link.issue_key,
            issue=issue,
            now=now,
            user_id=auth.user.id,
            digest=digest,
            published_text=published_text,
        )
        if not marked:
            await db.rollback()
            raise HTTPException(
                409,
                {
                    "error": "link_changed",
                    "message": (
                        "This ticket's Jira link changed while publishing; "
                        "the update was not applied."
                    ),
                },
            )
    else:
        # Fence the mark-linked write on the claim token. rowcount 0 means the
        # claim was reclaimed while we were outbound: leave the row alone and
        # report the issue we created (for a human to reconcile).
        marked = await _fenced_mark_linked(
            db,
            ticket_id=ticket_id,
            claim_token=claim_token,
            issue=issue,
            now=now,
            user_id=auth.user.id,
            digest=digest,
            published_text=published_text,
        )
        if not marked:
            await db.rollback()
            logger.warning(
                "publish claim for ticket %s was superseded (created %s)",
                ticket_id,
                issue.key,
            )
            raise HTTPException(
                409,
                {
                    "error": "publish_superseded",
                    "message": (
                        "This publish's claim was superseded by a newer "
                        "publish; the Jira issue it created was not linked."
                    ),
                    "issue_key": issue.key,
                    "issue_url": issue.url,
                },
            )

    db.add(
        TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket_id,
            author_user_id=auth.user.id,
            author_persona=None,
            content=(
                f"Promoted to Jira: {issue.url}"
                if event_type == "jira_issue_created"
                else f"Updated Jira issue: {issue.url}"
            ),
            session_id=None,
            created_at=now,
            actor_type=auth.actor_type,
            service_key_id=auth.service_key_id,
            service_key_name=auth.service_key_name,
            verdict_trusted=False,
        )
    )

    org = await db.get(Organization, project.org_id)
    _org_audit(
        db,
        org=org,
        actor=auth.user,
        actor_role=None,
        actor_type=auth.actor_type,
        event_type=event_type,
        target_type="ticket",
        target_id=ticket_id,
        after={"issue_key": issue.key, "issue_url": issue.url},
    )
    await db.commit()

    return {
        "ticket_id": ticket_id,
        "provider": "jira",
        "issue_key": issue.key,
        "issue_url": issue.url,
        "last_pushed_at": now.isoformat(),
        "last_known_status": None,
        "last_status_checked_at": None,
        "payload_hash": digest,
    }


@router.get("/{project_id}/tickets/{ticket_id}/jira")
async def get_jira_link(
    project_id: str,
    ticket_id: str,
    auth: AuthContext = Depends(require_scope("tickets:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project, ticket, conn, _mapping = await _authorize_ticket_jira(
        db, auth, ctx, project_id, ticket_id
    )
    link = await _get_link(ticket.id, db)
    if link is None:
        raise HTTPException(404, "No Jira link for this ticket")
    if link.status == "pending":
        # A pending claim is not a link: no outbound call, just tell the caller
        # a publish is already in flight.
        raise HTTPException(409, _publish_in_progress())
    if link.status == "unknown":
        # A timed-out publish left the row in ``unknown``; there is no confirmed
        # issue yet, so tell the caller to re-publish (which adopts-or-creates).
        raise HTTPException(
            409,
            {
                "error": "jira_publish_unknown",
                "message": (
                    "A previous publish timed out and the Jira issue is not yet "
                    "confirmed; re-publish to resolve it."
                ),
            },
        )
    _assert_link_current(link, project.org_id, conn.site_url)

    now = datetime.now(timezone.utc)
    last_checked = _aware(link.last_status_checked_at)
    cache_fresh = last_checked is not None and (now - last_checked).total_seconds() < 60
    if not cache_fresh:
        client = JiraClient(
            conn.site_url, conn.account_email, decrypt_secret(conn.encrypted_api_token)
        )
        try:
            issue = await client.get_issue(link.issue_key)
        except TrackerError as exc:
            raise HTTPException(502, {"error": "jira_upstream_error", "message": str(exc)}) from exc
        finally:
            await client.aclose()
        # Truncate the remote status name to the column's width so a long
        # custom status name can't overflow the row.
        status = (issue.status or "")[:_LAST_STATUS_MAX]
        link.last_known_status = status or None
        link.last_status_checked_at = now
        await db.commit()

    return {
        "ticket_id": ticket.id,
        "provider": link.provider,
        "issue_key": link.issue_key,
        "issue_url": link.issue_url,
        "last_pushed_at": link.last_pushed_at.isoformat() if link.last_pushed_at else None,
        "last_known_status": link.last_known_status,
        "last_status_checked_at": (
            link.last_status_checked_at.isoformat() if link.last_status_checked_at else None
        ),
        "payload_hash": link.payload_hash,
    }


@router.delete("/{project_id}/tickets/{ticket_id}/jira", status_code=204)
async def unlink_jira(
    project_id: str,
    ticket_id: str,
    auth: AuthContext = Depends(require_scope("tickets:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> None:
    project, ticket, _conn, _mapping = await _authorize_ticket_jira(
        db, auth, ctx, project_id, ticket_id, project_admin=True
    )
    link = await _get_link(ticket.id, db)
    if link is None:
        raise HTTPException(404, "No Jira link for this ticket")

    org = await db.get(Organization, project.org_id)
    _org_audit(
        db,
        org=org,
        actor=auth.user,
        actor_role=None,
        actor_type=auth.actor_type,
        event_type="jira_unlinked",
        target_type="ticket",
        target_id=ticket.id,
        before={"issue_key": link.issue_key, "issue_url": link.issue_url},
        after=None,
    )
    await db.delete(link)
    await db.commit()
