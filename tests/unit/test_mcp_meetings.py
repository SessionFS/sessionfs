"""Dispatch tests for the meeting-transcript MCP tools (GitHub #116, M2).

HTTP is faked, so these only check what each tool sends and how it reports
the server's answer. Access rules live in the routes and are tested there.
"""

from __future__ import annotations

import json

import pytest

from sessionfs.mcp import server as mcp_server

API = "https://api.test"


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


@pytest.fixture
def http(monkeypatch):
    import httpx

    state: dict = {"calls": [], "responses": {}}

    class _Client:
        def __init__(self, *a, **k):
            state.setdefault("timeouts", []).append(k.get("timeout"))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def _go(self, method, url, params=None, json=None, headers=None):
            state["calls"].append(
                {"method": method, "url": url, "params": params or {}, "json": json, "headers": headers or {}}
            )
            status, body = state["responses"].get((method, url), (200, {}))
            return _Resp(status, body)

        async def get(self, url, *, params=None, headers=None):
            return await self._go("GET", url, params=params, headers=headers)

        async def post(self, url, *, json=None, headers=None, params=None):
            return await self._go("POST", url, params=params, json=json, headers=headers)

    monkeypatch.setattr(httpx, "AsyncClient", _Client)

    async def _resolve(git_remote=""):
        return API, "test-key", "proj_1"

    monkeypatch.setattr(mcp_server, "_resolve_project_id", _resolve)

    class _Cfg:
        class sync:
            api_url = API
            api_key = "test-key"

    monkeypatch.setattr(mcp_server, "load_config", lambda: _Cfg)
    return state


def test_all_six_tools_registered_and_dispatched():
    names = {t.name for t in mcp_server._TOOLS}
    expected = {
        "upload_meeting_transcript",
        "list_meeting_transcripts",
        "get_meeting_transcript",
        "extract_meeting_action_items",
        "promote_meeting_action_item",
        "search_meetings",
    }
    assert expected <= names


def test_promote_description_states_preview_only():
    tool = next(t for t in mcp_server._TOOLS if t.name == "promote_meeting_action_item")
    assert "PREVIEW ONLY" in tool.description
    assert "confirm" in tool.inputSchema["properties"]
    extract = next(t for t in mcp_server._TOOLS if t.name == "extract_meeting_action_items")
    assert "YOUR OWN" in extract.description


@pytest.mark.asyncio
async def test_upload_project(http):
    http["responses"][("POST", f"{API}/api/v1/projects/proj_1/meetings")] = (200, {"id": "meet_1"})
    res = await mcp_server._handle_upload_meeting_transcript(
        {"title": " Sync ", "content": "A: hi", "meeting_at": "2026-10-29T10:00:00Z", "extract": True}
    )
    assert res["id"] == "meet_1"
    call = http["calls"][0]
    assert call["json"] == {
        "title": "Sync",
        "content": "A: hi",
        "source": "manual_paste",
        "meeting_at": "2026-10-29T10:00:00Z",
        "extract": True,
    }
    assert call["headers"]["Authorization"] == "Bearer test-key"


@pytest.mark.asyncio
async def test_upload_group_uses_group_route_and_validates(http):
    http["responses"][("POST", f"{API}/api/v1/groups/pg_1/meetings")] = (200, {"id": "meet_2"})
    res = await mcp_server._handle_upload_meeting_transcript(
        {"title": "T", "content": "x", "group_id": "pg_1", "source": "file_upload"}
    )
    assert res["id"] == "meet_2"
    assert http["calls"][0]["json"]["source"] == "file_upload"

    assert "title" in (await mcp_server._handle_upload_meeting_transcript({"content": "x"}))["error"]
    assert "content" in (await mcp_server._handle_upload_meeting_transcript({"title": "t"}))["error"]
    bad = await mcp_server._handle_upload_meeting_transcript({"title": "t", "content": "x", "source": "email"})
    assert "source" in bad["error"]
    assert len(http["calls"]) == 1


@pytest.mark.asyncio
async def test_upload_surfaces_api_error(http):
    http["responses"][("POST", f"{API}/api/v1/projects/proj_1/meetings")] = (
        422, {"detail": {"code": "dlp_blocked"}},
    )
    res = await mcp_server._handle_upload_meeting_transcript({"title": "t", "content": "x"})
    assert res["error"].startswith("API error 422")
    assert "dlp_blocked" in res["error"]


@pytest.mark.asyncio
async def test_list_project_and_group_params(http):
    http["responses"][("GET", f"{API}/api/v1/projects/proj_1/meetings")] = (
        200, {"meetings": [], "next_cursor": None},
    )
    http["responses"][("GET", f"{API}/api/v1/groups/pg_1/meetings")] = (200, {"meetings": []})
    await mcp_server._handle_list_meeting_transcripts({"q": " roadmap ", "limit": 10, "before": "meet_9"})
    assert http["calls"][0]["params"] == {"q": "roadmap", "before": "meet_9", "limit": "10"}
    await mcp_server._handle_list_meeting_transcripts({"group_id": "pg_1"})
    assert http["calls"][1]["url"].endswith("/groups/pg_1/meetings")
    assert http["calls"][1]["params"] == {"limit": "100"}
    bad = await mcp_server._handle_list_meeting_transcripts({"limit": 0})
    assert "limit" in bad["error"]


@pytest.mark.asyncio
async def test_search_requires_q_and_uses_list_endpoint(http):
    assert "q is required" in (await mcp_server._handle_search_meetings({}))["error"]
    http["responses"][("GET", f"{API}/api/v1/projects/proj_1/meetings")] = (200, {"meetings": [{"id": "m"}]})
    res = await mcp_server._handle_search_meetings({"q": "budget"})
    assert res["meetings"] == [{"id": "m"}]
    assert http["calls"][0]["params"] == {"q": "budget", "limit": "25"}


@pytest.mark.asyncio
async def test_get_and_extract(http):
    http["responses"][("GET", f"{API}/api/v1/meetings/meet_1")] = (200, {"id": "meet_1"})
    http["responses"][("POST", f"{API}/api/v1/meetings/meet_1/extract")] = (200, {"dropped_items": 0})
    assert (await mcp_server._handle_get_meeting_transcript({"meeting_id": "meet_1"}))["id"] == "meet_1"
    assert (await mcp_server._handle_extract_meeting_action_items({"meeting_id": "meet_1"}))["dropped_items"] == 0
    assert "meeting_id" in (await mcp_server._handle_get_meeting_transcript({}))["error"]
    with pytest.raises(Exception):
        await mcp_server._handle_get_meeting_transcript({"meeting_id": "../../admin"})


_DRAFT = {
    "meeting_id": "meet_1",
    "item_id": "mi_1",
    "title": "Ship it.",
    "description": "d",
    "priority": "medium",
    "kind": "task",
    "assigned_to": None,
    "assigned_user_id": "u_1",
    "due_date": "2026-11-01",
    "context_refs": ["meeting:meet_1#mi_1"],
    "target_project_id": "proj_1",
}
_ROOT = f"{API}/api/v1/meetings/meet_1/action-items/mi_1/promote"


@pytest.mark.asyncio
async def test_promote_without_confirm_only_previews(http):
    http["responses"][("POST", f"{_ROOT}/preview")] = (200, _DRAFT)
    res = await mcp_server._handle_promote_meeting_action_item({"meeting_id": "meet_1", "item_id": "mi_1"})
    assert res["confirmed"] is False
    assert res["preview"] == _DRAFT
    assert [c["url"] for c in http["calls"]] == [f"{_ROOT}/preview"]


@pytest.mark.asyncio
async def test_promote_confirm_sends_draft_with_overrides(http):
    http["responses"][("POST", f"{_ROOT}/preview")] = (200, _DRAFT)
    http["responses"][("POST", _ROOT)] = (200, {"ticket_id": "tk_1", "status": "promoted"})
    res = await mcp_server._handle_promote_meeting_action_item(
        {
            "meeting_id": "meet_1",
            "item_id": "mi_1",
            "confirm": True,
            "title": "Better title",
            "priority": "high",
            "publish_to_jira": True,
        }
    )
    assert res["ticket_id"] == "tk_1" and res["confirmed"] is True
    body = http["calls"][1]["json"]
    assert body["title"] == "Better title"
    assert body["priority"] == "high"
    assert body["description"] == "d"
    assert body["assigned_user_id"] == "u_1"
    assert body["due_date"] == "2026-11-01"
    assert body["target_project_id"] == "proj_1"
    assert body["publish_to_jira"] is True


@pytest.mark.asyncio
async def test_promote_validation_and_errors(http):
    bad = await mcp_server._handle_promote_meeting_action_item(
        {"meeting_id": "meet_1", "item_id": "mi_1", "confirm": "yes"}
    )
    assert "confirm" in bad["error"]
    assert not http["calls"]
    http["responses"][("POST", f"{_ROOT}/preview")] = (409, {"detail": {"code": "item_not_pending"}})
    res = await mcp_server._handle_promote_meeting_action_item(
        {"meeting_id": "meet_1", "item_id": "mi_1", "confirm": True}
    )
    assert "item_not_pending" in res["error"]
    assert len(http["calls"]) == 1


@pytest.mark.asyncio
async def test_call_tool_dispatches_meeting_tools(http):
    http["responses"][("GET", f"{API}/api/v1/meetings/meet_1")] = (200, {"id": "meet_1"})
    out = await mcp_server.call_tool("get_meeting_transcript", {"meeting_id": "meet_1"})
    assert json.loads(out[0].text)["id"] == "meet_1"


async def _confirm(http, **args):
    http["responses"][("POST", f"{_ROOT}/preview")] = (200, dict(_DRAFT, assigned_to="atlas", assigned_user_id=None))
    http["responses"][("POST", _ROOT)] = (200, {"ticket_id": "tk_1", "status": "promoted"})
    res = await mcp_server._handle_promote_meeting_action_item(
        {"meeting_id": "meet_1", "item_id": "mi_1", "confirm": True, **args}
    )
    return res, http["calls"][-1]["json"]


@pytest.mark.asyncio
async def test_promote_persona_to_person_replaces_assignee(http):
    res, body = await _confirm(http, assigned_user_id="u_9")
    assert body["assigned_user_id"] == "u_9" and body["assigned_to"] is None
    assert res["draft_sent"] == body


@pytest.mark.asyncio
async def test_promote_person_to_persona_replaces_assignee(http):
    http["responses"][("POST", f"{_ROOT}/preview")] = (200, dict(_DRAFT))  # draft assigns u_1
    http["responses"][("POST", _ROOT)] = (200, {"ticket_id": "tk_1"})
    await mcp_server._handle_promote_meeting_action_item(
        {"meeting_id": "meet_1", "item_id": "mi_1", "confirm": True, "assigned_to": "nova"}
    )
    body = http["calls"][-1]["json"]
    assert body["assigned_to"] == "nova" and body["assigned_user_id"] is None


@pytest.mark.asyncio
async def test_promote_null_clears_assignee_due_and_keeps_others(http):
    _, body = await _confirm(http, assigned_to=None, due_date=None)
    assert body["assigned_to"] is None and body["assigned_user_id"] is None
    assert body["due_date"] is None
    assert body["title"] == "Ship it." and body["target_project_id"] == "proj_1"
    _, body = await _confirm(http, title="  ")  # blank string is still ignored
    assert body["title"] == "Ship it."
    assert body["assigned_to"] == "atlas"  # untouched assignee stays


@pytest.mark.asyncio
async def test_promote_with_jira_uses_long_timeout(http):
    await _confirm(http, publish_to_jira=True)
    assert 150 in http["timeouts"]
    http["timeouts"].clear()
    await _confirm(http)
    assert 150 not in http["timeouts"]


def test_upload_description_describes_skipped_extraction():
    tool = next(t for t in mcp_server._TOOLS if t.name == "upload_meeting_transcript")
    assert "extraction_skipped: llm_key_required" in tool.description
    assert "409" not in tool.description


@pytest.mark.asyncio
async def test_every_meeting_response_carries_the_untrusted_notice(http):
    notice = mcp_server._MEETING_UNTRUSTED_NOTICE
    http["responses"][("GET", f"{API}/api/v1/projects/proj_1/meetings")] = (200, {"meetings": []})
    http["responses"][("GET", f"{API}/api/v1/meetings/meet_1")] = (200, {"id": "meet_1", "content": "x"})
    http["responses"][("POST", f"{API}/api/v1/projects/proj_1/meetings")] = (200, {"id": "meet_1"})
    http["responses"][("POST", f"{API}/api/v1/meetings/meet_1/extract")] = (200, {"id": "meet_1"})
    http["responses"][("POST", f"{_ROOT}/preview")] = (200, dict(_DRAFT))
    results = [
        await mcp_server._handle_list_meeting_transcripts({}),
        await mcp_server._handle_search_meetings({"q": "x"}),
        await mcp_server._handle_get_meeting_transcript({"meeting_id": "meet_1"}),
        await mcp_server._handle_upload_meeting_transcript({"title": "t", "content": "c"}),
        await mcp_server._handle_extract_meeting_action_items({"meeting_id": "meet_1"}),
        await mcp_server._handle_promote_meeting_action_item({"meeting_id": "meet_1", "item_id": "mi_1"}),
    ]
    for res in results:
        assert res["untrusted_content_notice"] == notice, res
    # Existing fields are not reshaped.
    assert results[2]["content"] == "x" and results[0]["meetings"] == []
    for name in (
        "upload_meeting_transcript", "list_meeting_transcripts", "get_meeting_transcript",
        "extract_meeting_action_items", "promote_meeting_action_item", "search_meetings",
    ):
        tool = next(t for t in mcp_server._TOOLS if t.name == name)
        assert "untrusted" in tool.description, name


def test_promote_description_warns_that_confirm_creates_immediately():
    tool = next(t for t in mcp_server._TOOLS if t.name == "promote_meeting_action_item")
    assert "confirm=true creates the ticket immediately" in tool.description
    assert "WITHOUT confirm" in tool.description


@pytest.mark.asyncio
async def test_group_id_and_git_remote_together_are_rejected(http):
    for handler, args in (
        (mcp_server._handle_list_meeting_transcripts, {}),
        (mcp_server._handle_search_meetings, {"q": "x"}),
        (mcp_server._handle_upload_meeting_transcript, {"title": "t", "content": "c"}),
    ):
        res = await handler({**args, "group_id": "pg_1", "git_remote": "github.com/o/r"})
        assert "either group_id or git_remote" in res["error"]
    assert not http["calls"]
    # Either alone still works, and an empty git_remote is not "both".
    http["responses"][("GET", f"{API}/api/v1/groups/pg_1/meetings")] = (200, {"meetings": []})
    ok = await mcp_server._handle_list_meeting_transcripts({"group_id": "pg_1", "git_remote": " "})
    assert "error" not in ok


# ── Chat threads (GitHub #118, C2) ──


def test_upload_chat_thread_registered_with_kind_on_meeting_tools():
    tools = {t.name: t for t in mcp_server._TOOLS}
    chat = tools["upload_chat_thread"]
    assert set(chat.inputSchema["required"]) == {"provider", "channel_name", "content"}
    assert chat.inputSchema["properties"]["provider"]["enum"] == [
        "slack", "discord", "paste", "teams_paste",
    ]
    for name in ("list_meeting_transcripts", "search_meetings", "get_meeting_transcript",
                 "extract_meeting_action_items"):
        assert tools[name].inputSchema["properties"]["kind"]["enum"] == ["meeting", "chat", "all"]
    promote = tools["promote_meeting_action_item"].inputSchema["properties"]
    # ``kind`` stays the ticket kind; the row filter has its own name.
    assert promote["kind"]["enum"] == ["task", "issue"]
    assert promote["conversation_kind"]["enum"] == ["meeting", "chat", "all"]


_SLACK_CHANNEL = '{"channel": {"id": "C1", "name": "infra"}, "messages": []}'


@pytest.mark.asyncio
async def test_upload_chat_thread_project_and_group(http):
    url = f"{API}/api/v1/projects/proj_1/chat-threads"
    http["responses"][("POST", url)] = (200, {"id": "chat_1", "kind": "chat"})
    res = await mcp_server._handle_upload_chat_thread({
        "provider": "slack", "channel_name": " #infra ", "content": _SLACK_CHANNEL,
        "users": "[]", "title": " T ", "extract": True,
    })
    assert res["id"] == "chat_1"
    assert "untrusted_content_notice" in res
    call = http["calls"][0]
    assert call["url"] == url
    assert call["json"] == {
        "provider": "slack", "channel_name": "#infra", "content": _SLACK_CHANNEL,
        "attestation": False, "users": "[]", "title": "T", "extract": True,
    }
    await mcp_server._handle_upload_chat_thread({
        "provider": "paste", "channel_name": "#x", "content": "A: hi",
        "attestation": True, "group_id": "pg_1",
    })
    assert http["calls"][1]["url"] == f"{API}/api/v1/groups/pg_1/chat-threads"


@pytest.mark.asyncio
async def test_upload_chat_thread_validates_locally(http):
    base = {"provider": "slack", "channel_name": "#c", "content": _SLACK_CHANNEL}
    up = mcp_server._handle_upload_chat_thread
    assert "provider" in (await up({**base, "provider": "irc"}))["error"]
    assert "channel_name" in (await up({**base, "channel_name": " "}))["error"]
    assert "80" in (await up({**base, "channel_name": "#" + "x" * 80}))["error"]
    assert "content" in (await up({**base, "content": ""}))["error"]
    for provider in ("paste", "teams_paste"):
        err = (await up({**base, "provider": provider, "content": "A: hi"}))["error"]
        assert "attestation" in err
    big = "x" * (2 * 1024 * 1024 + 1)
    err = (await up({**base, "content": big}))["error"]
    assert "2 MiB" in err
    assert "attestation" in (await up({**base, "attestation": "yes"}))["error"]
    assert "users" in (await up({**base, "users": 5}))["error"]
    assert http["calls"] == []


@pytest.mark.asyncio
async def test_upload_chat_thread_surfaces_api_error(http):
    http["responses"][("POST", f"{API}/api/v1/projects/proj_1/chat-threads")] = (
        422, {"detail": {"code": "dm_not_supported"}},
    )
    res = await mcp_server._handle_upload_chat_thread(
        {"provider": "slack", "channel_name": "#c", "content": _SLACK_CHANNEL}
    )
    assert "422" in res["error"] and "dm_not_supported" in res["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["[]", '{"messages": []}', '{"channel": "C1", "messages": []}'])
async def test_upload_chat_thread_slack_without_channel_object_needs_attestation(http, content):
    up = mcp_server._handle_upload_chat_thread
    base = {"provider": "slack", "channel_name": "#c", "content": content}
    err = (await up(base))["error"]
    assert "attestation=true" in err and "channel object" in err
    assert http["calls"] == []
    http["responses"][("POST", f"{API}/api/v1/projects/proj_1/chat-threads")] = (200, {"id": "chat_1"})
    assert (await up({**base, "attestation": True}))["id"] == "chat_1"
    assert http["calls"][0]["json"]["attestation"] is True


@pytest.mark.asyncio
async def test_kind_filter_on_list_search_get_extract_promote(http):
    http["responses"][("GET", f"{API}/api/v1/projects/proj_1/meetings")] = (200, {"meetings": []})
    await mcp_server._handle_list_meeting_transcripts({"kind": "chat"})
    assert http["calls"][-1]["params"] == {"limit": "100", "kind": "chat"}
    await mcp_server._handle_search_meetings({"q": "cert", "kind": "all"})
    assert http["calls"][-1]["params"] == {"q": "cert", "limit": "25", "kind": "all"}
    await mcp_server._handle_list_meeting_transcripts({})
    assert "kind" not in http["calls"][-1]["params"]
    assert "kind" in (await mcp_server._handle_list_meeting_transcripts({"kind": "x"}))["error"]

    await mcp_server._handle_get_meeting_transcript({"meeting_id": "chat_1", "kind": "chat"})
    assert http["calls"][-1]["params"] == {"kind": "chat"}
    await mcp_server._handle_get_meeting_transcript({"meeting_id": "chat_1", "kind": "all"})
    assert http["calls"][-1]["params"] == {}
    await mcp_server._handle_extract_meeting_action_items({"meeting_id": "chat_1", "kind": "chat"})
    assert http["calls"][-1]["url"].endswith("/meetings/chat_1/extract")
    assert http["calls"][-1]["params"] == {"kind": "chat"}
    n = len(http["calls"])
    bad = await mcp_server._handle_get_meeting_transcript({"meeting_id": "chat_1", "kind": "x"})
    assert "kind" in bad["error"]
    assert len(http["calls"]) == n

    root = f"{API}/api/v1/meetings/chat_1/action-items/mi_1/promote"
    http["responses"][("POST", f"{root}/preview")] = (200, dict(_DRAFT, meeting_id="chat_1"))
    http["responses"][("POST", root)] = (200, {"ticket_id": "tk_1"})
    await mcp_server._handle_promote_meeting_action_item({
        "meeting_id": "chat_1", "item_id": "mi_1", "conversation_kind": "chat",
        "confirm": True, "kind": "issue",
    })
    preview, create = http["calls"][-2], http["calls"][-1]
    assert preview["params"] == {"kind": "chat"} and create["params"] == {"kind": "chat"}
    assert create["json"]["kind"] == "issue"
    bad = await mcp_server._handle_promote_meeting_action_item(
        {"meeting_id": "chat_1", "item_id": "mi_1", "conversation_kind": "x"}
    )
    assert "conversation_kind" in bad["error"]


@pytest.mark.asyncio
async def test_call_tool_dispatches_upload_chat_thread(http):
    http["responses"][("POST", f"{API}/api/v1/projects/proj_1/chat-threads")] = (200, {"id": "chat_1"})
    out = await mcp_server.call_tool(
        "upload_chat_thread", {"provider": "discord", "channel_name": "#c", "content": "{}"}
    )
    assert json.loads(out[0].text)["id"] == "chat_1"


_DISCORD_MSGS = [{"type": "Default", "content": "hi", "author": {"name": "tolu"}}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [
        {"messages": _DISCORD_MSGS},
        {"channel": {"type": "GuildTextChat"}, "messages": _DISCORD_MSGS},
        {"guild": {"id": "1"}, "channel": {"type": "Mystery"}, "messages": _DISCORD_MSGS},
        {"guild": {"id": "1"}, "channel": {"name": "x"}, "messages": _DISCORD_MSGS},
    ],
)
async def test_upload_chat_thread_discord_without_guild_channel_needs_attestation(http, body):
    up = mcp_server._handle_upload_chat_thread
    base = {"provider": "discord", "channel_name": "#c", "content": json.dumps(body)}
    err = (await up(base))["error"]
    assert "attestation=true" in err and "Discord" in err
    assert http["calls"] == []
    http["responses"][("POST", f"{API}/api/v1/projects/proj_1/chat-threads")] = (200, {"id": "chat_1"})
    assert (await up({**base, "attestation": True}))["id"] == "chat_1"


@pytest.mark.asyncio
@pytest.mark.parametrize("ctype", ["GuildTextChat", "GuildPublicThread", 0, 11])
async def test_upload_chat_thread_discord_guild_channel_needs_no_attestation(http, ctype):
    http["responses"][("POST", f"{API}/api/v1/projects/proj_1/chat-threads")] = (200, {"id": "chat_1"})
    content = json.dumps({"guild": {"id": "1"}, "channel": {"type": ctype}, "messages": _DISCORD_MSGS})
    out = await mcp_server._handle_upload_chat_thread(
        {"provider": "discord", "channel_name": "#c", "content": content}
    )
    assert out["id"] == "chat_1"
    assert http["calls"][0]["json"]["attestation"] is False


# ── link_meeting_action_item (0.29.11) ──

_ITEM = f"{API}/api/v1/meetings/meet_1/action-items/mi_1"
_PREVIEW = {
    "meeting_id": "meet_1",
    "item_id": "mi_1",
    "ticket": {"id": "tk_9", "project_id": "proj_1", "title": "T", "status": "open"},
    "suggested": True,
    "comment": "Raised again in meeting ...",
}


def test_link_tool_registered_and_states_preview_only():
    tool = next(t for t in mcp_server._TOOLS if t.name == "link_meeting_action_item")
    assert "PREVIEW ONLY" in tool.description
    assert set(tool.inputSchema["required"]) == {"meeting_id", "item_id"}
    getter = next(t for t in mcp_server._TOOLS if t.name == "get_meeting_transcript")
    assert "suggested_ticket" in getter.description


@pytest.mark.asyncio
async def test_link_without_confirm_only_previews(http):
    http["responses"][("POST", f"{_ITEM}/link/preview")] = (200, _PREVIEW)
    res = await mcp_server._handle_link_meeting_action_item(
        {"meeting_id": "meet_1", "item_id": "mi_1", "conversation_kind": "chat"}
    )
    assert res["confirmed"] is False and res["preview"]["ticket"]["id"] == "tk_9"
    assert [c["url"] for c in http["calls"]] == [f"{_ITEM}/link/preview"]
    assert http["calls"][0]["json"] is None
    assert http["calls"][0]["params"] == {"kind": "chat"}
    assert "untrusted_content_notice" in res


@pytest.mark.asyncio
async def test_link_confirm_pins_the_previewed_ticket(http):
    http["responses"][("POST", f"{_ITEM}/link/preview")] = (200, _PREVIEW)
    http["responses"][("POST", f"{_ITEM}/link")] = (200, {"ticket_id": "tk_9", "status": "linked"})
    res = await mcp_server._handle_link_meeting_action_item(
        {"meeting_id": "meet_1", "item_id": "mi_1", "ticket_id": " tk_9 ", "confirm": True}
    )
    assert res["confirmed"] is True and res["status"] == "linked"
    assert http["calls"][0]["json"] == {"ticket_id": "tk_9"}
    assert http["calls"][1]["url"] == f"{_ITEM}/link"
    assert http["calls"][1]["json"] == {"ticket_id": "tk_9"}


@pytest.mark.asyncio
async def test_link_unlink_and_validation(http):
    noop = await mcp_server._handle_link_meeting_action_item(
        {"meeting_id": "meet_1", "item_id": "mi_1", "unlink": True}
    )
    assert noop["confirmed"] is False and http["calls"] == []
    http["responses"][("POST", f"{_ITEM}/unlink")] = (200, {"id": "mi_1", "status": "pending"})
    out = await mcp_server._handle_link_meeting_action_item(
        {"meeting_id": "meet_1", "item_id": "mi_1", "unlink": True, "confirm": True}
    )
    assert out["unlinked"] is True and out["item"]["status"] == "pending"

    for bad in (
        {"item_id": "mi_1"},
        {"meeting_id": "meet_1"},
        {"meeting_id": "meet_1", "item_id": "mi_1", "confirm": "yes"},
        {"meeting_id": "meet_1", "item_id": "mi_1", "ticket_id": ""},
        {"meeting_id": "meet_1", "item_id": "mi_1", "conversation_kind": "video"},
    ):
        assert "error" in await mcp_server._handle_link_meeting_action_item(bad)

    http["responses"][("POST", f"{_ITEM}/link/preview")] = (
        422, {"error": {"code": "ticket_not_linkable"}}
    )
    err = await mcp_server._handle_link_meeting_action_item(
        {"meeting_id": "meet_1", "item_id": "mi_1", "ticket_id": "tk_x", "confirm": True}
    )
    assert "422" in err["error"]
    assert not any(c["url"] == f"{_ITEM}/link" for c in http["calls"])


@pytest.mark.asyncio
async def test_link_confirm_requires_the_previewed_ticket_id(http):
    tool = next(t for t in mcp_server._TOOLS if t.name == "link_meeting_action_item")
    assert "REQUIRES ticket_id" in tool.description
    # confirm without ticket_id: refused locally, no request at all.
    res = await mcp_server._handle_link_meeting_action_item(
        {"meeting_id": "meet_1", "item_id": "mi_1", "confirm": True}
    )
    assert "error" in res and "ticket_id" in res["error"]
    assert http["calls"] == []

    # The preview resolves to a different ticket than the one named: no link.
    http["responses"][("POST", f"{_ITEM}/link/preview")] = (200, _PREVIEW)
    res = await mcp_server._handle_link_meeting_action_item(
        {"meeting_id": "meet_1", "item_id": "mi_1", "ticket_id": "tk_other", "confirm": True}
    )
    assert res["confirmed"] is False and "does not match" in res["error"]
    assert res["preview"]["ticket"]["id"] == "tk_9"
    assert not any(c["url"] == f"{_ITEM}/link" for c in http["calls"])
