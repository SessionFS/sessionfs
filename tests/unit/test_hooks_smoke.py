"""End-to-end smoke test: install hook → emit → output is hook-spec valid.

This is the v0.9.9.6 brief's twelfth test: the round-trip from
``sfs hooks install`` to ``sfs rules emit`` produces stdout that Claude
Code's hook subsystem could consume without error.

We don't shell out to actual Claude Code; instead we validate against
the documented JSON shape:

    {
      "hookSpecificOutput": {
        "hookEventName": "SessionStart",
        "additionalContext": "<string>"
      }
    }
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from typer.testing import CliRunner

from sessionfs.cli.cmd_hooks import CLAUDE_CODE_HOOK_COMMAND, hooks_app
from sessionfs.cli.cmd_rules import rules_app
from sessionfs.sync.hooks_installer import (
    install_session_start_hook,
    is_hook_installed,
)


runner = CliRunner()


def test_install_then_emit_produces_hook_spec_compliant_output(tmp_path: Path):
    """Round-trip: install lands a managed entry; emit prints the matching JSON."""
    # Step 1: install via the typer app, with paths redirected into tmp_path
    # so we don't touch the real ~/.claude/.
    user_settings = tmp_path / "settings.json"
    with patch(
        "sessionfs.cli.cmd_hooks._user_settings_path",
        return_value=user_settings,
    ):
        install_result = runner.invoke(
            hooks_app, ["install", "--for", "claude-code", "--user"]
        )
    assert install_result.exit_code == 0, install_result.output
    assert is_hook_installed(user_settings) is True

    # Inspect the installed command — emit must use exactly this string.
    data = json.loads(user_settings.read_text())
    installed_cmd = data["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert installed_cmd == CLAUDE_CODE_HOOK_COMMAND
    assert installed_cmd == "sfs rules emit --tool claude-code --format hook"

    # Step 2: simulate the hook firing by invoking `sfs rules emit` with the
    # same args the installed command uses. Patch the cache resolver so we
    # don't depend on git/cwd state — the test focuses on the JSON shape.
    sample_rules = "# Example compiled rules\n- prefer type hints\n"
    with patch(
        "sessionfs.cli.cmd_rules._resolve_cached_content",
        return_value=sample_rules,
    ):
        emit_result = runner.invoke(
            rules_app,
            ["emit", "--tool", "claude-code", "--format", "hook"],
        )
    assert emit_result.exit_code == 0, emit_result.output

    # Step 3: parse stdout as the hook envelope Claude Code expects.
    payload = json.loads(emit_result.stdout)
    assert isinstance(payload, dict)
    assert set(payload.keys()) == {"hookSpecificOutput"}
    inner = payload["hookSpecificOutput"]
    assert inner["hookEventName"] == "SessionStart"
    assert inner["additionalContext"] == sample_rules
    # Strict shape: nothing unexpected leaks into the envelope.
    assert set(inner.keys()) == {"hookEventName", "additionalContext"}
