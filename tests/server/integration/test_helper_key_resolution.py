"""resolve_helper_llm — the key precedence for the session helper.

User judge settings win over the org-wide key; a disabled org key is ignored;
a non-member cannot use the org key; and with neither, resolution is None.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    OrgLlmSettings,
    OrgMember,
    Organization,
    User,
    UserJudgeSettings,
)
from sessionfs.server.helper.keys import ResolvedLLM, resolve_helper_llm
from sessionfs.server.llm_keys import encrypt_llm_key


async def _make_user(db: AsyncSession, name: str) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier="team",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_org(db: AsyncSession) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Test Org",
        slug=f"test-{uuid.uuid4().hex[:6]}",
        tier="enterprise",
        seats_limit=10,
    )
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


async def _member(db: AsyncSession, org_id: str, user_id: str, role: str = "member") -> None:
    db.add(OrgMember(org_id=org_id, user_id=user_id, role=role))
    await db.commit()


async def _set_judge(db: AsyncSession, user_id: str, api_key: str) -> None:
    db.add(
        UserJudgeSettings(
            user_id=user_id,
            provider="anthropic",
            model="claude-opus",
            encrypted_api_key=encrypt_llm_key(api_key),
            base_url=None,
        )
    )
    await db.commit()


async def _set_org_key(
    db: AsyncSession, org_id: str, api_key: str, *, enabled: bool = True
) -> None:
    db.add(
        OrgLlmSettings(
            org_id=org_id,
            provider="openai",
            model="gpt-4o",
            encrypted_api_key=encrypt_llm_key(api_key),
            base_url=None,
            enabled=enabled,
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_user_judge_settings_win(db_session: AsyncSession):
    user = await _make_user(db_session, "alice")
    org = await _make_org(db_session)
    await _member(db_session, org.id, user.id)
    await _set_judge(db_session, user.id, "user-key")
    await _set_org_key(db_session, org.id, "org-key")

    resolved = await resolve_helper_llm(user, org.id, db_session)
    assert resolved is not None
    assert resolved.key_source == "user"
    assert resolved.api_key == "user-key"
    assert resolved.provider == "anthropic"


@pytest.mark.asyncio
async def test_org_key_used_when_no_user_key(db_session: AsyncSession):
    user = await _make_user(db_session, "bob")
    org = await _make_org(db_session)
    await _member(db_session, org.id, user.id)
    await _set_org_key(db_session, org.id, "org-key")

    resolved = await resolve_helper_llm(user, org.id, db_session)
    assert resolved is not None
    assert resolved.key_source == "org"
    assert resolved.api_key == "org-key"
    assert resolved.provider == "openai"


@pytest.mark.asyncio
async def test_disabled_org_key_ignored(db_session: AsyncSession):
    user = await _make_user(db_session, "carol")
    org = await _make_org(db_session)
    await _member(db_session, org.id, user.id)
    await _set_org_key(db_session, org.id, "org-key", enabled=False)

    assert await resolve_helper_llm(user, org.id, db_session) is None


@pytest.mark.asyncio
async def test_non_member_cannot_use_org_key(db_session: AsyncSession):
    user = await _make_user(db_session, "dave")
    org = await _make_org(db_session)
    # No OrgMember row for dave in this org.
    await _set_org_key(db_session, org.id, "org-key")

    assert await resolve_helper_llm(user, org.id, db_session) is None


@pytest.mark.asyncio
async def test_none_when_no_keys(db_session: AsyncSession):
    user = await _make_user(db_session, "erin")
    org = await _make_org(db_session)
    await _member(db_session, org.id, user.id)

    assert await resolve_helper_llm(user, org.id, db_session) is None


def test_resolved_llm_repr_hides_key():
    resolved = ResolvedLLM(
        provider="anthropic",
        model="claude-opus",
        api_key="sk-super-secret-key",
        base_url=None,
        key_source="user",
    )
    assert "sk-super-secret-key" not in repr(resolved)


@pytest.mark.asyncio
async def test_downgraded_org_key_does_not_resolve(db_session: AsyncSession):
    user = await _make_user(db_session, "frank")
    org = await _make_org(db_session)
    await _member(db_session, org.id, user.id)
    await _set_org_key(db_session, org.id, "org-key")

    assert (await resolve_helper_llm(user, org.id, db_session)).key_source == "org"

    org.tier = "team"
    await db_session.commit()

    assert await resolve_helper_llm(user, org.id, db_session) is None
