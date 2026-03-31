"""Tests for LLM narrative summary generation."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from sessionfs.server.services.summarizer import (
    SessionSummary,
    _build_narrative_prompt,
    generate_narrative,
)


def _make_summary() -> SessionSummary:
    return SessionSummary(
        session_id="test-123",
        title="Fix auth bug",
        tool="claude-code",
        model="claude-sonnet-4",
        duration_minutes=15,
        message_count=20,
        tool_call_count=12,
        files_modified=["src/auth.py", "src/middleware.py"],
        files_read=["src/config.py"],
        commands_executed=5,
        tests_run=8,
        tests_passed=7,
        tests_failed=1,
        packages_installed=["pyjwt"],
        errors_encountered=["Error: token expired"],
    )


def _make_messages() -> list[dict]:
    return [
        {"role": "user", "content": [{"type": "text", "text": "Fix the auth bug"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "I'll investigate the auth issue."}]},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "src/auth.py"}},
        ]},
        {"role": "assistant", "content": [{"type": "text", "text": "Found the bug in token validation."}]},
    ]


@pytest.mark.asyncio
async def test_generate_narrative_no_api_key():
    """Narrative generation returns summary unchanged when no API key."""
    summary = _make_summary()
    result = await generate_narrative(summary, _make_messages(), api_key=None)
    assert result is summary
    assert result.what_happened is None
    assert result.narrative_model is None


@pytest.mark.asyncio
async def test_generate_narrative_success():
    """Narrative generation fills in fields from LLM response."""
    summary = _make_summary()
    messages = _make_messages()

    llm_response = json.dumps({
        "what_happened": "Fixed a JWT token expiration bug in the auth middleware.",
        "key_decisions": ["Switched to RS256 algorithm", "Added token refresh logic"],
        "outcome": "Auth bug resolved, 7 of 8 tests passing.",
        "open_issues": ["One flaky test remains"],
    })

    with patch("sessionfs.judge.providers.call_llm", new_callable=AsyncMock, return_value=llm_response):
        result = await generate_narrative(summary, messages, model="claude-sonnet-4", api_key="sk-test")

    assert result.what_happened == "Fixed a JWT token expiration bug in the auth middleware."
    assert result.key_decisions == ["Switched to RS256 algorithm", "Added token refresh logic"]
    assert result.outcome == "Auth bug resolved, 7 of 8 tests passing."
    assert result.open_issues == ["One flaky test remains"]
    assert result.narrative_model == "claude-sonnet-4"


@pytest.mark.asyncio
async def test_generate_narrative_llm_failure():
    """Narrative generation returns summary unchanged on LLM error."""
    summary = _make_summary()
    messages = _make_messages()

    with patch("sessionfs.judge.providers.call_llm", new_callable=AsyncMock, side_effect=Exception("API error")):
        result = await generate_narrative(summary, messages, model="claude-sonnet-4", api_key="sk-test")

    assert result.what_happened is None
    assert result.narrative_model is None


def test_build_prompt_includes_stats():
    """Prompt includes files_modified, tests, commands, and errors."""
    summary = _make_summary()
    messages = _make_messages()

    prompt = _build_narrative_prompt(summary, messages)

    assert "src/auth.py" in prompt
    assert "src/middleware.py" in prompt
    assert "7/8 passed" in prompt
    assert "1 failed" in prompt
    assert "Commands: 5" in prompt
    assert "pyjwt" in prompt
    assert "Error: token expired" in prompt


def test_build_prompt_includes_assistant_messages():
    """Prompt includes recent assistant text messages."""
    summary = _make_summary()
    messages = _make_messages()

    prompt = _build_narrative_prompt(summary, messages)

    assert "investigate the auth issue" in prompt
    assert "Found the bug" in prompt


@pytest.mark.asyncio
async def test_generate_narrative_strips_code_fences():
    """Narrative generation handles markdown code fences in LLM response."""
    summary = _make_summary()
    messages = _make_messages()

    llm_response = '```json\n' + json.dumps({
        "what_happened": "Fixed the bug.",
        "key_decisions": [],
        "outcome": "Done.",
        "open_issues": [],
    }) + '\n```'

    with patch("sessionfs.judge.providers.call_llm", new_callable=AsyncMock, return_value=llm_response):
        result = await generate_narrative(summary, messages, model="gpt-4o", api_key="sk-test")

    assert result.what_happened == "Fixed the bug."
    assert result.narrative_model == "gpt-4o"
