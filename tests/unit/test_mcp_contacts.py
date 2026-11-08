"""GitHub #125 — the four external-contact MCP tools wrap the org routes."""

from __future__ import annotations

import json

import pytest

from sessionfs.mcp import server as mcp_server


def _install(monkeypatch, calls: list, status: int = 200, body=None):
    body = body if body is not None else {"id": "ec_aaaaaaaaaaaa"}

    async def _cfg(args):
        return ("https://api.test", "k", args.get("org_id") or "org_default")

    monkeypatch.setattr(mcp_server, "_group_config", _cfg)

    class _Resp:
        def __init__(self):
            self.status_code = status
            self.text = json.dumps(body)

        def json(self):
            return body

    async def _request(method, url, api_key, **kwargs):
        calls.append((method, url, kwargs))
        return _Resp()

    monkeypatch.setattr(mcp_server, "_contact_request", _request)


@pytest.mark.asyncio
async def test_list_create_get_update(monkeypatch):
    calls: list = []
    _install(monkeypatch, calls)
    await mcp_server._handle_list_external_contacts({"q": " tolu ", "limit": 5})
    assert calls[-1] == (
        "GET",
        "https://api.test/api/v1/orgs/org_default/contacts",
        {"params": {"q": "tolu", "limit": 5}},
    )
    await mcp_server._handle_create_external_contact(
        {"org_id": "org_x", "display_name": " Tolu ", "aliases": ["T"]}
    )
    assert calls[-1][0] == "POST"
    assert calls[-1][1] == "https://api.test/api/v1/orgs/org_x/contacts"
    assert calls[-1][2]["json"] == {"display_name": "Tolu", "aliases": ["T"]}
    out = await mcp_server._handle_get_external_contact({"contact_id": "ec_aaaaaaaaaaaa"})
    assert calls[-1][1].endswith("/contacts/ec_aaaaaaaaaaaa/references")
    assert "untrusted_content_notice" in out
    calls.clear()
    await mcp_server._handle_update_external_contact(
        {
            "contact_id": "ec_aaaaaaaaaaaa",
            "role": None,
            "add_aliases": ["Tee"],
            "remove_aliases": ["T"],
        }
    )
    assert [c[0] for c in calls] == ["PATCH", "POST", "DELETE"]
    assert calls[0][2]["json"] == {"role": None}


@pytest.mark.asyncio
async def test_bad_ids_and_404(monkeypatch):
    calls: list = []
    _install(monkeypatch, calls, status=404, body={"detail": "Contact not found"})
    assert await mcp_server._handle_get_external_contact({"contact_id": "../x"}) == {
        "error": "Contact not found"
    }
    assert not calls
    assert await mcp_server._handle_get_external_contact(
        {"contact_id": "ec_bbbbbbbbbbbb"}
    ) == {"error": "Contact not found"}
    assert "error" in await mcp_server._handle_create_external_contact({})
    assert "error" in await mcp_server._handle_update_external_contact(
        {"contact_id": "ec_bbbbbbbbbbbb"}
    )


@pytest.mark.asyncio
async def test_ticket_tools_forward_assigned_contact(monkeypatch):
    sent: dict = {}

    async def _resolve(git_remote=""):
        return ("https://api.test", "k", "proj_1")

    monkeypatch.setattr(mcp_server, "_resolve_project_id", _resolve)
    import httpx

    class _Resp:
        status_code = 200
        text = "{}"

        def json(self):
            return {}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, *, headers=None, json=None):
            sent["post"] = json
            return _Resp()

        async def put(self, url, *, headers=None, json=None):
            sent["put"] = json
            return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    await mcp_server._handle_create_ticket({"title": "t", "assigned_contact": "ec_aaaaaaaaaaaa"})
    assert sent["post"]["assigned_contact_id"] == "ec_aaaaaaaaaaaa"
    await mcp_server._handle_update_ticket({"ticket_id": "tk_1", "assigned_contact": None})
    assert sent["put"] == {"assigned_contact_id": None}
