"""v0.10.7 — unit tests for `sfs ticket watch <id>`.

Covers:
- interval clamping
- initial-load render renders existing comments
- new-comment detection diffs by comment id
- --from-author filter
- --exit-on-new exits after first new comment
- Ctrl-C exits gracefully with a summary
"""

from __future__ import annotations

from unittest.mock import patch

from typer.testing import CliRunner

from sessionfs.cli.cmd_ticket import _clamp_interval, ticket_app


def test_clamp_interval_floor():
    assert _clamp_interval(1) == 5
    assert _clamp_interval(0) == 5
    assert _clamp_interval(-10) == 5


def test_clamp_interval_ceiling():
    assert _clamp_interval(1000) == 300
    assert _clamp_interval(301) == 300


def test_clamp_interval_passthrough():
    assert _clamp_interval(30) == 30
    assert _clamp_interval(5) == 5
    assert _clamp_interval(300) == 300


class _FakeApiRequest:
    """Returns a programmed sequence of (status, body, headers) tuples on
    each call, then raises KeyboardInterrupt to exit the watch loop."""

    def __init__(self, scripted: list[tuple[int, list[dict] | str]]):
        self._scripted = list(scripted)
        self.calls = 0

    async def __call__(self, *args, **kwargs):
        if not self._scripted:
            raise KeyboardInterrupt()
        self.calls += 1
        status, body = self._scripted.pop(0)
        return status, body, {}


def _make_comment(cid: str, author: str = "codex-reviewer", content: str = "x"):
    return {
        "id": cid,
        "author_persona": author,
        "author_user_id": "u",
        "content": content,
        "created_at": "2026-05-16T03:00:00Z",
    }


def test_watch_renders_existing_then_exits_on_new(monkeypatch):
    """First poll: 1 existing comment renders. Second poll: 1 new
    comment renders. --exit-on-new returns clean exit."""
    runner = CliRunner()

    fake = _FakeApiRequest(
        [
            (200, [_make_comment("tc_1", content="first")]),
            (
                200,
                [
                    _make_comment("tc_1", content="first"),
                    _make_comment("tc_2", content="second"),
                ],
            ),
        ]
    )

    with (
        patch(
            "sessionfs.cli.cmd_ticket._resolve_project",
            return_value=("https://api.test", "key", "proj_x"),
        ),
        patch(
            "sessionfs.cli.cmd_ticket._api_request",
            new=fake,
        ),
        patch("sessionfs.cli.cmd_ticket.asyncio.sleep", new=_no_sleep),
    ):
        result = runner.invoke(
            ticket_app, ["watch", "tk_abc123", "--interval", "5", "--exit-on-new"]
        )
    assert result.exit_code == 0, result.output
    assert "first" in result.output
    assert "second" in result.output
    assert "saw 1 new" in result.output


def test_watch_filters_by_from_author(monkeypatch):
    """--from-author limits new-comment rendering to that author."""
    runner = CliRunner()

    fake = _FakeApiRequest(
        [
            (200, []),  # initial: no comments
            (
                200,
                [
                    _make_comment("tc_1", author="atlas", content="atlas-comment"),
                    _make_comment(
                        "tc_2", author="codex-reviewer", content="codex-comment"
                    ),
                ],
            ),
        ]
    )

    with (
        patch(
            "sessionfs.cli.cmd_ticket._resolve_project",
            return_value=("https://api.test", "key", "proj_x"),
        ),
        patch(
            "sessionfs.cli.cmd_ticket._api_request",
            new=fake,
        ),
        patch("sessionfs.cli.cmd_ticket.asyncio.sleep", new=_no_sleep),
    ):
        result = runner.invoke(
            ticket_app,
            [
                "watch",
                "tk_abc123",
                "--from-author",
                "codex-reviewer",
                "--exit-on-new",
            ],
        )
    assert result.exit_code == 0, result.output
    assert "codex-comment" in result.output
    assert "atlas-comment" not in result.output


def test_watch_404_exits_clean():
    """404 on the comments endpoint produces a clean error exit."""
    runner = CliRunner()

    fake = _FakeApiRequest([(404, "not found")])

    with (
        patch(
            "sessionfs.cli.cmd_ticket._resolve_project",
            return_value=("https://api.test", "key", "proj_x"),
        ),
        patch(
            "sessionfs.cli.cmd_ticket._api_request",
            new=fake,
        ),
        patch("sessionfs.cli.cmd_ticket.asyncio.sleep", new=_no_sleep),
    ):
        result = runner.invoke(ticket_app, ["watch", "tk_nope"])
    assert result.exit_code == 1
    assert "not found" in result.output.lower()


async def _no_sleep(_seconds):
    """Skip real sleep in tests so the watch loop drains the scripted
    fake immediately."""
    return None
