"""CLI tests for `sfs chat …` (GitHub #118, C2). The API is faked."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from sessionfs.cli import cmd_chat, cmd_meeting
from sessionfs.cli.cmd_chat import chat_app, detect_provider

runner = CliRunner()

_SLACK = json.dumps([{"type": "message", "user": "U1", "text": "hi", "ts": "1700000000.000100"}])
_SLACK_OBJ = json.dumps({"channel": {"id": "C1"}, "messages": [{"ts": "1.2", "text": "x"}]})
_DISCORD = json.dumps({
    "guild": {"id": "1", "name": "G"},
    "channel": {"id": "2", "type": "GuildTextChat", "name": "infra"},
    "messages": [{"id": "3", "timestamp": "2026-10-29T14:02:00Z",
                  "author": {"name": "tolu"}, "content": "hello"}],
})

_THREAD = {
    "id": "chat_1",
    "kind": "chat",
    "channel_name": "#infra",
    "title": "#infra",
    "created_at": "2026-10-29T10:00:00Z",
    "line_count": 2,
    "extraction_status": "done",
    "content": "[2026-10-29 14:02] Tolu: ship it\n[2026-10-29 14:03] Bob: ok",
    "participants": [],
    "action_items": [
        {"id": "mi_1", "kind": "action", "description": "Ship it", "owner_participant_id": None,
         "due_date": None, "line_start": 1, "line_end": 2, "status": "pending",
         "promoted_ticket_id": None},
    ],
}


class Fake:
    def __init__(self):
        self.calls: list[dict] = []
        self.responses: dict = {}

    async def __call__(self, method, path, api_url, api_key, json_data=None, extra_headers=None, params=None, timeout=30):
        self.calls.append({"method": method, "path": path, "json": json_data, "params": params, "timeout": timeout})
        status, body = self.responses.get((method, path), (200, {}))
        return status, body, {}


@pytest.fixture
def api(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(cmd_meeting, "_api_request", fake)
    monkeypatch.setattr(cmd_meeting, "_get_api_config", lambda: ("https://api.test", "key"))
    return fake


def _run(args, input=None):
    return runner.invoke(chat_app, args, input=input)


def test_detect_provider():
    assert detect_provider(_SLACK) == "slack"
    assert detect_provider(_SLACK_OBJ) == "slack"
    assert detect_provider(_DISCORD) == "discord"
    assert detect_provider("Tolu: hi\nBob: ok") == "paste"
    assert detect_provider("[1, 2]") == "paste"
    assert detect_provider('{"a": 1}') == "paste"
    assert detect_provider('"x"') == "paste"


def test_upload_detects_slack_and_sends_users(api, tmp_path):
    f = tmp_path / "c.json"
    f.write_text(_SLACK)
    u = tmp_path / "users.json"
    u.write_text('[{"id": "U1", "name": "tolu"}]')
    api.responses[("POST", "/api/v1/projects/proj_1/chat-threads")] = (
        200, {"id": "chat_9", "line_count": 1, "warnings": ["duplicate_of: chat_1"]},
    )
    res = _run(["upload", str(f), "--project", "proj_1", "--channel", " #infra ", "--users", str(u),
                "--attest"])
    assert res.exit_code == 0, res.output
    assert "chat_9" in res.output and "(slack)" in res.output and "duplicate_of" in res.output
    call = api.calls[0]
    assert call["json"] == {
        "provider": "slack", "channel_name": "#infra", "content": _SLACK,
        "attestation": True, "users": '[{"id": "U1", "name": "tolu"}]',
    }
    assert call["timeout"] == 30


def test_upload_discord_group_extract(api, tmp_path):
    f = tmp_path / "d.json"
    f.write_text(_DISCORD)
    api.responses[("POST", "/api/v1/groups/pg_1/chat-threads")] = (
        200, {"id": "chat_2", "line_count": 1, "extraction_status": "done", "action_items": []},
    )
    res = _run(["upload", str(f), "--group", "pg_1", "--channel", "#infra", "--extract", "--title", "T"])
    assert res.exit_code == 0, res.output
    assert api.calls[0]["json"]["provider"] == "discord"
    assert api.calls[0]["json"]["extract"] is True
    assert api.calls[0]["json"]["title"] == "T"
    assert api.calls[0]["timeout"] == cmd_meeting._LONG_TIMEOUT


def test_paste_requires_attest(api, tmp_path):
    f = tmp_path / "p.txt"
    f.write_text("Tolu: ship it\nBob: ok\n")
    res = _run(["upload", str(f), "--project", "proj_1", "--channel", "#infra"])
    assert res.exit_code == 1
    assert "--attest" in res.output
    res = _run(["upload", str(f), "--project", "proj_1", "--channel", "#infra", "--provider", "teams_paste"])
    assert res.exit_code == 1
    assert api.calls == []
    api.responses[("POST", "/api/v1/projects/proj_1/chat-threads")] = (200, {"id": "chat_3"})
    res = _run(["upload", str(f), "--project", "proj_1", "--channel", "#infra", "--attest"])
    assert res.exit_code == 0, res.output
    assert api.calls[0]["json"]["provider"] == "paste"
    assert api.calls[0]["json"]["attestation"] is True


def test_bare_slack_array_requires_attest(api, tmp_path):
    f = tmp_path / "c.json"
    f.write_text(_SLACK)
    res = _run(["upload", str(f), "--project", "proj_1", "--channel", "#infra"])
    assert res.exit_code == 1
    assert "--attest" in res.output and "channel object" in res.output
    assert api.calls == []
    # A Slack export with a channel object needs no attestation.
    g = tmp_path / "obj.json"
    g.write_text(_SLACK_OBJ)
    api.responses[("POST", "/api/v1/projects/proj_1/chat-threads")] = (200, {"id": "chat_4"})
    res = _run(["upload", str(g), "--project", "proj_1", "--channel", "#infra"])
    assert res.exit_code == 0, res.output
    assert api.calls[0]["json"]["attestation"] is False


def test_file_size_refused_before_any_request(api, tmp_path):
    f = tmp_path / "big.json"
    f.write_bytes(b"x" * (cmd_chat.MAX_CHAT_FILE_BYTES + 1))
    res = _run(["upload", str(f), "--project", "proj_1", "--channel", "#c", "--attest"])
    assert res.exit_code == 1
    assert "2 MiB" in res.output
    ok = tmp_path / "c.json"
    ok.write_text(_SLACK)
    big_users = tmp_path / "users.json"
    big_users.write_bytes(b"x" * (cmd_chat.MAX_CHAT_FILE_BYTES + 1))
    res = _run(["upload", str(ok), "--project", "proj_1", "--channel", "#c", "--users", str(big_users)])
    assert res.exit_code == 1
    assert api.calls == []


def test_upload_argument_errors(api, tmp_path):
    f = tmp_path / "c.json"
    f.write_text(_SLACK)
    assert _run(["upload", str(f), "--channel", "#c", "--provider", "irc"]).exit_code == 1
    assert _run(["upload", str(f), "--channel", "#" + "x" * 80]).exit_code == 1
    d = tmp_path / "d.json"
    d.write_text(_DISCORD)
    res = _run(["upload", str(d), "--project", "p", "--channel", "#c", "--users", str(f)])
    assert res.exit_code == 1 and "Slack" in res.output
    empty = tmp_path / "e.txt"
    empty.write_text("  \n")
    assert _run(["upload", str(empty), "--project", "p", "--channel", "#c", "--attest"]).exit_code == 1
    assert _run(["upload", str(tmp_path / "missing"), "--project", "p", "--channel", "#c"]).exit_code == 1
    assert api.calls == []


def test_list_search_send_kind_chat(api):
    api.responses[("GET", "/api/v1/projects/proj_1/meetings")] = (
        200, {"meetings": [dict(_THREAD)], "next_cursor": "chat_0"},
    )
    res = _run(["list", "--project", "proj_1", "--query", "cert"])
    assert res.exit_code == 0, res.output
    assert api.calls[0]["params"] == {"limit": "50", "kind": "chat", "q": "cert"}
    assert "#infra" in res.output
    assert "sfs chat list" in res.output
    res = _run(["search", "cert", "--group", "pg_1"])
    assert api.calls[1]["path"] == "/api/v1/groups/pg_1/meetings"
    assert api.calls[1]["params"] == {"q": "cert", "limit": "25", "kind": "chat"}


def test_show_numbers_messages_and_items_with_m(api):
    api.responses[("GET", "/api/v1/meetings/chat_1")] = (200, _THREAD)
    res = _run(["show", "chat_1"])
    assert res.exit_code == 0, res.output
    assert api.calls[0]["params"] == {"kind": "chat"}
    assert "M1" in res.output and "M2" in res.output
    assert "M1-2" in res.output
    assert "2 messages" in res.output


def test_id_commands_send_kind_chat(api):
    api.responses[("POST", "/api/v1/meetings/chat_1/extract")] = (200, {"extraction_status": "done"})
    api.responses[("GET", "/api/v1/meetings/chat_1")] = (200, _THREAD)
    assert _run(["extract", "chat_1"]).exit_code == 0
    assert _run(["delete", "chat_1", "--yes"]).exit_code == 0
    assert _run(["dismiss", "chat_1", "mi_1"]).exit_code == 0
    assert _run(["restore", "chat_1", "mi_1"]).exit_code == 0
    assert _run(["action-items", "chat_1"]).exit_code == 0
    assert _run(["participants", "chat_1"]).exit_code == 0
    assert _run(["resolve-participant", "chat_1", "mp_1", "--user", "u_1"]).exit_code == 0
    paths = [(c["method"], c["path"]) for c in api.calls]
    assert ("POST", "/api/v1/meetings/chat_1/extract") in paths
    assert ("DELETE", "/api/v1/meetings/chat_1") in paths
    assert ("POST", "/api/v1/meetings/chat_1/action-items/mi_1/dismiss") in paths
    assert ("POST", "/api/v1/meetings/chat_1/action-items/mi_1/restore") in paths
    assert ("PUT", "/api/v1/meetings/chat_1/participants/mp_1") in paths
    assert all(c["params"] == {"kind": "chat"} for c in api.calls)


def test_promote_sends_kind_chat(api):
    root = "/api/v1/meetings/chat_1/action-items/mi_1/promote"
    api.responses[("POST", f"{root}/preview")] = (200, {
        "title": "Ship it", "description": "d", "priority": "medium", "kind": "task",
        "assigned_to": None, "assigned_user_id": None, "due_date": None,
        "target_project_id": "proj_1",
    })
    api.responses[("POST", root)] = (200, {"ticket_id": "tk_1"})
    res = _run(["promote", "chat_1", "mi_1", "--yes"])
    assert res.exit_code == 0, res.output
    assert "tk_1" in res.output
    assert [c["params"] for c in api.calls] == [{"kind": "chat"}, {"kind": "chat"}]


def test_link_and_unlink_send_kind_chat(api):
    root = "/api/v1/meetings/chat_1/action-items/mi_1"
    api.responses[("POST", f"{root}/link/preview")] = (200, {
        "ticket": {"id": "tk_1", "project_id": "p", "title": "T", "status": "open"},
        "suggested": False, "comment": "Raised again in chat thread ...",
    })
    api.responses[("POST", f"{root}/link")] = (200, {"ticket_id": "tk_1", "status": "linked"})
    res = _run(["link", "chat_1", "mi_1", "tk_1", "--yes"])
    assert res.exit_code == 0, res.output
    assert _run(["unlink", "chat_1", "mi_1"]).exit_code == 0
    assert [c["path"] for c in api.calls] == [f"{root}/link/preview", f"{root}/link", f"{root}/unlink"]
    assert all(c["params"] == {"kind": "chat"} for c in api.calls)


def test_registered_on_main_app():
    from sessionfs.cli.main import app

    assert "chat" in {g.name for g in app.registered_groups}


def test_discord_without_guild_channel_requires_attest(api, tmp_path):
    f = tmp_path / "d.json"
    f.write_text(json.dumps({"messages": [{"author": {"name": "tolu"}, "content": "hi"}]}))
    res = _run(["upload", str(f), "--project", "proj_1", "--channel", "#infra", "--provider", "discord"])
    assert res.exit_code == 1
    assert "--attest" in res.output and "Discord" in res.output
    unknown = tmp_path / "u.json"
    unknown.write_text(json.dumps(dict(json.loads(_DISCORD), channel={"id": "2", "type": "Mystery"})))
    res = _run(["upload", str(unknown), "--project", "proj_1", "--channel", "#infra"])
    assert res.exit_code == 1 and api.calls == []
    api.responses[("POST", "/api/v1/projects/proj_1/chat-threads")] = (200, {"id": "chat_5"})
    res = _run(["upload", str(unknown), "--project", "proj_1", "--channel", "#infra", "--attest"])
    assert res.exit_code == 0, res.output
    assert api.calls[0]["json"]["attestation"] is True
