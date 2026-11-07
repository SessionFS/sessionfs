"""Helper publishes an EXISTING, unlinked ticket to Jira (propose_jira_publish).

Propose applies the same gates as propose_ticket's publish branch; confirm
re-checks them and publishes through the dashboard's publish path
(``publish_ticket_draft``), refusing a ticket that got linked in between.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import HelperAction, HelperMessage, TicketTrackerLink
from sessionfs.server.helper.tools import TOOLS
from sessionfs.server.services.helper_ticket_actions import publish_ticket_to_jira
from tests.server.integration.test_helper_jira import (
    _SITE,
    _ctx,
    _fake_publish_client,
    _hdrs,
    _make_action,
    _make_connection,
    _make_conversation,
    _make_link,
    _make_mapping,
    _make_org,
    _make_project,
    _make_session,
    _make_ticket,
    _make_user,
)


async def _setup(db_session, *, tier: str = "enterprise", mapped: bool = True):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, user, tier=tier)
    project = await _make_project(db_session, user, org)
    ticket = await _make_ticket(db_session, user, project)
    await _make_connection(db_session, org, user)
    if mapped:
        await _make_mapping(db_session, org, project, user, jira_project_key="ACME")
    return user, key, org, project, ticket


async def _propose(db_session, user, ticket, **extra):
    ctx = _ctx(db_session, user)
    out = await TOOLS["propose_jira_publish"].handler(ctx, {"ticket_id": ticket.id, **extra})
    return ctx, out


@pytest.mark.asyncio
async def test_propose_publish_existing_ticket(db_session: AsyncSession):
    user, _, _, project, ticket = await _setup(db_session)
    ctx, out = await _propose(db_session, user, ticket)
    assert "Proposed action" in out.text
    [prop] = ctx.pending_actions
    assert prop["kind"] == "publish_ticket_jira"
    assert prop["params"] == {
        "ticket_id": ticket.id,
        "project_id": project.id,
        "jira_project_key": "ACME",
    }
    assert prop["preview"]["summary"] == f"Publish {ticket.title} to Jira ACME"
    # Proposing writes nothing.
    assert (await db_session.execute(select(TicketTrackerLink))).scalars().all() == []


@pytest.mark.asyncio
async def test_propose_publish_refusals(db_session: AsyncSession):
    # Already linked → pointed at propose_jira_overwrite.
    user, _, org, _, ticket = await _setup(db_session)
    await _make_link(db_session, org, ticket, issue_key="ACME-9")
    ctx, out = await _propose(db_session, user, ticket)
    assert ctx.pending_actions == []
    assert "already linked to Jira ACME-9" in out.text
    assert "propose_jira_overwrite" in out.text

    # Not an editor (captured-session reader).
    admin, _, org, project, ticket = await _setup(db_session)
    reader, _ = await _make_user(db_session, name="reader")
    await _make_session(db_session, reader, project_id=project.id)
    ctx, out = await _propose(db_session, reader, ticket)
    assert ctx.pending_actions == []
    assert "requires editor access" in out.text

    # Plan without Jira integration.
    user, _, _, _, ticket = await _setup(db_session, tier="team")
    ctx, out = await _propose(db_session, user, ticket)
    assert ctx.pending_actions == []
    assert "plan does not include Jira integration" in out.text

    # Project without a Jira mapping.
    user, _, _, _, ticket = await _setup(db_session, mapped=False)
    ctx, out = await _propose(db_session, user, ticket)
    assert ctx.pending_actions == []
    assert "no Jira mapping" in out.text

    # An unmapped project-key override.
    user, _, _, _, ticket = await _setup(db_session)
    ctx, out = await _propose(db_session, user, ticket, jira_project_key="NOPE")
    assert ctx.pending_actions == []
    assert out.text.startswith("Cannot publish to Jira:")


@pytest.mark.asyncio
async def test_confirm_publishes_existing_ticket(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key, _, project, ticket = await _setup(db_session)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="publish_ticket_jira",
        params={"ticket_id": ticket.id, "project_id": project.id, "jira_project_key": "ACME"},
        conversation=conv,
    )
    state = _fake_publish_client(monkeypatch, issue_key="ACME-42")

    resp = await client.post(f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    [target] = resp.json()["result"]["targets"]
    assert target["ok"] is True and target["jira_published"] is True
    assert target["issue_key"] == "ACME-42"
    assert target["issue_url"] == f"{_SITE}/browse/ACME-42"
    assert state["create_issue"] == 1 and state["update_issue"] == 0

    link = (
        await db_session.execute(
            select(TicketTrackerLink).where(TicketTrackerLink.ticket_id == ticket.id)
        )
    ).scalar_one()
    assert link.status == "linked" and link.issue_key == "ACME-42"
    note = (
        await db_session.execute(
            select(HelperMessage).where(
                HelperMessage.conversation_id == conv.id, HelperMessage.role == "note"
            )
        )
    ).scalar_one()
    assert f"published ticket [ticket:{ticket.id}] to Jira ACME-42" in note.content


@pytest.mark.asyncio
async def test_confirm_after_link_elsewhere_is_refused(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    user, key, org, project, ticket = await _setup(db_session)
    conv = await _make_conversation(db_session, user)
    action = await _make_action(
        db_session,
        user,
        kind="publish_ticket_jira",
        params={"ticket_id": ticket.id, "project_id": project.id, "jira_project_key": "ACME"},
        conversation=conv,
    )
    # Someone links the ticket between proposal and confirm.
    await _make_link(db_session, org, ticket, issue_key="ACME-7")
    state = _fake_publish_client(monkeypatch)

    resp = await client.post(f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    [target] = resp.json()["result"]["targets"]
    assert target["ok"] is False
    assert target["error"] == "jira_already_linked"
    assert state["create_issue"] == 0 and state["update_issue"] == 0
    link = await db_session.get(TicketTrackerLink, ticket.id)
    await db_session.refresh(link)
    assert link.issue_key == "ACME-7"
    stored = await db_session.get(HelperAction, action.id)
    await db_session.refresh(stored)
    assert stored.status == "failed"


@pytest.mark.asyncio
async def test_confirm_after_losing_edit_access_is_refused(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    admin, _, org, project, ticket = await _setup(db_session)
    reader, reader_key = await _make_user(db_session, name="reader")
    await _make_session(db_session, reader, project_id=project.id)
    conv = await _make_conversation(db_session, reader)
    action = await _make_action(
        db_session,
        reader,
        kind="publish_ticket_jira",
        params={"ticket_id": ticket.id, "project_id": project.id, "jira_project_key": "ACME"},
        conversation=conv,
    )
    state = _fake_publish_client(monkeypatch)
    resp = await client.post(
        f"/api/v1/helper/actions/{action.id}/confirm", headers=_hdrs(reader_key)
    )
    assert resp.status_code == 200, resp.text
    [target] = resp.json()["result"]["targets"]
    assert target["ok"] is False and target["error"] == "jira_edit_required"
    assert state["create_issue"] == 0


@pytest.mark.asyncio
async def test_shared_publish_path_refuses_a_linked_ticket_when_required(
    db_session: AsyncSession, monkeypatch
):
    """The guard inside publish_ticket_draft (after the claim) closes the
    race the confirm's pre-check can't: a link committed just before the
    claim is refused, never updated."""
    user, _, org, project, ticket = await _setup(db_session)
    await _make_link(db_session, org, ticket, issue_key="ACME-7")
    state = _fake_publish_client(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await publish_ticket_to_jira(
            db_session,
            project_id=project.id,
            ticket_id=ticket.id,
            actor_user_id=user.id,
            require_unlinked=True,
        )
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "jira_already_linked"
    assert state["update_issue"] == 0 and state["create_issue"] == 0


def test_outcome_note_never_says_nothing_changed_after_a_jira_timeout() -> None:
    import json as _json

    from sessionfs.server.db.models import HelperAction
    from sessionfs.server.routes.helper import _action_outcome_text

    result = _json.dumps({"targets": [{"id": "tk_1", "ok": False, "error": "jira_timeout"}]})
    for status in ("failed", "confirmed"):
        action = HelperAction(id="ha_" + "0" * 24, kind="publish_ticket_jira", status=status, result=result)
        text = _action_outcome_text(action)
        assert "nothing" not in text, text
        assert "may" in text, text
