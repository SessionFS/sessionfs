"""Integration tests for two multi-repo paper-cut fixes.

#2 (tk_9b5fd8c3e2604254): PATCH /api/v1/projects/{id} project rename.
   - happy path (owner)
   - validation: empty/whitespace → 422, null bytes → 422, HTML stripped,
     255-char cap
   - project-admin gating (non-admin → 403; org-admin → 200)
   - 404 unknown project

#3 (tk_c4e17bf662834dd1): merge auto-links the source project's repos
   into the target.
   - dry-run plan reports the repo count
   - after a real merge the source repo appears in the target's repos
     list with NO manual link-repo, as a NON-primary linked repo
     (the target keeps its own primary)
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    Organization,
    OrgMember,
    Project,
    ProjectRepo,
    User,
)


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
    verification_method: str = "legacy_backfill",
) -> ProjectRepo:
    remote = remote or project.git_remote_normalized
    r = ProjectRepo(
        id=f"repo_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        git_remote_normalized=remote,
        is_primary=is_primary,
        verified=False,
        verification_method=verification_method,
    )
    db.add(r)
    await db.commit()
    await db.refresh(r)
    return r


async def _make_user(db: AsyncSession, email: str) -> User:
    u = User(
        id=str(uuid.uuid4()),
        email=email,
        display_name=email.split("@")[0],
        tier="pro",
        email_verified=True,
        created_at=_now(),
    )
    db.add(u)
    await db.commit()
    await db.refresh(u)
    return u


# ── #2: project rename PATCH ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_rename_project_happy_path(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    project = await _make_project(db_session, owner=test_user, name="old name")

    resp = await client.patch(
        f"/api/v1/projects/{project.id}",
        json={"name": "Brand New Name"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "Brand New Name"
    assert body["id"] == project.id
    # repos list is included in the response (detail-shaped).
    assert "repos" in body

    await db_session.refresh(project)
    assert project.name == "Brand New Name"


@pytest.mark.asyncio
async def test_rename_project_strips_html_and_trims(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    project = await _make_project(db_session, owner=test_user)
    resp = await client.patch(
        f"/api/v1/projects/{project.id}",
        json={"name": "  <b>Clean</b> Name  "},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Clean Name"


@pytest.mark.asyncio
async def test_rename_project_empty_name_422(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    project = await _make_project(db_session, owner=test_user)
    resp = await client.patch(
        f"/api/v1/projects/{project.id}",
        json={"name": "   "},
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_rename_project_only_html_becomes_empty_422(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    project = await _make_project(db_session, owner=test_user)
    resp = await client.patch(
        f"/api/v1/projects/{project.id}",
        json={"name": "<script></script>"},
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_rename_project_null_bytes_422(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    project = await _make_project(db_session, owner=test_user)
    resp = await client.patch(
        f"/api/v1/projects/{project.id}",
        json={"name": "bad\x00name"},
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_rename_project_too_long_422(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    project = await _make_project(db_session, owner=test_user)
    resp = await client.patch(
        f"/api/v1/projects/{project.id}",
        json={"name": "x" * 256},
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_rename_project_non_admin_denied(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    other = await _make_user(db_session, "stranger@example.com")
    project = await _make_project(db_session, owner=other)

    resp = await client.patch(
        f"/api/v1/projects/{project.id}",
        json={"name": "Hijacked"},
        headers=auth_headers,
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_rename_project_org_admin_allowed(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """An org-admin of the project's org can rename even if not the owner."""
    owner = await _make_user(db_session, "owner@example.com")
    org_name = f"org-{uuid.uuid4().hex[:6]}"
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:16]}",
        name=org_name,
        slug=org_name,
    )
    db_session.add(org)
    await db_session.commit()
    # test_user is an ADMIN of the org; the project belongs to the org but
    # is owned by `owner`.
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="admin"))
    db_session.add(OrgMember(org_id=org.id, user_id=owner.id, role="member"))
    await db_session.commit()

    project = await _make_project(
        db_session, owner=owner, org_id=org.id, name="org project",
    )

    resp = await client.patch(
        f"/api/v1/projects/{project.id}",
        json={"name": "Renamed By Admin"},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Renamed By Admin"


@pytest.mark.asyncio
async def test_rename_project_404(
    client: AsyncClient,
    auth_headers: dict,
):
    resp = await client.patch(
        "/api/v1/projects/proj_does_not_exist",
        json={"name": "Nope"},
        headers=auth_headers,
    )
    assert resp.status_code == 404, resp.text


# ── #3: merge auto-links source repos into the target ─────────────────


@pytest.mark.asyncio
async def test_merge_dry_run_reports_repo_count(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    source = await _make_project(
        db_session, owner=test_user, remote="github.com/a/merge-src-dry",
    )
    await _add_repo(
        db_session, project=source,
        remote="github.com/a/merge-src-dry", is_primary=True,
    )
    target = await _make_project(
        db_session, owner=test_user, remote="github.com/a/merge-tgt-dry",
    )
    await _add_repo(
        db_session, project=target,
        remote="github.com/a/merge-tgt-dry", is_primary=True,
    )

    resp = await client.post(
        f"/api/v1/projects/{target.id}/merge",
        json={"source_project_id": source.id, "dry_run": True},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["dry_run"] is True
    # The plan reports the source repo count (N repos will be linked).
    assert body["stats"]["repos"] == 1


@pytest.mark.asyncio
async def test_merge_auto_links_source_repos_into_target(
    client: AsyncClient,
    auth_headers: dict,
    test_user: User,
    db_session: AsyncSession,
):
    """After a real merge, the source repo appears in the target's repos
    list with NO manual link-repo, and as a NON-primary linked repo (the
    target keeps its own primary)."""
    source = await _make_project(
        db_session, owner=test_user, remote="github.com/a/merge-src",
    )
    await _add_repo(
        db_session, project=source,
        remote="github.com/a/merge-src", is_primary=True,
    )
    target = await _make_project(
        db_session, owner=test_user, remote="github.com/a/merge-tgt",
    )
    await _add_repo(
        db_session, project=target,
        remote="github.com/a/merge-tgt", is_primary=True,
    )

    resp = await client.post(
        f"/api/v1/projects/{target.id}/merge",
        json={"source_project_id": source.id, "dry_run": False},
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["dry_run"] is False

    # Target now owns BOTH repos.
    target_repos = (
        await db_session.execute(
            select(ProjectRepo).where(ProjectRepo.project_id == target.id)
        )
    ).scalars().all()
    remotes = {r.git_remote_normalized: r for r in target_repos}
    assert "github.com/a/merge-src" in remotes
    assert "github.com/a/merge-tgt" in remotes

    # Exactly one primary, and it's the TARGET's original repo
    # (the source's ex-primary is now a NON-primary linked repo).
    primaries = [r for r in target_repos if r.is_primary]
    assert len(primaries) == 1
    assert primaries[0].git_remote_normalized == "github.com/a/merge-tgt"
    assert remotes["github.com/a/merge-src"].is_primary is False

    # Source project owns NO repos anymore (all reassigned).
    src_repos = (
        await db_session.execute(
            select(ProjectRepo).where(ProjectRepo.project_id == source.id)
        )
    ).scalars().all()
    assert len(src_repos) == 0

    # The source repo is now resolvable to the TARGET via the join table.
    from sessionfs.server.services.project_resolver import (
        resolve_project_by_remote,
    )
    resolved = await resolve_project_by_remote(
        db_session, "github.com/a/merge-src",
    )
    assert resolved is not None
    assert resolved.id == target.id
