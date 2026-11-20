"""For You on 0.29.7+ develop: chat-thread action items (``kind='chat'``)
flow through the conversation provider with message ranges, and the card
actions for handoffs (Claim) and paused loops (Raise limit)."""

from __future__ import annotations

import hashlib
from datetime import timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    Handoff,
    MeetingActionItem,
    MeetingParticipant,
    MeetingTranscript,
    Reminder,
    Session,
)
from sessionfs.server.services import attention as svc
from tests.server.integration.test_attention_feed import (
    NOW,
    _feed,
    _h,
    _id,
    _project,
    _user,
)

pytestmark = pytest.mark.asyncio

CHAT = (
    "[2026-11-02 09:14] Kemi: Morning all\n"
    "[2026-11-02 09:15] Kemi: Ola, can you confirm the 0.29.8 ship date here?\n"
    "[2026-11-02 09:16] Ola: will do"
)


async def _chat_item(db: AsyncSession, project, me, *, owned: bool = True):
    m = MeetingTranscript(
        id=_id("chat"), org_id=project.org_id, project_id=project.id, kind="chat",
        provider="slack", channel_name="#release-train", title="release-train",
        source="chat_export", content=CHAT,
        content_sha256=hashlib.sha256(CHAT.encode()).hexdigest(),
        line_count=3, created_by_user_id=me.id,
    )
    db.add(m)
    await db.flush()
    part = MeetingParticipant(id=_id("mp"), transcript_id=m.id, name_raw="Ola",
                              user_id=me.id, resolution_method="manual")
    db.add(part)
    await db.flush()
    item = MeetingActionItem(id=_id("mai"), transcript_id=m.id, kind="action",
                             description="Confirm the 0.29.8 ship date",
                             owner_participant_id=part.id if owned else None,
                             line_start=2, line_end=3, status="pending", extracted_at=NOW)
    db.add(item)
    await db.commit()
    return m, item


async def test_chat_action_item_is_a_chat_card_with_message_ranges(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    me, key = await _user(db_session)
    p = await _project(db_session, me)
    m, item = await _chat_item(db_session, p, me)

    body = await _feed(client, key)
    (card,) = [i for i in body["items"] if i["kind"] == "chat_action"]
    assert card["source"] == {"type": "chat_item", "id": item.id}
    assert card["group"] == "meetings"
    assert card["actions"] == ["promote", "remind", "dismiss"]
    assert card["why"].startswith("From chat “release-train”")
    ctx = card["context"]
    assert ctx["source_label"] == "Chat"
    assert ctx["ref"] == f"{m.id} · M2-3"
    assert ctx["meeting"]["kind"] == "chat" and ctx["meeting"]["range"] == "M2-3"
    assert ctx["meeting"]["channel"] == "#release-train"
    # The stored "[time] Name: text" line gives the speaker without the time.
    assert ctx["speaker"] == "Kemi"
    assert ctx["quote"]["text"].startswith("Kemi: Ola, can you confirm")
    assert "[2026" not in ctx["quote"]["text"]
    assert ctx["quote"]["by"] == "release-train · M2-3 · Kemi"
    facts = dict(ctx["facts"])
    assert facts["Cited messages"] == "M2-3" and facts["Channel"] == "#release-train"
    assert body["groups"]["meetings"] == 1


async def test_chat_suggestion_when_nobody_was_named(client: AsyncClient, db_session: AsyncSession) -> None:
    me, key = await _user(db_session)
    p = await _project(db_session, me)
    await _chat_item(db_session, p, me, owned=False)
    body = await _feed(client, key)
    # Nobody was named, so this is awareness: "lately", not "your turn".
    assert [i["kind"] for i in body["items"] if i["group"] == "lately"] == ["chat_suggestion"]
    assert [i for i in body["items"] if i["band"] == "your_turn"] == []


async def test_chat_card_actions_use_the_meeting_routes(client: AsyncClient, db_session: AsyncSession) -> None:
    me, key = await _user(db_session)
    p = await _project(db_session, me)
    m, item = await _chat_item(db_session, p, me)

    # Acknowledge = the shared promote preview.
    prev = await client.post(
        f"/api/v1/meetings/{m.id}/action-items/{item.id}/promote/preview", headers=_h(key)
    )
    assert prev.status_code == 200, prev.text
    # Remind me later = the item reminder route, stored as a chat_item reminder.
    rem = await client.post(
        f"/api/v1/projects/{p.id}/meetings/{m.id}/items/{item.id}/reminder",
        headers=_h(key),
        json={"due_at": (NOW + timedelta(days=1)).isoformat()},
    )
    assert rem.status_code == 201, rem.text
    row = (await db_session.execute(select(Reminder).where(Reminder.id == rem.json()["id"]))).scalar_one()
    assert row.source_kind == "chat_item" and row.source_id == item.id
    # Not an action = the shared dismiss; the card leaves the feed.
    gone = await client.post(f"/api/v1/meetings/{m.id}/action-items/{item.id}/dismiss", headers=_h(key))
    assert gone.status_code == 200, gone.text
    body = await _feed(client, key)
    assert not [i for i in body["items"] if i["kind"].startswith("chat_")]


async def test_handoff_card_offers_claim(client: AsyncClient, db_session: AsyncSession) -> None:
    me, key = await _user(db_session)
    dara, _ = await _user(db_session, "dara")
    p = await _project(db_session, dara)
    sess = Session(id=_id("ses"), user_id=dara.id, title="x", tags="[]", source_tool="claude-code",
                   blob_key="x", blob_size_bytes=0, etag="x", project_id=p.id,
                   git_remote_normalized=p.git_remote_normalized,
                   created_at=NOW, updated_at=NOW, uploaded_at=NOW)
    db_session.add(sess)
    await db_session.commit()
    h = Handoff(id=_id("h")[:20], session_id=sess.id, sender_id=dara.id, recipient_user_id=me.id,
                status="pending", created_at=NOW, expires_at=NOW + timedelta(days=5))
    db_session.add(h)
    await db_session.commit()
    (card,) = [i for i in (await _feed(client, key))["items"] if i["kind"] == "handoff"]
    assert card["actions"][0] == "claim"


async def test_paused_loop_offers_raise_limit_only_to_editors_on_a_limit(
    db_session: AsyncSession,
) -> None:
    import json

    from sessionfs.server.db.models import AgentRun, Ticket, TicketComment

    me, _ = await _user(db_session)
    p = await _project(db_session, me)  # me owns it -> editor

    async def seed(project, *, config: dict, tokens: int = 0, verdict: str | None = None) -> str:
        parent = Ticket(
            id=_id("tk"), project_id=project.id, title="loop", kind="issue",
            status="open", created_by_user_id=me.id, loop_config=json.dumps(config),
        )
        db_session.add(parent)
        await db_session.flush()
        child = Ticket(
            id=_id("tk"), project_id=project.id, title="Child", kind="task",
            status="open", parent_ticket_id=parent.id, created_by_user_id=me.id,
        )
        db_session.add(child)
        await db_session.flush()
        if tokens:
            db_session.add(
                AgentRun(
                    id=_id("run"), project_id=project.id, persona_name="atlas",
                    role="implementer", ticket_id=child.id, head_sha="a1b2c3d4e5f6a7b8",
                    branch="feat/loop", input_tokens=tokens,
                    triggered_by_user_id=me.id, created_at=NOW,
                )
            )
        if verdict is not None:
            db_session.add(
                TicketComment(
                    id=_id("tc"), ticket_id=child.id, author_user_id=me.id,
                    author_persona="codex-reviewer", content=verdict,
                    verdict_trusted=True, reviewed_commit="a1b2c3d4e5f6a7b8", created_at=NOW,
                )
            )
        await db_session.commit()
        return child.id

    # An editor's child that hit the round cap offers Raise limit (rounds).
    rounds_child = await seed(
        p, config={"reviewer_persona": "reviewer", "max_review_rounds": 1},
        verdict="Codex R1 review on tk_x: CHANGES-REQUESTED",
    )
    # An editor's child that hit the token budget offers Raise limit (tokens).
    tokens_child = await seed(
        p, config={"reviewer_persona": "reviewer", "token_budget": 10}, tokens=20,
    )

    # A captured-session reader can read a project but not edit it.
    reader, _ = await _user(db_session, "reader")
    other = await _project(db_session, reader)
    db_session.add(
        Session(
            id=_id("ses"), user_id=me.id, title="synced from the same repo",
            tags="[]", source_tool="claude-code", blob_key="x", blob_size_bytes=0,
            etag="x", git_remote_normalized=other.git_remote_normalized,
            created_at=NOW, updated_at=NOW, uploaded_at=NOW,
        )
    )
    await db_session.commit()
    reader_child = await seed(
        other, config={"reviewer_persona": "reviewer", "token_budget": 10}, tokens=20,
    )

    feed = await svc.build_feed(db_session, svc.AttentionScope(user_id=me.id))
    cards = {i.source_id.split("#")[0]: i for i in feed.items if i.kind == "loop_needs_human"}

    assert cards[rounds_child].actions[0] == "raise"
    assert cards[rounds_child].context["loop"]["limit"] == "rounds"
    assert cards[tokens_child].actions[0] == "raise"
    assert cards[tokens_child].context["loop"]["limit"] == "tokens"
    # Not an editor: the same limit does not offer Raise limit.
    assert "raise" not in cards[reader_child].actions
    assert "loop" not in cards[reader_child].context


async def test_pending_suggestion_shows_in_the_why_line(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from sessionfs.server.db.models import Ticket

    me, key = await _user(db_session)
    p = await _project(db_session, me)
    other = await _project(db_session, me)
    m, item = await _chat_item(db_session, p, me)
    ticket = Ticket(id=_id("tk"), project_id=p.id, title="Confirm the ship date",
                    created_by_user_id=me.id)
    stray = Ticket(id=_id("tk"), project_id=other.id, title="Out of scope",
                   created_by_user_id=me.id)
    db_session.add_all([ticket, stray])
    item.suggested_ticket_id = ticket.id
    await db_session.commit()

    (card,) = [i for i in (await _feed(client, key))["items"] if i["kind"] == "chat_action"]
    assert f"looks like {ticket.id} “Confirm the ship date”" in card["why"]
    assert "not a ticket yet" not in card["why"]
    assert dict(card["context"]["facts"])["Looks like"] == f"{ticket.id} · Confirm the ship date"

    # A suggestion outside the conversation's scope, or dismissed, is not shown.
    for change in ({"suggested_ticket_id": stray.id}, {"suggestion_dismissed_at": NOW}):
        for k, v in change.items():
            setattr(item, k, v)
        await db_session.commit()
        (card,) = [i for i in (await _feed(client, key))["items"] if i["kind"] == "chat_action"]
        assert "not a ticket yet" in card["why"] and "looks like" not in card["why"]
