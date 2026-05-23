"""v0.10.22 — org invite email + decline + resend + my-invites tests.

Pins tk_6afbcfefe5804c1d. Before this work the invite POST landed an
OrgInvite row and returned 200 but never emailed the recipient, and
the recipient had no in-product way to see the invite. The 4 CEO
signups on 2026-05-23 had to be unblocked by hand-messaged accept URLs.

These tests stub `app.state.email_service` with a recorder that
captures every `(to, subject, html)` tuple so we can assert:
  - both invite endpoints actually fire send_org_invite,
  - resend re-fires without creating a new invite row,
  - decline marks the invite refused and blocks subsequent accept,
  - /invites/me lists pending rows matching the user's email,
  - email-send failure is best-effort (does NOT 500 the invite route).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    OrgInvite,
    OrgMember,
    Organization,
    User,
)
from sessionfs.server.email import EmailProvider


# ── helpers ──


class RecordingProvider(EmailProvider):
    """In-memory email provider that records sends for assertions."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.fail_next = False

    async def send(self, to: str, subject: str, html: str) -> bool:
        if self.fail_next:
            self.fail_next = False
            return False
        self.sent.append({"to": to, "subject": subject, "html": html})
        return True


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _make_user(
    db: AsyncSession, *, email: str, tier: str = "team", display_name: str = "User"
) -> tuple[User, dict[str, str]]:
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        display_name=display_name,
        tier=tier,
        email_verified=True,
        created_at=_now(),
    )
    db.add(user)
    await db.flush()
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"key-{uuid.uuid4().hex[:6]}",
            created_at=_now(),
        )
    )
    await db.commit()
    return user, {"Authorization": f"Bearer {raw}"}


async def _make_org_with_admin(
    db: AsyncSession, user: User, *, tier: str = "team"
) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Test Org",
        slug=f"test-{uuid.uuid4().hex[:8]}",
        tier=tier,
        seats_limit=10,
    )
    db.add(org)
    await db.flush()
    db.add(
        OrgMember(
            org_id=org.id,
            user_id=user.id,
            role="admin",
            invited_by=user.id,
            invited_at=_now(),
        )
    )
    await db.commit()
    return org


@pytest.fixture
def recorder(client: AsyncClient) -> RecordingProvider:
    """Install a recording email provider on the test app."""
    rec = RecordingProvider()
    # client is the AsyncClient; we reach the underlying ASGI app via transport.
    app = client._transport.app  # type: ignore[attr-defined]
    app.state.email_service = rec
    return rec


# ── tests ──


@pytest.mark.asyncio
async def test_multi_org_invite_sends_email(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingProvider
):
    admin, admin_hdr = await _make_user(
        db_session, email="admin@example.com", display_name="Admin Person"
    )
    org = await _make_org_with_admin(db_session, admin)

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/members/invite",
        headers=admin_hdr,
        json={"email": "newhire@example.com", "role": "member"},
    )
    assert resp.status_code == 200, resp.text

    assert len(recorder.sent) == 1
    sent = recorder.sent[0]
    assert sent["to"] == "newhire@example.com"
    assert "Admin Person" in sent["subject"]
    assert org.name in sent["subject"]
    assert "/invites/" in sent["html"]

    # last_emailed_at stamped on the invite row.
    invite = (
        await db_session.execute(
            select(OrgInvite).where(OrgInvite.email == "newhire@example.com")
        )
    ).scalar_one()
    assert invite.last_emailed_at is not None


@pytest.mark.asyncio
async def test_legacy_invite_sends_email(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingProvider
):
    """Legacy /api/v1/org/invite endpoint now fires email too."""
    admin, admin_hdr = await _make_user(
        db_session, email="legacy-admin@example.com", display_name="Legacy Admin"
    )
    await _make_org_with_admin(db_session, admin)

    resp = await client.post(
        "/api/v1/org/invite",
        headers=admin_hdr,
        json={"email": "legacy-invitee@example.com", "role": "member"},
    )
    assert resp.status_code == 200, resp.text

    assert len(recorder.sent) == 1
    assert recorder.sent[0]["to"] == "legacy-invitee@example.com"


@pytest.mark.asyncio
async def test_invite_route_does_not_500_when_email_fails(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingProvider
):
    """Best-effort send: provider failure must not 500 the invite route."""
    admin, admin_hdr = await _make_user(db_session, email="be-admin@example.com")
    org = await _make_org_with_admin(db_session, admin)

    recorder.fail_next = True
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/members/invite",
        headers=admin_hdr,
        json={"email": "be-invitee@example.com", "role": "member"},
    )
    assert resp.status_code == 200, resp.text  # invite row persisted

    invite = (
        await db_session.execute(
            select(OrgInvite).where(OrgInvite.email == "be-invitee@example.com")
        )
    ).scalar_one()
    # No last_emailed_at stamp on a failed send — but the invite row exists.
    assert invite.last_emailed_at is None


@pytest.mark.asyncio
async def test_resend_invite_does_not_create_new_row(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingProvider
):
    admin, admin_hdr = await _make_user(db_session, email="resend-admin@example.com")
    org = await _make_org_with_admin(db_session, admin)

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/members/invite",
        headers=admin_hdr,
        json={"email": "resend-target@example.com", "role": "member"},
    )
    assert resp.status_code == 200
    invite_id = resp.json()["invite_id"]
    assert len(recorder.sent) == 1

    # Resend.
    resend = await client.post(
        f"/api/v1/orgs/{org.id}/invites/{invite_id}/resend",
        headers=admin_hdr,
    )
    assert resend.status_code == 200, resend.text
    body = resend.json()
    assert body["invite_id"] == invite_id
    assert body["sent"] is True
    assert body["last_emailed_at"] is not None

    # Exactly two sends, one OrgInvite row.
    assert len(recorder.sent) == 2
    rows = (
        await db_session.execute(
            select(OrgInvite).where(OrgInvite.email == "resend-target@example.com")
        )
    ).scalars().all()
    assert len(rows) == 1


@pytest.mark.asyncio
async def test_resend_invite_admin_only(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingProvider
):
    admin, admin_hdr = await _make_user(db_session, email="resend-owner@example.com")
    org = await _make_org_with_admin(db_session, admin)

    # Outsider with no membership.
    _, outsider_hdr = await _make_user(db_session, email="outsider@example.com")

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/members/invite",
        headers=admin_hdr,
        json={"email": "victim@example.com", "role": "member"},
    )
    invite_id = resp.json()["invite_id"]

    resend = await client.post(
        f"/api/v1/orgs/{org.id}/invites/{invite_id}/resend",
        headers=outsider_hdr,
    )
    assert resend.status_code == 403, resend.text


@pytest.mark.asyncio
async def test_decline_invite_blocks_subsequent_accept(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingProvider
):
    admin, admin_hdr = await _make_user(db_session, email="decline-admin@example.com")
    org = await _make_org_with_admin(db_session, admin)

    invitee_email = "decliner@example.com"
    invitee, invitee_hdr = await _make_user(db_session, email=invitee_email, tier="free")

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/members/invite",
        headers=admin_hdr,
        json={"email": invitee_email, "role": "member"},
    )
    invite_id = resp.json()["invite_id"]

    # Decline as the invitee.
    decline = await client.post(
        f"/api/v1/org/invite/{invite_id}/decline",
        headers=invitee_hdr,
        json={"reason": "wrong account"},
    )
    assert decline.status_code == 200, decline.text

    # Accept now refuses.
    accept = await client.post(
        f"/api/v1/org/invite/{invite_id}/accept",
        headers=invitee_hdr,
    )
    assert accept.status_code == 400
    assert "declined" in accept.json()["error"]["message"].lower()


@pytest.mark.asyncio
async def test_decline_wrong_email_denied(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingProvider
):
    """Only the addressed recipient can decline — defense against
    a stranger declining someone else's pending invite."""
    admin, admin_hdr = await _make_user(db_session, email="dwe-admin@example.com")
    org = await _make_org_with_admin(db_session, admin)
    _, outsider_hdr = await _make_user(db_session, email="dwe-outsider@example.com", tier="free")

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/members/invite",
        headers=admin_hdr,
        json={"email": "dwe-real-target@example.com", "role": "member"},
    )
    invite_id = resp.json()["invite_id"]

    decline = await client.post(
        f"/api/v1/org/invite/{invite_id}/decline",
        headers=outsider_hdr,
    )
    assert decline.status_code == 403


@pytest.mark.asyncio
async def test_list_my_invites_returns_pending_for_user(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingProvider
):
    admin, admin_hdr = await _make_user(db_session, email="lmi-admin@example.com")
    org = await _make_org_with_admin(db_session, admin)

    invitee_email = "lmi-target@example.com"
    _, invitee_hdr = await _make_user(db_session, email=invitee_email, tier="free")

    await client.post(
        f"/api/v1/orgs/{org.id}/members/invite",
        headers=admin_hdr,
        json={"email": invitee_email, "role": "member"},
    )

    resp = await client.get("/api/v1/org/invites/me", headers=invitee_hdr)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["invites"]) == 1
    row = body["invites"][0]
    assert row["org_name"] == org.name
    assert row["invited_by_email"] == "lmi-admin@example.com"
    assert row["role"] == "member"


@pytest.mark.asyncio
async def test_list_my_invites_hides_accepted_and_declined(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingProvider
):
    """Accepted, declined, and expired invites must not appear in /invites/me."""
    admin, admin_hdr = await _make_user(db_session, email="hide-admin@example.com")
    org = await _make_org_with_admin(db_session, admin)

    accepted_email = "hide-accepted@example.com"
    declined_email = "hide-declined@example.com"
    pending_email = "hide-pending@example.com"

    _, accepted_hdr = await _make_user(db_session, email=accepted_email, tier="free")
    _, declined_hdr = await _make_user(db_session, email=declined_email, tier="free")
    _, pending_hdr = await _make_user(db_session, email=pending_email, tier="free")

    for email in (accepted_email, declined_email, pending_email):
        await client.post(
            f"/api/v1/orgs/{org.id}/members/invite",
            headers=admin_hdr,
            json={"email": email, "role": "member"},
        )

    accepted_invite_id = (
        await db_session.execute(
            select(OrgInvite.id).where(OrgInvite.email == accepted_email)
        )
    ).scalar_one()
    declined_invite_id = (
        await db_session.execute(
            select(OrgInvite.id).where(OrgInvite.email == declined_email)
        )
    ).scalar_one()

    assert (
        await client.post(
            f"/api/v1/org/invite/{accepted_invite_id}/accept", headers=accepted_hdr
        )
    ).status_code == 200
    assert (
        await client.post(
            f"/api/v1/org/invite/{declined_invite_id}/decline", headers=declined_hdr
        )
    ).status_code == 200

    # Each invitee's /invites/me reflects only THEIR own pending state.
    pending_resp = await client.get("/api/v1/org/invites/me", headers=pending_hdr)
    assert len(pending_resp.json()["invites"]) == 1

    accepted_resp = await client.get("/api/v1/org/invites/me", headers=accepted_hdr)
    assert accepted_resp.json()["invites"] == []

    declined_resp = await client.get("/api/v1/org/invites/me", headers=declined_hdr)
    assert declined_resp.json()["invites"] == []


@pytest.mark.asyncio
async def test_resend_refuses_accepted_invite(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingProvider
):
    admin, admin_hdr = await _make_user(db_session, email="raa-admin@example.com")
    org = await _make_org_with_admin(db_session, admin)

    invitee_email = "raa-target@example.com"
    _, invitee_hdr = await _make_user(db_session, email=invitee_email, tier="free")

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/members/invite",
        headers=admin_hdr,
        json={"email": invitee_email, "role": "member"},
    )
    invite_id = resp.json()["invite_id"]
    await client.post(
        f"/api/v1/org/invite/{invite_id}/accept", headers=invitee_hdr
    )

    resend = await client.post(
        f"/api/v1/orgs/{org.id}/invites/{invite_id}/resend", headers=admin_hdr
    )
    assert resend.status_code == 409
