"""Streaming, multi-turn tool-calling layer on top of the BYOK providers.

This module extends ``providers.py`` (single-shot ``call_llm``) with a neutral
streaming interface for a future "session helper" agent. It reuses the existing
provider detection, URLs, and reasoning-model rules; it adds no new dependencies
and changes no existing behaviour.

Neutral message shape
---------------------
A conversation is a list of plain dictionaries. Every message is ONE of:

    {"role": "user", "content": "<text>"}
    {"role": "assistant", "content": "<text>", "tool_calls": [ToolCall, ...]}
    {"role": "tool", "tool_call_id": "<id>", "content": "<text>", "is_error": bool}

``content`` is always a ``str`` (assistant text may be ``""`` when the turn is
tool-calls only). ``tool_calls`` appears only on assistant messages and is a
list of :class:`ToolCall` dataclasses. A tool result is represented by the
``tool`` role; :class:`ToolResult` is what ``execute_tool`` returns and is turned
into such a message with :func:`tool_result_message`.

Stream events (each turn, in order)
-----------------------------------
* :class:`TextDelta` — a piece of assistant text.
* :class:`ToolCallEvent` — emitted once a tool call's arguments are complete.
* :class:`Usage` — input/output token counts for the turn.
* :class:`Done` — end of the turn; ``stop_reason`` is ``"stop"``,
  ``"tool_calls"`` or ``"max_tokens"``.

``run_agent`` additionally emits :class:`ToolResultEvent` after each tool it
runs (or rejects) and exactly one final :class:`Done` whose ``stop_reason`` is
``"stop"``, ``"max_tokens"``, ``"max_steps"``, ``"token_budget"`` or
``"deadline"``.

If the model emits tool-call arguments that are not valid JSON, the turn emits a
:class:`ToolCallEvent` whose :class:`ToolCall` carries ``arguments={}`` and
``invalid_arguments=True``. ``run_agent`` answers that call with an ``is_error``
:class:`ToolResult` instead of executing it. :class:`ToolArgumentsError` is kept
exported for compatibility but the stream functions no longer raise it.

Limits (fail closed)
--------------------
``run_agent`` stops *before* running a tool when any hard limit is reached:
``max_steps`` turns, ``max_total_tokens`` cumulative input+output tokens,
``max_tool_calls_per_turn`` tool calls in a single turn, or ``deadline_seconds``
wall clock (enforced inside each turn and around each tool, not just between
turns). Tool calls beyond ``max_tool_calls_per_turn`` in a turn receive an
``is_error`` result rather than running. When it stops for a limit it yields a
final ``Done`` (``"max_steps"``, ``"token_budget"``, ``"max_tokens"`` or
``"deadline"``) and never runs another tool.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable
from uuid import uuid4

import httpx

from sessionfs.judge.providers import (
    _ANTHROPIC_URL,
    _OPENAI_URL,
    _OPENROUTER_URL,
    _detect_provider,
    _is_reasoning_model,
)

logger = logging.getLogger("sessionfs.judge.agent_stream")

_GOOGLE_STREAM_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}"
    ":streamGenerateContent?alt=sse"
)

_TOOL_ERROR_MESSAGE = "Tool execution failed."


# ---------------------------------------------------------------------------
# Neutral types
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ToolSpec:
    """A tool available to the model, described by a JSON Schema object."""

    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass(frozen=True)
class ToolCall:
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any]
    invalid_arguments: bool = False


@dataclass(frozen=True)
class ToolResult:
    """The result of executing a tool call."""

    tool_call_id: str
    content: str
    is_error: bool = False


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolCallEvent:
    tool_call: ToolCall


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class Done:
    stop_reason: str


@dataclass(frozen=True)
class ToolResultEvent:
    call: ToolCall
    result: ToolResult


StreamEvent = TextDelta | ToolCallEvent | Usage | Done
AgentEvent = StreamEvent | ToolResultEvent

Message = dict[str, Any]


class ProviderError(Exception):
    """An HTTP error from a provider. Carries only a sanitised message."""

    def __init__(self, status: int, message: str) -> None:
        self.status = status
        self.message = message
        super().__init__(f"Provider error {status}: {message}")


class ToolArgumentsError(Exception):
    """The model emitted tool-call arguments that are not valid JSON."""

    def __init__(self, tool_call_id: str, tool_name: str) -> None:
        self.tool_call_id = tool_call_id
        self.tool_name = tool_name
        super().__init__(f"Malformed arguments JSON for tool {tool_name!r} ({tool_call_id})")


# ---------------------------------------------------------------------------
# Neutral message helpers
# ---------------------------------------------------------------------------

def user_message(content: str) -> Message:
    return {"role": "user", "content": content}


def assistant_message(content: str, tool_calls: list[ToolCall] | None = None) -> Message:
    return {"role": "assistant", "content": content, "tool_calls": tool_calls or []}


def tool_result_message(result: ToolResult) -> Message:
    return {
        "role": "tool",
        "tool_call_id": result.tool_call_id,
        "content": result.content,
        "is_error": result.is_error,
    }


# ---------------------------------------------------------------------------
# Client factory (patchable in tests)
# ---------------------------------------------------------------------------

def _make_client(timeout: float) -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=timeout)


async def _iter_sse_json(resp: httpx.Response) -> AsyncIterator[dict[str, Any]]:
    """Yield parsed JSON objects from ``data:`` lines of an SSE stream."""
    async for raw in resp.aiter_lines():
        line = raw.strip()
        if not line or line.startswith(":"):
            continue
        if not line.startswith("data:"):
            continue
        payload = line[len("data:") :].strip()
        if not payload or payload == "[DONE]":
            return
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            yield data


def _extract_error_message(raw: str) -> str:
    """Pull a short, human-readable message out of a provider error body."""
    try:
        data = json.loads(raw)
    except Exception:
        data = None
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            msg = err.get("message")
            if isinstance(msg, str) and msg:
                return msg
        elif isinstance(err, str) and err:
            return err
        msg = data.get("message")
        if isinstance(msg, str) and msg:
            return msg
    return raw.strip() or "unknown error"


def _sanitise_error_message(message: str, api_key: str) -> str:
    """Strip the API key and cap an error message before it is surfaced."""
    if api_key:
        message = message.replace(api_key, "***")
    if len(message) > 300:
        message = message[:300] + "..."
    return message


def _raise_stream_error(err: Any, api_key: str) -> None:
    """Raise a :class:`ProviderError` from a mid-stream error payload."""
    status = 502
    message = "unknown error"
    if isinstance(err, dict):
        code = err.get("code")
        if isinstance(code, int) or (isinstance(code, str) and code.isdigit()):
            status = int(code)
        msg = err.get("message")
        if isinstance(msg, str) and msg:
            message = msg
    elif isinstance(err, str) and err:
        message = err
    raise ProviderError(status, _sanitise_error_message(message, api_key))


async def _raise_provider_error(resp: httpx.Response, api_key: str) -> None:
    try:
        raw = (await resp.aread()).decode("utf-8", errors="replace")
    except Exception:
        raw = ""
    message = _extract_error_message(raw)
    raise ProviderError(resp.status_code, _sanitise_error_message(message, api_key))


# ---------------------------------------------------------------------------
# Wire-format translation
# ---------------------------------------------------------------------------

def _openai_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            tool_calls = m.get("tool_calls") or []
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
            if tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": c.id,
                        "type": "function",
                        "function": {
                            "name": c.name,
                            "arguments": json.dumps(c.arguments),
                        },
                    }
                    for c in tool_calls
                ]
            out.append(msg)
        elif role == "tool":
            content = m.get("content", "")
            if m.get("is_error"):
                content = f"Error: {content}"
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": m.get("tool_call_id", ""),
                    "content": content,
                }
            )
        else:  # user (and any unknown role) -> user
            out.append({"role": "user", "content": m.get("content", "")})
    return out


def _openai_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.input_schema,
            },
        }
        for t in tools
    ]


def _anthropic_messages(messages: list[Message]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    i = 0
    while i < len(messages):
        m = messages[i]
        role = m.get("role")
        if role == "tool":
            # Merge consecutive tool results into a single user message so the
            # Anthropic user/assistant alternation is preserved for parallel calls.
            blocks: list[dict[str, Any]] = []
            while i < len(messages) and messages[i].get("role") == "tool":
                tm = messages[i]
                block: dict[str, Any] = {
                    "type": "tool_result",
                    "tool_use_id": tm.get("tool_call_id", ""),
                    "content": tm.get("content", ""),
                }
                if tm.get("is_error"):
                    block["is_error"] = True
                blocks.append(block)
                i += 1
            out.append({"role": "user", "content": blocks})
            continue
        if role == "assistant":
            content_blocks: list[dict[str, Any]] = []
            text = m.get("content") or ""
            if text:
                content_blocks.append({"type": "text", "text": text})
            for c in m.get("tool_calls") or []:
                content_blocks.append(
                    {"type": "tool_use", "id": c.id, "name": c.name, "input": c.arguments}
                )
            # Anthropic rejects assistant messages with empty content blocks.
            if not content_blocks:
                i += 1
                continue
            out.append({"role": "assistant", "content": content_blocks})
        else:  # user
            text = m.get("content") or ""
            # Anthropic rejects empty text blocks.
            if not text:
                i += 1
                continue
            out.append({"role": "user", "content": [{"type": "text", "text": text}]})
        i += 1
    return out


def _anthropic_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.input_schema}
        for t in tools
    ]


def _google_messages(messages: list[Message]) -> list[dict[str, Any]]:
    # Google functionResponse parts need the function *name*, which the neutral
    # tool message does not carry; resolve it from the preceding assistant calls.
    name_by_id: dict[str, str] = {}
    for m in messages:
        if m.get("role") == "assistant":
            for c in m.get("tool_calls") or []:
                name_by_id[c.id] = c.name

    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            parts: list[dict[str, Any]] = []
            text = m.get("content") or ""
            if text:
                parts.append({"text": text})
            for c in m.get("tool_calls") or []:
                parts.append({"functionCall": {"name": c.name, "args": c.arguments}})
            out.append({"role": "model", "parts": parts})
        elif role == "tool":
            tool_id = m.get("tool_call_id", "")
            if m.get("is_error"):
                response: dict[str, Any] = {"error": m.get("content", "")}
            else:
                response = {"result": m.get("content", "")}
            out.append(
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": name_by_id.get(tool_id, ""),
                                "response": response,
                            }
                        }
                    ],
                }
            )
        else:  # user
            out.append({"role": "user", "parts": [{"text": m.get("content", "")}]})
    return out


def _google_tools(tools: list[ToolSpec]) -> list[dict[str, Any]]:
    return [
        {
            "functionDeclarations": [
                {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.input_schema,
                }
                for t in tools
            ]
        }
    ]


# ---------------------------------------------------------------------------
# Stop-reason normalisation
# ---------------------------------------------------------------------------

def _anthropic_stop_reason(reason: str | None) -> str:
    if reason == "tool_use":
        return "tool_calls"
    if reason == "max_tokens":
        return "max_tokens"
    return "stop"


def _openai_stop_reason(reason: str | None) -> str:
    if reason == "tool_calls":
        return "tool_calls"
    if reason in ("length", "max_tokens"):
        return "max_tokens"
    return "stop"


def _google_stop_reason(reason: str | None, has_function_calls: bool) -> str:
    if has_function_calls:
        return "tool_calls"
    if reason == "MAX_TOKENS":
        return "max_tokens"
    return "stop"


# ---------------------------------------------------------------------------
# Per-provider streaming turns
# ---------------------------------------------------------------------------

async def _stream_openai_compatible(
    *,
    url: str,
    model: str,
    system: str,
    messages: list[Message],
    tools: list[ToolSpec],
    api_key: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
    openrouter: bool,
) -> AsyncIterator[StreamEvent]:
    url = url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = f"{url}/chat/completions"

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if openrouter:
        headers["HTTP-Referer"] = "https://sessionfs.dev"
        headers["X-Title"] = "SessionFS"

    base_model = model.split("/")[-1] if "/" in model else model
    token_field = "max_tokens" if openrouter else "max_completion_tokens"
    body: dict[str, Any] = {
        "model": model,
        "messages": [{"role": "system", "content": system}, *_openai_messages(messages)],
        "stream": True,
        "stream_options": {"include_usage": True},
        token_field: max_tokens,
    }
    if _is_reasoning_model(base_model):
        body["reasoning"] = {"effort": "medium"}
    else:
        body["temperature"] = temperature
    if tools:
        body["tools"] = _openai_tools(tools)

    tool_frags: dict[int, dict[str, str]] = {}
    input_tokens = 0
    output_tokens = 0
    finish_reason: str | None = None

    async with _make_client(timeout) as client:
        async with client.stream("POST", url, json=body, headers=headers) as resp:
            if resp.status_code >= 400:
                await _raise_provider_error(resp, api_key)
            async for data in _iter_sse_json(resp):
                if "error" in data:
                    _raise_stream_error(data["error"], api_key)
                choices = data.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    if delta.get("content"):
                        yield TextDelta(delta["content"])
                    for frag in delta.get("tool_calls") or []:
                        idx = frag.get("index", 0)
                        slot = tool_frags.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if frag.get("id"):
                            slot["id"] = frag["id"]
                        fn = frag.get("function") or {}
                        if fn.get("name"):
                            slot["name"] = fn["name"]
                        if fn.get("arguments"):
                            slot["args"] += fn["arguments"]
                    fr = choices[0].get("finish_reason")
                    if fr:
                        finish_reason = fr
                usage = data.get("usage")
                if usage:
                    input_tokens = usage.get("prompt_tokens", 0)
                    output_tokens = usage.get("completion_tokens", 0)

    if finish_reason is None:
        raise ProviderError(502, "Provider stream ended before a finish_reason.")

    for idx in sorted(tool_frags):
        slot = tool_frags[idx]
        raw_args = slot["args"].strip()
        arguments: dict[str, Any] = {}
        invalid_arguments = False
        try:
            arguments = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            invalid_arguments = True
        if not isinstance(arguments, dict):
            arguments = {}
        yield ToolCallEvent(
            ToolCall(slot["id"] or f"call_{idx}", slot["name"], arguments, invalid_arguments)
        )

    yield Usage(input_tokens, output_tokens)
    yield Done(_openai_stop_reason(finish_reason))


async def _stream_anthropic(
    *,
    model: str,
    system: str,
    messages: list[Message],
    tools: list[ToolSpec],
    api_key: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> AsyncIterator[StreamEvent]:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "system": system,
        "messages": _anthropic_messages(messages),
        "stream": True,
    }
    if tools:
        body["tools"] = _anthropic_tools(tools)

    tool_blocks: dict[int, dict[str, str]] = {}
    input_tokens = 0
    output_tokens = 0
    stop_reason: str | None = None
    saw_message_stop = False

    async with _make_client(timeout) as client:
        async with client.stream("POST", _ANTHROPIC_URL, json=body, headers=headers) as resp:
            if resp.status_code >= 400:
                await _raise_provider_error(resp, api_key)
            async for data in _iter_sse_json(resp):
                event_type = data.get("type")
                if event_type == "error":
                    _raise_stream_error(data.get("error"), api_key)
                elif event_type == "message_start":
                    message = data.get("message") or {}
                    usage = message.get("usage") or {}
                    input_tokens = usage.get("input_tokens", 0)
                elif event_type == "content_block_start":
                    block = data.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        idx = data.get("index", 0)
                        tool_blocks[idx] = {
                            "id": block.get("id", ""),
                            "name": block.get("name", ""),
                            "buf": "",
                        }
                elif event_type == "content_block_delta":
                    delta = data.get("delta") or {}
                    idx = data.get("index", 0)
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield TextDelta(delta["text"])
                    elif delta.get("type") == "input_json_delta":
                        slot = tool_blocks.get(idx)
                        if slot is not None and delta.get("partial_json"):
                            slot["buf"] += delta["partial_json"]
                elif event_type == "content_block_stop":
                    idx = data.get("index", 0)
                    slot = tool_blocks.pop(idx, None)
                    if slot is not None:
                        raw_args = slot["buf"].strip()
                        arguments: dict[str, Any] = {}
                        invalid_arguments = False
                        try:
                            arguments = json.loads(raw_args) if raw_args else {}
                        except json.JSONDecodeError:
                            invalid_arguments = True
                        if not isinstance(arguments, dict):
                            arguments = {}
                        yield ToolCallEvent(
                            ToolCall(slot["id"], slot["name"], arguments, invalid_arguments)
                        )
                elif event_type == "message_delta":
                    usage = data.get("usage") or {}
                    output_tokens = usage.get("output_tokens", 0)
                    delta = data.get("delta") or {}
                    stop_reason = delta.get("stop_reason")
                elif event_type == "message_stop":
                    saw_message_stop = True

    if not saw_message_stop:
        raise ProviderError(502, "Provider stream ended before message_stop.")

    yield Usage(input_tokens, output_tokens)
    yield Done(_anthropic_stop_reason(stop_reason))


async def _stream_google(
    *,
    model: str,
    system: str,
    messages: list[Message],
    tools: list[ToolSpec],
    api_key: str,
    temperature: float,
    max_tokens: int,
    timeout: float,
) -> AsyncIterator[StreamEvent]:
    url = _GOOGLE_STREAM_URL_TEMPLATE.format(model=model)
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    body: dict[str, Any] = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": _google_messages(messages),
        "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature},
    }
    if tools:
        body["tools"] = _google_tools(tools)

    input_tokens = 0
    output_tokens = 0
    finish_reason: str | None = None
    has_function_calls = False

    async with _make_client(timeout) as client:
        async with client.stream("POST", url, json=body, headers=headers) as resp:
            if resp.status_code >= 400:
                await _raise_provider_error(resp, api_key)
            async for data in _iter_sse_json(resp):
                candidates = data.get("candidates") or []
                if candidates:
                    content = candidates[0].get("content") or {}
                    parts = content.get("parts") or []
                    for part in parts:
                        if not isinstance(part, dict):
                            continue
                        if part.get("text"):
                            yield TextDelta(part["text"])
                        elif "functionCall" in part:
                            has_function_calls = True
                            fc = part["functionCall"]
                            args = fc.get("args") or {}
                            if not isinstance(args, dict):
                                args = {}
                            yield ToolCallEvent(
                                ToolCall(f"call_{uuid4().hex[:12]}", fc.get("name", ""), args)
                            )
                    fr = candidates[0].get("finishReason")
                    if fr:
                        finish_reason = fr
                usage = data.get("usageMetadata")
                if usage:
                    input_tokens = usage.get("promptTokenCount", 0)
                    output_tokens = usage.get("candidatesTokenCount", 0)

    yield Usage(input_tokens, output_tokens)
    yield Done(_google_stop_reason(finish_reason, has_function_calls))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def stream_chat(
    *,
    model: str,
    system: str,
    messages: list[Message],
    tools: list[ToolSpec],
    api_key: str,
    provider: str | None = None,
    base_url: str | None = None,
    temperature: float = 0,
    max_tokens: int = 4096,
    timeout: float = 120,
) -> AsyncIterator[StreamEvent]:
    """Stream a single model turn, translating neutral messages/tools and back.

    ``base_url`` forces the OpenAI-compatible chat-completions format (matching
    ``_call_openai_compatible``). Otherwise the provider is auto-detected from
    the model name when not given. Reasoning models omit temperature.
    """
    if provider is None:
        provider = _detect_provider(model)
    provider = provider.lower()

    if base_url:
        # Re-run the server's SSRF guard (installed into judge.providers by
        # create_app) before every request to a caller-supplied endpoint, not
        # only when the key was resolved: a multi-step turn can run for
        # minutes, and a hostname may start resolving internally in between.
        from sessionfs.judge.providers import _check_base_url

        try:
            await _check_base_url(base_url)
        except Exception as exc:  # noqa: BLE001 - never surface resolver detail
            raise ProviderError(400, "The configured LLM endpoint is not allowed.") from exc
        turn = _stream_openai_compatible(
            url=base_url,
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            openrouter=False,
        )
    elif provider == "anthropic":
        turn = _stream_anthropic(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    elif provider == "openai":
        turn = _stream_openai_compatible(
            url=_OPENAI_URL,
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            openrouter=False,
        )
    elif provider == "openrouter":
        turn = _stream_openai_compatible(
            url=_OPENROUTER_URL,
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
            openrouter=True,
        )
    elif provider == "google":
        turn = _stream_google(
            model=model,
            system=system,
            messages=messages,
            tools=tools,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout=timeout,
        )
    else:
        raise ValueError(f"Unsupported provider: {provider}")

    async for event in turn:
        yield event


async def _bounded_stream(
    agen: AsyncIterator[StreamEvent], deadline: float
) -> AsyncIterator[StreamEvent]:
    """Yield events from ``agen`` until the ``deadline`` (loop time) passes.

    On expiry the underlying generator is closed so its HTTP stream does not
    leak, and iteration stops without signalling which way it ended — the caller
    checks the clock again to decide whether the deadline was hit.
    """
    loop = asyncio.get_running_loop()
    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            break
        try:
            event = await asyncio.wait_for(agen.__anext__(), timeout=remaining)
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            break
        yield event
    try:
        await agen.aclose()
    except Exception:  # noqa: BLE001 - best-effort cleanup
        pass


async def run_agent(
    *,
    model: str,
    system: str,
    messages: list[Message],
    tools: list[ToolSpec],
    execute_tool: Callable[[ToolCall], Awaitable[ToolResult]],
    api_key: str,
    provider: str | None = None,
    base_url: str | None = None,
    max_steps: int = 8,
    max_total_tokens: int = 200_000,
    max_tool_calls_per_turn: int = 8,
    deadline_seconds: float = 300,
    temperature: float = 0,
    max_tokens: int = 4096,
    timeout: float = 120,
) -> AsyncIterator[AgentEvent]:
    """Run the agent loop: stream turns and execute requested tools until it stops.

    Yields each turn's text, tool-call and usage events, a :class:`ToolResultEvent`
    after each tool, and exactly one final :class:`Done`. Hard limits fail closed:
    a tool is never run once ``max_steps``, ``max_total_tokens``,
    ``max_tool_calls_per_turn`` or ``deadline_seconds`` (checked inside each turn
    and around each tool, not just between turns) has been reached. Tool exceptions
    and invalid tool arguments become an ``is_error`` :class:`ToolResult` with a
    generic message (arguments are never logged).
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    deadline = started + deadline_seconds
    conversation = [dict(m) for m in messages]
    total_tokens = 0
    steps = 0

    while True:
        if steps >= max_steps:
            yield Done("max_steps")
            return
        steps += 1

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        turn_stop_reason: str | None = None

        async for event in _bounded_stream(
            stream_chat(
                model=model,
                system=system,
                messages=conversation,
                tools=tools,
                api_key=api_key,
                provider=provider,
                base_url=base_url,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
            ),
            deadline,
        ):
            if isinstance(event, Done):
                turn_stop_reason = event.stop_reason
                continue
            yield event
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
            elif isinstance(event, ToolCallEvent):
                tool_calls.append(event.tool_call)
            elif isinstance(event, Usage):
                total_tokens += event.input_tokens + event.output_tokens

        conversation.append(assistant_message("".join(text_parts), tool_calls))

        if loop.time() >= deadline:
            yield Done("deadline")
            return

        if not tool_calls:
            yield Done("max_tokens" if turn_stop_reason == "max_tokens" else "stop")
            return

        # Hard limits — check before running any tool.
        if total_tokens >= max_total_tokens:
            yield Done("token_budget")
            return

        for call in tool_calls[:max_tool_calls_per_turn]:
            if call.invalid_arguments:
                result = ToolResult(call.id, "Arguments were not valid JSON.", is_error=True)
                yield ToolResultEvent(call, result)
                conversation.append(tool_result_message(result))
                continue

            remaining = deadline - loop.time()
            if remaining <= 0:
                yield Done("deadline")
                return
            try:
                result = await asyncio.wait_for(execute_tool(call), timeout=remaining)
            except asyncio.TimeoutError:
                yield Done("deadline")
                return
            except Exception as exc:  # noqa: BLE001 - fail closed, keep the loop alive
                logger.warning(
                    "Tool %r (%s) raised %s", call.name, call.id, type(exc).__name__
                )
                result = ToolResult(
                    tool_call_id=call.id, content=_TOOL_ERROR_MESSAGE, is_error=True
                )
            yield ToolResultEvent(call, result)
            conversation.append(tool_result_message(result))

        # Tool calls beyond the per-turn cap never run but still get a result so
        # the conversation stays valid for the provider.
        for call in tool_calls[max_tool_calls_per_turn:]:
            result = ToolResult(
                call.id,
                f"Too many tool calls in one turn; at most {max_tool_calls_per_turn} are run.",
                is_error=True,
            )
            yield ToolResultEvent(call, result)
            conversation.append(tool_result_message(result))
