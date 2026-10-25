"""Retry-on-parameter-rejection behaviour for OpenAI-compatible endpoints.

Covers the LiteLLM/Azure gateway failure in issue #102: a reasoning model
rejects a non-default ``temperature`` (or an unknown ``reasoning`` parameter)
with HTTP 400/422, and the client must retry once without the rejected
parameter and remember the omission per (endpoint URL, model).
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from sessionfs.judge.agent_stream import ProviderError, TextDelta, stream_chat, user_message
from sessionfs.judge.providers import (
    _classify_param_error,
    _clear_omission_cache,
    _is_reasoning_model,
    call_llm,
)

AZURE_TEMPERATURE_ERROR = (
    "Unsupported value: 'temperature' does not support 0 with this model. "
    "Only the default (1) value is supported."
)

OPENAI_ERROR_JSON = {"error": {"message": AZURE_TEMPERATURE_ERROR}}


async def collect(agen):
    return [event async for event in agen]


@pytest.fixture(autouse=True)
def _clear_cache():
    _clear_omission_cache()
    yield
    _clear_omission_cache()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sync_requests(monkeypatch: pytest.MonkeyPatch, responses: list[httpx.Response]):
    """Route sync httpx.AsyncClient through a MockTransport replaying ``responses``."""
    requests: list[httpx.Request] = []
    idx = 0

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        nonlocal idx
        resp = responses[min(idx, len(responses) - 1)]
        idx += 1
        return resp

    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return requests


def _ok_json(content: str) -> httpx.Response:
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


def _stream_handler(responses: list[httpx.Response]):
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        resp = responses[min(calls, len(responses) - 1)]
        calls += 1
        return resp

    return handler


def _mock_stream(handler):
    def factory(timeout):
        return httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(handler))

    return patch("sessionfs.judge.agent_stream._make_client", side_effect=factory)


OPENAI_TEXT_STREAM = b"".join(
    [
        b'data: {"choices":[{"delta":{"role":"assistant","content":"Hello"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":" there"}}]}\n\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
        b'data: [DONE]\n\n',
    ]
)


# ---------------------------------------------------------------------------
# Parameter rejection classification and prefix detection (pure units)
# ---------------------------------------------------------------------------

class TestClassifyParamError:
    def test_azure_temperature_rejection(self):
        assert _classify_param_error(400, AZURE_TEMPERATURE_ERROR) == frozenset({"temperature"})

    def test_litellm_reasoning_rejection(self):
        text = "Unsupported parameter: 'reasoning' is not supported for this model."
        assert _classify_param_error(400, text) == frozenset({"reasoning"})

    def test_reasoning_effort_rejection(self):
        text = "BadRequestError: reasoning_effort is unknown."
        assert _classify_param_error(422, text) == frozenset({"reasoning"})

    def test_top_p_rejection(self):
        text = "top_p does not support a value other than 1."
        assert _classify_param_error(400, text) == frozenset({"top_p"})

    def test_unrelated_error_returns_empty(self):
        assert _classify_param_error(400, "missing required field 'messages'") == frozenset()

    def test_unsupported_status_returns_empty(self):
        assert _classify_param_error(500, AZURE_TEMPERATURE_ERROR) == frozenset()

    def test_mentions_param_without_rejection_token(self):
        # "temperature" appears, but no rejection token -> not a rejection.
        assert _classify_param_error(400, "temperature is fine, thanks") == frozenset()


class TestReasoningPrefixDetection:
    @pytest.mark.parametrize(
        "model",
        [
            "o1",
            "o1-mini",
            "o1-pro",
            "o3",
            "o3-mini",
            "o3-mini-2025-04-16",
            "o4-mini",
            "gpt-5",
            "gpt-5.6-sol",
            "gpt-6-astra",
            "gpt-6.1-sol",
        ],
    )
    def test_reasoning_families_detected(self, model):
        assert _is_reasoning_model(model) is True

    @pytest.mark.parametrize(
        "model",
        ["gpt-4o", "gpt-4.2", "gpt-4.2-turbo", "gpt-4-2025-04-16", "claude-sonnet-4", "deepseek-v4-pro"],
    )
    def test_non_reasoning_names_not_mangled(self, model):
        # "gpt-4.2"-style names contain "-2" but must not be stripped into a
        # reasoning prefix (or any other reasoning family).
        assert _is_reasoning_model(model) is False


# ---------------------------------------------------------------------------
# Sync retry behaviour (call_llm -> _call_openai_compatible)
# ---------------------------------------------------------------------------

class TestSyncRetry:
    @pytest.mark.asyncio
    async def test_retries_once_without_temperature_on_azure_error(self, monkeypatch):
        reqs = _sync_requests(
            monkeypatch,
            [httpx.Response(400, json=OPENAI_ERROR_JSON), _ok_json("ok")],
        )
        out = await call_llm("gpt-4o", "sys", "prompt", "k", base_url="https://litellm.internal/v1")
        assert out == "ok"
        assert len(reqs) == 2
        assert "temperature" in json.loads(reqs[0].content)
        assert "temperature" not in json.loads(reqs[1].content)

    @pytest.mark.asyncio
    async def test_retries_without_reasoning_effort(self, monkeypatch):
        reqs = _sync_requests(
            monkeypatch,
            [
                httpx.Response(
                    400,
                    json={"error": {"message": "Unrecognized request argument supplied: reasoning_effort"}},
                ),
                _ok_json("ok"),
            ],
        )
        out = await call_llm("gpt-5.6-sol", "sys", "prompt", "k", base_url="https://litellm.internal/v1")
        assert out == "ok"
        assert len(reqs) == 2
        body1 = json.loads(reqs[0].content)
        body2 = json.loads(reqs[1].content)
        # OpenAI-compatible gateways get OpenAI's top-level reasoning_effort.
        assert body1["reasoning_effort"] == "medium"
        assert "reasoning_effort" not in body2 and "reasoning" not in body2
        # A reasoning model must not get temperature re-added on the retry.
        assert "temperature" not in body2

    @pytest.mark.asyncio
    async def test_no_retry_on_unrelated_400(self, monkeypatch):
        reqs = _sync_requests(
            monkeypatch,
            [httpx.Response(400, json={"error": {"message": "missing required field 'messages'"}})],
        )
        with pytest.raises(httpx.HTTPStatusError):
            await call_llm("gpt-4o", "sys", "prompt", "k", base_url="https://litellm.internal/v1")
        assert len(reqs) == 1

    @pytest.mark.asyncio
    async def test_no_second_retry(self, monkeypatch):
        reqs = _sync_requests(
            monkeypatch,
            [httpx.Response(400, json=OPENAI_ERROR_JSON), httpx.Response(400, json=OPENAI_ERROR_JSON)],
        )
        with pytest.raises(httpx.HTTPStatusError):
            await call_llm("gpt-4o", "sys", "prompt", "k", base_url="https://litellm.internal/v1")
        assert len(reqs) == 2  # initial + one retry, never a third

    @pytest.mark.asyncio
    async def test_cache_skips_temperature_on_next_call(self, monkeypatch):
        reqs = _sync_requests(
            monkeypatch,
            [
                httpx.Response(400, json=OPENAI_ERROR_JSON),
                _ok_json("first"),
                _ok_json("second"),
            ],
        )
        await call_llm("gpt-4o", "sys", "prompt", "k", base_url="https://litellm.internal/v1")
        await call_llm("gpt-4o", "sys", "prompt", "k", base_url="https://litellm.internal/v1")
        assert len(reqs) == 3
        assert "temperature" in json.loads(reqs[0].content)
        assert "temperature" not in json.loads(reqs[1].content)  # retry
        assert "temperature" not in json.loads(reqs[2].content)  # cached, up front


# ---------------------------------------------------------------------------
# Streaming retry behaviour (stream_chat -> _stream_openai_compatible)
# ---------------------------------------------------------------------------

class TestStreamingRetry:
    @pytest.mark.asyncio
    async def test_retries_once_without_temperature_on_azure_error(self):
        handler = _stream_handler(
            [
                httpx.Response(400, json=OPENAI_ERROR_JSON),
                httpx.Response(200, content=OPENAI_TEXT_STREAM, headers={"content-type": "text/event-stream"}),
            ]
        )
        requests: list[dict] = []

        def wrapping(request: httpx.Request) -> httpx.Response:
            requests.append(json.loads(request.content))
            return handler(request)

        with _mock_stream(wrapping):
            events = await collect(
                stream_chat(
                    model="gpt-4o",
                    system="sys",
                    messages=[user_message("hi")],
                    tools=[],
                    api_key="k",
                    base_url="https://litellm.internal/v1",
                )
            )

        text = "".join(e.text for e in events if isinstance(e, TextDelta))
        assert text == "Hello there"
        assert len(requests) == 2
        assert "temperature" in requests[0]
        assert "temperature" not in requests[1]

    @pytest.mark.asyncio
    async def test_no_retry_on_unrelated_400(self):
        handler = _stream_handler(
            [httpx.Response(400, json={"error": {"message": "bad request"}})]
        )
        with _mock_stream(handler):
            with pytest.raises(ProviderError):
                await collect(
                    stream_chat(
                        model="gpt-4o",
                        system="sys",
                        messages=[user_message("hi")],
                        tools=[],
                        api_key="k",
                        base_url="https://litellm.internal/v1",
                    )
                )


class TestReasoningParamShape:
    @pytest.mark.asyncio
    async def test_openai_gets_reasoning_effort_string(self, monkeypatch):
        reqs = _sync_requests(monkeypatch, [_ok_json("ok")])
        await call_llm("gpt-6-astra", "sys", "prompt", "k", provider="openai")
        body = json.loads(reqs[0].content)
        assert body["reasoning_effort"] == "medium"
        assert "reasoning" not in body and "temperature" not in body

    @pytest.mark.asyncio
    async def test_openrouter_gets_reasoning_object(self, monkeypatch):
        reqs = _sync_requests(monkeypatch, [_ok_json("ok")])
        await call_llm("openai/gpt-6-astra", "sys", "prompt", "k", provider="openrouter")
        body = json.loads(reqs[0].content)
        assert body["reasoning"] == {"effort": "medium"}
        assert "reasoning_effort" not in body

    @pytest.mark.asyncio
    async def test_openai_unknown_parameter_wording_retries(self, monkeypatch):
        reqs = _sync_requests(
            monkeypatch,
            [
                httpx.Response(400, json={"error": {"message": "Unknown parameter: 'reasoning_effort'."}}),
                _ok_json("ok"),
            ],
        )
        assert await call_llm("o3", "sys", "prompt", "k", provider="openai") == "ok"
        assert "reasoning_effort" not in json.loads(reqs[1].content)
