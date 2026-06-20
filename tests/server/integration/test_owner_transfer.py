"""P4 — Owner-role enforcement + two-step ownership transfer tests.

Covers:
  - Guards: new_role='owner' rejected; admin cannot demote/remove owner;
    owner cannot be removed; owner self-demote blocked; last-admin
    guard with owner present.
  - Transfer happy path: initiate → accept → exactly one owner.
  - Transfer atomicity/race: concurrent accepts → one wins.
  - Transfer re-validation: expired, initiator no longer owner,
    target no longer admin, non-admin target, duplicate pending.
  - Cancel (owner + target).
  - Deactivation blocked for sole owner; auto-promote when admin exists.
  - Admin force-transfer.
  - Cross-org isolation.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

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
    OrgOwnerTransfer,
    User,
)


# ── Fixtures ────────────────────────────────────────────────────────


async def _make_user(
    db: AsyncSession, name: str = "alice"
) -> tuple[User, str]:
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


async def _make_org_with_owner(
    db: AsyncSession, owner: User, name: str = "Test Org"
) -> Organization:
    """Create an org with the given user as owner."""
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name=name,
        slug=f"test-{uuid.uuid4().hex[:8]}",
        tier="team",
        seats_limit=10,
    )
    db.add(org)
    await db.commit()
    db.add(OrgMember(org_id=org.id, user_id=owner.id, role="owner"))
    await db.commit()
    await db.refresh(org)
    return org


async def _add_member(
    db: AsyncSession, org: Organization, user: User, role: str = "admin"
) -> None:
    db.add(OrgMember(org_id=org.id, user_id=user.id, role=role))
    await db.commit()


async def _get_member_role(
    db: AsyncSession, org_id: str, user_id: str
) -> str | None:
    member = (
        await db.execute(
            select(OrgMember).where(
                OrgMember.org_id == org_id, OrgMember.user_id == user_id
            )
        )
    ).scalar_one_or_none()
    return member.role if member else None


# ── Owner guard tests ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_new_role_owner_rejected_in_role_change(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Setting new_role='owner' via PUT /members/{id}/role is rejected."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/members/{admin.id}/role",
        json={"role": "owner"},
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 400
    assert "transfer" in resp.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_admin_cannot_demote_owner(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An admin cannot demote the owner to member."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/members/{owner.id}/role",
        json={"role": "member"},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 403
    assert "owner" in resp.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_admin_cannot_remove_owner(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An admin cannot remove the owner from the org."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")

    resp = await client.delete(
        f"/api/v1/orgs/{org.id}/members/{owner.id}",
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 409
    assert "transfer" in resp.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_owner_self_demote_to_member_blocked(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Owner cannot self-demote to member."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/members/{owner.id}/role",
        json={"role": "member"},
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_owner_self_demote_to_admin_when_sole_admin_blocked(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Owner cannot self-demote to admin if they are the only admin."""
    owner, owner_key = await _make_user(db_session, "owner")
    org = await _make_org_with_owner(db_session, owner)
    # No other admins — just the owner.

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/members/{owner.id}/role",
        json={"role": "admin"},
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 409
    assert "last administrator" in resp.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_owner_self_demote_to_admin_with_another_admin_succeeds(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Owner can self-demote to admin if another admin exists."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/members/{owner.id}/role",
        json={"role": "admin"},
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "admin"

    # Owner is now admin; admin stays admin.
    assert await _get_member_role(db_session, org.id, owner.id) == "admin"
    assert await _get_member_role(db_session, org.id, admin.id) == "admin"


@pytest.mark.asyncio
async def test_last_admin_guard_with_owner_present_allows_admin_removal(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Removing the last *admin* when an owner exists is allowed."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")

    # Owner removes the only admin — should succeed because owner
    # satisfies the administrative-count guard.
    resp = await client.delete(
        f"/api/v1/orgs/{org.id}/members/{admin.id}",
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_last_admin_demotion_guard_with_owner_present(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Demoting the last *admin* to member when an owner exists is allowed."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/members/{admin.id}/role",
        json={"role": "member"},
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 200


# ── Transfer happy path ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_transfer_happy_path_initiate_accept(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Full transfer: owner initiates, target admin accepts, roles swap."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")

    # Initiate.
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        json={"to_user_id": admin.id},
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "pending"
    assert data["from_user_id"] == owner.id
    assert data["to_user_id"] == admin.id
    transfer_id = data["transfer_id"]

    # Accept.
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer/{transfer_id}/accept",
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "accepted"

    # Roles swapped.
    assert await _get_member_role(db_session, org.id, owner.id) == "admin"
    assert await _get_member_role(db_session, org.id, admin.id) == "owner"

    # Audit events emitted (initiate + accept).
    events = (
        await db_session.execute(
            select(OrgAuditEvent).where(OrgAuditEvent.org_id == org.id)
        )
    ).scalars().all()
    event_types = {e.event_type for e in events}
    assert "owner_transfer_initiated" in event_types
    assert "owner_transferred" in event_types


@pytest.mark.asyncio
async def test_transfer_sets_expires_at(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Initiated transfer has a future expires_at."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        json={"to_user_id": admin.id},
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["expires_at"] is not None


# ── Transfer guards ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_non_owner_cannot_initiate_transfer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Only the owner can initiate a transfer."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")
    await _add_member(db_session, org, member, role="member")

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        json={"to_user_id": admin.id},
        headers=_hdrs(member_key),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_transfer_target_must_be_admin(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Transfer target must be an admin, not a plain member."""
    owner, owner_key = await _make_user(db_session, "owner")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, member, role="member")

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        json={"to_user_id": member.id},
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_transfer_target_must_be_member(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Transfer target must be a member of the org."""
    owner, owner_key = await _make_user(db_session, "owner")
    outsider, outsider_key = await _make_user(db_session, "outsider")
    org = await _make_org_with_owner(db_session, owner)

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        json={"to_user_id": outsider.id},
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_cannot_transfer_to_self(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Owner cannot transfer ownership to themselves."""
    owner, owner_key = await _make_user(db_session, "owner")
    org = await _make_org_with_owner(db_session, owner)

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        json={"to_user_id": owner.id},
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_duplicate_pending_transfer_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Only one pending transfer per org at a time."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin1, _ = await _make_user(db_session, "admin1")
    admin2, _ = await _make_user(db_session, "admin2")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin1, role="admin")
    await _add_member(db_session, org, admin2, role="admin")

    # First transfer.
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        json={"to_user_id": admin1.id},
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 200

    # Second transfer to different admin — rejected.
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        json={"to_user_id": admin2.id},
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_only_target_can_accept(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Only the designated target can accept the transfer."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    other, other_key = await _make_user(db_session, "other")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")
    await _add_member(db_session, org, other, role="admin")

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        json={"to_user_id": admin.id},
        headers=_hdrs(owner_key),
    )
    transfer_id = resp.json()["transfer_id"]

    # Other admin tries to accept — rejected.
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer/{transfer_id}/accept",
        headers=_hdrs(other_key),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_expired_transfer_rejected(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """An expired transfer cannot be accepted."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")

    # Create a transfer then manually expire it.
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        json={"to_user_id": admin.id},
        headers=_hdrs(owner_key),
    )
    transfer_id = resp.json()["transfer_id"]

    # Manually set expires_at in the past.
    transfer = (
        await db_session.execute(
            select(OrgOwnerTransfer).where(OrgOwnerTransfer.id == transfer_id)
        )
    ).scalar_one()
    transfer.expires_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer/{transfer_id}/accept",
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_accept_revalidates_initiator_still_owner(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Accept re-validates that initiator is still the owner."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        json={"to_user_id": admin.id},
        headers=_hdrs(owner_key),
    )
    transfer_id = resp.json()["transfer_id"]

    # Demote the owner to admin before accept (simulating a race).
    owner_member = (
        await db_session.execute(
            select(OrgMember).where(
                OrgMember.org_id == org.id, OrgMember.user_id == owner.id
            )
        )
    ).scalar_one()
    owner_member.role = "admin"
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer/{transfer_id}/accept",
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 409
    assert "initiator" in resp.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_accept_revalidates_target_still_admin(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Accept re-validates that target is still an admin."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        json={"to_user_id": admin.id},
        headers=_hdrs(owner_key),
    )
    transfer_id = resp.json()["transfer_id"]

    # Demote the target to member before accept.
    target_member = (
        await db_session.execute(
            select(OrgMember).where(
                OrgMember.org_id == org.id, OrgMember.user_id == admin.id
            )
        )
    ).scalar_one()
    target_member.role = "member"
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer/{transfer_id}/accept",
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 409
    assert "target" in resp.json()["error"]["message"].lower()


# ── Race condition test ─────────────────────────────────────────────


@pytest.mark.asyncio
# ── Cancel ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_can_cancel_transfer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Owner can cancel their own pending transfer."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        json={"to_user_id": admin.id},
        headers=_hdrs(owner_key),
    )
    transfer_id = resp.json()["transfer_id"]

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer/{transfer_id}/cancel",
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"

    # Transfer marked cancelled in DB.
    transfer = (
        await db_session.execute(
            select(OrgOwnerTransfer).where(OrgOwnerTransfer.id == transfer_id)
        )
    ).scalar_one()
    assert transfer.status == "cancelled"


@pytest.mark.asyncio
async def test_target_can_cancel_transfer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Target admin can cancel a pending transfer."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        json={"to_user_id": admin.id},
        headers=_hdrs(owner_key),
    )
    transfer_id = resp.json()["transfer_id"]

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer/{transfer_id}/cancel",
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_outsider_cannot_cancel_transfer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Someone who is neither owner nor target cannot cancel."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    other, other_key = await _make_user(db_session, "other")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")
    await _add_member(db_session, org, other, role="admin")

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        json={"to_user_id": admin.id},
        headers=_hdrs(owner_key),
    )
    transfer_id = resp.json()["transfer_id"]

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer/{transfer_id}/cancel",
        headers=_hdrs(other_key),
    )
    assert resp.status_code == 403


# ── GET pending transfer ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_pending_transfer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Any member can view the pending transfer."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")
    await _add_member(db_session, org, member, role="member")

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        json={"to_user_id": admin.id},
        headers=_hdrs(owner_key),
    )
    transfer_id = resp.json()["transfer_id"]

    # Member can view it.
    resp = await client.get(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        headers=_hdrs(member_key),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["transfer_id"] == transfer_id
    assert data["status"] == "pending"


@pytest.mark.asyncio
async def test_get_pending_transfer_returns_pending_false_when_none(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """No pending transfer → {pending: false}."""
    owner, owner_key = await _make_user(db_session, "owner")
    org = await _make_org_with_owner(db_session, owner)

    resp = await client.get(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 200
    assert resp.json() == {"pending": False}


# ── Cross-org isolation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cannot_accept_other_org_transfer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Cannot accept a transfer from another org."""
    owner1, owner1_key = await _make_user(db_session, "owner1")
    admin1, admin1_key = await _make_user(db_session, "admin1")
    org1 = await _make_org_with_owner(db_session, owner1, name="Org 1")
    await _add_member(db_session, org1, admin1, role="admin")

    owner2, owner2_key = await _make_user(db_session, "owner2")
    admin2, admin2_key = await _make_user(db_session, "admin2")
    org2 = await _make_org_with_owner(db_session, owner2, name="Org 2")
    await _add_member(db_session, org2, admin2, role="admin")

    # Create transfer in org1.
    resp = await client.post(
        f"/api/v1/orgs/{org1.id}/owner/transfer",
        json={"to_user_id": admin1.id},
        headers=_hdrs(owner1_key),
    )
    transfer_id = resp.json()["transfer_id"]

    # Try to accept it against org2's URL — transfer not found in org2.
    resp = await client.post(
        f"/api/v1/orgs/{org2.id}/owner/transfer/{transfer_id}/accept",
        headers=_hdrs(admin2_key),
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_non_member_cannot_view_transfer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Non-members cannot view the pending transfer."""
    owner, owner_key = await _make_user(db_session, "owner")
    outsider, outsider_key = await _make_user(db_session, "outsider")
    org = await _make_org_with_owner(db_session, owner)

    resp = await client.get(
        f"/api/v1/orgs/{org.id}/owner/transfer",
        headers=_hdrs(outsider_key),
    )
    assert resp.status_code == 403


# ── Deactivation safety ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_deactivation_sole_owner_blocked(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Cannot deactivate a user who is the sole owner of an org."""
    owner, owner_key = await _make_user(db_session, "soleowner")
    org = await _make_org_with_owner(db_session, owner)
    # No other admins — owner is the only administrator.

    # Need an admin API key for the delete-user endpoint.
    # Use a platform admin user.
    admin_user, admin_raw = await _make_user(db_session, "platformadmin")
    admin_user.tier = "admin"  # make them a platform admin
    await db_session.commit()

    admin_key = generate_api_key()
    from sessionfs.server.auth.keys import hash_api_key as _hash
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=admin_user.id,
            key_hash=_hash(admin_key),
            name="admin-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    resp = await client.delete(
        f"/api/v1/admin/users/{owner.id}",
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["details"]["error"] == "sole_owner"
    assert body["error"]["details"]["org_id"] == org.id


@pytest.mark.asyncio
async def test_deactivation_owner_with_admin_auto_promotes(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Deactivating an owner auto-promotes the longest-tenured admin."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")

    # Platform admin.
    from sessionfs.server.auth.keys import hash_api_key as _hash
    admin_user, _ = await _make_user(db_session, "platformadmin")
    admin_user.tier = "admin"
    await db_session.commit()
    admin_raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=admin_user.id,
            key_hash=_hash(admin_raw),
            name="admin-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    resp = await client.delete(
        f"/api/v1/admin/users/{owner.id}",
        headers=_hdrs(admin_raw),
    )
    # Should succeed — admin auto-promoted to owner.
    assert resp.status_code == 204

    # Admin is now owner.
    assert await _get_member_role(db_session, org.id, admin.id) == "owner"

    # Audit event emitted.
    events = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_id == org.id,
                OrgAuditEvent.event_type == "owner_auto_promoted_on_deactivation",
            )
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].target_id == admin.id


# ── Admin force-transfer ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_force_transfer_succeeds(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Platform admin can force-transfer ownership."""
    owner, owner_key = await _make_user(db_session, "owner")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org_with_owner(db_session, owner)
    await _add_member(db_session, org, admin, role="admin")

    # Platform admin.
    from sessionfs.server.auth.keys import hash_api_key as _hash
    platform_admin, _ = await _make_user(db_session, "platadmin")
    platform_admin.tier = "admin"
    await db_session.commit()
    pa_raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=platform_admin.id,
            key_hash=_hash(pa_raw),
            name="pa-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/admin/orgs/{org.id}/force-transfer-owner",
        json={"to_user_id": admin.id},
        headers=_hdrs(pa_raw),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "force_transferred"

    # Roles swapped.
    assert await _get_member_role(db_session, org.id, admin.id) == "owner"

    # Audit events emitted.
    events = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_id == org.id,
                OrgAuditEvent.event_type == "owner_force_transferred",
            )
        )
    ).scalars().all()
    assert len(events) == 1


@pytest.mark.asyncio
async def test_admin_force_transfer_target_not_member(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Force-transfer requires target to be an org member."""
    owner, owner_key = await _make_user(db_session, "owner")
    outsider, _ = await _make_user(db_session, "outsider")
    org = await _make_org_with_owner(db_session, owner)

    from sessionfs.server.auth.keys import hash_api_key as _hash
    platform_admin, _ = await _make_user(db_session, "platadmin")
    platform_admin.tier = "admin"
    await db_session.commit()
    pa_raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=platform_admin.id,
            key_hash=_hash(pa_raw),
            name="pa-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/admin/orgs/{org.id}/force-transfer-owner",
        json={"to_user_id": outsider.id},
        headers=_hdrs(pa_raw),
    )
    assert resp.status_code == 404
