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

Assistant messages may additionally carry a private ``reasoning_content`` key
(DeepSeek/Kimi) that is echoed back on follow-up tool-loop requests for the
providers that require it and is never surfaced to callers.

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

from sessionfs.judge import llm_errors
from sessionfs.judge.provider_registry import get_provider
from sessionfs.judge.providers import (
    _ANTHROPIC_URL,
    _chat_completions_url,
    _apply_sampling,
    _classify_param_error,
    _has_param,
    _detect_provider,
    _omitted_params,
    _remember_omission,
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
    # Accumulated reasoning_content for the turn (DeepSeek/Kimi). Never
    # surfaced to callers — it is carried so the tool loop can echo it back
    # on the assistant message in follow-up requests.
    reasoning_content: str = ""


@dataclass(frozen=True)
class ToolResultEvent:
    call: ToolCall
    result: ToolResult


StreamEvent = TextDelta | ToolCallEvent | Usage | Done
AgentEvent = StreamEvent | ToolResultEvent

Message = dict[str, Any]


class ProviderError(Exception):
    """An HTTP error from a provider. Carries only a sanitised message.

    ``code`` is the readable failure class from ``llm_errors`` (for example
    ``endpoint_not_api`` or ``invalid_key``) when one could be determined.
    """

    def __init__(
        self,
        status: int,
        message: str,
        code: str | None = None,
        *,
        definitive: bool = False,
        detail: str | None = None,
    ) -> None:
        self.status = status
        self.message = message
        self.code = code
        # A sanitised snippet of the provider's own JSON error message (or
        # our own text), safe to show; ``None`` when the body wasn't JSON.
        self.detail = detail
        # The reply is certain: for ``endpoint_not_api`` it proves the
        # address is not an API (405, a web page, a console host); for
        # ``invalid_key`` it is a 401 or a 400 that says the key is invalid.
        self.definitive = definitive
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


def assistant_message(
    content: str,
    tool_calls: list[ToolCall] | None = None,
    reasoning_content: str = "",
) -> Message:
    msg = {"role": "assistant", "content": content, "tool_calls": tool_calls or []}
    if reasoning_content:
        msg["reasoning_content"] = reasoning_content
    return msg


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


async def _iter_lines_capped(
    chunks: AsyncIterator[bytes], max_bytes: int | None
) -> AsyncIterator[str]:
    """Split a byte stream into lines, refusing more than ``max_bytes``
    (``None`` = no cap)."""
    import codecs

    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    buffer = ""
    total = 0
    async for chunk in chunks:
        total += len(chunk)
        if max_bytes is not None and total > max_bytes:
            raise ProviderError(
                502,
                "The provider's response was too large.",
                llm_errors.PROVIDER_ERROR,
                detail="The provider's response was too large.",
            )
        buffer += decoder.decode(chunk)
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            yield line.rstrip("\r")
    buffer += decoder.decode(b"", final=True)
    if buffer:
        yield buffer.rstrip("\r")


async def _iter_sse_json(
    resp: httpx.Response, max_bytes: int | None = None
) -> AsyncIterator[dict[str, Any]]:
    """Yield parsed JSON objects from ``data:`` lines of an SSE stream.

    ``max_bytes`` caps the whole body (the connection test sets it); ``None``
    keeps the historical unbounded read for long helper answers.
    """
    lines = (
        resp.aiter_lines()
        if max_bytes is None
        else _iter_lines_capped(resp.aiter_bytes(), max_bytes)
    )
    async for data in _sse_json_from_lines(lines):
        yield data


async def _iter_sse_json_bytes(
    chunks: AsyncIterator[bytes], max_bytes: int | None = None
) -> AsyncIterator[dict[str, Any]]:
    """``_iter_sse_json`` over an explicit byte iterator (already-peeked bytes)."""
    async for data in _sse_json_from_lines(_iter_lines_capped(chunks, max_bytes)):
        yield data


async def _prefixed(prefix: bytes, rest: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    if prefix:
        yield prefix
    async for chunk in rest:
        yield chunk


def _looks_like_sse(head: bytes) -> bool:
    text = head.lstrip()[:16]
    return text.startswith(b"data:") or text.startswith(b":") or text.startswith(b"event:")


async def _sse_json_from_lines(lines: AsyncIterator[str]) -> AsyncIterator[dict[str, Any]]:
    async for raw in lines:
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
    return llm_errors.extract_error_message(raw)


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
    clean = _sanitise_error_message(message, api_key)
    raise ProviderError(status, clean, detail=clean if message != "unknown error" else None)


_ERROR_BODY_MAX_BYTES = 64 * 1024
_JSON_COMPLETION_MAX_BYTES = 8 * 1024 * 1024


async def _json_labelled_body(
    resp: httpx.Response, max_bytes: int | None
) -> tuple[Any, AsyncIterator[dict[str, Any]] | None]:
    """Handle a 2xx body labelled ``application/json``.

    Returns ``(completion, None)`` for a non-streamed chat completion, or
    ``(None, sse_events)`` when the body is really an SSE stream mislabelled
    as JSON (some gateways do this). The stream case is detected from the
    first bytes, so it keeps streaming; a buffered body that isn't valid
    JSON is also parsed as SSE lines, exactly as the normal path would.
    """
    chunks = resp.aiter_bytes()
    head = b""
    async for chunk in chunks:
        head += chunk
        if head.strip():
            break
    if _looks_like_sse(head):
        return None, _iter_sse_json_bytes(_prefixed(head, chunks), max_bytes)
    limit = max_bytes or _JSON_COMPLETION_MAX_BYTES
    parts = [head]
    size = len(head)
    async for chunk in chunks:
        size += len(chunk)
        if size > limit:
            raise ProviderError(
                502,
                "The provider's response was too large.",
                llm_errors.PROVIDER_ERROR,
                detail="The provider's response was too large.",
            )
        parts.append(chunk)
    body = b"".join(parts)
    try:
        return json.loads(body), None
    except ValueError:

        async def single() -> AsyncIterator[bytes]:
            yield body

        return None, _iter_sse_json_bytes(single(), max_bytes)


async def _read_error_body(resp: httpx.Response) -> str:
    """Read (at most 64 KiB of) a provider error body as text, never raising."""
    chunks: list[bytes] = []
    size = 0
    try:
        async for chunk in resp.aiter_bytes():
            chunks.append(chunk)
            size += len(chunk)
            if size >= _ERROR_BODY_MAX_BYTES:
                break
    except Exception:
        pass
    return b"".join(chunks)[:_ERROR_BODY_MAX_BYTES].decode("utf-8", errors="replace")


async def _raise_provider_error(
    resp: httpx.Response,
    api_key: str,
    *,
    base_url: str | None = None,
    provider: str | None = None,
    body: str | None = None,
) -> None:
    """Raise a :class:`ProviderError` for an HTTP error response.

    ``base_url`` is the caller-supplied endpoint, if any. When that endpoint
    is not an API (404/405/HTML — e.g. a provider's web console), the message
    names the right API address instead of echoing the upstream body.
    """
    if body is None:
        body = await _read_error_body(resp)
    content_type = resp.headers.get("content-type", "")
    code = llm_errors.classify_http_failure(
        resp.status_code, body, content_type, base_url=base_url
    )
    definitive = False
    if code == llm_errors.ENDPOINT_NOT_API:
        definitive = llm_errors.is_definitive_not_api(
            resp.status_code, body, content_type, base_url=base_url
        )
        message = llm_errors.endpoint_not_api_message(
            base_url, provider, definitive=definitive
        )
    else:
        # Only the provider's own JSON error field is surfaced; an HTML page
        # or a proxy's plain-text body is never echoed.
        own = llm_errors.json_error_message(body)
        message = (
            _sanitise_error_message(own, api_key)
            if own
            else f"The provider returned HTTP {resp.status_code}."
        )
        detail = message if own else None
        definitive = code == llm_errors.INVALID_KEY and llm_errors.is_definitive_invalid_key(
            resp.status_code, body
        )
        raise ProviderError(
            resp.status_code, message, code, detail=detail, definitive=definitive
        )
    raise ProviderError(resp.status_code, message, code, definitive=definitive)


# ---------------------------------------------------------------------------
# Wire-format translation
# ---------------------------------------------------------------------------

def _openai_messages(messages: list[Message], *, echo_reasoning: bool = False) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in messages:
        role = m.get("role")
        if role == "assistant":
            tool_calls = m.get("tool_calls") or []
            msg: dict[str, Any] = {"role": "assistant", "content": m.get("content") or None}
            if echo_reasoning and m.get("reasoning_content"):
                # Some reasoning providers (DeepSeek/Kimi) require the assistant's
                # reasoning_content to be echoed back on multi-turn tool requests.
                msg["reasoning_content"] = m["reasoning_content"]
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

def _replayed_without_reasoning(messages: list[Message]) -> bool:
    """True when a prior assistant message lacks reasoning_content.

    History rebuilt from storage (e.g. the session helper's persisted turns)
    never carries reasoning text, so a resumed tool-loop turn needs the
    provider's thinking disabled rather than echoing a reasoning it cannot
    reconstruct.
    """
    for m in messages:
        if m.get("role") == "assistant" and not m.get("reasoning_content"):
            return True
    return False


async def _stream_openai_compatible(
    *,
    url: str,
    model: str,
    system: str,
    messages: list[Message],
    tools: list[ToolSpec],
    api_key: str,
    temperature: float | None,
    max_tokens: int,
    timeout: float,
    token_field: str = "max_completion_tokens",
    omit_temperature: bool = False,
    extra_headers: tuple[tuple[str, str], ...] = (),
    reasoning_effort: bool = True,
    reasoning_object: bool = False,
    echo_reasoning: bool = False,
    thinking_disable_on_replay: bool = False,
    top_p: float | None = None,
    reasoning_effort_value: str | None = None,
    custom_base_url: str | None = None,
    provider_hint: str | None = None,
    max_response_bytes: int | None = None,
) -> AsyncIterator[StreamEvent]:
    url = _chat_completions_url(url)

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for key, value in extra_headers:
        headers[key] = value


    def build(drop: frozenset[str]) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                *_openai_messages(messages, echo_reasoning=echo_reasoning),
            ],
            "stream": True,
            "stream_options": {"include_usage": True},
            token_field: max_tokens,
        }
        if thinking_disable_on_replay and _replayed_without_reasoning(messages):
            # DeepSeek rejects a replayed tool-loop turn whose assistant messages
            # carry no reasoning_content while thinking is on; disable it for this
            # request (the conversation history cannot reconstruct the reasoning).
            body["thinking"] = {"type": "disabled"}
        _apply_sampling(
            body,
            model=model,
            temperature=temperature,
            omit_temperature=omit_temperature,
            reasoning_effort=reasoning_effort,
            reasoning_object=reasoning_object,
            drop=drop,
            top_p=top_p,
            reasoning_effort_value=reasoning_effort_value,
        )
        if tools:
            body["tools"] = _openai_tools(tools)
        return body

    tool_frags: dict[int, dict[str, str]] = {}
    reasoning_parts: list[str] = []
    input_tokens = 0
    output_tokens = 0
    finish_reason: str | None = None
    saw_data = False

    drop = _omitted_params(url, model)

    async with _make_client(timeout) as client:
        for attempt in range(2):
            body = build(drop)
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code >= 400:
                    error_body = await _read_error_body(resp)
                    if attempt == 0:
                        new_drop = _classify_param_error(resp.status_code, error_body)
                        effective = new_drop - drop
                        if effective and any(_has_param(body, param) for param in effective):
                            drop = drop | new_drop
                            _remember_omission(url, model, drop)
                            continue
                    await _raise_provider_error(
                        resp,
                        api_key,
                        base_url=custom_base_url,
                        provider=provider_hint,
                        body=error_body,
                    )
                if custom_base_url and llm_errors.looks_like_html(
                    "", resp.headers.get("content-type", "")
                ):
                    # A web page, not an API (a console URL that answers 200).
                    raise ProviderError(
                        resp.status_code,
                        llm_errors.endpoint_not_api_message(custom_base_url, provider_hint),
                        llm_errors.ENDPOINT_NOT_API,
                        definitive=True,
                    )
                content_type = resp.headers.get("content-type", "").lower()
                events: AsyncIterator[dict[str, Any]] | None = None
                completion: Any = None
                is_json_label = "json" in content_type and "event-stream" not in content_type
                if is_json_label:
                    # Some gateways ignore ``stream: true`` and answer with one
                    # JSON chat completion (accepted as a single-chunk stream);
                    # others send a real SSE stream labelled as JSON.
                    completion, events = await _json_labelled_body(resp, max_response_bytes)
                if is_json_label and events is None:
                    if isinstance(completion, dict) and "error" in completion:
                        _raise_stream_error(completion["error"], api_key)
                    choices = (completion or {}).get("choices") if isinstance(completion, dict) else None
                    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
                        message = choices[0].get("message")
                        if isinstance(message, dict):
                            saw_data = True
                            if isinstance(message.get("content"), str) and message["content"]:
                                yield TextDelta(message["content"])
                            if isinstance(message.get("reasoning_content"), str):
                                reasoning_parts.append(message["reasoning_content"])
                            for idx, call in enumerate(message.get("tool_calls") or []):
                                if not isinstance(call, dict):
                                    continue
                                fn = call.get("function") or {}
                                args = fn.get("arguments") or ""
                                tool_frags[idx] = {
                                    "id": str(call.get("id") or ""),
                                    "name": str(fn.get("name") or ""),
                                    "args": args if isinstance(args, str) else json.dumps(args),
                                }
                            finish_reason = choices[0].get("finish_reason") or "stop"
                    usage = completion.get("usage") if isinstance(completion, dict) else None
                    if isinstance(usage, dict):
                        input_tokens = usage.get("prompt_tokens", 0)
                        output_tokens = usage.get("completion_tokens", 0)
                    break
                if events is None:
                    events = _iter_sse_json(resp, max_response_bytes)
                async for data in events:
                    saw_data = True
                    if "error" in data:
                        _raise_stream_error(data["error"], api_key)
                    choices = data.get("choices") or []
                    if choices:
                        delta = choices[0].get("delta") or {}
                        if delta.get("content"):
                            yield TextDelta(delta["content"])
                        # Reasoning text (DeepSeek/Kimi) is accumulated but never
                        # surfaced, logged, or persisted — only echoed back.
                        if delta.get("reasoning_content"):
                            reasoning_parts.append(delta["reasoning_content"])
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
            break

    if finish_reason is None:
        if custom_base_url and not saw_data:
            # Nothing that looks like a chat-completions stream came back:
            # the address answers, but it isn't an OpenAI-compatible API.
            raise ProviderError(
                502,
                llm_errors.endpoint_not_api_message(custom_base_url, provider_hint),
                llm_errors.ENDPOINT_NOT_API,
                definitive=True,
            )
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
    yield Done(_openai_stop_reason(finish_reason), reasoning_content="".join(reasoning_parts))


async def _stream_anthropic(
    *,
    model: str,
    system: str,
    messages: list[Message],
    tools: list[ToolSpec],
    api_key: str,
    temperature: float | None,
    max_tokens: int,
    timeout: float,
    top_p: float | None = None,
    max_response_bytes: int | None = None,
) -> AsyncIterator[StreamEvent]:
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": _anthropic_messages(messages),
        "stream": True,
    }
    # Anthropic rejects temperature and top_p together on current models:
    # when the caller set top_p, it wins and temperature is not sent.
    if top_p is not None:
        body["top_p"] = top_p
    elif temperature is not None:
        body["temperature"] = temperature
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
            async for data in _iter_sse_json(resp, max_response_bytes):
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
    temperature: float | None,
    max_tokens: int,
    timeout: float,
    top_p: float | None = None,
    max_response_bytes: int | None = None,
) -> AsyncIterator[StreamEvent]:
    url = _GOOGLE_STREAM_URL_TEMPLATE.format(model=model)
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    generation_config: dict[str, Any] = {"maxOutputTokens": max_tokens}
    if temperature is not None:
        generation_config["temperature"] = temperature
    if top_p is not None:
        generation_config["topP"] = top_p
    body: dict[str, Any] = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": _google_messages(messages),
        "generationConfig": generation_config,
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
            async for data in _iter_sse_json(resp, max_response_bytes):
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
    temperature: float | None = 0,
    max_tokens: int = 4096,
    timeout: float = 120,
    top_p: float | None = None,
    reasoning_effort_value: str | None = None,
    max_response_bytes: int | None = None,
) -> AsyncIterator[StreamEvent]:
    """Stream a single model turn, translating neutral messages/tools and back.

    ``base_url`` forces the OpenAI-compatible chat-completions format (matching
    ``_call_openai_compatible``). Otherwise the provider is auto-detected from
    the model name when not given. Reasoning models omit temperature. ``top_p``
    and ``reasoning_effort_value`` are only sent when set.
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
            raise ProviderError(
                400, "The configured LLM endpoint is not allowed.", llm_errors.ENDPOINT_BLOCKED
            ) from exc
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
            top_p=top_p,
            reasoning_effort_value=reasoning_effort_value,
            custom_base_url=base_url,
            provider_hint=provider,
            max_response_bytes=max_response_bytes,
        )
    else:
        profile = get_provider(provider)
        if profile is None:
            raise ValueError(f"Unsupported provider: {provider}")
        if profile.api_style == "anthropic":
            turn = _stream_anthropic(
                model=model,
                system=system,
                messages=messages,
                tools=tools,
                api_key=api_key,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                top_p=top_p,
                max_response_bytes=max_response_bytes,
            )
        elif profile.api_style == "google":
            turn = _stream_google(
                model=model,
                system=system,
                messages=messages,
                tools=tools,
                api_key=api_key,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                top_p=top_p,
                max_response_bytes=max_response_bytes,
            )
        else:
            turn = _stream_openai_compatible(
                url=_chat_completions_url(profile.base_url),
                model=model,
                system=system,
                messages=messages,
                tools=tools,
                api_key=api_key,
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=timeout,
                token_field=profile.token_field,
                omit_temperature=profile.omit_temperature,
                extra_headers=profile.extra_headers,
                reasoning_effort=profile.reasoning_effort,
                reasoning_object=profile.reasoning_object,
                echo_reasoning=profile.echo_reasoning,
                thinking_disable_on_replay=profile.thinking_disable_on_replay,
                top_p=top_p,
                reasoning_effort_value=reasoning_effort_value,
                max_response_bytes=max_response_bytes,
            )

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
    temperature: float | None = 0,
    max_tokens: int = 4096,
    timeout: float = 120,
    top_p: float | None = None,
    reasoning_effort_value: str | None = None,
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
        turn_reasoning: str = ""

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
                top_p=top_p,
                reasoning_effort_value=reasoning_effort_value,
            ),
            deadline,
        ):
            if isinstance(event, Done):
                turn_stop_reason = event.stop_reason
                turn_reasoning = event.reasoning_content
                continue
            yield event
            if isinstance(event, TextDelta):
                text_parts.append(event.text)
            elif isinstance(event, ToolCallEvent):
                tool_calls.append(event.tool_call)
            elif isinstance(event, Usage):
                total_tokens += event.input_tokens + event.output_tokens

        conversation.append(
            assistant_message("".join(text_parts), tool_calls, reasoning_content=turn_reasoning)
        )

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
