"""SSO-P4 — enforcement + break-glass + tier gate + deprovision.

Covers the full mandatory matrix from the SSO-P4 brief:
  1. enforced org + enforced-domain user + non-sso_minted key → 403
  2. same user + sso_minted key → ALLOWED
  3. SERVICE KEY under enforcement → STILL WORKS (categorical exemption)
  4. OWNER with non-sso_minted key → ALLOWED (never locked out)
  5. Break-glass: issue, use, expire, revoke, duplicate 409
  6. Non-enforced-domain user → unaffected
  7. Enforcement arming: admin/owner allowed, member 403
  8. Tier gate: FREE org → 403 on provider/domain/enforce
  9. Deprovision: identities deactivated + sso_minted keys revoked
  10. FAIL-OPEN on enforcement eval error → allowed
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    ExternalIdentity,
    OrgDomainVerification,
    OrgIdentityProvider,
    OrgMember,
    Organization,
    SsoBreakGlassGrant,
    User,
)


# ---------------------------------------------------------------------------
# Helpers (mirror test_sso_admin.py pattern)
# ---------------------------------------------------------------------------


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _make_user(
    db: AsyncSession,
    name: str = "alice",
    email: str | None = None,
    tier: str = "team",
) -> tuple[User, str]:
    """Create a User + ApiKey, return (user, raw_key)."""
    user = User(
        id=str(uuid.uuid4()),
        email=email or f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier=tier,
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


async def _make_user_with_sso_key(
    db: AsyncSession,
    name: str = "bob",
    email: str | None = None,
    tier: str = "team",
) -> tuple[User, str]:
    """Create a User + an sso_minted ApiKey."""
    user = User(
        id=str(uuid.uuid4()),
        email=email or f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier=tier,
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
            name=f"{name}-sso-key",
            sso_minted=True,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


async def _make_org_with_admin(
    db: AsyncSession,
    admin: User,
    *,
    owner: User | None = None,
    name: str = "Test Org",
    tier: str = "team",
) -> Organization:
    """Create an org with *admin* as admin and optional *owner*."""
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name=name,
        slug=f"test-{uuid.uuid4().hex[:8]}",
        tier=tier,
        seats_limit=10,
    )
    db.add(org)
    await db.commit()
    db.add(OrgMember(org_id=org.id, user_id=admin.id, role="admin"))
    if owner is not None and owner.id != admin.id:
        db.add(OrgMember(org_id=org.id, user_id=owner.id, role="owner"))
    elif owner is not None and owner.id == admin.id:
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


async def _setup_enforcing_org(
    db: AsyncSession,
    admin: User,
    *,
    owner: User | None = None,
    domain: str = "acme.com",
    tier: str = "team",
) -> tuple[Organization, OrgIdentityProvider]:
    """Create an org with enabled+enforced OIDC provider + verified domain."""
    org = await _make_org_with_admin(db, admin, owner=owner, tier=tier)
    now = datetime.now(timezone.utc)
    idp = OrgIdentityProvider(
        id=f"oidp_{uuid.uuid4().hex[:12]}",
        org_id=org.id,
        protocol="oidc",
        display_name="Test IdP",
        issuer="https://idp.acme.com",
        client_id="test-client",
        client_secret_ref="env:SECRET",
        enabled=True,
        enforced=True,
        discovery_fetched_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add(idp)
    dv = OrgDomainVerification(
        id=f"odv_{uuid.uuid4().hex[:12]}",
        org_id=org.id,
        domain=domain,
        verification_token="tok",
        status="verified",
        verified_at=now,
        created_at=now,
    )
    db.add(dv)
    await db.commit()
    await db.refresh(idp)
    return org, idp


# Minimal valid OIDC discovery doc for testing
_MINIMAL_DISCOVERY = {
    "issuer": "https://accounts.example.com",
    "authorization_endpoint": "https://accounts.example.com/authorize",
    "token_endpoint": "https://accounts.example.com/token",
    "jwks_uri": "https://accounts.example.com/jwks",
}


async def _mock_oidc_fetch_json(url: str, **kwargs) -> dict:
    """Mock oidc_fetch_json — returns a valid discovery doc."""
    return dict(
        _MINIMAL_DISCOVERY,
        issuer=url.replace("/.well-known/openid-configuration", ""),
    )


# ---------------------------------------------------------------------------
# 1. Core enforcement — non-sso_minted key → 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enforced_user_non_sso_key_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Enforced org + verified-domain user + NON-sso_minted key → 403."""
    admin, admin_key = await _make_user(db_session, "admin", email="admin@acme.com")
    org, _ = await _setup_enforcing_org(db_session, admin, domain="acme.com")

    # Add a member with email on the enforced domain
    member, member_key = await _make_user(db_session, "member", email="member@acme.com")
    db_session.add(OrgMember(org_id=org.id, user_id=member.id, role="member"))
    await db_session.commit()

    # Make a request as the member — should get 403 enforcement
    resp = await client.get(
        f"/api/v1/orgs/{org.id}/sso/provider",
        headers=_hdrs(member_key),
    )
    assert resp.status_code == 403, resp.text
    body = resp.json()
    assert "error" in body
    assert "sso" in str(body).lower()


# ---------------------------------------------------------------------------
# 2. sso_minted key → ALLOWED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enforced_user_sso_minted_key_allowed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Same user with sso_minted key → allowed even under enforcement."""
    admin, admin_key = await _make_user(db_session, "admin", email="admin@acme.com")
    org, idp = await _setup_enforcing_org(db_session, admin, domain="acme.com")

    # Create a member with sso_minted key
    member, member_key = await _make_user_with_sso_key(db_session, "member", email="member@acme.com")
    db_session.add(OrgMember(org_id=org.id, user_id=member.id, role="member"))
    # Also create an active ExternalIdentity for live-identity check
    now = datetime.now(timezone.utc)
    db_session.add(
        ExternalIdentity(
            id=f"ext_{uuid.uuid4().hex[:12]}",
            user_id=member.id,
            org_idp_id=idp.id,
            provider_issuer=idp.issuer,
            subject="sub-123",
            email_at_link="member@acme.com",
            link_method="jit_provision",
            linked_at=now,
            created_at=now,
        )
    )
    await db_session.commit()

    # SSO-minted key should work
    resp = await client.get(
        f"/api/v1/orgs/{org.id}/sso/provider",
        headers=_hdrs(member_key),
    )
    # This member is not admin so they'll get a 403/404 on the SSO admin
    # route (not enforcement-related). As long as it's not 403 enforcement.
    assert resp.status_code != 403 or "sso_enforcement_required" not in resp.text, (
        f"SSO-minted key should not be blocked by enforcement. Got: {resp.text}"
    )

    # Verify the key works for /me endpoint (no admin required)
    resp = await client.get("/api/v1/auth/me", headers=_hdrs(member_key))
    assert resp.status_code == 200, f"SSO-minted key blocked on /me: {resp.text}"


# ---------------------------------------------------------------------------
# 3. SERVICE KEY under enforcement → STILL WORKS (THE LOAD-BEARING TEST)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_key_works_under_enforcement(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Service key is CATEGORICALLY EXEMPT from SSO enforcement."""
    admin, admin_key = await _make_user(db_session, "admin", email="admin@acme.com")
    org, _ = await _setup_enforcing_org(db_session, admin, domain="acme.com")

    # Create a service key for the org
    svc_raw = generate_api_key()
    svc_key = ApiKey(
        id=str(uuid.uuid4()),
        user_id=admin.id,
        key_hash=hash_api_key(svc_raw),
        name="ci-runner",
        key_kind="service",
        org_id=org.id,
        scopes='["agent_runs:write"]',
        service_key_name="ci-runner",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(svc_key)
    await db_session.commit()

    # Service key calling a scoped route should work
    # Use the GET provider route as a canary — it requires require_scope
    # so the service key won't get rejected by get_current_user deny-by-default.
    # But our SSO admin routes use get_current_user not require_scope.
    # The key test: service key on /me should get 403 service_key_not_allowed
    # NOT 403 sso_enforcement_required — meaning the enforcement check was
    # SKIPPED (service keys are categorically exempt).
    resp = await client.get("/api/v1/auth/me", headers=_hdrs(svc_raw))
    assert resp.status_code == 403, resp.text
    body = resp.json()
    # Must be "service_key_not_allowed" — NOT "sso_enforcement_required"
    assert "service_key_not_allowed" in str(body), (
        f"Expected service_key_not_allowed, got: {body}"
    )
    assert "sso_enforcement" not in str(body), (
        f"Service key must not hit SSO enforcement. Got: {body}"
    )


# ---------------------------------------------------------------------------
# 4. OWNER exempt — never locked out
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_owner_exempt_from_enforcement(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Owner with non-sso_minted key under enforcement → ALLOWED."""
    owner, owner_key = await _make_user(db_session, "owner", email="owner@acme.com")
    org, _ = await _setup_enforcing_org(db_session, owner, owner=owner, domain="acme.com")

    # Owner uses a plain (non-SSO) key — should still work
    resp = await client.get(
        f"/api/v1/orgs/{org.id}/sso/provider",
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 200, f"Owner should be exempt. Got: {resp.text}"


# ---------------------------------------------------------------------------
# 5. Break-glass
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_break_glass_grant_allows_admin(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Owner issues break-glass grant → admin's non-sso key now works."""
    owner, owner_key = await _make_user(db_session, "owner", email="owner@acme.com")
    org, _ = await _setup_enforcing_org(db_session, owner, owner=owner, domain="acme.com")

    admin2, admin2_key = await _make_user(db_session, "admin2", email="admin2@acme.com")
    db_session.add(OrgMember(org_id=org.id, user_id=admin2.id, role="admin"))
    await db_session.commit()

    # Admin without SSO key should be rejected
    resp = await client.get("/api/v1/auth/me", headers=_hdrs(admin2_key))
    assert resp.status_code == 403, f"Admin without SSO key should be blocked. Got: {resp.text}"
    assert "sso_enforcement_required" in resp.text

    # Owner issues break-glass
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/sso/break-glass",
        headers=_hdrs(owner_key),
        json={"admin_user_id": admin2.id},
    )
    assert resp.status_code == 200, resp.text
    grant = resp.json()
    assert grant["admin_user_id"] == admin2.id

    # Admin's key now works
    resp = await client.get("/api/v1/auth/me", headers=_hdrs(admin2_key))
    assert resp.status_code == 200, f"Break-glass should allow admin. Got: {resp.text}"


@pytest.mark.asyncio
async def test_break_glass_expired_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Break-glass grant that has expired → admin rejected again."""
    owner, owner_key = await _make_user(db_session, "owner", email="owner@acme.com")
    org, _ = await _setup_enforcing_org(db_session, owner, owner=owner, domain="acme.com")

    admin2, admin2_key = await _make_user(db_session, "admin2", email="admin2@acme.com")
    db_session.add(OrgMember(org_id=org.id, user_id=admin2.id, role="admin"))
    await db_session.commit()

    # Issue a grant that expires in the past
    now = datetime.now(timezone.utc)
    grant = SsoBreakGlassGrant(
        id=f"sbg_{uuid.uuid4().hex[:12]}",
        org_id=org.id,
        admin_user_id=admin2.id,
        issued_by_user_id=owner.id,
        expires_at=now - timedelta(hours=1),  # already expired
        created_at=now,
    )
    db_session.add(grant)
    await db_session.commit()

    # Admin should still be rejected (grant expired)
    resp = await client.get("/api/v1/auth/me", headers=_hdrs(admin2_key))
    assert resp.status_code == 403, f"Expired grant should not help. Got: {resp.text}"
    assert "sso_enforcement_required" in resp.text


@pytest.mark.asyncio
async def test_break_glass_revoked_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Revoked break-glass grant → admin rejected."""
    owner, owner_key = await _make_user(db_session, "owner", email="owner@acme.com")
    org, _ = await _setup_enforcing_org(db_session, owner, owner=owner, domain="acme.com")

    admin2, admin2_key = await _make_user(db_session, "admin2", email="admin2@acme.com")
    db_session.add(OrgMember(org_id=org.id, user_id=admin2.id, role="admin"))
    await db_session.commit()

    # Issue grant
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/sso/break-glass",
        headers=_hdrs(owner_key),
        json={"admin_user_id": admin2.id},
    )
    assert resp.status_code == 200, resp.text
    grant_id = resp.json()["id"]

    # Revoke it
    resp = await client.delete(
        f"/api/v1/orgs/{org.id}/sso/break-glass/{grant_id}",
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 200, resp.text

    # Admin should be rejected again
    resp = await client.get("/api/v1/auth/me", headers=_hdrs(admin2_key))
    assert resp.status_code == 403, f"Revoked grant should not help. Got: {resp.text}"
    assert "sso_enforcement_required" in resp.text


@pytest.mark.asyncio
async def test_break_glass_duplicate_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Second active grant for same admin → 409."""
    owner, owner_key = await _make_user(db_session, "owner", email="owner@acme.com")
    org, _ = await _setup_enforcing_org(db_session, owner, owner=owner, domain="acme.com")

    admin2, _ = await _make_user(db_session, "admin2", email="admin2@acme.com")
    db_session.add(OrgMember(org_id=org.id, user_id=admin2.id, role="admin"))
    await db_session.commit()

    # First grant
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/sso/break-glass",
        headers=_hdrs(owner_key),
        json={"admin_user_id": admin2.id},
    )
    assert resp.status_code == 200, resp.text

    # Second grant for same admin → 409
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/sso/break-glass",
        headers=_hdrs(owner_key),
        json={"admin_user_id": admin2.id},
    )
    assert resp.status_code == 409, resp.text


@pytest.mark.asyncio
async def test_break_glass_owner_only(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Admin (non-owner) cannot issue break-glass grant → 403."""
    admin, admin_key = await _make_user(db_session, "admin", email="admin@acme.com")
    org, _ = await _setup_enforcing_org(db_session, admin, domain="acme.com")

    admin2, _ = await _make_user(db_session, "admin2", email="admin2@acme.com")
    db_session.add(OrgMember(org_id=org.id, user_id=admin2.id, role="admin"))
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/sso/break-glass",
        headers=_hdrs(admin_key),
        json={"admin_user_id": admin2.id},
    )
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# 6. Non-enforced domain → unaffected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_enforced_domain_user_unaffected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """User whose domain is NOT an enforced verified domain → allowed."""
    admin, admin_key = await _make_user(db_session, "admin", email="admin@acme.com")
    org, _ = await _setup_enforcing_org(db_session, admin, domain="acme.com")

    # User with a completely different domain, not in the org
    outsider, outsider_key = await _make_user(db_session, "outsider", email="outsider@other.com")
    # Add them as a member of the org — but their email domain is NOT verified
    db_session.add(OrgMember(org_id=org.id, user_id=outsider.id, role="member"))
    await db_session.commit()

    # This user's domain is not enforced, so they should be unaffected
    resp = await client.get("/api/v1/auth/me", headers=_hdrs(outsider_key))
    assert resp.status_code == 200, (
        f"Non-enforced-domain user should be unaffected. Got: {resp.text}"
    )


# ---------------------------------------------------------------------------
# 7. Enforcement arming authz
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enforcement_arming_admin_allowed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Admin with sso_minted key can flip enforced toggle."""
    admin, admin_key = await _make_user_with_sso_key(db_session, "admin", email="admin@acme.com")
    org, idp = await _setup_enforcing_org(db_session, admin, domain="acme.com")
    # Give the admin an active ExternalIdentity for live-identity check
    now = datetime.now(timezone.utc)
    db_session.add(
        ExternalIdentity(
            id=f"ext_{uuid.uuid4().hex[:12]}",
            user_id=admin.id,
            org_idp_id=idp.id,
            provider_issuer=idp.issuer,
            subject="sub-admin",
            email_at_link="admin@acme.com",
            link_method="jit_provision",
            linked_at=now,
            created_at=now,
        )
    )
    await db_session.commit()

    # Admin with SSO key can disable enforcement
    resp = await client.patch(
        f"/api/v1/orgs/{org.id}/sso/provider/enforcement",
        headers=_hdrs(admin_key),
        json={"enforced": False},
    )
    assert resp.status_code == 200, f"Admin should be able to disable. Got: {resp.text}"

    # Then re-enable
    resp = await client.patch(
        f"/api/v1/orgs/{org.id}/sso/provider/enforcement",
        headers=_hdrs(admin_key),
        json={"enforced": True},
    )
    assert resp.status_code == 200, f"Admin should be able to re-enable. Got: {resp.text}"


@pytest.mark.asyncio
async def test_enforcement_arming_owner_allowed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Owner can flip enforced toggle."""
    owner, owner_key = await _make_user(db_session, "owner", email="owner@acme.com")
    org, _ = await _setup_enforcing_org(db_session, owner, owner=owner, domain="acme.com")

    resp = await client.patch(
        f"/api/v1/orgs/{org.id}/sso/provider/enforcement",
        headers=_hdrs(owner_key),
        json={"enforced": False},
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_enforcement_arming_member_403(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Member cannot flip enforced toggle → 403."""
    admin, admin_key = await _make_user(db_session, "admin", email="admin@acme.com")
    org, _ = await _setup_enforcing_org(db_session, admin, domain="acme.com")

    member, member_key = await _make_user(db_session, "member", email="member@acme.com")
    db_session.add(OrgMember(org_id=org.id, user_id=member.id, role="member"))
    await db_session.commit()

    resp = await client.patch(
        f"/api/v1/orgs/{org.id}/sso/provider/enforcement",
        headers=_hdrs(member_key),
        json={"enforced": False},
    )
    # Member enforced-domain user with non-sso key → enforced first.
    # But the enforcement route itself should reject them.
    # They'll get either 403 enforcement or 403 insufficient_role first.
    assert resp.status_code == 403, f"Member should be denied. Got: {resp.text}"


@pytest.mark.asyncio
async def test_enforcement_arming_requires_verified_domain(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Cannot enable enforcement without at least one verified domain."""
    admin, admin_key = await _make_user(db_session, "admin", email="admin@acme.com")
    org = await _make_org_with_admin(db_session, admin)
    now = datetime.now(timezone.utc)

    # Create an enabled IdP but no verified domain
    idp = OrgIdentityProvider(
        id=f"oidp_{uuid.uuid4().hex[:12]}",
        org_id=org.id,
        protocol="oidc",
        display_name="Test IdP",
        issuer="https://idp.acme.com",
        client_id="test-client",
        client_secret_ref="env:SECRET",
        enabled=True,
        enforced=False,
        discovery_fetched_at=now,
        created_at=now,
        updated_at=now,
    )
    db_session.add(idp)
    await db_session.commit()

    resp = await client.patch(
        f"/api/v1/orgs/{org.id}/sso/provider/enforcement",
        headers=_hdrs(admin_key),
        json={"enforced": True},
    )
    assert resp.status_code == 400, f"Should require verified domain. Got: {resp.text}"


@pytest.mark.asyncio
async def test_enforcement_arming_requires_enabled_provider(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Cannot enable enforcement without an enabled provider."""
    admin, admin_key = await _make_user(db_session, "admin", email="admin@acme.com")
    org = await _make_org_with_admin(db_session, admin)
    now = datetime.now(timezone.utc)

    idp = OrgIdentityProvider(
        id=f"oidp_{uuid.uuid4().hex[:12]}",
        org_id=org.id,
        protocol="oidc",
        display_name="Test IdP",
        issuer="https://idp.acme.com",
        client_id="test-client",
        client_secret_ref="env:SECRET",
        enabled=False,
        enforced=False,
        discovery_fetched_at=now,
        created_at=now,
        updated_at=now,
    )
    db_session.add(idp)
    # Add a verified domain but provider not enabled
    dv = OrgDomainVerification(
        id=f"odv_{uuid.uuid4().hex[:12]}",
        org_id=org.id,
        domain="acme.com",
        verification_token="tok",
        status="verified",
        verified_at=now,
        created_at=now,
    )
    db_session.add(dv)
    await db_session.commit()

    resp = await client.patch(
        f"/api/v1/orgs/{org.id}/sso/provider/enforcement",
        headers=_hdrs(admin_key),
        json={"enforced": True},
    )
    assert resp.status_code == 400, f"Should require enabled provider. Got: {resp.text}"


# ---------------------------------------------------------------------------
# 8. Tier gate — FREE org → 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_free_tier_blocked_on_provider_create(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """FREE tier org cannot create an OIDC provider."""
    admin, admin_key = await _make_user(db_session, "admin", tier="free", email="admin@acme.com")
    org = await _make_org_with_admin(db_session, admin, tier="free")

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
                "client_secret_ref": "env:SECRET",
            },
        )
    assert resp.status_code == 403, f"FREE tier should be blocked. Got: {resp.text}"
    body = resp.json()
    assert "upgrade_required" in str(body) or "feature" in str(body)


@pytest.mark.asyncio
async def test_free_tier_blocked_on_break_glass(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """FREE tier org cannot issue break-glass grants."""
    admin, admin_key = await _make_user(db_session, "admin", tier="free", email="admin@acme.com")
    org = await _make_org_with_admin(db_session, admin, tier="free")
    # Make admin also owner
    member = (
        await db_session.execute(
            select(OrgMember).where(
                OrgMember.org_id == org.id, OrgMember.user_id == admin.id
            )
        )
    ).scalar_one()
    member.role = "owner"
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/sso/break-glass",
        headers=_hdrs(admin_key),
        json={"admin_user_id": admin.id},
    )
    assert resp.status_code == 403, f"FREE tier should be blocked. Got: {resp.text}"


@pytest.mark.asyncio
async def test_starter_tier_allowed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Starter tier org CAN create an OIDC provider."""
    admin, admin_key = await _make_user(db_session, "admin", tier="starter", email="admin@acme.com")
    org = await _make_org_with_admin(db_session, admin, tier="starter")

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
                "client_secret_ref": "k8s:ns/sec/key",
            },
        )
    assert resp.status_code == 200, f"Starter tier should be allowed. Got: {resp.text}"


# ---------------------------------------------------------------------------
# 9. Deprovision — identities deactivated + sso_minted keys revoked
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deprovision_deactivates_identity_and_revokes_keys(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Removing a member deactivates ExternalIdentity + revokes sso_minted keys."""
    owner, owner_key = await _make_user(db_session, "owner", email="owner@acme.com")
    org, idp = await _setup_enforcing_org(db_session, owner, owner=owner, domain="acme.com")

    member, member_key = await _make_user_with_sso_key(db_session, "member", email="member@acme.com")
    db_session.add(OrgMember(org_id=org.id, user_id=member.id, role="member"))
    now = datetime.now(timezone.utc)
    ext = ExternalIdentity(
        id=f"ext_{uuid.uuid4().hex[:12]}",
        user_id=member.id,
        org_idp_id=idp.id,
        provider_issuer=idp.issuer,
        subject="sub-member",
        email_at_link="member@acme.com",
        link_method="jit_provision",
        linked_at=now,
        created_at=now,
    )
    db_session.add(ext)
    await db_session.commit()

    ext_id = ext.id

    # Owner removes the member
    resp = await client.delete(
        f"/api/v1/orgs/{org.id}/members/{member.id}",
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 200, f"Member removal failed: {resp.text}"

    # Verify ExternalIdentity was deactivated
    await db_session.refresh(ext)
    assert ext.deactivated_at is not None, "ExternalIdentity should be deactivated"

    # Verify sso_minted keys were revoked
    key_result = await db_session.execute(
        select(ApiKey).where(
            ApiKey.user_id == member.id,
            ApiKey.sso_minted.is_(True),
        )
    )
    keys = key_result.scalars().all()
    assert len(keys) > 0, "Should have at least one sso_minted key"
    for k in keys:
        assert k.revoked_at is not None, f"Key {k.id} should be revoked"
        assert k.revoke_reason == "deprovisioned", f"Key {k.id} revoke_reason: {k.revoke_reason}"


# ---------------------------------------------------------------------------
# 10. FAIL-OPEN — enforcement eval error → ALLOWED
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enforcement_fail_open_on_query_error(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """If the enforcement query raises unexpectedly, fail-open ALLOWS.

    Tests the REAL _sso_enforcement_check function in isolation with a mock
    db that raises, verifying the fail-open catch block works.
    """
    from sessionfs.server.auth.dependencies import AuthContext, _sso_enforcement_check

    # Create a real User + AuthContext
    user, _ = await _make_user(db_session, "testuser", email="user@acme.com")
    ctx = AuthContext(
        user=user,
        api_key_id="test-key-id",
        key_kind="user",
        sso_minted=False,
    )

    # Mock DB that raises on execute (simulating transient DB error)
    mock_db = AsyncMock()
    mock_db.execute.side_effect = RuntimeError("simulated transient error")

    # Should NOT raise — the fail-open catch swallows the error
    try:
        await _sso_enforcement_check(ctx, mock_db)
    except Exception as exc:
        pytest.fail(f"_sso_enforcement_check must fail-open, but raised: {exc}")

    # Service keys should also be unaffected (immediate return)
    ctx_svc = AuthContext(
        user=user,
        api_key_id="svc-key-id",
        key_kind="service",
        sso_minted=False,
    )
    # Even with a raising DB, service key path returns immediately before
    # any DB call — should not raise
    try:
        await _sso_enforcement_check(ctx_svc, mock_db)
    except Exception as exc:
        pytest.fail(f"Service key path must not raise: {exc}")


# ---------------------------------------------------------------------------
# 11. Edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_no_email_domain_unaffected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """User with no @ in email → enforcement skipped (no domain to match)."""
    admin, admin_key = await _make_user(db_session, "admin", email="admin@acme.com")
    org, _ = await _setup_enforcing_org(db_session, admin, domain="acme.com")

    # User with no domain-part in email (edge case)
    no_domain_user, no_domain_key = await _make_user(db_session, "nodomain", email="noemail")
    db_session.add(OrgMember(org_id=org.id, user_id=no_domain_user.id, role="member"))
    await db_session.commit()

    resp = await client.get("/api/v1/auth/me", headers=_hdrs(no_domain_key))
    # Should not crash — enforcement skipped because no domain
    assert resp.status_code == 200, f"Got: {resp.text}"


@pytest.mark.asyncio
async def test_enforcement_404_for_no_provider(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Enforcement toggle on org with no provider → 404."""
    admin, admin_key = await _make_user(db_session, "admin", email="admin@acme.com")
    org = await _make_org_with_admin(db_session, admin)

    resp = await client.patch(
        f"/api/v1/orgs/{org.id}/sso/provider/enforcement",
        headers=_hdrs(admin_key),
        json={"enforced": True},
    )
    assert resp.status_code == 404, f"Should be 404. Got: {resp.text}"


@pytest.mark.asyncio
async def test_list_active_break_glass_grants(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """List only active (non-revoked, non-expired) break-glass grants."""
    owner, owner_key = await _make_user(db_session, "owner", email="owner@acme.com")
    org, _ = await _setup_enforcing_org(db_session, owner, owner=owner, domain="acme.com")

    admin2, _ = await _make_user(db_session, "admin2", email="admin2@acme.com")
    db_session.add(OrgMember(org_id=org.id, user_id=admin2.id, role="admin"))
    await db_session.commit()

    # Issue one grant
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/sso/break-glass",
        headers=_hdrs(owner_key),
        json={"admin_user_id": admin2.id},
    )
    assert resp.status_code == 200, resp.text

    # List should include it
    resp = await client.get(
        f"/api/v1/orgs/{org.id}/sso/break-glass",
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 200, resp.text
    grants = resp.json()
    assert len(grants) >= 1
    assert any(g["admin_user_id"] == admin2.id for g in grants)


@pytest.mark.asyncio
async def test_multi_org_admin_not_500_on_sso_route(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Sentinel F1 regression: a user who is a member of MORE THAN ONE org
    must not 500 on an SSO admin route. The tier gate resolves the PATH
    org's entitlement (uq_org_members_org_user → exactly one membership row
    per org), not the caller's ambient membership (which would raise
    MultipleResultsFound for a multi-org user)."""
    admin, admin_key = await _make_user(
        db_session, "admin", tier="starter", email="admin@acme.com"
    )
    # Path org (paid) — the one we configure SSO on.
    org = await _make_org_with_admin(db_session, admin, tier="starter", name="Primary")
    # A SECOND org the same admin belongs to → multi-org membership.
    other = await _make_org_with_admin(db_session, admin, tier="team", name="Secondary")
    assert other.id != org.id

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
                "client_secret_ref": "env:SECRET",
            },
        )
    # The whole point: NOT a 500. The paid path org is allowed → 200.
    assert resp.status_code == 200, f"multi-org admin must not 500. Got: {resp.text}"
