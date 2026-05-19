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
