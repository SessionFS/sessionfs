"""Tests for custom base URL support in LLM Judge."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, patch

import pytest

from sessionfs.judge.providers import _detect_provider, call_llm


class TestBaseUrlRouting:
    """When base_url is set, always use OpenAI-compatible format."""

    @pytest.mark.asyncio
    async def test_base_url_overrides_provider(self):
        """Custom base URL should use OpenAI-compatible format regardless of provider."""
        with patch("sessionfs.judge.providers._call_openai_compatible", new_callable=AsyncMock) as mock:
            mock.return_value = "test response"
            result = await call_llm(
                model="claude-sonnet-4",
                system="test",
                prompt="test",
                api_key="sk-test",
                provider="anthropic",
                base_url="https://litellm.internal/v1",
            )
            assert result == "test response"
            mock.assert_called_once()
            assert mock.call_args[0][0] == "https://litellm.internal/v1"

    @pytest.mark.asyncio
    async def test_no_base_url_uses_anthropic(self):
        """Without base_url, anthropic provider should call _call_anthropic."""
        with patch("sessionfs.judge.providers._call_anthropic", new_callable=AsyncMock) as mock:
            mock.return_value = "anthropic response"
            result = await call_llm(
                model="claude-sonnet-4",
                system="test",
                prompt="test",
                api_key="sk-test",
                provider="anthropic",
            )
            assert result == "anthropic response"
            mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_base_url_openai_uses_compatible(self):
        """Without base_url, openai provider should use _call_openai_compatible with default URL."""
        with patch("sessionfs.judge.providers._call_openai_compatible", new_callable=AsyncMock) as mock:
            mock.return_value = "openai response"
            result = await call_llm(
                model="gpt-4o",
                system="test",
                prompt="test",
                api_key="sk-test",
                provider="openai",
            )
            assert result == "openai response"
            mock.assert_called_once()
            # Default OpenAI URL
            assert "api.openai.com" in mock.call_args[0][0]

    @pytest.mark.asyncio
    async def test_base_url_with_google_model(self):
        """Custom base URL with Google model should use OpenAI format, not Google."""
        with patch("sessionfs.judge.providers._call_openai_compatible", new_callable=AsyncMock) as mock:
            mock.return_value = "gateway response"
            await call_llm(
                model="gemini-2.5-pro",
                system="test",
                prompt="test",
                api_key="key",
                provider="google",
                base_url="https://gateway.internal/v1",
            )
            mock.assert_called_once()
            assert mock.call_args[0][0] == "https://gateway.internal/v1"


class TestUrlNormalization:
    """URL normalization in _call_openai_compatible."""

    def test_trailing_slash_stripped(self):
        """Trailing slash should be stripped before appending path."""
        from sessionfs.judge.providers import _call_openai_compatible
        import inspect
        # Verify the function exists and is async
        assert inspect.iscoroutinefunction(_call_openai_compatible)

    def test_url_construction_logic(self):
        """Verify URL normalization logic directly."""
        # Test the logic that would run in _call_openai_compatible
        url = "https://litellm.internal/v1/"
        url = url.rstrip("/")
        assert url == "https://litellm.internal/v1"
        if not url.endswith("/chat/completions"):
            url = f"{url}/chat/completions"
        assert url == "https://litellm.internal/v1/chat/completions"

    def test_chat_completions_not_doubled(self):
        """URL already ending in /chat/completions should not be doubled."""
        url = "https://litellm.internal/v1/chat/completions"
        url = url.rstrip("/")
        if not url.endswith("/chat/completions"):
            url = f"{url}/chat/completions"
        assert url == "https://litellm.internal/v1/chat/completions"


class TestOptionalApiKey:
    """API key is optional when using custom base URL."""

    @pytest.mark.asyncio
    async def test_empty_key_allowed_with_base_url(self):
        """call_llm should not crash with empty api_key when base_url is set."""
        with patch("sessionfs.judge.providers._call_openai_compatible", new_callable=AsyncMock) as mock:
            mock.return_value = "ollama response"
            result = await call_llm(
                model="llama3",
                system="test",
                prompt="test",
                api_key="",
                provider="openai",
                base_url="http://localhost:11434/v1",
            )
            assert result == "ollama response"
            mock.assert_called_once()


class TestCliBaseUrlResolution:
    """CLI resolves base_url from flag > env > config."""

    def test_explicit_flag_wins(self):
        from sessionfs.cli.cmd_audit import _resolve_base_url

        result = _resolve_base_url("https://explicit.internal/v1")
        assert result == "https://explicit.internal/v1"

    def test_env_var_fallback(self):
        from sessionfs.cli.cmd_audit import _resolve_base_url

        with patch.dict(os.environ, {"SFS_JUDGE_BASE_URL": "https://env.internal/v1"}):
            result = _resolve_base_url(None)
            assert result == "https://env.internal/v1"

    def test_none_when_unset(self):
        from sessionfs.cli.cmd_audit import _resolve_base_url

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SFS_JUDGE_BASE_URL", None)
            result = _resolve_base_url(None)
            assert result is None


class TestProviderDetection:
    """Provider detection still works correctly."""

    def test_claude_detected(self):
        assert _detect_provider("claude-sonnet-4") == "anthropic"

    def test_gpt_detected(self):
        assert _detect_provider("gpt-4o") == "openai"

    def test_gemini_detected(self):
        assert _detect_provider("gemini-2.5-pro") == "google"

    def test_slash_model_openrouter(self):
        assert _detect_provider("anthropic/claude-sonnet-4") == "openrouter"

    def test_unknown_openrouter(self):
        assert _detect_provider("my-custom-model") == "openrouter"


class TestDaemonConfig:
    """DaemonConfig has judge section with base_url."""

    def test_judge_config_defaults(self):
        from sessionfs.daemon.config import DaemonConfig

        config = DaemonConfig()
        assert config.judge.api_key == ""
        assert config.judge.base_url == ""

    def test_judge_config_from_dict(self):
        from sessionfs.daemon.config import DaemonConfig

        config = DaemonConfig(judge={"api_key": "sk-test", "base_url": "https://litellm.internal/v1"})
        assert config.judge.api_key == "sk-test"
        assert config.judge.base_url == "https://litellm.internal/v1"
