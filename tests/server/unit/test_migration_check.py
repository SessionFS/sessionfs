"""Tests for the DB-vs-code migration head check (GitHub #66 defense in depth)."""

from __future__ import annotations

import asyncio

from sessionfs.server.db import migration_check
from sessionfs.server.db.migration_check import (
    code_head_revision,
    describe_migration_gap,
    known_revisions,
)


def test_describe_gap_up_to_date_returns_none():
    assert describe_migration_gap("063", "063") is None


def test_describe_gap_behind_names_both_revisions_and_fix():
    message = describe_migration_gap("063", "054")
    assert message is not None
    assert "054" in message
    assert "063" in message
    assert "alembic upgrade head" in message


def test_describe_gap_unknown_db_revision_stays_quiet():
    # A fresh/unreachable DB yields None — must not trip a false alarm.
    assert describe_migration_gap("063", None) is None


def test_describe_gap_unknown_code_head_stays_quiet():
    assert describe_migration_gap("", "054") is None


def test_describe_gap_db_ahead_of_code_says_so():
    message = describe_migration_gap("063", "999", {"062", "063"})
    assert message is not None
    assert "ahead" in message
    assert "alembic upgrade head" not in message


def test_describe_gap_known_older_revision_is_behind():
    message = describe_migration_gap("063", "062", {"062", "063"})
    assert message is not None
    assert "behind" in message


def test_code_head_revision_is_a_known_revision():
    head = code_head_revision()
    revisions = known_revisions()
    assert head is not None
    assert revisions is not None
    assert head in revisions
    assert "001" in revisions or len(revisions) > 50


def test_startup_check_never_raises_when_the_script_dir_is_broken(monkeypatch):
    def boom() -> str:
        raise RuntimeError("unreadable migrations dir")

    monkeypatch.setattr(migration_check, "code_head_revision", boom)
    asyncio.run(migration_check.log_migration_gap_if_behind())


def _engine_with_revision(revision: str | None):
    """An in-memory SQLite engine whose alembic_version holds ``revision``
    (or has no alembic_version table at all when ``revision`` is None)."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine("sqlite+aiosqlite://")

    async def setup() -> None:
        if revision is None:
            return
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32))"))
            await conn.execute(
                text("INSERT INTO alembic_version VALUES (:v)"), {"v": revision}
            )

    return engine, setup


def _state_for(monkeypatch, revision: str | None) -> str:
    from sessionfs.server.db import engine as engine_module

    async def run() -> str:
        engine, setup = _engine_with_revision(revision)
        await setup()
        monkeypatch.setattr(engine_module, "_engine", engine)
        try:
            return await migration_check.schema_state()
        finally:
            await engine.dispose()

    return asyncio.run(run())


def test_schema_state_classifies_current_behind_ahead_and_missing(monkeypatch):
    head = code_head_revision()
    assert _state_for(monkeypatch, head) == "current"
    assert _state_for(monkeypatch, "001") == "behind"
    assert _state_for(monkeypatch, "999_not_a_revision") == "ahead"
    assert _state_for(monkeypatch, None) == "missing"


def test_schema_state_is_unknown_without_an_engine(monkeypatch):
    from sessionfs.server.db import engine as engine_module

    monkeypatch.setattr(engine_module, "_engine", None)
    assert asyncio.run(migration_check.schema_state()) == "unknown"


def test_readiness_endpoint_maps_states_to_status_codes(monkeypatch):
    from sessionfs.server.routes import health
    from sessionfs.server.routes.health import readiness

    def check(state: str) -> int:
        async def fake_state(state=state) -> str:
            return state

        monkeypatch.setattr(migration_check, "schema_state", fake_state)
        return asyncio.run(readiness()).status_code

    monkeypatch.setattr(health, "_schema_confirmed", False)
    # A fresh pod that can't reach the database is not ready yet.
    assert check("unknown") == 503
    assert check("behind") == 503
    assert check("missing") == 503
    assert check("current") == 200
    # Once confirmed, a database blip keeps the pod in service ...
    assert check("unknown") == 200
    # ... but a schema that is genuinely behind still takes it out.
    assert check("behind") == 503
    monkeypatch.setattr(health, "_schema_confirmed", False)
    assert check("ahead") == 200


def test_readiness_over_http_reports_503_against_a_behind_database(monkeypatch):
    """GitHub #83: a database one migration behind the code must fail the
    readiness probe end to end (real route, real alembic_version row)."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from sessionfs.server.db import engine as engine_module
    from sessionfs.server.routes import health

    monkeypatch.setattr(health, "_schema_confirmed", False)
    app = FastAPI()
    app.include_router(health.router)
    previous = sorted(r for r in known_revisions() if r != code_head_revision())[-1]

    async def probe(revision: str | None):
        engine, setup = _engine_with_revision(revision)
        await setup()
        monkeypatch.setattr(engine_module, "_engine", engine)
        try:
            async with AsyncClient(
                transport=ASGITransport(app=app), base_url="http://api"
            ) as client:
                return await client.get("/health/ready")
        finally:
            await engine.dispose()

    behind = asyncio.run(probe(previous))
    assert behind.status_code == 503
    assert behind.json() == {"status": "not_ready", "schema": "behind"}

    current = asyncio.run(probe(code_head_revision()))
    assert current.status_code == 200
    assert current.json() == {"status": "ready", "schema": "current"}
