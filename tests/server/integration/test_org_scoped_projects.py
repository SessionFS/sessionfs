"""v0.10.0 Phase 1 — schema foundations regression tests.

Pins migration 035: `Project.org_id`, `User.default_org_id`, and the
new `ProjectTransfer` state-machine table. The semantics under test:

- New columns default to NULL (existing rows untouched on upgrade).
- The new `ProjectTransfer` state-machine columns roundtrip with
  correct defaults (state='pending', accepted_by NULL, etc.).
- The FK constraints carry `ON DELETE SET NULL` for `projects.org_id`
  and `users.default_org_id` — verified via schema inspection because
  SQLite (the test DB) doesn't enforce FK cascades by default; the
  contract is enforced at PG runtime. Inspecting the metadata is the
  durable way to assert the migration shipped the right ON DELETE
  clause.
- Hot-path indexes are present, including the composite inbox index
  for the "incoming pending transfers for user X" dashboard query.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    Organization,
    Project,
    ProjectTransfer,
    User,
)


@pytest.fixture
async def base_user(db_session: AsyncSession) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"u-{uuid.uuid4().hex[:8]}@example.com",
        display_name="Phase1 User",
        tier="free",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def base_org(db_session: AsyncSession) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Phase1 Org",
        slug=f"phase1-{uuid.uuid4().hex[:8]}",
        tier="team",
    )
    db_session.add(org)
    await db_session.commit()
    await db_session.refresh(org)
    return org


@pytest.mark.asyncio
async def test_project_org_id_defaults_to_null(
    db_session: AsyncSession, base_user: User
) -> None:
    """Project created without org_id stays personal (org_id IS NULL).

    Locks the migration's contract: existing rows are untouched, new
    rows opt in to org-scope only when explicitly set.
    """
    proj = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name="personal proj",
        git_remote_normalized=f"github.com/x/{uuid.uuid4().hex[:8]}",
        owner_id=base_user.id,
    )
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)

    assert proj.org_id is None


@pytest.mark.asyncio
async def test_project_org_id_can_be_set(
    db_session: AsyncSession, base_user: User, base_org: Organization
) -> None:
    """Org-scoped project: assigning a valid org id roundtrips."""
    proj = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name="team proj",
        git_remote_normalized=f"github.com/y/{uuid.uuid4().hex[:8]}",
        owner_id=base_user.id,
        org_id=base_org.id,
    )
    db_session.add(proj)
    await db_session.commit()
    await db_session.refresh(proj)

    assert proj.org_id == base_org.id


@pytest.mark.asyncio
async def test_fk_ondelete_set_null_on_projects_org_id(db_engine) -> None:
    """FK on projects.org_id MUST be `ON DELETE SET NULL`.

    CEO's data-stays-access-revoked invariant (KB 230 #3): deleting an
    org demotes its projects to personal-scope rather than cascading a
    destroy. The owner_id FK keeps the row alive. The test DB (SQLite)
    doesn't enforce FK cascades by default — the contract lives in the
    schema metadata that PG honors at runtime. Inspect that directly.
    """
    async with db_engine.connect() as conn:
        fks = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_foreign_keys("projects")
        )

    org_fks = [fk for fk in fks if fk["referred_table"] == "organizations"]
    assert org_fks, "projects.org_id FK to organizations missing"
    org_fk = org_fks[0]
    assert "org_id" in org_fk["constrained_columns"]
    options = org_fk.get("options") or {}
    assert options.get("ondelete") == "SET NULL", (
        f"projects.org_id ondelete must be SET NULL, got {options.get('ondelete')!r}"
    )


@pytest.mark.asyncio
async def test_fk_ondelete_set_null_on_users_default_org_id(db_engine) -> None:
    """FK on users.default_org_id MUST be `ON DELETE SET NULL`.

    Same reasoning: a user whose default-org is deleted falls back to
    personal-scope, doesn't carry a dangling FK that breaks every
    subsequent `default_org_id` read.
    """
    async with db_engine.connect() as conn:
        fks = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_foreign_keys("users")
        )

    default_org_fks = [
        fk
        for fk in fks
        if fk["referred_table"] == "organizations"
        and "default_org_id" in fk["constrained_columns"]
    ]
    assert default_org_fks, "users.default_org_id FK to organizations missing"
    options = default_org_fks[0].get("options") or {}
    assert options.get("ondelete") == "SET NULL", (
        f"users.default_org_id ondelete must be SET NULL, got {options.get('ondelete')!r}"
    )


@pytest.mark.asyncio
async def test_project_transfer_roundtrip_and_indexes(
    db_session: AsyncSession, db_engine, base_user: User, base_org: Organization
) -> None:
    """ProjectTransfer table accepts a pending row, indexes present.

    Covers (a) the state machine columns default correctly,
    (b) state defaults to 'pending', (c) target_user_id sets correctly
    while accepted_by stays NULL on pending (Codex Phase-1 round-1
    catch — pending-inbox query needs the recipient on a separate
    column, not accepted_by), (d) the composite inbox index is
    `(state, target_user_id)` and is registered.
    """
    proj = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name="being-transferred",
        git_remote_normalized=f"github.com/t/{uuid.uuid4().hex[:8]}",
        owner_id=base_user.id,
        org_id=None,
    )
    db_session.add(proj)
    await db_session.commit()

    # Org→personal transfer: initiator is an admin (re-using
    # base_user here for test simplicity), target is base_user — the
    # recipient who must accept. target_user_id populated, state
    # remains pending, accepted_by stays NULL until the accept.
    transfer = ProjectTransfer(
        id=f"xfer_{uuid.uuid4().hex[:12]}",
        project_id=proj.id,
        initiated_by=base_user.id,
        target_user_id=base_user.id,
        from_scope="personal",
        to_scope=base_org.id,
    )
    db_session.add(transfer)
    await db_session.commit()
    await db_session.refresh(transfer)

    assert transfer.state == "pending"
    assert transfer.target_user_id == base_user.id
    assert transfer.accepted_by is None
    assert transfer.accepted_at is None
    assert transfer.created_at is not None
    assert transfer.updated_at is not None
    assert transfer.from_scope == "personal"
    assert transfer.to_scope == base_org.id

    # Indexes — name-checked against the live schema so a future
    # migration that drops/renames an index fails this test loudly.
    # Also verify the composite inbox index is on (state, target_user_id)
    # — using accepted_by would have shipped a dead index because
    # pending rows have accepted_by IS NULL by definition.
    async with db_engine.connect() as conn:
        transfer_indexes = await conn.run_sync(
            lambda sc: inspect(sc).get_indexes("project_transfers")
        )
        project_indexes = await conn.run_sync(
            lambda sc: {ix["name"] for ix in inspect(sc).get_indexes("projects")}
        )

    transfer_index_names = {ix["name"] for ix in transfer_indexes}
    assert "idx_project_transfers_project" in transfer_index_names
    assert "idx_project_transfers_state" in transfer_index_names
    assert "idx_project_transfers_inbox" in transfer_index_names
    assert "idx_projects_org_id" in project_indexes

    # The inbox index MUST be on target_user_id, not accepted_by.
    inbox_ix = next(
        ix for ix in transfer_indexes if ix["name"] == "idx_project_transfers_inbox"
    )
    assert inbox_ix["column_names"] == ["state", "target_user_id"], (
        f"inbox index columns wrong: {inbox_ix['column_names']!r} "
        "(must be ['state', 'target_user_id'] — accepted_by is NULL on pending rows)"
    )


@pytest.mark.asyncio
async def test_audit_durability_project_id_set_null_not_cascade(
    db_engine,
) -> None:
    """Audit-durability invariant: project_transfers.project_id must
    be ON DELETE SET NULL, NEVER CASCADE.

    Codex Phase-1 round-2 catch (KB entry 235): the durability claim
    ("rows are NEVER deleted post-resolution") cannot hold if a real
    hard project delete cascade-destroys the audit row. The route at
    routes/projects.py:346-363 does `await db.delete(project)`. With
    ondelete=CASCADE on this FK, every transfer audit row for that
    project would vaporize.

    Inspect the FK metadata directly so a future migration that
    "fixes" this back to CASCADE fails this test loudly.
    """
    async with db_engine.connect() as conn:
        fks = await conn.run_sync(
            lambda sync_conn: inspect(sync_conn).get_foreign_keys(
                "project_transfers"
            )
        )

    project_fks = [
        fk for fk in fks if fk["referred_table"] == "projects"
    ]
    assert project_fks, (
        "project_transfers.project_id FK to projects missing"
    )
    project_fk = project_fks[0]
    assert "project_id" in project_fk["constrained_columns"]
    options = project_fk.get("options") or {}
    assert options.get("ondelete") == "SET NULL", (
        f"project_transfers.project_id ondelete must be SET NULL "
        f"(audit-durability invariant), got {options.get('ondelete')!r}"
    )


@pytest.mark.asyncio
async def test_project_transfer_records_both_snapshots(
    db_session: AsyncSession, base_user: User, base_org: Organization
) -> None:
    """Both snapshot columns roundtrip and serve distinct roles.

    `project_name_snapshot` is a human-readable label (display only,
    not unique). `project_git_remote_snapshot` is the STABLE durable
    identifier — `projects.git_remote_normalized` is unique=True so
    a snapshot of it disambiguates two deleted projects with the
    same display name. Codex Phase-1 round-3 catch (KB entry 238).

    The Phase 2 route layer populates both at create time. The
    "always populated" contract is enforced by the route, not by
    NOT NULL constraints.
    """
    git_remote = f"github.com/s/{uuid.uuid4().hex[:8]}"
    proj = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name="snapshot-source",
        git_remote_normalized=git_remote,
        owner_id=base_user.id,
    )
    db_session.add(proj)
    await db_session.commit()

    transfer = ProjectTransfer(
        id=f"xfer_{uuid.uuid4().hex[:12]}",
        project_id=proj.id,
        project_git_remote_snapshot=proj.git_remote_normalized,
        project_name_snapshot=proj.name,
        initiated_by=base_user.id,
        target_user_id=base_user.id,
        from_scope="personal",
        to_scope=base_org.id,
    )
    db_session.add(transfer)
    await db_session.commit()
    await db_session.refresh(transfer)

    assert transfer.project_name_snapshot == "snapshot-source"
    assert transfer.project_git_remote_snapshot == git_remote


@pytest.mark.asyncio
async def test_audit_survives_project_delete_with_stable_identity(
    db_session: AsyncSession, base_user: User, base_org: Organization
) -> None:
    """End-to-end durability: delete the project, audit row survives
    with project_id NULL but BOTH snapshots intact.

    Pins the full audit-durability contract that Codex chased across
    three rounds (KB entries 235, 238): the row outlives the project,
    and the surviving identity is the stable git_remote (not just
    the non-unique name).

    SQLite-test caveat: the test DB doesn't enforce FK cascades by
    default, so we manually null project_id to simulate the
    ON DELETE SET NULL that PG runtime performs. The FK-metadata
    check (test_audit_durability_project_id_set_null_not_cascade
    above) is the canary for the runtime semantics.
    """
    git_remote = f"github.com/will-be-deleted/{uuid.uuid4().hex[:8]}"
    proj = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name="doomed-project",
        git_remote_normalized=git_remote,
        owner_id=base_user.id,
    )
    db_session.add(proj)
    await db_session.commit()

    xfer_id = f"xfer_{uuid.uuid4().hex[:12]}"
    transfer = ProjectTransfer(
        id=xfer_id,
        project_id=proj.id,
        project_git_remote_snapshot=proj.git_remote_normalized,
        project_name_snapshot=proj.name,
        initiated_by=base_user.id,
        target_user_id=base_user.id,
        from_scope="personal",
        to_scope=base_org.id,
        state="accepted",  # resolved row, post-accept
    )
    db_session.add(transfer)
    await db_session.commit()

    # Simulate ON DELETE SET NULL (SQLite doesn't enforce, PG does).
    transfer.project_id = None
    await db_session.commit()
    await db_session.delete(proj)
    await db_session.commit()
    await db_session.refresh(transfer)

    # The audit row must survive...
    found = (
        await db_session.execute(
            select(ProjectTransfer).where(ProjectTransfer.id == xfer_id)
        )
    ).scalar_one_or_none()
    assert found is not None, "audit row destroyed on project delete"

    # ...with project_id NULL but stable identity preserved.
    assert found.project_id is None
    assert found.project_git_remote_snapshot == git_remote
    assert found.project_name_snapshot == "doomed-project"
    # Two deleted projects with the same name would still be
    # distinguishable via the git_remote snapshot — that's the
    # whole point of the round-3 fix.


def test_migration_035_module_chain_and_callable() -> None:
    """Migration 035 imports cleanly, declares the right chain.

    Codex Phase-1 round-1 LOW: the rest of this module verifies ORM
    metadata via `Base.metadata.create_all` (the test DB path), not
    Alembic itself. A targeted in-memory Alembic round-trip isn't
    practical here because earlier migrations in the chain use PG-
    only DDL (`CREATE INDEX ... USING GIN`) that fails on SQLite.
    Closing the gap by at least asserting 035 is importable, chains
    off 034, and exposes the required upgrade/downgrade callables.
    Deeper migration-runtime testing belongs in a PG-flavored CI job.
    """
    import importlib

    module = importlib.import_module(
        "sessionfs.server.db.migrations.versions.035_org_scoped_projects_and_transfers"
    )
    assert module.revision == "035"
    assert module.down_revision == "034"
    assert callable(module.upgrade)
    assert callable(module.downgrade)
