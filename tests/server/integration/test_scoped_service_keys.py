"""v0.10.10 — scoped service API keys (tk_2e030a85253143df).

Codex R1 + R2 reviews demanded specific properties; these tests are
the regression bar that proves they hold.

Critical properties under test:
1. **Deny-by-default for service keys on undecorated routes** (R2 HIGH):
   service key with no scope → calls a route still on plain
   `get_current_user` → 403 service_key_not_allowed BEFORE any side
   effect (no DB row created).
2. **Scope allow/deny** (R1): service key with handoffs:write succeeds
   on require_scope('handoffs:write'); service key with tickets:read
   fails on the same route with 403 insufficient_scope.
3. **Expiry** (R1): key past expires_at returns 401 api_key_expired.
4. **Revocation** (R1): revoked key returns 401 api_key_revoked.
5. **Cross-org enforcement** (R1 MEDIUM 1): service key bound to org_a
   → request hitting a project in org_b → 403 cross_org_denied.
6. **Secret never in list responses** (R1 MEDIUM 3): GET /service-keys
   omits the raw key field.
7. **Raw key never logged** (R1 MEDIUM 3): create + rotate paths emit
   logs that do NOT contain the raw secret.
8. **Back-compat for legacy user keys** (R1 finding C): existing user
   keys (key_kind='user', scopes='["*"]') pass every route they could
   before, including plain get_current_user and require_scope(...).
9. **last_used_at + last_used_ip update on each auth** (R1).
10. **Org admin authority** (R1 MEDIUM 2): non-admin org member gets
    403 on POST /orgs/{id}/service-keys.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentPersona,
    ApiKey,
    KnowledgeEntry,
    OrgMember,
    Organization,
    Project,
    Ticket,
    User,
)


# ── helpers ──


async def _make_user_with_key(
    db_session: AsyncSession, email: str, tier: str = "team"
) -> tuple[User, str]:
    """Create a user + an ordinary user key (scopes='*'). Returns (user, raw_key)."""
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        display_name=email.split("@")[0],
        tier=tier,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.flush()
    raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"user-key-{email}",
            is_active=True,
            key_kind="user",
            scopes=json.dumps(["*"]),
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    return user, raw


async def _make_org_with_admin(
    db_session: AsyncSession, slug: str | None = None
) -> tuple[Organization, User, str]:
    """Create org + admin user with key. Returns (org, admin_user, raw_key)."""
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name=slug or f"Org-{uuid.uuid4().hex[:6]}",
        slug=slug or f"o-{uuid.uuid4().hex[:6]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(org)
    await db_session.flush()
    admin_user, raw = await _make_user_with_key(
        db_session, f"admin-{uuid.uuid4().hex[:6]}@x.com"
    )
    db_session.add(
        OrgMember(
            org_id=org.id,
            user_id=admin_user.id,
            role="admin",
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    return org, admin_user, raw


async def _create_service_key(
    db_session: AsyncSession,
    org_id: str,
    minter: User,
    scopes: list[str],
    expires_at: datetime | None = None,
    revoked_at: datetime | None = None,
    project_ids: list[str] | None = None,
) -> tuple[ApiKey, str]:
    """Directly insert a service key (skipping the route layer)."""
    raw = generate_api_key()
    row = ApiKey(
        id=str(uuid.uuid4()),
        user_id=minter.id,
        key_hash=hash_api_key(raw),
        name=f"svc-{uuid.uuid4().hex[:6]}",
        is_active=revoked_at is None,
        key_kind="service",
        org_id=org_id,
        scopes=json.dumps(scopes),
        expires_at=expires_at,
        revoked_at=revoked_at,
        revoke_reason="test" if revoked_at else None,
        created_by_user_id=minter.id,
        service_key_name=f"svc-{uuid.uuid4().hex[:6]}",
        project_ids=json.dumps(project_ids) if project_ids else None,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row, raw


async def _make_org_project(
    db_session: AsyncSession,
    org: Organization,
    owner: User,
) -> "Project":
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name=f"svc-ticket-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"github.com/sessionfs/{uuid.uuid4().hex[:8]}",
        owner_id=owner.id,
        org_id=org.id,
        context_document="",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _make_ticket(
    db_session: AsyncSession,
    project: "Project",
    owner: User,
    *,
    status: str = "open",
    assigned_to: str | None = None,
) -> "Ticket":
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title=f"svc ticket {uuid.uuid4().hex[:6]}",
        description="service key route coverage",
        priority="medium",
        assigned_to=assigned_to,
        created_by_user_id=owner.id,
        status=status,
        context_refs="[]",
        file_refs="[]",
        related_sessions="[]",
        acceptance_criteria="[]",
        changed_files="[]",
        knowledge_entry_ids="[]",
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(ticket)
    await db_session.commit()
    await db_session.refresh(ticket)
    return ticket


async def _make_knowledge_entry(
    db_session: AsyncSession,
    project: "Project",
    owner: User,
    *,
    content: str | None = None,
    claim_class: str = "note",
    confidence: float = 0.9,
    freshness_class: str = "current",
) -> "KnowledgeEntry":
    entry = KnowledgeEntry(
        project_id=project.id,
        session_id=f"ses_{uuid.uuid4().hex[:12]}",
        user_id=owner.id,
        entry_type="discovery",
        content=content
        or (
            f"src/service/{uuid.uuid4().hex[:8]}.py owns scoped-key "
            "knowledge route regression coverage with durable detail."
        ),
        confidence=confidence,
        claim_class=claim_class,
        freshness_class=freshness_class,
        dismissed=False,
    )
    db_session.add(entry)
    await db_session.commit()
    await db_session.refresh(entry)
    return entry


async def _make_persona(
    db_session: AsyncSession,
    project_id: str,
    owner: User,
    name: str = "atlas",
) -> None:
    from sessionfs.server.db.models import AgentPersona

    db_session.add(
        AgentPersona(
            id=f"per_{uuid.uuid4().hex[:12]}",
            project_id=project_id,
            name=name,
            role="Backend",
            created_by=owner.id,
            is_active=True,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()


def _hdrs(raw_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {raw_key}"}


async def _get_user_key(db_session: AsyncSession, user: User) -> tuple[ApiKey, str]:
    """Fetch the first user-kind ApiKey for `user` (returns row + raw
    placeholder — for tests that need to drive the API as that user
    when the caller doesn't already have the raw key cached)."""
    # We can't recover the raw key from the hash, so we mint a fresh
    # one for the user. Caller is responsible for the cleanup if any.
    raw = generate_api_key()
    row = ApiKey(
        id=str(uuid.uuid4()),
        user_id=user.id,
        key_hash=hash_api_key(raw),
        name="aux-test-key",
        is_active=True,
        key_kind="user",
        scopes='["*"]',
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row, raw


def _structured_error(body: dict) -> dict:
    """Navigate the {error: {code, details, message}} envelope the
    server's exception handler wraps structured-dict HTTPException
    detail in. Returns the inner `details` dict (which is what we
    raised), or {} on shape mismatch."""
    if not isinstance(body, dict):
        return {}
    err = body.get("error") or body.get("detail") or body
    if isinstance(err, dict):
        inner = err.get("details") or err.get("detail")
        if isinstance(inner, dict):
            return inner
        return err
    return {}


# ── Property 1: deny-by-default for service keys on undecorated routes ──


@pytest.mark.asyncio
async def test_service_key_denied_on_undecorated_route(
    client: AsyncClient, db_session: AsyncSession
):
    """Codex R2 HIGH proof — a service key calling a route that still
    uses plain `get_current_user` must be rejected with 403
    service_key_not_allowed BEFORE any route side effect.

    Phase 2 converted POST /handoffs to require_scope, so the canary is
    now any route still on get_current_user. GET /api/v1/sessions (list
    sessions) is on plain get_current_user and is non-destructive —
    perfect for proving the deny-by-default still holds for routes that
    have not yet been opted in to scoped auth."""
    org, admin, _admin_key = await _make_org_with_admin(db_session)
    _row, svc_key = await _create_service_key(
        db_session, org.id, admin, scopes=["handoffs:write"]
    )

    # Service key with handoffs:write tries to hit GET /api/v1/sessions
    # — that route is still on plain get_current_user, so the
    # deny-by-default for service keys fires.
    resp = await client.get("/api/v1/sessions", headers=_hdrs(svc_key))
    assert resp.status_code == 403, resp.text
    structured = _structured_error(resp.json())
    assert structured.get("error") == "service_key_not_allowed", resp.json()


# ── Property 2: scope allow/deny via require_scope on /service-keys route ──
# ── Property 3+4: expiry + revocation rejection ──


@pytest.mark.asyncio
async def test_expired_service_key_returns_api_key_expired(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _ = await _make_org_with_admin(db_session)
    _row, raw = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["handoffs:write"],
        expires_at=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    resp = await client.get("/api/v1/auth/me", headers=_hdrs(raw))
    assert resp.status_code == 401
    structured = _structured_error(resp.json())
    assert structured.get("error") == "api_key_expired", resp.json()


@pytest.mark.asyncio
async def test_revoked_service_key_returns_api_key_revoked(
    client: AsyncClient, db_session: AsyncSession
):
    """Codex R3 MEDIUM 1 — revoked key must surface structured error
    code `api_key_revoked`, NOT generic 'Invalid API key'. Real revoke
    sets is_active=False AND revoked_at=now; the auth dependency must
    check revoked_at BEFORE is_active so the structured code fires."""
    org, admin, _ = await _make_org_with_admin(db_session)
    _row, raw = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["handoffs:write"],
        revoked_at=datetime.now(timezone.utc),
    )
    resp = await client.get("/api/v1/auth/me", headers=_hdrs(raw))
    assert resp.status_code == 401
    structured = _structured_error(resp.json())
    assert structured.get("error") == "api_key_revoked", resp.json()


# ── Property 6: secret never in list responses ──


@pytest.mark.asyncio
async def test_service_key_create_returns_raw_once_then_list_omits(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, admin_raw = await _make_org_with_admin(db_session)
    create = await client.post(
        f"/api/v1/orgs/{org.id}/service-keys",
        headers=_hdrs(admin_raw),
        json={
            "name": "Bedrock agent",
            "scopes": ["handoffs:write", "tickets:read"],
            "expires_in_days": 30,
        },
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body.get("key", "").startswith("sk_sfs_"), "raw key must be on create response"
    assert body["scopes"] == ["handoffs:write", "tickets:read"]
    assert body["org_id"] == org.id
    # Codex R3 MEDIUM 2 — key_prefix must match the actual raw key's
    # first 12 chars, NOT be synthesized from the UUID. Critical for
    # incident response and rotation UX.
    assert body["key_prefix"] == body["key"][:12], (
        f"key_prefix must equal raw_key[:12]; got prefix={body['key_prefix']!r} "
        f"vs raw[:12]={body['key'][:12]!r}"
    )

    listing = await client.get(
        f"/api/v1/orgs/{org.id}/service-keys", headers=_hdrs(admin_raw)
    )
    assert listing.status_code == 200
    rows = listing.json()
    matched = [r for r in rows if r["id"] == body["id"]]
    assert matched, "created key must appear in list"
    for r in rows:
        assert "key" not in r, "list response must NOT include raw key"
        assert r["key_prefix"].startswith("sk_sfs_")
    # The same prefix returned on create must be returned on list.
    assert matched[0]["key_prefix"] == body["key_prefix"]


# ── Property 7: raw key never logged ──


@pytest.mark.asyncio
async def test_raw_key_not_in_logs_on_create_or_rotate(
    client: AsyncClient, db_session: AsyncSession, caplog
):
    org, admin, admin_raw = await _make_org_with_admin(db_session)
    with caplog.at_level(logging.DEBUG):
        create = await client.post(
            f"/api/v1/orgs/{org.id}/service-keys",
            headers=_hdrs(admin_raw),
            json={"name": "test", "scopes": ["handoffs:write"]},
        )
    assert create.status_code == 201
    raw = create.json()["key"]
    full_log = "\n".join(rec.getMessage() for rec in caplog.records)
    assert raw not in full_log, (
        "Raw API key MUST NOT appear in log output (Codex R1 MEDIUM 3)"
    )

    key_id = create.json()["id"]
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        rotate = await client.post(
            f"/api/v1/orgs/{org.id}/service-keys/{key_id}/rotate",
            headers=_hdrs(admin_raw),
        )
    assert rotate.status_code == 200
    new_raw = rotate.json()["key"]
    full_log = "\n".join(rec.getMessage() for rec in caplog.records)
    assert new_raw not in full_log


# ── Property 8: back-compat for legacy user keys ──


@pytest.mark.asyncio
async def test_legacy_user_key_passes_get_current_user_routes(
    client: AsyncClient, auth_headers: dict
):
    """The default auth_headers fixture mints a user key. It must still
    work against routes that use get_current_user (i.e. all existing
    routes) without any behavior change."""
    resp = await client.get("/api/v1/auth/me", headers=auth_headers)
    assert resp.status_code == 200


# ── Property 9: last_used updates on each auth ──


@pytest.mark.asyncio
async def test_last_used_at_and_ip_update_on_auth(
    client: AsyncClient, db_session: AsyncSession
):
    user, raw = await _make_user_with_key(
        db_session, f"poll-{uuid.uuid4().hex[:6]}@x.com"
    )
    # First call — last_used_at should now be set.
    r1 = await client.get(
        "/api/v1/auth/me", headers=_hdrs(raw), params={"x-forwarded-for": "10.1.2.3"}
    )
    assert r1.status_code == 200

    from sqlalchemy import select as _sel
    row = (
        await db_session.execute(_sel(ApiKey).where(ApiKey.user_id == user.id))
    ).scalar_one()
    await db_session.refresh(row)
    assert row.last_used_at is not None, "last_used_at must populate on auth"
    # IP is best-effort — at minimum a non-empty string when test client
    # uses 127.0.0.1.
    assert row.last_used_ip is not None


# ── Property 10: org admin authority on /service-keys ──


@pytest.mark.asyncio
async def test_non_admin_member_cannot_mint_service_key(
    client: AsyncClient, db_session: AsyncSession
):
    org, _admin, _admin_raw = await _make_org_with_admin(db_session)
    # Add a plain member to the same org
    member, member_raw = await _make_user_with_key(
        db_session, f"mem-{uuid.uuid4().hex[:6]}@x.com"
    )
    db_session.add(
        OrgMember(
            org_id=org.id,
            user_id=member.id,
            role="member",
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/service-keys",
        headers=_hdrs(member_raw),
        json={"name": "sneaky", "scopes": ["handoffs:write"]},
    )
    assert resp.status_code == 403


# ── Validation tests ──


@pytest.mark.asyncio
async def test_service_key_with_wildcard_scope_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    """Codex R1 finding C — '*' is reserved for user/admin keys."""
    org, _admin, admin_raw = await _make_org_with_admin(db_session)
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/service-keys",
        headers=_hdrs(admin_raw),
        json={"name": "no", "scopes": ["*"]},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_service_key_with_unknown_scope_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    org, _admin, admin_raw = await _make_org_with_admin(db_session)
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/service-keys",
        headers=_hdrs(admin_raw),
        json={"name": "no", "scopes": ["foo:bar"]},
    )
    assert resp.status_code == 422


# ── Personal key surface ──


@pytest.mark.asyncio
async def test_personal_key_create_and_list(
    client: AsyncClient, auth_headers: dict
):
    create = await client.post(
        "/api/v1/auth/me/api-keys",
        headers=auth_headers,
        json={"name": "my CI key", "expires_in_days": 90},
    )
    assert create.status_code == 201, create.text
    body = create.json()
    assert body["key"].startswith("sk_sfs_")
    assert body["name"] == "my CI key"
    assert body["expires_at"] is not None

    listing = await client.get("/api/v1/auth/me/api-keys", headers=auth_headers)
    assert listing.status_code == 200
    rows = listing.json()
    assert any(r["id"] == body["id"] for r in rows)
    for r in rows:
        assert "key" not in r


# ── Codex R3 LOW 1: positive require_scope integration test ──


@pytest.mark.asyncio
async def test_require_scope_admits_service_key_with_matching_scope(
    db_engine, db_session: AsyncSession
):
    """Mount a test-only route protected by require_scope('handoffs:write')
    and prove:
      - service key with that scope → 200 + AuthContext returned with
        actor_type='service_key' and service_key_id populated
      - service key WITHOUT that scope → 403 insufficient_scope
      - user key (legacy scopes='[\"*\"]') → 200 (back-compat wildcard)
    This is the positive proof Codex R3 LOW 1 demands."""
    from fastapi import Depends, FastAPI
    from httpx import ASGITransport, AsyncClient
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from sessionfs.server.auth.dependencies import AuthContext, require_scope
    from sessionfs.server.db.engine import get_db

    test_app = FastAPI()

    @test_app.get("/_test/scoped")
    async def scoped_endpoint(
        ctx: AuthContext = Depends(require_scope("handoffs:write")),
    ):
        return {
            "actor_type": ctx.actor_type,
            "key_kind": ctx.key_kind,
            "service_key_id": ctx.service_key_id,
            "service_key_name": ctx.service_key_name,
            "scopes": ctx.scopes,
        }

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    test_app.dependency_overrides[get_db] = override_get_db

    # Set up three keys: service-with-scope, service-without-scope,
    # legacy user-wildcard.
    org, admin, admin_user_raw = await _make_org_with_admin(db_session)
    _row, svc_with = await _create_service_key(
        db_session, org.id, admin, scopes=["handoffs:write"]
    )
    _row2, svc_without = await _create_service_key(
        db_session, org.id, admin, scopes=["tickets:read"]
    )

    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        # Matching scope → 200, AuthContext shows service-key provenance
        r1 = await c.get("/_test/scoped", headers=_hdrs(svc_with))
        assert r1.status_code == 200, r1.text
        body = r1.json()
        assert body["actor_type"] == "service_key"
        assert body["key_kind"] == "service"
        assert body["service_key_id"] is not None
        assert "handoffs:write" in body["scopes"]

        # Wrong scope → 403 insufficient_scope
        r2 = await c.get("/_test/scoped", headers=_hdrs(svc_without))
        assert r2.status_code == 403
        structured = _structured_error(r2.json())
        assert structured.get("error") == "insufficient_scope"
        assert "handoffs:write" in structured.get("required", [])

        # Legacy user key (scopes='["*"]') → 200 (back-compat wildcard)
        r3 = await c.get("/_test/scoped", headers=_hdrs(admin_user_raw))
        assert r3.status_code == 200
        body3 = r3.json()
        assert body3["actor_type"] == "user"
        assert body3["key_kind"] == "user"
        assert body3["service_key_id"] is None


# ── Codex R5 HIGH 1+2 — service-key cross-org/project boundary regressions ──


@pytest.mark.asyncio
async def test_service_key_cross_org_create_handoff_denied(
    client: AsyncClient, db_session: AsyncSession, sample_sfs_tar: bytes
):
    """R5 HIGH 2 — service key minted for org_A cannot create a handoff
    whose source session belongs to org_B, even if the backing user has
    a session in org_B. Tests assert_service_key_handoff_boundary fires
    before any Handoff row write."""
    from sessionfs.server.db.models import Project, Handoff
    from sqlalchemy import func, select as _sel

    # Set up two orgs. admin_a backs the service key. The session is
    # pushed by admin_b (different user) so its git_remote points at a
    # project owned by admin_a in org_b — that's the cross-org leak
    # surface: backing user has project access (as owner) but the
    # service key is bound to org_a.
    org_a, admin_a, _ = await _make_org_with_admin(db_session, slug="a")
    org_b, admin_b, admin_b_raw = await _make_org_with_admin(db_session, slug="b")

    # Service key minted under org_a, owned by admin_a.
    _row, svc_key = await _create_service_key(
        db_session, org_a.id, admin_a, scopes=["handoffs:write"]
    )

    # Push a session as admin_a (so create_handoff's Session.user_id ==
    # user.id check passes) and link the session's git_remote to a
    # project in org_b. The service key (org_a) then tries to hand off
    # that session — the source session resolves to a project in org_b
    # which triggers cross_org_denied.
    _, admin_a_raw = await _get_user_key(db_session, admin_a)
    sid = f"ses_{uuid.uuid4().hex[:12]}"
    push = await client.put(
        f"/api/v1/sessions/{sid}/sync",
        headers=_hdrs(admin_a_raw),  # session pushed by admin_a (key owner)
        files={"file": ("s.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert push.status_code == 201
    from sessionfs.server.db.models import Session as _Sess
    sess = (
        await db_session.execute(_sel(_Sess).where(_Sess.id == sid))
    ).scalar_one()
    project_b = Project(
        id=f"proj_b_{uuid.uuid4().hex[:8]}",
        name="B project",
        git_remote_normalized=sess.git_remote_normalized or "github.com/b/x",
        owner_id=admin_b.id,
        org_id=org_b.id,
        context_document="",
        created_at=datetime.now(timezone.utc),
    )
    if not sess.git_remote_normalized:
        sess.git_remote_normalized = project_b.git_remote_normalized
    db_session.add(project_b)
    await db_session.commit()

    # The svc_key (org_a) tries to create a handoff for the session
    # whose project belongs to org_b. Must be denied with cross_org_denied
    # BEFORE any Handoff row is created.
    before_count = (
        await db_session.execute(_sel(func.count(Handoff.id)))
    ).scalar()
    resp = await client.post(
        "/api/v1/handoffs",
        headers=_hdrs(svc_key),
        json={"session_id": sid, "recipient_email": "r@x.com"},
    )
    assert resp.status_code == 403, resp.text
    structured = _structured_error(resp.json())
    assert structured.get("error") == "cross_org_denied", resp.json()
    after_count = (
        await db_session.execute(_sel(func.count(Handoff.id)))
    ).scalar()
    assert after_count == before_count, (
        "no Handoff row may be inserted on cross-org-denied service-key request"
    )


@pytest.mark.asyncio
async def test_service_key_cross_org_agent_run_create_denied(
    client: AsyncClient, db_session: AsyncSession
):
    """R5 HIGH 1 — service key minted for org_A cannot create an
    AgentRun in a project belonging to org_B even if the backing user
    has org_B access."""
    from sessionfs.server.db.models import AgentPersona, AgentRun, Project
    from sqlalchemy import func, select as _sel

    org_a, admin_a, _ = await _make_org_with_admin(db_session, slug="aa")
    org_b, _admin_b, _ = await _make_org_with_admin(db_session, slug="bb")
    # admin_a owns a project in org_b (legitimate backing-user access
    # via project ownership). Service key bound to org_a must still be
    # denied — the boundary check uses key.org_id, not user access.
    project_b = Project(
        id=f"proj_bb_{uuid.uuid4().hex[:8]}",
        name="org-b proj",
        git_remote_normalized=f"github.com/b/{uuid.uuid4().hex[:6]}",
        owner_id=admin_a.id,
        org_id=org_b.id,
        context_document="",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(project_b)
    db_session.add(
        AgentPersona(
            id=f"per_{uuid.uuid4().hex[:8]}",
            project_id=project_b.id,
            name="atlas",
            role="Backend",
            created_by=admin_a.id,
        )
    )
    await db_session.commit()

    _row, svc_key = await _create_service_key(
        db_session, org_a.id, admin_a, scopes=["agent_runs:write"]
    )

    before = (await db_session.execute(_sel(func.count(AgentRun.id)))).scalar()
    resp = await client.post(
        f"/api/v1/projects/{project_b.id}/agent-runs",
        headers=_hdrs(svc_key),
        json={
            "persona_name": "atlas",
            "tool": "claude-code",
            "trigger_source": "manual",
        },
    )
    assert resp.status_code == 403, resp.text
    structured = _structured_error(resp.json())
    assert structured.get("error") == "cross_org_denied", resp.json()
    after = (await db_session.execute(_sel(func.count(AgentRun.id)))).scalar()
    assert after == before, "no AgentRun row may be inserted on cross-org-denied"


@pytest.mark.asyncio
async def test_service_key_event_records_service_key_id(
    client: AsyncClient, db_session: AsyncSession, sample_sfs_tar: bytes
):
    """R5 MEDIUM — HandoffEvent must now record service_key_id (not
    just service_key_name) for durable incident-response traceability."""
    from sessionfs.server.db.models import HandoffEvent, Project
    from sqlalchemy import select as _sel

    org, admin, admin_raw = await _make_org_with_admin(db_session)
    svc_row, svc_key = await _create_service_key(
        db_session, org.id, admin, scopes=["handoffs:write"]
    )

    # Push session AS admin so service_key (owned by admin) passes
    # Session.user_id == user.id, AND set up matching project in same org
    # so boundary passes.
    sid = f"ses_{uuid.uuid4().hex[:12]}"
    push = await client.put(
        f"/api/v1/sessions/{sid}/sync",
        headers=_hdrs(admin_raw),
        files={"file": ("s.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert push.status_code == 201
    from sessionfs.server.db.models import Session as _Sess
    sess = (
        await db_session.execute(_sel(_Sess).where(_Sess.id == sid))
    ).scalar_one()
    db_session.add(
        Project(
            id=f"proj_{uuid.uuid4().hex[:8]}",
            name="boundary-ok",
            git_remote_normalized=sess.git_remote_normalized or "github.com/x/y",
            owner_id=admin.id,
            org_id=org.id,
            context_document="",
            created_at=datetime.now(timezone.utc),
        )
    )
    if not sess.git_remote_normalized:
        # Use last project's git_remote_normalized.
        sess.git_remote_normalized = "github.com/x/y"
    await db_session.commit()

    resp = await client.post(
        "/api/v1/handoffs",
        headers=_hdrs(svc_key),
        json={"session_id": sid, "recipient_email": "r@x.com"},
    )
    assert resp.status_code == 201, resp.text
    handoff_id = resp.json()["id"]

    # The created event must carry actor_type='service_key' AND
    # service_key_id matching the row we minted (R5 MEDIUM — names
    # are not durable identifiers; id is).
    created_event = (
        await db_session.execute(
            _sel(HandoffEvent).where(
                HandoffEvent.handoff_id == handoff_id,
                HandoffEvent.event_type == "created",
            )
        )
    ).scalar_one()
    assert created_event.actor_type == "service_key"
    assert created_event.service_key_id == svc_row.id, (
        f"event.service_key_id must equal the minted key's id; "
        f"got {created_event.service_key_id!r} vs minted {svc_row.id!r}"
    )
    assert created_event.service_key_name


# ── Codex R6 HIGH — sessions.project_id is authoritative, not git_remote ──


@pytest.mark.asyncio
async def test_service_key_boundary_uses_session_project_id_anchor(
    client: AsyncClient, db_session: AsyncSession, sample_sfs_tar: bytes
):
    """R6 HIGH — the boundary helper must resolve the source project
    via `sessions.project_id` (authoritative since migration 036), NOT
    via git_remote_normalized lookup that ignores the explicit anchor.

    The schema UNIQUE constraint on projects.git_remote_normalized
    prevents two projects from sharing a remote, so the original Codex
    attack vector (shared remote + 'prefer org match' bypass) cannot
    occur in production data. This test proves the resolution path
    uses project_id even so — same outcome, but via the correct anchor.

    Scenario: service key in org_a, session anchored to project in
    org_b via session.project_id. Service key denied with cross_org_denied
    (proves project_id is consulted before any fallback)."""
    from sessionfs.server.db.models import Project, Handoff, Session as _Sess
    from sqlalchemy import func, select as _sel

    org_a, admin_a, _ = await _make_org_with_admin(db_session, slug="oa")
    org_b, admin_b, _ = await _make_org_with_admin(db_session, slug="ob")

    _row, svc_key = await _create_service_key(
        db_session, org_a.id, admin_a, scopes=["handoffs:write"]
    )

    # Push session as admin_a (so Session.user_id check passes).
    _, admin_a_raw = await _get_user_key(db_session, admin_a)
    sid = f"ses_{uuid.uuid4().hex[:12]}"
    push = await client.put(
        f"/api/v1/sessions/{sid}/sync",
        headers=_hdrs(admin_a_raw),
        files={"file": ("s.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert push.status_code == 201
    sess = (
        await db_session.execute(_sel(_Sess).where(_Sess.id == sid))
    ).scalar_one()

    # Create a project in org_b with a UNIQUE git_remote (different
    # from session's), then explicitly anchor session.project_id to it.
    # This is the case the R6 fix specifically guards: helper must use
    # project_id, not session.git_remote_normalized.
    unique_remote = f"github.com/b/{uuid.uuid4().hex[:8]}"
    project_b = Project(
        id=f"proj_b_{uuid.uuid4().hex[:8]}",
        name="org-b authoritative",
        git_remote_normalized=unique_remote,
        owner_id=admin_b.id,
        org_id=org_b.id,
        context_document="",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(project_b)
    sess.project_id = project_b.id  # authoritative anchor
    await db_session.commit()

    before = (await db_session.execute(_sel(func.count(Handoff.id)))).scalar()
    resp = await client.post(
        "/api/v1/handoffs",
        headers=_hdrs(svc_key),
        json={"session_id": sid, "recipient_email": "r@x.com"},
    )
    assert resp.status_code == 403, resp.text
    structured = _structured_error(resp.json())
    assert structured.get("error") == "cross_org_denied", resp.json()
    after = (await db_session.execute(_sel(func.count(Handoff.id)))).scalar()
    assert after == before, "no Handoff row may be inserted when project_id anchors to other org"


@pytest.mark.asyncio
async def test_service_key_revoke_denied_on_orphan_handoff(
    client: AsyncClient, db_session: AsyncSession, sample_sfs_tar: bytes
):
    """R6 MEDIUM — revoke/decline/comment must deny service keys when
    the source session is missing (orphan handoff). Helper denies on
    None; this regression proves the route no longer short-circuits."""
    from sessionfs.server.db.models import Handoff, Session as _Sess
    from sqlalchemy import select as _sel

    org, admin, admin_raw = await _make_org_with_admin(db_session)
    # Push session + register matching project so the create-handoff
    # boundary passes (admin owns project in admin's org).
    sid = f"ses_{uuid.uuid4().hex[:12]}"
    push = await client.put(
        f"/api/v1/sessions/{sid}/sync",
        headers=_hdrs(admin_raw),
        files={"file": ("s.tar.gz", io.BytesIO(sample_sfs_tar), "application/gzip")},
    )
    assert push.status_code == 201
    sess = (
        await db_session.execute(_sel(_Sess).where(_Sess.id == sid))
    ).scalar_one()
    from sessionfs.server.db.models import Project
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:8]}",
        name="ok",
        git_remote_normalized=sess.git_remote_normalized or "github.com/x/y",
        owner_id=admin.id,
        org_id=org.id,
        context_document="",
        created_at=datetime.now(timezone.utc),
    )
    if not sess.git_remote_normalized:
        sess.git_remote_normalized = project.git_remote_normalized
    db_session.add(project)
    sess.project_id = project.id
    await db_session.commit()

    # Create the handoff with a USER key (admin_raw, scope='*') so it
    # succeeds even with the service-key boundary applied (irrelevant
    # for user keys).
    create = await client.post(
        "/api/v1/handoffs",
        headers=_hdrs(admin_raw),
        json={"session_id": sid, "recipient_email": "r@x.com"},
    )
    assert create.status_code == 201, create.text
    handoff_id = create.json()["id"]

    # Now hard-delete the source session — handoff is orphaned.
    handoff_row = (
        await db_session.execute(_sel(Handoff).where(Handoff.id == handoff_id))
    ).scalar_one()
    await db_session.delete(sess)
    await db_session.commit()

    # Mint a service key in the same org. Try to revoke the orphan handoff.
    _row, svc_key = await _create_service_key(
        db_session, org.id, admin, scopes=["handoffs:write"]
    )
    resp = await client.post(
        f"/api/v1/handoffs/{handoff_id}/revoke",
        headers=_hdrs(svc_key),
        json={"reason": "cloud agent cleanup"},
    )
    assert resp.status_code == 403, resp.text
    structured = _structured_error(resp.json())
    assert structured.get("error") == "service_key_project_required", resp.json()

    # Handoff status must be unchanged (still pending) — boundary denied
    # BEFORE the UPDATE.
    await db_session.refresh(handoff_row)
    assert handoff_row.status == "pending"
    assert handoff_row.revoked_at is None


# ── Cross-org for service-key admin routes ──


@pytest.mark.asyncio
async def test_org_admin_cannot_list_other_org_keys(
    client: AsyncClient, db_session: AsyncSession
):
    """404 (not 403) for cross-org admin attempts — existence hiding."""
    _org_a, _admin_a, raw_a = await _make_org_with_admin(db_session, slug="a")
    org_b, _admin_b, _raw_b = await _make_org_with_admin(db_session, slug="b")
    resp = await client.get(
        f"/api/v1/orgs/{org_b.id}/service-keys", headers=_hdrs(raw_a)
    )
    assert resp.status_code == 404


# ── v0.10.10 Phase 3: ticket routes opt into service-key scopes ──


@pytest.mark.asyncio
async def test_service_key_tickets_read_routes_allowed(
    client: AsyncClient, db_session: AsyncSession
):
    from sessionfs.server.db.models import TicketComment

    org, admin, _ = await _make_org_with_admin(db_session, slug="tickets-read")
    project = await _make_org_project(db_session, org, admin)
    ticket = await _make_ticket(db_session, project, admin)
    db_session.add(
        TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket.id,
            author_user_id=admin.id,
            content="ready for polling",
        )
    )
    await db_session.commit()
    _row, svc_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["tickets:read"],
        project_ids=[project.id],
    )

    list_resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(svc_key),
    )
    assert list_resp.status_code == 200, list_resp.text
    assert [row["id"] for row in list_resp.json()] == [ticket.id]

    get_resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}",
        headers=_hdrs(svc_key),
    )
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["id"] == ticket.id

    comments_resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/comments",
        headers=_hdrs(svc_key),
    )
    assert comments_resp.status_code == 200, comments_resp.text
    assert comments_resp.json()[0]["content"] == "ready for polling"

    review_resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/review-state",
        headers=_hdrs(svc_key),
    )
    assert review_resp.status_code == 200, review_resp.text
    assert review_resp.json() == {"ticket_id": ticket.id, "review_state": None}


@pytest.mark.asyncio
async def test_service_key_can_create_ticket_with_write_scope(
    client: AsyncClient, db_session: AsyncSession
):
    from sqlalchemy import select as _sel

    org, admin, _ = await _make_org_with_admin(db_session, slug="ticket-create")
    project = await _make_org_project(db_session, org, admin)
    svc_row, svc_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["tickets:write"],
        project_ids=[project.id],
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(svc_key),
        json={
            "title": "Scout surfaced a backend follow-up",
            "description": "Created by scoped service key",
            "priority": "medium",
            "acceptance_criteria": ["route accepts scoped key"],
        },
    )
    assert resp.status_code == 201, resp.text
    ticket = (
        await db_session.execute(
            _sel(Ticket).where(Ticket.id == resp.json()["id"])
        )
    ).scalar_one()
    assert ticket.actor_type == "service_key"
    assert ticket.service_key_id == svc_row.id
    assert ticket.service_key_name == svc_row.service_key_name


@pytest.mark.asyncio
async def test_service_key_tickets_write_routes_allowed_and_comment_audited(
    client: AsyncClient, db_session: AsyncSession
):
    from sessionfs.server.db.models import TicketComment
    from sqlalchemy import select as _sel

    org, admin, _ = await _make_org_with_admin(db_session, slug="tickets-write")
    project = await _make_org_project(db_session, org, admin)
    await _make_persona(db_session, project.id, admin)
    ticket = await _make_ticket(db_session, project, admin, assigned_to="atlas")
    svc_row, svc_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["tickets:write"],
        project_ids=[project.id],
    )

    comment_resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/comments",
        headers=_hdrs(svc_key),
        json={"content": "triage says start this", "author_persona": "n8n"},
    )
    assert comment_resp.status_code == 201, comment_resp.text
    comment = (
        await db_session.execute(
            _sel(TicketComment).where(TicketComment.id == comment_resp.json()["id"])
        )
    ).scalar_one()
    assert comment.actor_type == "service_key"
    assert comment.service_key_id == svc_row.id
    assert comment.service_key_name == svc_row.service_key_name

    start_resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/start",
        headers=_hdrs(svc_key),
    )
    assert start_resp.status_code == 200, start_resp.text
    lease_epoch = start_resp.json()["ticket"]["lease_epoch"]

    complete_resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/complete",
        headers=_hdrs(svc_key),
        json={"notes": "finished", "lease_epoch": lease_epoch},
    )
    assert complete_resp.status_code == 200, complete_resp.text
    assert complete_resp.json()["status"] == "review"


@pytest.mark.asyncio
async def test_service_key_tickets_insufficient_scope_denied(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _ = await _make_org_with_admin(db_session, slug="ticket-scope")
    project = await _make_org_project(db_session, org, admin)
    ticket = await _make_ticket(db_session, project, admin)
    _row, svc_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["tickets:read"],
        project_ids=[project.id],
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/comments",
        headers=_hdrs(svc_key),
        json={"content": "should not write"},
    )
    assert resp.status_code == 403, resp.text
    structured = _structured_error(resp.json())
    assert structured.get("error") == "insufficient_scope", resp.json()
    assert structured.get("required") == ["tickets:write"]
    assert structured.get("current") == ["tickets:read"]


@pytest.mark.asyncio
async def test_service_key_tickets_project_allowlist_denied_before_write(
    client: AsyncClient, db_session: AsyncSession
):
    from sessionfs.server.db.models import TicketComment
    from sqlalchemy import func, select as _sel

    org, admin, _ = await _make_org_with_admin(db_session, slug="ticket-allow")
    allowed_project = await _make_org_project(db_session, org, admin)
    denied_project = await _make_org_project(db_session, org, admin)
    denied_ticket = await _make_ticket(db_session, denied_project, admin)
    _row, svc_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["tickets:read", "tickets:write"],
        project_ids=[allowed_project.id],
    )

    read_resp = await client.get(
        f"/api/v1/projects/{denied_project.id}/tickets",
        headers=_hdrs(svc_key),
    )
    assert read_resp.status_code == 403, read_resp.text
    structured = _structured_error(read_resp.json())
    assert structured.get("error") == "project_not_in_allowlist", read_resp.json()

    before = (
        await db_session.execute(_sel(func.count(TicketComment.id)))
    ).scalar()
    write_resp = await client.post(
        f"/api/v1/projects/{denied_project.id}/tickets/{denied_ticket.id}/comments",
        headers=_hdrs(svc_key),
        json={"content": "must not land"},
    )
    assert write_resp.status_code == 403, write_resp.text
    structured = _structured_error(write_resp.json())
    assert structured.get("error") == "project_not_in_allowlist", write_resp.json()
    after = (
        await db_session.execute(_sel(func.count(TicketComment.id)))
    ).scalar()
    assert after == before


@pytest.mark.asyncio
async def test_service_key_ticket_lease_required_mode_and_stale_fence(
    client: AsyncClient, db_session: AsyncSession
):
    import json as _json

    org, admin, _ = await _make_org_with_admin(db_session, slug="ticket-lease")
    org.settings = _json.dumps({"require_lease_epoch_on_ticket_writes": True})
    project = await _make_org_project(db_session, org, admin)
    await _make_persona(db_session, project.id, admin)
    ticket = await _make_ticket(db_session, project, admin, assigned_to="atlas")
    _row, svc_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["tickets:write"],
        project_ids=[project.id],
    )

    start_resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/start",
        headers=_hdrs(svc_key),
    )
    assert start_resp.status_code == 200, start_resp.text
    lease_epoch = start_resp.json()["ticket"]["lease_epoch"]

    no_lease_comment = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/comments",
        headers=_hdrs(svc_key),
        json={"content": "no lease"},
    )
    assert no_lease_comment.status_code == 422, no_lease_comment.text

    with_lease_comment = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/comments",
        headers=_hdrs(svc_key),
        json={"content": "with lease", "lease_epoch": lease_epoch},
    )
    assert with_lease_comment.status_code == 201, with_lease_comment.text

    no_lease_complete = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/complete",
        headers=_hdrs(svc_key),
        json={"notes": "no lease"},
    )
    assert no_lease_complete.status_code == 422, no_lease_complete.text

    stale_complete = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/complete",
        headers=_hdrs(svc_key),
        json={"notes": "stale", "lease_epoch": lease_epoch - 1},
    )
    assert stale_complete.status_code == 409, stale_complete.text

    complete_resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/complete",
        headers=_hdrs(svc_key),
        json={"notes": "done", "lease_epoch": lease_epoch},
    )
    assert complete_resp.status_code == 200, complete_resp.text
    assert complete_resp.json()["status"] == "review"


# ── v0.10.19 Phase 3.6: persona routes opt into service-key scopes ──


@pytest.mark.asyncio
async def test_service_key_can_create_persona_with_write_scope(
    client: AsyncClient, db_session: AsyncSession
):
    from sqlalchemy import select as _sel

    org, admin, _ = await _make_org_with_admin(db_session, slug="persona-create")
    project = await _make_org_project(db_session, org, admin)
    svc_row, svc_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["personas:write"],
        project_ids=[project.id],
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(svc_key),
        json={
            "name": "scout",
            "role": "Research Agent",
            "content": "Runtime persona loaded by an n8n workflow.",
            "specializations": ["research", "triage"],
        },
    )
    assert resp.status_code == 201, resp.text
    persona = (
        await db_session.execute(
            _sel(AgentPersona).where(AgentPersona.id == resp.json()["id"])
        )
    ).scalar_one()
    assert persona.actor_type == "service_key"
    assert persona.service_key_id == svc_row.id
    assert persona.service_key_name == svc_row.service_key_name


@pytest.mark.asyncio
async def test_service_key_can_list_and_get_personas_with_read_scope(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _ = await _make_org_with_admin(db_session, slug="persona-read")
    project = await _make_org_project(db_session, org, admin)
    await _make_persona(db_session, project.id, admin, name="atlas")
    await _make_persona(db_session, project.id, admin, name="scout")
    _row, svc_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["personas:read"],
        project_ids=[project.id],
    )

    list_resp = await client.get(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(svc_key),
    )
    assert list_resp.status_code == 200, list_resp.text
    assert [row["name"] for row in list_resp.json()] == ["atlas", "scout"]

    get_resp = await client.get(
        f"/api/v1/projects/{project.id}/personas/scout",
        headers=_hdrs(svc_key),
    )
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["name"] == "scout"


@pytest.mark.asyncio
async def test_service_key_can_update_and_delete_persona_with_write_scope(
    client: AsyncClient, db_session: AsyncSession
):
    from sqlalchemy import select as _sel

    org, admin, _ = await _make_org_with_admin(db_session, slug="persona-write")
    project = await _make_org_project(db_session, org, admin)
    await _make_persona(db_session, project.id, admin, name="scout")
    svc_row, svc_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["personas:write"],
        project_ids=[project.id],
    )

    update_resp = await client.put(
        f"/api/v1/projects/{project.id}/personas/scout",
        headers=_hdrs(svc_key),
        json={
            "role": "Runtime Scout",
            "content": "Updated by service-key workflow.",
        },
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["version"] == 2
    persona = (
        await db_session.execute(
            _sel(AgentPersona).where(
                AgentPersona.project_id == project.id,
                AgentPersona.name == "scout",
            )
        )
    ).scalar_one()
    assert persona.actor_type == "service_key"
    assert persona.service_key_id == svc_row.id
    assert persona.service_key_name == svc_row.service_key_name

    delete_resp = await client.delete(
        f"/api/v1/projects/{project.id}/personas/scout",
        headers=_hdrs(svc_key),
    )
    assert delete_resp.status_code == 204, delete_resp.text
    await db_session.refresh(persona)
    assert persona.is_active is False
    assert persona.version == 3
    assert persona.actor_type == "service_key"
    assert persona.service_key_id == svc_row.id
    assert persona.service_key_name == svc_row.service_key_name


@pytest.mark.asyncio
async def test_service_key_personas_insufficient_scope_denied(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _ = await _make_org_with_admin(db_session, slug="persona-scope")
    project = await _make_org_project(db_session, org, admin)
    _row, svc_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["personas:read"],
        project_ids=[project.id],
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(svc_key),
        json={"name": "scout", "role": "Research Agent"},
    )
    assert resp.status_code == 403, resp.text
    structured = _structured_error(resp.json())
    assert structured.get("error") == "insufficient_scope", resp.json()
    assert structured.get("required") == ["personas:write"]
    assert structured.get("current") == ["personas:read"]


@pytest.mark.asyncio
async def test_service_key_personas_project_allowlist_denied_before_write(
    client: AsyncClient, db_session: AsyncSession
):
    from sqlalchemy import func, select as _sel

    org, admin, _ = await _make_org_with_admin(db_session, slug="persona-allow")
    allowed_project = await _make_org_project(db_session, org, admin)
    denied_project = await _make_org_project(db_session, org, admin)
    _row, svc_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["personas:write"],
        project_ids=[allowed_project.id],
    )

    before = (
        await db_session.execute(_sel(func.count(AgentPersona.id)))
    ).scalar()
    resp = await client.post(
        f"/api/v1/projects/{denied_project.id}/personas",
        headers=_hdrs(svc_key),
        json={"name": "scout", "role": "Research Agent"},
    )
    assert resp.status_code == 403, resp.text
    structured = _structured_error(resp.json())
    assert structured.get("error") == "project_not_in_allowlist", resp.json()
    after = (
        await db_session.execute(_sel(func.count(AgentPersona.id)))
    ).scalar()
    assert after == before


@pytest.mark.asyncio
async def test_service_key_personas_cross_org_denied(
    client: AsyncClient, db_session: AsyncSession
):
    org_a, admin_a, _ = await _make_org_with_admin(db_session, slug="persona-a")
    org_b, admin_b, _ = await _make_org_with_admin(db_session, slug="persona-b")
    project_b = await _make_org_project(db_session, org_b, admin_b)
    _row, svc_key = await _create_service_key(
        db_session,
        org_a.id,
        admin_a,
        scopes=["personas:read"],
    )

    resp = await client.get(
        f"/api/v1/projects/{project_b.id}/personas",
        headers=_hdrs(svc_key),
    )
    assert resp.status_code == 403, resp.text
    structured = _structured_error(resp.json())
    assert structured.get("error") == "cross_org_denied", resp.json()


@pytest.mark.asyncio
async def test_user_key_regression_on_converted_persona_routes(
    client: AsyncClient, db_session: AsyncSession
):
    from sqlalchemy import select as _sel

    org, admin, user_key = await _make_org_with_admin(db_session, slug="persona-user")
    project = await _make_org_project(db_session, org, admin)

    create_resp = await client.post(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(user_key),
        json={
            "name": "atlas",
            "role": "Backend Architect",
            "content": "User-key persona creation still works.",
        },
    )
    assert create_resp.status_code == 201, create_resp.text
    persona = (
        await db_session.execute(
            _sel(AgentPersona).where(AgentPersona.id == create_resp.json()["id"])
        )
    ).scalar_one()
    assert persona.actor_type == "user"
    assert persona.service_key_id is None
    assert persona.service_key_name is None

    list_resp = await client.get(
        f"/api/v1/projects/{project.id}/personas",
        headers=_hdrs(user_key),
    )
    assert list_resp.status_code == 200, list_resp.text
    assert [row["name"] for row in list_resp.json()] == ["atlas"]

    get_resp = await client.get(
        f"/api/v1/projects/{project.id}/personas/atlas",
        headers=_hdrs(user_key),
    )
    assert get_resp.status_code == 200, get_resp.text

    update_resp = await client.put(
        f"/api/v1/projects/{project.id}/personas/atlas",
        headers=_hdrs(user_key),
        json={"role": "Senior Backend Architect"},
    )
    assert update_resp.status_code == 200, update_resp.text
    await db_session.refresh(persona)
    assert persona.actor_type == "user"
    assert persona.service_key_id is None

    delete_resp = await client.delete(
        f"/api/v1/projects/{project.id}/personas/atlas",
        headers=_hdrs(user_key),
    )
    assert delete_resp.status_code == 204, delete_resp.text
    await db_session.refresh(persona)
    assert persona.is_active is False
    assert persona.actor_type == "user"
    assert persona.service_key_id is None


# ── v0.10.18 Phase 3.5: knowledge routes opt into service-key scopes ──


@pytest.mark.asyncio
async def test_service_key_can_add_knowledge_entry_with_write_scope(
    client: AsyncClient, db_session: AsyncSession
):
    from sqlalchemy import select as _sel

    org, admin, _ = await _make_org_with_admin(db_session, slug="kb-add")
    project = await _make_org_project(db_session, org, admin)
    svc_row, svc_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["knowledge:write"],
        project_ids=[project.id],
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/entries/add",
        headers=_hdrs(svc_key),
        json={
            "entry_type": "discovery",
            "content": (
                "src/server/routes/knowledge.py accepts scoped service "
                "keys for Scout add-entry workflows."
            ),
            "confidence": 0.95,
            "session_id": "ses_scout_add",
        },
    )
    assert resp.status_code == 201, resp.text
    entry = (
        await db_session.execute(
            _sel(KnowledgeEntry).where(KnowledgeEntry.id == resp.json()["id"])
        )
    ).scalar_one()
    assert entry.actor_type == "service_key"
    assert entry.service_key_id == svc_row.id
    assert entry.service_key_name == svc_row.service_key_name


@pytest.mark.asyncio
async def test_service_key_can_list_and_get_knowledge_entries_with_read_scope(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _ = await _make_org_with_admin(db_session, slug="kb-read")
    project = await _make_org_project(db_session, org, admin)
    entry = await _make_knowledge_entry(db_session, project, admin, claim_class="claim")
    _row, svc_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["knowledge:read"],
        project_ids=[project.id],
    )

    list_resp = await client.get(
        f"/api/v1/projects/{project.id}/entries",
        headers=_hdrs(svc_key),
    )
    assert list_resp.status_code == 200, list_resp.text
    assert entry.id in {row["id"] for row in list_resp.json()}

    get_resp = await client.get(
        f"/api/v1/projects/{project.id}/entries/{entry.id}",
        headers=_hdrs(svc_key),
    )
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["id"] == entry.id


@pytest.mark.asyncio
async def test_service_key_can_update_promote_supersede_refresh_knowledge_entries_with_write_scope(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _ = await _make_org_with_admin(db_session, slug="kb-write")
    project = await _make_org_project(db_session, org, admin)
    old_entry = await _make_knowledge_entry(
        db_session,
        project,
        admin,
        claim_class="note",
        confidence=0.95,
        content=(
            "src/server/routes/knowledge.py has an explicit scoped "
            "write path for promote refresh and supersede coverage."
        ),
    )
    new_entry = await _make_knowledge_entry(
        db_session,
        project,
        admin,
        claim_class="claim",
        confidence=0.95,
        content=(
            "docs/api-keys.md records the Scout knowledge service-key "
            "contract with a different concrete identifier."
        ),
    )
    svc_row, svc_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["knowledge:write"],
        project_ids=[project.id],
    )

    refresh_resp = await client.put(
        f"/api/v1/projects/{project.id}/entries/{old_entry.id}/refresh",
        headers=_hdrs(svc_key),
    )
    assert refresh_resp.status_code == 200, refresh_resp.text
    assert refresh_resp.json()["freshness_class"] == "current"

    promote_resp = await client.put(
        f"/api/v1/projects/{project.id}/entries/{old_entry.id}/promote",
        headers=_hdrs(svc_key),
    )
    assert promote_resp.status_code == 200, promote_resp.text
    assert promote_resp.json()["claim_class"] == "claim"

    supersede_resp = await client.put(
        f"/api/v1/projects/{project.id}/entries/{old_entry.id}/supersede",
        headers=_hdrs(svc_key),
        json={"superseding_id": new_entry.id, "reason": "newer Scout evidence"},
    )
    assert supersede_resp.status_code == 200, supersede_resp.text

    dismiss_resp = await client.put(
        f"/api/v1/projects/{project.id}/entries/{new_entry.id}",
        headers=_hdrs(svc_key),
        json={"dismissed": True, "reason": "covered by supersession test"},
    )
    assert dismiss_resp.status_code == 200, dismiss_resp.text
    assert dismiss_resp.json()["dismissed"] is True

    await db_session.refresh(old_entry)
    await db_session.refresh(new_entry)
    assert old_entry.actor_type == "service_key"
    assert old_entry.service_key_id == svc_row.id
    assert old_entry.service_key_name == svc_row.service_key_name
    assert new_entry.actor_type == "service_key"
    assert new_entry.service_key_id == svc_row.id
    assert new_entry.service_key_name == svc_row.service_key_name


@pytest.mark.asyncio
async def test_service_key_knowledge_insufficient_scope_denied(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _ = await _make_org_with_admin(db_session, slug="kb-scope")
    project = await _make_org_project(db_session, org, admin)
    _row, svc_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["tickets:read"],
        project_ids=[project.id],
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/entries/add",
        headers=_hdrs(svc_key),
        json={
            "content": "src/server/routes/knowledge.py must require write scope.",
            "confidence": 0.9,
        },
    )
    assert resp.status_code == 403, resp.text
    structured = _structured_error(resp.json())
    assert structured.get("error") == "insufficient_scope", resp.json()
    assert structured.get("required") == ["knowledge:write"]
    assert structured.get("current") == ["tickets:read"]


@pytest.mark.asyncio
async def test_service_key_knowledge_project_allowlist_denied_before_write(
    client: AsyncClient, db_session: AsyncSession
):
    from sqlalchemy import func, select as _sel

    org, admin, _ = await _make_org_with_admin(db_session, slug="kb-allow")
    allowed_project = await _make_org_project(db_session, org, admin)
    denied_project = await _make_org_project(db_session, org, admin)
    _row, svc_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["knowledge:write"],
        project_ids=[allowed_project.id],
    )

    before = (
        await db_session.execute(_sel(func.count(KnowledgeEntry.id)))
    ).scalar()
    resp = await client.post(
        f"/api/v1/projects/{denied_project.id}/entries/add",
        headers=_hdrs(svc_key),
        json={
            "content": (
                "src/server/routes/knowledge.py must not write before "
                "the service-key project allowlist check."
            ),
            "confidence": 0.9,
        },
    )
    assert resp.status_code == 403, resp.text
    structured = _structured_error(resp.json())
    assert structured.get("error") == "project_not_in_allowlist", resp.json()
    after = (
        await db_session.execute(_sel(func.count(KnowledgeEntry.id)))
    ).scalar()
    assert after == before


@pytest.mark.asyncio
async def test_service_key_still_denied_on_compile_rebuild_dismiss_stale(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _ = await _make_org_with_admin(db_session, slug="kb-tier-c")
    project = await _make_org_project(db_session, org, admin)
    _row, svc_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["knowledge:read", "knowledge:write"],
        project_ids=[project.id],
    )

    requests = [
        ("post", f"/api/v1/projects/{project.id}/entries/dismiss-stale", None),
        ("post", f"/api/v1/projects/{project.id}/compile", {}),
        ("post", f"/api/v1/projects/{project.id}/rebuild", {}),
        ("get", f"/api/v1/projects/{project.id}/health", None),
        ("get", f"/api/v1/projects/{project.id}/compilations", None),
    ]
    for method, url, body in requests:
        if method == "post":
            resp = await client.post(url, headers=_hdrs(svc_key), json=body)
        else:
            resp = await client.get(url, headers=_hdrs(svc_key))
        assert resp.status_code == 403, resp.text
        structured = _structured_error(resp.json())
        assert structured.get("error") == "service_key_not_allowed", resp.json()


@pytest.mark.asyncio
async def test_service_key_still_denied_on_unconverted_ticket_route(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _ = await _make_org_with_admin(db_session, slug="ticket-deny")
    project = await _make_org_project(db_session, org, admin)
    ticket = await _make_ticket(db_session, project, admin, status="review")
    _row, svc_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["tickets:write"],
        project_ids=[project.id],
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/accept",
        headers=_hdrs(svc_key),
    )
    assert resp.status_code == 403, resp.text
    structured = _structured_error(resp.json())
    assert structured.get("error") == "service_key_not_allowed", resp.json()


@pytest.mark.asyncio
async def test_user_key_regression_on_converted_ticket_routes(
    client: AsyncClient, db_session: AsyncSession
):
    from sessionfs.server.db.models import TicketComment
    from sqlalchemy import select as _sel

    org, admin, user_key = await _make_org_with_admin(db_session, slug="ticket-user")
    project = await _make_org_project(db_session, org, admin)
    await _make_persona(db_session, project.id, admin)
    ticket = await _make_ticket(db_session, project, admin, assigned_to="atlas")

    list_resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(user_key),
    )
    assert list_resp.status_code == 200, list_resp.text

    get_resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}",
        headers=_hdrs(user_key),
    )
    assert get_resp.status_code == 200, get_resp.text

    comment_resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/comments",
        headers=_hdrs(user_key),
        json={"content": "still a user-key comment"},
    )
    assert comment_resp.status_code == 201, comment_resp.text
    comment = (
        await db_session.execute(
            _sel(TicketComment).where(TicketComment.id == comment_resp.json()["id"])
        )
    ).scalar_one()
    assert comment.actor_type == "user"
    assert comment.service_key_id is None

    comments_resp = await client.get(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/comments",
        headers=_hdrs(user_key),
    )
    assert comments_resp.status_code == 200, comments_resp.text

    start_resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/start",
        headers=_hdrs(user_key),
    )
    assert start_resp.status_code == 200, start_resp.text
    lease_epoch = start_resp.json()["ticket"]["lease_epoch"]

    complete_resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets/{ticket.id}/complete",
        headers=_hdrs(user_key),
        json={"notes": "done by user", "lease_epoch": lease_epoch},
    )
    assert complete_resp.status_code == 200, complete_resp.text


@pytest.mark.asyncio
async def test_user_key_regression_on_converted_knowledge_routes(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, user_key = await _make_org_with_admin(db_session, slug="kb-user")
    project = await _make_org_project(db_session, org, admin)

    add_resp = await client.post(
        f"/api/v1/projects/{project.id}/entries/add",
        headers=_hdrs(user_key),
        json={
            "entry_type": "discovery",
            "content": (
                "src/server/routes/knowledge.py continues to accept "
                "personal user keys after require_scope conversion."
            ),
            "confidence": 0.95,
            "session_id": "ses_user_kb",
        },
    )
    assert add_resp.status_code == 201, add_resp.text
    added_id = add_resp.json()["id"]
    added_entry = await db_session.get(KnowledgeEntry, added_id)
    assert added_entry is not None
    assert added_entry.actor_type == "user"
    assert added_entry.service_key_id is None

    list_resp = await client.get(
        f"/api/v1/projects/{project.id}/entries",
        headers=_hdrs(user_key),
    )
    assert list_resp.status_code == 200, list_resp.text
    assert added_id in {row["id"] for row in list_resp.json()}

    get_resp = await client.get(
        f"/api/v1/projects/{project.id}/entries/{added_id}",
        headers=_hdrs(user_key),
    )
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["id"] == added_id

    promote_entry = await _make_knowledge_entry(
        db_session,
        project,
        admin,
        claim_class="note",
        confidence=0.95,
        content=(
            "src/server/routes/knowledge.py user-key promote coverage "
            "uses a concrete route path and enough content."
        ),
    )
    superseding_entry = await _make_knowledge_entry(
        db_session,
        project,
        admin,
        claim_class="claim",
        confidence=0.95,
        content=(
            "docs/api-keys.md user-key regression coverage uses a separate "
            "concrete identifier for supersession."
        ),
    )

    refresh_resp = await client.put(
        f"/api/v1/projects/{project.id}/entries/{promote_entry.id}/refresh",
        headers=_hdrs(user_key),
    )
    assert refresh_resp.status_code == 200, refresh_resp.text

    promote_resp = await client.put(
        f"/api/v1/projects/{project.id}/entries/{promote_entry.id}/promote",
        headers=_hdrs(user_key),
    )
    assert promote_resp.status_code == 200, promote_resp.text

    supersede_resp = await client.put(
        f"/api/v1/projects/{project.id}/entries/{promote_entry.id}/supersede",
        headers=_hdrs(user_key),
        json={"superseding_id": superseding_entry.id, "reason": "user regression"},
    )
    assert supersede_resp.status_code == 200, supersede_resp.text

    dismiss_resp = await client.put(
        f"/api/v1/projects/{project.id}/entries/{superseding_entry.id}",
        headers=_hdrs(user_key),
        json={"dismissed": True, "reason": "user-key regression"},
    )
    assert dismiss_resp.status_code == 200, dismiss_resp.text

    await db_session.refresh(promote_entry)
    await db_session.refresh(superseding_entry)
    assert promote_entry.actor_type == "user"
    assert promote_entry.service_key_id is None
    assert superseding_entry.actor_type == "user"
    assert superseding_entry.service_key_id is None


@pytest.mark.asyncio
async def test_service_key_knowledge_read_cannot_mutate_freshness_counters(
    client: AsyncClient, db_session: AsyncSession
):
    """Codex R1 HIGH 1 — knowledge:read service key must not mutate
    used_in_answer_count, last_relevant_at, or retrieved_count via the
    search side-effect path on GET /entries. A service key with both
    knowledge:read AND knowledge:write keeps the existing telemetry
    behavior. User keys are unaffected.
    """
    org, admin, _ = await _make_org_with_admin(db_session, slug="kb-readonly")
    project = await _make_org_project(db_session, org, admin)

    keyword = uuid.uuid4().hex[:10]
    entry = await _make_knowledge_entry(
        db_session,
        project,
        admin,
        claim_class="claim",
        confidence=0.95,
        content=(
            f"src/scout/{keyword}.py owns the telemetry-mutation regression "
            "for knowledge:read service keys, covering the search side-effect "
            "path explicitly."
        ),
    )
    initial_used = entry.used_in_answer_count
    initial_retrieved = entry.retrieved_count
    initial_relevant = entry.last_relevant_at

    _ro_row, ro_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["knowledge:read"],
        project_ids=[project.id],
    )

    # Strong-signal path (used_in_answer=true) must NOT mutate.
    resp = await client.get(
        f"/api/v1/projects/{project.id}/entries",
        headers=_hdrs(ro_key),
        params={"search": keyword, "used_in_answer": "true"},
    )
    assert resp.status_code == 200, resp.text
    assert any(row["id"] == entry.id for row in resp.json())
    await db_session.refresh(entry)
    assert entry.used_in_answer_count == initial_used
    assert entry.retrieved_count == initial_retrieved
    assert entry.last_relevant_at == initial_relevant

    # Weak-signal path (search only) must NOT mutate retrieved_count either.
    resp = await client.get(
        f"/api/v1/projects/{project.id}/entries",
        headers=_hdrs(ro_key),
        params={"search": keyword},
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(entry)
    assert entry.retrieved_count == initial_retrieved
    assert entry.used_in_answer_count == initial_used
    assert entry.last_relevant_at == initial_relevant

    # A read+write key DOES update telemetry (regression on the gate).
    _rw_row, rw_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["knowledge:read", "knowledge:write"],
        project_ids=[project.id],
    )
    resp = await client.get(
        f"/api/v1/projects/{project.id}/entries",
        headers=_hdrs(rw_key),
        params={"search": keyword, "used_in_answer": "true"},
    )
    assert resp.status_code == 200, resp.text
    await db_session.refresh(entry)
    assert entry.used_in_answer_count == initial_used + 1
    assert entry.last_relevant_at is not None
    if initial_relevant is not None:
        assert entry.last_relevant_at > initial_relevant


@pytest.mark.asyncio
async def test_service_key_can_act_on_org_project_owned_by_other_member(
    client: AsyncClient, db_session: AsyncSession
):
    """Codex R1 MEDIUM 1 — service key minted by an org admin must work
    on a project owned by a DIFFERENT org member, even when the admin has
    no captured Session row matching the project's git_remote. The legacy
    `_get_project_or_404(project_id, db, user.id)` user-access gate must
    NOT block service-key requests; only the org/allowlist check (via
    `assert_service_key_can_access_project`) should apply.
    """
    org, admin, _ = await _make_org_with_admin(db_session, slug="kb-other-owner")
    other_member, _other_raw = await _make_user_with_key(
        db_session, f"member-{uuid.uuid4().hex[:6]}@x.com"
    )
    db_session.add(
        OrgMember(
            org_id=org.id,
            user_id=other_member.id,
            role="member",
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    project = await _make_org_project(db_session, org, other_member)
    # Confirm the gate condition the bug depended on: admin is NOT
    # project owner and has no Session row on project's git_remote.
    assert project.owner_id != admin.id

    # tickets:write service key minted by admin, scoped to that project.
    tk_row, tk_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["tickets:write"],
        project_ids=[project.id],
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/tickets",
        headers=_hdrs(tk_key),
        json={
            "title": "Scout cross-owner ticket",
            "description": (
                "src/scout/cross_owner.py opens a ticket for a project "
                "owned by another org member; the legacy user-access "
                "gate must not block this service key."
            ),
            "priority": "medium",
        },
    )
    assert resp.status_code == 201, resp.text
    ticket_id = resp.json()["id"]
    from sessionfs.server.db.models import Ticket
    ticket_row = await db_session.get(Ticket, ticket_id)
    assert ticket_row is not None
    assert ticket_row.actor_type == "service_key"
    assert ticket_row.service_key_id == tk_row.id

    # knowledge:write service key on the same other-owner project.
    kb_row, kb_key = await _create_service_key(
        db_session,
        org.id,
        admin,
        scopes=["knowledge:write"],
        project_ids=[project.id],
    )
    resp = await client.post(
        f"/api/v1/projects/{project.id}/entries/add",
        headers=_hdrs(kb_key),
        json={
            "entry_type": "discovery",
            "content": (
                "src/scout/cross_owner.py adds a finding to a project "
                "owned by a different org member, exercising the v0.10.19 "
                "service-key project-load branch."
            ),
            "confidence": 0.9,
        },
    )
    assert resp.status_code == 201, resp.text
    kb_entry_id = resp.json()["id"]
    kb_entry = await db_session.get(KnowledgeEntry, kb_entry_id)
    assert kb_entry is not None
    assert kb_entry.actor_type == "service_key"
    assert kb_entry.service_key_id == kb_row.id
