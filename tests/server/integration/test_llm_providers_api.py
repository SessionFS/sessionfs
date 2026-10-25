"""First-class LLM providers — list endpoint, PUT validation and discovery.

Covers GET /api/v1/llm/providers, the 422 ``unknown_provider`` guard on the
judge and org LLM settings PUTs, and provider-based model discovery via
POST /api/v1/settings/judge/models.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import ApiKey, OrgMember, Organization, User
from sessionfs.server.routes.llm_providers import validate_provider_or_422


# ---------------------------------------------------------------------------
# Helpers (mirror the org settings integration test so files stay independent)
# ---------------------------------------------------------------------------

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


async def _make_org_with(db: AsyncSession, admin: User, *, tier: str = "enterprise") -> Organization:
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
    await db.commit()
    await db.refresh(org)
    return org


@pytest.fixture
def upstream(monkeypatch):
    """Route outbound model-discovery calls through a MockTransport."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "api.deepseek.com":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": "deepseek-flash", "owned_by": "deepseek"},
                        {"id": "deepseek-v4-pro", "owned_by": "deepseek"},
                    ]
                },
            )
        return httpx.Response(500, json={"error": {"message": "boom"}})

    real = httpx.AsyncClient

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return calls


# ---------------------------------------------------------------------------
# GET /api/v1/llm/providers
# ---------------------------------------------------------------------------

class TestListProviders:
    async def test_returns_registry_shape(self, client: AsyncClient, auth_headers):
        resp = await client.get("/api/v1/llm/providers", headers=auth_headers)
        assert resp.status_code == 200, resp.text
        providers = resp.json()["providers"]
        ids = {p["id"] for p in providers}
        assert {
            "anthropic",
            "openai",
            "google",
            "openrouter",
            "deepseek",
            "kimi",
            "xai",
            "mistral",
            "zai",
            "qwen",
            "minimax",
            "groq",
            "together",
            "fireworks",
        } <= ids
        for p in providers:
            assert set(p) == {"id", "label", "api_style", "suggested_models", "discovery"}
            assert isinstance(p["suggested_models"], list)

    async def test_requires_auth(self, client: AsyncClient):
        resp = await client.get("/api/v1/llm/providers")
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# validate_provider_or_422 (shared guard)
# ---------------------------------------------------------------------------

class TestProviderValidation:
    def test_unknown_provider_raises_422(self):
        with pytest.raises(HTTPException) as exc:
            validate_provider_or_422("bogus", None)
        assert exc.value.status_code == 422
        assert exc.value.detail["code"] == "unknown_provider"

    def test_custom_base_url_skips_registry_check(self):
        # A caller-supplied endpoint uses the OpenAI-compatible format and is
        # allowed to carry an arbitrary provider label.
        validate_provider_or_422("my-gateway", "https://llm.example.com/v1")

    def test_known_provider_passes(self):
        validate_provider_or_422("deepseek", None)
        validate_provider_or_422("KIMI", None)


# ---------------------------------------------------------------------------
# PUT /api/v1/settings/judge
# ---------------------------------------------------------------------------

class TestJudgeSettingsValidation:
    async def test_unknown_provider_422(self, client: AsyncClient, auth_headers):
        resp = await client.put(
            "/api/v1/settings/judge",
            json={"provider": "bogus", "model": "m", "api_key": "k"},
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "unknown_provider"

    async def test_registry_provider_accepted(self, client: AsyncClient, auth_headers):
        resp = await client.put(
            "/api/v1/settings/judge",
            json={"provider": "deepseek", "model": "deepseek-flash", "api_key": "k"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# PUT /api/v1/orgs/{org_id}/llm-settings
# ---------------------------------------------------------------------------

class TestOrgSettingsValidation:
    async def test_unknown_provider_422(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        admin, key = await _make_user(db_session, "admin")
        org = await _make_org_with(db_session, admin)
        resp = await client.put(
            f"/api/v1/orgs/{org.id}/llm-settings",
            headers=_hdrs(key),
            json={"provider": "bogus", "model": "m", "api_key": "k", "enabled": True},
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "unknown_provider"

    async def test_registry_provider_accepted(
        self, client: AsyncClient, db_session: AsyncSession
    ):
        admin, key = await _make_user(db_session, "admin")
        org = await _make_org_with(db_session, admin)
        resp = await client.put(
            f"/api/v1/orgs/{org.id}/llm-settings",
            headers=_hdrs(key),
            json={"provider": "kimi", "model": "kimi-k3", "api_key": "sk-org-secret", "enabled": True},
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Provider-based model discovery (POST /api/v1/settings/judge/models)
# ---------------------------------------------------------------------------

class TestProviderDiscovery:
    async def test_discovery_uses_registry_models_endpoint(
        self, client: AsyncClient, auth_headers, upstream
    ):
        resp = await client.post(
            "/api/v1/settings/judge/models",
            json={"provider": "deepseek", "api_key": "sk-x"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["provider"] == "deepseek"
        assert body["base_url"] == "https://api.deepseek.com/models"
        assert body["models"] == [
            {"id": "deepseek-flash", "owned_by": "deepseek"},
            {"id": "deepseek-v4-pro", "owned_by": "deepseek"},
        ]
        assert len(upstream) == 1
        assert str(upstream[0].url) == "https://api.deepseek.com/models"
        assert upstream[0].headers["Authorization"] == "Bearer sk-x"

    async def test_non_discovery_provider_returns_suggested_models(
        self, client: AsyncClient, auth_headers, upstream
    ):
        resp = await client.post(
            "/api/v1/settings/judge/models",
            json={"provider": "anthropic"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["provider"] == "anthropic"
        assert body["base_url"] is None
        assert body["models"] == [{"id": m, "owned_by": ""} for m in
                                  ("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5")]
        assert upstream == []

    async def test_error_falls_back_to_suggested_models(
        self, client: AsyncClient, auth_headers, upstream
    ):
        resp = await client.post(
            "/api/v1/settings/judge/models",
            json={"provider": "kimi"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["provider"] == "kimi"
        assert body["base_url"] == "https://api.moonshot.ai/v1/models"
        assert body["error"] == "Endpoint returned 500"
        assert [m["id"] for m in body["models"]] == [
            "kimi-k3",
            "kimi-k3.1-pro",
            "kimi-k2.6",
        ]
        assert len(upstream) == 1

    async def test_unknown_provider_422(
        self, client: AsyncClient, auth_headers, upstream
    ):
        resp = await client.post(
            "/api/v1/settings/judge/models",
            json={"provider": "bogus"},
            headers=auth_headers,
        )
        assert resp.status_code == 422
        assert resp.json()["error"]["code"] == "unknown_provider"
        assert upstream == []


class TestProviderDiscoverySavedKey:
    async def test_saved_key_used_for_its_own_provider(
        self, client: AsyncClient, auth_headers, upstream
    ):
        await client.put(
            "/api/v1/settings/judge",
            json={"provider": "deepseek", "model": "deepseek-flash", "api_key": "sk-ds-saved"},
            headers=auth_headers,
        )
        resp = await client.post(
            "/api/v1/settings/judge/models",
            json={"provider": "deepseek"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert upstream[-1].headers["Authorization"] == "Bearer sk-ds-saved"

    async def test_saved_key_never_sent_to_another_provider(
        self, client: AsyncClient, auth_headers, upstream
    ):
        await client.put(
            "/api/v1/settings/judge",
            json={"provider": "deepseek", "model": "deepseek-flash", "api_key": "sk-ds-saved"},
            headers=auth_headers,
        )
        resp = await client.post(
            "/api/v1/settings/judge/models",
            json={"provider": "groq"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert "Authorization" not in upstream[-1].headers

    async def test_saved_custom_endpoint_key_not_sent_to_a_registry_provider(
        self, client: AsyncClient, auth_headers, upstream
    ):
        await client.put(
            "/api/v1/settings/judge",
            json={
                "provider": "openai",
                "model": "m",
                "api_key": "sk-gateway",
                "base_url": "https://llm.example.com/v1",
            },
            headers=auth_headers,
        )
        resp = await client.post(
            "/api/v1/settings/judge/models",
            json={"provider": "openai"},
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        assert "Authorization" not in upstream[-1].headers
