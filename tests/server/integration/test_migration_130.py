"""Migration 130: external contacts.

SQLite: the head metadata stamped at 130, downgraded to 129 (a
contact-resolved participant goes back to unresolved, the columns, the
widened CHECK and both tables go) and upgraded again. PostgreSQL: runs the
real chain (001-129) when SFS_TEST_PG_URL is set, then upgrade / downgrade /
upgrade with data, and asserts every constraint and FK action.
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
_TABLES = {"external_contacts", "external_contact_aliases"}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


_SEED = (
    "INSERT INTO users (id, email, email_verified, tier, created_at, is_active) "
    "VALUES ('u_m130', 'm130@example.com', {t}, 'free', CURRENT_TIMESTAMP, {t})",
    "INSERT INTO organizations (id, name, slug, tier, seats_limit, settings) "
    "VALUES ('org_m130', 'O', 'o-m130', 'team', 5, '{{}}')",
    "INSERT INTO projects (id, name, git_remote_normalized, owner_id, context_document, org_id) "
    "VALUES ('proj_m130', 'p', 'acme/m130', 'u_m130', '', 'org_m130')",
    "INSERT INTO meeting_transcripts (id, project_id, title, source, content, content_sha256, "
    "line_count, dlp_findings, extraction_status, created_at, updated_at) "
    "VALUES ('meet_m130', 'proj_m130', 't', 'manual_paste', 'a', 'x', 1, 0, 'done', "
    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
    "INSERT INTO meeting_participants (id, transcript_id, name_raw, resolution_method) "
    "VALUES ('mp_old', 'meet_m130', 'Tolu', 'unresolved')",
)

_CONTACT = (
    "INSERT INTO external_contacts (id, org_id, display_name, name_norm, email, status, "
    "created_at, updated_at) VALUES ('ec_aaaaaaaaaaaa', 'org_m130', 'Tolu', 'tolu', "
    "'tolu@v.example', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
    "INSERT INTO external_contact_aliases (contact_id, alias_norm, alias, org_id, created_at) "
    "VALUES ('ec_aaaaaaaaaaaa', 't', 'T', 'org_m130', CURRENT_TIMESTAMP)",
    "UPDATE meeting_participants SET contact_id = 'ec_aaaaaaaaaaaa', "
    "resolution_method = 'contact' WHERE id = 'mp_old'",
    "INSERT INTO tickets (id, project_id, title, created_by_user_id, assigned_contact_id) "
    "VALUES ('tk_m130', 'proj_m130', 't', 'u_m130', 'ec_aaaaaaaaaaaa')",
)

_DUP_EMAIL = (
    "INSERT INTO external_contacts (id, org_id, display_name, name_norm, email, status, "
    "created_at, updated_at) VALUES ('ec_bbbbbbbbbbbb', 'org_m130', 'T2', 't2', "
    "'tolu@v.example', 'active', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
)
_MERGED_NO_TARGET = (
    "INSERT INTO external_contacts (id, org_id, display_name, name_norm, status, "
    "created_at, updated_at) VALUES ('ec_cccccccccccc', 'org_m130', 'M', 'm', 'merged', "
    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
)
_BAD_METHOD = "UPDATE meeting_participants SET resolution_method = 'bogus' WHERE id = 'mp_old'"


def _sqlite_tables(engine: sa.Engine) -> set[str]:
    return set(sa.inspect(engine).get_table_names())


def _part_columns(engine: sa.Engine) -> set[str]:
    return {c["name"] for c in sa.inspect(engine).get_columns("meeting_participants")}


def _raises(engine: sa.Engine, stmt: str) -> bool:
    try:
        with engine.begin() as conn:
            conn.execute(sa.text(stmt))
    except sa.exc.IntegrityError:
        return True
    return False


def test_downgrade_and_upgrade_on_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "m130.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        for stmt in _SEED:
            conn.execute(sa.text(stmt.format(t="1")))
        for stmt in _CONTACT:
            conn.execute(sa.text(stmt))
    engine.dispose()
    config = _cfg(f"sqlite+aiosqlite:///{path}")
    command.stamp(config, "130")

    command.downgrade(config, "129")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        assert not (_TABLES & _sqlite_tables(engine))
        assert "contact_id" not in _part_columns(engine)
        ticket_cols = {c["name"] for c in sa.inspect(engine).get_columns("tickets")}
        assert "assigned_contact_id" not in ticket_cols
        with engine.begin() as conn:
            method = conn.execute(sa.text(
                "SELECT resolution_method FROM meeting_participants WHERE id = 'mp_old'"
            )).scalar_one()
        assert method == "unresolved"
        assert _raises(
            engine,
            "UPDATE meeting_participants SET resolution_method = 'contact' WHERE id = 'mp_old'",
        )
    finally:
        engine.dispose()

    command.upgrade(config, "130")
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        assert _TABLES <= _sqlite_tables(engine)
        assert "contact_id" in _part_columns(engine)
        with engine.begin() as conn:
            for stmt in _CONTACT[:3]:
                conn.execute(sa.text(stmt))
            conn.execute(sa.text(
                "UPDATE tickets SET assigned_contact_id = 'ec_aaaaaaaaaaaa' WHERE id = 'tk_m130'"
            ))
        assert _raises(engine, _DUP_EMAIL)
        assert _raises(engine, _MERGED_NO_TARGET)
        assert _raises(engine, _BAD_METHOD)
        idx = {i["name"] for i in sa.inspect(engine).get_indexes("meeting_participants")}
        assert {"idx_meeting_participants_transcript", "idx_meeting_participants_contact"} <= idx
        tidx = {i["name"] for i in sa.inspect(engine).get_indexes("tickets")}
        assert "idx_ticket_assigned_contact" in tidx
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


def _pg_raises(url: str, stmt: str) -> bool:
    def work(conn):
        try:
            with conn.begin_nested():
                conn.execute(sa.text(stmt))
        except sa.exc.IntegrityError:
            return True
        return False

    return bool(_pg(url, work))


def _pg_tables(conn) -> set[str]:
    return set(conn.execute(sa.text(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
    )).scalars().all())


def _pg_constraints(conn, table: str) -> set[str]:
    return set(conn.execute(sa.text(
        f"SELECT conname FROM pg_constraint WHERE conrelid = '{table}'::regclass"
    )).scalars().all())


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="needs SFS_TEST_PG_URL")
def test_upgrade_downgrade_upgrade_on_postgres() -> None:
    """Against the full real prior chain on PostgreSQL."""
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "129")

    def seed(conn):
        for stmt in _SEED:
            conn.execute(sa.text(stmt.format(t="true")))

    _pg(url, seed)
    assert not (_TABLES & _pg(url, _pg_tables))

    command.upgrade(config, "130")
    assert _TABLES <= _pg(url, _pg_tables)
    assert {
        "ck_external_contacts_status",
        "ck_external_contacts_merged",
    } <= _pg(url, lambda c: _pg_constraints(c, "external_contacts"))
    assert {
        "ck_meeting_participants_resolution_method",
        "fk_meeting_participants_contact",
    } <= _pg(url, lambda c: _pg_constraints(c, "meeting_participants"))
    assert "fk_tickets_assigned_contact" in _pg(url, lambda c: _pg_constraints(c, "tickets"))

    def contact(conn):
        for stmt in _CONTACT:
            conn.execute(sa.text(stmt))

    _pg(url, contact)
    assert _pg_raises(url, _DUP_EMAIL)
    assert _pg_raises(url, _MERGED_NO_TARGET)
    assert _pg_raises(url, _BAD_METHOD)

    # A merged row frees the email slot (partial unique index).
    def merge_then_reuse(conn):
        conn.execute(sa.text(
            "INSERT INTO external_contacts (id, org_id, display_name, name_norm, status, "
            "created_at, updated_at) VALUES ('ec_dddddddddddd', 'org_m130', 'D', 'd', 'active', "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
        conn.execute(sa.text(
            "UPDATE external_contacts SET status = 'merged', merged_into_id = 'ec_dddddddddddd' "
            "WHERE id = 'ec_aaaaaaaaaaaa'"
        ))
        conn.execute(sa.text(_DUP_EMAIL))

    _pg(url, merge_then_reuse)

    # Deleting a contact: participants and tickets SET NULL, aliases CASCADE.
    def erase(conn):
        conn.execute(sa.text("DELETE FROM external_contacts WHERE id = 'ec_bbbbbbbbbbbb'"))
        conn.execute(sa.text(
            "UPDATE external_contacts SET status = 'active', merged_into_id = NULL "
            "WHERE id = 'ec_aaaaaaaaaaaa'"
        ))
        conn.execute(sa.text("DELETE FROM external_contacts WHERE id = 'ec_aaaaaaaaaaaa'"))
        return (
            conn.execute(sa.text(
                "SELECT contact_id FROM meeting_participants WHERE id = 'mp_old'"
            )).scalar_one(),
            conn.execute(sa.text(
                "SELECT assigned_contact_id FROM tickets WHERE id = 'tk_m130'"
            )).scalar_one(),
            conn.execute(sa.text("SELECT count(*) FROM external_contact_aliases")).scalar_one(),
        )

    assert _pg(url, erase) == (None, None, 0)

    # Put a contact-resolved participant back, then downgrade.
    def recontact(conn):
        conn.execute(sa.text(_CONTACT[0]))
        conn.execute(sa.text(_CONTACT[2]))

    _pg(url, recontact)
    command.downgrade(config, "129")
    assert not (_TABLES & _pg(url, _pg_tables))
    method = _pg(url, lambda c: c.execute(sa.text(
        "SELECT resolution_method FROM meeting_participants WHERE id = 'mp_old'"
    )).scalar_one())
    assert method == "unresolved"
    assert _pg_raises(
        url, "UPDATE meeting_participants SET resolution_method = 'contact' WHERE id = 'mp_old'"
    )

    command.upgrade(config, "130")
    assert _TABLES <= _pg(url, _pg_tables)
    command.upgrade(config, "head")
