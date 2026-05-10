"""Add recipient_email_normalized + index to handoffs.

Inbox lookups filter on lower(recipient_email) but the raw-column index
isn't used by that predicate. The new normalized column is populated at
write time and indexed directly.

Revision ID: 032
Revises: 031
"""

from alembic import op
import sqlalchemy as sa

revision = "032"
down_revision = "031"


def upgrade() -> None:
    with op.batch_alter_table("handoffs") as batch_op:
        batch_op.add_column(
            sa.Column(
                "recipient_email_normalized",
                sa.String(255),
                nullable=True,
            )
        )

    # Backfill via SQLAlchemy Core for cross-DB compatibility (PG + SQLite).
    # Mirror the migration 030 pattern: bind to the active connection,
    # walk the table, lowercase the value, write it back. Uses parameter
    # binding throughout — no string interpolation.
    bind = op.get_bind()
    handoffs = sa.Table(
        "handoffs",
        sa.MetaData(),
        autoload_with=bind,
    )
    rows = bind.execute(
        sa.select(handoffs.c.id, handoffs.c.recipient_email)
    ).fetchall()
    for row in rows:
        if not row.recipient_email:
            continue
        # Match the runtime write path normalization exactly — strip then
        # lower. Without strip(), a legacy row with " Alice@Example.com "
        # would backfill as " alice@example.com " and never match the
        # inbox lookup (which strips before lowercasing).
        normalized = row.recipient_email.strip().lower()
        if not normalized:
            continue
        bind.execute(
            sa.update(handoffs)
            .where(handoffs.c.id == row.id)
            .values(recipient_email_normalized=normalized)
        )

    op.create_index(
        "idx_handoffs_recipient_email_normalized",
        "handoffs",
        ["recipient_email_normalized"],
        if_not_exists=True,
    )


def downgrade() -> None:
    op.drop_index(
        "idx_handoffs_recipient_email_normalized",
        table_name="handoffs",
        if_exists=True,
    )
    with op.batch_alter_table("handoffs") as batch_op:
        batch_op.drop_column("recipient_email_normalized")
