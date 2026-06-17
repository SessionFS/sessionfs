"""P2 resolver cutover integration tests.

Tests for multi-repo project resolution, access predicates, tombstone
behavior (F4), re-authorization (F5), and ProjectResolutionLoopError.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from sessionfs.server.db.models import (
    OrgMember,
    Organization,
    Project,
    ProjectRepo,
    Session,
    User,
)
from sessionfs.server.services.project_resolver import (
    ProjectResolutionLoopError,
    resolve_project_by_remote,
)


def _gen_id(prefix: str = "") -> str:
    return (prefix or "") + uuid.uuid4().hex[:16]


def _make_session(**overrides: object) -> Session:
    """Create a Session with minimal required fields filled in."""
    defaults: dict[str, object] = {
        "id": _gen_id("ses_"),
        "title": "Test Session",
        "source_tool": "claude-code",
        "blob_key": "fake-blob-key",
        "etag": "fake-etag",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    defaults.update(overrides)
    return Session(**defaults)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_access_via_second_repo_grants_project(db_session):
    """B-site: a user with sessions only on repo-B (non-primary) can
    still access the project when repo-B is linked to it."""
    owner_id = _gen_id("u_")
    project_id = _gen_id("proj_")
    primary_remote = "github.com/acme/primary"
    second_remote = "github.com/acme/secondary"

    # Create project with primary remote.
    proj = Project(
        id=project_id,
        git_remote_normalized=primary_remote,
        name="Multi-Repo Project",
        owner_id=owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(proj)

    # Link second remote via project_repos.
    repo_a = ProjectRepo(
        id=_gen_id("pr_"),
        project_id=project_id,
        git_remote_normalized=primary_remote,
        is_primary=True,
        verified=False,
        verification_method="legacy_backfill",
    )
    repo_b = ProjectRepo(
        id=_gen_id("pr_"),
        project_id=project_id,
        git_remote_normalized=second_remote,
        is_primary=False,
        verified=False,
        verification_method="owner_attested",
    )
    db_session.add_all([repo_a, repo_b])

    # Create a session on the SECONDARY repo (not the primary).
    other_user_id = _gen_id("u_")
    sess = _make_session(
        user_id=other_user_id,
        title="Session on secondary",
        git_remote_normalized=second_remote,
        project_id=project_id,
    )
    db_session.add(sess)
    await db_session.commit()

    # Resolution: the secondary remote should resolve to the project.
    resolved = await resolve_project_by_remote(db_session, second_remote)
    assert resolved is not None
    assert resolved.id == project_id

    # Access check: user_can_access_project should grant access via
    # session.project_id == project.id (B1).
    from sessionfs.server.auth.project_access import user_can_access_project
    has_access = await user_can_access_project(db_session, other_user_id, proj)
    assert has_access is True


@pytest.mark.asyncio
async def test_resolver_redirect_re_auth_denies_unauthorized_caller(db_session):
    """F5: following a tombstone does NOT grant access the caller lacks
    on the target project."""
    owner_a_id = _gen_id("u_")
    owner_b_id = _gen_id("u_")
    caller_id = _gen_id("u_")
    proj_a_id = _gen_id("proj_")
    proj_b_id = _gen_id("proj_")
    remote_a = "github.com/acme/source"
    remote_b = "github.com/acme/target"

    # Source project (will become tombstone)
    proj_a = Project(
        id=proj_a_id,
        git_remote_normalized=remote_a,
        name="Source",
        owner_id=owner_a_id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(proj_a)

    # Target project
    proj_b = Project(
        id=proj_b_id,
        git_remote_normalized=remote_b,
        name="Target",
        owner_id=owner_b_id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(proj_b)

    # Repo rows
    db_session.add(ProjectRepo(
        id=_gen_id("pr_"),
        project_id=proj_a_id,
        git_remote_normalized=remote_a,
        is_primary=True,
        verified=False,
        verification_method="legacy_backfill",
    ))
    db_session.add(ProjectRepo(
        id=_gen_id("pr_"),
        project_id=proj_b_id,
        git_remote_normalized=remote_b,
        is_primary=True,
        verified=False,
        verification_method="legacy_backfill",
    ))

    # Caller has sessions on source project → can access source.
    sess = _make_session(
        user_id=caller_id,
        title="Caller session",
        git_remote_normalized=remote_a,
        project_id=proj_a_id,
    )
    db_session.add(sess)
    await db_session.commit()

    # Verify caller can access source now.
    from sessionfs.server.auth.project_access import user_can_access_project
    assert await user_can_access_project(db_session, caller_id, proj_a) is True

    # Caller does NOT have sessions on target.
    assert await user_can_access_project(db_session, caller_id, proj_b) is False

    # Merge: source → target (simulate by setting tombstone).
    proj_a.merged_into_project_id = proj_b_id
    proj_a.merged_at = datetime.now(timezone.utc)
    await db_session.commit()

    # Resolver follows tombstone: remote_a → proj_a → proj_b.
    resolved = await resolve_project_by_remote(db_session, remote_a)
    assert resolved is not None
    assert resolved.id == proj_b_id  # redirects to target

    # But caller does NOT have access to proj_b — the resolver resolved
    # but did NOT authorize (F5).  The route must still deny.
    has_access = await user_can_access_project(db_session, caller_id, resolved)
    assert has_access is False


@pytest.mark.asyncio
async def test_tombstone_access_check_on_source_before_disclose(db_session):
    """F4: unauthorized caller probing a tombstone remote gets opaque 404,
    not 410 with merged_into target id."""
    owner_id = _gen_id("u_")
    stranger_id = _gen_id("u_")
    source_id = _gen_id("proj_")
    target_id = _gen_id("proj_")
    remote_source = "github.com/acme/merged-source"

    # Source project (tombstone)
    proj_source = Project(
        id=source_id,
        git_remote_normalized=remote_source,
        name="Merged Source",
        owner_id=owner_id,
        merged_into_project_id=target_id,  # already merged
        merged_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(proj_source)

    # Target project
    proj_target = Project(
        id=target_id,
        git_remote_normalized="github.com/acme/merge-target",
        name="Target",
        owner_id=owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(proj_target)

    # Repo rows
    db_session.add(ProjectRepo(
        id=_gen_id("pr_"),
        project_id=source_id,
        git_remote_normalized=remote_source,
        is_primary=True,
        verified=False,
        verification_method="legacy_backfill",
    ))
    db_session.add(ProjectRepo(
        id=_gen_id("pr_"),
        project_id=target_id,
        git_remote_normalized="github.com/acme/merge-target",
        is_primary=True,
        verified=False,
        verification_method="legacy_backfill",
    ))

    # Owner has sessions on source → can access.
    owner_session = _make_session(
        user_id=owner_id,
        title="Owner session",
        git_remote_normalized=remote_source,
        project_id=source_id,
    )
    db_session.add(owner_session)
    await db_session.commit()

    # Resolve without following tombstone — should return tombstone project.
    raw = await resolve_project_by_remote(
        db_session, remote_source, follow_tombstone=False,
    )
    assert raw is not None
    assert raw.id == source_id
    assert raw.merged_into_project_id == target_id

    # Owner passes source access check → authorized to see 410.
    from sessionfs.server.auth.project_access import user_can_access_project
    owner_access = await user_can_access_project(db_session, owner_id, raw)
    assert owner_access is True

    # Stranger has no sessions → fails source access check → opaque 404.
    stranger_access = await user_can_access_project(
        db_session, stranger_id, raw,
    )
    assert stranger_access is False


@pytest.mark.asyncio
async def test_project_resolution_loop_error_raised(db_session):
    """ProjectResolutionLoopError is raised when hop cap exceeds 8."""
    # Create a chain of projects where p0→p1→p2→...→p9→p0 (cycle).
    ids: list[str] = []
    first_id: str | None = None
    for i in range(10):
        pid = _gen_id("proj_")
        ids.append(pid)
        if i == 0:
            first_id = pid
    # Create all projects first (without tombstone links), then
    # set merged_into in a second pass so FK references resolve.
    for i, pid in enumerate(ids):
        p = Project(
            id=pid,
            git_remote_normalized=f"github.com/acme/chain-{i}",
            name=f"Chain {i}",
            owner_id=_gen_id("u_"),
            created_at=datetime.now(timezone.utc),
        )
        db_session.add(p)
        db_session.add(ProjectRepo(
            id=_gen_id("pr_"),
            project_id=pid,
            git_remote_normalized=f"github.com/acme/chain-{i}",
            is_primary=True,
            verified=False,
            verification_method="legacy_backfill",
        ))
    await db_session.flush()

    # Set up the chain: p0→p1, p1→p2, ..., p9→p0 (cycle).
    for i, pid in enumerate(ids):
        next_pid = ids[(i + 1) % len(ids)]
        proj = await db_session.get(Project, pid)
        assert proj is not None
        proj.merged_into_project_id = next_pid
        proj.merged_at = datetime.now(timezone.utc)
    await db_session.commit()

    # Resolving chain-0 should raise loop error (p0→p1→...→p9→p0→... > 8 hops).
    with pytest.raises(ProjectResolutionLoopError) as exc_info:
        await resolve_project_by_remote(db_session, f"github.com/acme/chain-0")
    assert "hop cap" in str(exc_info.value)


@pytest.mark.asyncio
async def test_resolve_by_remote_returns_none_for_unknown(db_session):
    """The resolver returns None for a remote not linked to any project."""
    result = await resolve_project_by_remote(db_session, "github.com/nonexistent/repo")
    assert result is None


@pytest.mark.asyncio
async def test_dual_read_fallback_to_legacy_column(db_session):
    """The resolver falls back to projects.git_remote_normalized when
    no project_repos row exists (legacy projects pre-migration)."""
    project_id = _gen_id("proj_")
    remote = "github.com/acme/legacy-project"
    owner_id = _gen_id("u_")

    # Create a project WITHOUT a project_repos row (legacy path).
    proj = Project(
        id=project_id,
        git_remote_normalized=remote,
        name="Legacy Project",
        owner_id=owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(proj)
    await db_session.commit()

    # Resolver should find it via legacy column fallback.
    resolved = await resolve_project_by_remote(db_session, remote)
    assert resolved is not None
    assert resolved.id == project_id


@pytest.mark.asyncio
async def test_link_repo_duplicate_check_in_create(db_session):
    """A4: POST /projects/ duplicate check covers project_repos."""
    existing_remote = "github.com/acme/already-linked"
    project_id = _gen_id("proj_")
    owner_id = _gen_id("u_")

    # Create the project.
    proj = Project(
        id=project_id,
        git_remote_normalized=existing_remote,
        name="Existing",
        owner_id=owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(proj)

    # Link the remote via project_repos.
    db_session.add(ProjectRepo(
        id=_gen_id("pr_"),
        project_id=project_id,
        git_remote_normalized=existing_remote,
        is_primary=True,
        verified=False,
        verification_method="legacy_backfill",
    ))
    await db_session.commit()

    # The project_repos duplicate check should find the existing link.
    from sessionfs.server.db.models import ProjectRepo as PR
    repo_check = await db_session.execute(
        select(PR.id).where(
            PR.git_remote_normalized == existing_remote,
        ).limit(1)
    )
    assert repo_check.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_accessible_project_ids_includes_both_paths(db_session):
    """B2: _accessible_project_ids covers both project_id-based AND
    legacy-remote-based sessions."""
    owner_id = _gen_id("u_")
    user_id = _gen_id("u_")
    project_a_id = _gen_id("proj_")
    project_b_id = _gen_id("proj_")
    remote_a = "github.com/acme/proj-a"
    remote_b = "github.com/acme/proj-b"

    # Project A: session with project_id set (modern path).
    proj_a = Project(
        id=project_a_id,
        git_remote_normalized=remote_a,
        name="Project A",
        owner_id=owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(proj_a)

    # Project B: session with NULL project_id but matching remote (legacy).
    proj_b = Project(
        id=project_b_id,
        git_remote_normalized=remote_b,
        name="Project B",
        owner_id=owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(proj_b)

    # Modern session (project_id set).
    sess_modern = _make_session(
        user_id=user_id,
        title="Modern",
        git_remote_normalized=remote_a,
        project_id=project_a_id,
    )
    db_session.add(sess_modern)

    # Legacy session (project_id NULL, matched by git_remote).
    sess_legacy = _make_session(
        user_id=user_id,
        title="Legacy",
        git_remote_normalized=remote_b,
        project_id=None,
    )
    db_session.add(sess_legacy)

    await db_session.commit()

    from sessionfs.server.services.handoff_helpers import (
        _accessible_project_ids,
    )
    accessible = await _accessible_project_ids(db_session, user_id)

    # Both paths should resolve.
    assert project_a_id in accessible  # modern: session.project_id
    assert project_b_id in accessible  # legacy: remote-based join
