"""Review fixes for reminders (tk_8a858d6354fc4ef3): text sanitising, actor
provenance, bounded sweep queries, per-creator cap, helper title redaction."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import Reminder, ReminderNote
from sessionfs.server.services import reminders as svc
from tests.server.integration.test_reminders_api import (
    _code,
    _create,
    _due,
    _hdrs,
    _make_user,
    _service_key,
    _soon,
    _team,
)
from tests.server.integration.test_ticket_watchers import _create_ticket

pytestmark = pytest.mark.asyncio


async def test_title_and_note_are_sanitised(client: AsyncClient, db_session: AsyncSession):
    from sessionfs.server.routes import reminders as routes

    # Lone surrogates can't be sent through httpx; the helpers must cope.
    assert routes._clean_title("a\ud800\x00 b") == "a\ufffd b"
    assert routes._clean_text("x\udc00y\x00").encode("utf-8")

    _, key = await _make_user(db_session, "nul")
    made = await _create(client, key, title="bad\x00 title")
    assert "\x00" not in made["title"]
    patched = await client.patch(
        f"/api/v1/me/reminders/{made['id']}", headers=_hdrs(key), json={"title": "x\x00y"}
    )
    assert patched.status_code == 200, patched.text
    assert "\x00" not in patched.json()["title"]
    note = await client.post(
        f"/api/v1/me/reminders/{made['id']}/notes", headers=_hdrs(key), json={"body": "a\x00b"}
    )
    assert note.status_code == 201, note.text
    assert "\x00" not in note.json()["body"]
    # A NUL-bearing timezone is a plain 422, not a crash.
    resp = await client.post(
        "/api/v1/me/reminders",
        headers=_hdrs(key),
        json={"title": "t", "due_at": _soon(), "timezone": "Europe/\x00London"},
    )
    assert resp.status_code == 422 and _code(resp) == "invalid_timezone"


async def test_load_sources_ignores_non_ascii_digits(db_session: AsyncSession):
    cache = svc.AccessCache()
    out = await svc.load_sources(db_session, [("knowledge_entry", "²")], cache)
    assert ("knowledge_entry", "²") in out  # no ValueError from int("²")


async def test_service_key_actor_recorded(client: AsyncClient, db_session: AsyncSession):
    lead, lead_key, _, _, org, project = await _team(db_session)
    ticket_id = await _create_ticket(client, project, lead_key)
    svc_key = await _service_key(
        db_session, owner=lead, org_id=org.id, scopes=["reminders:read", "reminders:write"]
    )
    src = {"kind": "ticket", "id": ticket_id}
    rem = await _create(client, svc_key, source=src)
    assert rem["actor_type"] == "service" and rem["service_key_name"] == "rem-svc"
    human = await _create(client, lead_key, source=src)
    assert human["actor_type"] == "user" and human["service_key_name"] is None

    note = await client.post(
        f"/api/v1/me/reminders/{human['id']}/notes", headers=_hdrs(svc_key), json={"body": "hi"}
    )
    assert note.status_code == 201, note.text
    assert note.json()["actor_type"] == "service"
    notes = await client.get(f"/api/v1/me/reminders/{human['id']}/notes", headers=_hdrs(lead_key))
    assert notes.json()["items"][0]["service_key_name"] == "rem-svc"
    row = (await db_session.execute(select(ReminderNote))).scalars().one()
    assert row.actor_type == "service" and row.service_key_id

    done = await client.patch(
        f"/api/v1/me/reminders/{rem['id']}", headers=_hdrs(svc_key), json={"status": "done"}
    )
    assert done.status_code == 200
    db_session.expire_all()
    closed = await db_session.get(Reminder, rem["id"])
    assert closed.closed_by_service_key_id is not None

    # Service keys can't delete, even their own reminders.
    resp = await client.delete(f"/api/v1/me/reminders/{rem['id']}", headers=_hdrs(svc_key))
    assert resp.status_code == 403
    still = await client.get(f"/api/v1/me/reminders/{rem['id']}", headers=_hdrs(lead_key))
    assert still.status_code == 200


async def test_sweep_visibility_queries_bounded(
    client: AsyncClient, db_engine, db_session: AsyncSession
):
    lead, lead_key, mate, _, _, project = await _team(db_session)
    tickets = [await _create_ticket(client, project, lead_key) for _ in range(12)]

    async def run(batch: list[str]) -> int:
        for tid in batch:
            await _due(
                db_session, mate, creator=lead, source_kind="ticket", source_id=tid,
                project_id=project.id,
            )
        statements: list[str] = []

        def before(conn, cursor, statement, params, context, executemany):
            if statement.lstrip().upper().startswith("SELECT"):
                statements.append(statement)

        event.listen(db_engine.sync_engine, "before_cursor_execute", before)
        try:
            from sqlalchemy.ext.asyncio import async_sessionmaker

            async with async_sessionmaker(db_engine, expire_on_commit=False)() as s:
                res = await svc.sweep_due_reminders(s)
        finally:
            event.remove(db_engine.sync_engine, "before_cursor_execute", before)
        assert res["reminders_notified"] == len(batch)
        return len(statements)

    small = await run(tickets[:2])
    large = await run(tickets[2:])
    assert large <= small + 2, (small, large)


async def test_helper_proposal_target_uses_redacted_title(db_session: AsyncSession):
    from tests.server.integration.test_helper_ticket_actions import _ctx as hctx
    from tests.server.integration.test_helper_ticket_actions import _make_user as hmake

    user, _ = await hmake(db_session)
    ctx = hctx(db_session, user)
    ctx.dlp_policy = {"mode": "redact", "categories": ["secrets"], "custom_patterns": []}
    secret = "AKIA1234567890ABCDEF"
    from sessionfs.server.helper.tools import TOOLS

    due = (datetime.now(timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%dT09:00")
    await TOOLS["propose_reminder"].handler(
        ctx, {"title": f"rotate key {secret}", "due_at": due}
    )
    [proposal] = ctx.pending_actions
    assert secret not in repr(proposal)
    # The targets handed to the action layer are already redacted.
    seen: list = []
    import sessionfs.server.helper.tools as tools_mod

    real = tools_mod._propose_action

    def spy(ctx_, **kw):
        seen.append(kw["targets"])
        return real(ctx_, **kw)

    tools_mod._propose_action = spy
    try:
        await TOOLS["propose_reminder"].handler(
            ctx, {"title": f"rotate key {secret}", "due_at": due}
        )
    finally:
        tools_mod._propose_action = real
    assert secret not in repr(seen)


async def test_per_creator_cap_for_other_owner(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    lead, lead_key, mate, _, _, project = await _team(db_session)
    ticket_id = await _create_ticket(client, project, lead_key)
    src = {"kind": "ticket", "id": ticket_id}
    for _ in range(svc.MAX_OPEN_PER_CREATOR_FOR_OTHER):
        await _create(client, lead_key, owner=mate.id, source=src)
    resp = await client.post(
        "/api/v1/me/reminders",
        headers=_hdrs(lead_key),
        json={"title": "one more", "due_at": _soon(), "owner": mate.id, "source": src},
    )
    assert resp.status_code == 409 and _code(resp) == "reminder_limit_reached"
    assert resp.json()["error"]["message"] == f"At most {svc.MAX_OPEN_PER_USER} open reminders per person."
    # The cap is per creator and doesn't limit reminders for oneself.
    await _create(client, lead_key, source=src)


async def test_dirty_ids_get_the_same_404(client: AsyncClient, db_session: AsyncSession):
    lead, lead_key, _, _, _, project = await _team(db_session)
    h = _hdrs(lead_key)
    miss = await client.get("/api/v1/me/reminders/rem_nope", headers=h)
    assert miss.status_code == 404
    for path in (
        "/api/v1/me/reminders/rem_%00",
        "/api/v1/me/reminders/rem_%00/notes",
        f"/api/v1/projects/{project.id}/meetings/mtg_%00/reminders",
        f"/api/v1/projects/{project.id}/tickets/tk_%00/reminders",
        f"/api/v1/projects/{project.id}/meetings/mtg_%00/items/mi_%00/reminder",
    ):
        resp = await client.get(path, headers=h) if not path.endswith("/reminder") else await client.post(path, headers=h)
        assert resp.status_code == 404, (path, resp.status_code)
    assert (await client.get("/api/v1/me/reminders/rem_%00", headers=h)).json() == miss.json()
    bad = await client.get("/api/v1/me/reminders?source=ticket:tk_%00", headers=h)
    assert bad.status_code == 422


async def test_updater_recorded_on_patch_and_snooze(client: AsyncClient, db_session: AsyncSession):
    lead, lead_key, _, _, org, project = await _team(db_session)
    ticket_id = await _create_ticket(client, project, lead_key)
    svc_key = await _service_key(
        db_session, owner=lead, org_id=org.id, scopes=["reminders:read", "reminders:write"]
    )
    rem = await _create(client, lead_key, source={"kind": "ticket", "id": ticket_id})
    assert rem["updated_actor_type"] == "user"
    renamed = await client.patch(
        f"/api/v1/me/reminders/{rem['id']}", headers=_hdrs(svc_key), json={"title": "renamed"}
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["updated_actor_type"] == "service"
    row = await db_session.get(Reminder, rem["id"])
    await db_session.refresh(row)
    assert row.updated_by_service_key_id is not None and row.updated_by_user_id == lead.id
    snoozed = await client.post(
        f"/api/v1/me/reminders/{rem['id']}/snooze", headers=_hdrs(lead_key), json={"preset": "1h"}
    )
    assert snoozed.json()["updated_actor_type"] == "user"
    await db_session.refresh(row)
    assert row.updated_by_service_key_id is None
