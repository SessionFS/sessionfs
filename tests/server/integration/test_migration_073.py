"""Migration 073 (GitHub installation ownership proof) round-trip.

The prior schema is the REAL SQLAlchemy metadata minus what 073 adds (the
github_claim_attempts table and five github_installations columns). Dialect
divergence is covered by `test_full_chain_on_postgres` when SFS_TEST_PG_URL
is set and by the `migrations-postgres` CI gate.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from sessionfs.server.db.models import Base
from tests.pg_helpers import reset_pg_schema

NEW_TABLES = {"github_claim_attempts", "github_webhook_deliveries"}
NEW_PR_COMMENT_COLUMNS = (
    "desired_body", "body_version", "published_version", "publish_lease", "publish_lease_until",
    "owner_user_id",
)
NEW_COLUMNS = (
    "account_id",
    "installer_github_user_id",
    "claim_verified_at",
    "claim_verified_github_user_id",
    "claim_verified_github_login",
    "claim_role",
)

_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)


def _cfg(path: Path) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{path}")
    return config


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


@pytest.fixture()
def pre_073_db(tmp_path: Path) -> Path:
    path = tmp_path / "m073.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    prior = [t for name, t in Base.metadata.tables.items() if name not in NEW_TABLES]
    Base.metadata.create_all(engine, tables=prior)
    engine.dispose()
    connection = sqlite3.connect(str(path))
    connection.execute("DROP INDEX uq_github_installations_user")
    for column in NEW_COLUMNS:
        connection.execute(f"ALTER TABLE github_installations DROP COLUMN {column}")
    for column in NEW_PR_COMMENT_COLUMNS:
        connection.execute(f"ALTER TABLE pr_comments DROP COLUMN {column}")
    connection.execute(
        "INSERT INTO users (id, email, tier, created_at, is_active, email_verified) "
        "VALUES ('u1', 'u1@example.com', 'pro', '2026-01-01 00:00:00', 1, 1)"
    )
    connection.execute(
        "INSERT INTO github_installations (id, user_id, account_login, account_type, "
        "auto_comment, include_trust_score, include_session_links) "
        "VALUES (42, 'u1', 'acme', 'Organization', 1, 1, 1)"
    )
    # Duplicate links for one user (possible before 073): the most recent
    # stays linked, older ones are detached with comments off.
    connection.execute(
        "INSERT INTO users (id, email, tier, created_at, is_active, email_verified) "
        "VALUES ('u2', 'u2@example.com', 'pro', '2026-01-01 00:00:00', 1, 1)"
    )
    for iid, created in ((51, "2026-01-01 00:00:00"), (52, "2026-03-01 00:00:00"), (53, "2026-02-01 00:00:00")):
        connection.execute(
            "INSERT INTO github_installations (id, user_id, account_login, account_type, "
            "auto_comment, include_trust_score, include_session_links, created_at) "
            "VALUES (?, 'u2', 'o', 'Organization', 1, 1, 1, ?)",
            (iid, created),
        )
    connection.execute(
        "INSERT INTO projects (id, name, git_remote_normalized, context_document, owner_id) "
        "VALUES ('p1', 'P', 'github.com/acme/a', '', 'u1')"
    )
    # Trust created through (possibly hijacked) pre-073 claims, plus rows
    # that must not change.
    for rid, remote, verified, method in (
        ("r1", "github.com/acme/a", 1, "github_app"),
        ("r2", "github.com/acme/b", 1, "github_app"),
        ("r3", "github.com/acme/c", 0, "owner_attested"),
        ("r4", "github.com/acme/d", 0, "legacy_backfill"),
    ):
        verified_row = method == "github_app"
        connection.execute(
            "INSERT INTO project_repos (id, project_id, git_remote_normalized, provider, "
            "provider_repo_id, is_primary, verified, verification_method) "
            "VALUES (?, 'p1', ?, ?, ?, 0, ?, ?)",
            (
                rid, remote,
                "github" if verified_row else None,
                f"id-{rid}" if verified_row else None,
                verified, method,
            ),
        )
    connection.execute(
        "INSERT INTO pr_comments (id, installation_id, repo_full_name, pr_number, "
        "comment_id, session_ids) VALUES ('c1', 42, 'acme/a', 1, 99, '[]')"
    )
    # A repo-reclaimed project with no repository left still carrying its
    # former remote, and a normal project.
    connection.execute(
        "INSERT INTO projects (id, name, git_remote_normalized, context_document, owner_id, "
        "repo_reclaimed_at) VALUES ('p-rec', 'R', 'github.com/acme/gone', '', 'u1', '2026-01-02')"
    )
    # A legacy project with a real remote but NO project_repos rows: the
    # backfill gives it an unverified primary row.
    connection.execute(
        "INSERT INTO projects (id, name, git_remote_normalized, context_document, owner_id) "
        "VALUES ('p-legacy', 'L', 'github.com/acme/legacy', '', 'u1')"
    )
    # A legacy project squatting a remote another project already holds in
    # project_repos: the legacy column loses and is nulled (and logged).
    connection.execute(
        "INSERT INTO projects (id, name, git_remote_normalized, context_document, owner_id) "
        "VALUES ('p-owner', 'O', 'github.com/acme/owned', '', 'u1')"
    )
    connection.execute(
        "INSERT INTO project_repos (id, project_id, git_remote_normalized, provider, "
        "provider_repo_id, is_primary, verified, verification_method) "
        "VALUES ('r5', 'p-owner', 'github.com/acme/squat', NULL, NULL, 1, 0, 'owner_attested')"
    )
    connection.execute(
        "INSERT INTO projects (id, name, git_remote_normalized, context_document, owner_id) "
        "VALUES ('p-squat', 'S', 'github.com/acme/squat', '', 'u1')"
    )
    # A 'reclaimed:' placeholder string (from the pre-073 displacement route
    # or a squatter): the namespace is gone, the value becomes NULL.
    connection.execute(
        "INSERT INTO projects (id, name, git_remote_normalized, context_document, owner_id) "
        "VALUES ('p-ph', 'PH', 'reclaimed:p-ph', '', 'u1')"
    )
    # A legacy project WITH repository rows whose legacy remote is held by
    # another project's project_repos row: the legacy column is re-aligned
    # to the project's own primary repository, not nulled (it still has
    # repositories).
    connection.execute(
        "INSERT INTO projects (id, name, git_remote_normalized, context_document, owner_id) "
        "VALUES ('p-real', 'RL', 'github.com/acme/held', '', 'u1')"
    )
    connection.execute(
        "INSERT INTO project_repos (id, project_id, git_remote_normalized, provider, "
        "provider_repo_id, is_primary, verified, verification_method) "
        "VALUES ('r6', 'p-real', 'github.com/acme/real-primary', NULL, NULL, 1, 0, 'owner_attested')"
    )
    # The project that legitimately holds the squatted remote as a
    # non-primary repo row (its own primary is a different remote).
    connection.execute(
        "INSERT INTO projects (id, name, git_remote_normalized, context_document, owner_id) "
        "VALUES ('p-holds', 'H', 'github.com/acme/holds-own', '', 'u1')"
    )
    connection.execute(
        "INSERT INTO project_repos (id, project_id, git_remote_normalized, provider, "
        "provider_repo_id, is_primary, verified, verification_method) "
        "VALUES ('r7', 'p-holds', 'github.com/acme/held', NULL, NULL, 0, 0, 'owner_attested')"
    )
    connection.execute(
        "INSERT INTO project_repos (id, project_id, git_remote_normalized, provider, "
        "provider_repo_id, is_primary, verified, verification_method) "
        "VALUES ('r8', 'p-holds', 'github.com/acme/holds-own', NULL, NULL, 1, 0, 'owner_attested')"
    )
    # A merge tombstone whose former remote now lives in another project's
    # project_repos row (the merge moved the repositories away but left the
    # legacy column). 073 nulls the legacy column without marking the
    # tombstone repo-reclaimed; the downgrade must NOT relabel it 'reclaimed:'.
    connection.execute(
        "INSERT INTO projects (id, name, git_remote_normalized, context_document, owner_id, "
        "merged_into_project_id) VALUES ('p-tomb', 'T', 'github.com/acme/tombstone', '', 'u1', 'p-target')"
    )
    connection.execute(
        "INSERT INTO projects (id, name, git_remote_normalized, context_document, owner_id) "
        "VALUES ('p-target', 'TG', 'github.com/acme/target-own', '', 'u1')"
    )
    connection.execute(
        "INSERT INTO project_repos (id, project_id, git_remote_normalized, provider, "
        "provider_repo_id, is_primary, verified, verification_method) "
        "VALUES ('r-tomb', 'p-target', 'github.com/acme/tombstone', NULL, NULL, 0, 0, 'owner_attested')"
    )
    connection.execute(
        "INSERT INTO project_repos (id, project_id, git_remote_normalized, provider, "
        "provider_repo_id, is_primary, verified, verification_method) "
        "VALUES ('r-target', 'p-target', 'github.com/acme/target-own', NULL, NULL, 1, 0, 'owner_attested')"
    )
    connection.commit()
    connection.close()
    command.stamp(_cfg(path), "072")
    return path


def test_prior_schema_lacks_073_objects(pre_073_db: Path) -> None:
    connection = sqlite3.connect(str(pre_073_db))
    assert not (NEW_TABLES & _tables(connection))
    assert not (set(NEW_COLUMNS) & _columns(connection, "github_installations"))
    assert not (set(NEW_PR_COMMENT_COLUMNS) & _columns(connection, "pr_comments"))
    connection.close()


def test_upgrade_revokes_github_app_repo_trust_without_unlinking(pre_073_db: Path) -> None:
    command.upgrade(_cfg(pre_073_db), "073")
    connection = sqlite3.connect(str(pre_073_db))
    rows = connection.execute(
        "SELECT id, verified, verification_method, provider, provider_repo_id "
        "FROM project_repos WHERE id IN ('r1', 'r2', 'r3', 'r4') ORDER BY id"
    ).fetchall()
    connection.close()
    # Revoked rows also lose the provider identity derived from the
    # unproven claim (it would block the real owner after a rename).
    assert rows == [
        ("r1", 0, "owner_attested", None, None),
        ("r2", 0, "owner_attested", None, None),
        ("r3", 0, "owner_attested", None, None),
        ("r4", 0, "legacy_backfill", None, None),
    ]


def test_upgrade_dedupes_links_then_enforces_one_per_user(pre_073_db: Path) -> None:
    command.upgrade(_cfg(pre_073_db), "073")
    connection = sqlite3.connect(str(pre_073_db))
    rows = connection.execute(
        "SELECT id, user_id, auto_comment FROM github_installations WHERE id IN (51, 52, 53) "
        "ORDER BY id"
    ).fetchall()
    assert rows == [(51, None, 0), (52, "u2", 1), (53, None, 0)]
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute("UPDATE github_installations SET user_id = 'u2' WHERE id = 51")
    connection.close()


def test_upgrade_keeps_pr_comments_with_defaults(pre_073_db: Path) -> None:
    command.upgrade(_cfg(pre_073_db), "073")
    connection = sqlite3.connect(str(pre_073_db))
    row = connection.execute(
        "SELECT comment_id, desired_body, body_version, published_version, owner_user_id "
        "FROM pr_comments"
    ).fetchone()
    indexes = {r[1] for r in connection.execute("PRAGMA index_list(pr_comments)")}
    connection.close()
    assert row == (99, None, 0, 0, None)
    assert "idx_pr_comments_installation_repo_pr" in indexes


def test_upgrade_downgrade_upgrade(pre_073_db: Path) -> None:
    config = _cfg(pre_073_db)
    command.upgrade(config, "073")
    connection = sqlite3.connect(str(pre_073_db))
    assert NEW_TABLES <= _tables(connection)
    assert set(NEW_COLUMNS) <= _columns(connection, "github_installations")
    # Existing claims become legacy (unverified) claims and keep their owner.
    row = connection.execute(
        "SELECT user_id, claim_verified_at, claim_role FROM github_installations WHERE id = 42"
    ).fetchone()
    assert row == ("u1", None, None)
    # Constraints on the attempts table.
    insert = (
        "INSERT INTO github_claim_attempts (id, state_hash, user_id, nonce_hash, "
        "pkce_verifier_hash, flow, status, created_at, expires_at) "
        "VALUES (?, ?, 'u1', 'n', 'p', ?, ?, '2026-01-01', '2026-01-02')"
    )
    connection.execute(insert, ("a1", "h1", "install", "pending"))
    # 'processing' (consumed, callback still working) is a valid state.
    connection.execute(insert, ("a5", "h5", "install", "processing"))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(insert, ("a2", "h1", "install", "pending"))  # state_hash unique
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(insert, ("a3", "h3", "bogus", "pending"))
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(insert, ("a4", "h4", "authorize", "expired"))
    connection.commit()
    connection.close()

    command.downgrade(config, "072")
    connection = sqlite3.connect(str(pre_073_db))
    assert not (NEW_TABLES & _tables(connection))
    assert not (set(NEW_PR_COMMENT_COLUMNS) & _columns(connection, "pr_comments"))
    assert not (set(NEW_COLUMNS) & _columns(connection, "github_installations"))
    assert connection.execute(
        "SELECT user_id FROM github_installations WHERE id = 42"
    ).fetchone() == ("u1",)
    connection.close()

    command.upgrade(config, "073")
    connection = sqlite3.connect(str(pre_073_db))
    assert set(NEW_COLUMNS) <= _columns(connection, "github_installations")
    connection.close()


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database to run the full chain",
)
def test_full_chain_on_postgres() -> None:
    """001..head, down to 069 and back, on REAL PostgreSQL."""
    reset_pg_schema()  # the chain runs from an empty database
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", os.environ["SFS_TEST_PG_URL"])
    command.upgrade(config, "head")
    command.downgrade(config, "072")
    command.upgrade(config, "head")


def test_upgrade_relocates_reclaimed_project_remotes(pre_073_db: Path) -> None:
    command.upgrade(_cfg(pre_073_db), "073")
    connection = sqlite3.connect(str(pre_073_db))
    rows = dict(connection.execute("SELECT id, git_remote_normalized FROM projects").fetchall())
    connection.close()
    # Reclaimed projects are structural: NULL remote + repo_reclaimed_at.
    assert rows["p-rec"] is None
    assert rows["p1"] == "github.com/acme/a"  # has repositories: untouched
    assert rows["p-owner"] == "github.com/acme/owned"  # has repositories
    # The 'reclaimed:' placeholder namespace is gone.
    assert rows["p-ph"] is None


def test_upgrade_backfills_legacy_projects_without_repo_rows(pre_073_db: Path) -> None:
    command.upgrade(_cfg(pre_073_db), "073")
    connection = sqlite3.connect(str(pre_073_db))
    rows = dict(connection.execute("SELECT id, git_remote_normalized FROM projects").fetchall())
    repo = connection.execute(
        "SELECT id, project_id, git_remote_normalized, is_primary, verified, "
        "verification_method FROM project_repos WHERE project_id = 'p-legacy'"
    ).fetchone()
    connection.close()
    # The legacy project gains an unverified primary row and keeps its
    # legacy column (the two agree).
    assert repo is not None
    assert repo[1:] == ("p-legacy", "github.com/acme/legacy", 1, 0, "legacy_backfill")
    assert rows["p-legacy"] == "github.com/acme/legacy"


def test_upgrade_nulls_legacy_remote_conflicting_with_repo_holder(pre_073_db: Path) -> None:
    command.upgrade(_cfg(pre_073_db), "073")
    connection = sqlite3.connect(str(pre_073_db))
    rows = dict(connection.execute("SELECT id, git_remote_normalized FROM projects").fetchall())
    squat_repos = connection.execute(
        "SELECT COUNT(*) FROM project_repos WHERE project_id = 'p-squat'"
    ).fetchone()[0]
    holder = connection.execute(
        "SELECT COUNT(*) FROM project_repos WHERE git_remote_normalized = 'github.com/acme/squat'"
    ).fetchone()[0]
    connection.close()
    # The squatter gets no backfill row and its legacy column is nulled;
    # the real holder keeps the only row for the remote.
    assert squat_repos == 0
    assert rows["p-squat"] is None
    assert holder == 1


def test_upgrade_realigns_squatted_legacy_remote_when_project_has_repos(pre_073_db: Path) -> None:
    command.upgrade(_cfg(pre_073_db), "073")
    connection = sqlite3.connect(str(pre_073_db))
    real = connection.execute(
        "SELECT git_remote_normalized, repo_reclaimed_at FROM projects WHERE id = 'p-real'"
    ).fetchone()
    holder = connection.execute(
        "SELECT git_remote_normalized FROM projects WHERE id = 'p-holds'"
    ).fetchone()
    held = connection.execute(
        "SELECT COUNT(*) FROM project_repos WHERE git_remote_normalized = 'github.com/acme/held'"
    ).fetchone()[0]
    real_repos = connection.execute(
        "SELECT COUNT(*) FROM project_repos WHERE project_id = 'p-real'"
    ).fetchone()[0]
    connection.close()
    # The squatted legacy value loses: it is re-aligned to the project's own
    # primary repository (not nulled — the project still has repositories,
    # so it must not be marked reclaimed).
    assert real == ("github.com/acme/real-primary", None)
    # The real holder keeps its remote and its own primary untouched.
    assert holder == ("github.com/acme/holds-own",)
    assert held == 1
    assert real_repos == 1


def test_upgrade_sets_repo_reclaimed_for_nulled_remotes(pre_073_db: Path) -> None:
    command.upgrade(_cfg(pre_073_db), "073")
    connection = sqlite3.connect(str(pre_073_db))
    rows = {
        id_: (remote, reclaimed)
        for id_, remote, reclaimed in connection.execute(
            "SELECT id, git_remote_normalized, repo_reclaimed_at FROM projects"
        ).fetchall()
    }
    connection.close()
    # A conflict loser (p-squat) and an unmarked 'reclaimed:' placeholder
    # (p-ph) end with NULL remote AND repo_reclaimed_at set — never a NULL
    # remote with repo_reclaimed_at still NULL, which would violate the
    # active-project ≥1-repository invariant while being unreachable by
    # remote (tk_b6c5e184ba8d4aa1).
    assert rows["p-squat"][0] is None
    assert rows["p-squat"][1] is not None
    assert rows["p-ph"][0] is None
    assert rows["p-ph"][1] is not None
    # A project that still has repositories keeps its remote and is not
    # marked reclaimed.
    assert rows["p-owner"][0] == "github.com/acme/owned"
    assert rows["p-owner"][1] is None


def test_upgrade_downgrade_recovers_not_null_remotes(pre_073_db: Path) -> None:
    config = _cfg(pre_073_db)
    command.upgrade(config, "073")
    command.downgrade(config, "072")
    connection = sqlite3.connect(str(pre_073_db))
    nulls = connection.execute(
        "SELECT COUNT(*) FROM projects WHERE git_remote_normalized IS NULL"
    ).fetchone()[0]
    rec = connection.execute(
        "SELECT git_remote_normalized FROM projects WHERE id = 'p-rec'"
    ).fetchone()[0]
    connection.close()
    # Downgrade restores the pre-073 representation before re-imposing
    # NOT NULL.
    assert nulls == 0
    assert rec == "reclaimed:p-rec"


def test_downgrade_does_not_relabel_merge_tombstone_as_reclaimed(pre_073_db: Path) -> None:
    config = _cfg(pre_073_db)
    command.upgrade(config, "073")
    connection = sqlite3.connect(str(pre_073_db))
    tomb = connection.execute(
        "SELECT git_remote_normalized, merged_into_project_id, repo_reclaimed_at "
        "FROM projects WHERE id = 'p-tomb'"
    ).fetchone()
    connection.close()
    # After upgrade the tombstone's remote is nulled (its former remote is
    # held by p-target) but it is never marked repo-reclaimed.
    assert tomb == (None, "p-target", None)

    command.downgrade(config, "072")
    connection = sqlite3.connect(str(pre_073_db))
    tomb = connection.execute(
        "SELECT git_remote_normalized, merged_into_project_id FROM projects WHERE id = 'p-tomb'"
    ).fetchone()
    nulls = connection.execute(
        "SELECT COUNT(*) FROM projects WHERE git_remote_normalized IS NULL"
    ).fetchone()[0]
    connection.close()
    # A merge tombstone gets a synthetic placeholder, NOT 'reclaimed:', so a
    # legacy access path can never mistake a merged project for a reclaimable
    # one. Every project still has a non-NULL remote for the NOT NULL column.
    assert tomb == ("merged:p-tomb", "p-target")
    assert nulls == 0


def test_reupgrade_restores_tombstone_null_remote(pre_073_db: Path) -> None:
    config = _cfg(pre_073_db)
    command.upgrade(config, "073")
    command.downgrade(config, "072")
    command.upgrade(config, "073")
    connection = sqlite3.connect(str(pre_073_db))
    tomb = connection.execute(
        "SELECT git_remote_normalized, merged_into_project_id, repo_reclaimed_at "
        "FROM projects WHERE id = 'p-tomb'"
    ).fetchone()
    backfill = connection.execute(
        "SELECT COUNT(*) FROM project_repos WHERE project_id = 'p-tomb'"
    ).fetchone()[0]
    connection.close()
    # The downgrade's placeholder is cleaned up on re-upgrade, and the
    # tombstone never gains a bogus backfill row.
    assert tomb == (None, "p-target", None)
    assert backfill == 0
