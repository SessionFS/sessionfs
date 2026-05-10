"""Unit tests for `sfs rules emit` (offline, hook-friendly).

Covers the four behaviours the v0.9.9.6 brief enumerates:

1. ``emit --tool claude-code --format hook`` returns valid JSON with
   ``hookSpecificOutput.additionalContext``.
2. ``emit --tool claude-code --format file`` returns plain text body.
3. Empty cache → empty ``additionalContext``, exit 0 (so Claude Code
   never reports a hook failure during normal use).
4. Unknown tool → error, exit 1.

We invoke the typer command via ``CliRunner`` because that exercises the
real argument parsing and `typer.Exit` semantics. We also patch
``_resolve_cached_content`` so the test doesn't depend on git/cwd state.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from typer.testing import CliRunner

from sessionfs.cli.cmd_rules import rules_app


runner = CliRunner()


def test_emit_hook_format_returns_valid_envelope():
    """Valid hook envelope: hookSpecificOutput.{hookEventName, additionalContext}."""
    sample_content = "# Project rules\nUse type hints everywhere.\n"
    with patch(
        "sessionfs.cli.cmd_rules._resolve_cached_content",
        return_value=sample_content,
    ):
        result = runner.invoke(
            rules_app,
            ["emit", "--tool", "claude-code", "--format", "hook"],
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert "hookSpecificOutput" in payload
    inner = payload["hookSpecificOutput"]
    assert inner["hookEventName"] == "SessionStart"
    assert inner["additionalContext"] == sample_content


def test_emit_file_format_returns_plain_body():
    """File format prints the raw rule content with no JSON wrapping."""
    sample_content = "# rules\nbody line one\nbody line two\n"
    with patch(
        "sessionfs.cli.cmd_rules._resolve_cached_content",
        return_value=sample_content,
    ):
        result = runner.invoke(
            rules_app,
            ["emit", "--tool", "claude-code", "--format", "file"],
        )

    assert result.exit_code == 0, result.output
    # Plain body: no JSON envelope, no Rich markup, no extra trailing newline
    # injected by typer.echo (we passed nl=False).
    assert result.stdout == sample_content


def test_emit_empty_cache_yields_empty_context_exit_zero():
    """Hook must not break Claude Code startup when the cache is empty."""
    with patch(
        "sessionfs.cli.cmd_rules._resolve_cached_content",
        return_value="",
    ):
        result = runner.invoke(
            rules_app,
            ["emit", "--tool", "claude-code", "--format", "hook"],
        )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["additionalContext"] == ""
    assert payload["hookSpecificOutput"]["hookEventName"] == "SessionStart"


def test_emit_unknown_tool_errors():
    """Unknown tool slugs must exit non-zero with a clear message."""
    result = runner.invoke(
        rules_app,
        ["emit", "--tool", "nonexistent-tool", "--format", "hook"],
    )
    assert result.exit_code != 0
    # The message goes to stderr in the typer runner with mix_stderr=False.
    assert "Unknown tool" in (result.stderr or "")
