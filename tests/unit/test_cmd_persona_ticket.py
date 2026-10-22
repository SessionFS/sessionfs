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


# ── GH #70 — org-level personas ──


def _record_api(monkeypatch, responses):
    """Patch the CLI API layer; returns the list of (method, path, json)."""
    from sessionfs.cli import cmd_persona

    calls: list[tuple[str, str, object]] = []
    monkeypatch.setattr(
        cmd_persona, "_resolve_project",
        lambda: ("https://api.test", "test-key", "proj_a"),
    )
    monkeypatch.setattr(
        cmd_persona, "_get_api_config", lambda: ("https://api.test", "test-key"),
    )

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        calls.append((method, path, json_data))
        return responses(method, path)

    monkeypatch.setattr(cmd_persona, "_api_request", _fake_api)
    return calls


def test_persona_list_shows_scope_column(monkeypatch):
    calls = _record_api(monkeypatch, lambda m, p: (200, [
        {"name": "atlas", "role": "Backend", "scope": "project", "specializations": []},
        {"name": "gcp-infra", "role": "TF", "scope": "org", "specializations": []},
    ], {}))
    result = runner.invoke(persona_app, ["list"])
    assert result.exit_code == 0, result.output
    assert calls[0][1] == "/api/v1/projects/proj_a/personas"
    assert "Org" in result.output and "Project" in result.output


def test_persona_list_no_org_and_org_flags(monkeypatch):
    calls = _record_api(monkeypatch, lambda m, p: (200, [], {}))
    runner.invoke(persona_app, ["list", "--no-org"])
    assert calls[-1][1] == "/api/v1/projects/proj_a/personas?include_org=false"
    runner.invoke(persona_app, ["list", "--org", "org_1"])
    assert calls[-1][1] == "/api/v1/orgs/org_1/personas"


def test_persona_create_and_delete_with_org(monkeypatch):
    def _resp(method, path):
        if method == "POST":
            return (201, {"name": "release", "scope": "org"}, {})
        return (204, None, {})

    calls = _record_api(monkeypatch, _resp)
    result = runner.invoke(
        persona_app, ["create", "release", "--role", "Gov", "--content", "# R", "--org", "org_1"],
    )
    assert result.exit_code == 0, result.output
    assert calls[-1][0] == "POST" and calls[-1][1] == "/api/v1/orgs/org_1/personas"
    assert calls[-1][2]["name"] == "release"

    result = runner.invoke(persona_app, ["delete", "release", "--yes", "--force", "--org", "org_1"])
    assert result.exit_code == 0, result.output
    assert calls[-1] == ("DELETE", "/api/v1/orgs/org_1/personas/release?force=true", None)


# ── GitHub #81 — the member an org persona represents ──

_ROSTER = {"members": [{"user_id": "u_2", "email": "kelvin@example.com"}]}


def test_persona_create_represents_needs_org_and_is_sent(monkeypatch):
    calls = _record_api(monkeypatch, lambda m, p: (201, {"name": "kelvin", "scope": "org"}, {}))
    result = runner.invoke(
        persona_app, ["create", "kelvin", "--role", "P", "--content", "# K", "--represents", "u_2"],
    )
    assert result.exit_code == 2
    assert calls == []
    result = runner.invoke(
        persona_app,
        ["create", "kelvin", "--role", "P", "--content", "# K", "--org", "org_1",
         "--represents", "kelvin@example.com"],
    )
    assert result.exit_code == 0, result.output
    assert calls[-1][2]["represented_user"] == "kelvin@example.com"


def test_persona_represent_sets_and_clears(monkeypatch):
    def _resp(method, path):
        if path.endswith("/members"):
            return (200, _ROSTER, {})
        return (200, {"name": "kelvin", "represented_user_id": "u_2"}, {})

    calls = _record_api(monkeypatch, _resp)
    result = runner.invoke(
        persona_app, ["represent", "kelvin", "--org", "org_1", "--user", "kelvin@example.com"],
    )
    assert result.exit_code == 0, result.output
    assert calls[0] == (
        "PUT", "/api/v1/orgs/org_1/personas/kelvin", {"represented_user": "kelvin@example.com"},
    )
    assert "kelvin@example.com" in result.output

    result = runner.invoke(persona_app, ["represent", "kelvin", "--org", "org_1", "--clear"])
    assert result.exit_code == 0, result.output
    assert calls[-1] == ("PUT", "/api/v1/orgs/org_1/personas/kelvin", {"represented_user": None})

    for args in (["--user", "u_2", "--clear"], []):
        result = runner.invoke(persona_app, ["represent", "kelvin", "--org", "org_1", *args])
        assert result.exit_code == 2, args


def test_persona_represent_shows_server_refusal(monkeypatch):
    _record_api(monkeypatch, lambda m, p: (422, {"error": {
        "code": "represented_user_not_member",
        "message": "represented_user must be a current member of this org.",
    }}, {}))
    result = runner.invoke(
        persona_app, ["represent", "kelvin", "--org", "org_1", "--user", "x@example.com"],
    )
    assert result.exit_code == 1
    assert "current member" in result.output


def test_persona_show_prints_represented_member(monkeypatch):
    def _resp(method, path):
        if path.endswith("/members"):
            return (200, _ROSTER, {})
        return (200, {
            "name": "kelvin", "role": "P", "content": "", "scope": "org",
            "org_id": "org_1", "represented_user_id": "u_2",
        }, {})

    _record_api(monkeypatch, _resp)
    result = runner.invoke(persona_app, ["show", "kelvin", "--org", "org_1"])
    assert result.exit_code == 0, result.output
    assert "Represents:" in result.output
    assert "kelvin@example.com" in result.output


def test_persona_edit_refuses_org_persona_without_org_flag(monkeypatch):
    _record_api(monkeypatch, lambda m, p: (200, {
        "name": "gcp-infra", "role": "TF", "content": "x", "scope": "org", "org_id": "org_1",
    }, {}))
    result = runner.invoke(persona_app, ["edit", "gcp-infra"])
    assert result.exit_code != 0
    assert "--org org_1" in result.output


def test_persona_404_keeps_server_detail_and_suggests_org_flag(monkeypatch):
    detail = (
        "Persona 'gcp-infra' is an org-level persona; manage it under "
        "/api/v1/orgs/org_1/personas (org admins), or create a project persona "
        "with the same name to override it."
    )
    _record_api(monkeypatch, lambda m, p: (404, {"error": {"code": "404", "message": detail}}, {}))
    result = runner.invoke(persona_app, ["delete", "gcp-infra", "--yes"])
    assert result.exit_code != 0
    assert "org-level persona" in result.output
    assert "--org org_1" in result.output


def test_persona_assume_writes_canonical_name(monkeypatch, tmp_path):
    from sessionfs import active_ticket as _at

    bundle = tmp_path / "active.json"
    monkeypatch.setattr(_at, "bundle_path", lambda: bundle)
    _record_api(monkeypatch, lambda m, p: (200, {"name": "Atlas", "scope": "org"}, {}))
    result = runner.invoke(persona_app, ["assume", "ATLAS"])
    assert result.exit_code == 0, result.output
    import json as _json
    assert _json.loads(bundle.read_text())["persona_name"] == "Atlas"


def test_persona_404_plain_falls_back_to_short_message(monkeypatch):
    _record_api(monkeypatch, lambda m, p: (404, {"error": {"code": "404", "message": "Persona not found"}}, {}))
    result = runner.invoke(persona_app, ["show", "ghost"])
    assert result.exit_code != 0
    assert "Persona 'ghost' not found." in result.output


def test_persona_pull_all_writes_org_personas(monkeypatch, tmp_path):
    calls = _record_api(monkeypatch, lambda m, p: (200, [
        {"name": "atlas", "role": "Backend Architect", "scope": "project", "content": "# A\n", "version": 1},
        {"name": "gcp-infra", "role": "Terraform engineer", "scope": "org", "org_id": "org_1",
         "content": "# G\n", "version": 3},
    ], {}))
    result = runner.invoke(persona_app, ["pull", "--all", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert calls[0][1] == "/api/v1/projects/proj_a/personas"
    assert (tmp_path / "atlas-backend.md").exists()
    org_file = tmp_path / "gcp-infra-terraform.md"
    assert org_file.exists()
    assert "Scope: org persona (org_1)" in org_file.read_text()

    runner.invoke(persona_app, ["pull", "--all", "--no-org", "--dir", str(tmp_path), "--force"])
    assert calls[-1][1] == "/api/v1/projects/proj_a/personas?include_org=false"
