"""Unit tests for cli.common.confirm_or_exit (v0.9.9.10 B1 fix).

Bug: `sfs push` showed "Unexpected error:" (empty body) when stdin was
piped and DLP was positive. click.confirm hits EOF immediately and
raises click.exceptions.Abort, which inherits from RuntimeError (not
ClickException), so the v0.9.9.8 handle_errors pass-through missed it.

Fix is two-pronged: a preemptive isatty() check in confirm_or_exit
that exits cleanly BEFORE typer.confirm runs, and an Abort-specific
handler in handle_errors as a backstop.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from sessionfs.cli.common import confirm_or_exit


def test_yes_flag_bypasses_prompt_entirely():
    """When yes=True, returns True without touching stdin or typer."""
    with patch("sys.stdin.isatty", return_value=False):
        # Even with no TTY, --yes wins — never prompts, never exits.
        assert confirm_or_exit("Continue?", yes=True) is True


def test_non_interactive_exits_with_clear_hint(monkeypatch, capsys):
    """Piped stdin: clean SystemExit(2) + a message naming --yes,
    NOT a click.Abort that falls through handle_errors as
    "Unexpected error:".
    """
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    with pytest.raises(SystemExit) as exc_info:
        confirm_or_exit("Continue pushing with findings?", default=False)

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    out = (captured.out + captured.err).lower()
    assert "continue pushing" in out  # message echoed
    assert "no interactive input" in out
    assert "--yes" in out  # hint visible


def test_non_interactive_with_custom_yes_hint(monkeypatch, capsys):
    """Caller can override the hint to be DLP-specific or context-specific."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    with pytest.raises(SystemExit):
        confirm_or_exit(
            "Continue pushing with these findings?",
            yes_hint="Pass --yes to push despite DLP findings.",
        )

    captured = capsys.readouterr()
    out = captured.out + captured.err
    assert "Pass --yes to push despite DLP findings" in out


def test_interactive_delegates_to_typer_confirm(monkeypatch):
    """When stdin IS a TTY and --yes isn't set, falls through to the
    normal typer.confirm path (we don't reimplement the prompt)."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    with patch("typer.confirm", return_value=True) as mock_confirm:
        result = confirm_or_exit("Proceed?", default=True)

    assert result is True
    mock_confirm.assert_called_once_with("Proceed?", default=True)


def test_interactive_user_says_no(monkeypatch):
    """Negative path still works — typer.confirm returns False, we
    propagate False rather than exit."""
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    with patch("typer.confirm", return_value=False):
        result = confirm_or_exit("Proceed?")
    assert result is False


def test_handle_errors_swallows_abort_with_friendly_message(capsys):
    """Backstop: even if some other call site bypasses confirm_or_exit
    and lets click.Abort fly, handle_errors catches it explicitly now
    instead of printing "Unexpected error:".
    """
    import click.exceptions

    from sessionfs.cli.common import handle_errors

    @handle_errors
    def _explodes():
        raise click.exceptions.Abort()

    with pytest.raises(SystemExit) as exc_info:
        _explodes()

    assert exc_info.value.code == 130
    captured = capsys.readouterr()
    out = (captured.out + captured.err).lower()
    assert "cancelled" in out
    assert "--yes" in out  # hint visible here too
    # The generic-Exception path would have printed this — must NOT appear.
    assert "unexpected error" not in out
