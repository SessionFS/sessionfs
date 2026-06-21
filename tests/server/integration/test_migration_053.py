"""Migration 053 upgrade/downgrade round-trip on SQLite.

tk_d42170b4670f4448 — trusted review-verdict provenance. Mirrors the
migration 051 test harness: build the pre-053 schema with raw sqlite3,
seed representative ticket_comments (including a legacy operator
codex-reviewer thread AND a non-operator codex-reviewer comment), stamp
at 052, upgrade to 053, and assert:
  - ticket_comments.verdict_trusted column exists (fail-closed default);
  - trusted_reviewers table + indexes + CHECK constraints exist;
  - the operator's legacy codex-reviewer comments are backfilled trusted;
  - a non-operator codex-reviewer comment stays untrusted;
  - a trusted_reviewers seed row exists for the operator;
  - downgrade drops the table + column.

The broken pre-003 PG-only migration chain is bypassed — stamp at 052,
upgrade only runs 052→053.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


# Must match migration 053's _OPERATOR_USER_ID.
_OPERATOR = "f973f29e-6da1-483e-b9f3-2851a90bf3c9"
_OTHER = "00000000-0000-0000-0000-000000000099"


@pytest.fixture
def migration_053_db_path(tmp_path: Path) -> Path:
    db_path = tmp_path / "migration_053_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")

    # ── Minimal prerequisite schema (tables 053 reads/alters) ──
    conn.execute(
        "CREATE TABLE users (id VARCHAR(36) PRIMARY KEY, email VARCHAR(255) UNIQUE)"
    )
    conn.execute(
        "CREATE TABLE organizations (id VARCHAR(64) PRIMARY KEY, name VARCHAR(255), "
        "slug VARCHAR(100) UNIQUE)"
    )
    conn.execute(
        "CREATE TABLE projects (id VARCHAR(64) PRIMARY KEY, name VARCHAR(255), "
        "org_id VARCHAR(64))"
    )
    conn.execute(
        "CREATE TABLE tickets (id VARCHAR(64) PRIMARY KEY, project_id VARCHAR(64))"
    )
    conn.execute(
        "CREATE TABLE ticket_comments ("
        "  id VARCHAR(64) PRIMARY KEY,"
        "  ticket_id VARCHAR(64) NOT NULL,"
        "  author_user_id VARCHAR(64) NOT NULL,"
        "  author_persona VARCHAR(50),"
        "  content TEXT NOT NULL,"
        "  session_id VARCHAR(64),"
        "  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,"
        "  actor_type VARCHAR(20) NOT NULL DEFAULT 'user',"
        "  service_key_id VARCHAR(36),"
        "  service_key_name VARCHAR(100)"
        ")"
    )

    # ── Seed data ──
    conn.execute("INSERT INTO users (id, email) VALUES (?, 'op@x.com')", (_OPERATOR,))
    conn.execute("INSERT INTO users (id, email) VALUES (?, 'other@x.com')", (_OTHER,))
    conn.execute(
        "INSERT INTO organizations (id, name, slug) VALUES ('org-1', 'O', 'o')"
    )
    # One org-scoped project, one personal project.
    conn.execute(
        "INSERT INTO projects (id, name, org_id) VALUES ('proj-org', 'P', 'org-1')"
    )
    conn.execute(
        "INSERT INTO projects (id, name, org_id) VALUES ('proj-personal', 'P2', NULL)"
    )
    conn.execute("INSERT INTO tickets (id, project_id) VALUES ('tk-org', 'proj-org')")
    conn.execute(
        "INSERT INTO tickets (id, project_id) VALUES ('tk-personal', 'proj-personal')"
    )

    # Operator codex-reviewer comments on BOTH projects (legacy verdicts).
    conn.execute(
        "INSERT INTO ticket_comments (id, ticket_id, author_user_id, author_persona, "
        "content) VALUES ('c-op-org', 'tk-org', ?, 'codex-reviewer', 'VERIFIED-CLEAN')",
        (_OPERATOR,),
    )
    conn.execute(
        "INSERT INTO ticket_comments (id, ticket_id, author_user_id, author_persona, "
        "content) VALUES ('c-op-personal', 'tk-personal', ?, 'codex-reviewer', 'x')",
        (_OPERATOR,),
    )
    # A NON-operator codex-reviewer comment (forged / out-of-band).
    conn.execute(
        "INSERT INTO ticket_comments (id, ticket_id, author_user_id, author_persona, "
        "content) VALUES ('c-other', 'tk-org', ?, 'codex-reviewer', 'forged')",
        (_OTHER,),
    )
    # A plain operator note (not codex-reviewer) — must stay untrusted.
    conn.execute(
        "INSERT INTO ticket_comments (id, ticket_id, author_user_id, author_persona, "
        "content) VALUES ('c-note', 'tk-org', ?, 'atlas', 'note')",
        (_OPERATOR,),
    )

    conn.commit()
    conn.close()

    cfg = Config()
    cfg.set_main_option("script_location", "src/sessionfs/server/db/migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    command.stamp(cfg, "052")
    return db_path


def _cfg(db_path: Path) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", "src/sessionfs/server/db/migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{db_path}")
    return cfg


class TestMigration053:
    def test_upgrade_creates_column_table_and_indexes(self, migration_053_db_path):
        command.upgrade(_cfg(migration_053_db_path), "053")
        conn = sqlite3.connect(str(migration_053_db_path))

        cols = {
            r[1] for r in conn.execute("PRAGMA table_info('ticket_comments')").fetchall()
        }
        assert "verdict_trusted" in cols

        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "trusted_reviewers" in tables

        tr_cols = {
            r[1] for r in conn.execute("PRAGMA table_info('trusted_reviewers')").fetchall()
        }
        for col in (
            "id", "org_id", "project_id", "user_id", "service_key_id",
            "reviewer_persona", "is_active", "created_by_user_id",
            "created_at", "revoked_at",
        ):
            assert col in tr_cols, f"Missing column: {col}"

        indexes = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND tbl_name='trusted_reviewers'"
            ).fetchall()
        ]
        assert "idx_trusted_reviewer_project" in indexes
        assert "idx_trusted_reviewer_org" in indexes
        conn.close()

    def test_check_constraints_enforced(self, migration_053_db_path):
        command.upgrade(_cfg(migration_053_db_path), "053")
        conn = sqlite3.connect(str(migration_053_db_path))

        # Neither identity present → CHECK violation.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO trusted_reviewers (id, org_id, reviewer_persona, "
                "created_by_user_id) VALUES ('tr-bad1', 'org-1', 'codex-reviewer', ?)",
                (_OPERATOR,),
            )
            conn.commit()
        conn.rollback()

        # Neither scope present → CHECK violation.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO trusted_reviewers (id, user_id, reviewer_persona, "
                "created_by_user_id) VALUES ('tr-bad2', ?, 'codex-reviewer', ?)",
                (_OPERATOR, _OPERATOR),
            )
            conn.commit()
        conn.rollback()

        # Valid row → ok.
        conn.execute(
            "INSERT INTO trusted_reviewers (id, org_id, user_id, reviewer_persona, "
            "created_by_user_id) VALUES ('tr-ok', 'org-1', ?, 'codex-reviewer', ?)",
            (_OPERATOR, _OPERATOR),
        )
        conn.commit()
        conn.close()

    def test_backfill_grounds_on_operator_identity(self, migration_053_db_path):
        command.upgrade(_cfg(migration_053_db_path), "053")
        conn = sqlite3.connect(str(migration_053_db_path))

        def trusted(cid: str) -> int:
            return conn.execute(
                "SELECT verdict_trusted FROM ticket_comments WHERE id=?", (cid,)
            ).fetchone()[0]

        # Operator codex-reviewer comments → trusted.
        assert trusted("c-op-org") == 1
        assert trusted("c-op-personal") == 1
        # Non-operator codex-reviewer comment → stays untrusted (defanged).
        assert trusted("c-other") == 0
        # Operator non-reviewer note → untrusted.
        assert trusted("c-note") == 0
        conn.close()

    def test_seed_rows_scope_org_and_personal(self, migration_053_db_path):
        command.upgrade(_cfg(migration_053_db_path), "053")
        conn = sqlite3.connect(str(migration_053_db_path))
        rows = conn.execute(
            "SELECT org_id, project_id, user_id, reviewer_persona, is_active "
            "FROM trusted_reviewers ORDER BY id"
        ).fetchall()
        # One org-wide row (proj-org's org) + one personal-project row.
        scopes = {(r[0], r[1]) for r in rows}
        assert ("org-1", None) in scopes  # org-wide for the org-scoped project
        assert (None, "proj-personal") in scopes  # personal project direct
        # All seeded rows bind the operator user + codex-reviewer + active.
        for r in rows:
            assert r[2] == _OPERATOR
            assert r[3] == "codex-reviewer"
            assert r[4] == 1
        conn.close()

    def test_downgrade_removes_table_and_column(self, migration_053_db_path):
        cfg = _cfg(migration_053_db_path)
        command.upgrade(cfg, "053")
        command.downgrade(cfg, "052")
        conn = sqlite3.connect(str(migration_053_db_path))
        tables = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        ]
        assert "trusted_reviewers" not in tables
        cols = {
            r[1] for r in conn.execute("PRAGMA table_info('ticket_comments')").fetchall()
        }
        assert "verdict_trusted" not in cols
        conn.close()
