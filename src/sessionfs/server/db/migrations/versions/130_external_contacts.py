"""External contacts (GitHub #125, docs/design/external-contacts.md).

Revision ID: 130
Revises: 129

- ``external_contacts``: an org-scoped person who is not a SessionFS user.
  CHECK ``ck_external_contacts_status`` (active | archived | merged) and
  ``ck_external_contacts_merged`` (merged exactly when merged_into_id is
  set); partial unique ``uq_external_contacts_org_email`` on (org_id, email)
  for rows with an email that are not merged; index
  (org_id, status, name_norm) for resolution and the directory list.
- ``external_contact_aliases``: PK (contact_id, alias_norm), org_id
  denormalised with an (org_id, alias_norm) index for the one-query lookup.
- ``meeting_participants.contact_id`` (FK external_contacts ON DELETE SET
  NULL) and ``ck_meeting_participants_resolution_method`` gains ``contact``.
- ``tickets.assigned_contact_id`` (no FK in metadata, like
  assigned_user_id; PostgreSQL-only FK ``fk_tickets_assigned_contact`` ON
  DELETE SET NULL) + ``idx_ticket_assigned_contact``.

All new columns are nullable, so existing rows need no backfill.

SQLite cannot alter a CHECK in place, so ``meeting_participants`` is rebuilt
from an explicit copy of its shape (the pattern 128 uses).

Downgrade: participants resolved to a contact go back to ``unresolved``
first (so the narrower CHECK still holds), then the columns, indexes and
the two tables go.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "130"
down_revision = "129"
branch_labels = None
depends_on = None

_PART = "meeting_participants"
_RES_OLD = (
    "resolution_method IN ('exact_email', 'display_name', 'persona', 'manual', 'unresolved')"
)
_RES_NEW = (
    "resolution_method IN ('exact_email', 'display_name', 'persona', 'manual', 'contact', "
    "'unresolved')"
)
_PART_FK = "fk_meeting_participants_contact"
_TICKET_FK = "fk_tickets_assigned_contact"


def _participants_table(*, with_contact: bool) -> sa.Table:
    """The meeting_participants shape (104, plus 130 when ``with_contact``)."""
    columns: list = [
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "transcript_id",
            sa.String(64),
            sa.ForeignKey("meeting_transcripts.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name_raw", sa.String(200), nullable=False),
        sa.Column("email_raw", sa.String(320), nullable=True),
        sa.Column(
            "user_id", sa.String(36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("persona_name", sa.String(64), nullable=True),
        sa.Column(
            "resolution_method",
            sa.String(20),
            nullable=False,
            server_default=sa.text("'unresolved'"),
        ),
        sa.Column(
            "resolved_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    ]
    if with_contact:
        columns.append(_contact_column())
    columns.append(
        sa.CheckConstraint(
            _RES_NEW if with_contact else _RES_OLD,
            name="ck_meeting_participants_resolution_method",
        )
    )
    return sa.Table(
        _PART,
        sa.MetaData(),
        *columns,
        sa.Index("idx_meeting_participants_transcript", "transcript_id"),
    )


def _contact_column() -> sa.Column:
    return sa.Column(
        "contact_id",
        sa.String(64),
        sa.ForeignKey("external_contacts.id", ondelete="SET NULL", name=_PART_FK),
        nullable=True,
    )


def upgrade() -> None:
    sqlite = op.get_context().dialect.name == "sqlite"
    op.create_table(
        "external_contacts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("display_name", sa.String(120), nullable=False),
        sa.Column("name_norm", sa.String(120), nullable=False),
        sa.Column("email", sa.String(320), nullable=True),
        sa.Column("role", sa.String(80), nullable=True),
        sa.Column("tags", sa.Text(), nullable=False, server_default=sa.text("'[]'")),
        sa.Column("status", sa.String(10), nullable=False, server_default=sa.text("'active'")),
        sa.Column(
            "merged_into_id",
            sa.String(64),
            sa.ForeignKey("external_contacts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "linked_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("linked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_by_user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("actor_type", sa.String(20), nullable=False, server_default=sa.text("'user'")),
        sa.Column("service_key_id", sa.String(36), nullable=True),
        sa.Column("service_key_name", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('active', 'archived', 'merged')", name="ck_external_contacts_status"
        ),
        sa.CheckConstraint(
            "(status = 'merged') = (merged_into_id IS NOT NULL)",
            name="ck_external_contacts_merged",
        ),
    )
    op.create_index(
        "uq_external_contacts_org_email",
        "external_contacts",
        ["org_id", "email"],
        unique=True,
        postgresql_where=sa.text("email IS NOT NULL AND status <> 'merged'"),
        sqlite_where=sa.text("email IS NOT NULL AND status <> 'merged'"),
    )
    op.create_index(
        "idx_external_contacts_org_status_name",
        "external_contacts",
        ["org_id", "status", "name_norm"],
    )
    op.create_index(
        "idx_external_contacts_linked_user", "external_contacts", ["linked_user_id"]
    )

    op.create_table(
        "external_contact_aliases",
        sa.Column(
            "contact_id",
            sa.String(64),
            sa.ForeignKey("external_contacts.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("alias_norm", sa.String(120), primary_key=True),
        sa.Column("alias", sa.String(120), nullable=False),
        sa.Column(
            "org_id",
            sa.String(64),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "idx_external_contact_aliases_org_norm",
        "external_contact_aliases",
        ["org_id", "alias_norm"],
    )

    # meeting_participants.contact_id + the widened resolution CHECK.
    if sqlite:
        with op.batch_alter_table(
            _PART, recreate="always", copy_from=_participants_table(with_contact=False)
        ) as batch:
            batch.add_column(_contact_column())
            batch.drop_constraint("ck_meeting_participants_resolution_method", type_="check")
            batch.create_check_constraint("ck_meeting_participants_resolution_method", _RES_NEW)
    else:
        op.add_column(_PART, sa.Column("contact_id", sa.String(64), nullable=True))
        op.create_foreign_key(
            _PART_FK, _PART, "external_contacts", ["contact_id"], ["id"], ondelete="SET NULL"
        )
        op.drop_constraint("ck_meeting_participants_resolution_method", _PART, type_="check")
        op.create_check_constraint("ck_meeting_participants_resolution_method", _PART, _RES_NEW)
    op.create_index("idx_meeting_participants_contact", _PART, ["contact_id"])

    # tickets.assigned_contact_id
    op.add_column("tickets", sa.Column("assigned_contact_id", sa.String(64), nullable=True))
    if not sqlite:
        op.create_foreign_key(
            _TICKET_FK,
            "tickets",
            "external_contacts",
            ["assigned_contact_id"],
            ["id"],
            ondelete="SET NULL",
        )
    op.create_index(
        "idx_ticket_assigned_contact",
        "tickets",
        ["project_id", "assigned_contact_id", "status"],
    )


def downgrade() -> None:
    sqlite = op.get_context().dialect.name == "sqlite"
    op.drop_index("idx_ticket_assigned_contact", table_name="tickets")
    if not sqlite:
        op.drop_constraint(_TICKET_FK, "tickets", type_="foreignkey")
    # Plain DROP COLUMN on both dialects (SQLite >= 3.35): no table rebuild.
    op.execute(sa.text("ALTER TABLE tickets DROP COLUMN assigned_contact_id"))

    # A contact-resolved participant can't satisfy the narrower CHECK.
    op.execute(
        sa.text(
            f"UPDATE {_PART} SET resolution_method = 'unresolved' "
            "WHERE resolution_method = 'contact'"
        )
    )
    op.drop_index("idx_meeting_participants_contact", table_name=_PART)
    if sqlite:
        with op.batch_alter_table(
            _PART, recreate="always", copy_from=_participants_table(with_contact=True)
        ) as batch:
            batch.drop_constraint("ck_meeting_participants_resolution_method", type_="check")
            batch.create_check_constraint("ck_meeting_participants_resolution_method", _RES_OLD)
            batch.drop_column("contact_id")
    else:
        op.drop_constraint("ck_meeting_participants_resolution_method", _PART, type_="check")
        op.create_check_constraint("ck_meeting_participants_resolution_method", _PART, _RES_OLD)
        op.drop_constraint(_PART_FK, _PART, type_="foreignkey")
        op.drop_column(_PART, "contact_id")

    op.drop_index("idx_external_contact_aliases_org_norm", table_name="external_contact_aliases")
    op.drop_table("external_contact_aliases")
    op.drop_index("idx_external_contacts_linked_user", table_name="external_contacts")
    op.drop_index("idx_external_contacts_org_status_name", table_name="external_contacts")
    op.drop_index("uq_external_contacts_org_email", table_name="external_contacts")
    op.drop_table("external_contacts")
