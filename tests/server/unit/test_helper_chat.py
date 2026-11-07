"""Session helper chat threads (GitHub #118, C2).

Chat threads are ``kind='chat'`` conversation rows read by the meeting tools
(``kind`` = meeting | chat | all, default all). Message N is line N and is
cited as ``[chat:<id> M<a>-<b>]``, validated exactly like a meeting
citation: seen earlier in the conversation, still readable, range inside
the thread, and the tag / id prefix / range letter / row kind must agree.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import sessionfs.server.routes.helper as helper_mod
from sessionfs.judge.agent_stream import Done, TextDelta, ToolCall, ToolCallEvent, ToolResultEvent, Usage
from sessionfs.server.db.models import MeetingTranscript
from sessionfs.server.helper.context import HelperScope
from sessionfs.server.helper.tools import TOOLS
from tests.server.integration.test_helper_api import (
    _fake_resolve,
    _hdrs,
    _make_org_with,
    _make_project as _route_make_project,
    _make_user as _route_make_user,
    _parse_sse,
)
from tests.server.unit.test_helper_coverage import (
    RecordingSink,
    _add_member,
    _ctx,
    _make_org,
    _make_project,
    _make_user,
    _now,
    _run,
)
from tests.server.unit.test_helper_meetings import _meeting, _owned_item

_CONTENT = (
    "[2026-10-29 14:02] Tolu: we're going with Dataproc\n"
    "[2026-10-29 14:03] Bob: ok, I'll renew the LB cert\n"
    "[2026-10-29 14:05] Tolu: thanks"
)


@pytest.fixture(autouse=True)
async def _enterprise_test_user(db_session, test_user):
    test_user.tier = "enterprise"
    await db_session.commit()
    return test_user


async def _chat(
    db: AsyncSession,
    *,
    project=None,
    group=None,
    channel: str = "#infra",
    title: str = "#infra",
    content: str = _CONTENT,
    deleted: bool = False,
) -> MeetingTranscript:
    m = MeetingTranscript(
        id=f"chat_{uuid.uuid4().hex[:16]}",
        org_id=(project.org_id if project is not None else group.org_id),
        project_id=project.id if project is not None else None,
        group_id=group.id if group is not None else None,
        kind="chat",
        provider="slack",
        channel_name=channel,
        title=title,
        source="chat_export",
        content=content,
        content_sha256=uuid.uuid4().hex + uuid.uuid4().hex[:32],
        line_count=len(content.split("\n")),
        deleted_at=_now() if deleted else None,
    )
    db.add(m)
    await db.commit()
    return m


def test_prompt_is_v17_and_teaches_chat_citations():
    assert helper_mod._HELPER_SYSTEM_PROMPT_VERSION == "v19"
    prompt = helper_mod._system_prompt(HelperScope())
    assert "[chat:<id> M<first>-<last>]" in prompt
    assert "[meeting:<id> L<first>-<last>]" in prompt
    assert "kind=chat" in prompt and "untrusted data" in prompt


def test_meeting_tools_take_kind_and_actions_cover_both_kinds():
    for name in ("search_meetings", "list_recent_meetings", "get_meeting", "list_my_meeting_actions"):
        props = TOOLS[name].input_schema["properties"]
        assert props["kind"]["enum"] == ["meeting", "chat", "all"], name
    desc = TOOLS["list_my_meeting_actions"].description
    assert "conversation actions" in desc and "[chat:<id> M<a>-<b>]" in desc


@pytest.mark.asyncio
async def test_get_chat_numbers_messages_and_marks_untrusted(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    chat = await _chat(db_session, project=project)
    sink = RecordingSink()
    ctx = _ctx(db_session, test_user, audit=sink)

    res = await _run(ctx, "get_meeting", {"meeting_id": chat.id})
    assert f"[chat:{chat.id}] chat #infra" in res.content
    assert "3 messages" in res.content
    assert "M1| [2026-10-29 14:02] Tolu: we're going with Dataproc" in res.content
    assert "M3|" in res.content and "L1|" not in res.content
    assert "Chat messages follow" in res.content and "untrusted data" in res.content
    assert sink.records[-1][2]["meeting_ids"] == [chat.id]
    assert chat.id in ctx.seen_meeting_ids

    page = await _run(ctx, "get_meeting", {"meeting_id": chat.id, "line_start": 2, "line_end": 2})
    assert "M2|" in page.content and "M1|" not in page.content
    # A kind that doesn't match the row is a plain miss.
    miss = await _run(ctx, "get_meeting", {"meeting_id": chat.id, "kind": "meeting"})
    assert miss.content == "Meeting not found."
    assert "M1|" in (await _run(ctx, "get_meeting", {"meeting_id": chat.id, "kind": "chat"})).content
    bad = await _run(ctx, "get_meeting", {"meeting_id": chat.id, "kind": "slack"})
    assert bad.is_error


@pytest.mark.asyncio
async def test_search_and_list_filter_by_kind(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    chat = await _chat(db_session, project=project)
    meeting = await _meeting(db_session, project=project, content="Ann: Dataproc it is\nBob: ok")
    ctx = _ctx(db_session, test_user, audit=RecordingSink())

    both = await _run(ctx, "search_meetings", {"query": "dataproc"})
    assert chat.id in both.content and meeting.id in both.content
    assert "M1: [2026-10-29 14:02] Tolu: we're going with Dataproc" in both.content
    assert "L1: Ann: Dataproc it is" in both.content
    only_chat = await _run(ctx, "search_meetings", {"query": "dataproc", "kind": "chat"})
    assert chat.id in only_chat.content and meeting.id not in only_chat.content
    only_meeting = await _run(ctx, "search_meetings", {"query": "dataproc", "kind": "meeting"})
    assert meeting.id in only_meeting.content and chat.id not in only_meeting.content

    listed = await _run(ctx, "list_recent_meetings", {"kind": "chat"})
    assert chat.id in listed.content and meeting.id not in listed.content
    assert f"[chat:{chat.id}]" in listed.content
    assert (await _run(ctx, "list_recent_meetings", {"kind": "x"})).is_error

    other = await _make_project(db_session, owner=test_user)
    await _meeting(db_session, project=other)
    empty = await _run(ctx, "search_meetings", {"query": "zzz", "kind": "chat"})
    assert empty.content == "No chat threads matched."


@pytest.mark.asyncio
async def test_deleted_and_foreign_chats_are_hidden(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    gone = await _chat(db_session, project=project, deleted=True)
    stranger = await _make_user(db_session, email=f"x-{uuid.uuid4().hex[:6]}@x.com", tier="enterprise")
    foreign = await _chat(db_session, project=await _make_project(db_session, owner=stranger))
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    listed = await _run(ctx, "list_recent_meetings", {"kind": "all"})
    assert gone.id not in listed.content and foreign.id not in listed.content
    for cid in (gone.id, foreign.id):
        assert (await _run(ctx, "get_meeting", {"meeting_id": cid})).content == "Meeting not found."


@pytest.mark.asyncio
async def test_chat_citation_validation(db_session, test_user):
    org = await _make_org(db_session, tier="enterprise")
    await _add_member(db_session, org=org, user=test_user, role="admin")
    project = await _make_project(db_session, owner=test_user, org=org)
    chat = await _chat(db_session, project=project)
    unseen = await _chat(db_session, project=project)
    meeting = await _meeting(db_session, project=project, content="a\nb\nc")
    stranger = await _make_user(db_session, email=f"x-{uuid.uuid4().hex[:6]}@x.com", tier="enterprise")
    unreadable = await _chat(db_session, project=await _make_project(db_session, owner=stranger))
    seen_ids = {chat.id, meeting.id, unreadable.id}

    text = (
        f"ok [chat:{chat.id} M1-2] dup [chat:{chat.id} M1-2] "
        f"range [chat:{chat.id} M1-4] zero [chat:{chat.id} M0-1] "
        f"inverted [chat:{chat.id} M3-2] unseen [chat:{unseen.id} M1-1] "
        f"unreadable [chat:{unreadable.id} M1-1] "
        f"letter [chat:{chat.id} L1-2] tag [meeting:{chat.id} L1-2] "
        f"rowkind [chat:{meeting.id.replace('meet_', 'chat_')} M1-1] "
        f"meeting-as-chat [meeting:{meeting.id} M1-1] "
        f"meeting [meeting:{meeting.id} L1-2]"
    )
    citations, cleaned = await helper_mod._parse_meeting_citations(
        db_session, test_user.id, text, seen_ids, HelperScope(project_id=project.id)
    )
    assert [(c["kind"], c["meeting_id"], c["line_start"], c["line_end"]) for c in citations] == [
        ("chat", chat.id, 1, 2),
        ("meeting", meeting.id, 1, 2),
    ]
    assert citations[0]["channel_name"] == "#infra"
    assert "channel_name" not in citations[1]
    assert cleaned.count(f"[chat:{chat.id} M1-2]") == 2
    for gone in ("M1-4", "M0-1", "M3-2", unseen.id, unreadable.id, f"[chat:{chat.id} L1-2]",
                 f"[meeting:{chat.id} L1-2]", f"[meeting:{meeting.id} M1-1]"):
        assert gone not in cleaned

    # The single-marker (streaming) validator applies the same rules.
    async def ok(marker: str, seen=seen_ids) -> bool:
        match = helper_mod._MEETING_CITATION_RE.fullmatch(marker)
        assert match is not None
        return await helper_mod._meeting_marker_valid(
            db_session, test_user.id, HelperScope(project_id=project.id), seen, match
        )

    assert await ok(f"[chat:{chat.id} M2-3]")
    assert not await ok(f"[chat:{chat.id} M2-9]")
    assert not await ok(f"[chat:{unseen.id} M1-1]")
    assert not await ok(f"[chat:{unreadable.id} M1-1]")
    assert not await ok(f"[meeting:{chat.id} L1-1]")
    assert not await ok(f"[chat:{chat.id} L1-1]")

    assert helper_mod._strip_meeting_citations(
        f"a [chat:{chat.id} M1-2] b [chat:{chat.id} M2-2]", {(chat.id, 1, 2)}
    ) == f"a [chat:{chat.id} M1-2] b "


@pytest.mark.asyncio
async def test_conversation_actions_cover_meetings_and_chats(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    meeting = await _meeting(db_session, project=project, title="Weekly sync")
    chat = await _chat(db_session, project=project)
    await _owned_item(db_session, meeting, user_id=test_user.id, text="ship the deploy")
    await _owned_item(db_session, chat, user_id=test_user.id, text="renew the LB cert")
    ctx = _ctx(db_session, test_user, audit=RecordingSink())

    both = await _run(ctx, "list_my_meeting_actions", {})
    assert f"[meeting:{meeting.id} L1-1] ship the deploy" in both.content
    assert f"[chat:{chat.id} M1-1] renew the LB cert" in both.content
    assert "chat #infra" in both.content and "meeting Weekly sync" in both.content
    assert chat.id in ctx.seen_meeting_ids and meeting.id in ctx.seen_meeting_ids

    only_chat = await _run(ctx, "list_my_meeting_actions", {"kind": "chat"})
    assert chat.id in only_chat.content and meeting.id not in only_chat.content
    only_meeting = await _run(ctx, "list_my_meeting_actions", {"kind": "meeting"})
    assert meeting.id in only_meeting.content and chat.id not in only_meeting.content


def _fake_split_chat_text(chat_id: str, hidden_id: str):
    text = (
        f"ok [chat:{chat_id} M1-2] bad-range [chat:{chat_id} M1-99] "
        f"unseen [chat:{hidden_id} M1-1] wrong [chat:{chat_id} L1-1] "
        f"plain [chat] and [chat:oops] end"
    )

    async def _fake(*, execute_tool, **kwargs):
        call = ToolCall(id="call_1", name="get_meeting", arguments={"meeting_id": chat_id})
        yield ToolCallEvent(call)
        result = await execute_tool(call)
        yield ToolResultEvent(call, result)
        yield Usage(input_tokens=5, output_tokens=2)
        for i in range(0, len(text), 4):
            yield TextDelta(text[i : i + 4])
        yield Done("stop")

    return _fake


@pytest.mark.asyncio
async def test_chat_markers_held_back_until_valid_when_streaming(client, db_session, monkeypatch):
    user, key = await _route_make_user(db_session)
    org = await _make_org_with(db_session, user)
    project = await _route_make_project(db_session, user, org.id)
    chat = await _chat(db_session, project=project)
    hidden = await _chat(db_session, project=project)

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_mod, "run_agent", _fake_split_chat_text(chat.id, hidden.id))
    body = ""
    async with client.stream(
        "POST", "/api/v1/helper/turns", headers=_hdrs(key), json={"message": "q", "stream": True}
    ) as resp:
        assert resp.status_code == 200
        async for chunk in resp.aiter_text():
            body += chunk
    events = _parse_sse(body)
    deltas = [d["delta"] for k, d in events if k == "text"]
    streamed = "".join(deltas)
    assert f"[chat:{chat.id} M1-2]" in streamed
    assert "M1-99" not in streamed and hidden.id not in streamed
    assert f"[chat:{chat.id} L1-1]" not in streamed
    assert all(hidden.id not in d and "M1-99" not in d for d in deltas)
    assert "plain [chat] and [chat:oops] end" in streamed
    cites = [d for k, d in events if k == "chat_citation"]
    assert [(c["meeting_id"], c["line_start"], c["line_end"]) for c in cites] == [(chat.id, 1, 2)]
    assert cites[0]["kind"] == "chat" and cites[0]["channel_name"] == "#infra"

    # JSON path: the same answer and a kind=chat citation.
    resp = await client.post(
        "/api/v1/helper/turns", headers=_hdrs(key), json={"message": "q", "stream": False}
    )
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert f"[chat:{chat.id} M1-2]" in out["answer"] and hidden.id not in out["answer"]
    chat_cites = [c for c in out["citations"] if c["kind"] == "chat"]
    assert [(c["meeting_id"], c["line_start"], c["line_end"]) for c in chat_cites] == [
        (chat.id, 1, 2)
    ]
    assert chat_cites[0]["channel_name"] == "#infra"


def _fake_cite_only(text: str):
    async def _fake(*, execute_tool, **kwargs):
        yield Usage(input_tokens=1, output_tokens=1)
        yield TextDelta(text)
        yield Done("stop")

    return _fake


@pytest.mark.asyncio
async def test_chat_citation_needs_an_earlier_tool_result(client, db_session, monkeypatch):
    user, key = await _route_make_user(db_session)
    org = await _make_org_with(db_session, user)
    project = await _route_make_project(db_session, user, org.id)
    chat = await _chat(db_session, project=project)

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_mod, "run_agent", _fake_split_chat_text(chat.id, chat.id))
    first = await client.post(
        "/api/v1/helper/turns", headers=_hdrs(key), json={"message": "read", "stream": False}
    )
    conversation_id = first.json()["conversation_id"]
    # Same conversation, no tool call: the earlier chat thread is citable.
    monkeypatch.setattr(helper_mod, "run_agent", _fake_cite_only(f"As said [chat:{chat.id} M2-3]."))
    second = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "again", "stream": False, "conversation_id": conversation_id},
    )
    assert f"[chat:{chat.id} M2-3]" in second.json()["answer"]
    # A fresh conversation never saw it: stripped.
    fresh = await client.post(
        "/api/v1/helper/turns", headers=_hdrs(key), json={"message": "new", "stream": False}
    )
    assert "[chat:" not in fresh.json()["answer"] and fresh.json()["citations"] == []
    # And once deleted it is no longer citable even in the old conversation.
    chat.deleted_at = _now()
    await db_session.commit()
    third = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "again", "stream": False, "conversation_id": conversation_id},
    )
    assert "[chat:" not in third.json()["answer"]


# ── Delimiter neutralising stays linear on uploaded text ──


def test_transcript_delimiter_neutraliser_is_linear():
    """Repeated unclosed tag starts and long whitespace runs after ``<`` were
    quadratic (``[^>]*>`` rescanned the line from every tag start; ``\\s*/?\\s*``
    let two whitespace runs compete). Both now finish well under a second."""
    import time

    from sessionfs.server.helper.settings import (
        neutralize_delimiters,
        neutralize_group_context_delimiters,
    )
    from sessionfs.server.services.meeting_extraction import _neutralize_transcript

    attacks = [
        "<chat_thread" * 40_000,
        "<transcript" * 40_000,
        "<org instructions" * 25_000,
        "<" + " " * 400_000,
    ]
    for text in attacks:
        start = time.perf_counter()
        _neutralize_transcript(text)
        neutralize_delimiters(text)
        neutralize_group_context_delimiters(text)
        assert time.perf_counter() - start < 3.0


def test_unclosed_tag_starts_are_defanged():
    from sessionfs.server.services.meeting_extraction import _neutralize_transcript

    for text in ("</chat_thread", "< / transcript", "</chat_thread foo", "<CHAT_THREAD>"):
        assert "[tag removed]" in _neutralize_transcript(text)
    assert _neutralize_transcript("a < b and c > d") == "a < b and c > d"
