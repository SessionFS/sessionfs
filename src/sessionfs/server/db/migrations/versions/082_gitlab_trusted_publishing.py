"""GitLab CI trusted publishing (GitHub #82).

Revision ID: 082
Revises: 081

skill_trusted_publishers gains a second provider:

- provider CHECK widens from ('github') to ('github', 'gitlab').
- issuer (the GitLab instance origin the row trusts, e.g.
  https://gitlab.com; must be on the operator allowlist
  SFS_GITLAB_OIDC_ISSUERS at creation and at exchange),
  gitlab_project_id (the numeric project id: the immutable pin, like
  GitHub's repository_id) and gitlab_namespace_id (optional pin on the
  owning group / user namespace). All nullable.
- require_protected_environment (NOT NULL, default false): a GitLab trust
  that names an environment may also require GitLab to report it as
  protected (``environment_protected`` == "true"); a job declares its own
  environment, so the name alone only restricts anything when the
  environment is protected in GitLab.
- repository_owner_id, repository_id and workflow_path become nullable
  (GitHub-only); ``repository`` stays NOT NULL as the display name for
  both providers (owner/repo, or the GitLab project path).
- ck_skill_trusted_publisher_shape: a GitHub row carries the GitHub ids
  and workflow and no GitLab fields; a GitLab row carries issuer and
  project id and no GitHub fields (no reusable workflow, no workflow_run).
- idx_skill_trusted_publishers_gitlab (provider, gitlab_project_id): the
  exchange lookup.

Existing rows are all GitHub rows and satisfy the new CHECK unchanged.

SQLite cannot alter a CHECK or a column's nullability in place, so the
SQLite leg rebuilds the table from its full shape (like 069). PostgreSQL
drops and re-creates the named constraints (the 071 CHECK is a real
constraint there; dropping it as an index is what broke 056 in production).

Downgrade deletes GitLab trusts (and their publish tokens) first: the 081
shape cannot hold them.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa
from alembic import op

revision = "082"
down_revision = "081"
branch_labels = None
depends_on = None

_TABLE = "skill_trusted_publishers"
_PROVIDER_OLD = "provider IN ('github')"
_PROVIDER_NEW = "provider IN ('github', 'gitlab')"
_SHAPE = (
    "(provider = 'github' AND repository_id IS NOT NULL AND repository_owner_id IS NOT NULL "
    "AND workflow_path IS NOT NULL AND issuer IS NULL AND gitlab_project_id IS NULL "
    "AND gitlab_namespace_id IS NULL AND require_protected_environment = false) OR "
    "(provider = 'gitlab' AND issuer IS NOT NULL AND gitlab_project_id IS NOT NULL "
    "AND repository_id IS NULL AND repository_owner_id IS NULL AND workflow_path IS NULL "
    "AND reusable_workflow IS NULL AND allow_workflow_run = false "
    "AND (require_protected_environment = false OR environment IS NOT NULL))"
)


def _require_protected_column() -> sa.Column:
    return sa.Column(
        "require_protected_environment",
        sa.Boolean(),
        nullable=False,
        server_default=sa.text("false"),
    )
_GITHUB_ONLY = ("repository_owner_id", "repository_id", "workflow_path")


def _user_fk(name: str) -> sa.Column:
    return sa.Column(
        name, sa.String(length=36), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


def _table(*, gitlab: bool) -> sa.Table:
    """The full skill_trusted_publishers shape (071, or 082 when ``gitlab``)
    for the SQLite table rebuild."""
    columns: list[sa.Column] = [
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column(
            "publisher_id",
            sa.String(length=64),
            sa.ForeignKey("skill_publishers.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("package_name", sa.String(length=100), nullable=True),
        sa.Column("provider", sa.String(length=20), nullable=False, server_default="github"),
        sa.Column("repository_owner_id", sa.BigInteger(), nullable=gitlab),
        sa.Column("repository_id", sa.BigInteger(), nullable=gitlab),
        sa.Column("repository", sa.String(length=200), nullable=False),
        sa.Column("workflow_path", sa.String(length=255), nullable=gitlab),
        sa.Column("reusable_workflow", sa.String(length=300), nullable=True),
        sa.Column(
            "allow_workflow_run", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("environment", sa.String(length=255), nullable=True),
        sa.Column("ref_pattern", sa.String(length=255), nullable=True),
        _user_fk("created_by_user_id"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        _user_fk("revoked_by_user_id"),
    ]
    constraints: list[Any] = [
        sa.CheckConstraint(
            _PROVIDER_NEW if gitlab else _PROVIDER_OLD, name="ck_skill_trusted_publisher_provider"
        ),
        sa.Index("idx_skill_trusted_publishers_repo", "provider", "repository_id"),
        sa.Index("idx_skill_trusted_publishers_publisher", "publisher_id"),
    ]
    if gitlab:
        columns += [
            sa.Column("issuer", sa.String(length=255), nullable=True),
            sa.Column("gitlab_project_id", sa.BigInteger(), nullable=True),
            sa.Column("gitlab_namespace_id", sa.BigInteger(), nullable=True),
            _require_protected_column(),
        ]
        constraints += [
            sa.CheckConstraint(_SHAPE, name="ck_skill_trusted_publisher_shape"),
            sa.Index("idx_skill_trusted_publishers_gitlab", "provider", "gitlab_project_id"),
        ]
    return sa.Table(_TABLE, sa.MetaData(), *columns, *constraints)


def _sqlite() -> bool:
    return op.get_context().dialect.name == "sqlite"


def upgrade() -> None:
    if _sqlite():
        # Columns first, so the rebuild copies them (NULL for every row).
        for column in (
            sa.Column("issuer", sa.String(length=255), nullable=True),
            sa.Column("gitlab_project_id", sa.BigInteger(), nullable=True),
            sa.Column("gitlab_namespace_id", sa.BigInteger(), nullable=True),
            _require_protected_column(),
        ):
            op.add_column(_TABLE, column)
        with op.batch_alter_table(_TABLE, recreate="always", copy_from=_table(gitlab=True)):
            pass
        return
    op.drop_constraint("ck_skill_trusted_publisher_provider", _TABLE, type_="check")
    op.create_check_constraint("ck_skill_trusted_publisher_provider", _TABLE, _PROVIDER_NEW)
    op.add_column(_TABLE, sa.Column("issuer", sa.String(length=255), nullable=True))
    op.add_column(_TABLE, sa.Column("gitlab_project_id", sa.BigInteger(), nullable=True))
    op.add_column(_TABLE, sa.Column("gitlab_namespace_id", sa.BigInteger(), nullable=True))
    op.add_column(_TABLE, _require_protected_column())
    for name in _GITHUB_ONLY:
        op.alter_column(_TABLE, name, nullable=True)
    op.create_check_constraint("ck_skill_trusted_publisher_shape", _TABLE, _SHAPE)
    op.create_index(
        "idx_skill_trusted_publishers_gitlab", _TABLE, ["provider", "gitlab_project_id"]
    )


def downgrade() -> None:
    # The 081 shape cannot hold GitLab trusts: remove them and their tokens
    # (explicitly; SQLite may run with foreign keys off).
    op.execute(
        sa.text(
            "DELETE FROM skill_publish_tokens WHERE trusted_publisher_id IN "
            f"(SELECT id FROM {_TABLE} WHERE provider = 'gitlab')"
        )
    )
    op.execute(sa.text(f"DELETE FROM {_TABLE} WHERE provider = 'gitlab'"))
    if _sqlite():
        # Rebuild from the 071 shape: the GitLab columns, CHECK and index are
        # not part of it, so they are not copied.
        op.drop_index("idx_skill_trusted_publishers_gitlab", table_name=_TABLE)
        with op.batch_alter_table(_TABLE, recreate="always", copy_from=_table(gitlab=False)):
            pass
        return
    op.drop_index("idx_skill_trusted_publishers_gitlab", table_name=_TABLE)
    op.drop_constraint("ck_skill_trusted_publisher_shape", _TABLE, type_="check")
    for name in _GITHUB_ONLY:
        op.alter_column(_TABLE, name, nullable=False)
    op.drop_column(_TABLE, "require_protected_environment")
    op.drop_column(_TABLE, "gitlab_namespace_id")
    op.drop_column(_TABLE, "gitlab_project_id")
    op.drop_column(_TABLE, "issuer")
    op.drop_constraint("ck_skill_trusted_publisher_provider", _TABLE, type_="check")
    op.create_check_constraint("ck_skill_trusted_publisher_provider", _TABLE, _PROVIDER_OLD)
