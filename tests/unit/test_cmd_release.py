"""Unit tests for `sfs release record|link|deploy`.

The HTTP boundary is patched on `sessionfs.cli.cmd_release._api_request` so the
tests are deterministic and need no running server. Covers the R1 CLI surface:
- `record --ticket` forwards the ticket ids in the body
- `link --ticket` POSTs to the per-release tickets endpoint
- `deploy` prints the version argument (not "None", which the deploy response
  does not contain)
"""

from __future__ import annotations

from typer.testing import CliRunner

from sessionfs.cli.cmd_release import release_app

runner = CliRunner()


def _invoke(monkeypatch, args: list[str], status: int, body):
    monkeypatch.setattr(
        "sessionfs.cli.cmd_release._resolve_project",
        lambda: ("http://api.test", "k", "proj_x"),
    )
    calls: list[tuple] = []

    async def _fake_api(method, path, api_url, api_key, json_data=None, **kwargs):
        calls.append((method, path, json_data))
        return (status, body, {})

    monkeypatch.setattr("sessionfs.cli.cmd_release._api_request", _fake_api)
    result = runner.invoke(release_app, args)
    return result, calls


def test_record_forwards_ticket_ids(monkeypatch):
    result, calls = _invoke(
        monkeypatch,
        ["record", "1.0.0", "--tag-sha", "a" * 40, "--ticket", "tk_abc"],
        200,
        {
            "id": "rel_x", "version": "1.0.0", "tag_sha": "a" * 40,
            "source": "manual", "pr_ids": [], "ticket_ids": ["tk_abc"],
        },
    )
    assert result.exit_code == 0, result.output
    method, path, json_data = calls[0]
    assert method == "POST"
    assert path == "/api/v1/projects/proj_x/releases"
    assert json_data["ticket_ids"] == ["tk_abc"]
    assert "Tickets linked: tk_abc" in result.output


def test_link_posts_to_tickets_endpoint(monkeypatch):
    result, calls = _invoke(
        monkeypatch,
        ["link", "1.0.0", "--ticket", "tk_abc", "--ticket", "tk_def"],
        200,
        {"project_id": "proj_x", "version": "1.0.0", "ticket_ids": ["tk_abc", "tk_def"]},
    )
    assert result.exit_code == 0, result.output
    method, path, json_data = calls[0]
    assert method == "POST"
    assert path == "/api/v1/projects/proj_x/releases/1.0.0/tickets"
    assert json_data == {"ticket_ids": ["tk_abc", "tk_def"]}
    assert "tk_abc" in result.output
    assert "tk_def" in result.output


def test_deploy_prints_version_argument(monkeypatch):
    result, calls = _invoke(
        monkeypatch,
        ["deploy", "2.1.0", "--env", "prod"],
        200,
        {"id": "dep_x", "environment": "prod", "source": "manual"},
    )
    assert result.exit_code == 0, result.output
    assert calls[0][0] == "POST"
    assert calls[0][1] == "/api/v1/projects/proj_x/releases/2.1.0/deployments"
    assert "deployed 2.1.0 →" in result.output
    assert "None →" not in result.output
