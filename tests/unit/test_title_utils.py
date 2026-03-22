"""Tests for shared smart title extraction (utils/title_utils.py)."""

from __future__ import annotations

import pytest

from sessionfs.utils.title_utils import (
    extract_smart_title,
    is_usable_title,
    sanitize_secrets,
)


class TestExtractSmartTitle:
    def test_good_raw_title(self):
        assert extract_smart_title(raw_title="Fix the auth bug") == "Fix the auth bug"

    def test_agent_persona_title_rejected(self):
        result = extract_smart_title(
            raw_title="# Agent: Atlas — SessionFS Backend Architect",
            messages=[{"role": "user", "content": [{"type": "text", "text": "What does this do?"}]}],
        )
        assert result == "What does this do?"

    def test_xml_title_rejected(self):
        result = extract_smart_title(
            raw_title="<command-name>some-tool</command-name>",
            messages=[{"role": "user", "content": [{"type": "text", "text": "Fix login"}]}],
        )
        assert result == "Fix login"

    def test_system_message_title_rejected(self):
        result = extract_smart_title(raw_title="[Request interrupt... by user]")
        assert "[Request" not in result

    def test_persona_load_instruction_rejected(self):
        result = extract_smart_title(
            raw_title="(Load full persona from .agents/atlas-backend.md)",
            messages=[{"role": "user", "content": [{"type": "text", "text": "Deploy it"}]}],
        )
        assert result == "Deploy it"

    def test_set_mode_rejected(self):
        result = extract_smart_title(
            raw_title="Set Fast mode to ON",
            messages=[{"role": "user", "content": [{"type": "text", "text": "Real question"}]}],
        )
        assert result == "Real question"

    def test_first_user_message(self):
        messages = [
            {"role": "assistant", "content": [{"type": "text", "text": "Hello"}]},
            {"role": "user", "content": [{"type": "text", "text": "Explain the middleware"}]},
        ]
        assert extract_smart_title(messages=messages) == "Explain the middleware"

    def test_skips_sidechain(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "agent only"}], "is_sidechain": True},
            {"role": "user", "content": [{"type": "text", "text": "Real prompt"}]},
        ]
        assert extract_smart_title(messages=messages) == "Real prompt"

    def test_multiline_skips_heading(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "# Agent: Foo\n\nActual question here"}]},
        ]
        assert extract_smart_title(messages=messages) == "Actual question here"

    def test_frontmatter_skipped(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "---\ntitle: foo\n---\nReal content"}]},
        ]
        assert extract_smart_title(messages=messages) == "Real content"

    def test_code_fence_skipped(self):
        messages = [
            {"role": "user", "content": [{"type": "text", "text": "```py\ncode\n```\nExplain this"}]},
        ]
        assert extract_smart_title(messages=messages) == "Explain this"

    def test_fallback_with_count(self):
        assert extract_smart_title(message_count=42) == "Untitled session (42 messages)"

    def test_fallback_no_messages(self):
        assert extract_smart_title() == "Untitled session"

    def test_truncation(self):
        long = "Can you help me understand how the authentication middleware works and suggest improvements for security?"
        result = extract_smart_title(raw_title=long, max_length=50)
        assert len(result) <= 51  # 50 + ellipsis
        assert result.endswith("\u2026")

    def test_sentence_extraction(self):
        result = extract_smart_title(raw_title="Fix the bug. Then write tests.")
        assert result == "Fix the bug."

    def test_string_content(self):
        messages = [{"role": "user", "content": "What is sessionfs?"}]
        assert extract_smart_title(messages=messages) == "What is sessionfs?"

    def test_implement_plan_rejected(self):
        result = extract_smart_title(
            raw_title="Implement the following plan: step 1, step 2",
            messages=[{"role": "user", "content": [{"type": "text", "text": "Fix database migration"}]}],
        )
        assert result == "Fix database migration"

    def test_unicode(self):
        result = extract_smart_title(raw_title="Recherchez les erreurs dans le code")
        assert result == "Recherchez les erreurs dans le code"


class TestIsUsableTitle:
    def test_normal(self):
        assert is_usable_title("What does this function do?") is True

    def test_heading(self):
        assert is_usable_title("# Agent: Atlas") is False

    def test_xml(self):
        assert is_usable_title("<command-name>foo</command-name>") is False

    def test_bracket_system(self):
        assert is_usable_title("[Request interrupt by user]") is False

    def test_paren_load(self):
        assert is_usable_title("(Load full persona from .agents/)") is False

    def test_empty(self):
        assert is_usable_title("") is False

    def test_too_short(self):
        assert is_usable_title("ab") is False

    def test_code_fence(self):
        assert is_usable_title("```python") is False

    def test_important(self):
        assert is_usable_title("IMPORTANT: Do not do this") is False

    def test_set_mode(self):
        assert is_usable_title("Set Fast mode to ON") is False


class TestSanitizeSecrets:
    def test_password_redacted(self):
        result = sanitize_secrets('password is "secret123!"')
        assert "secret123" not in result
        assert "[redacted]" in result

    def test_api_key_redacted(self):
        result = sanitize_secrets("Use key sk-ant-abc123def456ghi789jkl012")
        assert "sk-ant-" not in result

    def test_clean_unchanged(self):
        assert sanitize_secrets("Fix the login bug") == "Fix the login bug"

    def test_allowlisted_kept(self):
        assert "sk_sfs_test123" in sanitize_secrets("Use sk_sfs_test123 for auth")
