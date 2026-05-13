"""v0.10.1 Phase 1 — schema foundations regression tests.

Pins migration 037: agent_personas, tickets, ticket_dependencies,
ticket_comments + Session.persona_name + Session.ticket_id columns.

Coverage:
- Column defaults round-trip (specializations='[]', priority='medium',
  status='open', is_active=true, version=1).
- UNIQUE(project_id, name) on agent_personas.
- CHECK ticket_id != depends_on_id on ticket_dependencies.
- ON DELETE CASCADE on project_id for personas + tickets.
- ON DELETE CASCADE on ticket_id for dependencies + comments.
- Session.persona_name and Session.ticket_id columns exist and are
  nullable; Session.ticket_id is indexed.
- Composite indexes present: idx_ticket_project_status,
  idx_ticket_assigned, idx_persona_project_active, idx_comment_ticket.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentPersona,
    Project,
    Ticket,
    TicketDependency,
    User,
)


@pytest.fixture
async def base_user(db_session: AsyncSession) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"u-{uuid.uuid4().hex[:8]}@example.com",
        display_name="Phase1 User",
        tier="team",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def base_project(
    db_session: AsyncSession, base_user: User
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="phase1-project",
        git_remote_normalized=f"acme/p1-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=base_user.id,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


# ── agent_personas column defaults + uniqueness ──────────────


@pytest.mark.asyncio
async def test_persona_column_defaults(
    db_session: AsyncSession, base_project: Project, base_user: User
):
    p = AgentPersona(
        id=f"per_{uuid.uuid4().hex[:16]}",
        project_id=base_project.id,
        name="atlas",
        role="Backend Architect",
        created_by=base_user.id,
    )
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    # Defaults applied server-side.
    assert p.content == ""
    assert p.specializations == "[]"
    assert p.is_active is True
    assert p.version == 1
    assert p.created_at is not None
    assert p.updated_at is not None


@pytest.mark.asyncio
async def test_persona_unique_name_per_project(
    db_session: AsyncSession, base_project: Project, base_user: User
):
    db_session.add(
        AgentPersona(
            id=f"per_{uuid.uuid4().hex[:16]}",
            project_id=base_project.id,
            name="atlas",
            role="Backend Architect",
            created_by=base_user.id,
        )
    )
    await db_session.commit()
    db_session.add(
        AgentPersona(
            id=f"per_{uuid.uuid4().hex[:16]}",
            project_id=base_project.id,
            name="atlas",  # duplicate within same project
            role="Different Role",
            created_by=base_user.id,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_persona_same_name_allowed_across_projects(
    db_session: AsyncSession, base_user: User
):
    p1 = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="alpha",
        git_remote_normalized=f"acme/alpha-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=base_user.id,
    )
    p2 = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="beta",
        git_remote_normalized=f"acme/beta-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=base_user.id,
    )
    db_session.add_all([p1, p2])
    await db_session.commit()

    db_session.add(
        AgentPersona(
            id=f"per_{uuid.uuid4().hex[:16]}",
            project_id=p1.id,
            name="atlas",
            role="Backend",
            created_by=base_user.id,
        )
    )
    db_session.add(
        AgentPersona(
            id=f"per_{uuid.uuid4().hex[:16]}",
            project_id=p2.id,
            name="atlas",  # same name, different project
            role="Backend",
            created_by=base_user.id,
        )
    )
    await db_session.commit()  # must succeed


# ── tickets column defaults ──────────────────────────────────


@pytest.mark.asyncio
async def test_ticket_column_defaults(
    db_session: AsyncSession, base_project: Project, base_user: User
):
    t = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=base_project.id,
        title="Fix the thing",
        created_by_user_id=base_user.id,
    )
    db_session.add(t)
    await db_session.commit()
    await db_session.refresh(t)
    assert t.description == ""
    assert t.priority == "medium"
    assert t.status == "open"
    assert t.context_refs == "[]"
    assert t.file_refs == "[]"
    assert t.related_sessions == "[]"
    assert t.acceptance_criteria == "[]"
    assert t.changed_files == "[]"
    assert t.knowledge_entry_ids == "[]"
    assert t.resolved_at is None


# ── ticket_dependencies CHECK constraint ────────────────────


@pytest.mark.asyncio
async def test_ticket_cannot_depend_on_itself(
    db_session: AsyncSession, base_project: Project, base_user: User
):
    t = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=base_project.id,
        title="Self-loop attempt",
        created_by_user_id=base_user.id,
    )
    db_session.add(t)
    await db_session.commit()

    db_session.add(TicketDependency(ticket_id=t.id, depends_on_id=t.id))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# ── ON DELETE CASCADE ────────────────────────────────────────


@pytest.mark.asyncio
async def test_fk_ondelete_cascade_metadata(db_session: AsyncSession):
    """Schema-inspection canary for ON DELETE CASCADE.

    SQLite (the test DB) does not enforce FK cascades by default; the
    contract is enforced at PG runtime. Same pattern Phase 1 of
    v0.10.0 used (test_org_scoped_projects.py): inspect the metadata
    to assert the migration shipped the right ON DELETE clause.
    Behavioral cascade testing belongs in a PG-flavored CI job.
    """
    bind = db_session.bind
    assert bind is not None

    # agent_personas.project_id → projects.id CASCADE
    personas_fks = await _list_fks(bind, "agent_personas")
    project_fk = next(
        (fk for fk in personas_fks if fk["referred_table"] == "projects"), None
    )
    assert project_fk is not None
    assert project_fk["options"].get("ondelete", "").upper() == "CASCADE"

    # tickets.project_id → projects.id CASCADE
    tickets_fks = await _list_fks(bind, "tickets")
    t_project_fk = next(
        (fk for fk in tickets_fks if fk["referred_table"] == "projects"), None
    )
    assert t_project_fk is not None
    assert t_project_fk["options"].get("ondelete", "").upper() == "CASCADE"

    # ticket_dependencies both sides → tickets.id CASCADE
    deps_fks = await _list_fks(bind, "ticket_dependencies")
    assert len(deps_fks) == 2
    for fk in deps_fks:
        assert fk["referred_table"] == "tickets"
        assert fk["options"].get("ondelete", "").upper() == "CASCADE"

    # ticket_comments.ticket_id → tickets.id CASCADE
    comments_fks = await _list_fks(bind, "ticket_comments")
    t_fk = next(
        (fk for fk in comments_fks if fk["referred_table"] == "tickets"), None
    )
    assert t_fk is not None
    assert t_fk["options"].get("ondelete", "").upper() == "CASCADE"


async def _list_fks(bind, table: str) -> list[dict]:
    def _inspect(sync_conn):
        return inspect(sync_conn).get_foreign_keys(table)

    async with bind.connect() as conn:
        return await conn.run_sync(_inspect)


# ── Session.persona_name + Session.ticket_id columns ─────────


@pytest.mark.asyncio
async def test_session_has_persona_and_ticket_columns(db_session: AsyncSession):
    """Schema canary — both columns present, nullable, ticket_id indexed."""
    # Use sync inspector through the engine bound to db_session.
    bind = db_session.bind
    assert bind is not None
    column_names = {c["name"]: c for c in await _list_columns(bind, "sessions")}
    assert "persona_name" in column_names
    assert column_names["persona_name"]["nullable"] is True
    assert "ticket_id" in column_names
    assert column_names["ticket_id"]["nullable"] is True

    # An index over ticket_id MUST exist. The migration creates it as
    # `idx_sessions_ticket_id`; SQLAlchemy auto-derives `ix_sessions_
    # ticket_id` from `index=True`. The test DB is built from ORM
    # metadata, so the `ix_` form appears here; the `idx_` form lands
    # in production via the migration. Accept either.
    indexes = await _list_indexes(bind, "sessions")
    ticket_indexes = [
        idx for idx in indexes
        if "ticket_id" in idx.get("column_names", [])
    ]
    assert len(ticket_indexes) >= 1, "ticket_id must be indexed on sessions"


async def _list_columns(bind, table: str) -> list[dict]:
    def _inspect(sync_conn):
        return inspect(sync_conn).get_columns(table)

    async with bind.connect() as conn:
        return await conn.run_sync(_inspect)


async def _list_indexes(bind, table: str) -> list[dict]:
    def _inspect(sync_conn):
        return inspect(sync_conn).get_indexes(table)

    async with bind.connect() as conn:
        return await conn.run_sync(_inspect)


# ── Composite indexes canary ─────────────────────────────────


@pytest.mark.asyncio
async def test_composite_indexes_present(db_session: AsyncSession):
    bind = db_session.bind
    assert bind is not None

    tickets = {idx["name"]: idx for idx in await _list_indexes(bind, "tickets")}
    assert "idx_ticket_project_status" in tickets
    assert tickets["idx_ticket_project_status"]["column_names"] == [
        "project_id",
        "status",
    ]
    assert "idx_ticket_assigned" in tickets
    assert tickets["idx_ticket_assigned"]["column_names"] == [
        "project_id",
        "assigned_to",
        "status",
    ]

    personas = {idx["name"]: idx for idx in await _list_indexes(bind, "agent_personas")}
    assert "idx_persona_project_active" in personas
    assert personas["idx_persona_project_active"]["column_names"] == [
        "project_id",
        "is_active",
    ]

    comments = {
        idx["name"]: idx for idx in await _list_indexes(bind, "ticket_comments")
    }
    assert "idx_comment_ticket" in comments
    assert comments["idx_comment_ticket"]["column_names"] == ["ticket_id"]

    # Reverse-lookup index for dependency enrichment (Codex Phase 1
    # Round 1, KB 316). The composite PK is (ticket_id, depends_on_id),
    # so a depends_on_id-only predicate can't use it.
    deps = {
        idx["name"]: idx for idx in await _list_indexes(bind, "ticket_dependencies")
    }
    assert "idx_ticket_deps_depends_on" in deps
    assert deps["idx_ticket_deps_depends_on"]["column_names"] == ["depends_on_id"]


@pytest.mark.asyncio
async def test_ticket_json_text_columns_are_not_null(db_session: AsyncSession):
    """Codex Phase 1 Round 1 (KB 316) — six JSON-as-text columns must
    be NOT NULL with server_default='[]' so raw SQL writes can't leave
    a NULL that json.loads() would crash on in Phase 2/3 route code.

    Schema-inspection canary: the test DB is built from ORM metadata,
    but the migration is independent; checking nullable here verifies
    both surfaces declare the same shape.
    """
    bind = db_session.bind
    assert bind is not None
    columns = {c["name"]: c for c in await _list_columns(bind, "tickets")}
    for field in (
        "context_refs",
        "file_refs",
        "related_sessions",
        "acceptance_criteria",
        "changed_files",
        "knowledge_entry_ids",
    ):
        assert field in columns, f"{field} missing from tickets table"
        assert columns[field]["nullable"] is False, (
            f"{field} must be NOT NULL — see Codex Phase 1 Round 1 (KB 316)"
        )


# ── Migration chain canary ───────────────────────────────────


def test_migration_037_module_chain_and_callable() -> None:
    """Migration 037 imports cleanly and declares the 036 → 037 chain."""
    import importlib

    module = importlib.import_module(
        "sessionfs.server.db.migrations.versions.037_agent_personas_and_tickets"
    )
    assert module.revision == "037"
    assert module.down_revision == "036"
    assert callable(module.upgrade)
    assert callable(module.downgrade)


def test_migration_037_source_declares_not_null_on_json_text_fields() -> None:
    """Codex Phase 1 Round 2 (KB 318) — pin the MIGRATION source.

    The schema canary `test_ticket_json_text_columns_are_not_null` only
    inspects the ORM-created test DB, so deleting `nullable=False` from
    migration 037 while leaving the ORM untouched would still pass — the
    test surface and the bug surface diverge. This test introspects the
    migration's source AST: it finds the `op.create_table("tickets", ...)`
    call and asserts each of the six JSON-as-text columns is declared
    with `nullable=False`.
    """
    import ast
    import importlib

    module = importlib.import_module(
        "sessionfs.server.db.migrations.versions.037_agent_personas_and_tickets"
    )
    source = ast.parse(open(module.__file__).read())

    # Find every op.create_table("tickets", ...) call and extract its
    # column declarations. There should be exactly one in upgrade().
    tickets_table_calls: list[ast.Call] = []
    for node in ast.walk(source):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "create_table"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "tickets"
        ):
            tickets_table_calls.append(node)

    assert len(tickets_table_calls) == 1, (
        "Expected exactly one op.create_table('tickets', ...) call in "
        "migration 037"
    )

    create_call = tickets_table_calls[0]

    # Each column is an sa.Column(name, ...) Call. Build a dict mapping
    # column name → set of keyword arg names so we can assert
    # nullable=False is present on the six fields.
    columns_with_kwargs: dict[str, dict[str, ast.expr]] = {}
    for arg in create_call.args[1:]:
        if (
            isinstance(arg, ast.Call)
            and isinstance(arg.func, ast.Attribute)
            and arg.func.attr == "Column"
            and arg.args
            and isinstance(arg.args[0], ast.Constant)
        ):
            col_name = arg.args[0].value
            columns_with_kwargs[col_name] = {kw.arg: kw.value for kw in arg.keywords}

    required_not_null_fields = {
        "context_refs",
        "file_refs",
        "related_sessions",
        "acceptance_criteria",
        "changed_files",
        "knowledge_entry_ids",
    }
    for field in required_not_null_fields:
        assert field in columns_with_kwargs, (
            f"Migration 037 is missing the `{field}` column on tickets"
        )
        nullable_arg = columns_with_kwargs[field].get("nullable")
        assert isinstance(nullable_arg, ast.Constant) and nullable_arg.value is False, (
            f"Migration 037 must declare `nullable=False` on tickets.{field} "
            f"— see Codex Phase 1 Round 2 (KB 318). Current value: "
            f"{ast.dump(nullable_arg) if nullable_arg else 'omitted'}."
        )
