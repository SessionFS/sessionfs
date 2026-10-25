"""Judge settings / model discovery refuse internal LLM endpoints."""

from __future__ import annotations

import socket

import httpx
import pytest

_DNS: dict[str, str] = {}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    _DNS.clear()
    monkeypatch.delenv("SFS_LLM_ALLOW_PRIVATE_HOSTS", raising=False)
    monkeypatch.delenv("SFS_JUDGE_BASE_URL", raising=False)

    def fake(host, port, *a, **k):
        default = host if host[0].isdigit() or ":" in host else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_DNS.get(host, default), 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.fixture
def upstream(monkeypatch, client):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if "redir" in request.url.host:
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
        return httpx.Response(200, json={"data": [{"id": "llama3", "owned_by": "me"}]})

    real = httpx.AsyncClient

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return calls


async def test_put_judge_refuses_metadata_and_private(client, auth_headers):
    for url in ("http://169.254.169.254/v1", "https://10.0.0.5/v1", "https://[::ffff:127.0.0.1]/v1"):
        r = await client.put(
            "/api/v1/settings/judge",
            json={"provider": "openai", "model": "m", "api_key": "k", "base_url": url},
            headers=auth_headers,
        )
        assert r.status_code == 400, url
        assert "10.0.0.5" not in r.text and "127.0.0.1" not in r.text


async def test_put_judge_public_ok(client, auth_headers):
    r = await client.put(
        "/api/v1/settings/judge",
        json={"provider": "openai", "model": "m", "api_key": "k", "base_url": "https://llm.example.com/v1/"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["base_url"] == "https://llm.example.com/v1"


async def test_put_judge_private_allowed_with_flag(client, auth_headers, monkeypatch):
    monkeypatch.setenv("SFS_LLM_ALLOW_PRIVATE_HOSTS", "true")
    r = await client.put(
        "/api/v1/settings/judge",
        json={"provider": "openai", "model": "m", "api_key": "k", "base_url": "http://10.0.0.5:11434/v1"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    r = await client.put(
        "/api/v1/settings/judge",
        json={"provider": "openai", "model": "m", "api_key": "k", "base_url": "http://169.254.169.254/v1"},
        headers=auth_headers,
    )
    assert r.status_code == 400


async def test_discovery_get_refuses_query_api_key(client, auth_headers, upstream):
    r = await client.get(
        "/api/v1/settings/judge/models",
        params={"base_url": "https://llm.example.com/v1", "api_key": "sk-secret"},
        headers=auth_headers,
    )
    assert r.status_code == 400
    assert upstream == []


async def test_discovery_post_public(client, auth_headers, upstream):
    r = await client.post(
        "/api/v1/settings/judge/models",
        json={"base_url": "https://llm.example.com/v1", "api_key": "sk-x"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["models"] == [{"id": "llama3", "owned_by": "me"}]
    assert upstream[0].headers["Authorization"] == "Bearer sk-x"


async def test_discovery_get_uses_saved_key_for_its_own_endpoint(client, auth_headers, upstream):
    await client.put(
        "/api/v1/settings/judge",
        json={
            "provider": "openai",
            "model": "m",
            "api_key": "sk-saved",
            "base_url": "https://llm.example.com/v1",
        },
        headers=auth_headers,
    )
    r = await client.get(
        "/api/v1/settings/judge/models",
        params={"base_url": "https://llm.example.com/v1"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert upstream[0].headers["Authorization"] == "Bearer sk-saved"


async def test_discovery_never_sends_saved_key_to_another_endpoint(client, auth_headers, upstream):
    """A saved key is write-only: listing models at a different URL must not
    carry it there (that would hand the key to whoever runs that host)."""
    await client.put(
        "/api/v1/settings/judge",
        json={"provider": "openai", "model": "m", "api_key": "sk-saved", "base_url": None},
        headers=auth_headers,
    )
    r = await client.get(
        "/api/v1/settings/judge/models",
        params={"base_url": "https://llm.example.com/v1"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert "Authorization" not in upstream[0].headers


@pytest.mark.parametrize(
    "url",
    ["http://169.254.169.254", "https://10.0.0.5", "https://[::ffff:10.0.0.5]", "https://internal.example"],
)
async def test_discovery_refuses_internal(client, auth_headers, upstream, url):
    _DNS["internal.example"] = "172.16.0.4"
    r = await client.post(
        "/api/v1/settings/judge/models", json={"base_url": url}, headers=auth_headers
    )
    assert r.status_code == 400
    assert "172.16" not in r.text
    assert upstream == []


async def test_discovery_no_redirects_and_no_error_echo(client, auth_headers, upstream):
    r = await client.post(
        "/api/v1/settings/judge/models", json={"base_url": "https://redir.example.com"}, headers=auth_headers
    )
    assert r.status_code == 200
    assert r.json()["models"] == []
    assert len(upstream) == 1  # the redirect was not followed

