"""GitHub #59 (v0.17.0) — auto-join verified-email users by domain.

Covers:
  - settings PUT rejects an unverified domain (422, names the domain)
  - settings PUT accepts a verified domain (normalized + round-trips)
  - settings PUT clears the list; non-empty requires a paid plan
  - matching-domain user auto-joins as `member` (never admin/owner) + audit
  - non-matching domain / already-a-member / unverified-domain → no-op
  - seat cap full → skipped + `auto_join_skipped_seat_cap` audit, no error
  - previously-removed user is not silently re-added
  - lost entitlement (free) stops auto-join at join time
"""

from __future__ import annotations

import json
import secrets
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    Entitlement,
    OrgAuditEvent,
    OrgDomainVerification,
    OrgMember,
    Organization,
    User,
)
from sessionfs.server.services.auto_join import maybe_auto_join


async def _make_user(db: AsyncSession, email: str, verified: bool = True) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        display_name="Test",
        tier="team",
        email_verified=verified,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_org(db: AsyncSession, tier: str = "team", seats_limit: int = 10) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Corp",
        slug=f"c-{uuid.uuid4().hex[:6]}",
        tier=tier,
        seats_limit=seats_limit,
    )
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


async def _verified_domain(db: AsyncSession, org: Organization, domain: str = "corp.com") -> None:
    db.add(
        OrgDomainVerification(
            id=f"odv_{secrets.token_hex(12)}",
            org_id=org.id,
            domain=domain,
            method="dns_txt",
            verification_token=secrets.token_urlsafe(16),
            status="verified",
            verified_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


def _set_auto_join(org: Organization, domains: list[str]) -> None:
    org.settings = json.dumps({"general": {"auto_join_domains": domains}})


async def _members(db: AsyncSession, org_id: str) -> list[OrgMember]:
    return (
        await db.execute(select(OrgMember).where(OrgMember.org_id == org_id))
    ).scalars().all()


async def _audit(db: AsyncSession, org_id: str, event_type: str) -> list[OrgAuditEvent]:
    return (
        await db.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_id == org_id,
                OrgAuditEvent.event_type == event_type,
            )
        )
    ).scalars().all()


# ── settings PUT ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_settings_put_rejects_unverified_domain(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
):
    org = await _make_org(db_session)
    await _verified_domain(db_session, org, "corp.com")
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=auth_headers,
        json={"auto_join_domains": ["other.com"]},
    )
    assert resp.status_code == 422, resp.text
    err = resp.json()["error"]
    assert err["code"] == "unverified_domain"
    assert err["details"]["domain"] == "other.com"


@pytest.mark.asyncio
async def test_settings_put_accepts_verified_domain_normalized(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
):
    org = await _make_org(db_session)
    await _verified_domain(db_session, org, "corp.com")
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=auth_headers,
        json={"auto_join_domains": ["Corp.COM", "corp.com"]},  # dup + case
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["auto_join_domains"] == ["corp.com"]

    resp2 = await client.get(f"/api/v1/orgs/{org.id}/settings", headers=auth_headers)
    assert resp2.json()["auto_join_domains"] == ["corp.com"]


@pytest.mark.asyncio
async def test_settings_put_empty_clears_and_needs_no_gate(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
):
    org = await _make_org(db_session)
    await _verified_domain(db_session, org, "corp.com")
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()
    await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=auth_headers,
        json={"auto_join_domains": ["corp.com"]},
    )

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=auth_headers,
        json={"auto_join_domains": []},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["auto_join_domains"] == []


@pytest.mark.asyncio
async def test_settings_put_without_the_field_keeps_auto_join_domains(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
):
    org = await _make_org(db_session)
    await _verified_domain(db_session, org, "corp.com")
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()
    await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=auth_headers,
        json={"auto_join_domains": ["corp.com"]},
    )

    # A client that only edits KB settings (e.g. an older dashboard).
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=auth_headers,
        json={"kb_retention_days": 30},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["auto_join_domains"] == ["corp.com"]


@pytest.mark.asyncio
async def test_settings_put_tolerates_a_stored_domain_that_lost_verification(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
):
    """A domain stored earlier that is no longer verified must not block
    re-saving the same list (or removing it); only additions are validated."""
    org = await _make_org(db_session)
    _set_auto_join(org, ["gone.com"])  # stored; no verified domain row any more
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()

    same = await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=auth_headers,
        json={"auto_join_domains": ["gone.com"], "kb_retention_days": 30},
    )
    assert same.status_code == 200, same.text

    added = await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=auth_headers,
        json={"auto_join_domains": ["gone.com", "new.com"]},
    )
    assert added.status_code == 422, added.text

    removed = await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=auth_headers,
        json={"auto_join_domains": []},
    )
    assert removed.status_code == 200, removed.text
    assert removed.json()["auto_join_domains"] == []


@pytest.mark.asyncio
async def test_settings_put_nonempty_requires_paid_plan(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
):
    org = await _make_org(db_session, tier="free")
    await _verified_domain(db_session, org, "corp.com")
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=auth_headers,
        json={"auto_join_domains": ["corp.com"]},
    )
    assert resp.status_code == 403, resp.text
    err = resp.json()["error"]
    assert err["details"]["error"] == "upgrade_required"


# ── maybe_auto_join ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_auto_join_matching_domain_as_member(db_session: AsyncSession):
    org = await _make_org(db_session)
    await _verified_domain(db_session, org, "corp.com")
    _set_auto_join(org, ["corp.com"])
    await db_session.commit()

    user = await _make_user(db_session, "alice@corp.com")
    await maybe_auto_join(user, db_session)

    members = await _members(db_session, org.id)
    assert len(members) == 1
    assert members[0].user_id == user.id
    assert members[0].role == "member"  # never admin/owner

    joined = await _audit(db_session, org.id, "member_auto_joined")
    assert len(joined) == 1
    assert joined[0].target_id == user.id
    assert "corp.com" in joined[0].after


@pytest.mark.asyncio
async def test_auto_join_non_matching_domain_noop(db_session: AsyncSession):
    org = await _make_org(db_session)
    await _verified_domain(db_session, org, "corp.com")
    _set_auto_join(org, ["corp.com"])
    await db_session.commit()

    user = await _make_user(db_session, "alice@other.com")
    await maybe_auto_join(user, db_session)

    assert await _members(db_session, org.id) == []


@pytest.mark.asyncio
async def test_auto_join_unverified_domain_noop(db_session: AsyncSession):
    org = await _make_org(db_session)
    # No verified domain row — only auto_join_domains configured.
    _set_auto_join(org, ["corp.com"])
    await db_session.commit()

    user = await _make_user(db_session, "alice@corp.com")
    await maybe_auto_join(user, db_session)

    assert await _members(db_session, org.id) == []


@pytest.mark.asyncio
async def test_auto_join_user_already_in_any_org_noop(db_session: AsyncSession):
    other = await _make_org(db_session)

    org = await _make_org(db_session)
    await _verified_domain(db_session, org, "corp.com")
    _set_auto_join(org, ["corp.com"])
    await db_session.commit()

    user = await _make_user(db_session, "alice@corp.com")
    # User already belongs to `other`.
    other_member = OrgMember(org_id=other.id, user_id=user.id, role="member")
    db_session.add(other_member)
    await db_session.commit()

    await maybe_auto_join(user, db_session)

    assert await _members(db_session, org.id) == []


@pytest.mark.asyncio
async def test_auto_join_seat_cap_full_skipped(db_session: AsyncSession):
    org = await _make_org(db_session, seats_limit=1)
    await _verified_domain(db_session, org, "corp.com")
    _set_auto_join(org, ["corp.com"])

    existing = await _make_user(db_session, "bob@corp.com")
    db_session.add(OrgMember(org_id=org.id, user_id=existing.id, role="member"))
    await db_session.commit()

    user = await _make_user(db_session, "alice@corp.com")
    await maybe_auto_join(user, db_session)

    members = await _members(db_session, org.id)
    assert len(members) == 1  # seat cap not exceeded

    skipped = await _audit(db_session, org.id, "auto_join_skipped_seat_cap")
    assert len(skipped) == 1
    assert skipped[0].target_id == user.id
    assert await _audit(db_session, org.id, "member_auto_joined") == []


@pytest.mark.asyncio
async def test_auto_join_previously_removed_not_readded(db_session: AsyncSession):
    org = await _make_org(db_session)
    await _verified_domain(db_session, org, "corp.com")
    _set_auto_join(org, ["corp.com"])
    await db_session.commit()

    user = await _make_user(db_session, "alice@corp.com")
    db_session.add(
        OrgAuditEvent(
            id=f"oae_{secrets.token_hex(12)}",
            org_id=org.id,
            org_name_snapshot="Corp",
            event_type="member_removed",
            target_type="user",
            target_id=user.id,
        )
    )
    await db_session.commit()

    await maybe_auto_join(user, db_session)

    assert await _members(db_session, org.id) == []


@pytest.mark.asyncio
async def test_auto_join_lost_entitlement_no_join(db_session: AsyncSession):
    # org.tier cache still says "team", but the active entitlement is "free"
    # — join-time resolution must honor the entitlement and stop the join.
    org = await _make_org(db_session, tier="team")
    await _verified_domain(db_session, org, "corp.com")
    _set_auto_join(org, ["corp.com"])
    db_session.add(
        Entitlement(
            owner_type="org",
            owner_id=org.id,
            source="manual",
            tier="free",
            status="active",
        )
    )
    await db_session.commit()

    user = await _make_user(db_session, "alice@corp.com")
    await maybe_auto_join(user, db_session)

    assert await _members(db_session, org.id) == []


@pytest.mark.asyncio
async def test_auto_join_not_verified_email_noop(db_session: AsyncSession):
    org = await _make_org(db_session)
    await _verified_domain(db_session, org, "corp.com")
    _set_auto_join(org, ["corp.com"])
    await db_session.commit()

    user = await _make_user(db_session, "alice@corp.com", verified=False)
    await maybe_auto_join(user, db_session)

    assert await _members(db_session, org.id) == []


# ── wiring: only PROVEN email ownership triggers auto-join ───────────────


@pytest.mark.asyncio
async def test_signup_without_email_verification_never_auto_joins(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    # With verification disabled, signup marks the address verified without
    # the user proving ownership — anyone could claim name@corp.com. Auto-join
    # must not fire on that path.
    monkeypatch.setenv("SFS_REQUIRE_EMAIL_VERIFICATION", "false")
    org = await _make_org(db_session)
    await _verified_domain(db_session, org, "corp.com")
    _set_auto_join(org, ["corp.com"])
    await db_session.commit()

    resp = await client.post("/api/v1/auth/signup", json={"email": "mallory@corp.com"})
    assert resp.status_code == 201, resp.text

    assert await _members(db_session, org.id) == []
    assert await _audit(db_session, org.id, "member_auto_joined") == []


def _verification_link_token(user: User, secret: str, email: str | None = None) -> str:
    import jwt as pyjwt
    from datetime import timedelta

    return pyjwt.encode(
        {
            "user_id": user.id,
            "email": email if email is not None else user.email,
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        secret,
        algorithm="HS256",
    )


async def _org_auto_joining(db: AsyncSession, domain: str = "corp.com") -> Organization:
    org = await _make_org(db)
    await _verified_domain(db, org, domain)
    _set_auto_join(org, [domain])
    await db.commit()
    return org


_STRONG_SECRET = "x" * 48


@pytest.mark.asyncio
async def test_verification_link_auto_joins(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    from sessionfs.server.routes import auth as auth_routes

    monkeypatch.setattr(auth_routes, "SFS_VERIFICATION_SECRET", _STRONG_SECRET)
    org = await _org_auto_joining(db_session)
    user = await _make_user(db_session, "bob@corp.com", verified=False)

    resp = await client.get(
        "/api/v1/auth/verify",
        params={"token": _verification_link_token(user, _STRONG_SECRET)},
    )
    assert resp.status_code == 200, resp.text

    members = await _members(db_session, org.id)
    assert [(m.user_id, m.role) for m in members] == [(user.id, "member")]


@pytest.mark.asyncio
async def test_forgeable_default_secret_never_auto_joins(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    # With the shipped default secret anyone can sign a verification link for
    # attacker@corp.com, so the link must not be trusted for org membership.
    from sessionfs.server.routes import auth as auth_routes

    monkeypatch.setattr(auth_routes, "SFS_VERIFICATION_SECRET", "dev-verification-secret")
    org = await _org_auto_joining(db_session)
    user = await _make_user(db_session, "attacker@corp.com", verified=False)

    resp = await client.get(
        "/api/v1/auth/verify",
        params={"token": _verification_link_token(user, "dev-verification-secret")},
    )
    assert resp.status_code == 200, resp.text  # the account itself is verified
    assert await _members(db_session, org.id) == []


@pytest.mark.asyncio
async def test_verification_link_for_another_address_is_refused(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    from sessionfs.server.routes import auth as auth_routes

    monkeypatch.setattr(auth_routes, "SFS_VERIFICATION_SECRET", _STRONG_SECRET)
    org = await _org_auto_joining(db_session)
    user = await _make_user(db_session, "dave@corp.com", verified=False)

    resp = await client.get(
        "/api/v1/auth/verify",
        params={
            "token": _verification_link_token(
                user, _STRONG_SECRET, email="someone-else@corp.com"
            )
        },
    )
    assert resp.status_code == 400
    assert await _members(db_session, org.id) == []


@pytest.mark.asyncio
async def test_member_removal_audit_snapshots_the_org_name(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers: dict
):
    org = await _make_org(db_session)
    target = await _make_user(db_session, "carol@corp.com")
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    db_session.add(OrgMember(org_id=org.id, user_id=target.id, role="member"))
    await db_session.commit()

    resp = await client.delete(
        f"/api/v1/orgs/{org.id}/members/{target.id}", headers=auth_headers
    )
    assert resp.status_code in (200, 204), resp.text
    removed = await _audit(db_session, org.id, "member_removed")
    assert len(removed) == 1
    assert removed[0].org_name_snapshot == "Corp"


# ── members removed before removal audits existed ─────────────────────────


@pytest.mark.asyncio
async def test_auto_join_skips_users_with_any_earlier_invite(db_session: AsyncSession):
    """Removals made before this release left no member_removed audit. An
    accepted (past member), declined or pending invite for the same org and
    email is evidence of an earlier relationship, so auto-join stays out."""
    from datetime import timedelta

    from sessionfs.server.db.models import OrgInvite

    for state in ("accepted", "declined", "pending"):
        org = await _make_org(db_session)
        await _verified_domain(db_session, org, f"{state}.corp.com")
        _set_auto_join(org, [f"{state}.corp.com"])
        admin = await _make_user(db_session, f"admin@{state}.corp.com")
        now = datetime.now(timezone.utc)
        db_session.add(
            OrgInvite(
                id=f"inv_{secrets.token_hex(8)}",
                org_id=org.id,
                email=f"Former@{state}.corp.com",
                role="member",
                invited_by=admin.id,
                expires_at=now + timedelta(days=7),
                accepted_at=now if state == "accepted" else None,
                declined_at=now if state == "declined" else None,
            )
        )
        await db_session.commit()

        user = await _make_user(db_session, f"former@{state}.corp.com")
        await maybe_auto_join(user, db_session)
        assert await _members(db_session, org.id) == [], state


@pytest.mark.asyncio
async def test_auto_join_skips_users_with_an_sso_identity_for_the_org(
    db_session: AsyncSession,
):
    from sessionfs.server.db.models import ExternalIdentity, OrgIdentityProvider

    org = await _make_org(db_session)
    await _verified_domain(db_session, org, "corp.com")
    _set_auto_join(org, ["corp.com"])
    idp = OrgIdentityProvider(
        id=f"oidp_{secrets.token_hex(12)}",
        org_id=org.id,
        protocol="oidc",
        display_name="Okta",
        issuer="https://corp.okta.com",
        client_id="cid",
        client_secret_ref="env:X",
        allowed_scopes='["openid","email","profile"]',
        enabled=True,
    )
    db_session.add(idp)
    user = await _make_user(db_session, "sso-former@corp.com")
    now = datetime.now(timezone.utc)
    db_session.add(
        ExternalIdentity(
            id=f"ext_{uuid.uuid4().hex[:12]}",
            user_id=user.id,
            org_idp_id=idp.id,
            provider_issuer=idp.issuer,
            subject="sub-1",
            email_at_link=user.email,
            link_method="jit_provision",
            linked_at=now,
            created_at=now,
            deactivated_at=now,
        )
    )
    await db_session.commit()

    await maybe_auto_join(user, db_session)
    assert await _members(db_session, org.id) == []


@pytest.mark.asyncio
async def test_auto_join_never_adds_a_deactivated_user(db_session: AsyncSession):
    org = await _make_org(db_session)
    await _verified_domain(db_session, org, "corp.com")
    _set_auto_join(org, ["corp.com"])
    await db_session.commit()
    user = await _make_user(db_session, "gone@corp.com")
    user.is_active = False
    await db_session.commit()

    await maybe_auto_join(user, db_session)
    assert await _members(db_session, org.id) == []


@pytest.mark.asyncio
async def test_seat_cap_skip_is_audited_once_per_user(db_session: AsyncSession):
    org = await _make_org(db_session, seats_limit=1)
    await _verified_domain(db_session, org, "corp.com")
    _set_auto_join(org, ["corp.com"])
    existing = await _make_user(db_session, "bob@corp.com")
    db_session.add(OrgMember(org_id=org.id, user_id=existing.id, role="member"))
    await db_session.commit()

    user = await _make_user(db_session, "alice@corp.com")
    for _ in range(3):
        await maybe_auto_join(user, db_session)
    assert len(await _audit(db_session, org.id, "auto_join_skipped_seat_cap")) == 1


@pytest.mark.asyncio
async def test_replaying_a_verification_link_does_not_rerun_auto_join(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    from sessionfs.server.routes import auth as auth_routes

    monkeypatch.setattr(auth_routes, "SFS_VERIFICATION_SECRET", _STRONG_SECRET)
    calls: list[str] = []

    async def counting_auto_join(user, db):
        calls.append(user.id)

    monkeypatch.setattr(auth_routes, "maybe_auto_join", counting_auto_join)
    await _org_auto_joining(db_session)
    user = await _make_user(db_session, "eve@corp.com", verified=False)
    token = _verification_link_token(user, _STRONG_SECRET)

    for _ in range(3):
        resp = await client.get("/api/v1/auth/verify", params={"token": token})
        assert resp.status_code == 200, resp.text
    assert calls == [user.id]


@pytest.mark.asyncio
async def test_auto_join_rereads_the_domain_setting_under_the_lock(
    db_session: AsyncSession, monkeypatch
):
    """If the domain is removed from auto-join after the unlocked pre-check
    but before the lock, the fresh read under the lock must win."""
    from sessionfs.server.services import auto_join as auto_join_module

    org = await _make_org(db_session)
    await _verified_domain(db_session, org, "corp.com")
    _set_auto_join(org, ["corp.com"])
    await db_session.commit()
    user = await _make_user(db_session, "late@corp.com")

    real = auto_join_module.read_auto_join_domains
    calls = {"n": 0}

    def disabled_after_first_read(o):
        calls["n"] += 1
        return real(o) if calls["n"] == 1 else []

    monkeypatch.setattr(auto_join_module, "read_auto_join_domains", disabled_after_first_read)
    await maybe_auto_join(user, db_session)
    assert calls["n"] >= 2
    assert await _members(db_session, org.id) == []
