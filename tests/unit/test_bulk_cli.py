"""CLI tests for the bulk flags: `sfs org invite --file`, `sfs org invites`,
`sfs org accept`, `sfs project group grant --file` and
`sfs meeting|chat resolve-participant --file`. The API is faked."""

from __future__ import annotations

import re
from unittest.mock import patch

from typer.testing import CliRunner

from sessionfs.cli import cmd_meeting
from sessionfs.cli.cmd_chat import chat_app
from sessionfs.cli.cmd_meeting import meeting_app
from sessionfs.cli.cmd_org import org_app
from sessionfs.cli.cmd_project import project_app
from tests.unit.test_org_cli import FakeResp

runner = CliRunner()
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _out(result) -> str:
    parts = [getattr(result, k, "") or "" for k in ("stdout", "stderr", "output")]
    return _ANSI_RE.sub("", "\n".join(parts))


_BULK_FAIL = {
    "error": {
        "code": "bulk_item_failed",
        "message": "One or more items failed; nothing was changed.",
        "details": {
            "results": [
                {"index": 0, "email": "a@x.io", "ok": True, "message": "Not applied"},
                {"index": 1, "email": "b@x.io", "ok": False, "code": "already_member",
                 "message": "This user is already a member"},
            ]
        },
    }
}


def _cfg():
    return patch("sessionfs.cli.cmd_org._load_api_config", return_value=("http://api.test", "k"))


# ── sfs org invite --file ──


def test_org_invite_file_posts_bulk_and_lists_links(tmp_path):
    f = tmp_path / "emails.txt"
    f.write_text("# team\na@x.io\n\nb@x.io,admin\n")
    ok = FakeResp(200, {
        "org_id": "org_1",
        "invited": 2,
        "results": [
            {"index": 0, "email": "a@x.io", "role": "member", "ok": True, "invite_id": "inv_a",
             "email_sent": True, "accept_url": "https://app/invites?highlight=inv_a"},
            {"index": 1, "email": "b@x.io", "role": "admin", "ok": True, "invite_id": "inv_b",
             "email_sent": False, "accept_url": "https://app/invites?highlight=inv_b"},
        ],
    })
    with _cfg(), patch(
        "httpx.get", return_value=FakeResp(200, {"org": {"id": "org_1"}})
    ), patch("sessionfs.cli.cmd_org.idempotent_post", return_value=ok) as post:
        result = runner.invoke(org_app, ["invite", "--file", str(f)])
    assert result.exit_code == 0, _out(result)
    url = post.call_args[0][0]
    assert url.endswith("/api/v1/orgs/org_1/invites/bulk")
    assert post.call_args.kwargs["json"] == {
        "invites": [{"email": "a@x.io", "role": "member"}, {"email": "b@x.io", "role": "admin"}]
    }
    out = _out(result)
    assert "inv_b" in out and "highlight=inv_b" in out


def test_org_invite_file_reports_failed_items(tmp_path):
    f = tmp_path / "emails.txt"
    f.write_text("a@x.io\nb@x.io\n")
    with _cfg(), patch(
        "sessionfs.cli.cmd_org.idempotent_post", return_value=FakeResp(422, _BULK_FAIL)
    ):
        result = runner.invoke(org_app, ["invite", "--file", str(f), "--org", "org_1"])
    assert result.exit_code == 1
    out = _out(result)
    assert "Nothing was changed" in out
    assert "b@x.io: This user is already a member" in out


def test_org_invite_needs_email_or_file(tmp_path):
    with _cfg():
        neither = runner.invoke(org_app, ["invite"])
        both = runner.invoke(org_app, ["invite", "a@x.io", "--file", str(tmp_path / "x")])
    assert neither.exit_code == both.exit_code == 2


def test_org_invite_without_email_prints_accept_link():
    resp = FakeResp(200, {
        "invite_id": "inv_1", "email": "a@x.io", "role": "member",
        "email_sent": False, "accept_url": "https://app/invites?highlight=inv_1",
    })
    with _cfg(), patch("sessionfs.cli.cmd_org.idempotent_post", return_value=resp):
        result = runner.invoke(org_app, ["invite", "a@x.io"])
    assert result.exit_code == 0, _out(result)
    out = _out(result)
    assert "Invite created" in out and "No email was sent" in out
    assert "highlight=inv_1" in out and "sfs org accept" in out


def test_org_invites_and_accept():
    listing = FakeResp(200, {"invites": [{
        "invite_id": "inv_1", "org_id": "org_1", "org_name": "Acme", "role": "member",
        "invited_by_email": "boss@x.io", "created_at": "2026-11-01T00:00:00Z",
        "expires_at": "2026-11-08T00:00:00Z",
    }]})
    with _cfg(), patch("httpx.get", return_value=listing):
        result = runner.invoke(org_app, ["invites"])
    assert result.exit_code == 0 and "inv_1" in _out(result) and "Acme" in _out(result)

    with _cfg(), patch(
        "sessionfs.cli.cmd_org.idempotent_post",
        return_value=FakeResp(200, {"org_id": "org_1", "role": "member"}),
    ) as post:
        result = runner.invoke(org_app, ["accept", "inv_1"])
    assert result.exit_code == 0, _out(result)
    assert post.call_args[0][0].endswith("/api/v1/org/invite/inv_1/accept")


# ── sfs project group grant --file ──


class _Router:
    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[dict] = []

    async def __call__(self, method, path, api_url, api_key, json_data=None):
        self.calls.append({"method": method, "path": path, "body": json_data})
        return self.routes.get((method, path), {})


def test_group_grant_file_posts_bulk(tmp_path):
    f = tmp_path / "grants.txt"
    f.write_text("u_1\nu_2,admin\n")
    router = _Router({
        ("GET", "/api/v1/orgs/org_1/groups"): [{"id": "pg_core", "slug": "core"}],
        ("POST", "/api/v1/orgs/org_1/groups/pg_core/members/bulk"): {"granted": 2, "results": []},
    })
    with patch("sessionfs.cli.cmd_project_group._client", return_value=("http://api", "k")), patch(
        "sessionfs.cli.cmd_project_group._request", new=router
    ):
        result = runner.invoke(
            project_app,
            ["group", "grant", "core", "--file", str(f), "--role", "reader", "--org", "org_1"],
        )
    assert result.exit_code == 0, _out(result)
    post = [c for c in router.calls if c["method"] == "POST"][0]
    assert post["body"] == {
        "grants": [{"user_id": "u_1", "role": "reader"}, {"user_id": "u_2", "role": "admin"}]
    }


def test_group_grant_file_line_without_role_is_refused(tmp_path):
    f = tmp_path / "grants.txt"
    f.write_text("u_1\n")
    router = _Router({("GET", "/api/v1/orgs/org_1/groups"): [{"id": "pg_core", "slug": "core"}]})
    with patch("sessionfs.cli.cmd_project_group._client", return_value=("http://api", "k")), patch(
        "sessionfs.cli.cmd_project_group._request", new=router
    ):
        result = runner.invoke(project_app, ["group", "grant", "core", "--file", str(f), "--org", "org_1"])
    assert result.exit_code == 2
    assert not [c for c in router.calls if c["method"] == "POST"]


# ── sfs meeting|chat resolve-participant --file ──


class _Api:
    def __init__(self, status=200, body=None):
        self.calls: list[dict] = []
        self.status = status
        self.body = body if body is not None else {"results": []}

    async def __call__(self, method, path, api_url, api_key, json_data=None, extra_headers=None,
                       params=None, timeout=30):
        self.calls.append({"method": method, "path": path, "json": json_data, "params": params})
        return self.status, self.body, {}


def test_meeting_resolve_file_and_chat_kind(tmp_path, monkeypatch):
    f = tmp_path / "people.txt"
    f.write_text("mp_1,user:u_1\nmp_2,persona:Analyst\nmp_3,contact:ec_1\n")
    api = _Api(body={"results": [
        {"participant_id": "mp_1", "ok": True,
         "participant": {"name_raw": "Ann", "user_id": "u_1", "persona_name": None}},
    ]})
    monkeypatch.setattr(cmd_meeting, "_api_request", api)
    monkeypatch.setattr(cmd_meeting, "_get_api_config", lambda: ("https://api.test", "key"))
    result = runner.invoke(meeting_app, ["resolve-participant", "meet_1", "--file", str(f)])
    assert result.exit_code == 0, _out(result)
    call = api.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/v1/meetings/meet_1/participants/resolve-bulk"
    assert call["json"] == {"participants": [
        {"participant_id": "mp_1", "user_id": "u_1"},
        {"participant_id": "mp_2", "persona_name": "Analyst"},
        {"participant_id": "mp_3", "contact_id": "ec_1"},
    ]}

    result = runner.invoke(chat_app, ["resolve-participant", "chat_1", "--file", str(f)])
    assert result.exit_code == 0, _out(result)
    assert api.calls[-1]["params"] == {"kind": "chat"}


def test_meeting_resolve_file_bad_line_and_failure(tmp_path, monkeypatch):
    bad = tmp_path / "bad.txt"
    bad.write_text("mp_1,nobody:x\n")
    api = _Api(status=422, body={"error": {"code": "bulk_item_failed", "details": {"results": [
        {"participant_id": "mp_1", "ok": False, "code": "participant_not_found",
         "message": "Participant not found"},
    ]}}})
    monkeypatch.setattr(cmd_meeting, "_api_request", api)
    monkeypatch.setattr(cmd_meeting, "_get_api_config", lambda: ("https://api.test", "key"))
    result = runner.invoke(meeting_app, ["resolve-participant", "meet_1", "--file", str(bad)])
    assert result.exit_code == 2 and not api.calls

    good = tmp_path / "good.txt"
    good.write_text("mp_1,user:u_1\n")
    result = runner.invoke(meeting_app, ["resolve-participant", "meet_1", "--file", str(good)])
    assert result.exit_code == 1
    assert "mp_1: Participant not found" in _out(result)
