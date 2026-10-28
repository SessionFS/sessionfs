"""Unit tests for `sfs helper …` (ask / conversations / status).

The command is a thin transport layer over the helper API, so tests patch the
shared `_get_api_config` / `_api_request` helpers (and `_stream_ask` for the
streaming path) rather than hitting the network. `_iter_sse_events` is tested
directly against a fake streaming response.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from sessionfs.cli import cmd_helper
from sessionfs.cli.cmd_helper import helper_app


# ---------------------------------------------------------------------------
# Fake transports
# ---------------------------------------------------------------------------


class _FakeApiRequest:
    """Returns a programmed sequence of (status, body, headers) tuples."""

    def __init__(self, scripted: list[tuple[int, object]]):
        self._scripted = list(scripted)
        self.calls = []

    async def __call__(self, method, path, api_url, api_key, json_data=None, extra_headers=None):
        self.calls.append((method, path, json_data))
        status, body = self._scripted.pop(0)
        return status, body, {}


class _FakeStreamResponse:
    def __init__(self, status_code=200, lines=None, body=""):
        self.status_code = status_code
        self._lines = lines or []
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return self._body.encode("utf-8")


class _FakeStreamClient:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def stream(self, method, url, *, json=None, headers=None):
        return self._response


def _invoke(cmd: str, *args: str):
    return CliRunner().invoke(helper_app, [cmd, *args])


# ---------------------------------------------------------------------------
# sse parsing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iter_sse_events_parses_frames_and_pings():
    resp = _FakeStreamResponse(
        lines=[
            "event: conversation",
            'data: {"conversation_id": "hc_1"}',
            "",
            "event: text",
            'data: {"delta": "Hel"}',
            "",
            ": ping",
            "",
            "event: text",
            'data: {"delta": "lo"}',
            "",
        ]
    )
    events = [ev async for ev in cmd_helper._iter_sse_events(_FakeStreamClient(resp), "u", {}, {})]
    assert events == [
        ("conversation", '{"conversation_id": "hc_1"}'),
        ("text", '{"delta": "Hel"}'),
        ("text", '{"delta": "lo"}'),
    ]


@pytest.mark.asyncio
async def test_iter_sse_events_joins_multiline_data():
    resp = _FakeStreamResponse(
        lines=[
            "event: text",
            'data: {"delta": "a',
            'data: b"}',
            "",
        ]
    )
    events = [ev async for ev in cmd_helper._iter_sse_events(_FakeStreamClient(resp), "u", {}, {})]
    assert events == [("text", '{"delta": "a\nb"}')]


@pytest.mark.asyncio
async def test_iter_sse_events_surfaces_http_error():
    resp = _FakeStreamResponse(status_code=409, body='{"detail": {"message": "No LLM key"}}')
    events = [ev async for ev in cmd_helper._iter_sse_events(_FakeStreamClient(resp), "u", {}, {})]
    assert len(events) == 1
    kind, data = events[0]
    assert kind == "__http_error__"
    import json

    err = json.loads(data)
    assert err["status"] == 409
    assert "No LLM key" in err["body"]


# ---------------------------------------------------------------------------
# ask
# ---------------------------------------------------------------------------


def test_ask_no_stream_renders_answer_and_citations(monkeypatch):
    fake = _FakeApiRequest(
        [
            (
                200,
                {
                    "conversation_id": "hc_1",
                    "answer": "It was the **auth** middleware.",
                    "citations": [{"session_id": "ses_1", "message_index": 2, "title": "Auth"}],
                    "tool_calls": [],
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                    "stop_reason": "stop",
                    "key_source": "user",
                },
            )
        ]
    )
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("ask", "what failed?", "--no-stream")
    assert result.exit_code == 0, result.output
    assert "auth" in result.output
    assert "ses_1#msg-2" in result.output
    assert "Auth" in result.output
    # The non-streaming call sent stream=False.
    assert fake.calls[0][2] == {"message": "what failed?", "stream": False}


def test_ask_scopes_project_and_conversation(monkeypatch):
    fake = _FakeApiRequest([(200, {"answer": "x", "citations": []})])
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke(
            "ask",
            "summarize",
            "--no-stream",
            "--project",
            "proj_1",
            "--conversation",
            "hc_1",
        )
    assert result.exit_code == 0, result.output
    assert fake.calls[0][2] == {
        "message": "summarize",
        "stream": False,
        "conversation_id": "hc_1",
        "scope": {"project_id": "proj_1"},
    }


def test_ask_session_scope_forwards_session_ids(monkeypatch):
    """`--session` alone (repeatable) forwards every id in the scope."""
    fake = _FakeApiRequest([(200, {"answer": "x", "citations": []})])
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("ask", "summarize", "--no-stream", "--session", "ses_1", "--session", "ses_2")
    assert result.exit_code == 0, result.output
    assert fake.calls[0][2] == {
        "message": "summarize",
        "stream": False,
        "scope": {"session_ids": ["ses_1", "ses_2"]},
    }


def test_ask_rejects_project_and_session_scope(monkeypatch):
    """A scope must name at most one of project / group / session."""
    fake = _FakeApiRequest([(200, {"answer": "x", "citations": []})])
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("ask", "summarize", "--no-stream", "--project", "proj_1", "--session", "ses_1")
    assert result.exit_code == 1
    assert "at most one of --project, --group or --session" in result.output
    assert fake.calls == []


def test_ask_conversation_without_scope_omits_scope(monkeypatch):
    """`--conversation` alone sends no scope — the server recovers it (F5)."""
    fake = _FakeApiRequest([(200, {"answer": "x", "citations": []})])
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("ask", "continue", "--no-stream", "--conversation", "hc_1")
    assert result.exit_code == 0, result.output
    assert fake.calls[0][2] == {
        "message": "continue",
        "stream": False,
        "conversation_id": "hc_1",
    }


def test_ask_group_id_passthrough(monkeypatch):
    """`--group pg_…` goes straight to the turn scope without a lookup."""
    fake = _FakeApiRequest([(200, {"answer": "x", "citations": []})])
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("ask", "summarize", "--no-stream", "--group", "pg_123")
    assert result.exit_code == 0, result.output
    assert len(fake.calls) == 1
    assert fake.calls[0][:2] == ("POST", "/api/v1/helper/turns")
    assert fake.calls[0][2] == {
        "message": "summarize",
        "stream": False,
        "scope": {"group_id": "pg_123"},
    }


def test_ask_group_slug_resolves_to_id(monkeypatch):
    """`--group <slug>` looks the slug up and posts the resolved group id."""
    fake = _FakeApiRequest(
        [
            (200, [{"id": "pg_123", "slug": "ai-ml"}]),
            (200, {"answer": "x", "citations": []}),
        ]
    )
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("ask", "summarize", "--no-stream", "--group", "ai-ml")
    assert result.exit_code == 0, result.output
    assert fake.calls[0][:2] == ("GET", "/api/v1/me/groups")
    assert fake.calls[1][:2] == ("POST", "/api/v1/helper/turns")
    assert fake.calls[1][2] == {
        "message": "summarize",
        "stream": False,
        "scope": {"group_id": "pg_123"},
    }


def test_ask_ambiguous_group_slug_exits_with_ids(monkeypatch):
    """An ambiguous slug is refused with the matching ids, no turn is sent."""
    fake = _FakeApiRequest(
        [
            (
                200,
                [
                    {"id": "pg_1", "slug": "ml"},
                    {"id": "pg_2", "slug": "ml"},
                ],
            )
        ]
    )
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("ask", "summarize", "--no-stream", "--group", "ml")
    assert result.exit_code == 1
    assert "matches more than one" in result.output
    assert "pg_1" in result.output
    assert "pg_2" in result.output
    assert fake.calls[0][:2] == ("GET", "/api/v1/me/groups")
    assert len(fake.calls) == 1


def test_ask_json_prints_raw_document(monkeypatch):
    fake = _FakeApiRequest(
        [(200, {"conversation_id": "hc_1", "answer": "hi", "citations": [], "stop_reason": "stop"})]
    )
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("ask", "hi", "--json")
    assert result.exit_code == 0, result.output
    assert '"conversation_id": "hc_1"' in result.output
    assert '"answer": "hi"' in result.output


def test_ask_json_preserves_markup_like_tokens(monkeypatch):
    answer = (
        "The failing check is in [session:ses_abc msg:4] and it began right after "
        "[session:ses_abc msg:7] was merged."
    )
    fake = _FakeApiRequest(
        [
            (
                200,
                {
                    "conversation_id": "hc_1",
                    "answer": answer,
                    "citations": [
                        {"session_id": "ses_abc", "message_index": 4, "title": "Failing check"},
                    ],
                    "stop_reason": "stop",
                },
            )
        ]
    )
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("ask", "why did it fail?", "--json")
    assert result.exit_code == 0, result.output
    # The output is a single valid JSON document, tokens intact.
    parsed = json.loads(result.output)
    assert parsed["answer"] == answer
    assert "[session:ses_abc msg:4]" in result.output
    assert parsed["citations"][0]["session_id"] == "ses_abc"


def test_ask_plain_output_preserves_markup_like_tokens(monkeypatch):
    fake = _FakeApiRequest(
        [
            (
                200,
                {
                    "conversation_id": "hc_1",
                    "answer": "See [session:ses_abc msg:4] for the stack trace.",
                    "citations": [],
                    "stop_reason": "stop",
                },
            )
        ]
    )
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("ask", "why?", "--no-stream")
    assert result.exit_code == 0, result.output
    assert "[session:ses_abc msg:4]" in result.output


def test_ask_streams_by_default(monkeypatch):
    captured = {}

    async def fake_stream_ask(api_url, api_key, payload):
        captured["api_url"] = api_url
        captured["payload"] = payload

    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_stream_ask", new=fake_stream_ask),
    ):
        result = _invoke("ask", "what failed?")
    assert result.exit_code == 0, result.output
    assert captured["api_url"] == "https://api.test"
    # Streaming payload omits `stream` (the SSE Accept header drives it).
    assert captured["payload"] == {"message": "what failed?"}


def test_ask_surfaces_error_envelope(monkeypatch):
    fake = _FakeApiRequest(
        [(409, {"detail": {"code": "helper_key_required", "message": "No LLM key configured"}})]
    )
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("ask", "hi", "--no-stream")
    assert result.exit_code == 1
    assert "No LLM key configured" in result.output


def test_ask_rejects_empty_question():
    result = _invoke("ask", "   ")
    assert result.exit_code == 1
    assert "non-empty" in result.output


# ---------------------------------------------------------------------------
# streaming render
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_ask_renders_text_and_citations(monkeypatch, capsys):
    async def fake_events(client, url, headers, payload):
        yield ("conversation", '{"conversation_id": "hc_1"}')
        yield ("text", '{"delta": "Hel"}')
        yield ("text", '{"delta": "lo"}')
        yield ("citation", '{"session_id": "ses_1", "message_index": 2, "title": "Auth"}')
        yield (
            "done",
            '{"stop_reason": "stop", "input_tokens": 3, "output_tokens": 2, "key_source": "user"}',
        )

    monkeypatch.setattr(cmd_helper, "_iter_sse_events", fake_events)
    await cmd_helper._stream_ask("https://api.test", "key", {"message": "hi"})
    out = capsys.readouterr().out
    assert "Hello" in out
    assert "ses_1#msg-2" in out
    assert "Auth" in out


def test_ask_no_stream_renders_doc_citations(monkeypatch):
    fake = _FakeApiRequest(
        [
            (
                200,
                {
                    "conversation_id": "hc_1",
                    "answer": "Connect Jira first [doc:integrations/jira#setup].",
                    "citations": [
                        {"session_id": "ses_1", "message_index": None, "title": "Auth"},
                        {
                            "slug": "integrations/jira",
                            "anchor": "setup",
                            "title": "Jira Cloud",
                            "url": "https://sessionfs.dev/integrations/jira/#setup",
                        },
                    ],
                    "tool_calls": [],
                    "usage": {"input_tokens": 3, "output_tokens": 2},
                    "stop_reason": "stop",
                    "key_source": "user",
                },
            )
        ]
    )
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("ask", "how do I set up jira?", "--no-stream")
    assert result.exit_code == 0, result.output
    assert "Jira Cloud — https://sessionfs.dev/integrations/jira/#setup" in result.output
    assert "Auth — ses_1" in result.output


@pytest.mark.asyncio
async def test_stream_ask_renders_doc_citation_events(monkeypatch, capsys):
    async def fake_events(client, url, headers, payload):
        yield ("conversation", '{"conversation_id": "hc_1"}')
        yield ("text", '{"delta": "See [doc:quickstart]."}')
        yield (
            "doc_citation",
            '{"slug": "quickstart", "anchor": null, "title": "Quickstart",'
            ' "url": "https://sessionfs.dev/quickstart/"}',
        )
        yield (
            "done",
            '{"stop_reason": "stop", "input_tokens": 3, "output_tokens": 2, "key_source": "user"}',
        )

    monkeypatch.setattr(cmd_helper, "_iter_sse_events", fake_events)
    await cmd_helper._stream_ask("https://api.test", "key", {"message": "hi"})
    out = capsys.readouterr().out
    assert "Citations:" in out
    assert "Quickstart — https://sessionfs.dev/quickstart/" in out


@pytest.mark.asyncio
async def test_stream_ask_surfaces_error_event(monkeypatch, capsys):
    async def fake_events(client, url, headers, payload):
        yield ("text", '{"delta": "partial"}')
        yield ("error", '{"code": "provider_error", "message": "The model is down."}')

    monkeypatch.setattr(cmd_helper, "_iter_sse_events", fake_events)
    with pytest.raises(typer.Exit) as exc_info:
        await cmd_helper._stream_ask("https://api.test", "key", {"message": "hi"})
    assert exc_info.value.exit_code == 1
    captured = capsys.readouterr()
    assert "The model is down." in captured.err
    # The partial delta streams once and must not be re-rendered a second time.
    assert captured.out.count("partial") == 1


def test_ask_stream_error_exits_nonzero(monkeypatch):
    async def fake_events(client, url, headers, payload):
        yield ("text", '{"delta": "partial"}')
        yield ("error", '{"code": "provider_error", "message": "The model is down."}')

    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_iter_sse_events", new=fake_events),
    ):
        result = _invoke("ask", "hi")
    assert result.exit_code == 1
    assert "The model is down." in result.output


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_enabled(monkeypatch):
    fake = _FakeApiRequest(
        [(200, {"enabled": True, "key_source": "user", "provider": "anthropic", "model": "claude"})]
    )
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("status")
    assert result.exit_code == 0, result.output
    assert "enabled" in result.output
    assert "anthropic" in result.output


def test_status_disabled_plan(monkeypatch):
    fake = _FakeApiRequest(
        [(200, {"enabled": False, "key_source": None, "provider": None, "model": None})]
    )
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("status")
    assert result.exit_code == 0, result.output
    assert "not included in your plan" in result.output


def test_status_no_key(monkeypatch):
    fake = _FakeApiRequest(
        [(200, {"enabled": True, "key_source": None, "provider": None, "model": None})]
    )
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("status")
    assert result.exit_code == 0, result.output
    assert "no LLM key" in result.output


# ---------------------------------------------------------------------------
# conversations
# ---------------------------------------------------------------------------


def test_conversations_list(monkeypatch):
    fake = _FakeApiRequest(
        [
            (
                200,
                [
                    {"id": "hc_1", "org_id": None, "title": "First", "created_at": "", "updated_at": "t2"},
                    {"id": "hc_2", "org_id": None, "title": None, "created_at": "", "updated_at": "t1"},
                ],
            )
        ]
    )
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("conversations", "list")
    assert result.exit_code == 0, result.output
    assert "hc_1" in result.output
    assert "First" in result.output
    assert "Untitled conversation" in result.output


def test_conversations_show(monkeypatch):
    fake = _FakeApiRequest(
        [
            (
                200,
                {
                    "id": "hc_1",
                    "org_id": None,
                    "title": "Thread",
                    "created_at": "",
                    "updated_at": "",
                    "messages": [
                        {"id": "m1", "role": "user", "content": "question?"},
                        {
                            "id": "m2",
                            "role": "assistant",
                            "content": "the answer",
                            "citations": [{"session_id": "ses_1", "message_index": 0, "title": "Auth"}],
                        },
                    ],
                },
            )
        ]
    )
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("conversations", "show", "hc_1")
    assert result.exit_code == 0, result.output
    assert "Thread" in result.output
    assert "question?" in result.output
    assert "the answer" in result.output
    assert "ses_1#msg-0" in result.output


def test_conversations_delete_with_yes(monkeypatch):
    fake = _FakeApiRequest([(200, {"deleted": True})])
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("conversations", "delete", "hc_1", "--yes")
    assert result.exit_code == 0, result.output
    assert fake.calls[0][0] == "DELETE"
    assert fake.calls[0][1] == "/api/v1/helper/conversations/hc_1"
    assert "Deleted" in result.output


def test_conversations_surfaces_not_found(monkeypatch):
    fake = _FakeApiRequest([(404, {"detail": "Conversation not found"})])
    with (
        patch.object(cmd_helper, "_get_api_config", return_value=("https://api.test", "key")),
        patch.object(cmd_helper, "_api_request", new=fake),
    ):
        result = _invoke("conversations", "show", "hc_missing")
    assert result.exit_code == 1
    assert "Conversation not found" in result.output


@pytest.mark.asyncio
async def test_stream_ask_renders_ticket_and_wiki_citation_events(monkeypatch, capsys):
    async def fake_events(client, url, headers, payload):
        yield ("conversation", '{"conversation_id": "hc_1"}')
        yield ("text", '{"delta": "See it."}')
        yield (
            "ticket_citation",
            '{"kind": "ticket", "ticket_id": "tk_0123456789abcdef", "project_id":'
            ' "proj_0123456789abcdef", "title": "Fix SSO", "at": "2026-10-20T00:00:00+00:00"}',
        )
        yield (
            "wiki_citation",
            '{"kind": "wiki", "project_id": "proj_0123456789abcdef", "slug": "architecture",'
            ' "title": "Architecture", "at": "2026-10-20T00:00:00+00:00"}',
        )
        yield (
            "done",
            '{"stop_reason": "stop", "input_tokens": 3, "output_tokens": 2, "key_source": "user"}',
        )

    monkeypatch.setattr(cmd_helper, "_iter_sse_events", fake_events)
    await cmd_helper._stream_ask("https://api.test", "key", {"message": "hi"})
    out = capsys.readouterr().out
    assert "Fix SSO — tk_0123456789abcdef" in out
    assert "Architecture — wiki:proj_0123456789abcdef/architecture" in out


def test_render_citations_handles_every_kind(capsys):
    cmd_helper._render_citations(
        [
            {"kind": "session", "session_id": "ses_1", "message_index": 2, "title": "Auth"},
            {"session_id": "ses_2", "message_index": None, "title": "Legacy"},
            {"kind": "doc", "slug": "quickstart", "title": "Quickstart",
             "url": "https://sessionfs.dev/quickstart/"},
            {"slug": "cli", "anchor": None, "title": "CLI", "url": "https://sessionfs.dev/cli/"},
            {"kind": "ticket", "ticket_id": "tk_1", "project_id": "proj_1", "title": "T"},
            {"kind": "wiki", "project_id": "proj_1", "slug": "arch", "title": "Arch"},
        ],
        markdown=False,
    )
    out = capsys.readouterr().out
    assert "Auth — ses_1#msg-2" in out
    assert "Legacy — ses_2" in out
    assert "Quickstart — https://sessionfs.dev/quickstart/" in out
    assert "CLI — https://sessionfs.dev/cli/" in out
    assert "T — tk_1" in out
    assert "Arch — wiki:proj_1/arch" in out
