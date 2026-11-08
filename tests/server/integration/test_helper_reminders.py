"""Session-helper reminders (tk_8a858d6354fc4ef3, R2).

``propose_reminder`` only PROPOSES; the reminder exists only after the owner
confirms the pending ``helper_actions`` row, and confirm runs the REST route's
own creation core with ``created_via='helper'``. The owner is always the
confirming caller, whatever the model put in its arguments.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import HelperAction, Reminder
from sessionfs.server.helper.tools import TOOLS, ToolArgumentError
from tests.server.integration.test_helper_ticket_actions import (
    _ctx,
    _hdrs,
    _make_conversation,
    _make_project,
    _make_ticket,
    _make_user,
)


def _due(days: int = 2) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT09:00")


async def _store(db: AsyncSession, user, conv, proposal: dict) -> str:
    """Persist a queued proposal the way the turn handler does."""
    action = HelperAction(
        id=proposal["id"],
        user_id=user.id,
        conversation_id=conv.id,
        kind=proposal["kind"],
        params=json.dumps(proposal["params"], sort_keys=True, default=str),
        preview=json.dumps(proposal["preview"], sort_keys=True, default=str),
        status="pending",
        expires_at=proposal["expires_at"],
        created_at=proposal["created_at"],
    )
    db.add(action)
    await db.commit()
    return proposal["id"]


async def _reminders(db: AsyncSession) -> list[Reminder]:
    db.expire_all()
    return list((await db.execute(select(Reminder))).scalars())


@pytest.mark.asyncio
async def test_propose_never_creates_and_previews_local_time(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    ctx = _ctx(db_session, user)
    due = _due()
    out = await TOOLS["propose_reminder"].handler(
        ctx, {"title": "Book follow-up with Tolu", "due_at": due, "timezone": "Europe/London"}
    )
    assert "Proposed action" in out.text
    [proposal] = ctx.pending_actions
    assert proposal["kind"] == "create_reminder"
    params = proposal["params"]
    assert params["timezone"] == "Europe/London"
    # The preview shows the absolute wall time in the caller's zone.
    assert "09:00 Europe/London" in proposal["preview"]["summary"]
    assert "09:00 Europe/London" in params["due_local"]
    assert datetime.fromisoformat(params["due_at"]).tzinfo is not None
    assert await _reminders(db_session) == []


@pytest.mark.asyncio
async def test_propose_bounds_and_parses_due_at(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    ctx = _ctx(db_session, user)
    for bad in ("next thursday", _due(days=900)):
        with pytest.raises(ToolArgumentError):
            await TOOLS["propose_reminder"].handler(ctx, {"title": "x", "due_at": bad})
    with pytest.raises(ToolArgumentError):
        await TOOLS["propose_reminder"].handler(
            ctx, {"title": "x", "due_at": _due(), "timezone": "Mars/Base"}
        )
    assert ctx.pending_actions == []


@pytest.mark.asyncio
async def test_propose_refuses_unreadable_or_out_of_scope_source(db_session: AsyncSession):
    alice, _ = await _make_user(db_session, "alice")
    bob, _ = await _make_user(db_session, "bob")
    bobs = await _make_project(db_session, bob)
    secret = await _make_ticket(db_session, bob, bobs, title="Secret plan")
    ctx = _ctx(db_session, alice)
    out = await TOOLS["propose_reminder"].handler(
        ctx, {"title": "x", "due_at": _due(), "source": {"kind": "ticket", "id": secret.id}}
    )
    assert out.text == "Source not found." and ctx.pending_actions == []

    mine = await _make_project(db_session, alice, name="Mine")
    other = await _make_project(db_session, alice, name="Other")
    ticket = await _make_ticket(db_session, alice, other)
    scoped = _ctx(db_session, alice, project_id=mine.id)
    out = await TOOLS["propose_reminder"].handler(
        scoped, {"title": "x", "due_at": _due(), "source": {"kind": "ticket", "id": ticket.id}}
    )
    assert out.text == "Source not found." and scoped.pending_actions == []


@pytest.mark.asyncio
async def test_confirm_creates_exactly_one_and_twice_is_idempotent(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, user, project)
    user_id, ticket_id, project_id = user.id, ticket.id, project.id
    conv = await _make_conversation(db_session, user)
    ctx = _ctx(db_session, user)
    await TOOLS["propose_reminder"].handler(
        ctx,
        {
            "title": "Chase the CI fix",
            "due_at": _due(),
            "timezone": "Europe/London",
            "source": {"kind": "ticket", "id": ticket.id},
        },
    )
    action_id = await _store(db_session, user, conv, ctx.pending_actions[0])
    assert await _reminders(db_session) == []

    first = await client.post(f"/api/v1/helper/actions/{action_id}/confirm", headers=_hdrs(key))
    assert first.status_code == 200, first.text
    second = await client.post(f"/api/v1/helper/actions/{action_id}/confirm", headers=_hdrs(key))
    assert second.status_code == 200
    assert second.json()["status"] == first.json()["status"] == "confirmed"

    rows = await _reminders(db_session)
    assert len(rows) == 1
    r = rows[0]
    assert r.created_via == "helper"
    assert r.owner_user_id == user_id and r.created_by_user_id == user_id
    assert (r.source_kind, r.source_id, r.project_id) == ("ticket", ticket_id, project_id)
    assert r.timezone == "Europe/London"

    # The REST list shows it to the owner.
    listed = await client.get("/api/v1/me/reminders", headers=_hdrs(key))
    assert [i["id"] for i in listed.json()["items"]] == [r.id]


@pytest.mark.asyncio
async def test_unconfirmed_and_cancelled_proposals_create_nothing(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    conv = await _make_conversation(db_session, user)
    ctx = _ctx(db_session, user)
    await TOOLS["propose_reminder"].handler(ctx, {"title": "a", "due_at": _due()})
    await TOOLS["propose_reminder"].handler(ctx, {"title": "b", "due_at": _due()})
    pending, cancelled = [await _store(db_session, user, conv, p) for p in ctx.pending_actions]
    resp = await client.post(f"/api/v1/helper/actions/{cancelled}/cancel", headers=_hdrs(key))
    assert resp.status_code == 200
    assert await _reminders(db_session) == []
    db_session.expire_all()
    assert (await db_session.get(HelperAction, pending)).status == "pending"


@pytest.mark.asyncio
async def test_model_supplied_owner_is_ignored(client: AsyncClient, db_session: AsyncSession):
    user, key = await _make_user(db_session, "alice")
    other, _ = await _make_user(db_session, "bob")
    user_id = user.id
    conv = await _make_conversation(db_session, user)
    ctx = _ctx(db_session, user)
    await TOOLS["propose_reminder"].handler(
        ctx,
        {
            "title": "x",
            "due_at": _due(),
            "owner": other.email,
            "owner_user_id": other.id,
            "user_id": other.id,
        },
    )
    proposal = ctx.pending_actions[0]
    assert other.id not in json.dumps(proposal["params"])
    # Even a tampered stored proposal can't name an owner: confirm never
    # passes one to the creation core.
    proposal["params"]["owner"] = other.id
    action_id = await _store(db_session, user, conv, proposal)
    resp = await client.post(f"/api/v1/helper/actions/{action_id}/confirm", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    [r] = await _reminders(db_session)
    assert r.owner_user_id == user_id


@pytest.mark.asyncio
async def test_confirm_refuses_source_lost_since_proposal(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, user, project)
    conv = await _make_conversation(db_session, user)
    ctx = _ctx(db_session, user)
    await TOOLS["propose_reminder"].handler(
        ctx, {"title": "x", "due_at": _due(), "source": {"kind": "ticket", "id": ticket.id}}
    )
    action_id = await _store(db_session, user, conv, ctx.pending_actions[0])
    await db_session.delete(ticket)
    await db_session.commit()
    resp = await client.post(f"/api/v1/helper/actions/{action_id}/confirm", headers=_hdrs(key))
    assert resp.status_code == 200
    assert resp.json()["status"] == "failed"
    assert await _reminders(db_session) == []


@pytest.mark.asyncio
async def test_list_my_reminders_reads_visible_only_and_respects_scope(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, user, project)
    for body in (
        {"title": "Personal one", "due_at": _due(1)},
        {"title": "On the ticket", "due_at": _due(2), "source": {"kind": "ticket", "id": ticket.id}},
    ):
        resp = await client.post("/api/v1/me/reminders", json=body, headers=_hdrs(key))
        assert resp.status_code == 201, resp.text

    out = await TOOLS["list_my_reminders"].handler(_ctx(db_session, user), {})
    assert "Personal one" in out.text and "On the ticket" in out.text
    assert "<reminders>" in out.text

    scoped = _ctx(db_session, user, project_id=project.id)
    out = await TOOLS["list_my_reminders"].handler(scoped, {})
    assert "On the ticket" in out.text and "Personal one" not in out.text

    other_project = await _make_project(db_session, user, name="Elsewhere")
    elsewhere = _ctx(db_session, user, project_id=other_project.id)
    out = await TOOLS["list_my_reminders"].handler(elsewhere, {})
    assert out.text == "No reminders match."

    stranger, _ = await _make_user(db_session, "eve")
    out = await TOOLS["list_my_reminders"].handler(_ctx(db_session, stranger), {})
    assert out.text == "No reminders match."


@pytest.mark.asyncio
async def test_list_my_reminders_scoped_resolves_scope_projects_once(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """A scoped list resolves the scope's project set once, not per row."""
    import sessionfs.server.helper.tools as tools_mod

    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ticket = await _make_ticket(db_session, user, project)
    for i in range(5):
        resp = await client.post(
            "/api/v1/me/reminders",
            json={
                "title": f"On the ticket {i}",
                "due_at": _due(i + 1),
                "source": {"kind": "ticket", "id": ticket.id},
            },
            headers=_hdrs(key),
        )
        assert resp.status_code == 201, resp.text

    calls = 0
    real = tools_mod.scope_project_ids

    async def counting(db, user_id, scope):
        nonlocal calls
        calls += 1
        return await real(db, user_id, scope)

    monkeypatch.setattr(tools_mod, "scope_project_ids", counting)
    out = await TOOLS["list_my_reminders"].handler(
        _ctx(db_session, user, project_id=project.id), {}
    )
    assert all(f"On the ticket {i}" in out.text for i in range(5))
    assert calls == 1


@pytest.mark.asyncio
async def test_propose_accepts_chat_item_source(db_session: AsyncSession):
    from tests.server.integration.test_reminders_api import _chat, _meeting

    alice, _ = await _make_user(db_session, "alice")
    project = await _make_project(db_session, alice)
    _, chat_item = await _chat(db_session, project, alice)
    _, meeting_item = await _meeting(db_session, project, alice)
    assert "chat_item" in TOOLS["propose_reminder"].input_schema["properties"]["source"][
        "properties"]["kind"]["enum"]
    ctx = _ctx(db_session, alice)
    out = await TOOLS["propose_reminder"].handler(
        ctx, {"title": "x", "due_at": _due(), "source": {"kind": "chat_item", "id": chat_item}}
    )
    assert "Proposed action" in out.text
    assert ctx.pending_actions[0]["params"]["source"]["kind"] == "chat_item"
    # A meeting item id under chat_item (and the reverse) is not found.
    for kind, sid in (("chat_item", meeting_item), ("meeting_item", chat_item)):
        miss = _ctx(db_session, alice)
        out = await TOOLS["propose_reminder"].handler(
            miss, {"title": "x", "due_at": _due(), "source": {"kind": kind, "id": sid}}
        )
        assert out.text == "Source not found." and miss.pending_actions == []
