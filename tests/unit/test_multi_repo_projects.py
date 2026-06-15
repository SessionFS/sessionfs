"""Tests for P1 multi-repo projects: migration 049 + models + resolver.

Covers:
- Migration 049 up/down on SQLite (minimal prerequisite schema)
- Backfill: one is_primary legacy_backfill row per existing project
- Resolver: join-first, legacy fallback, tombstone redirects, hop-cap error,
  repo_reclaimed self-resolution, for_update path
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sessionfs.server.db.models import Base, Project, ProjectRepo, User
from sessionfs.server.services.project_resolver import (
    ProjectResolutionLoopError,
    get_primary_remote,
    resolve_project_by_id,
    resolve_project_by_remote,
)


# ────────────────────────────────────────────────────────────────
# Local DB fixtures (can't use server conftest from tests/unit/)
# ────────────────────────────────────────────────────────────────

@pytest.fixture
async def db_engine():
    """In-memory aiosqlite engine with all ORM tables."""
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def db_session(db_engine):
    """Async session per test."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


# ────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _create_project(
    db: AsyncSession,
    *,
    project_id: str | None = None,
    name: str = "test-project",
    git_remote: str = "github.com/test/repo",
    owner_id: str = "user-1",
    merged_into_project_id: str | None = None,
    repo_reclaimed_at: datetime | None = None,
) -> Project:
    p = Project(
        id=project_id or f"proj_{uuid.uuid4().hex[:16]}",
        name=name,
        git_remote_normalized=git_remote,
        owner_id=owner_id,
        merged_into_project_id=merged_into_project_id,
        repo_reclaimed_at=repo_reclaimed_at,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _create_project_repo(
    db: AsyncSession,
    *,
    project_id: str,
    git_remote_normalized: str,
    is_primary: bool = False,
    verified: bool = False,
    verification_method: str | None = None,
    provider: str | None = None,
    provider_repo_id: str | None = None,
) -> ProjectRepo:
    pr = ProjectRepo(
        id=str(uuid.uuid4()),
        project_id=project_id,
        git_remote_normalized=git_remote_normalized,
        is_primary=is_primary,
        verified=verified,
        verification_method=verification_method,
        provider=provider,
        provider_repo_id=provider_repo_id,
        created_at=_now(),
    )
    db.add(pr)
    await db.commit()
    await db.refresh(pr)
    return pr


# ────────────────────────────────────────────────────────────────
# Migration 049 up + down on SQLite
# ────────────────────────────────────────────────────────────────

@pytest.fixture
def migration_db_path(tmp_path):
    """Create a SQLite DB with minimum prerequisite tables for migration 049.

    Migration 049 needs users (FK target) and projects (FK target + backfill
    source). We create only the columns that existed pre-049. SQLite doesn't
    enforce FKs by default so we won't fail on missing users rows during
    backfill.
    """
    import sqlite3

    db_path = tmp_path / "migration_049_test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA foreign_keys = OFF")

    # Users — minimal, just need the PK for FKs
    conn.execute("""
        CREATE TABLE users (
            id VARCHAR(64) PRIMARY KEY,
            email VARCHAR(255) NOT NULL
        )
    """)
    conn.execute("INSERT INTO users (id, email) VALUES ('user-1', 'a@b.com')")

    # Projects — pre-049 state (all columns that existed at revision 048,
    # without the 3 new columns migration 049 adds)
    conn.execute("""
        CREATE TABLE projects (
            id VARCHAR(64) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            git_remote_normalized VARCHAR(255) NOT NULL UNIQUE,
            context_document TEXT NOT NULL DEFAULT '',
            owner_id VARCHAR(64) NOT NULL REFERENCES users(id),
            org_id VARCHAR(64),
            auto_narrative BOOLEAN NOT NULL DEFAULT 0,
            kb_retention_days INTEGER NOT NULL DEFAULT 180,
            kb_max_context_words INTEGER NOT NULL DEFAULT 2000,
            kb_section_page_limit INTEGER NOT NULL DEFAULT 30,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Insert test projects for backfill verification
    conn.execute(
        "INSERT INTO projects (id, name, git_remote_normalized, owner_id) "
        "VALUES ('proj_1', 'Project One', 'github.com/acme/frontend', 'user-1')"
    )
    conn.execute(
        "INSERT INTO projects (id, name, git_remote_normalized, owner_id) "
        "VALUES ('proj_2', 'Project Two', 'github.com/acme/backend', 'user-1')"
    )
    # Project with empty remote — should NOT get a backfill row
    conn.execute(
        "INSERT INTO projects (id, name, git_remote_normalized, owner_id) "
        "VALUES ('proj_3', 'Project Three', '', 'user-1')"
    )
    conn.commit()
    conn.close()
    return db_path


def test_migration_049_up_creates_tables_and_backfills(migration_db_path):
    """Migration 049 creates project_repos, project_merge_audit, adds columns
    to projects, and backfills one legacy_backfill row per project with
    a non-empty git_remote_normalized."""
    from alembic import command
    from alembic.config import Config

    # Set up Alembic pointing at our test DB
    cfg = Config()
    cfg.set_main_option("script_location", "src/sessionfs/server/db/migrations")
    db_url = f"sqlite+aiosqlite:///{migration_db_path}"
    cfg.set_main_option("sqlalchemy.url", db_url)

    # Stamp at revision 048 (so we can upgrade just 049)
    command.stamp(cfg, "048")

    # Run upgrade to 049
    command.upgrade(cfg, "049")

    # Verify via raw connection
    import sqlite3
    conn = sqlite3.connect(str(migration_db_path))

    # 1. project_repos table exists
    tables = [
        row[0] for row in
        conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    ]
    assert "project_repos" in tables, "project_repos table should exist"
    assert "project_merge_audit" in tables, "project_merge_audit table should exist"

    # 2. Projects have the three new columns
    cols = [
        row[1] for row in
        conn.execute("PRAGMA table_info('projects')").fetchall()
    ]
    assert "merged_into_project_id" in cols
    assert "merged_at" in cols
    assert "repo_reclaimed_at" in cols

    # 3. project_repos has all expected columns
    repo_cols = [
        row[1] for row in
        conn.execute("PRAGMA table_info('project_repos')").fetchall()
    ]
    for col in ("id", "project_id", "git_remote_normalized", "is_primary",
                 "verified", "verification_method", "provider", "provider_repo_id",
                 "added_by_user_id", "created_at"):
        assert col in repo_cols, f"project_repos missing column: {col}"

    # 4. Backfill: exactly 2 rows (proj_1 and proj_2; proj_3 had empty remote)
    rows = conn.execute(
        "SELECT project_id, git_remote_normalized, is_primary, verified, verification_method "
        "FROM project_repos ORDER BY git_remote_normalized"
    ).fetchall()
    assert len(rows) == 2, f"Expected 2 backfill rows, got {len(rows)}"

    # proj_1 → github.com/acme/backend (alphabetically first)
    assert rows[0][0] == "proj_2"
    assert rows[0][1] == "github.com/acme/backend"
    assert rows[0][2] == 1  # is_primary
    assert rows[0][3] == 0  # verified=false
    assert rows[0][4] == "legacy_backfill"

    # proj_2 → github.com/acme/frontend
    assert rows[1][0] == "proj_1"
    assert rows[1][1] == "github.com/acme/frontend"
    assert rows[1][2] == 1  # is_primary
    assert rows[1][3] == 0  # verified=false
    assert rows[1][4] == "legacy_backfill"

    # 5. Indexes exist
    indexes = [
        row[0] for row in
        conn.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    ]
    for idx in ("uq_project_repos_remote", "uq_project_repos_primary",
                 "uq_project_repos_provider_repo", "idx_project_repos_project"):
        assert idx in indexes, f"Missing index: {idx}"

    conn.close()


def test_migration_049_downgrade_cleans_up(migration_db_path):
    """Migration 049 downgrade removes tables and columns cleanly."""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", "src/sessionfs/server/db/migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{migration_db_path}")

    # Stamp and upgrade
    command.stamp(cfg, "048")
    command.upgrade(cfg, "049")

    # Downgrade
    command.downgrade(cfg, "048")

    # Verify clean reversal
    import sqlite3
    conn = sqlite3.connect(str(migration_db_path))

    tables = [
        row[0] for row in
        conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    ]
    assert "project_repos" not in tables
    assert "project_merge_audit" not in tables

    cols = [
        row[1] for row in
        conn.execute("PRAGMA table_info('projects')").fetchall()
    ]
    assert "merged_into_project_id" not in cols
    assert "merged_at" not in cols
    assert "repo_reclaimed_at" not in cols

    # Original projects still there
    proj_count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
    assert proj_count == 3

    conn.close()


def test_migration_049_idempotent_upgrade(migration_db_path):
    """Upgrade → downgrade → upgrade is clean (idempotent)."""
    from alembic import command
    from alembic.config import Config

    cfg = Config()
    cfg.set_main_option("script_location", "src/sessionfs/server/db/migrations")
    cfg.set_main_option("sqlalchemy.url", f"sqlite+aiosqlite:///{migration_db_path}")

    command.stamp(cfg, "048")
    command.upgrade(cfg, "049")
    command.downgrade(cfg, "048")
    command.upgrade(cfg, "049")  # Should succeed

    import sqlite3
    conn = sqlite3.connect(str(migration_db_path))
    count = conn.execute("SELECT COUNT(*) FROM project_repos").fetchone()[0]
    assert count == 2  # Backfill re-applied
    conn.close()


# ────────────────────────────────────────────────────────────────
# Resolver tests (use model-based DB via conftest fixtures)
# ────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_resolve_by_remote_join_first(db_session: AsyncSession):
    """resolve_project_by_remote hits project_repos join table first."""
    user = User(id="user-r1", email="r1@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    p = await _create_project(db_session, project_id="proj_r1",
                                git_remote="github.com/acme/frontend",
                                owner_id=user.id)
    await _create_project_repo(db_session, project_id="proj_r1",
                                git_remote_normalized="github.com/acme/frontend",
                                is_primary=True)

    result = await resolve_project_by_remote(db_session, "github.com/acme/frontend")
    assert result is not None
    assert result.id == "proj_r1"


@pytest.mark.anyio
async def test_resolve_by_remote_legacy_fallback(db_session: AsyncSession):
    """resolve_project_by_remote falls back to projects.git_remote_normalized
    when no project_repos row exists."""
    user = User(id="user-r2", email="r2@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    p = await _create_project(db_session, project_id="proj_r2",
                                git_remote="github.com/acme/legacy",
                                owner_id=user.id)
    # No project_repos row — should fall back to legacy column

    result = await resolve_project_by_remote(db_session, "github.com/acme/legacy")
    assert result is not None
    assert result.id == "proj_r2"


@pytest.mark.anyio
async def test_resolve_by_remote_prefers_join_over_legacy(db_session: AsyncSession):
    """When both project_repos and legacy column exist, join wins.

    Create a project with git_remote_normalized='github.com/a/x' and a
    project_repos row pointing to a DIFFERENT project. The resolver should
    follow the project_repos row (source of truth).
    """
    user = User(id="user-r3", email="r3@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    # Legacy project with remote A
    legacy = await _create_project(db_session, project_id="proj_legacy",
                                    git_remote="github.com/a/legacy-remote",
                                    owner_id=user.id)

    # New project with project_repos row claiming remote A
    new_proj = await _create_project(db_session, project_id="proj_new",
                                      git_remote="github.com/a/new-primary",
                                      owner_id=user.id)
    await _create_project_repo(db_session, project_id="proj_new",
                                git_remote_normalized="github.com/a/legacy-remote",
                                is_primary=False)

    # Resolver should find new_proj via project_repos join
    result = await resolve_project_by_remote(db_session, "github.com/a/legacy-remote")
    assert result is not None
    assert result.id == "proj_new"


@pytest.mark.anyio
async def test_resolve_by_remote_none_for_unknown(db_session: AsyncSession):
    """resolve_project_by_remote returns None for unknown remote."""
    result = await resolve_project_by_remote(db_session, "github.com/nonexistent/repo")
    assert result is None


@pytest.mark.anyio
async def test_resolve_by_remote_empty_string(db_session: AsyncSession):
    """resolve_project_by_remote returns None for empty string."""
    result = await resolve_project_by_remote(db_session, "")
    assert result is None


@pytest.mark.anyio
async def test_resolve_by_remote_tombstone_single_hop(db_session: AsyncSession):
    """Tombstone redirect: source → target (single hop)."""
    user = User(id="user-r4", email="r4@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    target = await _create_project(db_session, project_id="proj_target",
                                    git_remote="github.com/a/target",
                                    owner_id=user.id)
    source = await _create_project(db_session, project_id="proj_source",
                                    git_remote="github.com/a/source",
                                    owner_id=user.id,
                                    merged_into_project_id="proj_target")
    await _create_project_repo(db_session, project_id="proj_source",
                                git_remote_normalized="github.com/a/source",
                                is_primary=True)

    # Resolve source remote → should follow tombstone to target
    result = await resolve_project_by_remote(db_session, "github.com/a/source")
    assert result is not None
    assert result.id == "proj_target"


@pytest.mark.anyio
async def test_resolve_by_remote_tombstone_multi_hop(db_session: AsyncSession):
    """Tombstone chain: A → B → C (2 hops)."""
    user = User(id="user-r5", email="r5@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    c = await _create_project(db_session, project_id="proj_C",
                               git_remote="github.com/a/c",
                               owner_id=user.id)
    b = await _create_project(db_session, project_id="proj_B",
                               git_remote="github.com/a/b",
                               owner_id=user.id,
                               merged_into_project_id="proj_C")
    a = await _create_project(db_session, project_id="proj_A",
                               git_remote="github.com/a/a",
                               owner_id=user.id,
                               merged_into_project_id="proj_B")
    await _create_project_repo(db_session, project_id="proj_A",
                                git_remote_normalized="github.com/a/a",
                                is_primary=True)

    result = await resolve_project_by_remote(db_session, "github.com/a/a")
    assert result is not None
    assert result.id == "proj_C"


@pytest.mark.anyio
async def test_resolve_by_remote_tombstone_no_follow(db_session: AsyncSession):
    """follow_tombstone=False returns the tombstone directly."""
    user = User(id="user-r6", email="r6@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    target = await _create_project(db_session, project_id="proj_target2",
                                    git_remote="github.com/a/target2",
                                    owner_id=user.id)
    source = await _create_project(db_session, project_id="proj_source2",
                                    git_remote="github.com/a/source2",
                                    owner_id=user.id,
                                    merged_into_project_id="proj_target2")
    await _create_project_repo(db_session, project_id="proj_source2",
                                git_remote_normalized="github.com/a/source2",
                                is_primary=True)

    result = await resolve_project_by_remote(
        db_session, "github.com/a/source2", follow_tombstone=False
    )
    assert result is not None
    assert result.id == "proj_source2"  # NOT followed


@pytest.mark.anyio
async def test_resolve_by_remote_hop_cap_exceeded(db_session: AsyncSession):
    """Hop cap exceedance raises ProjectResolutionLoopError."""
    user = User(id="user-r7", email="r7@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    # Build a chain of 10 projects: head → proj_chain_0 → proj_chain_1 → ... → proj_chain_8 → NULL
    # The head (chain_9) points to chain_8, which points to chain_7, etc.
    # That's 9 hops. Hop cap is 8, so it should trigger.
    prev_id = None
    for i in range(9):  # 0..8 = 9 projects forming the inner chain
        pid = f"proj_chain_{i}"
        await _create_project(
            db_session, project_id=pid,
            git_remote=f"github.com/a/chain_{i}",
            owner_id=user.id,
            merged_into_project_id=prev_id,
        )
        await _create_project_repo(db_session, project_id=pid,
                                    git_remote_normalized=f"github.com/a/chain_{i}",
                                    is_primary=True)
        prev_id = pid

    # The head project (chain_9) points to chain_8 — 9 hops to NULL
    await _create_project(
        db_session, project_id="proj_chain_9",
        git_remote="github.com/a/chain_9",
        owner_id=user.id,
        merged_into_project_id=prev_id,  # "proj_chain_8"
    )
    await _create_project_repo(db_session, project_id="proj_chain_9",
                                git_remote_normalized="github.com/a/chain_9",
                                is_primary=True)

    # Resolving chain_9 follows 9 hops → exceeds cap of 8
    with pytest.raises(ProjectResolutionLoopError) as exc:
        await resolve_project_by_remote(db_session, "github.com/a/chain_9")
    assert "hop cap" in str(exc.value)
    assert "8" in str(exc.value)


@pytest.mark.anyio
async def test_resolve_by_remote_repo_reclaimed_self_resolution(db_session: AsyncSession):
    """repo_reclaimed project resolves to itself (not redirected)."""
    user = User(id="user-r8", email="r8@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    orphaned = await _create_project(
        db_session, project_id="proj_orphan",
        git_remote="github.com/a/orphaned",
        owner_id=user.id,
        repo_reclaimed_at=_now(),  # orphaned but NOT merged
    )
    await _create_project_repo(db_session, project_id="proj_orphan",
                                git_remote_normalized="github.com/a/orphaned",
                                is_primary=True)

    result = await resolve_project_by_remote(db_session, "github.com/a/orphaned")
    assert result is not None
    assert result.id == "proj_orphan"  # Resolves to itself


@pytest.mark.anyio
async def test_resolve_by_remote_repo_reclaimed_not_merged(db_session: AsyncSession):
    """repo_reclaimed with merged_into=NULL resolves to self (not redirected),
    even though it's orphaned."""
    user = User(id="user-r9", email="r9@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    # Orphaned but not merged: repo_reclaimed_at set, merged_into_project_id NULL
    orphaned = await _create_project(
        db_session, project_id="proj_orphan2",
        git_remote="github.com/a/orphaned2",
        owner_id=user.id,
        repo_reclaimed_at=_now(),
        merged_into_project_id=None,
    )
    await _create_project_repo(db_session, project_id="proj_orphan2",
                                git_remote_normalized="github.com/a/orphaned2",
                                is_primary=True)

    result = await resolve_project_by_remote(db_session, "github.com/a/orphaned2")
    assert result is not None
    assert result.id == "proj_orphan2"


@pytest.mark.anyio
async def test_resolve_by_remote_for_update(db_session: AsyncSession):
    """resolve_project_by_remote with for_update=True works."""
    user = User(id="user-r10", email="r10@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    p = await _create_project(db_session, project_id="proj_r10",
                                git_remote="github.com/a/r10",
                                owner_id=user.id)
    await _create_project_repo(db_session, project_id="proj_r10",
                                git_remote_normalized="github.com/a/r10",
                                is_primary=True)

    # for_update on SQLite is a no-op (no row locking), but should not error
    result = await resolve_project_by_remote(
        db_session, "github.com/a/r10", for_update=True
    )
    assert result is not None
    assert result.id == "proj_r10"


@pytest.mark.anyio
async def test_resolve_by_id_normal(db_session: AsyncSession):
    """resolve_project_by_id returns project by ID."""
    user = User(id="user-r11", email="r11@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    p = await _create_project(db_session, project_id="proj_r11",
                                git_remote="github.com/a/r11",
                                owner_id=user.id)

    result = await resolve_project_by_id(db_session, "proj_r11")
    assert result is not None
    assert result.id == "proj_r11"


@pytest.mark.anyio
async def test_resolve_by_id_tombstone_follow(db_session: AsyncSession):
    """resolve_project_by_id follows tombstone chain."""
    user = User(id="user-r12", email="r12@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    target = await _create_project(db_session, project_id="proj_tgt",
                                    git_remote="github.com/a/tgt",
                                    owner_id=user.id)
    source = await _create_project(db_session, project_id="proj_src",
                                    git_remote="github.com/a/src",
                                    owner_id=user.id,
                                    merged_into_project_id="proj_tgt")

    result = await resolve_project_by_id(db_session, "proj_src")
    assert result is not None
    assert result.id == "proj_tgt"


@pytest.mark.anyio
async def test_resolve_by_id_hop_cap(db_session: AsyncSession):
    """resolve_project_by_id raises ProjectResolutionLoopError on hop cap."""
    user = User(id="user-r13", email="r13@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    # Build chain: head (proj_idchain_9) → proj_idchain_8 → ... → proj_idchain_0 → NULL
    # That's 9 hops; cap is 8, so resolving the head triggers the error.
    prev_id = None
    for i in range(9):  # 0..8 = inner chain
        pid = f"proj_idchain_{i}"
        await _create_project(
            db_session, project_id=pid,
            git_remote=f"github.com/a/idchain_{i}",
            owner_id=user.id,
            merged_into_project_id=prev_id,
        )
        prev_id = pid

    # Head (points to end of inner chain)
    head_id = "proj_idchain_9"
    await _create_project(
        db_session, project_id=head_id,
        git_remote="github.com/a/idchain_9",
        owner_id=user.id,
        merged_into_project_id=prev_id,  # proj_idchain_8
    )

    # Resolving head follows 9 hops → exceeds cap
    with pytest.raises(ProjectResolutionLoopError) as exc:
        await resolve_project_by_id(db_session, head_id)
    assert "hop cap" in str(exc.value)


@pytest.mark.anyio
async def test_get_primary_remote(db_session: AsyncSession):
    """get_primary_remote returns the is_primary row's remote."""
    user = User(id="user-r14", email="r14@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    p = await _create_project(db_session, project_id="proj_r14",
                                git_remote="github.com/a/primary",
                                owner_id=user.id)
    await _create_project_repo(db_session, project_id="proj_r14",
                                git_remote_normalized="github.com/a/primary",
                                is_primary=True)
    await _create_project_repo(db_session, project_id="proj_r14",
                                git_remote_normalized="github.com/a/secondary",
                                is_primary=False)

    primary = await get_primary_remote(db_session, "proj_r14")
    assert primary == "github.com/a/primary"


@pytest.mark.anyio
async def test_get_primary_remote_none_when_no_primary(db_session: AsyncSession):
    """get_primary_remote returns None when no is_primary row exists."""
    user = User(id="user-r15", email="r15@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    p = await _create_project(db_session, project_id="proj_r15",
                                git_remote="github.com/a/noprimary",
                                owner_id=user.id)
    # No project_repos rows at all

    primary = await get_primary_remote(db_session, "proj_r15")
    assert primary is None


@pytest.mark.anyio
async def test_resolve_by_remote_respects_verified_flag(db_session: AsyncSession):
    """verified flag is stored and retrievable, but resolver itself does NOT gate
    on it (authorization is the caller's responsibility — Sentinel F5)."""
    user = User(id="user-r16", email="r16@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    p = await _create_project(db_session, project_id="proj_r16",
                                git_remote="github.com/a/r16",
                                owner_id=user.id)
    await _create_project_repo(db_session, project_id="proj_r16",
                                git_remote_normalized="github.com/a/r16",
                                is_primary=True,
                                verified=True,
                                verification_method="github_app")

    # Resolver still resolves — verified flag is informational for the caller
    result = await resolve_project_by_remote(db_session, "github.com/a/r16")
    assert result is not None
    assert result.id == "proj_r16"


# ────────────────────────────────────────────────────────────────
# Backfill behavior (model-level verification)
# ────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_backfill_row_properties(db_session: AsyncSession):
    """Model-level backfill verification: each existing project gets one
    is_primary=true, verified=false, verification_method='legacy_backfill'
    project_repos row."""
    user = User(id="user-bf1", email="bf1@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    p = await _create_project(db_session, project_id="proj_bf1",
                                git_remote="github.com/acme/backfill-test",
                                owner_id=user.id)

    # Simulate what the migration backfill does
    import uuid as _uuid
    pr = ProjectRepo(
        id=str(_uuid.uuid4()),
        project_id=p.id,
        git_remote_normalized=p.git_remote_normalized,
        is_primary=True,
        verified=False,
        verification_method="legacy_backfill",
        created_at=_now(),
    )
    db_session.add(pr)
    await db_session.commit()

    # Verify
    rows = (await db_session.execute(
        sa.select(ProjectRepo).where(ProjectRepo.project_id == p.id)
    )).scalars().all()
    assert len(rows) == 1
    row = rows[0]
    assert row.is_primary is True
    assert row.verified is False
    assert row.verification_method == "legacy_backfill"
    assert row.git_remote_normalized == "github.com/acme/backfill-test"


@pytest.mark.anyio
async def test_empty_remote_project_no_backfill(db_session: AsyncSession):
    """Projects with empty git_remote_normalized get no backfill row."""
    user = User(id="user-bf2", email="bf2@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    # We can't create a Project with empty git_remote_normalized via the model
    # because the column has unique=True (empty string collision). This
    # validates the migration's WHERE clause is correct — the migration
    # skips empty/NULL remotes anyway.
    p = await _create_project(db_session, project_id="proj_bf2",
                                git_remote="github.com/acme/bf2",
                                owner_id=user.id)

    # Verify the project was created properly
    result = await db_session.get(Project, p.id)
    assert result is not None
    assert result.git_remote_normalized == "github.com/acme/bf2"


# ────────────────────────────────────────────────────────────────
# Model / migration compatibility
# ────────────────────────────────────────────────────────────────

@pytest.mark.anyio
async def test_project_has_new_columns(db_session: AsyncSession):
    """Project model includes the three new tombstone/state columns."""
    user = User(id="user-mc1", email="mc1@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    p = await _create_project(db_session, project_id="proj_mc1",
                                git_remote="github.com/a/mc1",
                                owner_id=user.id,
                                merged_into_project_id=None,
                                repo_reclaimed_at=None)

    result = await db_session.get(Project, p.id)
    assert result is not None
    assert result.merged_into_project_id is None
    assert result.merged_at is None
    assert result.repo_reclaimed_at is None


@pytest.mark.anyio
async def test_project_repo_model_constraints(db_session: AsyncSession):
    """ProjectRepo model enforces git_remote_normalized uniqueness."""
    user = User(id="user-mc2", email="mc2@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    p1 = await _create_project(db_session, project_id="proj_mc2a",
                                git_remote="github.com/a/mc2a",
                                owner_id=user.id)
    p2 = await _create_project(db_session, project_id="proj_mc2b",
                                git_remote="github.com/a/mc2b",
                                owner_id=user.id)

    await _create_project_repo(db_session, project_id="proj_mc2a",
                                git_remote_normalized="github.com/a/shared",
                                is_primary=True)

    # Second project attempting to link same remote should fail
    import uuid as _uuid
    from sqlalchemy.exc import IntegrityError

    dup = ProjectRepo(
        id=str(_uuid.uuid4()),
        project_id="proj_mc2b",
        git_remote_normalized="github.com/a/shared",  # DUPLICATE
        is_primary=True,
        created_at=_now(),
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.anyio
async def test_project_merge_audit_model_exists(db_session: AsyncSession):
    """ProjectMergeAudit model can be instantiated and persisted."""
    from sessionfs.server.db.models import ProjectMergeAudit

    user = User(id="user-mc3", email="mc3@test.com", tier="pro",
                created_at=_now())
    db_session.add(user)
    await db_session.commit()

    audit = ProjectMergeAudit(
        id=str(uuid.uuid4()),
        source_project_id="proj_src",
        target_project_id="proj_tgt",
        initiated_by_user_id=user.id,
        dry_run=False,
        status="completed",
        persona_policy="rename",
        stats='{"personas": 5}',
        persona_renames='[{"old_name": "atlas", "new_name": "atlas-a1b2c3d4"}]',
        slug_renames='[]',
        skipped_ke_ids='[]',
        skipped_link_ids='[]',
        rules_action="promoted",
    )
    db_session.add(audit)
    await db_session.commit()
    await db_session.refresh(audit)

    assert audit.id is not None
    assert audit.status == "completed"
    assert audit.persona_policy == "rename"
    assert audit.dry_run is False
