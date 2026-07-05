"""Migration 057 (Resident R0) round-trip: upgrade/downgrade on SQLite.

Mirrors test_migration_056 harness: direct SQLite upgrade()/downgrade()
with a minimal pre-057 schema stamped at 056, then upgrade to 057 and
assert residents + resident_memory_entries tables + work_queue_items
F1 columns; then downgrade and assert reversal.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


def _build_pre_057_db(db_path: Path) -> None:
    """Minimal prerequisite schema for the tables migration 057 references."""
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")

    # organizations
    conn.execute(
        "CREATE TABLE organizations ("
        "  id VARCHAR(64) PRIMARY KEY"
        ")"
    )
    conn.execute("INSERT INTO organizations (id) VALUES ('org-1')")

    # projects — referenced by residents.project_id
    conn.execute(
        "CREATE TABLE projects ("
        "  id VARCHAR(64) PRIMARY KEY,"
        "  org_id VARCHAR(64) NOT NULL"
        ")"
    )
    conn.execute("INSERT INTO projects (id, org_id) VALUES ('proj-1', 'org-1')")

    # work_queues — referenced by residents.work_queue_id
    conn.execute(
        "CREATE TABLE work_queues ("
        "  id VARCHAR(64) PRIMARY KEY,"
        "  project_id VARCHAR(64) NOT NULL,"
        "  name VARCHAR(100) NOT NULL,"
        "  mode VARCHAR(30) NOT NULL,"
        "  status VARCHAR(20) NOT NULL DEFAULT 'active',"
        "  selector TEXT NOT NULL DEFAULT '{}',"
        "  cadence_seconds INTEGER NOT NULL DEFAULT 300,"
        "  max_tickets_per_run INTEGER NOT NULL DEFAULT 1,"
        "  max_attempts_per_item INTEGER NOT NULL DEFAULT 3,"
        "  lease_epoch INTEGER NOT NULL DEFAULT 0,"
        "  created_by_user_id VARCHAR(64) NOT NULL,"
        "  created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "  updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    conn.execute(
        "INSERT INTO work_queues (id, project_id, name, mode, created_by_user_id) "
        "VALUES ('wq-1', 'proj-1', 'test-q', 'implement_until_done', 'user-1')"
    )

    # tickets — referenced by work_queue_items.ticket_id
    conn.execute(
        "CREATE TABLE tickets ("
        "  id VARCHAR(64) PRIMARY KEY,"
        "  project_id VARCHAR(64) NOT NULL,"
        "  title VARCHAR(200) NOT NULL,"
        "  status VARCHAR(20) NOT NULL DEFAULT 'open',"
        "  lease_epoch INTEGER NOT NULL DEFAULT 0"
        ")"
    )

    # work_queue_items — pre-057 shape (without F1 columns)
    conn.execute(
        "CREATE TABLE work_queue_items ("
        "  id VARCHAR(64) PRIMARY KEY,"
        "  work_queue_id VARCHAR(64) NOT NULL,"
        "  ticket_id VARCHAR(64) NOT NULL,"
        "  item_status VARCHAR(20) NOT NULL DEFAULT 'pending',"
        "  last_seen_comment_at TEXT,"
        "  last_seen_comment_id VARCHAR(64),"
        "  last_acked_comment_at TEXT,"
        "  last_acked_comment_id VARCHAR(64),"
        "  open_directive_id VARCHAR(64),"
        "  open_directive_run_id VARCHAR(64),"
        "  last_agent_run_id VARCHAR(64),"
        "  last_verdict VARCHAR(20),"
        "  attempts INTEGER NOT NULL DEFAULT 0,"
        "  next_eligible_at TEXT,"
        "  created_at TEXT NOT NULL DEFAULT (datetime('now')),"
        "  updated_at TEXT NOT NULL DEFAULT (datetime('now'))"
        ")"
    )
    conn.execute(
        "INSERT INTO work_queue_items "
        "(id, work_queue_id, ticket_id, item_status) "
        "VALUES ('wqi-1', 'wq-1', 'tk-1', 'active')"
    )

    conn.commit()
    conn.close()


def _cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option(
        "script_location", "src/sessionfs/server/db/migrations"
    )
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


@pytest.fixture
def migration_057_db_path(tmp_path: Path) -> Path:
    db_path = tmp_path / "migration_057_test.db"
    _build_pre_057_db(db_path)
    command.stamp(_cfg(db_path), "056")
    return db_path


class TestMigration057:
    # ── upgrade assertions ──────────────────────────────────────────

    def test_upgrade_creates_residents_table(self, migration_057_db_path):
        command.upgrade(_cfg(migration_057_db_path), "057")
        conn = sqlite3.connect(str(migration_057_db_path))

        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "residents" in tables
        assert "resident_memory_entries" in tables
        assert "work_queue_items" in tables

        # residents columns.
        res_cols = {
            r[1]
            for r in conn.execute("PRAGMA table_info('residents')").fetchall()
        }
        for col in (
            "id", "org_id", "project_id", "kind", "persona_name",
            "service_key_id", "work_queue_id", "status",
            "mind_token_budget", "max_uncompacted_entries",
            "created_by_user_id", "actor_type", "service_key_name",
            "created_at", "updated_at",
        ):
            assert col in res_cols, f"Missing column: {col}"

        # resident_memory_entries columns.
        rme_cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info('resident_memory_entries')"
            ).fetchall()
        }
        for col in (
            "id", "resident_id", "org_id", "kind", "seq", "content",
            "token_estimate", "superseded_by", "compacted_at",
            "quarantined", "created_at",
        ):
            assert col in rme_cols, f"Missing column: {col}"

        # F1 columns on work_queue_items.
        wqi_cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info('work_queue_items')"
            ).fetchall()
        }
        for col in (
            "implementer_service_key_id", "implementer_user_id",
            "closed_by_service_key_id", "closed_by_user_id",
            "auto_close_review_kind",
        ):
            assert col in wqi_cols, f"Missing F1 column: {col}"

        conn.close()

    def test_upgrade_creates_indexes(self, migration_057_db_path):
        command.upgrade(_cfg(migration_057_db_path), "057")
        conn = sqlite3.connect(str(migration_057_db_path))

        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        for idx in (
            "uq_resident_service_key",
            "idx_resident_org_project",
            "idx_rme_resident_kind_seq",
            "idx_rme_resident_created",
            "idx_rme_org",
            "uq_resident_memory_seq",
        ):
            assert idx in indexes, f"Missing index: {idx}"

        conn.close()

    # ── uniqueness enforcement ──────────────────────────────────────

    def test_resident_service_key_unique(self, migration_057_db_path):
        """Duplicate service_key_id → rejected."""
        command.upgrade(_cfg(migration_057_db_path), "057")
        conn = sqlite3.connect(str(migration_057_db_path))
        conn.execute("PRAGMA foreign_keys = OFF")

        conn.execute(
            "INSERT INTO residents "
            "(id, org_id, project_id, kind, persona_name, service_key_id, "
            " created_by_user_id) "
            "VALUES ('res-1', 'org-1', 'proj-1', 'reviewer', 'codex-reviewer', "
            "        'sk-1', 'user-1')"
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO residents "
                "(id, org_id, project_id, kind, persona_name, service_key_id, "
                " created_by_user_id) "
                "VALUES ('res-2', 'org-1', 'proj-1', 'implementer', 'atlas', "
                "        'sk-1', 'user-1')"
            )
            conn.commit()
        conn.rollback()

        conn.close()

    def test_resident_memory_seq_unique(self, migration_057_db_path):
        """Duplicate (resident_id, seq) → rejected."""
        command.upgrade(_cfg(migration_057_db_path), "057")
        conn = sqlite3.connect(str(migration_057_db_path))
        conn.execute("PRAGMA foreign_keys = OFF")

        conn.execute(
            "INSERT INTO residents "
            "(id, org_id, project_id, kind, persona_name, service_key_id, "
            " created_by_user_id) "
            "VALUES ('res-1', 'org-1', 'proj-1', 'reviewer', 'codex-reviewer', "
            "        'sk-1', 'user-1')"
        )
        conn.commit()

        conn.execute(
            "INSERT INTO resident_memory_entries "
            "(id, resident_id, org_id, kind, seq, content) "
            "VALUES ('rme-1', 'res-1', 'org-1', 'reasoning', 1, 'hello')"
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO resident_memory_entries "
                "(id, resident_id, org_id, kind, seq, content) "
                "VALUES ('rme-2', 'res-1', 'org-1', 'reasoning', 1, 'duplicate')"
            )
            conn.commit()
        conn.rollback()

        # Different resident, same seq → OK.
        conn.execute(
            "INSERT INTO residents "
            "(id, org_id, project_id, kind, persona_name, service_key_id, "
            " created_by_user_id) "
            "VALUES ('res-2', 'org-1', 'proj-1', 'implementer', 'atlas', "
            "        'sk-2', 'user-1')"
        )
        conn.execute(
            "INSERT INTO resident_memory_entries "
            "(id, resident_id, org_id, kind, seq, content) "
            "VALUES ('rme-3', 'res-2', 'org-1', 'reasoning', 1, 'other')"
        )
        conn.commit()

        conn.close()

    # ── downgrade ───────────────────────────────────────────────────

    def test_downgrade_reverses_all_changes(self, migration_057_db_path):
        cfg = _cfg(migration_057_db_path)
        command.upgrade(cfg, "057")
        command.downgrade(cfg, "056")
        conn = sqlite3.connect(str(migration_057_db_path))

        # Tables are gone.
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "residents" not in tables
        assert "resident_memory_entries" not in tables
        # work_queue_items still exists but F1 columns are gone.
        assert "work_queue_items" in tables

        # F1 columns removed.
        wqi_cols = {
            r[1]
            for r in conn.execute(
                "PRAGMA table_info('work_queue_items')"
            ).fetchall()
        }
        for col in (
            "implementer_service_key_id", "implementer_user_id",
            "closed_by_service_key_id", "closed_by_user_id",
            "auto_close_review_kind",
        ):
            assert col not in wqi_cols, f"F1 column should be gone: {col}"

        # Existing columns still present.
        for col in ("id", "work_queue_id", "ticket_id", "item_status"):
            assert col in wqi_cols, f"Pre-existing column missing: {col}"

        # Indexes are gone.
        indexes = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            ).fetchall()
        }
        for idx in (
            "uq_resident_service_key",
            "idx_resident_org_project",
            "idx_rme_resident_kind_seq",
            "idx_rme_resident_created",
            "idx_rme_org",
            "uq_resident_memory_seq",
        ):
            assert idx not in indexes, f"Index should be gone: {idx}"

        conn.close()
