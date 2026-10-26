"""LLM key resolution for the session helper.

Piece 2 of the session helper: an org admin can store one org-wide LLM key
(``org_llm_settings``, migration 084) that any active member falls back to
when they have no personal judge settings. ``resolve_helper_llm`` is the
single place that turns a caller + org into a usable key, in the order the
design specifies:

1. the caller's own ``UserJudgeSettings`` (exactly as ``routes/summaries.py``
   resolves them today);
2. the org-wide key, when enabled and the caller is an active member of that
   org;
3. ``None`` (the caller then raises ``helper_key_required``).

``base_url`` (user or org) is re-validated by the SSRF guard here, so a
hostname that started resolving to an internal address after it was saved is
never handed back. The guard is also installed into
``sessionfs.judge.providers`` for the actual call, so this is defence in
depth rather than the only check.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import OrgLlmSettings, OrgMember, User, UserJudgeSettings
from sessionfs.server.llm_keys import decrypt_llm_key
from sessionfs.server.llm_url_guard import SsrfError, guard_llm_base_url
from sessionfs.server.tier_gate import resolve_org_plan_tier
from sessionfs.server.tiers import get_features_for_tier

_FEATURE = "session_helper"


@dataclass
class ResolvedLLM:
    provider: str
    model: str
    api_key: str = field(repr=False)
    base_url: str | None
    key_source: Literal["user", "org"]
    # The org whose key this turn uses (``None`` for a user key). Needed to
    # record the org on messages for the monthly token budget.
    key_org_id: str | None = None


async def _base_url_ok(base_url: str | None) -> bool:
    """True when the stored base_url (if any) still passes the SSRF guard."""
    if not base_url:
        return True
    try:
        await guard_llm_base_url(base_url)
    except SsrfError:
        return False
    return True


async def resolve_helper_llm(
    user: User,
    org_id: str,
    db: AsyncSession,
    *,
    skip_user_key: bool = False,
) -> ResolvedLLM | None:
    """Resolve the LLM key for a helper turn, or None when there is none.

    ``org_id`` is the conversation's scope org (explicit, because a user in
    several orgs uses the org that owns the conversation, not their default).

    ``skip_user_key`` omits step 1 (the caller's personal judge settings) and
    falls straight through to the org key. Service-key callers pass this so a
    minted key never spends its minter's personal LLM key.
    """
    # 1. The caller's own judge settings win — unless the caller is a service
    # key, whose minter's personal key must not be charged.
    if not skip_user_key:
        judge = (
            await db.execute(
                select(UserJudgeSettings).where(UserJudgeSettings.user_id == user.id)
            )
        ).scalar_one_or_none()
        if judge is not None and judge.encrypted_api_key:
            try:
                api_key = decrypt_llm_key(judge.encrypted_api_key)
            except Exception:
                api_key = ""
            if api_key and await _base_url_ok(judge.base_url):
                return ResolvedLLM(
                    provider=judge.provider,
                    model=judge.model,
                    api_key=api_key,
                    base_url=judge.base_url,
                    key_source="user",
                )

    # 2. The org-wide key, only when enabled and the caller is an active member.
    if not org_id or not user.is_active:
        return None
    member = (
        await db.execute(
            select(OrgMember.id).where(
                OrgMember.org_id == org_id,
                OrgMember.user_id == user.id,
            )
        )
    ).first()
    if member is None:
        return None
    # The org key only exists while the org's plan includes the helper; after
    # a downgrade the stored row stays but must not resolve.
    plan = await resolve_org_plan_tier(org_id, db)
    if _FEATURE not in get_features_for_tier(plan):
        return None
    org_settings = (
        await db.execute(
            select(OrgLlmSettings).where(OrgLlmSettings.org_id == org_id)
        )
    ).scalar_one_or_none()
    if org_settings is None or not org_settings.enabled or not org_settings.encrypted_api_key:
        return None
    try:
        api_key = decrypt_llm_key(org_settings.encrypted_api_key)
    except Exception:
        return None
    if not api_key or not await _base_url_ok(org_settings.base_url):
        return None
    return ResolvedLLM(
        provider=org_settings.provider,
        model=org_settings.model,
        api_key=api_key,
        base_url=org_settings.base_url,
        key_source="org",
        key_org_id=org_id,
    )
