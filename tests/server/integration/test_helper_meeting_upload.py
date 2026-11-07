"""Session helper: save a pasted transcript as a meeting, confirm-first.

``propose_meeting_upload`` only PROPOSES. The transcript is the caller's own
stored (redacted) message of the turn, never model text. Confirm re-checks the
conversation scope and the upload route's rules, then stores the meeting
through the upload core (DLP, caps, caption timing) and runs extraction.
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import sessionfs.server.services.meeting_extraction as extraction_mod
from sessionfs.judge.agent_stream import Done, TextDelta, Usage
from sessionfs.server.db.models import (
    HelperAction,
    HelperConversation,
    MeetingActionItem,
    MeetingTranscript,
    OrgMember,
    RetrievalAuditEvent,
)
from sessionfs.server.helper.keys import ResolvedLLM
from sessionfs.server.helper.tools import TOOLS, ToolArgumentError
from tests.server.integration.test_helper_ticket_actions import (
    _ctx,
    _hdrs,
    _make_org_with,
    _make_project,
    _make_user,
)

TRANSCRIPT = (
    "Please save this meeting and pull out the action items:\n"
    "Priya: Sam, can you draft the rollout plan by Friday?\n"
    "Sam: Yes, I will.\n"
    "Priya: Decision: we launch on November 20th."
)

_EXTRACTED = json.dumps({
    "participants": [{"name": "Priya"}, {"name": "Sam"}],
    "items": [
        {"kind": "action", "description": "Draft the rollout plan.", "owner_name": "Sam",
         "line_start": 1, "line_end": 2},
        {"kind": "decision", "description": "Launch on November 20th.", "line_start": 3,
         "line_end": 3},
    ],
})


def _install_llm(monkeypatch: pytest.MonkeyPatch, text: str = _EXTRACTED) -> None:
    async def _resolve(user, org_id, db, **kw):
        return ResolvedLLM(
            provider="openai", model="gpt-4o", api_key="sk-test", base_url=None, key_source="user"
        )

    async def _stream(**kwargs):
        yield Usage(input_tokens=5, output_tokens=5)
        yield TextDelta(text)
        yield Done("stop")

    monkeypatch.setattr(extraction_mod, "resolve_helper_llm", _resolve)
    monkeypatch.setattr(extraction_mod, "stream_chat", _stream)


async def _conversation(db: AsyncSession, user, *, project_id=None, group_id=None, org_id=None):
    import uuid

    conv = HelperConversation(
        id=f"hc_{uuid.uuid4().hex[:24]}",
        user_id=user.id,
        org_id=org_id,
        title="Meeting thread",
        scope=json.dumps({"project_id": project_id, "group_id": group_id, "session_ids": []}),
    )
    db.add(conv)
    await db.commit()
    return conv


async def _store(db: AsyncSession, user, conv, proposal: dict) -> str:
    db.add(
        HelperAction(
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
    )
    await db.commit()
    return proposal["id"]


async def _meetings(db: AsyncSession) -> list[MeetingTranscript]:
    db.expire_all()
    return list((await db.execute(select(MeetingTranscript))).scalars())


def _scoped_ctx(db, user, project_id, message=TRANSCRIPT):
    ctx = _ctx(db, user, project_id=project_id)
    ctx.turn_message = message
    return ctx


@pytest.mark.asyncio
async def test_propose_takes_the_callers_message_and_never_stores(db_session: AsyncSession):
    user, _ = await _make_user(db_session)
    project = await _make_project(db_session, user)
    ctx = _scoped_ctx(db_session, user, project.id)
    out = await TOOLS["propose_meeting_upload"].handler(
        ctx, {"title": "Platform sync", "first_line": 2, "meeting_at": "2026-11-03"}
    )
    assert "Proposed action" in out.text
    [proposal] = ctx.pending_actions
    assert proposal["kind"] == "create_meeting"
    params = proposal["params"]
    # The instruction line is left out; the text is the caller's, verbatim.
    assert params["content"] == "\n".join(TRANSCRIPT.split("\n")[1:])
    assert params["project_id"] == project.id and params["group_id"] is None
    assert params["extract"] is True and params["line_count"] == 3
    assert params["meeting_at"].startswith("2026-11-03T00:00:00")
    assert "Save meeting “Platform sync” (3 lines)" in proposal["preview"]["summary"]
    assert "extract its action items" in proposal["preview"]["summary"]
    assert await _meetings(db_session) == []


@pytest.mark.asyncio
async def test_propose_refuses_without_a_scope_or_text_or_edit_access(db_session: AsyncSession):
    alice, _ = await _make_user(db_session, "alice")
    bob, _ = await _make_user(db_session, "bob")
    # No project or group scope: nothing to save into.
    unscoped = _ctx(db_session, alice)
    unscoped.turn_message = TRANSCRIPT
    out = await TOOLS["propose_meeting_upload"].handler(unscoped, {"title": "x"})
    assert "project or a group" in out.text and unscoped.pending_actions == []
    # Someone else's project: same refusal as any non-editor, no proposal.
    bobs = await _make_project(db_session, bob)
    foreign = _scoped_ctx(db_session, alice, bobs.id)
    out = await TOOLS["propose_meeting_upload"].handler(foreign, {"title": "x"})
    assert "cannot add meetings" in out.text and foreign.pending_actions == []
    # An empty message slice and an out-of-range slice.
    mine = await _make_project(db_session, alice)
    blank = _scoped_ctx(db_session, alice, mine.id, message="   \n  ")
    out = await TOOLS["propose_meeting_upload"].handler(blank, {"title": "x"})
    assert "no transcript text" in out.text and blank.pending_actions == []
    ctx = _scoped_ctx(db_session, alice, mine.id)
    with pytest.raises(ToolArgumentError):
        await TOOLS["propose_meeting_upload"].handler(ctx, {"title": "x", "first_line": 9})
    with pytest.raises(ToolArgumentError):
        await TOOLS["propose_meeting_upload"].handler(ctx, {"title": "x", "meeting_at": "soon"})
    assert ctx.pending_actions == []


@pytest.mark.asyncio
async def test_confirm_stores_once_extracts_and_audits_without_the_text(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    _install_llm(monkeypatch)
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    project_id, user_id = project.id, user.id
    conv = await _conversation(db_session, user, project_id=project_id)
    ctx = _scoped_ctx(db_session, user, project_id)
    await TOOLS["propose_meeting_upload"].handler(ctx, {"title": "Platform sync", "first_line": 2})
    action_id = await _store(db_session, user, conv, ctx.pending_actions[0])

    first = await client.post(f"/api/v1/helper/actions/{action_id}/confirm", headers=_hdrs(key))
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["status"] == "confirmed"
    [target] = body["result"]["targets"]
    assert target["ok"] is True and target["meeting_id"].startswith("meet_")
    assert target["extraction_status"] == "done"
    assert target["action_item_count"] == 1 and target["decision_count"] == 1

    again = await client.post(f"/api/v1/helper/actions/{action_id}/confirm", headers=_hdrs(key))
    assert again.status_code == 200 and again.json()["status"] == "confirmed"
    [meeting] = await _meetings(db_session)
    assert meeting.project_id == project_id
    assert meeting.source == "manual_paste" and meeting.created_by_user_id == user_id
    assert meeting.content.startswith("Priya: Sam, can you draft")
    items = list(
        (await db_session.execute(
            select(MeetingActionItem).where(MeetingActionItem.transcript_id == meeting.id)
        )).scalars()
    )
    assert sorted(i.kind for i in items) == ["action", "decision"]

    # The decision's audit row records the action, never the transcript text.
    events = list((await db_session.execute(select(RetrievalAuditEvent))).scalars())
    blob = " ".join(json.dumps(e.arguments, default=str) if not isinstance(e.arguments, str) else e.arguments for e in events)
    assert "propose_meeting_upload" in " ".join(e.tool_name for e in events)
    assert "rollout plan" not in blob


@pytest.mark.asyncio
async def test_confirm_keeps_the_meeting_when_no_key_is_set(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    async def _no_key(user, org_id, db, **kw):
        return None

    monkeypatch.setattr(extraction_mod, "resolve_helper_llm", _no_key)
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    conv = await _conversation(db_session, user, project_id=project.id)
    ctx = _scoped_ctx(db_session, user, project.id)
    await TOOLS["propose_meeting_upload"].handler(ctx, {"title": "Sync"})
    action_id = await _store(db_session, user, conv, ctx.pending_actions[0])
    resp = await client.post(f"/api/v1/helper/actions/{action_id}/confirm", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    [target] = resp.json()["result"]["targets"]
    assert target["ok"] is True and target["extraction_error"] == "llm_key_required"
    assert len(await _meetings(db_session)) == 1


@pytest.mark.asyncio
async def test_confirm_rechecks_scope_and_access(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    _install_llm(monkeypatch)
    admin, _ = await _make_user(db_session, "admin")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org_with(db_session, admin, member)
    project = await _make_project(db_session, admin, org_id=org.id)
    conv = await _conversation(db_session, member, project_id=project.id, org_id=org.id)
    ctx = _scoped_ctx(db_session, member, project.id)
    await TOOLS["propose_meeting_upload"].handler(ctx, {"title": "Sync"})
    proposal = ctx.pending_actions[0]

    # A proposal whose params name another scope than its conversation is refused.
    tampered = dict(proposal, id="ha_tampered0000000000000000", params=dict(proposal["params"], project_id="prj_other"))
    tampered_id = await _store(db_session, member, conv, tampered)
    resp = await client.post(f"/api/v1/helper/actions/{tampered_id}/confirm", headers=_hdrs(member_key))
    assert resp.json()["status"] == "failed"
    assert resp.json()["result"]["targets"][0]["error"] == "not_in_scope"

    # Losing org membership after the proposal fails the confirm; nothing stored.
    action_id = await _store(db_session, member, conv, proposal)
    membership = (
        await db_session.execute(
            select(OrgMember).where(OrgMember.org_id == org.id, OrgMember.user_id == member.id)
        )
    ).scalar_one()
    await db_session.delete(membership)
    await db_session.commit()
    resp = await client.post(f"/api/v1/helper/actions/{action_id}/confirm", headers=_hdrs(member_key))
    # An org-bound thread is gone for someone who left the org.
    assert resp.status_code == 404, resp.text
    assert await _meetings(db_session) == []


@pytest.mark.asyncio
async def test_confirm_fails_when_edit_access_is_lost(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """A personal-thread proposal for a project the caller no longer edits
    fails at confirm with nothing stored."""
    _install_llm(monkeypatch)
    owner, _ = await _make_user(db_session, "owner")
    user, key = await _make_user(db_session, "user")
    project = await _make_project(db_session, user)
    project_id = project.id
    conv = await _conversation(db_session, user, project_id=project_id)
    ctx = _scoped_ctx(db_session, user, project_id)
    await TOOLS["propose_meeting_upload"].handler(ctx, {"title": "Sync"})
    action_id = await _store(db_session, user, conv, ctx.pending_actions[0])
    project.owner_id = owner.id
    await db_session.commit()
    resp = await client.post(f"/api/v1/helper/actions/{action_id}/confirm", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed"
    assert resp.json()["result"]["targets"][0]["error"] == "not_found"
    assert await _meetings(db_session) == []


@pytest.mark.asyncio
async def test_confirm_applies_the_upload_dlp_block(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """The proposal stores the turn's redacted text, but confirm still runs the
    upload core's DLP: a block-mode finding refuses and nothing is stored."""
    from sessionfs.server.routes import meetings as meeting_routes

    _install_llm(monkeypatch)
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    conv = await _conversation(db_session, user, project_id=project.id)
    ctx = _scoped_ctx(db_session, user, project.id)
    await TOOLS["propose_meeting_upload"].handler(ctx, {"title": "Sync"})
    action_id = await _store(db_session, user, conv, ctx.pending_actions[0])

    def _blocking(text, policy):
        from fastapi import HTTPException

        raise HTTPException(422, {"error": "dlp_blocked", "message": "blocked", "findings": {}})

    monkeypatch.setattr(meeting_routes, "_dlp_scan_content", _blocking)
    resp = await client.post(f"/api/v1/helper/actions/{action_id}/confirm", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "failed"
    assert resp.json()["result"]["targets"][0]["error"] == "dlp_blocked"
    assert await _meetings(db_session) == []
