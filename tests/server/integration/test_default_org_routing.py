"""v0.10.0 Phase 5 — multi-org daemon routing regression tests.

Covers:
  - GET /api/v1/auth/me now includes `default_org_id`.
  - PUT /api/v1/auth/me/default-org validates membership before setting.
  - POST /api/v1/projects/ accepts `org_id` and validates membership.
  - sync_push resolves session.project_id from git_remote, gated by
    project access (owner or org member).
  - Session.project_id is recorded on the new session row.

The daemon-side capture (workspace.json → git remote) is already
covered by the existing capture watcher tests; this file pins the
server contract that ties capture → project → org.
"""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    OrgMember,
    Organization,
    Project,
    Session,
    User,
)


async def _make_user(db: AsyncSession, name: str = "alice") -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier="team",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"{name}-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _make_org(db: AsyncSession, name: str = "Acme") -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name=name,
        slug=f"{name.lower()}-{uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
    )
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


# ── /me default_org_id surface ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_me_returns_default_org_id_when_set(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=user.id, role="member"))
    user.default_org_id = org.id
    await db_session.commit()

    resp = await client.get("/api/v1/auth/me", headers=_hdrs(key))
    assert resp.status_code == 200
    assert resp.json()["default_org_id"] == org.id


@pytest.mark.asyncio
async def test_me_returns_null_default_org_id_for_personal_user(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    resp = await client.get("/api/v1/auth/me", headers=_hdrs(key))
    assert resp.status_code == 200
    assert resp.json()["default_org_id"] is None


# ── PUT /me/default-org membership validation ─────────────────────────


@pytest.mark.asyncio
async def test_set_default_org_succeeds_for_member(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=user.id, role="member"))
    await db_session.commit()

    resp = await client.put(
        "/api/v1/auth/me/default-org",
        headers=_hdrs(key),
        json={"org_id": org.id},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["default_org_id"] == org.id

    # Confirm via /me — verifies the write persisted across the route
    # session (test fixture's db_session is a separate transaction).
    me_resp = await client.get("/api/v1/auth/me", headers=_hdrs(key))
    assert me_resp.status_code == 200
    assert me_resp.json()["default_org_id"] == org.id


@pytest.mark.asyncio
async def test_set_default_org_rejects_non_member(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, "Beta")  # user is NOT a member
    resp = await client.put(
        "/api/v1/auth/me/default-org",
        headers=_hdrs(key),
        json={"org_id": org.id},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_set_default_org_clears_when_null(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=user.id, role="member"))
    user.default_org_id = org.id
    await db_session.commit()

    resp = await client.put(
        "/api/v1/auth/me/default-org",
        headers=_hdrs(key),
        json={"org_id": None},
    )
    assert resp.status_code == 200
    assert resp.json()["default_org_id"] is None


# ── POST /projects/ org_id membership validation ──────────────────────


@pytest.mark.asyncio
async def test_create_project_with_org_id_succeeds_for_member(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=user.id, role="member"))
    await db_session.commit()

    resp = await client.post(
        "/api/v1/projects/",
        headers=_hdrs(key),
        json={
            "name": "phase5",
            "git_remote_normalized": f"acme/repo-{uuid.uuid4().hex[:6]}",
            "org_id": org.id,
        },
    )
    assert resp.status_code == 201, resp.text
    proj = (
        await db_session.execute(
            select(Project).where(Project.id == resp.json()["id"])
        )
    ).scalar_one()
    assert proj.org_id == org.id


@pytest.mark.asyncio
async def test_create_project_with_org_id_rejects_non_member(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    org = await _make_org(db_session, "Outside")
    resp = await client.post(
        "/api/v1/projects/",
        headers=_hdrs(key),
        json={
            "name": "intruder",
            "git_remote_normalized": f"outside/repo-{uuid.uuid4().hex[:6]}",
            "org_id": org.id,
        },
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_create_project_with_org_id_inherits_kb_defaults_at_creation(
    client: AsyncClient, db_session: AsyncSession
):
    """v0.10.0 Phase 6 Round 2 regression (KB entry 296).

    When a project is created inside an org that has set non-default
    KB knobs in its `general` settings block, the new project row
    should inherit those values at creation time. This is "creation
    defaults" inheritance — live inheritance would require nullable
    project columns and a resolver at every read site.
    """
    import json as _json

    user, key = await _make_user(db_session)
    org = await _make_org(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=user.id, role="admin"))
    # Seed the org's general settings.
    org.settings = _json.dumps({
        "general": {
            "kb_retention_days": 30,
            "kb_max_context_words": 1500,
            "kb_section_page_limit": 10,
        },
    })
    await db_session.commit()

    resp = await client.post(
        "/api/v1/projects/",
        headers=_hdrs(key),
        json={
            "name": "inherits",
            "git_remote_normalized": f"acme/inherits-{uuid.uuid4().hex[:6]}",
            "org_id": org.id,
        },
    )
    assert resp.status_code == 201, resp.text

    proj = (
        await db_session.execute(
            select(Project).where(Project.id == resp.json()["id"])
        )
    ).scalar_one()
    # New project picks up the org's creation defaults verbatim.
    assert proj.kb_retention_days == 30
    assert proj.kb_max_context_words == 1500
    assert proj.kb_section_page_limit == 10


@pytest.mark.asyncio
async def test_create_project_with_org_id_falls_back_to_column_defaults(
    client: AsyncClient, db_session: AsyncSession
):
    """When the org has NO general settings, new projects use the
    server's hardcoded Project column defaults (the pre-Phase-6
    behavior). This preserves backward compatibility."""
    user, key = await _make_user(db_session)
    org = await _make_org(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=user.id, role="admin"))
    await db_session.commit()

    resp = await client.post(
        "/api/v1/projects/",
        headers=_hdrs(key),
        json={
            "name": "no-org-defaults",
            "git_remote_normalized": f"acme/plain-{uuid.uuid4().hex[:6]}",
            "org_id": org.id,
        },
    )
    assert resp.status_code == 201, resp.text
    proj = (
        await db_session.execute(
            select(Project).where(Project.id == resp.json()["id"])
        )
    ).scalar_one()
    # Hardcoded server defaults from models.py.
    assert proj.kb_retention_days == 180
    assert proj.kb_max_context_words == 2000
    assert proj.kb_section_page_limit == 30


@pytest.mark.asyncio
async def test_create_project_without_org_id_is_personal(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    resp = await client.post(
        "/api/v1/projects/",
        headers=_hdrs(key),
        json={
            "name": "solo",
            "git_remote_normalized": f"solo/repo-{uuid.uuid4().hex[:6]}",
        },
    )
    assert resp.status_code == 201, resp.text
    proj = (
        await db_session.execute(
            select(Project).where(Project.id == resp.json()["id"])
        )
    ).scalar_one()
    assert proj.org_id is None


# ── sync_push project_id resolution ────────────────────────────────────


def _make_sfs_tarball(
    git_remote: str = "",
    session_id: str = "ses_phase5_x",
) -> bytes:
    """Build a minimal .sfs tarball the sync route will accept."""
    buf = io.BytesIO()
    manifest = {
        "session_id": session_id,
        "title": "Phase 5 test",
        "source_tool": "claude-code",
        "source_tool_version": "1.0",
        "model_provider": "anthropic",
        "model_id": "claude-3-5",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tags": [],
    }
    workspace = {
        "git": {
            "remote_url": git_remote,
            "branch": "main",
            "commit_sha": "abc",
        }
    }
    messages = ""
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in [
            ("manifest.json", json.dumps(manifest).encode()),
            ("workspace.json", json.dumps(workspace).encode()),
            ("messages.jsonl", messages.encode()),
            ("tools.json", b"[]"),
        ]:
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    buf.seek(0)
    return buf.read()


@pytest.mark.asyncio
async def test_sync_push_links_session_to_matching_project_for_owner(
    client: AsyncClient, db_session: AsyncSession
):
    """sync_push resolves project_id when git_remote matches a project the
    user owns (personal scope)."""
    user, key = await _make_user(db_session)
    remote = f"acme/phase5-{uuid.uuid4().hex[:6]}"
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="phase5-owner",
        git_remote_normalized=remote,
        context_document="",
        owner_id=user.id,
        org_id=None,
    )
    db_session.add(project)
    await db_session.commit()

    session_id = f"ses_{uuid.uuid4().hex[:24]}"
    blob = _make_sfs_tarball(git_remote=f"https://github.com/{remote}.git", session_id=session_id)
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=_hdrs(key),
        files={"file": ("session.tar.gz", blob, "application/gzip")},
    )
    assert resp.status_code in (200, 201), resp.text
    row = (
        await db_session.execute(select(Session).where(Session.id == session_id))
    ).scalar_one()
    assert row.project_id == project.id


@pytest.mark.asyncio
async def test_sync_push_links_session_for_org_member(
    client: AsyncClient, db_session: AsyncSession
):
    """sync_push resolves project_id when git_remote matches an org project
    the user belongs to (but doesn't own)."""
    owner, _ = await _make_user(db_session, "owner")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=owner.id, role="admin"))
    db_session.add(OrgMember(org_id=org.id, user_id=member.id, role="member"))
    await db_session.commit()

    remote = f"acme/phase5-{uuid.uuid4().hex[:6]}"
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="phase5-org",
        git_remote_normalized=remote,
        context_document="",
        owner_id=owner.id,
        org_id=org.id,
    )
    db_session.add(project)
    await db_session.commit()

    session_id = f"ses_{uuid.uuid4().hex[:24]}"
    blob = _make_sfs_tarball(git_remote=f"https://github.com/{remote}.git", session_id=session_id)
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=_hdrs(member_key),
        files={"file": ("session.tar.gz", blob, "application/gzip")},
    )
    assert resp.status_code in (200, 201), resp.text
    row = (
        await db_session.execute(select(Session).where(Session.id == session_id))
    ).scalar_one()
    assert row.project_id == project.id


@pytest.mark.asyncio
async def test_sync_push_leaves_project_id_null_for_non_member(
    client: AsyncClient, db_session: AsyncSession
):
    """sync_push does NOT link when the user lacks access to the org project."""
    owner, _ = await _make_user(db_session, "owner")
    outsider, outsider_key = await _make_user(db_session, "outsider")
    org = await _make_org(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=owner.id, role="admin"))
    await db_session.commit()

    remote = f"acme/private-{uuid.uuid4().hex[:6]}"
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="org-private",
        git_remote_normalized=remote,
        context_document="",
        owner_id=owner.id,
        org_id=org.id,
    )
    db_session.add(project)
    await db_session.commit()

    session_id = f"ses_{uuid.uuid4().hex[:24]}"
    blob = _make_sfs_tarball(git_remote=f"https://github.com/{remote}.git", session_id=session_id)
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=_hdrs(outsider_key),
        files={"file": ("session.tar.gz", blob, "application/gzip")},
    )
    # Sync should succeed (sessions are user-owned) but NOT link to the project.
    assert resp.status_code in (200, 201), resp.text
    row = (
        await db_session.execute(select(Session).where(Session.id == session_id))
    ).scalar_one()
    assert row.project_id is None


@pytest.mark.asyncio
async def test_sync_push_leaves_project_id_null_when_no_project(
    client: AsyncClient, db_session: AsyncSession
):
    """sync_push leaves project_id NULL when no project exists for the remote."""
    user, key = await _make_user(db_session)
    session_id = f"ses_{uuid.uuid4().hex[:24]}"
    blob = _make_sfs_tarball(
        git_remote="https://github.com/nonexistent/repo.git",
        session_id=session_id,
    )
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=_hdrs(key),
        files={"file": ("session.tar.gz", blob, "application/gzip")},
    )
    assert resp.status_code in (200, 201), resp.text
    row = (
        await db_session.execute(select(Session).where(Session.id == session_id))
    ).scalar_one()
    assert row.project_id is None


@pytest.mark.asyncio
async def test_sync_push_re_sync_picks_up_newly_created_project(
    client: AsyncClient, db_session: AsyncSession
):
    """Regression for Codex Phase 5 Round 1 (entry 285).

    A session uploaded before its project existed should pick up
    project_id on the next re-sync once the project is created.
    Without this, every existing session captured pre-Phase-5 (or
    pre-project) stays project_id=NULL forever.
    """
    user, key = await _make_user(db_session)
    remote = f"acme/late-init-{uuid.uuid4().hex[:6]}"

    # First sync: no project exists yet.
    session_id = f"ses_{uuid.uuid4().hex[:24]}"
    blob = _make_sfs_tarball(
        git_remote=f"https://github.com/{remote}.git", session_id=session_id
    )
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=_hdrs(key),
        files={"file": ("session.tar.gz", blob, "application/gzip")},
    )
    assert resp.status_code in (200, 201), resp.text
    row = (
        await db_session.execute(select(Session).where(Session.id == session_id))
    ).scalar_one()
    assert row.project_id is None

    # NOW the user creates the project for that remote.
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="late-init",
        git_remote_normalized=remote,
        context_document="",
        owner_id=user.id,
        org_id=None,
    )
    db_session.add(project)
    await db_session.commit()

    # Re-sync: the SAME session re-uploads. Server must update
    # project_id, not leave it stale at NULL.
    resp2 = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=_hdrs(key),
        files={"file": ("session.tar.gz", blob, "application/gzip")},
    )
    assert resp2.status_code in (200, 201), resp2.text
    # Force a fresh read — the previous Session row is cached in the
    # identity map. We re-fetch via a fresh execute after expiring.
    db_session.expunge_all()
    row2 = (
        await db_session.execute(select(Session).where(Session.id == session_id))
    ).scalar_one()
    assert row2.project_id == project.id


@pytest.mark.asyncio
async def test_post_sessions_resolves_project_id_for_owner(
    client: AsyncClient, db_session: AsyncSession
):
    """Regression for Codex Phase 5 Round 2 (entry 287).

    POST /api/v1/sessions must apply the same server-side project
    resolution as /sessions/{id}/sync. Otherwise non-daemon upload
    paths leave sessions unrouted even when the caller has access.
    """
    user, key = await _make_user(db_session)
    remote = f"acme/post-{uuid.uuid4().hex[:6]}"
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="post-route",
        git_remote_normalized=remote,
        context_document="",
        owner_id=user.id,
        org_id=None,
    )
    db_session.add(project)
    await db_session.commit()

    session_id = f"ses_{uuid.uuid4().hex[:24]}"
    blob = _make_sfs_tarball(
        git_remote=f"https://github.com/{remote}.git", session_id=session_id
    )
    resp = await client.post(
        "/api/v1/sessions?source_tool=claude-code",
        headers=_hdrs(key),
        files={"file": ("session.tar.gz", blob, "application/gzip")},
    )
    assert resp.status_code in (200, 201), resp.text
    new_session_id = resp.json()["session_id"]
    row = (
        await db_session.execute(
            select(Session).where(Session.id == new_session_id)
        )
    ).scalar_one()
    assert row.project_id == project.id


@pytest.mark.asyncio
async def test_post_sessions_leaves_project_id_null_for_non_member(
    client: AsyncClient, db_session: AsyncSession
):
    """POST /sessions also gates linkage on access, same as sync_push."""
    owner, _ = await _make_user(db_session, "owner")
    outsider, outsider_key = await _make_user(db_session, "outsider")
    org = await _make_org(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=owner.id, role="admin"))
    await db_session.commit()

    remote = f"acme/private-post-{uuid.uuid4().hex[:6]}"
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="private-post",
        git_remote_normalized=remote,
        context_document="",
        owner_id=owner.id,
        org_id=org.id,
    )
    db_session.add(project)
    await db_session.commit()

    blob = _make_sfs_tarball(
        git_remote=f"https://github.com/{remote}.git",
        session_id="ses_outsider_post",
    )
    resp = await client.post(
        "/api/v1/sessions?source_tool=claude-code",
        headers=_hdrs(outsider_key),
        files={"file": ("session.tar.gz", blob, "application/gzip")},
    )
    assert resp.status_code in (200, 201), resp.text
    new_session_id = resp.json()["session_id"]
    row = (
        await db_session.execute(
            select(Session).where(Session.id == new_session_id)
        )
    ).scalar_one()
    assert row.project_id is None


def test_resolve_project_helper_emits_for_update():
    """Regression for Codex Phase 5 Round 4 (entry 291).

    Codex required SELECT ... FOR UPDATE on the Project and OrgMember
    rows the helper reads, so concurrent project deletes or membership
    removals don't slip through between resolution and the session
    write. SQLite ignores FOR UPDATE at runtime, so we assert the
    clause is present in the COMPILED SQL — that proves PG (which
    honors row locks) will block concurrent mutations.
    """
    from sqlalchemy import select as _select
    from sqlalchemy.dialects import postgresql

    from sessionfs.server.db.models import OrgMember as _OrgMember
    from sessionfs.server.db.models import Project as _Project

    proj_sql = str(
        _select(_Project)
        .where(_Project.git_remote_normalized == "x/y")
        .with_for_update()
        .compile(dialect=postgresql.dialect())
    )
    assert "FOR UPDATE" in proj_sql

    mem_sql = str(
        _select(_OrgMember)
        .where(_OrgMember.user_id == "u", _OrgMember.org_id == "o")
        .with_for_update()
        .compile(dialect=postgresql.dialect())
    )
    assert "FOR UPDATE" in mem_sql


@pytest.mark.asyncio
async def test_sync_push_handles_project_deletion_between_syncs(
    client: AsyncClient, db_session: AsyncSession
):
    """Regression for Codex Phase 5 Round 3 (entry 289).

    Codex flagged that sync_push resolved project_id in the outer
    request-scoped session, then wrote in a fresh Phase 3 session. If
    the project was deleted between resolution and write, the FK
    failure would surface as a misreported 409 PK collision.

    The Round 4 fix moves resolution inside `db2`, so the helper sees
    the same state as the write. This test stages the simplest
    observable variant: project exists at first sync → linked; project
    is hard-deleted; re-sync sees no project → project_id clears to
    NULL without surfacing as a 409.
    """
    user, key = await _make_user(db_session)
    remote = f"acme/race-{uuid.uuid4().hex[:6]}"
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="race-test",
        git_remote_normalized=remote,
        context_document="",
        owner_id=user.id,
        org_id=None,
    )
    db_session.add(project)
    await db_session.commit()
    project_id = project.id

    session_id = f"ses_{uuid.uuid4().hex[:24]}"
    blob = _make_sfs_tarball(
        git_remote=f"https://github.com/{remote}.git", session_id=session_id
    )
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=_hdrs(key),
        files={"file": ("session.tar.gz", blob, "application/gzip")},
    )
    assert resp.status_code in (200, 201), resp.text
    row = (
        await db_session.execute(select(Session).where(Session.id == session_id))
    ).scalar_one()
    assert row.project_id == project_id

    # Hard-delete the project.
    await db_session.delete(project)
    await db_session.commit()

    # Re-sync. Resolution inside db2 must see the project is gone and
    # write project_id=NULL — NOT raise an FK error that the
    # IntegrityError catch misreports as a PK collision.
    resp2 = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=_hdrs(key),
        files={"file": ("session.tar.gz", blob, "application/gzip")},
    )
    assert resp2.status_code in (200, 201), resp2.text
    # Critical: NOT a 409 misreport.
    assert "Session created by another request" not in resp2.text
    db_session.expunge_all()
    row2 = (
        await db_session.execute(select(Session).where(Session.id == session_id))
    ).scalar_one()
    assert row2.project_id is None


@pytest.mark.asyncio
async def test_sync_push_re_sync_clears_project_id_when_access_revoked(
    client: AsyncClient, db_session: AsyncSession
):
    """Regression for Codex Phase 5 Round 1 (entry 285).

    A session linked to an org project must lose its project_id on
    re-sync once the user is removed from the org. Without this, a
    stale linkage survives membership changes — the very scenario the
    server-side resolution is meant to enforce.
    """
    owner, _ = await _make_user(db_session, "owner")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=owner.id, role="admin"))
    membership = OrgMember(org_id=org.id, user_id=member.id, role="member")
    db_session.add(membership)
    await db_session.commit()

    remote = f"acme/revoke-{uuid.uuid4().hex[:6]}"
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="revoke-test",
        git_remote_normalized=remote,
        context_document="",
        owner_id=owner.id,
        org_id=org.id,
    )
    db_session.add(project)
    await db_session.commit()

    # First sync (member): session linked.
    session_id = f"ses_{uuid.uuid4().hex[:24]}"
    blob = _make_sfs_tarball(
        git_remote=f"https://github.com/{remote}.git", session_id=session_id
    )
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=_hdrs(member_key),
        files={"file": ("session.tar.gz", blob, "application/gzip")},
    )
    assert resp.status_code in (200, 201), resp.text
    row = (
        await db_session.execute(select(Session).where(Session.id == session_id))
    ).scalar_one()
    assert row.project_id == project.id

    # Member is removed from the org.
    await db_session.delete(membership)
    await db_session.commit()

    # Re-sync: the member no longer has access. Session must clear
    # project_id so a stale linkage doesn't survive.
    resp2 = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=_hdrs(member_key),
        files={"file": ("session.tar.gz", blob, "application/gzip")},
    )
    assert resp2.status_code in (200, 201), resp2.text
    # Force a fresh read — the previous Session row is cached in the
    # identity map. We re-fetch via a fresh execute after expiring.
    db_session.expunge_all()
    row2 = (
        await db_session.execute(select(Session).where(Session.id == session_id))
    ).scalar_one()
    assert row2.project_id is None
