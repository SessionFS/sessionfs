"""The first-class provider registry (single source of truth for BYOK)."""

from __future__ import annotations

from sessionfs.judge.provider_registry import (
    PROVIDERS,
    detect_provider_id,
    get_provider,
    provider_specs,
)

_ALL_IDS = {p.id for p in PROVIDERS}


class TestGetProvider:
    def test_known_id(self):
        assert get_provider("deepseek").id == "deepseek"

    def test_case_insensitive(self):
        assert get_provider("DeepSeek").id == "deepseek"
        assert get_provider("KIMI").id == "kimi"

    def test_unknown_returns_none(self):
        assert get_provider("bogus") is None

    def test_empty_returns_none(self):
        assert get_provider("") is None


class TestDetectProviderId:
    def test_new_provider_prefixes(self):
        assert detect_provider_id("deepseek-flash") == "deepseek"
        assert detect_provider_id("kimi-k2-0711-preview") == "kimi"
        assert detect_provider_id("grok-4.7") == "xai"
        assert detect_provider_id("mistral-large-latest") == "mistral"
        assert detect_provider_id("codestral-latest") == "mistral"
        assert detect_provider_id("glm-5.3") == "zai"
        assert detect_provider_id("qwen-max") == "qwen"
        assert detect_provider_id("MiniMax-M3") == "minimax"

    def test_existing_prefixes_still_work(self):
        assert detect_provider_id("claude-sonnet-4") == "anthropic"
        assert detect_provider_id("gpt-4o") == "openai"
        assert detect_provider_id("gemini-2.5-pro") == "google"

    def test_openai_reasoning_prefixes(self):
        assert detect_provider_id("o1-mini") == "openai"
        assert detect_provider_id("o3-mini") == "openai"
        assert detect_provider_id("o4-mini") == "openai"

    def test_slash_model_routes_to_openrouter(self):
        assert detect_provider_id("deepseek/deepseek-chat") == "openrouter"

    def test_unknown_routes_to_openrouter(self):
        assert detect_provider_id("some-unknown-model") == "openrouter"


class TestRegistrySanity:
    def test_all_expected_providers_present(self):
        assert _ALL_IDS == {
            "anthropic",
            "openai",
            "google",
            "openrouter",
            "deepseek",
            "kimi",
            "xai",
            "mistral",
            "zai",
            "qwen",
            "minimax",
            "groq",
            "together",
            "fireworks",
        }

    def test_every_provider_has_label_base_url_and_style(self):
        for p in PROVIDERS:
            assert p.label
            assert p.base_url.startswith("https://")
            assert p.api_style in {"anthropic", "google", "openai"}

    def test_every_provider_offers_a_way_to_pick_a_model(self):
        # Either a suggested list or "Fetch models" (free text always works).
        for p in PROVIDERS:
            assert p.suggested_models or p.discovery, p.id
            assert all(m for m in p.suggested_models), p.id

    def test_discovery_providers_have_a_models_path(self):
        for p in PROVIDERS:
            if p.discovery:
                assert p.api_style == "openai", p.id
                assert p.models_path == "/models", p.id

    def test_temperature_is_never_sent_where_providers_reject_it(self):
        assert get_provider("kimi").omit_temperature is True
        assert get_provider("minimax").omit_temperature is True

    def test_international_endpoints(self):
        assert get_provider("zai").base_url == "https://api.z.ai/api/paas/v4"
        assert get_provider("qwen").base_url.startswith("https://dashscope-intl.")
        assert get_provider("minimax").base_url == "https://api.minimax.io/v1"

    def test_anthropic_and_google_are_not_discoverable(self):
        assert get_provider("anthropic").discovery is False
        assert get_provider("google").discovery is False

    def test_openrouter_has_referer_and_title_headers(self):
        headers = dict(get_provider("openrouter").extra_headers)
        assert headers["HTTP-Referer"] == "https://sessionfs.dev"
        assert headers["X-Title"] == "SessionFS"


class TestProviderSpecs:
    def test_specs_shape(self):
        specs = provider_specs()
        assert isinstance(specs, list)
        assert len(specs) == len(PROVIDERS)
        for spec in specs:
            assert set(spec) == {"id", "label", "api_style", "suggested_models", "discovery"}
            assert isinstance(spec["suggested_models"], list)

    def test_specs_cover_every_registry_id(self):
        assert {s["id"] for s in provider_specs()} == _ALL_IDS
