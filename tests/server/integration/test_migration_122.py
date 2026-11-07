"""Migration 122: chat threads as a second conversation kind.

SQLite: the real metadata (already 122-shaped) is stamped at 122, downgraded
to 121 (proving the downgrade drops the new columns/table and narrows the
CHECKs), then upgraded again. PostgreSQL: ``test_upgrade_on_postgres`` runs
the real chain (001-121) when SFS_TEST_PG_URL is set, seeds a meeting and a
ledger row, upgrades, and asserts the backfill and every CHECK.
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

_SEED = (
    "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
    "VALUES ('u_m122', 'm122@example.com', {true}, 'free', CURRENT_TIMESTAMP, {true})",
    "INSERT INTO projects (id, name, git_remote_normalized, owner_id, context_document) "
    "VALUES ('proj_m122', 'p', 'acme/m122', 'u_m122', '')",
    "INSERT INTO meeting_transcripts (id, project_id, title, source, content, "
    "content_sha256, line_count) VALUES ('meet_m122', 'proj_m122', 't', "
    "'manual_paste', 'a: b', 'x', 1)",
    "INSERT INTO helper_usage (id, user_id, key_source, source) "
    "VALUES ('hu_m122', 'u_m122', 'user', 'meeting_extract')",
)

_CHAT_OK = (
    "INSERT INTO meeting_transcripts (id, project_id, title, source, content, "
    "content_sha256, line_count, kind, provider, channel_name) VALUES "
    "('chat_ok', 'proj_m122', 't', 'chat_export', 'x', 'y', 1, 'chat', 'slack', '#infra')"
)
_BAD = (
    # chat without a channel name
    "INSERT INTO meeting_transcripts (id, project_id, title, source, content, "
    "content_sha256, line_count, kind, provider) VALUES "
    "('b1', 'proj_m122', 't', 'chat_export', 'x', 'y', 1, 'chat', 'slack')",
    # chat without a provider
    "INSERT INTO meeting_transcripts (id, project_id, title, source, content, "
    "content_sha256, line_count, kind, channel_name) VALUES "
    "('b2', 'proj_m122', 't', 'chat_export', 'x', 'y', 1, 'chat', 'c')",
    # unknown kind
    "INSERT INTO meeting_transcripts (id, project_id, title, source, content, "
    "content_sha256, line_count, kind) VALUES "
    "('b3', 'proj_m122', 't', 'manual_paste', 'x', 'y', 1, 'email')",
    # unknown provider
    "INSERT INTO meeting_transcripts (id, project_id, title, source, content, "
    "content_sha256, line_count, kind, provider, channel_name) VALUES "
    "('b4', 'proj_m122', 't', 'chat_export', 'x', 'y', 1, 'chat', 'irc', 'c')",
    # empty channel name
    "INSERT INTO meeting_transcripts (id, project_id, title, source, content, "
    "content_sha256, line_count, kind, provider, channel_name) VALUES "
    "('b5', 'proj_m122', 't', 'chat_export', 'x', 'y', 1, 'chat', 'paste', '')",
    "INSERT INTO meeting_transcripts (id, project_id, title, source, content, "
    "content_sha256, line_count) VALUES ('b6', 'proj_m122', 't', 'email', 'x', 'y', 1)",
    "INSERT INTO helper_usage (id, user_id, key_source, source) "
    "VALUES ('hu_bad', 'u_m122', 'user', 'chat')",
)


def _children(transcript_id: str, tag: str) -> list[str]:
    """A participant, an item, reminders on the item and the thread, a note."""
    item_kind = "chat_item" if tag == "c" else "meeting_item"
    return [
        "INSERT INTO meeting_participants (id, transcript_id, name_raw, resolution_method) "
        f"VALUES ('mp_{tag}', '{transcript_id}', 'Tolu', 'unresolved')",
        "INSERT INTO meeting_action_items (id, transcript_id, kind, description, line_start, "
        f"line_end, status, extracted_at) VALUES ('mai_{tag}', '{transcript_id}', 'action', "
        "'do it', 1, 1, 'pending', CURRENT_TIMESTAMP)",
        "INSERT INTO reminders (id, owner_user_id, created_via, actor_type, updated_actor_type, "
        "source_kind, source_id, title, due_at, original_due_at, status, snooze_count) VALUES "
        f"('rem_{tag}_item', 'u_m122', 'meeting', 'user', 'user', '{item_kind}', 'mai_{tag}', "
        "'t', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'open', 0)",
        "INSERT INTO reminders (id, owner_user_id, created_via, actor_type, updated_actor_type, "
        "source_kind, source_id, title, due_at, original_due_at, status, snooze_count) VALUES "
        f"('rem_{tag}_thread', 'u_m122', 'meeting', 'user', 'user', 'meeting', '{transcript_id}', "
        "'t', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 'open', 0)",
        "INSERT INTO reminder_notes (id, reminder_id, body, created_at) VALUES "
        f"('rn_{tag}', 'rem_{tag}_item', 'n', CURRENT_TIMESTAMP)",
    ] + (
        ["INSERT INTO conversation_message_meta (source_id, line_no, author_raw) "
         f"VALUES ('{transcript_id}', 1, 'Tolu')"]
        if tag == "c" else []
    )


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _columns(engine: sa.Engine, table: str) -> set[str]:
    inspector = sa.inspect(engine)
    if not inspector.has_table(table):
        return set()
    return {c["name"] for c in inspector.get_columns(table)}


def _assert_122(conn) -> None:
    assert conn.execute(sa.text(
        "SELECT kind, provider, channel_name FROM meeting_transcripts WHERE id = 'meet_m122'"
    )).one() == ("meeting", None, None)
    conn.execute(sa.text(_CHAT_OK))
    conn.execute(sa.text(
        "INSERT INTO helper_usage (id, user_id, key_source, source) "
        "VALUES ('hu_chat', 'u_m122', 'user', 'chat_extract')"
    ))
    conn.execute(sa.text(
        "INSERT INTO conversation_message_meta (source_id, line_no, author_raw) "
        "VALUES ('chat_ok', 1, 'Tolu')"
    ))


@pytest.fixture
def db_at_122(tmp_path: Path) -> Path:
    path = tmp_path / "m122.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for stmt in _SEED:
            conn.execute(sa.text(stmt.format(true=1)))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "122")
    return path


def test_downgrade_and_upgrade_on_sqlite(db_at_122: Path) -> None:
    config = _cfg(f"sqlite+aiosqlite:///{db_at_122}")
    command.downgrade(config, "121")
    engine = sa.create_engine(f"sqlite:///{db_at_122}")
    try:
        assert not {"kind", "provider", "channel_name"} & _columns(engine, "meeting_transcripts")
        assert not _columns(engine, "conversation_message_meta")
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO helper_usage (id, user_id, key_source, source) "
                "VALUES ('hu_x', 'u_m122', 'user', 'chat_extract')"
            ))
    finally:
        engine.dispose()

    command.upgrade(config, "122")
    engine = sa.create_engine(f"sqlite:///{db_at_122}")
    try:
        assert {"kind", "provider", "channel_name"} <= _columns(engine, "meeting_transcripts")
        assert {"source_id", "line_no", "author_raw", "email_raw", "posted_at"} <= _columns(
            engine, "conversation_message_meta"
        )
        with engine.begin() as conn:
            _assert_122(conn)
        for bad in _BAD:
            with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
                conn.execute(sa.text(bad))
        with engine.begin() as conn:
            conn.execute(sa.text("PRAGMA foreign_keys = ON"))
            conn.execute(sa.text("DELETE FROM meeting_transcripts WHERE id = 'chat_ok'"))
            assert conn.execute(sa.text(
                "SELECT count(*) FROM conversation_message_meta"
            )).scalar() == 0
    finally:
        engine.dispose()

    # Children of a chat row, plus the same children on the meeting (kept).
    engine = sa.create_engine(f"sqlite:///{db_at_122}")
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(_CHAT_OK))
            for stmt in _children("chat_ok", "c") + _children("meet_m122", "m"):
                conn.execute(sa.text(stmt))
    finally:
        engine.dispose()

    # A second downgrade drops chat rows (and every row hanging off them)
    # and re-tags their ledger rows.
    command.downgrade(config, "121")
    engine = sa.create_engine(f"sqlite:///{db_at_122}")
    try:
        with engine.begin() as conn:
            assert conn.execute(sa.text(
                "SELECT count(*) FROM helper_usage WHERE source = 'chat_extract'"
            )).scalar() == 0
            assert conn.execute(sa.text("SELECT count(*) FROM meeting_transcripts")).scalar() == 1
            for table, column in (
                ("meeting_action_items", "transcript_id"),
                ("meeting_participants", "transcript_id"),
            ):
                assert conn.execute(sa.text(
                    f"SELECT {column} FROM {table}"
                )).scalars().all() == ["meet_m122"]
            assert sorted(conn.execute(sa.text("SELECT id FROM reminders")).scalars()) == [
                "rem_m_item", "rem_m_thread",
            ]
            assert conn.execute(sa.text("SELECT reminder_id FROM reminder_notes")).scalars().all() == [
                "rem_m_item"
            ]
    finally:
        engine.dispose()
    command.upgrade(config, "122")


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


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    """Against the full real prior chain (001-121) on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "121")

    def seed(conn) -> None:
        for stmt in _SEED:
            conn.execute(sa.text(stmt.format(true="true")))

    _pg(url, seed)
    command.upgrade(config, "122")

    def check(conn) -> None:
        _assert_122(conn)
        for bad in _BAD:
            nested = conn.begin_nested()
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(bad))
            nested.rollback()

    _pg(url, check)
    command.downgrade(config, "121")

    def gone(conn) -> None:
        cols = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'meeting_transcripts'"
            )).all()
        }
        assert "kind" not in cols
        assert conn.execute(sa.text("SELECT count(*) FROM meeting_transcripts")).scalar() == 1
        assert conn.execute(sa.text(
            "SELECT source FROM helper_usage WHERE id = 'hu_chat'"
        )).scalar() == "meeting_extract"

    _pg(url, gone)
    command.upgrade(config, "head")
