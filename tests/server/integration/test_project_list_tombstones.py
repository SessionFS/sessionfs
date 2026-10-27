"""Merge tombstones stay out of project listings (GitHub #111).

A merged source keeps its owner, org and readability (direct GETs answer
410 with ``merged_into``), but ``GET /api/v1/projects`` and the session
helper's ``list_projects`` must not offer it next to its live target.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.auth.project_access import (
    accessible_project_ids_subquery,
    list_accessible_projects,
    user_can_access_project,
)
from sessionfs.server.db.models import (
    ApiKey,
    OrgMember,
    Organization,
    Project,
    ProjectGroup,
    ProjectGroupAccess,
    ProjectGroupMember,
    Session,
    User,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _user(db: AsyncSession, name: str) -> tuple[User, dict]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name,
        tier="enterprise",
        email_verified=True,
        created_at=_now(),
    )
    db.add(user)
    await db.flush()
    raw = generate_api_key()
    db.add(ApiKey(
        id=str(uuid.uuid4()), user_id=user.id, key_hash=hash_api_key(raw),
        name=f"{name}-key", created_at=_now(),
    ))
    await db.commit()
    return user, {"Authorization": f"Bearer {raw}"}


async def _org(db: AsyncSession, *, restrict: bool = False) -> Organization:
    settings = (
        json.dumps({"general": {"restrict_project_visibility": True}}) if restrict else "{}"
    )
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}", name="Acme",
        slug=f"acme-{uuid.uuid4().hex[:8]}", tier="enterprise",
        seats_limit=20, settings=settings,
    )
    db.add(org)
    await db.commit()
    return org


async def _project(
    db: AsyncSession, owner: User, name: str, org: Organization | None = None
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=name,
        git_remote_normalized=f"github.com/acme/{uuid.uuid4().hex[:8]}",
        owner_id=owner.id,
        org_id=org.id if org is not None else None,
    )
    db.add(project)
    await db.commit()
    return project


async def _merge(db: AsyncSession, source: Project, target: Project) -> None:
    await db.execute(
        update(Project)
        .where(Project.id == source.id)
        .values(merged_into_project_id=target.id, merged_at=_now())
    )
    await db.commit()
    await db.refresh(source)


async def _list_ids(client: AsyncClient, headers: dict) -> list[str]:
    resp = await client.get("/api/v1/projects/", headers=headers)
    assert resp.status_code == 200, resp.text
    return [p["id"] for p in resp.json()]


@pytest.mark.asyncio
async def test_owner_list_omits_merged_source(client: AsyncClient, db_session: AsyncSession):
    owner, headers = await _user(db_session, "owner")
    target = await _project(db_session, owner, "EMMS")
    source = await _project(db_session, owner, "EMMS")
    other = await _project(db_session, owner, "Other")
    await _merge(db_session, source, target)

    ids = await _list_ids(client, headers)
    assert target.id in ids and other.id in ids
    assert source.id not in ids

    # The tombstone is still readable: the direct GET redirects with 410.
    resp = await client.get(f"/api/v1/projects/{source.id}", headers=headers)
    assert resp.status_code == 410, resp.text
    assert resp.json()["error"]["details"]["merged_into"] == target.id


@pytest.mark.asyncio
async def test_every_access_path_omits_merged_source(db_session: AsyncSession):
    """Org member, group grantee and captured-session reader all lose the
    tombstone from the listing but keep read access to it."""
    owner, _ = await _user(db_session, "owner")
    member, _ = await _user(db_session, "member")
    grantee, _ = await _user(db_session, "grantee")
    capturer, _ = await _user(db_session, "capturer")
    org = await _org(db_session)
    # Restricted, so the grantee's membership alone admits nothing: only the
    # group grant does.
    other_org = await _org(db_session, restrict=True)
    for user, role in ((owner, "owner"), (member, "member")):
        db_session.add(OrgMember(org_id=org.id, user_id=user.id, role=role))
    db_session.add(OrgMember(org_id=other_org.id, user_id=grantee.id, role="member"))
    await db_session.commit()

    target = await _project(db_session, owner, "Live", org)
    source = await _project(db_session, owner, "Merged", org)
    # Group path: a project in another org, granted through a group.
    g_target = await _project(db_session, owner, "G live", other_org)
    g_source = await _project(db_session, owner, "G merged", other_org)
    group = ProjectGroup(
        id=f"pg_{uuid.uuid4().hex[:12]}", org_id=other_org.id, slug="g", name="G",
        created_by_user_id=owner.id, created_at=_now(), updated_at=_now(),
    )
    db_session.add(group)
    await db_session.flush()
    db_session.add(ProjectGroupMember(
        group_id=group.id, user_id=grantee.id, role="reader",
        granted_by_user_id=owner.id, created_at=_now(),
    ))
    for p in (g_target, g_source):
        db_session.add(ProjectGroupAccess(
            group_id=group.id, project_id=p.id, added_by_user_id=owner.id, created_at=_now(),
        ))
    # Captured-session path: personal projects reached via a session.
    c_target = await _project(db_session, owner, "C live")
    c_source = await _project(db_session, owner, "C merged")
    for p in (c_target, c_source):
        db_session.add(Session(
            id=f"ses_{uuid.uuid4().hex[:16]}", user_id=capturer.id,
            title="s", source_tool="claude-code", project_id=p.id,
            git_remote_normalized=p.git_remote_normalized,
            created_at=_now(), updated_at=_now(),
            blob_key=f"k/{uuid.uuid4().hex}", etag=uuid.uuid4().hex,
        ))
    await db_session.commit()

    await _merge(db_session, source, target)
    await _merge(db_session, g_source, g_target)
    await _merge(db_session, c_source, c_target)

    for user, live, merged in (
        (member, target, source),
        (grantee, g_target, g_source),
        (capturer, c_target, c_source),
    ):
        listed = {p.id for p in await list_accessible_projects(db_session, user.id)}
        assert live.id in listed, user.display_name
        assert merged.id not in listed, user.display_name
        # Read access (audit history, 410 redirects) is unchanged.
        readable = set(
            (await db_session.execute(
                await accessible_project_ids_subquery(db_session, user.id)
            )).scalars().all()
        )
        assert merged.id in readable, user.display_name
        assert await user_can_access_project(db_session, user.id, merged)
