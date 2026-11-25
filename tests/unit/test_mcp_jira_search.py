"""GitHub #144 — the search_jira_issues MCP tool wraps the org search route."""

from __future__ import annotations

import json

import pytest

from sessionfs.mcp import server as mcp_server


def _install(monkeypatch, calls: list, status: int = 200, body=None):
    body = body if body is not None else {"issues": [], "count": 0}

    async def _cfg(args):
        return ("https://api.test", "k", args.get("org_id") or "org_default")

    monkeypatch.setattr(mcp_server, "_group_config", _cfg)

    class _Resp:
        def __init__(self):
            self.status_code = status
            self.text = json.dumps(body)

        def json(self):
            return body

    class _Client:
        def __init__(self, timeout=None):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, headers=None, json=None):
            calls.append((url, headers, json))
            return _Resp()

    monkeypatch.setattr(mcp_server, "idempotent_async_client", _Client)


def test_tool_is_registered():
    names = [t.name for t in mcp_server._TOOLS]
    assert "search_jira_issues" in names
    tool = next(t for t in mcp_server._TOOLS if t.name == "search_jira_issues")
    assert tool.inputSchema["required"] == ["jql"]


@pytest.mark.asyncio
async def test_posts_to_the_org_search_route(monkeypatch):
    calls: list = []
    _install(monkeypatch, calls, body={"issues": [{"key": "ACME-1"}], "count": 1})
    out = await mcp_server._handle_search_jira_issues(
        {"jql": "assignee = Kelvin", "max_results": 99, "fields": ["summary"], "org_id": "org_x"}
    )
    assert out["issues"][0]["key"] == "ACME-1"
    url, headers, body = calls[-1]
    assert url == "https://api.test/api/v1/orgs/org_x/integrations/jira/search"
    assert headers == {"Authorization": "Bearer k"}
    assert body == {"jql": "assignee = Kelvin", "max_results": 50, "fields": ["summary"]}


@pytest.mark.asyncio
async def test_bad_args_and_errors(monkeypatch):
    calls: list = []
    _install(
        monkeypatch,
        calls,
        status=422,
        body={"error": {"code": "invalid_jql", "message": "jql has unbalanced parentheses"}},
    )
    assert "error" in await mcp_server._handle_search_jira_issues({"jql": 5})
    assert "error" in await mcp_server._handle_search_jira_issues({"jql": "", "max_results": "x"})
    assert "error" in await mcp_server._handle_search_jira_issues({"jql": "", "fields": "summary"})
    assert not calls
    out = await mcp_server._handle_search_jira_issues({"jql": ") OR x"})
    assert out == {"error": "API error 422: jql has unbalanced parentheses"}
