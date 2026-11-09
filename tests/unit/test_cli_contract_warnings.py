"""The CLI surfaces API-contract notices on stderr (tk_3855ef8b11f94ee2)."""

from __future__ import annotations

import io

from rich.console import Console

from sessionfs.cli import cmd_rules


def _capture(monkeypatch) -> io.StringIO:
    buf = io.StringIO()
    monkeypatch.setattr(cmd_rules, "err_console", Console(file=buf, width=200))
    return buf


def test_ignored_fields_warning(monkeypatch):
    buf = _capture(monkeypatch)
    cmd_rules._warn_contract_headers({"x-sessionfs-ignored-fields": "name,[bold]x"})
    assert "ignored these fields: name,[bold]x" in buf.getvalue()


def test_deprecation_warning(monkeypatch):
    buf = _capture(monkeypatch)
    cmd_rules._warn_contract_headers(
        {"deprecation": "true", "sunset": "Tue, 01 Jun 2027 00:00:00 GMT"}
    )
    assert "deprecated. It may be removed after Tue, 01 Jun 2027" in buf.getvalue()


def test_no_warning_without_headers(monkeypatch):
    buf = _capture(monkeypatch)
    cmd_rules._warn_contract_headers({"content-type": "application/json"})
    assert buf.getvalue() == ""
