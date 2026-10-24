"""SSRF policy for user-supplied LLM endpoints (judge / summaries / knowledge)."""

from __future__ import annotations

import socket

import httpx
import pytest

from sessionfs.judge import providers
from sessionfs.server.llm_url_guard import (
    SsrfError,
    guard_llm_base_url,
    validate_llm_base_url,
)

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
def sent(monkeypatch):
    calls: list[httpx.Request] = []
    kwargs: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/redirect/chat/completions"):
            return httpx.Response(302, headers={"Location": "http://169.254.169.254/latest/meta-data/"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}], "data": [{"id": "m1"}]})

    real = httpx.AsyncClient

    def factory(*a, **k):
        kwargs.append(dict(k))
        k["transport"] = httpx.MockTransport(handler)
        return real(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    providers.set_base_url_guard(guard_llm_base_url)
    yield calls, kwargs
    providers.set_base_url_guard(None)


def test_public_https_ok():
    assert validate_llm_base_url("https://llm.example.com/v1/") == "https://llm.example.com/v1"


@pytest.mark.parametrize(
    "url",
    [
        "http://llm.example.com/v1",  # plain http in hosted mode
        "https://169.254.169.254/v1",
        "https://10.0.0.5/v1",
        "https://127.0.0.1:11434/v1",
        "https://100.64.1.1/v1",
        "https://0.0.0.0/v1",
        "https://[::1]/v1",
        "https://[::ffff:10.0.0.1]/v1",
        "https://[::ffff:169.254.169.254]/v1",
        "https://user:pw@llm.example.com/v1",
        "file:///etc/passwd",
        "",
    ],
)
def test_hosted_mode_refuses(url):
    with pytest.raises(SsrfError):
        validate_llm_base_url(url)


def test_hostname_resolving_private_refused():
    _DNS["llm.example.com"] = "192.168.1.10"
    with pytest.raises(SsrfError):
        validate_llm_base_url("https://llm.example.com/v1")


@pytest.mark.parametrize(
    "url",
    ["http://10.0.0.5:11434/v1", "http://127.0.0.1:8000/v1", "http://ollama.svc:11434"],
)
def test_private_allowed_with_flag(monkeypatch, url):
    _DNS["ollama.svc"] = "10.1.2.3"
    monkeypatch.setenv("SFS_LLM_ALLOW_PRIVATE_HOSTS", "true")
    assert validate_llm_base_url(url) == url.rstrip("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/v1",
        "http://[::ffff:169.254.169.254]/v1",
        "http://[fd00:ec2::254]/v1",
        "http://169.254.10.10/v1",
        "http://0.0.0.0/v1",
        "http://meta.example/v1",
        "gopher://10.0.0.5/v1",
    ],
)
def test_metadata_refused_even_with_flag(monkeypatch, url):
    _DNS["meta.example"] = "169.254.169.254"
    monkeypatch.setenv("SFS_LLM_ALLOW_PRIVATE_HOSTS", "true")
    with pytest.raises(SsrfError):
        validate_llm_base_url(url)


def test_operator_judge_base_url_may_be_private(monkeypatch):
    monkeypatch.setenv("SFS_JUDGE_BASE_URL", "http://10.0.0.9:4000/v1/")
    assert validate_llm_base_url("http://10.0.0.9:4000/v1") == "http://10.0.0.9:4000/v1"
    with pytest.raises(SsrfError):
        validate_llm_base_url("http://10.0.0.8:4000/v1")


async def test_call_llm_public_sends(sent):
    calls, kwargs = sent
    out = await providers.call_llm("m", "s", "p", "k", base_url="https://llm.example.com/v1")
    assert out == "ok"
    assert str(calls[0].url) == "https://llm.example.com/v1/chat/completions"
    assert kwargs[0].get("follow_redirects") is False


async def test_dns_rebinding_after_save_refused_at_call_time(sent):
    calls, _ = sent
    validate_llm_base_url("https://llm.example.com/v1")  # save-time check passes
    _DNS["llm.example.com"] = "169.254.169.254"  # record flips afterwards
    with pytest.raises(SsrfError):
        await providers.call_llm("m", "s", "p", "k", base_url="https://llm.example.com/v1")
    assert await providers.discover_models("https://llm.example.com/v1", "k") == []
    assert await providers.validate_base_url("https://llm.example.com/v1", "k", "m") is False
    assert calls == []  # the key never left


async def test_redirects_not_followed(sent):
    calls, _ = sent
    with pytest.raises(httpx.HTTPStatusError):
        await providers.call_llm("m", "s", "p", "k", base_url="https://llm.example.com/redirect")
    assert len(calls) == 1


async def test_no_guard_without_server(monkeypatch, sent):
    calls, _ = sent
    providers.set_base_url_guard(None)  # CLI: a local Ollama keeps working
    assert await providers.call_llm("m", "s", "p", "", base_url="http://127.0.0.1:11434/v1") == "ok"
    assert len(calls) == 1
