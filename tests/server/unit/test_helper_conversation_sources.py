"""Helper write proposals may cite meeting and chat ranges (GitHub #124 B).

A proposal's ``sources`` accept ``[meeting:<id> L<a>-<b>]`` and
``[chat:<id> M<a>-<b>]`` under the same rule as answer citations: the id was
returned by a tool earlier in the conversation, the tag / id prefix / range
letter / stored row kind agree, the row is still readable in scope, and the
range sits inside the transcript.
"""

from __future__ import annotations

import uuid

import pytest

from sessionfs.server.db.models import MeetingTranscript, Ticket
from sessionfs.server.helper.context import HelperScope
from sessionfs.server.helper.tools import TOOLS, ToolArgumentError
from tests.server.unit.test_helper_chat import _chat
from tests.server.unit.test_helper_coverage import (
    _add_member,
    _ctx,
    _make_org,
    _make_project,
    _make_user,
    _now,
)
from tests.server.unit.test_helper_meetings import _meeting


@pytest.fixture(autouse=True)
async def _enterprise_test_user(db_session, test_user):
    test_user.tier = "enterprise"
    await db_session.commit()
    return test_user


async def _setup(db_session, test_user):
    org = await _make_org(db_session, tier="enterprise")
    await _add_member(db_session, org=org, user=test_user, role="admin")
    project = await _make_project(db_session, owner=test_user, org=org)
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="Renew the cert",
        description="",
        priority="medium",
        kind="task",
        status="open",
        created_by_user_id=test_user.id,
        context_refs="[]",
        file_refs="[]",
        related_sessions="[]",
        acceptance_criteria="[]",
        actor_type="user",
        lease_epoch=0,
    )
    db_session.add(ticket)
    await db_session.commit()
    meeting = await _meeting(db_session, project=project, content="a\nb\nc")
    chat = await _chat(db_session, project=project)
    ctx = _ctx(db_session, test_user, scope=HelperScope(project_id=project.id))
    ctx.seen_meeting_ids.update({meeting.id, chat.id})
    return project, ticket, meeting, chat, ctx


async def _comment(ctx, ticket, *sources: str):
    return await TOOLS["propose_comment"].handler(
        ctx,
        {"ticket_id": ticket.id, "body": "Bob owns the cert renewal.", "sources": {"body": list(sources)}},
    )


@pytest.mark.asyncio
async def test_comment_and_ticket_cite_seen_meeting_and_chat_ranges(db_session, test_user):
    project, ticket, meeting, chat, ctx = await _setup(db_session, test_user)

    out = await _comment(ctx, ticket, f"[meeting:{meeting.id} L1-3]", f"[chat:{chat.id} M2-2]")
    assert "Proposed action" in out.text
    assert ctx.pending_actions[-1]["params"]["sources"]["body"] == [
        f"[meeting:{meeting.id} L1-3]",
        f"[chat:{chat.id} M2-2]",
    ]

    out = await TOOLS["propose_ticket"].handler(
        ctx,
        {
            "project_id": project.id,
            "title": "Renew the LB cert",
            "description": "Bob agreed to renew the load balancer cert.",
            "acceptance_criteria": ["Cert renewed"],
            "sources": {
                "title": [f"[chat:{chat.id} M2-2]"],
                "description": [f"[meeting:{meeting.id} L2-2]", "user"],
                "acceptance_criteria": ["user"],
            },
        },
    )
    assert "Proposed action" in out.text
    assert ctx.pending_actions[-1]["kind"] == "create_ticket"


@pytest.mark.asyncio
async def test_bad_conversation_sources_are_refused(db_session, test_user):
    project, ticket, meeting, chat, ctx = await _setup(db_session, test_user)
    unseen = await _meeting(db_session, project=project)
    stranger = await _make_user(
        db_session, email=f"x-{uuid.uuid4().hex[:6]}@x.com", tier="enterprise"
    )
    unreadable = await _meeting(
        db_session, project=await _make_project(db_session, owner=stranger)
    )
    deleted = await _meeting(db_session, project=project, deleted=True)
    # A row whose id looks like a chat but whose stored kind is meeting.
    disguised = MeetingTranscript(
        id=f"chat_{uuid.uuid4().hex[:16]}",
        org_id=project.org_id,
        project_id=project.id,
        title="x",
        source="manual_paste",
        content="a\nb",
        content_sha256=uuid.uuid4().hex + uuid.uuid4().hex[:32],
        line_count=2,
        created_at=_now(),
    )
    db_session.add(disguised)
    await db_session.commit()
    ctx.seen_meeting_ids.update({unreadable.id, deleted.id, disguised.id})

    with pytest.raises(ToolArgumentError, match="was not returned by a tool"):
        await _comment(ctx, ticket, f"[meeting:{unseen.id} L1-1]")
    bad = [
        f"[chat:{meeting.id} M1-1]",  # chat marker on a meeting row
        f"[meeting:{chat.id} L1-1]",  # meeting marker on a chat row
        f"[chat:{chat.id} L1-1]",  # wrong range letter
        f"[chat:{disguised.id} M1-1]",  # stored row kind disagrees
        f"[meeting:{meeting.id} L2-4]",  # past the last line
        f"[meeting:{meeting.id} L0-1]",
        f"[meeting:{meeting.id} L3-2]",
        f"[meeting:{unreadable.id} L1-1]",  # not readable by the caller
        f"[meeting:{deleted.id} L1-1]",
    ]
    for src in bad:
        with pytest.raises(ToolArgumentError, match="not a readable meeting or chat range"):
            await _comment(ctx, ticket, "user", src)
    assert ctx.pending_actions == []
