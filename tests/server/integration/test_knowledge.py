"""Integration tests for knowledge entries and compilation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    ContextCompilation,
    KnowledgeEntry,
    Project,
    User,
)
from sessionfs.server.services.summarizer import SessionSummary


@pytest.fixture
async def test_project(db_session: AsyncSession, test_user: User) -> Project:
    """Create a test project."""
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="Test Project",
        git_remote_normalized="github.com/example/repo",
        context_document="# Project Context\n\n## Overview\nTest project.\n",
        owner_id=test_user.id,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


@pytest.fixture
def sample_summary() -> SessionSummary:
    """Create a sample session summary for extraction testing."""
    return SessionSummary(
        session_id="ses_test123",
        title="Test session",
        tool="claude-code",
        model="claude-sonnet-4",
        duration_minutes=15,
        message_count=10,
        tool_call_count=5,
        files_modified=["src/main.py", "tests/test_main.py"],
        files_read=["README.md"],
        commands_executed=3,
        tests_run=5,
        tests_passed=3,
        tests_failed=2,
        packages_installed=["requests", "flask"],
        errors_encountered=["AssertionError: expected True"],
        what_happened="Added API endpoint",
        key_decisions=["Use FastAPI instead of Flask", "Add rate limiting"],
        outcome="Partially complete",
        open_issues=["Rate limiting not tested", "Missing docs"],
        generated_at="2026-03-30T12:00:00Z",
    )


@pytest.mark.asyncio
async def test_extract_entries_from_summary(
    db_session: AsyncSession, test_user: User, test_project: Project, sample_summary: SessionSummary
):
    """Test that knowledge entries are extracted from a session summary."""
    from sessionfs.server.services.knowledge import extract_knowledge_entries

    entries = await extract_knowledge_entries(
        session_id="ses_test123",
        summary=sample_summary,
        project_id=test_project.id,
        user_id=test_user.id,
        db=db_session,
    )

    assert len(entries) > 0

    # Check entry types
    types = {e.entry_type for e in entries}
    assert "pattern" in types  # files modified
    assert "bug" in types  # tests failing + open_issues
    assert "dependency" in types  # packages installed
    assert "decision" in types  # key_decisions

    # Check pattern entries for files
    pattern_entries = [e for e in entries if e.entry_type == "pattern"]
    assert len(pattern_entries) == 2  # 2 files modified

    # Check dependency entries
    dep_entries = [e for e in entries if e.entry_type == "dependency"]
    assert len(dep_entries) == 2  # requests, flask
    assert all(e.confidence == 0.9 for e in dep_entries)

    # Check decision entries
    dec_entries = [e for e in entries if e.entry_type == "decision"]
    assert len(dec_entries) == 2
    assert all(e.confidence == 0.8 for e in dec_entries)


@pytest.mark.asyncio
async def test_compilation_creates_record(
    db_session: AsyncSession, test_user: User, test_project: Project
):
    """Test that compilation creates a compilation record."""
    # Add some pending entries
    for i in range(3):
        entry = KnowledgeEntry(
            project_id=test_project.id,
            session_id="ses_test123",
            user_id=test_user.id,
            entry_type="pattern",
            content=f"File modified: src/file{i}.py",
            confidence=0.5,
        )
        db_session.add(entry)
    await db_session.commit()

    from sessionfs.server.services.compiler import compile_project_context

    compilation = await compile_project_context(
        project_id=test_project.id,
        user_id=test_user.id,
        db=db_session,
    )

    assert compilation is not None
    assert compilation.project_id == test_project.id
    assert compilation.user_id == test_user.id
    assert compilation.entries_compiled == 3
    assert compilation.context_before is not None
    assert compilation.context_after is not None

    # Verify record persisted
    result = await db_session.execute(
        select(ContextCompilation).where(ContextCompilation.id == compilation.id)
    )
    persisted = result.scalar_one_or_none()
    assert persisted is not None


@pytest.mark.asyncio
async def test_entries_marked_compiled_after_compilation(
    db_session: AsyncSession, test_user: User, test_project: Project
):
    """Test that entries are marked as compiled after compilation."""
    entry = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_test456",
        user_id=test_user.id,
        entry_type="dependency",
        content="Package installed: numpy",
        confidence=0.9,
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)
    entry_id = entry.id

    assert entry.compiled_at is None

    from sessionfs.server.services.compiler import compile_project_context

    await compile_project_context(
        project_id=test_project.id,
        user_id=test_user.id,
        db=db_session,
    )

    # Re-fetch entry
    result = await db_session.execute(
        select(KnowledgeEntry).where(KnowledgeEntry.id == entry_id)
    )
    updated_entry = result.scalar_one()
    assert updated_entry.compiled_at is not None


@pytest.mark.asyncio
async def test_dismiss_marks_entry(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """Test that PUT dismiss endpoint marks an entry as dismissed."""
    entry = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_test789",
        user_id=test_user.id,
        entry_type="bug",
        content="Test failing: test_foo",
        confidence=0.7,
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)

    resp = await client.put(
        f"/api/v1/projects/{test_project.id}/entries/{entry.id}",
        json={"dismissed": True},
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["dismissed"] is True
    assert data["entry_type"] == "bug"
    assert data["content"] == "Test failing: test_foo"

    # Verify via GET endpoint that the entry is now dismissed
    resp2 = await client.get(
        f"/api/v1/projects/{test_project.id}/entries?pending=true",
        headers=auth_headers,
    )
    assert resp2.status_code == 200
    pending = resp2.json()
    # The dismissed entry should not appear in pending
    assert all(e["id"] != entry.id for e in pending)


@pytest.mark.asyncio
async def test_health_endpoint(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """Test that health endpoint returns correct status."""
    # Add a mix of entries
    pending_entry = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_health1",
        user_id=test_user.id,
        entry_type="pattern",
        content="File: a.py",
        confidence=0.5,
    )
    compiled_entry = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_health2",
        user_id=test_user.id,
        entry_type="dependency",
        content="Package: requests",
        confidence=0.9,
        compiled_at=datetime.now(timezone.utc),
    )
    dismissed_entry = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_health3",
        user_id=test_user.id,
        entry_type="bug",
        content="Old bug",
        confidence=0.7,
        dismissed=True,
    )
    db_session.add_all([pending_entry, compiled_entry, dismissed_entry])
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{test_project.id}/health",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["project_id"] == test_project.id
    assert data["total_entries"] == 3
    assert data["pending_entries"] == 1
    assert data["compiled_entries"] == 1
    assert data["dismissed_entries"] == 1
    assert data["total_compilations"] == 0
    assert "word_count" in data
    assert "section_count" in data
    assert "potentially_stale" in data


@pytest.mark.asyncio
async def test_health_pending_entries_matches_compile_filter(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """tk_935a4eb62be94676 regression — `pending_entries` must mirror the
    compile pipeline's eligibility filter EXACTLY:
        claim_class='claim' AND compiled_at IS NULL AND not dismissed
        AND freshness_class IN ('current', 'aging') AND superseded_by IS NULL

    Pre-fix the count filtered only on claim_class='claim' + dismissed=False
    + compiled_at IS NULL, so it overcounted superseded claims and stale
    claims that compile would skip. The "Run compile to process N pending
    entries" recommendation lied — users would compile, see entries_compiled=0,
    and the count would stay flat.

    `uncompiled_notes` must surface the note-class count separately, so the
    operator knows to call bulk_promote instead of compile.
    """
    eligible_claim = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_a",
        user_id=test_user.id,
        entry_type="decision",
        content="Eligible — current claim, no compiled_at",
        confidence=0.9,
        claim_class="claim",
        freshness_class="current",
    )
    aging_claim = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_b",
        user_id=test_user.id,
        entry_type="decision",
        content="Eligible — aging claim, no compiled_at",
        confidence=0.85,
        claim_class="claim",
        freshness_class="aging",
    )
    # Stale claim — compile will SKIP this. Must NOT count as pending.
    stale_claim = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_c",
        user_id=test_user.id,
        entry_type="decision",
        content="Stale claim — compile skips",
        confidence=0.6,
        claim_class="claim",
        freshness_class="stale",
    )
    # Superseded claim — compile will SKIP this. Must NOT count as pending.
    superseder = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_d1",
        user_id=test_user.id,
        entry_type="decision",
        content="Superseder",
        confidence=0.9,
        claim_class="claim",
        freshness_class="current",
    )
    db_session.add(superseder)
    await db_session.commit()
    await db_session.refresh(superseder)
    superseded_claim = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_d2",
        user_id=test_user.id,
        entry_type="decision",
        content="Superseded — compile skips",
        confidence=0.8,
        claim_class="claim",
        freshness_class="superseded",
        superseded_by=superseder.id,
    )
    # Note — needs promotion. Must surface as uncompiled_notes, NOT pending.
    note_1 = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_e1",
        user_id=test_user.id,
        entry_type="pattern",
        content="Manual note — not auto-compileable",
        confidence=0.7,
        claim_class="note",
        freshness_class="current",
    )
    note_2 = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_e2",
        user_id=test_user.id,
        entry_type="pattern",
        content="Another manual note",
        confidence=0.7,
        claim_class="note",
        freshness_class="current",
    )
    db_session.add_all([eligible_claim, aging_claim, stale_claim, superseded_claim, note_1, note_2])
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{test_project.id}/health",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    # Total includes everything (6 net new + 1 superseder)
    assert data["total_entries"] == 7

    # pending_entries: only the 2 compile-eligible (current + aging) claims.
    # NOT the stale, NOT the superseded, NOT the 2 notes, NOT the superseder
    # (current — wait, the superseder IS eligible). Let me recount:
    # - eligible_claim: current claim, no compiled_at → COUNTS
    # - aging_claim: aging claim, no compiled_at → COUNTS
    # - superseder: current claim, no compiled_at → COUNTS
    # - stale_claim: stale → SKIP
    # - superseded_claim: superseded_by set → SKIP
    # - note_1, note_2: claim_class='note' → SKIP
    # = 3 compile-eligible claims
    assert data["pending_entries"] == 3, (
        f"pending_entries must mirror compile filter — expected 3 "
        f"(eligible_claim + aging_claim + superseder, skipping stale "
        f"+ superseded + 2 notes), got {data['pending_entries']}"
    )

    # uncompiled_notes: only the 2 notes
    assert data["uncompiled_notes"] == 2, (
        f"uncompiled_notes must count claim_class='note' entries with "
        f"compiled_at IS NULL and not dismissed, expected 2, got "
        f"{data['uncompiled_notes']}"
    )

    # compiled_entries / dismissed_entries unchanged behavior (0 each here)
    assert data["compiled_entries"] == 0
    assert data["dismissed_entries"] == 0

    # Recommendations must reflect the corrected counts. With 3 pending
    # claims (<= 20) the message is the "pending N — run compile" variant.
    # The new uncompiled_notes hint must also fire.
    rec_text = " ".join(data["recommendations"])
    assert "3 pending" in rec_text, f"missing pending recommendation: {data['recommendations']}"
    assert "2 uncompiled notes" in rec_text, (
        f"missing uncompiled_notes recommendation: {data['recommendations']}"
    )


@pytest.mark.asyncio
async def test_health_counts_auto_promotable_evidence(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """tk_935a4eb62be94676 R1 MEDIUM 1 regression — health must surface
    auto-promotable evidence so the operator knows /compile will do work
    even when pending_entries is 0.

    The compiler's Phase 2a auto-promotes evidence (claim_class='evidence'
    AND confidence >= 0.5 AND length(content) >= 30 AND not dismissed)
    to claim BEFORE the pending-claim select. Pre-R1 fix, health reported
    pending_entries=0 for a project containing only eligible evidence and
    issued no recommendation — but /compile would have processed those
    rows. That's the inverse false-negative of the original bug.
    """
    eligible_evidence = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_ae1",
        user_id=test_user.id,
        entry_type="discovery",
        content="X" * 60,  # >= 30 chars
        confidence=0.7,  # >= 0.5
        claim_class="evidence",
        freshness_class="current",
    )
    short_evidence = KnowledgeEntry(  # too short — must NOT count
        project_id=test_project.id,
        session_id="ses_ae2",
        user_id=test_user.id,
        entry_type="discovery",
        content="too short",  # < 30 chars
        confidence=0.7,
        claim_class="evidence",
        freshness_class="current",
    )
    low_conf_evidence = KnowledgeEntry(  # below confidence floor — must NOT count
        project_id=test_project.id,
        session_id="ses_ae3",
        user_id=test_user.id,
        entry_type="discovery",
        content="Y" * 60,
        confidence=0.3,  # < 0.5
        claim_class="evidence",
        freshness_class="current",
    )
    dismissed_evidence = KnowledgeEntry(  # dismissed — must NOT count
        project_id=test_project.id,
        session_id="ses_ae4",
        user_id=test_user.id,
        entry_type="discovery",
        content="Z" * 60,
        confidence=0.7,
        claim_class="evidence",
        freshness_class="current",
        dismissed=True,
    )
    db_session.add_all([eligible_evidence, short_evidence, low_conf_evidence, dismissed_evidence])
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{test_project.id}/health",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    # Only the eligible row counts. pending_entries stays at 0 because
    # these are evidence rows, not claims yet.
    assert data["pending_entries"] == 0
    assert data["auto_promotable_evidence"] == 1, (
        f"expected 1 auto-promotable evidence row "
        f"(eligible_evidence), got {data['auto_promotable_evidence']}"
    )

    # Recommendation must fire — "Run compile" predicts /compile will
    # process the auto-promotable evidence even though pending_entries=0.
    rec_text = " ".join(data["recommendations"])
    assert "1 auto-promotable" in rec_text, (
        f"missing auto-promotable evidence recommendation: "
        f"{data['recommendations']}"
    )


@pytest.mark.asyncio
async def test_health_potentially_stale_ignores_notes_and_superseded(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """tk_935a4eb62be94676 R1 MEDIUM 2 regression — potentially_stale must
    use the same compile-eligible filter as pending_entries. Pre-R1 fix,
    a project where the compile-eligible claim was already represented
    in the context document could still flag potentially_stale=True if an
    uncompiled note or a superseded claim contained terms missing from
    the doc — driving a false-positive "Context may be stale" warning.
    """
    # Set up a project with a compile-eligible claim whose content is
    # already represented in the context document.
    test_project.context_document = "# Project Context\n\nPostgreSQL is the chosen database."
    db_session.add(test_project)
    await db_session.commit()

    represented_claim = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_rep",
        user_id=test_user.id,
        entry_type="decision",
        content="PostgreSQL database choice for the project",
        confidence=0.9,
        claim_class="claim",
        freshness_class="current",
    )
    # An uncompiled note with terms NOT in the doc — must NOT drive stale.
    unrepresented_note = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_note",
        user_id=test_user.id,
        entry_type="pattern",
        content="Kubernetes deployment topology with helm charts",
        confidence=0.7,
        claim_class="note",
        freshness_class="current",
    )
    # A superseded claim with novel terms — must NOT drive stale.
    superseder_for_test = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_super",
        user_id=test_user.id,
        entry_type="decision",
        content="Newer database choice info",
        confidence=0.9,
        claim_class="claim",
        freshness_class="current",
    )
    db_session.add(superseder_for_test)
    await db_session.commit()
    await db_session.refresh(superseder_for_test)
    superseded_with_novel_terms = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_supt",
        user_id=test_user.id,
        entry_type="decision",
        content="Cassandra wide-column distributed storage",
        confidence=0.8,
        claim_class="claim",
        freshness_class="superseded",
        superseded_by=superseder_for_test.id,
    )
    db_session.add_all([represented_claim, unrepresented_note, superseded_with_novel_terms])
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{test_project.id}/health",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    # The compile-eligible claim ("represented_claim" + "superseder_for_test")
    # are both represented in the doc OR have generic terms that overlap.
    # The note about Kubernetes and the superseded Cassandra claim contain
    # novel terms but compile WON'T process them, so they must NOT trigger
    # the stale flag.
    assert data["potentially_stale"] is False, (
        f"potentially_stale must ignore notes + superseded claims; "
        f"pending_entries={data['pending_entries']}, "
        f"uncompiled_notes={data['uncompiled_notes']}, "
        f"recommendations={data['recommendations']}"
    )


@pytest.mark.asyncio
async def test_health_auto_promotable_excludes_stale_and_superseded_evidence(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """tk_935a4eb62be94676 R2 LOW 1 regression — auto_promotable_evidence
    must filter on the SAME post-promotion predicates Phase 2b uses
    (compiled_at IS NULL, current/aging freshness, no superseder).
    Otherwise we count rows that promote to claim but Phase 2b skips,
    and the operator gets misleading "run compile to fold them into
    context" advice.
    """
    eligible = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_ok",
        user_id=test_user.id,
        entry_type="discovery",
        content="A" * 60,
        confidence=0.7,
        claim_class="evidence",
        freshness_class="current",
    )
    stale_evidence = KnowledgeEntry(  # stale — Phase 2b would skip
        project_id=test_project.id,
        session_id="ses_stale",
        user_id=test_user.id,
        entry_type="discovery",
        content="B" * 60,
        confidence=0.7,
        claim_class="evidence",
        freshness_class="stale",
    )
    already_compiled_evidence = KnowledgeEntry(  # compiled — Phase 2b skips
        project_id=test_project.id,
        session_id="ses_comp",
        user_id=test_user.id,
        entry_type="discovery",
        content="C" * 60,
        confidence=0.7,
        claim_class="evidence",
        freshness_class="current",
        compiled_at=datetime.now(timezone.utc),
    )
    # Superseded evidence — Phase 2b skips
    superseder_claim = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_sup_a",
        user_id=test_user.id,
        entry_type="discovery",
        content="D" * 60,
        confidence=0.9,
        claim_class="claim",
        freshness_class="current",
    )
    db_session.add(superseder_claim)
    await db_session.commit()
    await db_session.refresh(superseder_claim)
    superseded_evidence = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_sup_b",
        user_id=test_user.id,
        entry_type="discovery",
        content="E" * 60,
        confidence=0.7,
        claim_class="evidence",
        freshness_class="superseded",
        superseded_by=superseder_claim.id,
    )
    db_session.add_all([eligible, stale_evidence, already_compiled_evidence, superseded_evidence])
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{test_project.id}/health",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    # Only `eligible` matches Phase 2a + survives Phase 2b
    assert data["auto_promotable_evidence"] == 1, (
        f"expected exactly 1 auto-promotable evidence (eligible only — "
        f"stale + already-compiled + superseded must be excluded), "
        f"got {data['auto_promotable_evidence']}"
    )


@pytest.mark.asyncio
async def test_health_no_compile_advice_when_only_notes_exist(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """tk_935a4eb62be94676 R2 LOW 2 regression — the "No compilations yet"
    recommendation must not fire for a project that has entries but no
    compile-eligible work. A fresh project with only notes or ineligible
    evidence should get bulk_promote / claim-creation guidance, not run-
    compile advice that would no-op.
    """
    note = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_note_only",
        user_id=test_user.id,
        entry_type="pattern",
        content="Manual note that needs promotion",
        confidence=0.7,
        claim_class="note",
        freshness_class="current",
    )
    db_session.add(note)
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/projects/{test_project.id}/health",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()

    assert data["pending_entries"] == 0
    assert data["auto_promotable_evidence"] == 0
    assert data["uncompiled_notes"] == 1
    assert data["total_compilations"] == 0

    rec_text = " ".join(data["recommendations"])
    # MUST NOT advise "run compile to build context" — there's nothing
    # for compile to do.
    assert "No compilations yet — run compile" not in rec_text, (
        f"misleading 'run compile to build context' fired with no "
        f"compile-eligible work: {data['recommendations']}"
    )
    # MUST surface the bulk_promote hint instead
    assert "1 uncompiled note" in rec_text, (
        f"missing bulk_promote hint: {data['recommendations']}"
    )


@pytest.mark.asyncio
async def test_search_entries_with_query(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """Test search endpoint with query parameter returns matching entries."""
    entry1 = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_search1",
        user_id=test_user.id,
        entry_type="decision",
        content="Use PostgreSQL for the database",
        confidence=0.9,
    )
    entry2 = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_search2",
        user_id=test_user.id,
        entry_type="pattern",
        content="Always use Redis for caching",
        confidence=0.8,
    )
    entry3 = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_search3",
        user_id=test_user.id,
        entry_type="dependency",
        content="Package installed: psycopg2",
        confidence=0.9,
    )
    db_session.add_all([entry1, entry2, entry3])
    await db_session.commit()

    # Search for "PostgreSQL" — should match entry1
    resp = await client.get(
        f"/api/v1/projects/{test_project.id}/entries?search=PostgreSQL",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert "PostgreSQL" in data[0]["content"]


@pytest.mark.asyncio
async def test_search_entries_with_type_filter(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """Test search with type filter narrows results."""
    entry1 = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_filter1",
        user_id=test_user.id,
        entry_type="decision",
        content="Use FastAPI framework",
        confidence=0.9,
    )
    entry2 = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_filter2",
        user_id=test_user.id,
        entry_type="pattern",
        content="FastAPI pattern for middleware",
        confidence=0.8,
    )
    db_session.add_all([entry1, entry2])
    await db_session.commit()

    # Search "FastAPI" with type=decision — should only return entry1
    resp = await client.get(
        f"/api/v1/projects/{test_project.id}/entries?search=FastAPI&type=decision",
        headers=auth_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["entry_type"] == "decision"


@pytest.mark.asyncio
async def test_compilation_creates_recent_changes_section(
    db_session: AsyncSession, test_user: User, test_project: Project,
):
    """Test that simple compilation creates a Recent Changes section."""
    entry = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_recent1",
        user_id=test_user.id,
        entry_type="decision",
        content="Switched from Flask to FastAPI",
        confidence=0.9,
    )
    db_session.add(entry)
    await db_session.commit()

    from sessionfs.server.services.compiler import compile_project_context

    compilation = await compile_project_context(
        project_id=test_project.id,
        user_id=test_user.id,
        db=db_session,
    )

    assert compilation is not None
    context_after = compilation.context_after
    assert "## Recent Changes" in context_after
    assert "## Key Decisions" in context_after
    assert "Switched from Flask to FastAPI" in context_after


@pytest.mark.asyncio
async def test_repeated_compile_no_duplicate_sections(
    db_session: AsyncSession, test_user: User, test_project: Project,
):
    """Repeated compiles must not duplicate ## Recent Changes or ## Unverified."""
    from sessionfs.server.services.compiler import compile_project_context

    # First compile: one low-confidence entry
    entry1 = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_dup1",
        user_id=test_user.id,
        entry_type="bug",
        content="Possible race in queue handler",
        confidence=0.4,
    )
    db_session.add(entry1)
    await db_session.commit()

    c1 = await compile_project_context(
        project_id=test_project.id, user_id=test_user.id, db=db_session,
    )
    assert c1 is not None
    assert c1.context_after.count("## Recent Changes") == 1
    assert c1.context_after.count("## Unverified") == 1
    assert "(unverified) Possible race in queue handler" in c1.context_after

    # Second compile: new entry, same project
    entry2 = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_dup2",
        user_id=test_user.id,
        entry_type="decision",
        content="Use Redis for job queue",
        confidence=0.9,
    )
    db_session.add(entry2)
    await db_session.commit()

    c2 = await compile_project_context(
        project_id=test_project.id, user_id=test_user.id, db=db_session,
    )
    assert c2 is not None
    # Exactly one of each ephemeral section
    assert c2.context_after.count("## Recent Changes") == 1
    assert c2.context_after.count("## Unverified") == 1
    # Old unverified fact preserved
    assert "(unverified) Possible race in queue handler" in c2.context_after
    # New verified fact present
    assert "Use Redis for job queue" in c2.context_after


@pytest.mark.asyncio
async def test_unverified_promoted_to_verified_on_later_compile(
    db_session: AsyncSession, test_user: User, test_project: Project,
):
    """A fact that starts unverified should be promoted when a verified version arrives."""
    from sessionfs.server.services.compiler import compile_project_context

    # First compile: low-confidence entry
    entry1 = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_promo1",
        user_id=test_user.id,
        entry_type="pattern",
        content="All converters use streaming JSON",
        confidence=0.3,
    )
    db_session.add(entry1)
    await db_session.commit()

    c1 = await compile_project_context(
        project_id=test_project.id, user_id=test_user.id, db=db_session,
    )
    assert c1 is not None
    assert "(unverified) All converters use streaming JSON" in c1.context_after

    # Second compile: same fact, now high-confidence
    entry2 = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_promo2",
        user_id=test_user.id,
        entry_type="pattern",
        content="All converters use streaming JSON",
        confidence=0.9,
    )
    db_session.add(entry2)
    await db_session.commit()

    c2 = await compile_project_context(
        project_id=test_project.id, user_id=test_user.id, db=db_session,
    )
    assert c2 is not None
    # Verified bullet exists in main section (not under Unverified)
    assert "- All converters use streaming JSON" in c2.context_after
    # Unverified marker is gone — promoted to verified
    assert "(unverified) All converters use streaming JSON" not in c2.context_after


@pytest.mark.asyncio
async def test_compile_dedup_same_fact_across_batches(
    db_session: AsyncSession, test_user: User, test_project: Project,
):
    """Same fact compiled in two batches should not produce duplicate bullets."""
    from sessionfs.server.services.compiler import compile_project_context

    entry1 = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_dedup1",
        user_id=test_user.id,
        entry_type="decision",
        content="Use PostgreSQL for prod",
        confidence=0.9,
    )
    db_session.add(entry1)
    await db_session.commit()

    c1 = await compile_project_context(
        project_id=test_project.id, user_id=test_user.id, db=db_session,
    )
    assert c1 is not None
    assert c1.context_after.count("Use PostgreSQL for prod") == 2  # main + Recent Changes

    # Second compile: identical fact from a different session
    entry2 = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_dedup2",
        user_id=test_user.id,
        entry_type="decision",
        content="Use PostgreSQL for prod",
        confidence=0.9,
    )
    db_session.add(entry2)
    await db_session.commit()

    c2 = await compile_project_context(
        project_id=test_project.id, user_id=test_user.id, db=db_session,
    )
    assert c2 is not None
    # Main section should still have only one bullet for this fact
    main_section = c2.context_after.split("## Recent Changes")[0]
    assert main_section.count("Use PostgreSQL for prod") == 1


@pytest.mark.asyncio
async def test_mixed_confidence_same_batch_verified_wins(
    db_session: AsyncSession, test_user: User, test_project: Project,
):
    """Same fact at low and high confidence in one batch: verified wins."""
    from sessionfs.server.services.compiler import compile_project_context

    low = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_mix1",
        user_id=test_user.id,
        entry_type="pattern",
        content="Always use UTC timestamps",
        confidence=0.3,
    )
    high = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_mix2",
        user_id=test_user.id,
        entry_type="pattern",
        content="Always use UTC timestamps",
        confidence=0.9,
    )
    db_session.add_all([low, high])
    await db_session.commit()

    c = await compile_project_context(
        project_id=test_project.id, user_id=test_user.id, db=db_session,
    )
    assert c is not None
    # Verified bullet in main section
    assert "- Always use UTC timestamps" in c.context_after
    # NOT under Unverified
    assert "(unverified) Always use UTC timestamps" not in c.context_after


# ── v0.10.10 tk_483cede83deb443b — confidence update endpoint + noop_reason ──


@pytest.mark.asyncio
async def test_confidence_update_persists_and_unlocks_promote(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_project: Project, test_user: User
):
    """The original PUT /entries/{id} was dismiss-only; CEO confidence
    updates were silently dropped. New PUT /entries/{id}/confidence
    persists, and /promote then accepts the entry when it crosses 0.8."""
    # Seed a note at confidence 0.7 (below promote gate). Use enough
    # content to satisfy /promote 50-char minimum.
    entry = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_ceo1",
        user_id=test_user.id,
        entry_type="decision",
        content=(
            "Adopt scoped service API keys for all cloud agents — "
            "user tokens too broad for Bedrock/Vertex/CI."
        ),
        confidence=0.7,
        claim_class="note",
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)

    # Before fix: PUT /entries/{id} only handled dismiss; confidence
    # would silently stay at 0.7. After fix: dedicated endpoint persists.
    resp = await client.put(
        f"/api/v1/projects/{test_project.id}/entries/{entry.id}/confidence",
        headers=auth_headers,
        json={"confidence": 0.95},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    actual = body["confidence"]
    assert actual == 0.95, f"expected 0.95, got {actual}"

    # Get verifies persistence — the actual CEO bug was values not
    # surviving the round trip.
    g = await client.get(
        f"/api/v1/projects/{test_project.id}/entries/{entry.id}",
        headers=auth_headers,
    )
    assert g.status_code == 200
    assert g.json()["confidence"] == 0.95

    # Now /promote can succeed because confidence > 0.8.
    p = await client.put(
        f"/api/v1/projects/{test_project.id}/entries/{entry.id}/promote",
        headers=auth_headers,
    )
    assert p.status_code == 200, p.text
    assert p.json()["claim_class"] == "claim"


@pytest.mark.asyncio
async def test_confidence_update_rejects_out_of_range(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_project: Project, test_user: User
):
    """Pydantic Field(ge=0, le=1) returns 422 on out-of-range values."""
    entry = KnowledgeEntry(
        project_id=test_project.id, session_id="ses_x", user_id=test_user.id,
        entry_type="discovery", content="x" * 60, confidence=0.5, claim_class="note",
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)
    for bad in (-0.1, 1.5):
        r = await client.put(
            f"/api/v1/projects/{test_project.id}/entries/{entry.id}/confidence",
            headers=auth_headers, json={"confidence": bad},
        )
        assert r.status_code == 422, f"value {bad} should be rejected"


@pytest.mark.asyncio
async def test_add_entry_honors_explicit_confidence_from_manual_source(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """Codex review HIGH on tk_328006e4c6024dd8 — the real CEO bug was
    not the missing /confidence endpoint; it was that POST /entries/add
    with session_id='manual' (the MCP default) clamped confidence to
    min(0.7). A caller passing confidence=0.95 silently got 0.7 and
    could never promote. Fix: AddEntryRequest.confidence is now
    Optional; when caller specifies it, honor it; only apply the 0.7
    manual-source default when caller omits it entirely."""
    # Explicit 0.95 from a "manual" source — pre-fix this stored 0.7.
    r = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/add",
        headers=auth_headers,
        json={
            "content": "Adopt scoped service API keys for cloud agents — "
                       "user tokens too broad for Bedrock/Vertex/CI agents",
            "entry_type": "decision",
            "session_id": "manual",
            "confidence": 0.95,
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["confidence"] == 0.95, (
        f"Manual source explicit confidence must NOT be clamped to 0.7. "
        f"Got {body['confidence']}."
    )

    # And when caller omits confidence entirely, the legacy default
    # for manual sources (0.7) still applies — back-compat preserved.
    r2 = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/add",
        headers=auth_headers,
        json={
            "content": "Default-no-confidence: should still get manual-source default.",
            "entry_type": "discovery",
            "session_id": "manual",
        },
    )
    assert r2.status_code == 201
    assert r2.json()["confidence"] == 0.7, (
        "Manual source WITHOUT explicit confidence should still default to 0.7"
    )


@pytest.mark.asyncio
async def test_compile_noop_word_counts_match_health(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """Codex review MEDIUM 2 — when a project has existing context but
    no eligible entries, no-op compile must report the same word count
    as health (both derived from project.context_document), not 0.
    Pre-fix the surfaces disagreed and looked broken."""
    # The test_project fixture seeds context_document with a paragraph.
    # Run health and compile + assert they agree.
    h = await client.get(
        f"/api/v1/projects/{test_project.id}/health",
        headers=auth_headers,
    )
    assert h.status_code == 200
    health_words = h.json()["word_count"]
    assert health_words > 0, "test fixture should seed non-empty context"

    c = await client.post(
        f"/api/v1/projects/{test_project.id}/compile",
        headers=auth_headers,
        json={},
    )
    assert c.status_code == 200
    body = c.json()
    assert body["entries_compiled"] == 0
    # Codex MEDIUM 2: words_before/after must match health, not be zero.
    assert body["context_words_before"] == health_words, (
        f"compile no-op words_before ({body['context_words_before']}) "
        f"must match health.word_count ({health_words})"
    )
    assert body["context_words_after"] == health_words


@pytest.mark.asyncio
async def test_compile_noop_returns_explanatory_reason(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_project: Project, test_user: User
):
    """v0.10.10 tk_483cede83deb443b — compile of zero eligible entries
    must surface a noop_reason so callers know why entries_compiled=0.
    Before this, response showed compiled_at=<now> alongside health
    showing last_compilation_at=<older>, looking inconsistent."""
    # Seed only notes (never auto-compiled).
    db_session.add_all([
        KnowledgeEntry(
            project_id=test_project.id, session_id="ses_n1", user_id=test_user.id,
            entry_type="decision", content="x" * 60, confidence=0.7,
            claim_class="note",
        ),
        KnowledgeEntry(
            project_id=test_project.id, session_id="ses_n2", user_id=test_user.id,
            entry_type="pattern", content="y" * 60, confidence=0.6,
            claim_class="note",
        ),
    ])
    await db_session.commit()

    r = await client.post(
        f"/api/v1/projects/{test_project.id}/compile",
        headers=auth_headers, json={},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["entries_compiled"] == 0
    assert body["noop_reason"] is not None
    assert "note" in body["noop_reason"].lower()
    # Should mention the confidence/promote workflow so caller knows the
    # fix.
    assert "confidence" in body["noop_reason"].lower() or "promote" in body["noop_reason"].lower()



# ── v0.10.12 tk_c64915570f4d4042 — bulk-promote endpoint ──


@pytest.mark.asyncio
async def test_bulk_promote_dry_run_makes_no_writes(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    entry = KnowledgeEntry(
        project_id=test_project.id, session_id="ses_x", user_id=test_user.id,
        entry_type="decision",
        content="x" * 60, confidence=0.9, claim_class="note",
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)

    resp = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/bulk-promote",
        json={},  # dry_run defaults to True
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["promoted"] == 1
    assert entry.id in body["promoted_ids"]

    await db_session.refresh(entry)
    assert entry.claim_class == "note"
    assert entry.promoted_at is None


@pytest.mark.asyncio
async def test_bulk_promote_confirm_actually_promotes(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    entry = KnowledgeEntry(
        project_id=test_project.id, session_id="ses_x", user_id=test_user.id,
        entry_type="decision",
        content="x" * 60, confidence=0.9, claim_class="note",
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)

    resp = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/bulk-promote",
        json={"dry_run": False},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["promoted"] == 1
    assert body["dry_run"] is False

    await db_session.refresh(entry)
    assert entry.claim_class == "claim"
    assert entry.promoted_at is not None
    assert entry.promoted_by == test_user.id


@pytest.mark.asyncio
async def test_bulk_promote_set_confidence_overrides_low(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    entry = KnowledgeEntry(
        project_id=test_project.id, session_id="ses_x", user_id=test_user.id,
        entry_type="decision",
        content="x" * 60, confidence=0.5, claim_class="note",
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)

    resp = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/bulk-promote",
        json={"dry_run": False, "set_confidence": 0.9},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["promoted"] == 1
    await db_session.refresh(entry)
    assert entry.confidence == 0.9
    assert entry.claim_class == "claim"


@pytest.mark.asyncio
async def test_bulk_promote_per_reason_breakdown(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    # 2 eligible, 1 too short, 1 dismissed, 1 low confidence
    db_session.add_all([
        KnowledgeEntry(
            project_id=test_project.id, session_id="ses_a", user_id=test_user.id,
            entry_type="decision",
            content="x" * 60, confidence=0.9, claim_class="note",
        ),
        KnowledgeEntry(
            project_id=test_project.id, session_id="ses_b", user_id=test_user.id,
            entry_type="decision",
            content="y" * 60, confidence=0.9, claim_class="note",
        ),
        KnowledgeEntry(
            project_id=test_project.id, session_id="ses_c", user_id=test_user.id,
            entry_type="decision",
            content="z" * 10, confidence=0.9, claim_class="note",  # too short
        ),
        KnowledgeEntry(
            project_id=test_project.id, session_id="ses_d", user_id=test_user.id,
            entry_type="decision",
            content="w" * 60, confidence=0.9, claim_class="note",
            dismissed=True,
        ),
        KnowledgeEntry(
            project_id=test_project.id, session_id="ses_e", user_id=test_user.id,
            entry_type="decision",
            content="v" * 60, confidence=0.3, claim_class="note",
        ),
    ])
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/bulk-promote",
        json={"dry_run": True, "min_confidence": 0.85},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["promoted"] == 2
    assert body["skipped"] == 3
    assert body["reasons"]["too_short"] == 1
    assert body["reasons"]["dismissed"] == 1
    assert body["reasons"]["low_confidence"] == 1


@pytest.mark.asyncio
async def test_bulk_promote_validates_min_length_range(
    client: AsyncClient, auth_headers: dict, test_project: Project,
):
    resp = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/bulk-promote",
        json={"min_length": 0},
        headers=auth_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bulk_promote_validates_confidence_range(
    client: AsyncClient, auth_headers: dict, test_project: Project,
):
    resp = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/bulk-promote",
        json={"set_confidence": 1.5},
        headers=auth_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_bulk_promote_cross_project_denied(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User,
):
    """A user cannot bulk-promote entries in a project they don't own."""
    # Make a project owned by a different user.
    other_user = User(
        id=str(uuid.uuid4()),
        email="other@example.com", display_name="Other",
        tier="pro", email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(other_user)
    await db_session.commit()
    other_project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="Other", git_remote_normalized="github.com/other/repo",
        context_document="", owner_id=other_user.id,
    )
    db_session.add(other_project)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{other_project.id}/entries/bulk-promote",
        json={"dry_run": True},
        headers=auth_headers,
    )
    # _get_project_or_404 returns 403 or 404 for non-owners — either is
    # acceptable existence-hiding.
    assert resp.status_code in (403, 404)


@pytest.mark.asyncio
async def test_bulk_promote_entry_type_filter(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    db_session.add_all([
        KnowledgeEntry(
            project_id=test_project.id, session_id="ses_d", user_id=test_user.id,
            entry_type="decision",
            content="x" * 60, confidence=0.9, claim_class="note",
        ),
        KnowledgeEntry(
            project_id=test_project.id, session_id="ses_p", user_id=test_user.id,
            entry_type="pattern",
            content="y" * 60, confidence=0.9, claim_class="note",
        ),
    ])
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/bulk-promote",
        json={"dry_run": False, "entry_type": "decision"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["promoted"] == 1
    assert body["reasons"]["wrong_type"] == 1


# ── tk_bc3c02a63e994717 — fail-closed /rebuild + /compile rollback regressions ──
#
# These tests pin the v0.10.13 contract: any failure inside
# compile_project_context (including the destructive force_rebuild path)
# rolls back ALL writes. The prior good projection survives a crash.
#
# This is the test that would have caught the 2026-05-20 incident on
# proj_c0242b0fccbd48b4 where /rebuild wiped context_document + compiled_at
# in committed transactions BEFORE the recompile crashed.


@pytest.mark.asyncio
async def test_rebuild_rollback_on_compile_crash_preserves_prior_state(
    db_engine, db_session: AsyncSession, test_user: User,
    test_project: Project, monkeypatch,
):
    """The headline regression. Seed a project with a compiled context
    + claims at compiled_at != NULL. Patch _simple_compile to raise.
    Call compile_project_context(force_rebuild=True). Expect RuntimeError;
    AFTER the crash, project.context_document is unchanged AND every
    claim's compiled_at is still NOT NULL.

    Before tk_bc3c02a63e994717, both would have been wiped to '' / NULL by
    the destructive resets the /rebuild route committed before the failing
    recompile. That was the 2026-05-20 data-loss bug on proj_c0242b0fccbd48b4.

    Verification uses a FRESH session because the test-session's
    transaction state is unrecoverable in async context after the
    in-function crash (greenlet teardown).
    """
    from sessionfs.server.services import compiler
    from sqlalchemy.ext.asyncio import async_sessionmaker

    prior_context = "# Prior Compiled Context\n\nThis is the good state we must preserve.\n"
    test_project.context_document = prior_context
    prior_compile_ts = datetime.now(timezone.utc)
    claims = [
        KnowledgeEntry(
            project_id=test_project.id, session_id=f"ses_{i}",
            user_id=test_user.id, entry_type="decision",
            content="x" * 60, confidence=0.9, claim_class="claim",
            compiled_at=prior_compile_ts,
        )
        for i in range(3)
    ]
    db_session.add_all(claims)
    await db_session.commit()
    for c in claims:
        await db_session.refresh(c)
    prior_ids = [c.id for c in claims]
    project_id = test_project.id
    await db_session.close()

    # Run compile on its own session — same engine, same in-memory DB —
    # so a crash here doesn't poison the verification path.
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash inside the compile pipeline")
    monkeypatch.setattr(compiler, "_simple_compile", boom)

    async with factory() as crash_session:
        with pytest.raises(RuntimeError, match="simulated crash"):
            await compiler.compile_project_context(
                project_id=project_id,
                user_id=test_user.id,
                db=crash_session,
                force_rebuild=True,
            )

    # Verify rollback contract via a third fresh session.
    async with factory() as verify_session:
        refreshed_project = (await verify_session.execute(
            select(Project).where(Project.id == project_id)
        )).scalar_one()
        assert refreshed_project.context_document == prior_context, (
            "context_document was wiped — the destructive reset escaped "
            "the rollback. This is exactly the 2026-05-20 data-loss bug."
        )

        for cid in prior_ids:
            c = (await verify_session.execute(
                select(KnowledgeEntry).where(KnowledgeEntry.id == cid)
            )).scalar_one()
            assert c.compiled_at is not None, (
                f"compiled_at was nulled on entry {cid} despite the "
                f"recompile failing — the force_rebuild reset escaped "
                f"the rollback."
            )


@pytest.mark.asyncio
async def test_compile_rollback_on_inner_crash_preserves_prior_state(
    db_engine, db_session: AsyncSession, test_user: User,
    test_project: Project, monkeypatch,
):
    """Same contract for the non-force-rebuild path. A crash inside the
    compile pipeline must roll back any housekeeping writes (freshness /
    decay / auto-promote / mark-compiled) as well as the projection.
    Before v0.10.13's single-commit collapse, the housekeeping was
    committed at line 419 before the section-page work at line 522,
    opening a partial-success window."""
    from sessionfs.server.services import compiler
    from sqlalchemy.ext.asyncio import async_sessionmaker

    prior_context = "# Prior\n"
    test_project.context_document = prior_context

    pending_claim = KnowledgeEntry(
        project_id=test_project.id, session_id="ses_pending",
        user_id=test_user.id, entry_type="decision",
        content="x" * 60, confidence=0.9, claim_class="claim",
        compiled_at=None,  # pending
    )
    db_session.add(pending_claim)
    await db_session.commit()
    await db_session.refresh(pending_claim)
    pending_id = pending_claim.id
    project_id = test_project.id
    await db_session.close()

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    def boom(*args, **kwargs):
        raise RuntimeError("simulated crash inside simple_compile")
    monkeypatch.setattr(compiler, "_simple_compile", boom)

    async with factory() as crash_session:
        with pytest.raises(RuntimeError, match="simulated crash"):
            await compiler.compile_project_context(
                project_id=project_id,
                user_id=test_user.id,
                db=crash_session,
            )

    async with factory() as verify_session:
        refreshed_project = (await verify_session.execute(
            select(Project).where(Project.id == project_id)
        )).scalar_one()
        assert refreshed_project.context_document == prior_context

        refreshed_pending = (await verify_session.execute(
            select(KnowledgeEntry).where(KnowledgeEntry.id == pending_id)
        )).scalar_one()
        assert refreshed_pending.compiled_at is None, (
            "compiled_at was set on the pending entry despite the compile "
            "crashing — the mark-compiled write escaped the rollback."
        )


@pytest.mark.asyncio
async def test_rebuild_happy_path_direct_call_succeeds(
    db_engine, db_session: AsyncSession, test_user: User, test_project: Project,
):
    """Smoke: after the v0.10.13 refactor, compile_project_context with
    force_rebuild=True still produces a valid context document from a
    project with active claims. No LLM, no monkeypatch — exercises the
    real compile path end-to-end with the new single-commit shape."""
    from sessionfs.server.services import compiler
    from sqlalchemy.ext.asyncio import async_sessionmaker

    db_session.add_all([
        KnowledgeEntry(
            project_id=test_project.id, session_id=f"ses_{i}",
            user_id=test_user.id, entry_type="decision",
            content=f"Decision entry {i} with enough words to exceed the fifty character minimum gate.",
            confidence=0.9, claim_class="claim",
            compiled_at=datetime.now(timezone.utc),
        )
        for i in range(3)
    ])
    await db_session.commit()
    project_id = test_project.id
    await db_session.close()

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as compile_session:
        compilation = await compiler.compile_project_context(
            project_id=project_id,
            user_id=test_user.id,
            db=compile_session,
            force_rebuild=True,
        )
        assert compilation is not None
        assert compilation.entries_compiled == 3

    async with factory() as verify_session:
        refreshed = (await verify_session.execute(
            select(Project).where(Project.id == project_id)
        )).scalar_one()
        assert refreshed.context_document
        # _simple_compile writes the content into a "## Recent Changes"
        # section or per-type sections — exact wording varies; just
        # verify SOME claim text made it in.
        assert "Decision" in refreshed.context_document or "decision" in refreshed.context_document.lower()


@pytest.mark.asyncio
async def test_force_rebuild_drops_stale_text_from_prior_context(
    db_engine, db_session: AsyncSession, test_user: User, test_project: Project,
):
    """Codex R1 HIGH regression on tk_879dbd5a5a034d0e — force_rebuild
    must compile from an empty base, not merge new claims INTO the
    prior context_document. Otherwise stale/dismissed/superseded text
    can survive a full rebuild forever, defeating the whole point of
    the route.

    Seed: project.context_document contains a marker phrase that is
    NOT represented by any active claim (e.g. text for an entry that
    was later dismissed). Add one active claim with different content.
    Call compile_project_context(force_rebuild=True). Assert the stale
    marker is GONE from the new context_document.
    """
    from sessionfs.server.services import compiler
    from sqlalchemy.ext.asyncio import async_sessionmaker

    STALE_MARKER = "This stale text must not survive a force_rebuild."
    test_project.context_document = (
        f"# Prior Context\n\n## Old Section\n- {STALE_MARKER}\n"
    )

    db_session.add(KnowledgeEntry(
        project_id=test_project.id, session_id="ses_active",
        user_id=test_user.id, entry_type="decision",
        content="This active claim about a fresh architectural decision should appear.",
        confidence=0.9, claim_class="claim",
        compiled_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    project_id = test_project.id
    await db_session.close()

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as compile_session:
        compilation = await compiler.compile_project_context(
            project_id=project_id,
            user_id=test_user.id,
            db=compile_session,
            force_rebuild=True,
        )
        assert compilation is not None

    async with factory() as verify_session:
        refreshed = (await verify_session.execute(
            select(Project).where(Project.id == project_id)
        )).scalar_one()
        # The new active claim should appear.
        assert "architectural" in refreshed.context_document.lower()
        # The stale marker MUST be gone.
        assert STALE_MARKER not in refreshed.context_document, (
            f"force_rebuild preserved stale text from the prior "
            f"context_document. This is the Codex R1 HIGH regression "
            f"on tk_879dbd5a5a034d0e — force_rebuild=True must compile "
            f"from an empty base, not merge into the existing document. "
            f"Got context_document:\n{refreshed.context_document}"
        )

    # The compilation row's context_before still preserves the prior
    # text for the audit trail — that's the "preserve for audit" half
    # of the fix.
    async with factory() as audit_session:
        from sessionfs.server.db.models import ContextCompilation
        comp = (await audit_session.execute(
            select(ContextCompilation).where(
                ContextCompilation.id == compilation.id
            )
        )).scalar_one()
        assert STALE_MARKER in (comp.context_before or "")


@pytest.mark.asyncio
async def test_prune_dead_concept_pages_skips_malformed_links(
    db_engine, db_session: AsyncSession, test_user: User, test_project: Project,
):
    """tk_d92434fe63564c06 regression — _prune_dead_concept_pages used
    to do `int(lk.source_id)` without a try/except. A KnowledgeLink row
    with source_type='entry' but a non-numeric source_id (e.g. a slug
    from a manual seed, a UUID from a future schema, a malformed
    migration row) raises ValueError → the route's try/except catches
    it but the SQLAlchemy session is left in a poisoned state (partial
    in-progress delete writes) → subsequent db.execute calls in the
    route die uncaught → Cloud Run worker kill → 21-byte plain-text
    500 from Google Frontend.

    This was the underlying bug behind the post-v0.10.13 incident on
    proj_c0242b0fccbd48b4 where /compile returned 500 in 0.5s while
    bulk-promote on the same project returned 200. The data shape
    (concept page + KnowledgeLink with non-int source_id under
    source_type='entry') triggers the crash deterministically.

    The fix mirrors the sibling guard at compiler.py auto_generate_concepts
    line ~1198: skip malformed rows + log a warning.
    """
    from sessionfs.server.db.models import KnowledgeLink, KnowledgePage
    from sessionfs.server.services.compiler import _prune_dead_concept_pages
    from sqlalchemy.ext.asyncio import async_sessionmaker

    # Seed a concept page with a malformed source_id link
    page = KnowledgePage(
        id=f"kp_{uuid.uuid4().hex[:16]}",
        project_id=test_project.id,
        slug="concept/test-malformed",
        title="Test Malformed",
        page_type="concept",
        content="# Test\n\n- foo",
        word_count=2,
        entry_count=1,
        auto_generated=True,
    )
    db_session.add(page)
    await db_session.commit()
    await db_session.refresh(page)

    # Link with source_type='entry' but a non-int source_id (slug-like)
    db_session.add(KnowledgeLink(
        project_id=test_project.id,
        source_type="entry",
        source_id="not-an-int-slug",  # <- the trap
        target_type="page",
        target_id=page.id,
        link_type="related",
    ))
    # Add another valid link so the function has work to do post-guard
    valid_entry = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_x",
        user_id=test_user.id,
        entry_type="decision",
        content="x" * 60,
        confidence=0.9,
        claim_class="claim",
        dismissed=False,
    )
    db_session.add(valid_entry)
    await db_session.commit()
    await db_session.refresh(valid_entry)
    db_session.add(KnowledgeLink(
        project_id=test_project.id,
        source_type="entry",
        source_id=str(valid_entry.id),
        target_type="page",
        target_id=page.id,
        link_type="related",
    ))
    await db_session.commit()
    project_id = test_project.id
    await db_session.close()

    # Call _prune_dead_concept_pages on a fresh session. Before the fix
    # this raised ValueError; after the fix it skips the malformed row.
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        deleted = await _prune_dead_concept_pages(project_id, session)
        # The malformed link is skipped, the valid one is kept. The
        # valid entry is NOT dismissed, so dismissed_count != linked
        # count → page is NOT pruned. deleted == 0 is the contract here.
        assert deleted == 0

    # Verify the page survived (not deleted by partial work)
    async with factory() as verify:
        result = await verify.execute(
            select(KnowledgePage).where(KnowledgePage.id == page.id)
        )
        survivor = result.scalar_one_or_none()
        assert survivor is not None, "concept page wrongly deleted during malformed-row crash recovery"


@pytest.mark.asyncio
async def test_auto_generate_concepts_existing_page_skips_malformed_links(
    db_engine, db_session: AsyncSession, test_user: User, test_project: Project,
    monkeypatch,
):
    """Codex R1 HIGH (tk_e5185f5d432243f2) regression — auto_generate_concepts
    had a SECOND unguarded `int(lk.source_id)` at the per-existing-page
    deletion check (compiler.py:1249-1251 pre-fix). The dismissed-id prepass
    upstream was already guarded, but when the loop hit an existing concept
    page whose links contained a malformed source_id, the raw list
    comprehension still raised ValueError.

    This test seeds an existing concept page matching a forced candidate
    slug, attaches one malformed + one valid entry link, then calls
    auto_generate_concepts. Before the helper extraction: ValueError.
    After: malformed row skipped, valid row honored, no crash.
    """
    from sessionfs.server.db.models import KnowledgeLink, KnowledgePage
    from sessionfs.server.services import compiler as compiler_mod
    from sqlalchemy.ext.asyncio import async_sessionmaker

    # Seed an existing concept page that auto_generate_concepts will
    # encounter via the candidate slug match.
    page = KnowledgePage(
        id=f"kp_{uuid.uuid4().hex[:16]}",
        project_id=test_project.id,
        slug="concept/forced-topic",
        title="Forced Topic",
        page_type="concept",
        content="# Forced Topic\n\n- placeholder",
        word_count=3,
        entry_count=1,
        auto_generated=True,
    )
    db_session.add(page)
    await db_session.commit()
    await db_session.refresh(page)

    # Malformed link — the trap that crashed the unguarded comprehension
    db_session.add(KnowledgeLink(
        project_id=test_project.id,
        source_type="entry",
        source_id="not-an-int-slug",
        target_type="page",
        target_id=page.id,
        link_type="related",
    ))
    # Valid link so the post-guard list is non-empty
    valid_entry = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_existing",
        user_id=test_user.id,
        entry_type="decision",
        content="x" * 60,
        confidence=0.9,
        claim_class="claim",
        dismissed=False,
    )
    db_session.add(valid_entry)
    await db_session.commit()
    await db_session.refresh(valid_entry)
    db_session.add(KnowledgeLink(
        project_id=test_project.id,
        source_type="entry",
        source_id=str(valid_entry.id),
        target_type="page",
        target_id=page.id,
        link_type="related",
    ))
    await db_session.commit()
    project_id = test_project.id
    await db_session.close()

    # Force check_concept_candidates to return a candidate matching the
    # existing page's slug so we hit the existing-page branch (line ~1247).
    async def _fake_candidates(*args, **kwargs):
        return [{"slug": "forced-topic", "topic": "forced topic", "entry_count": 1}]
    monkeypatch.setattr(compiler_mod, "check_concept_candidates", _fake_candidates)

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        # Before the helper extraction this raised ValueError from the
        # existing-page branch. After: no exception.
        result = await compiler_mod.auto_generate_concepts(
            project_id, test_user.id, session,
        )
        # The valid entry is NOT dismissed → page is NOT deleted → no
        # "regenerate or delete" action taken on this candidate. The
        # function returns an empty list (no new pages created either,
        # because the page already exists and no entry-count growth path
        # is triggered without enough active claims).
        assert isinstance(result, list)

    # Verify the page survived
    async with factory() as verify:
        result = await verify.execute(
            select(KnowledgePage).where(KnowledgePage.id == page.id)
        )
        survivor = result.scalar_one_or_none()
        assert survivor is not None, (
            "concept page wrongly deleted via existing-page branch crash"
        )


@pytest.mark.asyncio
async def test_auto_supersede_idempotent_on_existing_contradicts_link(
    db_engine, db_session: AsyncSession, test_user: User, test_project: Project,
    monkeypatch,
):
    """tk_09d8bdf4f6374a13 regression — _auto_supersede previously created
    a 'contradicts' KnowledgeLink on every compile pass without checking
    whether a link for the same (source_id, target_id) pair already existed.
    The supersedes path was self-gating via `superseded_by`, but contradicts
    had no such gate. Each subsequent /compile triggered a UniqueViolationError
    on uq_kl_link at autoflush time. The error bubbled up uncaught past
    FastAPI middleware (the route has no try/except around
    compile_project_context), Starlette returned its default 21-byte
    text/plain 'Internal Server Error' 500, and ops misread the response
    as a Cloud Run worker kill — the bug that hit proj_c0242b0fccbd48b4 on
    2026-05-20 after v0.10.13 R5 restore.

    Codex R1 MEDIUM 2 — force the contradicts branch deterministically by
    monkeypatching word_overlap to return 0.75 (in the 0.5–0.9 contradicts
    band). Without this monkeypatch the test content could cross the
    overlap > 0.9 threshold and use the supersedes branch, which is already
    self-gating and wouldn't actually exercise the original failure mode.

    The fix prefetches existing entry→entry link pairs (source_id, target_id)
    — NOT including link_type, because uq_kl_link is on the pair only
    (Codex R1 HIGH catch) — and skips db.add(link) when the pair already
    exists. Second compile must NOT raise IntegrityError.
    """
    from sessionfs.server.db.models import KnowledgeLink
    from sessionfs.server.services import knowledge as knowledge_mod
    from sessionfs.server.services.compiler import _auto_supersede
    from sqlalchemy.ext.asyncio import async_sessionmaker

    # Force the contradicts branch by pinning word_overlap to 0.75 (Codex R1
    # MEDIUM 2). The compiler module imports word_overlap inside the
    # function body via a local `from ... import` re-bind, so we patch the
    # source module — that's where `_wo` actually resolves.
    monkeypatch.setattr(knowledge_mod, "word_overlap", lambda *a, **k: 0.75)

    older = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_a",
        user_id=test_user.id,
        entry_type="decision",
        content="Use PostgreSQL with read replicas and connection pooling enabled.",
        confidence=0.9,
        claim_class="claim",
        dismissed=False,
        entity_ref="src/db.py",
        freshness_class="current",
    )
    newer = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_b",
        user_id=test_user.id,
        entry_type="decision",
        content="Use PostgreSQL with hot standby replicas configured for failover.",
        confidence=0.9,
        claim_class="claim",
        dismissed=False,
        entity_ref="src/db.py",
        freshness_class="current",
    )
    db_session.add(older)
    await db_session.commit()
    await db_session.refresh(older)
    db_session.add(newer)
    await db_session.commit()
    await db_session.refresh(newer)
    project_id = test_project.id
    await db_session.close()

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # First compile: creates the contradicts link. word_overlap pinned to 0.75
    # forces this branch.
    async with factory() as s1:
        await _auto_supersede(project_id, s1)
        await s1.commit()

    # Verify a contradicts link got created (proves we hit the failure branch)
    async with factory() as verify:
        links = (
            await verify.execute(
                select(KnowledgeLink).where(
                    KnowledgeLink.project_id == project_id,
                    KnowledgeLink.source_type == "entry",
                    KnowledgeLink.target_type == "entry",
                )
            )
        ).scalars().all()
        assert len(links) == 1, f"expected 1 link, got {len(links)}"
        assert links[0].link_type == "contradicts", (
            f"expected contradicts branch, got link_type={links[0].link_type!r}"
        )
        initial_link_count = len(links)

    # Second compile on the SAME data shape: must NOT raise UniqueViolation.
    # Pre-fix this raised IntegrityError → Starlette 21-byte text/plain 500.
    async with factory() as s2:
        await _auto_supersede(project_id, s2)
        await s2.commit()

    # Verify no duplicate links were created (count stayed flat)
    async with factory() as verify2:
        links = (
            await verify2.execute(
                select(KnowledgeLink).where(
                    KnowledgeLink.project_id == project_id,
                    KnowledgeLink.source_type == "entry",
                    KnowledgeLink.target_type == "entry",
                )
            )
        ).scalars().all()
        assert len(links) == initial_link_count, (
            "_auto_supersede must be idempotent — second pass should not "
            f"create duplicate links (saw {len(links)} vs initial {initial_link_count})"
        )


@pytest.mark.asyncio
async def test_auto_supersede_skips_when_other_link_type_already_exists(
    db_engine, db_session: AsyncSession, test_user: User, test_project: Project,
    monkeypatch,
):
    """Codex R1 HIGH (tk_09d8bdf4f6374a13) regression — uq_kl_link is on
    (project_id, source_type, source_id, target_type, target_id), NOT on
    link_type. So if an existing KnowledgeLink for the same source→target
    pair has any link_type, a new add() for that pair MUST be skipped even
    if it would use a DIFFERENT link_type.

    Mixed-link-type case: seed an existing 'related' link between two entries,
    then run _auto_supersede with overlap forced into the contradicts band.
    Pre-fix: tried to add 'contradicts' on top, IntegrityError. Post-fix:
    pair-level prefetch sees the existing row and skips.
    """
    from sessionfs.server.db.models import KnowledgeLink
    from sessionfs.server.services import knowledge as knowledge_mod
    from sessionfs.server.services.compiler import _auto_supersede
    from sqlalchemy.ext.asyncio import async_sessionmaker

    monkeypatch.setattr(knowledge_mod, "word_overlap", lambda *a, **k: 0.75)

    older = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_a",
        user_id=test_user.id,
        entry_type="decision",
        content="Use PostgreSQL with read replicas and connection pooling enabled.",
        confidence=0.9,
        claim_class="claim",
        dismissed=False,
        entity_ref="src/db.py",
        freshness_class="current",
    )
    newer = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_b",
        user_id=test_user.id,
        entry_type="decision",
        content="Use PostgreSQL with hot standby replicas configured for failover.",
        confidence=0.9,
        claim_class="claim",
        dismissed=False,
        entity_ref="src/db.py",
        freshness_class="current",
    )
    db_session.add(older)
    await db_session.commit()
    await db_session.refresh(older)
    db_session.add(newer)
    await db_session.commit()
    await db_session.refresh(newer)

    # Seed an existing 'related' link for the same pair (different link_type)
    db_session.add(KnowledgeLink(
        project_id=test_project.id,
        source_type="entry",
        source_id=str(newer.id),
        target_type="entry",
        target_id=str(older.id),
        link_type="related",
    ))
    await db_session.commit()
    project_id = test_project.id
    await db_session.close()

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    # _auto_supersede must see the existing 'related' link and skip adding
    # a 'contradicts' for the same pair. No IntegrityError.
    async with factory() as s1:
        await _auto_supersede(project_id, s1)
        await s1.commit()

    # Verify still exactly one link, still 'related'
    async with factory() as verify:
        links = (
            await verify.execute(
                select(KnowledgeLink).where(
                    KnowledgeLink.project_id == project_id,
                    KnowledgeLink.source_type == "entry",
                    KnowledgeLink.target_type == "entry",
                )
            )
        ).scalars().all()
        assert len(links) == 1, (
            f"pair-level dedup should leave count at 1, got {len(links)}"
        )
        assert links[0].link_type == "related", (
            f"original 'related' link must survive, got {links[0].link_type!r}"
        )


@pytest.mark.asyncio
async def test_auto_generate_concepts_flushes_delete_before_insert(
    db_engine, db_session: AsyncSession, test_user: User, test_project: Project,
    monkeypatch,
):
    """tk_09d8bdf4f6374a13 R2-followup regression — auto_generate_concepts'
    existing-page branch deletes prefetched entry→page links and then
    immediately adds new ones for the same (entry_id, page_id) pairs.
    SQLAlchemy UnitOfWork orders INSERTs ahead of DELETEs for the same
    table by default, so on commit the INSERT for entry1→page1 fires
    while the old entry1→page1 row is still present, violating uq_kl_link.
    The IntegrityError bubbles past the route's try/except, leaves the
    session in PendingRollback state, and the next _count_pages call
    surfaces a Starlette text/plain 500.

    The fix calls `await db.flush()` after the delete loop and before
    the add loop so DELETEs land in the transaction (still rollbackable)
    ahead of INSERTs.
    """
    from sessionfs.server.db.models import KnowledgeLink, KnowledgePage
    from sessionfs.server.services import compiler as compiler_mod
    from sessionfs.server.services.compiler import auto_generate_concepts
    from sqlalchemy.ext.asyncio import async_sessionmaker

    # Seed TWO active claims with topic-matching content so matched_entries
    # has length 2 (Codex R3 MEDIUM — with a single entry and entry_count=1
    # on the page, the growth guard `len(matched_entries) <= old_count * 1.5`
    # evaluates `1 <= 1.5` and `continue`s before article generation, link
    # deletion, db.flush(), or link re-add — the regenerate branch never
    # actually runs and the test is a false positive).
    entry_a = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_concept_a",
        user_id=test_user.id,
        entry_type="pattern",
        content=(
            "All converters follow the pattern: parse native format then "
            "translate to canonical .sfs then write. Workspace-relative paths only."
        ),
        confidence=0.9,
        claim_class="claim",
        dismissed=False,
        freshness_class="current",
    )
    entry_b = KnowledgeEntry(
        project_id=test_project.id,
        session_id="ses_concept_b",
        user_id=test_user.id,
        entry_type="pattern",
        content=(
            "Each converter normalises its native session format into the "
            "canonical .sfs converter pipeline before disk write."
        ),
        confidence=0.9,
        claim_class="claim",
        dismissed=False,
        freshness_class="current",
    )
    db_session.add(entry_a)
    db_session.add(entry_b)
    await db_session.commit()
    await db_session.refresh(entry_a)
    await db_session.refresh(entry_b)

    page = KnowledgePage(
        id=f"kp_{uuid.uuid4().hex[:16]}",
        project_id=test_project.id,
        slug="concept/converter-pattern",
        title="Converter Pattern",
        page_type="concept",
        content="# Converter Pattern\n\n- placeholder",
        word_count=3,
        entry_count=1,
        auto_generated=True,
    )
    db_session.add(page)
    await db_session.commit()
    await db_session.refresh(page)

    # Pre-existing link for (entry_a, page). The regenerate branch must
    # delete this link and re-add it — the failure mode under uq_kl_link.
    db_session.add(KnowledgeLink(
        project_id=test_project.id,
        source_type="entry",
        source_id=str(entry_a.id),
        target_type="page",
        target_id=page.id,
        link_type="contributes",
        confidence=1.0,
    ))
    await db_session.commit()
    project_id = test_project.id
    entry_a_id = entry_a.id
    entry_b_id = entry_b.id
    page_id = page.id
    await db_session.close()

    async def _fake_candidates(*args, **kwargs):
        return [{
            "slug": "converter-pattern",
            "topic": "converter pattern",
            "entry_count": 5,
            "summary": "Pattern across converters",
        }]
    monkeypatch.setattr(compiler_mod, "check_concept_candidates", _fake_candidates)

    # Bypass LLM article generation; return deterministic content.
    async def _fake_article(*args, **kwargs):
        return "# Converter Pattern\n\n- Updated content"
    monkeypatch.setattr(compiler_mod, "generate_concept_article", _fake_article)

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        # Pre-fix: this raised IntegrityError on commit (uq_kl_link) because
        # INSERT for (entry_a, page, contributes) fired before DELETE for the
        # same row. Post-fix: flush serialises the delete ahead of the new
        # link inserts cleanly.
        result = await auto_generate_concepts(
            project_id, test_user.id, session,
        )

    # Codex R3 MEDIUM — assert the regenerate branch actually ran. result
    # must contain the concept page, the page content must be the fake
    # article (proves regenerate path completed), and both new links must
    # exist (proves re-add succeeded).
    assert isinstance(result, list)
    assert any(c["slug"] == "concept/converter-pattern" for c in result), (
        f"regenerate branch did not run — concept/converter-pattern not in "
        f"result: {result}"
    )

    async with factory() as verify:
        page_row = (
            await verify.execute(
                select(KnowledgePage).where(KnowledgePage.id == page_id)
            )
        ).scalar_one_or_none()
        assert page_row is not None, "page wrongly deleted"
        assert page_row.content == "# Converter Pattern\n\n- Updated content", (
            f"regenerate branch did not write the fake article content; "
            f"page.content={page_row.content!r}"
        )

        # Exactly one (entry_a, page) link survives the delete+re-add cycle.
        links_a = (
            await verify.execute(
                select(KnowledgeLink).where(
                    KnowledgeLink.project_id == project_id,
                    KnowledgeLink.target_id == page_id,
                    KnowledgeLink.source_id == str(entry_a_id),
                )
            )
        ).scalars().all()
        assert len(links_a) == 1, (
            f"expected exactly 1 (entry_a, page) link after delete+re-add, "
            f"got {len(links_a)}"
        )
        # entry_b's new link must also be present (proves re-add ran)
        links_b = (
            await verify.execute(
                select(KnowledgeLink).where(
                    KnowledgeLink.project_id == project_id,
                    KnowledgeLink.target_id == page_id,
                    KnowledgeLink.source_id == str(entry_b_id),
                )
            )
        ).scalars().all()
        assert len(links_b) == 1, (
            f"expected exactly 1 (entry_b, page) new link, got {len(links_b)}"
        )


# ─────────────────────────────────────────────────────────────────────
# v0.10.23 tk_49db8d2b6c424d35 — entity_ref upsert semantics
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_entity_ref_upsert_skips_similarity_dedup(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """Headline repro from the 2026-05-25 Scout cache incident: posting
    5x to the same entity_ref with ~6% delta each time must persist
    every write, not 409 on the similarity gate. Pre-fix, runs 650-653
    of Scout's n8n workflow lost 3 of 4 cache writes silently."""
    entity_ref = "scout-state:decisions"
    # Build a fake LRU map so each write differs by ~6% (1 of 16 keys).
    base = {f"key_{i:03d}": f"signal_id_{i}_classified_as_noise" for i in range(15)}

    last_id = None
    for i in range(15, 20):  # 5 writes, appending one key each
        base[f"key_{i:03d}"] = f"signal_id_{i}_classified_as_signal"
        r = await client.post(
            f"/api/v1/projects/{test_project.id}/entries/add",
            headers=auth_headers,
            json={
                "content": str(base),  # ~15 KB-ish JSON-shaped string
                "entry_type": "discovery",
                "entity_ref": entity_ref,
                "session_id": "manual",
                "confidence": 0.9,
                "upsert": True,
            },
        )
        assert r.status_code == 201, (
            f"write {i} returned {r.status_code}: {r.text} "
            f"(entity_ref upsert must bypass similarity dedup)"
        )
        body = r.json()
        if last_id is not None:
            assert body["upserted_from"] == [last_id], (
                f"write {i} should report upserted_from=[{last_id}], "
                f"got {body['upserted_from']}"
            )
        last_id = body["id"]

    # Only the latest entry is active.
    rows = (
        await db_session.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.project_id == test_project.id,
                KnowledgeEntry.entity_ref == entity_ref,
            )
        )
    ).scalars().all()
    active = [r for r in rows if not r.dismissed]
    assert len(active) == 1, (
        f"exactly one active row expected, got {len(active)} "
        f"(prior writes must be auto-superseded)"
    )
    assert active[0].id == last_id
    # Dismissed rows form a chain: each prior points to its immediate
    # successor, not necessarily the final active entry. Walk the chain
    # from the active head backward and confirm every dismissed row is
    # reachable + carries the audit trail.
    superseded = sorted([r for r in rows if r.dismissed], key=lambda r: r.id)
    assert len(superseded) == 4
    by_id = {r.id: r for r in rows}
    cursor = active[0]
    walked = 0
    while True:
        prior = next(
            (r for r in superseded if r.superseded_by == cursor.id),
            None,
        )
        if prior is None:
            break
        assert prior.dismissed_reason and "upsert" in prior.dismissed_reason
        assert prior.supersession_reason == "entity_ref upsert"
        assert prior.dismissed_by == test_user.id
        cursor = prior
        walked += 1
    assert walked == 4, f"expected to walk all 4 dismissed priors, walked {walked}"
    # Silence unused-name warning while keeping the lookup map for clarity.
    assert by_id  # noqa: PT018


@pytest.mark.asyncio
async def test_entity_ref_upsert_response_empty_when_no_prior(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """First write against a fresh entity_ref returns upserted_from=[].
    Distinguishes 'rolled forward' from 'brand new'."""
    r = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/add",
        headers=auth_headers,
        json={
            "content": "Initial scout cache state with several distinct entries to satisfy length gates and so on",
            "entry_type": "discovery",
            "entity_ref": "fresh-state:cache",
            "session_id": "manual",
            "confidence": 0.9,
            "upsert": True,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["upserted_from"] == []


@pytest.mark.asyncio
async def test_no_entity_ref_still_blocks_near_duplicates(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """The similarity gate is unchanged for adds without entity_ref —
    the upsert path is opt-in via entity_ref, not a blanket bypass."""
    payload = {
        "content": "Adopt scoped service API keys for cloud agents — user tokens too broad",
        "entry_type": "decision",
        "session_id": "manual",
        "confidence": 0.9,
    }
    r1 = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/add",
        headers=auth_headers,
        json=payload,
    )
    assert r1.status_code == 201
    # Same content again, no entity_ref → still 409.
    r2 = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/add",
        headers=auth_headers,
        json=payload,
    )
    assert r2.status_code == 409
    assert "Similar entry" in r2.json()["error"]["message"]


@pytest.mark.asyncio
async def test_entity_ref_upsert_skips_dismissed_priors(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """A user-dismissed prior with the same entity_ref must NOT count
    as an active row. The next write to that entity_ref is a 'brand
    new' add (upserted_from=[]), and the standard similarity gate
    applies against other active entries."""
    # Seed a dismissed prior directly (skips the route).
    prior = KnowledgeEntry(
        project_id=test_project.id,
        session_id="manual",
        user_id=test_user.id,
        entry_type="discovery",
        content="Prior content that was explicitly dismissed by the user.",
        confidence=0.9,
        entity_ref="reusable-state:slot",
        dismissed=True,
        dismissed_at=datetime.now(timezone.utc),
        dismissed_by=test_user.id,
        dismissed_reason="user dismissed",
    )
    db_session.add(prior)
    await db_session.commit()

    r = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/add",
        headers=auth_headers,
        json={
            "content": "Fresh content that bears no resemblance to the dismissed prior.",
            "entry_type": "discovery",
            "entity_ref": "reusable-state:slot",
            "session_id": "manual",
            "confidence": 0.9,
            "upsert": True,
        },
    )
    assert r.status_code == 201
    assert r.json()["upserted_from"] == []


@pytest.mark.asyncio
async def test_entity_ref_without_upsert_preserves_multi_active(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """Codex R1 MEDIUM safety net — entity_ref semantics ('what this
    claim is about') must survive. Multiple distinct claims about the
    same file/function/article/etc. must coexist as active rows when
    the caller has NOT opted into upsert. Compile-time _auto_supersede
    reasons about these; the Scout n8n docs use entity_ref as a
    canonical per-signal id (e.g. hn:38291847) — neither pattern
    should silently dismiss priors."""
    shared_entity = "src/sessionfs/server/db.py"
    # Two genuinely different claims about the same file (e.g. two
    # different discoveries during separate sessions).
    payload_a = {
        "content": "db.py uses connection pooling with max=50 by default for Cloud SQL",
        "entry_type": "discovery",
        "entity_ref": shared_entity,
        "session_id": "manual",
        "confidence": 0.9,
    }
    payload_b = {
        "content": "db.py declares isolation_level=READ COMMITTED via the engine config",
        "entry_type": "discovery",
        "entity_ref": shared_entity,
        "session_id": "manual",
        "confidence": 0.9,
    }

    r_a = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/add",
        headers=auth_headers,
        json=payload_a,
    )
    assert r_a.status_code == 201, r_a.text
    assert r_a.json()["upserted_from"] == []

    r_b = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/add",
        headers=auth_headers,
        json=payload_b,
    )
    assert r_b.status_code == 201, r_b.text
    # Both claims active — entry_a NOT dismissed by entry_b's insert.
    assert r_b.json()["upserted_from"] == []

    rows = (
        await db_session.execute(
            select(KnowledgeEntry).where(
                KnowledgeEntry.project_id == test_project.id,
                KnowledgeEntry.entity_ref == shared_entity,
            )
        )
    ).scalars().all()
    active = [r for r in rows if not r.dismissed]
    assert len(active) == 2, (
        f"expected both distinct claims active, got {len(active)} "
        f"(upsert=False must preserve multi-claim entity_ref)"
    )


@pytest.mark.asyncio
async def test_upsert_true_bypasses_similarity_on_fresh_slot(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """Codex R2 MEDIUM — the upsert contract says explicit upsert=true
    skips similarity dedup, full stop. Pre-fix the route only skipped
    when a prior active entry existed, so the first write to a fresh
    slot (or a slot whose prior was user-dismissed, or a renamed
    slot) could still hit 409 against an UNRELATED active entry. That
    breaks the state-cache bootstrap and reopens the original silent-
    loss class.

    Setup: seed an unrelated active KB entry whose content is the
    near-duplicate trigger. Then POST upsert=true to a FRESH
    entity_ref with the same content. Pre-fix: 409. Post-fix: 201
    with upserted_from=[] (no priors to dismiss).
    """
    trigger_content = (
        "Adopt scoped service API keys for cloud agents — user tokens "
        "too broad for Bedrock/Vertex/CI agents."
    )
    seed = KnowledgeEntry(
        project_id=test_project.id,
        session_id="manual",
        user_id=test_user.id,
        entry_type="decision",
        content=trigger_content,
        confidence=0.9,
        # No entity_ref — purely a similarity-gate trigger.
    )
    db_session.add(seed)
    await db_session.commit()

    # Same content, fresh state-cache slot. The fresh slot has no
    # active prior, but the explicit upsert=true must still bypass
    # the similarity gate against the unrelated seed row.
    r = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/add",
        headers=auth_headers,
        json={
            "content": trigger_content,
            "entry_type": "decision",
            "entity_ref": "scout-state:bootstrap-decisions",
            "session_id": "manual",
            "confidence": 0.9,
            "upsert": True,
        },
    )
    assert r.status_code == 201, (
        f"upsert=true on a fresh slot must bypass similarity dedup "
        f"even when an unrelated active entry triggers the 0.85 word "
        f"overlap. Got {r.status_code}: {r.text}"
    )
    body = r.json()
    assert body["upserted_from"] == [], (
        f"fresh slot → no priors to dismiss. Got {body['upserted_from']}"
    )


@pytest.mark.asyncio
async def test_upsert_true_without_entity_ref_returns_422(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """upsert=True with no entity_ref is a contract violation — no
    slot to roll forward. Server must reject 422 before any side
    effects."""
    r = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/add",
        headers=auth_headers,
        json={
            "content": "Long enough content with concrete file refs like src/foo.py to clear specificity gate.",
            "entry_type": "discovery",
            "session_id": "manual",
            "confidence": 0.9,
            "upsert": True,
        },
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_entity_ref_upsert_does_not_leak_across_projects(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession,
    test_user: User, test_project: Project,
):
    """Same entity_ref in a different project is invisible — the
    upsert query is scoped by project_id, and the project access gate
    already isolates the caller. Defense against an attacker who knows
    a victim project's entity_ref guessing across tenants."""
    other_project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="Other Project",
        git_remote_normalized=f"github.com/other/{uuid.uuid4().hex[:8]}",
        owner_id=test_user.id,
    )
    db_session.add(other_project)
    await db_session.commit()
    await db_session.refresh(other_project)

    # Seed an entity_ref in OTHER project.
    other_entry = KnowledgeEntry(
        project_id=other_project.id,
        session_id="manual",
        user_id=test_user.id,
        entry_type="discovery",
        content="Other project's cached state with rich content for length gating.",
        confidence=0.9,
        entity_ref="shared-ref:slot",
    )
    db_session.add(other_entry)
    await db_session.commit()
    await db_session.refresh(other_entry)

    # Write the same entity_ref in test_project — must not touch other_project.
    r = await client.post(
        f"/api/v1/projects/{test_project.id}/entries/add",
        headers=auth_headers,
        json={
            "content": "Distinct content in the caller's own project, sharing only entity_ref by chance.",
            "entry_type": "discovery",
            "entity_ref": "shared-ref:slot",
            "session_id": "manual",
            "confidence": 0.9,
            "upsert": True,
        },
    )
    assert r.status_code == 201
    assert r.json()["upserted_from"] == []

    # The other project's entry MUST still be active.
    refreshed = (
        await db_session.execute(
            select(KnowledgeEntry).where(KnowledgeEntry.id == other_entry.id)
        )
    ).scalar_one()
    assert refreshed.dismissed is False
    assert refreshed.superseded_by is None


