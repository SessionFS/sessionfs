"""The saved judge key only goes to the provider/endpoint it was saved for.

Narrative and audit requests that omit ``llm_api_key`` fall back to the
caller's saved judge key. Overriding ``base_url`` (or, for a key saved without
a custom endpoint, ``provider``) must not carry that stored key to the
caller-chosen host.
"""

from __future__ import annotations

import io
import socket
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from sessionfs.server.routes.settings import saved_judge_key_matches


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    monkeypatch.delenv("SFS_LLM_ALLOW_PRIVATE_HOSTS", raising=False)
    monkeypatch.delenv("SFS_JUDGE_BASE_URL", raising=False)

    def fake(host, port, *a, **k):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


@pytest.fixture
async def pushed_session(client, auth_headers, sample_sfs_tar) -> str:
    session_id = f"ses_keybind{uuid.uuid4().hex[:8]}"
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=auth_headers,
        files={"file": ("session.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert resp.status_code == 201
    return session_id


async def _save_key(client, auth_headers, *, provider="openai", base_url=None):
    r = await client.put(
        "/api/v1/settings/judge",
        json={"provider": provider, "model": "m", "api_key": "sk-saved", "base_url": base_url},
        headers=auth_headers,
    )
    assert r.status_code == 200


class _Row:
    def __init__(self, provider, base_url):
        self.provider = provider
        self.base_url = base_url


def test_matcher_rules():
    registry = _Row("openai", None)
    assert saved_judge_key_matches(registry, provider=None, base_url=None)
    assert saved_judge_key_matches(registry, provider="OpenAI", base_url=None)
    assert not saved_judge_key_matches(registry, provider="deepseek", base_url=None)
    assert not saved_judge_key_matches(registry, provider=None, base_url="https://evil.example/v1")

    custom = _Row("openai", "https://llm.example.com/v1")
    assert saved_judge_key_matches(custom, provider=None, base_url=None)
    assert saved_judge_key_matches(custom, provider="anything", base_url=None)
    assert saved_judge_key_matches(custom, provider=None, base_url="https://llm.example.com/v1/")
    assert not saved_judge_key_matches(custom, provider=None, base_url="https://evil.example/v1")


async def test_narrative_never_sends_saved_key_to_override_endpoint(
    client, auth_headers, pushed_session
):
    await _save_key(client, auth_headers)
    with patch("sessionfs.judge.providers.call_llm", new_callable=AsyncMock) as llm:
        r = await client.post(
            f"/api/v1/sessions/{pushed_session}/summary/narrative",
            json={"base_url": "https://evil.example.com/v1"},
            headers=auth_headers,
        )
    assert r.status_code == 400
    llm.assert_not_called()


async def test_narrative_never_sends_saved_key_to_other_provider(
    client, auth_headers, pushed_session
):
    await _save_key(client, auth_headers)
    with patch("sessionfs.judge.providers.call_llm", new_callable=AsyncMock) as llm:
        r = await client.post(
            f"/api/v1/sessions/{pushed_session}/summary/narrative",
            json={"provider": "deepseek"},
            headers=auth_headers,
        )
    assert r.status_code == 400
    llm.assert_not_called()


async def test_narrative_uses_saved_key_for_its_own_provider(
    client, auth_headers, pushed_session
):
    await _save_key(client, auth_headers)
    with patch(
        "sessionfs.judge.providers.call_llm",
        new_callable=AsyncMock,
        return_value='{"what_happened": "x"}',
    ) as llm:
        r = await client.post(
            f"/api/v1/sessions/{pushed_session}/summary/narrative",
            json={"provider": "openai"},
            headers=auth_headers,
        )
    assert r.status_code == 200
    assert llm.await_args.kwargs["api_key"] == "sk-saved"


async def test_audit_never_sends_saved_key_to_override_endpoint(
    client, auth_headers, pushed_session
):
    await _save_key(client, auth_headers)
    seen: dict = {}

    async def fake_pipeline(session_id, messages, model, llm_api_key, provider, base_url=None):
        seen["key"] = llm_api_key
        seen["base_url"] = base_url
        raise ValueError("stop")

    with patch("sessionfs.server.routes.audit._run_judge_pipeline", side_effect=fake_pipeline):
        await client.post(
            f"/api/v1/sessions/{pushed_session}/audit",
            json={"model": "m", "base_url": "https://evil.example.com/v1"},
            headers=auth_headers,
        )
    assert seen.get("base_url") == "https://evil.example.com/v1"
    assert not seen.get("key")


async def test_audit_uses_saved_key_for_its_own_endpoint(
    client, auth_headers, pushed_session
):
    await _save_key(client, auth_headers, base_url="https://llm.example.com/v1")
    seen: dict = {}

    async def fake_pipeline(session_id, messages, model, llm_api_key, provider, base_url=None):
        seen["key"] = llm_api_key
        raise ValueError("stop")

    with patch("sessionfs.server.routes.audit._run_judge_pipeline", side_effect=fake_pipeline):
        await client.post(
            f"/api/v1/sessions/{pushed_session}/audit",
            json={"model": "m", "base_url": "https://llm.example.com/v1/"},
            headers=auth_headers,
        )
    assert seen.get("key") == "sk-saved"


async def test_audit_failure_other_than_http_is_a_generic_502(
    client, auth_headers, pushed_session
):
    """When every judge call fails with something other than an HTTP status
    (e.g. a connection error to a custom endpoint), the route answers one
    generic 502 instead of an unhandled 500, and never the exception text."""

    async def fake_pipeline(*a, **k):
        raise RuntimeError("connect to 10.0.0.5:443 refused (internal detail)")

    with patch("sessionfs.server.routes.audit._run_judge_pipeline", side_effect=fake_pipeline):
        r = await client.post(
            f"/api/v1/sessions/{pushed_session}/audit",
            json={"model": "m", "provider": "openai", "llm_api_key": "sk-caller-key-123456"},
            headers=auth_headers,
        )
    assert r.status_code == 502, r.text
    assert r.json()["error"] == "llm_request_failed"
    assert "10.0.0.5" not in r.text
    assert "refused" not in r.text


async def test_audit_upstream_error_detail_never_echoes_the_key(
    client, auth_headers, pushed_session
):
    """The upstream error body is passed on only after the caller's key, any
    8+ character fragment of it, and recognised secrets are removed."""
    import httpx

    key = "sk-proj-ABCDEFGHIJKLMNOPQRSTUVWXYZ012345"
    body = (
        '{"error": {"message": "Incorrect API key provided: '
        + key[:12] + "*****" + key[-6:]
        + '. Also seen: ' + key + '"}}'
    )

    async def fake_pipeline(*a, **k):
        request = httpx.Request("POST", "https://api.openai.com/v1/chat/completions")
        response = httpx.Response(401, request=request, text=body)
        raise httpx.HTTPStatusError("401", request=request, response=response)

    with patch("sessionfs.server.routes.audit._run_judge_pipeline", side_effect=fake_pipeline):
        r = await client.post(
            f"/api/v1/sessions/{pushed_session}/audit",
            json={"model": "m", "provider": "openai", "llm_api_key": key},
            headers=auth_headers,
        )
    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    assert "Incorrect API key provided" in detail
    assert key not in r.text
    assert key[:12] not in r.text
