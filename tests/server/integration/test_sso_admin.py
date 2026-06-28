"""SSO-P3 — provider config CRUD + domain verification admin surface.

Covers:
  - Provider CRUD: create/get/update/delete
  - client_secret_ref: only refs accepted, plaintext rejected
  - OIDC discovery validation on save (mock oidc_fetch_json)
  - One-enabled-per-org partial-unique → 409 on second enable
  - Domain verification: request → TXT token, verify happy path, deny list,
    cross-org already-verified → 409, DNS-miss → not verified
  - DNS resolver mockability
  - Authz: non-member → 404, member-but-not-admin/owner → 403,
    owner allowed, admin allowed
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    OrgMember,
    Organization,
    User,
)


# ---------------------------------------------------------------------------
# Helpers (mirror test_org_members.py pattern)
# ---------------------------------------------------------------------------


async def _make_user(
    db: AsyncSession, name: str = "alice"
) -> tuple[User, str]:
    """Create a User + ApiKey, return (user, raw_key)."""
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


async def _make_org(
    db: AsyncSession,
    admin: User,
    *,
    owner: User | None = None,
    name: str = "Test Org",
) -> Organization:
    """Create an org with *admin* as admin and optional *owner*."""
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name=name,
        slug=f"test-{uuid.uuid4().hex[:8]}",
        tier="team",
        seats_limit=10,
    )
    db.add(org)
    await db.commit()
    db.add(OrgMember(org_id=org.id, user_id=admin.id, role="admin"))
    if owner is not None and owner.id != admin.id:
        db.add(OrgMember(org_id=org.id, user_id=owner.id, role="owner"))
    elif owner is not None and owner.id == admin.id:
        # Upgrade admin → owner
        member = (
            await db.execute(
                select(OrgMember).where(
                    OrgMember.org_id == org.id, OrgMember.user_id == admin.id
                )
            )
        ).scalar_one()
        member.role = "owner"
    await db.commit()
    await db.refresh(org)
    return org


# A minimal valid OIDC discovery doc for testing
_MINIMAL_DISCOVERY = {
    "issuer": "https://accounts.example.com",
    "authorization_endpoint": "https://accounts.example.com/authorize",
    "token_endpoint": "https://accounts.example.com/token",
    "jwks_uri": "https://accounts.example.com/jwks",
}


async def _mock_oidc_fetch_json(url: str, **kwargs) -> dict:
    """Mock oidc_fetch_json — returns a valid discovery doc for any HTTPS URL."""
    return dict(_MINIMAL_DISCOVERY, issuer=url.replace("/.well-known/openid-configuration", ""))


# ---------------------------------------------------------------------------
# A. Provider CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_provider_persists_secret_ref_only(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """POST creates the provider; client_secret_ref is stored as-is.
    The response NEVER contains a plaintext secret — only the ref field."""
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)

    with patch(
        "sessionfs.server.routes.sso_admin.oidc_fetch_json",
        new=AsyncMock(side_effect=_mock_oidc_fetch_json),
    ):
        resp = await client.post(
            f"/api/v1/orgs/{org.id}/sso/provider",
            headers=_hdrs(admin_key),
            json={
                "display_name": "Acme Okta",
                "issuer": "https://acme.okta.com",
                "client_id": "client-123",
                "client_secret_ref": "projects/my-project/secrets/oidc-secret/versions/latest",
                "allowed_scopes": ["openid", "email", "profile"],
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["display_name"] == "Acme Okta"
    assert body["client_secret_ref"] == "projects/my-project/secrets/oidc-secret/versions/latest"
    # No plaintext secret field exists
    assert "client_secret" not in body
    assert body["enabled"] is False
    assert body["enforced"] is False


@pytest.mark.asyncio
async def test_create_provider_rejects_plaintext_secret_ref(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """client_secret_ref must match an allowed scheme prefix."""
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/sso/provider",
        headers=_hdrs(admin_key),
        json={
            "display_name": "Acme Okta",
            "issuer": "https://acme.okta.com",
            "client_id": "client-123",
            "client_secret_ref": "my-raw-secret-value-12345",
            "allowed_scopes": ["openid"],
        },
    )
    assert resp.status_code == 422, resp.text  # Pydantic validation error


@pytest.mark.asyncio
async def test_create_provider_validates_discovery(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """On create, the OIDC discovery doc is fetched + validated."""
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)

    # Simulate SSRF rejection
    async def _mock_ssrf_reject(url: str, **kwargs) -> dict:
        from sessionfs.server.services.oidc_fetch import SsrfError
        raise SsrfError("Host resolves to internal IP")

    with patch(
        "sessionfs.server.routes.sso_admin.oidc_fetch_json",
        new=AsyncMock(side_effect=_mock_ssrf_reject),
    ):
        resp = await client.post(
            f"/api/v1/orgs/{org.id}/sso/provider",
            headers=_hdrs(admin_key),
            json={
                "display_name": "Bad IdP",
                "issuer": "https://10.0.0.1",
                "client_id": "client-123",
                "client_secret_ref": "env:SECRET",
                "allowed_scopes": ["openid"],
            },
        )
    assert resp.status_code == 400, resp.text
    assert "rejected" in resp.text.lower() or "internal" in resp.text.lower()


@pytest.mark.asyncio
async def test_create_provider_unreachable_issuer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A network failure on discovery fetch returns 502."""
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)

    async def _mock_network_error(url: str, **kwargs) -> dict:
        raise OSError("Connection refused")

    with patch(
        "sessionfs.server.routes.sso_admin.oidc_fetch_json",
        new=AsyncMock(side_effect=_mock_network_error),
    ):
        resp = await client.post(
            f"/api/v1/orgs/{org.id}/sso/provider",
            headers=_hdrs(admin_key),
            json={
                "display_name": "Unreachable",
                "issuer": "https://unreachable.example.com",
                "client_id": "client-123",
                "client_secret_ref": "env:SECRET",
                "allowed_scopes": ["openid"],
            },
        )
    assert resp.status_code == 502, resp.text


@pytest.mark.asyncio
async def test_get_provider_returns_config(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """GET returns the provider config with client_secret_ref (never plaintext)."""
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)

    with patch(
        "sessionfs.server.routes.sso_admin.oidc_fetch_json",
        new=AsyncMock(side_effect=_mock_oidc_fetch_json),
    ):
        await client.post(
            f"/api/v1/orgs/{org.id}/sso/provider",
            headers=_hdrs(admin_key),
            json={
                "display_name": "Acme Okta",
                "issuer": "https://acme.okta.com",
                "client_id": "client-123",
                "client_secret_ref": "k8s:ns/secret/key",
                "allowed_scopes": ["openid", "email"],
            },
        )

    resp = await client.get(
        f"/api/v1/orgs/{org.id}/sso/provider",
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["client_secret_ref"] == "k8s:ns/secret/key"
    assert "client_secret" not in body


@pytest.mark.asyncio
async def test_patch_provider_updates_fields(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """PATCH updates provider fields; re-validates discovery if issuer changes."""
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)

    with patch(
        "sessionfs.server.routes.sso_admin.oidc_fetch_json",
        new=AsyncMock(side_effect=_mock_oidc_fetch_json),
    ):
        await client.post(
            f"/api/v1/orgs/{org.id}/sso/provider",
            headers=_hdrs(admin_key),
            json={
                "display_name": "Acme Okta",
                "issuer": "https://acme.okta.com",
                "client_id": "client-123",
                "client_secret_ref": "env:SECRET",
                "allowed_scopes": ["openid"],
            },
        )

    resp = await client.patch(
        f"/api/v1/orgs/{org.id}/sso/provider",
        headers=_hdrs(admin_key),
        json={"display_name": "Updated Name"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name"] == "Updated Name"
    # unchanged fields preserved
    assert resp.json()["issuer"] == "https://acme.okta.com"


@pytest.mark.asyncio
async def test_create_second_provider_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Creating a second OIDC provider in the same org returns 409.
    The API is singular — one OIDC provider per org."""
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)

    with patch(
        "sessionfs.server.routes.sso_admin.oidc_fetch_json",
        new=AsyncMock(side_effect=_mock_oidc_fetch_json),
    ):
        # Create first provider
        r1 = await client.post(
            f"/api/v1/orgs/{org.id}/sso/provider",
            headers=_hdrs(admin_key),
            json={
                "display_name": "First IdP",
                "issuer": "https://first.example.com",
                "client_id": "c1",
                "client_secret_ref": "env:S1",
                "allowed_scopes": ["openid"],
            },
        )
        assert r1.status_code == 200

        # Create second provider → 409 (one per org)
        r2 = await client.post(
            f"/api/v1/orgs/{org.id}/sso/provider",
            headers=_hdrs(admin_key),
            json={
                "display_name": "Second IdP",
                "issuer": "https://second.example.com",
                "client_id": "c2",
                "client_secret_ref": "env:S2",
                "allowed_scopes": ["openid"],
            },
        )
    assert r2.status_code == 409, r2.text


@pytest.mark.asyncio
async def test_delete_provider(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """DELETE removes the provider config."""
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)

    with patch(
        "sessionfs.server.routes.sso_admin.oidc_fetch_json",
        new=AsyncMock(side_effect=_mock_oidc_fetch_json),
    ):
        await client.post(
            f"/api/v1/orgs/{org.id}/sso/provider",
            headers=_hdrs(admin_key),
            json={
                "display_name": "Acme Okta",
                "issuer": "https://acme.okta.com",
                "client_id": "client-123",
                "client_secret_ref": "env:SECRET",
                "allowed_scopes": ["openid"],
            },
        )

    resp = await client.delete(
        f"/api/v1/orgs/{org.id}/sso/provider",
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] is True

    # GET now returns 404
    resp2 = await client.get(
        f"/api/v1/orgs/{org.id}/sso/provider",
        headers=_hdrs(admin_key),
    )
    assert resp2.status_code == 404


# ---------------------------------------------------------------------------
# B. Domain verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_request_domain_returns_txt_token(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """POST /domains returns a pending row with a TXT record token."""
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/sso/domains",
        headers=_hdrs(admin_key),
        json={"domain": "acme.com"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["domain"] == "acme.com"
    assert body["status"] == "pending"
    assert body["verification_token"]
    assert body["txt_record"].startswith("sessionfs-verification=")
    assert body["txt_record"] == f"sessionfs-verification={body['verification_token']}"


@pytest.mark.asyncio
async def test_verify_domain_happy_path(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """POST /domains/{id}/verify flips status to verified when DNS has the token."""
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)

    # Request verification
    r1 = await client.post(
        f"/api/v1/orgs/{org.id}/sso/domains",
        headers=_hdrs(admin_key),
        json={"domain": "acme.com"},
    )
    assert r1.status_code == 200
    dv = r1.json()
    token = dv["verification_token"]

    # Mock DNS to return the token
    async def _mock_dns(domain: str) -> list[str]:
        return ["some other record", f"sessionfs-verification={token}"]

    with patch("sessionfs.server.routes.sso_admin._resolve_txt", new=_mock_dns):
        r2 = await client.post(
            f"/api/v1/orgs/{org.id}/sso/domains/{dv['id']}/verify",
            headers=_hdrs(admin_key),
        )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "verified"
    assert body["verified_at"] is not None


@pytest.mark.asyncio
async def test_verify_domain_dns_miss(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """POST /domains/{id}/verify with no matching TXT → status stays pending/failed."""
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)

    r1 = await client.post(
        f"/api/v1/orgs/{org.id}/sso/domains",
        headers=_hdrs(admin_key),
        json={"domain": "acme.com"},
    )
    assert r1.status_code == 200
    dv = r1.json()

    async def _mock_empty_dns(domain: str) -> list[str]:
        return ["v=spf1 -all"]

    # Default _resolve_txt returns [] → not verified.  But we'll use
    # an explicit mock to be sure.
    with patch("sessionfs.server.routes.sso_admin._resolve_txt", new=_mock_empty_dns):
        r2 = await client.post(
            f"/api/v1/orgs/{org.id}/sso/domains/{dv['id']}/verify",
            headers=_hdrs(admin_key),
        )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["status"] == "failed"
    assert body["verified_at"] is None


@pytest.mark.asyncio
async def test_consumer_domain_denied(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """gmail.com (and other consumer domains) → 400, never verifiable."""
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)

    for domain in ("gmail.com", "outlook.com", "yahoo.com"):
        resp = await client.post(
            f"/api/v1/orgs/{org.id}/sso/domains",
            headers=_hdrs(admin_key),
            json={"domain": domain},
        )
        assert resp.status_code == 400, f"{domain}: {resp.text}"
        assert "consumer" in resp.text.lower() or "cannot be verified" in resp.text.lower()


@pytest.mark.asyncio
async def test_second_org_cannot_verify_same_domain(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A verified domain is claimed by at most one org (partial-unique index)."""
    admin1, key1 = await _make_user(db_session, "admin1")
    org1 = await _make_org(db_session, admin1, name="Org 1")

    admin2, key2 = await _make_user(db_session, "admin2")
    org2 = await _make_org(db_session, admin2, name="Org 2")

    # Org 1 requests + verifies
    r1 = await client.post(
        f"/api/v1/orgs/{org1.id}/sso/domains",
        headers=_hdrs(key1),
        json={"domain": "acme.com"},
    )
    dv1 = r1.json()

    async def _mock_dns(domain: str) -> list[str]:
        return [f"sessionfs-verification={dv1['verification_token']}"]

    with patch("sessionfs.server.routes.sso_admin._resolve_txt", new=_mock_dns):
        await client.post(
            f"/api/v1/orgs/{org1.id}/sso/domains/{dv1['id']}/verify",
            headers=_hdrs(key1),
        )

    # Org 2 requests same domain
    r2 = await client.post(
        f"/api/v1/orgs/{org2.id}/sso/domains",
        headers=_hdrs(key2),
        json={"domain": "acme.com"},
    )
    # Should be rejected because domain is already verified by org1
    assert r2.status_code == 409, r2.text
    assert "already verified" in r2.text.lower()


@pytest.mark.asyncio
async def test_list_domains(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """GET /domains lists all verification requests for the org."""
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)

    await client.post(
        f"/api/v1/orgs/{org.id}/sso/domains",
        headers=_hdrs(admin_key),
        json={"domain": "acme.com"},
    )
    await client.post(
        f"/api/v1/orgs/{org.id}/sso/domains",
        headers=_hdrs(admin_key),
        json={"domain": "example.org"},
    )

    resp = await client.get(
        f"/api/v1/orgs/{org.id}/sso/domains",
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 2
    domains = {d["domain"] for d in body}
    assert domains == {"acme.com", "example.org"}


@pytest.mark.asyncio
async def test_delete_domain(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """DELETE /domains/{id} removes the verification request."""
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)

    r1 = await client.post(
        f"/api/v1/orgs/{org.id}/sso/domains",
        headers=_hdrs(admin_key),
        json={"domain": "acme.com"},
    )
    dv_id = r1.json()["id"]

    resp = await client.delete(
        f"/api/v1/orgs/{org.id}/sso/domains/{dv_id}",
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] is True


# ---------------------------------------------------------------------------
# C. Authorization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_member_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Cross-org isolation: a non-member of the org gets 404."""
    admin, _ = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)
    eve, eve_key = await _make_user(db_session, "eve")

    resp = await client.get(
        f"/api/v1/orgs/{org.id}/sso/provider",
        headers=_hdrs(eve_key),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_member_not_admin_403(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A plain member (not admin/owner) gets 403."""
    admin, _ = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)
    bob, bob_key = await _make_user(db_session, "bob")
    db_session.add(OrgMember(org_id=org.id, user_id=bob.id, role="member"))
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/orgs/{org.id}/sso/provider",
        headers=_hdrs(bob_key),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_owner_allowed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Owner can access SSO admin routes."""
    owner, owner_key = await _make_user(db_session, "owner")
    org = await _make_org(db_session, owner, owner=owner)  # owner is both admin and owner

    with patch(
        "sessionfs.server.routes.sso_admin.oidc_fetch_json",
        new=AsyncMock(side_effect=_mock_oidc_fetch_json),
    ):
        resp = await client.post(
            f"/api/v1/orgs/{org.id}/sso/provider",
            headers=_hdrs(owner_key),
            json={
                "display_name": "Owner's IdP",
                "issuer": "https://owner.example.com",
                "client_id": "client-owner",
                "client_secret_ref": "env:OWNER_SECRET",
                "allowed_scopes": ["openid"],
            },
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_admin_allowed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Admin (non-owner) can access SSO admin routes."""
    owner, _ = await _make_user(db_session, "owner")
    org = await _make_org(db_session, owner, owner=owner)
    admin, admin_key = await _make_user(db_session, "admin2")
    db_session.add(OrgMember(org_id=org.id, user_id=admin.id, role="admin"))
    await db_session.commit()

    with patch(
        "sessionfs.server.routes.sso_admin.oidc_fetch_json",
        new=AsyncMock(side_effect=_mock_oidc_fetch_json),
    ):
        resp = await client.post(
            f"/api/v1/orgs/{org.id}/sso/provider",
            headers=_hdrs(admin_key),
            json={
                "display_name": "Admin's IdP",
                "issuer": "https://admin.example.com",
                "client_id": "client-admin",
                "client_secret_ref": "k8s:ns/sec/key",
                "allowed_scopes": ["openid"],
            },
        )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# D. client_secret_ref scheme validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_patch_rejects_plaintext_secret_ref(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """PATCH also rejects plaintext-looking secret_refs."""
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)

    with patch(
        "sessionfs.server.routes.sso_admin.oidc_fetch_json",
        new=AsyncMock(side_effect=_mock_oidc_fetch_json),
    ):
        await client.post(
            f"/api/v1/orgs/{org.id}/sso/provider",
            headers=_hdrs(admin_key),
            json={
                "display_name": "Test",
                "issuer": "https://test.example.com",
                "client_id": "c1",
                "client_secret_ref": "env:SECRET",
                "allowed_scopes": ["openid"],
            },
        )

    resp = await client.patch(
        f"/api/v1/orgs/{org.id}/sso/provider",
        headers=_hdrs(admin_key),
        json={"client_secret_ref": "this-looks-like-a-raw-secret"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_env_ref_allowed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """env:VAR_NAME is a valid secret_ref scheme."""
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)

    with patch(
        "sessionfs.server.routes.sso_admin.oidc_fetch_json",
        new=AsyncMock(side_effect=_mock_oidc_fetch_json),
    ):
        resp = await client.post(
            f"/api/v1/orgs/{org.id}/sso/provider",
            headers=_hdrs(admin_key),
            json={
                "display_name": "Test",
                "issuer": "https://test.example.com",
                "client_id": "c1",
                "client_secret_ref": "env:MY_OIDC_SECRET",
                "allowed_scopes": ["openid"],
            },
        )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_k8s_ref_allowed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """k8s: namespace/secret/key is a valid secret_ref scheme."""
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)

    with patch(
        "sessionfs.server.routes.sso_admin.oidc_fetch_json",
        new=AsyncMock(side_effect=_mock_oidc_fetch_json),
    ):
        resp = await client.post(
            f"/api/v1/orgs/{org.id}/sso/provider",
            headers=_hdrs(admin_key),
            json={
                "display_name": "Test",
                "issuer": "https://test.example.com",
                "client_id": "c1",
                "client_secret_ref": "k8s:default/oidc-secret/client-secret",
                "allowed_scopes": ["openid"],
            },
        )
    assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# E. Idempotent domain re-request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_pending_domain_returns_existing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Requesting the same domain twice returns the existing pending row."""
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin)

    r1 = await client.post(
        f"/api/v1/orgs/{org.id}/sso/domains",
        headers=_hdrs(admin_key),
        json={"domain": "acme.com"},
    )
    assert r1.status_code == 200
    dv1 = r1.json()

    r2 = await client.post(
        f"/api/v1/orgs/{org.id}/sso/domains",
        headers=_hdrs(admin_key),
        json={"domain": "acme.com"},
    )
    assert r2.status_code == 200
    dv2 = r2.json()
    assert dv2["id"] == dv1["id"]
    assert dv2["verification_token"] == dv1["verification_token"]


# ---------------------------------------------------------------------------
# _resolve_txt real implementation (the production DNS path is otherwise
# mocked away by the endpoint tests — exercise it directly here).
# ---------------------------------------------------------------------------


class _FakeTxtRdata:
    def __init__(self, chunks: list[bytes]) -> None:
        self.strings = chunks


@pytest.mark.asyncio
async def test_resolve_txt_decodes_and_concatenates_chunks():
    import sessionfs.server.routes.sso_admin as mod

    async def _fake_resolve(domain, rtype):
        assert rtype == "TXT"
        return [
            _FakeTxtRdata([b"sessionfs-verification=", b"abc123"]),
            _FakeTxtRdata([b"v=spf1 -all"]),
        ]

    with patch("dns.asyncresolver.resolve", side_effect=_fake_resolve):
        out = await mod._resolve_txt("example.com")
    assert "sessionfs-verification=abc123" in out
    assert "v=spf1 -all" in out


@pytest.mark.asyncio
async def test_resolve_txt_fails_closed_on_dns_error():
    import sessionfs.server.routes.sso_admin as mod

    async def _boom(domain, rtype):
        raise Exception("NXDOMAIN")

    with patch("dns.asyncresolver.resolve", side_effect=_boom):
        out = await mod._resolve_txt("nope.invalid")
    assert out == []
