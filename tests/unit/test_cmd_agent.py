"""Smoke tests for `sfs agent` CLI group."""

from __future__ import annotations

from typer.testing import CliRunner

from sessionfs.cli.cmd_agent import agent_app

runner = CliRunner()


def test_agent_help_lists_all_commands():
    result = runner.invoke(agent_app, ["--help"])
    assert result.exit_code == 0
    for cmd in ("run", "complete", "status", "list"):
        assert cmd in result.output


def test_agent_run_requires_persona():
    result = runner.invoke(agent_app, ["run"])
    assert result.exit_code != 0


def test_agent_complete_requires_summary():
    result = runner.invoke(agent_app, ["complete", "run_x"])
    assert result.exit_code != 0
    assert "summary" in result.output.lower()


def test_agent_status_format_markdown_renders_step_summary(monkeypatch):
    """KB ticket criterion: --format markdown emits GitHub/GitLab step-
    summary-compatible markdown (plain print, no Rich decorations)."""
    from sessionfs.cli import cmd_agent

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        return (
            200,
            {
                "id": "run_42",
                "persona_name": "atlas",
                "status": "failed",
                "severity": "high",
                "findings_count": 3,
                "policy_result": "fail",
                "exit_code": 1,
                "result_summary": "3 issues found.",
            },
            {},
        )

    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(agent_app, ["status", "run_42", "--format", "markdown"])
    assert result.exit_code == 0
    # Markdown headings + bullets must reach stdout.
    assert "### AgentRun `run_42`" in result.output
    assert "**Persona:** atlas" in result.output
    assert "**Status:** `failed`" in result.output
    assert "**Findings:** 3" in result.output


def test_agent_status_format_json_emits_parseable_json(monkeypatch):
    """--format json writes parseable JSON to stdout (machine-safe)."""
    import json
    from sessionfs.cli import cmd_agent

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        return (
            200,
            {"id": "run_42", "persona_name": "atlas", "status": "passed",
             "findings_count": 0, "severity": "none"},
            {},
        )

    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(agent_app, ["status", "run_42", "--format", "json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output.strip())
    assert parsed["id"] == "run_42"


def test_get_api_config_honors_env_vars(monkeypatch):
    """Post-Round 1 MEDIUM: CI runners with only SESSIONFS_API_KEY set
    must authenticate without `sfs auth login` first."""
    from sessionfs.cli import cmd_rules

    monkeypatch.setenv("SESSIONFS_API_KEY", "test-key-from-env")
    monkeypatch.setenv("SESSIONFS_API_URL", "https://api.example.test/")
    # _load_sync_config must NOT be called — env overrides take precedence.
    def _explode():
        raise RuntimeError("_load_sync_config should not be called when env is set")
    monkeypatch.setattr("sessionfs.cli.cmd_cloud._load_sync_config", _explode)

    url, key = cmd_rules._get_api_config()
    assert url == "https://api.example.test"  # trailing slash stripped
    assert key == "test-key-from-env"


def test_get_api_config_falls_back_to_local_config(monkeypatch):
    """Without env vars, fall back to the ~/.sessionfs sync config."""
    from sessionfs.cli import cmd_rules

    monkeypatch.delenv("SESSIONFS_API_KEY", raising=False)
    monkeypatch.delenv("SESSIONFS_API_URL", raising=False)
    monkeypatch.setattr(
        "sessionfs.cli.cmd_cloud._load_sync_config",
        lambda: {"api_url": "https://api.sessionfs.dev", "api_key": "config-key"},
    )

    url, key = cmd_rules._get_api_config()
    assert url == "https://api.sessionfs.dev"
    assert key == "config-key"


def test_agent_complete_enforce_exits_nonzero_for_failed_status_even_with_exit_code_0(monkeypatch):
    """Post-Round 1 HIGH (CLI side): --enforce must exit non-zero for
    failed/errored even if the stored exit_code is 0 (defense-in-depth
    against pre-fix rows).
    """
    from sessionfs.cli import cmd_agent

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        # Simulated server response where status=failed but exit_code is
        # somehow 0 (e.g. a row written before the HIGH server-side fix).
        return (
            200,
            {
                "id": "run_x", "status": "failed",
                "policy_result": "pass", "exit_code": 0,
            },
            {},
        )

    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(
        agent_app,
        ["complete", "run_x", "--summary", "ok", "--severity", "none", "--enforce"],
    )
    # CLI's defense-in-depth kicks in: failed status → exit 1 regardless.
    assert result.exit_code == 1


def test_ticket_create_output_id_prints_only_id(monkeypatch):
    """KB ticket criterion: `sfs ticket create --output-id` prints
    exactly the ticket id, suitable for CI scripting."""
    from sessionfs.cli import cmd_ticket

    monkeypatch.setattr(
        cmd_ticket, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        return 201, {"id": "tk_abcdef0123", "title": "Test"}, {}

    monkeypatch.setattr(cmd_ticket, "_api_request", _fake_api)

    from sessionfs.cli.cmd_ticket import ticket_app
    result = runner.invoke(
        ticket_app,
        ["create", "--title", "Test", "--output-id"],
    )
    assert result.exit_code == 0
    # The id must appear as a standalone line (the `print(body["id"])`
    # statement). The "Created ticket ..." confirmation goes to
    # err_console — under a real shell those streams are separable via
    # `$(sfs ticket create ... --output-id)` which only captures stdout.
    # Here CliRunner merges streams, so we just check the id is on its
    # own line (not embedded inside the confirmation prose).
    lines = result.output.splitlines()
    assert "tk_abcdef0123" in lines, (
        f"Expected 'tk_abcdef0123' as a standalone line in output, got: {result.output!r}"
    )
