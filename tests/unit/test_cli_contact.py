"""`sfs contact …` builds the right requests (GitHub #125)."""

from __future__ import annotations

from typer.testing import CliRunner

from sessionfs.cli import cmd_contact
from sessionfs.cli.main import app

runner = CliRunner()


def _fake(monkeypatch, response):
    calls: list = []

    def _call(method, path, *, json_data=None, params=None, not_found="Not found."):
        calls.append((method, path, json_data, params))
        return response

    monkeypatch.setattr(cmd_contact, "_call", _call)
    monkeypatch.setattr(cmd_contact, "_org_id", lambda org: org or "org_default")
    return calls


_CONTACT = {"id": "ec_aaaaaaaaaaaa", "display_name": "Tolu", "status": "active", "aliases": []}


def test_create_sends_fields(monkeypatch):
    calls = _fake(monkeypatch, _CONTACT)
    result = runner.invoke(app, [
        "contact", "create", "Tolu", "--email", "t@v.example", "--tag", "vendor", "--alias", "T",
    ])
    assert result.exit_code == 0, result.output
    assert calls == [(
        "POST", "/api/v1/orgs/org_default/contacts",
        {"display_name": "Tolu", "email": "t@v.example", "tags": ["vendor"], "aliases": ["T"]},
        None,
    )]


def test_merge_previews_unless_yes(monkeypatch):
    preview = {"confirmed": False, "participants_moved": 1, "tickets_moved": 0, "aliases_added": []}
    calls = _fake(monkeypatch, preview)
    result = runner.invoke(app, ["contact", "merge", "ec_aaaaaaaaaaaa", "ec_bbbbbbbbbbbb", "--org", "org_x"])
    assert result.exit_code == 0, result.output
    assert calls[-1][1] == "/api/v1/orgs/org_x/contacts/ec_aaaaaaaaaaaa/merge-from/ec_bbbbbbbbbbbb"
    runner.invoke(app, ["contact", "merge", "ec_aaaaaaaaaaaa", "ec_bbbbbbbbbbbb", "--yes"])
    assert calls[-1][1].endswith("?confirm=true")


def test_edit_clear_and_alias_rm(monkeypatch):
    calls = _fake(monkeypatch, _CONTACT)
    assert runner.invoke(app, ["contact", "edit", "ec_aaaaaaaaaaaa", "--clear-role"]).exit_code == 0
    assert calls[-1][2] == {"role": None}
    assert runner.invoke(app, ["contact", "alias", "rm", "ec_aaaaaaaaaaaa", "Tee Tee"]).exit_code == 0
    assert calls[-1][:2] == ("DELETE", "/api/v1/orgs/org_default/contacts/ec_aaaaaaaaaaaa/aliases?alias=Tee%20Tee")
    assert runner.invoke(app, ["contact", "edit", "ec_aaaaaaaaaaaa"]).exit_code == 1
