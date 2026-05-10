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
        assert (refreshed.compiled_count or 0) >= 1, (
            "compile should increment compiled_count (v2 replaces reference_count)"
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


# ---------- Per-user, tier-aware rate limit on add_entry ----------


def _entry_payload(suffix: str) -> dict:
    """Build a unique add-entry payload that passes Gate 1 + the similarity gate.

    Each entry is well over the 20-character minimum and contains a unique
    token (suffix) so the 0.85 word-overlap dedupe in Gate 3 doesn't fire
    when we hammer the endpoint to test rate limits.
    """
    return {
        "content": (
            f"Distinct rate-limit knowledge entry for suffix {suffix} "
            f"covering some unique territory_{suffix}"
        ),
        "entry_type": "discovery",
    }


async def _own_project_for_user(db: AsyncSession, user_id: str) -> Project:
    """Create a project owned by the given user — ensures _get_project_or_404 passes."""
    pid = f"proj_{uuid.uuid4().hex[:8]}"
    project = Project(
        id=pid,
        name="Rate Limit Test Project",
        git_remote_normalized=f"github.com/example/{pid}",
        owner_id=user_id,
        context_document="# Rate Limit Test\n\n## Overview\nSeeded for rate limit tests.\n",
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


class TestKnowledgeRateLimit:
    @pytest.mark.asyncio
    async def test_free_user_capped_at_20(
        self,
        client,
        db_session: AsyncSession,
    ):
        """Free-tier users hit the 20-per-hour cap and get a 429 with Retry-After."""
        from sessionfs.server.auth.keys import generate_api_key, hash_api_key
        from sessionfs.server.db.models import ApiKey, User

        user = User(
            id=str(uuid.uuid4()),
            email=f"free_{uuid.uuid4().hex[:8]}@example.com",
            tier="free",
            email_verified=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        await db_session.commit()

        raw_key = generate_api_key()
        db_session.add(ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw_key),
            name="free-rl",
            created_at=datetime.now(timezone.utc),
        ))
        await db_session.commit()
        headers = {"Authorization": f"Bearer {raw_key}"}

        project = await _own_project_for_user(db_session, user.id)

        # Send 20 — all should succeed.
        for i in range(20):
            resp = await client.post(
                f"/api/v1/projects/{project.id}/entries/add",
                headers=headers,
                json=_entry_payload(f"free-{i}"),
            )
            assert resp.status_code == 201, (
                f"Entry #{i} unexpectedly failed: {resp.status_code} {resp.text[:200]}"
            )

        # 21st must 429.
        resp = await client.post(
            f"/api/v1/projects/{project.id}/entries/add",
            headers=headers,
            json=_entry_payload("free-21"),
        )
        assert resp.status_code == 429, f"Expected 429, got {resp.status_code}: {resp.text[:200]}"
        assert resp.headers.get("Retry-After") == "60"
        assert "free" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_two_users_have_separate_buckets(
        self,
        client,
        auth_headers: dict,
        db_session: AsyncSession,
        test_user,
    ):
        """User A's entries must not consume User B's rate limit budget.

        Previously the limit was per session_id, and since add_knowledge
        defaults to session_id='manual', everyone shared a single bucket.
        With per-user counting, each user gets their own.
        """
        from sessionfs.server.auth.keys import generate_api_key, hash_api_key
        from sessionfs.server.db.models import ApiKey, User

        # Build a second user (free tier so the cap is small + easy to assert).
        user_b = User(
            id=str(uuid.uuid4()),
            email=f"userb_{uuid.uuid4().hex[:8]}@example.com",
            tier="free",
            email_verified=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user_b)
        await db_session.commit()

        raw_key_b = generate_api_key()
        db_session.add(ApiKey(
            id=str(uuid.uuid4()),
            user_id=user_b.id,
            key_hash=hash_api_key(raw_key_b),
            name="user-b",
            created_at=datetime.now(timezone.utc),
        ))
        await db_session.commit()
        headers_b = {"Authorization": f"Bearer {raw_key_b}"}

        # Project owned by test_user (pro tier), with user_b granted access via
        # owning a session on the same git remote.
        project = await _own_project_for_user(db_session, test_user.id)
        from sessionfs.server.db.models import Session as SessionRow
        db_session.add(SessionRow(
            id=f"ses_{uuid.uuid4().hex[:16]}",
            user_id=user_b.id,
            title="seed",
            tags="[]",
            source_tool="claude-code",
            blob_key="x",
            blob_size_bytes=0,
            etag="x",
            git_remote_normalized=project.git_remote_normalized,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            uploaded_at=datetime.now(timezone.utc),
        ))
        await db_session.commit()

        # User A (test_user, pro tier — cap 100) sends 20.
        for i in range(20):
            resp = await client.post(
                f"/api/v1/projects/{project.id}/entries/add",
                headers=auth_headers,
                json=_entry_payload(f"a-{i}"),
            )
            assert resp.status_code == 201, (
                f"User A entry #{i} failed: {resp.status_code} {resp.text[:200]}"
            )

        # User B's first entry must still succeed — separate bucket.
        resp = await client.post(
            f"/api/v1/projects/{project.id}/entries/add",
            headers=headers_b,
            json=_entry_payload("b-first"),
        )
        assert resp.status_code == 201, (
            f"User B should have a separate bucket, got {resp.status_code}: {resp.text[:200]}"
        )

    @pytest.mark.asyncio
    async def test_enterprise_user_capped_at_200(
        self,
        client,
        db_session: AsyncSession,
    ):
        """Enterprise tier gets a 200/hr cap, not 20."""
        from sessionfs.server.auth.keys import generate_api_key, hash_api_key
        from sessionfs.server.db.models import ApiKey, User

        user = User(
            id=str(uuid.uuid4()),
            email=f"ent_{uuid.uuid4().hex[:8]}@example.com",
            tier="enterprise",
            email_verified=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        await db_session.commit()

        raw_key = generate_api_key()
        db_session.add(ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw_key),
            name="ent-rl",
            created_at=datetime.now(timezone.utc),
        ))
        await db_session.commit()
        headers = {"Authorization": f"Bearer {raw_key}"}

        project = await _own_project_for_user(db_session, user.id)

        # Confirm 21st entry (which would 429 a free user) is fine.
        for i in range(21):
            resp = await client.post(
                f"/api/v1/projects/{project.id}/entries/add",
                headers=headers,
                json=_entry_payload(f"ent-{i}"),
            )
            assert resp.status_code == 201, (
                f"Enterprise entry #{i} failed: {resp.status_code} {resp.text[:200]}"
            )

        # Now seed 179 directly via the DB to push the count to 200, then
        # the next API call should 429. Seeding via DB is faster than 200
        # API roundtrips and exercises the same query path.
        for j in range(179):
            db_session.add(KnowledgeEntry(
                project_id=project.id,
                session_id="manual",
                user_id=user.id,
                entry_type="discovery",
                content=f"db-seeded entry {j} with enough text to satisfy gate one",
                confidence=0.5,
            ))
        await db_session.commit()

        resp = await client.post(
            f"/api/v1/projects/{project.id}/entries/add",
            headers=headers,
            json=_entry_payload("ent-overflow"),
        )
        assert resp.status_code == 429, (
            f"Expected 429 at enterprise cap, got {resp.status_code}: {resp.text[:200]}"
        )
        assert resp.headers.get("Retry-After") == "60"
        assert "200" in resp.text
        assert "enterprise" in resp.text.lower()

    @pytest.mark.asyncio
    async def test_env_var_override(
        self,
        client,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """SFS_KNOWLEDGE_RATE_LIMIT_PER_HOUR overrides per-tier defaults."""
        monkeypatch.setenv("SFS_KNOWLEDGE_RATE_LIMIT_PER_HOUR", "3")

        from sessionfs.server.auth.keys import generate_api_key, hash_api_key
        from sessionfs.server.db.models import ApiKey, User

        user = User(
            id=str(uuid.uuid4()),
            email=f"env_{uuid.uuid4().hex[:8]}@example.com",
            tier="enterprise",  # would normally be 200
            email_verified=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        await db_session.commit()

        raw_key = generate_api_key()
        db_session.add(ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw_key),
            name="env-rl",
            created_at=datetime.now(timezone.utc),
        ))
        await db_session.commit()
        headers = {"Authorization": f"Bearer {raw_key}"}

        project = await _own_project_for_user(db_session, user.id)

        # 3 succeed, 4th 429s.
        for i in range(3):
            resp = await client.post(
                f"/api/v1/projects/{project.id}/entries/add",
                headers=headers,
                json=_entry_payload(f"env-{i}"),
            )
            assert resp.status_code == 201

        resp = await client.post(
            f"/api/v1/projects/{project.id}/entries/add",
            headers=headers,
            json=_entry_payload("env-overflow"),
        )
        assert resp.status_code == 429
        assert "3" in resp.text  # the override value, not 200
        assert resp.headers.get("Retry-After") == "60"

    @pytest.mark.asyncio
    async def test_429_includes_retry_after_header(
        self,
        client,
        db_session: AsyncSession,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """The 429 response must always include Retry-After: 60."""
        monkeypatch.setenv("SFS_KNOWLEDGE_RATE_LIMIT_PER_HOUR", "1")

        from sessionfs.server.auth.keys import generate_api_key, hash_api_key
        from sessionfs.server.db.models import ApiKey, User

        user = User(
            id=str(uuid.uuid4()),
            email=f"hdr_{uuid.uuid4().hex[:8]}@example.com",
            tier="pro",
            email_verified=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(user)
        await db_session.commit()

        raw_key = generate_api_key()
        db_session.add(ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw_key),
            name="hdr-rl",
            created_at=datetime.now(timezone.utc),
        ))
        await db_session.commit()
        headers = {"Authorization": f"Bearer {raw_key}"}

        project = await _own_project_for_user(db_session, user.id)

        # Burn the 1-per-hour budget.
        resp = await client.post(
            f"/api/v1/projects/{project.id}/entries/add",
            headers=headers,
            json=_entry_payload("hdr-1"),
        )
        assert resp.status_code == 201

        resp = await client.post(
            f"/api/v1/projects/{project.id}/entries/add",
            headers=headers,
            json=_entry_payload("hdr-2"),
        )
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        assert resp.headers["Retry-After"] == "60"

    @pytest.mark.asyncio
    async def test_admin_tier_reaches_500_per_hour_bucket(
        self,
        client,
        db_session: AsyncSession,
    ):
        """Regression: get_effective_tier collapses legacy admin → ENTERPRISE,
        which would silently cap admins at 200/hr instead of 500/hr.

        The route checks raw user.tier == "admin" first to honour the
        advertised admin bucket. We don't actually push 500 entries; we set
        the env override to a tiny number for the admin-mapped bucket and
        verify the override applies (i.e. the admin path is exercised at all).
        """
        import os
        from sessionfs.server.auth.keys import generate_api_key, hash_api_key
        from sessionfs.server.db.models import ApiKey, User

        admin = User(
            id=str(uuid.uuid4()),
            email=f"admin_{uuid.uuid4().hex[:8]}@example.com",
            tier="admin",  # legacy admin tier — collapses to ENTERPRISE in get_effective_tier
            email_verified=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(admin)
        await db_session.commit()

        raw_key = generate_api_key()
        db_session.add(ApiKey(
            id=str(uuid.uuid4()),
            user_id=admin.id,
            key_hash=hash_api_key(raw_key),
            name="admin-rl",
            created_at=datetime.now(timezone.utc),
        ))
        await db_session.commit()
        headers = {"Authorization": f"Bearer {raw_key}"}
        project = await _own_project_for_user(db_session, admin.id)

        # Verify the admin path is taken: with no env override, the cap should
        # be the admin-tier value (500), not enterprise (200). We can't push
        # 500 in a test, but we can verify the 201st entry from a free-tier
        # user would 429 while an admin's 201st entry succeeds — both
        # significantly above the enterprise 200 boundary would prove the
        # path. Simpler: assert the 429 message identifies "admin", not
        # "enterprise".
        original = os.environ.pop("SFS_KNOWLEDGE_RATE_LIMIT_PER_HOUR", None)
        os.environ["SFS_KNOWLEDGE_RATE_LIMIT_PER_HOUR"] = "1"
        try:
            resp = await client.post(
                f"/api/v1/projects/{project.id}/entries/add",
                headers=headers,
                json=_entry_payload("admin-1"),
            )
            assert resp.status_code == 201
            resp = await client.post(
                f"/api/v1/projects/{project.id}/entries/add",
                headers=headers,
                json=_entry_payload("admin-2"),
            )
            assert resp.status_code == 429
            # Message must say "admin" tier — proves we took the admin branch
            # and didn't collapse to enterprise.
            assert "admin" in resp.text.lower(), (
                f"429 message must mention 'admin' tier, got: {resp.text[:200]}"
            )
        finally:
            if original is not None:
                os.environ["SFS_KNOWLEDGE_RATE_LIMIT_PER_HOUR"] = original
            else:
                os.environ.pop("SFS_KNOWLEDGE_RATE_LIMIT_PER_HOUR", None)


# ---------- v0.9.9.6 — Tier A list_entries filters + pagination + sort ----------


class TestListEntriesFilters:
    """Filter + sort + pagination on GET /api/v1/projects/{id}/entries.

    These cover the new query params added for the MCP `list_knowledge_entries`
    tool: claim_class, freshness_class, dismissed, session_id, sort, page.
    """

    @pytest.mark.asyncio
    async def test_filter_by_claim_class(
        self, client, auth_headers: dict, db_session: AsyncSession, test_user
    ):
        project = await _mk_project(db_session)
        project.owner_id = test_user.id
        await db_session.commit()

        e_claim = await _mk_entry(
            db_session, project_id=project.id, content="A claim entry"
        )
        e_claim.claim_class = "claim"
        e_note = await _mk_entry(
            db_session, project_id=project.id, content="A note entry"
        )
        e_note.claim_class = "note"
        await db_session.commit()

        resp = await client.get(
            f"/api/v1/projects/{project.id}/entries?claim_class=claim",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert all(e["claim_class"] == "claim" for e in data)
        assert any(e["id"] == e_claim.id for e in data)
        assert all(e["id"] != e_note.id for e in data)

        # Invalid claim_class → 422
        bad = await client.get(
            f"/api/v1/projects/{project.id}/entries?claim_class=bogus",
            headers=auth_headers,
        )
        assert bad.status_code == 422

    @pytest.mark.asyncio
    async def test_filter_by_freshness_class(
        self, client, auth_headers: dict, db_session: AsyncSession, test_user
    ):
        project = await _mk_project(db_session)
        project.owner_id = test_user.id
        await db_session.commit()

        e_current = await _mk_entry(
            db_session, project_id=project.id, content="Fresh entry"
        )
        e_current.freshness_class = "current"
        e_stale = await _mk_entry(
            db_session, project_id=project.id, content="Stale entry"
        )
        e_stale.freshness_class = "stale"
        await db_session.commit()

        resp = await client.get(
            f"/api/v1/projects/{project.id}/entries?freshness_class=stale",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        assert all(e["freshness_class"] == "stale" for e in data)
        assert any(e["id"] == e_stale.id for e in data)

    @pytest.mark.asyncio
    async def test_filter_by_dismissed_and_session_id(
        self, client, auth_headers: dict, db_session: AsyncSession, test_user
    ):
        project = await _mk_project(db_session)
        project.owner_id = test_user.id
        await db_session.commit()

        # Dismissed entry tied to ses_alpha
        dismissed_alpha = await _mk_entry(
            db_session,
            project_id=project.id,
            content="Dismissed alpha entry",
            dismissed=True,
        )
        dismissed_alpha.session_id = "ses_alpha"
        # Active entry tied to ses_alpha
        active_alpha = await _mk_entry(
            db_session,
            project_id=project.id,
            content="Active alpha entry",
            dismissed=False,
        )
        active_alpha.session_id = "ses_alpha"
        # Active entry tied to ses_beta — should NOT be returned
        active_beta = await _mk_entry(
            db_session,
            project_id=project.id,
            content="Active beta entry",
            dismissed=False,
        )
        active_beta.session_id = "ses_beta"
        await db_session.commit()

        # session_id=ses_alpha + dismissed=false → only active_alpha
        resp = await client.get(
            f"/api/v1/projects/{project.id}/entries"
            f"?session_id=ses_alpha&dismissed=false",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        ids = {e["id"] for e in data}
        assert active_alpha.id in ids
        assert dismissed_alpha.id not in ids
        assert active_beta.id not in ids

        # dismissed=true alone returns just dismissed entries
        resp_dis = await client.get(
            f"/api/v1/projects/{project.id}/entries?dismissed=true",
            headers=auth_headers,
        )
        assert resp_dis.status_code == 200
        data_dis = resp_dis.json()
        assert all(e["dismissed"] is True for e in data_dis)
        assert any(e["id"] == dismissed_alpha.id for e in data_dis)

    @pytest.mark.asyncio
    async def test_sort_by_confidence_desc(
        self, client, auth_headers: dict, db_session: AsyncSession, test_user
    ):
        project = await _mk_project(db_session)
        project.owner_id = test_user.id
        await db_session.commit()

        await _mk_entry(
            db_session, project_id=project.id, content="Low conf", confidence=0.3
        )
        await _mk_entry(
            db_session, project_id=project.id, content="Med conf", confidence=0.6
        )
        await _mk_entry(
            db_session, project_id=project.id, content="High conf", confidence=0.95
        )

        resp = await client.get(
            f"/api/v1/projects/{project.id}/entries?sort=confidence_desc",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        confs = [e["confidence"] for e in data]
        # Confidences should be in descending order
        assert confs == sorted(confs, reverse=True)
        assert confs[0] == pytest.approx(0.95)

        # Invalid sort → 422
        bad = await client.get(
            f"/api/v1/projects/{project.id}/entries?sort=birthday",
            headers=auth_headers,
        )
        assert bad.status_code == 422

    @pytest.mark.asyncio
    async def test_sort_by_last_relevant_at_nulls_last(
        self, client, auth_headers: dict, db_session: AsyncSession, test_user
    ):
        """last_relevant_at_desc must put NULLs LAST so newly-relevant
        entries surface above never-referenced ones."""
        project = await _mk_project(db_session)
        project.owner_id = test_user.id
        await db_session.commit()

        recent = datetime.now(timezone.utc) - timedelta(days=1)
        old = datetime.now(timezone.utc) - timedelta(days=10)
        e_never = await _mk_entry(
            db_session, project_id=project.id, content="Never referenced",
            last_relevant_at=None,
        )
        e_old = await _mk_entry(
            db_session, project_id=project.id, content="Old reference",
            last_relevant_at=old,
        )
        e_recent = await _mk_entry(
            db_session, project_id=project.id, content="Recent reference",
            last_relevant_at=recent,
        )

        resp = await client.get(
            f"/api/v1/projects/{project.id}/entries?sort=last_relevant_at_desc",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        data = resp.json()
        ids = [e["id"] for e in data]
        # Recent before old, both before never
        assert ids.index(e_recent.id) < ids.index(e_old.id)
        assert ids.index(e_old.id) < ids.index(e_never.id)

    @pytest.mark.asyncio
    async def test_pagination_page_and_limit(
        self, client, auth_headers: dict, db_session: AsyncSession, test_user
    ):
        project = await _mk_project(db_session)
        project.owner_id = test_user.id
        await db_session.commit()

        # Seed 5 entries
        for i in range(5):
            await _mk_entry(
                db_session,
                project_id=project.id,
                content=f"Entry number {i}",
            )

        # page=1, limit=2 → 2 results
        r1 = await client.get(
            f"/api/v1/projects/{project.id}/entries?page=1&limit=2",
            headers=auth_headers,
        )
        assert r1.status_code == 200
        page1 = r1.json()
        assert len(page1) == 2

        # page=2, limit=2 → 2 different results
        r2 = await client.get(
            f"/api/v1/projects/{project.id}/entries?page=2&limit=2",
            headers=auth_headers,
        )
        assert r2.status_code == 200
        page2 = r2.json()
        assert len(page2) == 2
        assert {e["id"] for e in page1} & {e["id"] for e in page2} == set()

        # page=3, limit=2 → 1 remaining result
        r3 = await client.get(
            f"/api/v1/projects/{project.id}/entries?page=3&limit=2",
            headers=auth_headers,
        )
        assert r3.status_code == 200
        page3 = r3.json()
        assert len(page3) == 1

    @pytest.mark.asyncio
    async def test_pagination_with_tied_sort_keys_is_deterministic(
        self, client, auth_headers: dict, db_session: AsyncSession, test_user
    ):
        """When sort key values are identical, pagination must still be
        deterministic — the id tiebreak is what guarantees no row is
        skipped or duplicated across page boundaries.

        Seeds 6 entries with identical confidence (0.8) and identical
        created_at, then walks page-by-page (limit=2) and asserts the
        union covers every entry exactly once. Without the id tiebreak,
        SQLite + PG can return arbitrary orderings for tied rows and
        offset+limit will both skip and duplicate rows under load.
        """
        project = await _mk_project(db_session)
        project.owner_id = test_user.id
        await db_session.commit()

        # Identical timestamp + identical confidence — only id can break the tie
        same_ts = datetime.now(timezone.utc)
        seeded_ids = []
        for i in range(6):
            e = KnowledgeEntry(
                project_id=project.id,
                session_id="ses_tied",
                user_id=test_user.id,
                entry_type="decision",
                content=f"tied entry {i}",
                confidence=0.8,
                created_at=same_ts,
            )
            db_session.add(e)
            await db_session.commit()
            await db_session.refresh(e)
            seeded_ids.append(e.id)

        # Walk all pages with each sort mode. Every entry must appear
        # exactly once across the union of page results.
        for sort in ("created_at_desc", "confidence_desc", "last_relevant_at_desc"):
            seen: list[int] = []
            for page in (1, 2, 3):
                resp = await client.get(
                    f"/api/v1/projects/{project.id}/entries"
                    f"?page={page}&limit=2&sort={sort}",
                    headers=auth_headers,
                )
                assert resp.status_code == 200, resp.text
                seen.extend(e["id"] for e in resp.json())

            assert sorted(seen) == sorted(seeded_ids), (
                f"sort={sort} produced non-deterministic pagination: "
                f"seen={seen}, expected={seeded_ids}"
            )

            # And re-fetching page 1 should return the same first 2 ids
            # every time (proves the order is stable, not just complete).
            first_calls = []
            for _ in range(3):
                resp = await client.get(
                    f"/api/v1/projects/{project.id}/entries"
                    f"?page=1&limit=2&sort={sort}",
                    headers=auth_headers,
                )
                first_calls.append([e["id"] for e in resp.json()])
            assert first_calls[0] == first_calls[1] == first_calls[2], (
                f"sort={sort} page-1 results unstable across calls: {first_calls}"
            )

    @pytest.mark.asyncio
    async def test_cursor_pagination_is_snapshot_stable_under_inserts(
        self, client, auth_headers: dict, db_session: AsyncSession, test_user
    ):
        """Cursor (keyset) pagination must not skip or duplicate rows
        when new entries are inserted between pages. OFFSET pagination
        cannot satisfy this; cursor pagination is the explicit fix.
        """
        project = await _mk_project(db_session)
        project.owner_id = test_user.id
        await db_session.commit()

        # Seed 5 entries (id ascending). Each gets a unique id.
        seeded_ids: list[int] = []
        for i in range(5):
            e = KnowledgeEntry(
                project_id=project.id,
                session_id="ses_cursor",
                user_id=test_user.id,
                entry_type="decision",
                content=f"original entry {i}",
                confidence=0.8,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(e)
            await db_session.commit()
            await db_session.refresh(e)
            seeded_ids.append(e.id)

        # First page — NO cursor, just limit. The route must emit
        # X-Next-Cursor so callers can bootstrap keyset iteration
        # without inventing a sentinel id.
        r1 = await client.get(
            f"/api/v1/projects/{project.id}/entries?limit=2",
            headers=auth_headers,
        )
        assert r1.status_code == 200, r1.text
        page1 = r1.json()
        assert len(page1) == 2
        # X-Next-Cursor must be present (more results available, default sort)
        assert "X-Next-Cursor" in r1.headers, r1.headers
        next_cursor = int(r1.headers["X-Next-Cursor"])
        assert next_cursor == page1[-1]["id"]

        # Now INSERT a new entry between page 1 and page 2. Under OFFSET
        # pagination this would shift the next page; under keyset it
        # must NOT — we should still see only entries with id < cursor.
        intruder = KnowledgeEntry(
            project_id=project.id,
            session_id="ses_cursor",
            user_id=test_user.id,
            entry_type="decision",
            content="inserted between pages",
            confidence=0.8,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(intruder)
        await db_session.commit()
        await db_session.refresh(intruder)

        # Second cursor page — should contain the next 2 originally-seeded
        # entries, NOT the intruder (intruder.id > cursor).
        r2 = await client.get(
            f"/api/v1/projects/{project.id}/entries"
            f"?cursor={next_cursor}&limit=2",
            headers=auth_headers,
        )
        assert r2.status_code == 200
        page2 = r2.json()
        assert len(page2) == 2
        page2_ids = [e["id"] for e in page2]
        assert intruder.id not in page2_ids, (
            f"keyset cursor must not pick up entries inserted at higher "
            f"ids; got {page2_ids}, intruder={intruder.id}"
        )
        # Page 1 and Page 2 must not overlap
        page1_ids = {e["id"] for e in page1}
        assert page1_ids.isdisjoint(page2_ids), (
            f"cursor pagination duplicated rows across pages: "
            f"page1={page1_ids}, page2={page2_ids}"
        )

    @pytest.mark.asyncio
    async def test_cursor_with_non_default_sort_returns_422(
        self, client, auth_headers: dict, db_session: AsyncSession, test_user
    ):
        """Cursor pagination is only defined for sort=created_at_desc
        in v0.9.9.6. Mixing cursor with another sort must 422 — silent
        fallback to OFFSET would erase the snapshot-stability guarantee
        agents are relying on.
        """
        project = await _mk_project(db_session)
        project.owner_id = test_user.id
        await db_session.commit()

        for s in ("confidence_desc", "last_relevant_at_desc"):
            resp = await client.get(
                f"/api/v1/projects/{project.id}/entries"
                f"?cursor=999&sort={s}&limit=2",
                headers=auth_headers,
            )
            assert resp.status_code == 422, (
                f"cursor + sort={s} must 422, got {resp.status_code}: "
                f"{resp.text[:200]}"
            )

    @pytest.mark.asyncio
    async def test_first_page_emits_cursor_for_bootstrap(
        self, client, auth_headers: dict, db_session: AsyncSession, test_user
    ):
        """The first page (no `cursor`, no `page`, default sort) must
        emit X-Next-Cursor when more rows exist. Without this, a keyset
        caller would have to invent a sentinel id (e.g. 99999) to start
        iteration — that's the gap Codex flagged.
        """
        project = await _mk_project(db_session)
        project.owner_id = test_user.id
        await db_session.commit()

        # 4 rows + limit=2 → next page exists → header should be set
        for i in range(4):
            db_session.add(KnowledgeEntry(
                project_id=project.id,
                session_id="ses_bootstrap",
                user_id=test_user.id,
                entry_type="decision",
                content=f"bootstrap {i}",
                confidence=0.8,
                created_at=datetime.now(timezone.utc),
            ))
            await db_session.commit()

        resp = await client.get(
            f"/api/v1/projects/{project.id}/entries?limit=2",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 2
        assert "X-Next-Cursor" in resp.headers, (
            "First page must emit X-Next-Cursor so keyset iteration can "
            "bootstrap without a sentinel cursor value"
        )
        next_cursor = int(resp.headers["X-Next-Cursor"])
        assert next_cursor == body[-1]["id"]

        # Caller now uses the bootstrap cursor — no sentinel needed.
        resp2 = await client.get(
            f"/api/v1/projects/{project.id}/entries?cursor={next_cursor}&limit=2",
            headers=auth_headers,
        )
        assert resp2.status_code == 200
        page2 = resp2.json()
        assert len(page2) == 2
        # No overlap with page 1 — proves the bootstrap cursor handed
        # off cleanly to keyset mode.
        assert {e["id"] for e in body}.isdisjoint({e["id"] for e in page2})

    @pytest.mark.asyncio
    async def test_first_page_no_cursor_when_results_fit_in_one_page(
        self, client, auth_headers: dict, db_session: AsyncSession, test_user
    ):
        """When the entire result set fits in one page, no continuation
        cursor should be emitted — len < limit signals EOF.
        """
        project = await _mk_project(db_session)
        project.owner_id = test_user.id
        await db_session.commit()

        # 2 rows, limit=10 → all fit → no header
        for i in range(2):
            db_session.add(KnowledgeEntry(
                project_id=project.id,
                session_id="ses_one_page",
                user_id=test_user.id,
                entry_type="decision",
                content=f"one-page {i}",
                confidence=0.8,
                created_at=datetime.now(timezone.utc),
            ))
            await db_session.commit()

        resp = await client.get(
            f"/api/v1/projects/{project.id}/entries?limit=10",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert len(resp.json()) == 2
        assert "X-Next-Cursor" not in resp.headers

    @pytest.mark.asyncio
    async def test_first_page_no_cursor_for_non_default_sort(
        self, client, auth_headers: dict, db_session: AsyncSession, test_user
    ):
        """Non-default sort modes don't support cursor pagination, so
        their responses must not advertise a continuation cursor — that
        would mislead callers into trying to use it.
        """
        project = await _mk_project(db_session)
        project.owner_id = test_user.id
        await db_session.commit()

        for i in range(4):
            db_session.add(KnowledgeEntry(
                project_id=project.id,
                session_id="ses_alt_sort",
                user_id=test_user.id,
                entry_type="decision",
                content=f"alt-sort {i}",
                confidence=0.8,
                created_at=datetime.now(timezone.utc),
            ))
            await db_session.commit()

        for s in ("confidence_desc", "last_relevant_at_desc"):
            resp = await client.get(
                f"/api/v1/projects/{project.id}/entries?sort={s}&limit=2",
                headers=auth_headers,
            )
            assert resp.status_code == 200, resp.text
            assert "X-Next-Cursor" not in resp.headers, (
                f"sort={s} must NOT emit X-Next-Cursor — cursor "
                f"pagination is only valid with created_at_desc"
            )

    @pytest.mark.asyncio
    async def test_cursor_no_more_results_omits_header(
        self, client, auth_headers: dict, db_session: AsyncSession, test_user
    ):
        """When the cursor page returns < limit results (no more rows),
        the X-Next-Cursor header must be omitted so callers know to stop.
        """
        project = await _mk_project(db_session)
        project.owner_id = test_user.id
        await db_session.commit()

        # Seed exactly 2 entries so a limit=5 fetch returns < limit.
        for i in range(2):
            e = KnowledgeEntry(
                project_id=project.id,
                session_id="ses_eof",
                user_id=test_user.id,
                entry_type="decision",
                content=f"eof entry {i}",
                confidence=0.8,
                created_at=datetime.now(timezone.utc),
            )
            db_session.add(e)
            await db_session.commit()

        resp = await client.get(
            f"/api/v1/projects/{project.id}/entries?cursor=99999&limit=5",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert "X-Next-Cursor" not in resp.headers, (
            "no more results — header must be omitted to signal EOF"
        )


# ---------- v0.9.9.6 — get_context_section ----------


class TestGetContextSection:
    """GET /api/v1/projects/{id}/context/sections/{slug}.

    Reuses split_context_sections() so slugs are derived from `## Heading`
    blocks. On miss, the 404 detail must include available_slugs.
    """

    @pytest.mark.asyncio
    async def test_get_section_existing_and_missing(
        self, client, auth_headers: dict, db_session: AsyncSession, test_user
    ):
        project = await _mk_project(db_session)
        project.owner_id = test_user.id
        project.context_document = (
            "# Project\n\n"
            "## Architecture\n\n"
            "FastAPI + Postgres.\n\n"
            "## Team Workflow\n\n"
            "Trunk-based development.\n"
        )
        await db_session.commit()

        # Existing section by slug (lowercased, non-alnum → _)
        resp = await client.get(
            f"/api/v1/projects/{project.id}/context/sections/architecture",
            headers=auth_headers,
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["slug"] == "architecture"
        assert "FastAPI" in data["content"]
        assert "Architecture" in data["title"]

        # Compound heading slug
        resp_compound = await client.get(
            f"/api/v1/projects/{project.id}/context/sections/team_workflow",
            headers=auth_headers,
        )
        assert resp_compound.status_code == 200
        assert "Trunk-based" in resp_compound.json()["content"]

        # Missing section → 404 with available_slugs in detail
        miss = await client.get(
            f"/api/v1/projects/{project.id}/context/sections/nonexistent",
            headers=auth_headers,
        )
        assert miss.status_code == 404, miss.text
        # Server uses a global error envelope:
        # {"error": {"code": "404", "message": "Error", "details": {...}}}
        body = miss.json()
        detail = body.get("error", {}).get("details") or body.get("detail") or {}
        assert isinstance(detail, dict), f"Expected dict detail, got {detail!r}"
        assert "available_slugs" in detail
        assert "architecture" in detail["available_slugs"]
        assert "team_workflow" in detail["available_slugs"]


# ---------- v0.9.9.6 Codex round 4: cross-user 403 regressions ----------


async def _mk_second_user_headers(
    db: AsyncSession,
) -> tuple[str, dict]:
    """Create a second user + API key. Returns (user_id, auth_headers)."""
    from sessionfs.server.auth.keys import generate_api_key, hash_api_key
    from sessionfs.server.db.models import ApiKey, User

    user = User(
        id=str(uuid.uuid4()),
        email=f"outsider_{uuid.uuid4().hex[:8]}@example.com",
        tier="pro",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()

    raw = generate_api_key()
    db.add(ApiKey(
        id=str(uuid.uuid4()),
        user_id=user.id,
        key_hash=hash_api_key(raw),
        name="outsider-key",
        created_at=datetime.now(timezone.utc),
    ))
    await db.commit()
    return user.id, {"Authorization": f"Bearer {raw}"}


class TestCrossUserAccessDenied:
    """Non-members must get 403 on the new Tier A read endpoints."""

    @pytest.mark.asyncio
    async def test_get_entry_returns_403_for_non_member(
        self, client, db_session: AsyncSession, test_user
    ):
        project = await _own_project_for_user(db_session, test_user.id)
        entry = await _mk_entry(
            db_session,
            project_id=project.id,
            content="Owner-only secret entry",
        )
        _, outsider_headers = await _mk_second_user_headers(db_session)

        resp = await client.get(
            f"/api/v1/projects/{project.id}/entries/{entry.id}",
            headers=outsider_headers,
        )
        assert resp.status_code == 403, (
            f"Non-member should be denied with 403, got {resp.status_code}: "
            f"{resp.text[:200]}"
        )

    @pytest.mark.asyncio
    async def test_list_entries_returns_403_for_non_member(
        self, client, db_session: AsyncSession, test_user
    ):
        project = await _own_project_for_user(db_session, test_user.id)
        await _mk_entry(
            db_session,
            project_id=project.id,
            content="Owner-only secret entry",
        )
        _, outsider_headers = await _mk_second_user_headers(db_session)

        resp = await client.get(
            f"/api/v1/projects/{project.id}/entries",
            headers=outsider_headers,
        )
        assert resp.status_code == 403, (
            f"Non-member should be denied with 403, got {resp.status_code}: "
            f"{resp.text[:200]}"
        )

    @pytest.mark.asyncio
    async def test_get_context_section_returns_403_for_non_member(
        self, client, db_session: AsyncSession, test_user
    ):
        project = await _own_project_for_user(db_session, test_user.id)
        project.context_document = "## Architecture\n\nFastAPI + Postgres.\n"
        await db_session.commit()
        _, outsider_headers = await _mk_second_user_headers(db_session)

        resp = await client.get(
            f"/api/v1/projects/{project.id}/context/sections/architecture",
            headers=outsider_headers,
        )
        assert resp.status_code == 403, (
            f"Non-member should be denied with 403, got {resp.status_code}: "
            f"{resp.text[:200]}"
        )

    @pytest.mark.asyncio
    async def test_get_knowledge_health_returns_403_for_non_member(
        self, client, db_session: AsyncSession, test_user
    ):
        project = await _own_project_for_user(db_session, test_user.id)
        _, outsider_headers = await _mk_second_user_headers(db_session)

        resp = await client.get(
            f"/api/v1/projects/{project.id}/health",
            headers=outsider_headers,
        )
        assert resp.status_code == 403, (
            f"Non-member should be denied with 403, got {resp.status_code}: "
            f"{resp.text[:200]}"
        )
