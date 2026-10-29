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
