"""POST /api/v1/orgs/{org_id}/llm-settings/models — "Fetch models" for the
org helper key form.

Shares its implementation with POST /api/v1/settings/judge/models: owner/admin
only, the stored org key is used only for the provider/endpoint it was saved
with, and custom endpoints go through the LLM URL SSRF guard.
"""

from __future__ import annotations

import socket
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    OrgLlmSettings,
    OrgMember,
    Organization,
    User,
)
from sessionfs.server.llm_keys import encrypt_llm_key

_DNS: dict[str, str] = {}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    _DNS.clear()
    monkeypatch.delenv("SFS_LLM_ALLOW_PRIVATE_HOSTS", raising=False)

    def fake(host, port, *a, **k):
        default = host if host[0].isdigit() or ":" in host else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_DNS.get(host, default), 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.fixture
def upstream(monkeypatch, client):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"data": [{"id": "llama3", "owned_by": "me"}]})

    real = httpx.AsyncClient

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return calls


async def _make_user(db: AsyncSession, name: str) -> tuple[User, str]:
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


async def _make_org(
    db: AsyncSession, admin: User, member: User | None = None, *, tier: str = "enterprise"
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
    return org


async def _save_key(db: AsyncSession, org: Organization, *, provider: str, base_url: str | None) -> None:
    db.add(
        OrgLlmSettings(
            org_id=org.id,
            provider=provider,
            model="m",
            encrypted_api_key=encrypt_llm_key("sk-org-saved-key"),
            base_url=base_url,
            enabled=True,
        )
    )
    await db.commit()


def _h(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _url(org: Organization) -> str:
    return f"/api/v1/orgs/{org.id}/llm-settings/models"


async def test_admin_lists_models_with_typed_key(client, db_session, upstream):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)
    r = await client.post(
        _url(org),
        json={"base_url": "https://llm.example.com/v1", "api_key": "sk-typed"},
        headers=_h(key),
    )
    assert r.status_code == 200, r.text
    assert r.json()["models"] == [{"id": "llama3", "owned_by": "me"}]
    assert upstream[0].headers["Authorization"] == "Bearer sk-typed"


async def test_admin_registry_provider_uses_bound_saved_key(client, db_session, upstream):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)
    await _save_key(db_session, org, provider="openai", base_url=None)
    r = await client.post(_url(org), json={"provider": "openai"}, headers=_h(key))
    assert r.status_code == 200, r.text
    assert r.json()["provider"] == "openai"
    assert str(upstream[0].url) == "https://api.openai.com/v1/models"
    assert upstream[0].headers["Authorization"] == "Bearer sk-org-saved-key"


async def test_saved_custom_endpoint_key_used_for_same_endpoint(client, db_session, upstream):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)
    await _save_key(db_session, org, provider="openai", base_url="https://llm.example.com/v1")
    r = await client.post(
        _url(org), json={"base_url": "https://llm.example.com/v1/"}, headers=_h(key)
    )
    assert r.status_code == 200, r.text
    assert upstream[0].headers["Authorization"] == "Bearer sk-org-saved-key"


async def test_saved_key_never_sent_to_another_provider_or_endpoint(client, db_session, upstream):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)
    await _save_key(db_session, org, provider="anthropic", base_url=None)
    # A different registry provider.
    r = await client.post(_url(org), json={"provider": "openai"}, headers=_h(key))
    assert r.status_code == 200, r.text
    # A custom endpoint the caller picks.
    r = await client.post(
        _url(org), json={"base_url": "https://attacker.example.com/v1"}, headers=_h(key)
    )
    assert r.status_code == 200, r.text
    assert len(upstream) == 2
    assert all("Authorization" not in req.headers for req in upstream)


async def test_saved_custom_endpoint_key_not_sent_to_registry_provider(client, db_session, upstream):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)
    await _save_key(db_session, org, provider="openai", base_url="https://llm.example.com/v1")
    r = await client.post(_url(org), json={"provider": "openai"}, headers=_h(key))
    assert r.status_code == 200, r.text
    assert "Authorization" not in upstream[0].headers


@pytest.mark.parametrize(
    "url",
    ["http://169.254.169.254", "https://10.0.0.5", "https://[::ffff:10.0.0.5]", "https://internal.example"],
)
async def test_private_host_refused(client, db_session, upstream, url):
    _DNS["internal.example"] = "172.16.0.4"
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)
    r = await client.post(_url(org), json={"base_url": url, "api_key": "sk-x"}, headers=_h(key))
    assert r.status_code == 400
    assert "172.16" not in r.text and "10.0.0.5" not in r.text
    assert upstream == []


async def test_member_gets_403(client, db_session, upstream):
    admin, _ = await _make_user(db_session, "admin")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org(db_session, admin, member)
    r = await client.post(
        _url(org), json={"base_url": "https://llm.example.com/v1"}, headers=_h(member_key)
    )
    assert r.status_code == 403
    assert "insufficient_role" in r.text
    assert upstream == []


async def test_other_org_admin_gets_404(client, db_session, upstream):
    admin, _ = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)
    outsider, outsider_key = await _make_user(db_session, "outsider")
    await _make_org(db_session, outsider)
    r = await client.post(
        _url(org), json={"base_url": "https://llm.example.com/v1"}, headers=_h(outsider_key)
    )
    assert r.status_code == 404
    assert upstream == []


async def test_non_enterprise_admin_gets_upgrade_required(client, db_session, upstream):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin, tier="team")
    r = await client.post(
        _url(org), json={"base_url": "https://llm.example.com/v1", "api_key": "sk-x"}, headers=_h(key)
    )
    assert r.status_code == 403
    assert "upgrade_required" in r.text
    assert upstream == []


async def test_no_saved_row_and_no_key_lists_without_auth(client, db_session, upstream):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)
    r = await client.post(_url(org), json={"provider": "openai"}, headers=_h(key))
    assert r.status_code == 200, r.text
    assert r.json()["models"] == [{"id": "llama3", "owned_by": "me"}]
    assert "Authorization" not in upstream[0].headers


async def test_org_discovery_is_rate_limited_per_user(client, db_session, upstream):
    from sessionfs.server.services import llm_connection

    admin, key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)
    body = {"base_url": "https://llm.example.com/v1", "api_key": "sk-x"}
    for _ in range(llm_connection.TESTS_PER_MINUTE):
        assert (await client.post(_url(org), json=body, headers=_h(key))).status_code == 200
    r = await client.post(_url(org), json=body, headers=_h(key))
    assert r.status_code == 429
    assert "models_rate_limited" in r.text
    assert len(upstream) == llm_connection.TESTS_PER_MINUTE
    # A separate bucket: connection tests are still available.
    assert llm_connection.take_test_slot(admin.id)


async def test_personal_discovery_is_rate_limited_per_user(client, db_session, upstream):
    from sessionfs.server.services import llm_connection

    user, key = await _make_user(db_session, "solo")
    body = {"base_url": "https://llm.example.com/v1", "api_key": "sk-x"}
    for _ in range(llm_connection.TESTS_PER_MINUTE):
        r = await client.post("/api/v1/settings/judge/models", json=body, headers=_h(key))
        assert r.status_code == 200
    r = await client.post("/api/v1/settings/judge/models", json=body, headers=_h(key))
    assert r.status_code == 429
    assert "models_rate_limited" in r.text
    r = await client.get(
        "/api/v1/settings/judge/models", params={"base_url": "https://llm.example.com/v1"}, headers=_h(key)
    )
    assert r.status_code == 429
    other, other_key = await _make_user(db_session, "other")
    r = await client.post("/api/v1/settings/judge/models", json=body, headers=_h(other_key))
    assert r.status_code == 200


async def test_refused_and_suggested_only_lookups_do_not_use_the_limit(client, db_session, upstream):
    """Only a request that is about to call out takes a rate-limit slot."""
    from sessionfs.server.services import llm_connection

    user, key = await _make_user(db_session, "careful")
    url = "/api/v1/settings/judge/models"
    for _ in range(llm_connection.TESTS_PER_MINUTE + 2):
        # 400: refused endpoint (SSRF guard).
        assert (await client.post(url, json={"base_url": "https://10.0.0.5"}, headers=_h(key))).status_code == 400
        # 422: unknown provider.
        assert (await client.post(url, json={"provider": "nope"}, headers=_h(key))).status_code == 422
        # Suggested models only: no list-models API, no outbound call.
        r = await client.post(url, json={"provider": "anthropic"}, headers=_h(key))
        assert r.status_code == 200 and r.json()["models"]
    assert upstream == []
    r = await client.post(url, json={"base_url": "https://llm.example.com/v1", "api_key": "sk-x"}, headers=_h(key))
    assert r.status_code == 200
    assert len(upstream) == 1
