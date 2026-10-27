"""Tickets can be assigned directly to a person (GitHub #96).

Revision ID: 101
Revises: 100

Additive only.

- tickets.assigned_user_id: nullable; when set, the ticket is assigned to
  that person instead of a persona (``assigned_to``). A person assignment
  notifies the teammate directly and hands them "your turn" on approval,
  without going through a represented persona.
- idx_ticket_assigned_user (project_id, assigned_user_id, status).

The FK (users.id, ON DELETE SET NULL) is PostgreSQL-only, like 079/080's:
SQLite cannot add it without a table rebuild. A deleted or deactivated user
clears the assignment rather than failing or cascading. Downgrade drops the
index, the FK and the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "101"
down_revision = "100"
branch_labels = None
depends_on = None

_TABLE = "tickets"
_COLUMN = "assigned_user_id"


def upgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    op.add_column(_TABLE, sa.Column(_COLUMN, sa.String(64), nullable=True))
    if not sqlite:
        op.create_foreign_key(
            "fk_tickets_assigned_user",
            _TABLE,
            "users",
            [_COLUMN],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "idx_ticket_assigned_user", _TABLE, ["project_id", _COLUMN, "status"]
    )


def downgrade() -> None:
    op.drop_index("idx_ticket_assigned_user", table_name=_TABLE)
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("fk_tickets_assigned_user", _TABLE, type_="foreignkey")
    # Plain DROP COLUMN on both dialects (SQLite >= 3.35): no table rebuild.
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN {_COLUMN}"))
