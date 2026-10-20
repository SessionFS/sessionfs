"""GitHub App installation ownership proof.

Revision ID: 073
Revises: 072

(Chained from 069 while other migrations are built in parallel; the merge
re-chains the number, the name stays.)

- github_installations gains account_id, installer_github_user_id (the
  signed webhook's sender) plus ownership proof columns
  (claim_verified_at, claim_verified_github_user_id, claim_verified_github_login,
  claim_role). Existing rows keep claim_verified_at = NULL: they were
  claimed through the old installation_id path that proved nothing, so they
  are treated as legacy claims that must be re-verified.
- github_claim_attempts: single-use OAuth state for the claim flow, plus a
  short-lived installation chooser (status 'choosing', choices_json).
- github_webhook_deliveries: signed-payload fingerprints claimed before any
  webhook work runs (replay protection; processing/completed/failed so
  unfinished work can be retried), pruned after 30 days.
- pr_comments gains desired_body / body_version / published_version so a
  PR event arriving while the comment is being created is not lost, and
  publish_lease / publish_lease_until so one publisher at a time edits the
  comment without holding a database lock across the GitHub call, and
  owner_user_id (the installation owner the body was built for) so a body
  is never published after the installation changed hands.
- project_repos: every repository verification derived from a GitHub App
  installation claim is revoked (verified=false, owner_attested). Before
  this migration a claim proved nothing about who controlled the
  installation, so none of that trust can be kept. Links are NOT deleted;
  owners re-verify by linking the repository again after proving
  ownership through the new OAuth flow. The revoked rows also lose their
  provider identity (provider / provider_repo_id), which was derived from
  the same unproven claim and would otherwise block the real owner after a
  repository rename.
- projects: repo-reclaimed projects are now represented by a NULL
  git_remote_normalized plus repo_reclaimed_at (a structural marker), not a
  user-reachable 'reclaimed:<id>' remote string — the placeholder namespace
  was attacker-claimable and could block a real displacement with a
  uniqueness conflict. The column becomes nullable; every pre-existing
  'reclaimed:' value (written by this migration's earlier form, by the
  displacement route, or by a squatter) is converted to NULL. NULL never
  matches a remote equality lookup, so reclaimed projects stay unreachable
  by remote.
- project_repos: backfill. Legacy projects were created with ONLY the
  projects.git_remote_normalized column and no project_repos row, which
  left an installation-claim hijack path (a verified link of the same
  remote conflicted with the legacy unique column instead of displacing
  it). Every active project that has a real remote but no project_repos
  rows gains an unverified primary row (verification_method
  'legacy_backfill'), matching what project creation now writes
  atomically. When the remote is already held by another project's
  project_repos row, the legacy column loses: it is set to NULL and the
  conflict is logged.
- github_installations: one linked installation per user, enforced by a
  partial unique index on user_id. Existing duplicates are resolved first:
  the most recently verified (then most recent) row stays linked, the
  others are detached with PR comments turned off.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "073"
down_revision = "072"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("github_installations") as batch:
        batch.add_column(sa.Column("account_id", sa.BigInteger(), nullable=True))
        batch.add_column(
            sa.Column("installer_github_user_id", sa.BigInteger(), nullable=True)
        )
        batch.add_column(
            sa.Column("claim_verified_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch.add_column(sa.Column("claim_verified_github_user_id", sa.BigInteger(), nullable=True))
        batch.add_column(sa.Column("claim_verified_github_login", sa.String(255), nullable=True))
        batch.add_column(sa.Column("claim_role", sa.String(20), nullable=True))

    # One linked installation per user. Detach duplicates before the index.
    op.execute(
        sa.text(
            "UPDATE github_installations SET user_id = NULL, auto_comment = :f "
            "WHERE user_id IS NOT NULL AND id NOT IN ("
            "  SELECT id FROM ("
            "    SELECT id, ROW_NUMBER() OVER ("
            "      PARTITION BY user_id ORDER BY "
            "        (claim_verified_at IS NULL), claim_verified_at DESC, "
            "        (created_at IS NULL), created_at DESC, id DESC"
            "    ) AS rn FROM github_installations WHERE user_id IS NOT NULL"
            "  ) ranked WHERE rn = 1"
            ")"
        ).bindparams(f=False)
    )
    op.create_index(
        "uq_github_installations_user",
        "github_installations",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("user_id IS NOT NULL"),
        sqlite_where=sa.text("user_id IS NOT NULL"),
    )

    op.create_table(
        "github_claim_attempts",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("state_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "user_id",
            sa.String(36),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("nonce_hash", sa.String(64), nullable=False),
        sa.Column("pkce_verifier_hash", sa.String(64), nullable=False),
        sa.Column("flow", sa.String(20), nullable=False),
        sa.Column("target_installation_id", sa.BigInteger(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("choices_json", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'consumed', 'choosing')",
            name="ck_github_claim_attempt_status",
        ),
        sa.CheckConstraint(
            "flow IN ('install', 'authorize')", name="ck_github_claim_attempt_flow"
        ),
    )
    op.create_index("idx_github_claim_attempt_user", "github_claim_attempts", ["user_id"])
    op.create_index(
        "idx_github_claim_attempt_expires", "github_claim_attempts", ["expires_at"]
    )

    op.create_table(
        "github_webhook_deliveries",
        sa.Column("fingerprint", sa.String(64), primary_key=True),
        sa.Column("delivery_id", sa.String(64), nullable=True),
        sa.Column("event", sa.String(64), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="processing"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("lease_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("claimed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("payload", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('processing', 'completed', 'failed')",
            name="ck_github_webhook_delivery_status",
        ),
    )
    op.create_index(
        "idx_github_webhook_delivery_received", "github_webhook_deliveries", ["received_at"]
    )

    with op.batch_alter_table("pr_comments") as batch:
        batch.add_column(sa.Column("desired_body", sa.Text(), nullable=True))
        batch.add_column(
            sa.Column("body_version", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("published_version", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("publish_lease", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column("publish_lease_until", sa.DateTime(timezone=True), nullable=True)
        )
        # The installation owner the desired_body was built for: publishing
        # is refused once the installation belongs to someone else.
        batch.add_column(sa.Column("owner_user_id", sa.String(36), nullable=True))

    # Revoke GitHub-derived repository trust created before ownership proof
    # existed (see module docstring). verified=true is only ever set by the
    # github_app path, so both predicates select the same rows; using both
    # also catches any row whose method was left inconsistent.
    op.execute(
        sa.text(
            "UPDATE project_repos SET verified = :f, verification_method = 'owner_attested', "
            "provider = NULL, provider_repo_id = NULL "
            "WHERE verification_method = 'github_app' OR verified = :t"
        ).bindparams(f=False, t=True)
    )

    # Repo-reclaimed projects are represented structurally from here on:
    # NULL git_remote_normalized + repo_reclaimed_at, never a user-reachable
    # placeholder string. Make the column nullable FIRST (PostgreSQL refuses
    # the NULL writes below otherwise; batch mode recreates the table on
    # SQLite). The unique constraint stays — both dialects treat NULLs as
    # distinct, so any number of reclaimed projects can coexist.
    with op.batch_alter_table("projects") as batch:
        batch.alter_column(
            "git_remote_normalized",
            existing_type=sa.String(255),
            nullable=True,
        )

    # Backfill project_repos for legacy projects: an active project with a
    # real remote but NO project_repos rows at all gains an unverified
    # primary row, exactly what project creation writes atomically now.
    # Skipped (and logged) when the remote is already held by another
    # project's row — the legacy column loses that conflict (below).
    op.execute(
        sa.text(
            "INSERT INTO project_repos (id, project_id, git_remote_normalized, "
            "provider, provider_repo_id, is_primary, verified, verification_method, "
            "added_by_user_id, created_at) "
            "SELECT 'repo_bf_' || p.id, p.id, p.git_remote_normalized, "
            "NULL, NULL, :t, :f, 'legacy_backfill', p.owner_id, CURRENT_TIMESTAMP "
            "FROM projects p "
            "WHERE p.merged_into_project_id IS NULL "
            "AND p.repo_reclaimed_at IS NULL "
            "AND p.git_remote_normalized IS NOT NULL "
            "AND p.git_remote_normalized NOT LIKE 'reclaimed:%' "
            "AND NOT EXISTS (SELECT 1 FROM project_repos pr WHERE pr.project_id = p.id) "
            "AND NOT EXISTS (SELECT 1 FROM project_repos pr2 "
            "  WHERE pr2.git_remote_normalized = p.git_remote_normalized)"
        ).bindparams(t=True, f=False)
    )
    # A project whose legacy remote is displaced and that has no repository
    # left becomes structurally reclaimed: NULL remote PLUS repo_reclaimed_at.
    # A merged project is a merge tombstone (merged_into_project_id), a
    # distinct state — its remote is still nulled but it is never marked
    # repo-reclaimed.
    conflicted = op.get_bind().execute(
        sa.text(
            "UPDATE projects SET "
            "git_remote_normalized = NULL, "
            "repo_reclaimed_at = CASE WHEN merged_into_project_id IS NULL "
            "  THEN CURRENT_TIMESTAMP ELSE repo_reclaimed_at END "
            "WHERE repo_reclaimed_at IS NULL "
            "AND git_remote_normalized IS NOT NULL "
            "AND git_remote_normalized NOT LIKE 'reclaimed:%' "
            "AND NOT EXISTS (SELECT 1 FROM project_repos pr WHERE pr.project_id = projects.id) "
            "AND EXISTS (SELECT 1 FROM project_repos pr2 "
            "  WHERE pr2.git_remote_normalized = projects.git_remote_normalized)"
        )
    )
    if conflicted.rowcount:
        print(
            f"073: nulled {conflicted.rowcount} legacy project remote(s) whose "
            "remote is already held by another project's project_repos row"
        )

    # A project that DOES have repository rows but whose legacy remote is
    # squatted by another project's project_repos row is skipped by both
    # statements above (each is gated on the project having NO repo rows).
    # Its legacy column still points at the squatted remote, so
    # auth/project_access.py keeps granting it access to any user with a
    # session on that remote — the real owner's members. The legacy column
    # loses here too: it is re-aligned to the project's own primary (else
    # oldest) repository, exactly as the displacement route does. The write
    # is split into a NULL pass and a re-alignment pass so the squatted
    # values are freed before the new (globally unique) ones are written —
    # a chain of squatters (P1 squats R, P2 squats P1's primary, …) would
    # otherwise violate the unique constraint mid-statement.
    squatted = op.get_bind().execute(
        sa.text(
            "UPDATE projects SET git_remote_normalized = NULL "
            "WHERE repo_reclaimed_at IS NULL "
            "AND git_remote_normalized IS NOT NULL "
            "AND git_remote_normalized NOT LIKE 'reclaimed:%' "
            "AND EXISTS (SELECT 1 FROM project_repos pr WHERE pr.project_id = projects.id) "
            "AND EXISTS (SELECT 1 FROM project_repos pr2 "
            "  WHERE pr2.git_remote_normalized = projects.git_remote_normalized "
            "  AND pr2.project_id != projects.id)"
        )
    )
    if squatted.rowcount:
        print(
            f"073: nulled {squatted.rowcount} squatted legacy remote(s) whose "
            "project still has repositories (re-aligned below)"
        )
    op.execute(
        sa.text(
            "UPDATE projects SET git_remote_normalized = ("
            "  SELECT pr.git_remote_normalized FROM project_repos pr "
            "  WHERE pr.project_id = projects.id "
            "  ORDER BY pr.is_primary DESC, pr.created_at ASC, pr.id ASC "
            "  LIMIT 1"
            ") "
            "WHERE repo_reclaimed_at IS NULL "
            "AND git_remote_normalized IS NULL "
            "AND EXISTS (SELECT 1 FROM project_repos pr WHERE pr.project_id = projects.id)"
        )
    )

    # Repo-reclaimed projects with no repository left no longer carry their
    # former remote (it would keep matching legacy remote fallbacks), and no
    # 'reclaimed:' placeholder string survives — the namespace is gone.
    op.execute(
        sa.text(
            "UPDATE projects SET git_remote_normalized = NULL "
            "WHERE repo_reclaimed_at IS NOT NULL "
            "AND id NOT IN (SELECT project_id FROM project_repos)"
        )
    )
    op.execute(
        sa.text(
            "UPDATE projects SET "
            "git_remote_normalized = NULL, "
            "repo_reclaimed_at = CASE WHEN merged_into_project_id IS NULL "
            "  THEN CURRENT_TIMESTAMP ELSE repo_reclaimed_at END "
            "WHERE git_remote_normalized LIKE 'reclaimed:%'"
        )
    )
    # A downgrade->upgrade round trip writes a 'merged:<id>' placeholder for
    # merge tombstones that lost their remote (the pre-073 column is NOT
    # NULL; see downgrade()). Restore the structural NULL representation for
    # those tombstones here — they are never repo-reclaimed, so they must not
    # be re-classified, but they also must not keep a synthetic remote.
    op.execute(
        sa.text(
            "UPDATE projects SET git_remote_normalized = NULL "
            "WHERE git_remote_normalized LIKE 'merged:%'"
        )
    )


def downgrade() -> None:
    # Restore the pre-073 representation for any structurally-reclaimed
    # project before the column becomes NOT NULL again. A merge tombstone
    # (merged_into_project_id IS NOT NULL, repo_reclaimed_at IS NULL) that
    # lost its remote above is a distinct state and must NOT be relabelled
    # 'reclaimed:' — that would hand it a value the legacy access paths
    # treat as a reclaimable remote. It gets a synthetic placeholder below.
    op.execute(
        sa.text(
            "UPDATE projects SET git_remote_normalized = 'reclaimed:' || id "
            "WHERE git_remote_normalized IS NULL "
            "AND (merged_into_project_id IS NULL OR repo_reclaimed_at IS NOT NULL)"
        )
    )
    # Merge tombstones that lost their remote above still need a non-NULL
    # value for the NOT NULL re-imposition below. Give them a unique,
    # non-user-reachable placeholder that is NOT 'reclaimed:' so a later
    # re-upgrade never mistakes a merged project for a reclaimed one.
    op.execute(
        sa.text(
            "UPDATE projects SET git_remote_normalized = 'merged:' || id "
            "WHERE git_remote_normalized IS NULL "
            "AND merged_into_project_id IS NOT NULL "
            "AND repo_reclaimed_at IS NULL"
        )
    )
    with op.batch_alter_table("projects") as batch:
        batch.alter_column(
            "git_remote_normalized",
            existing_type=sa.String(255),
            nullable=False,
        )
    op.drop_index("uq_github_installations_user", table_name="github_installations")
    # The project_repos trust revocation is intentionally not reversed: the
    # revoked verifications were never backed by proof.
    with op.batch_alter_table("pr_comments") as batch:
        batch.drop_column("owner_user_id")
        batch.drop_column("publish_lease_until")
        batch.drop_column("publish_lease")
        batch.drop_column("published_version")
        batch.drop_column("body_version")
        batch.drop_column("desired_body")
    op.drop_index(
        "idx_github_webhook_delivery_received", table_name="github_webhook_deliveries"
    )
    op.drop_table("github_webhook_deliveries")
    op.drop_index("idx_github_claim_attempt_expires", table_name="github_claim_attempts")
    op.drop_index("idx_github_claim_attempt_user", table_name="github_claim_attempts")
    op.drop_table("github_claim_attempts")
    with op.batch_alter_table("github_installations") as batch:
        batch.drop_column("claim_role")
        batch.drop_column("claim_verified_github_login")
        batch.drop_column("claim_verified_github_user_id")
        batch.drop_column("claim_verified_at")
        batch.drop_column("installer_github_user_id")
        batch.drop_column("account_id")
