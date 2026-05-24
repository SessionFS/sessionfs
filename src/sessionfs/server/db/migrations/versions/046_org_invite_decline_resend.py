"""Org-invite decline + resend lifecycle columns.

Revision ID: 046
Revises: 045

Closes tk_6afbcfefe5804c1d (v0.10.22+). The original OrgInvite shape
only carried `accepted_at`; the recipient had no way to actively
refuse an invite, and the org admin had no audit row for resends.
Three nullable columns make those flows first-class without altering
the accept path:

- declined_at: timestamp when the recipient explicitly declined.
  Mutually exclusive with accepted_at — enforced at the route layer
  rather than via CHECK constraint so SQLite local-mode keeps working.
- decline_reason: optional Text the recipient supplied at decline
  time (e.g. "wrong email", "not joining team yet"). Nullable; no
  schema requirement to be present.
- last_emailed_at: last successful (or attempted) email send. The
  invite endpoints set this on initial send; the new resend endpoint
  updates it on each manual re-fire so admins can see in the
  dashboard when the recipient last got a nudge.

Strictly additive — no backfill required, no index changes. Existing
invite rows simply have all three columns NULL until the next write.
"""

from alembic import op
import sqlalchemy as sa


revision = "046"
down_revision = "045"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("org_invites") as batch:
        batch.add_column(
            sa.Column("declined_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("decline_reason", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("last_emailed_at", sa.DateTime(timezone=True), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("org_invites") as batch:
        batch.drop_column("last_emailed_at")
        batch.drop_column("decline_reason")
        batch.drop_column("declined_at")
