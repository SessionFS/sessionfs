"""Session-helper settings API.

``GET/PUT /api/v1/settings/helper`` manages the caller's personal helper model
settings and custom instructions, and reports an effective-settings preview for
their current key plus their own monthly token usage. Both need the
``session_helper`` feature on the caller's effective plan (the check
``/helper/status`` and turns use for a personal scope).

``GET/PUT /api/v1/orgs/{org_id}/helper-settings`` manages the org's helper
settings, ceilings, instructions and monthly token budget. Owners/admins read
and write everything (including a per-member usage summary); members read only
the effective values and their own usage. PUT needs the org's plan to include
``session_helper`` (Enterprise), like the org helper key.

Configuration is stored in ``helper_configs``, never on the key rows: saving
it creates no key row and deleting a key never touches it. PUT merges: an
omitted field keeps its value, an explicit ``null`` clears it, and inside
``settings`` each key merges the same way.

Instructions are control-stripped, refused if they contain the prompt's
delimiter tags, DLP-redacted, then length-checked; every mutation is audited
with only the length + sha256 of the instruction text — never the text itself.
Usage figures come only from the ``helper_usage`` ledger.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import get_current_user
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    HelperConfig,
    OrgAuditEvent,
    OrgMember,
    Organization,
    User,
)
from sessionfs.server.dlp import get_org_dlp_policy
from sessionfs.server.helper.keys import ResolvedLLM, resolve_helper_llm
from sessionfs.server.helper.redaction import RedactionError, redact, union_dlp_policies
from sessionfs.server.helper.settings import (
    MAX_ORG_INSTRUCTIONS_CHARS,
    MAX_PERSONAL_INSTRUCTIONS_CHARS,
    HelperModelSettings,
    HelperSettingsPatch,
    OrgHelperSettings,
    OrgHelperSettingsPatch,
    contains_delimiter,
    effective_settings,
    load_org_config,
    load_user_config,
    loads_settings,
    merge_settings,
    org_effective,
    org_settings_view,
    user_settings_view,
)
from sessionfs.server.helper.usage import org_monthly_usage_by_member, user_monthly_usage
from sessionfs.server.roles import has_minimum_role
from sessionfs.server.routes.helper import _FEATURE, _gate, _sanitize_controls
from sessionfs.server.routes.org_llm_settings import _require_enterprise
from sessionfs.server.tier_gate import get_user_org_membership, resolve_org_plan_tier
from sessionfs.server.tiers import get_features_for_tier

user_router = APIRouter(prefix="/api/v1/settings", tags=["helper-settings"])
org_router = APIRouter(prefix="/api/v1/orgs", tags=["helper-settings"])

_ORG_NOT_FOUND = "Organization not found"

# Raw request cap (the stored limit is checked after redaction); stops a huge
# body from reaching the DLP scanner.
_MAX_RAW_INSTRUCTIONS_CHARS = 20_000

Budget = Annotated[int, Field(strict=True, ge=0)]
RawInstructions = Annotated[str, Field(max_length=_MAX_RAW_INSTRUCTIONS_CHARS)]


class UserHelperSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings: HelperSettingsPatch | None = None
    instructions: RawInstructions | None = None


class OrgHelperSettingsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    settings: OrgHelperSettingsPatch | None = None
    instructions: RawInstructions | None = None
    monthly_token_budget: Budget | None = None


def _sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def _instructions_audit(raw: str | None) -> dict | None:
    """The audit-safe view of an instruction: length + hash, never the text."""
    if raw is None:
        return None
    return {"length": len(raw), "sha256": _sha256_hex(raw)}


def _instructions_error(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=422, detail={"code": code, "message": message})


def _prepare_instructions(
    raw: str | None, *, max_chars: int, dlp_policy: dict | None
) -> str | None:
    """Strip controls, refuse delimiter tags, DLP-redact, then length-check."""
    if raw is None:
        return None
    stripped = _sanitize_controls(raw)
    if contains_delimiter(stripped):
        raise _instructions_error(
            "instructions_invalid",
            "Custom instructions cannot contain <org-instructions> or "
            "<personal-instructions> tags.",
        )
    try:
        redacted = redact(stripped, dlp_policy)
    except RedactionError:
        raise _instructions_error(
            "instructions_redaction_failed",
            "The instructions could not be checked for sensitive data. "
            "Simplify the text and try again.",
        ) from None
    if len(redacted) > max_chars:
        raise _instructions_error(
            "instructions_too_long",
            f"Custom instructions must be at most {max_chars} characters "
            "(after sensitive data is redacted).",
        )
    return redacted if redacted.strip() else None


async def _personal_dlp_policy(db: AsyncSession, user: User) -> dict | None:
    """Strictest union of every org the caller is a member of (personal scope)."""
    org_ids = (
        await db.execute(select(OrgMember.org_id).where(OrgMember.user_id == user.id))
    ).scalars().all()
    if not org_ids:
        return None
    orgs = (
        await db.execute(select(Organization).where(Organization.id.in_(org_ids)))
    ).scalars().all()
    return union_dlp_policies([get_org_dlp_policy(org) for org in orgs])


async def _effective_for_user(
    db: AsyncSession, user: User, user_config: HelperConfig | None
) -> tuple[ResolvedLLM | None, HelperModelSettings | None]:
    """The caller's effective helper settings under their current key."""
    membership = await get_user_org_membership(user.id, db)
    key_org_id = membership.org_id if membership is not None else None
    resolved = await resolve_helper_llm(user, key_org_id or "", db)
    if resolved is None:
        return None, None
    org_config = (
        await load_org_config(db, resolved.key_org_id)
        if resolved.key_source == "org"
        else None
    )
    return resolved, effective_settings(resolved, org_config, user_config)


async def _org_member_role(
    db: AsyncSession, user: User, org_id: str
) -> tuple[Organization, str]:
    """Authorize membership; unknown org / non-member is 404 (don't leak)."""
    org = await db.get(Organization, org_id)
    role = (
        await db.execute(
            select(OrgMember.role).where(
                OrgMember.org_id == org_id, OrgMember.user_id == user.id
            )
        )
    ).scalars().first()
    if org is None or role is None:
        raise HTTPException(404, _ORG_NOT_FOUND)
    return org, role


def _insufficient_role(current_role: str) -> dict:
    return {
        "error": "insufficient_role",
        "current_role": current_role,
        "required_role": "admin",
        "message": "Managing org helper settings requires the admin or owner role.",
    }


def _usage_own(per_member: list[dict], user_id: str) -> dict:
    own = next((m for m in per_member if m["user_id"] == user_id), None)
    return {
        "input_tokens": own["input_tokens"] if own else 0,
        "output_tokens": own["output_tokens"] if own else 0,
        "total_tokens": own["total_tokens"] if own else 0,
    }


def _usage_total(per_member: list[dict]) -> dict:
    input_tokens = sum(m["input_tokens"] for m in per_member)
    output_tokens = sum(m["output_tokens"] for m in per_member)
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
    }


def _audit_event(
    *,
    user: User,
    org: Organization | None,
    role: str | None,
    event_type: str,
    before: dict | None,
    after: dict,
) -> OrgAuditEvent:
    return OrgAuditEvent(
        id=f"oae_{secrets.token_hex(12)}",
        org_id=org.id if org is not None else None,
        org_name_snapshot=org.name if org is not None else "",
        event_type=event_type,
        actor_user_id=user.id,
        actor_email_snapshot=user.email,
        actor_role_at_time=role,
        target_type="settings" if org is not None else "user",
        target_id=org.id if org is not None else user.id,
        target_email_snapshot=None if org is not None else user.email,
        before=json.dumps(before) if before is not None else None,
        after=json.dumps(after),
    )


def _config_snapshot(config: HelperConfig | None) -> dict | None:
    if config is None:
        return None
    snapshot: dict = {
        "settings": loads_settings(config.settings),
        "instructions": _instructions_audit(config.instructions),
    }
    if config.scope == "org":
        snapshot["monthly_token_budget"] = config.monthly_token_budget
    return snapshot


# ---------------------------------------------------------------------------
# Personal helper settings
# ---------------------------------------------------------------------------


@user_router.get("/helper")
async def get_user_helper_settings(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """The caller's personal helper settings, instructions, preview and usage."""
    await _gate(user, db, None)
    return await _user_response(db, user)


async def _user_response(db: AsyncSession, user: User) -> dict:
    config = await load_user_config(db, user.id)
    _, effective = await _effective_for_user(db, user, config)
    return {
        "settings": user_settings_view(config).model_dump(),
        "instructions": config.instructions if config else None,
        "effective_settings": effective.model_dump() if effective else None,
        "usage": await user_monthly_usage(db, user.id),
    }


@user_router.put("/helper")
async def put_user_helper_settings(
    body: UserHelperSettingsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Merge the caller's personal helper settings and instructions."""
    await _gate(user, db, None)
    fields = body.model_fields_set

    instructions: str | None = None
    if "instructions" in fields:
        instructions = _prepare_instructions(
            body.instructions,
            max_chars=MAX_PERSONAL_INSTRUCTIONS_CHARS,
            dlp_policy=await _personal_dlp_policy(db, user),
        )

    config = await load_user_config(db, user.id)
    before = _config_snapshot(config)
    if config is None:
        config = HelperConfig(
            id=f"hcfg_{secrets.token_hex(12)}", scope="user", user_id=user.id
        )
        db.add(config)

    if "settings" in fields:
        if body.settings is None:
            config.settings = None
        else:
            merged = merge_settings(loads_settings(config.settings), body.settings)
            HelperModelSettings.model_validate(merged)
            config.settings = json.dumps(merged) if merged else None
    if "instructions" in fields:
        config.instructions = instructions
    config.updated_by_user_id = user.id

    db.add(
        _audit_event(
            user=user,
            org=None,
            role=None,
            event_type="user_helper_settings_updated",
            before=before,
            after=_config_snapshot(config) or {},
        )
    )
    await db.commit()
    return await _user_response(db, user)


# ---------------------------------------------------------------------------
# Org helper settings
# ---------------------------------------------------------------------------


@org_router.get("/{org_id}/helper-settings")
async def get_org_helper_settings(
    org_id: str,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Org helper settings: admins read all + per-member usage; members get the
    effective values and their own usage only. Readable on any plan (like the
    org helper key) so a downgraded org can still see what is stored."""
    _, role = await _org_member_role(db, user, org_id)
    return await _org_response(db, user, org_id, role)


async def _org_response(db: AsyncSession, user: User, org_id: str, role: str) -> dict:
    config = await load_org_config(db, org_id)
    per_member = await org_monthly_usage_by_member(db, org_id)
    plan = await resolve_org_plan_tier(org_id, db)

    response: dict = {
        "role": role,
        "plan_includes_helper": _FEATURE in get_features_for_tier(plan),
        "effective_settings": org_effective(config).model_dump(),
        "has_instructions": bool(config and config.instructions),
    }
    if has_minimum_role(role, "admin"):
        response.update(
            settings=org_settings_view(config).model_dump(),
            instructions=config.instructions if config else None,
            monthly_token_budget=config.monthly_token_budget if config else None,
            usage={**_usage_total(per_member), "per_member": per_member},
        )
    else:
        response["usage"] = _usage_own(per_member, user.id)
        response.update(_member_budget_status(config, per_member))
    return response


def _member_budget_status(config: HelperConfig | None, per_member: list[dict]) -> dict:
    """Budget status a member may see: reached or not, and a whole percentage
    capped at 100 — never the budget amount or anyone else's usage. Matches
    the turn-time check (``used >= budget`` refuses with 429)."""
    budget = config.monthly_token_budget if config is not None else None
    if budget is None:
        return {"budget_reached": False, "budget_used_pct": None}
    used = _usage_total(per_member)["total_tokens"]
    # Floor, so the bar only shows 100 once turns are actually refused.
    pct = 100 if budget <= 0 else min(100, used * 100 // budget)
    return {"budget_reached": used >= budget, "budget_used_pct": pct}


@org_router.put("/{org_id}/helper-settings")
async def put_org_helper_settings(
    org_id: str,
    body: OrgHelperSettingsRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Merge org helper settings, ceilings, instructions and budget. Admin only;
    the org's plan must include the session helper."""
    org, role = await _org_member_role(db, user, org_id)
    if not has_minimum_role(role, "admin"):
        raise HTTPException(status_code=403, detail=_insufficient_role(role))
    plan = await resolve_org_plan_tier(org_id, db)
    if _FEATURE not in get_features_for_tier(plan):
        _require_enterprise(plan)
    fields = body.model_fields_set

    instructions: str | None = None
    if "instructions" in fields:
        instructions = _prepare_instructions(
            body.instructions,
            max_chars=MAX_ORG_INSTRUCTIONS_CHARS,
            dlp_policy=get_org_dlp_policy(org),
        )

    config = await load_org_config(db, org_id)
    before = _config_snapshot(config)
    if config is None:
        config = HelperConfig(
            id=f"hcfg_{secrets.token_hex(12)}", scope="org", org_id=org_id
        )
        db.add(config)

    if "settings" in fields:
        if body.settings is None:
            config.settings = None
        else:
            merged = merge_settings(loads_settings(config.settings), body.settings)
            OrgHelperSettings.model_validate(merged)
            config.settings = json.dumps(merged) if merged else None
    if "instructions" in fields:
        config.instructions = instructions
    if "monthly_token_budget" in fields:
        config.monthly_token_budget = body.monthly_token_budget
    config.updated_by_user_id = user.id

    db.add(
        _audit_event(
            user=user,
            org=org,
            role=role,
            event_type="org_helper_settings_updated",
            before=before,
            after=_config_snapshot(config) or {},
        )
    )
    await db.commit()
    return await _org_response(db, user, org_id, role)
