"""Migration 076 (marketplace discovery and freshness) round-trip.

The prior schema is the real SQLAlchemy metadata minus what 076 adds (six
tables, eight columns). The upgrade seeds the category taxonomy and
backfills the version / listing search facets from their JSON columns.
The PostgreSQL leg runs when SFS_TEST_PG_URL is set (and in the
`migrations-postgres` CI gate).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine

from sessionfs.server.db.models import Base
from sessionfs.skills.discovery import CATEGORIES
from tests.pg_helpers import reset_pg_schema

_MIGRATIONS = str(
    Path(__file__).resolve().parents[3] / "src" / "sessionfs" / "server" / "db" / "migrations"
)
NEW_TABLES = {
    "skill_categories",
    "skill_package_categories",
    "skill_install_daily",
    "skill_install_dedupe",
    "skill_package_freshness",
    "skill_package_tool_freshness",
}
NEW_COLUMNS = {
    "skill_packages": ("deprecated_at", "deprecation_message", "successor_package_id", "readme_path"),
    "skill_package_versions": ("license_key", "scan_severity_rank", "capability_flags"),
    "skill_marketplace_listings": ("provenance_attested",),
}


def _cfg(url: str) -> Config:
    config = Config()
    config.set_main_option("script_location", _MIGRATIONS)
    config.set_main_option("sqlalchemy.url", url)
    return config


def seed(execute) -> None:
    """One package with two versions and two listings (any dialect)."""
    execute(
        "INSERT INTO users (id, email, tier, is_active, email_verified, created_at) "
        "VALUES ('u1', 'u1@example.com', 'pro', :t, :t, CURRENT_TIMESTAMP)",
        {"t": True},
    )
    execute(
        "INSERT INTO skill_packages (id, owner_type, owner_id, namespace, name, display_name, "
        "description, source_type, source_locator, source_subdir, is_yanked) VALUES "
        "('skp_1', 'user', 'u1', 'ns', 'pkg', 'Pkg', '', 'upload', 'x', '', :f)",
        {"f": False},
    )
    versions = [
        ("skv_1", {"declared": True, "value": "  MIT "}, {"open_max_severity": "medium"}, {"scripts": True, "mcp": True}),
        ("skv_2", {"declared": False}, {"max_severity": "high"}, {"allowed_tools": []}),
    ]
    for vid, lic, scan, risk in versions:
        execute(
            "INSERT INTO skill_package_versions (id, package_id, content_hash, agent_plugin_schema, "
            "manifest_json, risk_summary, compatibility, instruction_bundle, license_metadata, "
            "signature_metadata, dlp_status, blob_key, blob_size_bytes, scan_summary, "
            "capability_manifest, manifest_version, created_at) VALUES (:id, 'skp_1', :hash, 's', '{}', "
            ":risk, '{}', '[]', :lic, '{}', 'passed', :blob, 1, :scan, '{}', 0, CURRENT_TIMESTAMP)",
            {"id": vid, "hash": f"sha256:{vid}", "risk": json.dumps(risk), "lic": json.dumps(lic),
             "blob": f"b/{vid}", "scan": json.dumps(scan)},
        )
    for lid, vid, sig in (
        ("skl_1", "skv_1", {"kind": "github_oidc", "verification": "oidc_token"}),
        ("skl_2", "skv_2", None),
    ):
        execute(
            "INSERT INTO skill_marketplace_listings (id, collection, package_id, version_id, "
            "owner_user_id, name, description, visibility, is_published, is_yanked, "
            "verification_state, moderation_state, search_text, published_at, signature_metadata) "
            "VALUES (:id, :col, 'skp_1', :vid, 'u1', 'pkg', '', 'user', :t, :f, 'unverified', "
            "'approved', 'pkg', CURRENT_TIMESTAMP, :sig)",
            {"id": lid, "col": f"c{lid}", "vid": vid, "t": True, "f": False,
             "sig": json.dumps(sig) if sig else None},
        )


def check_backfill(query) -> None:
    facets = {row[0]: row[1:] for row in query(
        "SELECT id, license_key, scan_severity_rank, capability_flags FROM skill_package_versions"
    )}
    assert facets["skv_1"] == ("mit", 2, ",scripts,mcp,")
    assert facets["skv_2"] == (None, 3, "")
    attested = dict(query("SELECT id, provenance_attested FROM skill_marketplace_listings"))
    assert bool(attested["skl_1"]) is True and bool(attested["skl_2"]) is False
    slugs = [row[0] for row in query("SELECT slug FROM skill_categories ORDER BY sort_order")]
    assert slugs == [slug for slug, _ in CATEGORIES]


def _prior_metadata() -> sa.MetaData:
    metadata = sa.MetaData()
    for name, table in Base.metadata.tables.items():
        if name in NEW_TABLES:
            continue
        copy = table.to_metadata(metadata)
        for column_name in NEW_COLUMNS.get(name, ()):
            column = copy.c[column_name]
            for constraint in list(copy.constraints):
                if isinstance(constraint, sa.ForeignKeyConstraint) and column_name in constraint.column_keys:
                    copy.constraints.discard(constraint)
            for foreign_key in list(copy.foreign_keys):
                if foreign_key.parent is column:
                    copy.foreign_keys.discard(foreign_key)
            copy._columns.remove(column)  # noqa: SLF001 - test-only schema rewind
    return metadata


@pytest.fixture
def pre_076_db(tmp_path: Path) -> Path:
    path = tmp_path / "m076.db"
    engine = sa.create_engine(f"sqlite:///{path}")
    _prior_metadata().create_all(engine)
    with engine.begin() as conn:
        seed(lambda sql, params: conn.execute(sa.text(sql), params))
    engine.dispose()
    command.stamp(_cfg(f"sqlite+aiosqlite:///{path}"), "075")
    return path


def _query(path: Path):
    def run(sql: str) -> list[tuple]:
        engine = sa.create_engine(f"sqlite:///{path}")
        try:
            with engine.connect() as conn:
                return [tuple(row) for row in conn.execute(sa.text(sql))]
        finally:
            engine.dispose()

    return run


def _schema(path: Path) -> tuple[set[str], dict[str, set[str]]]:
    engine = sa.create_engine(f"sqlite:///{path}")
    try:
        insp = sa.inspect(engine)
        return set(insp.get_table_names()), {
            table: {c["name"] for c in insp.get_columns(table)} for table in NEW_COLUMNS
        }
    finally:
        engine.dispose()


def test_upgrade_creates_schema_seeds_and_backfills(pre_076_db: Path) -> None:
    command.upgrade(_cfg(f"sqlite+aiosqlite:///{pre_076_db}"), "076")
    tables, columns = _schema(pre_076_db)
    assert NEW_TABLES <= tables
    for table, names in NEW_COLUMNS.items():
        assert set(names) <= columns[table]
    check_backfill(_query(pre_076_db))


def test_downgrade_and_reupgrade(pre_076_db: Path) -> None:
    config = _cfg(f"sqlite+aiosqlite:///{pre_076_db}")
    command.upgrade(config, "076")
    command.downgrade(config, "075")
    tables, columns = _schema(pre_076_db)
    assert not (NEW_TABLES & tables)
    for table, names in NEW_COLUMNS.items():
        assert not (set(names) & columns[table])
    command.upgrade(config, "076")
    check_backfill(_query(pre_076_db))


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database to run the full chain",
)
def test_full_chain_on_postgres() -> None:
    reset_pg_schema()  # the chain runs from an empty database
    url = os.environ["SFS_TEST_PG_URL"]
    config = _cfg(url)
    command.upgrade(config, "head")
    command.downgrade(config, "075")

    def _reseed(conn) -> None:
        for statement in (
            "DELETE FROM skill_marketplace_listings WHERE id IN ('skl_1', 'skl_2')",
            "DELETE FROM skill_package_versions WHERE id IN ('skv_1', 'skv_2')",
            "DELETE FROM skill_packages WHERE id = 'skp_1'",
            "DELETE FROM users WHERE id = 'u1'",
        ):
            conn.execute(sa.text(statement))
        seed(lambda sql, params: conn.execute(sa.text(sql), params))

    def _check(conn) -> None:
        check_backfill(lambda sql: [tuple(row) for row in conn.execute(sa.text(sql))])
        assert conn.execute(sa.text(
            "SELECT 1 FROM information_schema.table_constraints "
            "WHERE constraint_name = 'fk_skill_packages_successor'"
        )).all()

    async def _run(fn, *, write: bool) -> None:
        engine = create_async_engine(url)
        try:
            async with (engine.begin() if write else engine.connect()) as conn:
                await conn.run_sync(fn)
        finally:
            await engine.dispose()

    asyncio.run(_run(_reseed, write=True))
    command.upgrade(config, "head")
    asyncio.run(_run(_check, write=False))
    command.downgrade(config, "075")
    command.upgrade(config, "head")
    asyncio.run(_run(_check, write=False))
