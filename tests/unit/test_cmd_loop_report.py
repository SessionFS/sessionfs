"""CLI tests for `sfs loop report|insights` (Review Loop R3, Brief E2).

The HTTP boundary is patched on ``sessionfs.cli.cmd_loop`` so the tests never
touch the network. Covers ticket-id URL quoting (parity with the MCP handler)
and clean error rendering instead of dumping the raw response body.
"""

from __future__ import annotations

import re
from unittest.mock import patch

from typer.testing import CliRunner

from sessionfs.cli.cmd_loop import loop_app

runner = CliRunner()

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(s: str) -> str:
    return _ANSI_RE.sub("", s)


def _combined(result) -> str:
    parts = [
        getattr(result, "stdout", "") or "",
        getattr(result, "stderr", "") or "",
        getattr(result, "output", "") or "",
    ]
    return _plain("\n".join(parts))


class _FakeAPI:
    """Records (method, path) and returns a fixed (status, body, headers)."""

    def __init__(self, status: int, body):
        self.status = status
        self.body = body
        self.calls: list[dict] = []

    async def __call__(self, method, path, api_url, api_key, **kwargs):
        self.calls.append({"method": method, "path": path, "kwargs": kwargs})
        return self.status, self.body, {}


def test_loop_report_quotes_ticket_id_in_url():
    fake = _FakeAPI(200, {"rounds": []})
    with (
        patch(
            "sessionfs.cli.cmd_loop._resolve_project",
            return_value=("http://api", "key", "proj_1"),
        ),
        patch("sessionfs.cli.cmd_loop._api_request", new=fake),
    ):
        result = runner.invoke(loop_app, ["report", "tk_1?x=2"])
    assert result.exit_code == 0, _combined(result)
    assert fake.calls[0]["path"] == (
        "/api/v1/projects/proj_1/tickets/tk_1%3Fx%3D2/loop-report"
    )


def test_loop_report_prints_clean_api_error():
    fake = _FakeAPI(
        422,
        {
            "error": {
                "code": "duplicate_resource",
                "message": "A resource with that value already exists.",
            }
        },
    )
    with (
        patch(
            "sessionfs.cli.cmd_loop._resolve_project",
            return_value=("http://api", "key", "proj_1"),
        ),
        patch("sessionfs.cli.cmd_loop._api_request", new=fake),
    ):
        result = runner.invoke(loop_app, ["report", "tk_1"])
    assert result.exit_code == 1, _combined(result)
    out = _combined(result)
    assert "duplicate_resource: A resource with that value already exists." in out
    assert "{'error'" not in out


def test_loop_insights_prints_clean_api_error():
    fake = _FakeAPI(400, {"detail": "Something broke"})
    with (
        patch(
            "sessionfs.cli.cmd_loop._resolve_project",
            return_value=("http://api", "key", "proj_1"),
        ),
        patch("sessionfs.cli.cmd_loop._api_request", new=fake),
    ):
        result = runner.invoke(loop_app, ["insights"])
    assert result.exit_code == 1, _combined(result)
    out = _combined(result)
    assert "Something broke" in out
    assert "{'detail'" not in out
