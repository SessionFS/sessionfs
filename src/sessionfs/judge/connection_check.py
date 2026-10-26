"""One-request LLM connection check ("Test connection").

Sends a single minimal chat request ("ping", a handful of output tokens)
through the SAME streaming code path the session helper uses
(``agent_stream.stream_chat``): the registry's URL building and request
quirks for a provider, or the OpenAI-compatible chat-completions format for a
custom Base URL. A pass therefore means real helper and audit calls reach a
working chat endpoint with this key and model.

The check is bounded: per-request timeout, a hard total deadline, a response
size cap, no redirects (the client never follows them) and, for a custom
Base URL, the installed SSRF guard runs right before the request. The only
retry is the existing sampling-parameter retry (GitHub #102). The result never
contains the API key or a raw upstream body.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

import httpx

from sessionfs.judge import llm_errors
from sessionfs.judge.agent_stream import Done, ProviderError, stream_chat, user_message
from sessionfs.judge.provider_registry import get_provider

logger = logging.getLogger("sessionfs.judge.connection_check")

REQUEST_TIMEOUT_SECONDS = 15.0
TOTAL_DEADLINE_SECONDS = 20.0
MAX_RESPONSE_BYTES = 256 * 1024
MAX_OUTPUT_TOKENS = 16

_SYSTEM = "This is a connection test. Reply with the single word: ok"
_GOOGLE_HOST = "generativelanguage.googleapis.com"


@dataclass(frozen=True)
class ConnectionCheckResult:
    ok: bool
    code: str
    message: str
    provider: str
    model: str
    endpoint_host: str
    latency_ms: int | None
    # Internal: an ``endpoint_not_api`` failure that proves the address is
    # not an API (405, a web page, a console host). Not part of the API.
    definitive: bool = field(default=False, compare=False)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "code": self.code,
            "message": self.message,
            "provider": self.provider,
            "model": self.model,
            "endpoint_host": self.endpoint_host,
            "latency_ms": self.latency_ms,
        }


def endpoint_host(provider: str | None, base_url: str | None) -> str:
    """The host a request for this configuration goes to."""
    if base_url:
        try:
            return (urlparse(base_url).hostname or "").lower()
        except ValueError:
            return ""
    profile = get_provider(provider or "")
    if profile is None:
        return ""
    if profile.api_style == "google":
        return _GOOGLE_HOST
    return (urlparse(profile.base_url).hostname or "").lower()


async def check_llm_connection(
    *,
    provider: str,
    model: str,
    api_key: str,
    base_url: str | None = None,
    timeout: float = REQUEST_TIMEOUT_SECONDS,
    deadline: float = TOTAL_DEADLINE_SECONDS,
) -> ConnectionCheckResult:
    """Make one minimal chat request and classify the outcome."""
    base_url = (base_url or "").strip() or None
    provider = (provider or "").strip().lower()
    host = endpoint_host(provider, base_url)
    started = time.monotonic()

    def result(code: str, message: str, definitive: bool = False) -> ConnectionCheckResult:
        return ConnectionCheckResult(
            ok=code == "ok",
            code=code,
            message=message,
            provider=provider,
            model=model,
            endpoint_host=host,
            latency_ms=int((time.monotonic() - started) * 1000),
            definitive=definitive,
        )

    def failure(
        code: str, detail: str | None = None, definitive: bool = False
    ) -> ConnectionCheckResult:
        return result(
            code,
            llm_errors.friendly_message(
                code,
                base_url=base_url,
                provider=provider,
                model=model,
                host=host,
                key_sent=bool(api_key),
                detail=detail,
                timeout_seconds=timeout,
                definitive=definitive,
            ),
            definitive,
        )

    async def run() -> None:
        events = stream_chat(
            model=model,
            system=_SYSTEM,
            messages=[user_message("ping")],
            tools=[],
            api_key=api_key,
            provider=provider or None,
            base_url=base_url,
            temperature=None,
            max_tokens=MAX_OUTPUT_TOKENS,
            timeout=timeout,
            max_response_bytes=MAX_RESPONSE_BYTES,
        )
        try:
            async for event in events:
                if isinstance(event, Done):
                    return
        finally:
            await events.aclose()

    try:
        await asyncio.wait_for(run(), timeout=deadline)
    except (asyncio.TimeoutError, httpx.TimeoutException):
        return failure(llm_errors.TIMEOUT)
    except ProviderError as exc:
        code = exc.code or llm_errors.PROVIDER_ERROR
        # Only a snippet of the provider's own JSON error field is shown.
        detail = llm_errors.sanitise_snippet(exc.detail, api_key) if exc.detail else None
        return failure(code, detail, exc.definitive)
    except httpx.RequestError:
        # Never echo the transport error: with a caller-chosen endpoint it
        # would tell refused from TLS failure from a filtered port.
        return failure(llm_errors.UNREACHABLE)
    except ValueError:
        return failure(llm_errors.PROVIDER_ERROR, "Unknown provider.")
    except Exception as exc:  # noqa: BLE001 - classify, never raise
        logger.warning("Connection check failed unexpectedly: %s", type(exc).__name__)
        return failure(llm_errors.PROVIDER_ERROR)

    label = host or provider
    return result("ok", f"Connected to {label} · {model}")
