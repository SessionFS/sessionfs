"""GH #89 — platform-admin project merge override.

POST /api/v1/admin/projects/{target}/merge-from/{source} runs the normal
merge engine for an operator who owns neither project, with a reason and an
AdminAction that commits in the merge's transaction.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import AdminAction, Project, Session, Ticket, TicketEvent
from tests.server.integration.test_admin_overrides import _hdrs, _org, _project, _user

pytestmark = pytest.mark.asyncio


def _url(target: str, source: str) -> str:
    return f"/api/v1/admin/projects/{target}/merge-from/{source}"


async def _fresh(db: AsyncSession, project_id: str) -> Project:
    return (
        await db.execute(
            select(Project).where(Project.id == project_id).execution_options(populate_existing=True)
        )
    ).scalar_one()


async def test_admin_merges_projects_they_do_not_own(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, admin_key = await _user(db_session, "ops", tier="admin")
    alice, _ = await _user(db_session, "alice")
    bob, _ = await _user(db_session, "bob")
    source = await _project(db_session, alice)
    target = await _project(db_session, bob)
    now = datetime.now(timezone.utc)
    db_session.add(Session(id=f"ses_{uuid.uuid4().hex[:16]}", user_id=alice.id,
                           source_tool="claude-code", blob_key="b", etag="e",
                           project_id=source.id, created_at=now, updated_at=now))
    await db_session.commit()

    dry = await client.post(_url(target.id, source.id), json={"reason": "dup twins"},
                            headers=_hdrs(admin_key))
    assert dry.status_code == 200, dry.text
    assert dry.json()["status"] == "planned" and dry.json()["dry_run"] is True
    assert (await _fresh(db_session, source.id)).merged_into_project_id is None
    assert await _actions_for(db_session, source.id) == []

    done = await client.post(_url(target.id, source.id),
                             json={"reason": "dup twins", "dry_run": False},
                             headers=_hdrs(admin_key))
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["status"] == "merged"
    assert (await _fresh(db_session, source.id)).merged_into_project_id == target.id

    [entry] = await _actions_for(db_session, source.id)
    assert entry.id == body["admin_action_id"]
    assert entry.admin_id == admin.id and entry.reason == "dup twins" and entry.source == "api"
    assert json.loads(entry.before_state)["source"]["owner_id"] == alice.id
    assert json.loads(entry.after_state)["merged_into"] == target.id
    moved = (
        await db_session.execute(
            select(Session.project_id).where(Session.user_id == alice.id)
            .execution_options(populate_existing=True)
        )
    ).scalars().all()
    assert moved == [target.id]


async def test_admin_merge_reassigns_ticket_events(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, admin_key = await _user(db_session, "ops", tier="admin")
    alice, _ = await _user(db_session, "alice")
    bob, _ = await _user(db_session, "bob")
    source = await _project(db_session, alice)
    target = await _project(db_session, bob)
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=source.id,
        title="t",
        created_by_user_id=alice.id,
    )
    db_session.add(ticket)
    await db_session.flush()
    db_session.add(TicketEvent(
        id=f"evt_{uuid.uuid4().hex[:16]}",
        ticket_id=ticket.id,
        project_id=source.id,
        action="created",
        actor_type="user",
    ))
    await db_session.commit()

    done = await client.post(_url(target.id, source.id),
                             json={"reason": "dup twins", "dry_run": False},
                             headers=_hdrs(admin_key))
    assert done.status_code == 200, done.text

    assert (await db_session.execute(
        select(Ticket.project_id).where(Ticket.id == ticket.id)
        .execution_options(populate_existing=True)
    )).scalar_one() == target.id
    assert (await db_session.execute(
        select(TicketEvent.project_id).where(TicketEvent.ticket_id == ticket.id)
        .execution_options(populate_existing=True)
    )).scalar_one() == target.id


async def _actions_for(db: AsyncSession, project_id: str) -> list[AdminAction]:
    return list(
        (
            await db.execute(
                select(AdminAction)
                .where(AdminAction.action == "project_merge", AdminAction.target_id == project_id)
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )


async def test_non_admin_is_refused(client: AsyncClient, db_session: AsyncSession) -> None:
    alice, alice_key = await _user(db_session, "alice")
    source = await _project(db_session, alice)
    target = await _project(db_session, alice)
    resp = await client.post(_url(target.id, source.id),
                             json={"reason": "x", "dry_run": False}, headers=_hdrs(alice_key))
    assert resp.status_code == 403
    assert (await _fresh(db_session, source.id)).merged_into_project_id is None


async def test_cross_scope_and_bad_input_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, admin_key = await _user(db_session, "ops", tier="admin")
    alice, _ = await _user(db_session, "alice")
    org = await _org(db_session, owner=alice)
    personal = await _project(db_session, alice)
    in_org = await _project(db_session, alice, org_id=org.id)
    h = _hdrs(admin_key)

    resp = await client.post(_url(in_org.id, personal.id), json={"reason": "x"}, headers=h)
    assert resp.status_code == 400 and "cross_scope" in resp.text
    resp = await client.post(_url(personal.id, personal.id), json={"reason": "x"}, headers=h)
    assert resp.status_code == 400
    resp = await client.post(_url(personal.id, "proj_missing"), json={"reason": "x"}, headers=h)
    assert resp.status_code == 404
    for bad in ({"reason": "  "}, {}, {"reason": "x", "dry_run": "false"},
                {"reason": "x", "persona_policy": "clobber"}):
        resp = await client.post(_url(in_org.id, personal.id), json=bad, headers=h)
        assert resp.status_code == 422, bad


async def test_already_merged_source_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, admin_key = await _user(db_session, "ops", tier="admin")
    alice, _ = await _user(db_session, "alice")
    a = await _project(db_session, alice)
    b = await _project(db_session, alice)
    c = await _project(db_session, alice)
    h = _hdrs(admin_key)
    ok = await client.post(_url(b.id, a.id), json={"reason": "x", "dry_run": False}, headers=h)
    assert ok.status_code == 200, ok.text
    again = await client.post(_url(c.id, a.id), json={"reason": "x", "dry_run": False}, headers=h)
    assert again.status_code in (400, 409)
    assert len(await _actions_for(db_session, a.id)) == 1


async def test_failure_under_the_lock_leaves_no_audit_entry(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
) -> None:
    """The AdminAction is added in the merge's transaction: if anything
    fails after it is added, neither the merge nor the entry survives."""
    from sessionfs.server.routes import admin as admin_routes

    _, admin_key = await _user(db_session, "ops", tier="admin")
    alice, _ = await _user(db_session, "alice")
    source = await _project(db_session, alice)
    target = await _project(db_session, alice)
    real = admin_routes._log_action_sync

    def add_then_fail(*args, **kwargs):
        real(*args, **kwargs)
        raise RuntimeError("boom after the audit row was added")

    monkeypatch.setattr(admin_routes, "_log_action_sync", add_then_fail)
    with pytest.raises(RuntimeError):
        await client.post(_url(target.id, source.id),
                          json={"reason": "x", "dry_run": False}, headers=_hdrs(admin_key))
    assert (await _fresh(db_session, source.id)).merged_into_project_id is None
    assert await _actions_for(db_session, source.id) == []


async def test_engine_rechecks_platform_admin_under_the_lock(
    client: AsyncClient, db_session: AsyncSession  # client sets the engine's session factory
) -> None:
    from fastapi import HTTPException

    from sessionfs.server.db import engine as engine_mod
    from sessionfs.server.services.merge import merge_projects

    alice, _ = await _user(db_session, "alice")
    source = await _project(db_session, alice)
    target = await _project(db_session, alice)
    stranger, _ = await _user(db_session, "stranger")  # not a platform admin
    source_id, target_id, stranger_id = source.id, target.id, stranger.id
    with pytest.raises(HTTPException) as exc:
        await merge_projects(
            db=db_session, source_id=source_id, target_id=target_id, user_id=stranger_id,
            dry_run=False, persona_policy="rename",
            session_factory=engine_mod._session_factory, platform_admin=True,
        )
    assert exc.value.status_code == 403
    assert (await _fresh(db_session, source_id)).merged_into_project_id is None
