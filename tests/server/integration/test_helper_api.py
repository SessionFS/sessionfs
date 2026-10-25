"""Session helper API (piece 4): turns, conversations, retention sweep.

The model call is faked at the ``run_agent`` boundary — no provider is ever
contacted.  Streaming is exercised end-to-end through the ASGI client and at
the generator level (where a client disconnect is observable), JSON through the
ASGI client, and the retention sweep against the admin endpoint.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.judge.agent_stream import (
    Done,
    ProviderError,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolResult,
    ToolResultEvent,
    Usage,
)
from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    HelperConversation,
    HelperMessage,
    OrgMember,
    Organization,
    Project,
    RetrievalAuditContext,
    RetrievalAuditEvent,
    Session,
    User,
)
from sessionfs.server.helper.keys import ResolvedLLM

import sessionfs.server.routes.helper as helper_mod

FAKE_KEY = "sk-test-key-1234"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


async def _make_user(
    db: AsyncSession, name: str = "alice", *, tier: str = "enterprise"
) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier=tier,
        email_verified=True,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"{name}-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _make_org_with(
    db: AsyncSession,
    admin: User,
    member: User | None = None,
    *,
    tier: str = "enterprise",
    settings: dict | None = None,
) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Test Org",
        slug=f"test-{uuid.uuid4().hex[:6]}",
        tier=tier,
        seats_limit=10,
        settings=json.dumps(settings) if settings is not None else "{}",
    )
    db.add(org)
    await db.commit()
    db.add(OrgMember(org_id=org.id, user_id=admin.id, role="admin"))
    if member is not None:
        db.add(OrgMember(org_id=org.id, user_id=member.id, role="member"))
    await db.commit()
    await db.refresh(org)
    return org


async def _make_session(
    db: AsyncSession, user: User, title: str = "Test Session", *, project_id: str | None = None
) -> Session:
    now = datetime.now(timezone.utc)
    session = Session(
        id=f"ses_{uuid.uuid4().hex[:16]}",
        user_id=user.id,
        title=title,
        source_tool="claude-code",
        blob_key=f"sessions/{user.id}/x.tar.gz",
        etag=uuid.uuid4().hex,
        created_at=now,
        updated_at=now,
        uploaded_at=now,
        project_id=project_id,
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def _make_project(
    db: AsyncSession, user: User, org_id: str | None = None, name: str = "Repo"
) -> Project:
    project = Project(
        id=f"prj_{uuid.uuid4().hex[:12]}",
        name=name,
        owner_id=user.id,
        org_id=org_id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_conversation(
    db: AsyncSession, user: User, org_id: str | None, updated_at: datetime
) -> HelperConversation:
    conv = HelperConversation(
        id=f"hc_{uuid.uuid4().hex[:24]}",
        user_id=user.id,
        org_id=org_id,
        title="Old thread",
        created_at=updated_at,
        updated_at=updated_at,
    )
    db.add(conv)
    await db.flush()
    db.add(
        HelperMessage(
            id=f"hm_{uuid.uuid4().hex[:24]}",
            conversation_id=conv.id,
            role="user",
            content="hi",
            created_at=updated_at,
        )
    )
    await db.commit()
    return conv


def _fake_resolve(key_source: str = "user"):
    async def _resolve(user: User, org_id: str, db: AsyncSession) -> ResolvedLLM | None:
        return ResolvedLLM(
            provider="openai",
            model="gpt-4o",
            api_key=FAKE_KEY,
            base_url=None,
            key_source=key_source,  # type: ignore[arg-type]
        )

    return _resolve


def _fake_no_key():
    async def _resolve(user: User, org_id: str, db: AsyncSession) -> ResolvedLLM | None:
        return None

    return _resolve


def _fake_run_agent_text_only(session_id: str | None = None):
    async def _fake(*, execute_tool, **kwargs):
        yield Usage(input_tokens=7, output_tokens=2)
        text = "Nothing found." if not session_id else f"See [session:{session_id}]."
        yield TextDelta(text)
        yield Done("stop")

    return _fake


def _fake_run_agent_tool_then_text(session_id: str, title: str):
    async def _fake(*, execute_tool, **kwargs):
        call = ToolCall(id="call_1", name="list_recent_sessions", arguments={})
        yield ToolCallEvent(call)
        result = await execute_tool(call)
        yield ToolResultEvent(call, result)
        yield Usage(input_tokens=10, output_tokens=3)
        yield TextDelta(f"Found it in [session:{session_id} msg:0] ({title}).")
        yield Done("stop")

    return _fake


def _fake_run_agent_hang():
    async def _fake(*, execute_tool, **kwargs):
        yield TextDelta("partial answer ")
        await asyncio.Event().wait()
        yield TextDelta("never")

    return _fake


def _fake_run_agent_tool_then_hang():
    async def _fake(*, execute_tool, **kwargs):
        call = ToolCall(id="call_1", name="list_recent_sessions", arguments={})
        yield ToolCallEvent(call)
        result = await execute_tool(call)
        yield ToolResultEvent(call, result)
        yield TextDelta("partial answer ")
        await asyncio.Event().wait()
        yield TextDelta("never")

    return _fake


def _fake_run_agent_provider_error():
    async def _fake(*, execute_tool, **kwargs):
        raise ProviderError(429, "upstream rate limited")
        yield  # pragma: no cover - makes this an async generator

    return _fake


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        event_type: str | None = None
        data: dict = {}
        for line in block.split("\n"):
            if line.startswith("event:"):
                event_type = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        if event_type is not None:
            events.append((event_type, data))
    return events


def _request(blob_store=None):
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(blob_store=blob_store)))


# ---------------------------------------------------------------------------
# POST /turns — streaming (SSE)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_tool_then_text_with_citation(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    session = await _make_session(db_session, user, title="Refactor notes")

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(
        helper_mod, "run_agent", _fake_run_agent_tool_then_text(session.id, session.title)
    )

    body = ""
    async with client.stream(
        "POST",
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "What did I do?", "stream": True},
    ) as resp:
        assert resp.status_code == 200, await resp.aread()
        assert "text/event-stream" in resp.headers["content-type"]
        async for chunk in resp.aiter_text():
            body += chunk

    events = _parse_sse(body)
    kinds = [kind for kind, _ in events]

    conversation_event = next(e for e in events if e[0] == "conversation")
    conversation_id = conversation_event[1]["conversation_id"]

    assert "tool_call" in kinds
    tool_call = next(e[1] for e in events if e[0] == "tool_call")
    assert tool_call["name"] == "list_recent_sessions"

    assert "tool_result" in kinds
    tool_result = next(e[1] for e in events if e[0] == "tool_result")
    assert tool_result["ok"] is True

    texts = [e[1]["delta"] for e in events if e[0] == "text"]
    assert "".join(texts) == f"Found it in [session:{session.id} msg:0] (Refactor notes)."

    citations = [e[1] for e in events if e[0] == "citation"]
    assert citations == [
        {"session_id": session.id, "message_index": 0, "title": "Refactor notes"}
    ]

    done = next(e[1] for e in events if e[0] == "done")
    assert done["stop_reason"] == "stop"
    assert done["key_source"] == "user"

    # The assistant answer + tool row are persisted.
    rows = (
        await db_session.execute(
            select(HelperMessage)
            .where(HelperMessage.conversation_id == conversation_id)
            .order_by(HelperMessage.created_at.asc(), HelperMessage.id.asc())
        )
    ).scalars().all()
    roles = [r.role for r in rows]
    assert roles == ["user", "tool", "assistant"]
    assert rows[-1].content.startswith("Found it in [session:")
    assert json.loads(rows[-1].citations) == [
        {"session_id": session.id, "message_index": 0, "title": "Refactor notes"}
    ]


@pytest.mark.asyncio
async def test_stream_drops_unreadable_citations(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    other, _ = await _make_user(db_session, "bob")
    other_session = await _make_session(db_session, other, title="Private")

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(
        helper_mod,
        "run_agent",
        _fake_run_agent_text_only(),  # placeholder; replaced below
    )

    async def _fake(*, execute_tool, **kwargs):
        yield Usage(input_tokens=1, output_tokens=1)
        yield TextDelta(f"Secret: [session:{other_session.id}] and bogus [session:ses_missing].")
        yield Done("stop")

    monkeypatch.setattr(helper_mod, "run_agent", _fake)

    body = ""
    async with client.stream(
        "POST",
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "hi", "stream": True},
    ) as resp:
        assert resp.status_code == 200, await resp.aread()
        async for chunk in resp.aiter_text():
            body += chunk

    events = _parse_sse(body)
    citations = [e[1] for e in events if e[0] == "citation"]
    assert citations == []  # the other user's session and the missing id are dropped


@pytest.mark.asyncio
async def test_stream_provider_error_event_never_leaks_key(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_mod, "run_agent", _fake_run_agent_provider_error())

    body = ""
    async with client.stream(
        "POST",
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "hi", "stream": True},
    ) as resp:
        assert resp.status_code == 200, await resp.aread()
        async for chunk in resp.aiter_text():
            body += chunk

    assert FAKE_KEY not in body
    events = _parse_sse(body)
    assert ("error", {"code": "provider_error", "message": "upstream rate limited"}) in events


# ---------------------------------------------------------------------------
# POST /turns — JSON (stream=false)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_json_turn_returns_document(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    session = await _make_session(db_session, user, title="Notes")

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_mod, "run_agent", _fake_run_agent_text_only(session.id))

    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "hello", "stream": False},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["conversation_id"].startswith("hc_")
    assert body["answer"] == f"See [session:{session.id}]."
    assert body["citations"] == [
        {"session_id": session.id, "message_index": None, "title": "Notes"}
    ]
    assert body["tool_calls"] == []
    assert body["usage"] == {"input_tokens": 7, "output_tokens": 2}
    assert body["stop_reason"] == "stop"
    assert body["key_source"] == "user"


@pytest.mark.asyncio
async def test_json_provider_error_is_502(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_mod, "run_agent", _fake_run_agent_provider_error())

    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "hello", "stream": False},
    )
    assert resp.status_code == 502, resp.text
    assert FAKE_KEY not in resp.text
    assert resp.json()["error"]["code"] == "provider_error"
    assert resp.json()["error"]["message"] == "upstream rate limited"


# ---------------------------------------------------------------------------
# Auth / gating / validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_key_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    user, _ = await _make_user(db_session)
    svc_raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(svc_raw),
            name="ci-runner",
            key_kind="service",
            scopes='["agent_runs:write"]',
            service_key_name="ci-runner",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(svc_raw),
        json={"message": "hi", "stream": False},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "service_key_not_allowed"


@pytest.mark.asyncio
async def test_tier_gate_returns_upgrade_envelope(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session, tier="team")
    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())

    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "hi", "stream": False},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "upgrade_required"
    details = resp.json()["error"]["details"]
    assert details["error"] == "upgrade_required"
    assert details["feature"] == "session_helper"
    assert details["plan_scope"] == "user"
    assert details["required_tier"] == "enterprise"


@pytest.mark.asyncio
async def test_org_scope_tier_gate_uses_org_plan(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    # The user's own plan is enterprise, but the project belongs to a team
    # org — the org's plan must win.
    admin, key = await _make_user(db_session, "admin", tier="enterprise")
    org = await _make_org_with(db_session, admin, tier="team")
    project = Project(
        id=f"prj_{uuid.uuid4().hex[:12]}",
        name="Repo",
        owner_id=admin.id,
        org_id=org.id,
    )
    db_session.add(project)
    await db_session.commit()

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())

    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={
            "message": "hi",
            "stream": False,
            "scope": {"project_id": project.id},
        },
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "upgrade_required"
    details = resp.json()["error"]["details"]
    assert details["plan_scope"] == "org"
    assert details["current_tier"] == "team"
    assert details["required_tier"] == "enterprise"


@pytest.mark.asyncio
async def test_no_key_returns_helper_key_required(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_no_key())

    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "hi", "stream": False},
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "helper_key_required"


@pytest.mark.asyncio
async def test_other_users_session_is_404(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    other, _ = await _make_user(db_session, "bob")
    other_session = await _make_session(db_session, other)

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())

    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={
            "message": "hi",
            "stream": False,
            "scope": {"session_ids": [other_session.id]},
        },
    )
    assert resp.status_code == 404, resp.text


@pytest.mark.asyncio
async def test_session_scope_mixed_orgs_is_400(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """Session-only scope spanning two orgs' projects is refused (scope_mixed_orgs)."""
    user, key = await _make_user(db_session)
    org_a = await _make_org_with(db_session, user)
    org_b = await _make_org_with(db_session, user)
    project_a = await _make_project(db_session, user, org_a.id)
    project_b = await _make_project(db_session, user, org_b.id)
    session_a = await _make_session(db_session, user, project_id=project_a.id)
    session_b = await _make_session(db_session, user, project_id=project_b.id)

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())

    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={
            "message": "hi",
            "stream": False,
            "scope": {"session_ids": [session_a.id, session_b.id]},
        },
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "scope_mixed_orgs"


@pytest.mark.asyncio
async def test_busy_limit_returns_409(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())

    helper_mod._in_flight[user.id] = 2
    try:
        resp = await client.post(
            "/api/v1/helper/turns",
            headers=_hdrs(key),
            json={"message": "hi", "stream": False},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["error"]["code"] == "helper_busy"
        # The busy limit is enforced before the conversation is created, so a
        # rejected turn leaves no empty thread behind (N5b).
        assert (await db_session.execute(select(HelperConversation))).scalars().all() == []
    finally:
        helper_mod._in_flight.pop(user.id, None)


# ---------------------------------------------------------------------------
# Conversations CRUD
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_get_and_delete_conversation(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_mod, "run_agent", _fake_run_agent_text_only())

    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "first question", "stream": False},
    )
    conversation_id = resp.json()["conversation_id"]

    listed = await client.get("/api/v1/helper/conversations", headers=_hdrs(key))
    assert listed.status_code == 200, listed.text
    assert [c["id"] for c in listed.json()] == [conversation_id]

    got = await client.get(
        f"/api/v1/helper/conversations/{conversation_id}", headers=_hdrs(key)
    )
    assert got.status_code == 200, got.text
    assert got.json()["id"] == conversation_id
    assert [m["role"] for m in got.json()["messages"]] == ["user", "assistant"]

    deleted = await client.delete(
        f"/api/v1/helper/conversations/{conversation_id}", headers=_hdrs(key)
    )
    assert deleted.status_code == 200, deleted.text
    assert deleted.json() == {"deleted": True}

    gone = await client.get(
        f"/api/v1/helper/conversations/{conversation_id}", headers=_hdrs(key)
    )
    assert gone.status_code == 404, gone.text


@pytest.mark.asyncio
async def test_conversation_owner_only_404(
    client: AsyncClient, db_session: AsyncSession
):
    alice, alice_key = await _make_user(db_session, "alice")
    bob, bob_key = await _make_user(db_session, "bob")

    now = datetime.now(timezone.utc)
    conv = await _make_conversation(db_session, alice, None, now)

    # Bob cannot read Alice's thread.
    resp = await client.get(
        f"/api/v1/helper/conversations/{conv.id}", headers=_hdrs(bob_key)
    )
    assert resp.status_code == 404, resp.text

    # Alice can.
    ok = await client.get(
        f"/api/v1/helper/conversations/{conv.id}", headers=_hdrs(alice_key)
    )
    assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_reports_enabled_and_key_source(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())

    resp = await client.get("/api/v1/helper/status", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is True
    assert body["key_source"] == "user"
    assert body["provider"] == "openai"
    assert body["model"] == "gpt-4o"


# ---------------------------------------------------------------------------
# Retrieval audit (source="helper")
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_turn_writes_retrieval_audit_source_helper(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    user, key = await _make_user(db_session)
    session = await _make_session(db_session, user)

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(
        helper_mod, "run_agent", _fake_run_agent_tool_then_text(session.id, session.title)
    )

    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "hello", "stream": False},
    )
    assert resp.status_code == 200, resp.text

    contexts = (
        await db_session.execute(
            select(RetrievalAuditContext).where(
                RetrievalAuditContext.created_by_user_id == user.id
            )
        )
    ).scalars().all()
    assert len(contexts) == 1

    events = (
        await db_session.execute(
            select(RetrievalAuditEvent).where(
                RetrievalAuditEvent.context_id == contexts[0].id
            )
        )
    ).scalars().all()
    assert len(events) == 1
    assert events[0].source == "helper"
    assert events[0].tool_name == "list_recent_sessions"
    assert events[0].caller_user_id == user.id


# ---------------------------------------------------------------------------
# Disconnect (generator level) — partial answer persisted, model cancelled
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disconnect_persists_partial_answer(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    from sqlalchemy.ext.asyncio import async_sessionmaker

    user, _ = await _make_user(db_session)
    now = datetime.now(timezone.utc)
    conv = HelperConversation(
        id=f"hc_{uuid.uuid4().hex[:24]}",
        user_id=user.id,
        org_id=None,
        title="partial",
        created_at=now,
        updated_at=now,
    )
    db_session.add(conv)
    await db_session.commit()

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_mod, "run_agent", _fake_run_agent_hang())

    # Fresh sessions on the same engine as db_session (the turn persists outside
    # the request scope via the factory, not the test's session).
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    resolved = await helper_mod.resolve_helper_llm(user, "", db_session)
    slot = helper_mod._acquire_slot(user.id)
    events = helper_mod._turn_events(
        _request(),
        user,
        factory,
        scope=helper_mod.HelperScope(),
        org_id=None,
        dlp_policy=None,
        resolved=resolved,
        conversation=conv,
        message="hello",
        slot=slot,
    )
    await events.__anext__()  # conversation
    await events.__anext__()  # first text delta
    await events.aclose()  # simulate disconnect -> finally persists partial

    assert helper_mod._in_flight.get(user.id, 0) == 0  # slot released

    rows = (
        await db_session.execute(
            select(HelperMessage).where(HelperMessage.conversation_id == conv.id)
        )
    ).scalars().all()
    roles = [r.role for r in rows]
    assert roles == ["user", "assistant"]
    assert rows[-1].content == "partial answer "


@pytest.mark.asyncio
async def test_shielded_persistence_timeout_releases_slot(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    """A persistence commit that hangs is bounded: the turn finishes (dropping the
    write) and the in-flight slot is released regardless (N3)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    user, _ = await _make_user(db_session)
    now = datetime.now(timezone.utc)
    conv = HelperConversation(
        id=f"hc_{uuid.uuid4().hex[:24]}",
        user_id=user.id,
        org_id=None,
        title="bounded",
        created_at=now,
        updated_at=now,
    )
    db_session.add(conv)
    await db_session.commit()

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_mod, "run_agent", _fake_run_agent_text_only())
    monkeypatch.setattr(helper_mod, "_SHIELDED_PERSIST_SECONDS", 0.1)

    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)

    # The audit-context commit (first) succeeds; the persistence commit (second)
    # hangs forever, so the shielded fail_after must cut it off.
    real_commit = AsyncSession.commit
    calls = 0

    async def flaky_commit(self):
        nonlocal calls
        calls += 1
        if calls >= 2:
            await asyncio.Event().wait()
        await real_commit(self)

    monkeypatch.setattr(AsyncSession, "commit", flaky_commit)

    resolved = await helper_mod.resolve_helper_llm(user, "", db_session)
    slot = helper_mod._acquire_slot(user.id)
    events = helper_mod._turn_events(
        _request(),
        user,
        factory,
        scope=helper_mod.HelperScope(),
        org_id=None,
        dlp_policy=None,
        resolved=resolved,
        conversation=conv,
        message="hello",
        slot=slot,
    )
    # Drive the generator to completion; the hung persistence commit must not
    # block past the fail_after deadline.
    with caplog.at_level(logging.WARNING, logger="sessionfs.helper"):
        async for _ in events:
            pass

    assert helper_mod._in_flight.get(user.id, 0) == 0  # slot released
    # The dropped write is logged with both the conversation and the
    # retrieval-audit context id, so an org can trace the abandoned turn (R3-3).
    assert any(
        "retrieval-audit context" in r.message and "ra_" in r.message
        for r in caplog.records
    )


# ---------------------------------------------------------------------------
# Retention sweep
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_helper_retention_sweep_hard_deletes_expired(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin", tier="admin")
    org = await _make_org_with(
        db_session, admin, settings={"general": {"helper_retention_days": 1}}
    )
    now = datetime.now(timezone.utc)

    expired_org = await _make_conversation(
        db_session, admin, org.id, now - timedelta(days=2)
    )
    kept_org = await _make_conversation(db_session, admin, org.id, now)
    expired_personal = await _make_conversation(
        db_session, admin, None, now - timedelta(days=200)
    )
    kept_personal = await _make_conversation(db_session, admin, None, now)

    resp = await client.post(
        "/api/v1/admin/sweeps/helper-retention", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["purged"] == 2
    assert body["messages_deleted"] == 2

    remaining = (
        await db_session.execute(select(HelperConversation.id))
    ).scalars().all()
    assert set(remaining) == {kept_org.id, kept_personal.id}
    assert expired_org.id not in remaining
    assert expired_personal.id not in remaining

    # Messages of the purged conversations are gone too.
    message_ids = (await db_session.execute(select(HelperMessage.id))).scalars().all()
    assert len(message_ids) == 2  # one per kept conversation


# ---------------------------------------------------------------------------
# H1 — org DLP union + audit attribution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unscoped_turn_redacts_org_dlp_phi_in_tool_output(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """An unscoped turn still applies the union of the caller's org DLP policies.

    The org's policy adds ``phi`` (which redacts SSNs); an SSN in a session
    title must be redacted from the tool output before it reaches the model.
    """
    user, key = await _make_user(db_session)
    await _make_org_with(
        db_session,
        user,
        settings={"dlp": {"enabled": True, "mode": "redact", "categories": ["phi"]}},
    )
    session = await _make_session(db_session, user, title="SSN 123-45-6789 refactor")

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(
        helper_mod, "run_agent", _fake_run_agent_tool_then_text(session.id, session.title)
    )

    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "what did I do", "stream": False},
    )
    assert resp.status_code == 200, resp.text
    conversation_id = resp.json()["conversation_id"]

    tool_rows = (
        await db_session.execute(
            select(HelperMessage).where(
                HelperMessage.conversation_id == conversation_id,
                HelperMessage.role == "tool",
            )
        )
    ).scalars().all()
    assert len(tool_rows) == 1
    assert "123-45-6789" not in tool_rows[0].content
    assert "[REDACTED:ssn]" in tool_rows[0].content


@pytest.mark.asyncio
async def test_conversation_title_is_redacted(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """The stored title is built from the REDACTED message, so a secret in the
    first 80 chars never leaks into the conversation title (N1)."""
    user, key = await _make_user(db_session)
    await _make_org_with(
        db_session,
        user,
        settings={"dlp": {"enabled": True, "mode": "redact", "categories": ["phi"]}},
    )
    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_mod, "run_agent", _fake_run_agent_text_only())

    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "my ssn 123-45-6789", "stream": False},
    )
    assert resp.status_code == 200, resp.text
    conversation_id = resp.json()["conversation_id"]

    conv = (
        await db_session.execute(
            select(HelperConversation).where(HelperConversation.id == conversation_id)
        )
    ).scalar_one()
    assert "123-45-6789" not in conv.title
    assert "[REDACTED:ssn]" in conv.title


@pytest.mark.asyncio
async def test_audit_event_carries_item_project_id(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """Each retrieval audit event carries the referenced item's project id."""
    user, key = await _make_user(db_session)
    org = await _make_org_with(db_session, user)
    project = await _make_project(db_session, user, org.id)
    session = await _make_session(db_session, user, project_id=project.id)

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(
        helper_mod, "run_agent", _fake_run_agent_tool_then_text(session.id, session.title)
    )

    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "hi", "stream": False},
    )
    assert resp.status_code == 200, resp.text

    events = (await db_session.execute(select(RetrievalAuditEvent))).scalars().all()
    assert len(events) == 1
    assert events[0].project_id == project.id


@pytest.mark.asyncio
async def test_audit_arguments_record_key_source_and_conversation(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """The audit event's arguments JSON records key_source + conversation id (L7)."""
    user, key = await _make_user(db_session)
    session = await _make_session(db_session, user)

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(
        helper_mod, "run_agent", _fake_run_agent_tool_then_text(session.id, session.title)
    )

    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "hello", "stream": False},
    )
    assert resp.status_code == 200, resp.text
    conversation_id = resp.json()["conversation_id"]

    events = (await db_session.execute(select(RetrievalAuditEvent))).scalars().all()
    assert len(events) == 1
    args = json.loads(events[0].arguments)
    assert args["key_source"] == "user"
    assert args["conversation_id"] == conversation_id


# ---------------------------------------------------------------------------
# M1 — conversation scope/membership
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_conversation_reuse_across_orgs_is_409(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """Reusing a thread under a different scope org is refused (scope_mismatch)."""
    user, key = await _make_user(db_session)
    org_a = await _make_org_with(db_session, user)
    org_b = await _make_org_with(db_session, user)
    project_b = await _make_project(db_session, user, org_b.id)
    conv = await _make_conversation(
        db_session, user, org_a.id, datetime.now(timezone.utc)
    )

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())

    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={
            "message": "reuse",
            "stream": False,
            "conversation_id": conv.id,
            "scope": {"project_id": project_b.id},
        },
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "scope_mismatch"


@pytest.mark.asyncio
async def test_resume_scoped_conversation_without_scope_recovers_org(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """Resuming an org-bound thread without re-sending its scope no longer 409s.

    The server stores the thread's last scope and owning org, so a
    ``conversation_id``-only continuation recovers them instead of being read as
    personal (F5).
    """
    user, key = await _make_user(db_session)
    org = await _make_org_with(db_session, user)
    project = await _make_project(db_session, user, org.id)

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_mod, "run_agent", _fake_run_agent_text_only())

    # First turn scoped to the project creates the org-bound thread.
    first = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "q1", "stream": False, "scope": {"project_id": project.id}},
    )
    assert first.status_code == 200, first.text
    conversation_id = first.json()["conversation_id"]

    # Resume with the conversation id only — no scope. Previously 409.
    resume = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "q2", "stream": False, "conversation_id": conversation_id},
    )
    assert resume.status_code == 200, resume.text
    assert resume.json()["conversation_id"] == conversation_id

    # The stored scope is returned so the dashboard can restore it.
    got = await client.get(
        f"/api/v1/helper/conversations/{conversation_id}", headers=_hdrs(key)
    )
    assert got.status_code == 200, got.text
    assert got.json()["scope"] == {"project_id": project.id, "session_ids": []}


@pytest.mark.asyncio
async def test_scopeless_resume_revalidates_stored_scope_after_project_moves(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """A scope-less resume re-derives the org from the stored scope, so a project
    that moved orgs can't keep running under the thread's old org binding."""
    user, key = await _make_user(db_session)
    org_a = await _make_org_with(db_session, user)
    org_b = await _make_org_with(db_session, user)
    project = await _make_project(db_session, user, org_a.id)

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_mod, "run_agent", _fake_run_agent_text_only())

    first = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "q1", "stream": False, "scope": {"project_id": project.id}},
    )
    assert first.status_code == 200, first.text
    conversation_id = first.json()["conversation_id"]

    project.org_id = org_b.id
    await db_session.commit()

    resume = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "q2", "stream": False, "conversation_id": conversation_id},
    )
    assert resume.status_code == 409, resume.text


@pytest.mark.asyncio
async def test_org_conversation_non_member_read_is_404(
    client: AsyncClient, db_session: AsyncSession
):
    """An org-bound thread is hidden from its owner once membership is revoked."""
    owner, _ = await _make_user(db_session, "owner")
    org = await _make_org_with(db_session, owner)
    former, former_key = await _make_user(db_session, "former")
    conv = await _make_conversation(
        db_session, former, org.id, datetime.now(timezone.utc)
    )

    # Not an active member: the thread is hidden (404), not leaked.
    resp = await client.get(
        f"/api/v1/helper/conversations/{conv.id}", headers=_hdrs(former_key)
    )
    assert resp.status_code == 404, resp.text

    # Re-add membership: the same thread is readable again.
    db_session.add(OrgMember(org_id=org.id, user_id=former.id, role="member"))
    await db_session.commit()
    ok = await client.get(
        f"/api/v1/helper/conversations/{conv.id}", headers=_hdrs(former_key)
    )
    assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------------------
# M2 — per-message retention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retention_deletes_only_expired_messages_keeps_recent(
    client: AsyncClient, db_session: AsyncSession
):
    """Per-message retention: the old message expires, the recent one keeps the
    conversation alive."""
    admin, key = await _make_user(db_session, "admin", tier="admin")
    org = await _make_org_with(
        db_session, admin, settings={"general": {"helper_retention_days": 1}}
    )
    now = datetime.now(timezone.utc)
    conv = HelperConversation(
        id=f"hc_{uuid.uuid4().hex[:24]}",
        user_id=admin.id,
        org_id=org.id,
        title="mixed",
        created_at=now - timedelta(days=3),
        updated_at=now,
    )
    db_session.add(conv)
    await db_session.flush()
    old_msg = HelperMessage(
        id=f"hm_{uuid.uuid4().hex[:24]}",
        conversation_id=conv.id,
        role="user",
        content="old",
        created_at=now - timedelta(days=3),
    )
    recent_msg = HelperMessage(
        id=f"hm_{uuid.uuid4().hex[:24]}",
        conversation_id=conv.id,
        role="assistant",
        content="recent",
        created_at=now,
    )
    db_session.add_all([old_msg, recent_msg])
    await db_session.commit()

    resp = await client.post(
        "/api/v1/admin/sweeps/helper-retention", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text

    conv_ids = set((await db_session.execute(select(HelperConversation.id))).scalars().all())
    assert conv.id in conv_ids  # kept: still has a recent message

    msg_ids = set(
        (await db_session.execute(
            select(HelperMessage.id).where(HelperMessage.conversation_id == conv.id)
        )).scalars().all()
    )
    assert recent_msg.id in msg_ids
    assert old_msg.id not in msg_ids


@pytest.mark.asyncio
async def test_empty_conversation_purge_respects_one_hour_grace(
    client: AsyncClient, db_session: AsyncSession
):
    """Only conversations left empty AND untouched for over an hour are purged
    (N5c) — a just-created/just-written thread survives the sweep."""
    admin, key = await _make_user(db_session, "admin", tier="admin")
    await _make_org_with(db_session, admin)
    now = datetime.now(timezone.utc)

    recent_empty = HelperConversation(
        id=f"hc_{uuid.uuid4().hex[:24]}",
        user_id=admin.id,
        org_id=None,
        title="recent",
        created_at=now - timedelta(minutes=30),
        updated_at=now - timedelta(minutes=30),
    )
    stale_empty = HelperConversation(
        id=f"hc_{uuid.uuid4().hex[:24]}",
        user_id=admin.id,
        org_id=None,
        title="stale",
        created_at=now - timedelta(hours=2),
        updated_at=now - timedelta(hours=2),
    )
    db_session.add_all([recent_empty, stale_empty])
    await db_session.commit()

    resp = await client.post(
        "/api/v1/admin/sweeps/helper-retention", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["purged"] == 1

    remaining = (
        await db_session.execute(select(HelperConversation.id))
    ).scalars().all()
    assert recent_empty.id in remaining
    assert stale_empty.id not in remaining


@pytest.mark.asyncio
async def test_sweep_batches_deletes_with_small_batch_size(
    client: AsyncClient, db_session: AsyncSession
):
    """The sweep drains a backlog in bounded batches: a small ``batch_size`` still
    deletes every expired message without loading them all at once (N5d)."""
    admin, key = await _make_user(db_session, "admin", tier="admin")
    await _make_org_with(db_session, admin)
    now = datetime.now(timezone.utc)

    conv = HelperConversation(
        id=f"hc_{uuid.uuid4().hex[:24]}",
        user_id=admin.id,
        org_id=None,
        title="batch",
        created_at=now - timedelta(days=200),
        updated_at=now,
    )
    db_session.add(conv)
    await db_session.flush()

    expired = [
        HelperMessage(
            id=f"hm_{uuid.uuid4().hex[:24]}",
            conversation_id=conv.id,
            role="user",
            content="old",
            created_at=now - timedelta(days=200),
        )
        for _ in range(5)
    ]
    recent = HelperMessage(
        id=f"hm_{uuid.uuid4().hex[:24]}",
        conversation_id=conv.id,
        role="assistant",
        content="recent",
        created_at=now,
    )
    db_session.add_all([*expired, recent])
    await db_session.commit()

    resp = await client.post(
        "/api/v1/admin/sweeps/helper-retention",
        headers=_hdrs(key),
        params={"batch_size": 2},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["messages_deleted"] == 5

    # All five expired messages gone despite a batch of 2; the recent one (and
    # its conversation) survives.
    remaining = (
        await db_session.execute(
            select(HelperMessage.id).where(HelperMessage.conversation_id == conv.id)
        )
    ).scalars().all()
    assert remaining == [recent.id]
    assert (
        await db_session.execute(
            select(HelperConversation.id).where(HelperConversation.id == conv.id)
        )
    ).scalar_one() == conv.id


# ---------------------------------------------------------------------------
# L1/L2 — history trim + structured tool_result
# ---------------------------------------------------------------------------


def test_trim_history_drops_leading_tool_messages():
    """A leading tool message (orphaned from its assistant tool_calls) is
    dropped so history never starts with a tool result (L1)."""
    trimmed = helper_mod._trim_history(
        [
            {"role": "tool", "tool_call_id": "c1", "content": "orphan"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
    )
    assert [m["role"] for m in trimmed] == ["user", "assistant"]

    # A tool row that follows its assistant tool_calls is preserved.
    kept = helper_mod._trim_history(
        [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "", "tool_calls": [ToolCall(
                id="c1", name="list_recent_sessions", arguments={}
            )]},
            {"role": "tool", "tool_call_id": "c1", "content": "result"},
        ]
    )
    assert [m["role"] for m in kept] == ["user", "assistant", "tool"]

    # A leading assistant message is dropped too, so history never starts with
    # an assistant turn (N5e).
    trimmed_assistant = helper_mod._trim_history(
        [
            {"role": "assistant", "content": "orphaned answer"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
    )
    assert [m["role"] for m in trimmed_assistant] == ["user", "assistant"]


def test_tool_result_event_carries_only_structured_metadata():
    """tool_result events expose metadata only — never excerpt text (L2)."""
    call = ToolCall(id="call_1", name="list_recent_sessions", arguments={})
    result = ToolResult(
        tool_call_id="call_1", content="SSN 123-45-6789 excerpt", is_error=False
    )
    event = ToolResultEvent(call, result)
    refs = {
        "session_ids": ["ses_b", "ses_a"],
        "message_indexes": [2, 0, 0],
        "kb_entry_ids": [5],
        "project_ids": ["prj_x"],
    }
    payload = helper_mod._tool_result_event(event, refs)

    assert payload["type"] == "tool_result"
    assert payload["id"] == "call_1"
    assert payload["name"] == "list_recent_sessions"
    assert payload["ok"] is True
    assert payload["item_count"] == 4  # 2 sessions + 1 kb entry + 1 project
    assert payload["session_ids"] == ["ses_a", "ses_b"]
    assert payload["message_indexes"] == [0, 2]

    # No excerpt/summary text leaks into the event.
    for key in ("summary", "content", "text", "excerpt"):
        assert key not in payload
    assert "123-45-6789" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# L3/L4 — session_ids length + primary-org key resolution
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_ids_item_too_long_is_422(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)

    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "hi", "stream": False, "scope": {"session_ids": ["s" * 65]}},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_unscoped_turn_resolves_key_through_primary_org(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """An unscoped turn resolves the caller's LLM key through their primary org."""
    user, key = await _make_user(db_session)
    org = await _make_org_with(db_session, user)
    user.default_org_id = org.id
    await db_session.commit()

    captured: dict[str, str | None] = {}

    async def _capture(u: User, org_id: str, db: AsyncSession) -> ResolvedLLM:
        captured["org_id"] = org_id
        return ResolvedLLM(
            provider="openai",
            model="gpt-4o",
            api_key=FAKE_KEY,
            base_url=None,
            key_source="user",  # type: ignore[arg-type]
        )

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _capture)
    monkeypatch.setattr(helper_mod, "run_agent", _fake_run_agent_text_only())

    resp = await client.post(
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "hi", "stream": False},
    )
    assert resp.status_code == 200, resp.text
    assert captured["org_id"] == org.id


# ---------------------------------------------------------------------------
# H2 — cancellation-safe persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_control_chars_in_delta_are_sanitized_in_storage(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """A NUL in a model delta must not poison storage; the stream completes."""
    user, key = await _make_user(db_session)
    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())

    async def _fake(*, execute_tool, **kwargs):
        yield Usage(input_tokens=1, output_tokens=1)
        yield TextDelta("before\x00after")
        yield Done("stop")

    monkeypatch.setattr(helper_mod, "run_agent", _fake)

    body = ""
    async with client.stream(
        "POST",
        "/api/v1/helper/turns",
        headers=_hdrs(key),
        json={"message": "hi", "stream": True},
    ) as resp:
        assert resp.status_code == 200, await resp.aread()
        async for chunk in resp.aiter_text():
            body += chunk

    events = _parse_sse(body)
    assert any(kind == "done" for kind, _ in events)

    conversation_id = next(
        e[1]["conversation_id"] for e in events if e[0] == "conversation"
    )
    assistant = (
        await db_session.execute(
            select(HelperMessage).where(
                HelperMessage.conversation_id == conversation_id,
                HelperMessage.role == "assistant",
            )
        )
    ).scalars().one()
    assert "\x00" not in assistant.content
    assert assistant.content == "beforeafter"


@pytest.mark.asyncio
async def test_stream_body_never_iterated_still_releases_slot(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """The route acquires the slot before returning the StreamingResponse and
    attaches a BackgroundTask, so a stream body that is never iterated still
    releases the slot (N5a)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    user, _ = await _make_user(db_session)
    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_mod, "run_agent", _fake_run_agent_hang())
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(helper_mod, "get_session_factory", lambda: factory)

    request = SimpleNamespace(
        headers={},
        app=SimpleNamespace(state=SimpleNamespace(blob_store=None)),
    )
    body = helper_mod.HelperTurnRequest(message="hello", stream=True)

    response = await helper_mod.create_helper_turn(
        body=body, request=request, user=user, db=db_session
    )

    # Slot was acquired before the response returned, and the response carries
    # a background release for the never-iterated case.
    assert helper_mod._in_flight.get(user.id, 0) == 1
    assert response.background is not None
    try:
        await response.background()
        assert helper_mod._in_flight.get(user.id, 0) == 0
    finally:
        if hasattr(response.body_iterator, "aclose"):
            await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_raw_asgi_disconnect_persists_turn_audit_and_releases_slot(
    tmp_path, blob_store, monkeypatch: pytest.MonkeyPatch
):
    """A client disconnect mid-turn persists the tool row + audit event + partial
    answer under a shielded scope and releases the in-flight slot.

    File-backed SQLite: the route holds a request-scoped session open across the
    streaming response while ``_turn_events`` opens its own sessions, so the
    shared in-memory engine (single connection) cannot service the overlap.
    """
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig
    from sessionfs.server.db.engine import get_db, install_sqlite_unicode_lower
    from sessionfs.server.db.models import Base
    import sessionfs.server.db.engine as _engine_mod

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'disconnect.db'}")
    install_sqlite_unicode_lower(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with factory() as setup:
            user, key = await _make_user(setup)
            await _make_session(setup, user, title="Disco")

        monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
        monkeypatch.setattr(helper_mod, "run_agent", _fake_run_agent_tool_then_hang())

        config = ServerConfig(database_url="sqlite+aiosqlite://")
        app = create_app(config)

        async def override_get_db():
            async with factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        app.state.blob_store = blob_store
        app.state.config = config
        _engine_mod._session_factory = factory

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/api/v1/helper/turns",
            "raw_path": b"/api/v1/helper/turns",
            "query_string": b"",
            "headers": [
                (b"authorization", f"Bearer {key}".encode()),
                (b"content-type", b"application/json"),
                (b"accept", b"text/event-stream"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
        }

        body_sent = False
        disconnect_event = asyncio.Event()
        partial_seen = asyncio.Event()
        sent_bodies: list[bytes] = []

        async def receive():
            nonlocal body_sent
            if not body_sent:
                body_sent = True
                return {
                    "type": "http.request",
                    "body": json.dumps({"message": "hi", "stream": True}).encode(),
                    "more_body": False,
                }
            await disconnect_event.wait()
            return {"type": "http.disconnect"}

        async def send(message):
            if message["type"] == "http.response.body":
                sent_bodies.append(message.get("body", b""))
                # Disconnect only once the partial answer has been streamed, so
                # the tool call has completed and the disconnect lands in the
                # model's hang (not mid-tool), exactly like a real mid-stream
                # disconnect after a tool round-trip.
                if "event: text" in b"".join(sent_bodies).decode():
                    partial_seen.set()

        app_task = asyncio.create_task(app(scope, receive, send))
        try:
            await asyncio.wait_for(partial_seen.wait(), timeout=5)
            disconnect_event.set()
            await asyncio.wait_for(app_task, timeout=5)
        finally:
            _engine_mod._session_factory = None

        # Slot released.
        assert helper_mod._in_flight.get(user.id, 0) == 0

        # Assertions in a fresh short-lived session.
        async with factory() as check:
            rows = (
                await check.execute(
                    select(HelperMessage).order_by(
                        HelperMessage.created_at.asc(), HelperMessage.id.asc()
                    )
                )
            ).scalars().all()
            assert [r.role for r in rows] == ["user", "tool", "assistant"]
            assert rows[-1].stop_reason == "client_disconnected"
            assert rows[-1].content == "partial answer "

            contexts = (await check.execute(select(RetrievalAuditContext))).scalars().all()
            assert len(contexts) == 1
            events = (await check.execute(select(RetrievalAuditEvent))).scalars().all()
            assert len(events) == 1
            assert events[0].source == "helper"
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_audit_context_commits_before_model_and_survives_delete(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """The audit context + user message are committed before the model runs, so a
    concurrent DELETE of the conversation mid-turn cannot drop the audit trail."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    user, _ = await _make_user(db_session)
    now = datetime.now(timezone.utc)
    conv = HelperConversation(
        id=f"hc_{uuid.uuid4().hex[:24]}",
        user_id=user.id,
        org_id=None,
        title="doomed",
        created_at=now,
        updated_at=now,
    )
    db_session.add(conv)
    await db_session.commit()

    monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve())
    monkeypatch.setattr(helper_mod, "run_agent", _fake_run_agent_hang())

    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    resolved = await helper_mod.resolve_helper_llm(user, "", db_session)
    events = helper_mod._turn_events(
        _request(),
        user,
        factory,
        scope=helper_mod.HelperScope(),
        org_id=None,
        dlp_policy=None,
        resolved=resolved,
        conversation=conv,
        message="hello",
    )
    await events.__anext__()  # conversation -> user message + audit context committed

    # The audit context is already durable before the model call.
    assert (
        await db_session.execute(select(RetrievalAuditContext))
    ).scalars().one() is not None

    # A concurrent DELETE mid-turn (FKs are not enforced in the SQLite test DB,
    # so this simulates the production cascade) must not drop the audit context.
    await db_session.execute(
        delete(HelperMessage).where(HelperMessage.conversation_id == conv.id)
    )
    await db_session.execute(
        delete(HelperConversation).where(HelperConversation.id == conv.id)
    )
    await db_session.commit()

    await events.aclose()

    assert (
        await db_session.execute(select(RetrievalAuditContext))
    ).scalars().one() is not None

