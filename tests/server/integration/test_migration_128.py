"""Migration 128: meeting action items suggest and link existing tickets.

SQLite: the head metadata stamped at 128, downgraded to 127 (a linked row
turns back into a pending one, the columns and the widened CHECK go) and
upgraded again. PostgreSQL: runs the real chain (001-127) when
SFS_TEST_PG_URL is set, then upgrade / downgrade / upgrade with data in the
table, and asserts every constraint.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from sessionfs.server.db.models import Base
from tests.pg_helpers import reset_pg_schema

_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)

_NEW_COLUMNS = {
    "suggested_ticket_id",
    "suggestion_source",
    "suggestion_score",
    "suggestion_dismissed_at",
    "linked_ticket_id",
    "linked_at",
    "linked_by_user_id",
}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _columns(engine: sa.Engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("meeting_action_items")}


_SEED = (
    "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
    "VALUES ('u_m128', 'm128@example.com', {t}, 'free', CURRENT_TIMESTAMP, {t})",
    "INSERT INTO projects (id, name, git_remote_normalized, owner_id, context_document) "
    "VALUES ('proj_m128', 'p', 'acme/m128', 'u_m128', '')",
    "INSERT INTO meeting_transcripts (id, project_id, title, source, content, content_sha256, "
    "line_count, dlp_findings, extraction_status, created_at, updated_at) "
    "VALUES ('meet_m128', 'proj_m128', 't', 'manual_paste', 'a', 'x', 1, 0, 'done', "
    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
    "INSERT INTO meeting_action_items (id, transcript_id, description, line_start, line_end, "
    "status, extracted_at) VALUES ('mai_old', 'meet_m128', 'old', 1, 1, 'pending', "
    "CURRENT_TIMESTAMP)",
)

_LINKED = (
    "INSERT INTO meeting_action_items (id, transcript_id, description, line_start, line_end, "
    "status, extracted_at, suggested_ticket_id, suggestion_source, suggestion_score, "
    "linked_ticket_id, linked_at, linked_by_user_id) VALUES ('mai_linked', 'meet_m128', "
    "'linked', 1, 1, 'linked', CURRENT_TIMESTAMP, 'tk_a', 'model', 0.5, 'tk_a', "
    "CURRENT_TIMESTAMP, 'u_m128')"
)


def _statuses(conn) -> dict[str, str]:
    return dict(conn.execute(sa.text("SELECT id, status FROM meeting_action_items")).all())


def test_downgrade_and_upgrade_on_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "m128.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for stmt in _SEED:
            conn.execute(sa.text(stmt.format(t="1")))
        conn.execute(sa.text(_LINKED))
    assert _NEW_COLUMNS <= _columns(engine)
    engine.dispose()
    config = _cfg(f"sqlite+aiosqlite:///{path}")
    command.stamp(config, "128")

    command.downgrade(config, "127")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        assert not (_NEW_COLUMNS & _columns(engine))
        with engine.begin() as conn:
            assert _statuses(conn) == {"mai_old": "pending", "mai_linked": "pending"}
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "UPDATE meeting_action_items SET status = 'linked' WHERE id = 'mai_old'"
                ))
    finally:
        engine.dispose()

    command.upgrade(config, "128")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        assert _NEW_COLUMNS <= _columns(engine)
        with engine.begin() as conn:
            conn.execute(sa.text(
                "UPDATE meeting_action_items SET status = 'linked', linked_ticket_id = 'tk_b' "
                "WHERE id = 'mai_old'"
            ))
        with engine.begin() as conn:
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "UPDATE meeting_action_items SET suggestion_source = 'guess' "
                    "WHERE id = 'mai_old'"
                ))
        with engine.begin() as conn:
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(
                    "UPDATE meeting_action_items SET status = 'bogus' WHERE id = 'mai_old'"
                ))
        idx = {i["name"] for i in sa.inspect(engine).get_indexes("meeting_action_items")}
        assert "idx_meeting_action_items_transcript" in idx
    finally:
        engine.dispose()


def _pg(url: str, work) -> object:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    async def _go():
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                return await conn.run_sync(work)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _pg_columns(conn) -> set[str]:
    return set(conn.execute(sa.text(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = 'meeting_action_items'"
    )).scalars().all())


def _pg_constraints(conn) -> set[str]:
    return set(conn.execute(sa.text(
        "SELECT conname FROM pg_constraint WHERE conrelid = 'meeting_action_items'::regclass"
    )).scalars().all())


def _pg_raises(url: str, stmt: str) -> bool:
    def work(conn):
        try:
            with conn.begin_nested():
                conn.execute(sa.text(stmt))
        except sa.exc.IntegrityError:
            return True
        return False

    return bool(_pg(url, work))


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="needs SFS_TEST_PG_URL")
def test_upgrade_downgrade_upgrade_on_postgres() -> None:
    """Against the full real prior chain on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "127")

    def seed(conn):
        for stmt in _SEED:
            conn.execute(sa.text(stmt.format(t="true")))

    _pg(url, seed)
    assert not (_NEW_COLUMNS & _pg(url, _pg_columns))

    command.upgrade(config, "128")
    assert _NEW_COLUMNS <= _pg(url, _pg_columns)
    names = _pg(url, _pg_constraints)
    assert {
        "ck_meeting_action_items_status",
        "ck_meeting_action_items_suggestion_source",
        "fk_meeting_action_items_linked_by_user",
    } <= names
    _pg(url, lambda conn: conn.execute(sa.text(_LINKED)))
    assert _pg_raises(
        url, "UPDATE meeting_action_items SET suggestion_source = 'guess' WHERE id = 'mai_old'"
    )
    assert _pg_raises(url, "UPDATE meeting_action_items SET status = 'bogus' WHERE id = 'mai_old'")
    # Deleting the linking user nulls linked_by_user_id (ON DELETE SET NULL).
    _pg(url, lambda conn: conn.execute(sa.text(
        "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
        "VALUES ('u_gone', 'gone@example.com', true, 'free', CURRENT_TIMESTAMP, true)"
    )))
    _pg(url, lambda conn: conn.execute(sa.text(
        "UPDATE meeting_action_items SET linked_by_user_id = 'u_gone' WHERE id = 'mai_linked'"
    )))
    _pg(url, lambda conn: conn.execute(sa.text("DELETE FROM users WHERE id = 'u_gone'")))
    assert _pg(url, lambda conn: conn.execute(sa.text(
        "SELECT linked_by_user_id FROM meeting_action_items WHERE id = 'mai_linked'"
    )).scalar_one()) is None

    command.downgrade(config, "127")
    assert not (_NEW_COLUMNS & _pg(url, _pg_columns))
    assert _pg(url, _statuses) == {"mai_old": "pending", "mai_linked": "pending"}
    assert "ck_meeting_action_items_suggestion_source" not in _pg(url, _pg_constraints)
    assert _pg_raises(url, "UPDATE meeting_action_items SET status = 'linked' WHERE id = 'mai_old'")

    command.upgrade(config, "128")
    assert _NEW_COLUMNS <= _pg(url, _pg_columns)
    assert not _pg_raises(
        url,
        "UPDATE meeting_action_items SET status = 'linked', linked_ticket_id = 'tk_b' "
        "WHERE id = 'mai_old'",
    )
