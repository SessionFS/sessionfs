"""Unit tests for SessionStart hook install/uninstall/status logic.

Covers the seven behaviours the v0.9.9.6 brief enumerates:

5.  install on empty settings.json creates the hook
6.  install is idempotent
7.  install preserves existing user hooks
8.  uninstall removes only SessionFS-managed entries (sentinel match)
9.  uninstall preserves user hooks
10. status reports correctly across all tool slots
11. malformed settings.json → clear error, no crash

We exercise the underlying merge logic in
:mod:`sessionfs.sync.hooks_installer` directly (pure-function, no IO mocks
needed) and invoke the typer ``status`` command via ``CliRunner`` so the
output rendering itself is covered.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sessionfs.cli.cmd_hooks import CLAUDE_CODE_HOOK_COMMAND, hooks_app
from sessionfs.sync.hooks_installer import (
    MalformedSettingsError,
    install_session_start_hook,
    is_hook_installed,
    uninstall_session_start_hook,
)


runner = CliRunner()


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def test_install_on_empty_settings_creates_hook(tmp_path: Path):
    """Test 5: install on missing settings.json creates a managed entry."""
    settings = tmp_path / "settings.json"
    assert not settings.exists()

    changed = install_session_start_hook(settings, CLAUDE_CODE_HOOK_COMMAND)

    assert changed is True
    assert settings.exists()
    data = json.loads(settings.read_text())
    entries = data["hooks"]["SessionStart"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["sfs:managed"] is True
    assert entry["hooks"][0]["type"] == "command"
    assert entry["hooks"][0]["command"] == CLAUDE_CODE_HOOK_COMMAND


def test_install_is_idempotent(tmp_path: Path):
    """Test 6: running install twice is a no-op the second time."""
    settings = tmp_path / "settings.json"

    first = install_session_start_hook(settings, CLAUDE_CODE_HOOK_COMMAND)
    assert first is True

    second = install_session_start_hook(settings, CLAUDE_CODE_HOOK_COMMAND)
    assert second is False, "second install should report no change"

    # Exactly one managed entry — no duplicates.
    data = json.loads(settings.read_text())
    entries = data["hooks"]["SessionStart"]
    managed = [e for e in entries if isinstance(e, dict) and e.get("sfs:managed")]
    assert len(managed) == 1


def test_install_preserves_existing_user_hooks(tmp_path: Path):
    """Test 7: user-defined hooks at every level survive install."""
    settings = tmp_path / "settings.json"
    pre_existing = {
        "model": "claude-sonnet-4-5",  # arbitrary user setting at top level
        "hooks": {
            "PreToolUse": [{"hooks": [{"type": "command", "command": "echo before"}]}],
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "echo user-start"}]}
            ],
        },
    }
    settings.write_text(json.dumps(pre_existing, indent=2))

    install_session_start_hook(settings, CLAUDE_CODE_HOOK_COMMAND)

    data = json.loads(settings.read_text())
    # Top-level non-hook keys preserved.
    assert data["model"] == "claude-sonnet-4-5"
    # Other hook events preserved.
    assert data["hooks"]["PreToolUse"][0]["hooks"][0]["command"] == "echo before"
    # User SessionStart entry still there alongside our managed one.
    session_start = data["hooks"]["SessionStart"]
    assert len(session_start) == 2
    user_entries = [e for e in session_start if not e.get("sfs:managed")]
    managed_entries = [e for e in session_start if e.get("sfs:managed")]
    assert len(user_entries) == 1
    assert user_entries[0]["hooks"][0]["command"] == "echo user-start"
    assert len(managed_entries) == 1
    assert managed_entries[0]["hooks"][0]["command"] == CLAUDE_CODE_HOOK_COMMAND


# ---------------------------------------------------------------------------
# uninstall
# ---------------------------------------------------------------------------


def test_uninstall_removes_only_managed_entries(tmp_path: Path):
    """Test 8: only entries with the sentinel are removed."""
    settings = tmp_path / "settings.json"
    install_session_start_hook(settings, CLAUDE_CODE_HOOK_COMMAND)
    assert is_hook_installed(settings) is True

    changed = uninstall_session_start_hook(settings)
    assert changed is True
    assert is_hook_installed(settings) is False
    # File still exists; SessionStart slot was the only one and got pruned.
    data = json.loads(settings.read_text())
    assert "hooks" not in data  # tidied empty hooks dict


def test_uninstall_preserves_user_hooks(tmp_path: Path):
    """Test 9: user-defined SessionStart entries survive uninstall."""
    settings = tmp_path / "settings.json"
    user_only = {
        "hooks": {
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "echo user"}]}
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "echo stop"}]}],
        }
    }
    settings.write_text(json.dumps(user_only, indent=2))

    # Now install our hook on top, then uninstall.
    install_session_start_hook(settings, CLAUDE_CODE_HOOK_COMMAND)
    uninstall_session_start_hook(settings)

    data = json.loads(settings.read_text())
    # User SessionStart survived.
    session_start = data["hooks"]["SessionStart"]
    assert len(session_start) == 1
    assert session_start[0]["hooks"][0]["command"] == "echo user"
    # Stop event untouched.
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "echo stop"


# ---------------------------------------------------------------------------
# status (CLI)
# ---------------------------------------------------------------------------


def test_status_reports_each_tool_slot(tmp_path: Path):
    """Test 10: status renders user/project/N/A lines for every tool."""
    user_settings = tmp_path / "user_settings.json"
    install_session_start_hook(user_settings, CLAUDE_CODE_HOOK_COMMAND)

    # Patch the path resolvers so the test doesn't touch real ~/.claude/.
    with patch(
        "sessionfs.cli.cmd_hooks._user_settings_path",
        return_value=user_settings,
    ), patch(
        "sessionfs.cli.cmd_hooks._project_settings_path",
        return_value=None,  # simulate "not in a git repo"
    ):
        result = runner.invoke(hooks_app, ["status"])

    assert result.exit_code == 0, result.output
    out = result.stdout
    # User scope: installed.
    assert "claude-code (user)" in out
    assert "INSTALLED" in out
    # Project scope: simulated as not-in-repo.
    assert "claude-code (project)" in out
    assert "not in a git repo" in out
    # Capture-only tools rendered as N/A.
    for tool in ("codex", "gemini", "cursor", "copilot"):
        assert tool in out
    assert "N/A" in out


# ---------------------------------------------------------------------------
# malformed input
# ---------------------------------------------------------------------------


def test_malformed_settings_raises_clear_error(tmp_path: Path):
    """Test 11: parse failures raise a typed error rather than crashing."""
    settings = tmp_path / "settings.json"
    settings.write_text("{not valid json at all")

    with pytest.raises(MalformedSettingsError) as ei:
        install_session_start_hook(settings, CLAUDE_CODE_HOOK_COMMAND)
    # Error message names the path so the user can find the file.
    assert str(settings) in str(ei.value)

    # is_hook_installed treats malformed as "not installed" rather than crashing.
    assert is_hook_installed(settings) is False
