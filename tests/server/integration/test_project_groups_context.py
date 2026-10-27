"""Project-group knowledge context (GitHub #101, phase 2) — HTTP + service tests.

Covers the deterministic group compile, the stored-context view rule, the
group-wide knowledge search / page list, and the readable-member-project
boundary that all three share. Helpers are imported from
``test_project_groups`` (the established cross-test pattern for this suite).
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    KnowledgeEntry,
    KnowledgePage,
    OrgAuditEvent,
    Project,
    ProjectGroupAccess,
)
from sessionfs.server.services import group_compiler
from sessionfs.server.services.group_compiler import (
    ClaimInput,
    ProjectInput,
    build_group_document,
)
from tests.server.integration.test_project_groups import (
    _capture_session,
    _grant,
    _group,
    _member,
    _org,
    _place,
    _user,
)

pytestmark = pytest.mark.asyncio


# ── Local helpers ──


async def _proj(
    db: AsyncSession,
    owner,
    *,
    org,
    name: str = "Alpha",
    context: str = "# Alpha Context\n\n## Key Decisions\n- Ship v1",
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=name,
        git_remote_normalized=f"github.com/acme/{uuid.uuid4().hex[:8]}",
        context_document=context,
        owner_id=owner.id,
        org_id=org.id,
    )
    db.add(project)
    await db.commit()
    return project


async def _claim(
    db: AsyncSession,
    project: Project,
    *,
    entry_type: str = "decision",
    content: str = "Use Postgres",
    confidence: float = 0.9,
    freshness: str = "current",
    claim_class: str = "claim",
    dismissed: bool = False,
    superseded_by: int | None = None,
) -> KnowledgeEntry:
    entry = KnowledgeEntry(
        project_id=project.id,
        session_id=f"ses_{uuid.uuid4().hex[:16]}",
        user_id=project.owner_id,
        entry_type=entry_type,
        content=content,
        confidence=confidence,
        claim_class=claim_class,
        freshness_class=freshness,
        dismissed=dismissed,
        superseded_by=superseded_by,
    )
    db.add(entry)
    await db.commit()
    await db.refresh(entry)
    return entry


async def _page(
    db: AsyncSession, project: Project, *, slug: str = "getting-started", title: str = "Getting Started"
) -> KnowledgePage:
    page = KnowledgePage(
        id=f"kp_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        slug=slug,
        title=title,
        page_type="section",
        content="# Getting Started",
    )
    db.add(page)
    await db.commit()
    return page


def _compile_url(org, group) -> str:
    return f"/api/v1/orgs/{org.id}/groups/{group.slug}/compile"


def _context_url(org, group) -> str:
    return f"/api/v1/orgs/{org.id}/groups/{group.slug}/context"


# ── Compile gating ──


async def test_compile_disabled_returns_409(client: AsyncClient, db_session: AsyncSession):
    owner, owner_h = await _user(db_session, "owner")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    project = await _proj(db_session, owner, org=org)
    group = await _group(db_session, org, owner, name="Core")
    await _place(db_session, group, project)
    # compile_enabled defaults to False: existing groups opt out.

    resp = await client.post(_compile_url(org, group), headers=owner_h)
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "group_compile_disabled"


async def test_compile_requires_editor_or_admin(client: AsyncClient, db_session: AsyncSession):
    owner, owner_h = await _user(db_session, "owner")
    editor, editor_h = await _user(db_session, "editor")
    reader, reader_h = await _user(db_session, "reader")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, editor, role="member")
    await _member(db_session, org, reader, role="member")
    project = await _proj(db_session, owner, org=org)
    group = await _group(db_session, org, owner, name="Core")
    await _place(db_session, group, project)
    await _grant(db_session, group, editor, "editor")
    await _grant(db_session, group, reader, "reader")
    group.compile_enabled = True
    await db_session.commit()

    # A group reader may view the group but not compile it.
    resp = await client.post(_compile_url(org, group), headers=reader_h)
    assert resp.status_code == 403, resp.text

    # A group editor may compile.
    resp = await client.post(_compile_url(org, group), headers=editor_h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["compiled_by_user_id"] == editor.id

    # An org owner (admin standing) may compile too.
    resp = await client.post(_compile_url(org, group), headers=owner_h)
    assert resp.status_code == 200, resp.text


async def test_compile_requires_project_groups_feature(client: AsyncClient, db_session: AsyncSession):
    owner, owner_h = await _user(db_session, "owner")
    org = await _org(db_session, tier="free")
    await _member(db_session, org, owner, role="owner")
    project = await _proj(db_session, owner, org=org)
    group = await _group(db_session, org, owner, name="Core")
    await _place(db_session, group, project)
    group.compile_enabled = True
    await db_session.commit()

    resp = await client.post(_compile_url(org, group), headers=owner_h)
    assert resp.status_code == 403, resp.text
    detail = resp.json()["error"]["details"]
    assert detail["error"] == "upgrade_required"
    assert detail["feature"] == "project_groups"


# ── Compile output and the stored-context view ──


async def test_compile_and_view_route(client: AsyncClient, db_session: AsyncSession):
    owner, owner_h = await _user(db_session, "owner")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    project = await _proj(db_session, owner, org=org, name="Alpha")
    await _claim(db_session, project, entry_type="decision", content="Ship v1", confidence=0.9)
    group = await _group(db_session, org, owner, name="Core")
    await _place(db_session, group, project)
    group.compile_enabled = True
    await db_session.commit()

    resp = await client.post(_compile_url(org, group), headers=owner_h)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["group_id"] == group.id
    assert body["slug"] == group.slug
    assert body["compiled_by_user_id"] == owner.id
    assert body["sources"] == [project.id]
    assert "# Core — Group Knowledge Context" in body["document"]
    assert "Ship v1" in body["document"]
    assert "Shaped by 1 claims from 1 projects" in body["document"]
    assert "last compile" in body["document"]

    # The view returns the stored document verbatim.
    resp = await client.get(_context_url(org, group), headers=owner_h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["document"] == body["document"]


async def test_context_view_not_compiled_404(client: AsyncClient, db_session: AsyncSession):
    owner, owner_h = await _user(db_session, "owner")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    project = await _proj(db_session, owner, org=org)
    group = await _group(db_session, org, owner, name="Core")
    await _place(db_session, group, project)

    resp = await client.get(_context_url(org, group), headers=owner_h)
    assert resp.status_code == 404, resp.text


async def test_context_view_partial_returns_403(client: AsyncClient, db_session: AsyncSession):
    owner, owner_h = await _user(db_session, "owner")
    reader, reader_h = await _user(db_session, "reader")
    org = await _org(db_session, restrict=True)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, reader, role="member")
    project_a = await _proj(db_session, owner, org=org, name="Alpha")
    project_b = await _proj(db_session, owner, org=org, name="Beta")
    group = await _group(db_session, org, owner, name="Core")
    await _place(db_session, group, project_a)
    await _place(db_session, group, project_b)
    await _grant(db_session, group, reader, "reader")
    group.compile_enabled = True
    await db_session.commit()

    resp = await client.post(_compile_url(org, group), headers=owner_h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["sources"] == [project_a.id, project_b.id]

    # While both projects are in the group, the reader's grant covers both.
    resp = await client.get(_context_url(org, group), headers=reader_h)
    assert resp.status_code == 200, resp.text

    # Remove Beta from the group: the reader can still read Alpha (grant) but
    # no longer Beta (removed, restricted org), so the stored context is partial.
    row = (
        await db_session.execute(
            select(ProjectGroupAccess).where(
                ProjectGroupAccess.group_id == group.id,
                ProjectGroupAccess.project_id == project_b.id,
            )
        )
    ).scalar_one()
    await db_session.delete(row)
    await db_session.commit()

    resp = await client.get(_context_url(org, group), headers=reader_h)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "group_context_partial"

    # The org owner still reads every source project.
    resp = await client.get(_context_url(org, group), headers=owner_h)
    assert resp.status_code == 200, resp.text


# ── Deterministic compile + word-budget cap (service level) ──


async def test_compile_deterministic_and_footer(db_session: AsyncSession):
    owner, _ = await _user(db_session, "owner")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    project = await _proj(db_session, owner, org=org, name="Alpha")
    await _claim(db_session, project, entry_type="decision", content="Use Postgres", confidence=0.95)
    await _claim(db_session, project, entry_type="pattern", content="Lay out src/tests", confidence=0.8)
    group = await _group(db_session, org, owner, name="Core")
    await _place(db_session, group, project)

    fixed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    stats = await group_compiler.compile_group_context(db_session, group, owner.id, now=fixed)
    doc1 = group.context_document
    assert stats == {"projects": 1, "claims": 2, "claims_omitted": 0, "truncated": False}

    # Same inputs (including the same timestamp) → byte-identical output.
    await group_compiler.compile_group_context(db_session, group, owner.id, now=fixed)
    assert group.context_document == doc1

    assert "# Core — Group Knowledge Context" in doc1
    assert "Use Postgres" in doc1
    assert "Lay out src/tests" in doc1
    assert "Shaped by 2 claims from 1 projects" in doc1
    assert "last compile 2026-01-02 03:04 UTC" in doc1
    assert group.context_sources == [project.id]
    assert group.compiled_by_user_id == owner.id
    assert group.context_compiled_at == fixed


# ── Group-wide search / pages + the readable-project boundary ──


async def test_search_and_pages_routes(client: AsyncClient, db_session: AsyncSession):
    owner, owner_h = await _user(db_session, "owner")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    project = await _proj(db_session, owner, org=org, name="Alpha")
    await _claim(db_session, project, entry_type="decision", content="Use Postgres for persistence", confidence=0.95)
    await _claim(db_session, project, entry_type="pattern", content="Lay out src/tests", confidence=0.8)
    await _claim(db_session, project, entry_type="decision", content="Dismissed decision", dismissed=True)
    await _page(db_session, project, slug="getting-started", title="Getting Started")
    group = await _group(db_session, org, owner, name="Core")
    await _place(db_session, group, project)

    base = f"/api/v1/orgs/{org.id}/groups/{group.slug}"

    # All active claims (the dismissed one is excluded), highest confidence first.
    resp = await client.get(f"{base}/knowledge/search", headers=owner_h)
    assert resp.status_code == 200, resp.text
    hits = resp.json()
    assert len(hits) == 2
    assert {h["project_name"] for h in hits} == {"Alpha"}
    assert hits[0]["confidence"] >= hits[1]["confidence"]

    # entry_type filter.
    resp = await client.get(f"{base}/knowledge/search?entry_type=decision", headers=owner_h)
    assert resp.status_code == 200, resp.text
    assert [h["entry_type"] for h in resp.json()] == ["decision"]

    # Full-text filter.
    resp = await client.get(f"{base}/knowledge/search?q=Postgres", headers=owner_h)
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1
    assert resp.json()[0]["content"] == "Use Postgres for persistence"

    # Invalid entry_type → 422.
    resp = await client.get(f"{base}/knowledge/search?entry_type=bogus", headers=owner_h)
    assert resp.status_code == 422, resp.text

    # Pages list.
    resp = await client.get(f"{base}/pages", headers=owner_h)
    assert resp.status_code == 200, resp.text
    pages = resp.json()
    assert [p["slug"] for p in pages] == ["getting-started"]
    assert pages[0]["project_name"] == "Alpha"


async def test_search_and_pages_filter_to_readable_projects(db_session: AsyncSession):
    owner, _ = await _user(db_session, "owner")
    stranger, _ = await _user(db_session, "stranger")
    org = await _org(db_session)  # unrestricted
    await _member(db_session, org, owner, role="owner")
    project_a = await _proj(db_session, owner, org=org, name="Alpha")
    project_b = await _proj(db_session, owner, org=org, name="Beta")
    await _claim(db_session, project_a, content="Alpha decision")
    await _claim(db_session, project_b, content="Beta decision")
    await _page(db_session, project_a, slug="alpha-page", title="Alpha Page")
    await _page(db_session, project_b, slug="beta-page", title="Beta Page")

    group = await _group(db_session, org, owner, name="All")
    await _place(db_session, group, project_a)
    await _place(db_session, group, project_b)

    # The stranger is not an org member but captured a session on Alpha only,
    # so in the unrestricted org they can read Alpha but not Beta. The group
    # reads must honour that boundary.
    await _capture_session(db_session, stranger, project_a)

    hits = await group_compiler.search_group_knowledge(db_session, group, stranger.id)
    assert {h["project_id"] for h in hits} == {project_a.id}
    assert all(h["project_name"] == "Alpha" for h in hits)

    pages = await group_compiler.list_group_pages(db_session, group, stranger.id)
    assert {p["project_id"] for p in pages} == {project_a.id}


# ── Turning compile on (PATCH compile_enabled) ──


def _patch_url(org, group) -> str:
    return f"/api/v1/orgs/{org.id}/groups/{group.id}"


async def test_enable_compile_via_api_then_compile(client: AsyncClient, db_session: AsyncSession):
    owner, owner_h = await _user(db_session, "owner")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    project = await _proj(db_session, owner, org=org, name="Alpha")
    await _claim(db_session, project, content="Ship v1")
    group = await _group(db_session, org, owner, name="Core")
    await _place(db_session, group, project)

    resp = await client.post(_compile_url(org, group), headers=owner_h)
    assert resp.status_code == 409, resp.text

    resp = await client.patch(_patch_url(org, group), json={"compile_enabled": True}, headers=owner_h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["compile_enabled"] is True
    assert resp.json()["name"] == "Core"  # untouched fields stay

    resp = await client.post(_compile_url(org, group), headers=owner_h)
    assert resp.status_code == 200, resp.text
    assert "Ship v1" in resp.json()["document"]

    detail = (await client.get(_patch_url(org, group), headers=owner_h)).json()
    assert detail["compile_enabled"] is True
    assert detail["context_compiled_at"] is not None

    events = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_id == org.id,
                OrgAuditEvent.event_type == "project_group_updated",
            )
        )
    ).scalars().all()
    assert len(events) == 1
    assert json.loads(events[0].before)["compile_enabled"] is False
    assert json.loads(events[0].after)["compile_enabled"] is True

    # An explicit null leaves the flag unchanged; false turns it off.
    resp = await client.patch(_patch_url(org, group), json={"compile_enabled": None}, headers=owner_h)
    assert resp.json()["compile_enabled"] is True
    resp = await client.patch(_patch_url(org, group), json={"compile_enabled": False}, headers=owner_h)
    assert resp.json()["compile_enabled"] is False
    resp = await client.post(_compile_url(org, group), headers=owner_h)
    assert resp.status_code == 409, resp.text


async def test_enable_compile_needs_group_admin(client: AsyncClient, db_session: AsyncSession):
    owner, _owner_h = await _user(db_session, "owner")
    editor, editor_h = await _user(db_session, "editor")
    gadmin, gadmin_h = await _user(db_session, "gadmin")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, editor, role="member")
    await _member(db_session, org, gadmin, role="member")
    group = await _group(db_session, org, owner, name="Core")
    await _grant(db_session, group, editor, "editor")
    await _grant(db_session, group, gadmin, "admin")

    resp = await client.patch(_patch_url(org, group), json={"compile_enabled": True}, headers=editor_h)
    assert resp.status_code == 403, resp.text
    resp = await client.patch(_patch_url(org, group), json={"compile_enabled": True}, headers=gadmin_h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["compile_enabled"] is True


async def test_enable_compile_is_plan_gated_but_disable_is_not(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_h = await _user(db_session, "owner")
    org = await _org(db_session, tier="free")
    await _member(db_session, org, owner, role="owner")
    group = await _group(db_session, org, owner, name="Core")

    resp = await client.patch(_patch_url(org, group), json={"compile_enabled": True}, headers=owner_h)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["details"]["feature"] == "project_groups"

    # A group enabled before a downgrade can still be switched off.
    group.compile_enabled = True
    await db_session.commit()
    resp = await client.patch(_patch_url(org, group), json={"compile_enabled": False}, headers=owner_h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["compile_enabled"] is False


# ── Bounded, fair, linear-time document build ──


def _claims(n: int, *, start: int = 1, entry_type: str = "decision", words: int = 5) -> tuple:
    return tuple(
        ClaimInput(
            id=start + i,
            entry_type=entry_type,
            content=" ".join(f"w{i}x{j}" for j in range(words)),
            confidence=0.5 + (i % 50) / 100,
            relevant_ts=float(i),
            has_entity=0,
        )
        for i in range(n)
    )


_FIXED = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc)


async def test_build_orders_claims_highest_value_first() -> None:
    claims = (
        ClaimInput(id=1, entry_type="decision", content="low", confidence=0.4, relevant_ts=1.0, has_entity=0),
        ClaimInput(id=2, entry_type="decision", content="high", confidence=0.9, relevant_ts=1.0, has_entity=0),
        ClaimInput(id=3, entry_type="decision", content="mid newer", confidence=0.7, relevant_ts=9.0, has_entity=0),
        ClaimInput(id=4, entry_type="decision", content="mid older", confidence=0.7, relevant_ts=2.0, has_entity=0),
    )
    doc = build_group_document(
        "Core", [ProjectInput(id="p1", name="Alpha", context="", claims=claims)], now=_FIXED
    ).text
    order = [doc.index(f"] {text}") for text in ("high", "mid newer", "mid older", "low")]
    assert order == sorted(order)


async def test_build_truncation_is_fair_and_keeps_structure() -> None:
    big = ProjectInput(id="p1", name="Alpha", context="# Alpha\n\nSummary text", claims=_claims(50, words=40))
    small = ProjectInput(id="p2", name="Beta", context="", claims=_claims(3, start=1000, entry_type="bug"))
    late = ProjectInput(id="p3", name="Gamma", context="", claims=_claims(50, start=2000, words=40))

    doc = build_group_document("Core", [big, small, late], now=_FIXED, max_words=900)
    text = doc.text

    assert doc.truncated
    assert len(text.split()) <= 900
    # Structure survives: header, every project heading, the note, the footer.
    assert text.startswith("# Core — Group Knowledge Context")
    for heading in ("## Alpha", "## Beta", "## Gamma"):
        assert heading in text
    assert "_…(truncated:" in text
    assert text.rstrip().splitlines()[-1].startswith("Shaped by ")
    # The small project keeps everything; the late large project still gets
    # its fair share instead of nothing.
    assert all(f"claim #{1000 + i}]" in text for i in range(3))
    assert any(f"claim #{2000 + i}]" in text for i in range(50))
    # The footer and stats report what was actually emitted.
    emitted = text.count("/claim #")
    assert doc.claims_emitted == emitted
    assert doc.claims_omitted == 103 - emitted
    assert f"Shaped by {emitted} claims from 3 projects" in text
    assert f"{doc.claims_omitted} lower-priority claims omitted" in text


async def test_build_tiny_budget_still_keeps_footer_and_note() -> None:
    project = ProjectInput(id="p1", name="Alpha", context="lots of words " * 100, claims=_claims(10))
    text = build_group_document("Core", [project], now=_FIXED, max_words=20).text
    assert "# Core — Group Knowledge Context" in text
    assert "## Alpha" in text
    assert "_…(truncated:" in text
    assert "Shaped by 0 claims from 1 projects" in text


async def test_build_quotes_project_context_so_headings_stay_nested() -> None:
    project = ProjectInput(id="p1", name="Alpha", context="# Alpha Context\n\n## Key Decisions", claims=())
    text = build_group_document("Core", [project], now=_FIXED).text
    assert "> # Alpha Context" in text
    assert "\n# Alpha Context" not in text


def _best_build_seconds(projects: list[ProjectInput], max_words: int, repeats: int = 3) -> float:
    best = float("inf")
    for _ in range(repeats):
        started = time.process_time()
        build_group_document("Core", projects, now=_FIXED, max_words=max_words)
        best = min(best, time.process_time() - started)
    return best


async def test_build_scales_linearly() -> None:
    """8x the projects (and budget): linear work is ~8x, quadratic ~64x.
    Min-of-rounds like the other linearity guards (noise only adds time)."""

    def _projects(n: int) -> list[ProjectInput]:
        return [
            ProjectInput(
                id=f"p{i}",
                name=f"Project {i}",
                context="context words " * 200,
                claims=_claims(50, start=i * 100, words=30),
            )
            for i in range(n)
        ]

    small, large = _projects(6), _projects(48)
    ok = False
    t_small = t_large = float("inf")
    for _ in range(3):
        # Budgets force truncation so the fair-split path is measured too.
        t_small = min(t_small, _best_build_seconds(small, 6_000))
        t_large = min(t_large, _best_build_seconds(large, 48_000))
        if t_large < t_small * 24 + 0.05:
            ok = True
            break
    assert ok, (t_small, t_large)


async def test_compile_takes_top_claims_per_project_in_sql(db_session: AsyncSession):
    owner, _ = await _user(db_session, "owner")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    project = await _proj(db_session, owner, org=org, name="Alpha")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for i in range(8):
        entry = KnowledgeEntry(
            project_id=project.id,
            session_id=f"ses_{uuid.uuid4().hex[:16]}",
            user_id=owner.id,
            entry_type="decision",
            content=f"claim number {i}",
            confidence=0.1 * (i + 1),
            claim_class="claim",
            freshness_class="current",
            created_at=base + timedelta(minutes=i),
        )
        db_session.add(entry)
    await db_session.commit()
    group = await _group(db_session, org, owner, name="Core")
    await _place(db_session, group, project)

    stats = await group_compiler.compile_group_context(
        db_session, group, owner.id, now=_FIXED, claims_per_project=3
    )
    assert stats["claims"] == 3
    doc = group.context_document
    for i in (5, 6, 7):  # the three highest-confidence claims
        assert f"claim number {i}" in doc
    for i in range(5):
        assert f"claim number {i}" not in doc


# ── Search validation, LIKE escaping, page limits ──


async def test_search_query_bounds_and_wildcards(client: AsyncClient, db_session: AsyncSession):
    owner, owner_h = await _user(db_session, "owner")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    project = await _proj(db_session, owner, org=org, name="Alpha")
    await _claim(db_session, project, content="Rollout is 100% complete")
    await _claim(db_session, project, content="Use snake_case names")
    await _claim(db_session, project, content="Plain decision text")
    group = await _group(db_session, org, owner, name="Core")
    await _place(db_session, group, project)
    url = f"/api/v1/orgs/{org.id}/groups/{group.slug}/knowledge/search"

    assert (await client.get(url, params={"q": "ab"}, headers=owner_h)).status_code == 422
    assert (await client.get(url, params={"q": "x" * 201}, headers=owner_h)).status_code == 422
    assert (await client.get(url, params={"limit": 101}, headers=owner_h)).status_code == 422

    # % and _ match literally instead of acting as wildcards.
    resp = await client.get(url, params={"q": "0% c"}, headers=owner_h)
    assert [h["content"] for h in resp.json()] == ["Rollout is 100% complete"]
    resp = await client.get(url, params={"q": "%%%"}, headers=owner_h)
    assert resp.json() == []
    resp = await client.get(url, params={"q": "e_c"}, headers=owner_h)
    assert [h["content"] for h in resp.json()] == ["Use snake_case names"]


async def test_pages_limit(client: AsyncClient, db_session: AsyncSession):
    owner, owner_h = await _user(db_session, "owner")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    project = await _proj(db_session, owner, org=org, name="Alpha")
    for i in range(3):
        await _page(db_session, project, slug=f"page-{i}", title=f"Page {i}")
    group = await _group(db_session, org, owner, name="Core")
    await _place(db_session, group, project)
    url = f"/api/v1/orgs/{org.id}/groups/{group.slug}/pages"

    assert len((await client.get(url, headers=owner_h)).json()) == 3
    assert len((await client.get(url, params={"limit": 2}, headers=owner_h)).json()) == 2
    assert (await client.get(url, params={"limit": 501}, headers=owner_h)).status_code == 422
    assert (await client.get(url, params={"limit": 0}, headers=owner_h)).status_code == 422


# ── Route-level access matrix (search, pages, context) ──


async def test_group_knowledge_routes_access_matrix(client: AsyncClient, db_session: AsyncSession):
    owner, owner_h = await _user(db_session, "owner")
    reader, reader_h = await _user(db_session, "reader")
    loose, loose_h = await _user(db_session, "loose")  # unrestricted-org member, no grant
    tight, tight_h = await _user(db_session, "tight")  # restricted-org member, no grant
    outsider, outsider_h = await _user(db_session, "outsider")

    org = await _org(db_session, restrict=True)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, reader, role="member")
    await _member(db_session, org, tight, role="member")
    project = await _proj(db_session, owner, org=org, name="Alpha")
    await _claim(db_session, project, content="Restricted org decision")
    await _page(db_session, project, slug="alpha-page", title="Alpha Page")
    group = await _group(db_session, org, owner, name="Core")
    await _place(db_session, group, project)
    await _grant(db_session, group, reader, "reader")
    group.compile_enabled = True
    await db_session.commit()
    assert (await client.post(_compile_url(org, group), headers=owner_h)).status_code == 200

    # A second, unrestricted org with a group of the SAME slug.
    org2 = await _org(db_session)
    await _member(db_session, org2, owner, role="owner")
    await _member(db_session, org2, loose, role="member")
    project2 = await _proj(db_session, owner, org=org2, name="Other")
    await _claim(db_session, project2, content="Other org decision")
    await _page(db_session, project2, slug="other-page", title="Other Page")
    group2 = await _group(db_session, org2, owner, name="Core")
    await _place(db_session, group2, project2)
    group2.compile_enabled = True
    await db_session.commit()
    assert (await client.post(_compile_url(org2, group2), headers=owner_h)).status_code == 200

    def urls(o, g):
        base = f"/api/v1/orgs/{o.id}/groups/{g.slug}"
        return (f"{base}/knowledge/search", f"{base}/pages", f"{base}/context")

    async def reads(headers, o, g):
        return [await client.get(u, headers=headers) for u in urls(o, g)]

    # Org admin and a reader grant holder (restricted org) see org 1's data only.
    for headers in (owner_h, reader_h):
        search, pages, context = await reads(headers, org, group)
        assert search.status_code == pages.status_code == context.status_code == 200
        assert [h["content"] for h in search.json()] == ["Restricted org decision"]
        assert [p["slug"] for p in pages.json()] == ["alpha-page"]
        assert "Restricted org decision" in context.json()["document"]
        assert "Other org decision" not in context.json()["document"]

    # The same slug in org 2 returns org 2's data, never org 1's.
    search, pages, context = await reads(owner_h, org2, group2)
    assert [h["content"] for h in search.json()] == ["Other org decision"]
    assert [p["slug"] for p in pages.json()] == ["other-page"]
    assert "Restricted org decision" not in context.json()["document"]

    # No grant → 404, restricted or not; a non-member of the org → 404; and
    # an org-2 member asking about org 1's slug → 404.
    for headers, o, g in (
        (loose_h, org2, group2),
        (tight_h, org, group),
        (outsider_h, org, group),
        (loose_h, org, group),
    ):
        for resp in await reads(headers, o, g):
            assert resp.status_code == 404, (resp.request.url, resp.text)
            assert "Restricted org decision" not in resp.text


async def test_build_skips_an_oversized_claim_but_keeps_smaller_ones() -> None:
    claims = (
        ClaimInput(id=1, entry_type="decision", content="huge " * 400, confidence=0.99, relevant_ts=1.0, has_entity=0),
        ClaimInput(id=2, entry_type="decision", content="small one", confidence=0.5, relevant_ts=1.0, has_entity=0),
        ClaimInput(id=3, entry_type="bug", content="small two", confidence=0.4, relevant_ts=1.0, has_entity=0),
    )
    project = ProjectInput(id="p1", name="Alpha", context="", claims=claims)
    doc = build_group_document("Core", [project], now=_FIXED, max_words=120)
    assert doc.truncated
    assert "claim #1]" not in doc.text
    assert "] small one" in doc.text and "] small two" in doc.text
    assert doc.claims_emitted == 2 and doc.claims_omitted == 1


async def test_build_caps_summary_share_so_claims_still_appear() -> None:
    projects = [
        ProjectInput(
            id=f"p{i}",
            name=f"Project {i}",
            context="\n".join(f"context line {j} with several words" for j in range(80)),
            claims=_claims(5, start=i * 10, words=3),
        )
        for i in range(10)
    ]
    # ~100 words per project: an uncapped 250-word summary would eat it all.
    doc = build_group_document("Core", projects, now=_FIXED, max_words=1_000)
    assert doc.truncated
    for i in range(10):
        section = doc.text.split(f"## Project {i}\n", 1)[1].split("\n## ", 1)[0]
        assert "/claim #" in section, section
        summary_words = sum(
            len(line.split()) for line in section.splitlines() if line.startswith(">")
        )
        assert summary_words <= 40


async def test_top_claims_window_uses_relevance_recency(db_session: AsyncSession):
    owner, _ = await _user(db_session, "owner")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    project = await _proj(db_session, owner, org=org, name="Alpha")
    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    for content, created, relevant in (
        ("newer created, never relevant", base + timedelta(days=5), None),
        ("older created, relevant lately", base, base + timedelta(days=9)),
    ):
        db_session.add(
            KnowledgeEntry(
                project_id=project.id,
                session_id=f"ses_{uuid.uuid4().hex[:16]}",
                user_id=owner.id,
                entry_type="decision",
                content=content,
                confidence=0.8,
                claim_class="claim",
                freshness_class="current",
                created_at=created,
                last_relevant_at=relevant,
            )
        )
    await db_session.commit()
    group = await _group(db_session, org, owner, name="Core")
    await _place(db_session, group, project)

    await group_compiler.compile_group_context(
        db_session, group, owner.id, now=_FIXED, claims_per_project=1
    )
    assert "older created, relevant lately" in group.context_document
    assert "newer created, never relevant" not in group.context_document
