"""CLI tests for `sfs remind …` (tk_8a858d6354fc4ef3, R2). The API is faked."""

from __future__ import annotations

from datetime import datetime

import pytest
import typer
from typer.testing import CliRunner

from sessionfs.cli import cmd_remind
from sessionfs.cli.cmd_remind import parse_at, remind_app

runner = CliRunner()

_REM = {
    "id": "rem_1",
    "title": "Book follow-up",
    "status": "open",
    "is_due": False,
    "due_at": "2026-11-06T09:00:00+00:00",
    "timezone": "Europe/London",
    "source": None,
    "source_hidden": False,
    "snooze_count": 0,
}


class Fake:
    def __init__(self):
        self.calls: list[dict] = []
        self.responses: dict = {}

    async def __call__(self, method, path, api_url, api_key, json_data=None, extra_headers=None, params=None, timeout=30):
        self.calls.append({"method": method, "path": path, "json": json_data, "params": params})
        status, body = self.responses.get((method, path), (200, dict(_REM)))
        return status, body, {}


@pytest.fixture
def api(monkeypatch):
    fake = Fake()
    monkeypatch.setattr(cmd_remind, "_api_request", fake)
    monkeypatch.setattr(cmd_remind, "_get_api_config", lambda: ("https://api.test", "key"))
    monkeypatch.setattr(cmd_remind, "_local_zone_name", lambda: "America/New_York")
    return fake


def _run(args):
    return runner.invoke(remind_app, args)


# ── --at parsing ──


def test_parse_at_with_tz_keeps_wall_time_and_zone():
    assert parse_at("2026-11-06 09:00", "Europe/London") == ("2026-11-06T09:00:00", "Europe/London")


def test_parse_at_without_tz_uses_machine_zone(monkeypatch):
    monkeypatch.setattr(cmd_remind, "_local_zone_name", lambda: "Asia/Tokyo")
    assert parse_at("2026-11-06T09:30", None) == ("2026-11-06T09:30:00", "Asia/Tokyo")


def test_parse_at_without_any_zone_sends_local_offset(monkeypatch):
    monkeypatch.setattr(cmd_remind, "_local_zone_name", lambda: None)
    iso, zone = parse_at("2026-11-06 09:00", None)
    assert zone is None
    assert datetime.fromisoformat(iso).tzinfo is not None


def test_parse_at_offset_value_is_sent_unchanged():
    iso, zone = parse_at("2026-11-06T09:00:00Z", None)
    assert iso == "2026-11-06T09:00:00+00:00" and zone is None


def test_parse_at_bare_date_means_nine():
    assert parse_at("2026-11-06", "UTC")[0] == "2026-11-06T09:00:00"


def test_parse_at_rejects_bad_values():
    with pytest.raises(typer.BadParameter):
        parse_at("next thursday", None)
    with pytest.raises(typer.BadParameter):
        parse_at("2026-11-06 09:00", "Mars/Olympus")


# ── commands ──


def test_add_with_tz_and_meeting_lines(api):
    res = _run(["add", "Book follow-up", "--at", "2026-11-06 09:00", "--tz", "Europe/London",
                "--meeting", "mt_abc:12-18"])
    assert res.exit_code == 0, res.output
    call = api.calls[0]
    assert (call["method"], call["path"]) == ("POST", "/api/v1/me/reminders")
    assert call["json"] == {
        "title": "Book follow-up",
        "due_at": "2026-11-06T09:00:00",
        "timezone": "Europe/London",
        "source": {"kind": "meeting", "id": "mt_abc", "lines": "12-18"},
    }
    assert "rem_1" in res.output


def test_add_without_tz_ticket_and_for(api):
    res = _run(["add", "Chase", "--at", "2026-11-06 09:00", "--ticket", "tk_1", "--for", "a@b.test"])
    assert res.exit_code == 0, res.output
    body = api.calls[0]["json"]
    assert body["timezone"] == "America/New_York"
    assert body["source"] == {"kind": "ticket", "id": "tk_1"}
    assert body["owner"] == "a@b.test"


def test_add_refuses_meeting_and_ticket(api):
    res = _run(["add", "x", "--at", "2026-11-06 09:00", "--meeting", "mt_1", "--ticket", "tk_1"])
    assert res.exit_code != 0 and not api.calls


def test_add_bad_at_makes_no_call(api):
    res = _run(["add", "x", "--at", "soon"])
    assert res.exit_code != 0 and not api.calls


def test_list_status_flags(api):
    api.responses[("GET", "/api/v1/me/reminders")] = (200, {"items": [dict(_REM, is_due=True)]})
    assert _run(["list"]).exit_code == 0
    assert api.calls[-1]["params"]["status"] == "open"
    assert _run(["list", "--all"]).exit_code == 0
    assert api.calls[-1]["params"]["status"] == "all"
    res = _run(["list", "--done", "--due-before", "2026-12-01 00:00"])
    assert res.exit_code == 0, res.output
    assert api.calls[-1]["params"]["status"] == "done"
    assert api.calls[-1]["params"]["due_before"] == "2026-12-01T00:00:00-05:00"


def test_show_note_done_dismiss(api):
    api.responses[("GET", "/api/v1/me/reminders/rem_1/notes")] = (200, {"items": [{"body": "hi", "created_at": "x"}]})
    res = _run(["show", "rem_1"])
    assert res.exit_code == 0 and "hi" in res.output
    assert _run(["note", "rem_1", "called"]).exit_code == 0
    assert api.calls[-1]["json"] == {"body": "called"}
    assert _run(["done", "rem_1"]).exit_code == 0
    assert api.calls[-1] == {"method": "PATCH", "path": "/api/v1/me/reminders/rem_1", "json": {"status": "done"}, "params": None}
    assert _run(["dismiss", "rem_1"]).exit_code == 0
    assert api.calls[-1]["json"] == {"status": "dismissed"}


def test_snooze_presets_and_until(api):
    assert _run(["snooze", "rem_1", "next-week"]).exit_code == 0
    assert api.calls[-1]["json"] == {"preset": "next_week"}
    assert _run(["snooze", "rem_1", "1h"]).exit_code == 0
    assert api.calls[-1]["json"] == {"preset": "1h"}
    res = _run(["snooze", "rem_1", "--until", "2026-11-07 10:00", "--tz", "UTC"])
    assert res.exit_code == 0, res.output
    assert api.calls[-1]["json"] == {"preset": "custom", "until": "2026-11-07T10:00:00+00:00"}
    n = len(api.calls)
    assert _run(["snooze", "rem_1"]).exit_code != 0
    assert _run(["snooze", "rem_1", "later"]).exit_code != 0
    assert len(api.calls) == n


def test_not_found_message(api):
    api.responses[("GET", "/api/v1/me/reminders/rem_x")] = (404, {"detail": "Reminder not found"})
    res = _run(["show", "rem_x"])
    assert res.exit_code == 1


def test_reminder_id_is_one_encoded_path_segment(api):
    assert _run(["done", "../../sessions/x?y"]).exit_code == 0
    assert api.calls[-1]["path"] == "/api/v1/me/reminders/..%2F..%2Fsessions%2Fx%3Fy"
    assert _run(["note", "a/b", "hi"]).exit_code == 0
    assert api.calls[-1]["path"] == "/api/v1/me/reminders/a%2Fb/notes"
