"""Tests for the streaming, tool-calling provider layer (agent_stream)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from sessionfs.judge.agent_stream import (
    Done,
    ProviderError,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolResult,
    ToolResultEvent,
    ToolSpec,
    Usage,
    run_agent,
    stream_chat,
    user_message,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

DONE = b"data: [DONE]\n\n"


def sse(*events: Any) -> bytes:
    """Encode provider events as an SSE byte stream."""
    out = bytearray()
    for e in events:
        if isinstance(e, bytes):
            out += e
        else:
            out += b"data: " + json.dumps(e).encode() + b"\n\n"
    return bytes(out)


async def collect(agen):
    return [event async for event in agen]


def mock_stream(handler):
    """Patch the client factory to route through a MockTransport handler."""

    def factory(timeout):
        return httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(handler))

    return patch("sessionfs.judge.agent_stream._make_client", side_effect=factory)


def capturing_handler(bodies: list[bytes]):
    """Return (handler, requests) where the handler replays one body per call."""
    requests: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        idx = len(requests) - 1
        return httpx.Response(200, content=bodies[idx], headers={"content-type": "text/event-stream"})

    return handler, requests


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
# Text-only streaming per provider
# ---------------------------------------------------------------------------

ANTHROPIC_TEXT = sse(
    {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 0}}},
    {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " world"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 5}},
    {"type": "message_stop"},
)

OPENAI_TEXT = sse(
    {"choices": [{"delta": {"role": "assistant", "content": ""}, "finish_reason": None}]},
    {"choices": [{"delta": {"content": "Hello"}, "finish_reason": None}]},
    {"choices": [{"delta": {"content": " world"}, "finish_reason": None}]},
    {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}},
    DONE,
)

GOOGLE_TEXT = sse(
    {
        "candidates": [
            {"content": {"parts": [{"text": "Hello"}], "role": "model"}, "finishReason": None, "index": 0}
        ]
    },
    {
        "candidates": [
            {
                "content": {"parts": [{"text": " world"}], "role": "model"},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    },
)


class TestTextStreaming:
    @pytest.mark.asyncio
    async def test_anthropic_text(self):
        with mock_stream(lambda req: httpx.Response(200, content=ANTHROPIC_TEXT)):
            events = await collect(
                stream_chat(
                    model="claude-sonnet-4",
                    system="sys",
                    messages=[user_message("hi")],
                    tools=[],
                    api_key="k",
                    provider="anthropic",
                )
            )
        assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello world"
        usage = [e for e in events if isinstance(e, Usage)][0]
        assert (usage.input_tokens, usage.output_tokens) == (10, 5)
        assert events[-1] == Done("stop")

    @pytest.mark.asyncio
    async def test_openai_text(self):
        with mock_stream(lambda req: httpx.Response(200, content=OPENAI_TEXT)):
            events = await collect(
                stream_chat(
                    model="gpt-4o",
                    system="sys",
                    messages=[user_message("hi")],
                    tools=[],
                    api_key="k",
                    provider="openai",
                )
            )
        assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello world"
        usage = [e for e in events if isinstance(e, Usage)][0]
        assert (usage.input_tokens, usage.output_tokens) == (10, 5)
        assert events[-1] == Done("stop")

    @pytest.mark.asyncio
    async def test_base_url_uses_openai_compatible(self):
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, content=OPENAI_TEXT)

        with mock_stream(handler):
            events = await collect(
                stream_chat(
                    model="claude-sonnet-4",
                    system="sys",
                    messages=[user_message("hi")],
                    tools=[],
                    api_key="k",
                    provider="anthropic",
                    base_url="https://litellm.internal/v1/",
                )
            )
        assert seen["url"] == "https://litellm.internal/v1/chat/completions"
        assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello world"

    @pytest.mark.asyncio
    async def test_base_url_guard_runs_before_every_request(self):
        """The installed SSRF guard is re-checked on each custom-endpoint call."""
        from sessionfs.judge import providers

        checked: list[str] = []

        async def guard(url: str) -> None:
            checked.append(url)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=OPENAI_TEXT)

        providers.set_base_url_guard(guard)
        try:
            with mock_stream(handler):
                for _ in range(2):
                    await collect(
                        stream_chat(
                            model="m",
                            system="sys",
                            messages=[user_message("hi")],
                            tools=[],
                            api_key="k",
                            base_url="https://llm.example.com/v1",
                        )
                    )
        finally:
            providers.set_base_url_guard(None)
        assert checked == ["https://llm.example.com/v1"] * 2

    @pytest.mark.asyncio
    async def test_base_url_guard_refusal_sends_nothing(self):
        from sessionfs.judge import providers

        sent: list[str] = []

        async def guard(url: str) -> None:
            raise ValueError("resolves to 10.0.0.5")

        def handler(request: httpx.Request) -> httpx.Response:
            sent.append(str(request.url))
            return httpx.Response(200, content=OPENAI_TEXT)

        providers.set_base_url_guard(guard)
        try:
            with mock_stream(handler):
                with pytest.raises(ProviderError) as info:
                    await collect(
                        stream_chat(
                            model="m",
                            system="sys",
                            messages=[user_message("hi")],
                            tools=[],
                            api_key="secret-key",
                            base_url="https://rebind.example.com/v1",
                        )
                    )
        finally:
            providers.set_base_url_guard(None)
        assert sent == []
        assert info.value.status == 400
        assert "10.0.0.5" not in info.value.message

    @pytest.mark.asyncio
    async def test_openrouter_sends_headers(self):
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["referer"] = request.headers.get("HTTP-Referer")
            seen["title"] = request.headers.get("X-Title")
            seen["auth"] = request.headers.get("Authorization")
            return httpx.Response(200, content=OPENAI_TEXT)

        with mock_stream(handler):
            await collect(
                stream_chat(
                    model="anthropic/claude-sonnet-4",
                    system="sys",
                    messages=[user_message("hi")],
                    tools=[],
                    api_key="k",
                    provider="openrouter",
                )
            )
        assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
        assert seen["referer"] == "https://sessionfs.dev"
        assert seen["title"] == "SessionFS"
        assert seen["auth"] == "Bearer k"

    @pytest.mark.asyncio
    async def test_google_text(self):
        seen: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            return httpx.Response(200, content=GOOGLE_TEXT)

        with mock_stream(handler):
            events = await collect(
                stream_chat(
                    model="gemini-2.5-pro",
                    system="sys",
                    messages=[user_message("hi")],
                    tools=[],
                    api_key="k",
                    provider="google",
                )
            )
        assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "Hello world"
        assert seen["url"].endswith(":streamGenerateContent?alt=sse")
        assert events[-1] == Done("stop")


# ---------------------------------------------------------------------------
# Tool-call parsing
# ---------------------------------------------------------------------------

ANTHROPIC_TOOL = sse(
    {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 0}}},
    {
        "type": "content_block_start",
        "index": 0,
        "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}},
    },
    {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"loc"}},
    {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "ation\":\"Paris\"}"}},
    {"type": "content_block_stop", "index": 0},
    {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 8}},
    {"type": "message_stop"},
)

OPENAI_TOOL = sse(
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
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"loc"}}]}, "finish_reason": None}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "ation\":\"Paris\"}"}}]}, "finish_reason": None}]},
    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 8}},
    DONE,
)

OPENAI_TWO_TOOLS = sse(
    {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {"index": 0, "id": "call_1", "type": "function", "function": {"name": "get_weather", "arguments": ""}},
                        {"index": 1, "id": "call_2", "type": "function", "function": {"name": "get_time", "arguments": ""}},
                    ]
                },
                "finish_reason": None,
            }
        ]
    },
    {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "{\"location\":\"Paris\"}"}}]}, "finish_reason": None}]},
    {"choices": [{"delta": {"tool_calls": [{"index": 1, "function": {"arguments": "{\"tz\":\"UTC\"}"}}]}, "finish_reason": None}]},
    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 12}},
    DONE,
)


class TestToolCallParsing:
    @pytest.mark.asyncio
    async def test_anthropic_tool_call_split_json(self):
        with mock_stream(lambda req: httpx.Response(200, content=ANTHROPIC_TOOL)):
            events = await collect(
                stream_chat(
                    model="claude-sonnet-4",
                    system="sys",
                    messages=[user_message("weather?")],
                    tools=[WEATHER_TOOL],
                    api_key="k",
                    provider="anthropic",
                )
            )
        calls = [e.tool_call for e in events if isinstance(e, ToolCallEvent)]
        assert len(calls) == 1
        assert calls[0] == ToolCall("toolu_1", "get_weather", {"location": "Paris"})
        assert events[-1] == Done("tool_calls")

    @pytest.mark.asyncio
    async def test_openai_tool_call_split_json(self):
        with mock_stream(lambda req: httpx.Response(200, content=OPENAI_TOOL)):
            events = await collect(
                stream_chat(
                    model="gpt-4o",
                    system="sys",
                    messages=[user_message("weather?")],
                    tools=[WEATHER_TOOL],
                    api_key="k",
                    provider="openai",
                )
            )
        calls = [e.tool_call for e in events if isinstance(e, ToolCallEvent)]
        assert len(calls) == 1
        assert calls[0] == ToolCall("call_1", "get_weather", {"location": "Paris"})
        assert events[-1] == Done("tool_calls")

    @pytest.mark.asyncio
    async def test_openai_two_parallel_tool_calls(self):
        with mock_stream(lambda req: httpx.Response(200, content=OPENAI_TWO_TOOLS)):
            events = await collect(
                stream_chat(
                    model="gpt-4o",
                    system="sys",
                    messages=[user_message("weather and time?")],
                    tools=[WEATHER_TOOL],
                    api_key="k",
                    provider="openai",
                )
            )
        calls = [e.tool_call for e in events if isinstance(e, ToolCallEvent)]
        assert calls == [
            ToolCall("call_1", "get_weather", {"location": "Paris"}),
            ToolCall("call_2", "get_time", {"tz": "UTC"}),
        ]


# ---------------------------------------------------------------------------
# Request body translation
# ---------------------------------------------------------------------------

ANTHROPIC_TOOL_THEN_TEXT = [
    ANTHROPIC_TOOL,
    sse(
        {"type": "message_start", "message": {"usage": {"input_tokens": 20, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "22C"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}},
        {"type": "message_stop"},
    ),
]

OPENAI_TOOL_THEN_TEXT = [
    OPENAI_TOOL,
    sse(
        {"choices": [{"delta": {"content": "22C"}, "finish_reason": None}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 3}},
        DONE,
    ),
]


class TestRequestBody:
    @pytest.mark.asyncio
    async def test_anthropic_tools_and_tool_result_second_turn(self):
        handler, requests = capturing_handler(ANTHROPIC_TOOL_THEN_TEXT)

        async def execute_tool(call: ToolCall) -> ToolResult:
            return ToolResult(call.id, "22C", is_error=False)

        with mock_stream(handler):
            await collect(
                run_agent(
                    model="claude-sonnet-4",
                    system="sys",
                    messages=[user_message("weather?")],
                    tools=[WEATHER_TOOL],
                    execute_tool=execute_tool,
                    api_key="k",
                    provider="anthropic",
                )
            )

        assert len(requests) == 2
        first = requests[0]
        assert first["tools"] == [
            {
                "name": "get_weather",
                "description": "Get the weather for a location",
                "input_schema": WEATHER_TOOL.input_schema,
            }
        ]
        assert first["stream"] is True
        # First turn: only the user message.
        assert first["messages"] == [{"role": "user", "content": [{"type": "text", "text": "weather?"}]}]

        second = requests[1]
        # Second turn: assistant tool_use followed by a user tool_result block.
        assistant_msg = second["messages"][1]
        assert assistant_msg["role"] == "assistant"
        assert assistant_msg["content"] == [
            {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {"location": "Paris"}}
        ]
        result_msg = second["messages"][2]
        assert result_msg == {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "22C"}],
        }

    @pytest.mark.asyncio
    async def test_openai_tools_and_tool_result_second_turn(self):
        handler, requests = capturing_handler(OPENAI_TOOL_THEN_TEXT)

        async def execute_tool(call: ToolCall) -> ToolResult:
            return ToolResult(call.id, "22C", is_error=False)

        with mock_stream(handler):
            await collect(
                run_agent(
                    model="gpt-4o",
                    system="sys",
                    messages=[user_message("weather?")],
                    tools=[WEATHER_TOOL],
                    execute_tool=execute_tool,
                    api_key="k",
                    provider="openai",
                )
            )

        assert len(requests) == 2
        first = requests[0]
        assert first["tools"] == [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "description": "Get the weather for a location",
                    "parameters": WEATHER_TOOL.input_schema,
                },
            }
        ]
        assert first["stream_options"] == {"include_usage": True}
        assert "max_completion_tokens" in first
        assert first["messages"] == [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "weather?"},
        ]

        second = requests[1]
        # [system, user, assistant(tool_calls), tool(result)]
        assistant_msg = second["messages"][2]
        assert assistant_msg["role"] == "assistant"
        assert assistant_msg["tool_calls"] == [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": json.dumps({"location": "Paris"})},
            }
        ]
        assert second["messages"][3] == {"role": "tool", "tool_call_id": "call_1", "content": "22C"}

    @pytest.mark.asyncio
    async def test_openrouter_uses_max_tokens(self):
        handler, requests = capturing_handler([OPENAI_TEXT])

        with mock_stream(handler):
            await collect(
                stream_chat(
                    model="anthropic/claude-sonnet-4",
                    system="sys",
                    messages=[user_message("hi")],
                    tools=[],
                    api_key="k",
                    provider="openrouter",
                )
            )
        assert "max_tokens" in requests[0]
        assert "max_completion_tokens" not in requests[0]

    @pytest.mark.asyncio
    async def test_google_tool_translation(self):
        handler, requests = capturing_handler([GOOGLE_TEXT])

        with mock_stream(handler):
            await collect(
                stream_chat(
                    model="gemini-2.5-pro",
                    system="sys",
                    messages=[user_message("hi")],
                    tools=[WEATHER_TOOL],
                    api_key="k",
                    provider="google",
                )
            )
        body = requests[0]
        assert body["tools"] == [
            {
                "functionDeclarations": [
                    {
                        "name": "get_weather",
                        "description": "Get the weather for a location",
                        "parameters": WEATHER_TOOL.input_schema,
                    }
                ]
            }
        ]
        assert body["system_instruction"] == {"parts": [{"text": "sys"}]}

    @pytest.mark.asyncio
    async def test_reasoning_model_omits_temperature(self):
        handler, requests = capturing_handler([OPENAI_TEXT])

        with mock_stream(handler):
            await collect(
                stream_chat(
                    model="o3-mini",
                    system="sys",
                    messages=[user_message("hi")],
                    tools=[],
                    api_key="k",
                    provider="openai",
                    temperature=0.7,
                )
            )
        body = requests[0]
        assert "temperature" not in body
        assert body.get("reasoning_effort") == "medium"


# ---------------------------------------------------------------------------
# run_agent loop
# ---------------------------------------------------------------------------

class TestRunAgent:
    @pytest.mark.asyncio
    async def test_two_step_tool_then_final_text(self):
        handler, _ = capturing_handler(OPENAI_TOOL_THEN_TEXT)

        async def execute_tool(call: ToolCall) -> ToolResult:
            return ToolResult(call.id, "22C", is_error=False)

        with mock_stream(handler):
            events = await collect(
                run_agent(
                    model="gpt-4o",
                    system="sys",
                    messages=[user_message("weather?")],
                    tools=[WEATHER_TOOL],
                    execute_tool=execute_tool,
                    api_key="k",
                    provider="openai",
                )
            )

        texts = "".join(e.text for e in events if isinstance(e, TextDelta))
        assert texts == "22C"
        results = [e for e in events if isinstance(e, ToolResultEvent)]
        assert len(results) == 1
        assert results[0].result == ToolResult("call_1", "22C", is_error=False)
        # Exactly one Done is emitted, and it is the final event.
        dones = [e for e in events if isinstance(e, Done)]
        assert dones == [Done("stop")]
        assert events[-1] == Done("stop")

    @pytest.mark.asyncio
    async def test_max_steps_stops_and_runs_tool_once(self):
        handler, _ = capturing_handler([OPENAI_TOOL, OPENAI_TOOL])
        executed: list[ToolCall] = []

        async def execute_tool(call: ToolCall) -> ToolResult:
            executed.append(call)
            return ToolResult(call.id, "ok")

        with mock_stream(handler):
            events = await collect(
                run_agent(
                    model="gpt-4o",
                    system="sys",
                    messages=[user_message("weather?")],
                    tools=[WEATHER_TOOL],
                    execute_tool=execute_tool,
                    api_key="k",
                    provider="openai",
                    max_steps=1,
                )
            )
        assert events[-1] == Done("max_steps")
        assert len(executed) == 1

    @pytest.mark.asyncio
    async def test_token_budget_stops_before_tool(self):
        handler, _ = capturing_handler([OPENAI_TOOL])
        executed: list[ToolCall] = []

        async def execute_tool(call: ToolCall) -> ToolResult:
            executed.append(call)
            return ToolResult(call.id, "ok")

        with mock_stream(handler):
            events = await collect(
                run_agent(
                    model="gpt-4o",
                    system="sys",
                    messages=[user_message("weather?")],
                    tools=[WEATHER_TOOL],
                    execute_tool=execute_tool,
                    api_key="k",
                    provider="openai",
                    max_total_tokens=10,  # 10 prompt + 8 completion = 18 >= 10
                )
            )
        assert events[-1] == Done("token_budget")
        assert executed == []

    @pytest.mark.asyncio
    async def test_deadline_stops_before_tool(self):
        handler, _ = capturing_handler([OPENAI_TOOL])
        executed: list[ToolCall] = []

        async def execute_tool(call: ToolCall) -> ToolResult:
            executed.append(call)
            return ToolResult(call.id, "ok")

        with mock_stream(handler):
            events = await collect(
                run_agent(
                    model="gpt-4o",
                    system="sys",
                    messages=[user_message("weather?")],
                    tools=[WEATHER_TOOL],
                    execute_tool=execute_tool,
                    api_key="k",
                    provider="openai",
                    deadline_seconds=0,
                )
            )
        assert events[-1] == Done("deadline")
        assert executed == []

    @pytest.mark.asyncio
    async def test_execute_tool_raises_becomes_error_result(self):
        handler, _ = capturing_handler(OPENAI_TOOL_THEN_TEXT)

        async def execute_tool(call: ToolCall) -> ToolResult:
            raise RuntimeError("boom with secrets")

        with mock_stream(handler):
            events = await collect(
                run_agent(
                    model="gpt-4o",
                    system="sys",
                    messages=[user_message("weather?")],
                    tools=[WEATHER_TOOL],
                    execute_tool=execute_tool,
                    api_key="k",
                    provider="openai",
                )
            )
        results = [e for e in events if isinstance(e, ToolResultEvent)]
        assert len(results) == 1
        assert results[0].result.is_error is True
        assert results[0].result.content == "Tool execution failed."
        # The loop continues to the final text.
        assert events[-1] == Done("stop")
        assert "".join(e.text for e in events if isinstance(e, TextDelta)) == "22C"


# ---------------------------------------------------------------------------
# Errors and malformed input
# ---------------------------------------------------------------------------

class TestErrors:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("status", [401, 429, 500])
    async def test_provider_error_no_key_leak(self, status, caplog):
        key = "sk-super-secret-key-123"

        def handler(request: httpx.Request) -> httpx.Response:
            # A buggy gateway could echo the key back in its error body.
            return httpx.Response(
                status,
                json={"error": {"message": f"failed for key {key}"}},
            )

        with mock_stream(handler):
            with pytest.raises(ProviderError) as exc_info:
                await collect(
                    stream_chat(
                        model="gpt-4o",
                        system="sys",
                        messages=[user_message("hi")],
                        tools=[],
                        api_key=key,
                        provider="openai",
                    )
                )
        assert exc_info.value.status == status
        assert key not in str(exc_info.value)
        assert key not in exc_info.value.message
        assert key not in caplog.text

    @pytest.mark.asyncio
    async def test_anthropic_malformed_tool_json_sets_invalid_flag(self):
        malformed = sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 0}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}},
            },
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"location\": "}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        )

        with mock_stream(lambda req: httpx.Response(200, content=malformed)):
            events = await collect(
                stream_chat(
                    model="claude-sonnet-4",
                    system="sys",
                    messages=[user_message("hi")],
                    tools=[WEATHER_TOOL],
                    api_key="k",
                    provider="anthropic",
                )
            )
        calls = [e.tool_call for e in events if isinstance(e, ToolCallEvent)]
        assert len(calls) == 1
        assert calls[0].id == "toolu_1"
        assert calls[0].name == "get_weather"
        assert calls[0].arguments == {}
        assert calls[0].invalid_arguments is True

    @pytest.mark.asyncio
    async def test_openai_malformed_tool_json_sets_invalid_flag(self):
        malformed = sse(
            {
                "choices": [
                    {
                        "delta": {
                            "tool_calls": [
                                {"index": 0, "id": "call_1", "function": {"name": "get_weather", "arguments": "{\"location\": "}}
                            ]
                        },
                        "finish_reason": None,
                    }
                ]
            },
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
            DONE,
        )

        with mock_stream(lambda req: httpx.Response(200, content=malformed)):
            events = await collect(
                stream_chat(
                    model="gpt-4o",
                    system="sys",
                    messages=[user_message("hi")],
                    tools=[WEATHER_TOOL],
                    api_key="k",
                    provider="openai",
                )
            )
        calls = [e.tool_call for e in events if isinstance(e, ToolCallEvent)]
        assert len(calls) == 1
        assert calls[0].id == "call_1"
        assert calls[0].name == "get_weather"
        assert calls[0].arguments == {}
        assert calls[0].invalid_arguments is True

    @pytest.mark.asyncio
    async def test_unsupported_provider_raises(self):
        with pytest.raises(ValueError, match="Unsupported provider"):
            await collect(
                stream_chat(
                    model="x",
                    system="sys",
                    messages=[user_message("hi")],
                    tools=[],
                    api_key="k",
                    provider="bogus",
                )
            )

    @pytest.mark.asyncio
    async def test_anthropic_mid_stream_error_raises(self):
        body = sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 1, "output_tokens": 0}}},
            {"type": "error", "error": {"type": "overloaded_error", "message": "overloaded"}},
        )

        with mock_stream(lambda req: httpx.Response(200, content=body)):
            with pytest.raises(ProviderError) as exc_info:
                await collect(
                    stream_chat(
                        model="claude-sonnet-4",
                        system="sys",
                        messages=[user_message("hi")],
                        tools=[],
                        api_key="k",
                        provider="anthropic",
                    )
                )
        assert exc_info.value.status == 502
        assert exc_info.value.message == "overloaded"

    @pytest.mark.asyncio
    async def test_openai_mid_stream_error_raises(self):
        key = "sk-super-secret-key-123"
        body = sse(
            {"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]},
            {"error": {"message": f"overloaded for {key}", "code": 429}},
        )

        with mock_stream(lambda req: httpx.Response(200, content=body)):
            with pytest.raises(ProviderError) as exc_info:
                await collect(
                    stream_chat(
                        model="gpt-4o",
                        system="sys",
                        messages=[user_message("hi")],
                        tools=[],
                        api_key=key,
                        provider="openai",
                    )
                )
        assert exc_info.value.status == 429
        assert "overloaded" in exc_info.value.message
        assert key not in exc_info.value.message

    @pytest.mark.asyncio
    async def test_anthropic_truncated_stream_raises(self):
        body = sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 1, "output_tokens": 0}}},
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "hi"}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 1}},
        )

        with mock_stream(lambda req: httpx.Response(200, content=body)):
            with pytest.raises(ProviderError) as exc_info:
                await collect(
                    stream_chat(
                        model="claude-sonnet-4",
                        system="sys",
                        messages=[user_message("hi")],
                        tools=[],
                        api_key="k",
                        provider="anthropic",
                    )
                )
        assert exc_info.value.status == 502

    @pytest.mark.asyncio
    async def test_openai_truncated_stream_raises(self):
        body = sse(
            {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]},
            DONE,
        )

        with mock_stream(lambda req: httpx.Response(200, content=body)):
            with pytest.raises(ProviderError) as exc_info:
                await collect(
                    stream_chat(
                        model="gpt-4o",
                        system="sys",
                        messages=[user_message("hi")],
                        tools=[],
                        api_key="k",
                        provider="openai",
                    )
                )
        assert exc_info.value.status == 502


# ---------------------------------------------------------------------------
# Low-item translations
# ---------------------------------------------------------------------------

class TestTranslations:
    @pytest.mark.asyncio
    async def test_anthropic_skips_empty_assistant_and_user(self):
        handler, requests = capturing_handler([ANTHROPIC_TEXT])

        messages = [
            {"role": "user", "content": ""},
            {"role": "assistant", "content": "", "tool_calls": []},
            {"role": "user", "content": "real question"},
        ]

        with mock_stream(handler):
            await collect(
                stream_chat(
                    model="claude-sonnet-4",
                    system="sys",
                    messages=messages,
                    tools=[],
                    api_key="k",
                    provider="anthropic",
                )
            )
        assert requests[0]["messages"] == [
            {"role": "user", "content": [{"type": "text", "text": "real question"}]}
        ]

    @pytest.mark.asyncio
    async def test_openai_tool_result_error_prefix(self):
        handler, requests = capturing_handler([OPENAI_TEXT])

        messages = [
            user_message("hi"),
            {"role": "assistant", "content": "", "tool_calls": [ToolCall("call_1", "get_weather", {})]},
            {"role": "tool", "tool_call_id": "call_1", "content": "boom", "is_error": True},
        ]

        with mock_stream(handler):
            await collect(
                stream_chat(
                    model="gpt-4o",
                    system="sys",
                    messages=messages,
                    tools=[WEATHER_TOOL],
                    api_key="k",
                    provider="openai",
                )
            )
        tool_msg = [m for m in requests[0]["messages"] if m["role"] == "tool"][0]
        assert tool_msg["content"] == "Error: boom"

    @pytest.mark.asyncio
    async def test_google_tool_result_error_shape(self):
        handler, requests = capturing_handler([GOOGLE_TEXT])

        messages = [
            user_message("hi"),
            {"role": "assistant", "content": "", "tool_calls": [ToolCall("call_x", "get_weather", {})]},
            {"role": "tool", "tool_call_id": "call_x", "content": "boom", "is_error": True},
        ]

        with mock_stream(handler):
            await collect(
                stream_chat(
                    model="gemini-2.5-pro",
                    system="sys",
                    messages=messages,
                    tools=[WEATHER_TOOL],
                    api_key="k",
                    provider="google",
                )
            )
        contents = requests[0]["contents"]
        response_parts = [
            part["functionResponse"]["response"]
            for msg in contents
            for part in msg["parts"]
            if isinstance(part, dict) and "functionResponse" in part
        ]
        assert response_parts == [{"error": "boom"}]


# ---------------------------------------------------------------------------
# run_agent deadline, per-turn cap, single Done and invalid arguments
# ---------------------------------------------------------------------------

OPENAI_THREE_TOOLS = sse(
    {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {"index": 0, "id": "call_0", "type": "function", "function": {"name": "tool_a", "arguments": "{}"}},
                        {"index": 1, "id": "call_1", "type": "function", "function": {"name": "tool_b", "arguments": "{}"}},
                        {"index": 2, "id": "call_2", "type": "function", "function": {"name": "tool_c", "arguments": "{}"}},
                    ]
                },
                "finish_reason": None,
            }
        ]
    },
    {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
    {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 8}},
    DONE,
)

OPENAI_MAX_TOKENS = sse(
    {"choices": [{"delta": {"content": "partial"}, "finish_reason": None}]},
    {"choices": [{"delta": {}, "finish_reason": "length"}]},
    {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}},
    DONE,
)

GOOGLE_WEATHER_CALL = sse(
    {
        "candidates": [
            {
                "content": {"parts": [{"functionCall": {"name": "get_weather", "args": {"location": "Paris"}}}], "role": "model"},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    }
)

GOOGLE_TIME_CALL = sse(
    {
        "candidates": [
            {
                "content": {"parts": [{"functionCall": {"name": "get_time", "args": {"tz": "UTC"}}}], "role": "model"},
                "finishReason": "STOP",
                "index": 0,
            }
        ],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
    }
)


def _function_response_names(body: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for msg in body.get("contents", []):
        for part in msg.get("parts", []):
            if isinstance(part, dict) and "functionResponse" in part:
                names.append(part["functionResponse"]["name"])
    return names


class TestRunAgentLimits:
    @pytest.mark.asyncio
    async def test_deadline_enforced_during_stream(self):
        class _SlowStream(httpx.AsyncByteStream):
            def __init__(self, agen):
                self._agen = agen

            async def __aiter__(self):
                async for chunk in self._agen:
                    yield chunk

        async def handler(request: httpx.Request) -> httpx.Response:
            async def stream():
                yield b"data: " + json.dumps(
                    {"choices": [{"delta": {"content": "hi"}, "finish_reason": None}]}
                ).encode() + b"\n\n"
                await asyncio.sleep(1.0)
                yield b"data: " + json.dumps(
                    {"choices": [{"delta": {}, "finish_reason": "stop"}]}
                ).encode() + b"\n\n"

            return httpx.Response(
                200, stream=_SlowStream(stream()), headers={"content-type": "text/event-stream"}
            )

        async def execute_tool(call: ToolCall) -> ToolResult:
            return ToolResult(call.id, "ok")

        with mock_stream(handler):
            events = await collect(
                run_agent(
                    model="gpt-4o",
                    system="sys",
                    messages=[user_message("hi")],
                    tools=[],
                    execute_tool=execute_tool,
                    api_key="k",
                    provider="openai",
                    deadline_seconds=0.05,
                )
            )
        assert events[-1] == Done("deadline")

    @pytest.mark.asyncio
    async def test_deadline_enforced_during_tool(self):
        handler, _ = capturing_handler([OPENAI_TOOL])
        executed: list[ToolCall] = []

        async def execute_tool(call: ToolCall) -> ToolResult:
            executed.append(call)
            await asyncio.sleep(1.0)
            return ToolResult(call.id, "ok")

        with mock_stream(handler):
            events = await collect(
                run_agent(
                    model="gpt-4o",
                    system="sys",
                    messages=[user_message("weather?")],
                    tools=[WEATHER_TOOL],
                    execute_tool=execute_tool,
                    api_key="k",
                    provider="openai",
                    deadline_seconds=0.05,
                )
            )
        assert events[-1] == Done("deadline")
        assert not any(isinstance(e, ToolResultEvent) for e in events)
        assert len(executed) == 1  # started but did not finish

    @pytest.mark.asyncio
    async def test_max_tool_calls_per_turn_caps_excess(self):
        handler, requests = capturing_handler([OPENAI_THREE_TOOLS, OPENAI_TEXT])
        executed: list[ToolCall] = []

        async def execute_tool(call: ToolCall) -> ToolResult:
            executed.append(call)
            return ToolResult(call.id, "ok")

        with mock_stream(handler):
            events = await collect(
                run_agent(
                    model="gpt-4o",
                    system="sys",
                    messages=[user_message("do things")],
                    tools=[WEATHER_TOOL],
                    execute_tool=execute_tool,
                    api_key="k",
                    provider="openai",
                    max_tool_calls_per_turn=2,
                )
            )

        results = [e for e in events if isinstance(e, ToolResultEvent)]
        assert [r.result.is_error for r in results] == [False, False, True]
        assert results[2].result.content == "Too many tool calls in one turn; at most 2 are run."
        assert len(executed) == 2
        # The second turn still carries a result for every requested call.
        tool_msgs = [m for m in requests[1]["messages"] if m["role"] == "tool"]
        assert len(tool_msgs) == 3

    @pytest.mark.asyncio
    async def test_final_done_is_max_tokens_when_turn_hits_output_limit(self):
        handler, _ = capturing_handler([OPENAI_MAX_TOKENS])

        async def execute_tool(call: ToolCall) -> ToolResult:
            return ToolResult(call.id, "ok")

        with mock_stream(handler):
            events = await collect(
                run_agent(
                    model="gpt-4o",
                    system="sys",
                    messages=[user_message("hi")],
                    tools=[],
                    execute_tool=execute_tool,
                    api_key="k",
                    provider="openai",
                )
            )
        dones = [e for e in events if isinstance(e, Done)]
        assert dones == [Done("max_tokens")]

    @pytest.mark.asyncio
    async def test_google_tool_ids_unique_across_turns(self):
        handler, requests = capturing_handler(
            [GOOGLE_WEATHER_CALL, GOOGLE_TIME_CALL, GOOGLE_TEXT]
        )

        async def execute_tool(call: ToolCall) -> ToolResult:
            return ToolResult(call.id, f"result-{call.name}")

        with mock_stream(handler):
            await collect(
                run_agent(
                    model="gemini-2.5-pro",
                    system="sys",
                    messages=[user_message("go")],
                    tools=[WEATHER_TOOL],
                    execute_tool=execute_tool,
                    api_key="k",
                    provider="google",
                )
            )

        assert len(requests) == 3
        # Turn 2's request carries turn 1's result under the right function name.
        assert _function_response_names(requests[1]) == ["get_weather"]
        # Turn 3's request carries both results under their own function names.
        assert _function_response_names(requests[2]) == ["get_weather", "get_time"]

    @pytest.mark.asyncio
    async def test_run_agent_invalid_arguments_yields_error_result(self):
        malformed = sse(
            {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 0}}},
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "tool_use", "id": "toolu_1", "name": "get_weather", "input": {}},
            },
            {"type": "content_block_delta", "index": 0, "delta": {"type": "input_json_delta", "partial_json": "{\"location\": "}},
            {"type": "content_block_stop", "index": 0},
            {"type": "message_delta", "delta": {"stop_reason": "tool_use"}, "usage": {"output_tokens": 5}},
            {"type": "message_stop"},
        )
        handler, _ = capturing_handler([malformed, ANTHROPIC_TEXT])
        executed: list[ToolCall] = []

        async def execute_tool(call: ToolCall) -> ToolResult:
            executed.append(call)
            return ToolResult(call.id, "ok")

        with mock_stream(handler):
            events = await collect(
                run_agent(
                    model="claude-sonnet-4",
                    system="sys",
                    messages=[user_message("hi")],
                    tools=[WEATHER_TOOL],
                    execute_tool=execute_tool,
                    api_key="k",
                    provider="anthropic",
                )
            )

        results = [e for e in events if isinstance(e, ToolResultEvent)]
        assert len(results) == 1
        assert results[0].result == ToolResult("toolu_1", "Arguments were not valid JSON.", is_error=True)
        assert executed == []
        assert events[-1] == Done("stop")
