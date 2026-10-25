"""Per-provider request quirks (URL, token field, temperature, headers,
reasoning echo, DeepSeek replay) exercised through the registry dispatch."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from sessionfs.judge.agent_stream import (
    Done,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolResult,
    ToolResultEvent,
    ToolSpec,
    assistant_message,
    run_agent,
    stream_chat,
    user_message,
)
from sessionfs.judge.providers import call_llm


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DONE = b"data: [DONE]\n\n"


def sse(*events: Any) -> bytes:
    out = bytearray()
    for e in events:
        if isinstance(e, bytes):
            out += e
        else:
            out += b"data: " + json.dumps(e).encode() + b"\n\n"
    return bytes(out)


async def collect(agen):
    return [event async for event in agen]


def _sync_requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Route every sync httpx.AsyncClient through a capturing MockTransport."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    real = httpx.AsyncClient

    def factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return requests


def capturing_handler(bodies: list[bytes]):
    """Return (handler, requests) where the handler replays one body per call."""
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        idx = len(requests) - 1
        return httpx.Response(200, content=bodies[idx], headers={"content-type": "text/event-stream"})

    return handler, requests


def mock_stream(handler):
    def factory(timeout):
        return httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(handler))

    return patch("sessionfs.judge.agent_stream._make_client", side_effect=factory)


WEATHER_TOOL = ToolSpec(
    name="get_weather",
    description="Get the weather for a location",
    input_schema={
        "type": "object",
        "properties": {"location": {"type": "string"}},
        "required": ["location"],
    },
)

# ---------------------------------------------------------------------------
# Sync call path (providers.call_llm -> _call_openai_compatible)
# ---------------------------------------------------------------------------

DEEPSEEK_REASONING_TEXT = sse(
    {"choices": [{"delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
    {"choices": [{"delta": {"reasoning_content": "Let me think"}, "finish_reason": None}]},
    {"choices": [{"delta": {"reasoning_content": " carefully."}, "finish_reason": None}]},
    {"choices": [{"delta": {"content": "Paris"}, "finish_reason": None}]},
    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
    DONE,
)

DEEPSEEK_TOOL = sse(
    {"choices": [{"delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
    {"choices": [{"delta": {"reasoning_content": "Need weather."}, "finish_reason": None}]},
    {"choices": [{"delta": {"content": ""}, "finish_reason": None}]},
    {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {"index": 0, "id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": ""}}
                    ]
                },
                "finish_reason": None,
            }
        ]
    },
    {
        "choices": [
            {"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"location":"Paris"}'}}]}, "finish_reason": None}
        ]
    },
    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 8}},
    DONE,
)

DEEPSEEK_TEXT = sse(
    {"choices": [{"delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
    {"choices": [{"delta": {"content": "22C"}, "finish_reason": None}]},
    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 3}},
    DONE,
)


class TestSyncRequestQuirks:
    @pytest.mark.asyncio
    async def test_deepseek_uses_max_tokens_and_its_url(self, monkeypatch):
        reqs = _sync_requests(monkeypatch)
        await call_llm("deepseek-flash", "sys", "prompt", "k", provider="deepseek")
        req = reqs[0]
        assert str(req.url) == "https://api.deepseek.com/chat/completions"
        body = json.loads(req.content)
        assert "max_tokens" in body
        assert "max_completion_tokens" not in body

    @pytest.mark.asyncio
    async def test_kimi_omits_temperature_even_when_requested(self, monkeypatch):
        reqs = _sync_requests(monkeypatch)
        await call_llm("kimi-latest", "sys", "prompt", "k", provider="kimi", temperature=0.7)
        body = json.loads(reqs[0].content)
        assert "temperature" not in body
        assert "max_tokens" in body
        assert str(reqs[0].url) == "https://api.moonshot.ai/v1/chat/completions"

    @pytest.mark.asyncio
    async def test_openai_uses_max_completion_tokens(self, monkeypatch):
        reqs = _sync_requests(monkeypatch)
        await call_llm("gpt-4o", "sys", "prompt", "k", provider="openai")
        body = json.loads(reqs[0].content)
        assert "max_completion_tokens" in body
        assert "max_tokens" not in body

    @pytest.mark.asyncio
    async def test_openrouter_sends_referer_and_title_headers(self, monkeypatch):
        reqs = _sync_requests(monkeypatch)
        await call_llm("deepseek/deepseek-v4-pro", "sys", "prompt", "k", provider="openrouter")
        req = reqs[0]
        assert req.headers["HTTP-Referer"] == "https://sessionfs.dev"
        assert req.headers["X-Title"] == "SessionFS"
        assert "max_tokens" in json.loads(req.content)

    @pytest.mark.asyncio
    async def test_reasoning_model_uses_effort_instead_of_temperature(self, monkeypatch):
        reqs = _sync_requests(monkeypatch)
        await call_llm("o3", "sys", "prompt", "k", provider="openai", temperature=0.5)
        body = json.loads(reqs[0].content)
        assert body["reasoning_effort"] == "medium"
        assert "temperature" not in body


class TestStreamingRequestQuirks:
    @pytest.mark.asyncio
    async def test_deepseek_stream_uses_max_tokens(self):
        handler, requests = capturing_handler([DEEPSEEK_REASONING_TEXT])
        with mock_stream(handler):
            await collect(
                stream_chat(
                    model="deepseek-v4-pro",
                    system="sys",
                    messages=[user_message("Where is the Eiffel Tower?")],
                    tools=[],
                    api_key="k",
                    provider="deepseek",
                )
            )
        body = requests[0]
        assert "max_tokens" in body
        assert "max_completion_tokens" not in body

    @pytest.mark.asyncio
    async def test_kimi_stream_omits_temperature(self):
        handler, requests = capturing_handler([DEEPSEEK_REASONING_TEXT])
        with mock_stream(handler):
            await collect(
                stream_chat(
                    model="kimi-k2-0711-preview",
                    system="sys",
                    messages=[user_message("hi")],
                    tools=[],
                    api_key="k",
                    provider="kimi",
                    temperature=0.9,
                )
            )
        body = requests[0]
        assert "temperature" not in body
        assert "max_tokens" in body


class TestDeepSeekReasoning:
    @pytest.mark.asyncio
    async def test_reasoning_content_is_accumulated_but_never_surfaced(self):
        handler, _ = capturing_handler([DEEPSEEK_REASONING_TEXT])
        with mock_stream(handler):
            events = await collect(
                stream_chat(
                    model="deepseek-v4-pro",
                    system="sys",
                    messages=[user_message("Where is the Eiffel Tower?")],
                    tools=[],
                    api_key="k",
                    provider="deepseek",
                )
            )
        text = "".join(e.text for e in events if isinstance(e, TextDelta))
        assert text == "Paris"
        dones = [e for e in events if isinstance(e, Done)]
        assert dones[-1].reasoning_content == "Let me think carefully."

    @pytest.mark.asyncio
    async def test_replayed_history_without_reasoning_disables_thinking(self):
        handler, requests = capturing_handler([DEEPSEEK_REASONING_TEXT])
        with mock_stream(handler):
            await collect(
                stream_chat(
                    model="deepseek-v4-pro",
                    system="sys",
                    messages=[user_message("hi"), assistant_message("", [])],
                    tools=[],
                    api_key="k",
                    provider="deepseek",
                )
            )
        assert requests[0]["thinking"] == {"type": "disabled"}

    @pytest.mark.asyncio
    async def test_run_agent_echoes_reasoning_on_followup_tool_turn(self):
        handler, requests = capturing_handler([DEEPSEEK_TOOL, DEEPSEEK_TEXT])

        async def execute_tool(call: ToolCall) -> ToolResult:
            return ToolResult(call.id, "22C")

        with mock_stream(handler):
            events = await collect(
                run_agent(
                    model="deepseek-v4-pro",
                    system="sys",
                    messages=[user_message("Weather in Paris?")],
                    tools=[WEATHER_TOOL],
                    execute_tool=execute_tool,
                    api_key="k",
                    provider="deepseek",
                )
            )

        # Turn 1: fresh conversation, no assistant messages -> thinking stays on.
        assert "thinking" not in requests[0]

        # Turn 2: the assistant turn's reasoning is echoed, so thinking stays on.
        assert "thinking" not in requests[1]
        assistants = [m for m in requests[1]["messages"] if m.get("role") == "assistant"]
        assert len(assistants) == 1
        assert assistants[0]["reasoning_content"] == "Need weather."
        assert assistants[0]["tool_calls"][0]["function"]["name"] == "get_weather"

        # Reasoning never leaks into surfaced text, and the loop completes.
        text = "".join(e.text for e in events if isinstance(e, TextDelta))
        assert text == "22C"
        assert isinstance(events[-1], Done)
        assert events[-1].stop_reason == "stop"

    @pytest.mark.asyncio
    async def test_agent_emits_tool_events_for_reasoning_turn(self):
        handler, _ = capturing_handler([DEEPSEEK_TOOL, DEEPSEEK_TEXT])

        async def execute_tool(call: ToolCall) -> ToolResult:
            return ToolResult(call.id, "22C")

        with mock_stream(handler):
            events = await collect(
                run_agent(
                    model="deepseek-v4-pro",
                    system="sys",
                    messages=[user_message("Weather in Paris?")],
                    tools=[WEATHER_TOOL],
                    execute_tool=execute_tool,
                    api_key="k",
                    provider="deepseek",
                )
            )

        tool_events = [e for e in events if isinstance(e, ToolCallEvent)]
        result_events = [e for e in events if isinstance(e, ToolResultEvent)]
        assert len(tool_events) == 1
        assert tool_events[0].tool_call.name == "get_weather"
        assert tool_events[0].tool_call.arguments == {"location": "Paris"}
        assert len(result_events) == 1
        assert result_events[0].result.content == "22C"
