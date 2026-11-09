"""Client-side Idempotency-Key stamping for the CLI and MCP server
(tk_bd80efd511da4a2b)."""

from __future__ import annotations

import asyncio
import inspect
import uuid
from pathlib import Path

import httpx
import pytest

from sessionfs import http_idempotency
from sessionfs.http_idempotency import (
    IDEMPOTENCY_HEADER,
    idempotency_headers,
    idempotent_async_client,
    idempotent_client,
)

_SRC = Path(__file__).resolve().parents[2] / "src" / "sessionfs"


def _recorder() -> tuple[list[httpx.Request], httpx.MockTransport]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    return seen, httpx.MockTransport(handler)


def test_idempotency_headers_only_for_post() -> None:
    base = {"Authorization": "Bearer x"}
    post = idempotency_headers("post", base)
    assert uuid.UUID(post[IDEMPOTENCY_HEADER]).version == 4
    assert base == {"Authorization": "Bearer x"}  # caller's dict untouched
    assert IDEMPOTENCY_HEADER not in idempotency_headers("GET", base)
    assert idempotency_headers("POST", {"idempotency-key": "mine"}) == {"idempotency-key": "mine"}
    assert idempotency_headers("POST")[IDEMPOTENCY_HEADER] != idempotency_headers("POST")[IDEMPOTENCY_HEADER]


def test_async_client_stamps_every_post_with_a_fresh_key() -> None:
    seen, transport = _recorder()

    async def go() -> None:
        async with idempotent_async_client(transport=transport) as client:
            await client.post("http://api/x", json={"a": 1})
            await client.post("http://api/x", json={"a": 1})
            await client.get("http://api/x")
            await client.put("http://api/x", json={})
            await client.post("http://api/x", headers={IDEMPOTENCY_HEADER: "kept"})

    asyncio.run(go())
    keys = [r.headers.get(IDEMPOTENCY_HEADER) for r in seen]
    assert keys[0] and keys[1] and keys[0] != keys[1]
    assert keys[2] is None and keys[3] is None
    assert keys[4] == "kept"


def test_sync_client_and_post_helper_stamp_posts(monkeypatch: pytest.MonkeyPatch) -> None:
    seen, transport = _recorder()
    with idempotent_client(transport=transport) as client:
        client.post("http://api/x")
        client.request("POST", "http://api/x")
        client.get("http://api/x")
    assert [bool(r.headers.get(IDEMPOTENCY_HEADER)) for r in seen] == [True, True, False]

    captured: dict = {}

    def fake_post(url, **kwargs):
        captured.update(kwargs)
        return httpx.Response(200)

    monkeypatch.setattr(httpx, "post", fake_post)
    http_idempotency.idempotent_post("http://api/x", headers={"Authorization": "Bearer y"})
    assert captured["headers"]["Authorization"] == "Bearer y"
    assert captured["headers"][IDEMPOTENCY_HEADER]


def test_stand_in_client_without_hooks_still_works(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Fake:
        def __init__(self, *args, **kwargs) -> None:
            self.kwargs = kwargs

    monkeypatch.setattr(httpx, "AsyncClient", _Fake)
    client = idempotent_async_client(timeout=5)
    assert isinstance(client, _Fake) and client.kwargs == {"timeout": 5}


def _patch_transport(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    real = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = transport
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def test_cli_shared_api_request_sends_a_key_on_post(monkeypatch: pytest.MonkeyPatch) -> None:
    from sessionfs.cli import cmd_project, cmd_rules

    seen, transport = _recorder()
    _patch_transport(monkeypatch, transport)
    asyncio.run(cmd_rules._api_request("POST", "/api/v1/x", "http://api", "k", json_data={}))
    asyncio.run(cmd_rules._api_request("GET", "/api/v1/x", "http://api", "k"))
    asyncio.run(cmd_project._api_request("POST", "/api/v1/y", "http://api", "k", json_data={}))
    assert seen[0].headers.get(IDEMPOTENCY_HEADER)
    assert seen[1].headers.get(IDEMPOTENCY_HEADER) is None
    assert seen[2].headers.get(IDEMPOTENCY_HEADER)
    assert seen[0].headers["Authorization"] == "Bearer k"


def test_mcp_server_and_cli_helpers_use_the_stamping_client() -> None:
    from sessionfs.mcp import cloud_client, server

    for module in (server, cloud_client):
        source = inspect.getsource(module)
        assert "httpx.AsyncClient(" not in source, module.__name__
        assert "idempotent_async_client(" in source, module.__name__
    for rel in (
        "cli/cmd_rules.py",
        "cli/cmd_project.py",
        "cli/cmd_project_group.py",
        "cli/cmd_policy.py",
        "cli/cmd_catalog.py",
        "cli/cmd_skill_publisher.py",
        "cli/cmd_org.py",
        "cli/cmd_cloud.py",
        "cli/cmd_delete.py",
    ):
        text = (_SRC / rel).read_text(encoding="utf-8")
        assert "httpx.post(" not in text, rel
        assert "httpx.AsyncClient(" not in text and "httpx.Client(" not in text, rel
