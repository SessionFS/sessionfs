"""CLI tests for `sfs meeting …` (GitHub #116, M2). The API is faked."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from sessionfs.cli import cmd_meeting
from sessionfs.cli.cmd_meeting import meeting_app

runner = CliRunner()

_DRAFT = {
    "meeting_id": "meet_1",
    "item_id": "mi_1",
    "title": "Ship the thing.",
    "description": "Ship it\n\nFrom meeting *Sync*",
    "priority": "medium",
    "kind": "task",
    "assigned_to": None,
    "assigned_user_id": "u_1",
    "due_date": "2026-11-01",
    "context_refs": ["meeting:meet_1#mi_1"],
    "target_project_id": "proj_1",
    "target_project_options": None,
}

_MEETING = {
    "id": "meet_1",
    "title": "Weekly sync",
    "meeting_at": "2026-10-29T10:00:00Z",
    "created_at": "2026-10-29T10:00:00Z",
    "line_count": 2,
    "extraction_status": "done",
    "content": "Ann: ship it\nBob: ok",
    "participants": [
        {"id": "mp_1", "name_raw": "Ann", "email_raw": None, "user_id": "u_1",
         "persona_name": None, "resolution_method": "email"},
        {"id": "mp_2", "name_raw": "Bob", "email_raw": None, "user_id": None,
         "persona_name": None, "resolution_method": "unresolved"},
    ],
    "action_items": [
        {"id": "mi_1", "kind": "action", "description": "Ship it", "owner_participant_id": "mp_1",
         "due_date": "2026-11-01", "line_start": 1, "line_end": 1, "status": "pending",
         "promoted_ticket_id": None},
    ],
}


class Fake:
    def __init__(self, responses=None):
        self.calls: list[dict] = []
        self.responses = responses or {}

    async def __call__(self, method, path, api_url, api_key, json_data=None, extra_headers=None, params=None, timeout=30):
        self.calls.append(
            {"method": method, "path": path, "json": json_data, "params": params, "timeout": timeout}
        )
        status, body = self.responses.get((method, path), (200, {}))
        return status, body, {}


@pytest.fixture
def api(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(cmd_meeting, "_api_request", fake)
    monkeypatch.setattr(cmd_meeting, "_get_api_config", lambda: ("https://api.test", "key"))
    return fake


def _run(args, input=None):
    return runner.invoke(meeting_app, args, input=input)


def test_upload_file_sets_file_source_and_group_route(api, tmp_path):
    f = tmp_path / "m.vtt"
    f.write_text("WEBVTT\n\n00:00.000 --> 00:01.000\nhi\n")
    api.responses[("POST", "/api/v1/groups/pg_1/meetings")] = (
        200, {"id": "meet_9", "line_count": 1, "dlp_findings": 2, "warnings": ["duplicate_of: meet_1"]},
    )
    res = _run(["upload", str(f), "--title", "Sync", "--group", "pg_1", "--date", "2026-10-29"])
    assert res.exit_code == 0, res.output
    assert "meet_9" in res.output and "duplicate_of" in res.output
    call = api.calls[0]
    assert call["json"]["source"] == "file_upload"
    assert call["json"]["meeting_at"] == "2026-10-29"
    assert "extract" not in call["json"]
    assert call["json"]["content"].startswith("WEBVTT")


def test_upload_stdin_project_extract(api):
    api.responses[("POST", "/api/v1/projects/proj_1/meetings")] = (
        200, {"id": "meet_2", "line_count": 1, "extraction_status": "done",
              "participants": [], "action_items": [{}], "dropped_items": 1},
    )
    res = _run(["upload", "--stdin", "--title", "T", "--project", "proj_1", "--extract"], input="Ann: hi\n")
    assert res.exit_code == 0, res.output
    assert api.calls[0]["json"]["source"] == "manual_paste"
    assert api.calls[0]["json"]["extract"] is True
    assert "1 dropped" in res.output


def test_upload_argument_errors(api, tmp_path):
    assert _run(["upload", "--title", "T", "--project", "p"]).exit_code == 1
    f = tmp_path / "a.txt"
    f.write_text("x")
    assert _run(["upload", str(f), "--stdin", "--title", "T", "--project", "p"]).exit_code == 1
    assert _run(["upload", str(f), "--title", "T", "--project", "p", "--group", "g"]).exit_code == 1
    empty = tmp_path / "e.txt"
    empty.write_text("  \n")
    assert _run(["upload", str(empty), "--title", "T", "--project", "p"]).exit_code == 1
    assert _run(["upload", str(tmp_path / "missing.txt"), "--title", "T", "--project", "p"]).exit_code == 1
    assert not api.calls


def test_list_and_search_params(api):
    api.responses[("GET", "/api/v1/projects/proj_1/meetings")] = (
        200, {"meetings": [dict(_MEETING, content=None)], "next_cursor": "meet_1"},
    )
    res = _run(["list", "--project", "proj_1", "-q", "roadmap", "-n", "5", "--before", "meet_7"])
    assert res.exit_code == 0, res.output
    assert api.calls[0]["params"] == {"limit": "5", "q": "roadmap", "before": "meet_7"}
    assert "Weekly sync" in res.output
    assert (
        "sfs meeting list --project proj_1 --query roadmap --limit 5 --before meet_1"
        in res.output
    )
    res = _run(["search", "budget", "--project", "proj_1"])
    assert res.exit_code == 0
    assert api.calls[1]["params"] == {"q": "budget", "limit": "25"}


def test_show_numbers_lines_and_lists_items(api):
    api.responses[("GET", "/api/v1/meetings/meet_1")] = (200, _MEETING)
    res = _run(["show", "meet_1"])
    assert res.exit_code == 0, res.output
    assert "1  Ann: ship it" in res.output and "2  Bob: ok" in res.output
    assert "unresolved" in res.output and "Ship it" in res.output and "L1-1" in res.output
    assert "Ann: ship it" not in _run(["show", "meet_1", "--no-transcript"]).output


def test_participants_and_action_items(api):
    api.responses[("GET", "/api/v1/meetings/meet_1")] = (200, _MEETING)
    assert "mp_2" in _run(["participants", "meet_1"]).output
    assert "mi_1" in _run(["action-items", "meet_1"]).output


def test_resolve_participant_needs_exactly_one(api):
    assert _run(["resolve-participant", "meet_1", "mp_2"]).exit_code == 1
    assert _run(["resolve-participant", "meet_1", "mp_2", "--user", "u", "--persona", "p"]).exit_code == 1
    assert not api.calls
    api.responses[("PUT", "/api/v1/meetings/meet_1/participants/mp_2")] = (
        200, {"name_raw": "Bob", "persona_name": "atlas", "user_id": None},
    )
    res = _run(["resolve-participant", "meet_1", "mp_2", "--persona", "atlas"])
    assert res.exit_code == 0 and "atlas" in res.output
    assert api.calls[0]["json"] == {"persona_name": "atlas"}


def test_delete_requires_confirmation(api):
    res = _run(["delete", "meet_1"])  # non-interactive, no --yes
    assert res.exit_code == 2
    assert not api.calls
    assert _run(["delete", "meet_1", "--yes"]).exit_code == 0
    assert api.calls[0]["method"] == "DELETE"


def test_extract_and_dismiss_restore(api):
    api.responses[("POST", "/api/v1/meetings/meet_1/extract")] = (
        200, dict(_MEETING, dropped_items=0),
    )
    assert "done" in _run(["extract", "meet_1"]).output
    assert _run(["dismiss", "meet_1", "mi_1"]).exit_code == 0
    assert _run(["restore", "meet_1", "mi_1"]).exit_code == 0
    assert [c["path"] for c in api.calls[1:]] == [
        "/api/v1/meetings/meet_1/action-items/mi_1/dismiss",
        "/api/v1/meetings/meet_1/action-items/mi_1/restore",
    ]


def _promote_api(api):
    api.responses[("POST", "/api/v1/meetings/meet_1/action-items/mi_1/promote/preview")] = (200, dict(_DRAFT))
    api.responses[("POST", "/api/v1/meetings/meet_1/action-items/mi_1/promote")] = (
        200, {"ticket_id": "tk_1", "warnings": ["jira_skipped"]},
    )


def test_promote_without_yes_shows_preview_and_creates_nothing(api):
    _promote_api(api)
    res = _run(["promote", "meet_1", "mi_1"])  # non-interactive: refuses
    assert res.exit_code == 2
    assert "Ship the thing." in res.output
    assert [c["path"].endswith("/preview") for c in api.calls] == [True]


def test_promote_interactive_decline_creates_nothing(api, monkeypatch):
    _promote_api(api)
    monkeypatch.setattr(cmd_meeting, "confirm_or_exit", lambda *a, **k: False)
    res = _run(["promote", "meet_1", "mi_1"])
    assert res.exit_code == 0 and "Nothing created" in res.output
    assert len(api.calls) == 1


def test_promote_yes_sends_edited_draft(api):
    _promote_api(api)
    res = _run([
        "promote", "meet_1", "mi_1", "--yes", "--title", "Edited", "--assignee", "atlas",
        "--due", "2026-12-01", "--priority", "high", "--jira",
    ])
    assert res.exit_code == 0, res.output
    assert "tk_1" in res.output and "jira_skipped" in res.output
    body = api.calls[1]["json"]
    assert body["title"] == "Edited"
    assert body["assigned_to"] == "atlas" and body["assigned_user_id"] is None
    assert body["due_date"] == "2026-12-01" and body["priority"] == "high"
    assert body["target_project_id"] == "proj_1"
    assert body["publish_to_jira"] is True


def test_promote_user_override_and_conflicts(api):
    _promote_api(api)
    assert _run(["promote", "meet_1", "mi_1", "--assignee", "a", "--user", "u", "--yes"]).exit_code == 1
    assert not api.calls
    assert _run(["promote", "meet_1", "mi_1", "--yes", "--user", "u_9"]).exit_code == 0
    body = api.calls[1]["json"]
    assert body["assigned_user_id"] == "u_9" and body["assigned_to"] is None
    assert body["publish_to_jira"] is False


def test_promote_group_meeting_needs_target_project(api):
    api.responses[("POST", "/api/v1/meetings/meet_1/action-items/mi_1/promote/preview")] = (
        200, dict(_DRAFT, target_project_id=None, target_project_options=[{"id": "proj_a", "name": "A"}]),
    )
    res = _run(["promote", "meet_1", "mi_1", "--yes"])
    assert res.exit_code == 1 and "proj_a" in res.output
    assert len(api.calls) == 1
    api.responses[("POST", "/api/v1/meetings/meet_1/action-items/mi_1/promote")] = (200, {"ticket_id": "tk_2"})
    assert _run(["promote", "meet_1", "mi_1", "--yes", "--target-project", "proj_a"]).exit_code == 0
    assert api.calls[-1]["json"]["target_project_id"] == "proj_a"


def test_api_errors_exit_nonzero(api):
    api.responses[("GET", "/api/v1/meetings/meet_x")] = (404, {"detail": "Meeting not found"})
    res = _run(["show", "meet_x"])
    assert res.exit_code == 1
    assert "404" in res.output


def test_registered_on_main_app():
    from sessionfs.cli.main import app

    names = {g.name for g in app.registered_groups}
    assert "meeting" in names


def test_long_running_calls_get_a_long_timeout(api):
    api.responses[("POST", "/api/v1/projects/proj_1/meetings")] = (200, {"id": "m", "line_count": 1})
    assert _run(["upload", "--stdin", "--title", "T", "--project", "proj_1"], input="x\n").exit_code == 0
    assert api.calls[-1]["timeout"] == 30
    assert _run(["upload", "--stdin", "--title", "T", "--project", "proj_1", "--extract"], input="x\n").exit_code == 0
    assert api.calls[-1]["timeout"] >= 150

    api.responses[("POST", "/api/v1/meetings/meet_1/extract")] = (200, dict(_MEETING, dropped_items=0))
    assert _run(["extract", "meet_1"]).exit_code == 0
    assert api.calls[-1]["timeout"] >= 150

    _promote_api(api)
    assert _run(["promote", "meet_1", "mi_1", "--yes"]).exit_code == 0
    assert api.calls[-1]["timeout"] == 30
    assert _run(["promote", "meet_1", "mi_1", "--yes", "--jira"]).exit_code == 0
    assert api.calls[-1]["timeout"] >= 150


def test_list_pagination_hint_keeps_group_and_quotes_query(api):
    api.responses[("GET", "/api/v1/groups/pg_1/meetings")] = (
        200, {"meetings": [], "next_cursor": "meet_4"},
    )
    res = _run(["list", "--group", "pg_1", "-q", "q3 plan"])
    assert "sfs meeting list --group pg_1 --query 'q3 plan' --limit 50 --before meet_4" in res.output
