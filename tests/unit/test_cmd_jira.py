"""Unit tests for the `sfs ticket jira …` and `sfs org jira …` commands.

The HTTP layer is patched with async fakes (no real network); the ticket
commands resolve the project via ``_resolve_project`` and the org commands
via ``_load_api_config`` / ``_org_id``.
"""

from __future__ import annotations

import json
from unittest.mock import patch

from typer.testing import CliRunner

from sessionfs.cli.cmd_org import org_app
from sessionfs.cli.cmd_ticket import ticket_app
from tests.utils.ansi import assert_in_ansi

_PREVIEW = {
    "ticket_id": "tk_abc",
    "project_id": "proj_x",
    "summary": "Ship the auth migration",
    "jira_project_key": "ACME",
    "issue_type": "Story",
    "llm_used": False,
    "key_source": "none",
    "citations": ["description:L1-L2"],
    "sections": [
        {
            "key": "summary",
            "heading": "Summary",
            "mode": "rewrite",
            "paragraphs": [
                {"text": "Ship the auth migration", "citations": ["description:L1-L2"]},
            ],
        },
    ],
}


class _TicketApi:
    """Routes ticket jira calls by URL/method."""

    def __init__(self, preview=_PREVIEW, publish=None, status=None, status_code=200):
        self._preview = preview
        self._publish = publish
        self._status = status
        self._status_code = status_code
        self.publish_calls = []

    async def __call__(self, method, path, api_url, api_key, json_data=None):
        if path.endswith("/jira/preview"):
            return 200, self._preview, {}
        if path.endswith("/jira/publish"):
            self.publish_calls.append(json_data)
            if self._publish is not None:
                return self._publish[0], self._publish[1], {}
            return self._status_code, {"issue_key": "ACME-1",
                                       "issue_url": "https://acme.atlassian.net/browse/ACME-1"}, {}
        if path.endswith("/jira"):
            if self._status is not None:
                return self._status[0], self._status[1], {}
            return 200, {"issue_key": "ACME-1", "last_known_status": "Done",
                         "issue_url": "https://acme.atlassian.net/browse/ACME-1"}, {}
        raise AssertionError(f"unexpected: {method} {path}")


def _run_ticket(args, api):
    runner = CliRunner()
    with (
        patch("sessionfs.cli.cmd_ticket._resolve_project",
              return_value=("https://api.test", "key", "proj_x")),
        patch("sessionfs.cli.cmd_ticket._api_request", new=api),
    ):
        return runner.invoke(ticket_app, args)


def test_ticket_preview_renders_draft():
    result = _run_ticket(["jira", "preview", "tk_abc"], _TicketApi())
    assert result.exit_code == 0, result.output
    assert "Ship the auth migration" in result.output
    assert "ACME" in result.output
    assert "Story" in result.output
    assert "description:L1-L2" in result.output
    assert "Deterministic draft" in result.output


def test_ticket_preview_renders_existing_link():
    preview = dict(_PREVIEW, existing={"issue_key": "ACME-1", "issue_url": "https://x"})
    result = _run_ticket(["jira", "preview", "tk_abc"], _TicketApi(preview=preview))
    assert result.exit_code == 0
    assert "Already linked" in result.output
    assert "ACME-1" in result.output


def test_ticket_preview_out_writes_json(tmp_path):
    out = tmp_path / "preview.json"
    result = _run_ticket(["jira", "preview", "tk_abc", "--out", str(out)], _TicketApi())
    assert result.exit_code == 0, result.output
    assert "Preview written" in result.output
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["summary"] == "Ship the auth migration"
    assert written["sections"] == _PREVIEW["sections"]


def test_ticket_publish_refuses_without_from():
    result = _run_ticket(["jira", "publish", "tk_abc"], _TicketApi())
    assert result.exit_code != 0, result.output
    assert_in_ansi("--from", result.output)


def test_ticket_publish_requires_yes_without_a_tty(tmp_path, monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    from_file = tmp_path / "preview.json"
    from_file.write_text(json.dumps(_PREVIEW))
    api = _TicketApi()
    result = _run_ticket(["jira", "publish", "tk_abc", "--from", str(from_file)], api)
    assert result.exit_code == 2, result.output
    assert_in_ansi("--yes", result.output)
    assert not api.publish_calls


def test_ticket_publish_refuses_mismatched_ticket(tmp_path):
    from_file = tmp_path / "preview.json"
    from_file.write_text(json.dumps(_PREVIEW))
    api = _TicketApi()
    result = _run_ticket(["jira", "publish", "tk_other", "--from", str(from_file), "--yes"], api)
    assert result.exit_code == 1, result.output
    assert "tk_abc" in result.output
    assert "tk_other" in result.output
    assert not api.publish_calls


def test_ticket_publish_with_from_and_yes_posts_sections(tmp_path):
    from_file = tmp_path / "preview.json"
    from_file.write_text(json.dumps(_PREVIEW))
    api = _TicketApi()
    result = _run_ticket(["jira", "publish", "tk_abc", "--from", str(from_file), "--yes"], api)
    assert result.exit_code == 0, result.output
    assert "Published to Jira" in result.output
    assert "ACME-1" in result.output
    assert api.publish_calls and api.publish_calls[0]["confirm"] is True
    assert api.publish_calls[0]["summary"] == "Ship the auth migration"
    assert api.publish_calls[0]["sections"] == _PREVIEW["sections"]


def test_ticket_publish_echoes_the_previewed_template(tmp_path):
    from_file = tmp_path / "preview.json"
    preview = dict(_PREVIEW, template={"slug": "design-lite", "name": "Design Lite", "version": 3})
    from_file.write_text(json.dumps(preview))
    api = _TicketApi()
    result = _run_ticket(["jira", "publish", "tk_abc", "--from", str(from_file), "--yes"], api)
    assert result.exit_code == 0, result.output
    assert api.publish_calls[0]["template"] == "design-lite"
    assert api.publish_calls[0]["template_version"] == 3


def test_ticket_publish_without_template_in_file_sends_no_template(tmp_path):
    from_file = tmp_path / "preview.json"
    from_file.write_text(json.dumps(_PREVIEW))
    api = _TicketApi()
    result = _run_ticket(["jira", "publish", "tk_abc", "--from", str(from_file), "--yes"], api)
    assert result.exit_code == 0, result.output
    assert "template" not in api.publish_calls[0]
    assert "template_version" not in api.publish_calls[0]


def test_ticket_status_renders_issue():
    result = _run_ticket(["jira", "status", "tk_abc"], _TicketApi())
    assert result.exit_code == 0, result.output
    assert "ACME-1" in result.output
    assert "Done" in result.output


def test_ticket_status_404_no_link():
    api = _TicketApi(status=(404, {"detail": "No Jira link for this ticket"}))
    result = _run_ticket(["jira", "status", "tk_abc"], api)
    assert result.exit_code == 1
    assert "no Jira link" in result.output


def test_ticket_unlink_requires_confirm():
    result = _run_ticket(["jira", "unlink", "tk_abc"], _TicketApi())
    assert result.exit_code == 1
    assert "--confirm" in result.output


def test_ticket_unlink_with_confirm():
    api = _TicketApi(status=(204, ""))
    result = _run_ticket(["jira", "unlink", "tk_abc", "--confirm"], api)
    assert result.exit_code == 0, result.output
    assert "Unlinked Jira issue" in result.output


# ── org commands ──


class _OrgApi:
    def __init__(self, scripted: dict):
        self._scripted = scripted
        self.calls: list[tuple[str, str]] = []

    async def __call__(self, method, path, api_url, api_key, json_data=None):
        self.calls.append((method, path))
        for key, value in self._scripted.items():
            if path.endswith(key):
                status, body = value
                return status, body, {}
        raise AssertionError(f"unexpected: {method} {path}")


def _run_org(args, api):
    runner = CliRunner()
    with (
        patch("sessionfs.cli.cmd_org._load_api_config",
              return_value=("https://api.test", "key")),
        patch("sessionfs.cli.cmd_org._org_id", return_value="org_x"),
        patch("sessionfs.cli.cmd_org._api_request", new=api),
    ):
        return runner.invoke(org_app, args)


def test_org_connect_puts_connection():
    api = _OrgApi({
        "/integrations/jira": (200, {"site_url": "https://acme.atlassian.net",
                                     "account_email": "jira@acme.com"}),
    })
    result = _run_org([
        "jira", "connect", "--site-url", "https://acme.atlassian.net",
        "--email", "jira@acme.com", "--api-token", "tok", "--template", "agile",
    ], api)
    assert result.exit_code == 0, result.output
    assert "Connected Jira" in result.output
    assert "https://acme.atlassian.net" in result.output
    assert api.calls[0] == ("PUT", "/api/v1/orgs/org_x/integrations/jira")


def test_org_connect_surfaces_upstream_error():
    api = _OrgApi({"/integrations/jira": (422, {"error": "jira_connection_failed",
                                                "message": "bad"})})
    result = _run_org([
        "jira", "connect", "--site-url", "https://acme.atlassian.net",
        "--email", "jira@acme.com", "--api-token", "tok", "--template", "agile",
    ], api)
    assert result.exit_code == 1
    assert "bad" in result.output


def test_org_connect_requires_template():
    api = _OrgApi({})
    result = _run_org([
        "jira", "connect", "--site-url", "https://acme.atlassian.net",
        "--email", "jira@acme.com", "--api-token", "tok",
    ], api)
    assert result.exit_code != 0
    assert_in_ansi("--template", result.output)
    assert not api.calls


def test_org_mappings_empty():
    api = _OrgApi({"/integrations/jira/mappings": (200, [])})
    result = _run_org(["jira", "mappings"], api)
    assert result.exit_code == 0, result.output
    assert "No Jira project mappings yet" in result.output


def test_org_mappings_renders_rows():
    api = _OrgApi({"/integrations/jira/mappings": (200, [
        {"project_id": "proj_1", "jira_project_key": "ACME",
         "issue_type": "Story", "template": None, "done_status_name": None},
    ])})
    result = _run_org(["jira", "mappings"], api)
    assert result.exit_code == 0
    assert "ACME" in result.output


def test_org_health_renders_counts():
    api = _OrgApi({"/integrations/jira/health": (200, {
        "unmapped_project_count": 2, "last_push_failures": [], "stale_links": [
            {"ticket_id": "tk_1", "issue_key": "ACME-1", "last_status_checked_at": None},
        ],
    })})
    result = _run_org(["jira", "health"], api)
    assert result.exit_code == 0, result.output
    assert "Unmapped projects: 2" in result.output
    assert "ACME-1" in result.output
