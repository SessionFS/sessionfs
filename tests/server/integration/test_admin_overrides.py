"""GH #84 — audited operator overrides.

Covers:
    - POST /api/v1/admin/projects/{id}/force-transfer (paved personal→org
      override: AdminAction with reason + before/after in the same
      transaction as the move)
    - POST /api/v1/admin/audit-log (operator-recorded actions)
    - GET /api/v1/admin/audit-log filters + new fields
    - reason / before / after on the existing tier and owner overrides
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AdminAction,
    ApiKey,
    OrgAuditEvent,
    OrgMember,
    Organization,
    Project,
    ProjectTransfer,
    User,
)


async def _user(db: AsyncSession, name: str, tier: str = "team") -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name,
        tier=tier,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
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


async def _org(db: AsyncSession, owner: User | None = None) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Baptist Health",
        slug=f"bh-{uuid.uuid4().hex[:8]}",
        tier="enterprise",
    )
    db.add(org)
    await db.commit()
    if owner is not None:
        db.add(OrgMember(org_id=org.id, user_id=owner.id, role="owner"))
        await db.commit()
    return org


async def _join(db: AsyncSession, org: Organization, user: User, role: str = "member") -> None:
    db.add(OrgMember(org_id=org.id, user_id=user.id, role=role))
    await db.commit()


async def _project_owner(db: AsyncSession, project_id: str) -> str:
    return (
        await db.execute(
            select(Project.owner_id)
            .where(Project.id == project_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _project(db: AsyncSession, owner: User, org_id: str | None = None) -> Project:
    proj = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name="clinical-notes",
        git_remote_normalized=f"github.com/x/{uuid.uuid4().hex[:8]}",
        owner_id=owner.id,
        org_id=org_id,
    )
    db.add(proj)
    await db.commit()
    return proj


async def _actions(db: AsyncSession, **where) -> list[AdminAction]:
    stmt = select(AdminAction).execution_options(populate_existing=True)
    for key, value in where.items():
        stmt = stmt.where(getattr(AdminAction, key) == value)
    return list((await db.execute(stmt)).scalars().all())


async def _project_org(db: AsyncSession, project_id: str) -> str | None:
    return (
        await db.execute(
            select(Project.org_id)
            .where(Project.id == project_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


# ── force-transfer ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_force_transfer_moves_personal_project_and_audits(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, admin_key = await _user(db_session, "ops", tier="admin")
    owner, _ = await _user(db_session, "jingcheng")
    org = await _org(db_session)
    # The owner already belongs to the org but is offline and never
    # initiated the transfer.
    await _join(db_session, org, owner)
    project = await _project(db_session, owner)

    resp = await client.post(
        f"/api/v1/admin/projects/{project.id}/force-transfer",
        json={"to_org_id": org.id, "reason": "  owner offline, team needs access  "},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "transferred"
    assert body["from_scope"] == "personal"
    assert body["to_org_id"] == org.id
    assert body["cancelled_transfer_ids"] == []
    assert body["owner_id"] == body["previous_owner_id"] == owner.id

    assert await _project_org(db_session, project.id) == org.id
    assert await _project_owner(db_session, project.id) == owner.id

    [entry] = await _actions(db_session, action="project_transfer_override")
    assert entry.id == body["admin_action_id"]
    assert entry.admin_id == admin.id
    assert entry.target_type == "project"
    assert entry.target_id == project.id
    assert entry.source == "api"
    assert entry.reason == "owner offline, team needs access"
    before = json.loads(entry.before_state)
    after = json.loads(entry.after_state)
    assert before["org_id"] is None and before["scope"] == "personal"
    assert before["owner_id"] == owner.id
    assert after["org_id"] == org.id and after["owner_id"] == owner.id
    assert after["transfer_id"] == body["transfer_id"]

    # The transfer history shows the move like any other accepted transfer.
    xfer = (
        await db_session.execute(
            select(ProjectTransfer).where(ProjectTransfer.id == body["transfer_id"])
        )
    ).scalar_one()
    assert xfer.state == "accepted"
    assert xfer.initiated_by == admin.id and xfer.accepted_by == admin.id
    assert (xfer.from_scope, xfer.to_scope) == ("personal", org.id)

    events = (
        await db_session.execute(
            select(OrgAuditEvent).where(OrgAuditEvent.target_id == project.id)
        )
    ).scalars().all()
    assert [(e.org_id, e.event_type) for e in events] == [
        (org.id, "project_force_transferred")
    ]

    # The audit log lists it with the new fields.
    log = await client.get(
        f"/api/v1/admin/audit-log?target_type=project&target_id={project.id}",
        headers=_hdrs(admin_key),
    )
    assert log.status_code == 200, log.text
    [row] = log.json()["actions"]
    assert row["reason"] == "owner offline, team needs access"
    assert row["before_state"]["scope"] == "personal"
    assert row["after_state"]["org_id"] == org.id
    assert row["source"] == "api"


@pytest.mark.asyncio
async def test_force_transfer_org_to_org_records_both_orgs(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, admin_key = await _user(db_session, "ops", tier="admin")
    owner, _ = await _user(db_session, "dev")
    src = await _org(db_session)
    dest = await _org(db_session)
    await _join(db_session, dest, owner)
    project = await _project(db_session, owner, org_id=src.id)

    resp = await client.post(
        f"/api/v1/admin/projects/{project.id}/force-transfer",
        json={"to_org_id": dest.id, "reason": "org consolidation"},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["from_scope"] == src.id
    assert await _project_org(db_session, project.id) == dest.id
    orgs = {
        e.org_id
        for e in (
            await db_session.execute(
                select(OrgAuditEvent).where(OrgAuditEvent.target_id == project.id)
            )
        ).scalars()
    }
    assert orgs == {src.id, dest.id}


@pytest.mark.asyncio
async def test_force_transfer_cancels_pending_transfer(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, admin_key = await _user(db_session, "ops", tier="admin")
    owner, _ = await _user(db_session, "dev")
    other = await _org(db_session, owner=owner)
    dest = await _org(db_session)
    await _join(db_session, dest, owner)
    project = await _project(db_session, owner)

    # A pending owner-initiated transfer would otherwise sit in an inbox.
    pending = ProjectTransfer(
        id=f"xfer_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        initiated_by=owner.id,
        target_user_id=owner.id,
        from_scope="personal",
        to_scope=other.id,
        state="pending",
    )
    db_session.add(pending)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/admin/projects/{project.id}/force-transfer",
        json={"to_org_id": dest.id, "reason": "owner left"},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["cancelled_transfer_ids"] == [pending.id]
    state = (
        await db_session.execute(
            select(ProjectTransfer.state)
            .where(ProjectTransfer.id == pending.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert state == "cancelled"
    [entry] = await _actions(db_session, action="project_transfer_override")
    assert json.loads(entry.before_state)["pending_transfer_ids"] == [pending.id]


@pytest.mark.asyncio
async def test_force_transfer_already_in_org_is_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, admin_key = await _user(db_session, "ops", tier="admin")
    owner, _ = await _user(db_session, "dev")
    org = await _org(db_session)
    project = await _project(db_session, owner, org_id=org.id)

    resp = await client.post(
        f"/api/v1/admin/projects/{project.id}/force-transfer",
        json={"to_org_id": org.id, "reason": "noop"},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "already_in_org"
    assert await _actions(db_session) == []


@pytest.mark.asyncio
async def test_force_transfer_404s(client: AsyncClient, db_session: AsyncSession) -> None:
    _, admin_key = await _user(db_session, "ops", tier="admin")
    owner, _ = await _user(db_session, "dev")
    org = await _org(db_session)
    project = await _project(db_session, owner)

    missing_project = await client.post(
        "/api/v1/admin/projects/proj_nope/force-transfer",
        json={"to_org_id": org.id, "reason": "x"},
        headers=_hdrs(admin_key),
    )
    assert missing_project.status_code == 404
    missing_org = await client.post(
        f"/api/v1/admin/projects/{project.id}/force-transfer",
        json={"to_org_id": "org_nope", "reason": "x"},
        headers=_hdrs(admin_key),
    )
    assert missing_org.status_code == 404
    assert await _project_org(db_session, project.id) is None
    assert await _actions(db_session) == []


@pytest.mark.asyncio
async def test_force_transfer_refuses_merged_project(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, admin_key = await _user(db_session, "ops", tier="admin")
    owner, _ = await _user(db_session, "dev")
    org = await _org(db_session)
    target = await _project(db_session, owner)
    project = await _project(db_session, owner)
    project.merged_into_project_id = target.id
    project.merged_at = datetime.now(timezone.utc)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/admin/projects/{project.id}/force-transfer",
        json={"to_org_id": org.id, "reason": "x"},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "project_merged"
    assert await _actions(db_session) == []


@pytest.mark.asyncio
async def test_force_transfer_requires_admin(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, owner_key = await _user(db_session, "dev", tier="enterprise")
    org = await _org(db_session, owner=owner)
    project = await _project(db_session, owner)

    resp = await client.post(
        f"/api/v1/admin/projects/{project.id}/force-transfer",
        json={"to_org_id": org.id, "reason": "let me in"},
        headers=_hdrs(owner_key),
    )
    assert resp.status_code == 403
    assert await _project_org(db_session, project.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"reason": ""},
        {"reason": "   "},
        {"reason": "x" * 501},
        {"reason": 5},
    ],
)
async def test_force_transfer_requires_a_reason(
    client: AsyncClient, db_session: AsyncSession, payload: dict
) -> None:
    _, admin_key = await _user(db_session, "ops", tier="admin")
    owner, _ = await _user(db_session, "dev")
    org = await _org(db_session)
    project = await _project(db_session, owner)

    resp = await client.post(
        f"/api/v1/admin/projects/{project.id}/force-transfer",
        json={"to_org_id": org.id, **payload},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 422, resp.text
    assert await _project_org(db_session, project.id) is None


@pytest.mark.asyncio
async def test_force_transfer_stranded_personas_guard_and_force(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    from sessionfs.server.routes import project_transfers

    async def stranded(db, project_id, *, current_org_id, dest_org_id):
        return [
            SimpleNamespace(
                persona="gcp-infra", ticket_ids=["tk_1"], run_ids=[], handoff_ids=[]
            )
        ]

    monkeypatch.setattr(project_transfers, "personas_stranded_by_scope_change", stranded)
    _, admin_key = await _user(db_session, "ops", tier="admin")
    owner, _ = await _user(db_session, "dev")
    src = await _org(db_session)
    dest = await _org(db_session)
    await _join(db_session, dest, owner)
    project = await _project(db_session, owner, org_id=src.id)

    url = f"/api/v1/admin/projects/{project.id}/force-transfer"
    refused = await client.post(
        url, json={"to_org_id": dest.id, "reason": "x"}, headers=_hdrs(admin_key)
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "personas_stranded"
    assert await _project_org(db_session, project.id) == src.id

    forced = await client.post(
        url, json={"to_org_id": dest.id, "reason": "x", "force": True}, headers=_hdrs(admin_key)
    )
    assert forced.status_code == 200, forced.text
    assert await _project_org(db_session, project.id) == dest.id
    [entry] = await _actions(db_session, action="project_transfer_override")
    assert json.loads(entry.details)["force"] is True


@pytest.mark.asyncio
async def test_force_transfer_is_atomic_on_failure(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    """A failure after the move and the audit row are staged leaves
    neither behind: the AdminAction commits only with the change."""
    from sessionfs.server.routes import admin as admin_routes

    def boom(*args, **kwargs):
        raise RuntimeError("audit event write failed")

    monkeypatch.setattr(admin_routes, "_project_override_org_event", boom)
    _, admin_key = await _user(db_session, "ops", tier="admin")
    owner, _ = await _user(db_session, "dev")
    lead, _ = await _user(db_session, "lead")
    org = await _org(db_session)
    await _join(db_session, org, lead)
    project = await _project(db_session, owner)

    with pytest.raises(RuntimeError):
        await client.post(
            f"/api/v1/admin/projects/{project.id}/force-transfer",
            json={"to_org_id": org.id, "reason": "x", "new_owner_id": lead.id},
            headers=_hdrs(admin_key),
        )
    assert await _project_org(db_session, project.id) is None
    assert await _project_owner(db_session, project.id) == owner.id
    assert await _actions(db_session) == []
    transfers = (
        await db_session.execute(
            select(ProjectTransfer).where(ProjectTransfer.project_id == project.id)
        )
    ).scalars().all()
    assert transfers == []


@pytest.mark.asyncio
async def test_force_transfer_refuses_owner_who_is_not_a_member(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Moving a project into an org its owner does not belong to would leave
    a non-member owner with full access that member removal can never
    revoke, so it needs an explicit new owner."""
    _, admin_key = await _user(db_session, "ops", tier="admin")
    owner, _ = await _user(db_session, "departed")
    org = await _org(db_session)
    project = await _project(db_session, owner)

    resp = await client.post(
        f"/api/v1/admin/projects/{project.id}/force-transfer",
        json={"to_org_id": org.id, "reason": "owner left the company"},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "owner_not_member"
    assert await _project_org(db_session, project.id) is None
    assert await _actions(db_session) == []


@pytest.mark.asyncio
async def test_force_transfer_assigns_new_owner(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, admin_key = await _user(db_session, "ops", tier="admin")
    owner, _ = await _user(db_session, "departed")
    lead, _ = await _user(db_session, "lead")
    src = await _org(db_session)
    org = await _org(db_session)
    await _join(db_session, org, lead)
    project = await _project(db_session, owner, org_id=src.id)

    resp = await client.post(
        f"/api/v1/admin/projects/{project.id}/force-transfer",
        json={"to_org_id": org.id, "reason": "owner left", "new_owner_id": lead.id},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert (body["previous_owner_id"], body["owner_id"]) == (owner.id, lead.id)
    assert await _project_org(db_session, project.id) == org.id
    assert await _project_owner(db_session, project.id) == lead.id

    [entry] = await _actions(db_session, action="project_transfer_override")
    assert json.loads(entry.before_state)["owner_id"] == owner.id
    assert json.loads(entry.after_state)["owner_id"] == lead.id
    assert json.loads(entry.details)["owner_changed"] is True
    events = (
        await db_session.execute(
            select(OrgAuditEvent).where(OrgAuditEvent.target_id == project.id)
        )
    ).scalars().all()
    assert {e.org_id for e in events} == {src.id, org.id}
    for event in events:
        assert json.loads(event.before)["owner_id"] == owner.id
        assert json.loads(event.after)["owner_id"] == lead.id


@pytest.mark.asyncio
async def test_force_transfer_reassigns_owner_who_is_a_member(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, admin_key = await _user(db_session, "ops", tier="admin")
    owner, _ = await _user(db_session, "dev")
    lead, _ = await _user(db_session, "lead")
    org = await _org(db_session)
    await _join(db_session, org, owner)
    await _join(db_session, org, lead, role="admin")
    project = await _project(db_session, owner)

    resp = await client.post(
        f"/api/v1/admin/projects/{project.id}/force-transfer",
        json={"to_org_id": org.id, "reason": "handover", "new_owner_id": lead.id},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 200, resp.text
    assert await _project_owner(db_session, project.id) == lead.id


@pytest.mark.asyncio
async def test_force_transfer_refuses_new_owner_outside_the_org(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, admin_key = await _user(db_session, "ops", tier="admin")
    owner, _ = await _user(db_session, "dev")
    outsider, _ = await _user(db_session, "outsider")
    inactive, _ = await _user(db_session, "inactive")
    org = await _org(db_session)
    other = await _org(db_session)
    await _join(db_session, other, outsider)
    await _join(db_session, org, inactive)
    inactive.is_active = False
    await db_session.commit()
    project = await _project(db_session, owner)

    for new_owner in (outsider.id, inactive.id, "no-such-user"):
        resp = await client.post(
            f"/api/v1/admin/projects/{project.id}/force-transfer",
            json={"to_org_id": org.id, "reason": "x", "new_owner_id": new_owner},
            headers=_hdrs(admin_key),
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "new_owner_not_member"
    assert await _project_org(db_session, project.id) is None
    assert await _project_owner(db_session, project.id) == owner.id
    assert await _actions(db_session) == []


@pytest.mark.asyncio
async def test_force_transfer_expected_source_scope(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """A delayed retry must not bounce a project that has moved since."""
    _, admin_key = await _user(db_session, "ops", tier="admin")
    owner, _ = await _user(db_session, "dev")
    src = await _org(db_session)
    dest = await _org(db_session)
    await _join(db_session, src, owner)
    await _join(db_session, dest, owner)
    project = await _project(db_session, owner)
    url = f"/api/v1/admin/projects/{project.id}/force-transfer"

    wrong = await client.post(
        url,
        json={"to_org_id": dest.id, "reason": "x", "expected_org_id": src.id},
        headers=_hdrs(admin_key),
    )
    assert wrong.status_code == 409, wrong.text
    assert wrong.json()["error"]["code"] == "unexpected_source_scope"

    # null = expect personal.
    first = await client.post(
        url,
        json={"to_org_id": src.id, "reason": "x", "expected_org_id": None},
        headers=_hdrs(admin_key),
    )
    assert first.status_code == 200, first.text
    # Replaying the same request after it succeeded is refused, not re-run.
    replay = await client.post(
        url,
        json={"to_org_id": dest.id, "reason": "x", "expected_org_id": None},
        headers=_hdrs(admin_key),
    )
    assert replay.status_code == 409
    assert replay.json()["error"]["code"] == "unexpected_source_scope"
    assert await _project_org(db_session, project.id) == src.id
    assert len(await _actions(db_session, action="project_transfer_override")) == 1

    ok = await client.post(
        url,
        json={"to_org_id": dest.id, "reason": "x", "expected_org_id": src.id},
        headers=_hdrs(admin_key),
    )
    assert ok.status_code == 200, ok.text
    assert await _project_org(db_session, project.id) == dest.id


# ── operator-recorded actions ──────────────────────────────────────


@pytest.mark.asyncio
async def test_record_operator_action(client: AsyncClient, db_session: AsyncSession) -> None:
    admin, admin_key = await _user(db_session, "ops", tier="admin")
    resp = await client.post(
        "/api/v1/admin/audit-log",
        json={
            "action": "user_merge",
            "target_type": "user",
            "target_id": "u_typo",
            "reason": "signed up twice with a typo in the email domain",
            "before_state": {"emails": ["a@baptisthelth.net", "a@baptisthealth.net"]},
            "after_state": {"email": "a@baptisthealth.net", "rows_reassigned": 62},
        },
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["source"] == "operator_record"
    assert body["admin_id"] == admin.id
    assert body["after_state"]["rows_reassigned"] == 62

    [entry] = await _actions(db_session)
    assert entry.source == "operator_record"
    assert entry.action == "user_merge"
    assert json.loads(entry.before_state)["emails"][0] == "a@baptisthelth.net"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"action": "User_Merge"},
        {"action": "1merge"},
        {"action": "a" * 51},
        {"action": "drop table; --"},
        {"target_type": "a" * 21},
        {"target_type": "User"},
        {"target_id": ""},
        {"target_id": "x" * 65},
        {"target_id": " padded"},
        {"target_id": "bad\nid"},
        {"reason": ""},
        {"reason": "   "},
        {"reason": "x" * 501},
        {"before_state": ["not", "an", "object"]},
        {"after_state": "text"},
    ],
)
async def test_record_validation(
    client: AsyncClient, db_session: AsyncSession, override: dict
) -> None:
    _, admin_key = await _user(db_session, "ops", tier="admin")
    payload = {
        "action": "tier_change",
        "target_type": "org",
        "target_id": "org_1",
        "reason": "manual fix",
        **override,
    }
    resp = await client.post("/api/v1/admin/audit-log", json=payload, headers=_hdrs(admin_key))
    assert resp.status_code == 422, resp.text
    assert await _actions(db_session) == []


@pytest.mark.asyncio
async def test_record_requires_reason(client: AsyncClient, db_session: AsyncSession) -> None:
    _, admin_key = await _user(db_session, "ops", tier="admin")
    resp = await client.post(
        "/api/v1/admin/audit-log",
        json={"action": "bulk_update", "target_type": "project", "target_id": "p"},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_record_state_size_cap(client: AsyncClient, db_session: AsyncSession) -> None:
    _, admin_key = await _user(db_session, "ops", tier="admin")
    base = {"action": "bulk_update", "target_type": "project", "target_id": "p", "reason": "r"}
    too_big = {"blob": "x" * (64 * 1024)}
    for field in ("before_state", "after_state"):
        resp = await client.post(
            "/api/v1/admin/audit-log", json={**base, field: too_big}, headers=_hdrs(admin_key)
        )
        assert resp.status_code == 422, resp.text
        assert field in resp.text
    fits = {"blob": "x" * (60 * 1024)}
    ok = await client.post(
        "/api/v1/admin/audit-log", json={**base, "after_state": fits}, headers=_hdrs(admin_key)
    )
    assert ok.status_code == 201, ok.text
    assert len(await _actions(db_session)) == 1


@pytest.mark.asyncio
async def test_record_requires_admin(
    client: AsyncClient, db_session: AsyncSession, auth_headers: dict
) -> None:
    resp = await client.post(
        "/api/v1/admin/audit-log",
        json={"action": "x", "target_type": "user", "target_id": "u", "reason": "r"},
        headers=auth_headers,
    )
    assert resp.status_code == 403
    listing = await client.get("/api/v1/admin/audit-log", headers=auth_headers)
    assert listing.status_code == 403
    assert await _actions(db_session) == []


# ── audit-log listing ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_audit_log_filters(client: AsyncClient, db_session: AsyncSession) -> None:
    _, admin_key = await _user(db_session, "ops", tier="admin")
    rows = [
        ("user_merge", "user", "u_1"),
        ("user_merge", "user", "u_2"),
        ("bulk_update", "project", "p_1"),
    ]
    for action, target_type, target_id in rows:
        resp = await client.post(
            "/api/v1/admin/audit-log",
            json={
                "action": action,
                "target_type": target_type,
                "target_id": target_id,
                "reason": "r",
            },
            headers=_hdrs(admin_key),
        )
        assert resp.status_code == 201
    # A server-written row too, to tell the sources apart.
    victim, _ = await _user(db_session, "victim")
    tier = await client.put(
        f"/api/v1/admin/users/{victim.id}/tier",
        json={"tier": "pro"},
        headers=_hdrs(admin_key),
    )
    assert tier.status_code == 200

    async def listing(query: str) -> dict:
        resp = await client.get(f"/api/v1/admin/audit-log?{query}", headers=_hdrs(admin_key))
        assert resp.status_code == 200, resp.text
        return resp.json()

    assert (await listing(""))["total"] == 4
    by_action = await listing("action=user_merge")
    assert by_action["total"] == 2
    assert {a["target_id"] for a in by_action["actions"]} == {"u_1", "u_2"}
    by_target = await listing("target_type=user&target_id=u_2")
    assert [a["target_id"] for a in by_target["actions"]] == ["u_2"]
    assert (await listing("target_type=project"))["total"] == 1
    assert (await listing("source=operator_record"))["total"] == 3
    api_rows = await listing("source=api")
    assert [a["action"] for a in api_rows["actions"]] == ["tier_change"]
    paged = await listing("page=2&page_size=3")
    assert paged["total"] == 4 and len(paged["actions"]) == 1


@pytest.mark.asyncio
async def test_audit_log_tolerates_hand_written_rows(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Rows inserted by raw SQL may carry malformed JSON; the listing shows
    the raw text instead of failing."""
    admin, admin_key = await _user(db_session, "ops", tier="admin")
    db_session.add(
        AdminAction(
            id=str(uuid.uuid4()),
            admin_id=admin.id,
            action="manual",
            target_type="user",
            target_id="u",
            details="{not json",
            before_state="also not json",
        )
    )
    await db_session.commit()
    resp = await client.get("/api/v1/admin/audit-log", headers=_hdrs(admin_key))
    assert resp.status_code == 200, resp.text
    [row] = resp.json()["actions"]
    assert row["details"] == "{not json"
    assert row["before_state"] == "also not json"
    assert row["source"] == "api"


# ── reason + before/after on existing overrides ────────────────────


@pytest.mark.asyncio
async def test_user_tier_change_records_reason_and_state(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, admin_key = await _user(db_session, "ops", tier="admin")
    target, _ = await _user(db_session, "dev", tier="free")
    resp = await client.put(
        f"/api/v1/admin/users/{target.id}/tier",
        json={"tier": "team", "reason": "contract signed"},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 200, resp.text
    [entry] = await _actions(db_session, action="tier_change")
    assert entry.reason == "contract signed"
    assert json.loads(entry.before_state) == {"tier": "free"}
    assert json.loads(entry.after_state) == {"tier": "team"}

    # Existing clients that send no reason keep working.
    again = await client.put(
        f"/api/v1/admin/users/{target.id}/tier",
        json={"tier": "pro"},
        headers=_hdrs(admin_key),
    )
    assert again.status_code == 200
    bad = await client.put(
        f"/api/v1/admin/users/{target.id}/tier",
        json={"tier": "pro", "reason": "x" * 501},
        headers=_hdrs(admin_key),
    )
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_org_tier_change_records_reason_and_state(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, admin_key = await _user(db_session, "ops", tier="admin")
    org = await _org(db_session)
    resp = await client.put(
        f"/api/v1/admin/orgs/{org.id}/tier",
        json={"tier": "team", "seats_limit": 40, "reason": "renewal downgrade"},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 200, resp.text
    assert "reason" not in resp.json()["updated"]
    [entry] = await _actions(db_session, action="admin_update_org")
    assert entry.reason == "renewal downgrade"
    assert json.loads(entry.before_state) == {"tier": "enterprise", "seats_limit": 5}
    assert json.loads(entry.after_state) == {"tier": "team", "seats_limit": 40}


@pytest.mark.asyncio
async def test_force_transfer_owner_records_reason_and_state(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, admin_key = await _user(db_session, "ops", tier="admin")
    owner, _ = await _user(db_session, "owner")
    deputy, _ = await _user(db_session, "deputy")
    org = await _org(db_session, owner=owner)
    db_session.add(OrgMember(org_id=org.id, user_id=deputy.id, role="admin"))
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/admin/orgs/{org.id}/force-transfer-owner",
        json={"to_user_id": deputy.id, "reason": "owner departed"},
        headers=_hdrs(admin_key),
    )
    assert resp.status_code == 200, resp.text
    [entry] = await _actions(db_session, action="force_transfer_owner")
    assert entry.reason == "owner departed"
    assert json.loads(entry.before_state)["owner_user_id"] == owner.id
    assert json.loads(entry.after_state) == {
        "owner_user_id": deputy.id,
        "displaced_owner_role": "admin",
    }
