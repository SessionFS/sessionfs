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
    # tk_457d060822bc48c0 — _get_api_config now resolves through the
    # shared profile resolver (sessionfs.profiles.resolve_auth), which
    # handles the SESSIONFS_API_KEY env path itself. The real config.toml
    # / _load_sync_config is never consulted when the env key is set.
    url, key = cmd_rules._get_api_config()
    assert url == "https://api.example.test"  # trailing slash stripped
    assert key == "test-key-from-env"


def test_get_api_config_falls_back_to_local_config(monkeypatch):
    """Without env vars, resolve through the active profile.

    tk_457d060822bc48c0 — the seam moved from cmd_cloud._load_sync_config
    to sessionfs.profiles.resolve_auth (the single resolver both the
    coordination and sync commands now share). Patch that boundary so
    the test doesn't read the developer's real ~/.sessionfs/config.toml.
    """
    from sessionfs.cli import cmd_rules
    from sessionfs.profiles import ResolvedAuth

    monkeypatch.delenv("SESSIONFS_API_KEY", raising=False)
    monkeypatch.delenv("SESSIONFS_API_URL", raising=False)
    monkeypatch.setattr(
        "sessionfs.profiles.resolve_auth",
        lambda: ResolvedAuth(
            api_url="https://api.sessionfs.dev",
            api_key="config-key",
            source="profile",
            profile_name="default",
        ),
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


def test_agent_complete_findings_rejects_non_object_elements(monkeypatch, tmp_path):
    """v0.10.2 follow-up: API model is `list[dict[str, Any]]` and rejects
    `[1]` / `["bad"]` with 422. The CLI must catch the bad shape locally
    so the run never reaches a stuck `running` state."""
    from sessionfs.cli import cmd_agent

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )

    async def _fake_api(*a, **kw):  # pragma: no cover — should not run
        raise AssertionError("API must not be called when findings shape is bad")

    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    findings = tmp_path / "findings.json"
    findings.write_text('[1, "bad"]')

    result = runner.invoke(
        agent_app,
        [
            "complete", "run_x",
            "--summary", "ok",
            "--severity", "none",
            "--findings-file", str(findings),
        ],
    )
    assert result.exit_code == 2
    assert "list of objects" in result.output
    # Specifically reports which element failed (index 0).
    assert "Element 0" in result.output


def test_agent_complete_findings_accepts_list_of_objects(monkeypatch, tmp_path):
    """Positive control: a well-formed list of objects passes the new
    guard and reaches the API."""
    from sessionfs.cli import cmd_agent

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        return (
            200,
            {
                "id": "run_x",
                "status": "passed",
                "policy_result": "pass",
                "exit_code": 0,
            },
            {},
        )

    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    findings = tmp_path / "findings.json"
    findings.write_text('[{"severity": "low", "title": "x"}]')

    result = runner.invoke(
        agent_app,
        [
            "complete", "run_x",
            "--summary", "ok",
            "--severity", "low",
            "--findings-file", str(findings),
        ],
    )
    assert result.exit_code == 0


# ── v0.17.0 `sfs agent feedback …` ───────────────────────────────


def test_agent_feedback_add_posts_body_and_prints_id(monkeypatch):
    """add builds the POST body from flags (target + vocabulary) and
    emits the created id."""
    from sessionfs.cli import cmd_agent

    captured = {}

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        captured["method"] = method
        captured["path"] = path
        captured["json_data"] = json_data
        return (201, {
            "id": "afb_abc123",
            "project_id": "proj_a",
            "feedback_type": "route",
            "verdict": "negative",
            "label": "wrong_route",
            "final_owner": "sentinel",
            "notes": None,
            "source": "human",
            "ticket_id": "tk_1",
            "agent_run_id": None,
            "comment_id": None,
            "created_by_user_id": "u",
            "actor_type": "user",
            "service_key_id": None,
            "service_key_name": None,
            "created_at": "2026-10-13T00:00:00Z",
        }, {})

    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(agent_app, [
        "feedback", "add",
        "--type", "route",
        "--verdict", "negative",
        "--label", "wrong_route",
        "--ticket", "tk_1",
        "--final-owner", "sentinel",
    ])
    assert result.exit_code == 0, result.output
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/projects/proj_a/agent-feedback"
    assert captured["json_data"]["feedback_type"] == "route"
    assert captured["json_data"]["ticket_id"] == "tk_1"
    assert captured["json_data"]["final_owner"] == "sentinel"
    assert "afb_abc123" in result.output


def test_agent_feedback_list_json_emits_parseable_list(monkeypatch):
    import json
    from sessionfs.cli import cmd_agent

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        return (200, [{
            "id": "afb_abc123",
            "project_id": "proj_a",
            "feedback_type": "route",
            "verdict": "negative",
            "label": "wrong_route",
            "source": "auto",
            "ticket_id": "tk_1",
            "agent_run_id": None,
            "comment_id": None,
            "final_owner": "sentinel",
            "notes": None,
            "created_by_user_id": "u",
            "actor_type": "user",
            "service_key_id": None,
            "service_key_name": None,
            "created_at": "2026-10-13T00:00:00Z",
        }], {})

    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(agent_app, ["feedback", "list", "--format", "json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output.strip())
    assert isinstance(parsed, list)
    assert parsed[0]["id"] == "afb_abc123"


def test_agent_feedback_summary_json_emits_parseable_object(monkeypatch):
    import json
    from sessionfs.cli import cmd_agent

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        return (200, {
            "total": 3,
            "groups": [
                {"feedback_type": "route", "verdict": "negative",
                 "label": "wrong_route", "count": 2},
                {"feedback_type": "summary", "verdict": "positive",
                 "label": "summary_good", "count": 1},
            ],
        }, {})

    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(agent_app, ["feedback", "summary", "--format", "json"])
    assert result.exit_code == 0
    parsed = json.loads(result.output.strip())
    assert parsed["total"] == 3
    assert len(parsed["groups"]) == 2


# ── GitHub #95 `sfs agent replay` / `sfs agent compare` ───────────


def _replay_response() -> dict:
    return {
        "run": {
            "id": "run_new",
            "project_id": "proj_a",
            "persona_name": "atlas",
            "tool": "generic",
            "trigger_source": "manual",
            "status": "queued",
            "replayed_from": "run_parent",
            "prompt": "edited",
            "context_refs": ["a", "b"],
        },
        "command": "sfs agent run --run-id run_new",
        "ci_hint": (
            "# Replay this agent run in CI.\n"
            "github_actions:\n"
            "  - name: Replay agent run\n"
            "    run: sfs agent run --run-id run_new\n"
            "gitlab_ci:\n"
            "  replay:\n"
            "    script:\n"
            "      - sfs agent run --run-id run_new\n"
        ),
    }


def test_agent_replay_posts_body_and_output_id_prints_only_id(monkeypatch):
    from sessionfs.cli import cmd_agent

    captured = {}

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        captured["method"] = method
        captured["path"] = path
        captured["json_data"] = json_data
        return (201, _replay_response(), {})

    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(agent_app, [
        "replay", "run_parent",
        "--prompt", "edited",
        "--ticket", "tk_2",
        "--persona", "sentinel",
        "--context-ref", "a",
        "--context-ref", "b",
        "--output-id",
    ])
    assert result.exit_code == 0, result.output
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/projects/proj_a/agent-runs/run_parent/replay"
    assert captured["json_data"] == {
        "prompt": "edited",
        "ticket_id": "tk_2",
        "persona_name": "sentinel",
        "context_refs": ["a", "b"],
    }
    assert result.output.strip() == "run_new"


def test_agent_replay_prompt_file_reads_content(monkeypatch, tmp_path):
    from sessionfs.cli import cmd_agent

    captured = {}

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        captured["json_data"] = json_data
        return (201, _replay_response(), {})

    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("prompt from a file\nline two")

    result = runner.invoke(agent_app, ["replay", "run_parent", "--prompt-file", str(prompt_file)])
    assert result.exit_code == 0, result.output
    assert captured["json_data"] == {"prompt": "prompt from a file\nline two"}


def test_agent_replay_rejects_prompt_and_prompt_file_together(monkeypatch):
    from sessionfs.cli import cmd_agent

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )
    result = runner.invoke(
        agent_app,
        ["replay", "run_parent", "--prompt", "x", "--prompt-file", "/tmp/x"],
    )
    assert result.exit_code == 2
    assert "not both" in result.output


def test_agent_compare_json_emits_parseable_object(monkeypatch):
    import json
    from sessionfs.cli import cmd_agent

    captured = {}

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        captured["path"] = path
        return (200, {
            "a_id": "run_a",
            "b_id": "run_b",
            "a_findings_count": 2,
            "b_findings_count": 1,
            "a_severity_counts": {"high": 1, "low": 1},
            "b_severity_counts": {"high": 1},
            "added": [{"rule": "r3"}],
            "removed": [{"rule": "r2"}],
            "unchanged": [{"rule": "r1"}],
        }, {})

    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(agent_app, ["compare", "run_a", "run_b", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/v1/projects/proj_a/agent-runs/run_a/compare/run_b"
    parsed = json.loads(result.output.strip())
    assert parsed["a_id"] == "run_a"
    assert [f["rule"] for f in parsed["added"]] == ["r3"]


def test_agent_run_resume_run_id_starts_existing_run(monkeypatch):
    """GitHub #95: `--run-id` resumes the queued replay instead of creating."""
    from sessionfs.cli import cmd_agent

    captured = {}

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        captured["method"] = method
        captured["path"] = path
        captured["json_data"] = json_data
        return (200, {"id": "run_x", "status": "running"}, {})

    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(agent_app, ["run", "--run-id", "run_x", "--output-id"])
    assert result.exit_code == 0, result.output
    assert captured["method"] == "POST"
    assert captured["path"] == "/api/v1/projects/proj_a/agent-runs/run_x/start"
    assert captured["json_data"] is None
    assert result.output.strip() == "run_x"


def test_agent_run_run_id_and_persona_mutually_exclusive(monkeypatch):
    from sessionfs.cli import cmd_agent

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )
    result = runner.invoke(agent_app, ["run", "atlas", "--run-id", "run_x"])
    assert result.exit_code == 2
    assert "not both" in result.output


def test_agent_replay_route_404_prints_upgrade_hint(monkeypatch):
    """GitHub #95: servers without the replay route answer 405 (another
    route owns the path) in the standard error envelope."""
    from sessionfs.cli import cmd_agent

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        return (405, {"error": {"code": "405", "message": "Method Not Allowed"}}, {})

    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(agent_app, ["replay", "run_parent"])
    assert result.exit_code == 1
    assert "doesn't support replay" in result.output


def test_agent_replay_domain_404_prints_not_found(monkeypatch):
    """GitHub #95: a domain 404 means the run itself is missing."""
    from sessionfs.cli import cmd_agent

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        return (404, {"error": {"code": "404", "message": "Agent run not found"}}, {})

    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(agent_app, ["replay", "run_parent"])
    assert result.exit_code == 1
    assert "Run run_parent not found" in result.output


def test_agent_replay_bare_not_found_prints_upgrade_hint(monkeypatch):
    from sessionfs.cli import cmd_agent

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        return (404, {"error": {"code": "404", "message": "Not Found"}}, {})

    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(agent_app, ["replay", "run_parent"])
    assert result.exit_code == 1
    assert "doesn't support replay" in result.output


def test_agent_run_run_id_rejects_create_only_flags(monkeypatch):
    from sessionfs.cli import cmd_agent

    calls: list[str] = []

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        calls.append(path)
        return (200, {}, {})

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )
    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(agent_app, ["run", "--run-id", "run_x", "--fail-on", "high"])
    assert result.exit_code == 2
    assert "--fail-on" in result.output
    assert calls == []


def test_agent_run_run_id_rejects_new_record_flags(monkeypatch):
    """Review Loop R3 A1: model/provider/role/branch are create-only."""
    from sessionfs.cli import cmd_agent

    calls: list[str] = []

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        calls.append(path)
        return (200, {}, {})

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )
    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(agent_app, [
        "run", "--run-id", "run_x", "--model", "claude-sonnet-5-5",
    ])
    assert result.exit_code == 2
    assert "--model" in result.output
    assert calls == []


# ── Review Loop R3 A1: agent-run record fields (cost/model/loop) ──


def test_agent_run_forwards_record_fields(monkeypatch):
    from sessionfs.cli import cmd_agent

    calls: list[tuple[str, str, dict | None]] = []

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        calls.append((method, path, json_data))
        if path.endswith("/start"):
            return (200, {"id": "run_x", "compiled_context": ""}, {})
        return (200, {"id": "run_x", "status": "queued"}, {})

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )
    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(agent_app, [
        "run", "atlas",
        "--model", "claude-sonnet-5-5",
        "--provider", "anthropic",
        "--role", "implementer",
        "--branch", "feat/rl-a1",
    ])
    assert result.exit_code == 0, result.output

    create_call = next(c for c in calls if c[1].endswith("/agent-runs"))
    assert create_call[2] == {
        "persona_name": "atlas",
        "tool": "generic",
        "trigger_source": "manual",
        "model": "claude-sonnet-5-5",
        "provider": "anthropic",
        "role": "implementer",
        "branch": "feat/rl-a1",
    }


def test_agent_complete_forwards_usage_fields(monkeypatch):
    from sessionfs.cli import cmd_agent

    captured: dict = {}

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        captured["method"] = method
        captured["path"] = path
        captured["json_data"] = json_data
        return (200, {"id": "run_x", "status": "passed",
                      "policy_result": "pass", "exit_code": 0}, {})

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )
    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(agent_app, [
        "complete", "run_x", "--summary", "ok",
        "--input-tokens", "100",
        "--output-tokens", "50",
        "--cache-read-tokens", "10",
        "--cache-write-tokens", "5",
        "--cost-usd", "0.25",
        "--cost-basis", "reported",
        "--head-sha", "abcdef1234567",
        "--exit-reason", "finished",
    ])
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/v1/projects/proj_a/agent-runs/run_x/complete"
    body = captured["json_data"]
    assert body["input_tokens"] == 100
    assert body["output_tokens"] == 50
    assert body["cache_read_tokens"] == 10
    assert body["cache_write_tokens"] == 5
    assert body["cost_usd"] == 0.25
    assert body["cost_basis"] == "reported"
    assert body["head_sha"] == "abcdef1234567"
    assert body["exit_reason"] == "finished"


def test_agent_usage_posts_body_and_prints_confirm(monkeypatch):
    from sessionfs.cli import cmd_agent

    captured: dict = {}

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        captured["method"] = method
        captured["path"] = path
        captured["json_data"] = json_data
        return (200, {"id": "run_x", "input_tokens": 100, "output_tokens": 50,
                      "cost_usd": 0.25}, {})

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )
    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(agent_app, [
        "usage", "run_x", "--input-tokens", "100", "--output-tokens", "50",
        "--cost-usd", "0.25",
    ])
    assert result.exit_code == 0, result.output
    assert captured["path"] == "/api/v1/projects/proj_a/agent-runs/run_x/usage"
    assert captured["json_data"] == {
        "input_tokens": 100,
        "output_tokens": 50,
        "cost_usd": 0.25,
    }
    assert "Recorded usage for run_x" in result.output


def test_agent_usage_requires_at_least_one_field(monkeypatch):
    from sessionfs.cli import cmd_agent

    calls: list[str] = []

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        calls.append(path)
        return (200, {}, {})

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )
    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(agent_app, ["usage", "run_x"])
    assert result.exit_code == 2
    assert "at least one usage field" in result.output
    assert calls == []


def test_agent_usage_conflict_prints_terminal_message(monkeypatch):
    from sessionfs.cli import cmd_agent

    async def _fake_api(method, path, api_url, api_key, json_data=None, extra_headers=None):
        return (409, {"error": {"code": "409", "message": "conflict"}}, {})

    monkeypatch.setattr(
        cmd_agent, "_resolve_project",
        lambda: ("https://api.test", "k", "proj_a"),
    )
    monkeypatch.setattr(cmd_agent, "_api_request", _fake_api)

    result = runner.invoke(agent_app, ["usage", "run_x", "--input-tokens", "10"])
    assert result.exit_code == 1
    assert "usage can't be changed" in result.output
