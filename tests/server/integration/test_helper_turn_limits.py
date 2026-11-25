"""Per-question helper limits, specific stop messages and "try again"
keeping the conversation's history (GitHub #143).

The model is faked at ``stream_chat`` (for ``run_agent``'s own limits) or at
the ``run_agent`` boundary (for the route); no provider is ever contacted.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.judge import agent_stream
from sessionfs.judge.agent_stream import (
    Done,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolResult,
    ToolResultEvent,
    Usage,
)
from sessionfs.server.db.models import HelperConfig, HelperConversation, HelperMessage
from sessionfs.server.helper.keys import ResolvedLLM
from sessionfs.server.helper.settings import (
    HelperModelSettings,
    HelperSettingsPatch,
    effective_settings,
)
import sessionfs.server.routes.helper as helper_mod
from tests.server.integration.test_helper_api import (
    _fake_resolve,
    _fake_run_agent_recording,
    _hdrs,
    _make_user,
    _rows,
    _stream_turn,
)


# ---------------------------------------------------------------------------
# run_agent limits
# ---------------------------------------------------------------------------


def _always_tools(calls: list[dict], *, per_step: int = 1, usage: int | None = 10):
    """A fake model that asks for ``per_step`` tools on every tool-enabled
    call and answers in plain text on the tool-free synthesis call."""

    async def fake_stream_chat(**kwargs):
        calls.append(kwargs)
        if kwargs["tools"]:
            for i in range(per_step):
                yield ToolCallEvent(
                    ToolCall(id=f"c{len(calls)}_{i}", name="search_sessions", arguments={})
                )
            if usage is not None:
                yield Usage(usage, 1)
            yield Done("tool_use")
        else:
            yield TextDelta("Here is what I found.")
            yield Done("stop")

    return fake_stream_chat


async def _run(**kwargs) -> list:
    defaults = dict(
        model="m",
        system="sys",
        messages=[{"role": "user", "content": "q"}],
        tools=[agent_stream.ToolSpec("search_sessions", "d", {"type": "object"})],
        api_key="k",
        final_synthesis="Answer now.",
    )
    defaults.update(kwargs)
    return [e async for e in agent_stream.run_agent(**defaults)]


@pytest.mark.asyncio
async def test_tool_call_cap_counts_calls_across_rounds(monkeypatch):
    """8 rounds x 8 parallel calls used to run 64 calls; the cap counts calls."""
    calls: list[dict] = []
    monkeypatch.setattr(agent_stream, "stream_chat", _always_tools(calls, per_step=8))
    ran: list[str] = []

    async def execute_tool(call):
        ran.append(call.id)
        return ToolResult(call.id, "row")

    events = await _run(execute_tool=execute_tool, max_steps=9, max_tool_calls=25)
    assert len(ran) == 25
    skipped = [e for e in events if isinstance(e, ToolResultEvent) and e.result.not_run]
    assert skipped and all(e.result.is_error for e in skipped)
    # The tool-free synthesis still runs, then the turn reports the cap.
    assert calls[-1]["tools"] == []
    assert "Here is what I found." in "".join(e.text for e in events if isinstance(e, TextDelta))
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "tool_call_cap"


@pytest.mark.asyncio
async def test_consecutive_failures_stop_the_question(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(agent_stream, "stream_chat", _always_tools(calls, per_step=2))
    ran: list[str] = []

    async def execute_tool(call):
        ran.append(call.id)
        return ToolResult(call.id, "backend unavailable", is_error=True)

    events = await _run(execute_tool=execute_tool, max_consecutive_failures=3)
    assert len(ran) == 3
    assert isinstance(events[-1], Done) and events[-1].stop_reason == "tool_failures"
    assert calls[-1]["tools"] == []


@pytest.mark.asyncio
async def test_a_success_resets_the_failure_streak(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(agent_stream, "stream_chat", _always_tools(calls, per_step=1))
    outcomes = iter([True, True, False, True, True, False, True, True, True])

    async def execute_tool(call):
        return ToolResult(call.id, "x", is_error=next(outcomes))

    events = await _run(execute_tool=execute_tool, max_steps=20, max_consecutive_failures=3)
    ran = [e for e in events if isinstance(e, ToolResultEvent) and not e.result.not_run]
    assert len(ran) == 9
    assert events[-1].stop_reason == "tool_failures"


@pytest.mark.asyncio
async def test_input_token_ceiling_uses_reported_usage(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(agent_stream, "stream_chat", _always_tools(calls, usage=40_000))

    async def execute_tool(call):
        return ToolResult(call.id, "row")

    events = await _run(execute_tool=execute_tool, max_steps=20, max_input_tokens=150_000)
    reported = sum(e.input_tokens for e in events if isinstance(e, Usage))
    # Never past the ceiling, and no synthesis call (it would go past it).
    assert reported <= 150_000
    assert all(c["tools"] for c in calls)
    assert events[-1].stop_reason == "input_token_cap"


@pytest.mark.asyncio
async def test_input_token_ceiling_estimates_without_usage(monkeypatch):
    """A provider that reports no usage is counted by a size estimate."""
    calls: list[dict] = []
    monkeypatch.setattr(agent_stream, "stream_chat", _always_tools(calls, usage=None))

    async def execute_tool(call):
        return ToolResult(call.id, "x" * 30_000)

    events = await _run(execute_tool=execute_tool, max_steps=50, max_input_tokens=50_000)
    assert events[-1].stop_reason == "input_token_cap"
    assert len(calls) < 10


@pytest.mark.asyncio
async def test_no_limits_keep_the_old_behaviour(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(agent_stream, "stream_chat", _always_tools(calls, per_step=1))

    async def execute_tool(call):
        return ToolResult(call.id, "fail", is_error=True)

    events = await _run(execute_tool=execute_tool, max_steps=3)
    assert events[-1].stop_reason == "max_steps"


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


def test_limit_defaults_and_bounds():
    s = HelperModelSettings()
    assert (s.max_tool_calls, s.max_consecutive_tool_failures, s.max_input_tokens) == (
        25,
        3,
        150_000,
    )
    for bad in (
        {"max_tool_calls": 0},
        {"max_tool_calls": 101},
        {"max_consecutive_tool_failures": 0},
        {"max_consecutive_tool_failures": 11},
        {"max_input_tokens": 9_999},
        {"max_input_tokens": 1_000_001},
        {"max_tool_calls": True},
    ):
        with pytest.raises(ValidationError):
            HelperModelSettings.model_validate(bad)
    with pytest.raises(ValidationError):
        HelperSettingsPatch.model_validate({"ceiling_max_tool_calls": 3})


def test_org_ceilings_cap_the_limits():
    org = HelperConfig(
        org_id="org_x",
        settings=json.dumps(
            {
                "max_tool_calls": 80,
                "max_input_tokens": 500_000,
                "max_consecutive_tool_failures": 9,
                "ceiling_max_tool_calls": 30,
                "ceiling_max_input_tokens": 200_000,
                "ceiling_max_consecutive_tool_failures": 4,
            }
        ),
    )
    resolved = ResolvedLLM(
        provider="openai", model="m", api_key="k", base_url=None, key_source="org", key_org_id="org_x"
    )
    s = effective_settings(resolved, org, None)
    assert (s.max_tool_calls, s.max_input_tokens, s.max_consecutive_tool_failures) == (
        30,
        200_000,
        4,
    )


@pytest.mark.asyncio
async def test_settings_round_trip_and_reset(client: AsyncClient, db_session: AsyncSession):
    _user, key = await _make_user(db_session)
    resp = await client.put(
        "/api/v1/settings/helper",
        headers=_hdrs(key),
        json={"settings": {"max_tool_calls": 40, "max_input_tokens": 300_000}},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["settings"]["max_tool_calls"] == 40
    assert resp.json()["settings"]["max_input_tokens"] == 300_000
    resp = await client.put(
        "/api/v1/settings/helper",
        headers=_hdrs(key),
        json={"settings": {"max_tool_calls": None}},
    )
    assert resp.json()["settings"]["max_tool_calls"] == 25
    assert resp.json()["settings"]["max_input_tokens"] == 300_000
    resp = await client.put(
        "/api/v1/settings/helper",
        headers=_hdrs(key),
        json={"settings": {"max_consecutive_tool_failures": 11}},
    )
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Route: limits reach run_agent, stop messages name the reason
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_route_passes_the_limits(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    _user, key = await _make_user(db_session)
    seen: list[dict] = []

    async def fake(*, execute_tool, **kwargs):
        seen.append(kwargs)
        yield TextDelta("Answer.")
        yield Done("stop")

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_mod, "run_agent", fake)
    resp = await client.post(
        "/api/v1/helper/turns", headers=_hdrs(key), json={"message": "hi", "stream": False}
    )
    assert resp.status_code == 200, resp.text
    assert seen[0]["max_tool_calls"] == 25
    assert seen[0]["max_consecutive_failures"] == 3
    assert seen[0]["max_input_tokens"] == 150_000
    assert resp.json()["stop_reason"] == "stop"
    assert resp.json()["stop_message"] is None


def _fake_failing_tools(names: list[str], *, stop_reason: str = "tool_failures"):
    async def fake(*, execute_tool, **kwargs):
        for i, name in enumerate(names):
            call = ToolCall(id=f"c{i}", name=name, arguments={})
            yield ToolCallEvent(call)
            yield ToolResultEvent(
                call, ToolResult(call.id, "Search backend timed out.\nTraceback: secret", is_error=True)
            )
        yield Usage(1000, 10)
        yield Done(stop_reason)

    return fake


@pytest.mark.asyncio
async def test_failure_stop_names_tools_and_errors(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    _user, key = await _make_user(db_session)
    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(
        helper_mod,
        "run_agent",
        _fake_failing_tools(["search_sessions", "list_tickets", "search_sessions"]),
    )
    events = await _stream_turn(client, key, {"message": "find it"})
    done = next(d for e, d in events if e == "done")
    assert done["stop_reason"] == "tool_failures"
    message = done["stop_message"]
    assert message.startswith("Stopped early: 3 tool calls failed in a row:")
    assert "search_sessions (Search backend timed out)" in message
    assert "list_tickets" in message
    # Only the first line of an error is ever shown.
    assert "Traceback" not in message
    text = "".join(d["delta"] for e, d in events if e == "text")
    assert text.endswith(message)

    conversation_id = next(d["conversation_id"] for e, d in events if e == "conversation")
    answer = [r for r in await _rows(db_session, conversation_id) if r.role == "assistant"][0]
    assert answer.stop_reason == "tool_failures"
    assert answer.content.endswith(message)
    resp = await client.get(f"/api/v1/helper/conversations/{conversation_id}", headers=_hdrs(key))
    stored = [m for m in resp.json()["messages"] if m["role"] == "assistant"][0]
    assert stored["stop_reason"] == "tool_failures"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stop_reason", "expected"),
    [
        ("tool_call_cap", "reached its limit of 25 tool calls"),
        ("input_token_cap", "reached its input limit of 150,000 tokens"),
    ],
)
async def test_limit_stops_name_the_limit(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    stop_reason: str,
    expected: str,
):
    _user, key = await _make_user(db_session)

    async def fake(*, execute_tool, **kwargs):
        yield TextDelta("Partial findings.")
        yield Done(stop_reason)

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_mod, "run_agent", fake)
    resp = await client.post(
        "/api/v1/helper/turns", headers=_hdrs(key), json={"message": "q", "stream": False}
    )
    body = resp.json()
    assert body["stop_reason"] == stop_reason
    assert expected in body["stop_message"]
    assert body["answer"].startswith("Partial findings.")
    assert body["answer"].endswith(body["stop_message"])


@pytest.mark.asyncio
async def test_unknown_tool_is_a_missing_capability(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    _user, key = await _make_user(db_session)

    async def fake(*, execute_tool, **kwargs):
        call = ToolCall(id="c1", name="send_email", arguments={})
        yield ToolCallEvent(call)
        yield ToolResultEvent(call, await execute_tool(call))
        yield TextDelta("I can't send email.")
        yield Done("stop")

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_mod, "run_agent", fake)
    resp = await client.post(
        "/api/v1/helper/turns", headers=_hdrs(key), json={"message": "email bob", "stream": False}
    )
    body = resp.json()
    assert body["stop_reason"] == "missing_capability"
    assert "I don't have a tool for what was asked (send_email)" in body["stop_message"]


def test_stop_note_hides_odd_tool_names():
    assert helper_mod._safe_tool_name("<script>") == "an unnamed tool"
    assert helper_mod._safe_tool_name("search_sessions") == "search_sessions"


# ---------------------------------------------------------------------------
# "Try again" keeps the conversation's history
# ---------------------------------------------------------------------------


async def _seed_capped_turn(db: AsyncSession, user_id: str, *, tool_rows: int = 64) -> str:
    """A thread whose only turn ran ``tool_rows`` tool calls and then gave a
    stop message, as the customer's 0.32.0 turn did."""
    conversation = HelperConversation(
        id=f"hc_{uuid.uuid4().hex[:24]}", user_id=user_id, title="Capped"
    )
    db.add(conversation)
    await db.commit()
    base = datetime.now(timezone.utc) - timedelta(minutes=5)
    db.add(
        HelperMessage(
            id=helper_mod._new_message_id(),
            conversation_id=conversation.id,
            role="user",
            content="Which sessions touched the billing webhook?",
            created_at=base,
        )
    )
    for i in range(tool_rows):
        db.add(
            HelperMessage(
                id=helper_mod._new_message_id(),
                conversation_id=conversation.id,
                role="tool",
                content=f"result {i}",
                tool_name="search_sessions",
                tool_call_id=f"call_{i}",
                tool_args=json.dumps({"query": f"q{i}"}),
                created_at=base + timedelta(seconds=1 + i),
            )
        )
    db.add(
        HelperMessage(
            id=helper_mod._new_message_id(),
            conversation_id=conversation.id,
            role="assistant",
            content=helper_mod._UNFINISHED_ANSWER,
            stop_reason="max_steps",
            created_at=base + timedelta(seconds=tool_rows + 2),
        )
    )
    await db.commit()
    return conversation.id


@pytest.mark.asyncio
async def test_try_again_after_a_long_turn_keeps_the_question(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """The reported bug: after a 64-call turn, "try again" reached the model
    with no earlier messages, so it answered that there were none."""
    user, key = await _make_user(db_session)
    conversation_id = await _seed_capped_turn(db_session, user.id)
    seen: list[list[dict]] = []
    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_mod, "run_agent", _fake_run_agent_recording(seen))
    await _stream_turn(client, key, {"message": "try again", "conversation_id": conversation_id})
    history = seen[0]
    assert history[0]["role"] == "user"
    assert history[0]["content"] == "Which sessions touched the billing webhook?"
    assert history[-1] == {"role": "user", "content": "try again"}
    # The earlier answer is there too; the 64 tool exchanges are not.
    assert any(m["role"] == "assistant" for m in history)
    assert not any(m["role"] == "tool" for m in history)


@pytest.mark.asyncio
async def test_retry_button_after_a_capped_turn_re_asks_with_history(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    conversation_id = await _seed_capped_turn(db_session, user.id)
    seen: list[list[dict]] = []
    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_mod, "run_agent", _fake_run_agent_recording(seen))
    question = "Which sessions touched the billing webhook?"
    await _stream_turn(
        client, key, {"message": question, "conversation_id": conversation_id, "retry": True}
    )
    history = seen[0]
    assert history[0] == {"role": "user", "content": question}
    assert history[-1] == {"role": "user", "content": question}
    assert len(history) == 3


def test_trim_history_keeps_recent_turns_whole_when_they_fit():
    call = ToolCall(id="c1", name="search_sessions", arguments={})
    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old answer"},
        {"role": "user", "content": "new"},
        {"role": "assistant", "content": "", "tool_calls": [call]},
        {"role": "tool", "tool_call_id": "c1", "content": "row"},
        {"role": "assistant", "content": "new answer"},
    ]
    assert helper_mod._trim_history(messages) == messages


def test_trim_history_compacts_an_oversized_turn():
    calls = [ToolCall(id=f"c{i}", name="search_sessions", arguments={}) for i in range(30)]
    messages: list[dict] = [{"role": "user", "content": "big question"}]
    messages.append({"role": "assistant", "content": "", "tool_calls": calls})
    messages.extend({"role": "tool", "tool_call_id": c.id, "content": "row"} for c in calls)
    messages.append({"role": "assistant", "content": "big answer"})
    trimmed = helper_mod._trim_history(messages)
    assert trimmed == [
        {"role": "user", "content": "big question"},
        {"role": "assistant", "content": "big answer"},
    ]
