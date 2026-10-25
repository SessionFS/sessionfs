"""GH #96 — admin-proposed project transfer with owner consent.

Covers the propose → accept / decline / expire flow end-to-end against the
new ``POST /api/v1/orgs/{org_id}/projects/{project_id}/propose-transfer``
route, plus the proposal-aware accept/reject/list paths:

    - propose happy path (pending, addressed to owner, 7-day expiry, email)
    - auth matrix: non-admin of destination 403, admin of another org 403
    - destination org missing 404, already-in-org 409, duplicate pending 409
    - accept re-runs the owner-standing + destination-membership checks
    - decline/expire notify the proposer (best-effort email)
    - AdminAction rows for propose / accept / decline / expire
    - list resolves ``proposed_by_name`` for the owner's inbox
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AdminAction,
    ApiKey,
    OrgMember,
    Organization,
    Project,
    ProjectTransfer,
    User,
)
from sessionfs.server.email import EmailProvider


class RecordingProvider(EmailProvider):
    """In-memory email provider that records sends for assertions."""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []

    async def send(self, to: str, subject: str, html: str) -> bool:
        self.sent.append({"to": to, "subject": subject, "html": html})
        return True


@pytest.fixture
def recorder(client: AsyncClient) -> RecordingProvider:
    """Install a recording email provider on the test app."""
    rec = RecordingProvider()
    app = client._transport.app  # type: ignore[attr-defined]
    app.state.email_service = rec
    return rec


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _make_user(
    db: AsyncSession, name: str = "alice"
) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier="team",
        email_verified=True,
        created_at=_now(),
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
            created_at=_now(),
        )
    )
    await db.commit()
    return user, raw


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _make_org(
    db: AsyncSession, admin: User | None = None, name: str = "Test Org"
) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name=name,
        slug=f"test-{uuid.uuid4().hex[:8]}",
        tier="team",
    )
    db.add(org)
    await db.commit()
    if admin is not None:
        db.add(OrgMember(org_id=org.id, user_id=admin.id, role="admin"))
        await db.commit()
    await db.refresh(org)
    return org


async def _add_member(
    db: AsyncSession, org: Organization, user: User, role: str = "member"
) -> None:
    db.add(OrgMember(org_id=org.id, user_id=user.id, role=role))
    await db.commit()


async def _remove_member(
    db: AsyncSession, org: Organization, user: User
) -> None:
    await db.execute(
        delete(OrgMember).where(
            OrgMember.org_id == org.id, OrgMember.user_id == user.id
        )
    )
    await db.commit()


async def _set_member_role(
    db: AsyncSession, org: Organization, user: User, role: str
) -> None:
    await db.execute(
        update(OrgMember)
        .where(OrgMember.org_id == org.id, OrgMember.user_id == user.id)
        .values(role=role)
    )
    await db.commit()


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


async def _propose(
    client: AsyncClient,
    admin_key: str,
    org: Organization,
    project: Project,
    reason: str = "Consolidate under the platform org",
) -> dict:
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/projects/{project.id}/propose-transfer",
        json={"reason": reason},
        headers=_hdrs(admin_key),
    )
    return {
        "status": resp.status_code,
        "body": resp.json(),
        "text": resp.text,
        "headers": resp.headers,
    }


async def _audit_actions(
    db: AsyncSession, project_id: str
) -> list[AdminAction]:
    return (
        await db.execute(
            select(AdminAction)
            .where(AdminAction.target_id == project_id)
            .order_by(AdminAction.created_at)
        )
    ).scalars().all()


# ── propose ──


@pytest.mark.asyncio
async def test_propose_creates_pending_proposal_and_emails_owner(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingProvider
) -> None:
    alice, _ = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    await _add_member(db_session, org, alice, role="member")
    project = await _make_project(db_session, owner=alice, org_id=None)

    result = await _propose(client, admin_key, org, project)
    assert result["status"] == 200, result["text"]
    body = result["body"]
    # GH #96 review round 1 (M1) — the propose response is deliberately
    # minimal: only transfer_id, project_id, status and expires_at. No
    # owner/target/scope fields that could leak an existence/ownership oracle.
    assert set(body.keys()) == {"transfer_id", "project_id", "status", "expires_at"}
    assert body["project_id"] == project.id
    assert body["status"] == "pending"

    expires = datetime.fromisoformat(body["expires_at"])
    if expires.tzinfo is None:  # SQLite roundtrips timezone-aware columns naive
        expires = expires.replace(tzinfo=timezone.utc)
    remaining = expires - _now()
    assert timedelta(days=6, hours=23) < remaining < timedelta(days=7, hours=1)

    # The project has not moved.
    await db_session.refresh(project)
    assert project.org_id is None

    # The owner is emailed the proposal.
    assert len(recorder.sent) == 1
    sent = recorder.sent[0]
    assert sent["to"] == alice.email
    assert "transfer" in sent["subject"].lower()
    assert org.name in sent["html"]
    assert "Consolidate under the platform org" in sent["html"]

    # Audit row names the proposer and the owner.
    actions = await _audit_actions(db_session, project.id)
    assert len(actions) == 1
    assert actions[0].action == "project_transfer_proposed"
    assert actions[0].source == "api"
    details = json.loads(actions[0].details)
    assert details["proposer_user_id"] == admin.id
    assert details["owner_user_id"] == alice.id


@pytest.mark.asyncio
async def test_propose_rejects_blank_or_overlong_reason(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, _ = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    project = await _make_project(db_session, owner=alice, org_id=None)

    blank = await _propose(client, admin_key, org, project, reason="   ")
    assert blank["status"] == 422

    overlong = await _propose(client, admin_key, org, project, reason="x" * 1001)
    assert overlong["status"] == 422


@pytest.mark.asyncio
async def test_propose_non_admin_of_destination_is_403(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, _ = await _make_user(db_session, "alice")
    admin, _ = await _make_user(db_session, "admin")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org(db_session, admin=admin)
    await _add_member(db_session, org, member, role="member")
    project = await _make_project(db_session, owner=alice, org_id=None)

    result = await _propose(client, member_key, org, project)
    assert result["status"] == 403, result["text"]


@pytest.mark.asyncio
async def test_propose_admin_of_different_org_is_403(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, _ = await _make_user(db_session, "alice")
    other_admin, other_key = await _make_user(db_session, "other_admin")
    other_org = await _make_org(db_session, admin=other_admin, name="Other Org")
    dest_admin, _ = await _make_user(db_session, "dest_admin")
    dest_org = await _make_org(db_session, admin=dest_admin, name="Dest Org")
    project = await _make_project(db_session, owner=alice, org_id=None)

    # other_admin is an admin of other_org, NOT dest_org.
    result = await _propose(client, other_key, dest_org, project)
    assert result["status"] == 403, result["text"]

    # Sanity: other_admin CAN propose into their own org once the owner is a
    # member of it (proposals are personal-project + owner-member only).
    await _add_member(db_session, other_org, alice, role="member")
    ok = await _propose(client, other_key, other_org, project)
    assert ok["status"] == 200, ok["text"]


@pytest.mark.asyncio
async def test_propose_destination_org_not_found_is_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, _ = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    await _make_org(db_session, admin=admin)
    project = await _make_project(db_session, owner=alice, org_id=None)

    resp = await client.post(
        f"/api/v1/orgs/org_does_not_exist/projects/{project.id}/propose-transfer",
        json={"reason": "Consolidate"},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_propose_project_already_in_destination_org_is_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, _ = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    # The project already lives in the destination org.
    project = await _make_project(db_session, owner=alice, org_id=org.id)

    result = await _propose(client, admin_key, org, project)
    assert result["status"] == 409
    assert result["body"]["error"]["code"] == "already_in_org"


@pytest.mark.asyncio
async def test_propose_duplicate_pending_is_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, _ = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    await _add_member(db_session, org, alice, role="member")
    project = await _make_project(db_session, owner=alice, org_id=None)

    first = await _propose(client, admin_key, org, project)
    assert first["status"] == 200, first["text"]

    second = await _propose(client, admin_key, org, project)
    assert second["status"] == 409, second["text"]
    # GH #96 review round 1 (L5) — the duplicate refusal carries a structured
    # error code the CLI/docs can rely on.
    assert second["body"]["error"]["code"] == "transfer_pending"


# ── accept ──


@pytest.mark.asyncio
async def test_accept_proposal_moves_project_and_writes_audit(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, alice_key = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    await _add_member(db_session, org, alice, role="member")
    project = await _make_project(db_session, owner=alice, org_id=None)

    proposed = await _propose(client, admin_key, org, project)
    xfer_id = proposed["body"]["transfer_id"]

    resp = await client.post(
        f"/api/v1/transfers/{xfer_id}/accept", headers=_hdrs(alice_key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["state"] == "accepted"
    assert body["accepted_by"] == alice.id

    await db_session.refresh(project)
    assert project.org_id == org.id

    actions = await _audit_actions(db_session, project.id)
    assert [a.action for a in actions] == [
        "project_transfer_proposed",
        "project_transfer_proposal_accepted",
    ]
    accepted = actions[1]
    assert json.loads(accepted.after_state)["scope"] == org.id
    assert json.loads(accepted.before_state)["scope"] == "personal"


@pytest.mark.asyncio
async def test_accept_proposal_fails_when_owner_loses_destination_membership(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingProvider
) -> None:
    alice, alice_key = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    await _add_member(db_session, org, alice, role="member")
    project = await _make_project(db_session, owner=alice, org_id=None)

    proposed = await _propose(client, admin_key, org, project)
    xfer_id = proposed["body"]["transfer_id"]
    recorder.sent.clear()

    # The owner is removed from the destination org before they accept.
    await _remove_member(db_session, org, alice)

    resp = await client.post(
        f"/api/v1/transfers/{xfer_id}/accept", headers=_hdrs(alice_key)
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "proposal_no_longer_valid"

    # The proposal is marked expired (not left pending), and the proposer is
    # notified of the expiry.
    transfer = (
        await db_session.execute(
            select(ProjectTransfer).where(ProjectTransfer.id == xfer_id)
        )
    ).scalar_one()
    assert transfer.state == "expired"
    assert len(recorder.sent) == 1
    assert recorder.sent[0]["to"] == admin.email
    assert "expired" in recorder.sent[0]["subject"].lower()

    await db_session.refresh(project)
    assert project.org_id is None


# ── decline ──


@pytest.mark.asyncio
async def test_decline_proposal_notifies_proposer_and_writes_audit(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingProvider
) -> None:
    alice, alice_key = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    # alice must be a member for the proposal to be created; declining never
    # moves the project, so membership is not re-checked at decline time.
    await _add_member(db_session, org, alice, role="member")
    project = await _make_project(db_session, owner=alice, org_id=None)

    proposed = await _propose(client, admin_key, org, project)
    xfer_id = proposed["body"]["transfer_id"]
    recorder.sent.clear()  # drop the propose email

    resp = await client.post(
        f"/api/v1/transfers/{xfer_id}/reject", headers=_hdrs(alice_key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "rejected"

    await db_session.refresh(project)
    assert project.org_id is None

    # Proposer notified of the decline.
    assert len(recorder.sent) == 1
    assert recorder.sent[0]["to"] == admin.email
    assert "declined" in recorder.sent[0]["subject"].lower()

    actions = await _audit_actions(db_session, project.id)
    assert [a.action for a in actions] == [
        "project_transfer_proposed",
        "project_transfer_proposal_declined",
    ]


# ── expiry ──


@pytest.mark.asyncio
async def test_expired_proposal_lazily_expires_and_notifies_proposer(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingProvider
) -> None:
    alice, alice_key = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    await _add_member(db_session, org, alice, role="member")
    project = await _make_project(db_session, owner=alice, org_id=None)

    proposed = await _propose(client, admin_key, org, project)
    xfer_id = proposed["body"]["transfer_id"]
    recorder.sent.clear()

    # Backdate the expiry so the proposal is already past its window.
    transfer = (
        await db_session.execute(
            select(ProjectTransfer).where(ProjectTransfer.id == xfer_id)
        )
    ).scalar_one()
    transfer.expires_at = _now() - timedelta(minutes=1)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/transfers/{xfer_id}/accept", headers=_hdrs(alice_key)
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["message"] == "Transfer has expired"

    await db_session.refresh(transfer)
    assert transfer.state == "expired"

    # Proposer notified of the expiry.
    assert len(recorder.sent) == 1
    assert recorder.sent[0]["to"] == admin.email
    assert "expired" in recorder.sent[0]["subject"].lower()

    actions = await _audit_actions(db_session, project.id)
    assert [a.action for a in actions] == [
        "project_transfer_proposed",
        "project_transfer_proposal_expired",
    ]

    # The slot frees up: a fresh proposal is accepted again.
    again = await _propose(client, admin_key, org, project)
    assert again["status"] == 200, again["text"]


# ── list ──


@pytest.mark.asyncio
async def test_list_incoming_resolves_proposed_by_name(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    alice, alice_key = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    await _add_member(db_session, org, alice, role="member")
    project = await _make_project(db_session, owner=alice, org_id=None)

    await _propose(client, admin_key, org, project)

    incoming = await client.get(
        "/api/v1/transfers?direction=incoming", headers=_hdrs(alice_key)
    )
    assert incoming.status_code == 200
    rows = incoming.json()["transfers"]
    assert len(rows) == 1
    assert rows[0]["proposed_by_user_id"] == admin.id
    assert rows[0]["proposed_by_name"] == admin.display_name
    assert rows[0]["proposal_reason"] == "Consolidate under the platform org"
    assert rows[0]["expires_at"] is not None


# ── GH #96 review round 1 regressions ──


@pytest.mark.asyncio
async def test_propose_org_project_by_source_member_is_409_personal_only(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """H1 — a plain member of the source org cannot yank an org-scoped project
    out by proposing it into an org they themselves own: the proposal path
    must not bypass the source-org admin gate."""
    owner, _ = await _make_user(db_session, "owner")
    source_admin, _ = await _make_user(db_session, "source_admin")
    source_org = await _make_org(db_session, admin=source_admin, name="Source Org")

    member, member_key = await _make_user(db_session, "member")
    await _add_member(db_session, source_org, member, role="member")
    # The plain member also owns their own destination org.
    dest_org = await _make_org(db_session, admin=member, name="Member Org")

    project = await _make_project(db_session, owner=owner, org_id=source_org.id)

    result = await _propose(client, member_key, dest_org, project)
    assert result["status"] == 409, result["text"]
    assert result["body"]["error"]["code"] == "proposal_personal_only"


@pytest.mark.asyncio
async def test_propose_org_project_by_unrelated_admin_is_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """M1 — an admin with no relationship to an org-scoped project gets the
    same 404 as a missing project (no existence/scope oracle)."""
    owner, _ = await _make_user(db_session, "owner")
    source_admin, _ = await _make_user(db_session, "source_admin")
    source_org = await _make_org(db_session, admin=source_admin, name="Source Org")
    project = await _make_project(db_session, owner=owner, org_id=source_org.id)

    unrelated, unrelated_key = await _make_user(db_session, "unrelated")
    unrelated_org = await _make_org(db_session, admin=unrelated, name="Unrelated Org")

    result = await _propose(client, unrelated_key, unrelated_org, project)
    assert result["status"] == 404, result["text"]


@pytest.mark.asyncio
async def test_propose_personal_project_owner_not_member_is_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """M1 — a personal project whose owner is not a member of the destination
    org is indistinguishable from a missing project (uniform 404)."""
    alice, _ = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    project = await _make_project(db_session, owner=alice, org_id=None)

    result = await _propose(client, admin_key, org, project)
    assert result["status"] == 404, result["text"]


@pytest.mark.asyncio
async def test_propose_missing_project_is_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """M1 — a missing project returns the same 404 as every other
    no-relationship refusal."""
    alice, _ = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    await _add_member(db_session, org, alice, role="member")

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/projects/proj_does_not_exist/propose-transfer",
        json={"reason": "Consolidate"},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_decline_email_uses_owner_display_name_not_email(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingProvider
) -> None:
    """M1 — the proposer's decline email identifies the owner by display name,
    never leaking the owner's email address."""
    alice, alice_key = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    await _add_member(db_session, org, alice, role="member")
    project = await _make_project(db_session, owner=alice, org_id=None)

    proposed = await _propose(client, admin_key, org, project)
    xfer_id = proposed["body"]["transfer_id"]
    recorder.sent.clear()

    resp = await client.post(
        f"/api/v1/transfers/{xfer_id}/reject", headers=_hdrs(alice_key)
    )
    assert resp.status_code == 200, resp.text

    assert len(recorder.sent) == 1
    html = recorder.sent[0]["html"]
    assert alice.email not in html
    assert alice.display_name in html


@pytest.mark.asyncio
async def test_propose_after_decline_is_cooldown_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """M2 — a declined proposal puts the project+org on a 7-day cooldown."""
    alice, alice_key = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    await _add_member(db_session, org, alice, role="member")
    project = await _make_project(db_session, owner=alice, org_id=None)

    proposed = await _propose(client, admin_key, org, project)
    xfer_id = proposed["body"]["transfer_id"]

    declined = await client.post(
        f"/api/v1/transfers/{xfer_id}/reject", headers=_hdrs(alice_key)
    )
    assert declined.status_code == 200, declined.text

    again = await _propose(client, admin_key, org, project)
    assert again["status"] == 409, again["text"]
    assert again["body"]["error"]["code"] == "proposal_cooldown"
    retry_after = again["body"]["error"]["details"]["retry_after"]
    assert 0 < retry_after <= 7 * 24 * 3600
    assert again["headers"]["retry-after"] == str(retry_after)


@pytest.mark.asyncio
async def test_propose_after_cancel_is_cooldown_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """M2 — a cancelled proposal puts the project+org on a 24-hour cooldown."""
    alice, _ = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    await _add_member(db_session, org, alice, role="member")
    project = await _make_project(db_session, owner=alice, org_id=None)

    proposed = await _propose(client, admin_key, org, project)
    xfer_id = proposed["body"]["transfer_id"]

    cancelled = await client.post(
        f"/api/v1/transfers/{xfer_id}/cancel", headers=_hdrs(admin_key)
    )
    assert cancelled.status_code == 200, cancelled.text

    again = await _propose(client, admin_key, org, project)
    assert again["status"] == 409, again["text"]
    assert again["body"]["error"]["code"] == "proposal_cooldown"
    retry_after = again["body"]["error"]["details"]["retry_after"]
    assert 0 < retry_after <= 24 * 3600


@pytest.mark.asyncio
async def test_propose_org_pending_cap_is_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """M2 — a proposing org may hold at most 10 pending proposals."""
    alice, _ = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    await _add_member(db_session, org, alice, role="member")

    for i in range(10):
        project = await _make_project(
            db_session, owner=alice, org_id=None, name=f"project-{i}"
        )
        result = await _propose(client, admin_key, org, project)
        assert result["status"] == 200, result["text"]

    eleventh = await _make_project(db_session, owner=alice, org_id=None, name="project-11")
    result = await _propose(client, admin_key, org, eleventh)
    assert result["status"] == 409, result["text"]
    assert result["body"]["error"]["code"] == "proposal_limit"


@pytest.mark.asyncio
async def test_accept_proposal_fails_when_proposer_demoted(
    client: AsyncClient, db_session: AsyncSession, recorder: RecordingProvider
) -> None:
    """M3 — if the proposer is no longer an owner/admin of the destination org,
    accept fails cleanly (409 proposal_no_longer_valid) and the proposal is
    marked expired."""
    alice, alice_key = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    await _add_member(db_session, org, alice, role="member")
    project = await _make_project(db_session, owner=alice, org_id=None)

    proposed = await _propose(client, admin_key, org, project)
    xfer_id = proposed["body"]["transfer_id"]
    recorder.sent.clear()

    # Demote the proposer from admin to plain member.
    await _set_member_role(db_session, org, admin, "member")

    resp = await client.post(
        f"/api/v1/transfers/{xfer_id}/accept", headers=_hdrs(alice_key)
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "proposal_no_longer_valid"

    transfer = (
        await db_session.execute(
            select(ProjectTransfer).where(ProjectTransfer.id == xfer_id)
        )
    ).scalar_one()
    assert transfer.state == "expired"
    assert len(recorder.sent) == 1
    assert recorder.sent[0]["to"] == admin.email

    await db_session.refresh(project)
    assert project.org_id is None


@pytest.mark.asyncio
async def test_expired_proposal_does_not_block_owner_initiated_transfer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """L1 — an overdue proposal is lazily expired so it never blocks an
    owner-initiated transfer of the same project."""
    alice, alice_key = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    await _add_member(db_session, org, alice, role="member")
    project = await _make_project(db_session, owner=alice, org_id=None)

    proposed = await _propose(client, admin_key, org, project)
    xfer_id = proposed["body"]["transfer_id"]

    # Backdate the proposal so it is already past its window but still pending.
    transfer = (
        await db_session.execute(
            select(ProjectTransfer).where(ProjectTransfer.id == xfer_id)
        )
    ).scalar_one()
    transfer.expires_at = _now() - timedelta(minutes=1)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer",
        json={"to": org.id},
        headers=_hdrs(alice_key),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["state"] == "accepted"

    await db_session.refresh(transfer)
    assert transfer.state == "expired"

    await db_session.refresh(project)
    assert project.org_id == org.id


@pytest.mark.asyncio
async def test_list_never_shows_overdue_proposal_as_pending(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """L1 — an overdue proposal is lazily expired on list, so it never shows
    up under state=pending."""
    alice, alice_key = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    await _add_member(db_session, org, alice, role="member")
    project = await _make_project(db_session, owner=alice, org_id=None)

    proposed = await _propose(client, admin_key, org, project)
    xfer_id = proposed["body"]["transfer_id"]

    transfer = (
        await db_session.execute(
            select(ProjectTransfer).where(ProjectTransfer.id == xfer_id)
        )
    ).scalar_one()
    transfer.expires_at = _now() - timedelta(minutes=1)
    await db_session.commit()

    pending = await client.get(
        "/api/v1/transfers?direction=incoming&state=pending",
        headers=_hdrs(alice_key),
    )
    assert pending.status_code == 200
    assert pending.json()["transfers"] == []

    all_rows = await client.get(
        "/api/v1/transfers?direction=incoming", headers=_hdrs(alice_key)
    )
    assert all_rows.status_code == 200
    rows = all_rows.json()["transfers"]
    assert len(rows) == 1
    assert rows[0]["state"] == "expired"


# ── GH #96 review round 2 regressions ──


@pytest.mark.asyncio
async def test_propose_merged_stranger_project_is_uniform_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """M1 (review round 2) — a merged (tombstoned) project a stranger proposes
    returns the same 404 body as a missing id. Round 1 took the scope lock
    first and leaked 409 project_merged + merged_into_project_id."""
    alice, _ = await _make_user(db_session, "alice")
    target = await _make_project(db_session, owner=alice, org_id=None, name="target")
    merged = await _make_project(db_session, owner=alice, org_id=None, name="merged")
    merged.merged_into_project_id = target.id
    await db_session.commit()

    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)

    missing = await client.post(
        f"/api/v1/orgs/{org.id}/projects/proj_does_not_exist/propose-transfer",
        json={"reason": "Consolidate"},
        headers=_hdrs(admin_key),
    )
    assert missing.status_code == 404

    merged_result = await _propose(client, admin_key, org, merged)
    assert merged_result["status"] == 404
    assert merged_result["text"] == missing.text


@pytest.mark.asyncio
async def test_propose_unrelated_projects_take_no_lock(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M1/M3 (review round 2) — every uniform-404 refusal (missing, merged,
    org-scoped-stranger, owner-not-member) is returned before any scope lock,
    so the pre-lock path stays at one project SELECT plus at most one
    membership check. The happy path still takes the lock exactly once."""
    import sessionfs.server.routes.project_transfers as transfers

    alice, _ = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)

    # An org-scoped project in an org the admin does not belong to.
    source_admin, _ = await _make_user(db_session, "source_admin")
    source_org = await _make_org(db_session, admin=source_admin, name="Source Org")
    org_project = await _make_project(
        db_session, owner=alice, org_id=source_org.id, name="org-project"
    )

    # A merged personal project owned by a stranger.
    target = await _make_project(db_session, owner=alice, org_id=None, name="target")
    merged = await _make_project(db_session, owner=alice, org_id=None, name="merged")
    merged.merged_into_project_id = target.id
    await db_session.commit()

    # A personal project whose owner is not a member of the destination org.
    personal = await _make_project(db_session, owner=alice, org_id=None, name="personal")

    calls: list[str] = []
    original = transfers.lock_project_scope

    async def spy(db, project, *, extra_org_ids=(), require_live=True):
        calls.append(project.id)
        await original(db, project, extra_org_ids=extra_org_ids, require_live=require_live)

    monkeypatch.setattr(transfers, "lock_project_scope", spy)

    missing = await client.post(
        f"/api/v1/orgs/{org.id}/projects/proj_does_not_exist/propose-transfer",
        json={"reason": "Consolidate"},
        headers=_hdrs(admin_key),
    )
    assert missing.status_code == 404
    assert (await _propose(client, admin_key, org, merged))["status"] == 404
    assert (await _propose(client, admin_key, org, org_project))["status"] == 404
    assert (await _propose(client, admin_key, org, personal))["status"] == 404

    # No refusal above took the scope lock.
    assert calls == []

    # The happy path still locks exactly once.
    await _add_member(db_session, org, alice, role="member")
    happy_project = await _make_project(db_session, owner=alice, org_id=None, name="happy")
    ok = await _propose(client, admin_key, org, happy_project)
    assert ok["status"] == 200, ok["text"]
    assert calls == [happy_project.id]


@pytest.mark.asyncio
async def test_overdue_proposals_do_not_count_toward_pending_cap(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """L2 (review round 2) — an overdue-but-still-'pending' proposal must not
    consume one of the org's 10 live-pending slots."""
    alice, _ = await _make_user(db_session, "alice")
    admin, admin_key = await _make_user(db_session, "admin")
    org = await _make_org(db_session, admin=admin)
    await _add_member(db_session, org, alice, role="member")

    stale_project = await _make_project(
        db_session, owner=alice, org_id=None, name="stale"
    )
    stale = await _propose(client, admin_key, org, stale_project)
    assert stale["status"] == 200, stale["text"]
    stale_xfer = (
        await db_session.execute(
            select(ProjectTransfer).where(
                ProjectTransfer.id == stale["body"]["transfer_id"]
            )
        )
    ).scalar_one()
    stale_xfer.expires_at = _now() - timedelta(minutes=1)
    await db_session.commit()

    # Ten live proposals succeed — the stale one is excluded from the count
    # (round 1 would have blocked the 10th live proposal at 10 total pending).
    for i in range(10):
        project = await _make_project(
            db_session, owner=alice, org_id=None, name=f"live-{i}"
        )
        result = await _propose(client, admin_key, org, project)
        assert result["status"] == 200, result["text"]

    eleventh = await _make_project(db_session, owner=alice, org_id=None, name="eleventh")
    result = await _propose(client, admin_key, org, eleventh)
    assert result["status"] == 409, result["text"]
    assert result["body"]["error"]["code"] == "proposal_limit"
