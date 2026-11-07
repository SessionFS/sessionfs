"""CLI tests for `sfs approval …` (approval policies A2). The API is faked."""

from __future__ import annotations

import json

import pytest
import typer
from typer.testing import CliRunner

from sessionfs.cli import cmd_approval
from sessionfs.cli.cmd_approval import approval_app, parse_filter, parse_requirement

runner = CliRunner()

_POLICY = {
    "id": "ap_1", "lineage_id": "ap_1", "name": "Compliance", "target_kind": "kb_promote",
    "requirements": [{"who": "org_role", "role": "admin", "count": 1}], "blocking": True,
    "active": True, "version": 1, "description": "", "filter": {"entity_ref_prefix_any": ["docs/"]},
}
_REQUEST = {
    "id": "ar_1", "policy": {"id": "ap_1", "name": "Compliance", "version": 1, "blocking": True},
    "target_kind": "kb_promote", "target_id": "42", "target_fingerprint": "ab" * 32,
    "state": "pending", "approved_count": 0, "required_count": 1, "can_decide": True,
    "decisions": [],
}


class Fake:
    def __init__(self):
        self.calls: list[dict] = []
        self.responses: dict = {}

    async def __call__(self, method, path, api_url, api_key, json_data=None, extra_headers=None, params=None, timeout=30):
        self.calls.append({"method": method, "path": path, "json": json_data, "params": params})
        default = (200, {"default_org_id": "org_d"}) if path == "/api/v1/auth/me" else (200, {})
        status, body = self.responses.get((method, path), default)
        return status, body, {}


@pytest.fixture
def api(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(cmd_approval, "_api_request", fake)
    monkeypatch.setattr(cmd_approval, "_get_api_config", lambda: ("https://api.test", "key"))
    return fake


def test_parse_requirement():
    assert parse_requirement("role:admin:1") == {"who": "org_role", "role": "admin", "count": 1}
    assert parse_requirement("users:u1, u2:2") == {"who": "users", "user_ids": ["u1", "u2"], "count": 2}
    for bad in ("role:member:1", "users::1", "admin", "role:admin:x"):
        with pytest.raises(typer.BadParameter):
            parse_requirement(bad)


def test_parse_filter(tmp_path):
    assert parse_filter(None, None) is None
    assert parse_filter('{"entry_type_any": ["decision"]}', None) == {"entry_type_any": ["decision"]}
    f = tmp_path / "f.json"
    f.write_text('{"project_ids": ["p1"]}')
    assert parse_filter(None, f) == {"project_ids": ["p1"]}
    with pytest.raises(typer.BadParameter):
        parse_filter("[1]", None)
    with pytest.raises(typer.BadParameter):
        parse_filter("{nope", None)


def test_policy_create_uses_default_org(api):
    api.responses[("POST", "/api/v1/orgs/org_d/approval-policies")] = (201, _POLICY)
    res = runner.invoke(approval_app, [
        "policy", "create", "--name", "Compliance", "--target", "kb_promote",
        "--require", "role:admin:1", "--filter", '{"entity_ref_prefix_any": ["docs/"]}',
        "--advisory",
    ])
    assert res.exit_code == 0, res.output
    call = api.calls[-1]
    assert call["json"] == {
        "name": "Compliance", "description": "", "target_kind": "kb_promote",
        "filter": {"entity_ref_prefix_any": ["docs/"]},
        "requirements": [{"who": "org_role", "role": "admin", "count": 1}],
        "blocking": False,
    }


def test_policy_create_rejects_bad_target(api):
    res = runner.invoke(approval_app, ["policy", "create", "--name", "x", "--target", "close",
                                       "--require", "role:admin:1"])
    assert res.exit_code != 0 and not api.calls


def test_policy_update_merges_current(api):
    api.responses[("GET", "/api/v1/orgs/org_a/approval-policies")] = (200, {"policies": [_POLICY]})
    api.responses[("PUT", "/api/v1/orgs/org_a/approval-policies/ap_1")] = (200, {**_POLICY, "id": "ap_2", "version": 2})
    res = runner.invoke(approval_app, ["policy", "update", "ap_1", "--org", "org_a", "--advisory",
                                       "--require", "role:owner:1"])
    assert res.exit_code == 0, res.output
    put = api.calls[-1]
    assert put["method"] == "PUT"
    assert put["json"]["blocking"] is False
    assert put["json"]["requirements"] == [{"who": "org_role", "role": "owner", "count": 1}]
    assert put["json"]["filter"] == _POLICY["filter"] and put["json"]["name"] == "Compliance"
    assert "version 2" in res.output


def test_policy_update_needs_a_change(api):
    res = runner.invoke(approval_app, ["policy", "update", "ap_1", "--org", "org_a"])
    assert res.exit_code != 0 and not api.calls


def test_policy_disable_and_list(api):
    res = runner.invoke(approval_app, ["policy", "disable", "ap_1", "--org", "org_a"])
    assert res.exit_code == 0
    assert api.calls[-1] == {"method": "DELETE", "path": "/api/v1/orgs/org_a/approval-policies/ap_1",
                             "json": None, "params": None}
    api.responses[("GET", "/api/v1/orgs/org_a/approval-policies")] = (200, {"policies": [_POLICY]})
    res = runner.invoke(approval_app, ["policy", "list", "--org", "org_a", "--all"])
    assert res.exit_code == 0 and "Compliance" in res.output
    assert api.calls[-1]["params"] == {"include_inactive": "true"}


def test_list_show_and_decide(api):
    api.responses[("GET", "/api/v1/me/approvals")] = (200, {"requests": [{
        "id": "ar_1", "policy_name": "Compliance", "target_kind": "kb_promote", "target_id": "42",
        "target_title": "Decision: PHI", "approved_count": 0, "required_count": 1}]})
    res = runner.invoke(approval_app, ["list"])
    assert res.exit_code == 0 and "ar_1" in res.output

    api.responses[("GET", "/api/v1/approval-requests/ar_1")] = (200, _REQUEST)
    res = runner.invoke(approval_app, ["show", "ar_1"])
    assert res.exit_code == 0 and "You can decide" in res.output

    api.responses[("POST", "/api/v1/approval-requests/ar_1/decide")] = (200, {**_REQUEST, "state": "approved", "approved_count": 1})
    res = runner.invoke(approval_app, ["approve", "ar_1", "--reason", "Checked the key handling."])
    assert res.exit_code == 0, res.output
    assert api.calls[-1]["json"] == {"decision": "approve", "reason": "Checked the key handling."}
    res = runner.invoke(approval_app, ["reject", "ar_1", "--reason", "no"])
    assert api.calls[-1]["json"] == {"decision": "reject", "reason": "no"}


def test_decide_requires_reason(api):
    res = runner.invoke(approval_app, ["approve", "ar_1"])
    assert res.exit_code != 0 and not api.calls


def test_request_id_is_one_segment(api):
    runner.invoke(approval_app, ["show", "../../admin"])
    assert api.calls[-1]["path"] == "/api/v1/approval-requests/..%2F..%2Fadmin"


def test_not_found_message(api):
    api.responses[("GET", "/api/v1/approval-requests/ar_x")] = (404, {"detail": "Approval request not found"})
    res = runner.invoke(approval_app, ["show", "ar_x"])
    assert res.exit_code == 1 and "Approval request not found" in res.output


def test_registered_in_main():
    from sessionfs.cli.main import app

    names = {g.name for g in app.registered_groups}
    assert "approval" in names
    assert json  # keep import used
