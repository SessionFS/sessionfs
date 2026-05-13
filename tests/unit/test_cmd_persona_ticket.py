"""Smoke tests for `sfs persona` and `sfs ticket` CLI groups."""

from __future__ import annotations

from typer.testing import CliRunner

from sessionfs.cli.cmd_persona import persona_app
from sessionfs.cli.cmd_ticket import ticket_app

runner = CliRunner()


def test_persona_help_lists_all_commands():
    result = runner.invoke(persona_app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("list", "show", "create", "edit", "delete"):
        assert cmd in result.output


def test_ticket_help_lists_all_commands():
    result = runner.invoke(ticket_app, ["--help"])
    assert result.exit_code == 0
    for cmd in (
        "list", "show", "create", "start", "complete", "comment",
        "status", "block", "unblock", "reopen", "approve", "dismiss",
    ):
        assert cmd in result.output


def test_persona_create_requires_role():
    result = runner.invoke(persona_app, ["create", "atlas"])
    assert result.exit_code != 0
    assert "role" in result.output.lower()


def test_ticket_create_requires_title():
    result = runner.invoke(ticket_app, ["create"])
    assert result.exit_code != 0
    assert "title" in result.output.lower()


def test_ticket_complete_requires_notes():
    result = runner.invoke(ticket_app, ["complete", "tk_x"])
    assert result.exit_code != 0
    assert "notes" in result.output.lower()


def test_ticket_status_no_bundle(tmp_path, monkeypatch):
    """`sfs ticket status` with no bundle prints 'No active ticket.'."""
    from sessionfs import active_ticket as at
    monkeypatch.setattr(at, "bundle_path", lambda: tmp_path / "missing.json")
    result = runner.invoke(ticket_app, ["status"])
    assert result.exit_code == 0
    assert "no active ticket" in result.output.lower()


def test_ticket_status_with_bundle(tmp_path, monkeypatch):
    """`sfs ticket status` reads and pretty-prints the bundle."""
    from sessionfs import active_ticket as at
    monkeypatch.setattr(at, "bundle_path", lambda: tmp_path / "active.json")
    at.write_bundle(ticket_id="tk_42", persona_name="atlas", project_id="proj_a")
    result = runner.invoke(ticket_app, ["status"])
    assert result.exit_code == 0
    # Rich Panel may wrap output, so check substrings without strict layout.
    out = result.output.replace("\n", " ")
    assert "tk_42" in out
    assert "atlas" in out
    assert "proj_a" in out


def test_persona_delete_409_surfaces_error_envelope(monkeypatch):
    """KB 341 LOW: the global error handler wraps HTTPException(detail=...)
    into `body["error"]["message"]`. The CLI must read that shape too —
    otherwise the user sees an empty red line instead of the actual count.
    """
    from sessionfs.cli import cmd_persona

    monkeypatch.setattr(
        cmd_persona, "_resolve_project",
        lambda: ("https://api.test", "test-key", "proj_a"),
    )

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        return (
            409,
            {"error": {"code": "409", "message": "Persona 'atlas' is still assigned to 3 non-terminal ticket(s)."}},
            {},
        )

    monkeypatch.setattr(cmd_persona, "_api_request", _fake_api)

    result = runner.invoke(persona_app, ["delete", "atlas", "--yes"])
    assert result.exit_code != 0
    # The server-rendered message must reach the user.
    assert "still assigned to 3 non-terminal" in result.output
    # And the actionable hint with the persona name in it must too.
    assert "atlas" in result.output


def test_ticket_start_warns_on_bundle_write_failure(monkeypatch):
    """KB 339 LOW: when write_bundle returns False, the CLI must NOT
    print 'Active ticket bundle written' — it must surface a warning so
    the user knows the daemon won't tag subsequent sessions."""
    from sessionfs.cli import cmd_ticket

    monkeypatch.setattr(
        cmd_ticket, "_resolve_project",
        lambda: ("https://api.test", "test-key", "proj_a"),
    )

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        return (
            200,
            {"ticket": {"id": "tk_x", "assigned_to": "atlas"}, "compiled_context": ""},
            {},
        )

    monkeypatch.setattr(cmd_ticket, "_api_request", _fake_api)
    monkeypatch.setattr(cmd_ticket, "write_bundle", lambda **kw: False)

    result = runner.invoke(
        ticket_app, ["start", "tk_x", "--no-print-context"]
    )
    assert result.exit_code == 0
    assert "could not write" in result.output.lower()
    assert "Active ticket bundle written" not in result.output
