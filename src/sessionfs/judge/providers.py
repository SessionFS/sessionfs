"""Multi-provider LLM client (BYOK — Bring Your Own Key).

Uses httpx directly with no SDK dependencies. The API key is used for a
single request and never stored.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable

import httpx

from sessionfs.judge import llm_errors
from sessionfs.judge.provider_registry import detect_provider_id, get_provider

logger = logging.getLogger("sessionfs.judge.providers")

# Optional SSRF guard for caller-supplied endpoint URLs. The API server
# installs one (sessionfs.server.llm_url_guard) in create_app; the CLI runs
# without it so a local Ollama keeps working. It runs right before every
# outbound request to a custom base_url and raises to refuse it.
_base_url_guard: Callable[[str], Awaitable[None]] | None = None


def set_base_url_guard(guard: Callable[[str], Awaitable[None]] | None) -> None:
    global _base_url_guard
    _base_url_guard = guard


async def _check_base_url(url: str) -> None:
    if _base_url_guard is not None:
        await _base_url_guard(url)


# Reasoning families that reject non-default temperature (OpenAI o-series and
# the newer gpt-5/gpt-6 families). Prefix-matched so dated/dotted variants such
# as "o3-mini-2025-04-16", "gpt-5.6-sol" and "gpt-6-astra" are all covered
# without mangling names that merely contain "-2" (e.g. "gpt-4.2-style").
_REASONING_PREFIXES = ("o1", "o3", "o4", "gpt-5", "gpt-6")

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_GOOGLE_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)


def _detect_provider(model: str) -> str:
    """Auto-detect provider from model name (registry id)."""
    return detect_provider_id(model)


def _is_reasoning_model(model: str) -> bool:
    """Check if a model is a reasoning model that rejects temperature."""
    base = model.lower()
    return any(base.startswith(prefix) for prefix in _REASONING_PREFIXES)


# ---------------------------------------------------------------------------
# Parameter-omission cache
# ---------------------------------------------------------------------------
#
# Some OpenAI-compatible endpoints (LiteLLM/Azure gateways, vLLM) reject
# ``temperature`` / ``top_p`` / ``reasoning`` on reasoning models. When we see
# such a 400/422 once, remember the parameter per (endpoint URL, model) so the
# next call omits it up front and doesn't waste a request. Process-local and
# bounded — no DB, no Redis.

_PARAM_OMISSION_CACHE: OrderedDict[tuple[str, str], frozenset[str]] = OrderedDict()
_PARAM_OMISSION_CACHE_MAX = 512

_REJECTION_TOKENS = (
    "unsupported",
    "not support",
    "does not support",
    "only the default",
    "invalid",
    "unknown",
    "unrecognized",
)


def _omitted_params(url: str, model: str) -> frozenset[str]:
    """Parameters remembered as rejected for (endpoint URL, model)."""
    return _PARAM_OMISSION_CACHE.get((url, model), frozenset())


def _remember_omission(url: str, model: str, params: frozenset[str]) -> None:
    key = (url, model)
    _PARAM_OMISSION_CACHE[key] = params
    _PARAM_OMISSION_CACHE.move_to_end(key)
    while len(_PARAM_OMISSION_CACHE) > _PARAM_OMISSION_CACHE_MAX:
        _PARAM_OMISSION_CACHE.popitem(last=False)


def _clear_omission_cache() -> None:
    """Empty the parameter-omission cache (used by tests)."""
    _PARAM_OMISSION_CACHE.clear()


def _classify_param_error(status: int, text: str) -> frozenset[str]:
    """Map a 400/422 provider error body to request parameters to drop.

    Conservative: the body must mention the parameter name AND one of the
    rejection tokens. Returns an empty set when nothing matches.
    """
    if status not in (400, 422):
        return frozenset()
    low = text.lower()
    if not any(token in low for token in _REJECTION_TOKENS):
        return frozenset()
    omitted: set[str] = set()
    if "temperature" in low:
        omitted.add("temperature")
    if "top_p" in low:
        omitted.add("top_p")
    if "reasoning_effort" in low or "reasoning" in low:
        omitted.add("reasoning")
    return frozenset(omitted)


def _has_param(body: dict, param: str) -> bool:
    """Whether a logical parameter ("temperature", "top_p", "reasoning") is set."""
    if param == "reasoning":
        return "reasoning" in body or "reasoning_effort" in body
    return param in body


def _apply_sampling(
    body: dict,
    *,
    model: str,
    temperature: float | None,
    omit_temperature: bool,
    reasoning_effort: bool,
    reasoning_object: bool,
    drop: frozenset[str],
    top_p: float | None = None,
    reasoning_effort_value: str | None = None,
) -> None:
    """Set reasoning effort / temperature / top_p on a chat-completions body.

    Reasoning models never get ``temperature`` (only their default is
    accepted); dropping the reasoning parameter must not add it back. OpenAI's
    Chat Completions and OpenAI-compatible gateways take a top-level
    ``reasoning_effort`` string; OpenRouter takes a ``reasoning`` object.

    ``reasoning_effort_value`` overrides the historical "medium" default
    (``None`` keeps "medium"; ``"off"`` leaves the provider's default). ``top_p``
    and a ``None`` ``temperature`` are only sent when set, so callers can opt
    into sampling without the provider's default being overridden.
    """
    base_model = model.split("/")[-1] if "/" in model else model
    is_reasoning = reasoning_effort and _is_reasoning_model(base_model)
    if is_reasoning:
        if "reasoning" not in drop and reasoning_effort_value != "off":
            effort = reasoning_effort_value or "medium"
            if reasoning_object:
                body["reasoning"] = {"effort": effort}
            else:
                body["reasoning_effort"] = effort
    else:
        if not omit_temperature and "temperature" not in drop and temperature is not None:
            body["temperature"] = temperature
        if not omit_temperature and top_p is not None and "top_p" not in drop:
            body["top_p"] = top_p


def _response_error_text(resp: httpx.Response) -> str:
    """Decode a provider error body without ever raising."""
    try:
        return resp.content.decode("utf-8", errors="replace")
    except Exception:
        return ""


def _build_openai_body(
    *,
    model: str,
    system: str,
    prompt: str,
    temperature: float,
    token_field: str,
    omit_temperature: bool,
    reasoning_effort: bool,
    reasoning_object: bool = False,
    drop: frozenset[str] = frozenset(),
) -> dict:
    """Build a chat-completions body, honouring remembered omissions.

    Reasoning models never get ``temperature`` (their default is 1); dropping
    ``reasoning`` for one of them must not add ``temperature`` back in.
    """
    body: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        token_field: 4096,
    }
    _apply_sampling(
        body,
        model=model,
        temperature=temperature,
        omit_temperature=omit_temperature,
        reasoning_effort=reasoning_effort,
        reasoning_object=reasoning_object,
        drop=drop,
    )
    return body


def _chat_completions_url(base_url: str) -> str:
    """Append /chat/completions to an OpenAI-compatible base URL (idempotent)."""
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        url = f"{url}/chat/completions"
    return url


async def _call_openai_compatible(
    url: str,
    model: str,
    system: str,
    prompt: str,
    api_key: str,
    temperature: float = 0,
    *,
    token_field: str = "max_completion_tokens",
    omit_temperature: bool = False,
    extra_headers: tuple[tuple[str, str], ...] = (),
    reasoning_effort: bool = True,
    reasoning_object: bool = False,
    custom_base_url: str | None = None,
    provider_hint: str | None = None,
) -> str:
    """Call any OpenAI-compatible endpoint (OpenAI, OpenRouter, LiteLLM, vLLM, Ollama, etc.).

    The keyword-only arguments carry the request quirks of the provider profile
    (see ``provider_registry``); the generic defaults preserve the historical
    behaviour for caller-supplied ``base_url`` endpoints.
    """
    url = _chat_completions_url(url)

    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    for key, value in extra_headers:
        headers[key] = value

    drop = _omitted_params(url, model)

    def build(drop: frozenset[str]) -> dict:
        return _build_openai_body(
            model=model,
            system=system,
            prompt=prompt,
            temperature=temperature,
            token_field=token_field,
            omit_temperature=omit_temperature,
            reasoning_effort=reasoning_effort,
            reasoning_object=reasoning_object,
            drop=drop,
        )

    body = build(drop)
    async with httpx.AsyncClient(timeout=120, follow_redirects=False) as client:
        resp = await client.post(url, json=body, headers=headers)
        if resp.status_code in (400, 422):
            new_drop = _classify_param_error(resp.status_code, _response_error_text(resp))
            effective = new_drop - drop
            if effective and any(_has_param(body, param) for param in effective):
                drop = drop | new_drop
                _remember_omission(url, model, drop)
                body = build(drop)
                resp = await client.post(url, json=body, headers=headers)
        # A caller-supplied endpoint that isn't an API (a provider's web
        # console answers 404/405/HTML) gets a readable error naming the
        # right address instead of the upstream body.
        endpoint_error = llm_errors.endpoint_error_from_response(
            resp, base_url=custom_base_url, provider=provider_hint
        )
        if endpoint_error is not None:
            raise endpoint_error
        resp.raise_for_status()
    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError):
        if not custom_base_url:
            raise
        raise llm_errors.LlmEndpointError(
            llm_errors.ENDPOINT_NOT_API,
            llm_errors.endpoint_not_api_message(custom_base_url, provider_hint),
            request=resp.request,
            response=resp,
        ) from None


async def _call_anthropic(model: str, system: str, prompt: str, api_key: str, temperature: float = 0) -> str:
    """Call the Anthropic Messages API."""
    headers = {
        "x-api-key": api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    body: dict = {
        "model": model,
        "max_tokens": 4096,
        "temperature": temperature,
        "system": system,
        "messages": [{"role": "user", "content": prompt}],
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(_ANTHROPIC_URL, json=body, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    content = data.get("content", [])
    parts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block["text"])
    return "\n".join(parts)


async def _call_google(model: str, system: str, prompt: str, api_key: str, temperature: float = 0) -> str:
    """Call the Google Generative Language API."""
    url = _GOOGLE_URL_TEMPLATE.format(model=model)
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }
    body = {
        "system_instruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": 4096, "temperature": temperature},
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=body, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    candidates = data.get("candidates", [])
    if candidates:
        parts = candidates[0].get("content", {}).get("parts", [])
        return "\n".join(p.get("text", "") for p in parts)
    return ""


async def call_llm(
    model: str,
    system: str,
    prompt: str,
    api_key: str,
    provider: str | None = None,
    temperature: float = 0,
    base_url: str | None = None,
) -> str:
    """Call an LLM provider with the given system prompt and user prompt.

    If base_url is set, always uses OpenAI-compatible chat completions format
    regardless of provider — this supports LiteLLM, vLLM, Ollama, Azure OpenAI,
    and any OpenAI-compatible gateway.

    Auto-detects provider from model name if not specified, and dispatches
    through the provider registry (no per-provider if/elif ladder). Uses httpx
    directly — no SDK dependencies. The API key is used for this single request
    only and is never persisted. Temperature defaults to 0 for deterministic
    judge output; reasoning models use reasoning effort instead of temperature.
    """
    if provider is None:
        provider = _detect_provider(model)

    provider = provider.lower()
    logger.info("Calling %s provider with model %s (temp=%s, base_url=%s)", provider, model, temperature, base_url or "default")

    # Custom base URL — always use OpenAI-compatible format
    if base_url:
        await _check_base_url(base_url)
        return await _call_openai_compatible(
            base_url,
            model,
            system,
            prompt,
            api_key,
            temperature,
            custom_base_url=base_url,
            provider_hint=provider,
        )

    profile = get_provider(provider)
    if profile is None:
        raise ValueError(f"Unsupported provider: {provider}")

    if profile.api_style == "anthropic":
        return await _call_anthropic(model, system, prompt, api_key, temperature)
    if profile.api_style == "google":
        return await _call_google(model, system, prompt, api_key, temperature)

    return await _call_openai_compatible(
        _chat_completions_url(profile.base_url),
        model,
        system,
        prompt,
        api_key,
        temperature,
        token_field=profile.token_field,
        omit_temperature=profile.omit_temperature,
        extra_headers=profile.extra_headers,
        reasoning_effort=profile.reasoning_effort,
        reasoning_object=profile.reasoning_object,
    )


async def discover_models(base_url: str, api_key: str = "") -> list[str]:
    """Query an OpenAI-compatible /models endpoint and return model IDs."""
    try:
        await _check_base_url(base_url)
    except Exception as e:
        logger.warning("Model discovery refused for %s: %s", base_url, e)
        return []
    url = base_url.rstrip("/")
    if not url.endswith("/models"):
        if url.endswith("/v1"):
            url = f"{url}/models"
        else:
            url = f"{url}/v1/models"

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            resp = await client.get(url, headers=headers)
        if resp.status_code >= 300:
            return []
        data = resp.json()
        models_list = data.get("data", data.get("models", []))
        result = []
        for m in models_list:
            if isinstance(m, dict):
                mid = m.get("id", m.get("model", ""))
                if mid:
                    result.append(mid)
            elif isinstance(m, str):
                result.append(m)
        return sorted(result)
    except Exception as e:
        logger.warning("Model discovery failed for %s: %s", base_url, e)
        return []


async def validate_base_url(base_url: str, api_key: str, model: str) -> bool:
    """Test a custom endpoint with a minimal request."""
    try:
        await _check_base_url(base_url)
        result = await _call_openai_compatible(
            url=base_url,
            model=model,
            system="You are a test assistant.",
            prompt="Say 'ok'",
            api_key=api_key,
            temperature=0,
            custom_base_url=base_url,
        )
        return bool(result)
    except Exception as e:
        logger.warning("Base URL validation failed: %s — %s", base_url, e)
        return False
