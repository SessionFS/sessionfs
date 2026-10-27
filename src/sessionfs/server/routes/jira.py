"""Jira integration routes (GitHub #98).

Org-scoped connection + mapping + health under
``/api/v1/orgs/{org_id}/integrations/jira``; ticket promotion under
``/api/v1/projects/{pid}/tickets/{tid}/jira``. Enterprise-only (feature flag
``jira_integration``). Two-way sync (GitHub #98 part B): outbound status/
comment/evidence pushes through ``services.jira_sync``, and an inbound webhook
(``/webhooks/jira/{org_id}``) mirrors Jira edits back — status transitions are
applied through the same atomic FSM the ticket routes use, field edits become
system comments, and Jira comments become ``source='jira'`` ticket comments.

The Jira router must be registered before ``projects.router`` in ``app.py``
because the latter is a ``/{git_remote_normalized:path}`` catch-all.
"""

from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import logging
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import (
    AuthContext,
    get_current_user,
    require_scope,
)
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    JiraTemplate,
    JiraTemplateVersion,
    OrgAuditEvent,
    OrgMember,
    OrgTrackerConnection,
    Organization,
    Project,
    Ticket,
    TicketComment,
    TicketTrackerLink,
    TrackerProjectMapping,
    TrackerSyncEvent,
    User,
)
from sessionfs.server.integrations.trackers import (
    DEFAULT_TEMPLATE,
    JiraClient,
    Template,
    get_template,
    is_builtin_slug,
    list_templates,
)
from sessionfs.server.integrations.trackers.base import TrackerError, TrackerIssue
from sessionfs.server.integrations.trackers.custom_templates import (
    MAX_TEMPLATES_PER_ORG,
    ResolvedTemplate,
    TemplateValidationError,
    builtin_resolved,
    sections_to_template,
    validate_description,
    validate_name,
    validate_sections,
    validate_slug,
)
from sessionfs.server.config import dashboard_link_base
from sessionfs.server.integrations.trackers.jira import (
    adf_to_text,
    sessionfs_label,
    validate_site_url,
)
from sessionfs.server.services import jira_sync
from sessionfs.server.integrations.trackers.promote import (
    ContentTooLarge,
    adf_text_length,
    build_deterministic_sections,
    build_preview,
    collect_sources,
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
# Inbound webhook (authenticated by the org's per-connection secret, not a user
# token). Mounted under ``/api/v1`` — not the bare ``/webhooks`` GitHub/GitLab
# use — so it is reachable through the Helm dashboard nginx, which proxies only
# /api, /mcp and /health to the API, and on the hosted API alike.
webhook_router = APIRouter(prefix="/api/v1/webhooks", tags=["jira"])

logger = logging.getLogger("sessionfs.api")

_ORG_NOT_FOUND = "Organization not found"

# Jira project keys are all-uppercase (``ACME``, ``ACME_1``) — 2 to 10 chars.
_JIRA_PROJECT_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,9}$")

# Jira project picker: a search term is at most 100 characters, a page at
# most 100 projects (default 50). The legacy unpaged list stops at 500.
_PROJECT_SEARCH_MAX_CHARS = 100
_PROJECT_SEARCH_DEFAULT_LIMIT = 50
_PROJECT_SEARCH_MAX_LIMIT = 100
_PROJECT_LIST_CAP = 500

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
    # Two-way sync settings (GitHub #98 part B). Optional and MERGED: an
    # omitted field keeps its stored value, so reconnecting / rotating the
    # token never resets sync. ``PATCH .../integrations/jira/sync`` changes
    # them without re-entering the token.
    sync_status: bool | None = None
    sync_comments: str | None = None
    status_map: dict | None = None


class JiraSyncPatch(BaseModel):
    """Partial update of the two-way sync settings (merge semantics)."""

    sync_status: bool | None = None
    sync_comments: str | None = None
    status_map: dict | None = None


def _apply_sync_settings(conn: OrgTrackerConnection, body: BaseModel) -> None:
    """Merge the sync fields the caller actually sent (``model_fields_set``).

    An explicit ``status_map: null`` resets the map to the built-in default;
    ``sync_status`` / ``sync_comments`` ignore an explicit null.
    """
    fields = body.model_fields_set
    sync_status = getattr(body, "sync_status", None)
    if "sync_status" in fields and sync_status is not None:
        conn.sync_status = bool(sync_status)
    sync_comments = getattr(body, "sync_comments", None)
    if "sync_comments" in fields and sync_comments is not None:
        conn.sync_comments = jira_sync.validate_sync_comments(sync_comments)
    if "status_map" in fields:
        normalized = jira_sync.validate_status_map(getattr(body, "status_map", None))
        conn.status_map = json.dumps(normalized, separators=(",", ":"))


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
    # Per-ticket template override (optional). When omitted the template falls
    # back to the mapping override, then the org's connection default. An
    # unknown/archived slug is a strict 422 — the caller picked it explicitly.
    template: str | None = Field(None, max_length=50)
    # The template version the draft was previewed with (``preview.template``
    # echoed back). When ``template`` or ``template_version`` is sent and the
    # current resolution differs, publish refuses with 409 template_changed so
    # a story is never rendered with a version nobody previewed.
    template_version: int | None = None


class JiraPreviewBody(BaseModel):
    template: str | None = Field(None, max_length=50)


# ── Custom template request bodies ──


class JiraTemplateSectionBody(BaseModel):
    key: str = Field(max_length=64)
    heading: str = Field(max_length=80)
    mode: str
    source_fields: list[str] = Field(min_length=1)
    guidance: str | None = Field(None, max_length=500)


class JiraTemplateBody(BaseModel):
    slug: str = Field(max_length=50)
    name: str = Field(max_length=120)
    description: str | None = Field(None, max_length=1000)
    sections: list[JiraTemplateSectionBody] = Field(min_length=1, max_length=12)
    # PUT only: the version the editor loaded. A mismatch (someone else saved
    # in between) is a 409 template_version_conflict instead of a lost update.
    expected_version: int | None = None


class JiraTemplatePreviewBody(BaseModel):
    project_id: str = Field(max_length=64)
    ticket_id: str = Field(max_length=64)
    # Optional unsaved draft sections: the editor previews what it is about to
    # save. When omitted the stored template (by slug) is rendered.
    sections: list[JiraTemplateSectionBody] | None = Field(None, min_length=1, max_length=12)
    name: str | None = Field(None, max_length=120)


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
        "sync_status": conn.sync_status,
        "sync_comments": conn.sync_comments or "off",
        "status_map": jira_sync.parse_status_map(conn.status_map),
        # The secret is stored Fernet-encrypted (the server must recompute the
        # HMAC); only the last 4 characters are ever returned.
        "has_webhook_secret": bool(conn.encrypted_webhook_secret),
        "webhook_secret_last4": conn.webhook_secret_last4,
    }


def _jira_webhook_url(request: Request, org_id: str) -> str:
    """Canonical public inbound webhook URL for this org.

    Prefers ``config.public_api_url`` (the operator-declared public origin) and
    falls back to the request's own base URL so a self-hosted install still gets
    a usable value. The path matches ``webhook_router``'s ``/api/v1/webhooks``
    mount.
    """
    config = getattr(request.app.state, "config", None)
    base = str(getattr(config, "public_api_url", "") or "").strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return f"{base}/api/v1/webhooks/jira/{org_id}"


def _sync_event_response(event: TrackerSyncEvent, issue_key: str | None) -> dict:
    """One ledger row for the admin sync-events list and the ticket timeline."""
    try:
        payload = json.loads(event.payload) if event.payload else {}
    except ValueError:
        payload = {}
    return {
        "id": event.id,
        "ticket_id": event.ticket_id,
        "issue_key": issue_key,
        "direction": event.direction,
        "kind": event.kind,
        "payload": payload,
        "jira_event_id": event.jira_event_id,
        "applied": event.applied,
        "error": event.error,
        "state": event.state,
        "retry_count": event.retry_count,
        "next_attempt_at": event.next_attempt_at.isoformat() if event.next_attempt_at else None,
        "updated_at": event.updated_at.isoformat() if event.updated_at else None,
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


# ── Connection ──


@org_router.get("/{org_id}/integrations/jira")
async def get_jira_connection(
    org_id: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _authorize_org_jira(db, user, org_id)
    conn = await _get_connection(org_id, db)
    if conn is None:
        return {"connected": False, "has_token": False}
    return {**_connection_response(conn), "webhook_url": _jira_webhook_url(request, org_id)}


@org_router.put("/{org_id}/integrations/jira")
async def put_jira_connection(
    org_id: str,
    body: JiraConnectionPut,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org, role = await _authorize_org_jira(db, user, org_id)

    if not body.account_email.strip():
        raise HTTPException(422, "account_email is required")
    if not body.api_token.strip():
        raise HTTPException(422, "api_token is required")
    # Validate the sync fields up front (422) before any outbound call.
    if body.sync_comments is not None:
        jira_sync.validate_sync_comments(body.sync_comments)
    if "status_map" in body.model_fields_set:
        jira_sync.validate_status_map(body.status_map)
    # The default may be a built-in slug or an active org-custom template; an
    # unknown or archived slug is rejected with a 422. Re-saving the connection
    # with its unchanged (possibly since-archived) default is accepted, so
    # rotating a token never silently switches the org default.
    existing_conn = await _get_connection(org_id, db)
    await _resolve_template(
        db,
        org_id,
        body.default_template,
        strict=True,
        allow_archived=(
            existing_conn is not None
            and existing_conn.default_template == body.default_template
        ),
    )

    try:
        site_url = validate_site_url(body.site_url)
    except TrackerError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "jira_connection_failed", "message": str(exc)},
        ) from exc

    conn = existing_conn
    client = JiraClient(site_url, body.account_email, body.api_token)
    try:
        identity = await client.validate_connection()
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
            sync_status=False,
            sync_comments="off",
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
    _apply_sync_settings(conn, body)
    account_id = identity.get("account_id") if isinstance(identity, dict) else None
    conn.jira_account_id = str(account_id)[:128] if account_id else None

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
    return {**_connection_response(conn), "webhook_url": _jira_webhook_url(request, org_id)}


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


@org_router.patch("/{org_id}/integrations/jira/sync")
async def patch_jira_sync(
    org_id: str,
    body: JiraSyncPatch,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Change the two-way sync settings without re-entering the API token.

    Merge semantics: only the fields sent change (``status_map: null`` resets
    the map to the default). Org owner/admin, Enterprise.
    """
    org, role = await _authorize_org_jira(db, user, org_id)
    conn = await _get_connection(org_id, db)
    if conn is None:
        raise HTTPException(404, "No Jira connection")
    before = _connection_response(conn)
    _apply_sync_settings(conn, body)
    _org_audit(
        db,
        org=org,
        actor=user,
        actor_role=role,
        event_type="jira_sync_settings_updated",
        target_type="jira_connection",
        target_id=org_id,
        before=before,
        after=_connection_response(conn),
    )
    await db.commit()
    return {**_connection_response(conn), "webhook_url": _jira_webhook_url(request, org_id)}


# ── Inbound webhook secret (generate / rotate) ──


@org_router.post("/{org_id}/integrations/jira/webhook-secret")
async def rotate_jira_webhook_secret(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Generate (or rotate) the org's inbound Jira webhook secret.

    Returns the secret ONCE in the response. It is stored Fernet-encrypted
    (the server must recompute the HMAC to verify a delivery) alongside a
    sha256 fingerprint; only the last 4 characters are ever shown again. The
    webhook is authenticated by ``X-Hub-Signature: sha256=<hex>`` over the raw
    body. Rotating invalidates every prior delivery.
    """
    org, role = await _authorize_org_jira(db, user, org_id)
    conn = await _get_connection(org_id, db)
    if conn is None:
        raise HTTPException(404, "No Jira connection")

    secret = jira_sync.generate_webhook_secret()
    conn.encrypted_webhook_secret = encrypt_secret(secret)
    conn.webhook_secret_hash = jira_sync.hash_webhook_secret(secret)
    conn.webhook_secret_last4 = jira_sync.webhook_secret_last4(secret)

    _org_audit(
        db,
        org=org,
        actor=user,
        actor_role=role,
        event_type="jira_webhook_secret_rotated",
        target_type="jira_connection",
        target_id=org_id,
    )
    await db.commit()
    return {"webhook_secret": secret, "webhook_secret_last4": conn.webhook_secret_last4}


# ── Inbound webhook ──

# Rejected deliveries are counted on the connection in a rolling 24 h window;
# at most one audit row per org per hour records that rejections happened.
_AUTH_FAILURE_WINDOW = timedelta(hours=24)
_AUTH_FAILURE_AUDIT_EVERY = timedelta(hours=1)
_WEBHOOK_ID_HEADER = "x-atlassian-webhook-identifier"


def _webhook_actor(payload: dict) -> tuple[str, str | None]:
    """(display name, accountId) of the Jira user behind a delivery."""
    user = payload.get("user") if isinstance(payload.get("user"), dict) else None
    display = "a Jira user"
    account_id: str | None = None
    if isinstance(user, dict):
        for key in ("displayName", "name", "key"):
            value = user.get(key)
            if isinstance(value, str) and value.strip():
                display = value.strip()
                break
        if isinstance(user.get("accountId"), str):
            account_id = user["accountId"]
    return display, account_id


def _own_account_id(conn: OrgTrackerConnection) -> str | None:
    """The connection's own Atlassian accountId, when known."""
    return conn.jira_account_id or _account_id_cache.get(conn.org_id) or None


def _field_change_excerpt(item: dict) -> str:
    """A short ``from → to`` excerpt for one changelog item (capped later)."""
    parts: list[str] = []
    for key in ("fromString", "toString"):
        value = item.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    if not parts:
        for key in ("from", "to"):
            value = item.get(key)
            if value not in (None, "") and not isinstance(value, (dict, list)):
                parts.append(str(value))
    return " → ".join(parts)


def _jira_event_id(payload: dict, event_type: str, delivery_id: str | None) -> str:
    """A deterministic per-delivery idempotency key (stable across retries).

    Jira's ``X-Atlassian-Webhook-Identifier`` header is the primary key: it is
    unique per webhook event and repeated on retries. Without it, the key falls
    back to Jira's own ids — namespaced by event type, so a ``comment_created``
    delivery is never deduplicated against the ``jira:issue_updated``
    (issue_commented) delivery Jira sends for the same comment — and finally to
    a hash of the canonical payload.
    """
    if delivery_id:
        return f"wh:{delivery_id.strip()[:100]}"
    ns = event_type[:40]
    comment = payload.get("comment")
    if isinstance(comment, dict) and comment.get("id"):
        return f"{ns}:comment:{str(comment['id'])[:60]}"
    changelog = payload.get("changelog")
    if isinstance(changelog, dict) and changelog.get("id"):
        return f"{ns}:changelog:{str(changelog['id'])[:60]}"
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:32]
    return f"{ns}:{digest}"


async def _resolve_inbound_link(
    db: AsyncSession, conn: OrgTrackerConnection, payload: dict
) -> TicketTrackerLink | None:
    """The linked ticket for the delivery's issue, on the connection's site.

    Duplicate rows (which the schema does not forbid across tickets) resolve
    to the most recently pushed link instead of raising.
    """
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else None
    issue_key = issue.get("key") if issue else None
    issue_id = issue.get("id") if issue else None
    conditions = [
        TicketTrackerLink.org_id == conn.org_id,
        TicketTrackerLink.status == "linked",
        TicketTrackerLink.site_url == conn.site_url,
    ]
    for column, value in (
        (TicketTrackerLink.issue_key, issue_key),
        (TicketTrackerLink.issue_id, issue_id),
    ):
        if not isinstance(value, (str, int)) or value == "":
            continue
        link = (
            await db.execute(
                select(TicketTrackerLink)
                .where(*conditions, column == str(value))
                .order_by(
                    TicketTrackerLink.last_pushed_at.desc().nullslast(),
                    TicketTrackerLink.ticket_id,
                )
                .limit(1)
            )
        ).scalars().first()
        if link is not None:
            return link
    return None


async def _record_webhook_auth_failure(
    db: AsyncSession, conn: OrgTrackerConnection, reason: str
) -> None:
    """Count a rejected delivery without one write per request into the audit log.

    The connection carries a rolling 24 h counter (reset when the window is
    older than 24 h) that health reports; an audit row is added at most once
    per org per hour so the rejection is still on the record.
    """
    now = datetime.now(timezone.utc)
    window_cut = now - _AUTH_FAILURE_WINDOW
    fresh_window = or_(
        OrgTrackerConnection.webhook_auth_failure_window_start.is_(None),
        OrgTrackerConnection.webhook_auth_failure_window_start < window_cut,
    )
    await db.execute(
        update(OrgTrackerConnection)
        .where(OrgTrackerConnection.org_id == conn.org_id)
        .values(
            webhook_auth_failure_count=case(
                (fresh_window, 1),
                else_=OrgTrackerConnection.webhook_auth_failure_count + 1,
            ),
            webhook_auth_failure_window_start=case(
                (fresh_window, now),
                else_=OrgTrackerConnection.webhook_auth_failure_window_start,
            ),
            last_webhook_auth_failure_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    recent = (
        await db.execute(
            select(OrgAuditEvent.id)
            .where(
                OrgAuditEvent.org_id == conn.org_id,
                OrgAuditEvent.event_type == "jira_webhook_auth_failure",
                OrgAuditEvent.created_at >= now - _AUTH_FAILURE_AUDIT_EVERY,
            )
            .limit(1)
        )
    ).first()
    if recent is not None:
        return
    org = await db.get(Organization, conn.org_id)
    db.add(
        OrgAuditEvent(
            id=f"oae_{uuid.uuid4().hex[:24]}",
            org_id=conn.org_id,
            org_name_snapshot=org.name if org is not None else conn.org_id,
            event_type="jira_webhook_auth_failure",
            actor_user_id=None,
            actor_email_snapshot="jira",
            actor_role_at_time="jira_webhook",
            target_type="jira_webhook",
            target_id=conn.org_id,
            after=json.dumps({"reason": reason}, separators=(",", ":")),
            created_at=now,
        )
    )


def _webhook_auth_failures_24h(conn: OrgTrackerConnection) -> int:
    start = conn.webhook_auth_failure_window_start
    if start is None:
        return 0
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    if start < datetime.now(timezone.utc) - _AUTH_FAILURE_WINDOW:
        return 0
    return int(conn.webhook_auth_failure_count or 0)


def _inbound_audit(db: AsyncSession, org: Organization, ticket_id: str, event_type: str, details: dict) -> None:
    db.add(
        OrgAuditEvent(
            id=f"oae_{uuid.uuid4().hex[:24]}",
            org_id=org.id,
            org_name_snapshot=org.name,
            event_type=event_type,
            actor_user_id=None,
            actor_email_snapshot="jira",
            actor_role_at_time="jira_webhook",
            target_type="ticket",
            target_id=ticket_id,
            after=json.dumps(details, separators=(",", ":")),
        )
    )


async def _read_webhook_body(request: Request) -> bytes:
    """Stream the body, refusing (413) as soon as it passes the cap — a chunked
    request without Content-Length is never buffered beyond 1 MiB."""
    cap = jira_sync.MAX_INBOUND_BODY_BYTES
    length = request.headers.get("content-length")
    if length is not None:
        try:
            if int(length) > cap:
                raise HTTPException(413, "Payload too large")
        except ValueError:
            pass
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            raise HTTPException(413, "Payload too large")
        chunks.append(chunk)
    return b"".join(chunks)


def _system_comment(db: AsyncSession, ticket: Ticket, content: str) -> None:
    db.add(
        TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket.id,
            author_user_id="jira-sync",
            author_persona="system",
            content=content[: jira_sync.INBOUND_COMMENT_MAX_CHARS],
            source="jira",
            verdict_trusted=False,
        )
    )


def _changelog_items(payload: dict) -> list[dict]:
    changelog = payload.get("changelog") if isinstance(payload.get("changelog"), dict) else None
    items = changelog.get("items") if changelog else None
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _item_field(item: dict) -> str:
    for key in ("fieldId", "field"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return ""


async def _handle_issue_updated(
    db: AsyncSession,
    conn: OrgTrackerConnection,
    org: Organization,
    project: Project,
    link: TicketTrackerLink,
    ticket: Ticket,
    payload: dict,
    base_event_id: str,
) -> list[str]:
    """Field edits → system comments; a status change → the mapped action.

    Only changelog items count: an ``issue_updated`` without a status item
    never touches the ticket's status (the issue's *current* status is in
    every delivery, so reading it would re-apply state on unrelated edits).
    Returns outbox ids to drain after commit (completion evidence).
    """
    user_display, actor_account_id = _webhook_actor(payload)
    own_account = _own_account_id(conn)
    seq = 0

    def next_event_id() -> str:
        nonlocal seq
        seq += 1
        return base_event_id if seq == 1 else f"{base_event_id}#{seq}"

    items = _changelog_items(payload)
    # SessionFS never pushes summary/description/priority changes, so a field
    # edit is never our echo — even from the connection's own account (admins
    # often connect with their personal token).
    for item in items:
        field = _item_field(item)
        if field not in jira_sync.SYSTEM_COMMENT_FIELDS:
            continue
        excerpt = jira_sync.redact_then_cap(
            _field_change_excerpt(item), org, jira_sync.FIELD_EXCERPT_MAX_CHARS
        )
        who = jira_sync.redact_then_cap(user_display, org, 120)
        text = f"Edited in Jira by {who}: {field} changed"
        if excerpt:
            text += f" ({excerpt})"
        _system_comment(db, ticket, text)
        jira_sync.record_sync_event(
            db,
            org_id=link.org_id,
            link_id=link.ticket_id,
            ticket_id=ticket.id,
            direction="inbound",
            kind="field_edit",
            payload={"field": field, "excerpt": excerpt},
            jira_event_id=next_event_id(),
            applied=True,
        )

    status_item = next((i for i in items if _item_field(i) == "status"), None)
    if status_item is None or not conn.sync_status:
        return []
    issue = payload.get("issue") if isinstance(payload.get("issue"), dict) else None
    fields = issue.get("fields") if issue else None
    status = fields.get("status") if isinstance(fields, dict) else None
    category = status.get("statusCategory") if isinstance(status, dict) else None
    category_key = category.get("key") if isinstance(category, dict) else None
    action = jira_sync.mapped_action(
        jira_sync.parse_status_map(conn.status_map),
        category_key if isinstance(category_key, str) else None,
    )
    if action is None:
        return []
    to_name = status_item.get("toString") if isinstance(status_item.get("toString"), str) else None
    base_payload = {"category": category_key, "action": action, "jira_status": to_name}

    # An echo is a delivery that matches something we actually pushed: a
    # recent outbound transition with the same name for the same target
    # status. The connection's accountId is only a hint — it can rule an echo
    # out (a different actor), never in.
    actor_differs = bool(own_account and actor_account_id and actor_account_id != own_account)
    echo = not actor_differs and await jira_sync.recent_outbound_transition(
        db,
        link.ticket_id,
        to_name=to_name,
        to_status=jira_sync.action_target(action, ticket.kind),
    )
    if echo:
        # Our own outbound transition coming back: record it for the timeline
        # but never act on it.
        jira_sync.record_sync_event(
            db,
            org_id=link.org_id,
            link_id=link.ticket_id,
            ticket_id=ticket.id,
            direction="inbound",
            kind="status",
            payload={**base_payload, "outcome": "echo"},
            jira_event_id=next_event_id(),
            state=jira_sync.STATE_SKIPPED,
        )
        return []

    before = ticket.status
    outcome, error = await jira_sync.apply_inbound_action(db, project, ticket, action)
    await db.refresh(ticket)
    jira_sync.record_sync_event(
        db,
        org_id=link.org_id,
        link_id=link.ticket_id,
        ticket_id=ticket.id,
        direction="inbound",
        kind="status",
        payload={**base_payload, "from_status": before, "to_status": ticket.status, "outcome": outcome},
        jira_event_id=next_event_id(),
        applied=outcome == "applied",
        error=error,
    )
    if outcome == "applied":
        link.last_known_status = (to_name or "")[:_LAST_STATUS_MAX] or link.last_known_status
        if jira_sync.is_terminal_status(ticket.status, ticket.kind):
            evidence_id = await jira_sync.enqueue_evidence(db, ticket, link)
            return [evidence_id] if evidence_id else []
    return []


async def _handle_comment_created(
    db: AsyncSession,
    conn: OrgTrackerConnection,
    org: Organization,
    link: TicketTrackerLink,
    ticket: Ticket,
    payload: dict,
    event_id: str,
) -> None:
    if (conn.sync_comments or "off") not in jira_sync.INBOUND_COMMENT_MODES:
        return
    comment = payload.get("comment") if isinstance(payload.get("comment"), dict) else None
    if comment is None:
        return
    author = comment.get("author") if isinstance(comment.get("author"), dict) else {}
    body = comment.get("body")
    raw_text = adf_to_text(body) if isinstance(body, dict) else (str(body) if body else "")
    raw_id = comment.get("id")
    jira_comment_id = str(raw_id) if isinstance(raw_id, (str, int)) and raw_id != "" else None
    # Our own outbound comments come back as comment_created. Drop only those
    # we can tie to a post we made (recorded comment id or our outbox
    # reference); a human comment from the connection's account is mirrored.
    author_account = author.get("accountId") if isinstance(author.get("accountId"), str) else None
    if await jira_sync.is_pushed_comment(
        db,
        link.ticket_id,
        jira_comment_id,
        raw_text,
        author_account_id=author_account,
        own_account_id=_own_account_id(conn),
    ):
        return
    display = author.get("displayName") or author.get("name") or "a Jira user"
    if not isinstance(display, str):
        display = "a Jira user"
    text = jira_sync.redact_then_cap(raw_text, org, jira_sync.INBOUND_COMMENT_MAX_CHARS)
    if not text.strip():
        text = "(empty Jira comment)"
    label = jira_sync.jira_author_label(display, org)
    db.add(
        TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket.id,
            author_user_id="jira-sync",
            author_persona=label,
            content=text,
            source="jira",
            verdict_trusted=False,
        )
    )
    comment_id = comment.get("id")
    jira_sync.record_sync_event(
        db,
        org_id=link.org_id,
        link_id=link.ticket_id,
        ticket_id=ticket.id,
        direction="inbound",
        kind="comment",
        payload={
            "author": label,
            "comment_id": str(comment_id)[:60] if isinstance(comment_id, (str, int)) else None,
        },
        jira_event_id=event_id,
        applied=True,
    )


@webhook_router.post("/jira/{org_id}")
async def jira_inbound_webhook(
    org_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Inbound Jira webhook (HMAC-authenticated by the per-connection secret).

    Fails closed: no connection → 404, no secret → 503, missing/invalid
    signature → 401. An unknown issue is 200-ignored (Jira keeps retrying
    otherwise). Idempotent per (link, delivery id). The response never carries
    ticket data.
    """
    conn = await _get_connection(org_id, db)
    if conn is None or not conn.enabled:
        raise HTTPException(404, _ORG_NOT_FOUND)
    secret = None
    if conn.encrypted_webhook_secret:
        try:
            secret = decrypt_secret(conn.encrypted_webhook_secret)
        except Exception:
            secret = None
    if not secret:
        raise HTTPException(503, "Jira webhooks are not configured for this org")
    body = await _read_webhook_body(request)
    signature = request.headers.get("x-hub-signature")
    if not jira_sync.verify_webhook_signature(secret, body, signature):
        await _record_webhook_auth_failure(db, conn, "invalid_signature")
        await db.commit()
        raise HTTPException(401, "Invalid webhook signature")
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid webhook body")

    event_type = payload.get("webhookEvent") or ""
    if event_type not in {"jira:issue_updated", "comment_created"}:
        return {"status": "ignored", "reason": "unsupported_event"}

    link = await _resolve_inbound_link(db, conn, payload)
    if link is None:
        return {"status": "ignored", "reason": "unknown_issue"}
    ticket = await db.get(Ticket, link.ticket_id)
    project = await db.get(Project, ticket.project_id) if ticket is not None else None
    if ticket is None or project is None:
        return {"status": "ignored", "reason": "missing_ticket"}
    if project.org_id != org_id or link.org_id != org_id:
        # The project moved to another org (or to personal); its old link is
        # stale and the former org's Jira must not drive it.
        return {"status": "ignored", "reason": "stale_link"}

    base_event_id = _jira_event_id(
        payload, event_type, request.headers.get(_WEBHOOK_ID_HEADER)
    )
    if await jira_sync.inbound_event_exists(db, link.ticket_id, base_event_id):
        return {"status": "duplicate"}

    if not await jira_sync.org_has_jira(db, org_id):
        # The plan lost the integration: record why nothing happened and stop.
        jira_sync.record_sync_event(
            db,
            org_id=link.org_id,
            link_id=link.ticket_id,
            ticket_id=ticket.id,
            direction="inbound",
            kind="error",
            payload={"event": event_type},
            jira_event_id=base_event_id,
            error="The org's plan no longer includes the Jira integration.",
        )
        await db.commit()
        return {"status": "ignored", "reason": "not_entitled"}

    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(404, _ORG_NOT_FOUND)
    link.last_inbound_at = datetime.now(timezone.utc)

    outbox: list[str] = []
    if event_type == "jira:issue_updated":
        outbox = await _handle_issue_updated(
            db, conn, org, project, link, ticket, payload, base_event_id
        )
    else:
        await _handle_comment_created(db, conn, org, link, ticket, payload, base_event_id)

    _inbound_audit(db, org, ticket.id, "jira_inbound_webhook", {"event": event_type})
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent delivery of the same event won the unique slot.
        await db.rollback()
        return {"status": "duplicate"}
    jira_sync.schedule_drain(outbox)
    return {"status": "ok"}


# ── Projects (mapping UI) ──


@org_router.get("/{org_id}/integrations/jira/projects", response_model=None)
async def list_jira_projects(
    org_id: str,
    response: Response,
    q: str | None = Query(None, max_length=_PROJECT_SEARCH_MAX_CHARS),
    limit: int | None = Query(None, ge=1, le=_PROJECT_SEARCH_MAX_LIMIT),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict] | dict:
    """Jira projects for the mapping picker.

    With ``q`` and/or ``limit`` (the paged form the dashboard uses) the
    response is ``{"projects": [...], "has_more": bool, "query", "limit"}``:
    one page of at most ``limit`` projects (default 50, max 100), filtered by
    Jira itself on key and name when ``q`` is given. Without either
    parameter the original bare list is returned (first 500 projects) and an
    ``X-Jira-Projects-Truncated: true`` header says when the site has more.
    """
    await _authorize_org_jira(db, user, org_id)
    query = (q or "").strip() or None
    paged = q is not None or limit is not None
    page_limit = (limit or _PROJECT_SEARCH_DEFAULT_LIMIT) if paged else _PROJECT_LIST_CAP
    conn = await _get_connection(org_id, db)
    if conn is None:
        raise HTTPException(404, "No Jira connection")
    client = JiraClient(conn.site_url, conn.account_email, decrypt_secret(conn.encrypted_api_token))
    try:
        page = await client.search_projects(query, page_limit)
    except TrackerError as exc:
        raise HTTPException(502, {"error": "jira_upstream_error", "message": str(exc)}) from exc
    finally:
        await client.aclose()
    # Issue types are fetched lazily via the per-project endpoint below, so
    # listing a large account doesn't issue one createmeta call per project.
    projects = [
        {"key": project.key, "name": project.name, "issue_types": []}
        for project in page.projects
    ]
    if not paged:
        response.headers["X-Jira-Projects-Truncated"] = "true" if page.has_more else "false"
        return projects
    return {
        "projects": projects,
        "has_more": page.has_more,
        "query": query,
        "limit": page_limit,
    }


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
    existing_mapping = await _get_mapping(project_id, org_id, db)
    if body.template is not None:
        # A mapping override may be a built-in slug or an active org-custom
        # template; an unknown or archived slug is rejected with a 422. Keeping
        # the mapping's unchanged (since-archived) override is accepted.
        await _resolve_template(
            db,
            org_id,
            body.template,
            strict=True,
            allow_archived=(
                existing_mapping is not None
                and existing_mapping.template == body.template
            ),
        )

    project = (
        await db.execute(
            select(Project).where(Project.id == project_id, Project.org_id == org_id)
        )
    ).scalar_one_or_none()
    if project is None:
        raise HTTPException(404, "Project not found")

    mapping = existing_mapping
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

    # Two-way sync health: most-recent inbound/outbound link timestamps, failed
    # outbound events in the last 24h, and rejected webhook signatures in the
    # last 24h (both appended-only ledgers, so a count is all we need).
    day_ago = datetime.now(timezone.utc) - timedelta(days=1)
    last_inbound = (
        await db.execute(
            select(func.max(TicketTrackerLink.last_inbound_at)).where(
                TicketTrackerLink.org_id == org_id,
                TicketTrackerLink.status == "linked",
            )
        )
    ).scalar()
    last_synced = (
        await db.execute(
            select(func.max(TicketTrackerLink.last_synced_at)).where(
                TicketTrackerLink.org_id == org_id,
                TicketTrackerLink.status == "linked",
            )
        )
    ).scalar()
    # Failed outbound pushes (pending retry or exhausted), refused inbound
    # transitions, and events skipped for a lost entitlement all carry an
    # ``error``; a retried push that later succeeds clears it.
    failed_sync_events_24h = (
        await db.execute(
            select(func.count(TrackerSyncEvent.id)).where(
                TrackerSyncEvent.org_id == org_id,
                TrackerSyncEvent.error.is_not(None),
                or_(
                    TrackerSyncEvent.created_at >= day_ago,
                    TrackerSyncEvent.updated_at >= day_ago,
                ),
            )
        )
    ).scalar() or 0
    pending_sync_events = (
        await db.execute(
            select(func.count(TrackerSyncEvent.id)).where(
                TrackerSyncEvent.org_id == org_id,
                TrackerSyncEvent.state == jira_sync.STATE_PENDING,
            )
        )
    ).scalar() or 0
    sync_conn = await _get_connection(org_id, db)
    webhook_auth_failures_24h = (
        _webhook_auth_failures_24h(sync_conn) if sync_conn is not None else 0
    )

    # Links, mappings and the connection default pinned to a custom template
    # that has since been archived keep working, but the admin should know
    # they're on a template that can no longer be newly selected.
    archived_slugs = set(
        (
            await db.execute(
                select(JiraTemplate.slug).where(
                    JiraTemplate.org_id == org_id,
                    JiraTemplate.archived_at.is_not(None),
                )
            )
        ).scalars().all()
    )
    archived_template_references: list[dict] = []
    mappings_on_archived_templates: list[dict] = []
    connection_default_archived = False
    if archived_slugs:
        pinned_rows = (
            await db.execute(
                select(TicketTrackerLink).where(
                    TicketTrackerLink.org_id == org_id,
                    TicketTrackerLink.status == "linked",
                    TicketTrackerLink.template_slug.in_(archived_slugs),
                )
            )
        ).scalars().all()
        archived_template_references = [
            {
                "ticket_id": r.ticket_id,
                "template_slug": r.template_slug,
                "template_version": r.template_version,
            }
            for r in pinned_rows
        ]
        mapping_rows = (
            await db.execute(
                select(TrackerProjectMapping).where(
                    TrackerProjectMapping.org_id == org_id,
                    TrackerProjectMapping.template.in_(archived_slugs),
                )
            )
        ).scalars().all()
        mappings_on_archived_templates = [
            {"project_id": m.project_id, "template_slug": m.template}
            for m in mapping_rows
        ]
        conn = await _get_connection(org_id, db)
        connection_default_archived = (
            conn is not None and conn.default_template in archived_slugs
        )

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
        "archived_template_references": archived_template_references,
        "mappings_on_archived_templates": mappings_on_archived_templates,
        "connection_default_archived": connection_default_archived,
        "last_inbound_at": last_inbound.isoformat() if last_inbound else None,
        "last_synced_at": last_synced.isoformat() if last_synced else None,
        "failed_sync_events_24h": failed_sync_events_24h,
        "pending_sync_events": pending_sync_events,
        "webhook_auth_failures_24h": webhook_auth_failures_24h,
    }


@org_router.get("/{org_id}/integrations/jira/sync-events")
async def list_jira_sync_events(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    direction: str | None = Query(None, pattern="^(inbound|outbound)$"),
) -> dict:
    """Recent two-way sync ledger rows for the admin (most-recent first).

    ``issue_key`` is resolved from the ticket's link so the list is readable
    without a second call; rows for links that were since unlinked simply have
    ``issue_key: null``.
    """
    await _authorize_org_jira(db, user, org_id)

    conditions = [TrackerSyncEvent.org_id == org_id]
    if direction:
        conditions.append(TrackerSyncEvent.direction == direction)
    rows = (
        await db.execute(
            select(TrackerSyncEvent, TicketTrackerLink.issue_key)
            .outerjoin(
                TicketTrackerLink,
                TicketTrackerLink.ticket_id == TrackerSyncEvent.link_id,
            )
            .where(*conditions)
            .order_by(TrackerSyncEvent.created_at.desc())
            .limit(limit)
        )
    ).all()
    return {
        "events": [
            _sync_event_response(event, issue_key) for event, issue_key in rows
        ]
    }


# ── Custom templates ──


def _template_response(t: JiraTemplate) -> dict:
    return {
        "slug": t.slug,
        "name": t.name,
        "description": t.description,
        "sections": json.loads(t.sections),
        "version": t.version,
        "builtin": False,
        "archived_at": t.archived_at.isoformat() if t.archived_at else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "updated_at": t.updated_at.isoformat() if t.updated_at else None,
        "created_by_user_id": t.created_by_user_id,
        "updated_by_user_id": t.updated_by_user_id,
    }


def _builtin_template_response(t: Template) -> dict:
    return {
        "slug": t.slug,
        "name": t.name,
        "description": None,
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
        "version": None,
        "builtin": True,
        "archived_at": None,
        "created_at": None,
        "updated_at": None,
        "created_by_user_id": None,
        "updated_by_user_id": None,
    }


async def _get_template(
    org_id: str, slug: str, db: AsyncSession
) -> JiraTemplate | None:
    return (
        await db.execute(
            select(JiraTemplate).where(
                JiraTemplate.org_id == org_id,
                JiraTemplate.slug == slug,
            )
        )
    ).scalar_one_or_none()


def _template_validation_error(exc: TemplateValidationError) -> HTTPException:
    return HTTPException(
        422,
        {"error": "template_validation", "field": exc.field, "message": exc.message},
    )


def _template_conflict(message: str, current_version: int | None = None) -> HTTPException:
    detail: dict = {"error": "template_version_conflict", "message": message}
    if current_version is not None:
        detail["current_version"] = current_version
    return HTTPException(409, detail)


async def _active_template_count(org_id: str, db: AsyncSession) -> int:
    return len(
        (
            await db.execute(
                select(JiraTemplate.id).where(
                    JiraTemplate.org_id == org_id,
                    JiraTemplate.archived_at.is_(None),
                )
            )
        ).scalars().all()
    )


def _template_limit_error() -> HTTPException:
    return HTTPException(
        422,
        {
            "error": "template_limit",
            "message": (
                f"an org can have at most {MAX_TEMPLATES_PER_ORG} active custom "
                "templates; archive one first"
            ),
        },
    )


@org_router.get("/{org_id}/integrations/jira/templates")
async def list_jira_templates(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await _authorize_org_jira(db, user, org_id)
    customs = (
        await db.execute(
            select(JiraTemplate)
            .where(JiraTemplate.org_id == org_id)
            .order_by(JiraTemplate.created_at)
        )
    ).scalars().all()
    # An org custom template wins over a built-in that later gained the same
    # slug, so the shadowed built-in is not listed for this org.
    custom_slugs = {t.slug for t in customs}
    builtins = [
        _builtin_template_response(t)
        for t in list_templates()
        if t.slug not in custom_slugs
    ]
    return builtins + [_template_response(t) for t in customs]


@org_router.post("/{org_id}/integrations/jira/templates", status_code=201)
async def create_jira_template(
    org_id: str,
    body: JiraTemplateBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org, role = await _authorize_org_jira(db, user, org_id)
    try:
        slug = validate_slug(body.slug)
        name = validate_name(body.name)
        description = validate_description(body.description)
        sections = validate_sections(
            [s.model_dump(exclude_none=True) for s in body.sections]
        )
    except TemplateValidationError as exc:
        raise _template_validation_error(exc) from exc

    taken = (
        await db.execute(
            select(JiraTemplate.id).where(
                JiraTemplate.org_id == org_id, JiraTemplate.slug == slug
            )
        )
    ).first()
    if taken is not None:
        # Archived templates keep their slug reserved: links and versions pin
        # it, so reusing it would make old provenance point at new content.
        raise HTTPException(
            422,
            {
                "error": "template_slug_taken",
                "field": "slug",
                "message": f"template slug {slug!r} is already in use in this org",
            },
        )
    if await _active_template_count(org_id, db) >= MAX_TEMPLATES_PER_ORG:
        raise _template_limit_error()

    now = datetime.now(timezone.utc)
    template = JiraTemplate(
        id=f"jt_{uuid.uuid4().hex[:24]}",
        org_id=org_id,
        slug=slug,
        name=name,
        description=description,
        sections=json.dumps(sections),
        version=1,
        created_by_user_id=user.id,
        updated_by_user_id=user.id,
        created_at=now,
        updated_at=now,
    )
    db.add(template)
    db.add(
        JiraTemplateVersion(
            id=f"jtv_{uuid.uuid4().hex[:24]}",
            template_id=template.id,
            version=1,
            sections=json.dumps(sections),
            created_by_user_id=user.id,
            created_at=now,
        )
    )
    _org_audit(
        db,
        org=org,
        actor=user,
        actor_role=role,
        event_type="jira_template_created",
        target_type="jira_template",
        target_id=template.id,
        after=_template_response(template),
    )
    response = _template_response(template)
    try:
        await db.commit()
    except IntegrityError as exc:
        # A concurrent create took the slug between our check and the insert.
        await db.rollback()
        raise HTTPException(
            409,
            {
                "error": "template_slug_taken",
                "field": "slug",
                "message": f"template slug {slug!r} was just created by someone else",
            },
        ) from exc
    return response


@org_router.get("/{org_id}/integrations/jira/templates/{slug}")
async def get_jira_template(
    org_id: str,
    slug: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _authorize_org_jira(db, user, org_id)
    row = await _get_template(org_id, slug, db)
    if row is not None:
        return _template_response(row)
    builtin = get_template(slug)
    if builtin is not None:
        return _builtin_template_response(builtin)
    raise HTTPException(404, "Template not found")


def _builtin_readonly(action: str) -> HTTPException:
    return HTTPException(
        422,
        {"error": "builtin_readonly", "message": f"Built-in templates can't be {action}."},
    )


@org_router.put("/{org_id}/integrations/jira/templates/{slug}")
async def put_jira_template(
    org_id: str,
    slug: str,
    body: JiraTemplateBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org, role = await _authorize_org_jira(db, user, org_id)
    row = await _get_template(org_id, slug, db)
    if row is None:
        if is_builtin_slug(slug):
            raise _builtin_readonly("edited")
        raise HTTPException(404, "Template not found")
    if row.archived_at is not None:
        raise HTTPException(
            422,
            {"error": "template_archived", "message": "An archived template can't be edited."},
        )
    try:
        new_slug = (body.slug or "").strip()
        if new_slug != slug:
            # The slug is the pinned identity on existing links; renaming
            # would orphan them, so it's immutable.
            raise HTTPException(
                422,
                {
                    "error": "slug_immutable",
                    "field": "slug",
                    "message": "template slug is immutable",
                },
            )
        name = validate_name(body.name)
        description = validate_description(body.description)
        sections = validate_sections(
            [s.model_dump(exclude_none=True) for s in body.sections]
        )
    except TemplateValidationError as exc:
        raise _template_validation_error(exc) from exc

    expected = body.expected_version if body.expected_version is not None else row.version
    if expected != row.version:
        raise _template_conflict(
            f"This template was changed by someone else (now v{row.version}); "
            "reload it and re-apply your edits.",
            row.version,
        )

    before = _template_response(row)
    now = datetime.now(timezone.utc)
    new_version = expected + 1
    # Conditional bump: only the writer that still sees ``expected`` wins, so a
    # concurrent PUT can never silently overwrite another admin's edit.
    res = await db.execute(
        update(JiraTemplate)
        .where(
            JiraTemplate.id == row.id,
            JiraTemplate.version == expected,
            JiraTemplate.archived_at.is_(None),
        )
        .values(
            name=name,
            description=description,
            sections=json.dumps(sections),
            version=new_version,
            updated_by_user_id=user.id,
            updated_at=now,
        )
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:  # type: ignore[attr-defined]
        await db.rollback()
        raise _template_conflict(
            "This template was changed by someone else while saving; reload it "
            "and re-apply your edits."
        )
    # The UPDATE bypassed the ORM (synchronize_session=False), so ``row`` still
    # holds the pre-update values; reload it so nothing below reads a stale
    # version. The response is built from ``after`` either way.
    await db.refresh(row)
    db.add(
        JiraTemplateVersion(
            id=f"jtv_{uuid.uuid4().hex[:24]}",
            template_id=row.id,
            version=new_version,
            sections=json.dumps(sections),
            created_by_user_id=user.id,
            created_at=now,
        )
    )
    after = {
        **before,
        "name": name,
        "description": description,
        "sections": sections,
        "version": new_version,
        "updated_at": now.isoformat(),
        "updated_by_user_id": user.id,
    }
    _org_audit(
        db,
        org=org,
        actor=user,
        actor_role=role,
        event_type="jira_template_updated",
        target_type="jira_template",
        target_id=row.id,
        before=before,
        after=after,
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        # The (template_id, version) snapshot already exists: a concurrent PUT
        # bumped to the same version first.
        await db.rollback()
        raise _template_conflict(
            "This template was changed by someone else while saving; reload it "
            "and re-apply your edits."
        ) from exc
    return after


@org_router.delete("/{org_id}/integrations/jira/templates/{slug}", status_code=204)
async def delete_jira_template(
    org_id: str,
    slug: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    org, role = await _authorize_org_jira(db, user, org_id)
    row = await _get_template(org_id, slug, db)
    if row is None:
        if is_builtin_slug(slug):
            raise _builtin_readonly("archived")
        raise HTTPException(404, "Template not found")
    if row.archived_at is None:
        before = _template_response(row)
        now = datetime.now(timezone.utc)
        row.archived_at = now
        row.updated_by_user_id = user.id
        row.updated_at = now
        _org_audit(
            db,
            org=org,
            actor=user,
            actor_role=role,
            event_type="jira_template_archived",
            target_type="jira_template",
            target_id=row.id,
            before=before,
            after=_template_response(row),
        )
        await db.commit()


@org_router.post("/{org_id}/integrations/jira/templates/{slug}/unarchive")
async def unarchive_jira_template(
    org_id: str,
    slug: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    org, role = await _authorize_org_jira(db, user, org_id)
    row = await _get_template(org_id, slug, db)
    if row is None:
        if is_builtin_slug(slug):
            raise _builtin_readonly("unarchived")
        raise HTTPException(404, "Template not found")
    if row.archived_at is None:
        return _template_response(row)
    if await _active_template_count(org_id, db) >= MAX_TEMPLATES_PER_ORG:
        raise _template_limit_error()
    before = _template_response(row)
    now = datetime.now(timezone.utc)
    # Unarchiving restores the template as-is: same slug, same version, so
    # links pinned to it keep pointing at the content they were published with.
    row.archived_at = None
    row.updated_by_user_id = user.id
    row.updated_at = now
    _org_audit(
        db,
        org=org,
        actor=user,
        actor_role=role,
        event_type="jira_template_unarchived",
        target_type="jira_template",
        target_id=row.id,
        before=before,
        after=_template_response(row),
    )
    await db.commit()
    return _template_response(row)


@org_router.get("/{org_id}/integrations/jira/templates/{slug}/versions")
async def list_jira_template_versions(
    org_id: str,
    slug: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    await _authorize_org_jira(db, user, org_id)
    row = await _get_template(org_id, slug, db)
    if row is None:
        if is_builtin_slug(slug):
            raise HTTPException(404, "Built-in templates have no versions")
        raise HTTPException(404, "Template not found")
    versions = (
        await db.execute(
            select(JiraTemplateVersion)
            .where(JiraTemplateVersion.template_id == row.id)
            .order_by(JiraTemplateVersion.version.desc())
        )
    ).scalars().all()
    return [
        {
            "version": v.version,
            "sections": json.loads(v.sections),
            "created_by_user_id": v.created_by_user_id,
            "created_at": v.created_at.isoformat() if v.created_at else None,
        }
        for v in versions
    ]


@org_router.post("/{org_id}/integrations/jira/templates/{slug}/preview")
async def preview_jira_template(
    org_id: str,
    slug: str,
    body: JiraTemplatePreviewBody,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    await _authorize_org_jira(db, user, org_id)
    if body.sections is not None:
        # An unsaved draft from the editor: validate it exactly like a save,
        # then render it deterministically. Nothing is stored.
        try:
            sections = validate_sections(
                [s.model_dump(exclude_none=True) for s in body.sections]
            )
            name = validate_name(body.name or slug)
        except TemplateValidationError as exc:
            raise _template_validation_error(exc) from exc
        resolved = ResolvedTemplate(
            slug=slug,
            name=name,
            version=None,
            builtin=False,
            template=sections_to_template(slug, name, sections),
        )
    else:
        resolved = await _resolve_template(
            db, org_id, slug, strict=True, allow_archived=True
        )
    project = await db.get(Project, body.project_id)
    if project is None or project.org_id != org_id:
        raise HTTPException(404, "Project not found")
    mapping = await _get_mapping(project.id, org_id, db)
    if mapping is None:
        raise HTTPException(
            409,
            {"error": "jira_project_unmapped", "message": "This project is not mapped."},
        )
    ticket = await db.get(Ticket, body.ticket_id)
    if ticket is None or ticket.project_id != project.id:
        raise HTTPException(404, "Ticket not found")
    sources = await collect_sources(ticket, db)
    try:
        rendered = build_deterministic_sections(resolved.template, sources)
    except ContentTooLarge as exc:
        raise _content_too_large(exc.section) from exc
    return {
        "ticket_id": ticket.id,
        "project_id": project.id,
        "template": {
            "slug": resolved.slug,
            "name": resolved.name,
            "version": resolved.version,
        },
        "summary": sources.title,
        "sections": rendered,
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
    require_editor: bool = False,
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
        # GitHub #101 — in an org that restricts project visibility, writing
        # to Jira through the org's credential (preview / publish) needs edit
        # standing on the project, not just read standing.
        if require_editor:
            from sessionfs.server.auth.project_access import (
                project_edit_forbidden,
                project_restricts_visibility,
                user_can_edit_project,
            )

            if await project_restricts_visibility(
                db, project
            ) and not await user_can_edit_project(db, auth.user.id, project):
                raise project_edit_forbidden()
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


async def _resolve_template(
    db: AsyncSession,
    org_id: str,
    slug: str | None,
    *,
    strict: bool = False,
    allow_archived: bool = False,
) -> ResolvedTemplate:
    """Resolve a template slug (org-custom or built-in) to its current version.

    An org custom template wins over a built-in with the same slug (a built-in
    added later must not silently shadow a template an org already uses).

    * Inherited (non-strict) resolution — mapping override / connection
      default — resolves archived custom templates normally: archiving stops
      NEW selections, it never switches existing configuration. Only a slug
      that no longer exists at all falls back to ``DEFAULT_TEMPLATE``.
    * ``strict`` (a NEW selection) refuses an unknown slug with a 422, and an
      archived one too unless ``allow_archived`` (the caller is keeping a value
      it already had, e.g. re-saving a mapping or re-publishing a pinned link).
    """
    if slug is None:
        if strict:
            raise HTTPException(422, "unknown template: None")
        return builtin_resolved(DEFAULT_TEMPLATE)
    row = await _get_template(org_id, slug, db)
    if row is None:
        if is_builtin_slug(slug):
            return builtin_resolved(slug)
        if strict:
            raise HTTPException(422, f"unknown template: {slug}")
        return builtin_resolved(DEFAULT_TEMPLATE)
    if row.archived_at is not None and strict and not allow_archived:
        raise HTTPException(
            422,
            {
                "error": "template_archived",
                "message": (
                    f"template {slug!r} is archived and can't be newly selected"
                ),
            },
        )
    return ResolvedTemplate(
        slug=row.slug,
        name=row.name,
        version=row.version,
        builtin=False,
        template=sections_to_template(row.slug, row.name, json.loads(row.sections)),
    )


async def _resolve_ticket_template(
    db: AsyncSession,
    *,
    org_id: str,
    mapping: TrackerProjectMapping,
    conn: OrgTrackerConnection,
    link: TicketTrackerLink | None,
    override: str | None,
) -> ResolvedTemplate:
    """Inheritance: per-ticket override → mapping override → connection default.

    Neither echoing the inherited slug back nor re-choosing the template this
    ticket's link is pinned to is a new selection, so both resolve even when
    that template has since been archived (a published ticket keeps its
    template). Any other override is a new selection and must be selectable
    (built-in or active custom).
    """
    inherited = mapping.template or conn.default_template
    if override is None or override == inherited:
        return await _resolve_template(db, org_id, inherited)
    pinned = (
        link.template_slug
        if link is not None and link.status == "linked"
        else None
    )
    return await _resolve_template(
        db, org_id, override, strict=True, allow_archived=override == pinned
    )


def _ticket_url(dashboard_url: str, project_id: str, ticket_id: str) -> str:
    if not dashboard_url:
        return ""
    return f"{dashboard_url}/projects/{project_id}?tab=work&sub=tickets&ticket={ticket_id}"


def _template_label(resolved: ResolvedTemplate) -> str:
    if resolved.version is not None:
        return f"{resolved.name} v{resolved.version}"
    return resolved.name


def _footer_adf(resolved: ResolvedTemplate, ticket_url: str) -> list[dict]:
    """The provenance footer appended to every published description.

    Always ``Generated by SessionFS · template: <name> v<n>`` (built-ins have
    no version, so just ``<name>``). When the dashboard origin is configured
    it continues `` · source ticket <url>`` with the URL as an ADF link; a
    self-hosted install without one never links to the hosted dashboard.
    """
    content: list[dict] = [
        {
            "type": "text",
            "text": f"Generated by SessionFS · template: {_template_label(resolved)}",
        }
    ]
    if ticket_url:
        content.append({"type": "text", "text": " · source ticket "})
        content.append(
            {
                "type": "text",
                "text": ticket_url,
                "marks": [{"type": "link", "attrs": {"href": ticket_url}}],
            }
        )
    return [{"type": "paragraph", "content": content}]


async def _get_link(ticket_id: str, db: AsyncSession) -> TicketTrackerLink | None:
    return (
        await db.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket_id)
        )
    ).scalar_one_or_none()


def _can_send_to_jira(
    link: TicketTrackerLink,
    conn: OrgTrackerConnection,
    project: Project,
    *,
    can_edit: bool,
) -> bool:
    """Whether the comment composer may offer "send to Jira": an editor, a
    live link on the connection's own org and site (never a stale link), and
    outbound comment sync on."""
    return (
        can_edit
        and link.status == "linked"
        and bool(link.issue_key)
        and link.org_id == project.org_id
        and link.site_url == conn.site_url
        and conn.enabled
        and (conn.sync_comments or "off") in jira_sync.OUTBOUND_COMMENT_MODES
    )


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
    template_slug: str,
    template_version: int | None,
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
            template_slug=template_slug,
            template_version=template_version,
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
    template_slug: str,
    template_version: int | None,
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
            template_slug=template_slug,
            template_version=template_version,
            last_push_failed_at=None,
            last_push_failure=None,
        )
        .execution_options(synchronize_session=False)
    )
    return res.rowcount == 1  # type: ignore[attr-defined]


@router.get("/{project_id}/tickets/{ticket_id}/jira/templates")
async def list_ticket_jira_templates(
    project_id: str,
    ticket_id: str,
    auth: AuthContext = Depends(require_scope("tickets:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Templates a member may pick for this ticket in the promotion dialog.

    Gated exactly like preview/publish. Returns only the selectable set as
    ``{slug, name, version, builtin, archived}`` — never section definitions
    or guidance: the built-ins, the org's active custom templates and, if
    archived, this ticket's own pinned template (the only ``archived: true``
    entry). Also the inherited default and the link's pinned slug/version.
    """
    project, ticket, conn, mapping = await _authorize_ticket_jira(
        db, auth, ctx, project_id, ticket_id
    )
    org_id = project.org_id
    customs = (
        await db.execute(
            select(JiraTemplate)
            .where(JiraTemplate.org_id == org_id, JiraTemplate.archived_at.is_(None))
            .order_by(JiraTemplate.created_at)
        )
    ).scalars().all()
    all_custom_slugs = set(
        (
            await db.execute(
                select(JiraTemplate.slug).where(JiraTemplate.org_id == org_id)
            )
        ).scalars().all()
    )
    templates = [
        {"slug": t.slug, "name": t.name, "version": None, "builtin": True, "archived": False}
        for t in list_templates()
        if t.slug not in all_custom_slugs
    ] + [
        {
            "slug": t.slug,
            "name": t.name,
            "version": t.version,
            "builtin": False,
            "archived": False,
        }
        for t in customs
    ]
    inherited = await _resolve_template(
        db, org_id, mapping.template or conn.default_template
    )
    link = await _get_link(ticket.id, db)
    pinned = None
    if link is not None and link.status == "linked" and link.template_slug:
        pinned = {"slug": link.template_slug, "version": link.template_version}
        # A published ticket keeps its template even after it is archived: the
        # pinned archived template stays selectable for THIS ticket only.
        if not any(t["slug"] == link.template_slug for t in templates):
            row = await _get_template(org_id, link.template_slug, db)
            if row is not None and row.archived_at is not None:
                templates.append(
                    {
                        "slug": row.slug,
                        "name": row.name,
                        "version": row.version,
                        "builtin": False,
                        "archived": True,
                    }
                )
    return {
        "templates": templates,
        "inherited_default": {
            "slug": inherited.slug,
            "name": inherited.name,
            "version": inherited.version,
        },
        "pinned": pinned,
    }


@router.post("/{project_id}/tickets/{ticket_id}/jira/preview")
async def preview_jira(
    project_id: str,
    ticket_id: str,
    body: JiraPreviewBody | None = None,
    auth: AuthContext = Depends(require_scope("tickets:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project, ticket, conn, mapping = await _authorize_ticket_jira(
        db, auth, ctx, project_id, ticket_id, require_editor=True
    )
    # Inheritance: per-ticket override (strict) → mapping override → connection
    # default. The resolved slug/version is returned so the dialog can report
    # "Published with <name> v<n>; current is v<m>", and publish echoes it.
    link = await _get_link(ticket.id, db)
    resolved = await _resolve_ticket_template(
        db,
        org_id=project.org_id,
        mapping=mapping,
        conn=conn,
        link=link,
        override=body.template if body is not None else None,
    )
    try:
        preview = await build_preview(
            ticket=ticket,
            template=resolved.template,
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
    response["template"] = {
        "slug": resolved.slug,
        "name": resolved.name,
        "version": resolved.version,
    }
    if link is not None and link.status == "linked":
        _assert_link_current(link, project.org_id, conn.site_url)
        response["existing"] = {
            "issue_key": link.issue_key,
            "issue_url": link.issue_url,
            "last_known_status": link.last_known_status,
            "template_slug": link.template_slug,
            "template_version": link.template_version,
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
    request: Request,
    auth: AuthContext = Depends(require_scope("tickets:write")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    project, ticket, conn, mapping = await _authorize_ticket_jira(
        db, auth, ctx, project_id, ticket_id, require_editor=True
    )
    if not body.confirm:
        raise HTTPException(400, "Set confirm=true to publish to Jira")

    # A linked row that points at another org/site blocks publish before we
    # even look at the body — a stale link must never be re-targeted.
    existing_link = await _get_link(ticket_id, db)
    if existing_link is not None and existing_link.status == "linked":
        _assert_link_current(existing_link, project.org_id, conn.site_url)

    # Inheritance: per-ticket override (strict) → mapping override → connection
    # default. The resolved slug/version is stored on the link at publish.
    resolved = await _resolve_ticket_template(
        db,
        org_id=project.org_id,
        mapping=mapping,
        conn=conn,
        link=existing_link,
        override=body.template,
    )
    # The draft was previewed against a specific template version; if the
    # resolution moved since (template edited, mapping/default changed), refuse
    # rather than render or pin a version nobody previewed.
    if body.template is not None or "template_version" in body.model_fields_set:
        expected_slug = body.template if body.template is not None else resolved.slug
        if expected_slug != resolved.slug or body.template_version != resolved.version:
            raise HTTPException(
                409,
                {
                    "error": "template_changed",
                    "message": (
                        "The story template changed since this draft was "
                        f"previewed (now {_template_label(resolved)}); preview "
                        "again before publishing."
                    ),
                    "template": {
                        "slug": resolved.slug,
                        "name": resolved.name,
                        "version": resolved.version,
                    },
                },
            )

    # The client never chooses section rendering: mode is resolved from the
    # ticket's template by section key, and an unknown key is rejected before
    # any outbound call.
    mode_by_key = {s.key: s.mode for s in resolved.template.sections}
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

    # Append the provenance footer (the source-ticket link only when the
    # dashboard origin is configured) BEFORE the size guard so the footer
    # counts toward Jira's description cap.
    footer = _footer_adf(
        resolved,
        _ticket_url(
            dashboard_link_base(getattr(request.app.state, "config", None)),
            project.id,
            ticket.id,
        ),
    )
    adf["content"] = list(adf["content"]) + footer

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
            template_slug=resolved.slug,
            template_version=resolved.version,
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
            template_slug=resolved.slug,
            template_version=resolved.version,
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
        "template": {
            "slug": resolved.slug,
            "name": resolved.name,
            "version": resolved.version,
        },
    }


@router.get("/{project_id}/tickets/{ticket_id}/jira-sync-events")
async def get_ticket_jira_sync_events(
    project_id: str,
    ticket_id: str,
    auth: AuthContext = Depends(require_scope("tickets:read")),
    ctx: UserContext = Depends(get_user_context),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Sync ledger rows for one linked ticket, most-recent first.

    A project reader may see these (they're the same Jira activity the ticket's
    comments/timeline already surface); the org-wide admin list stays on
    ``/integrations/jira/sync-events``.
    """
    project, ticket, _conn, _mapping = await _authorize_ticket_jira(
        db, auth, ctx, project_id, ticket_id
    )
    link = await _get_link(ticket.id, db)
    if link is None:
        return {"events": []}
    rows = (
        await db.execute(
            select(TrackerSyncEvent)
            .where(TrackerSyncEvent.link_id == ticket.id)
            .order_by(TrackerSyncEvent.created_at.desc())
        )
    ).scalars().all()
    return {
        "events": [
            _sync_event_response(event, link.issue_key) for event in rows
        ]
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

    # Two-way sync: whether this caller may tick "send to Jira" on a comment
    # (editor standing or a service key, and outbound comment sync on).
    from sessionfs.server.auth.project_access import user_can_edit_project

    comment_sync_outbound = (conn.sync_comments or "off") in jira_sync.OUTBOUND_COMMENT_MODES
    can_edit = auth.key_kind == "service" or await user_can_edit_project(
        db, auth.user.id, project
    )
    can_send = _can_send_to_jira(link, conn, project, can_edit=can_edit)
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
        "comment_sync_outbound": comment_sync_outbound,
        "can_send_to_jira": can_send,
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
