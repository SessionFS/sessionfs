"""tk_f503ce5c24c54040 — org-admin CRUD surface for the trusted_reviewers
registry (routes/trusted_reviewers.py).

This registry grants verdict-trust authority — a row here decides whose
review verdicts the work-queue stop oracle (is_registered_trusted_reviewer)
counts as authoritative. These tests are the regression bar for the gating,
identity-AND-scope + bound-identity validation, soft-revoke semantics, and
the OrgAuditEvent audit trail.

Properties under test:
1. Org admin can register a service_key_id reviewer (org-wide + project-scoped).
2. Non-admin org member → 403; non-member → 404 (existence-hiding).
3. Cross-org: admin of org A cannot register/list/revoke on org B → 404.
4. Identity-AND-scope: missing identity → 422 (not 500 from the DB CHECK).
5. Bound-identity: a service_key_id from another org / a non-member user_id →
   rejected (422); a project_id outside the org → 422.
6. Revoke deactivates (is_active=false + revoked_at), does NOT hard-delete;
   a revoked reviewer is no longer trusted by is_registered_trusted_reviewer.
7. OrgAuditEvent rows written on register + revoke.
8. Round-trip: register a service key for 'codex-reviewer' →
   is_registered_trusted_reviewer returns True for that key on that scope.
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
    OrgMember,
    Organization,
    Project,
    TrustedReviewer,
    User,
)
from sessionfs.server.routes.tickets import is_registered_trusted_reviewer


# ── helpers ──


def _hdrs(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


async def _make_user_with_key(
    db_session: AsyncSession, email: str, tier: str = "team"
) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        display_name=email.split("@")[0],
        tier=tier,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.flush()
    raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"user-key-{email}",
            is_active=True,
            key_kind="user",
            scopes=json.dumps(["*"]),
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    return user, raw


async def _make_org_with_admin(
    db_session: AsyncSession,
) -> tuple[Organization, User, str]:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name=f"Org-{uuid.uuid4().hex[:6]}",
        slug=f"o-{uuid.uuid4().hex[:6]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(org)
    await db_session.flush()
    admin_user, raw = await _make_user_with_key(
        db_session, f"admin-{uuid.uuid4().hex[:6]}@x.com"
    )
    db_session.add(
        OrgMember(
            org_id=org.id,
            user_id=admin_user.id,
            role="admin",
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    return org, admin_user, raw


async def _add_member(
    db_session: AsyncSession, org: Organization, role: str = "member"
) -> tuple[User, str]:
    user, raw = await _make_user_with_key(
        db_session, f"{role}-{uuid.uuid4().hex[:6]}@x.com"
    )
    db_session.add(
        OrgMember(
            org_id=org.id,
            user_id=user.id,
            role=role,
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    return user, raw


async def _make_service_key(
    db_session: AsyncSession, org_id: str, minter: User
) -> str:
    """Insert a service key in the given org. Returns its id."""
    key_id = str(uuid.uuid4())
    db_session.add(
        ApiKey(
            id=key_id,
            user_id=minter.id,
            key_hash=hash_api_key(generate_api_key()),
            name="codex-reviewer-key",
            is_active=True,
            key_kind="service",
            org_id=org_id,
            scopes=json.dumps(["tickets:write"]),
            created_by_user_id=minter.id,
            service_key_name="codex-reviewer-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    return key_id


async def _make_project(
    db_session: AsyncSession, org_id: str | None
) -> Project:
    proj = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name=f"P-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"github.com/x/{uuid.uuid4().hex[:8]}",
        owner_id=str(uuid.uuid4()),  # owner irrelevant for these tests
        org_id=org_id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(proj)
    await db_session.commit()
    return proj


async def _count_audit(
    db_session: AsyncSession, org_id: str, event_type: str
) -> int:
    rows = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_id == org_id,
                OrgAuditEvent.event_type == event_type,
            )
        )
    ).scalars().all()
    return len(rows)


# ── Property 1: register a service-key reviewer (org-wide + project) ──


@pytest.mark.asyncio
async def test_admin_registers_service_key_orgwide(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, admin_raw = await _make_org_with_admin(db_session)
    key_id = await _make_service_key(db_session, org.id, admin)

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/trusted-reviewers",
        headers=_hdrs(admin_raw),
        json={"service_key_id": key_id, "reviewer_persona": "codex-reviewer"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["service_key_id"] == key_id
    assert body["reviewer_persona"] == "codex-reviewer"
    assert body["is_active"] is True
    # org-wide → org_id set, project_id NULL.
    assert body["org_id"] == org.id
    assert body["project_id"] is None
    assert body["created_by_user_id"] == admin.id
    assert body["revoked_at"] is None


@pytest.mark.asyncio
async def test_admin_registers_service_key_project_scoped(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, admin_raw = await _make_org_with_admin(db_session)
    key_id = await _make_service_key(db_session, org.id, admin)
    proj = await _make_project(db_session, org.id)

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/trusted-reviewers",
        headers=_hdrs(admin_raw),
        json={"service_key_id": key_id, "project_id": proj.id},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # project-scoped → project_id set, org_id NULL.
    assert body["project_id"] == proj.id
    assert body["org_id"] is None


@pytest.mark.asyncio
async def test_owner_can_register(client: AsyncClient, db_session: AsyncSession):
    org, admin, _ = await _make_org_with_admin(db_session)
    owner, owner_raw = await _add_member(db_session, org, role="owner")
    key_id = await _make_service_key(db_session, org.id, owner)
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/trusted-reviewers",
        headers=_hdrs(owner_raw),
        json={"service_key_id": key_id},
    )
    assert resp.status_code == 201, resp.text


# ── Property 2: non-admin member 403, non-member 404 ──


@pytest.mark.asyncio
async def test_non_admin_member_forbidden(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _ = await _make_org_with_admin(db_session)
    member, member_raw = await _add_member(db_session, org, role="member")
    key_id = await _make_service_key(db_session, org.id, admin)
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/trusted-reviewers",
        headers=_hdrs(member_raw),
        json={"service_key_id": key_id},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_non_member_gets_404(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _ = await _make_org_with_admin(db_session)
    # An admin of a *different* org (so they have team_management) hitting
    # this org → 404 existence-hiding. Use a non-member with their own org.
    other_org, _other_admin, other_raw = await _make_org_with_admin(db_session)
    key_id = await _make_service_key(db_session, org.id, admin)
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/trusted-reviewers",
        headers=_hdrs(other_raw),
        json={"service_key_id": key_id},
    )
    assert resp.status_code == 404, resp.text


# ── Property 3: cross-org isolation on list + revoke ──


@pytest.mark.asyncio
async def test_cross_org_list_and_revoke_404(
    client: AsyncClient, db_session: AsyncSession
):
    org_a, admin_a, raw_a = await _make_org_with_admin(db_session)
    org_b, admin_b, raw_b = await _make_org_with_admin(db_session)
    key_b = await _make_service_key(db_session, org_b.id, admin_b)

    # Register in org B as B's admin.
    reg = await client.post(
        f"/api/v1/orgs/{org_b.id}/trusted-reviewers",
        headers=_hdrs(raw_b),
        json={"service_key_id": key_b},
    )
    assert reg.status_code == 201
    rid = reg.json()["id"]

    # Admin A listing org B → 404 (not a member of B).
    lst = await client.get(
        f"/api/v1/orgs/{org_b.id}/trusted-reviewers", headers=_hdrs(raw_a)
    )
    assert lst.status_code == 404

    # Admin A revoking B's reviewer → 404.
    rev = await client.request(
        "DELETE",
        f"/api/v1/orgs/{org_b.id}/trusted-reviewers/{rid}",
        headers=_hdrs(raw_a),
    )
    assert rev.status_code == 404

    # The row is untouched.
    row = (
        await db_session.execute(
            select(TrustedReviewer).where(TrustedReviewer.id == rid)
        )
    ).scalar_one()
    assert row.is_active is True


# ── Property 4: identity-AND-scope validation → 422, never 500 ──


@pytest.mark.asyncio
async def test_missing_identity_422(
    client: AsyncClient, db_session: AsyncSession
):
    org, _admin, admin_raw = await _make_org_with_admin(db_session)
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/trusted-reviewers",
        headers=_hdrs(admin_raw),
        json={"reviewer_persona": "codex-reviewer"},  # no identity
    )
    assert resp.status_code == 422, resp.text


# ── Property 5: bound-identity validation ──


@pytest.mark.asyncio
async def test_service_key_from_other_org_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    org_a, admin_a, raw_a = await _make_org_with_admin(db_session)
    org_b, admin_b, _raw_b = await _make_org_with_admin(db_session)
    key_b = await _make_service_key(db_session, org_b.id, admin_b)

    resp = await client.post(
        f"/api/v1/orgs/{org_a.id}/trusted-reviewers",
        headers=_hdrs(raw_a),
        json={"service_key_id": key_b},
    )
    assert resp.status_code == 422, resp.text
    assert "service_key_not_in_org" in resp.text


@pytest.mark.asyncio
async def test_non_member_user_id_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    org, _admin, admin_raw = await _make_org_with_admin(db_session)
    stranger, _ = await _make_user_with_key(
        db_session, f"stranger-{uuid.uuid4().hex[:6]}@x.com"
    )
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/trusted-reviewers",
        headers=_hdrs(admin_raw),
        json={"user_id": stranger.id},
    )
    assert resp.status_code == 422, resp.text
    assert "user_not_org_member" in resp.text


@pytest.mark.asyncio
async def test_member_user_id_accepted(
    client: AsyncClient, db_session: AsyncSession
):
    org, _admin, admin_raw = await _make_org_with_admin(db_session)
    member, _ = await _add_member(db_session, org, role="member")
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/trusted-reviewers",
        headers=_hdrs(admin_raw),
        json={"user_id": member.id},
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["user_id"] == member.id


@pytest.mark.asyncio
async def test_project_outside_org_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, admin_raw = await _make_org_with_admin(db_session)
    key_id = await _make_service_key(db_session, org.id, admin)
    foreign_proj = await _make_project(db_session, org_id=None)  # personal
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/trusted-reviewers",
        headers=_hdrs(admin_raw),
        json={"service_key_id": key_id, "project_id": foreign_proj.id},
    )
    assert resp.status_code == 422, resp.text
    assert "project_not_in_org" in resp.text


# ── Property 6: revoke is soft delete + stops trust ──


@pytest.mark.asyncio
async def test_revoke_soft_deletes(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, admin_raw = await _make_org_with_admin(db_session)
    key_id = await _make_service_key(db_session, org.id, admin)
    reg = await client.post(
        f"/api/v1/orgs/{org.id}/trusted-reviewers",
        headers=_hdrs(admin_raw),
        json={"service_key_id": key_id},
    )
    rid = reg.json()["id"]

    rev = await client.request(
        "DELETE",
        f"/api/v1/orgs/{org.id}/trusted-reviewers/{rid}",
        headers=_hdrs(admin_raw),
        json={"reason": "rotating reviewer"},
    )
    assert rev.status_code == 204, rev.text

    # Row still exists (soft delete), is_active=false, revoked_at set.
    row = (
        await db_session.execute(
            select(TrustedReviewer).where(TrustedReviewer.id == rid)
        )
    ).scalar_one()
    assert row.is_active is False
    assert row.revoked_at is not None

    # Default list excludes revoked; include_revoked surfaces it.
    active_list = await client.get(
        f"/api/v1/orgs/{org.id}/trusted-reviewers", headers=_hdrs(admin_raw)
    )
    assert all(r["id"] != rid for r in active_list.json())
    all_list = await client.get(
        f"/api/v1/orgs/{org.id}/trusted-reviewers?include_revoked=true",
        headers=_hdrs(admin_raw),
    )
    assert any(r["id"] == rid for r in all_list.json())


@pytest.mark.asyncio
async def test_revoked_reviewer_no_longer_trusted(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, admin_raw = await _make_org_with_admin(db_session)
    key_id = await _make_service_key(db_session, org.id, admin)
    proj = await _make_project(db_session, org.id)
    reg = await client.post(
        f"/api/v1/orgs/{org.id}/trusted-reviewers",
        headers=_hdrs(admin_raw),
        json={"service_key_id": key_id, "project_id": proj.id},
    )
    rid = reg.json()["id"]

    # Trusted before revoke.
    assert await is_registered_trusted_reviewer(
        db_session,
        project_id=proj.id,
        org_id=org.id,
        user_id=admin.id,
        service_key_id=key_id,
        claimed_persona="codex-reviewer",
    )

    rev = await client.request(
        "DELETE",
        f"/api/v1/orgs/{org.id}/trusted-reviewers/{rid}",
        headers=_hdrs(admin_raw),
    )
    assert rev.status_code == 204

    # No longer trusted after revoke.
    assert not await is_registered_trusted_reviewer(
        db_session,
        project_id=proj.id,
        org_id=org.id,
        user_id=admin.id,
        service_key_id=key_id,
        claimed_persona="codex-reviewer",
    )


# ── Property 7: audit events on register + revoke ──


@pytest.mark.asyncio
async def test_audit_events_written(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, admin_raw = await _make_org_with_admin(db_session)
    key_id = await _make_service_key(db_session, org.id, admin)

    assert await _count_audit(db_session, org.id, "trusted_reviewer_registered") == 0
    reg = await client.post(
        f"/api/v1/orgs/{org.id}/trusted-reviewers",
        headers=_hdrs(admin_raw),
        json={"service_key_id": key_id},
    )
    rid = reg.json()["id"]
    assert await _count_audit(db_session, org.id, "trusted_reviewer_registered") == 1

    rev = await client.request(
        "DELETE",
        f"/api/v1/orgs/{org.id}/trusted-reviewers/{rid}",
        headers=_hdrs(admin_raw),
        json={"reason": "done"},
    )
    assert rev.status_code == 204
    assert await _count_audit(db_session, org.id, "trusted_reviewer_revoked") == 1

    # The revoke audit row carries the actor + target + reason.
    row = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_id == org.id,
                OrgAuditEvent.event_type == "trusted_reviewer_revoked",
            )
        )
    ).scalar_one()
    assert row.actor_user_id == admin.id
    assert row.target_id == rid
    assert row.target_type == "trusted_reviewer"
    after = json.loads(row.after)
    assert after["reason"] == "done"


# ── Property 8: round-trip trust ──


@pytest.mark.asyncio
async def test_round_trip_register_then_trusted(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, admin_raw = await _make_org_with_admin(db_session)
    key_id = await _make_service_key(db_session, org.id, admin)
    proj = await _make_project(db_session, org.id)

    # Not trusted before registration.
    assert not await is_registered_trusted_reviewer(
        db_session,
        project_id=proj.id,
        org_id=org.id,
        user_id=admin.id,
        service_key_id=key_id,
        claimed_persona="codex-reviewer",
    )

    reg = await client.post(
        f"/api/v1/orgs/{org.id}/trusted-reviewers",
        headers=_hdrs(admin_raw),
        json={"service_key_id": key_id, "reviewer_persona": "codex-reviewer"},
    )
    assert reg.status_code == 201

    # Org-wide registration → trusted for any project in the org.
    assert await is_registered_trusted_reviewer(
        db_session,
        project_id=proj.id,
        org_id=org.id,
        user_id=admin.id,
        service_key_id=key_id,
        claimed_persona="codex-reviewer",
    )
    # Wrong persona → not trusted.
    assert not await is_registered_trusted_reviewer(
        db_session,
        project_id=proj.id,
        org_id=org.id,
        user_id=admin.id,
        service_key_id=key_id,
        claimed_persona="atlas",
    )
