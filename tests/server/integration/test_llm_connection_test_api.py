"""POST /settings/judge/test, POST /orgs/{org}/llm-settings/test and the
save-time Base URL check (tk_e6880f06870541bf).

The upstream model endpoint is mocked at ``agent_stream._make_client``;
DNS is faked so the SSRF guard sees public addresses.
"""

from __future__ import annotations

import json
import socket

import httpx
import pytest
from unittest.mock import patch

from tests.server.integration.test_org_llm_settings import (
    _hdrs,
    _make_org_with,
    _make_user,
)

SAVED = "sk-saved-key-123456"
TYPED = "sk-typed-key-987654"
OK_SSE = (
    b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
    b"data: [DONE]\n\n"
)


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    monkeypatch.delenv("SFS_LLM_ALLOW_PRIVATE_HOSTS", raising=False)
    monkeypatch.delenv("SFS_JUDGE_BASE_URL", raising=False)

    def fake(host, port, *a, **k):
        ip = host if host[0].isdigit() else "93.184.216.34"
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]

    monkeypatch.setattr(socket, "getaddrinfo", fake)


class Upstream:
    """Records requests; replies per host (default: a successful stream)."""

    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.replies: dict[str, httpx.Response] = {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        reply = self.replies.get(request.url.host)
        if reply is not None:
            return httpx.Response(
                reply.status_code, content=reply.content, headers=reply.headers
            )
        return httpx.Response(200, content=OK_SSE, headers={"content-type": "text/event-stream"})

    def auth(self, i: int = -1) -> str:
        return self.calls[i].headers.get("authorization", "")


@pytest.fixture
def upstream():
    up = Upstream()

    def factory(timeout):
        return httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(up.handler))

    with patch("sessionfs.judge.agent_stream._make_client", side_effect=factory):
        yield up


CONSOLE_405 = httpx.Response(
    405, content=b'{"detail":"Method Not Allowed"}', headers={"content-type": "application/json"}
)


async def _save_personal(client, headers, **body):
    payload = {"provider": "deepseek", "model": "deepseek-flash", "api_key": SAVED}
    payload.update(body)
    r = await client.put("/api/v1/settings/judge", json=payload, headers=headers)
    assert r.status_code == 200, r.text
    return r


# ---------------------------------------------------------------------------
# Personal test endpoint
# ---------------------------------------------------------------------------


async def test_typed_key_tests_exactly_that_combination(client, auth_headers, upstream):
    r = await client.post(
        "/api/v1/settings/judge/test",
        json={
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "base_url": "https://api.deepseek.com",
            "api_key": TYPED,
        },
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["code"] == "ok"
    assert body["endpoint_host"] == "api.deepseek.com"
    assert body["model"] == "deepseek-v4-pro"
    assert set(body) == {"ok", "code", "message", "provider", "model", "endpoint_host", "latency_ms"}
    assert upstream.auth() == f"Bearer {TYPED}"
    assert TYPED not in r.text


async def test_console_url_is_endpoint_not_api(client, auth_headers, upstream):
    upstream.replies["platform.deepseek.com"] = CONSOLE_405
    r = await client.post(
        "/api/v1/settings/judge/test",
        json={
            "provider": "deepseek",
            "model": "deepseek-flash",
            "base_url": "https://platform.deepseek.com",
            "api_key": TYPED,
        },
        headers=auth_headers,
    )
    body = r.json()
    assert body["ok"] is False
    assert body["code"] == "endpoint_not_api"
    assert "https://api.deepseek.com" in body["message"]
    assert "Method Not Allowed" not in r.text


async def test_saved_key_used_when_binding_matches(client, auth_headers, upstream):
    await _save_personal(client, auth_headers, base_url="https://llm.example.com/v1")
    r = await client.post(
        "/api/v1/settings/judge/test",
        json={"provider": "deepseek", "model": "m2", "base_url": "https://llm.example.com/v1/"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert upstream.auth() == f"Bearer {SAVED}"
    assert str(upstream.calls[-1].url) == "https://llm.example.com/v1/chat/completions"


async def test_empty_body_tests_saved_settings(client, auth_headers, upstream):
    await _save_personal(client, auth_headers)
    r = await client.post("/api/v1/settings/judge/test", json={}, headers=auth_headers)
    assert r.json()["ok"] is True
    assert str(upstream.calls[-1].url) == "https://api.deepseek.com/chat/completions"
    assert upstream.auth() == f"Bearer {SAVED}"


@pytest.mark.parametrize(
    "saved_base,request_body",
    [
        # Different custom endpoint.
        ("https://llm.example.com/v1", {"base_url": "https://evil.example.com/v1"}),
        # Saved custom endpoint, request switches to the registry API.
        ("https://llm.example.com/v1", {"provider": "deepseek", "base_url": ""}),
        ("https://llm.example.com/v1", {"provider": "openai", "base_url": None}),
        # Saved registry provider, request picks another provider.
        (None, {"provider": "openai", "model": "gpt-6-luna", "base_url": ""}),
        # Saved registry provider, request adds a custom endpoint.
        (None, {"provider": "deepseek", "base_url": "https://evil.example.com"}),
    ],
)
async def test_saved_key_never_sent_elsewhere(
    client, auth_headers, upstream, saved_base, request_body
):
    await _save_personal(client, auth_headers, base_url=saved_base)
    before = len(upstream.calls)
    r = await client.post("/api/v1/settings/judge/test", json=request_body, headers=auth_headers)
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "key_required"
    assert "Enter the key" in r.json()["error"]["message"]
    assert len(upstream.calls) == before


async def test_no_saved_key_registry_provider_is_key_required(client, auth_headers, upstream):
    r = await client.post(
        "/api/v1/settings/judge/test",
        json={"provider": "deepseek", "model": "deepseek-flash"},
        headers=auth_headers,
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "key_required"


async def test_keyless_custom_endpoint_is_tested_without_key(client, auth_headers, upstream):
    r = await client.post(
        "/api/v1/settings/judge/test",
        json={"provider": "openai", "model": "llama3", "base_url": "https://ollama.example.com/v1"},
        headers=auth_headers,
    )
    assert r.json()["ok"] is True
    assert upstream.auth() == ""


async def test_ssrf_blocked_base_url(client, auth_headers, upstream):
    for url in ("https://10.0.0.5/v1", "http://169.254.169.254/v1", "http://llm.example.com/v1"):
        r = await client.post(
            "/api/v1/settings/judge/test",
            json={"provider": "openai", "model": "m", "base_url": url, "api_key": TYPED},
            headers=auth_headers,
        )
        assert r.status_code == 200, r.text
        assert r.json()["code"] == "endpoint_blocked", url
        assert "10.0.0.5" not in r.json()["message"]
    assert upstream.calls == []


async def test_unknown_provider_is_422(client, auth_headers, upstream):
    r = await client.post(
        "/api/v1/settings/judge/test",
        json={"provider": "nope", "model": "m", "api_key": TYPED},
        headers=auth_headers,
    )
    assert r.status_code == 422


async def test_rate_limited_after_ten(client, auth_headers, upstream):
    body = {"provider": "deepseek", "model": "deepseek-flash", "api_key": TYPED}
    for _ in range(10):
        r = await client.post("/api/v1/settings/judge/test", json=body, headers=auth_headers)
        assert r.status_code == 200, r.text
    r = await client.post("/api/v1/settings/judge/test", json=body, headers=auth_headers)
    assert r.status_code == 429
    assert r.json()["error"]["code"] == "test_rate_limited"
    assert len(upstream.calls) == 10


# ---------------------------------------------------------------------------
# Personal save-time check
# ---------------------------------------------------------------------------


@pytest.mark.real_llm_save_check
async def test_save_refuses_console_url(client, auth_headers, upstream):
    upstream.replies["platform.deepseek.com"] = CONSOLE_405
    r = await client.put(
        "/api/v1/settings/judge",
        json={
            "provider": "deepseek",
            "model": "deepseek-flash",
            "api_key": TYPED,
            "base_url": "https://platform.deepseek.com",
        },
        headers=auth_headers,
    )
    assert r.status_code == 422, r.text
    detail = r.json()["error"]
    assert detail["code"] == "endpoint_not_api"
    assert "https://api.deepseek.com" in detail["message"]
    g = await client.get("/api/v1/settings/judge", headers=auth_headers)
    assert g.json()["key_set"] is False


@pytest.mark.real_llm_save_check
async def test_save_refuses_rejected_key(client, auth_headers, upstream):
    upstream.replies["llm.example.com"] = httpx.Response(
        401, content=b'{"error":{"message":"bad key"}}', headers={"content-type": "application/json"}
    )
    r = await client.put(
        "/api/v1/settings/judge",
        json={"provider": "openai", "model": "m", "api_key": TYPED, "base_url": "https://llm.example.com/v1"},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_key"


@pytest.mark.real_llm_save_check
async def test_save_warns_on_transient_failure(client, auth_headers, upstream):
    upstream.replies["llm.example.com"] = httpx.Response(
        503, content=b'{"error":{"message":"overloaded"}}', headers={"content-type": "application/json"}
    )
    r = await client.put(
        "/api/v1/settings/judge",
        json={"provider": "openai", "model": "m", "api_key": TYPED, "base_url": "https://llm.example.com/v1"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    warnings = r.json()["warnings"]
    assert [w["code"] for w in warnings] == ["provider_error"]
    g = await client.get("/api/v1/settings/judge", headers=auth_headers)
    assert g.json()["base_url"] == "https://llm.example.com/v1"


@pytest.mark.real_llm_save_check
async def test_save_without_base_url_makes_no_call(client, auth_headers, upstream):
    r = await client.put(
        "/api/v1/settings/judge",
        json={"provider": "deepseek", "model": "deepseek-flash", "api_key": TYPED},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert r.json()["warnings"] == []
    assert upstream.calls == []


@pytest.mark.real_llm_save_check
async def test_save_unchanged_base_url_without_key_makes_no_call(client, auth_headers, upstream):
    await _save_personal(client, auth_headers, base_url="https://llm.example.com/v1")
    calls = len(upstream.calls)
    r = await client.put(
        "/api/v1/settings/judge",
        json={"provider": "deepseek", "model": "other", "api_key": "", "base_url": "https://llm.example.com/v1"},
        headers=auth_headers,
    )
    assert r.status_code == 200
    assert len(upstream.calls) == calls


@pytest.mark.real_llm_save_check
async def test_save_keeps_ssrf_400(client, auth_headers, upstream):
    r = await client.put(
        "/api/v1/settings/judge",
        json={"provider": "openai", "model": "m", "api_key": TYPED, "base_url": "https://10.0.0.5/v1"},
        headers=auth_headers,
    )
    assert r.status_code == 400
    assert upstream.calls == []


# ---------------------------------------------------------------------------
# Org test endpoint + save-time check
# ---------------------------------------------------------------------------


async def _org_put(client, key, org_id, **body):
    payload = {"provider": "deepseek", "model": "deepseek-flash", "api_key": SAVED}
    payload.update(body)
    return await client.put(f"/api/v1/orgs/{org_id}/llm-settings", json=payload, headers=_hdrs(key))


async def test_org_admin_tests_with_stored_key(client, db_session, upstream):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    assert (await _org_put(client, key, org.id)).status_code == 200
    r = await client.post(
        f"/api/v1/orgs/{org.id}/llm-settings/test",
        json={"provider": "deepseek", "model": "deepseek-flash", "base_url": ""},
        headers=_hdrs(key),
    )
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert upstream.auth() == f"Bearer {SAVED}"


async def test_org_stored_key_not_sent_to_new_endpoint(client, db_session, upstream):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    assert (await _org_put(client, key, org.id)).status_code == 200
    r = await client.post(
        f"/api/v1/orgs/{org.id}/llm-settings/test",
        json={"provider": "deepseek", "model": "m", "base_url": "https://evil.example.com"},
        headers=_hdrs(key),
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "key_required"
    assert upstream.calls == []


async def test_org_member_is_403_and_other_org_is_404(client, db_session, upstream):
    admin, admin_key = await _make_user(db_session, "admin")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org_with(db_session, admin, member)
    outsider, outsider_key = await _make_user(db_session, "outsider")
    await _make_org_with(db_session, outsider)
    body = {"provider": "deepseek", "model": "m", "api_key": TYPED}
    r = await client.post(f"/api/v1/orgs/{org.id}/llm-settings/test", json=body, headers=_hdrs(member_key))
    assert r.status_code == 403
    r = await client.post(f"/api/v1/orgs/{org.id}/llm-settings/test", json=body, headers=_hdrs(outsider_key))
    assert r.status_code == 404
    assert upstream.calls == []


async def test_org_below_enterprise_is_403(client, db_session, upstream):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin, tier="team")
    r = await client.post(
        f"/api/v1/orgs/{org.id}/llm-settings/test",
        json={"provider": "deepseek", "model": "m", "api_key": TYPED},
        headers=_hdrs(key),
    )
    assert r.status_code == 403
    assert r.json()["error"]["details"]["error"] == "upgrade_required"
    assert upstream.calls == []


@pytest.mark.real_llm_save_check
async def test_org_save_refuses_console_and_warns_on_rate_limit(client, db_session, upstream):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    upstream.replies["platform.deepseek.com"] = CONSOLE_405
    r = await _org_put(client, key, org.id, base_url="https://platform.deepseek.com")
    assert r.status_code == 422, r.text
    assert r.json()["error"]["code"] == "endpoint_not_api"

    upstream.replies["api.deepseek.com"] = httpx.Response(
        429, content=b'{"error":{"message":"slow"}}', headers={"content-type": "application/json"}
    )
    r = await _org_put(client, key, org.id, base_url="https://api.deepseek.com")
    assert r.status_code == 200, r.text
    assert [w["code"] for w in r.json()["warnings"]] == ["rate_limited"]
    sent = json.loads(upstream.calls[-1].content)
    assert sent["messages"][-1]["content"] == "ping"


# ---------------------------------------------------------------------------
# Round 1 review fixes
# ---------------------------------------------------------------------------


def _html(status: int) -> httpx.Response:
    return httpx.Response(
        status,
        content=b"<!DOCTYPE html><html><title>Bad gateway</title>cloudflare</html>",
        headers={"content-type": "text/html; charset=UTF-8"},
    )


@pytest.mark.real_llm_save_check
@pytest.mark.parametrize("status", [502, 503])
async def test_save_cloudflare_html_outage_is_a_warning(client, auth_headers, upstream, status):
    upstream.replies["llm.example.com"] = _html(status)
    r = await client.put(
        "/api/v1/settings/judge",
        json={"provider": "openai", "model": "m", "api_key": TYPED, "base_url": "https://llm.example.com/v1"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    warnings = r.json()["warnings"]
    assert [w["code"] for w in warnings] == ["provider_error"]
    assert "accept chat requests" not in warnings[0]["message"]
    assert "cloudflare" not in r.text


@pytest.mark.real_llm_save_check
async def test_save_json_404_route_not_found_is_a_warning(client, auth_headers, upstream):
    upstream.replies["llm.example.com"] = httpx.Response(
        404, content=b'{"error":"route not found"}', headers={"content-type": "application/json"}
    )
    r = await client.put(
        "/api/v1/settings/judge",
        json={"provider": "openai", "model": "m", "api_key": TYPED, "base_url": "https://llm.example.com/v1"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert [w["code"] for w in r.json()["warnings"]] == ["endpoint_not_api"]


@pytest.mark.real_llm_save_check
@pytest.mark.parametrize("reply", ["outage", "405"])
async def test_key_rotation_on_unchanged_url_saves_with_warning(client, auth_headers, upstream, reply):
    url = "https://llm.example.com/v1"
    await _save_personal(client, auth_headers, base_url=url)
    upstream.replies["llm.example.com"] = _html(503) if reply == "outage" else CONSOLE_405
    r = await client.put(
        "/api/v1/settings/judge",
        json={"provider": "deepseek", "model": "deepseek-flash", "api_key": TYPED, "base_url": url},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["warnings"]) == 1


@pytest.mark.real_llm_save_check
async def test_key_rotation_on_unchanged_url_still_refuses_rejected_key(client, auth_headers, upstream):
    url = "https://llm.example.com/v1"
    await _save_personal(client, auth_headers, base_url=url)
    upstream.replies["llm.example.com"] = httpx.Response(
        401, content=b'{"error":{"message":"bad key"}}', headers={"content-type": "application/json"}
    )
    r = await client.put(
        "/api/v1/settings/judge",
        json={"provider": "deepseek", "model": "deepseek-flash", "api_key": TYPED, "base_url": url},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_key"


@pytest.mark.real_llm_save_check
async def test_org_key_rotation_during_outage_saves_with_warning(client, db_session, upstream):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    url = "https://llm.example.com/v1"
    assert (await _org_put(client, key, org.id, base_url=url)).status_code == 200
    upstream.replies["llm.example.com"] = _html(502)
    r = await _org_put(client, key, org.id, base_url=url, api_key=TYPED)
    assert r.status_code == 200, r.text
    assert [w["code"] for w in r.json()["warnings"]] == ["provider_error"]
    g = await client.get(f"/api/v1/orgs/{org.id}/llm-settings", headers=_hdrs(key))
    assert g.json()["key_hint"] == TYPED[-4:]


async def test_private_target_timeout_reads_unreachable_without_timing(
    client, auth_headers, monkeypatch
):
    monkeypatch.setenv("SFS_LLM_ALLOW_PRIVATE_HOSTS", "true")

    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    def factory(timeout):
        return httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(slow))

    with patch("sessionfs.judge.agent_stream._make_client", side_effect=factory):
        r = await client.post(
            "/api/v1/settings/judge/test",
            json={"provider": "openai", "model": "llama3", "base_url": "http://10.0.0.5:11434/v1"},
            headers=auth_headers,
        )
    body = r.json()
    assert body["code"] == "unreachable"
    assert body["latency_ms"] is None


async def test_public_target_timeout_keeps_code_and_timing(client, auth_headers):
    def slow(request):
        raise httpx.ReadTimeout("slow", request=request)

    def factory(timeout):
        return httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(slow))

    with patch("sessionfs.judge.agent_stream._make_client", side_effect=factory):
        r = await client.post(
            "/api/v1/settings/judge/test",
            json={"provider": "openai", "model": "m", "base_url": "https://llm.example.com/v1", "api_key": TYPED},
            headers=auth_headers,
        )
    assert r.json()["code"] == "timeout"
    assert isinstance(r.json()["latency_ms"], int)



# ---------------------------------------------------------------------------
# Round 2 review fixes
# ---------------------------------------------------------------------------


def _json(status: int, message: str) -> httpx.Response:
    return httpx.Response(
        status,
        content=json.dumps({"error": {"message": message}}).encode(),
        headers={"content-type": "application/json"},
    )


@pytest.mark.real_llm_save_check
@pytest.mark.parametrize(
    "message,expected",
    [("Your region is not supported", "access_denied"), ("Invalid API key", "invalid_key")],
)
async def test_key_rotation_403_is_a_warning(client, auth_headers, upstream, message, expected):
    url = "https://llm.example.com/v1"
    await _save_personal(client, auth_headers, base_url=url)
    upstream.replies["llm.example.com"] = _json(403, message)
    r = await client.put(
        "/api/v1/settings/judge",
        json={"provider": "deepseek", "model": "deepseek-flash", "api_key": TYPED, "base_url": url},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert [w["code"] for w in r.json()["warnings"]] == [expected]


@pytest.mark.real_llm_save_check
async def test_new_url_403_invalid_key_is_refused(client, auth_headers, upstream):
    upstream.replies["llm.example.com"] = _json(403, "Invalid API key")
    r = await client.put(
        "/api/v1/settings/judge",
        json={"provider": "openai", "model": "m", "api_key": TYPED, "base_url": "https://llm.example.com/v1"},
        headers=auth_headers,
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_key"


@pytest.mark.real_llm_save_check
async def test_new_url_403_permission_is_a_warning(client, auth_headers, upstream):
    upstream.replies["llm.example.com"] = _json(403, "Model access denied for your organization")
    r = await client.put(
        "/api/v1/settings/judge",
        json={"provider": "openai", "model": "m", "api_key": TYPED, "base_url": "https://llm.example.com/v1"},
        headers=auth_headers,
    )
    assert r.status_code == 200, r.text
    assert [w["code"] for w in r.json()["warnings"]] == ["access_denied"]


@pytest.mark.real_llm_save_check
async def test_key_rotation_401_is_still_refused(client, auth_headers, upstream):
    url = "https://llm.example.com/v1"
    await _save_personal(client, auth_headers, base_url=url)
    upstream.replies["llm.example.com"] = _json(401, "bad key")
    r = await client.put(
        "/api/v1/settings/judge",
        json={"provider": "deepseek", "model": "deepseek-flash", "api_key": TYPED, "base_url": url},
        headers=auth_headers,
    )
    assert r.status_code == 422


async def _private_test(client, auth_headers, reply, api_key=TYPED):
    def handler(request):
        return httpx.Response(reply.status_code, content=reply.content, headers=reply.headers)

    def factory(timeout):
        return httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(handler))

    with patch("sessionfs.judge.agent_stream._make_client", side_effect=factory):
        r = await client.post(
            "/api/v1/settings/judge/test",
            json={"provider": "openai", "model": "llama3", "base_url": "http://10.0.0.5:8080/v1",
                  "api_key": api_key},
            headers=auth_headers,
        )
    return r.json()


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(404, content=b"<html>Jenkins</html>", headers={"content-type": "text/html"}),
        httpx.Response(200, content=b"<html>admin</html>", headers={"content-type": "text/html"}),
        httpx.Response(404, content=b'{"detail":"Not Found"}', headers={"content-type": "application/json"}),
        httpx.Response(405, content=b'{"detail":"Method Not Allowed"}', headers={"content-type": "application/json"}),
    ],
)
async def test_private_not_api_reads_unreachable(client, auth_headers, monkeypatch, reply):
    monkeypatch.setenv("SFS_LLM_ALLOW_PRIVATE_HOSTS", "true")
    body = await _private_test(client, auth_headers, reply)
    assert body["code"] == "unreachable"
    assert body["latency_ms"] is None
    assert "Jenkins" not in body["message"]


async def test_private_failure_drops_detail(client, auth_headers, monkeypatch):
    monkeypatch.setenv("SFS_LLM_ALLOW_PRIVATE_HOSTS", "true")
    body = await _private_test(client, auth_headers, _json(500, "internal: redis at 10.0.0.9 down"))
    assert body["code"] == "provider_error"
    assert "redis" not in body["message"]
    assert body["message"] == "The provider returned an error."


async def test_private_keeps_ok_and_invalid_key(client, auth_headers, monkeypatch, upstream):
    monkeypatch.setenv("SFS_LLM_ALLOW_PRIVATE_HOSTS", "true")
    body = await _private_test(client, auth_headers, _json(401, "bad key"))
    assert body["code"] == "invalid_key"
    r = await client.post(
        "/api/v1/settings/judge/test",
        json={"provider": "openai", "model": "llama3", "base_url": "http://10.0.0.5:11434/v1"},
        headers=auth_headers,
    )
    assert r.json()["ok"] is True
    assert isinstance(r.json()["latency_ms"], int)


async def test_private_failures_are_padded(client, auth_headers, monkeypatch):
    import time

    from sessionfs.server.services import llm_connection

    monkeypatch.setenv("SFS_LLM_ALLOW_PRIVATE_HOSTS", "true")
    monkeypatch.setattr(llm_connection, "PRIVATE_FAILURE_MIN_SECONDS", 0.4)
    started = time.monotonic()
    body = await _private_test(
        client, auth_headers,
        httpx.Response(404, content=b"<html/>", headers={"content-type": "text/html"}),
    )
    assert body["code"] == "unreachable"
    assert time.monotonic() - started >= 0.4
