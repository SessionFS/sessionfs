"""Read-tool coverage (GitHub #104 part 1) unit tests.

Covers the 7 new read tools — ``list_tickets`` / ``get_ticket`` /
``list_wiki_pages`` / ``get_wiki_page`` / ``list_agent_runs`` /
``get_agent_run`` / ``list_personas`` — against the real SQLite schema,
plus the cross-cutting fixes from the same brief:

  * every result line carries a source date (``YYYY-MM-DD (Nd ago)``),
  * project-scope is a hard filter and multi-repo legacy sessions (NULL
    ``project_id`` + matching remote) still resolve to their project,
  * a repeated identical (tool, args) call within one turn returns a
    cached marker and writes no second retrieval-audit event, and
  * ``[ticket:…]`` / ``[wiki:…]`` citations are only accepted for ids a
    tool surfaced this turn.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession

import sessionfs.server.routes.helper as helper_routes
from sessionfs.judge.agent_stream import ToolCall
from sessionfs.server.auth.project_access import (
    project_session_clause,
    user_can_access_project,
    user_has_read_standing,
)
from sessionfs.server.email import mask_address
from sessionfs.server.db.models import (
    AgentPersona,
    AgentRun,
    KnowledgeEntry,
    KnowledgePage,
    OrgMember,
    Organization,
    Project,
    ProjectRepo,
    Session,
    Ticket,
    TicketComment,
    TicketDependency,
    User,
    WikiPageRevision,
)
from sessionfs.server.helper.context import (
    HelperContext,
    HelperScope,
    RetrievalBudget,
)
from sessionfs.server.helper.tools import (
    _IdentityScope,
    _age,
    _fmt_dt_with_age,
    _identity_person_label,
    _identity_scope,
    execute,
    tool_specs,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _tkid() -> str:
    return f"tk_{uuid.uuid4().hex[:16]}"


def _pid() -> str:
    return f"proj_{uuid.uuid4().hex[:16]}"


async def _make_user(
    db: AsyncSession, *, email: str, display_name: str = "Member", tier: str = "pro"
) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        display_name=display_name,
        tier=tier,
        email_verified=True,
        created_at=_now(),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_org(db: AsyncSession, *, tier: str = "team") -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Org",
        slug=f"slug-{uuid.uuid4().hex[:8]}",
        tier=tier,
    )
    db.add(org)
    await db.commit()
    return org


async def _add_member(
    db: AsyncSession, *, org: Organization, user: User, role: str = "member"
) -> None:
    db.add(
        OrgMember(
            org_id=org.id,
            user_id=user.id,
            role=role,
            invited_by=user.id,
            invited_at=_now(),
        )
    )
    await db.commit()


async def _make_project(
    db: AsyncSession,
    *,
    owner: User,
    org: Organization | None = None,
    remote: str | None = None,
    context_document: str = "# ctx",
) -> Project:
    project = Project(
        id=_pid(),
        name="Proj",
        git_remote_normalized=remote or f"github.com/t/{uuid.uuid4().hex[:8]}",
        context_document=context_document,
        owner_id=owner.id,
        org_id=org.id if org is not None else None,
    )
    db.add(project)
    await db.commit()
    return project


async def _make_session(
    db: AsyncSession,
    user: User,
    *,
    title: str = "Session",
    project_id: str | None = None,
    remote: str | None = None,
) -> Session:
    session = Session(
        id=f"ses_{uuid.uuid4().hex[:16]}",
        user_id=user.id,
        title=title,
        source_tool="claude-code",
        blob_key=f"blobs/{uuid.uuid4().hex}",
        blob_size_bytes=0,
        etag="x",
        project_id=project_id,
        git_remote_normalized=remote,
        created_at=_now(),
        updated_at=_now(),
        uploaded_at=_now(),
    )
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


async def _make_ticket(
    db: AsyncSession,
    project: Project,
    *,
    title: str = "Ticket",
    status: str = "open",
    kind: str = "task",
    assigned_to: str | None = None,
    assigned_user_id: str | None = None,
    created_by: User | None = None,
    description: str = "",
    acceptance_criteria: str = "[]",
    parent_ticket_id: str | None = None,
    resolved_at: datetime | None = None,
    updated_at: datetime | None = None,
    priority: str = "medium",
) -> Ticket:
    ticket = Ticket(
        id=_tkid(),
        project_id=project.id,
        kind=kind,
        title=title,
        description=description,
        priority=priority,
        assigned_to=assigned_to,
        assigned_user_id=assigned_user_id,
        created_by_user_id=(created_by or project.owner_id),
        status=status,
        acceptance_criteria=acceptance_criteria,
        parent_ticket_id=parent_ticket_id,
        resolved_at=resolved_at,
        created_at=_now(),
        updated_at=updated_at or _now(),
    )
    db.add(ticket)
    await db.commit()
    await db.refresh(ticket)
    return ticket


async def _make_wiki_page(
    db: AsyncSession,
    project: Project,
    *,
    slug: str = "architecture",
    title: str = "Architecture",
    content: str = "body",
    updated_at: datetime | None = None,
) -> KnowledgePage:
    page = KnowledgePage(
        id=f"wp_{uuid.uuid4().hex[:12]}",
        project_id=project.id,
        slug=slug,
        title=title,
        page_type="page",
        content=content,
        updated_at=updated_at or _now(),
    )
    db.add(page)
    await db.commit()
    await db.refresh(page)
    return page


async def _make_wiki_revision(
    db: AsyncSession, project: Project, *, slug: str, user_id: str | None = None
) -> WikiPageRevision:
    rev = WikiPageRevision(
        project_id=project.id,
        page_slug=slug,
        revision_number=1,
        title="Architecture",
        content_snapshot="body",
        user_id=user_id,
        revised_at=_now(),
    )
    db.add(rev)
    await db.commit()
    await db.refresh(rev)
    return rev


async def _make_agent_run(
    db: AsyncSession,
    project: Project,
    *,
    persona: str = "scout",
    status: str = "passed",
    ticket_id: str | None = None,
    severity: str | None = "high",
    findings: str = "[]",
    result_summary: str | None = None,
    created_at: datetime | None = None,
) -> AgentRun:
    run = AgentRun(
        id=f"run_{uuid.uuid4().hex[:12]}",
        project_id=project.id,
        persona_name=persona,
        status=status,
        ticket_id=ticket_id,
        severity=severity,
        findings=findings,
        findings_count=0,
        result_summary=result_summary,
        created_at=created_at or _now(),
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


async def _make_persona(
    db: AsyncSession,
    project: Project,
    *,
    name: str = "scout",
    role: str = "Security reviewer",
    is_active: bool = True,
) -> AgentPersona:
    persona = AgentPersona(
        id=f"ap_{uuid.uuid4().hex[:12]}",
        project_id=project.id,
        org_id=None,
        name=name,
        role=role,
        is_active=is_active,
        created_by=project.owner_id,
        updated_at=_now(),
    )
    db.add(persona)
    await db.commit()
    await db.refresh(persona)
    return persona


async def _make_org_persona(
    db: AsyncSession,
    org: Organization,
    *,
    name: str,
    represented: User,
    role: str = "Engineer",
    is_active: bool = True,
) -> AgentPersona:
    persona = AgentPersona(
        id=f"ap_{uuid.uuid4().hex[:12]}",
        project_id=None,
        org_id=org.id,
        name=name,
        role=role,
        is_active=is_active,
        represented_user_id=represented.id,
        created_by=represented.id,
        updated_at=_now(),
    )
    db.add(persona)
    await db.commit()
    await db.refresh(persona)
    return persona


@pytest.fixture(autouse=True)
async def _enterprise_test_user(db_session, test_user):
    """Tickets / agent runs / personas are plan-gated like their routes; the
    caller's personal projects resolve to the caller's own tier."""
    test_user.tier = "enterprise"
    await db_session.commit()
    return test_user


def _ctx(
    db: AsyncSession,
    user: User,
    *,
    scope: HelperScope | None = None,
    budget: RetrievalBudget | None = None,
    audit: "RecordingSink | None" = None,
) -> HelperContext:
    return HelperContext(
        user=user,
        db=db,
        org_id=None,
        scope=scope or HelperScope(),
        budget=budget or RetrievalBudget(),
        audit=audit,
        blob_store=None,
        dlp_policy=None,
    )


class RecordingSink:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict, dict]] = []

    async def record(self, tool_name: str, sanitized_args: dict, returned_refs: dict) -> None:
        self.records.append((tool_name, sanitized_args, returned_refs))


async def _run(ctx: HelperContext, name: str, args: dict):
    call = ToolCall(id=f"tc_{uuid.uuid4().hex[:8]}", name=name, arguments=args)
    return await execute(ctx, call)


# ── registry shape ──


def test_tool_specs_exposes_new_read_tools():
    names = {s.name for s in tool_specs()}
    assert {
        "list_tickets",
        "get_ticket",
        "list_wiki_pages",
        "get_wiki_page",
        "list_agent_runs",
        "get_agent_run",
        "list_personas",
        "resolve_identity",
        "list_project_groups",
    } <= names


# ── list_tickets ──


@pytest.mark.asyncio
async def test_list_tickets_happy_path(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    t = await _make_ticket(db_session, project, title="Fix the SSO redirect")
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "list_tickets", {"project_id": project.id})
    assert not result.is_error
    assert f"[ticket:{t.id}]" in result.content
    assert "Fix the SSO redirect" in result.content
    assert "kind: task" in result.content
    assert "status: open" in result.content
    assert "priority: medium" in result.content
    assert "project: Proj" in result.content
    # freshness: every line carries a source date
    assert "(" in result.content and ")" in result.content


@pytest.mark.asyncio
async def test_list_tickets_inaccessible_project_is_not_found(db_session, test_user):
    owner = await _make_user(db_session, email="owner@example.com")
    project = await _make_project(db_session, owner=owner)
    await _make_ticket(db_session, project, title="other project ticket")
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "list_tickets", {"project_id": project.id})
    assert not result.is_error
    assert result.content == "No tickets accessible."


@pytest.mark.asyncio
async def test_list_tickets_scope_project_not_widened(db_session, test_user):
    pa = await _make_project(db_session, owner=test_user)
    pb = await _make_project(db_session, owner=test_user)
    await _make_ticket(db_session, pa, title="ticket in A")
    await _make_ticket(db_session, pb, title="ticket in B")

    ctx = _ctx(
        db_session, test_user, scope=HelperScope(project_id=pa.id), audit=RecordingSink()
    )
    # Model asks for B; scope (A) wins.
    denied = await _run(ctx, "list_tickets", {"project_id": pb.id})
    assert denied.content == "No tickets accessible."

    # No project_id: only A's tickets surface.
    ok = await _run(ctx, "list_tickets", {})
    assert "ticket in A" in ok.content
    assert "ticket in B" not in ok.content


@pytest.mark.asyncio
async def test_list_tickets_caps_and_notes_truncation(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    for i in range(4):
        await _make_ticket(db_session, project, title=f"ticket {i}")
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "list_tickets", {"project_id": project.id, "limit": 3})
    lines = [ln for ln in result.content.splitlines() if ln.startswith("[ticket:")]
    assert len(lines) == 3
    assert "showing 3 of more" in result.content


@pytest.mark.asyncio
async def test_list_tickets_filters_status_kind_and_assignee(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    await _make_ticket(db_session, project, title="open task", status="open", kind="task")
    await _make_ticket(db_session, project, title="done issue", status="done", kind="issue")
    # The assignee filter now resolves through personas/people (GitHub #117), so
    # the persona the ticket's ``assigned_to`` names must exist in scope.
    await _make_persona(db_session, project, name="scout")
    await _make_ticket(db_session, project, title="assigned", assigned_to="scout")

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    done = await _run(ctx, "list_tickets", {"project_id": project.id, "status": "done"})
    assert "done issue" in done.content
    assert "open task" not in done.content

    issues = await _run(ctx, "list_tickets", {"project_id": project.id, "kind": "issue"})
    assert "done issue" in issues.content
    assert "open task" not in issues.content

    assigned = await _run(ctx, "list_tickets", {"project_id": project.id, "assignee": "scout"})
    assert "assigned" in assigned.content
    assert "open task" not in assigned.content


# ── resolve_identity ──


@pytest.mark.asyncio
async def test_resolve_identity_matches_persona_exact_and_insensitive(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    await _make_persona(db_session, project, name="scout", role="Security reviewer")
    ctx = _ctx(db_session, test_user, audit=RecordingSink())

    exact = await _run(ctx, "resolve_identity", {"name": "scout"})
    assert not exact.is_error
    assert "Personas matching 'scout':" in exact.content
    assert "Security reviewer" in exact.content

    ci = await _run(ctx, "resolve_identity", {"name": "SCOUT"})
    assert "Personas matching 'SCOUT':" in ci.content
    assert "Security reviewer" in ci.content


@pytest.mark.asyncio
async def test_resolve_identity_matches_person_by_display_and_email(db_session, test_user):
    org = await _make_org(db_session)
    await _add_member(db_session, org=org, user=test_user, role="admin")
    alice = await _make_user(
        db_session, email="alice@example.com", display_name="Alice Smith"
    )
    await _add_member(db_session, org=org, user=alice, role="member")
    project = await _make_project(db_session, owner=test_user, org=org)

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    # Display-name match (caller is an org admin → full email is shown).
    result = await _run(ctx, "resolve_identity", {"name": "Alice Smith"})
    assert not result.is_error
    assert "People matching 'Alice Smith':" in result.content
    assert "Alice Smith" in result.content
    assert "alice@example.com" in result.content

    # Email local-part match.
    local = await _run(ctx, "resolve_identity", {"name": "alice"})
    assert "Alice Smith" in local.content

    # Full email match.
    full = await _run(ctx, "resolve_identity", {"name": "alice@example.com"})
    assert "Alice Smith" in full.content


@pytest.mark.asyncio
async def test_resolve_identity_no_match(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "resolve_identity", {"name": "nobody"})
    assert not result.is_error
    assert result.content == "No persona or person matched 'nobody'."


@pytest.mark.asyncio
async def test_resolve_identity_lists_persona_and_person_candidates(db_session, test_user):
    org = await _make_org(db_session)
    await _add_member(db_session, org=org, user=test_user, role="admin")
    alice = await _make_user(
        db_session, email="alice@example.com", display_name="Alice Smith"
    )
    await _add_member(db_session, org=org, user=alice, role="member")
    await _make_project(db_session, owner=test_user, org=org)
    # "alice" is both an org persona and the email local-part of a person, so
    # resolve_identity reports both candidates rather than picking one.
    await _make_org_persona(db_session, org, name="alice", represented=alice)

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "resolve_identity", {"name": "alice"})
    assert not result.is_error
    assert "Personas matching 'alice':" in result.content
    assert "People matching 'alice':" in result.content
    assert "Alice Smith" in result.content


@pytest.mark.asyncio
async def test_resolve_identity_person_in_other_org_never_resolves(db_session, test_user):
    my_org = await _make_org(db_session)
    await _add_member(db_session, org=my_org, user=test_user, role="admin")
    await _make_project(db_session, owner=test_user, org=my_org)

    other_org = await _make_org(db_session)
    alice = await _make_user(
        db_session, email="alice@example.com", display_name="Alice Smith"
    )
    await _add_member(db_session, org=other_org, user=alice, role="member")

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "resolve_identity", {"name": "alice"})
    assert not result.is_error
    assert result.content == "No persona or person matched 'alice'."


@pytest.mark.asyncio
async def test_list_tickets_assignee_matches_person_and_representing_persona(
    db_session, test_user,
):
    org = await _make_org(db_session)
    await _add_member(db_session, org=org, user=test_user, role="admin")
    alice = await _make_user(
        db_session, email="alice@example.com", display_name="Alice Smith"
    )
    await _add_member(db_session, org=org, user=alice, role="member")
    project = await _make_project(db_session, owner=test_user, org=org)

    # An org persona "alice" that represents Alice, plus a ticket assigned
    # directly to Alice's user id and another assigned to the persona name.
    await _make_org_persona(db_session, org, name="alice", represented=alice)
    await _make_ticket(db_session, project, title="direct", assigned_user_id=alice.id)
    await _make_ticket(db_session, project, title="via persona", assigned_to="alice")
    await _make_ticket(db_session, project, title="someone else", assigned_to="scout")

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "list_tickets", {"project_id": project.id, "assignee": "alice"})
    assert not result.is_error
    # The output names what "alice" resolved to.
    assert "'alice' matched personas alice; people Alice Smith" in result.content
    assert "direct" in result.content
    assert "via persona" in result.content
    assert "someone else" not in result.content


@pytest.mark.asyncio
async def test_list_tickets_assignee_unknown_is_reported(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    await _make_ticket(db_session, project, title="unassigned", assigned_to="scout")
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "list_tickets", {"project_id": project.id, "assignee": "ghost"})
    assert not result.is_error
    assert result.content == "No persona or person matched 'ghost'."


@pytest.mark.asyncio
async def test_list_tickets_assignee_persona_name_returns_each_ticket_once(
    db_session, test_user,
):
    org = await _make_org(db_session)
    await _add_member(db_session, org=org, user=test_user, role="admin")
    alice = await _make_user(
        db_session, email="alice@example.com", display_name="Alice Smith"
    )
    await _add_member(db_session, org=org, user=alice, role="member")
    project = await _make_project(db_session, owner=test_user, org=org)

    # "alice" is a persona name (several tickets) and the person the persona
    # represents (one direct assignment).  Every ticket must surface exactly
    # once — the persona and person paths are OR'd in one SQL query, never
    # unioned into duplicates.
    await _make_org_persona(db_session, org, name="alice", represented=alice)
    by_persona = [
        await _make_ticket(db_session, project, title=f"persona {i}", assigned_to="alice")
        for i in range(3)
    ]
    by_person = await _make_ticket(
        db_session, project, title="person", assigned_user_id=alice.id
    )
    other = await _make_ticket(db_session, project, title="other", assigned_to="scout")

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "list_tickets", {"project_id": project.id, "assignee": "alice"})
    assert not result.is_error
    for t in (*by_persona, by_person):
        assert result.content.count(t.id) == 1
    assert other.id not in result.content


@pytest.mark.asyncio
async def test_list_tickets_assignee_finds_deactivated_persona_tickets(
    db_session, test_user,
):
    # The raw assigned_to string must always be OR'd in (GitHub #117): a ticket
    # left assigned to a now-deactivated persona is still found by that name.
    project = await _make_project(db_session, owner=test_user)
    persona = await _make_persona(db_session, project, name="scout")
    await _make_ticket(db_session, project, title="legacy assignment", assigned_to="scout")
    persona.is_active = False
    await db_session.commit()

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "list_tickets", {"project_id": project.id, "assignee": "scout"})
    assert not result.is_error
    assert "legacy assignment" in result.content


@pytest.mark.asyncio
async def test_list_tickets_assignee_does_not_prefix_match_persona(
    db_session, test_user,
):
    # Persona matching is exact/case-insensitive only (GitHub #117): a short
    # assignee like "sa" must not pull persona "sam".
    project = await _make_project(db_session, owner=test_user)
    await _make_persona(db_session, project, name="sam")
    await _make_ticket(db_session, project, title="sam ticket", assigned_to="sam")

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "list_tickets", {"project_id": project.id, "assignee": "sa"})
    assert not result.is_error
    assert result.content == "No persona or person matched 'sa'."


@pytest.mark.asyncio
async def test_identity_scope_query_count_is_constant_in_project_count(
    db_engine, db_session, test_user,
):
    # GitHub #117: collecting personas/people must issue a fixed number of SQL
    # statements, never one query per project.  20 projects (each with a
    # persona) would blow past a small bound under the old per-project loop.
    org = await _make_org(db_session)
    await _add_member(db_session, org=org, user=test_user, role="admin")
    projects = [
        await _make_project(db_session, owner=test_user, org=org) for _ in range(20)
    ]
    for project in projects:
        await _make_persona(db_session, project, name="scout")

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    statements: list[str] = []

    def _on_execute(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(db_engine.sync_engine, "before_cursor_execute", _on_execute)
    try:
        scope = await _identity_scope(ctx, projects, name="scout")
    finally:
        event.remove(db_engine.sync_engine, "before_cursor_execute", _on_execute)

    assert len(projects) == 20
    assert scope.personas  # the batched project-persona query found every row
    assert len(statements) <= 8


def test_identity_person_label_falls_back_to_masked_email():
    # GitHub #117: the display-name fallback must be the address we actually
    # show — masked when the caller lacks read standing — never the raw email.
    user = SimpleNamespace(id="u1", email="alice@example.com", display_name=None)
    scope = _IdentityScope(
        personas=[],
        people={},
        allowed_org_ids={"org1"},
        representing_names={},
        represented_display={},
        standing_by_org={"org1": False},
    )
    label = _identity_person_label(user, {"org1"}, scope)
    masked = mask_address("alice@example.com")
    assert masked in label
    assert "alice@example.com" not in label


# ── get_ticket ──


@pytest.mark.asyncio
async def test_get_ticket_full_detail_and_surfaced_refs(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    parent = await _make_ticket(db_session, project, title="parent issue", kind="issue")
    dep = await _make_ticket(db_session, project, title="dependency")
    ticket = await _make_ticket(
        db_session,
        project,
        title="the task",
        description="do the thing",
        acceptance_criteria='["works", "tested"]',
        parent_ticket_id=parent.id,
    )
    db_session.add(TicketDependency(ticket_id=ticket.id, depends_on_id=dep.id))
    db_session.add(
        TicketComment(
            id=f"tc_{uuid.uuid4().hex[:12]}",
            ticket_id=ticket.id,
            author_user_id=test_user.id,
            content="looking good",
        )
    )
    await db_session.commit()

    sink = RecordingSink()
    ctx = _ctx(db_session, test_user, audit=sink)
    result = await _run(ctx, "get_ticket", {"ticket_id": ticket.id})
    assert not result.is_error
    assert f"[ticket:{ticket.id}]" in result.content
    assert "kind: task | status: open" in result.content
    assert "acceptance criteria:" in result.content
    assert "works" in result.content
    assert "description:" in result.content
    assert "do the thing" in result.content
    assert "looking good" in result.content
    # parent + dependency ids are also surfaced (so they may be cited)
    _, _, refs = sink.records[0]
    assert set(refs["ticket_ids"]) >= {ticket.id, parent.id, dep.id}


@pytest.mark.asyncio
async def test_get_ticket_inaccessible_is_not_found(db_session, test_user):
    owner = await _make_user(db_session, email="owner@example.com")
    project = await _make_project(db_session, owner=owner)
    ticket = await _make_ticket(db_session, project, title="secret ticket")
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "get_ticket", {"ticket_id": ticket.id})
    assert not result.is_error
    assert result.content == "Ticket not found."


@pytest.mark.asyncio
async def test_get_ticket_masks_emails_without_read_standing(db_session, test_user):
    owner = await _make_user(
        db_session, email="owner@example.com", display_name="Owner", tier="team"
    )
    project = await _make_project(
        db_session, owner=owner, remote="github.com/acme/repo"
    )
    creator = await _make_user(
        db_session, email="creator@example.com", display_name="Creator Person"
    )
    ticket = await _make_ticket(db_session, project, title="secret", created_by=creator.id)

    # The owner has read standing: the creator line shows the full address.
    owner_ctx = _ctx(db_session, owner, audit=RecordingSink())
    owner_result = await _run(owner_ctx, "get_ticket", {"ticket_id": ticket.id})
    assert "creator@example.com" in owner_result.content

    # A captured-session-only reader (no read standing) sees the masked address.
    stranger = await _make_user(db_session, email="stranger@example.com")
    await _make_session(db_session, stranger, project_id=None, remote="github.com/acme/repo")
    assert await user_can_access_project(db_session, stranger.id, project) is True
    assert await user_has_read_standing(db_session, stranger.id, project) is False

    stranger_ctx = _ctx(db_session, stranger, audit=RecordingSink())
    stranger_result = await _run(stranger_ctx, "get_ticket", {"ticket_id": ticket.id})
    assert "creator@example.com" not in stranger_result.content
    assert mask_address("creator@example.com") in stranger_result.content


@pytest.mark.asyncio
async def test_get_ticket_caps_comment_bodies(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    ticket = await _make_ticket(db_session, project, title="verbose ticket")
    db_session.add(
        TicketComment(
            id=f"tc_{uuid.uuid4().hex[:12]}",
            ticket_id=ticket.id,
            author_user_id=test_user.id,
            content="x" * 5_000,
        )
    )
    await db_session.commit()

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "get_ticket", {"ticket_id": ticket.id})
    assert not result.is_error
    # the full 5k body never surfaces; the capped body ends with an ellipsis
    assert "x" * 5_000 not in result.content
    assert ("x" * 2_000) not in result.content
    assert ("x" * 1_500) + "…[truncated]" in result.content


@pytest.mark.asyncio
async def test_get_ticket_worst_case_fits_result_cap(db_session, test_user):
    """Every per-part cap at its maximum still fits the 40k result cap, and a
    long dependency list is cut to the first 50 plus a "+N more" count."""
    import json as _json

    from sessionfs.server.helper.tools import (
        MAX_TICKET_COMMENTS,
        MAX_TICKET_DEPENDENCIES,
        MAX_TOOL_RESULT_CHARS,
    )

    project = await _make_project(db_session, owner=test_user)
    project.name = "P" * 200
    deps = [
        await _make_ticket(db_session, project, title=f"dep {i}")
        for i in range(MAX_TICKET_DEPENDENCIES + 10)
    ]
    ticket = await _make_ticket(
        db_session,
        project,
        title="t" * 200,
        description="d" * 50_000,
        acceptance_criteria=_json.dumps(["c" * 5_000] * 100),
    )
    for dep in deps:
        db_session.add(TicketDependency(ticket_id=ticket.id, depends_on_id=dep.id))
    for i in range(MAX_TICKET_COMMENTS + 5):
        db_session.add(
            TicketComment(
                id=f"tc_{uuid.uuid4().hex[:12]}",
                ticket_id=ticket.id,
                author_user_id=test_user.id,
                author_persona="p" * 50,
                content="x" * 20_000,
            )
        )
    await db_session.commit()

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "get_ticket", {"ticket_id": ticket.id})
    assert not result.is_error
    assert len(result.content) <= MAX_TOOL_RESULT_CHARS
    assert "[truncated: tool result exceeded" not in result.content
    assert "+10 more" in result.content
    shown = [d for d in deps if f"[ticket:{d.id}]" in result.content]
    assert len(shown) == MAX_TICKET_DEPENDENCIES
    # Only the shown dependencies become citable.
    assert len(ctx.seen_ticket_ids) == MAX_TICKET_DEPENDENCIES + 1


# ── list_wiki_pages / get_wiki_page ──


@pytest.mark.asyncio
async def test_list_wiki_pages_happy_path_and_refs(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    await _make_wiki_page(db_session, project, slug="architecture", title="Architecture")
    sink = RecordingSink()
    ctx = _ctx(db_session, test_user, audit=sink)
    result = await _run(ctx, "list_wiki_pages", {"project_id": project.id})
    assert not result.is_error
    assert f"[wiki:{project.id}/architecture]" in result.content
    assert "Architecture" in result.content
    _, _, refs = sink.records[0]
    assert (project.id, "architecture") in [tuple(r) for r in refs["wiki_refs"]]


@pytest.mark.asyncio
async def test_list_wiki_pages_inaccessible_is_not_found(db_session, test_user):
    owner = await _make_user(db_session, email="owner@example.com")
    project = await _make_project(db_session, owner=owner)
    await _make_wiki_page(db_session, project, slug="architecture")
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "list_wiki_pages", {"project_id": project.id})
    assert result.content == "Project not found."


@pytest.mark.asyncio
async def test_list_wiki_pages_caps_and_notes_truncation(db_session, test_user, monkeypatch):
    from sessionfs.server.helper import tools as tools_mod

    monkeypatch.setattr(tools_mod, "MAX_LIST_WIKI_PAGES", 2)
    project = await _make_project(db_session, owner=test_user)
    for i in range(3):
        await _make_wiki_page(db_session, project, slug=f"page-{i}", title=f"Page {i}")
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "list_wiki_pages", {"project_id": project.id})
    lines = [ln for ln in result.content.splitlines() if ln.startswith("[wiki:")]
    assert len(lines) == 2
    assert "showing 2 of more pages" in result.content


@pytest.mark.asyncio
async def test_get_wiki_page_happy_path_with_editor_and_date(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    await _make_wiki_page(db_session, project, slug="architecture", content="the body")
    await _make_wiki_revision(db_session, project, slug="architecture", user_id=test_user.id)
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "get_wiki_page", {"project_id": project.id, "slug": "architecture"})
    assert not result.is_error
    assert f"[wiki:{project.id}/architecture]" in result.content
    assert "the body" in result.content
    assert "last editor: Test User" in result.content
    assert "updated:" in result.content


@pytest.mark.asyncio
async def test_get_wiki_page_inaccessible_is_not_found(db_session, test_user):
    owner = await _make_user(db_session, email="owner@example.com")
    project = await _make_project(db_session, owner=owner)
    page = await _make_wiki_page(db_session, project, slug="architecture")
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "get_wiki_page", {"project_id": project.id, "slug": page.slug})
    assert result.content == "Project not found."


@pytest.mark.asyncio
async def test_get_wiki_page_truncates_body(db_session, test_user, monkeypatch):
    from sessionfs.server.helper import tools as tools_mod

    monkeypatch.setattr(tools_mod, "MAX_WIKI_BODY_CHARS", 5)
    project = await _make_project(db_session, owner=test_user)
    await _make_wiki_page(db_session, project, slug="long", content="1234567890")
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "get_wiki_page", {"project_id": project.id, "slug": "long"})
    assert "12345" in result.content
    assert "truncated" in result.content
    assert "67890" not in result.content


# ── list_agent_runs / get_agent_run ──


@pytest.mark.asyncio
async def test_list_agent_runs_happy_path(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    await _make_agent_run(db_session, project, persona="scout", status="passed")
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "list_agent_runs", {"project_id": project.id})
    assert not result.is_error
    assert "persona: scout" in result.content
    assert "status: passed" in result.content


@pytest.mark.asyncio
async def test_list_agent_runs_inaccessible_is_not_found(db_session, test_user):
    owner = await _make_user(db_session, email="owner@example.com")
    project = await _make_project(db_session, owner=owner)
    await _make_agent_run(db_session, project)
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "list_agent_runs", {"project_id": project.id})
    assert result.content == "Project not found."


@pytest.mark.asyncio
async def test_list_agent_runs_caps_and_notes_truncation(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    for _ in range(3):
        await _make_agent_run(db_session, project)
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "list_agent_runs", {"project_id": project.id, "limit": 2})
    lines = [ln for ln in result.content.splitlines() if "persona:" in ln]
    assert len(lines) == 2
    assert "showing 2 of more runs" in result.content


@pytest.mark.asyncio
async def test_get_agent_run_full_detail(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    ticket = await _make_ticket(db_session, project, title="linked ticket")
    run = await _make_agent_run(
        db_session,
        project,
        ticket_id=ticket.id,
        findings='[{"rule": "KSV-101", "severity": "high", "file": "a.py", "line": 3}]',
        result_summary="found a problem",
    )
    sink = RecordingSink()
    ctx = _ctx(db_session, test_user, audit=sink)
    result = await _run(ctx, "get_agent_run", {"project_id": project.id, "run_id": run.id})
    assert not result.is_error
    assert f"ticket: [ticket:{ticket.id}]" in result.content
    assert "KSV-101" in result.content
    assert "result_summary: found a problem" in result.content
    _, _, refs = sink.records[0]
    assert refs["ticket_ids"] == [ticket.id]


@pytest.mark.asyncio
async def test_get_agent_run_inaccessible_is_not_found(db_session, test_user):
    owner = await _make_user(db_session, email="owner@example.com")
    project = await _make_project(db_session, owner=owner)
    run = await _make_agent_run(db_session, project)
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "get_agent_run", {"project_id": project.id, "run_id": run.id})
    assert result.content == "Project not found."


# ── list_personas ──


@pytest.mark.asyncio
async def test_list_personas_happy_path(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    await _make_persona(db_session, project, name="scout", role="Security reviewer")
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "list_personas", {"project_id": project.id})
    assert not result.is_error
    assert "scout" in result.content
    assert "role: Security reviewer" in result.content
    assert "scope: project" in result.content


@pytest.mark.asyncio
async def test_list_personas_inaccessible_is_not_found(db_session, test_user):
    owner = await _make_user(db_session, email="owner@example.com")
    project = await _make_project(db_session, owner=owner)
    await _make_persona(db_session, project, name="scout")
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "list_personas", {"project_id": project.id})
    assert result.content == "No personas accessible."


# ── freshness: date formatting + KB fresh→stale ordering ──


def test_age_helpers_emit_relative_date_text():
    now = datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc)
    assert _age(now - timedelta(days=6, hours=2), now) == 6
    assert _age(now - timedelta(hours=2), now) == 0
    # Same-day results read "today" (age is relative to real "now").
    assert _fmt_dt_with_age(datetime.now(timezone.utc)).endswith("(today)")


@pytest.mark.asyncio
async def test_read_tool_lines_carry_a_source_date(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    six_days_ago = _now() - timedelta(days=6, hours=2)
    await _make_ticket(db_session, project, title="old ticket", updated_at=six_days_ago)
    await _make_wiki_page(db_session, project, slug="arch", title="Arch", updated_at=six_days_ago)
    await _make_agent_run(db_session, project, created_at=six_days_ago)

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    tickets = await _run(ctx, "list_tickets", {"project_id": project.id})
    assert "6d ago" in tickets.content
    wiki = await _run(ctx, "list_wiki_pages", {"project_id": project.id})
    assert "6d ago" in wiki.content
    runs = await _run(ctx, "list_agent_runs", {"project_id": project.id})
    assert "6d ago" in runs.content


@pytest.mark.asyncio
async def test_kb_results_ordered_fresh_to_stale(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    for content, freshness in (("STALE", "stale"), ("CURRENT", "current"), ("AGING", "aging")):
        db_session.add(
            KnowledgeEntry(
                project_id=project.id,
                session_id=f"ses_{uuid.uuid4().hex[:12]}",
                user_id=test_user.id,
                entry_type="decision",
                content=f"the {content} entry about SSO",
                confidence=0.9,
                claim_class="claim",
                freshness_class=freshness,
                created_at=_now(),
            )
        )
    await db_session.commit()

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "search_project_knowledge", {"project_id": project.id, "query": "SSO"})
    assert not result.is_error
    current = result.content.index("the CURRENT entry")
    aging = result.content.index("the AGING entry")
    stale = result.content.index("the STALE entry")
    assert current < aging < stale


# ── scope + multi-repo correctness ──


@pytest.mark.asyncio
async def test_two_projects_never_leak_read_rows(db_session, test_user):
    pa = await _make_project(db_session, owner=test_user)
    pb = await _make_project(db_session, owner=test_user)
    await _make_ticket(db_session, pa, title="ticket A")
    await _make_ticket(db_session, pb, title="ticket B")
    await _make_wiki_page(db_session, pa, slug="arch-a", title="Arch A")
    await _make_wiki_page(db_session, pb, slug="arch-b", title="Arch B")
    await _make_agent_run(db_session, pa, persona="scout-a")
    await _make_agent_run(db_session, pb, persona="scout-b")
    await _make_persona(db_session, pa, name="persona-a")
    await _make_persona(db_session, pb, name="persona-b")

    ctx = _ctx(db_session, test_user, audit=RecordingSink())

    tickets = await _run(ctx, "list_tickets", {"project_id": pa.id})
    assert "ticket A" in tickets.content
    assert "ticket B" not in tickets.content

    wiki = await _run(ctx, "list_wiki_pages", {"project_id": pa.id})
    assert "Arch A" in wiki.content
    assert "Arch B" not in wiki.content

    runs = await _run(ctx, "list_agent_runs", {"project_id": pa.id})
    assert "scout-a" in runs.content
    assert "scout-b" not in runs.content

    personas = await _run(ctx, "list_personas", {"project_id": pa.id})
    assert "persona-a" in personas.content
    assert "persona-b" not in personas.content


@pytest.mark.asyncio
async def test_project_session_clause_matches_legacy_null_project_id_session(db_session, test_user):
    project = await _make_project(db_session, owner=test_user, remote="github.com/acme/repo")
    matching = await _make_session(
        db_session, test_user, project_id=None, remote="github.com/acme/repo"
    )
    other = await _make_session(
        db_session, test_user, project_id=None, remote="github.com/other/repo"
    )

    rows = (
        await db_session.execute(
            select(Session.id).where(
                Session.user_id == test_user.id, project_session_clause(project)
            )
        )
    ).scalars().all()
    assert matching.id in rows
    assert other.id not in rows

    # A captured-session-only reader is admitted via the remote fallback.
    stranger = await _make_user(db_session, email="stranger@example.com")
    await _make_session(db_session, stranger, project_id=None, remote="github.com/acme/repo")
    assert await user_can_access_project(db_session, stranger.id, project) is True


@pytest.mark.asyncio
async def test_project_session_clause_matches_linked_project_repo_remote(db_session, test_user):
    # Primary remote is repo-a; repo-b is linked via ProjectRepo.  A legacy
    # session synced against repo-b (not the primary remote) still belongs.
    project = await _make_project(db_session, owner=test_user, remote="github.com/acme/repo-a")
    db_session.add(
        ProjectRepo(
            id=f"pr_{uuid.uuid4().hex[:12]}",
            project_id=project.id,
            git_remote_normalized="github.com/acme/repo-b",
        )
    )
    await db_session.commit()

    linked = await _make_session(
        db_session, test_user, project_id=None, remote="github.com/acme/repo-b"
    )
    stranger = await _make_session(
        db_session, test_user, project_id=None, remote="github.com/acme/repo-c"
    )

    rows = (
        await db_session.execute(
            select(Session.id).where(
                Session.user_id == test_user.id, project_session_clause(project)
            )
        )
    ).scalars().all()
    assert linked.id in rows
    assert stranger.id not in rows


@pytest.mark.asyncio
async def test_project_session_clause_excludes_reclaimed_project_remotes(db_session, test_user):
    project = await _make_project(db_session, owner=test_user, remote="github.com/acme/repo")
    project.repo_reclaimed_at = _now()
    await db_session.commit()

    # Remote-matched legacy sessions no longer belong once the repo is
    # reclaimed by a verified owner…
    legacy = await _make_session(
        db_session, test_user, project_id=None, remote="github.com/acme/repo"
    )
    # …but an explicitly-attributed session still does.
    attributed = await _make_session(
        db_session, test_user, project_id=project.id, remote="github.com/acme/repo"
    )

    rows = (
        await db_session.execute(
            select(Session.id).where(
                Session.user_id == test_user.id, project_session_clause(project)
            )
        )
    ).scalars().all()
    assert legacy.id not in rows
    assert attributed.id in rows


@pytest.mark.asyncio
async def test_get_ticket_scope_project_not_widened(db_session, test_user):
    pa = await _make_project(db_session, owner=test_user)
    pb = await _make_project(db_session, owner=test_user)
    ticket_b = await _make_ticket(db_session, pb, title="ticket in B")
    ctx = _ctx(db_session, test_user, scope=HelperScope(project_id=pa.id), audit=RecordingSink())
    result = await _run(ctx, "get_ticket", {"ticket_id": ticket_b.id})
    assert not result.is_error
    assert result.content == "Ticket not found."


@pytest.mark.asyncio
async def test_get_wiki_page_scope_project_not_widened(db_session, test_user):
    pa = await _make_project(db_session, owner=test_user)
    pb = await _make_project(db_session, owner=test_user)
    await _make_wiki_page(db_session, pb, slug="arch-b", title="Arch B")
    ctx = _ctx(db_session, test_user, scope=HelperScope(project_id=pa.id), audit=RecordingSink())
    result = await _run(ctx, "get_wiki_page", {"project_id": pb.id, "slug": "arch-b"})
    assert not result.is_error
    assert result.content == "Project not found."


@pytest.mark.asyncio
async def test_get_agent_run_scope_project_not_widened(db_session, test_user):
    pa = await _make_project(db_session, owner=test_user)
    pb = await _make_project(db_session, owner=test_user)
    run_b = await _make_agent_run(db_session, pb, persona="scout-b")
    ctx = _ctx(db_session, test_user, scope=HelperScope(project_id=pa.id), audit=RecordingSink())
    result = await _run(ctx, "get_agent_run", {"project_id": pb.id, "run_id": run_b.id})
    assert not result.is_error
    assert result.content == "Project not found."


# ── duplicate retrieval audit ──


@pytest.mark.asyncio
async def test_duplicate_retrieval_is_cached_without_second_audit_row(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    await _make_ticket(db_session, project, title="SSO fix")
    sink = RecordingSink()
    ctx = _ctx(db_session, test_user, audit=sink)

    first = await _run(ctx, "list_tickets", {"project_id": project.id})
    assert not first.is_error
    assert "SSO fix" in first.content

    second = await _run(ctx, "list_tickets", {"project_id": project.id})
    assert not second.is_error
    assert "already retrieved" in second.content
    assert "SSO fix" not in second.content

    assert len(sink.records) == 1


@pytest.mark.asyncio
async def test_distinct_args_still_run_and_audit(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    await _make_ticket(db_session, project, title="one")
    await _make_ticket(db_session, project, title="two", status="done")
    sink = RecordingSink()
    ctx = _ctx(db_session, test_user, audit=sink)

    await _run(ctx, "list_tickets", {"project_id": project.id})
    await _run(ctx, "list_tickets", {"project_id": project.id, "status": "done"})
    assert len(sink.records) == 2


# ── citation validation (server-side) ──


@pytest.mark.asyncio
async def test_execute_records_surfaced_ticket_and_wiki_refs(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    ticket = await _make_ticket(db_session, project, title="A ticket")
    await _make_wiki_page(db_session, project, slug="arch", title="Arch")

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    await _run(ctx, "list_tickets", {"project_id": project.id})
    await _run(ctx, "list_wiki_pages", {"project_id": project.id})

    assert ticket.id in ctx.seen_ticket_ids
    assert (project.id, "arch") in ctx.seen_wiki_refs


@pytest.mark.asyncio
async def test_ticket_citation_only_accepted_when_surfaced(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    ticket = await _make_ticket(db_session, project, title="Fix login")
    text = f"[ticket:{ticket.id}]"

    # Exists, but never surfaced by a tool this turn → dropped.
    citations, cleaned = await helper_routes._parse_ticket_citations(
        db_session, text, set(), {project.id}
    )
    assert citations == []
    assert cleaned == ""

    # Surfaced → kept and enriched.
    citations, cleaned = await helper_routes._parse_ticket_citations(
        db_session, text, {ticket.id}, {project.id}
    )
    assert [c["ticket_id"] for c in citations] == [ticket.id]
    assert citations[0]["title"] == "Fix login"
    assert citations[0]["project_id"] == project.id
    assert cleaned == text


@pytest.mark.asyncio
async def test_wiki_citation_only_accepted_when_surfaced(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    await _make_wiki_page(db_session, project, slug="architecture", title="Architecture")
    text = f"[wiki:{project.id}/architecture]"

    citations, cleaned = await helper_routes._parse_wiki_citations(
        db_session, text, set(), {project.id}
    )
    assert citations == []
    assert cleaned == ""

    citations, cleaned = await helper_routes._parse_wiki_citations(
        db_session, text, {(project.id, "architecture")}, {project.id}
    )
    assert [c["slug"] for c in citations] == ["architecture"]
    assert citations[0]["title"] == "Architecture"
    assert cleaned == text


def test_strip_ticket_and_wiki_citations_drop_unknown_ids():
    text = "[ticket:tk_0000000000000001] kept [ticket:tk_ffffffffffffffff]"
    assert helper_routes._strip_ticket_citations(text, {"tk_0000000000000001"}) == (
        "[ticket:tk_0000000000000001] kept "
    )
    assert helper_routes._strip_ticket_citations(text, set()) == " kept "

    wiki = "[wiki:proj_0000000000000001/arch] kept [wiki:proj_ffffffffffffffff/other]"
    assert helper_routes._strip_wiki_citations(wiki, {("proj_0000000000000001", "arch")}) == (
        "[wiki:proj_0000000000000001/arch] kept "
    )
    assert helper_routes._strip_wiki_citations(wiki, set()) == " kept "


@pytest.mark.asyncio
async def test_resolve_identity_and_list_tickets_accept_user_id(db_session, test_user):
    # The caller block and resolve_identity hand the model user ids, so both
    # tools must resolve a person by id (GitHub #117 round 2).
    org = await _make_org(db_session)
    await _add_member(db_session, org=org, user=test_user, role="admin")
    alice = await _make_user(
        db_session, email="alice@example.com", display_name="Alice Smith"
    )
    await _add_member(db_session, org=org, user=alice, role="member")
    project = await _make_project(db_session, owner=test_user, org=org)
    await _make_ticket(
        db_session, project, title="alice direct", assigned_user_id=alice.id
    )

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    found = await _run(ctx, "resolve_identity", {"name": alice.id})
    assert "Alice Smith" in found.content
    listed = await _run(
        ctx, "list_tickets", {"project_id": project.id, "assignee": alice.id}
    )
    assert "alice direct" in listed.content


@pytest.mark.asyncio
async def test_list_tickets_matched_persona_without_tickets_is_not_no_match(
    db_session, test_user,
):
    # A persona that exists but has nothing under the filter must not be
    # reported as unknown.
    project = await _make_project(db_session, owner=test_user)
    await _make_persona(db_session, project, name="atlas")
    await _make_ticket(
        db_session, project, title="atlas done", status="done", assigned_to="atlas"
    )

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(
        ctx,
        "list_tickets",
        {"project_id": project.id, "assignee": "atlas", "status": "open"},
    )
    assert not result.is_error
    assert "No persona or person matched" not in result.content
    assert "'atlas' matched personas atlas." in result.content
    assert "No tickets matched." in result.content


@pytest.mark.asyncio
async def test_list_tickets_assignee_stays_inside_listed_projects(db_session, test_user):
    # The raw assigned_to match never reaches a readable project that is not
    # being listed.
    listed = await _make_project(db_session, owner=test_user)
    other = await _make_project(db_session, owner=test_user)
    await _make_ticket(db_session, listed, title="in listed", assigned_to="ghost")
    await _make_ticket(db_session, other, title="in other", assigned_to="ghost")

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(
        ctx, "list_tickets", {"project_id": listed.id, "assignee": "ghost"}
    )
    assert "in listed" in result.content
    assert "in other" not in result.content


@pytest.mark.asyncio
async def test_resolve_identity_treats_like_wildcards_literally(db_session, test_user):
    org = await _make_org(db_session)
    await _add_member(db_session, org=org, user=test_user, role="admin")
    bob = await _make_user(db_session, email="bob@example.com", display_name="Bob Jones")
    await _add_member(db_session, org=org, user=bob, role="member")
    await _make_project(db_session, owner=test_user, org=org)

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    for needle in ("%", "_ob", "b%"):
        result = await _run(ctx, "resolve_identity", {"name": needle})
        assert "Bob Jones" not in result.content, needle
