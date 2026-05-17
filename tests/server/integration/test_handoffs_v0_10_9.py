"""v0.10.9 — comprehensive handoff redesign integration tests.

Covers schema validation, provenance carry-through, attachments,
lifecycle (revoke/decline/comments/events), lazy expiry, viewed_at,
404-not-403 existence hiding, and team-handoff flow.

Companion to test_handoffs.py (which keeps the v0.10.8 backward-compat
contract). This file is structured by behavior, not by endpoint, so it
documents the v0.10.9 product story.
"""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentPersona,
    ApiKey,
    Handoff,
    KnowledgePage,
    OrgMember,
    Organization,
    Project,
    Ticket,
    User,
)
from sessionfs.server.auth.keys import generate_api_key, hash_api_key


# ── helpers ──


async def _push_session(client, headers, sample_sfs_tar) -> str:
    sid = f"ses_{uuid.uuid4().hex[:12]}"
    resp = await client.put(
        f"/api/v1/sessions/{sid}/sync",
        headers=headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201, resp.text
    return sid


async def _create_handoff(client, headers, session_id, **kwargs) -> dict:
    body = {"session_id": session_id}
    body.update(kwargs)
    resp = await client.post("/api/v1/handoffs", headers=headers, json=body)
    assert resp.status_code == 201, f"{resp.status_code}: {resp.text}"
    return resp.json()


@pytest.fixture
async def second_user(db_session: AsyncSession) -> tuple[User, dict]:
    """A second user (recipient) with API key."""
    user = User(
        id=str(uuid.uuid4()),
        email="recipient@example.com",
        display_name="Recipient",
        tier="pro",
        created_at=datetime.now(timezone.utc),
        email_verified=True,
    )
    db_session.add(user)
    await db_session.flush()
    raw = generate_api_key()
    key = ApiKey(
        id=str(uuid.uuid4()),
        user_id=user.id,
        key_hash=hash_api_key(raw),
        name="r-key",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(key)
    await db_session.commit()
    return user, {"Authorization": f"Bearer {raw}"}


# ── schema validation ──


@pytest.mark.asyncio
async def test_exactly_one_recipient_required(client, auth_headers, sample_sfs_tar):
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    # Zero recipients
    r0 = await client.post(
        "/api/v1/handoffs", headers=auth_headers, json={"session_id": sid}
    )
    assert r0.status_code == 422
    # Two recipients (email + user_id)
    r2 = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": sid,
            "recipient_email": "a@x.com",
            "recipient_user_id": "u123",
        },
    )
    assert r2.status_code == 422


@pytest.mark.asyncio
async def test_expires_in_hours_clamped_to_tier_max(
    client, auth_headers, sample_sfs_tar, db_session, test_user
):
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    # 800h exceeds Pro 720h ceiling — should clamp silently to 720.
    h = await _create_handoff(
        client,
        auth_headers,
        sid,
        recipient_email="r@x.com",
        expires_in_hours=800,
    )
    row = (
        await db_session.execute(
            __import__("sqlalchemy").select(Handoff).where(Handoff.id == h["id"])
        )
    ).scalar_one()
    # Should be within 1 second of "now + 720h" — generous tolerance.
    delta = row.expires_at.replace(tzinfo=timezone.utc) - datetime.now(timezone.utc)
    assert 719 <= delta.total_seconds() / 3600 <= 721


@pytest.mark.asyncio
async def test_blank_recipient_user_id_stripped_to_none(
    client, auth_headers, sample_sfs_tar
):
    """LOW #4 — whitespace-only IDs were passing exactly-one-recipient."""
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    r = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": sid,
            "recipient_user_id": "   ",  # blank after strip
            "recipient_email": "r@x.com",
        },
    )
    # The blank user_id should strip to None → only email remains → 201.
    assert r.status_code == 201


# ── provenance carry-through ──


@pytest.mark.asyncio
async def test_ticket_id_persona_validated_against_session_project(
    client, auth_headers, sample_sfs_tar, db_session, test_user
):
    """Provenance refs must resolve in the session's project."""
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    # The session's git_remote_normalized from sample_sfs_tar — look it up.
    sess = (
        await db_session.execute(
            __import__("sqlalchemy").select(__import__("sessionfs.server.db.models", fromlist=["Session"]).Session).where(
                __import__("sessionfs.server.db.models", fromlist=["Session"]).Session.id == sid
            )
        )
    ).scalar_one()

    proj = Project(
        id="proj_a",
        git_remote_normalized=sess.git_remote_normalized or "github.com/x/y",
        name="X",
        owner_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(proj)
    if not sess.git_remote_normalized:
        sess.git_remote_normalized = "github.com/x/y"
    persona = AgentPersona(
        id="p_a",
        project_id="proj_a",
        name="atlas",
        role="backend",
        content="x",
        is_active=True,
        created_by=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(persona)
    await db_session.commit()

    # Unknown ticket_id should 422
    bad = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": sid,
            "recipient_email": "r@x.com",
            "ticket_id": "tk_doesnotexist",
        },
    )
    assert bad.status_code == 422

    # Persona that exists is fine
    ok = await _create_handoff(
        client,
        auth_headers,
        sid,
        recipient_email="r@x.com",
        persona_name="atlas",
    )
    assert ok["persona_name"] == "atlas"
    assert ok["snapshot_persona_name"] == "atlas"


@pytest.mark.asyncio
async def test_attachments_unknown_kind_rejected(
    client, auth_headers, sample_sfs_tar
):
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    r = await client.post(
        "/api/v1/handoffs",
        headers=auth_headers,
        json={
            "session_id": sid,
            "recipient_email": "r@x.com",
            "attachments": [{"kind": "bogus", "ref_id": "1"}],
        },
    )
    # Pydantic enum validation rejects unknown kind.
    assert r.status_code == 422


# ── revoke ──


@pytest.mark.asyncio
async def test_revoke_pending_handoff(client, auth_headers, sample_sfs_tar):
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    h = await _create_handoff(
        client, auth_headers, sid, recipient_email="r@x.com"
    )
    r = await client.post(
        f"/api/v1/handoffs/{h['id']}/revoke",
        headers=auth_headers,
        json={"reason": "wrong recipient"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "revoked"
    assert body["revoke_reason"] == "wrong recipient"


@pytest.mark.asyncio
async def test_revoke_requires_reason(client, auth_headers, sample_sfs_tar):
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    h = await _create_handoff(client, auth_headers, sid, recipient_email="r@x.com")
    r = await client.post(
        f"/api/v1/handoffs/{h['id']}/revoke", headers=auth_headers, json={}
    )
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_revoke_non_sender_returns_404(
    client, auth_headers, sample_sfs_tar, second_user
):
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    _, recipient_headers = second_user
    h = await _create_handoff(
        client, auth_headers, sid, recipient_email="recipient@example.com"
    )
    r = await client.post(
        f"/api/v1/handoffs/{h['id']}/revoke",
        headers=recipient_headers,
        json={"reason": "trying to sabotage"},
    )
    # Recipient can't revoke — 404 (existence is sensitive).
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_revoke_after_claim_409(
    client, auth_headers, sample_sfs_tar, second_user
):
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    _, recipient_headers = second_user
    h = await _create_handoff(
        client, auth_headers, sid, recipient_email="recipient@example.com"
    )
    claim = await client.post(
        f"/api/v1/handoffs/{h['id']}/claim", headers=recipient_headers
    )
    assert claim.status_code == 200
    r = await client.post(
        f"/api/v1/handoffs/{h['id']}/revoke",
        headers=auth_headers,
        json={"reason": "too late"},
    )
    assert r.status_code == 409


# ── decline ──


@pytest.mark.asyncio
async def test_decline_by_recipient(
    client, auth_headers, sample_sfs_tar, second_user
):
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    _, recipient_headers = second_user
    h = await _create_handoff(
        client, auth_headers, sid, recipient_email="recipient@example.com"
    )
    r = await client.post(
        f"/api/v1/handoffs/{h['id']}/decline",
        headers=recipient_headers,
        json={"reason": "not my area"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "declined"


@pytest.mark.asyncio
async def test_decline_by_non_recipient_404(
    client, auth_headers, sample_sfs_tar
):
    """Sender can't decline their own handoff — only recipients can."""
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    h = await _create_handoff(client, auth_headers, sid, recipient_email="other@x.com")
    r = await client.post(
        f"/api/v1/handoffs/{h['id']}/decline",
        headers=auth_headers,  # sender's own headers
        json={},
    )
    assert r.status_code == 404


# ── comments ──


@pytest.mark.asyncio
async def test_comment_thread_basic_flow(
    client, auth_headers, sample_sfs_tar, second_user
):
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    _, recipient_headers = second_user
    h = await _create_handoff(
        client, auth_headers, sid, recipient_email="recipient@example.com"
    )
    # Sender posts
    r1 = await client.post(
        f"/api/v1/handoffs/{h['id']}/comments",
        headers=auth_headers,
        json={"content": "Hi, can you pick this up tomorrow?"},
    )
    assert r1.status_code == 201
    # Recipient replies
    r2 = await client.post(
        f"/api/v1/handoffs/{h['id']}/comments",
        headers=recipient_headers,
        json={"content": "Sure, will do."},
    )
    assert r2.status_code == 201
    # Both see the thread
    listing = await client.get(
        f"/api/v1/handoffs/{h['id']}/comments", headers=auth_headers
    )
    assert listing.status_code == 200
    rows = listing.json()
    assert len(rows) == 2
    assert rows[0]["content"].startswith("Hi")
    assert rows[1]["content"].startswith("Sure")


@pytest.mark.asyncio
async def test_comment_outsider_404(
    client, auth_headers, sample_sfs_tar, second_user, db_session
):
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    h = await _create_handoff(client, auth_headers, sid, recipient_email="someone@x.com")
    # second_user is NOT the recipient (different email) and NOT the sender
    _, outsider_headers = second_user
    r = await client.post(
        f"/api/v1/handoffs/{h['id']}/comments",
        headers=outsider_headers,
        json={"content": "hi"},
    )
    assert r.status_code == 404


# ── events ──


@pytest.mark.asyncio
async def test_events_audit_log(
    client, auth_headers, sample_sfs_tar, second_user
):
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    _, recipient_headers = second_user
    h = await _create_handoff(
        client, auth_headers, sid, recipient_email="recipient@example.com"
    )
    # Recipient views (records viewed event)
    await client.get(f"/api/v1/handoffs/{h['id']}", headers=recipient_headers)
    # Recipient claims (records claimed event)
    await client.post(f"/api/v1/handoffs/{h['id']}/claim", headers=recipient_headers)
    # Events should include created + viewed + claimed
    ev = await client.get(
        f"/api/v1/handoffs/{h['id']}/events", headers=auth_headers
    )
    assert ev.status_code == 200
    types = [e["event_type"] for e in ev.json()]
    assert "created" in types
    assert "viewed" in types
    assert "claimed" in types


# ── lazy expiry + viewed_at ──


@pytest.mark.asyncio
async def test_get_handoff_returns_404_for_inaccessible(
    client, sample_sfs_tar, auth_headers, second_user
):
    """v0.10.9 — non-party callers see 404, not 403."""
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    h = await _create_handoff(
        client, auth_headers, sid, recipient_email="someone@x.com"
    )
    _, outsider_headers = second_user
    r = await client.get(f"/api/v1/handoffs/{h['id']}", headers=outsider_headers)
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_viewed_at_set_on_recipient_first_get(
    client, auth_headers, sample_sfs_tar, second_user
):
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    _, recipient_headers = second_user
    h = await _create_handoff(
        client, auth_headers, sid, recipient_email="recipient@example.com"
    )
    # First fetch by recipient
    g1 = await client.get(f"/api/v1/handoffs/{h['id']}", headers=recipient_headers)
    assert g1.status_code == 200
    assert g1.json()["viewed_at"] is not None
    # Sender's later fetch must not overwrite viewed_at
    g2 = await client.get(f"/api/v1/handoffs/{h['id']}", headers=auth_headers)
    assert g2.status_code == 200
    # Datetime serialization may differ in trailing 'Z'; compare normalized prefix.
    assert g2.json()["viewed_at"].rstrip("Z") == g1.json()["viewed_at"].rstrip("Z")


@pytest.mark.asyncio
async def test_lazy_expiry_on_get(
    client, auth_headers, sample_sfs_tar, db_session
):
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    h = await _create_handoff(
        client, auth_headers, sid, recipient_email="r@x.com"
    )
    # Backdate expires_at
    from sqlalchemy import update

    await db_session.execute(
        update(Handoff)
        .where(Handoff.id == h["id"])
        .values(expires_at=datetime.now(timezone.utc) - timedelta(hours=1))
    )
    await db_session.commit()
    g = await client.get(f"/api/v1/handoffs/{h['id']}", headers=auth_headers)
    assert g.status_code == 410
    # Status should now be persisted as 'expired'
    await db_session.refresh(
        (
            await db_session.execute(
                __import__("sqlalchemy").select(Handoff).where(Handoff.id == h["id"])
            )
        ).scalar_one()
    )


# ── claim: dropped attachments + race-loss ──


@pytest.mark.asyncio
async def test_claim_drops_inaccessible_attachments(
    client, auth_headers, sample_sfs_tar, db_session, test_user, second_user
):
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    sess = (
        await db_session.execute(
            __import__("sqlalchemy").select(__import__("sessionfs.server.db.models", fromlist=["Session"]).Session).where(
                __import__("sessionfs.server.db.models", fromlist=["Session"]).Session.id == sid
            )
        )
    ).scalar_one()
    git_remote = sess.git_remote_normalized or "github.com/x/y"
    if not sess.git_remote_normalized:
        sess.git_remote_normalized = git_remote
    proj = Project(
        id="proj_drop",
        git_remote_normalized=git_remote,
        name="X",
        owner_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(proj)
    page = KnowledgePage(
        id=str(uuid.uuid4()),
        project_id="proj_drop",
        slug="auth-flow",
        title="Auth Flow",
        page_type="article",
        content="x",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(page)
    await db_session.commit()

    user, recipient_headers = second_user
    h = await _create_handoff(
        client,
        auth_headers,
        sid,
        recipient_email=user.email,
        attachments=[{"kind": "wiki_page", "ref_id": "auth-flow"}],
    )
    # Simulate the attachment's underlying entity being deleted between
    # create-time validation and claim-time validation (race with KB
    # cleanup). The validator should drop the ref with reason='deleted'.
    await db_session.delete(page)
    await db_session.commit()

    claim = await client.post(
        f"/api/v1/handoffs/{h['id']}/claim", headers=recipient_headers
    )
    assert claim.status_code == 200
    body = claim.json()
    dropped = body.get("dropped_attachments") or []
    assert len(dropped) == 1
    assert dropped[0]["kind"] == "wiki_page"
    assert dropped[0]["reason"] == "deleted"


@pytest.mark.asyncio
async def test_claim_active_ticket_payload_present(
    client, auth_headers, sample_sfs_tar, db_session, test_user, second_user
):
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    sess = (
        await db_session.execute(
            __import__("sqlalchemy").select(__import__("sessionfs.server.db.models", fromlist=["Session"]).Session).where(
                __import__("sessionfs.server.db.models", fromlist=["Session"]).Session.id == sid
            )
        )
    ).scalar_one()
    git_remote = sess.git_remote_normalized or "github.com/x/y"
    if not sess.git_remote_normalized:
        sess.git_remote_normalized = git_remote
    # Org so recipient can access via OrgMember
    org = Organization(
        id="org_shared",
        name="shared",
        slug="shared",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(org)
    user, recipient_headers = second_user
    db_session.add(
        OrgMember(
            org_id="org_shared",
            user_id=test_user.id,
            role="admin",
            joined_at=datetime.now(timezone.utc),
        )
    )
    db_session.add(
        OrgMember(
            org_id="org_shared",
            user_id=user.id,
            role="member",
            joined_at=datetime.now(timezone.utc),
        )
    )
    proj = Project(
        id="proj_carry",
        org_id="org_shared",
        git_remote_normalized=git_remote,
        name="X",
        owner_id=test_user.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(proj)
    ticket = Ticket(
        id="tk_carry01",
        project_id="proj_carry",
        title="Pick up",
        priority="medium",
        status="in_progress",
        created_by_user_id=test_user.id,
        lease_epoch=3,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(ticket)
    await db_session.commit()

    h = await _create_handoff(
        client,
        auth_headers,
        sid,
        recipient_email=user.email,
        ticket_id="tk_carry01",
        persona_name=None,
    )
    claim = await client.post(
        f"/api/v1/handoffs/{h['id']}/claim", headers=recipient_headers
    )
    assert claim.status_code == 200
    payload = claim.json().get("active_ticket_payload")
    assert payload is not None
    assert payload["ticket_id"] == "tk_carry01"
    assert payload["project_id"] == "proj_carry"
    assert payload["lease_epoch"] == 3


# ── team CRUD ──


@pytest.fixture
async def org_admin_user(db_session: AsyncSession) -> tuple[User, str, dict]:
    """Create a user that is an admin of an org on Team tier."""
    user = User(
        id=str(uuid.uuid4()),
        email="admin@org.com",
        display_name="Admin",
        tier="team",
        created_at=datetime.now(timezone.utc),
        email_verified=True,
    )
    db_session.add(user)
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name="ACME",
        slug=f"acme-{uuid.uuid4().hex[:6]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(org)
    await db_session.flush()
    db_session.add(
        OrgMember(
            org_id=org.id,
            user_id=user.id,
            role="admin",
            joined_at=datetime.now(timezone.utc),
        )
    )
    raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name="org-admin",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    return user, org.id, {"Authorization": f"Bearer {raw}"}


@pytest.mark.asyncio
async def test_team_create_list_delete(client, org_admin_user):
    _, _, headers = org_admin_user
    create = await client.post(
        "/api/v1/teams",
        headers=headers,
        json={"name": "Backend", "slug": "backend"},
    )
    assert create.status_code == 201, create.text
    team_id = create.json()["id"]

    listing = await client.get("/api/v1/teams", headers=headers)
    assert listing.status_code == 200
    assert any(t["id"] == team_id for t in listing.json())

    detail = await client.get(f"/api/v1/teams/{team_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.json()["slug"] == "backend"

    deleted = await client.delete(f"/api/v1/teams/{team_id}", headers=headers)
    assert deleted.status_code == 204

    gone = await client.get(f"/api/v1/teams/{team_id}", headers=headers)
    assert gone.status_code == 404


@pytest.mark.asyncio
async def test_team_membership_add_list_remove(
    client, db_session, org_admin_user
):
    admin_user, org_id, admin_headers = org_admin_user
    # Add a second user to the same org
    member_user = User(
        id=str(uuid.uuid4()),
        email="mem@org.com",
        display_name="M",
        tier="team",
        created_at=datetime.now(timezone.utc),
        email_verified=True,
    )
    db_session.add(member_user)
    db_session.add(
        OrgMember(
            org_id=org_id,
            user_id=member_user.id,
            role="member",
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    create = await client.post(
        "/api/v1/teams",
        headers=admin_headers,
        json={"name": "Ops", "slug": "ops"},
    )
    team_id = create.json()["id"]

    add = await client.post(
        f"/api/v1/teams/{team_id}/members",
        headers=admin_headers,
        json={"user_id": member_user.id},
    )
    assert add.status_code == 201

    listing = await client.get(
        f"/api/v1/teams/{team_id}/members", headers=admin_headers
    )
    assert listing.status_code == 200
    assert any(m["user_id"] == member_user.id for m in listing.json())

    rm = await client.delete(
        f"/api/v1/teams/{team_id}/members/{member_user.id}", headers=admin_headers
    )
    assert rm.status_code == 204


@pytest.mark.asyncio
async def test_team_member_must_belong_to_org(
    client, db_session, org_admin_user
):
    _, _, admin_headers = org_admin_user
    create = await client.post(
        "/api/v1/teams",
        headers=admin_headers,
        json={"name": "X", "slug": "x"},
    )
    team_id = create.json()["id"]

    # Non-org user
    stranger = User(
        id=str(uuid.uuid4()),
        email="outsider@x.com",
        display_name="Out",
        tier="pro",
        created_at=datetime.now(timezone.utc),
        email_verified=True,
    )
    db_session.add(stranger)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/teams/{team_id}/members",
        headers=admin_headers,
        json={"user_id": stranger.id},
    )
    assert r.status_code == 422


# ── R3 LOW 2 — team handoff end-to-end ──


@pytest.mark.asyncio
async def test_team_handoff_member_claims_non_member_404(
    client, db_session, org_admin_user, sample_sfs_tar
):
    """End-to-end team handoff: sender (org admin) creates a team handoff;
    a team member sees it in inbox + can claim; a non-member sees 404 on
    GET and 404 on claim. This is the core v0.10.9 team-flow contract."""
    admin_user, org_id, admin_headers = org_admin_user

    # Create a team and add two members: one team member, one
    # org-but-not-team member.
    team_member = User(
        id=str(uuid.uuid4()),
        email="onteam@org.com",
        display_name="OnTeam",
        tier="team",
        created_at=datetime.now(timezone.utc),
        email_verified=True,
    )
    other_org_member = User(
        id=str(uuid.uuid4()),
        email="offteam@org.com",
        display_name="OffTeam",
        tier="team",
        created_at=datetime.now(timezone.utc),
        email_verified=True,
    )
    db_session.add_all([team_member, other_org_member])
    db_session.add_all(
        [
            OrgMember(
                org_id=org_id,
                user_id=team_member.id,
                role="member",
                joined_at=datetime.now(timezone.utc),
            ),
            OrgMember(
                org_id=org_id,
                user_id=other_org_member.id,
                role="member",
                joined_at=datetime.now(timezone.utc),
            ),
        ]
    )
    raw_member = generate_api_key()
    raw_off = generate_api_key()
    db_session.add_all(
        [
            ApiKey(
                id=str(uuid.uuid4()),
                user_id=team_member.id,
                key_hash=hash_api_key(raw_member),
                name="onteam",
                created_at=datetime.now(timezone.utc),
            ),
            ApiKey(
                id=str(uuid.uuid4()),
                user_id=other_org_member.id,
                key_hash=hash_api_key(raw_off),
                name="offteam",
                created_at=datetime.now(timezone.utc),
            ),
        ]
    )
    await db_session.commit()
    member_headers = {"Authorization": f"Bearer {raw_member}"}
    off_headers = {"Authorization": f"Bearer {raw_off}"}

    # Admin creates team + adds team_member AND themselves (the sender
    # has to be a team member to create a team handoff per route guard).
    create = await client.post(
        "/api/v1/teams",
        headers=admin_headers,
        json={"name": "Eng", "slug": "eng"},
    )
    assert create.status_code == 201, create.text
    team_id = create.json()["id"]
    for uid in (team_member.id, admin_user.id):
        add = await client.post(
            f"/api/v1/teams/{team_id}/members",
            headers=admin_headers,
            json={"user_id": uid},
        )
        assert add.status_code == 201, add.text

    # Admin pushes a session and creates a team handoff.
    sid = await _push_session(client, admin_headers, sample_sfs_tar)
    h = await _create_handoff(
        client,
        admin_headers,
        sid,
        recipient_team_id=team_id,
    )
    assert h["handoff_kind"] == "team"
    assert h["recipient_team_id"] == team_id

    # Team member sees the handoff in inbox.
    inbox_member = await client.get(
        "/api/v1/handoffs/inbox", headers=member_headers
    )
    assert inbox_member.status_code == 200
    assert any(x["id"] == h["id"] for x in inbox_member.json()["handoffs"])

    # Off-team org member does NOT see it in inbox.
    inbox_off = await client.get(
        "/api/v1/handoffs/inbox", headers=off_headers
    )
    assert inbox_off.status_code == 200
    assert not any(x["id"] == h["id"] for x in inbox_off.json()["handoffs"])

    # Off-team org member gets 404 on GET (existence hidden).
    get_off = await client.get(
        f"/api/v1/handoffs/{h['id']}", headers=off_headers
    )
    assert get_off.status_code == 404

    # Off-team org member gets 404 on claim attempt (existence hidden,
    # R3 HIGH 1 — eligibility check before status check).
    claim_off = await client.post(
        f"/api/v1/handoffs/{h['id']}/claim", headers=off_headers
    )
    assert claim_off.status_code == 404

    # Team member can claim successfully.
    claim_member = await client.post(
        f"/api/v1/handoffs/{h['id']}/claim", headers=member_headers
    )
    assert claim_member.status_code == 200, claim_member.text
    assert claim_member.json()["status"] == "claimed"


@pytest.mark.asyncio
async def test_outsider_decline_returns_404_not_409_on_non_pending(
    client, db_session, auth_headers, sample_sfs_tar, second_user
):
    """R3 HIGH 1 — non-recipient decline against a non-pending handoff
    must return 404 (existence hidden), not 409 (which would leak that
    the handoff exists in a non-pending state)."""
    sid = await _push_session(client, auth_headers, sample_sfs_tar)
    user, recipient_headers = second_user
    h = await _create_handoff(
        client, auth_headers, sid, recipient_email=user.email
    )
    # Recipient claims it — handoff is now in 'claimed' state.
    claim = await client.post(
        f"/api/v1/handoffs/{h['id']}/claim", headers=recipient_headers
    )
    assert claim.status_code == 200

    # Outsider (a fresh user) — they can't see the handoff exists. Make
    # one ad-hoc.
    outsider = User(
        id=str(uuid.uuid4()),
        email="outsider@nowhere.com",
        display_name="O",
        tier="pro",
        created_at=datetime.now(timezone.utc),
        email_verified=True,
    )
    raw = generate_api_key()
    db_session.add(outsider)
    await db_session.flush()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=outsider.id,
            key_hash=hash_api_key(raw),
            name="o",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    out_headers = {"Authorization": f"Bearer {raw}"}

    # Outsider decline against a claimed handoff: must be 404, NOT 409.
    # If we returned 409 here, outsider could confirm the handoff exists.
    r_decline = await client.post(
        f"/api/v1/handoffs/{h['id']}/decline",
        headers=out_headers,
        json={},
    )
    assert r_decline.status_code == 404, (
        f"outsider must get 404 (not 409) on decline against a non-pending "
        f"handoff to avoid leaking existence (got {r_decline.status_code})"
    )

    # Same for claim — outsider should never see 409 'already claimed'.
    r_claim = await client.post(
        f"/api/v1/handoffs/{h['id']}/claim", headers=out_headers
    )
    assert r_claim.status_code == 404
