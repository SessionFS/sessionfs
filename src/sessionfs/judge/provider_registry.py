"""Single source of truth for first-class LLM providers.

Every consumer of the BYOK LLM layer reads from this registry: the sync call
path (``judge/providers.py``), the streaming/tool-calling path
(``judge/agent_stream.py``), model discovery, server-side validation
(``server/routes/settings.py`` and ``server/routes/org_llm_settings.py``), CLI
choices (``cli/cmd_audit.py``) and the dashboard's provider picker (via
``GET /api/v1/llm/providers``).

Adding a provider is a data change here — never a new if/elif ladder in a
call site. Each entry carries only the request quirks that differ between
OpenAI-compatible vendors; anthropic/google are their own wire formats and
ignore those fields.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Provider:
    """A first-class LLM provider and its OpenAI-compatible request quirks."""

    # Stable registry id (stored in the DB and sent by the dashboard/CLI).
    id: str
    # Human label for the UI.
    label: str
    # Wire format: "anthropic" | "google" | "openai" (OpenAI-compatible).
    api_style: str
    # Base endpoint. For OpenAI-compatible providers this is the root the
    # chat-completions path is appended to (e.g. "https://api.openai.com/v1").
    base_url: str
    # OpenAI-compatible list-models path (e.g. "/models"); None when there is
    # no list-models endpoint we call directly.
    models_path: str | None = None
    # OpenAI-compatible token field ("max_tokens" or "max_completion_tokens").
    token_field: str = "max_tokens"
    # Never send temperature/top_p (Kimi rejects both).
    omit_temperature: bool = False
    # Echo accumulated reasoning_content back on the assistant message in
    # follow-up tool-loop requests (DeepSeek/Kimi reasoning models).
    echo_reasoning: bool = False
    # Substitute reasoning effort for temperature on reasoning models (OpenAI
    # o-series and OpenRouter pass-throughs). Must stay False for providers
    # whose reasoning models work differently (DeepSeek/Kimi/Grok/Mistral/...).
    reasoning_effort: bool = False
    # Send reasoning effort as OpenRouter's ``reasoning: {effort}`` object
    # instead of OpenAI's top-level ``reasoning_effort`` string.
    reasoning_object: bool = False
    # Extra request headers (OpenRouter identifies apps this way).
    extra_headers: tuple[tuple[str, str], ...] = ()
    # Disable the provider's thinking block when a replayed turn has no
    # reasoning_content on prior assistant messages (DeepSeek 400s otherwise).
    thinking_disable_on_replay: bool = False
    # Lower-case model-name prefixes used for provider auto-detection.
    detect_prefixes: tuple[str, ...] = ()
    # Short list of representative model ids shown before discovery fills in.
    suggested_models: tuple[str, ...] = ()
    # Whether the provider exposes a usable OpenAI-compatible /models endpoint.
    discovery: bool = False
    # Environment variables the CLI reads this provider's key from, in order.
    # A key is only ever taken from its own provider's variables.
    env_vars: tuple[str, ...] = ()
    # Hosts of the provider's web console / chat app. They answer a chat
    # request with 404/405/HTML, so a Base URL pointing at one gets a message
    # naming the real API address. Only hosts we know serve the console.
    console_hosts: tuple[str, ...] = ()

    @property
    def default_base_url(self) -> str | None:
        """The API address to show as the Base URL placeholder.

        Only OpenAI-compatible providers: a custom Base URL is always called
        with the OpenAI chat-completions format, so Anthropic/Google are used
        by leaving it empty.
        """
        return self.base_url if self.api_style == "openai" else None


# ---------------------------------------------------------------------------
# Provider table
# ---------------------------------------------------------------------------
#
# suggested_models are the providers' documented flagship/chat model ids. They
# are only a starting point: the dashboard offers "Fetch models" for
# discovery-capable providers and free-text entry everywhere.

PROVIDERS: tuple[Provider, ...] = (
    Provider(
        id="anthropic",
        console_hosts=("console.anthropic.com", "platform.claude.com", "claude.ai"),
        env_vars=("ANTHROPIC_API_KEY",),
        label="Anthropic",
        api_style="anthropic",
        base_url="https://api.anthropic.com/v1/messages",
        suggested_models=(
            "claude-opus-5-5",
            "claude-sonnet-5-5",
            "claude-haiku-5-5",
        ),
        detect_prefixes=("claude-",),
    ),
    Provider(
        # developers.openai.com/api/docs/models
        id="openai",
        console_hosts=("platform.openai.com", "chatgpt.com", "chat.openai.com"),
        env_vars=("OPENAI_API_KEY",),
        label="OpenAI",
        api_style="openai",
        base_url="https://api.openai.com/v1",
        models_path="/models",
        token_field="max_completion_tokens",
        reasoning_effort=True,
        suggested_models=(
            "gpt-6-astra",
            "gpt-6.1-sol",
            "gpt-6-luna",
        ),
        detect_prefixes=("gpt-", "o1", "o3", "o4"),
        discovery=True,
    ),
    Provider(
        # ai.google.dev/gemini-api/docs/models
        id="google",
        console_hosts=("aistudio.google.com", "gemini.google.com"),
        env_vars=("GEMINI_API_KEY", "GOOGLE_API_KEY"),
        label="Google Gemini",
        api_style="google",
        base_url="https://generativelanguage.googleapis.com/v1beta",
        suggested_models=(
            "gemini-3.1-pro-preview",
            "gemini-3.6-flash",
        ),
        detect_prefixes=("gemini-",),
    ),
    Provider(
        # Hundreds of models; the list comes from "Fetch models".
        id="openrouter",
        env_vars=("OPENROUTER_API_KEY",),
        label="OpenRouter",
        api_style="openai",
        base_url="https://openrouter.ai/api/v1",
        models_path="/models",
        token_field="max_tokens",
        reasoning_effort=True,
        reasoning_object=True,
        extra_headers=(
            ("HTTP-Referer", "https://sessionfs.dev"),
            ("X-Title", "SessionFS"),
        ),
        discovery=True,
    ),
    Provider(
        # api-docs.deepseek.com/quick_start/pricing — deepseek-chat and
        # deepseek-reasoner were retired 2026-07-24. Thinking is on by default.
        id="deepseek",
        console_hosts=("platform.deepseek.com", "chat.deepseek.com"),
        env_vars=("DEEPSEEK_API_KEY",),
        label="DeepSeek",
        api_style="openai",
        base_url="https://api.deepseek.com",
        models_path="/models",
        token_field="max_tokens",
        echo_reasoning=True,
        thinking_disable_on_replay=True,
        suggested_models=(
            "deepseek-flash",
            "deepseek-v4-pro",
        ),
        detect_prefixes=("deepseek-",),
        discovery=True,
    ),
    Provider(
        # platform.kimi.ai/docs — temperature/top_p are fixed per mode and any
        # other value is rejected, so they are never sent.
        id="kimi",
        console_hosts=("platform.moonshot.ai", "kimi.com", "www.kimi.com"),
        env_vars=("MOONSHOT_API_KEY", "KIMI_API_KEY"),
        label="Kimi (Moonshot)",
        api_style="openai",
        base_url="https://api.moonshot.ai/v1",
        models_path="/models",
        token_field="max_tokens",
        omit_temperature=True,
        echo_reasoning=True,
        suggested_models=(
            "kimi-k3",
            "kimi-k3.1-pro",
            "kimi-k2.6",
        ),
        detect_prefixes=("kimi-",),
        discovery=True,
    ),
    Provider(
        # docs.x.ai/developers/grok-4-7
        id="xai",
        console_hosts=("console.x.ai", "grok.com"),
        env_vars=("XAI_API_KEY",),
        label="xAI (Grok)",
        api_style="openai",
        base_url="https://api.x.ai/v1",
        models_path="/models",
        token_field="max_tokens",
        suggested_models=("grok-4.7",),
        detect_prefixes=("grok-",),
        discovery=True,
    ),
    Provider(
        # docs.mistral.ai — the -latest aliases follow each family's newest model.
        id="mistral",
        console_hosts=("console.mistral.ai", "chat.mistral.ai"),
        env_vars=("MISTRAL_API_KEY",),
        label="Mistral",
        api_style="openai",
        base_url="https://api.mistral.ai/v1",
        models_path="/models",
        token_field="max_tokens",
        suggested_models=(
            "mistral-large-latest",
            "mistral-medium-latest",
            "mistral-small-latest",
        ),
        detect_prefixes=("mistral-", "codestral-", "magistral-", "devstral-"),
        discovery=True,
    ),
    Provider(
        # docs.z.ai — international pay-as-you-go endpoint (mainland China
        # accounts use open.bigmodel.cn through a custom endpoint URL).
        id="zai",
        env_vars=("ZAI_API_KEY",),
        label="Z.ai (GLM)",
        api_style="openai",
        base_url="https://api.z.ai/api/paas/v4",
        token_field="max_tokens",
        suggested_models=("glm-5.3",),
        detect_prefixes=("glm-",),
    ),
    Provider(
        # alibabacloud.com/help/en/model-studio/base-url — Singapore endpoint;
        # keys are region-bound, other regions use a custom endpoint URL.
        id="qwen",
        env_vars=("DASHSCOPE_API_KEY",),
        label="Qwen (Alibaba Cloud)",
        api_style="openai",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        models_path="/models",
        token_field="max_tokens",
        suggested_models=(
            "qwen-max",
            "qwen-plus",
        ),
        detect_prefixes=("qwen",),
        discovery=True,
    ),
    Provider(
        # platform.minimax.io/docs — international endpoint. Temperature must
        # be in (0, 1], so the judge's 0 is never sent.
        id="minimax",
        console_hosts=("platform.minimax.io",),
        env_vars=("MINIMAX_API_KEY",),
        label="MiniMax",
        api_style="openai",
        base_url="https://api.minimax.io/v1",
        token_field="max_tokens",
        omit_temperature=True,
        suggested_models=("MiniMax-M3",),
        detect_prefixes=("minimax-",),
    ),
    Provider(
        # console.groq.com/docs/openai
        id="groq",
        console_hosts=("console.groq.com",),
        env_vars=("GROQ_API_KEY",),
        label="Groq",
        api_style="openai",
        base_url="https://api.groq.com/openai/v1",
        models_path="/models",
        token_field="max_completion_tokens",
        discovery=True,
    ),
    Provider(
        # docs.together.ai — OpenAI-compatible
        id="together",
        env_vars=("TOGETHER_API_KEY",),
        label="Together AI",
        api_style="openai",
        base_url="https://api.together.xyz/v1",
        models_path="/models",
        token_field="max_tokens",
        discovery=True,
    ),
    Provider(
        # docs.fireworks.ai — OpenAI-compatible inference endpoint
        id="fireworks",
        console_hosts=("fireworks.ai",),
        env_vars=("FIREWORKS_API_KEY",),
        label="Fireworks AI",
        api_style="openai",
        base_url="https://api.fireworks.ai/inference/v1",
        models_path="/models",
        token_field="max_tokens",
        discovery=True,
    ),
)


PROVIDER_BY_ID: dict[str, Provider] = {p.id: p for p in PROVIDERS}

# Ordered prefix table used for auto-detection. Prefixes are checked in this
# order, so the (shorter, dash-less) openai "o1"/"o3"/"o4" entries are kept
# ahead of nothing that could shadow them; every new prefix is distinct from
# the existing ones.
_DETECT_PREFIXES: tuple[tuple[str, str], ...] = tuple(
    (prefix, p.id) for p in PROVIDERS for prefix in p.detect_prefixes
)


def get_provider(provider_id: str) -> Provider | None:
    """Return the registry entry for a provider id, or None if unknown."""
    if not provider_id:
        return None
    return PROVIDER_BY_ID.get(provider_id.strip().lower())


def detect_provider_id(model: str) -> str:
    """Auto-detect the registry id for a model name.

    Models containing "/" are routed to OpenRouter (vendor/model shorthand).
    Unknown models fall back to OpenRouter, matching the historical default.
    """
    if "/" in model:
        return "openrouter"
    model_lower = model.lower()
    for prefix, provider_id in _DETECT_PREFIXES:
        if model_lower.startswith(prefix):
            return provider_id
    return "openrouter"


def provider_specs() -> list[dict]:
    """The public view of the registry for ``GET /api/v1/llm/providers``."""
    return [
        {
            "id": p.id,
            "label": p.label,
            "api_style": p.api_style,
            "suggested_models": list(p.suggested_models),
            "discovery": p.discovery,
            "default_base_url": p.default_base_url,
        }
        for p in PROVIDERS
    ]
