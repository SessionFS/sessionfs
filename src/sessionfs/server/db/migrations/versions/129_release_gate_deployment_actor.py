"""Actor provenance on release gates and deployments.

Revision ID: 129
Revises: 128

Additive only. On both ``release_gates`` and ``deployments``:

- ``actor_type`` (VARCHAR 20): ``user`` | ``service_key`` for a write through
  the releases API, ``webhook`` for a row the delivery webhook ingest wrote.
  CHECK ``ck_<table>_actor_type`` (NULL allowed for pre-129 rows).
- ``actor_user_id`` (VARCHAR 36): the person behind a user-key write; NULL
  for a service key (its identity is the key, never its minter) and for a
  webhook. FK users ON DELETE SET NULL (``fk_<table>_actor_user``).
- ``service_key_id`` (VARCHAR 36) and ``service_key_name`` (VARCHAR 100):
  the service key that wrote the row.

A gate is an upsert per (release, kind), so its actor columns name whoever
recorded its current status (a failed→passed flip is attributed).

Existing rows keep NULL: their writer was never recorded and is not guessed.
The FKs and CHECKs are PostgreSQL-only, like 080's: SQLite cannot add them
without a table rebuild. Downgrade drops the constraints and the columns.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "129"
down_revision = "128"
branch_labels = None
depends_on = None

_TABLES = ("release_gates", "deployments")
_COLUMNS = ("actor_type", "actor_user_id", "service_key_id", "service_key_name")
_ACTOR_CHECK = "actor_type IS NULL OR actor_type IN ('user', 'service_key', 'webhook')"


def upgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    for table in _TABLES:
        op.add_column(table, sa.Column("actor_type", sa.String(20), nullable=True))
        op.add_column(table, sa.Column("actor_user_id", sa.String(36), nullable=True))
        op.add_column(table, sa.Column("service_key_id", sa.String(36), nullable=True))
        op.add_column(table, sa.Column("service_key_name", sa.String(100), nullable=True))
        if not sqlite:
            op.create_foreign_key(
                f"fk_{table}_actor_user",
                table,
                "users",
                ["actor_user_id"],
                ["id"],
                ondelete="SET NULL",
            )
            op.create_check_constraint(f"ck_{table}_actor_type", table, _ACTOR_CHECK)


def downgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    for table in _TABLES:
        if not sqlite:
            op.drop_constraint(f"ck_{table}_actor_type", table, type_="check")
            op.drop_constraint(f"fk_{table}_actor_user", table, type_="foreignkey")
        # Plain DROP COLUMN on both dialects (SQLite >= 3.35): no rebuild.
        for column in _COLUMNS:
            op.execute(sa.text(f"ALTER TABLE {table} DROP COLUMN {column}"))
