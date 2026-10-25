"""``sfs audit`` takes a key only from the provider the request goes to."""

from __future__ import annotations

import pytest

from sessionfs.cli import cmd_audit

ALL_VARS = (
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY",
    "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "KIMI_API_KEY",
    "XAI_API_KEY", "MISTRAL_API_KEY", "ZAI_API_KEY", "DASHSCOPE_API_KEY",
    "MINIMAX_API_KEY", "GROQ_API_KEY", "TOGETHER_API_KEY", "FIREWORKS_API_KEY",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ALL_VARS:
        monkeypatch.delenv(var, raising=False)

    class _Cfg:
        def model_dump(self):
            return {"judge": {"api_key": ""}}

    monkeypatch.setattr("sessionfs.daemon.config.load_config", lambda: _Cfg())


def test_never_sends_another_vendors_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    assert cmd_audit._resolve_api_key(None, "deepseek-flash") is None
    assert cmd_audit._resolve_api_key(None, "m", "groq") is None


def test_uses_the_detected_providers_variable(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-ds")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant")
    assert cmd_audit._resolve_api_key(None, "deepseek-flash") == "sk-ds"
    assert cmd_audit._resolve_api_key(None, "claude-sonnet-5-5") == "sk-ant"


def test_explicit_provider_wins_over_detection(monkeypatch):
    monkeypatch.setenv("MOONSHOT_API_KEY", "sk-kimi")
    assert cmd_audit._resolve_api_key(None, "some-model", "kimi") == "sk-kimi"


def test_unknown_model_without_provider_goes_to_openrouter_key_only(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai")
    assert cmd_audit._resolve_api_key(None, "some-model") is None
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or")
    assert cmd_audit._resolve_api_key(None, "some-model") == "sk-or"


def test_explicit_key_and_config_key_still_win(monkeypatch):
    assert cmd_audit._resolve_api_key("sk-flag", "deepseek-flash") == "sk-flag"
