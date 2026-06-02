"""Integration tests for admin API endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import ApiKey, Session, User


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    """Create an admin user."""
    user = User(
        id=str(uuid.uuid4()),
        email="admin@sessionfs.dev",
        display_name="Admin",
        tier="admin",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def admin_api_key(db_session: AsyncSession, admin_user: User) -> tuple[str, ApiKey]:
    """Create an API key for the admin user."""
    raw_key = generate_api_key()
    api_key = ApiKey(
        id=str(uuid.uuid4()),
        user_id=admin_user.id,
        key_hash=hash_api_key(raw_key),
        name="admin-key",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(api_key)
    await db_session.commit()
    await db_session.refresh(api_key)
    return raw_key, api_key


@pytest.fixture
def admin_headers(admin_api_key: tuple[str, ApiKey]) -> dict[str, str]:
    """Authorization headers for the admin user."""
    return {"Authorization": f"Bearer {admin_api_key[0]}"}


@pytest.fixture
async def extra_user(db_session: AsyncSession) -> User:
    """Create an additional non-admin user for testing."""
    user = User(
        id=str(uuid.uuid4()),
        email="regular@example.com",
        display_name="Regular User",
        tier="free",
        email_verified=False,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def extra_session(db_session: AsyncSession, extra_user: User) -> Session:
    """Create a session owned by extra_user."""
    import hashlib

    session_id = f"ses_{uuid.uuid4().hex[:16]}"
    now = datetime.now(timezone.utc)
    session = Session(
        id=session_id,
        user_id=extra_user.id,
        title="Extra session",
        tags="[]",
        source_tool="codex",
        blob_key=f"sessions/{extra_user.id}/{session_id}/session.tar.gz",
        blob_size_bytes=1024,
        etag=hashlib.sha256(b"test").hexdigest(),
        created_at=now,
        updated_at=now,
        uploaded_at=now,
    )
    db_session.add(session)
    await db_session.commit()
    await db_session.refresh(session)
    return session


# ---------------------------------------------------------------------------
# Non-admin gets 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_admin_gets_403(client: AsyncClient, auth_headers: dict):
    """Regular users cannot access admin endpoints."""
    resp = await client.get("/api/v1/admin/users", headers=auth_headers)
    assert resp.status_code == 403
    assert "Admin access required" in resp.json()["error"]["message"]


@pytest.mark.asyncio
async def test_non_admin_stats_403(client: AsyncClient, auth_headers: dict):
    resp = await client.get("/api/v1/admin/stats", headers=auth_headers)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# List users
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_users(client: AsyncClient, admin_headers: dict, extra_user: User):
    resp = await client.get("/api/v1/admin/users", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 2  # admin + extra + test_user
    assert isinstance(data["users"], list)
    emails = [u["email"] for u in data["users"]]
    assert "regular@example.com" in emails


@pytest.mark.asyncio
async def test_list_users_search(client: AsyncClient, admin_headers: dict, extra_user: User):
    resp = await client.get("/api/v1/admin/users?search=regular", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] == 1
    assert data["users"][0]["email"] == "regular@example.com"


@pytest.mark.asyncio
async def test_list_users_tier_filter(client: AsyncClient, admin_headers: dict, extra_user: User):
    resp = await client.get("/api/v1/admin/users?tier_filter=admin", headers=admin_headers)
    assert resp.status_code == 200
    for u in resp.json()["users"]:
        assert u["tier"] == "admin"


# ---------------------------------------------------------------------------
# Get user detail
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_user_detail(client: AsyncClient, admin_headers: dict, extra_user: User):
    resp = await client.get(f"/api/v1/admin/users/{extra_user.id}", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["email"] == "regular@example.com"
    assert data["session_count"] == 0
    assert "storage_used_bytes" in data
    assert "api_key_count" in data


@pytest.mark.asyncio
async def test_get_user_detail_not_found(client: AsyncClient, admin_headers: dict):
    resp = await client.get("/api/v1/admin/users/nonexistent", headers=admin_headers)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Change tier
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_change_user_tier(client: AsyncClient, admin_headers: dict, extra_user: User):
    resp = await client.put(
        f"/api/v1/admin/users/{extra_user.id}/tier",
        json={"tier": "pro"},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["old_tier"] == "free"
    assert data["new_tier"] == "pro"

    # Verify change persisted
    detail = await client.get(f"/api/v1/admin/users/{extra_user.id}", headers=admin_headers)
    assert detail.json()["tier"] == "pro"


@pytest.mark.asyncio
async def test_change_tier_invalid(client: AsyncClient, admin_headers: dict, extra_user: User):
    resp = await client.put(
        f"/api/v1/admin/users/{extra_user.id}/tier",
        json={"tier": "invalid"},
        headers=admin_headers,
    )
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Force verify
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_force_verify(client: AsyncClient, admin_headers: dict, extra_user: User):
    resp = await client.put(
        f"/api/v1/admin/users/{extra_user.id}/verify",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    assert resp.json()["email_verified"] is True


# ---------------------------------------------------------------------------
# Mint API key on behalf of user (tk_4afbae8ed3a442e9)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mint_api_key_on_behalf_happy_path(
    client: AsyncClient,
    admin_headers: dict,
    extra_user: User,
    admin_user: User,
    db_session: AsyncSession,
):
    """Admin mints a fresh user-kind key on behalf of another user.
    Raw key returned exactly once; response includes user_id +
    user_email so the operator knows which account got the key.
    """
    from sessionfs.server.db.models import AdminAction

    resp = await client.post(
        f"/api/v1/admin/users/{extra_user.id}/api-keys",
        json={"name": "pianolinux-recovery-2026-06"},
        headers=admin_headers,
    )
    assert resp.status_code == 201, resp.text[:200]
    body = resp.json()
    assert body["user_id"] == extra_user.id
    assert body["user_email"] == extra_user.email
    assert body["name"] == "pianolinux-recovery-2026-06"
    assert body["raw_key"].startswith("sk_sfs_")
    assert "key_id" in body

    # ApiKey row exists, key_kind='user', scopes='["*"]' (defaults).
    key = (
        await db_session.execute(
            select(ApiKey).where(ApiKey.id == body["key_id"])
        )
    ).scalar_one_or_none()
    assert key is not None
    assert key.user_id == extra_user.id
    assert key.key_kind == "user"
    assert key.scopes == '["*"]'
    assert key.is_active is True

    # Audit row written with correct action + details.
    audit = (
        await db_session.execute(
            select(AdminAction).where(
                AdminAction.action == "mint_api_key_on_behalf",
                AdminAction.target_id == extra_user.id,
            )
        )
    ).scalar_one_or_none()
    assert audit is not None
    assert audit.admin_id == admin_user.id
    assert audit.target_type == "user"


@pytest.mark.asyncio
async def test_mint_api_key_on_behalf_default_name(
    client: AsyncClient, admin_headers: dict, extra_user: User
):
    """When body omits `name`, defaults to 'admin-minted'."""
    resp = await client.post(
        f"/api/v1/admin/users/{extra_user.id}/api-keys",
        json={},
        headers=admin_headers,
    )
    assert resp.status_code == 201
    assert resp.json()["name"] == "admin-minted"


@pytest.mark.asyncio
async def test_mint_api_key_on_behalf_minted_key_authenticates(
    client: AsyncClient, admin_headers: dict, extra_user: User
):
    """The raw key returned must actually authenticate a subsequent
    request as the target user — proves the hash matches what
    `hash_api_key()` expects and the key_kind='user' default makes
    it admissible by `get_current_user`.
    """
    mint_resp = await client.post(
        f"/api/v1/admin/users/{extra_user.id}/api-keys",
        json={"name": "auth-test"},
        headers=admin_headers,
    )
    assert mint_resp.status_code == 201
    raw_key = mint_resp.json()["raw_key"]

    # Use the minted key to call /auth/me — should return extra_user's
    # profile, not the admin's.
    me_resp = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert me_resp.status_code == 200
    assert me_resp.json()["email"] == extra_user.email


@pytest.mark.asyncio
async def test_mint_api_key_on_behalf_404_unknown_user(
    client: AsyncClient, admin_headers: dict
):
    fake_id = str(uuid.uuid4())
    resp = await client.post(
        f"/api/v1/admin/users/{fake_id}/api-keys",
        json={"name": "ghost"},
        headers=admin_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mint_api_key_on_behalf_403_for_non_admin(
    client: AsyncClient, auth_headers: dict, extra_user: User
):
    resp = await client.post(
        f"/api/v1/admin/users/{extra_user.id}/api-keys",
        json={"name": "not-allowed"},
        headers=auth_headers,
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_mint_api_key_on_behalf_403_for_inactive_user(
    client: AsyncClient,
    admin_headers: dict,
    extra_user: User,
    db_session: AsyncSession,
):
    """Disabled accounts must not be a backdoor — admin cannot mint a
    key for an `is_active=false` user."""
    extra_user.is_active = False
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/admin/users/{extra_user.id}/api-keys",
        json={"name": "should-fail"},
        headers=admin_headers,
    )
    assert resp.status_code == 403
    body = resp.json()
    msg = body.get("detail") or body.get("error", {}).get("message", "")
    assert "inactive" in str(msg).lower()


# ---------------------------------------------------------------------------
# Delete user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_user(client: AsyncClient, admin_headers: dict, extra_user: User):
    resp = await client.delete(
        f"/api/v1/admin/users/{extra_user.id}",
        headers=admin_headers,
    )
    assert resp.status_code == 204

    # User should now show as inactive
    detail = await client.get(f"/api/v1/admin/users/{extra_user.id}", headers=admin_headers)
    assert detail.json()["is_active"] is False


@pytest.mark.asyncio
async def test_delete_user_expires_legacy_mixed_case_handoffs(
    client: AsyncClient,
    admin_headers: dict,
    db_session: AsyncSession,
):
    """Codex perf-2 round 2 finding: delete_user must expire pending
    handoffs whose recipient_email is mixed-case OR has surrounding
    whitespace OR pre-dates migration 032 (NULL normalized column).
    Pre-fix: filter was raw `recipient_email == user.email`, which
    silently missed legacy rows.
    """
    from sessionfs.server.db.models import Handoff

    # Recipient with whitespace + mixed case in the raw column. Migration
    # 032 has NOT been "run" against this row in the test (we set
    # recipient_email_normalized to NULL explicitly to simulate the
    # legacy state).
    recipient = User(
        id=str(uuid.uuid4()),
        email="legacy@example.com",
        tier="free",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(recipient)
    sender = User(
        id=str(uuid.uuid4()),
        email="sender@example.com",
        tier="pro",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(sender)
    await db_session.commit()

    # Need a session for the handoff FK
    import hashlib

    session_row = Session(
        id=f"ses_{uuid.uuid4().hex[:16]}",
        user_id=sender.id,
        title="t",
        tags="[]",
        source_tool="claude-code",
        blob_key="x",
        blob_size_bytes=0,
        etag=hashlib.sha256(b"x").hexdigest(),
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        uploaded_at=datetime.now(timezone.utc),
    )
    db_session.add(session_row)
    await db_session.commit()

    legacy_handoff = Handoff(
        id=f"hnd_{uuid.uuid4().hex[:8]}",
        session_id=session_row.id,
        sender_id=sender.id,
        # Mixed case + leading whitespace + NULL normalized — simulate
        # a pre-migration row.
        recipient_email="  Legacy@Example.COM  ",
        recipient_email_normalized=None,
        message="hi",
        status="pending",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc),
    )
    db_session.add(legacy_handoff)
    await db_session.commit()

    # Now delete the recipient via admin endpoint
    resp = await client.delete(
        f"/api/v1/admin/users/{recipient.id}",
        headers=admin_headers,
    )
    assert resp.status_code == 204

    # Verify handoff was expired despite the mixed-case + whitespace
    from sqlalchemy import select as _select
    refreshed = (await db_session.execute(
        _select(Handoff).where(Handoff.id == legacy_handoff.id)
    )).scalar_one()
    # SQLAlchemy may have a cached entity; force a re-read
    await db_session.refresh(refreshed)
    assert refreshed.status == "expired", (
        f"legacy mixed-case handoff still pending after user delete: "
        f"status={refreshed.status!r}"
    )


@pytest.mark.asyncio
async def test_delete_self_rejected(client: AsyncClient, admin_headers: dict, admin_user: User):
    resp = await client.delete(
        f"/api/v1/admin/users/{admin_user.id}",
        headers=admin_headers,
    )
    assert resp.status_code == 400
    assert "own account" in resp.json()["error"]["message"]


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_sessions(
    client: AsyncClient, admin_headers: dict, extra_session: Session,
):
    resp = await client.get("/api/v1/admin/sessions", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    ids = [s["id"] for s in data["sessions"]]
    assert extra_session.id in ids


@pytest.mark.asyncio
async def test_delete_session(
    client: AsyncClient, admin_headers: dict, extra_session: Session,
):
    resp = await client.delete(
        f"/api/v1/admin/sessions/{extra_session.id}",
        headers=admin_headers,
    )
    assert resp.status_code == 204

    # Session should no longer appear in listing
    listing = await client.get("/api/v1/admin/sessions", headers=admin_headers)
    ids = [s["id"] for s in listing.json()["sessions"]]
    assert extra_session.id not in ids


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stats(client: AsyncClient, admin_headers: dict, extra_session: Session):
    resp = await client.get("/api/v1/admin/stats", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert "users" in data
    assert "sessions" in data
    assert "handoffs" in data
    assert "storage" in data
    assert data["users"]["total"] >= 2
    assert data["sessions"]["total"] >= 1


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit_log_records_actions(
    client: AsyncClient, admin_headers: dict, extra_user: User,
):
    # Perform an action that gets logged
    await client.put(
        f"/api/v1/admin/users/{extra_user.id}/tier",
        json={"tier": "team"},
        headers=admin_headers,
    )

    resp = await client.get("/api/v1/admin/audit-log", headers=admin_headers)
    assert resp.status_code == 200
    data = resp.json()
    assert data["total"] >= 1
    actions = data["actions"]
    assert any(a["action"] == "tier_change" and a["target_id"] == extra_user.id for a in actions)


# ── tk_dd3ba7082ef0432e — R5 admin repair: restore project from compilation ──


@pytest.mark.asyncio
async def test_restore_from_compilation_dry_run_reports_no_writes(
    client: AsyncClient, admin_headers: dict, db_session: AsyncSession,
    admin_user: User,
):
    """Dry-run returns the counts without modifying anything."""
    import json
    from sessionfs.server.db.models import (
        ContextCompilation, KnowledgeEntry, Project,
    )

    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="restore-test",
        git_remote_normalized=f"acme/r-{uuid.uuid4().hex[:6]}",
        context_document="# Current (different from snapshot)\n",
        owner_id=admin_user.id,
    )
    db_session.add(project)
    await db_session.commit()

    entry = KnowledgeEntry(
        project_id=project.id, session_id="ses_x", user_id=admin_user.id,
        entry_type="decision", content="x" * 60, confidence=0.9,
        claim_class="claim", compiled_at=None,  # will stay null after dry-run
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)

    compilation = ContextCompilation(
        project_id=project.id,
        user_id=admin_user.id,
        entries_compiled=1,
        context_before="",
        context_after="# Snapshot\n\nSome compiled content from earlier.\n",
        source_manifest=json.dumps({
            "key-decisions": [{"kb_entry_id": entry.id}],
        }),
    )
    db_session.add(compilation)
    await db_session.commit()
    await db_session.refresh(compilation)

    resp = await client.post(
        f"/api/v1/admin/projects/{project.id}/restore-from-compilation",
        json={"compilation_id": compilation.id, "dry_run": True},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    assert body["compilation_id"] == compilation.id
    assert body["context_words_restored"] > 0
    assert body["entries_compiled_at_restored"] == 1

    # Verify no writes happened:
    await db_session.refresh(project)
    assert project.context_document == "# Current (different from snapshot)\n"
    await db_session.refresh(entry)
    assert entry.compiled_at is None


@pytest.mark.asyncio
async def test_restore_from_compilation_apply_writes_context_and_compiled_at(
    client: AsyncClient, admin_headers: dict, db_session: AsyncSession,
    admin_user: User,
):
    """The headline path. After apply, project.context_document matches
    compilation.context_after AND every entry in source_manifest has
    compiled_at set to compilation.compiled_at."""
    import json
    from sessionfs.server.db.models import (
        ContextCompilation, KnowledgeEntry, Project,
    )

    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="restore-test",
        git_remote_normalized=f"acme/r-{uuid.uuid4().hex[:6]}",
        context_document="",  # post-rebuild-crash state
        owner_id=admin_user.id,
    )
    db_session.add(project)
    await db_session.commit()

    entries = [
        KnowledgeEntry(
            project_id=project.id, session_id=f"ses_{i}", user_id=admin_user.id,
            entry_type="decision", content=f"Entry {i} with at least fifty characters total here.",
            confidence=0.9, claim_class="claim", compiled_at=None,
        )
        for i in range(3)
    ]
    db_session.add_all(entries)
    await db_session.commit()
    for e in entries:
        await db_session.refresh(e)
    entry_ids = [e.id for e in entries]

    snapshot_text = "# Restored Snapshot\n\n## Key Decisions\n- Entry 0\n- Entry 1\n- Entry 2\n"
    compilation = ContextCompilation(
        project_id=project.id,
        user_id=admin_user.id,
        entries_compiled=3,
        context_before="",
        context_after=snapshot_text,
        source_manifest=json.dumps({
            "key-decisions": [
                {"kb_entry_id": eid} for eid in entry_ids
            ],
        }),
    )
    db_session.add(compilation)
    await db_session.commit()
    await db_session.refresh(compilation)

    resp = await client.post(
        f"/api/v1/admin/projects/{project.id}/restore-from-compilation",
        json={"compilation_id": compilation.id, "dry_run": False},
        headers=admin_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is False
    assert body["entries_compiled_at_restored"] == 3

    # Verify via a fresh session — the test's db_session shares an
    # identity map with prior reads and SQLAlchemy's async lifecycle
    # interacts badly with the test client's ASGI session boundary.
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)

    async with factory() as verify_session:
        project_fresh = (await verify_session.execute(
            select(Project).where(Project.id == project.id)
        )).scalar_one()
        assert project_fresh.context_document == snapshot_text

        for eid in entry_ids:
            e = (await verify_session.execute(
                select(KnowledgeEntry).where(KnowledgeEntry.id == eid)
            )).scalar_one()
            assert e.compiled_at is not None
            assert e.compiled_at == compilation.compiled_at


@pytest.mark.asyncio
async def test_restore_from_compilation_404_for_unknown_compilation(
    client: AsyncClient, admin_headers: dict, db_session: AsyncSession,
    admin_user: User,
):
    from sessionfs.server.db.models import Project
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="no-comp", git_remote_normalized=f"acme/n-{uuid.uuid4().hex[:6]}",
        context_document="", owner_id=admin_user.id,
    )
    db_session.add(project)
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/admin/projects/{project.id}/restore-from-compilation",
        json={"compilation_id": 99999, "dry_run": True},
        headers=admin_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_restore_from_compilation_rejects_cross_project_compilation(
    client: AsyncClient, admin_headers: dict, db_session: AsyncSession,
    admin_user: User,
):
    """A compilation row belonging to project A cannot be used to restore
    project B. Defense-in-depth against admin typo or path-tampering."""
    from sessionfs.server.db.models import ContextCompilation, Project

    project_a = Project(
        id=f"proj_a_{uuid.uuid4().hex[:12]}",
        name="proj-a", git_remote_normalized=f"acme/a-{uuid.uuid4().hex[:6]}",
        context_document="A doc", owner_id=admin_user.id,
    )
    project_b = Project(
        id=f"proj_b_{uuid.uuid4().hex[:12]}",
        name="proj-b", git_remote_normalized=f"acme/b-{uuid.uuid4().hex[:6]}",
        context_document="B doc", owner_id=admin_user.id,
    )
    db_session.add_all([project_a, project_b])
    await db_session.commit()

    compilation_a = ContextCompilation(
        project_id=project_a.id, user_id=admin_user.id, entries_compiled=0,
        context_before="", context_after="A snapshot", source_manifest="{}",
    )
    db_session.add(compilation_a)
    await db_session.commit()
    await db_session.refresh(compilation_a)

    # Try to restore project_b from compilation_a — must 404.
    resp = await client.post(
        f"/api/v1/admin/projects/{project_b.id}/restore-from-compilation",
        json={"compilation_id": compilation_a.id, "dry_run": True},
        headers=admin_headers,
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_restore_from_compilation_requires_admin(
    client: AsyncClient, db_session: AsyncSession,
):
    """Non-admin users get 403/401."""
    # Use a non-admin user
    from sessionfs.server.auth.keys import generate_api_key, hash_api_key
    user = User(
        id=str(uuid.uuid4()),
        email="reg@example.com",
        display_name="Reg",
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
        name="reg-key",
        created_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()

    resp = await client.post(
        "/api/v1/admin/projects/proj_anything/restore-from-compilation",
        json={"compilation_id": 1, "dry_run": True},
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_restore_from_compilation_422_on_empty_context_after(
    client: AsyncClient, admin_headers: dict, db_session: AsyncSession,
    admin_user: User,
):
    """A compilation row whose context_after is empty is not a valid
    restore source (nothing to write). Return 422 rather than wiping
    the project's existing document."""
    from sessionfs.server.db.models import ContextCompilation, Project

    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="empty-comp", git_remote_normalized=f"acme/e-{uuid.uuid4().hex[:6]}",
        context_document="# Existing", owner_id=admin_user.id,
    )
    db_session.add(project)
    await db_session.commit()

    compilation = ContextCompilation(
        project_id=project.id, user_id=admin_user.id, entries_compiled=0,
        context_before="", context_after="",  # empty
        source_manifest="{}",
    )
    db_session.add(compilation)
    await db_session.commit()
    await db_session.refresh(compilation)

    resp = await client.post(
        f"/api/v1/admin/projects/{project.id}/restore-from-compilation",
        json={"compilation_id": compilation.id, "dry_run": False},
        headers=admin_headers,
    )
    assert resp.status_code == 422

    # Confirm we did NOT overwrite the existing doc.
    await db_session.refresh(project)
    assert project.context_document == "# Existing"


@pytest.mark.asyncio
async def test_restore_from_compilation_validates_compilation_id(
    client: AsyncClient, admin_headers: dict,
):
    resp = await client.post(
        "/api/v1/admin/projects/proj_anything/restore-from-compilation",
        json={"compilation_id": "not-an-int", "dry_run": True},
        headers=admin_headers,
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_restore_from_compilation_rejects_boolean_compilation_id(
    client: AsyncClient, admin_headers: dict,
):
    """Codex R1 MEDIUM regression on tk_879dbd5a5a034d0e — Python's bool
    is an int subclass, so a malformed admin body like
    {"compilation_id": true} would otherwise coerce to compilation_id=1
    and target the wrong row. Explicit rejection at the boundary."""
    for bad in (True, False):
        resp = await client.post(
            "/api/v1/admin/projects/proj_anything/restore-from-compilation",
            json={"compilation_id": bad, "dry_run": True},
            headers=admin_headers,
        )
        assert resp.status_code == 422, (
            f"compilation_id={bad} (bool) must be rejected with 422; "
            f"got {resp.status_code}: {resp.text}"
        )


@pytest.mark.asyncio
async def test_restore_from_compilation_rejects_non_bool_dry_run(
    client: AsyncClient, admin_headers: dict,
):
    """Non-bool dry_run values must be rejected so a body like
    {"dry_run": "false"} doesn't surprise via Python truthiness."""
    for bad in ("false", "true", 0, 1):
        resp = await client.post(
            "/api/v1/admin/projects/proj_anything/restore-from-compilation",
            json={"compilation_id": 1, "dry_run": bad},
            headers=admin_headers,
        )
        assert resp.status_code == 422, (
            f"dry_run={bad!r} ({type(bad).__name__}) must be rejected; "
            f"got {resp.status_code}: {resp.text}"
        )
