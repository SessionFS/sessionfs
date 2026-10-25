"""Org-wide LLM key for the session helper — API surface (migration 084).

Covers GET/PUT/DELETE /api/v1/orgs/{org_id}/llm-settings: org owner/admin
only, Enterprise-only (feature ``session_helper``), the key never returned,
the stored key kept on PUT without ``api_key``, the base_url SSRF guard, and
append-only OrgAuditEvent rows that never contain the key.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    OrgAuditEvent,
    OrgLlmSettings,
    OrgMember,
    Organization,
    User,
)
from sessionfs.server.llm_keys import decrypt_llm_key


async def _make_user(db: AsyncSession, name: str = "alice") -> tuple[User, str]:
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
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"{name}-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _make_org_with(
    db: AsyncSession,
    admin: User,
    member: User | None = None,
    *,
    tier: str = "enterprise",
) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Test Org",
        slug=f"test-{uuid.uuid4().hex[:6]}",
        tier=tier,
        seats_limit=10,
    )
    db.add(org)
    await db.commit()
    db.add(OrgMember(org_id=org.id, user_id=admin.id, role="admin"))
    if member is not None:
        db.add(OrgMember(org_id=org.id, user_id=member.id, role="member"))
    await db.commit()
    await db.refresh(org)
    return org


async def _audit_events(db: AsyncSession, org_id: str) -> list[OrgAuditEvent]:
    result = await db.execute(
        select(OrgAuditEvent).where(OrgAuditEvent.org_id == org_id)
    )
    return list(result.scalars().all())


@pytest.mark.asyncio
async def test_get_returns_defaults_when_unset(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    resp = await client.get(f"/api/v1/orgs/{org.id}/llm-settings", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body == {
        "provider": "",
        "model": "",
        "has_api_key": False,
        "key_hint": None,
        "base_url": None,
        "enabled": True,
        "plan_includes_helper": True,
    }


@pytest.mark.asyncio
async def test_admin_put_and_get_round_trip(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    secret = "sk-org-secret-7f3a9d"
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={
            "provider": "anthropic",
            "model": "claude-sonnet-5-5",
            "api_key": secret,
            "base_url": None,
            "enabled": True,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_api_key"] is True
    assert body["key_hint"] == secret[-4:]
    assert secret not in resp.text  # the key is never returned

    resp2 = await client.get(f"/api/v1/orgs/{org.id}/llm-settings", headers=_hdrs(key))
    assert resp2.status_code == 200, resp2.text
    body2 = resp2.json()
    assert body2["provider"] == "anthropic"
    assert body2["model"] == "claude-sonnet-5-5"
    assert body2["has_api_key"] is True
    assert body2["key_hint"] == secret[-4:]
    assert secret not in resp2.text


@pytest.mark.asyncio
async def test_put_without_api_key_keeps_stored_key(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    secret = "sk-org-secret-keepme"
    await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={"provider": "anthropic", "model": "m", "api_key": secret, "enabled": True},
    )
    # A follow-up PUT that omits api_key must not wipe the stored key (a
    # model change is fine; changing provider without a key is a 422).
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={"provider": "anthropic", "model": "gpt-4o", "base_url": None, "enabled": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["provider"] == "anthropic"
    assert body["model"] == "gpt-4o"
    assert body["has_api_key"] is True
    assert body["key_hint"] == secret[-4:]


@pytest.mark.asyncio
async def test_key_never_in_response_or_audit(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    secret = "sk-org-secret-9x9x9x"
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={"provider": "anthropic", "model": "m", "api_key": secret, "enabled": True},
    )
    assert resp.status_code == 200
    assert secret not in resp.text

    events = await _audit_events(db_session, org.id)
    assert events, "a mutation must write an audit row"
    for event in events:
        assert secret not in (event.before or "")
        assert secret not in (event.after or "")
        assert event.event_type == "org_llm_settings_updated"
        # The before/after carry the safe fields, not the key.
        assert event.after is not None
        assert '"provider"' in event.after


@pytest.mark.asyncio
async def test_member_cannot_read_or_write(
    client: AsyncClient, db_session: AsyncSession
):
    admin, _ = await _make_user(db_session, "admin")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org_with(db_session, admin, member)

    get_resp = await client.get(
        f"/api/v1/orgs/{org.id}/llm-settings", headers=_hdrs(member_key)
    )
    assert get_resp.status_code == 403, get_resp.text
    assert get_resp.json()["error"]["details"]["error"] == "insufficient_role"

    put_resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(member_key),
        json={"provider": "anthropic", "model": "m", "api_key": "k", "enabled": True},
    )
    assert put_resp.status_code == 403, put_resp.text
    assert put_resp.json()["error"]["details"]["error"] == "insufficient_role"


@pytest.mark.asyncio
async def test_admin_of_another_org_gets_404(
    client: AsyncClient, db_session: AsyncSession
):
    admin_a, key_a = await _make_user(db_session, "admina")
    admin_b, _ = await _make_user(db_session, "adminb")
    org_a = await _make_org_with(db_session, admin_a)
    org_b = await _make_org_with(db_session, admin_b)

    # admin_a is not a member of org_b.
    resp = await client.get(
        f"/api/v1/orgs/{org_b.id}/llm-settings", headers=_hdrs(key_a)
    )
    assert resp.status_code == 404, resp.text
    assert org_a.id != org_b.id


@pytest.mark.asyncio
async def test_non_enterprise_org_gets_upgrade_envelope(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin, tier="team")
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={"provider": "anthropic", "model": "m", "api_key": "k", "enabled": True},
    )
    assert resp.status_code == 403, resp.text
    details = resp.json()["error"]["details"]
    assert details["error"] == "upgrade_required"
    assert details["feature"] == "session_helper"
    assert details["plan_scope"] == "org"
    assert details["required_tier"] == "enterprise"


@pytest.mark.asyncio
async def test_base_url_guard_rejects_private_host(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={
            "provider": "anthropic",
            "model": "m",
            "api_key": "k",
            "base_url": "http://127.0.0.1:8000/v1",
            "enabled": True,
        },
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_delete_removes_and_audits(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={"provider": "anthropic", "model": "m", "api_key": "sk-org-secret-1234", "enabled": True},
    )

    resp = await client.delete(
        f"/api/v1/orgs/{org.id}/llm-settings", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": True}

    events = await _audit_events(db_session, org.id)
    assert events[-1].event_type == "org_llm_settings_deleted"
    assert events[-1].before is not None
    assert events[-1].after is None

    # After delete, GET reports no key.
    get_resp = await client.get(
        f"/api/v1/orgs/{org.id}/llm-settings", headers=_hdrs(key)
    )
    assert get_resp.json()["has_api_key"] is False


@pytest.mark.asyncio
async def test_delete_when_unset_is_404(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    resp = await client.delete(
        f"/api/v1/orgs/{org.id}/llm-settings", headers=_hdrs(key)
    )
    assert resp.status_code == 404, resp.text


async def _seed_key(
    client: AsyncClient, db_session: AsyncSession, org: Organization, key: str,
    *, provider: str = "anthropic", model: str = "m", enabled: bool = True,
    api_key: str = "sk-org-secret-1234",
) -> None:
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={
            "provider": provider,
            "model": model,
            "api_key": api_key,
            "base_url": None,
            "enabled": enabled,
        },
    )
    assert resp.status_code == 200, resp.text


async def _downgrade(db_session: AsyncSession, org: Organization) -> None:
    org.tier = "team"
    await db_session.commit()


@pytest.mark.asyncio
async def test_downgraded_org_can_delete(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    await _seed_key(client, db_session, org, key)
    await _downgrade(db_session, org)

    resp = await client.delete(
        f"/api/v1/orgs/{org.id}/llm-settings", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": True}

    # The row is gone; a follow-up GET still works (no tier gate) and shows
    # the plan no longer includes the helper.
    get_resp = await client.get(
        f"/api/v1/orgs/{org.id}/llm-settings", headers=_hdrs(key)
    )
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["has_api_key"] is False
    assert get_resp.json()["plan_includes_helper"] is False


@pytest.mark.asyncio
async def test_downgraded_org_can_disable(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    await _seed_key(client, db_session, org, key)
    await _downgrade(db_session, org)

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={"provider": "anthropic", "model": "m", "base_url": None, "enabled": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False
    assert resp.json()["has_api_key"] is True


@pytest.mark.asyncio
async def test_downgraded_org_enable_is_403(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    await _seed_key(client, db_session, org, key, enabled=False)
    await _downgrade(db_session, org)

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={"provider": "anthropic", "model": "m", "base_url": None, "enabled": True},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["details"]["error"] == "upgrade_required"


@pytest.mark.asyncio
async def test_downgraded_org_change_key_is_403(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    await _seed_key(client, db_session, org, key)
    await _downgrade(db_session, org)

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={
            "provider": "anthropic",
            "model": "m",
            "api_key": "sk-org-secret-newkey",
            "base_url": None,
            "enabled": True,
        },
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["details"]["error"] == "upgrade_required"


@pytest.mark.asyncio
async def test_whitespace_api_key_is_422(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={"provider": "anthropic", "model": "m", "api_key": "   ", "enabled": True},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_short_api_key_is_422(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={"provider": "anthropic", "model": "m", "api_key": "short", "enabled": True},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_api_key_is_stripped(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    secret = "sk-org-secret-stripped"
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={
            "provider": "anthropic",
            "model": "m",
            "api_key": f"  {secret}  ",
            "enabled": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["key_hint"] == secret[-4:]

    row = (
        await db_session.execute(
            select(OrgLlmSettings).where(OrgLlmSettings.org_id == org.id)
        )
    ).scalar_one()
    assert decrypt_llm_key(row.encrypted_api_key) == secret


@pytest.mark.asyncio
async def test_key_hint_null_for_short_stored_key(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={"provider": "anthropic", "model": "m", "api_key": "12345678", "enabled": True},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_api_key"] is True
    assert body["key_hint"] is None


@pytest.mark.asyncio
async def test_key_rotation_audit_marks_key_changed(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    await _seed_key(client, db_session, org, key)
    # Rotate only the key — provider/model/base_url/enabled all unchanged.
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={
            "provider": "anthropic",
            "model": "m",
            "api_key": "sk-org-secret-rotated",
            "base_url": None,
            "enabled": True,
        },
    )
    assert resp.status_code == 200, resp.text

    events = await _audit_events(db_session, org.id)
    updated = [e for e in events if e.event_type == "org_llm_settings_updated"]
    assert len(updated) == 2
    before = json.loads(updated[-1].before)
    after = json.loads(updated[-1].after)
    assert before["has_api_key"] is True
    assert after["has_api_key"] is True
    assert after.get("key_changed") is True
    # The snapshots must differ now (key_changed is the only new field).
    assert before != after


@pytest.mark.asyncio
async def test_audit_failure_rolls_back_settings(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)

    def _boom(*args, **kwargs):
        raise RuntimeError("audit write failed")

    import sessionfs.server.routes.org_llm_settings as mod

    monkeypatch.setattr(mod, "_audit", _boom)

    try:
        await client.put(
            f"/api/v1/orgs/{org.id}/llm-settings",
            headers=_hdrs(key),
            json={
                "provider": "anthropic",
                "model": "m",
                "api_key": "sk-org-secret-1234",
                "base_url": None,
                "enabled": True,
            },
        )
    except Exception:
        pass

    result = await db_session.execute(
        select(OrgLlmSettings).where(OrgLlmSettings.org_id == org.id)
    )
    assert result.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_put_refreshes_updated_at(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    await _seed_key(client, db_session, org, key)

    row = (
        await db_session.execute(
            select(OrgLlmSettings).where(OrgLlmSettings.org_id == org.id)
        )
    ).scalar_one()
    row.updated_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={"provider": "anthropic", "model": "m2", "base_url": None, "enabled": True},
    )
    assert resp.status_code == 200, resp.text

    await db_session.refresh(row)
    assert row.updated_at.replace(tzinfo=None) > datetime(2020, 1, 1)


@pytest.mark.asyncio
async def test_provider_change_without_key_is_422(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    await _seed_key(client, db_session, org, key)

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={"provider": "openai", "model": "m", "base_url": None, "enabled": True},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "api_key_required"


@pytest.mark.asyncio
async def test_provider_change_allowed_when_no_key_stored(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    await _seed_key(client, db_session, org, key, api_key="")

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={"provider": "openai", "model": "m", "base_url": None, "enabled": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["provider"] == "openai"


@pytest.mark.asyncio
async def test_disable_works_when_stored_base_url_now_fails_guard(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    await _seed_key(client, db_session, org, key)
    row = (
        await db_session.execute(
            select(OrgLlmSettings).where(OrgLlmSettings.org_id == org.id)
        )
    ).scalar_one()
    row.base_url = "https://10.0.0.5/v1"
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={
            "provider": "anthropic",
            "model": "m",
            "base_url": "https://10.0.0.5/v1",
            "enabled": False,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["enabled"] is False

    # Re-enabling the same endpoint is still refused by the guard.
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={
            "provider": "anthropic",
            "model": "m",
            "base_url": "https://10.0.0.5/v1",
            "enabled": True,
        },
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.asyncio
async def test_base_url_change_without_key_is_422(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """An admin can't re-point the stored key at another host without re-entering it."""
    import sessionfs.server.routes.org_llm_settings as mod

    async def _ok(url):
        return url.rstrip("/") if url else url

    monkeypatch.setattr(mod, "_check_llm_base_url_or_400", _ok)

    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    await _seed_key(client, db_session, org, key)

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={
            "provider": "anthropic",
            "model": "m",
            "base_url": "https://attacker.example.com/v1",
            "enabled": True,
        },
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "api_key_required"
    row = (
        await db_session.execute(
            select(OrgLlmSettings).where(OrgLlmSettings.org_id == org.id)
        )
    ).scalar_one()
    await db_session.refresh(row)
    assert row.base_url is None

    # Re-entering the key with the new endpoint is allowed.
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/llm-settings",
        headers=_hdrs(key),
        json={
            "provider": "anthropic",
            "model": "m",
            "api_key": "sk-new-key-0123456789",
            "base_url": "https://llm.example.com/v1",
            "enabled": True,
        },
    )
    assert resp.status_code == 200, resp.text
