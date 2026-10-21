"""Marketplace discovery and freshness (marketplace v2 §9).

Revision ID: 076
Revises: 075

- skill_categories: the fixed category taxonomy, seeded here.
- skill_package_categories: up to three categories per package.
- skill_packages: deprecated_at, deprecation_message, successor_package_id
  (same-publisher successor), readme_path.
- skill_package_versions: search facets derived from the JSON columns
  (license_key, scan_severity_rank, capability_flags), backfilled here and
  kept current by ORM flush listeners.
- skill_marketplace_listings.provenance_attested: verified publication
  provenance (trusted publishing), backfilled from signature_metadata.
- skill_install_daily: daily install-event aggregates (no per-user rows).
- skill_install_dedupe: pseudonymous per-principal dedupe, pruned after 30
  days by the freshness sweeper.
- skill_package_freshness / skill_package_tool_freshness: sweeper output
  (stale, unmaintained, last updated / last verified per tool, ranking
  inputs).

Additive only. Downgrade drops everything this adds.
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "076"
down_revision = "075"
branch_labels = None
depends_on = None

# Frozen copy of skills/discovery.py CATEGORIES as of this migration.
_CATEGORIES = (
    ("review", "Code review"),
    ("testing", "Testing"),
    ("debugging", "Debugging"),
    ("refactoring", "Refactoring"),
    ("docs", "Documentation"),
    ("security", "Security"),
    ("frontend", "Frontend"),
    ("backend", "Backend and APIs"),
    ("data", "Data and analytics"),
    ("infra", "Infrastructure"),
    ("deployment", "Deployment and release"),
    ("workflow", "Workflow and planning"),
    ("integrations", "Integrations"),
    ("ai-ml", "AI and ML"),
    ("productivity", "Productivity"),
)
_SEVERITY_RANK = {"none": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
_BATCH = 500


def _obj(raw):
    try:
        value = json.loads(raw) if raw else {}
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _license_key(raw):
    meta = _obj(raw)
    value = meta.get("value") if meta.get("declared") else None
    if not isinstance(value, str):
        return None
    return " ".join(value.split()).casefold()[:100] or None


def _severity_rank(raw):
    summary = _obj(raw)
    severity = summary.get("open_max_severity")
    if severity is None:
        severity = summary.get("max_severity")
    return _SEVERITY_RANK.get(str(severity or "none"), 0)


def _capability_flags(raw):
    risk = _obj(raw)
    present = [
        name
        for name, key in (
            ("scripts", "scripts"),
            ("mcp", "mcp"),
            ("hooks", "hooks"),
            ("executables", "executable_extensions"),
            ("allowed_tools", "allowed_tools"),
        )
        if risk.get(key)
    ]
    return "," + ",".join(present) + "," if present else ""


def _attested(raw):
    signature = _obj(raw)
    return bool(signature.get("attested")) or (
        signature.get("kind") == "github_oidc" and signature.get("verification") == "oidc_token"
    )


def _backfill(bind) -> None:
    versions = sa.table(
        "skill_package_versions",
        sa.column("id", sa.String),
        sa.column("license_metadata", sa.Text),
        sa.column("scan_summary", sa.Text),
        sa.column("risk_summary", sa.Text),
        sa.column("license_key", sa.String),
        sa.column("scan_severity_rank", sa.SmallInteger),
        sa.column("capability_flags", sa.String),
    )
    last = ""
    while True:
        rows = bind.execute(
            sa.select(versions.c.id, versions.c.license_metadata, versions.c.scan_summary, versions.c.risk_summary)
            .where(versions.c.id > last)
            .order_by(versions.c.id)
            .limit(_BATCH)
        ).all()
        if not rows:
            break
        for row in rows:
            bind.execute(
                versions.update()
                .where(versions.c.id == row.id)
                .values(
                    license_key=_license_key(row.license_metadata),
                    scan_severity_rank=_severity_rank(row.scan_summary),
                    capability_flags=_capability_flags(row.risk_summary),
                )
            )
        last = rows[-1].id
    listings = sa.table(
        "skill_marketplace_listings",
        sa.column("id", sa.String),
        sa.column("signature_metadata", sa.Text),
        sa.column("provenance_attested", sa.Boolean),
    )
    last = ""
    while True:
        rows = bind.execute(
            sa.select(listings.c.id, listings.c.signature_metadata)
            .where(listings.c.id > last)
            .order_by(listings.c.id)
            .limit(_BATCH)
        ).all()
        if not rows:
            break
        attested = [row.id for row in rows if _attested(row.signature_metadata)]
        if attested:
            bind.execute(
                listings.update().where(listings.c.id.in_(attested)).values(provenance_attested=True)
            )
        last = rows[-1].id


def upgrade() -> None:
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"

    categories = op.create_table(
        "skill_categories",
        sa.Column("slug", sa.String(length=40), primary_key=True),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
    )
    op.bulk_insert(
        categories,
        [
            {"slug": slug, "display_name": name, "sort_order": index * 10}
            for index, (slug, name) in enumerate(_CATEGORIES)
        ],
    )
    op.create_table(
        "skill_package_categories",
        sa.Column(
            "package_id",
            sa.String(length=64),
            sa.ForeignKey("skill_packages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "category_slug",
            sa.String(length=40),
            sa.ForeignKey("skill_categories.slug", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_index(
        "idx_skill_package_categories_slug",
        "skill_package_categories",
        ["category_slug", "package_id"],
    )

    op.create_table(
        "skill_install_daily",
        sa.Column("version_id", sa.String(length=64), primary_key=True),
        sa.Column("tool", sa.String(length=50), primary_key=True),
        sa.Column("source", sa.String(length=20), primary_key=True),
        sa.Column("event", sa.String(length=20), primary_key=True),
        sa.Column("actor_type", sa.String(length=20), primary_key=True),
        sa.Column("org_id", sa.String(length=64), primary_key=True, server_default=""),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column(
            "package_id",
            sa.String(length=64),
            sa.ForeignKey("skill_packages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("count", sa.Integer(), nullable=False, server_default="0"),
        sa.CheckConstraint(
            "event IN ('install', 'uninstall', 'active', 'translation_failure')",
            name="ck_skill_install_daily_event",
        ),
        sa.CheckConstraint(
            "source IN ('download', 'reconcile', 'native')", name="ck_skill_install_daily_source"
        ),
        sa.CheckConstraint("actor_type IN ('user', 'service')", name="ck_skill_install_daily_actor"),
    )
    op.create_index("idx_skill_install_daily_package_day", "skill_install_daily", ["package_id", "day"])
    op.create_index("idx_skill_install_daily_org_day", "skill_install_daily", ["org_id", "day"])
    op.create_index("idx_skill_install_daily_day", "skill_install_daily", ["day"])

    op.create_table(
        "skill_install_dedupe",
        sa.Column("principal_hash", sa.String(length=64), primary_key=True),
        sa.Column("version_id", sa.String(length=64), primary_key=True),
        sa.Column("tool", sa.String(length=50), primary_key=True),
        sa.Column("event", sa.String(length=20), primary_key=True),
        sa.Column("day", sa.Date(), primary_key=True),
        sa.Column(
            "package_id",
            sa.String(length=64),
            sa.ForeignKey("skill_packages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("actor_type", sa.String(length=20), nullable=False, server_default="user"),
    )
    op.create_index("idx_skill_install_dedupe_package_day", "skill_install_dedupe", ["package_id", "day"])
    op.create_index("idx_skill_install_dedupe_day", "skill_install_dedupe", ["day"])

    op.create_table(
        "skill_package_freshness",
        sa.Column(
            "package_id",
            sa.String(length=64),
            sa.ForeignKey("skill_packages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("last_version_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("works_in_regressed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_archived", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("unmaintained", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("installs_30d", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("service_installs_30d", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("popularity_rank", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("idx_skill_package_freshness_stale", "skill_package_freshness", ["stale"])

    op.create_table(
        "skill_package_tool_freshness",
        sa.Column(
            "package_id",
            sa.String(length=64),
            sa.ForeignKey("skill_packages.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("tool", sa.String(length=50), primary_key=True),
        sa.Column("last_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_verified_version_id", sa.String(length=64), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_checked_version_id", sa.String(length=64), nullable=True),
        sa.Column("verify_status", sa.String(length=20), nullable=False, server_default="unknown"),
        sa.Column("verify_detail", sa.String(length=300), nullable=True),
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "computed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint(
            "verify_status IN ('unknown', 'passing', 'failing', 'regressed')",
            name="ck_skill_package_tool_freshness_status",
        ),
    )
    op.create_index(
        "idx_skill_package_tool_freshness_tool",
        "skill_package_tool_freshness",
        ["tool", "verify_status"],
    )

    # Column additions. SQLite: plain columns without REFERENCES (SQLite
    # cannot drop a column that takes part in a foreign key). PostgreSQL:
    # real constraints.
    if sqlite:
        for statement in (
            "ALTER TABLE skill_packages ADD COLUMN deprecated_at DATETIME",
            "ALTER TABLE skill_packages ADD COLUMN deprecation_message TEXT",
            "ALTER TABLE skill_packages ADD COLUMN successor_package_id VARCHAR(64)",
            "ALTER TABLE skill_packages ADD COLUMN readme_path VARCHAR(300)",
            "ALTER TABLE skill_package_versions ADD COLUMN license_key VARCHAR(100)",
            "ALTER TABLE skill_package_versions ADD COLUMN scan_severity_rank SMALLINT NOT NULL DEFAULT 0",
            "ALTER TABLE skill_package_versions ADD COLUMN capability_flags VARCHAR(200) NOT NULL DEFAULT ''",
            "ALTER TABLE skill_marketplace_listings ADD COLUMN provenance_attested BOOLEAN NOT NULL DEFAULT 0",
        ):
            op.execute(sa.text(statement))
    else:
        op.add_column("skill_packages", sa.Column("deprecated_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column("skill_packages", sa.Column("deprecation_message", sa.Text(), nullable=True))
        op.add_column(
            "skill_packages", sa.Column("successor_package_id", sa.String(length=64), nullable=True)
        )
        op.create_foreign_key(
            "fk_skill_packages_successor",
            "skill_packages",
            "skill_packages",
            ["successor_package_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.add_column("skill_packages", sa.Column("readme_path", sa.String(length=300), nullable=True))
        op.add_column(
            "skill_package_versions", sa.Column("license_key", sa.String(length=100), nullable=True)
        )
        op.add_column(
            "skill_package_versions",
            sa.Column("scan_severity_rank", sa.SmallInteger(), nullable=False, server_default="0"),
        )
        op.add_column(
            "skill_package_versions",
            sa.Column("capability_flags", sa.String(length=200), nullable=False, server_default=""),
        )
        op.add_column(
            "skill_marketplace_listings",
            sa.Column("provenance_attested", sa.Boolean(), nullable=False, server_default=sa.false()),
        )
    _backfill(bind)


def downgrade() -> None:
    bind = op.get_bind()
    sqlite = bind.dialect.name == "sqlite"
    if not sqlite:
        op.drop_constraint("fk_skill_packages_successor", "skill_packages", type_="foreignkey")
    op.drop_column("skill_marketplace_listings", "provenance_attested")
    for column in ("capability_flags", "scan_severity_rank", "license_key"):
        op.drop_column("skill_package_versions", column)
    for column in ("readme_path", "successor_package_id", "deprecation_message", "deprecated_at"):
        op.drop_column("skill_packages", column)
    op.drop_index("idx_skill_package_tool_freshness_tool", table_name="skill_package_tool_freshness")
    op.drop_table("skill_package_tool_freshness")
    op.drop_index("idx_skill_package_freshness_stale", table_name="skill_package_freshness")
    op.drop_table("skill_package_freshness")
    op.drop_index("idx_skill_install_dedupe_day", table_name="skill_install_dedupe")
    op.drop_index("idx_skill_install_dedupe_package_day", table_name="skill_install_dedupe")
    op.drop_table("skill_install_dedupe")
    op.drop_index("idx_skill_install_daily_day", table_name="skill_install_daily")
    op.drop_index("idx_skill_install_daily_org_day", table_name="skill_install_daily")
    op.drop_index("idx_skill_install_daily_package_day", table_name="skill_install_daily")
    op.drop_table("skill_install_daily")
    op.drop_index("idx_skill_package_categories_slug", table_name="skill_package_categories")
    op.drop_table("skill_package_categories")
    op.drop_table("skill_categories")
