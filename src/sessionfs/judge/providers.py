"""Multi-provider LLM client (BYOK — Bring Your Own Key).

Uses httpx directly with no SDK dependencies. The API key is used for a
single request and never stored.
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("sessionfs.judge.providers")

_PROVIDER_DETECT = [
    ("claude-", "anthropic"),
    ("gpt-", "openai"),
    ("o1", "openai"),
    ("o3", "openai"),
    ("o4", "openai"),
    ("gemini-", "google"),
]

# Reasoning models that reject temperature — use reasoning effort instead
_REASONING_MODELS = {"o1", "o1-pro", "o3", "o3-mini", "o4-mini"}

_ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
_OPENAI_URL = "https://api.openai.com/v1/chat/completions"
_GOOGLE_URL_TEMPLATE = (
    "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
)
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


def _detect_provider(model: str) -> str:
    """Auto-detect provider from model name.

    Models containing "/" are routed to OpenRouter.
    Unknown models fall back to OpenRouter.
    """
    if "/" in model:
        return "openrouter"

    model_lower = model.lower()
    for prefix, provider in _PROVIDER_DETECT:
        if model_lower.startswith(prefix):
            return provider

    # Unknown model — fall back to OpenRouter
    return "openrouter"


def _is_reasoning_model(model: str) -> bool:
    """Check if a model is a reasoning model that rejects temperature."""
    base = model.lower().split("-2")[0]  # strip date suffix like -2025-04-16
    return base in _REASONING_MODELS


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


async def _call_openai(model: str, system: str, prompt: str, api_key: str, temperature: float = 0) -> str:
    """Call the OpenAI Chat Completions API."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    body: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_completion_tokens": 4096,
    }
    # Reasoning models (o3, o4-mini) reject temperature
    if _is_reasoning_model(model):
        body["reasoning"] = {"effort": "medium"}
    else:
        body["temperature"] = temperature

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(_OPENAI_URL, json=body, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


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


async def _call_openrouter(model: str, system: str, prompt: str, api_key: str, temperature: float = 0) -> str:
    """Call the OpenRouter Chat Completions API."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "HTTP-Referer": "https://sessionfs.dev",
        "X-Title": "SessionFS",
    }
    # Extract the base model name for reasoning check (e.g., "openai/o3" -> "o3")
    base_model = model.split("/")[-1] if "/" in model else model

    body: dict = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 4096,
    }
    if _is_reasoning_model(base_model):
        body["reasoning"] = {"effort": "medium"}
    else:
        body["temperature"] = temperature

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(_OPENROUTER_URL, json=body, headers=headers)
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


async def call_llm(
    model: str,
    system: str,
    prompt: str,
    api_key: str,
    provider: str | None = None,
    temperature: float = 0,
) -> str:
    """Call an LLM provider with the given system prompt and user prompt.

    Auto-detects provider from model name if not specified:
    - claude-* -> anthropic
    - gpt-*, o1*, o3*, o4* -> openai
    - gemini-* -> google
    - models containing "/" -> openrouter
    - unknown models -> openrouter (fallback)

    Uses httpx directly — no SDK dependencies. The API key is used for
    this single request only and is never persisted. Temperature defaults
    to 0 for deterministic judge output. Reasoning models (o3, o4-mini)
    use reasoning effort instead of temperature.
    """
    if provider is None:
        provider = _detect_provider(model)

    provider = provider.lower()
    logger.info("Calling %s provider with model %s (temp=%s)", provider, model, temperature)

    if provider == "anthropic":
        return await _call_anthropic(model, system, prompt, api_key, temperature)
    elif provider == "openai":
        return await _call_openai(model, system, prompt, api_key, temperature)
    elif provider == "google":
        return await _call_google(model, system, prompt, api_key, temperature)
    elif provider == "openrouter":
        return await _call_openrouter(model, system, prompt, api_key, temperature)
    else:
        raise ValueError(f"Unsupported provider: {provider}")
