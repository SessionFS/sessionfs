"""Unit tests for `sfs org jira templates list|show`.

Covers GitHub #98 (custom Jira templates, CLI surface):
  - `sfs org jira templates list` renders built-in and org-custom rows
  - `sfs org jira templates show <slug>` renders each section's heading,
    mode, source fields and guidance
  - `--versions` lists the version history for a custom template
  - a non-2xx response surfaces a friendly error and exits non-zero

The HTTP boundary is patched on `sessionfs.cli.cmd_org._api_request` so the
tests are deterministic and need no running server.
"""

from __future__ import annotations

from typer.testing import CliRunner

from sessionfs.cli.cmd_org import org_app

runner = CliRunner()


def _invoke(monkeypatch, args: list[str], status: int, body, headers=None):
    """Drive the org jira CLI with a stubbed API config and HTTP boundary."""

    monkeypatch.setattr(
        "sessionfs.cli.cmd_org._load_api_config",
        lambda: ("http://api.test", "k"),
    )
    monkeypatch.setattr("sessionfs.cli.cmd_org._org_id", lambda api_url, api_key: "org_x")

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None, params=None):
        return (status, body, headers or {})

    monkeypatch.setattr("sessionfs.cli.cmd_org._api_request", _fake_api)
    return runner.invoke(org_app, ["jira", "templates", *args])


def test_templates_list_renders_builtins_and_customs(monkeypatch):
    result = _invoke(
        monkeypatch,
        ["list"],
        200,
        [
            {
                "slug": "agile",
                "name": "Agile Standard",
                "builtin": True,
                "version": None,
                "archived_at": None,
            },
            {
                "slug": "design-lite",
                "name": "Design Lite",
                "builtin": False,
                "version": 3,
                "archived_at": None,
            },
            {
                "slug": "old-tpl",
                "name": "Old",
                "builtin": False,
                "version": 1,
                "archived_at": "2026-01-01T00:00:00Z",
            },
        ],
    )
    assert result.exit_code == 0
    assert "agile" in result.output
    assert "Agile Standard" in result.output
    assert "built-in" in result.output
    assert "design-lite" in result.output
    assert "v3" in result.output
    assert "custom" in result.output
    assert "archived" in result.output


def test_templates_show_renders_sections_and_guidance(monkeypatch):
    result = _invoke(
        monkeypatch,
        ["show", "design-lite"],
        200,
        {
            "slug": "design-lite",
            "name": "Design Lite",
            "description": "A lean UX brief",
            "builtin": False,
            "version": 2,
            "sections": [
                {
                    "key": "journey",
                    "heading": "User journey",
                    "mode": "rewrite",
                    "source_fields": ["description", "linked_sessions"],
                    "guidance": "Keep it to three steps.",
                },
                {
                    "key": "metric",
                    "heading": "Success metric",
                    "mode": "verbatim",
                    "source_fields": ["acceptance_criteria"],
                    "guidance": None,
                },
            ],
        },
    )
    assert result.exit_code == 0
    assert "Design Lite" in result.output
    assert "design-lite" in result.output
    assert "v2" in result.output
    assert "User journey" in result.output
    assert "rewrite" in result.output
    assert "description, linked_sessions" in result.output
    assert "Keep it to three steps." in result.output
    assert "Success metric" in result.output
    assert "verbatim" in result.output


def test_templates_show_versions_lists_history(monkeypatch):
    calls = []

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None, params=None):
        calls.append(path)
        if path.endswith("/versions"):
            return (
                200,
                [
                    {"version": 2, "created_at": "2026-02-01T00:00:00Z"},
                    {"version": 1, "created_at": "2026-01-01T00:00:00Z"},
                ],
                {},
            )
        return (
            200,
            {"slug": "design-lite", "name": "Design Lite", "builtin": False, "version": 2, "sections": []},
            {},
        )

    monkeypatch.setattr(
        "sessionfs.cli.cmd_org._load_api_config",
        lambda: ("http://api.test", "k"),
    )
    monkeypatch.setattr("sessionfs.cli.cmd_org._org_id", lambda api_url, api_key: "org_x")
    monkeypatch.setattr("sessionfs.cli.cmd_org._api_request", _fake_api)

    result = runner.invoke(org_app, ["jira", "templates", "show", "design-lite", "--versions"])
    assert result.exit_code == 0
    assert "Versions:" in result.output
    assert "v2" in result.output
    assert "v1" in result.output
    # The versions endpoint was hit for a custom template.
    assert any(p.endswith("/design-lite/versions") for p in calls)


def test_templates_show_skips_versions_for_builtins(monkeypatch):
    result = _invoke(
        monkeypatch,
        ["show", "agile", "--versions"],
        200,
        {"slug": "agile", "name": "Agile Standard", "builtin": True, "version": None, "sections": []},
    )
    assert result.exit_code == 0
    assert "Versions:" not in result.output


def test_templates_list_surfaces_api_errors(monkeypatch):
    result = _invoke(
        monkeypatch,
        ["list"],
        403,
        {"error": {"code": "403", "message": "Forbidden"}},
    )
    assert result.exit_code != 0
    assert "API error (403)" in result.output


def test_templates_show_url_quotes_the_slug(monkeypatch):
    monkeypatch.setattr(
        "sessionfs.cli.cmd_org._load_api_config",
        lambda: ("http://api.test", "k"),
    )
    monkeypatch.setattr("sessionfs.cli.cmd_org._org_id", lambda api_url, api_key: "org_x")
    paths: list[str] = []

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None, params=None):
        paths.append(path)
        return (404, {"detail": "Template not found"}, {})

    monkeypatch.setattr("sessionfs.cli.cmd_org._api_request", _fake_api)
    result = runner.invoke(org_app, ["jira", "templates", "show", "../mappings?x=1"])
    assert result.exit_code == 1
    assert paths == [
        "/api/v1/orgs/org_x/integrations/jira/templates/..%2Fmappings%3Fx%3D1"
    ]


def test_templates_list_rejects_an_unexpected_shape(monkeypatch):
    result = _invoke(monkeypatch, ["list"], 200, {"not": "a list"})
    assert result.exit_code == 1
    assert "Unexpected response" in result.output


def test_templates_show_rejects_an_unexpected_shape(monkeypatch):
    result = _invoke(monkeypatch, ["show", "design-lite"], 200, ["not", "an", "object"])
    assert result.exit_code == 1
    assert "Unexpected response" in result.output


def test_templates_show_escapes_rich_markup_in_template_text(monkeypatch):
    result = _invoke(
        monkeypatch,
        ["show", "design-lite"],
        200,
        {
            "slug": "design-lite",
            "name": "[red]Design[/red]",
            "builtin": False,
            "version": 1,
            "archived_at": None,
            "sections": [
                {"key": "s", "heading": "[bold]H[/bold]", "mode": "rewrite",
                 "source_fields": ["title"], "guidance": "[link=x]g[/link]"},
            ],
        },
    )
    assert result.exit_code == 0, result.output
    assert "[red]Design[/red]" in result.output
    assert "[bold]H[/bold]" in result.output
