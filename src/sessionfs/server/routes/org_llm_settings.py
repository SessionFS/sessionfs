"""Org-wide LLM key for the session helper (migration 084).

GET / PUT / DELETE ``/api/v1/orgs/{org_id}/llm-settings`` — org owner/admin
only, Enterprise only (feature ``session_helper``). The stored API key is
Fernet-encrypted with the same derivation as ``UserJudgeSettings``
(``server/llm_keys.py``) and is never returned; ``GET`` reports
``has_api_key`` plus a last-4 ``key_hint`` instead. Every mutation writes an
append-only ``OrgAuditEvent`` (``org_llm_settings_updated`` /
``org_llm_settings_deleted``) whose before/after payloads carry
provider / model / base_url / enabled / has_api_key — never the key.

Mirroring the org skills policy, an owner/admin of an org that already has a
row may still read it and may delete or disable it after the org drops below
Enterprise (the stored key stops resolving, so nothing to tighten); enabling
or changing provider / key still requires the feature.
"""

from __future__ import annotations

import json
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    OrgAuditEvent,
    OrgLlmSettings,
    OrgMember,
    Organization,
    User,
)
from sessionfs.server.llm_keys import decrypt_llm_key, encrypt_llm_key
from sessionfs.server.roles import has_minimum_role
from sessionfs.server.routes.llm_providers import validate_provider_or_422
from sessionfs.server.routes.settings import _check_llm_base_url_or_400
from sessionfs.server.tier_gate import resolve_org_plan_tier
from sessionfs.server.tiers import get_features_for_tier, get_minimum_tier_for_feature

router = APIRouter(prefix="/api/v1/orgs", tags=["org-llm-settings"])

_FEATURE = "session_helper"
_ORG_NOT_FOUND = "Organization not found"
_MIN_KEY_LENGTH = 8
_KEY_HINT_MIN_LENGTH = 12


class OrgLlmSettingsRequest(BaseModel):
    provider: str = Field(..., min_length=1, max_length=32)
    model: str = Field(..., min_length=1, max_length=128)
    api_key: str = Field("", max_length=4096)
    base_url: str | None = Field(None, max_length=500)
    enabled: bool = True


class OrgLlmSettingsResponse(BaseModel):
    provider: str
    model: str
    has_api_key: bool
    key_hint: str | None
    base_url: str | None
    enabled: bool
    plan_includes_helper: bool


def _insufficient_role(current_role: str) -> dict:
    return {
        "error": "insufficient_role",
        "current_role": current_role,
        "required_role": "admin",
        "message": "Managing the org-wide helper key requires the admin or owner role.",
    }


async def _authorize_role(
    db: AsyncSession, user: User, org_id: str
) -> tuple[Organization, str]:
    """Authorize an org owner/admin, without the plan-tier check.

    Order matches the org-scoped auth pattern (``_authorize_org_personas``):
    an unknown org or a non-member is 404 (don't leak org existence); a
    member who is not an admin is 403 ``insufficient_role``.
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
    return org, role


def _require_enterprise(plan) -> None:
    """403 ``upgrade_required`` when the resolved plan lacks the helper."""
    required = get_minimum_tier_for_feature(_FEATURE)
    raise HTTPException(
        status_code=403,
        detail={
            "error": "upgrade_required",
            "feature": _FEATURE,
            "plan_scope": "org",
            "current_tier": plan.value,
            "required_tier": required,
            "upgrade_url": "https://sessionfs.dev/pricing",
            "message": f"This feature requires the org's plan to be {required} or above.",
        },
    )


def _validated_api_key(raw: str) -> tuple[str, bool]:
    """Return ``(stripped_key, provided)`` after validating the raw key.

    An empty string means "no key supplied" (keep the stored key). Anything
    else must be a real key of at least ``_MIN_KEY_LENGTH`` chars after
    stripping; a whitespace-only value is rejected rather than silently
    treated as "not provided".
    """
    stripped = raw.strip()
    if stripped == "":
        if raw == "":
            return "", False
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_api_key",
                "message": "The API key must not be blank.",
            },
        )
    if len(stripped) < _MIN_KEY_LENGTH:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "invalid_api_key",
                "message": "The API key must be at least 8 characters.",
            },
        )
    return stripped, True


def _key_hint(encrypted: str) -> str | None:
    """Last 4 chars of the decrypted key, only when the key is long enough."""
    if not encrypted:
        return None
    try:
        plain = decrypt_llm_key(encrypted)
    except Exception:
        return None
    if len(plain) < _KEY_HINT_MIN_LENGTH:
        return None
    return plain[-4:]


def _snapshot(row: OrgLlmSettings | None, *, key_changed: bool = False) -> dict | None:
    """The audit-safe view of a row — every field except the key."""
    if row is None:
        return None
    snap = {
        "provider": row.provider,
        "model": row.model,
        "base_url": row.base_url,
        "enabled": row.enabled,
        "has_api_key": bool(row.encrypted_api_key),
    }
    if key_changed:
        snap["key_changed"] = True
    return snap


def _audit(
    db: AsyncSession,
    *,
    org: Organization,
    event_type: str,
    actor: User,
    actor_role: str,
    before: dict | None,
    after: dict | None,
) -> None:
    db.add(
        OrgAuditEvent(
            id=f"oae_{secrets.token_hex(12)}",
            org_id=org.id,
            org_name_snapshot=org.name,
            event_type=event_type,
            actor_user_id=actor.id,
            actor_email_snapshot=actor.email,
            actor_role_at_time=actor_role,
            target_type="settings",
            target_id=org.id,
            before=json.dumps(before) if before is not None else None,
            after=json.dumps(after) if after is not None else None,
        )
    )


@router.get("/{org_id}/llm-settings", response_model=OrgLlmSettingsResponse)
async def get_org_llm_settings(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrgLlmSettingsResponse:
    """Return the org's helper-key config (never the key itself).

    Owner/admin only; works regardless of the plan tier so a downgraded org
    can still see its stored key and disable or remove it.
    """
    await _authorize_role(db, user, org_id)
    plan = await resolve_org_plan_tier(org_id, db)
    plan_includes_helper = _FEATURE in get_features_for_tier(plan)
    row = (
        await db.execute(
            select(OrgLlmSettings).where(OrgLlmSettings.org_id == org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        return OrgLlmSettingsResponse(
            provider="", model="", has_api_key=False, key_hint=None,
            base_url=None, enabled=True,
            plan_includes_helper=plan_includes_helper,
        )
    return OrgLlmSettingsResponse(
        provider=row.provider,
        model=row.model,
        has_api_key=bool(row.encrypted_api_key),
        key_hint=_key_hint(row.encrypted_api_key),
        base_url=row.base_url,
        enabled=row.enabled,
        plan_includes_helper=plan_includes_helper,
    )


@router.put("/{org_id}/llm-settings", response_model=OrgLlmSettingsResponse)
async def put_org_llm_settings(
    org_id: str,
    body: OrgLlmSettingsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> OrgLlmSettingsResponse:
    """Create or replace the org helper key. Admin only.

    An omitted/empty ``api_key`` keeps the stored key (or stores no key on
    first save, letting the admin pre-configure provider/model/base_url).
    Below Enterprise only a pure disable of an existing row is allowed.
    """
    org, role = await _authorize_role(db, user, org_id)

    validate_provider_or_422(body.provider, body.base_url)

    row = (
        await db.execute(
            select(OrgLlmSettings).where(OrgLlmSettings.org_id == org_id)
        )
    ).scalar_one_or_none()

    # Disabling must keep working when the stored endpoint has since started
    # failing the guard (resolution refuses it anyway), so an unchanged
    # base_url on a disable isn't re-checked.
    if not (
        row is not None and body.enabled is False and body.base_url == row.base_url
    ):
        body.base_url = await _check_llm_base_url_or_400(body.base_url)

    plan = await resolve_org_plan_tier(org_id, db)
    plan_includes_helper = _FEATURE in get_features_for_tier(plan)

    config_changed = (
        row is None
        or row.provider != body.provider
        or row.model != body.model
        or row.base_url != body.base_url
    )
    key_supplied = bool(body.api_key.strip())
    disable_only = (
        row is not None
        and body.enabled is False
        and not key_supplied
        and not config_changed
    )
    if not disable_only and not plan_includes_helper:
        _require_enterprise(plan)

    stripped_key, key_provided = _validated_api_key(body.api_key)

    # A stored key may only ever be sent to the endpoint it was saved for.
    # Changing the provider OR the base_url without re-entering the key would
    # let an admin who cannot read the key point it at a host they control.
    if (
        row is not None
        and row.encrypted_api_key
        and (row.provider != body.provider or (row.base_url or None) != (body.base_url or None))
        and not key_provided
    ):
        raise HTTPException(
            status_code=422,
            detail={
                "code": "api_key_required",
                "message": "Changing the provider or endpoint URL requires a new API key.",
            },
        )

    before = _snapshot(row)
    if row is None:
        row = OrgLlmSettings(org_id=org_id)
        db.add(row)

    row.provider = body.provider
    row.model = body.model
    if key_provided:
        row.encrypted_api_key = encrypt_llm_key(stripped_key)
    elif row.encrypted_api_key is None:
        row.encrypted_api_key = ""  # first save without a key
    row.base_url = body.base_url
    row.enabled = body.enabled
    row.updated_by_user_id = user.id
    row.updated_at = datetime.now(timezone.utc)

    await db.flush()
    _audit(
        db,
        org=org,
        event_type="org_llm_settings_updated",
        actor=user,
        actor_role=role,
        before=before,
        after=_snapshot(row, key_changed=key_provided),
    )
    await db.commit()

    return OrgLlmSettingsResponse(
        provider=row.provider,
        model=row.model,
        has_api_key=bool(row.encrypted_api_key),
        key_hint=_key_hint(row.encrypted_api_key),
        base_url=row.base_url,
        enabled=row.enabled,
        plan_includes_helper=plan_includes_helper,
    )


@router.delete("/{org_id}/llm-settings")
async def delete_org_llm_settings(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Delete the org helper key. Admin only, any plan tier."""
    org, role = await _authorize_role(db, user, org_id)
    row = (
        await db.execute(
            select(OrgLlmSettings).where(OrgLlmSettings.org_id == org_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "No org LLM settings configured")
    _audit(
        db,
        org=org,
        event_type="org_llm_settings_deleted",
        actor=user,
        actor_role=role,
        before=_snapshot(row),
        after=None,
    )
    await db.execute(delete(OrgLlmSettings).where(OrgLlmSettings.org_id == org_id))
    await db.commit()
    return {"deleted": True}
