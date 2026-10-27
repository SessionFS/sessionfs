"""Session helper scope + access hardening (GitHub #104 part 1 review).

* Every tool in ``TOOLS`` run inside a project-scoped conversation returns
  nothing from a second project, even when the model names that project's
  ids explicitly.
* Restricted project visibility (GitHub #101): a member without a group
  grant and a captured-session reader see nothing from a restricted
  project; an org admin sees it.  Cross-org projects are never reachable.
* ``assigned_to_me``, the session scope bleed (a session attributed to
  project B on project A's remote), ticket/wiki citation filtering, the
  per-project audit fan-out, plan gates and the result caps.
"""

from __future__ import annotations

import json
import uuid

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

import sessionfs.server.routes.helper as helper_routes
from sessionfs.server.db.models import (
    AgentPersona,
    KnowledgeEntry,
    Organization,
    ProjectGroup,
    ProjectGroupAccess,
    ProjectGroupMember,
    TicketDependency,
    User,
)
from sessionfs.server.helper import tools as tools_mod
from sessionfs.server.helper.context import HelperAuditSink, HelperScope
from sessionfs.server.helper.tools import TOOLS, scope_project_ids
from sessionfs.server.services import jira_sync
from tests.server.unit.test_helper_coverage import (
    RecordingSink,
    _add_member,
    _ctx,
    _make_agent_run,
    _make_persona,
    _make_project,
    _make_session,
    _make_ticket,
    _make_user,
    _make_wiki_page,
    _make_wiki_revision,
    _now,
    _run,
)


@pytest.fixture(autouse=True)
async def _enterprise_test_user(db_session, test_user):
    test_user.tier = "enterprise"
    await db_session.commit()
    return test_user


@pytest.fixture(autouse=True)
def _no_real_jira(monkeypatch):
    """get_jira_issue resolves a ticket's live link; the scope matrix must
    refuse an out-of-scope ticket before ever constructing a Jira client."""

    class _UnreachableJira:
        def __init__(self, *args, **kwargs):
            raise AssertionError(
                "get_jira_issue reached the Jira client for an out-of-scope ticket"
            )

    monkeypatch.setattr(jira_sync, "JiraClient", _UnreachableJira)


async def _seed_project(
    db: AsyncSession, owner: User, marker: str, *, org: Organization | None = None
) -> dict:
    """A project whose every row carries ``marker`` in its visible text."""
    remote = f"github.com/acme/{marker.lower()}-{uuid.uuid4().hex[:6]}"
    project = await _make_project(
        db, owner=owner, org=org, remote=remote, context_document=f"# {marker} context"
    )
    project.name = f"{marker} project"
    await db.commit()
    attributed = await _make_session(
        db, owner, title=f"{marker} attributed session", project_id=project.id, remote=remote
    )
    legacy = await _make_session(
        db, owner, title=f"{marker} legacy session", project_id=None, remote=remote
    )
    attributed.messages_text = f"{marker} discussion of the login bug"
    legacy.messages_text = f"{marker} discussion of the login bug"
    db.add(
        KnowledgeEntry(
            project_id=project.id,
            session_id=attributed.id,
            user_id=owner.id,
            entry_type="decision",
            content=f"{marker} decision about the login bug",
            confidence=0.9,
            claim_class="claim",
            freshness_class="current",
            created_at=_now(),
        )
    )
    await db.commit()
    ticket = await _make_ticket(db, project, title=f"{marker} ticket", created_by=owner.id)
    page = await _make_wiki_page(db, project, slug=f"{marker.lower()}-page", title=f"{marker} page")
    run = await _make_agent_run(db, project, persona=f"{marker.lower()}-scout", ticket_id=ticket.id)
    persona = await _make_persona(db, project, name=f"{marker.lower()}-persona")
    return {
        "project": project,
        "attributed": attributed,
        "legacy": legacy,
        "ticket": ticket,
        "page": page,
        "run": run,
        "persona": persona,
    }


def _b_ids(b: dict) -> set[str]:
    return {
        b["project"].id,
        b["attributed"].id,
        b["legacy"].id,
        b["ticket"].id,
        b["run"].id,
    }


# Per tool: argument sets aimed at project B (the model trying to widen the
# scope) plus neutral ones.  Every tool in TOOLS must be listed.
def _tool_calls(a: dict, b: dict) -> dict[str, list[dict]]:
    bp = b["project"].id
    return {
        "search_sessions": [
            {"query": "login"},
            {"query": "login", "project_id": bp},
            {"model": "unknown", "project_id": bp},
        ],
        "list_recent_sessions": [
            {},
            {"project_id": bp},
            {"model": "unknown", "project_id": bp},
        ],
        "list_session_models": [{}, {"project_id": bp}],
        "find_related_sessions": [{"error_text": "login"}, {"error_text": "login", "project_id": bp}],
        "get_session_summary": [{"session_id": b["attributed"].id}, {"session_id": b["legacy"].id}],
        "get_session_excerpts": [{"session_id": b["attributed"].id}],
        "search_project_knowledge": [{"project_id": bp, "query": "login"}],
        "get_project_context": [{"project_id": bp}],
        "summarize_sessions": [{"session_ids": [a["attributed"].id, b["attributed"].id]}],
        "sessions_touching_path": [{"path": "src"}, {"path": "src", "project_id": bp}],
        "list_projects": [{}],
        "get_project_settings": [{"project_id": bp}],
        "set_project_auto_narrative": [{"project_ids": [bp], "enabled": True}],
        "search_sessionfs_docs": [{"query": "install"}],
        "get_sessionfs_doc": [{"slug": "quickstart"}],
        "list_tickets": [{}, {"project_id": bp}, {"assigned_to_me": True}],
        "get_ticket": [{"ticket_id": b["ticket"].id}],
        "get_jira_issue": [{"ticket_id": b["ticket"].id}],
        "list_wiki_pages": [{"project_id": bp}],
        "get_wiki_page": [{"project_id": bp, "slug": b["page"].slug}],
        "list_agent_runs": [{"project_id": bp}],
        "get_agent_run": [{"project_id": bp, "run_id": b["run"].id}],
        "list_personas": [{}, {"project_id": bp}],
        "trash_sessions": [
            {"session_ids": [b["attributed"].id, b["legacy"].id]},
            {"model": "unknown", "project_id": bp},
        ],
        "propose_ticket": [
            {
                "project_id": bp,
                "title": "Fix the login bug",
                "description": "Fix it",
                "sources": {
                    "title": ["user"],
                    "description": ["user"],
                    "acceptance_criteria": ["user"],
                },
            }
        ],
        "propose_assignment": [
            {"ticket_id": b["ticket"].id, "persona_name": "some-persona"}
        ],
        "propose_comment": [
            {
                "ticket_id": b["ticket"].id,
                "body": "Looks good",
                "sources": {"body": ["user"]},
            }
        ],
        "propose_jira_link": [
            {"ticket_id": b["ticket"].id, "issue_key": "ACME-7"}
        ],
        "propose_jira_overwrite": [
            {"ticket_id": b["ticket"].id, "direction": "to_jira"},
            {"ticket_id": b["ticket"].id, "direction": "from_jira"},
        ],
    }


def test_scope_matrix_covers_every_tool():
    placeholder = {
        k: type("X", (), {"id": "x", "slug": "x"})()
        for k in ("project", "attributed", "legacy", "ticket", "page", "run")
    }
    assert set(_tool_calls(placeholder, placeholder)) == set(TOOLS)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", sorted(TOOLS))
async def test_project_scope_never_returns_other_project_rows(
    db_session, test_user, tool_name
):
    a = await _seed_project(db_session, test_user, "ALPHA")
    b = await _seed_project(db_session, test_user, "BRAVO")
    # B's legacy session sits on A's remote too: it must not bleed into A.
    bleed = await _make_session(
        db_session,
        test_user,
        title="BRAVO session on alpha remote",
        project_id=b["project"].id,
        remote=a["project"].git_remote_normalized,
    )
    bleed.messages_text = "BRAVO discussion of the login bug"
    await db_session.commit()

    sink = RecordingSink()
    ctx = _ctx(db_session, test_user, scope=HelperScope(project_id=a["project"].id), audit=sink)
    forbidden_ids = _b_ids(b) | {bleed.id}
    for args in _tool_calls(a, b)[tool_name]:
        result = await _run(ctx, tool_name, args)
        assert "BRAVO" not in result.content, (tool_name, args, result.content)
        echoed = json.dumps(args)
        for oid in forbidden_ids:
            if oid not in echoed:
                assert oid not in result.content, (tool_name, args, oid)
    for _name, _args, refs in sink.records:
        assert b["project"].id not in (refs.get("project_ids") or [])
        assert not (set(refs.get("session_ids") or []) & {b["attributed"].id, bleed.id})
        assert b["ticket"].id not in (refs.get("ticket_ids") or [])
    assert b["ticket"].id not in ctx.seen_ticket_ids
    assert not any(pid == b["project"].id for pid, _ in ctx.seen_wiki_refs)


@pytest.mark.asyncio
async def test_session_attributed_elsewhere_on_scope_remote_is_excluded(db_session, test_user):
    """A session attributed to project B but synced on project A's remote
    never appears in an A-scoped conversation (legacy_only)."""
    remote = "github.com/acme/shared"
    pa = await _make_project(db_session, owner=test_user, remote=remote)
    pb = await _make_project(db_session, owner=test_user)
    in_a = await _make_session(db_session, test_user, title="A legacy", remote=remote)
    in_b = await _make_session(
        db_session, test_user, title="B on A remote", project_id=pb.id, remote=remote
    )
    ctx = _ctx(db_session, test_user, scope=HelperScope(project_id=pa.id), audit=RecordingSink())

    recent = await _run(ctx, "list_recent_sessions", {})
    assert in_a.id in recent.content
    assert in_b.id not in recent.content
    summary = await _run(ctx, "get_session_summary", {"session_id": in_b.id})
    assert summary.content == "Session not found."

    # The access predicate keeps the wider default.
    from sqlalchemy import select

    from sessionfs.server.auth.project_access import project_session_clause
    from sessionfs.server.db.models import Session

    wide = set(
        (
            await db_session.execute(
                select(Session.id).where(project_session_clause(pa))
            )
        ).scalars()
    )
    narrow = set(
        (
            await db_session.execute(
                select(Session.id).where(project_session_clause(pa, legacy_only=True))
            )
        ).scalars()
    )
    assert in_b.id in wide
    assert in_b.id not in narrow and in_a.id in narrow


# ── restricted visibility (GitHub #101) + cross-org ──


async def _restricted_org(db: AsyncSession) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Restricted",
        slug=f"r-{uuid.uuid4().hex[:8]}",
        tier="enterprise",
        settings=json.dumps({"general": {"restrict_project_visibility": True}}),
    )
    db.add(org)
    await db.commit()
    return org


async def _all_reads(ctx, seeded: dict) -> str:
    pid = seeded["project"].id
    calls = [
        ("list_projects", {}),
        ("list_tickets", {}),
        ("list_tickets", {"project_id": pid}),
        ("get_ticket", {"ticket_id": seeded["ticket"].id}),
        ("list_wiki_pages", {"project_id": pid}),
        ("get_wiki_page", {"project_id": pid, "slug": seeded["page"].slug}),
        ("list_agent_runs", {"project_id": pid}),
        ("get_agent_run", {"project_id": pid, "run_id": seeded["run"].id}),
        ("list_personas", {}),
        ("search_project_knowledge", {"project_id": pid, "query": "login"}),
        ("get_project_context", {"project_id": pid}),
    ]
    out = []
    for name, args in calls:
        out.append((await _run(ctx, name, args)).content)
    return "\n".join(out)


@pytest.mark.asyncio
async def test_restricted_member_without_grant_sees_nothing(db_session, test_user):
    owner = await _make_user(db_session, email=f"own-{uuid.uuid4().hex[:6]}@x.com", tier="enterprise")
    org = await _restricted_org(db_session)
    await _add_member(db_session, org=org, user=owner, role="owner")
    await _add_member(db_session, org=org, user=test_user, role="member")
    seeded = await _seed_project(db_session, owner, "HIDDEN", org=org)

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    text = await _all_reads(ctx, seeded)
    assert "HIDDEN" not in text
    assert seeded["ticket"].id not in ctx.seen_ticket_ids
    assert await scope_project_ids(db_session, test_user.id, HelperScope()) == set()


@pytest.mark.asyncio
async def test_restricted_captured_session_reader_sees_nothing(db_session, test_user):
    owner = await _make_user(db_session, email=f"own-{uuid.uuid4().hex[:6]}@x.com", tier="enterprise")
    org = await _restricted_org(db_session)
    await _add_member(db_session, org=org, user=owner, role="owner")
    seeded = await _seed_project(db_session, owner, "HIDDEN", org=org)
    # test_user is not a member but syncs sessions from the same repository.
    await _make_session(
        db_session, test_user, title="my capture", remote=seeded["project"].git_remote_normalized
    )

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    text = await _all_reads(ctx, seeded)
    assert "HIDDEN" not in text
    assert await scope_project_ids(db_session, test_user.id, HelperScope()) == set()


@pytest.mark.asyncio
async def test_restricted_org_admin_sees_project(db_session, test_user):
    owner = await _make_user(db_session, email=f"own-{uuid.uuid4().hex[:6]}@x.com", tier="enterprise")
    org = await _restricted_org(db_session)
    await _add_member(db_session, org=org, user=owner, role="owner")
    await _add_member(db_session, org=org, user=test_user, role="admin")
    seeded = await _seed_project(db_session, owner, "VISIBLE", org=org)

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    text = await _all_reads(ctx, seeded)
    assert "VISIBLE ticket" in text
    assert "VISIBLE page" in text
    assert "visible-scout" in text
    assert "visible-persona" in text
    assert "VISIBLE decision" in text
    assert seeded["ticket"].id in ctx.seen_ticket_ids
    assert seeded["project"].id in await scope_project_ids(
        db_session, test_user.id, HelperScope()
    )


@pytest.mark.asyncio
async def test_restricted_member_with_group_grant_sees_project(db_session, test_user):
    """A restricted org member holding a group grant (reader) can read the
    ticket and wiki tools for a project in that group (#101 positive case)."""
    owner = await _make_user(db_session, email=f"own-{uuid.uuid4().hex[:6]}@x.com", tier="enterprise")
    org = await _restricted_org(db_session)
    await _add_member(db_session, org=org, user=owner, role="owner")
    await _add_member(db_session, org=org, user=test_user, role="member")
    seeded = await _seed_project(db_session, owner, "GRANTED", org=org)

    group = ProjectGroup(
        id=f"pg_{uuid.uuid4().hex[:12]}",
        org_id=org.id,
        slug=f"g-{uuid.uuid4().hex[:8]}",
        name="Frontend",
        created_by_user_id=owner.id,
        created_at=_now(),
        updated_at=_now(),
    )
    db_session.add(group)
    await db_session.commit()
    db_session.add(
        ProjectGroupMember(
            group_id=group.id,
            user_id=test_user.id,
            role="reader",
            granted_by_user_id=owner.id,
            created_at=_now(),
        )
    )
    db_session.add(
        ProjectGroupAccess(
            group_id=group.id,
            project_id=seeded["project"].id,
            added_by_user_id=owner.id,
            created_at=_now(),
        )
    )
    await db_session.commit()

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    text = await _all_reads(ctx, seeded)
    assert "GRANTED ticket" in text
    assert "GRANTED page" in text
    assert seeded["ticket"].id in ctx.seen_ticket_ids
    assert seeded["project"].id in await scope_project_ids(
        db_session, test_user.id, HelperScope()
    )


@pytest.mark.asyncio
async def test_cross_org_project_never_reachable(db_session, test_user):
    other_owner = await _make_user(
        db_session, email=f"oo-{uuid.uuid4().hex[:6]}@x.com", tier="enterprise"
    )
    other_org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}", name="Other", slug=f"o-{uuid.uuid4().hex[:8]}",
        tier="enterprise",
    )
    db_session.add(other_org)
    await db_session.commit()
    await _add_member(db_session, org=other_org, user=other_owner, role="owner")
    my_org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}", name="Mine", slug=f"m-{uuid.uuid4().hex[:8]}",
        tier="enterprise",
    )
    db_session.add(my_org)
    await db_session.commit()
    await _add_member(db_session, org=my_org, user=test_user, role="admin")
    seeded = await _seed_project(db_session, other_owner, "FOREIGN", org=other_org)

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    text = await _all_reads(ctx, seeded)
    assert "FOREIGN" not in text
    assert seeded["project"].id not in await scope_project_ids(
        db_session, test_user.id, HelperScope()
    )


# ── assigned_to_me ──


async def _org_persona(
    db: AsyncSession, org: Organization, name: str, *, represents: User | None, active: bool = True
) -> AgentPersona:
    persona = AgentPersona(
        id=f"ap_{uuid.uuid4().hex[:12]}",
        project_id=None,
        org_id=org.id,
        name=name,
        role="Engineer",
        is_active=active,
        represented_user_id=represents.id if represents else None,
        created_by=represents.id if represents else None,
        updated_at=_now(),
    )
    db.add(persona)
    await db.commit()
    return persona


@pytest.mark.asyncio
async def test_assigned_to_me_matches_created_and_represented_personas(db_session, test_user):
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}", name="Team", slug=f"t-{uuid.uuid4().hex[:8]}",
        tier="enterprise",
    )
    db_session.add(org)
    await db_session.commit()
    owner = await _make_user(db_session, email=f"own-{uuid.uuid4().hex[:6]}@x.com", tier="enterprise")
    await _add_member(db_session, org=org, user=owner, role="owner")
    await _add_member(db_session, org=org, user=test_user, role="member")
    pa = await _make_project(db_session, owner=owner, org=org)
    pb = await _make_project(db_session, owner=owner, org=org)
    await _org_persona(db_session, org, "Atlas", represents=test_user)
    await _org_persona(db_session, org, "Other", represents=owner)
    # In project B an active PROJECT persona named "atlas" overrides the org
    # persona, so an "atlas" ticket there is not the caller's.
    await _make_persona(db_session, pb, name="atlas")

    mine_created = await _make_ticket(db_session, pa, title="created by me", created_by=test_user.id)
    mine_assigned = await _make_ticket(db_session, pa, title="assigned to me", assigned_to="ATLAS")
    await _make_ticket(db_session, pa, title="assigned to other", assigned_to="Other")
    await _make_ticket(db_session, pb, title="shadowed atlas", assigned_to="atlas")
    nobody = await _make_ticket(db_session, pa, title="nobody's ticket")

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "list_tickets", {"assigned_to_me": True})
    assert mine_created.id in result.content
    assert mine_assigned.id in result.content
    assert "assigned to other" not in result.content
    assert "shadowed atlas" not in result.content
    assert nobody.id not in result.content


# ── ticket ids surfaced only when readable ──


@pytest.mark.asyncio
async def test_get_ticket_hides_unreadable_parent_and_dependency(db_session, test_user):
    mine = await _make_project(db_session, owner=test_user)
    stranger = await _make_user(db_session, email=f"s-{uuid.uuid4().hex[:6]}@x.com")
    theirs = await _make_project(db_session, owner=stranger)
    foreign_parent = await _make_ticket(db_session, theirs, title="foreign parent")
    foreign_dep = await _make_ticket(db_session, theirs, title="foreign dep")
    local_dep = await _make_ticket(db_session, mine, title="local dep")
    ticket = await _make_ticket(
        db_session, mine, title="child", parent_ticket_id=foreign_parent.id
    )
    db_session.add_all(
        [
            TicketDependency(ticket_id=ticket.id, depends_on_id=foreign_dep.id),
            TicketDependency(ticket_id=ticket.id, depends_on_id=local_dep.id),
        ]
    )
    await db_session.commit()

    sink = RecordingSink()
    ctx = _ctx(db_session, test_user, audit=sink)
    result = await _run(ctx, "get_ticket", {"ticket_id": ticket.id})
    assert foreign_parent.id not in result.content
    assert foreign_dep.id not in result.content
    assert f"[ticket:{local_dep.id}]" in result.content
    assert "parent: none" in result.content
    assert ctx.seen_ticket_ids == {ticket.id, local_dep.id}


@pytest.mark.asyncio
async def test_agent_run_ticket_only_citable_when_readable(db_session, test_user):
    mine = await _make_project(db_session, owner=test_user)
    stranger = await _make_user(db_session, email=f"s-{uuid.uuid4().hex[:6]}@x.com")
    theirs = await _make_project(db_session, owner=stranger)
    foreign = await _make_ticket(db_session, theirs, title="foreign")
    local = await _make_ticket(db_session, mine, title="local")
    r1 = await _make_agent_run(db_session, mine, ticket_id=foreign.id)
    await _make_agent_run(db_session, mine, ticket_id=local.id)

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    listed = await _run(ctx, "list_agent_runs", {"project_id": mine.id})
    assert f"[ticket:{local.id}]" in listed.content
    assert f"[ticket:{foreign.id}]" not in listed.content
    detail = await _run(ctx, "get_agent_run", {"project_id": mine.id, "run_id": r1.id})
    assert f"[ticket:{foreign.id}]" not in detail.content
    assert "(not accessible)" in detail.content
    assert foreign.id not in ctx.seen_ticket_ids
    assert local.id in ctx.seen_ticket_ids


@pytest.mark.asyncio
async def test_citations_filtered_to_scope_projects(db_session, test_user):
    pa = await _make_project(db_session, owner=test_user)
    pb = await _make_project(db_session, owner=test_user)
    tb = await _make_ticket(db_session, pb, title="B")
    await _make_wiki_page(db_session, pb, slug="arch")
    allowed = await scope_project_ids(db_session, test_user.id, HelperScope(project_id=pa.id))
    assert allowed == {pa.id}
    cites, cleaned = await helper_routes._parse_ticket_citations(
        db_session, f"x [ticket:{tb.id}]", {tb.id}, allowed
    )
    assert cites == [] and cleaned == "x "
    # Unscoped, B is accessible and the same citation is kept.
    allowed_all = await scope_project_ids(db_session, test_user.id, HelperScope())
    cites, _ = await helper_routes._parse_ticket_citations(
        db_session, f"[ticket:{tb.id}]", {tb.id}, allowed_all
    )
    assert [c["ticket_id"] for c in cites] == [tb.id]


# ── audit fan-out ──


@pytest.mark.asyncio
async def test_unscoped_list_audits_only_projects_with_rows(db_session, test_user):
    projects = [await _make_project(db_session, owner=test_user) for _ in range(5)]
    await _make_ticket(db_session, projects[1], title="one")
    await _make_ticket(db_session, projects[3], title="two")

    from sessionfs.server.helper.context import HelperContext, RetrievalBudget

    sink = HelperAuditSink(
        db_session, context_id="ra_test", user_id=test_user.id, persist=False
    )
    ctx = HelperContext(
        user=test_user,
        db=db_session,
        org_id=None,
        scope=HelperScope(),
        budget=RetrievalBudget(),
        audit=sink,
    )
    result = await _run(ctx, "list_tickets", {})
    assert not result.is_error
    assert sorted(e.project_id for e in sink.events) == sorted(
        [projects[1].id, projects[3].id]
    )
    # Identical repeat in the same turn: answered from the cache, no rows.
    await _run(ctx, "list_tickets", {})
    assert len(sink.events) == 2

    # Personas too: only projects that returned personas are audited.
    await _make_persona(db_session, projects[0], name="solo")
    await _run(ctx, "list_personas", {})
    persona_events = [e for e in sink.events if e.tool_name == "list_personas"]
    assert [e.project_id for e in persona_events] == [projects[0].id]


# ── wiki editor name, plan gates, caps ──


@pytest.mark.asyncio
async def test_wiki_editor_never_falls_back_to_email(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    nameless = await _make_user(db_session, email="secret-editor@example.com", display_name="")
    nameless.display_name = None
    await db_session.commit()
    await _make_wiki_page(db_session, project, slug="arch")
    await _make_wiki_revision(db_session, project, slug="arch", user_id=nameless.id)
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "get_wiki_page", {"project_id": project.id, "slug": "arch"})
    assert "secret-editor@example.com" not in result.content
    assert "last editor: a project member" in result.content


@pytest.mark.asyncio
async def test_plan_gated_project_returns_no_data(db_session, test_user):
    test_user.tier = "pro"  # tickets + agent runs need Team; personas need Pro
    await db_session.commit()
    project = await _make_project(db_session, owner=test_user)
    ticket = await _make_ticket(db_session, project, title="GATED ticket")
    run = await _make_agent_run(db_session, project, persona="gated-scout")

    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    listed = await _run(ctx, "list_tickets", {"project_id": project.id})
    assert listed.content == "Tickets are not available on this project's plan."
    unscoped = await _run(ctx, "list_tickets", {})
    assert unscoped.content == "No tickets accessible."
    got = await _run(ctx, "get_ticket", {"ticket_id": ticket.id})
    assert got.content == "Ticket not found."
    runs = await _run(ctx, "list_agent_runs", {"project_id": project.id})
    assert runs.content == "Agent runs are not available on this project's plan."
    one = await _run(ctx, "get_agent_run", {"project_id": project.id, "run_id": run.id})
    assert one.content == "Agent runs are not available on this project's plan."
    assert ctx.seen_ticket_ids == set()

    test_user.tier = "free"
    await db_session.commit()
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    await _make_persona(db_session, project, name="gated-persona")
    personas = await _run(ctx, "list_personas", {"project_id": project.id})
    assert personas.content == "Personas are not available on this project's plan."


@pytest.mark.asyncio
async def test_ticket_and_run_text_caps(db_session, test_user):
    project = await _make_project(db_session, owner=test_user)
    ticket = await _make_ticket(
        db_session,
        project,
        title="long",
        description="d" * 9_000,
        acceptance_criteria=json.dumps(["c" * 600] + ["ok"] * 60),
    )
    findings = [{"severity": "high", "message": "m" * 900}] * 25
    run = await _make_agent_run(
        db_session, project, result_summary="s" * 3_000, findings=json.dumps(findings)
    )
    ctx = _ctx(db_session, test_user, audit=RecordingSink())

    t = (await _run(ctx, "get_ticket", {"ticket_id": ticket.id})).content
    assert "d" * tools_mod.MAX_TICKET_DESCRIPTION_CHARS + "…[truncated]" in t
    assert "d" * (tools_mod.MAX_TICKET_DESCRIPTION_CHARS + 1) not in t
    assert "c" * tools_mod.MAX_TICKET_CRITERION_CHARS + "…[truncated]" in t
    assert f"[showing {tools_mod.MAX_TICKET_CRITERIA} of 61 criteria]" in t

    r = (await _run(ctx, "get_agent_run", {"project_id": project.id, "run_id": run.id})).content
    assert "s" * tools_mod.MAX_RUN_SUMMARY_CHARS + "…[truncated]" in r
    assert "s" * (tools_mod.MAX_RUN_SUMMARY_CHARS + 1) not in r
    assert "m" * tools_mod.MAX_FINDING_MESSAGE_CHARS + "…[truncated]" in r
    assert f"[showing {tools_mod.MAX_AGENT_RUN_FINDINGS} of 25 findings]" in r

    listed = (await _run(ctx, "list_agent_runs", {"project_id": project.id})).content
    assert "s" * tools_mod.MAX_RUN_SUMMARY_LINE_CHARS + "…[truncated]" in listed


@pytest.mark.asyncio
async def test_global_tool_result_cap(db_session, test_user, monkeypatch):
    monkeypatch.setattr(tools_mod, "MAX_TOOL_RESULT_CHARS", 500)
    project = await _make_project(db_session, owner=test_user, context_document="x" * 5_000)
    ctx = _ctx(db_session, test_user, audit=RecordingSink())
    result = await _run(ctx, "get_project_context", {"project_id": project.id})
    assert result.content.startswith("x" * 500)
    assert result.content.endswith("[truncated: tool result exceeded 500 characters]")
    assert len(result.content) < 600


def test_result_cap_fits_a_full_wiki_page():
    assert tools_mod.MAX_TOOL_RESULT_CHARS >= tools_mod.MAX_WIKI_BODY_CHARS
    # ...and stays well inside the per-turn retrieval budget (chars ≈ 4×tokens).
    from sessionfs.server.helper.context import DEFAULT_TOKENS_PER_TURN

    assert tools_mod.MAX_TOOL_RESULT_CHARS < DEFAULT_TOKENS_PER_TURN * 4

