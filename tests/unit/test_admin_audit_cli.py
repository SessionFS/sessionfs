"""GH #84 — `sfs admin audit-log` and `sfs admin override ...`.

The HTTP boundary is patched on `sessionfs.cli.cmd_admin._api_request`;
the server side has its own coverage in
tests/server/integration/test_admin_overrides.py.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from sessionfs.cli.cmd_admin import admin_app

runner = CliRunner()
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _out(result) -> str:
    parts = [getattr(result, name, "") or "" for name in ("stdout", "stderr", "output")]
    return _ANSI_RE.sub("", "\n".join(parts))


class _FakeRequest:
    def __init__(self, status: int, body):
        self.status = status
        self.body = body
        self.calls: list[dict] = []

    async def __call__(self, method, path, api_url, api_key, json_data=None, extra_headers=None):
        self.calls.append({"method": method, "path": path, "body": json_data})
        return self.status, self.body, {}


@pytest.fixture(autouse=True)
def _api_config():
    with patch(
        "sessionfs.cli.cmd_admin._get_api_config",
        return_value=("https://api.example", "sk_sfs_test"),
    ):
        yield


def _invoke(fake: _FakeRequest, args: list[str], **kwargs):
    with patch("sessionfs.cli.cmd_admin._api_request", new=fake):
        return runner.invoke(admin_app, args, **kwargs)


_ROW = {
    "id": "aa_1",
    "admin_id": "u_admin",
    "action": "project_transfer_override",
    "target_type": "project",
    "target_id": "proj_1",
    "details": None,
    "reason": "owner offline",
    "before_state": {"org_id": None},
    "after_state": {"org_id": "org_b"},
    "source": "api",
    "created_at": "2026-10-22T10:00:00+00:00",
}


def test_audit_log_passes_filters_and_renders_table():
    fake = _FakeRequest(200, {"total": 1, "page": 1, "page_size": 20, "actions": [_ROW]})
    result = _invoke(
        fake,
        ["audit-log", "--target-type", "project", "--target-id", "proj_1",
         "--action", "project_transfer_override", "--limit", "20"],
    )
    assert result.exit_code == 0, _out(result)
    call = fake.calls[0]
    assert call["method"] == "GET"
    assert call["path"].startswith("/api/v1/admin/audit-log?")
    query = call["path"].split("?", 1)[1]
    for part in ("page_size=20", "target_type=project", "target_id=proj_1",
                 "action=project_transfer_override"):
        assert part in query
    out = _out(result)
    assert "project_transfer_override" in out
    assert "owner offline" in out


def test_audit_log_json_output():
    payload = {"total": 1, "page": 1, "page_size": 50, "actions": [_ROW]}
    fake = _FakeRequest(200, payload)
    result = _invoke(fake, ["audit-log", "--json"])
    assert result.exit_code == 0, _out(result)
    assert json.loads(result.stdout) == payload


def test_audit_log_empty_and_error():
    empty = _FakeRequest(200, {"total": 0, "actions": []})
    assert "No matching" in _out(_invoke(empty, ["audit-log"]))
    denied = _FakeRequest(403, {"error": {"code": "forbidden", "message": "Admin access required"}})
    result = _invoke(denied, ["audit-log"])
    assert result.exit_code == 1
    assert "Admin access required" in _out(result)


def test_override_project_transfer_with_yes():
    fake = _FakeRequest(200, {
        "status": "transferred",
        "project_id": "proj_1",
        "from_scope": "personal",
        "to_org_id": "org_b",
        "transfer_id": "xfer_1",
        "cancelled_transfer_ids": ["xfer_0"],
        "admin_action_id": "aa_1",
    })
    result = _invoke(
        fake,
        ["override", "project-transfer", "proj_1", "--to-org", "org_b",
         "--reason", "owner offline", "--yes"],
    )
    assert result.exit_code == 0, _out(result)
    call = fake.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == "/api/v1/admin/projects/proj_1/force-transfer"
    assert call["body"] == {"to_org_id": "org_b", "reason": "owner offline", "force": False}
    out = _out(result)
    assert "xfer_0" in out and "aa_1" in out


def test_override_project_transfer_prompts_and_can_cancel():
    fake = _FakeRequest(200, {})
    result = _invoke(
        fake,
        ["override", "project-transfer", "proj_1", "--to-org", "org_b", "--reason", "r"],
        input="n\n",
    )
    assert result.exit_code == 0
    assert fake.calls == []
    assert "Cancelled" in _out(result)


def test_override_project_transfer_rejects_blank_reason_locally():
    fake = _FakeRequest(200, {})
    result = _invoke(
        fake,
        ["override", "project-transfer", "proj_1", "--to-org", "org_b", "--reason", "  ", "-y"],
    )
    assert result.exit_code == 2
    assert fake.calls == []


def test_override_project_transfer_surfaces_server_error():
    fake = _FakeRequest(409, {"error": {"code": "already_in_org", "message": "The project is already in that org"}})
    result = _invoke(
        fake,
        ["override", "project-transfer", "proj_1", "--to-org", "org_b", "--reason", "r", "-y", "--force"],
    )
    assert result.exit_code == 1
    assert fake.calls[0]["body"]["force"] is True
    assert "already in that org" in _out(result)


def test_override_record_with_state_files(tmp_path: Path):
    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    before.write_text(json.dumps({"emails": ["a@x", "a@y"]}))
    after.write_text(json.dumps({"email": "a@y"}))
    fake = _FakeRequest(201, {
        "id": "aa_9", "action": "user_merge", "target_type": "user", "target_id": "u_1",
    })
    result = _invoke(
        fake,
        ["override", "record", "--action", "user_merge", "--target-type", "user",
         "--target-id", "u_1", "--reason", "typo signup",
         "--before-file", str(before), "--after-file", str(after)],
    )
    assert result.exit_code == 0, _out(result)
    call = fake.calls[0]
    assert (call["method"], call["path"]) == ("POST", "/api/v1/admin/audit-log")
    assert call["body"] == {
        "action": "user_merge",
        "target_type": "user",
        "target_id": "u_1",
        "reason": "typo signup",
        "before_state": {"emails": ["a@x", "a@y"]},
        "after_state": {"email": "a@y"},
    }
    assert "aa_9" in _out(result)


def test_override_record_without_state_files():
    fake = _FakeRequest(201, {"id": "aa_2", "action": "bulk_update", "target_type": "project", "target_id": "p"})
    result = _invoke(
        fake,
        ["override", "record", "--action", "bulk_update", "--target-type", "project",
         "--target-id", "p", "--reason", "rename"],
    )
    assert result.exit_code == 0, _out(result)
    assert "before_state" not in fake.calls[0]["body"]
    assert "after_state" not in fake.calls[0]["body"]


@pytest.mark.parametrize("content", ["[1, 2]", "{not json"])
def test_override_record_rejects_bad_state_file(tmp_path: Path, content: str):
    bad = tmp_path / "bad.json"
    bad.write_text(content)
    fake = _FakeRequest(201, {})
    result = _invoke(
        fake,
        ["override", "record", "--action", "x", "--target-type", "user", "--target-id", "u",
         "--reason", "r", "--before-file", str(bad)],
    )
    assert result.exit_code == 2
    assert fake.calls == []


def test_audit_log_admin_id_filter():
    fake = _FakeRequest(200, {"total": 0, "actions": []})
    result = _invoke(fake, ["audit-log", "--admin-id", "u_admin"])
    assert result.exit_code == 0, _out(result)
    assert "admin_id=u_admin" in fake.calls[0]["path"]


def test_override_project_transfer_sends_new_owner_and_expected_scope():
    fake = _FakeRequest(200, {
        "project_id": "proj_1", "from_scope": "personal", "to_org_id": "org_b",
        "transfer_id": "xfer_1", "admin_action_id": "aa_1",
        "previous_owner_id": "u_old", "owner_id": "u_lead",
    })
    result = _invoke(
        fake,
        ["override", "project-transfer", "proj_1", "--to-org", "org_b", "--reason", "r",
         "--new-owner", "u_lead", "--expect-from", "personal", "-y"],
    )
    assert result.exit_code == 0, _out(result)
    assert fake.calls[0]["body"] == {
        "to_org_id": "org_b", "reason": "r", "force": False,
        "new_owner_id": "u_lead", "expected_org_id": None,
    }
    assert "u_old → u_lead" in _out(result)

    org_fake = _FakeRequest(200, {"project_id": "proj_1"})
    result = _invoke(
        org_fake,
        ["override", "project-transfer", "proj_1", "--to-org", "org_b", "--reason", "r",
         "--expect-from", "org_a", "-y"],
    )
    assert result.exit_code == 0, _out(result)
    assert org_fake.calls[0]["body"]["expected_org_id"] == "org_a"
    assert "new_owner_id" not in org_fake.calls[0]["body"]


def test_error_text_with_brackets_is_printed_verbatim():
    detail = "action must match ^[a-z][a-z0-9_]{0,49}$"
    fake = _FakeRequest(422, {"detail": detail})
    result = _invoke(
        fake,
        ["override", "record", "--action", "x", "--target-type", "user", "--target-id", "u",
         "--reason", "r"],
    )
    assert result.exit_code == 1
    assert detail in _out(result)


def test_success_output_with_markup_like_ids_does_not_crash():
    """The row is already written when the success line prints; a target id
    such as '[/x]' must not make Rich raise afterwards."""
    fake = _FakeRequest(201, {
        "id": "aa_[b]", "action": "bulk_update", "target_type": "user", "target_id": "[/x]",
    })
    result = _invoke(
        fake,
        ["override", "record", "--action", "bulk_update", "--target-type", "user",
         "--target-id", "[/x]", "--reason", "r"],
    )
    assert result.exit_code == 0, _out(result)
    out = _out(result)
    assert "user:[/x]" in out and "aa_[b]" in out

    transfer = _FakeRequest(200, {
        "project_id": "[/p]", "from_scope": "personal", "to_org_id": "[bold]o",
        "transfer_id": "x", "admin_action_id": "a", "cancelled_transfer_ids": ["[/c]"],
    })
    result = _invoke(
        transfer,
        ["override", "project-transfer", "[/p]", "--to-org", "[bold]o", "--reason", "r", "-y"],
    )
    assert result.exit_code == 0, _out(result)
    assert "[/p] personal → [bold]o" in _out(result)

    listing = _FakeRequest(200, {"total": 1, "actions": [{
        **_ROW, "action": "a[/x]", "target_id": "[red]t", "source": "operator_record",
    }]})
    result = _invoke(listing, ["audit-log"])
    assert result.exit_code == 0, _out(result)
    assert "project:[red]t" in _out(result)
    assert "operator-recorded" in _out(result)


def test_override_record_surfaces_validation_error():
    fake = _FakeRequest(422, {"detail": [{"loc": ["body", "action"], "msg": "String should match pattern"}]})
    result = _invoke(
        fake,
        ["override", "record", "--action", "BAD", "--target-type", "user", "--target-id", "u",
         "--reason", "r"],
    )
    assert result.exit_code == 1
    assert "action" in _out(result)


_MERGE_PLAN = {
    "reassigned": {"sessions.user_id": {"count": 62, "ids": [], "truncated": False}},
    "revoked": {"api_keys": {"count": 1, "ids": [], "truncated": False}},
    "cancelled": {},
    "deduped": {"team_members": {"moved": {"count": 0}, "dropped_target_wins": {"count": 1}}},
    "org_members": [{"org_id": "org_1", "action": "merged", "source_role": "owner",
                     "target_role": "member", "resulting_role": "owner"}],
}


def test_override_user_merge_defaults_to_dry_run():
    fake = _FakeRequest(200, {"status": "dry_run", "plan": _MERGE_PLAN})
    result = _invoke(fake, ["override", "user-merge", "u_src", "u_tgt", "--reason", "typo"])
    assert result.exit_code == 0, _out(result)
    call = fake.calls[0]
    assert call["path"] == "/api/v1/admin/users/u_src/merge-into/u_tgt"
    assert call["body"] == {"reason": "typo", "dry_run": True}
    out = _out(result)
    assert "nothing changed" in out and "sessions.user_id: 62" in out and "--apply" in out


def test_override_user_merge_apply_confirms_and_can_cancel():
    fake = _FakeRequest(200, {})
    result = _invoke(fake, ["override", "user-merge", "u_src", "u_tgt", "-r", "typo", "--apply"],
                     input="n\n")
    assert result.exit_code == 0 and fake.calls == []
    assert "Cancelled" in _out(result)


def test_override_user_merge_apply_yes():
    fake = _FakeRequest(200, {"status": "merged", "plan": _MERGE_PLAN, "admin_action_id": "aa_9"})
    result = _invoke(fake, ["override", "user-merge", "u_src", "u_tgt", "-r", "typo", "--apply", "-y"])
    assert result.exit_code == 0, _out(result)
    assert fake.calls[0]["body"] == {"reason": "typo", "dry_run": False}
    assert "aa_9" in _out(result)


def test_override_user_merge_blank_reason_and_server_error():
    fake = _FakeRequest(200, {})
    assert _invoke(fake, ["override", "user-merge", "a", "b", "-r", "  "]).exit_code == 2
    assert fake.calls == []
    denied = _FakeRequest(
        409, {"error": {"code": "entitlement_conflict", "message": "Both users hold an active entitlement"}}
    )
    result = _invoke(denied, ["override", "user-merge", "a", "b", "-r", "x"])
    assert result.exit_code == 1 and "active entitlement" in _out(result)


def test_override_project_merge_defaults_to_dry_run():
    fake = _FakeRequest(200, {"status": "planned", "dry_run": True,
                              "stats": {"sessions": 12, "knowledge_entries": 4}})
    result = _invoke(fake, ["override", "project-merge", "proj_src", "proj_tgt", "-r", "twins"])
    assert result.exit_code == 0, _out(result)
    call = fake.calls[0]
    assert call["path"] == "/api/v1/admin/projects/proj_tgt/merge-from/proj_src"
    assert call["body"] == {"reason": "twins", "dry_run": True, "persona_policy": "rename"}
    out = _out(result)
    assert "nothing changed" in out and "sessions: 12" in out and "--apply" in out


def test_override_project_merge_apply_and_validation():
    fake = _FakeRequest(200, {"status": "merged", "stats": {}, "admin_action_id": "aa_7"})
    result = _invoke(fake, ["override", "project-merge", "a", "b", "-r", "x", "--apply", "-y",
                            "--persona-policy", "skip"])
    assert result.exit_code == 0, _out(result)
    assert fake.calls[0]["body"] == {"reason": "x", "dry_run": False, "persona_policy": "skip"}
    assert "aa_7" in _out(result)
    none = _FakeRequest(200, {})
    assert _invoke(none, ["override", "project-merge", "a", "b", "-r", " "]).exit_code == 2
    assert _invoke(none, ["override", "project-merge", "a", "b", "-r", "x",
                          "--persona-policy", "clobber"]).exit_code == 2
    cancelled = _invoke(none, ["override", "project-merge", "a", "b", "-r", "x", "--apply"], input="n\n")
    assert cancelled.exit_code == 0 and none.calls == []
