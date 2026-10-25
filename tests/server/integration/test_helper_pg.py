"""PG-only regression tests for the session helper turn persistence.

Runs when ``SFS_TEST_PG_URL`` points at a scratch PostgreSQL database. The
shielded persistence block bounds its write with a transaction-local
``SET LOCAL statement_timeout``; these tests prove a turn persists on the real
dialect and that the bound never leaks into the pooled connection (which would
otherwise hand a 10 s timeout to every later request that reuses it).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from sessionfs.judge.agent_stream import (
    Done,
    TextDelta,
    ToolCall,
    ToolCallEvent,
    ToolResultEvent,
    Usage,
)
from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    Base,
    HelperConversation,
    HelperMessage,
    RetrievalAuditEvent,
    User,
)
from sessionfs.server.helper.keys import ResolvedLLM

import sessionfs.server.routes.helper as helper_mod

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("SFS_TEST_PG_URL"),
        reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database",
    ),
]

_FAKE_KEY = "sk-test-key-1234"


async def _fake_resolve(user: User, org_id: str, db) -> ResolvedLLM | None:
    return ResolvedLLM(
        provider="openai",
        model="gpt-4o",
        api_key=_FAKE_KEY,
        base_url=None,
        key_source="user",
    )


def _fake_run_agent_text_only():
    async def _fake(*, execute_tool, **kwargs):
        yield Usage(input_tokens=5, output_tokens=2)
        yield TextDelta("Nothing found.")
        yield Done("stop")

    return _fake


def _fake_run_agent_with_tool():
    async def _fake(*, execute_tool, **kwargs):
        call = ToolCall(id="call_1", name="list_recent_sessions", arguments={})
        yield ToolCallEvent(call)
        result = await execute_tool(call)
        yield ToolResultEvent(call, result)
        yield Usage(input_tokens=5, output_tokens=2)
        yield TextDelta("Nothing relevant.")
        yield Done("stop")

    return _fake


def _request():
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(blob_store=None)))


async def _seed_user_and_conversation(factory) -> tuple[User, HelperConversation]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4().hex[:6]}@example.com",
        display_name="PG Helper",
        tier="enterprise",
        email_verified=True,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    conv = HelperConversation(
        id=f"hc_{uuid.uuid4().hex[:24]}",
        user_id=user.id,
        org_id=None,
        title="pg turn",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    async with factory() as db:
        db.add(user)
        await db.flush()
        db.add(conv)
        await db.flush()
        # A first user message so the conversation already has history.
        db.add(
            HelperMessage(
                id=f"hm_{uuid.uuid4().hex[:24]}",
                conversation_id=conv.id,
                role="user",
                content="hi",
                created_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
    return user, conv


async def test_persistence_does_not_leak_statement_timeout(monkeypatch: pytest.MonkeyPatch):
    """A turn's shielded persistence uses ``SET LOCAL``, so after the commit the
    pooled connection reports the server default ``statement_timeout`` (not the
    10 s bound) when checked out again (R3-1)."""
    url = os.environ["SFS_TEST_PG_URL"]
    # A pool of one forces the probe to reuse the exact connection the turn's
    # persistence just returned — what a later request on a pooled connection
    # would see. A fresh connection would already show the server default, so
    # a larger pool would let the leak slip through unnoticed.
    engine = create_async_engine(url, pool_size=1, max_overflow=0)
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        user, conv = await _seed_user_and_conversation(factory)

        monkeypatch.setattr(helper_mod, "run_agent", _fake_run_agent_text_only())

        resolved = await _fake_resolve(user, "", None)
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
        async for _ in events:
            pass

        # Check out a connection from the SAME engine/pool and confirm the
        # transaction-local timeout reverted at commit.
        async with engine.connect() as conn:
            value = (await conn.execute(text("SHOW statement_timeout"))).scalar_one()
            assert value == "0"
    finally:
        await engine.dispose()


@pytest.mark.parametrize("with_tool", [False, True])
async def test_existing_turn_releases_request_session_before_stream(
    monkeypatch: pytest.MonkeyPatch, with_tool: bool
):
    """R3-3: a second turn on an existing thread must not leave the
    request-scoped session's transaction open for the stream.

    The route resolves everything it needs up front and commits the
    request-scoped session before yielding to the turn's fresh sessions. With a
    pool of exactly one connection, that transaction would otherwise starve the
    turn's own persistence (which shares the pool) and the request would time
    out instead of persisting.
    """
    from httpx import ASGITransport, AsyncClient

    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig
    from sessionfs.server.db.engine import get_db
    import sessionfs.server.db.engine as engine_mod

    url = os.environ["SFS_TEST_PG_URL"]
    engine = create_async_engine(url, pool_size=1, max_overflow=0, pool_timeout=3)
    engine_mod._session_factory = None
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)

        user, conv = await _seed_user_and_conversation(factory)
        raw = generate_api_key()
        async with factory() as db:
            db.add(
                ApiKey(
                    id=str(uuid.uuid4()),
                    user_id=user.id,
                    key_hash=hash_api_key(raw),
                    name="pg-helper-key",
                    created_at=datetime.now(timezone.utc),
                )
            )
            await db.commit()

        config = ServerConfig(database_url="sqlite+aiosqlite://")
        app = create_app(config)

        async def override_get_db():
            async with factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        app.state.blob_store = None
        # The lifespan is not run under ASGITransport; mirror what the sweep
        # routes read from request.app.state.config.
        app.state.config = config
        # The turn persists on its own fresh sessions via the module factory, so
        # point it at the single-connection pool like the request dependency.
        engine_mod._session_factory = factory

        monkeypatch.setattr(helper_mod, "resolve_helper_llm", _fake_resolve)
        monkeypatch.setattr(
            helper_mod,
            "run_agent",
            _fake_run_agent_with_tool() if with_tool else _fake_run_agent_text_only(),
        )

        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            resp = await client.post(
                "/api/v1/helper/turns",
                headers={"Authorization": f"Bearer {raw}"},
                json={
                    "message": "hi again",
                    "stream": False,
                    "conversation_id": conv.id,
                },
            )
            assert resp.status_code == 200, resp.text

        # The second user message and the assistant answer both persisted.
        async with factory() as db:
            roles = [
                r.role
                for r in (
                    await db.execute(
                        select(HelperMessage)
                        .where(HelperMessage.conversation_id == conv.id)
                        .order_by(
                            HelperMessage.created_at.asc(), HelperMessage.id.asc()
                        )
                    )
                ).scalars().all()
            ]
            if with_tool:
                # A tool-calling turn holds the agent-loop session's connection;
                # it must be released before persisting or the single-connection
                # pool starves and the audit events are dropped.
                assert roles == ["user", "user", "tool", "assistant"]
                events = (
                    await db.execute(select(RetrievalAuditEvent))
                ).scalars().all()
                assert len(events) >= 1
            else:
                assert roles == ["user", "user", "assistant"]
    finally:
        engine_mod._session_factory = None
        await engine.dispose()
