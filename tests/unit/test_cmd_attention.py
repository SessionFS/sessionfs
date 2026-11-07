"""CLI tests for `sfs attention` (tk_dc118ffd6e804a9a). The API is faked."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from sessionfs.cli import cmd_attention
from sessionfs.cli.main import app

runner = CliRunner()

_BODY = {
    "items": [
        {"item_key": "fy_1", "kind": "reminder", "band": "now", "title": "Call Bob",
         "why": "Reminder · overdue", "link": "/reminders?reminder=rem_1"},
        {"item_key": "fy_2", "kind": "ticket", "band": "your_turn", "title": "Fix login",
         "why": "Assigned to you · due today", "link": "/projects/p?tab=work"},
    ],
    "counts": {"now": 1, "your_turn": 1, "lately": 0, "total": 2},
    "sections": {"tickets": {"count": 1, "error": None}, "proof": {"count": 0, "error": "couldn't load"}},
    "truncated": False,
}


@pytest.fixture
def api(monkeypatch):
    calls: list[dict] = []
    state = {"status": 200, "body": _BODY}

    async def fake(method, path, api_url, api_key, json_data=None, extra_headers=None, params=None, timeout=30):
        calls.append({"method": method, "path": path, "params": params})
        return state["status"], state["body"], {}

    monkeypatch.setattr(cmd_attention, "_api_request", fake)
    monkeypatch.setattr(cmd_attention, "_get_api_config", lambda: ("https://api.test", "key"))
    return calls, state


def test_lists_bands_and_section_errors(api):
    calls, _ = api
    res = runner.invoke(app, ["attention"])
    assert res.exit_code == 0, res.output
    assert calls == [{"method": "GET", "path": "/api/v1/me/for-you", "params": {"limit": 100}}]
    assert "Now (1)" in res.output and "Your turn (1)" in res.output
    assert "Call Bob" in res.output and "Fix login" in res.output
    assert "Couldn't load: proof" in res.output


def test_band_and_json(api):
    calls, _ = api
    res = runner.invoke(app, ["attention", "--band", "now", "--json", "--limit", "5"])
    assert res.exit_code == 0, res.output
    assert calls[0]["params"] == {"limit": 5, "band": "now"}
    assert json.loads(res.output)["counts"]["total"] == 2


def test_bad_band_is_rejected_locally(api):
    calls, _ = api
    res = runner.invoke(app, ["attention", "--band", "soon"])
    assert res.exit_code != 0 and not calls


def test_caught_up_and_api_error(api):
    calls, state = api
    state["body"] = {"items": [], "counts": {"total": 0}, "sections": {}, "truncated": False}
    res = runner.invoke(app, ["attention"])
    assert "all caught up" in res.output
    state["status"], state["body"] = 403, {"detail": "nope"}
    res = runner.invoke(app, ["attention"])
    assert res.exit_code == 1


def test_conversation_items_are_tagged_with_their_range():
    chat = {"title": "Confirm the date", "context": {"meeting": {"kind": "chat", "range": "M2-3", "lines": "2-3"}}}
    meeting = {"title": "Send notes", "context": {"meeting": {"kind": "meeting", "lines": "4"}}}
    assert cmd_attention._what(chat) == "[chat M2-3] Confirm the date"
    assert cmd_attention._what(meeting) == "[meeting L4] Send notes"
    assert cmd_attention._what({"title": "Fix login"}) == "Fix login"
