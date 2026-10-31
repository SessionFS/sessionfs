"""Per-org delivery webhooks for GitHub and GitLab (Review Loop R3, track F2).

Two surfaces, both keyed by a per-org, per-provider secret:

- Admin (org owner/admin), under ``/api/v1/orgs``: ``rotate`` returns the
  plaintext secret exactly once plus the inbound URL; ``status`` shows only
  ``has_secret`` / ``secret_last4`` / ``url``.
- Public inbound, under ``/api/v1/webhooks``: GitHub verifies
  ``X-Hub-Signature-256`` (HMAC-SHA256 over the raw body), GitLab compares
  ``X-Gitlab-Token`` in constant time. The same safeguards as the Jira webhook
  apply: a 1 MiB streamed body cap, an idempotency ledger
  (``X-GitHub-Delivery`` / ``X-Gitlab-Event-UUID``), and fail-closed auth.

An unknown org answers the public endpoints with the *same* 401 body as a bad
signature — there is no existence oracle. A known org without a secret is 503.
This router is registered before ``projects.router`` in ``app.py`` for the same
reason as the Jira router (the project catch-all would otherwise swallow it).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    OrgAuditEvent,
    OrgDeliveryWebhook,
    OrgMember,
    Organization,
    User,
)
from sessionfs.server.llm_keys import decrypt_secret, encrypt_secret
from sessionfs.server.roles import has_minimum_role
from sessionfs.server.services import delivery_ingest, jira_sync

org_router = APIRouter(prefix="/api/v1/orgs", tags=["delivery-webhooks"])
webhook_router = APIRouter(prefix="/api/v1/webhooks", tags=["delivery-webhooks"])

_ORG_NOT_FOUND = "Organization not found"
_VALID_PROVIDERS = {"github", "gitlab"}

# Same 401 body for a bad signature and an unknown org: no existence oracle.
_INVALID_SIGNATURE = "Invalid webhook signature"


def _insufficient_role(current_role: str | None) -> dict:
    return {
        "error": "insufficient_role",
        "current_role": current_role or "none",
        "required_role": "admin",
        "message": "Managing delivery webhooks requires the admin role.",
    }


def _validate_provider(provider: str) -> None:
    if provider not in _VALID_PROVIDERS:
        raise HTTPException(422, f"Unsupported provider: {provider}")


async def _authorize_org_delivery(
    db: AsyncSession, user: User, org_id: str
) -> tuple[Organization, str]:
    """Return (org, caller_role) for an org owner/admin (delivery webhooks are
    on every plan, so there is no feature gate — see design/review-loop-r3)."""
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
) -> None:
    db.add(
        OrgAuditEvent(
            id=f"oae_{uuid.uuid4().hex[:24]}",
            org_id=org.id,
            org_name_snapshot=org.name,
            event_type=event_type,
            actor_user_id=actor.id,
            actor_email_snapshot=actor.email,
            actor_role_at_time=actor_role or "user",
            target_type=target_type,
            target_id=target_id,
        )
    )


async def _get_delivery_row(
    db: AsyncSession, org_id: str, provider: str
) -> OrgDeliveryWebhook | None:
    return (
        await db.execute(
            select(OrgDeliveryWebhook).where(
                OrgDeliveryWebhook.org_id == org_id,
                OrgDeliveryWebhook.provider == provider,
            )
        )
    ).scalars().first()


def _webhook_url(request: Request, provider: str, org_id: str) -> str:
    """Canonical public inbound URL for this org/provider. Prefers
    ``config.public_api_url`` and falls back to the request base URL."""
    config = getattr(request.app.state, "config", None)
    base = str(getattr(config, "public_api_url", "") or "").strip().rstrip("/")
    if not base:
        base = str(request.base_url).rstrip("/")
    return f"{base}/api/v1/webhooks/{provider}/{org_id}"


# ── Admin endpoints ──


@org_router.post("/{org_id}/delivery-webhooks/{provider}/rotate")
async def rotate_delivery_webhook(
    org_id: str,
    provider: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Generate (or rotate) the org's delivery-webhook secret for a provider.

    Returns the secret ONCE in the response; it is stored Fernet-encrypted and
    only ``secret_last4`` is shown again. Rotating invalidates every prior
    delivery for that (org, provider).
    """
    _validate_provider(provider)
    org, role = await _authorize_org_delivery(db, user, org_id)

    secret = jira_sync.generate_webhook_secret()
    row = await _get_delivery_row(db, org_id, provider)
    if row is None:
        row = OrgDeliveryWebhook(org_id=org_id, provider=provider, encrypted_secret="", secret_last4="")
        db.add(row)
    row.encrypted_secret = encrypt_secret(secret)
    row.secret_last4 = jira_sync.webhook_secret_last4(secret)
    row.created_by_user_id = user.id
    row.rotated_at = datetime.now(timezone.utc)

    _org_audit(
        db,
        org=org,
        actor=user,
        actor_role=role,
        event_type="delivery_webhook_secret_rotated",
        target_type="delivery_webhook",
        target_id=org_id,
    )
    await db.commit()
    return {
        "webhook_secret": secret,
        "webhook_secret_last4": row.secret_last4,
        "webhook_url": _webhook_url(request, provider, org_id),
    }


@org_router.get("/{org_id}/delivery-webhooks/{provider}/status")
async def delivery_webhook_status(
    org_id: str,
    provider: str,
    request: Request,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Read-only status: whether a secret exists, its last-4 hint, and the URL."""
    _validate_provider(provider)
    await _authorize_org_delivery(db, user, org_id)
    row = await _get_delivery_row(db, org_id, provider)
    return {
        "has_secret": bool(row is not None and row.encrypted_secret),
        "secret_last4": row.secret_last4 if row is not None else None,
        "webhook_url": _webhook_url(request, provider, org_id),
    }


# ── Public inbound webhook ──


async def _read_webhook_body(request: Request) -> bytes:
    """Stream the body, refusing (413) as soon as it passes the 1 MiB cap."""
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


def _verify(provider: str, secret: str, body: bytes, request: Request) -> bool:
    """Fail-closed signature/token check. Never raises."""
    if provider == "github":
        return jira_sync.verify_webhook_signature(
            secret, body, request.headers.get("x-hub-signature-256")
        )
    token = request.headers.get("x-gitlab-token")
    if not token:
        return False
    return hmac.compare_digest(secret.encode(), token.encode())


def _event_name(provider: str, payload: dict, request: Request) -> str:
    if provider == "github":
        return (request.headers.get("x-github-event") or "").strip()
    return str(payload.get("object_kind") or "")


def _idempotency_key(provider: str, body: bytes, request: Request) -> str:
    if provider == "github":
        key = request.headers.get("x-github-delivery")
    else:
        key = request.headers.get("x-gitlab-event-uuid")
    if key:
        return key[:64]
    # Providers always send these, but a deterministic fallback keeps a
    # header-less delivery idempotent too.
    return hashlib.sha256(body).hexdigest()


async def _inbound_webhook(
    db: AsyncSession, *, org_id: str, provider: str, request: Request
) -> dict:
    # Unknown org, org without a secret, and a bad signature all return the
    # same 401 so the endpoint is not an org-existence oracle. The body is
    # read (and size-capped) first so an oversized request gets the same 413
    # whether or not the org exists or has a secret.
    body = await _read_webhook_body(request)
    org = await db.get(Organization, org_id)
    if org is None:
        raise HTTPException(401, _INVALID_SIGNATURE)

    row = await _get_delivery_row(db, org_id, provider)
    secret = None
    if row is not None and row.encrypted_secret:
        try:
            secret = decrypt_secret(row.encrypted_secret)
        except Exception:
            secret = None
    if not secret:
        raise HTTPException(401, _INVALID_SIGNATURE)

    if not _verify(provider, secret, body, request):
        raise HTTPException(401, _INVALID_SIGNATURE)

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(400, "Invalid JSON body")
    if not isinstance(payload, dict):
        raise HTTPException(400, "Invalid webhook body")

    event = _event_name(provider, payload, request)
    idempotency_key = _idempotency_key(provider, body, request)
    if not await delivery_ingest.record_delivery_event(
        db, provider=provider, org_id=org_id, idempotency_key=idempotency_key
    ):
        return {"status": "duplicate"}

    result = await delivery_ingest.ingest_event(
        db, provider=provider, org_id=org_id, event=event, payload=payload
    )
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent delivery of the same event won the ledger slot.
        await db.rollback()
        return {"status": "duplicate"}
    return result


@webhook_router.post("/github/{org_id}")
async def github_inbound_webhook(
    org_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _inbound_webhook(db, org_id=org_id, provider="github", request=request)


@webhook_router.post("/gitlab/{org_id}")
async def gitlab_inbound_webhook(
    org_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> dict:
    return await _inbound_webhook(db, org_id=org_id, provider="gitlab", request=request)
