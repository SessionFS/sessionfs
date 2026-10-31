"""Pull requests, releases, gates and deployments (Review Loop R3, track F1).

Revision ID: 114
Revises: 112

Six tables that correlate a ticket's loop story with what actually shipped:

- ``pull_requests``    — one provider PR, unique per (project, provider,
                          repo, number); the loop auto-links tickets from
                          ``tk_…`` ids in title/body/branch.
- ``ticket_pr_links``  — the ticket↔PR correlation with a provenance
                          ``link_source`` (auto_parsed | manual |
                          commit_message | loop).
- ``releases``         — a version cut in the project, unique per
                          (project, version), with its tag SHAs and source.
- ``release_prs``      — which PRs shipped in a release.
- ``release_gates``    — per-kind quality gates recorded against a release.
- ``deployments``      — where (environment) and when a release was deployed.

All six carry inline CHECK constraints (declared at create_table, the same
SQLite-safe pattern as migrations 050–054). Downgrade drops in reverse
dependency order.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "114"
down_revision = "113"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "pull_requests",
        sa.Column("id", sa.String(length=64), primary_key=True),  # pr_<hex>
        sa.Column(
            "project_id",
            sa.String(length=64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("provider", sa.String(length=20), nullable=False),
        # The provider's native opaque id (GitHub PR database id / GitLab iid
        # mirror); nullable — a manually recorded PR may not carry one.
        sa.Column("external_id", sa.String(length=200), nullable=True),
        sa.Column("repo", sa.String(length=200), nullable=False),
        sa.Column("number", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column(
            "state",
            sa.String(length=20),
            nullable=False,
            server_default="open",
        ),
        # Resolved via identity (#117); SET NULL so the PR row survives a
        # user row going away.
        sa.Column(
            "author_user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("branch", sa.String(length=200), nullable=False),
        sa.Column("head_sha", sa.String(length=64), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merge_commit_sha", sa.String(length=64), nullable=True),
        sa.Column("external_url", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "project_id", "provider", "repo", "number",
            name="uq_pull_requests_project_provider_repo_number",
        ),
        sa.CheckConstraint(
            "provider IN ('github', 'gitlab')",
            name="ck_pull_requests_provider",
        ),
        sa.CheckConstraint(
            "state IN ('open', 'closed', 'merged')",
            name="ck_pull_requests_state",
        ),
    )
    op.create_index(
        "idx_pull_requests_project_state",
        "pull_requests",
        ["project_id", "state"],
    )
    op.create_index(
        "idx_pull_requests_project_created",
        "pull_requests",
        ["project_id", "created_at"],
    )

    op.create_table(
        "ticket_pr_links",
        sa.Column(
            "ticket_id",
            sa.String(length=64),
            sa.ForeignKey("tickets.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "pr_id",
            sa.String(length=64),
            sa.ForeignKey("pull_requests.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "link_source",
            sa.String(length=20),
            nullable=False,
            server_default="manual",
        ),
        sa.CheckConstraint(
            "link_source IN ('auto_parsed', 'manual', 'commit_message', 'loop')",
            name="ck_ticket_pr_links_link_source",
        ),
    )
    op.create_index("idx_ticket_pr_links_pr", "ticket_pr_links", ["pr_id"])

    op.create_table(
        "releases",
        sa.Column("id", sa.String(length=64), primary_key=True),  # rel_<hex>
        sa.Column(
            "project_id",
            sa.String(length=64),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version", sa.String(length=100), nullable=False),
        sa.Column("tag_sha", sa.String(length=64), nullable=False),
        sa.Column("previous_tag_sha", sa.String(length=64), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "source",
            sa.String(length=20),
            nullable=False,
            server_default="manual",
        ),
        sa.Column(
            "created_by_user_id",
            sa.String(length=64),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("service_key_id", sa.String(length=36), nullable=True),
        # Insertion time is the keyset cursor for GET …/releases (newest
        # first) — a manual record with no released_at still sorts stably.
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "project_id", "version", name="uq_releases_project_version"
        ),
        sa.CheckConstraint(
            "source IN ('github_release', 'gitlab_release', 'helm_sync', "
            "'manual')",
            name="ck_releases_source",
        ),
    )
    op.create_index(
        "idx_releases_project_created", "releases", ["project_id", "created_at"]
    )

    op.create_table(
        "release_prs",
        sa.Column(
            "release_id",
            sa.String(length=64),
            sa.ForeignKey("releases.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "pr_id",
            sa.String(length=64),
            sa.ForeignKey("pull_requests.id", ondelete="CASCADE"),
            primary_key=True,
        ),
    )
    op.create_index("idx_release_prs_pr", "release_prs", ["pr_id"])

    op.create_table(
        "release_gates",
        sa.Column("id", sa.String(length=64), primary_key=True),  # rg_<hex>
        sa.Column(
            "release_id",
            sa.String(length=64),
            sa.ForeignKey("releases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="passed",
        ),
        # CI check-run id that produced this gate (nullable — a manual gate
        # has no run to point at).
        sa.Column("run_id", sa.String(length=64), nullable=True),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "release_id", "kind", name="uq_release_gates_release_kind"
        ),
        sa.CheckConstraint(
            "status IN ('passed', 'failed', 'skipped')",
            name="ck_release_gates_status",
        ),
    )
    op.create_index(
        "idx_release_gates_release", "release_gates", ["release_id"]
    )

    op.create_table(
        "deployments",
        sa.Column("id", sa.String(length=64), primary_key=True),  # dep_<hex>
        sa.Column(
            "release_id",
            sa.String(length=64),
            sa.ForeignKey("releases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("environment", sa.String(length=64), nullable=False),
        sa.Column(
            "deployed_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "source",
            sa.String(length=20),
            nullable=False,
            server_default="manual",
        ),
        sa.CheckConstraint(
            "source IN ('argocd', 'helm', 'manual')",
            name="ck_deployments_source",
        ),
    )
    op.create_index(
        "idx_deployments_release", "deployments", ["release_id"]
    )


def downgrade() -> None:
    op.drop_index("idx_deployments_release", table_name="deployments")
    op.drop_table("deployments")
    op.drop_index("idx_release_gates_release", table_name="release_gates")
    op.drop_table("release_gates")
    op.drop_index("idx_release_prs_pr", table_name="release_prs")
    op.drop_table("release_prs")
    op.drop_index("idx_releases_project_created", table_name="releases")
    op.drop_table("releases")
    op.drop_index("idx_ticket_pr_links_pr", table_name="ticket_pr_links")
    op.drop_table("ticket_pr_links")
    op.drop_index("idx_pull_requests_project_created", table_name="pull_requests")
    op.drop_index("idx_pull_requests_project_state", table_name="pull_requests")
    op.drop_table("pull_requests")
