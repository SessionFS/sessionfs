"""Integration tests for knowledge entries and compilation."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    ContextCompilation,
    KnowledgeEntry,
    Project,
    Session,
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
