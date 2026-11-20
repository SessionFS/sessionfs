"""Migration 139: review_requests + review_runners (auto-reviewer P1a).

SQLite: the prior schema is the real metadata with both tables dropped,
stamped at 138. PostgreSQL: ``test_upgrade_on_postgres`` runs the real chain
when SFS_TEST_PG_URL is set.
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

_RUNNER_COLUMNS = {
    "id",
    "owner_user_id",
    "name",
    "kind",
    "project_ids",
    "status",
    "last_seen_at",
    "created_at",
    "retired_at",
}
_REQUEST_COLUMNS = {
    "id",
    "project_id",
    "requester_user_id",
    "ticket_id",
    "provider",
    "repo",
    "pr_number",
    "head_sha",
    "base_sha",
    "profile",
    "instructions_version",
    "instructions_snapshot",
    "instructions_sha256",
    "mode",
    "budget_tokens",
    "budget_seconds",
    "status",
    "lease_epoch",
    "lease_runner_id",
    "lease_expires_at",
    "superseded_by_id",
    "completed_at",
    "verdict",
    "findings",
    "findings_count",
    "coverage",
    "packet_manifest",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "cost_usd",
    "cost_basis",
    "error",
    "created_at",
    "updated_at",
}
# Two live rows sharing this idempotency key must collide; a superseded row is
# excluded from the partial unique index and is therefore allowed.
_REQUEST_INSERT = (
    "INSERT INTO review_requests "
    "(id, project_id, requester_user_id, provider, repo, head_sha, profile) "
    "VALUES ('rvq_a', 'proj_1', 'usr_1', 'github', 'acme/repo', "
    "'0123456789abcdef0123456789abcdef01234567', 'default')"
)
_RUNNER_INSERT = (
    "INSERT INTO review_runners (id, owner_user_id, name, project_ids) "
    "VALUES ('rnr_a', 'usr_1', 'my runner', '[\"proj_1\"]')"
)


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def pre_139_db(tmp_path: Path) -> Path:
    path = tmp_path / "m139.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(sa.text("DROP TABLE review_requests"))
        conn.execute(sa.text("DROP TABLE review_runners"))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "138")
    return path


def test_upgrade_and_downgrade_on_sqlite(pre_139_db: Path) -> None:
    config = _cfg(f"sqlite+aiosqlite:///{pre_139_db}")
    command.upgrade(config, "139")
    engine = sa.create_engine(f"sqlite:///{pre_139_db}")
    try:
        inspector = sa.inspect(engine)
        assert {c["name"] for c in inspector.get_columns("review_runners")} == _RUNNER_COLUMNS
        assert {c["name"] for c in inspector.get_columns("review_requests")} == _REQUEST_COLUMNS
        assert inspector.get_pk_constraint("review_runners")["constrained_columns"] == ["id"]
        assert inspector.get_pk_constraint("review_requests")["constrained_columns"] == ["id"]

        runner_indexes = {i["name"] for i in inspector.get_indexes("review_runners")}
        assert "uq_review_runners_owner_active" in runner_indexes

        # The idempotency index is expression-based (COALESCE(pr_number, 0)) so
        # SQLAlchemy can't reflect it; read the review_requests indexes from the
        # schema catalog instead.
        with engine.begin() as conn:
            request_indexes = {
                row[0]
                for row in conn.execute(sa.text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='index' AND tbl_name='review_requests'"
                ))
            }
        assert "uq_review_requests_idempotent" in request_indexes
        assert "idx_review_requests_claim" in request_indexes

        # Server defaults and the partial unique idempotency index.
        with engine.begin() as conn:
            conn.execute(sa.text(_RUNNER_INSERT))
            row = conn.execute(
                sa.text("SELECT kind, status, last_seen_at FROM review_runners")
            ).one()
            assert row[0] == "local" and row[1] == "active" and row[2] is None

            conn.execute(sa.text(_REQUEST_INSERT))
            req = conn.execute(
                sa.text(
                    "SELECT status, mode, instructions_version, findings, findings_count "
                    "FROM review_requests"
                )
            ).one()
            assert req[0] == "queued"
            assert req[1] == "draft_for_me"
            assert req[2] == 1
            assert req[3] == "[]"
            assert req[4] == 0

        # A second live row with the same key collides on the partial unique index.
        dup = _REQUEST_INSERT.replace("rvq_a", "rvq_b")
        with pytest.raises(sa.exc.IntegrityError), engine.begin() as conn:
            conn.execute(sa.text(dup))
        # A superseded row is excluded from the index and is therefore allowed.
        with engine.begin() as conn:
            conn.execute(sa.text(
                "INSERT INTO review_requests "
                "(id, project_id, requester_user_id, provider, repo, head_sha, "
                "profile, superseded_by_id, status) "
                "VALUES ('rvq_c', 'proj_1', 'usr_1', 'github', 'acme/repo', "
                "'0123456789abcdef0123456789abcdef01234567', 'default', "
                "'rvq_a', 'superseded')"
            ))
            # A different provider (same repo/head sha) is a distinct target.
            conn.execute(sa.text(
                "INSERT INTO review_requests "
                "(id, project_id, requester_user_id, provider, repo, head_sha, "
                "profile) "
                "VALUES ('rvq_d', 'proj_1', 'usr_1', 'gitlab', 'acme/repo', "
                "'0123456789abcdef0123456789abcdef01234567', 'default')"
            ))
            # A different pr_number (COALESCE(pr_number, 0)) is distinct too.
            conn.execute(sa.text(
                "INSERT INTO review_requests "
                "(id, project_id, requester_user_id, provider, repo, head_sha, "
                "profile, pr_number) "
                "VALUES ('rvq_e', 'proj_1', 'usr_1', 'github', 'acme/repo', "
                "'0123456789abcdef0123456789abcdef01234567', 'default', 42)"
            ))
    finally:
        engine.dispose()

    command.downgrade(config, "138")
    engine = sa.create_engine(f"sqlite:///{pre_139_db}")
    try:
        assert not sa.inspect(engine).has_table("review_requests")
        assert not sa.inspect(engine).has_table("review_runners")
    finally:
        engine.dispose()
    command.upgrade(config, "139")


@pytest.mark.skipif(not os.environ.get("SFS_TEST_PG_URL"), reason="SFS_TEST_PG_URL not set")
def test_upgrade_on_postgres() -> None:
    import asyncio

    from sqlalchemy.ext.asyncio import create_async_engine

    reset_pg_schema()
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "138")
    command.upgrade(config, "139")

    def check(conn) -> None:
        cols = {
            r[0]: r[1]
            for r in conn.execute(sa.text(
                "SELECT column_name, data_type FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'review_requests'"
            ))
        }
        assert set(cols) == _REQUEST_COLUMNS
        assert cols["budget_tokens"] == "bigint"
        assert cols["input_tokens"] == "bigint"
        assert cols["cost_usd"] == "numeric"
        indexes = {
            r[0]
            for r in conn.execute(sa.text(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public' "
                "AND tablename = 'review_requests'"
            ))
        }
        assert "uq_review_requests_idempotent" in indexes

    async def _go():
        engine = create_async_engine(url)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(check)
        finally:
            await engine.dispose()

    asyncio.run(_go())
    command.downgrade(config, "138")
    command.upgrade(config, "139")
