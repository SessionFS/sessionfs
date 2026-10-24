"""GitLabClient re-checks the instance host before every outbound request."""

from __future__ import annotations

import socket

import httpx
import pytest

from sessionfs.server.gitlab_client import (
    GitLabClient,
    SsrfError,
    validate_gitlab_instance_url,
)

_DNS: dict[str, str] = {}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    _DNS.clear()
    monkeypatch.delenv("SFS_GITLAB_ALLOW_PRIVATE_HOSTS", raising=False)

    def fake(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (_DNS.get(host, host if host[0].isdigit() or ":" in host else "93.184.216.34"), 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.fixture
def sent(monkeypatch):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json={"id": 1})

    real = httpx.AsyncClient

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return calls


async def test_public_host_sends(sent):
    client = GitLabClient("https://gitlab.example.com", "tok")
    assert (await client.post_mr_comment(1, 2, "hi")) == {"id": 1}
    assert sent[0].headers["PRIVATE-TOKEN"] == "tok"


async def test_dns_rebinding_after_save_is_refused(sent):
    client = GitLabClient("https://gitlab.example.com", "tok")
    validate_gitlab_instance_url("https://gitlab.example.com")  # PUT-time check passes
    _DNS["gitlab.example.com"] = "169.254.169.254"  # record flips afterwards
    with pytest.raises(SsrfError):
        await client.post_mr_comment(1, 2, "hi")
    with pytest.raises(SsrfError):
        await client.update_mr_comment(1, 2, 3, "hi")
    assert await client.test_connection() is False
    assert sent == []  # the token never left the process


async def test_rebinding_to_private_refused_in_hosted_mode(sent):
    client = GitLabClient("https://gitlab.example.com", "tok")
    _DNS["gitlab.example.com"] = "10.0.0.5"
    with pytest.raises(SsrfError):
        await client.post_mr_comment(1, 2, "x")
    assert sent == []


async def test_self_hosted_allows_private_but_not_metadata(sent, monkeypatch):
    monkeypatch.setenv("SFS_GITLAB_ALLOW_PRIVATE_HOSTS", "true")
    _DNS["gitlab.corp"] = "10.0.0.5"
    await GitLabClient("http://gitlab.corp", "tok").post_mr_comment(1, 2, "x")
    assert len(sent) == 1
    _DNS["gitlab.corp"] = "169.254.169.254"
    with pytest.raises(SsrfError):
        await GitLabClient("http://gitlab.corp", "tok").post_mr_comment(1, 2, "x")
    assert len(sent) == 1


async def test_http_refused_in_hosted_mode(sent):
    with pytest.raises(SsrfError):
        await GitLabClient("http://gitlab.example.com", "tok").post_mr_comment(1, 2, "x")
    assert sent == []


@pytest.mark.parametrize("addr", ["::ffff:169.254.169.254", "::ffff:127.0.0.1", "0.0.0.0", "100.64.0.1"])
def test_hosted_mode_refuses_wrapped_and_reserved_addresses(addr):
    _DNS["gitlab.example.com"] = addr
    with pytest.raises(SsrfError):
        validate_gitlab_instance_url("https://gitlab.example.com")


@pytest.mark.parametrize("addr", ["::ffff:169.254.169.254", "0.0.0.0"])
def test_self_hosted_mode_still_refuses_wrapped_metadata(addr):
    _DNS["gitlab.internal"] = addr
    with pytest.raises(SsrfError):
        validate_gitlab_instance_url("http://gitlab.internal", allow_private=True)


def test_self_hosted_mode_allows_wrapped_private_address():
    _DNS["gitlab.internal"] = "::ffff:10.0.0.5"
    assert validate_gitlab_instance_url("http://gitlab.internal", allow_private=True) == "http://gitlab.internal"
