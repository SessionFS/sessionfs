"""Smoke tests for `sfs persona` and `sfs ticket` CLI groups."""

from __future__ import annotations

from typer.testing import CliRunner

from sessionfs.cli.cmd_persona import persona_app
from sessionfs.cli.cmd_ticket import ticket_app

runner = CliRunner()


def test_persona_help_lists_all_commands():
    result = runner.invoke(persona_app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("list", "show", "create", "edit", "delete", "pull"):
        assert cmd in result.output


def test_persona_pull_resolve_target_prefers_existing(tmp_path):
    """`_resolve_pull_target` should reuse an existing `<name>-*.md` to
    preserve the established .agents/ filename convention on re-pull."""
    from sessionfs.cli.cmd_persona import _resolve_pull_target

    existing = tmp_path / "atlas-backend.md"
    existing.write_text("old")
    target = _resolve_pull_target(tmp_path, "atlas", "Backend Architect")
    assert target == existing


def test_persona_pull_resolve_target_falls_back_to_role_fragment(tmp_path):
    from sessionfs.cli.cmd_persona import _resolve_pull_target

    target = _resolve_pull_target(tmp_path, "forge", "DevOps and GCP Platform Engineer")
    assert target == tmp_path / "forge-devops.md"


def test_persona_pull_resolve_target_no_role(tmp_path):
    """Empty role still produces a valid filename (no leading dash)."""
    from sessionfs.cli.cmd_persona import _resolve_pull_target

    target = _resolve_pull_target(tmp_path, "rogue", "")
    assert target == tmp_path / "rogue.md"


def test_persona_pull_format_markdown_no_duplicate_h1():
    """The preamble should be HTML comments only — the persona's own
    content already starts with its identity H1, so wrapping with
    another `# Agent: …` line would create two H1s in the same file."""
    from sessionfs.cli.cmd_persona import _format_persona_markdown

    out = _format_persona_markdown({
        "name": "atlas",
        "role": "Backend Architect",
        "content": "# Agent: Atlas — Backend Architect\n\nBody.\n",
        "version": 2,
        "specializations": ["backend", "api"],
    })
    h1_count = out.count("\n# Agent") + (1 if out.startswith("# Agent") else 0)
    assert h1_count == 1, f"Expected one H1, got {h1_count}: {out!r}"
    assert "Server version: 2" in out
    assert "backend, api" in out


def test_persona_pull_requires_name_or_all():
    """Passing neither --all nor a name should be a usage error."""
    result = runner.invoke(persona_app, ["pull"])
    assert result.exit_code != 0


def test_persona_pull_rejects_name_and_all():
    """--all + name is ambiguous; refuse it."""
    result = runner.invoke(persona_app, ["pull", "atlas", "--all"])
    assert result.exit_code != 0


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
