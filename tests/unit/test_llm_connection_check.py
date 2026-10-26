"""Connection test + readable provider errors (tk_e6880f06870541bf).

Upstreams are mocked at ``agent_stream._make_client`` (streaming path, used
by the connection test and the helper) and with ``httpx.MockTransport`` on
``httpx.AsyncClient`` (single-shot path, used by audit / narrative).
"""

from __future__ import annotations

import asyncio
import json
from unittest.mock import patch

import httpx
import pytest

from sessionfs.judge import llm_errors
from sessionfs.judge import providers
from sessionfs.judge.agent_stream import ProviderError, stream_chat, user_message
from sessionfs.judge.connection_check import check_llm_connection
from sessionfs.judge.llm_errors import LlmEndpointError, classify_http_failure

KEY = "sk-secret-test-key-0042"
CONSOLE = "https://platform.deepseek.com"

OK_SSE = (
    b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":null}]}\n\n'
    b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
    b'"usage":{"prompt_tokens":3,"completion_tokens":1}}\n\n'
    b"data: [DONE]\n\n"
)


def mock_stream(handler):
    def factory(timeout):
        return httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(handler))

    return patch("sessionfs.judge.agent_stream._make_client", side_effect=factory)


def responder(status: int, body: bytes | str = b"", content_type: str = "application/json"):
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        content = body.encode() if isinstance(body, str) else body
        return httpx.Response(status, content=content, headers={"content-type": content_type})

    return handler, calls


async def run_check(handler, **kwargs):
    params = {"provider": "deepseek", "model": "deepseek-flash", "api_key": KEY}
    params.update(kwargs)
    with mock_stream(handler):
        return await check_llm_connection(**params)


# ---------------------------------------------------------------------------
# Classification table
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,body,ctype,custom,expected",
    [
        (405, '{"detail":"Method Not Allowed"}', "application/json", True, "endpoint_not_api"),
        (404, '{"detail":"Not Found"}', "application/json", True, "endpoint_not_api"),
        (404, "<html>nope</html>", "text/html", True, "endpoint_not_api"),
        (200, "<!DOCTYPE html><html></html>", "text/html", True, "endpoint_not_api"),
        (404, '{"error":{"message":"The model `x` does not exist","code":"model_not_found"}}',
         "application/json", True, "model_not_found"),
        (404, '{"type":"error","error":{"type":"not_found_error","message":"model: claude-x"}}',
         "application/json", False, "model_not_found"),
        (400, '{"error":{"message":"Model Not Exist"}}', "application/json", False, "model_not_found"),
        (400, '{"error":{"message":"The supported API model names are deepseek-flash, '
              'deepseek-v4-pro, but you passed nope."}}', "application/json", False,
         "model_not_found"),
        (400, '{"error":{"message":"API key not valid. Please pass a valid API key."}}',
         "application/json", False, "invalid_key"),
        (401, '{"error":{"message":"Authentication Fails"}}', "application/json", True, "invalid_key"),
        (403, '{"error":"forbidden"}', "application/json", False, "access_denied"),
        (403, '{"error":{"message":"Your region is not supported"}}', "application/json", True,
         "access_denied"),
        (403, '{"error":{"message":"Incorrect API key provided"}}', "application/json", True,
         "invalid_key"),
        (402, '{"error":{"message":"Insufficient Balance"}}', "application/json", False,
         "insufficient_balance"),
        (429, '{"error":{"message":"You exceeded your current quota","code":"insufficient_quota"}}',
         "application/json", False, "insufficient_balance"),
        (429, '{"error":{"message":"Rate limit reached"}}', "application/json", False, "rate_limited"),
        (500, '{"error":{"message":"boom"}}', "application/json", False, "provider_error"),
        (405, '{"detail":"Method Not Allowed"}', "application/json", False, "provider_error"),
        # Outage / rate-limit pages are not "wrong address" (M1).
        (502, "<html><title>502 Bad Gateway</title>cloudflare</html>", "text/html", True,
         "provider_error"),
        (503, "<!DOCTYPE html><html>Service Unavailable</html>", "text/html", True,
         "provider_error"),
        (429, "<html>Too Many Requests</html>", "text/html", True, "rate_limited"),
        (403, "<html>Attention Required! | Cloudflare</html>", "text/html", True, "provider_error"),
        # A parameter error mentioning the model is not model_not_found (L6).
        (400, '{"error":{"message":"Unsupported parameter: \'max_tokens\' is not supported '
              'with this model. Use \'max_completion_tokens\' instead."}}', "application/json",
         False, "provider_error"),
        (404, '{"detail":"Not Found"}', "application/json", False, "provider_error"),
    ],
)
def test_classification_table(status, body, ctype, custom, expected):
    base_url = "https://llm.example.com/v1" if custom else None
    assert classify_http_failure(status, body, ctype, base_url=base_url) == expected


def test_console_message_names_the_api_address():
    msg = llm_errors.endpoint_not_api_message(CONSOLE, "deepseek")
    assert "https://api.deepseek.com" in msg
    assert "platform.deepseek.com" in msg


def test_api_host_with_wrong_path_names_the_api_address():
    msg = llm_errors.endpoint_not_api_message("https://api.deepseek.com/anthropic", None)
    assert "https://api.deepseek.com" in msg
    msg = llm_errors.endpoint_not_api_message("https://openrouter.ai/settings/keys", None)
    assert "https://openrouter.ai/api/v1" in msg


def test_anthropic_console_says_leave_empty():
    msg = llm_errors.endpoint_not_api_message("https://console.anthropic.com", None)
    assert "Leave Base URL empty" in msg


def test_unknown_host_generic_message():
    msg = llm_errors.endpoint_not_api_message("https://intranet.example.com", None)
    assert msg.startswith("This address doesn't accept chat requests.")
    assert "https://api.deepseek.com" in msg


# ---------------------------------------------------------------------------
# check_llm_connection
# ---------------------------------------------------------------------------


async def test_deepseek_console_405_is_endpoint_not_api():
    handler, calls = responder(405, '{"detail":"Method Not Allowed"}')
    result = await run_check(handler, base_url=CONSOLE)
    assert not result.ok
    assert result.code == "endpoint_not_api"
    assert "https://api.deepseek.com" in result.message
    assert "Method Not Allowed" not in result.message
    assert result.endpoint_host == "platform.deepseek.com"
    assert len(calls) == 1
    assert str(calls[0].url) == "https://platform.deepseek.com/chat/completions"


async def test_success_reports_host_model_latency():
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=OK_SSE, headers={"content-type": "text/event-stream"})

    result = await run_check(handler, base_url="https://api.deepseek.com")
    assert result.ok and result.code == "ok"
    assert result.endpoint_host == "api.deepseek.com"
    assert result.model == "deepseek-flash"
    assert result.latency_ms >= 0
    body = json.loads(calls[0].content)
    assert body["messages"][-1] == {"role": "user", "content": "ping"}
    assert body["max_completion_tokens"] <= 16  # custom endpoints: generic field
    assert "temperature" not in body
    assert calls[0].headers["authorization"] == f"Bearer {KEY}"


async def test_registry_provider_uses_registry_url():
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=OK_SSE, headers={"content-type": "text/event-stream"})

    result = await run_check(handler)
    assert result.ok
    assert str(calls[0].url) == "https://api.deepseek.com/chat/completions"
    assert result.endpoint_host == "api.deepseek.com"


async def test_anthropic_registry_path():
    calls: list[httpx.Request] = []
    events = [
        {"type": "message_start", "message": {"usage": {"input_tokens": 3}}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "ok"}},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        {"type": "message_stop"},
    ]
    sse = b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events)

    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"})

    result = await run_check(handler, provider="anthropic", model="claude-haiku-5-5")
    assert result.ok
    assert result.endpoint_host == "api.anthropic.com"
    assert calls[0].headers["x-api-key"] == KEY


@pytest.mark.parametrize(
    "status,body,code",
    [
        (401, '{"error":{"message":"Authentication Fails"}}', "invalid_key"),
        (404, '{"error":{"message":"The model `nope` does not exist"}}', "model_not_found"),
        (400, '{"error":{"message":"Model Not Exist"}}', "model_not_found"),
        (429, '{"error":{"message":"slow down"}}', "rate_limited"),
        (402, '{"error":{"message":"Insufficient Balance"}}', "insufficient_balance"),
    ],
)
async def test_failure_codes(status, body, code):
    handler, _ = responder(status, body)
    result = await run_check(handler, base_url="https://api.deepseek.com")
    assert result.code == code
    assert not result.ok
    assert KEY not in result.message


async def test_html_200_is_endpoint_not_api():
    handler, _ = responder(200, "<!doctype html><html>console</html>", "text/html; charset=utf-8")
    result = await run_check(handler, base_url="https://llm.example.com")
    assert result.code == "endpoint_not_api"


async def test_json_200_without_stream_is_endpoint_not_api():
    handler, _ = responder(200, '{"status":"ok"}')
    result = await run_check(handler, base_url="https://llm.example.com")
    assert result.code == "endpoint_not_api"


async def test_provider_error_is_sanitised():
    handler, _ = responder(500, json.dumps({"error": {"message": f"bad thing with {KEY} " + "x" * 600}}))
    result = await run_check(handler, base_url="https://api.deepseek.com")
    assert result.code == "provider_error"
    assert KEY not in result.message
    assert "bad thing" in result.message
    assert len(result.message) < 300


async def test_invalid_key_without_key_asks_for_one():
    handler, _ = responder(401, '{"error":"unauthorized"}')
    result = await run_check(handler, base_url="https://llm.example.com", api_key="")
    assert result.code == "invalid_key"
    assert "needs an API key" in result.message


async def test_timeout_and_unreachable():
    def timeout_handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    result = await run_check(timeout_handler, base_url="https://api.deepseek.com")
    assert result.code == "timeout"

    def connect_handler(request):
        raise httpx.ConnectError("refused 10.1.2.3", request=request)

    result = await run_check(connect_handler, base_url="https://api.deepseek.com")
    assert result.code == "unreachable"
    assert "10.1.2.3" not in result.message
    assert "api.deepseek.com" in result.message


async def test_total_deadline_is_enforced():
    async def slow(request):
        await asyncio.sleep(5)
        return httpx.Response(200, content=OK_SSE)

    result = await run_check(slow, base_url="https://api.deepseek.com", deadline=0.1)
    assert result.code == "timeout"


async def test_response_size_is_capped():
    chunk = b'data: {"choices":[{"delta":{"content":"' + b"a" * 1000 + b'"}}]}\n\n'

    def handler(request):
        return httpx.Response(200, content=chunk * 400, headers={"content-type": "text/event-stream"})

    result = await run_check(handler, base_url="https://api.deepseek.com")
    assert result.code == "provider_error"
    assert "too large" in result.message


async def test_installed_ssrf_guard_blocks():
    async def guard(url):
        raise ValueError("blocked")

    providers.set_base_url_guard(guard)
    try:
        handler, calls = responder(200, OK_SSE, "text/event-stream")
        result = await run_check(handler, base_url="https://internal.example.com")
    finally:
        providers.set_base_url_guard(None)
    assert result.code == "endpoint_blocked"
    assert calls == []


async def test_no_redirects_followed():
    calls: list[httpx.Request] = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    result = await run_check(handler, base_url="https://llm.example.com")
    assert result.code == "endpoint_not_api"
    assert len(calls) == 1


# ---------------------------------------------------------------------------
# Runtime mapping (helper stream + single-shot audit/narrative path)
# ---------------------------------------------------------------------------


async def test_stream_chat_405_from_custom_base_url_is_readable():
    handler, _ = responder(405, '{"detail":"Method Not Allowed"}')
    with mock_stream(handler):
        with pytest.raises(ProviderError) as info:
            async for _ in stream_chat(
                model="deepseek-v4-pro",
                system="s",
                messages=[user_message("hi")],
                tools=[],
                api_key=KEY,
                provider="deepseek",
                base_url=CONSOLE,
            ):
                pass
    assert info.value.code == "endpoint_not_api"
    assert "https://api.deepseek.com" in info.value.message
    assert "Method Not Allowed" not in info.value.message


async def test_stream_chat_registry_error_keeps_provider_message():
    handler, _ = responder(429, '{"error":{"message":"upstream busy"}}')
    with mock_stream(handler):
        with pytest.raises(ProviderError) as info:
            async for _ in stream_chat(
                model="deepseek-flash",
                system="s",
                messages=[user_message("hi")],
                tools=[],
                api_key=KEY,
                provider="deepseek",
            ):
                pass
    assert info.value.code == "rate_limited"
    assert info.value.message == "upstream busy"


@pytest.fixture
def single_shot(monkeypatch):
    state: dict = {}
    real = httpx.AsyncClient

    def factory(*a, **k):
        k["transport"] = httpx.MockTransport(state["handler"])
        return real(*a, **k)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return state


async def test_call_llm_405_raises_readable_endpoint_error(single_shot):
    single_shot["handler"], _ = responder(405, '{"detail":"Method Not Allowed"}')
    with pytest.raises(LlmEndpointError) as info:
        await providers.call_llm("deepseek-flash", "s", "p", KEY, provider="deepseek", base_url=CONSOLE)
    assert info.value.code == "endpoint_not_api"
    assert "https://api.deepseek.com" in info.value.friendly_message
    # Still an HTTPStatusError for existing handlers.
    assert isinstance(info.value, httpx.HTTPStatusError)


async def test_call_llm_html_200_raises_readable_endpoint_error(single_shot):
    single_shot["handler"], _ = responder(200, "<html></html>", "text/html")
    with pytest.raises(LlmEndpointError):
        await providers.call_llm("m", "s", "p", KEY, base_url="https://llm.example.com")


async def test_call_llm_registry_404_unchanged(single_shot):
    single_shot["handler"], _ = responder(404, '{"detail":"Not Found"}')
    with pytest.raises(httpx.HTTPStatusError) as info:
        await providers.call_llm("deepseek-flash", "s", "p", KEY, provider="deepseek")
    assert not isinstance(info.value, LlmEndpointError)


def test_narrative_error_uses_friendly_message():
    from sessionfs.server.services.summarizer import _narrative_error_snippet

    request = httpx.Request("POST", "https://platform.deepseek.com/chat/completions")
    response = httpx.Response(405, content=b'{"detail":"Method Not Allowed"}', request=request)
    exc = LlmEndpointError(
        "endpoint_not_api",
        llm_errors.endpoint_not_api_message(CONSOLE, "deepseek"),
        request=request,
        response=response,
    )
    status, message = _narrative_error_snippet(exc, KEY)
    assert status == 405
    assert "https://api.deepseek.com" in message



# ---------------------------------------------------------------------------
# Round 1 review fixes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,body,ctype,base_url,expected",
    [
        (405, '{"detail":"Method Not Allowed"}', "application/json", "https://llm.example.com", True),
        (404, "<html>nope</html>", "text/html", "https://llm.example.com", True),
        (200, "<html></html>", "text/html", "https://llm.example.com", True),
        (302, "", "", "https://llm.example.com", True),
        (404, '{"error":"route not found"}', "application/json", "https://llm.example.com", False),
        (404, '{"detail":"Not Found"}', "application/json", CONSOLE, True),
        (503, "<html>down</html>", "text/html", "https://llm.example.com", False),
    ],
)
def test_definitive_not_api(status, body, ctype, base_url, expected):
    assert llm_errors.is_definitive_not_api(status, body, ctype, base_url=base_url) is expected


async def test_cloudflare_html_outage_is_not_wrong_address():
    for status in (502, 503):
        handler, _ = responder(
            status, "<!DOCTYPE html><html><title>Bad gateway</title>cloudflare</html>", "text/html"
        )
        result = await run_check(handler, base_url="https://llm.example.com/v1")
        assert result.code == "provider_error"
        assert "accept chat requests" not in result.message
        assert "<" not in result.message and "cloudflare" not in result.message
        assert result.definitive is False


async def test_json_404_route_not_found_is_soft():
    handler, _ = responder(404, '{"error":"route not found"}')
    result = await run_check(handler, base_url="https://llm.example.com/v1")
    assert result.code == "endpoint_not_api"
    assert result.definitive is False
    assert "404" in result.message


async def test_console_405_is_definitive():
    handler, _ = responder(405, '{"detail":"Method Not Allowed"}')
    result = await run_check(handler, base_url=CONSOLE)
    assert result.definitive is True


async def test_non_streaming_json_completion_counts_as_ok():
    completion = {
        "id": "x",
        "object": "chat.completion",
        "choices": [
            {"index": 0, "message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}
        ],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1},
    }
    handler, _ = responder(200, json.dumps(completion), "application/json")
    result = await run_check(handler, base_url="https://gateway.example.com/v1")
    assert result.ok, result


async def test_non_streaming_json_completion_streams_text_and_tools():
    from sessionfs.judge.agent_stream import Done, TextDelta, ToolCallEvent, Usage

    completion = {
        "choices": [
            {
                "message": {
                    "content": "hello",
                    "tool_calls": [
                        {"id": "c1", "type": "function",
                         "function": {"name": "get_weather", "arguments": '{"location": "Oslo"}'}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    handler, _ = responder(200, json.dumps(completion), "application/json")
    with mock_stream(handler):
        events = [
            e
            async for e in stream_chat(
                model="m", system="s", messages=[user_message("hi")], tools=[],
                api_key=KEY, base_url="https://gateway.example.com/v1",
            )
        ]
    assert TextDelta("hello") in events
    calls = [e.tool_call for e in events if isinstance(e, ToolCallEvent)]
    assert calls[0].name == "get_weather" and calls[0].arguments == {"location": "Oslo"}
    assert Usage(5, 2) in events
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "tool_calls"


async def test_json_200_that_is_not_a_completion_is_not_api():
    handler, _ = responder(200, '{"status":"ok"}', "application/json")
    result = await run_check(handler, base_url="https://llm.example.com")
    assert result.code == "endpoint_not_api"
    assert result.definitive is True


async def test_plain_text_body_is_never_echoed():
    handler, _ = responder(500, "internal stack trace at /srv/app.py line 12", "text/plain")
    result = await run_check(handler, base_url="https://api.deepseek.com")
    assert result.code == "provider_error"
    assert "stack trace" not in result.message
    assert result.message == "The provider returned an error."


async def test_runtime_plain_text_error_is_generic():
    handler, _ = responder(500, "internal stack trace at /srv/app.py", "text/plain")
    with mock_stream(handler):
        with pytest.raises(ProviderError) as info:
            async for _ in stream_chat(
                model="deepseek-flash", system="s", messages=[user_message("hi")], tools=[],
                api_key=KEY, provider="deepseek",
            ):
                pass
    assert "stack trace" not in info.value.message
    assert info.value.message == "The provider returned HTTP 500."



# ---------------------------------------------------------------------------
# Round 2 review fixes
# ---------------------------------------------------------------------------


def _sse_events(*events):
    return b"".join(b"data: " + json.dumps(e).encode() + b"\n\n" for e in events) + b"data: [DONE]\n\n"


SSE_WITH_TOOL = _sse_events(
    {"choices": [{"delta": {"content": "hel"}, "finish_reason": None}]},
    {"choices": [{"delta": {"content": "lo"}, "finish_reason": None}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1",
        "function": {"name": "get_weather", "arguments": '{"location": "Oslo"}'}}]},
        "finish_reason": None}]},
    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}],
     "usage": {"prompt_tokens": 4, "completion_tokens": 2}},
)


@pytest.mark.parametrize("ctype", ["application/json", "application/json; charset=utf-8"])
async def test_sse_stream_labelled_json_still_streams(ctype):
    from sessionfs.judge.agent_stream import Done, TextDelta, ToolCallEvent

    handler, _ = responder(200, SSE_WITH_TOOL, ctype)
    with mock_stream(handler):
        events = [
            e
            async for e in stream_chat(
                model="m", system="s", messages=[user_message("hi")], tools=[],
                api_key=KEY, base_url="https://gateway.example.com/v1",
            )
        ]
    assert [e.text for e in events if isinstance(e, TextDelta)] == ["hel", "lo"]
    calls = [e.tool_call for e in events if isinstance(e, ToolCallEvent)]
    assert calls[0].name == "get_weather" and calls[0].arguments == {"location": "Oslo"}
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "tool_calls"


async def test_sse_stream_labelled_json_registry_provider():
    from sessionfs.judge.agent_stream import Done

    handler, _ = responder(200, SSE_WITH_TOOL, "application/json")
    with mock_stream(handler):
        events = [
            e
            async for e in stream_chat(
                model="deepseek-flash", system="s", messages=[user_message("hi")], tools=[],
                api_key=KEY, provider="deepseek",
            )
        ]
    assert isinstance(events[-1], Done)


async def test_connection_test_ok_for_sse_labelled_json():
    handler, _ = responder(200, SSE_WITH_TOOL, "application/json")
    result = await run_check(handler, base_url="https://gateway.example.com/v1")
    assert result.ok, result


async def test_sse_after_leading_blank_lines_labelled_json():
    handler, _ = responder(200, b"\n\n" + SSE_WITH_TOOL, "application/json")
    result = await run_check(handler, base_url="https://gateway.example.com/v1")
    assert result.ok


async def test_403_permission_is_access_denied_not_key():
    handler, _ = responder(403, '{"error":{"message":"Model access is not enabled for this region"}}')
    result = await run_check(handler, base_url="https://api.deepseek.com")
    assert result.code == "access_denied"
    assert "access, region or model permissions" in result.message


def test_definitive_invalid_key():
    assert llm_errors.is_definitive_invalid_key(401, "{}")
    assert llm_errors.is_definitive_invalid_key(400, '{"error":{"message":"API key not valid."}}')
    assert not llm_errors.is_definitive_invalid_key(403, '{"error":{"message":"Invalid API key"}}')
