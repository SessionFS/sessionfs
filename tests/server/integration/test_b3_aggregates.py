"""Regression tests for v0.9.9.10 B3 — collapsed sync/admin aggregates.

Pre-fix:
- sync_status ran 5 separate COUNT/SUM queries.
- admin /orgs ran one COUNT per org in a Python loop (N+1).

Post-fix:
- sync_status runs 2 queries: 1 multi-aggregate over Session, 1 GROUP BY
  over SyncWatchlist by status.
- admin /orgs batch-loads member counts via WHERE org_id IN (...) GROUP BY.

These tests pin the BEHAVIOURAL semantics of the new code paths. They
don't measure query counts (that's a perf concern, not a correctness
one) — they assert the response shape and numbers stay identical
across status mixes and edge cases.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession


# ── sync_status GROUP BY watchlist semantics ──


@pytest.mark.asyncio
async def test_sync_status_watchlist_group_by_handles_mixed_statuses(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
):
    """Watchlist contract that the GROUP BY rewrite must preserve:
      - `watched_sessions` is the TOTAL row count regardless of status
      - `queued` counts only status='queued'
      - `failed` counts only status='failed'
    A status like 'in_progress' or 'pending' should NOT inflate
    queued/failed, but MUST contribute to watched.
    """
    from sessionfs.server.db.models import SyncWatchlist

    statuses_to_seed = {
        "queued": 3,
        "failed": 2,
        "pending": 4,
        "in_progress": 1,
        "completed": 5,
    }
    for status, count in statuses_to_seed.items():
        for _ in range(count):
            db_session.add(SyncWatchlist(
                user_id=test_user.id,
                session_id=f"ses_{uuid.uuid4().hex[:16]}",
                status=status,
                created_at=datetime.now(timezone.utc),
            ))
    await db_session.commit()

    resp = await client.get("/api/v1/sync/status", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()

    expected_total = sum(statuses_to_seed.values())
    assert data["watched_sessions"] == expected_total, (
        f"watched_sessions must be total row count, not just queued+failed"
    )
    assert data["queued"] == statuses_to_seed["queued"]
    assert data["failed"] == statuses_to_seed["failed"]


@pytest.mark.asyncio
async def test_sync_status_empty_watchlist_returns_zeros(
    client: AsyncClient, auth_headers: dict
):
    """No watchlist rows → GROUP BY returns nothing → watched/queued/failed
    must all be 0 (not None, not crash)."""
    resp = await client.get("/api/v1/sync/status", headers=auth_headers)
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["watched_sessions"] == 0
    assert data["queued"] == 0
    assert data["failed"] == 0


@pytest.mark.asyncio
async def test_sync_status_only_unknown_status_still_counts_to_watched(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user
):
    """A future status enum value the route doesn't know about must
    still increment `watched_sessions` (since the contract is "total
    rows regardless of status"). Without this, adding a new status
    value to the watchlist could silently drop it from the metric."""
    from sessionfs.server.db.models import SyncWatchlist

    for _ in range(3):
        db_session.add(SyncWatchlist(
            user_id=test_user.id,
            session_id=f"ses_{uuid.uuid4().hex[:16]}",
            status="future_unknown_state",
            created_at=datetime.now(timezone.utc),
        ))
    await db_session.commit()

    resp = await client.get("/api/v1/sync/status", headers=auth_headers)
    data = resp.json()
    assert data["watched_sessions"] == 3
    assert data["queued"] == 0
    assert data["failed"] == 0


# ── admin /orgs batched member counts ──


def _seed_org(
    db: AsyncSession,
    *,
    name: str,
    slug: str,
    member_user_ids: list[str],
) -> str:
    """Helper: insert an Organization + N OrgMember rows. Returns org_id."""
    from sessionfs.server.db.models import Organization, OrgMember

    org_id = f"org_{uuid.uuid4().hex[:12]}"
    db.add(Organization(
        id=org_id,
        name=name,
        slug=slug,
        tier="team",
        stripe_customer_id=None,
        stripe_subscription_id=None,
        storage_limit_bytes=0,
        storage_used_bytes=0,
        seats_limit=10,
        settings="{}",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    ))
    for uid in member_user_ids:
        db.add(OrgMember(
            org_id=org_id,
            user_id=uid,
            role="member",
        ))
    return org_id


@pytest.mark.asyncio
async def test_admin_orgs_empty_page_does_not_crash(
    client: AsyncClient, db_session: AsyncSession
):
    """The `if orgs:` guard prevents WHERE org_id IN () — which
    PostgreSQL and SQLite handle differently. Empty page must return
    200 with empty list, not 500."""
    from sessionfs.server.auth.keys import generate_api_key, hash_api_key
    from sessionfs.server.db.models import ApiKey, User

    admin = User(
        id=str(uuid.uuid4()),
        email=f"admin_{uuid.uuid4().hex[:8]}@example.com",
        tier="admin",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(admin)
    raw = generate_api_key()
    db_session.add(ApiKey(
        id=str(uuid.uuid4()),
        user_id=admin.id,
        key_hash=hash_api_key(raw),
        name="admin",
        created_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    admin_headers = {"Authorization": f"Bearer {raw}"}

    # No orgs seeded — page should be empty without a SQL error.
    resp = await client.get("/api/v1/admin/orgs", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["orgs"] == []
    assert body["total"] == 0


@pytest.mark.asyncio
async def test_admin_orgs_batched_member_counts_correct_per_org(
    client: AsyncClient, db_session: AsyncSession
):
    """Multi-org page: each org's member_count must be correct. The
    batched GROUP BY query must produce one row per non-zero org and
    the route must zero-fill orgs without members."""
    from sessionfs.server.auth.keys import generate_api_key, hash_api_key
    from sessionfs.server.db.models import ApiKey, User

    admin = User(
        id=str(uuid.uuid4()),
        email=f"admin_{uuid.uuid4().hex[:8]}@example.com",
        tier="admin",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(admin)
    raw = generate_api_key()
    db_session.add(ApiKey(
        id=str(uuid.uuid4()),
        user_id=admin.id,
        key_hash=hash_api_key(raw),
        name="admin",
        created_at=datetime.now(timezone.utc),
    ))

    # Seed 3 orgs with different membership sizes (including one empty).
    member_pool = []
    for _ in range(7):
        u = User(
            id=str(uuid.uuid4()),
            email=f"u_{uuid.uuid4().hex[:8]}@example.com",
            tier="free",
            email_verified=True,
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(u)
        member_pool.append(u.id)
    await db_session.commit()

    org_alpha = _seed_org(
        db_session,
        name="Alpha",
        slug=f"alpha-{uuid.uuid4().hex[:6]}",
        member_user_ids=member_pool[:4],
    )
    org_beta = _seed_org(
        db_session,
        name="Beta",
        slug=f"beta-{uuid.uuid4().hex[:6]}",
        member_user_ids=member_pool[4:7],
    )
    org_empty = _seed_org(
        db_session,
        name="Empty",
        slug=f"empty-{uuid.uuid4().hex[:6]}",
        member_user_ids=[],
    )
    await db_session.commit()

    admin_headers = {"Authorization": f"Bearer {raw}"}
    resp = await client.get("/api/v1/admin/orgs", headers=admin_headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    by_id = {o["id"]: o for o in body["orgs"]}
    assert by_id[org_alpha]["member_count"] == 4
    assert by_id[org_beta]["member_count"] == 3
    assert by_id[org_empty]["member_count"] == 0, (
        "An org with zero members must still appear with member_count=0; "
        "the GROUP BY query returns no row for it but the route must "
        "zero-fill via member_counts.get(org.id, 0)."
    )


# ── B4: KB content search still functions on both backends ──


@pytest.mark.asyncio
async def test_kb_content_search_returns_match_on_substring(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    test_user,
):
    """The pg_trgm GIN index in migration 034 must not change behaviour:
    search still returns every entry whose content contains the
    substring (case-insensitive). Locks the contract so a future
    optimization can't silently change semantics (e.g. trigram
    similarity, which would reject short queries).
    """
    from sessionfs.server.db.models import KnowledgeEntry, Project

    pid = f"proj_{uuid.uuid4().hex[:8]}"
    proj = Project(
        id=pid,
        name="Search test project",
        git_remote_normalized=f"github.com/example/search-{pid}",
        owner_id=test_user.id,
        context_document="",
    )
    db_session.add(proj)
    await db_session.commit()

    contents = [
        "The migration backfill must run cross-DB",
        "DLP scan rejects oversize members",
        "Migration 034 adds the trigram index",
        "Unrelated note about something else",
    ]
    for content in contents:
        db_session.add(KnowledgeEntry(
            project_id=pid,
            session_id="ses_search_test",
            user_id=test_user.id,
            entry_type="discovery",
            content=content,
            confidence=0.5,
            created_at=datetime.now(timezone.utc),
        ))
    await db_session.commit()

    # Substring search for "migration" must match 2 entries
    resp = await client.get(
        f"/api/v1/projects/{pid}/entries?search=migration",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    matched_ids = {e["id"] for e in resp.json()}
    matched_contents = {
        e["content"] for e in resp.json()
    }
    assert any("migration backfill" in c.lower() for c in matched_contents)
    assert any("Migration 034" in c for c in matched_contents)
    # Sanity: the unrelated note is NOT in the result set.
    assert not any("Unrelated note" in c for c in matched_contents)


@pytest.mark.asyncio
async def test_kb_content_search_rejects_under_3_chars(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    test_user,
):
    """Codex review-finding 207 (v0.9.9.10 LOW 2): the pg_trgm index
    only accelerates queries of 3+ consecutive non-wildcard chars.
    Shorter queries must 422 — otherwise the optimization claim is
    false and the route silently falls back to a sequential scan."""
    from sessionfs.server.db.models import Project

    pid = f"proj_{uuid.uuid4().hex[:8]}"
    db_session.add(Project(
        id=pid,
        name="min-len test",
        git_remote_normalized=f"github.com/example/minlen-{pid}",
        owner_id=test_user.id,
        context_document="",
    ))
    await db_session.commit()

    for short in ("", " ", "a", "ab", "  ab  "):
        resp = await client.get(
            f"/api/v1/projects/{pid}/entries?search={short}",
            headers=auth_headers,
        )
        assert resp.status_code == 422, (
            f"search={short!r} must reject with 422, got {resp.status_code}: "
            f"{resp.text[:200]}"
        )

    # 3+ chars passes the gate (no entries → empty list, not an error).
    resp = await client.get(
        f"/api/v1/projects/{pid}/entries?search=abc",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


@pytest.mark.asyncio
async def test_kb_content_search_case_insensitive(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    test_user,
):
    """ILIKE is case-insensitive; pg_trgm preserves that semantic. The
    test covers the bug class where a future swap to LIKE (or to a
    case-sensitive trigram operator) would silently break dashboard
    search."""
    from sessionfs.server.db.models import KnowledgeEntry, Project

    pid = f"proj_{uuid.uuid4().hex[:8]}"
    db_session.add(Project(
        id=pid,
        name="case test",
        git_remote_normalized=f"github.com/example/case-{pid}",
        owner_id=test_user.id,
        context_document="",
    ))
    await db_session.commit()

    db_session.add(KnowledgeEntry(
        project_id=pid,
        session_id="ses_case",
        user_id=test_user.id,
        entry_type="discovery",
        content="DLP scan rejects OVERSIZE members",
        confidence=0.5,
        created_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    # Lowercase query against uppercase content must still match.
    resp = await client.get(
        f"/api/v1/projects/{pid}/entries?search=oversize",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()) == 1
    assert "OVERSIZE" in resp.json()[0]["content"]
