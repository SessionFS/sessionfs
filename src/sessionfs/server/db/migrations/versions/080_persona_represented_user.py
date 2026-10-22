"""Org personas can name the user they represent (GitHub #81).

Revision ID: 080
Revises: 079

Additive only.

- agent_personas.represented_user_id: nullable; set only on org personas.
  That user may update the persona's role, content and specializations
  without org admin standing, while they remain a member of the org.
  Existing rows get NULL, so nobody gains edit rights on upgrade.
- idx_persona_represented_user (represented_user_id).

The FK (users.id, ON DELETE SET NULL) and the org-only CHECK are
PostgreSQL-only, like 079's: SQLite cannot add them without a table
rebuild. Downgrade drops the index, the constraints and the column.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "080"
down_revision = "079"
branch_labels = None
depends_on = None

_TABLE = "agent_personas"


def upgrade() -> None:
    sqlite = op.get_bind().dialect.name == "sqlite"
    op.add_column(_TABLE, sa.Column("represented_user_id", sa.String(36), nullable=True))
    if not sqlite:
        op.create_foreign_key(
            "fk_agent_personas_represented_user",
            _TABLE,
            "users",
            ["represented_user_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.create_check_constraint(
            "ck_agent_personas_represented_user_org",
            _TABLE,
            "represented_user_id IS NULL OR org_id IS NOT NULL",
        )
    op.create_index("idx_persona_represented_user", _TABLE, ["represented_user_id"])


def downgrade() -> None:
    op.drop_index("idx_persona_represented_user", table_name=_TABLE)
    if op.get_bind().dialect.name != "sqlite":
        op.drop_constraint("ck_agent_personas_represented_user_org", _TABLE, type_="check")
        op.drop_constraint("fk_agent_personas_represented_user", _TABLE, type_="foreignkey")
    # Plain DROP COLUMN on both dialects (SQLite >= 3.35): no table rebuild.
    op.execute(sa.text(f"ALTER TABLE {_TABLE} DROP COLUMN represented_user_id"))
