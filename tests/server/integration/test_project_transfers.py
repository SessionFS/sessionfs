"""v0.10.0 Phase 2 — project transfer endpoint regression tests.

Covers every state transition and the load-bearing edge cases:
    - initiate happy path (personal → org by project owner, auto-accept)
    - initiate org → personal by admin, target accepts
    - reject path
    - cancel path
    - 403 on non-owner / non-admin initiate
    - 403 on non-target accept / reject
    - 403 on non-initiator cancel
    - 409 on accept of an already-resolved transfer (stale state)
    - 400 on same-scope transfer
    - audit row preserved post-resolution (project.org_id mutated,
      transfer row still has both snapshots intact)
    - list_transfers incoming vs outgoing filter
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    OrgMember,
    Organization,
    Project,
    User,
)


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


async def _make_org(
    db: AsyncSession, admin: User | None = None
) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Test Org",
        slug=f"test-{uuid.uuid4().hex[:8]}",
        tier="team",
    )
    db.add(org)
    await db.commit()
    if admin is not None:
        db.add(
            OrgMember(
                org_id=org.id,
                user_id=admin.id,
                role="admin",
            )
        )
        await db.commit()
    await db.refresh(org)
    return org


async def _make_project(
    db: AsyncSession,
    owner: User,
    org_id: str | None = None,
    name: str = "test-project",
) -> Project:
    proj = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name=name,
        git_remote_normalized=f"github.com/x/{uuid.uuid4().hex[:8]}",
        owner_id=owner.id,
        org_id=org_id,
    )
    db.add(proj)
    await db.commit()
    await db.refresh(proj)
    return proj


# ── Tests ──


@pytest.mark.asyncio
async def test_initiate_personal_to_org_auto_accepts_when_owner_initiates(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Personal → Org by the project owner who's also an org admin:
    auto-accept (initiator == target), state goes directly to accepted,
    project.org_id is updated synchronously."""
    alice, alice_key = await _make_user(db_session, "alice")
    org = await _make_org(db_session, admin=alice)
    project = await _make_project(db_session, owner=alice, org_id=None)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": org.id},
        headers=_hdrs(alice_key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "accepted"
    assert body["from_scope"] == "personal"
    assert body["to_scope"] == org.id
    assert body["accepted_by"] == alice.id
    assert body["project_git_remote_snapshot"] == project.git_remote_normalized
    assert body["project_name_snapshot"] == project.name

    # The project itself moved.
    await db_session.refresh(project)
    assert project.org_id == org.id


@pytest.mark.asyncio
async def test_initiate_org_to_personal_creates_pending_for_owner(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Org → Personal initiated by admin: target = project owner, who
    must accept. Pending row in target's inbox until acceptance."""
    bob, _ = await _make_user(db_session, "bob")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    # bob owns a project that's currently org-scoped
    project = await _make_project(db_session, owner=bob, org_id=org.id)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": "personal"},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "pending"
    assert body["target_user_id"] == bob.id
    assert body["from_scope"] == org.id
    assert body["to_scope"] == "personal"

    # Project hasn't moved yet.
    await db_session.refresh(project)
    assert project.org_id == org.id


@pytest.mark.asyncio
async def test_accept_flips_state_and_moves_project(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Target accepts: state → accepted, project.org_id is applied,
    accepted_by + accepted_at populate."""
    bob, bob_key = await _make_user(db_session, "bob")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    project = await _make_project(db_session, owner=bob, org_id=org.id)

    init = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": "personal"},
        headers=_hdrs(admin_key),
    )
    xfer_id = init.json()["id"]

    resp = await client.post(
        f"/api/v1/transfers/{xfer_id}/accept",
        headers=_hdrs(bob_key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "accepted"
    assert body["accepted_by"] == bob.id
    assert body["accepted_at"] is not None

    await db_session.refresh(project)
    assert project.org_id is None  # moved to personal


@pytest.mark.asyncio
async def test_non_target_cannot_accept(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Random user with API key cannot accept a transfer addressed to bob."""
    bob, _ = await _make_user(db_session, "bob")
    admin, admin_key = await _make_user(db_session, "admin")
    eve, eve_key = await _make_user(db_session, "eve")  # bystander
    org = await _make_org(db_session, admin=admin)
    project = await _make_project(db_session, owner=bob, org_id=org.id)

    init = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": "personal"},
        headers=_hdrs(admin_key),
    )
    xfer_id = init.json()["id"]

    resp = await client.post(
        f"/api/v1/transfers/{xfer_id}/accept",
        headers=_hdrs(eve_key),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_reject_flips_state_without_moving_project(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    bob, bob_key = await _make_user(db_session, "bob")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    project = await _make_project(db_session, owner=bob, org_id=org.id)

    init = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": "personal"},
        headers=_hdrs(admin_key),
    )
    xfer_id = init.json()["id"]

    resp = await client.post(
        f"/api/v1/transfers/{xfer_id}/reject",
        headers=_hdrs(bob_key),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "rejected"

    await db_session.refresh(project)
    assert project.org_id == org.id  # unmoved


@pytest.mark.asyncio
async def test_initiator_can_cancel_pending(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    bob, _ = await _make_user(db_session, "bob")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    project = await _make_project(db_session, owner=bob, org_id=org.id)

    init = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": "personal"},
        headers=_hdrs(admin_key),
    )
    xfer_id = init.json()["id"]

    resp = await client.post(
        f"/api/v1/transfers/{xfer_id}/cancel",
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "cancelled"


@pytest.mark.asyncio
async def test_non_initiator_cannot_cancel(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    bob, bob_key = await _make_user(db_session, "bob")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    project = await _make_project(db_session, owner=bob, org_id=org.id)

    init = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": "personal"},
        headers=_hdrs(admin_key),
    )
    xfer_id = init.json()["id"]

    # bob is the target, NOT the initiator. Cancel must be 403.
    resp = await client.post(
        f"/api/v1/transfers/{xfer_id}/cancel",
        headers=_hdrs(bob_key),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_accept_after_cancel_is_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Cancel-then-accept race: the second action 409s with stale-state."""
    bob, bob_key = await _make_user(db_session, "bob")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    project = await _make_project(db_session, owner=bob, org_id=org.id)

    init = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": "personal"},
        headers=_hdrs(admin_key),
    )
    xfer_id = init.json()["id"]

    # Admin cancels first.
    cancel_resp = await client.post(
        f"/api/v1/transfers/{xfer_id}/cancel",
        headers=_hdrs(admin_key),
    )
    assert cancel_resp.status_code == 200

    # Bob's accept arrives stale.
    accept_resp = await client.post(
        f"/api/v1/transfers/{xfer_id}/accept",
        headers=_hdrs(bob_key),
    )
    assert accept_resp.status_code == 409


@pytest.mark.asyncio
async def test_same_scope_transfer_is_400(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, alice_key = await _make_user(db_session, "alice")
    project = await _make_project(db_session, owner=alice, org_id=None)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": "personal"},
        headers=_hdrs(alice_key),
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_non_owner_cannot_initiate_personal_transfer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, _ = await _make_user(db_session, "alice")
    eve, eve_key = await _make_user(db_session, "eve")
    org = await _make_org(db_session, admin=eve)
    project = await _make_project(db_session, owner=alice, org_id=None)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": org.id},
        headers=_hdrs(eve_key),
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_list_incoming_and_outgoing_separation(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """incoming/outgoing direction filters are isolated by user."""
    bob, bob_key = await _make_user(db_session, "bob")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    project = await _make_project(db_session, owner=bob, org_id=org.id)

    await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": "personal"},
        headers=_hdrs(admin_key),
    )

    # Bob's incoming inbox: should see the pending transfer.
    incoming = await client.get(
        "/api/v1/transfers?direction=incoming",
        headers=_hdrs(bob_key),
    )
    assert incoming.status_code == 200
    inbox = incoming.json()["transfers"]
    assert len(inbox) == 1
    assert inbox[0]["target_user_id"] == bob.id

    # Admin's outgoing list: should see what they initiated.
    outgoing = await client.get(
        "/api/v1/transfers?direction=outgoing",
        headers=_hdrs(admin_key),
    )
    assert outgoing.status_code == 200
    sent = outgoing.json()["transfers"]
    assert len(sent) == 1
    assert sent[0]["initiated_by"] == admin.id

    # Admin's INCOMING list: should be empty.
    admin_incoming = await client.get(
        "/api/v1/transfers?direction=incoming",
        headers=_hdrs(admin_key),
    )
    assert admin_incoming.status_code == 200
    assert admin_incoming.json()["transfers"] == []


@pytest.mark.asyncio
async def test_personal_to_org_requires_destination_membership(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Codex Phase-2 round-1 HIGH (KB entry 246) regression.

    A personal project owner who is NOT a member of the destination
    org MUST NOT be allowed to move their project into it. The pre-
    fix route only validated `body.to` existed as an Organization;
    it didn't gate on membership, so the auto-accept branch would
    fire and the project would land in an org the user has no
    relationship with.
    """
    alice, alice_key = await _make_user(db_session, "alice")
    # Create an org with a DIFFERENT admin — alice is not a member.
    stranger, _ = await _make_user(db_session, "stranger")
    org = await _make_org(db_session, admin=stranger)
    project = await _make_project(db_session, owner=alice, org_id=None)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": org.id},
        headers=_hdrs(alice_key),
    )
    assert resp.status_code == 403, resp.text

    # And the project did NOT move.
    await db_session.refresh(project)
    assert project.org_id is None


@pytest.mark.asyncio
async def test_duplicate_pending_initiate_is_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Codex Phase-2 round-1 MEDIUM (KB entry 246) regression.

    Two simultaneous pending transfers for the same project cause
    a state-integrity bug: both can later accept, last-writer-wins
    on project.org_id, two "accepted" audit rows contradict.
    Route now refuses the second initiate with 409 while one is
    still pending.
    """
    bob, _ = await _make_user(db_session, "bob")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    project = await _make_project(db_session, owner=bob, org_id=org.id)

    first = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": "personal"},
        headers=_hdrs(admin_key),
    )
    assert first.status_code == 200
    assert first.json()["state"] == "pending"

    second = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": "personal"},
        headers=_hdrs(admin_key),
    )
    assert second.status_code == 409, second.text

    # Exactly one pending transfer should exist for this project.
    from sqlalchemy import select as _select
    from sessionfs.server.db.models import ProjectTransfer

    pending = (
        await db_session.execute(
            _select(ProjectTransfer).where(
                ProjectTransfer.project_id == project.id,
                ProjectTransfer.state == "pending",
            )
        )
    ).scalars().all()
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_initiate_after_cancel_is_allowed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """The duplicate-pending guard must NOT prevent re-initiating
    after a cancel — that's a legitimate retry shape.
    """
    bob, _ = await _make_user(db_session, "bob")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    project = await _make_project(db_session, owner=bob, org_id=org.id)

    first = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": "personal"},
        headers=_hdrs(admin_key),
    )
    xfer_id = first.json()["id"]
    await client.post(
        f"/api/v1/transfers/{xfer_id}/cancel",
        headers=_hdrs(admin_key),
    )

    # Re-initiate after cancel — should succeed.
    retry = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": "personal"},
        headers=_hdrs(admin_key),
    )
    assert retry.status_code == 200
    assert retry.json()["state"] == "pending"


@pytest.mark.asyncio
async def test_accept_fails_when_target_removed_from_destination_org(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Codex Phase-2 round-2 MEDIUM #1 (KB entry 248) regression.

    Stale-target authorization: the user picked as target_user_id can
    be removed from the destination org BETWEEN initiate and accept.
    The pre-fix accept only checked `target_user_id == user.id` so the
    removed user could still land a project move into an org they no
    longer belong to. The fix re-derives standing on every action.

    Setup: alice owns a personal project. admin (also an org member)
    is in the destination org. alice is added to the destination org
    as a non-admin member. admin initiates a transfer FROM alice's
    personal project INTO the org — but alice's already-org-member
    status auto-accepts, so we need a different shape to get a
    pending row addressed to alice... Use an org→org shape with
    target = alice (member of destination org), then remove alice
    from destination before she accepts.
    """
    alice, alice_key = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")

    src_org = await _make_org(db_session, admin=admin)
    dst_org = await _make_org(db_session, admin=admin)

    # alice is a member of dst_org (so she's a valid target for an
    # org→org transfer landing there).
    db_session.add(
        OrgMember(org_id=dst_org.id, user_id=alice.id, role="member")
    )
    await db_session.commit()

    # bob is the project owner; project is in src_org.
    bob, _ = await _make_user(db_session, "bob")
    project = await _make_project(db_session, owner=bob, org_id=src_org.id)

    # admin initiates src_org → dst_org. _resolve_target_user_id picks
    # the FIRST admin of dst_org (admin themselves) — which is the
    # auto-accept shape. We need to force a pending row with alice as
    # target. Easiest: insert the ProjectTransfer directly.
    from sessionfs.server.db.models import ProjectTransfer

    transfer = ProjectTransfer(
        id=f"xfer_{uuid.uuid4().hex[:12]}",
        project_id=project.id,
        project_git_remote_snapshot=project.git_remote_normalized,
        project_name_snapshot=project.name,
        initiated_by=admin.id,
        target_user_id=alice.id,
        from_scope=src_org.id,
        to_scope=dst_org.id,
        state="pending",
    )
    db_session.add(transfer)
    await db_session.commit()
    xfer_id = transfer.id

    # Now remove alice from dst_org BEFORE she accepts.
    from sqlalchemy import delete as _delete

    await db_session.execute(
        _delete(OrgMember).where(
            OrgMember.org_id == dst_org.id, OrgMember.user_id == alice.id
        )
    )
    await db_session.commit()

    # Alice's accept must now be rejected: she no longer has standing.
    resp = await client.post(
        f"/api/v1/transfers/{xfer_id}/accept",
        headers=_hdrs(alice_key),
    )
    assert resp.status_code == 403, resp.text

    # Project did NOT move — still in src_org.
    await db_session.refresh(project)
    assert project.org_id == src_org.id


@pytest.mark.asyncio
async def test_reject_fails_when_target_removed_from_destination_org(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Same standing-recheck applies to reject. Without it, a removed
    user could still mutate state, leaving an orphaned audit row."""
    alice, alice_key = await _make_user(db_session, "alice")
    admin, _ = await _make_user(db_session, "admin")
    src_org = await _make_org(db_session, admin=admin)
    dst_org = await _make_org(db_session, admin=admin)
    db_session.add(
        OrgMember(org_id=dst_org.id, user_id=alice.id, role="member")
    )
    await db_session.commit()

    bob, _ = await _make_user(db_session, "bob")
    project = await _make_project(db_session, owner=bob, org_id=src_org.id)

    from sessionfs.server.db.models import ProjectTransfer

    transfer = ProjectTransfer(
        id=f"xfer_{uuid.uuid4().hex[:12]}",
        project_id=project.id,
        project_git_remote_snapshot=project.git_remote_normalized,
        project_name_snapshot=project.name,
        initiated_by=admin.id,
        target_user_id=alice.id,
        from_scope=src_org.id,
        to_scope=dst_org.id,
        state="pending",
    )
    db_session.add(transfer)
    await db_session.commit()
    xfer_id = transfer.id

    from sqlalchemy import delete as _delete

    await db_session.execute(
        _delete(OrgMember).where(
            OrgMember.org_id == dst_org.id, OrgMember.user_id == alice.id
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/transfers/{xfer_id}/reject",
        headers=_hdrs(alice_key),
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_accept_fails_when_target_demoted_from_admin_to_member(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Codex Phase-2 round-3 MEDIUM (KB entry 250) regression.

    For org→org transfers, the target is selected as an ADMIN of
    the destination org. If that admin is demoted to plain member
    before acting, they must lose standing — anything else lets a
    demoted admin land a project move that they would no longer be
    authorized to initiate.
    """
    alice, alice_key = await _make_user(db_session, "alice")
    src_admin, _ = await _make_user(db_session, "src_admin")
    src_org = await _make_org(db_session, admin=src_admin)
    dst_org = await _make_org(db_session, admin=alice)  # alice = dst admin

    bob, _ = await _make_user(db_session, "bob")
    project = await _make_project(db_session, owner=bob, org_id=src_org.id)

    from sessionfs.server.db.models import ProjectTransfer
    from sqlalchemy import update as _update

    # Direct-insert a pending org→org transfer targeting alice.
    transfer = ProjectTransfer(
        id=f"xfer_{uuid.uuid4().hex[:12]}",
        project_id=project.id,
        project_git_remote_snapshot=project.git_remote_normalized,
        project_name_snapshot=project.name,
        initiated_by=src_admin.id,
        target_user_id=alice.id,
        from_scope=src_org.id,
        to_scope=dst_org.id,
        state="pending",
    )
    db_session.add(transfer)
    await db_session.commit()
    xfer_id = transfer.id

    # Demote alice from admin to member in dst_org BEFORE she accepts.
    await db_session.execute(
        _update(OrgMember)
        .where(
            OrgMember.org_id == dst_org.id, OrgMember.user_id == alice.id
        )
        .values(role="member")
    )
    await db_session.commit()

    # Alice (now plain member) must NOT be able to accept the
    # admin-targeted transfer.
    resp = await client.post(
        f"/api/v1/transfers/{xfer_id}/accept",
        headers=_hdrs(alice_key),
    )
    assert resp.status_code == 403, resp.text

    # Project did NOT move.
    await db_session.refresh(project)
    assert project.org_id == src_org.id


@pytest.mark.asyncio
async def test_partial_unique_index_blocks_concurrent_duplicate_pending(
    db_session: AsyncSession,
) -> None:
    """Codex Phase-2 round-2 MEDIUM #2 (KB entry 248) regression.

    Schema-level concurrency safety: the partial-unique index on
    (project_id) WHERE state='pending' MUST refuse a second pending
    row for the same project. This is the backstop for the route's
    SELECT-then-INSERT precheck — two concurrent initiates would
    both pass the SELECT but only one INSERT survives.
    """
    from sqlalchemy.exc import IntegrityError as _IntegrityError

    from sessionfs.server.db.models import ProjectTransfer

    bob, _ = await _make_user(db_session, "bob")
    admin, _ = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    project = await _make_project(db_session, owner=bob, org_id=org.id)

    db_session.add(
        ProjectTransfer(
            id=f"xfer_{uuid.uuid4().hex[:12]}",
            project_id=project.id,
            project_git_remote_snapshot=project.git_remote_normalized,
            project_name_snapshot=project.name,
            initiated_by=admin.id,
            target_user_id=bob.id,
            from_scope=org.id,
            to_scope="personal",
            state="pending",
        )
    )
    await db_session.commit()

    # A second pending row for the same project must fail at commit.
    db_session.add(
        ProjectTransfer(
            id=f"xfer_{uuid.uuid4().hex[:12]}",
            project_id=project.id,
            project_git_remote_snapshot=project.git_remote_normalized,
            project_name_snapshot=project.name,
            initiated_by=admin.id,
            target_user_id=bob.id,
            from_scope=org.id,
            to_scope="personal",
            state="pending",
        )
    )
    with pytest.raises(_IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_partial_unique_index_allows_resolved_plus_new_pending(
    db_session: AsyncSession,
) -> None:
    """The partial-unique constraint must ONLY apply to pending rows
    — accepted/rejected/cancelled rows for the same project must
    happily coexist with a new pending row. Otherwise the cancel-then-
    reinitiate path (covered separately) would be blocked at the DB
    layer.
    """
    from sessionfs.server.db.models import ProjectTransfer

    bob, _ = await _make_user(db_session, "bob")
    admin, _ = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    project = await _make_project(db_session, owner=bob, org_id=org.id)

    # First transfer cancelled.
    db_session.add(
        ProjectTransfer(
            id=f"xfer_{uuid.uuid4().hex[:12]}",
            project_id=project.id,
            project_git_remote_snapshot=project.git_remote_normalized,
            project_name_snapshot=project.name,
            initiated_by=admin.id,
            target_user_id=bob.id,
            from_scope=org.id,
            to_scope="personal",
            state="cancelled",
        )
    )
    # Second transfer pending — should not collide.
    db_session.add(
        ProjectTransfer(
            id=f"xfer_{uuid.uuid4().hex[:12]}",
            project_id=project.id,
            project_git_remote_snapshot=project.git_remote_normalized,
            project_name_snapshot=project.name,
            initiated_by=admin.id,
            target_user_id=bob.id,
            from_scope=org.id,
            to_scope="personal",
            state="pending",
        )
    )
    # Both commit cleanly.
    await db_session.commit()


@pytest.mark.asyncio
async def test_post_resolution_audit_row_intact(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """After accept, the transfer row has the project move applied
    AND both snapshots are still populated — audit durability."""
    bob, bob_key = await _make_user(db_session, "bob")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    project = await _make_project(db_session, owner=bob, org_id=org.id)

    init = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": "personal"},
        headers=_hdrs(admin_key),
    )
    xfer_id = init.json()["id"]

    accept = await client.post(
        f"/api/v1/transfers/{xfer_id}/accept",
        headers=_hdrs(bob_key),
    )
    body = accept.json()
    assert body["state"] == "accepted"
    # Both snapshot columns intact AFTER resolution.
    assert body["project_git_remote_snapshot"] == project.git_remote_normalized
    assert body["project_name_snapshot"] == project.name
    assert body["accepted_by"] == bob.id
    assert body["initiated_by"] == admin.id
