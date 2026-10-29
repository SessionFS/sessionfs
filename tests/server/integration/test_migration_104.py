"""Migration 104: stored meeting transcripts (GitHub #116, M1a).

Three NEW tables — ``meeting_transcripts``, ``meeting_participants``,
``meeting_action_items`` — with no existing-table changes. SQLite: simulate
the pre-104 schema by dropping the three tables from the current metadata,
stamp 103, then exercise upgrade/downgrade. PostgreSQL: run the real chain
when SFS_TEST_PG_URL is set and assert column types + check constraints.
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

TRANSCRIPT_COLUMNS = {
    "id", "org_id", "project_id", "group_id", "title", "meeting_at",
    "duration_minutes", "source", "content", "content_sha256", "line_count",
    "dlp_findings", "extraction_status", "extracted_at", "extraction_model",
    "extraction_error", "created_by_user_id", "created_at", "updated_at",
    "deleted_at",
}
PARTICIPANT_COLUMNS = {
    "id", "transcript_id", "name_raw", "email_raw", "user_id", "persona_name",
    "resolution_method", "resolved_by_user_id",
}
ACTION_ITEM_COLUMNS = {
    "id", "transcript_id", "kind", "description", "owner_participant_id",
    "due_date", "line_start", "line_end", "status", "promoted_ticket_id",
    "promoted_by_user_id", "extracted_at",
}

EXPECTED_CHECK_CONSTRAINTS = {
    "meeting_transcripts": {
        "ck_meeting_transcripts_scope",
        "ck_meeting_transcripts_source",
        "ck_meeting_transcripts_extraction_status",
    },
    "meeting_participants": {"ck_meeting_participants_resolution_method"},
    "meeting_action_items": {"ck_meeting_action_items_kind", "ck_meeting_action_items_status"},
}

# varchar columns -> expected PostgreSQL character_maximum_length
VARCHAR_LENGTHS = {
    "meeting_transcripts": {
        "id": 64, "org_id": 64, "project_id": 64, "group_id": 64,
        "title": 200, "source": 20, "content_sha256": 64,
        "extraction_status": 20, "extraction_model": 120, "extraction_error": 200,
        "created_by_user_id": 36,
    },
    "meeting_participants": {
        "id": 64, "transcript_id": 64, "name_raw": 200, "email_raw": 320,
        "user_id": 36, "persona_name": 64, "resolution_method": 20,
        "resolved_by_user_id": 36,
    },
    "meeting_action_items": {
        "id": 64, "transcript_id": 64, "kind": 10,
        "owner_participant_id": 64, "status": 12, "promoted_ticket_id": 64,
        "promoted_by_user_id": 36,
    },
}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def _columns(path: Path, table: str) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return {c["name"] for c in sa.inspect(engine).get_columns(table)}
    finally:
        engine.dispose()


def _check_constraints(path: Path, table: str) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return {c["name"] for c in sa.inspect(engine).get_check_constraints(table)}
    finally:
        engine.dispose()


def _indexes(path: Path, table: str) -> set[str]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        return {ix["name"] for ix in sa.inspect(engine).get_indexes(table)}
    finally:
        engine.dispose()


@pytest.fixture
def pre_104_db(tmp_path: Path) -> Path:
    path = tmp_path / "m104.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        # Simulate 103: the three new tables don't exist yet.
        conn.execute(sa.text("DROP TABLE meeting_action_items"))
        conn.execute(sa.text("DROP TABLE meeting_participants"))
        conn.execute(sa.text("DROP TABLE meeting_transcripts"))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "103")
    return path


def test_upgrade_creates_tables_and_downgrade_reverses(pre_104_db: Path) -> None:
    for table in ("meeting_transcripts", "meeting_participants", "meeting_action_items"):
        with pytest.raises(Exception):
            _columns(pre_104_db, table)

    config = _cfg(f"sqlite+aiosqlite:///{pre_104_db}")
    command.upgrade(config, "104")

    assert TRANSCRIPT_COLUMNS <= _columns(pre_104_db, "meeting_transcripts")
    assert PARTICIPANT_COLUMNS <= _columns(pre_104_db, "meeting_participants")
    assert ACTION_ITEM_COLUMNS <= _columns(pre_104_db, "meeting_action_items")

    for table, expected in EXPECTED_CHECK_CONSTRAINTS.items():
        assert expected <= _check_constraints(pre_104_db, table), table

    assert {
        "idx_meeting_transcripts_project_created",
        "idx_meeting_transcripts_group_created",
        "idx_meeting_transcripts_org_created",
    } <= _indexes(pre_104_db, "meeting_transcripts")
    assert "idx_meeting_participants_transcript" in _indexes(pre_104_db, "meeting_participants")
    assert "idx_meeting_action_items_transcript" in _indexes(pre_104_db, "meeting_action_items")

    command.downgrade(config, "103")
    for table in ("meeting_transcripts", "meeting_participants", "meeting_action_items"):
        with pytest.raises(Exception):
            _columns(pre_104_db, table)

    command.upgrade(config, "104")
    assert TRANSCRIPT_COLUMNS <= _columns(pre_104_db, "meeting_transcripts")


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


def _pg_columns(conn, table: str) -> dict[str, tuple[str, int | None, str]]:
    rows = conn.execute(sa.text(
        "SELECT column_name, data_type, character_maximum_length, is_nullable "
        "FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name = :t"
    ), {"t": table}).all()
    return {r[0]: (r[1], r[2], r[3]) for r in rows}


def _pg_checks(conn, table: str) -> set[str]:
    rows = conn.execute(sa.text(
        "SELECT conname FROM pg_constraint "
        "JOIN pg_class ON pg_class.oid = conrelid "
        "WHERE pg_class.relname = :t AND contype = 'c'"
    ), {"t": table}).all()
    return {r[0] for r in rows}


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "103")

    command.upgrade(config, "104")

    def check(conn) -> None:
        for table, lengths in VARCHAR_LENGTHS.items():
            cols = _pg_columns(conn, table)
            for name, length in lengths.items():
                assert cols[name][0] == "character varying", (table, name)
                assert cols[name][1] == length, (table, name)
            assert cols["id"][2] == "NO", (table, "id")
        # content is text, not varchar.
        assert _pg_columns(conn, "meeting_transcripts")["content"][0] == "text"
        assert _pg_columns(conn, "meeting_action_items")["description"][0] == "text"
        # Timestamptz columns.
        for name in ("meeting_at", "extracted_at", "created_at", "updated_at", "deleted_at"):
            assert _pg_columns(conn, "meeting_transcripts")[name][0] == "timestamp with time zone", name
        for table, expected in EXPECTED_CHECK_CONSTRAINTS.items():
            assert expected <= _pg_checks(conn, table), table

    _pg(url, check)

    command.downgrade(config, "103")

    def gone(conn) -> None:
        for table in ("meeting_transcripts", "meeting_participants", "meeting_action_items"):
            rows = conn.execute(sa.text(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name = :t"
            ), {"t": table}).scalar_one()
            assert rows == 0, table

    _pg(url, gone)
    command.upgrade(config, "head")


def test_sqlite_scope_check_constraint_rejects_both_and_neither(pre_104_db: Path) -> None:
    """The project/group XOR check (ck_meeting_transcripts_scope) is enforced
    on SQLite: a meeting with both scopes set, or with neither, is refused."""
    config = _cfg(f"sqlite+aiosqlite:///{pre_104_db}")
    command.upgrade(config, "104")

    engine = sa.create_engine(f"sqlite:///{pre_104_db}")
    try:
        with engine.begin() as conn:
            cols = ("id", "project_id", "group_id", "title", "source",
                    "content", "content_sha256", "line_count")
            insert = (
                f"INSERT INTO meeting_transcripts ({', '.join(cols)}) "
                "VALUES (:id, :project_id, :group_id, :title, :source, "
                ":content, :content_sha256, :line_count)"
            )
            common = {
                "title": "t", "source": "manual_paste", "content": "x",
                "content_sha256": "0" * 64, "line_count": 1,
            }
            # Both scopes set — violates the XOR.
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(insert), {
                    **common, "id": "meet_both", "project_id": "p1", "group_id": "g1",
                })
            # Neither scope set — also violates the XOR.
            with pytest.raises(sa.exc.IntegrityError):
                conn.execute(sa.text(insert), {
                    **common, "id": "meet_neither", "project_id": None, "group_id": None,
                })
    finally:
        engine.dispose()


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_meeting_fk_on_delete_rules_on_postgres() -> None:
    """FK on-delete rules for the three new tables on PostgreSQL: project and
    group deletion cascade transcripts, user deletion nulls out creator and
    participant references, and transcript deletion cascades children."""
    import asyncio
    from datetime import datetime, timezone

    from sqlalchemy import select as sa_select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sessionfs.server.db.models import (
        MeetingActionItem,
        MeetingParticipant,
        MeetingTranscript,
        Organization,
        Project,
        ProjectGroup,
        User,
    )

    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    command.upgrade(_cfg(url), "head")

    async def _go() -> None:
        engine = create_async_engine(url)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory() as db:
                now = datetime.now(timezone.utc)
                owner = User(id="u_owner_fk", email="owner@fk.test", display_name="Owner",
                             tier="team", email_verified=True, created_at=now)
                member = User(id="u_member_fk", email="member@fk.test", display_name="Member",
                              tier="team", email_verified=True, created_at=now)
                db.add_all([owner, member])
                await db.flush()
                org = Organization(id="org_fk", name="FK Org", slug="fk-org", tier="team")
                db.add(org)
                await db.flush()
                project = Project(id="proj_fk", name="P", owner_id=owner.id, org_id=org.id)
                group = ProjectGroup(id="pg_fk", org_id=org.id, slug="g", name="G",
                                     created_by_user_id=owner.id)
                db.add_all([project, group])
                await db.flush()

                mt_proj = MeetingTranscript(
                    id="mt_proj_fk", org_id=org.id, project_id=project.id, title="t",
                    source="manual_paste", content="x", content_sha256="0" * 64,
                    line_count=1, created_by_user_id=member.id,
                )
                mt_grp = MeetingTranscript(
                    id="mt_grp_fk", org_id=org.id, group_id=group.id, title="t",
                    source="manual_paste", content="x", content_sha256="0" * 64, line_count=1,
                )
                db.add_all([mt_proj, mt_grp])
                await db.flush()

                part = MeetingParticipant(
                    id="mp_fk", transcript_id=mt_proj.id, name_raw="N",
                    user_id=member.id, resolved_by_user_id=member.id,
                )
                db.add(part)
                await db.flush()
                item = MeetingActionItem(
                    id="mai_fk", transcript_id=mt_proj.id, kind="action",
                    description="d", line_start=1, line_end=1, status="pending",
                    extracted_at=now, owner_participant_id=part.id,
                )
                db.add(item)
                await db.commit()

                # User deletion SET NULLs creator + participant references.
                await db.delete(member)
                await db.commit()
                assert (await db.execute(
                    sa_select(MeetingTranscript.created_by_user_id)
                    .where(MeetingTranscript.id == mt_proj.id)
                )).scalar_one() is None
                assert (await db.execute(
                    sa_select(MeetingParticipant.user_id)
                    .where(MeetingParticipant.id == part.id)
                )).scalar_one() is None
                assert (await db.execute(
                    sa_select(MeetingParticipant.resolved_by_user_id)
                    .where(MeetingParticipant.id == part.id)
                )).scalar_one() is None

                # Project deletion CASCADEs its transcripts and their children.
                await db.delete(project)
                await db.commit()
                assert (await db.execute(
                    sa_select(MeetingTranscript.id).where(MeetingTranscript.id == mt_proj.id)
                )).scalar_one_or_none() is None
                assert (await db.execute(
                    sa_select(MeetingParticipant.id).where(MeetingParticipant.id == part.id)
                )).scalar_one_or_none() is None
                assert (await db.execute(
                    sa_select(MeetingActionItem.id).where(MeetingActionItem.id == item.id)
                )).scalar_one_or_none() is None

                # Group deletion CASCADEs its transcripts.
                await db.delete(group)
                await db.commit()
                assert (await db.execute(
                    sa_select(MeetingTranscript.id).where(MeetingTranscript.id == mt_grp.id)
                )).scalar_one_or_none() is None
        finally:
            await engine.dispose()

    asyncio.run(_go())
