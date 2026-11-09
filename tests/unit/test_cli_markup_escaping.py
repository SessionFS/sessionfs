"""User-controlled text must never be parsed as rich markup by the CLI.

A ticket title containing ``[/]`` crashed ``sfs ticket list`` with a
MarkupError, and the shared error handler then crashed again while
printing that error (its message contains brackets too).
"""

from __future__ import annotations

import pytest
from rich.console import Console

from sessionfs.cli import cmd_ticket, common


def _capture(monkeypatch, module, attr) -> Console:
    console = Console(record=True, width=200, force_terminal=False)
    monkeypatch.setattr(module, attr, console)
    return console


def test_ticket_table_prints_bracket_titles_literally(monkeypatch):
    console = _capture(monkeypatch, cmd_ticket, "console")
    cmd_ticket._print_ticket_table([
        {
            "id": "tk_1",
            "title": "fix [/] and [link=https://evil.example]click[/link]",
            "status": "open",
            "priority": "low",
            "assigned_to": "[bold]atlas[/bold]",
        }
    ])
    out = console.export_text()
    assert "fix [/] and [link=https://evil.example]click[/link]" in out
    assert "[bold]atlas[/bold]" in out


def test_handle_errors_prints_bracket_messages(monkeypatch):
    console = _capture(monkeypatch, common, "err_console")

    @common.handle_errors
    def boom():
        raise ValueError("closing tag '[/]' at position 8 has nothing to close")

    with pytest.raises(SystemExit) as info:
        boom()
    assert info.value.code == 1
    assert "closing tag '[/]' at position 8" in console.export_text()


@pytest.mark.parametrize("status", ["closed", "needs_human", "brand_new_status"])
def test_ticket_table_handles_statuses_without_a_style(monkeypatch, status):
    # An empty style used to render "[]closed[/]", which rich rejects.
    console = _capture(monkeypatch, cmd_ticket, "console")
    cmd_ticket._print_ticket_table([
        {"id": "tk_2", "title": "t", "status": status, "priority": "urgent", "assigned_to": None}
    ])
    out = console.export_text()
    assert status in out and "urgent" in out


def test_styled_escapes_and_skips_empty_style():
    assert cmd_ticket._styled("comment", "") == "comment"
    assert cmd_ticket._styled("[x]", "green") == "[green]\\[x][/]"
