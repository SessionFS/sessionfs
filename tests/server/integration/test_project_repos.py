"""P3 tests — link / unlink / list repos API + access control + displacement.

Covers:
- Link happy path (owner_attested + github_app mocked)
- Link denied (non-admin)
- 409 already-linked (opaque F3 vs full)
- Cross-org rejected (with verified-reclaim carve-out)
- Verified displaces unverified → repo_reclaimed
- Verified-vs-verified 409
- is_primary atomic swap
- Unlink last-repo 422
- Unlink primary auto-promotes
- List access-gated
- ProjectResponse.repos on detail only
- CONCURRENT-LINK race (is_primary — one winner)
- REGRESSION: existing project detail includes repos
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AdminAction,
    Organization,
    OrgMember,
    Project,
    ProjectRepo,
    User,
)


# ── helpers ───────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _make_project(
    db: AsyncSession,
    *,
    owner: User,
    name: str = "test",
    remote: str | None = None,
    org_id: str | None = None,
) -> Project:
    remote = remote or f"github.com/{uuid.uuid4().hex[:8]}/repo"
    p = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=name,
        git_remote_normalized=remote,
        context_document="",
        owner_id=owner.id,
        org_id=org_id,
    )
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _add_repo(
    db: AsyncSession,
    *,
    project: Project,
    remote: str | None = None,
    is_primary: bool = False,
    verified: bool = False,
    verification_method: str = "legacy_backfill",
    user_id: str | None = None,
) -> ProjectRepo:
    remote = remote or project.git_remote_normalized
    r = ProjectRepo(
        id=f"repo_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        git_remote_normalized=remote,
        is_primary=is_primary,
        verified=verified,
        verification_method=verification_method,
        added_by_user_id=user_id,
    )
    db.add(r)
    await db.commit()
    await db.refresh(r)
    return r


async def _make_org(
    db: AsyncSession,
    *,
    owner: User,
    name: str = "test-org",
) -> Organization:
    org_name = f"{name}-{uuid.uuid4().hex[:6]}"
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:16]}",
        name=org_name,
        slug=org_name.lower().replace(" ", "-"),
    )
    db.add(org)
    await db.commit()
    await db.refresh(org)
    return org


async def _add_org_member(
    db: AsyncSession,
    *,
    org: Organization,
    user: User,
    role: str = "member",
) -> OrgMember:
    m = OrgMember(
        org_id=org.id,
        user_id=user.id,
        role=role,
    )
    db.add(m)
    await db.commit()
    await db.refresh(m)
    return m


# ── link happy path ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_link_repo_owner_attested_happy_path(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """Owner links a repo without GitHub App → owner_attested, verified=false."""
    project = await _make_project(db_session, owner=test_user)

    # Also backfill the primary repo row (mimics migration 049).
    await _add_repo(
        db_session,
        project=project,
        remote=project.git_remote_normalized,
        is_primary=True,
        verified=False,
        verification_method="legacy_backfill",
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/repos",
        json={"git_remote": "github.com/acme/backend", "is_primary": False},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["git_remote_normalized"] == "github.com/acme/backend"
    assert body["verified"] is False
    assert body["verification_method"] == "owner_attested"
    assert body["is_primary"] is False
    assert "id" in body
    assert body["project_id"] == project.id


@pytest.mark.asyncio
async def test_link_repo_github_app_verified(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """GitHub App verification succeeds → verified=true, github_app."""
    project = await _make_project(db_session, owner=test_user)
    await _add_repo(
        db_session,
        project=project,
        remote=project.git_remote_normalized,
        is_primary=True,
        verified=False,
        verification_method="legacy_backfill",
    )

    with patch(
        "sessionfs.server.github_app.verify_repo_ownership",
        new=AsyncMock(return_value=(True, "github_app", "github", "123456")),
    ):
        resp = await client.post(
            f"/api/v1/projects/{project.id}/repos",
            json={"git_remote": "github.com/acme/backend"},
            headers=auth_headers,
        )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["verified"] is True
    assert body["verification_method"] == "github_app"
    assert body["provider"] == "github"
    assert body["provider_repo_id"] == "123456"


# ── link denied ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_link_repo_denied_non_admin(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    """A non-admin user cannot link a repo to someone else's project."""
    # Create project owned by another user.
    other = User(
        id=str(uuid.uuid4()),
        email="other@example.com",
        display_name="Other",
        tier="pro",
        email_verified=True,
        created_at=_now(),
    )
    db_session.add(other)
    await db_session.commit()

    project = await _make_project(db_session, owner=other)
    await _add_repo(
        db_session,
        project=project,
        remote=project.git_remote_normalized,
        is_primary=True,
        verified=False,
        verification_method="legacy_backfill",
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/repos",
        json={"git_remote": "github.com/acme/backend"},
        headers=auth_headers,
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_link_repo_hijack_denied_forged_session(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """A non-admin user with a captured session on the project's remote
    cannot link repos — the forged-session access predicate does NOT
    grant admin standing (F1 anti-hijack)."""
    # Create project owned by another user.
    other = User(
        id=str(uuid.uuid4()),
        email="owner@example.com",
        display_name="Owner",
        tier="pro",
        email_verified=True,
        created_at=_now(),
    )
    db_session.add(other)
    await db_session.commit()

    project = await _make_project(
        db_session, owner=other, remote="github.com/acme/target-repo",
    )
    await _add_repo(
        db_session,
        project=project,
        remote="github.com/acme/target-repo",
        is_primary=True,
        verified=False,
        verification_method="legacy_backfill",
    )

    # Create a captured session for test_user on the project's remote
    # (forges the session-based access predicate). This grants READ
    # access via user_can_access_project predicate #3 but does NOT
    # make test_user a project admin — only user_is_project_admin
    # gates link_repo (F1).
    from sessionfs.server.db.models import Session
    sess = Session(
        id=f"ses_{uuid.uuid4().hex[:16]}",
        user_id=test_user.id,
        title="Forged session",
        source_tool="claude-code",
        blob_key=f"blobs/test/{uuid.uuid4().hex}",
        blob_size_bytes=0,
        etag="abc",
        git_remote_normalized="github.com/acme/target-repo",
    )
    db_session.add(sess)
    await db_session.commit()

    # test_user is NOT the owner and NOT an org admin, so
    # user_is_project_admin returns False → 403.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/repos",
        json={"git_remote": "github.com/acme/new-repo"},
        headers=auth_headers,
    )
    assert resp.status_code == 403, resp.text


# ── 409 repo_already_linked (F3) ───────────────────────────────────────


@pytest.mark.asyncio
async def test_link_repo_409_cross_org_opaque(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """Org-A user probing an org-B remote → opaque 409 (F3)."""
    # Create org-A project (owner = test_user)
    project_a = await _make_project(
        db_session, owner=test_user, remote="github.com/org-a/repo",
    )
    await _add_repo(
        db_session, project=project_a,
        remote="github.com/org-a/repo", is_primary=True,
    )

    # Create org-B project (owner = other user)
    other = User(
        id=str(uuid.uuid4()),
        email="other@example.com",
        display_name="Other",
        tier="pro",
        email_verified=True,
        created_at=_now(),
    )
    db_session.add(other)
    await db_session.commit()

    project_b = await _make_project(
        db_session, owner=other, remote="github.com/org-b/repo",
    )
    await _add_repo(
        db_session, project=project_b,
        remote="github.com/org-b/repo", is_primary=True,
    )

    # Link org-b's remote to project_a — should be 409 (already linked).
    # test_user cannot access project_b → opaque 409.
    resp = await client.post(
        f"/api/v1/projects/{project_a.id}/repos",
        json={"git_remote": "github.com/org-b/repo"},
        headers=auth_headers,
    )
    assert resp.status_code == 409, resp.text
    err = resp.json().get("error", {})
    assert err.get("code") == "repo_already_linked"
    # Opaque — no existing_project_id.
    assert "existing_project_id" not in err.get("details", {})


@pytest.mark.asyncio
async def test_link_repo_409_same_org_full_response(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """Member of same org → full 409 with existing_project_id (F3)."""
    org = await _make_org(db_session, owner=test_user)
    await _add_org_member(db_session, org=org, user=test_user, role="admin")

    project_a = await _make_project(
        db_session, owner=test_user, remote="github.com/acme/repo-a",
        org_id=org.id,
    )
    await _add_repo(
        db_session, project=project_a,
        remote="github.com/acme/repo-a", is_primary=True,
    )

    project_b = await _make_project(
        db_session, owner=test_user, remote="github.com/acme/repo-b",
        org_id=org.id,
    )
    await _add_repo(
        db_session, project=project_b,
        remote="github.com/acme/repo-b", is_primary=True,
    )

    # Link repo-b's remote to project_a — already linked.
    # test_user is member of the owning org → full 409.
    resp = await client.post(
        f"/api/v1/projects/{project_a.id}/repos",
        json={"git_remote": "github.com/acme/repo-b"},
        headers=auth_headers,
    )
    assert resp.status_code == 409, resp.text
    err = resp.json().get("error", {})
    assert err.get("code") == "repo_already_linked"
    assert err.get("details", {}).get("existing_project_id") == project_b.id


# ── cross-org rejected ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_link_repo_cross_org_rejected(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """Cross-org linking is rejected (except verified reclaim)."""
    org_a = await _make_org(db_session, owner=test_user)
    await _add_org_member(db_session, org=org_a, user=test_user, role="admin")

    project_a = await _make_project(
        db_session, owner=test_user, remote="github.com/acme/frontend",
        org_id=org_a.id,
    )
    await _add_repo(
        db_session, project=project_a,
        remote="github.com/acme/frontend", is_primary=True,
    )

    # Link a completely new remote — should succeed (not cross-org).
    resp = await client.post(
        f"/api/v1/projects/{project_a.id}/repos",
        json={"git_remote": "github.com/acme/new-repo"},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text


# ── verified displaces unverified (F1 displacement) ───────────────────


@pytest.mark.asyncio
async def test_verified_displaces_unverified_holder_repo_reclaimed(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """Verified claim displaces unverified holder → holder repo_reclaimed."""
    # Create holder project (unverified, owner_attested).
    other = User(
        id=str(uuid.uuid4()),
        email="squatter@example.com",
        display_name="Squatter",
        tier="pro",
        email_verified=True,
        created_at=_now(),
    )
    db_session.add(other)
    await db_session.commit()

    holder_project = await _make_project(
        db_session, owner=other, remote="github.com/acme/squatted",
    )
    await _add_repo(
        db_session,
        project=holder_project,
        remote="github.com/acme/squatted",
        is_primary=True,
        verified=False,
        verification_method="owner_attested",
        user_id=other.id,
    )

    # Create claimant project (will verify via github_app).
    claimant = await _make_project(
        db_session, owner=test_user, remote="github.com/test/claimant",
    )
    await _add_repo(
        db_session,
        project=claimant,
        remote="github.com/test/claimant",
        is_primary=True,
        verified=False,
        verification_method="legacy_backfill",
    )

    with patch(
        "sessionfs.server.github_app.verify_repo_ownership",
        new=AsyncMock(return_value=(True, "github_app", "github", "99999")),
    ):
        resp = await client.post(
            f"/api/v1/projects/{claimant.id}/repos",
            json={"git_remote": "github.com/acme/squatted"},
            headers=auth_headers,
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["verified"] is True
    assert body["git_remote_normalized"] == "github.com/acme/squatted"
    assert body["project_id"] == claimant.id

    # Holder project should now be repo_reclaimed.
    await db_session.refresh(holder_project)
    assert holder_project.repo_reclaimed_at is not None

    # Holder's data is intact (NOT imported into claimant).
    assert holder_project.context_document == ""
    assert holder_project.owner_id == other.id


@pytest.mark.asyncio
async def test_verified_displaces_unverified_holder_has_other_repos(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """Holder has multiple repos; displaced primary → oldest promoted."""
    other = User(
        id=str(uuid.uuid4()),
        email="multi@example.com",
        display_name="Multi",
        tier="pro",
        email_verified=True,
        created_at=_now(),
    )
    db_session.add(other)
    await db_session.commit()

    holder = await _make_project(
        db_session, owner=other, remote="github.com/holder/primary",
    )
    # Primary (unverified)
    await _add_repo(
        db_session, project=holder,
        remote="github.com/holder/primary", is_primary=True,
        verified=False, verification_method="owner_attested",
    )
    # Secondary (also unverified)
    await _add_repo(
        db_session, project=holder,
        remote="github.com/holder/secondary", is_primary=False,
        verified=False, verification_method="owner_attested",
    )

    claimant = await _make_project(
        db_session, owner=test_user, remote="github.com/test/claimant",
    )
    await _add_repo(
        db_session, project=claimant,
        remote="github.com/test/claimant", is_primary=True,
    )

    with patch(
        "sessionfs.server.github_app.verify_repo_ownership",
        new=AsyncMock(return_value=(True, "github_app", "github", "88888")),
    ):
        resp = await client.post(
            f"/api/v1/projects/{claimant.id}/repos",
            json={"git_remote": "github.com/holder/primary"},
            headers=auth_headers,
        )

    assert resp.status_code == 201, resp.text

    # Holder still has one repo (secondary), should now be primary.
    await db_session.refresh(holder)
    assert holder.repo_reclaimed_at is None  # not orphaned
    remaining = (
        await db_session.execute(
            select(ProjectRepo).where(ProjectRepo.project_id == holder.id)
        )
    ).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].is_primary == True  # noqa: E712
    assert remaining[0].git_remote_normalized == "github.com/holder/secondary"
    # Project primary remote refreshed.
    assert holder.git_remote_normalized == "github.com/holder/secondary"


@pytest.mark.asyncio
async def test_verified_vs_verified_409(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """Both verified → 409 genuine conflict."""
    other = User(
        id=str(uuid.uuid4()),
        email="real-owner@example.com",
        display_name="Real",
        tier="pro",
        email_verified=True,
        created_at=_now(),
    )
    db_session.add(other)
    await db_session.commit()

    holder = await _make_project(
        db_session, owner=other, remote="github.com/acme/genuine",
    )
    await _add_repo(
        db_session, project=holder,
        remote="github.com/acme/genuine", is_primary=True,
        verified=True, verification_method="github_app",
    )

    claimant = await _make_project(
        db_session, owner=test_user, remote="github.com/test/claimant",
    )
    await _add_repo(
        db_session, project=claimant,
        remote="github.com/test/claimant", is_primary=True,
    )

    # Our verification ALSO passes — but holder is already verified.
    with patch(
        "sessionfs.server.github_app.verify_repo_ownership",
        new=AsyncMock(return_value=(True, "github_app", "github", "77777")),
    ):
        resp = await client.post(
            f"/api/v1/projects/{claimant.id}/repos",
            json={"git_remote": "github.com/acme/genuine"},
            headers=auth_headers,
        )

    assert resp.status_code == 409, resp.text
    err = resp.json().get("error", {})
    assert "verified" in err.get("message", "").lower()


# ── is_primary swap ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_link_repo_is_primary_atomic_swap(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """Linking with is_primary=True demotes the existing primary."""
    project = await _make_project(
        db_session, owner=test_user, remote="github.com/acme/old-primary",
    )
    await _add_repo(
        db_session, project=project,
        remote="github.com/acme/old-primary", is_primary=True,
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/repos",
        json={"git_remote": "github.com/acme/new-primary", "is_primary": True},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["is_primary"] is True
    assert body["git_remote_normalized"] == "github.com/acme/new-primary"

    # Old primary is now demoted.
    await db_session.refresh(project)
    assert project.git_remote_normalized == "github.com/acme/new-primary"

    old = (
        await db_session.execute(
            select(ProjectRepo).where(
                ProjectRepo.project_id == project.id,
                ProjectRepo.git_remote_normalized == "github.com/acme/old-primary",
            )
        )
    ).scalar_one_or_none()
    assert old is not None
    assert old.is_primary is False


# ── unlink ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unlink_repo_last_repo_422(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """Cannot unlink the last repo of an active project."""
    project = await _make_project(
        db_session, owner=test_user, remote="github.com/acme/solo",
    )
    repo = await _add_repo(
        db_session, project=project,
        remote="github.com/acme/solo", is_primary=True,
    )

    resp = await client.delete(
        f"/api/v1/projects/{project.id}/repos/{repo.id}",
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text
    err = resp.json().get("error", {})
    assert err.get("code") == "last_repo"


@pytest.mark.asyncio
async def test_unlink_repo_primary_auto_promotes(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """Unlinking the primary promotes the oldest remaining repo."""
    project = await _make_project(
        db_session, owner=test_user, remote="github.com/acme/primary",
    )
    primary = await _add_repo(
        db_session, project=project,
        remote="github.com/acme/primary", is_primary=True,
    )
    secondary = await _add_repo(
        db_session, project=project,
        remote="github.com/acme/secondary", is_primary=False,
    )

    resp = await client.delete(
        f"/api/v1/projects/{project.id}/repos/{primary.id}",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text

    # Secondary is now primary.
    await db_session.refresh(project)
    assert project.git_remote_normalized == "github.com/acme/secondary"

    # Expire identity-map cache — the server session committed
    # is_primary=True on secondary in a different transaction.
    await db_session.refresh(secondary)
    assert secondary.is_primary == True  # noqa: E712

    remaining = (
        await db_session.execute(
            select(ProjectRepo).where(ProjectRepo.project_id == project.id)
        )
    ).scalars().all()
    assert len(remaining) == 1
    assert remaining[0].id == secondary.id
    assert remaining[0].is_primary == True  # noqa: E712


@pytest.mark.asyncio
async def test_unlink_repo_non_admin_denied(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    """Non-admin cannot unlink repos."""
    other = User(
        id=str(uuid.uuid4()),
        email="other@example.com",
        display_name="Other",
        tier="pro",
        email_verified=True,
        created_at=_now(),
    )
    db_session.add(other)
    await db_session.commit()

    project = await _make_project(db_session, owner=other)
    repo = await _add_repo(db_session, project=project)

    resp = await client.delete(
        f"/api/v1/projects/{project.id}/repos/{repo.id}",
        headers=auth_headers,
    )
    assert resp.status_code == 403, resp.text


# ── list repos ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_repos_access_gated(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """Only authorized users can list repos."""
    project = await _make_project(
        db_session, owner=test_user, remote="github.com/acme/repo",
    )
    await _add_repo(
        db_session, project=project,
        remote="github.com/acme/repo", is_primary=True,
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/repos",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    repos = resp.json()
    assert isinstance(repos, list)
    assert len(repos) >= 1
    assert repos[0]["git_remote_normalized"] == "github.com/acme/repo"
    assert repos[0]["is_primary"] is True


@pytest.mark.asyncio
async def test_list_repos_denied_for_stranger(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    """A stranger cannot list another project's repos."""
    other = User(
        id=str(uuid.uuid4()),
        email="other@example.com",
        display_name="Other",
        tier="pro",
        email_verified=True,
        created_at=_now(),
    )
    db_session.add(other)
    await db_session.commit()

    project = await _make_project(db_session, owner=other)
    await _add_repo(db_session, project=project)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/repos",
        headers=auth_headers,
    )
    assert resp.status_code == 403, resp.text


# ── ProjectResponse.repos on detail ────────────────────────────────────


@pytest.mark.asyncio
async def test_project_detail_includes_repos(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """GET /projects/{remote} detail response includes repos list."""
    project = await _make_project(
        db_session, owner=test_user, remote="github.com/acme/detail-test",
    )
    await _add_repo(
        db_session, project=project,
        remote="github.com/acme/detail-test", is_primary=True,
    )

    resp = await client.get(
        f"/api/v1/projects/github.com/acme/detail-test",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body.get("repos") is not None
    assert isinstance(body["repos"], list)
    assert len(body["repos"]) >= 1
    assert body["repos"][0]["git_remote_normalized"] == "github.com/acme/detail-test"


@pytest.mark.asyncio
async def test_project_list_omits_repos(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """GET /projects (list) response does NOT include repos (N+1 avoidance)."""
    project = await _make_project(
        db_session, owner=test_user, remote="github.com/acme/list-test",
    )
    await _add_repo(
        db_session, project=project,
        remote="github.com/acme/list-test", is_primary=True,
    )

    resp = await client.get(
        "/api/v1/projects/",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    projects = resp.json()
    assert isinstance(projects, list)
    for p in projects:
        # repos is None (omitted) on list responses.
        assert p.get("repos") is None, f"Project {p['id']} had repos in list"


# ── concurrent race — is_primary ──────────────────────────────────────


@pytest.mark.asyncio
async def test_link_repo_is_primary_race_two_winners(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """Two links with is_primary=True: the partial index enforces one primary."""
    project = await _make_project(
        db_session, owner=test_user, remote="github.com/acme/race",
    )
    await _add_repo(
        db_session, project=project,
        remote="github.com/acme/race", is_primary=True,
    )

    # Link two repos, both requesting is_primary.
    # Only one can win — the second will succeed but demote the first.
    resp1 = await client.post(
        f"/api/v1/projects/{project.id}/repos",
        json={"git_remote": "github.com/acme/candidate-a", "is_primary": True},
        headers=auth_headers,
    )
    assert resp1.status_code == 201, resp1.text

    resp2 = await client.post(
        f"/api/v1/projects/{project.id}/repos",
        json={"git_remote": "github.com/acme/candidate-b", "is_primary": True},
        headers=auth_headers,
    )
    assert resp2.status_code == 201, resp2.text

    # Only one primary.
    repos = (
        await db_session.execute(
            select(ProjectRepo).where(
                ProjectRepo.project_id == project.id,
                ProjectRepo.is_primary == True,  # noqa: E712
            )
        )
    ).scalars().all()
    assert len(repos) == 1
    # The last one linked is primary (demotes first).
    assert repos[0].git_remote_normalized == "github.com/acme/candidate-b"


# ── FIX 3 (tk_b3fc4a81446544ff): --primary DEMOTES, never unlinks ──────


@pytest.mark.asyncio
async def test_link_primary_demotes_old_primary_keeps_row_resolvable(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """Acceptance: link A primary → link B primary → BOTH rows linked,
    B primary, A demoted (is_primary=False) and STILL resolvable by its
    own remote. The old primary must NOT be unlinked/dropped.
    """
    from sessionfs.server.services.project_resolver import (
        resolve_project_by_remote,
    )

    project = await _make_project(
        db_session, owner=test_user, remote="github.com/acme/seed",
    )
    await _add_repo(
        db_session, project=project,
        remote="github.com/acme/seed", is_primary=True,
    )

    # Link A as primary (demotes the seed).
    resp_a = await client.post(
        f"/api/v1/projects/{project.id}/repos",
        json={"git_remote": "github.com/acme/repo-a", "is_primary": True},
        headers=auth_headers,
    )
    assert resp_a.status_code == 201, resp_a.text
    assert resp_a.json()["is_primary"] is True

    # Link B as primary (demotes A — but KEEPS A's row).
    resp_b = await client.post(
        f"/api/v1/projects/{project.id}/repos",
        json={"git_remote": "github.com/acme/repo-b", "is_primary": True},
        headers=auth_headers,
    )
    assert resp_b.status_code == 201, resp_b.text
    assert resp_b.json()["is_primary"] is True

    # All three rows still linked to the project.
    repos = (
        await db_session.execute(
            select(ProjectRepo).where(ProjectRepo.project_id == project.id)
        )
    ).scalars().all()
    remotes = {r.git_remote_normalized: r for r in repos}
    assert set(remotes) == {
        "github.com/acme/seed",
        "github.com/acme/repo-a",
        "github.com/acme/repo-b",
    }

    # Exactly one primary, and it is B.
    primaries = [r for r in repos if r.is_primary]
    assert len(primaries) == 1
    assert primaries[0].git_remote_normalized == "github.com/acme/repo-b"

    # A was DEMOTED (not unlinked) — its row survives, is_primary=False.
    assert remotes["github.com/acme/repo-a"].is_primary is False

    # A is STILL resolvable by its own remote (row kept, not dropped).
    resolved = await resolve_project_by_remote(
        db_session, "github.com/acme/repo-a",
    )
    assert resolved is not None
    assert resolved.id == project.id

    # Project's display remote tracks the new primary B.
    await db_session.refresh(project)
    assert project.git_remote_normalized == "github.com/acme/repo-b"


# ── list shape: bare JSON array (tk_e1bd970236bc42fa server side) ──────


@pytest.mark.asyncio
async def test_list_repos_returns_bare_json_array(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """GET /repos returns a BARE JSON array (not a wrapped object).

    The CLI helper relies on this shape — confirms the response_model
    contract (list[ProjectRepoResponse]) the CLI fix tolerates.
    """
    project = await _make_project(
        db_session, owner=test_user, remote="github.com/acme/array-shape",
    )
    await _add_repo(
        db_session, project=project,
        remote="github.com/acme/array-shape", is_primary=True,
    )

    resp = await client.get(
        f"/api/v1/projects/{project.id}/repos",
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Top-level JSON value is an array, not a dict.
    assert isinstance(body, list)
    assert all(isinstance(item, dict) for item in body)
    assert body[0]["git_remote_normalized"] == "github.com/acme/array-shape"


# ── 400 bad remote ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_link_repo_bad_remote_400(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """Malformed remote URL → 400."""
    project = await _make_project(db_session, owner=test_user)
    await _add_repo(
        db_session, project=project,
        remote=project.git_remote_normalized, is_primary=True,
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/repos",
        json={"git_remote": ""},
        headers=auth_headers,
    )
    assert resp.status_code == 400, resp.text


# ── MCP/API/CLI parity smoke ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_repo_crud_round_trip(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """Full round-trip: link → list → unlink."""
    project = await _make_project(
        db_session, owner=test_user, remote="github.com/acme/roundtrip",
    )
    await _add_repo(
        db_session, project=project,
        remote="github.com/acme/roundtrip", is_primary=True,
    )

    # Link a second repo.
    link_resp = await client.post(
        f"/api/v1/projects/{project.id}/repos",
        json={"git_remote": "github.com/acme/second"},
        headers=auth_headers,
    )
    assert link_resp.status_code == 201
    new_repo_id = link_resp.json()["id"]

    # List should show both.
    list_resp = await client.get(
        f"/api/v1/projects/{project.id}/repos",
        headers=auth_headers,
    )
    assert list_resp.status_code == 200
    repos = list_resp.json()
    assert len(repos) == 2
    remotes = {r["git_remote_normalized"] for r in repos}
    assert "github.com/acme/roundtrip" in remotes
    assert "github.com/acme/second" in remotes

    # Unlink the second repo.
    unlink_resp = await client.delete(
        f"/api/v1/projects/{project.id}/repos/{new_repo_id}",
        headers=auth_headers,
    )
    assert unlink_resp.status_code == 200

    # List should show only the original.
    list_resp2 = await client.get(
        f"/api/v1/projects/{project.id}/repos",
        headers=auth_headers,
    )
    assert list_resp2.status_code == 200
    assert len(list_resp2.json()) == 1


# ── displacement audit — unverified-vs-any 409 ─────────────────────────


@pytest.mark.asyncio
async def test_unverified_cannot_displace_any(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """An unverified (owner_attested) caller cannot displace ANY holder."""
    other = User(
        id=str(uuid.uuid4()),
        email="holder@example.com",
        display_name="Holder",
        tier="pro",
        email_verified=True,
        created_at=_now(),
    )
    db_session.add(other)
    await db_session.commit()

    holder = await _make_project(
        db_session, owner=other, remote="github.com/acme/holder",
    )
    await _add_repo(
        db_session, project=holder,
        remote="github.com/acme/holder", is_primary=True,
        verified=False, verification_method="owner_attested",
    )

    claimant = await _make_project(
        db_session, owner=test_user, remote="github.com/test/claimant",
    )
    await _add_repo(
        db_session, project=claimant,
        remote="github.com/test/claimant", is_primary=True,
    )

    # No GitHub App verification (owner_attested fallback → verified=false).
    # verified=false cannot displace anyone.
    resp = await client.post(
        f"/api/v1/projects/{claimant.id}/repos",
        json={"git_remote": "github.com/acme/holder"},
        headers=auth_headers,
    )
    assert resp.status_code == 409, resp.text


# ── repo_reclaimed revival (Codex MED) ────────────────────────────────


@pytest.mark.asyncio
async def test_repo_reclaimed_revival_on_link(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """A zero-repo or repo_reclaimed project revives on first link:
    is_primary is forced True, git_remote_normalized is set,
    and repo_reclaimed_at is cleared."""
    # Create a project that has had its repos displaced (simulated).
    project = await _make_project(
        db_session, owner=test_user, remote="github.com/acme/dead-project",
    )
    # No ProjectRepo rows — simulates displacement-to-zero.
    # Set repo_reclaimed_at directly.
    from sqlalchemy import update
    await db_session.execute(
        update(Project)
        .where(Project.id == project.id)
        .values(repo_reclaimed_at=_now())
    )
    await db_session.commit()

    # Link a new repo — should revive the project.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/repos",
        json={"git_remote": "github.com/acme/new-life", "is_primary": False},
        headers=auth_headers,
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    # is_primary is forced True despite the request saying False.
    assert body["is_primary"] is True
    assert body["git_remote_normalized"] == "github.com/acme/new-life"

    # Project state is revived.
    await db_session.refresh(project)
    assert project.repo_reclaimed_at is None
    assert project.git_remote_normalized == "github.com/acme/new-life"

    # Verify the repo row exists.
    from sqlalchemy import select as sa_select
    repos = (await db_session.execute(
        sa_select(ProjectRepo).where(ProjectRepo.project_id == project.id)
    )).scalars().all()
    assert len(repos) == 1
    assert repos[0].is_primary is True


# ── displacement audit (Codex LOW + Shield L1) ────────────────────────


@pytest.mark.asyncio
async def test_displacement_writes_admin_action(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """Verified displacement writes a durable AdminAction row
    with actor snapshot (L4)."""
    # Holder project (unverified).
    other = User(
        id=str(uuid.uuid4()),
        email="holder@example.com",
        display_name="Holder",
        tier="pro",
        email_verified=True,
        created_at=_now(),
    )
    db_session.add(other)
    await db_session.commit()

    holder_project = await _make_project(
        db_session, owner=other, remote="github.com/acme/displace-audit",
    )
    await _add_repo(
        db_session, project=holder_project,
        remote="github.com/acme/displace-audit", is_primary=True,
        verified=False, verification_method="owner_attested",
        user_id=other.id,
    )

    # Claimant project (will verify via github_app).
    claimant = await _make_project(
        db_session, owner=test_user, remote="github.com/test/displace-claimant",
    )
    await _add_repo(
        db_session, project=claimant,
        remote="github.com/test/displace-claimant", is_primary=True,
        verified=False, verification_method="legacy_backfill",
    )

    with patch(
        "sessionfs.server.github_app.verify_repo_ownership",
        new=AsyncMock(return_value=(True, "github_app", "github", "11111")),
    ):
        resp = await client.post(
            f"/api/v1/projects/{claimant.id}/repos",
            json={"git_remote": "github.com/acme/displace-audit"},
            headers=auth_headers,
        )

    assert resp.status_code == 201, resp.text

    # AdminAction row exists for the displacement.
    result = await db_session.execute(
        select(AdminAction).where(
            AdminAction.action == "repo_displaced",
            AdminAction.target_id == holder_project.id,
        )
    )
    audit = result.scalar_one_or_none()
    assert audit is not None, "Displacement should write an AdminAction"
    assert audit.admin_id == test_user.id

    # Details include L4 actor snapshot.
    import json
    details = json.loads(audit.details)
    assert details["actor_user_id"] == test_user.id
    assert details["actor_email"] == test_user.email
    assert details["displaced_remote"] == "github.com/acme/displace-audit"
    assert details["claimant_project_id"] == claimant.id
    assert details["old_verified"] is False


# ── rate limit 429 (Shield MED F6) ────────────────────────────────────


@pytest.mark.asyncio
async def test_link_repo_rate_limit_429(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """Exceeding the per-user link rate limit returns 429."""
    project = await _make_project(
        db_session, owner=test_user, remote="github.com/acme/ratelimit",
    )
    await _add_repo(
        db_session, project=project,
        remote="github.com/acme/ratelimit", is_primary=True,
        verified=False, verification_method="legacy_backfill",
    )

    # Patch the limiter to always deny.
    from sessionfs.server.routes.projects import _link_unlink_limiter
    with patch.object(
        _link_unlink_limiter, "is_allowed", return_value=False,
    ):
        resp = await client.post(
            f"/api/v1/projects/{project.id}/repos",
            json={"git_remote": "github.com/acme/unique-remote"},
            headers=auth_headers,
        )
    assert resp.status_code == 429, resp.text
    err = resp.json().get("error", {})
    assert err.get("code") == "rate_limit"


# ── durable denial audit (Codex R2 MED fix) ────────────────────────────


@pytest.mark.asyncio
async def test_merge_cross_org_denial_audit_durable(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """Cross-org merge denial writes AdminAction through a FRESH session
    that commits before the raise, so the audit row survives the failed
    request's transaction rollback."""
    org_a = await _make_org(db_session, owner=test_user, name="org-a")
    org_b = await _make_org(db_session, owner=test_user, name="org-b")

    source = await _make_project(
        db_session, owner=test_user, remote="github.com/a/denial-src",
        org_id=org_a.id,
    )
    target = await _make_project(
        db_session, owner=test_user, remote="github.com/a/denial-tgt",
        org_id=org_b.id,
    )

    resp = await client.post(
        f"/api/v1/projects/{target.id}/merge",
        json={
            "source_project_id": source.id,
            "dry_run": False,
            "persona_policy": "rename",
        },
        headers=auth_headers,
    )

    assert resp.status_code == 400, resp.text
    assert "Cross-org" in resp.json().get("detail", resp.text)

    # Open a FRESH session and verify the AdminAction row EXISTS.
    # If the audit were written via db.add+flush on the request session,
    # it would be lost on rollback — this assertion proves durability.
    from sessionfs.server.db.engine import _session_factory

    assert _session_factory is not None, "test fixture should set session factory"
    async with _session_factory() as fresh_db:
        result = await fresh_db.execute(
            select(AdminAction).where(
                AdminAction.action == "merge_denied_cross_org",
                AdminAction.target_id == source.id,
            )
        )
        audit = result.scalar_one_or_none()

    assert audit is not None, (
        "Denial AdminAction must survive the failed request's rollback — "
        "it was committed in a separate session"
    )
    assert audit.admin_id == test_user.id
    assert audit.target_type == "project"

    import json
    details = json.loads(audit.details)
    assert details["target_project_id"] == target.id
    assert details["source_org_id"] == org_a.id
    assert details["target_org_id"] == org_b.id
    assert details["actor_user_id"] == test_user.id
    assert details["actor_email"] == test_user.email
    assert details["reason"] == "Cross-org merges are not supported"
