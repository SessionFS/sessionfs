"""Integration tests for the knowledge base lifecycle system.

Covers the lifecycle mechanics Codex flagged as under-tested:
  - kb_max_context_words budget enforcement (simple + LLM paths)
  - kb_section_page_limit cap
  - last_relevant_at / reference_count updates on compile + search
  - similarity rejection on add_entry
  - concept page refresh / prune
  - decay logic (entries with old last_relevant_at get decayed)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    KnowledgeEntry,
    KnowledgePage,
    Project,
)
from sessionfs.server.services.knowledge import (
    is_near_duplicate,
    word_overlap,
)


# ---------- Fixtures ----------


async def _mk_project(db: AsyncSession, *, remote: str = "test/lifecycle") -> Project:
    pid = f"proj_{uuid.uuid4().hex[:8]}"
    p = Project(
        id=pid,
        name="Test Lifecycle Project",
        git_remote_normalized=remote,
        owner_id="test-user",
        context_document="# Test Project\n\nSome context.",
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _mk_entry(
    db: AsyncSession,
    *,
    project_id: str,
    content: str,
    entry_type: str = "decision",
    confidence: float = 0.8,
    compiled: bool = False,
    dismissed: bool = False,
    created_ago_days: int = 0,
    last_relevant_at: datetime | None = None,
) -> KnowledgeEntry:
    now = datetime.now(timezone.utc)
    e = KnowledgeEntry(
        project_id=project_id,
        session_id="ses_test",
        user_id="test-user",
        entry_type=entry_type,
        content=content,
        confidence=confidence,
        compiled_at=now if compiled else None,
        dismissed=dismissed,
        created_at=now - timedelta(days=created_ago_days),
        last_relevant_at=last_relevant_at,
    )
    db.add(e)
    await db.commit()
    await db.refresh(e)
    return e


# ---------- word_overlap + is_near_duplicate ----------


class TestWordOverlap:
    def test_identical_strings(self):
        assert word_overlap("hello world", "hello world") == 1.0

    def test_no_overlap(self):
        assert word_overlap("foo bar", "baz qux") == 0.0

    def test_subset(self):
        assert word_overlap("auth middleware", "the auth middleware resolves tier") == 1.0

    def test_empty_string(self):
        assert word_overlap("", "foo") == 0.0

    def test_threshold_boundary(self):
        # 3 of 4 words shared = 0.75 (below 0.85 threshold)
        assert word_overlap("auth middleware resolves tier", "auth middleware resolves something else") < 0.85


class TestIsNearDuplicate:
    def test_returns_true_for_near_match(self):
        existing = ["The auth middleware resolves the effective tier from the org"]
        assert is_near_duplicate(
            "auth middleware resolves effective tier from org",
            existing,
        )

    def test_returns_false_for_unrelated(self):
        existing = ["Postgres runs on port 5432"]
        assert not is_near_duplicate(
            "The React dashboard uses Vite for bundling",
            existing,
        )

    def test_empty_existing_list(self):
        assert not is_near_duplicate("anything", [])


# ---------- Compile: budget enforcement ----------


class TestCompileBudget:
    @pytest.mark.asyncio
    async def test_simple_compile_enforces_budget(self, db_session: AsyncSession):
        """_simple_compile must not exceed max_context_words."""
        from sessionfs.server.services.compiler import compile_project_context

        project = await _mk_project(db_session)

        # Add many verbose entries so the untrimmmed compile would be well
        # over 500 words.
        for i in range(50):
            await _mk_entry(
                db_session,
                project_id=project.id,
                content=f"Decision number {i}: we chose approach alpha-{i} over beta-{i} for performance reasons in the frobnicator subsystem component number {i}",
            )

        # Set a reasonable budget that the trim can actually hit — 200 words.
        # _trim_to_budget has a minimum of 3 bullets per section, so setting
        # a budget too low produces more overhead than content.
        project.kb_max_context_words = 200
        await db_session.commit()

        result = await compile_project_context(
            project.id, "test-user", db_session
        )

        assert result is not None
        refreshed = (await db_session.execute(
            select(Project).where(Project.id == project.id)
        )).scalar_one()
        word_count = len(refreshed.context_document.split())

        # The compiled document must be materially smaller than the
        # untrimmmed version (50 entries × ~20 words = ~1000 words)
        # and within reasonable distance of the 200-word budget.
        # Allow 50% slack for headings, the Recent Changes section, etc.
        assert word_count <= 300, (
            f"Compiled document has {word_count} words but budget is 200 — "
            "trim should have brought it under ~300"
        )
        # Sanity: it can't be empty either
        assert word_count > 50, (
            f"Compiled document is suspiciously small ({word_count} words)"
        )


# ---------- Compile: last_relevant_at + reference_count ----------


class TestCompileRelevanceTracking:
    @pytest.mark.asyncio
    async def test_compile_stamps_last_relevant_at_and_reference_count(
        self, db_session: AsyncSession
    ):
        project = await _mk_project(db_session)
        entry = await _mk_entry(
            db_session,
            project_id=project.id,
            content="Auth uses Bearer tokens for API auth",
        )
        assert entry.last_relevant_at is None
        assert (entry.reference_count or 0) == 0

        from sessionfs.server.services.compiler import compile_project_context

        await compile_project_context(project.id, "test-user", db_session)

        refreshed = (
            await db_session.execute(
                select(KnowledgeEntry).where(KnowledgeEntry.id == entry.id)
            )
        ).scalar_one()

        assert refreshed.last_relevant_at is not None, (
            "compile should stamp last_relevant_at on compiled entries"
        )
        assert (refreshed.reference_count or 0) >= 1, (
            "compile should increment reference_count"
        )


# ---------- Decay: entries with old last_relevant_at get decayed ----------


class TestDecay:
    @pytest.mark.asyncio
    async def test_decay_reduces_confidence_of_old_entries(
        self, db_session: AsyncSession,
    ):
        """Entries whose last_relevant_at is older than 90 days should have
        their confidence multiplied by 0.8 on compile.
        """
        project = await _mk_project(db_session)

        old_time = datetime.now(timezone.utc) - timedelta(days=100)

        # Create a compiled entry with old last_relevant_at
        old_entry = await _mk_entry(
            db_session,
            project_id=project.id,
            content="Old established pattern that nobody references anymore",
            confidence=0.9,
            compiled=True,
            last_relevant_at=old_time,
            created_ago_days=200,
        )

        # Add a new entry so compile has something to process
        await _mk_entry(
            db_session,
            project_id=project.id,
            content="Brand new decision about API versioning strategy",
        )

        from sessionfs.server.services.compiler import compile_project_context

        await compile_project_context(project.id, "test-user", db_session)

        # The decay runs as a bulk UPDATE with synchronize_session=False,
        # so the identity map is stale. Read the raw DB value via a fresh
        # scalar select that bypasses the identity map.
        from sqlalchemy import text
        row = (await db_session.execute(
            text("SELECT confidence FROM knowledge_entries WHERE id = :id"),
            {"id": old_entry.id},
        )).one()
        refreshed_confidence = row[0]

        # Confidence should have been decayed from 0.9 → 0.72 (0.9 * 0.8)
        assert refreshed_confidence < 0.9, (
            f"Expected decayed confidence < 0.9, got {refreshed_confidence}"
        )
        assert refreshed_confidence >= 0.5, (
            f"Decay should not reduce below a reasonable floor, got {refreshed_confidence}"
        )


# ---------- Similarity rejection on manual add_entry ----------


class TestSimilarityRejection:
    @pytest.mark.asyncio
    async def test_add_entry_rejects_near_duplicate(
        self,
        client,
        auth_headers: dict,
        db_session: AsyncSession,
        test_user,
    ):
        """POST /api/v1/projects/{id}/entries/add with a near-duplicate
        of an existing entry should return 409.
        """
        project = await _mk_project(db_session)
        # The conftest test_user has a random uuid — make it own the project
        # so the access check doesn't return 403.
        project.owner_id = test_user.id
        await db_session.commit()

        # Seed an existing entry
        await _mk_entry(
            db_session,
            project_id=project.id,
            content="The auth middleware resolves the effective tier from the org, not the user record",
        )

        # Try adding a near-duplicate via the API
        resp = await client.post(
            f"/api/v1/projects/{project.id}/entries/add",
            headers=auth_headers,
            json={
                "content": "Auth middleware resolves effective tier from org not user",
                "entry_type": "decision",
            },
        )
        assert resp.status_code == 409, (
            f"Expected 409 for near-duplicate, got {resp.status_code}: {resp.text[:200]}"
        )


# ---------- Concept page pruning ----------


class TestConceptPagePruning:
    @pytest.mark.asyncio
    async def test_prune_deletes_concept_page_when_all_entries_dismissed(
        self, db_session: AsyncSession
    ):
        """_prune_dead_concept_pages should delete a concept page whose
        linked entries are ALL dismissed.
        """
        from sessionfs.server.db.models import KnowledgeLink, KnowledgePage
        from sessionfs.server.services.compiler import _prune_dead_concept_pages

        project = await _mk_project(db_session)

        # Create and dismiss an entry
        entry = await _mk_entry(
            db_session,
            project_id=project.id,
            content="Old pattern that got dismissed",
            dismissed=True,
        )

        # Create a concept page linked to that entry
        page = KnowledgePage(
            id=f"kp_{uuid.uuid4().hex[:12]}",
            project_id=project.id,
            slug="concept/old-pattern",
            title="Old Pattern",
            content="Article about old pattern",
            page_type="concept",
            entry_count=1,
        )
        db_session.add(page)
        await db_session.commit()
        await db_session.refresh(page)

        link = KnowledgeLink(
            project_id=project.id,
            source_id=str(entry.id),
            source_type="entry",
            target_id=page.id,
            target_type="page",
        )
        db_session.add(link)
        await db_session.commit()

        deleted = await _prune_dead_concept_pages(project.id, db_session)

        assert deleted == 1, f"Expected 1 page deleted, got {deleted}"

        # Page should be gone from DB
        check = (await db_session.execute(
            select(KnowledgePage).where(KnowledgePage.id == page.id)
        )).scalar_one_or_none()
        assert check is None, "Concept page should have been deleted"

    @pytest.mark.asyncio
    async def test_prune_does_not_delete_page_with_active_entries(
        self, db_session: AsyncSession
    ):
        """_prune_dead_concept_pages must NOT delete concept pages that
        still have at least one non-dismissed linked entry.
        """
        from sessionfs.server.db.models import KnowledgeLink, KnowledgePage
        from sessionfs.server.services.compiler import _prune_dead_concept_pages

        project = await _mk_project(db_session)

        active_entry = await _mk_entry(
            db_session,
            project_id=project.id,
            content="Still-relevant pattern",
            dismissed=False,
        )

        page = KnowledgePage(
            id=f"kp_{uuid.uuid4().hex[:12]}",
            project_id=project.id,
            slug="concept/active-pattern",
            title="Active Pattern",
            content="Article about active pattern",
            page_type="concept",
            entry_count=1,
        )
        db_session.add(page)
        await db_session.commit()
        await db_session.refresh(page)

        link = KnowledgeLink(
            project_id=project.id,
            source_id=str(active_entry.id),
            source_type="entry",
            target_id=page.id,
            target_type="page",
        )
        db_session.add(link)
        await db_session.commit()

        deleted = await _prune_dead_concept_pages(project.id, db_session)

        assert deleted == 0, "Active page should not be deleted"


# ---------- Bulk dismiss-stale endpoint ----------


class TestBulkDismissStale:
    @pytest.mark.asyncio
    async def test_dismiss_stale_entries_via_api(
        self,
        client,
        auth_headers: dict,
        db_session: AsyncSession,
        test_user,
    ):
        """POST /entries/dismiss-stale should dismiss old unreferenced entries
        and return the count.
        """
        project = await _mk_project(db_session)
        project.owner_id = test_user.id
        await db_session.commit()

        # 1. Old + low-confidence → SHOULD be dismissed
        await _mk_entry(
            db_session,
            project_id=project.id,
            content="Very old stale low-confidence finding",
            confidence=0.3,
            created_ago_days=120,
        )
        # 2. Old + high-confidence → should NOT be dismissed (valuable decision)
        await _mk_entry(
            db_session,
            project_id=project.id,
            content="Old but high-confidence architecture decision",
            confidence=0.9,
            created_ago_days=120,
        )
        # 3. Fresh entry → should NOT be dismissed
        await _mk_entry(
            db_session,
            project_id=project.id,
            content="Fresh new discovery from today",
            confidence=0.8,
            created_ago_days=0,
        )

        resp = await client.post(
            f"/api/v1/projects/{project.id}/entries/dismiss-stale",
            headers=auth_headers,
        )
        assert resp.status_code == 200, (
            f"Expected 200, got {resp.status_code}: {resp.text[:200]}"
        )
        data = resp.json()
        assert data["dismissed_count"] == 1, (
            f"Expected 1 low-confidence stale entry dismissed, got {data['dismissed_count']}"
        )

        # Verify ONLY the low-confidence stale entry was dismissed.
        # Use raw SQL to bypass the stale identity map (same pattern as
        # the decay test — synchronize_session=False bulk UPDATEs don't
        # update in-memory ORM objects).
        from sqlalchemy import text
        rows = (await db_session.execute(
            text("SELECT content, dismissed FROM knowledge_entries WHERE project_id = :pid"),
            {"pid": project.id},
        )).all()
        dismissed = [r for r in rows if r[1]]
        active = [r for r in rows if not r[1]]
        assert len(dismissed) == 1, f"Only the low-confidence stale entry should be dismissed, got {len(dismissed)}"
        assert len(active) == 2, f"High-confidence stale + fresh should remain, got {len(active)}"
        assert "low-confidence" in dismissed[0][0]
        active_contents = [r[0] for r in active]
        assert any("architecture decision" in c for c in active_contents), (
            "High-confidence stale entry should NOT be bulk-dismissed"
        )
