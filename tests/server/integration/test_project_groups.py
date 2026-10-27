"""Project groups + restricted project visibility (GitHub #101, phase 1).

Covers the access predicates (``user_can_access_project``,
``user_can_edit_project``, ``user_group_role``, ``user_is_project_admin``,
``list_accessible_projects``) and the phase-1 REST surface (group CRUD,
member grants, project placement, and the ``restrict_project_visibility``
org setting). The CLI / MCP / dashboard surfaces are out of scope here.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.auth.project_access import (
    list_accessible_projects,
    user_can_access_project,
    user_can_edit_project,
    user_group_role,
    user_is_project_admin,
)
from sessionfs.server.db.models import (
    ApiKey,
    OrgAuditEvent,
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


async def _user(db: AsyncSession, name: str, tier: str = "enterprise") -> tuple[User, dict]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name,
        tier=tier,
        email_verified=True,
        created_at=_now(),
    )
    db.add(user)
    await db.flush()
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"{name}-key",
            created_at=_now(),
        )
    )
    await db.commit()
    return user, {"Authorization": f"Bearer {raw}"}


async def _org(db: AsyncSession, *, tier: str = "team", restrict: bool = False) -> Organization:
    settings = (
        json.dumps({"general": {"restrict_project_visibility": True}})
        if restrict
        else "{}"
    )
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name=f"Org {uuid.uuid4().hex[:4]}",
        slug=f"org-{uuid.uuid4().hex[:8]}",
        tier=tier,
        seats_limit=20,
        settings=settings,
    )
    db.add(org)
    await db.commit()
    return org


async def _member(
    db: AsyncSession, org: Organization, user: User, role: str = "member"
) -> None:
    db.add(OrgMember(org_id=org.id, user_id=user.id, role=role))
    await db.commit()


async def _project(
    db: AsyncSession, owner: User, *, org: Organization | None = None
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="Shared repo",
        git_remote_normalized=f"github.com/acme/{uuid.uuid4().hex[:8]}",
        context_document="# original",
        owner_id=owner.id,
        org_id=org.id if org is not None else None,
    )
    db.add(project)
    await db.commit()
    return project


async def _group(
    db: AsyncSession, org: Organization, creator: User, name: str = "Frontend"
) -> ProjectGroup:
    group = ProjectGroup(
        id=f"pg_{uuid.uuid4().hex[:12]}",
        org_id=org.id,
        slug=name.lower().replace(" ", "-"),
        name=name,
        created_by_user_id=creator.id,
        created_at=_now(),
        updated_at=_now(),
    )
    db.add(group)
    await db.commit()
    return group


async def _grant(db: AsyncSession, group: ProjectGroup, user: User, role: str) -> None:
    db.add(
        ProjectGroupMember(
            group_id=group.id,
            user_id=user.id,
            role=role,
            granted_by_user_id=user.id,
            created_at=_now(),
        )
    )
    await db.commit()


async def _place(db: AsyncSession, group: ProjectGroup, project: Project) -> None:
    db.add(
        ProjectGroupAccess(
            group_id=group.id,
            project_id=project.id,
            added_by_user_id=project.owner_id,
            created_at=_now(),
        )
    )
    await db.commit()


async def _capture_session(db: AsyncSession, user: User, project: Project) -> None:
    db.add(
        Session(
            id=f"ses_{uuid.uuid4().hex[:16]}",
            user_id=user.id,
            title="synced",
            tags="[]",
            source_tool="claude-code",
            blob_key="x",
            blob_size_bytes=0,
            etag="x",
            project_id=project.id,
            created_at=_now(),
            updated_at=_now(),
            uploaded_at=_now(),
        )
    )
    await db.commit()


# ---------------------------------------------------------------------------
# Access predicates under restricted visibility
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restricted_org_member_loses_read_and_edit(db_session: AsyncSession):
    owner, _ = await _user(db_session, "owner")
    member, _ = await _user(db_session, "member")
    outsider, _ = await _user(db_session, "outsider")
    org = await _org(db_session, restrict=True)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, member, role="member")
    project = await _project(db_session, owner, org=org)

    # Owner always keeps both.
    assert await user_can_access_project(db_session, owner.id, project)
    assert await user_can_edit_project(db_session, owner.id, project)

    # A plain member is no longer admitted by membership alone.
    assert not await user_can_access_project(db_session, member.id, project)
    assert not await user_can_edit_project(db_session, member.id, project)
    # A captured session grants nothing in a restricted org (GitHub #101).
    await _capture_session(db_session, member, project)
    assert not await user_can_access_project(db_session, member.id, project)
    assert not await user_can_edit_project(db_session, member.id, project)
    # A stranger with no standing at all is denied.
    assert not await user_can_access_project(db_session, outsider.id, project)


@pytest.mark.asyncio
async def test_restricted_org_group_grants_admit_read_and_edit(db_session: AsyncSession):
    owner, _ = await _user(db_session, "owner")
    reader, _ = await _user(db_session, "reader")
    editor, _ = await _user(db_session, "editor")
    admin, _ = await _user(db_session, "admin")
    org = await _org(db_session, restrict=True)
    for u in (owner, reader, editor, admin):
        await _member(db_session, org, u, role="owner" if u is owner else "member")
    project = await _project(db_session, owner, org=org)
    group = await _group(db_session, org, owner)
    await _place(db_session, group, project)
    await _grant(db_session, group, reader, "reader")
    await _grant(db_session, group, editor, "editor")
    await _grant(db_session, group, admin, "admin")

    assert await user_can_access_project(db_session, reader.id, project)
    assert not await user_can_edit_project(db_session, reader.id, project)

    assert await user_can_access_project(db_session, editor.id, project)
    assert await user_can_edit_project(db_session, editor.id, project)

    assert await user_can_access_project(db_session, admin.id, project)
    assert await user_can_edit_project(db_session, admin.id, project)
    # A group admin grant never confers project-ADMIN standing.
    assert not await user_is_project_admin(db_session, admin.id, project)
    assert await user_is_project_admin(db_session, owner.id, project)


@pytest.mark.asyncio
async def test_assignable_users_includes_restricted_org_group_grantee(
    client: AsyncClient, db_session: AsyncSession
):
    """F11 — in a restricted org, a group grant confers read standing, so the
    grantee is assignable while a plain member without a grant is not."""
    owner, owner_h = await _user(db_session, "owner")
    grantee, _ = await _user(db_session, "grantee")
    plain, _ = await _user(db_session, "plain")
    org = await _org(db_session, restrict=True)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, grantee, role="member")
    await _member(db_session, org, plain, role="member")
    project = await _project(db_session, owner, org=org)
    group = await _group(db_session, org, owner)
    await _place(db_session, group, project)
    await _grant(db_session, group, grantee, "reader")

    resp = await client.get(
        f"/api/v1/projects/{project.id}/assignable-users", headers=owner_h
    )
    assert resp.status_code == 200, resp.text
    ids = [u["id"] for u in resp.json()["users"]]
    assert owner.id in ids
    assert grantee.id in ids          # group grant confers read standing
    assert plain.id not in ids        # no grant in a restricted org


@pytest.mark.asyncio
async def test_restricted_org_org_admin_keeps_full_standing(db_session: AsyncSession):
    owner, _ = await _user(db_session, "owner")
    admin, _ = await _user(db_session, "admin")
    member, _ = await _user(db_session, "member")
    org = await _org(db_session, restrict=True)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, admin, role="admin")
    await _member(db_session, org, member, role="member")
    project = await _project(db_session, owner, org=org)

    assert await user_can_access_project(db_session, admin.id, project)
    assert await user_can_edit_project(db_session, admin.id, project)
    assert await user_is_project_admin(db_session, admin.id, project)
    # Org admins have admin standing on every group in the org.
    group = await _group(db_session, org, owner)
    assert await user_group_role(db_session, admin.id, group) == "admin"


@pytest.mark.asyncio
async def test_unrestricted_org_membership_unchanged(db_session: AsyncSession):
    owner, _ = await _user(db_session, "owner")
    member, _ = await _user(db_session, "member")
    org = await _org(db_session, restrict=False)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, member, role="member")
    project = await _project(db_session, owner, org=org)

    assert await user_can_access_project(db_session, member.id, project)
    assert await user_can_edit_project(db_session, member.id, project)


@pytest.mark.asyncio
async def test_list_accessible_projects_respects_restriction(db_session: AsyncSession):
    owner, _ = await _user(db_session, "owner")
    member, _ = await _user(db_session, "member")
    org = await _org(db_session, restrict=True)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, member, role="member")

    visible = await _project(db_session, owner, org=org)   # granted via group
    hidden = await _project(db_session, owner, org=org)    # no grant
    group = await _group(db_session, org, owner)
    await _place(db_session, group, visible)
    await _grant(db_session, group, member, "reader")

    member_sees = {p.id for p in await list_accessible_projects(db_session, member.id)}
    assert visible.id in member_sees
    assert hidden.id not in member_sees

    # The owner sees both.
    owner_sees = {p.id for p in await list_accessible_projects(db_session, owner.id)}
    assert {visible.id, hidden.id} <= owner_sees


# ---------------------------------------------------------------------------
# REST surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_crud_and_roles(client: AsyncClient, db_session: AsyncSession):
    owner, owner_h = await _user(db_session, "owner")
    member, member_h = await _user(db_session, "member")
    admin, admin_h = await _user(db_session, "admin")
    stranger, stranger_h = await _user(db_session, "stranger")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, member, role="member")
    await _member(db_session, org, admin, role="admin")

    # Non-member gets 404 (the org's existence never leaks).
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/groups",
        headers=stranger_h,
        json={"name": "Nope"},
    )
    assert resp.status_code == 404, resp.text

    # Plain member cannot create (org admin only).
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/groups",
        headers=member_h,
        json={"name": "Nope"},
    )
    assert resp.status_code == 403, resp.text

    # Org admin creates.
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/groups",
        headers=admin_h,
        json={"name": "Frontend", "description": "UI projects"},
    )
    assert resp.status_code == 201, resp.text
    group = resp.json()
    assert group["slug"] == "frontend"
    assert group["my_role"] == "admin"
    assert group["members"] == []

    # A plain member without a grant sees no groups and 404s on detail.
    resp = await client.get(f"/api/v1/orgs/{org.id}/groups", headers=member_h)
    assert resp.status_code == 200, resp.text
    assert resp.json() == []
    resp = await client.get(
        f"/api/v1/orgs/{org.id}/groups/{group['id']}", headers=member_h
    )
    assert resp.status_code == 404, resp.text
    # Once granted, the member lists and reads it.
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/groups/{group['id']}/members/{member.id}",
        headers=admin_h,
        json={"role": "reader"},
    )
    assert resp.status_code == 200, resp.text
    resp = await client.get(f"/api/v1/orgs/{org.id}/groups", headers=member_h)
    assert [g["id"] for g in resp.json()] == [group["id"]]
    assert resp.json()[0]["my_role"] == "reader"
    resp = await client.get(
        f"/api/v1/orgs/{org.id}/groups/{group['id']}", headers=member_h
    )
    assert resp.status_code == 200, resp.text
    # Non-members 404 on list and detail.
    resp = await client.get(f"/api/v1/orgs/{org.id}/groups", headers=stranger_h)
    assert resp.status_code == 404, resp.text
    resp = await client.get(
        f"/api/v1/orgs/{org.id}/groups/{group['id']}", headers=stranger_h
    )
    assert resp.status_code == 404, resp.text

    # A duplicate slug 409s.
    resp = await client.post(
        f"/api/v1/orgs/{org.id}/groups",
        headers=admin_h,
        json={"name": "Frontend Two", "slug": "frontend"},
    )
    assert resp.status_code == 409, resp.text

    # The owner may rename; a plain member may not.
    resp = await client.patch(
        f"/api/v1/orgs/{org.id}/groups/{group['id']}",
        headers=member_h,
        json={"name": "Hijacked"},
    )
    assert resp.status_code == 403, resp.text
    resp = await client.patch(
        f"/api/v1/orgs/{org.id}/groups/{group['id']}",
        headers=owner_h,
        json={"name": "Frontend Eng"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["name"] == "Frontend Eng"

    # Soft delete is org-admin only; the group leaves the live list.
    resp = await client.delete(
        f"/api/v1/orgs/{org.id}/groups/{group['id']}", headers=owner_h
    )
    assert resp.status_code == 204, resp.text
    resp = await client.get(f"/api/v1/orgs/{org.id}/groups", headers=member_h)
    assert resp.json() == []


@pytest.mark.asyncio
async def test_member_grant_revoke_and_non_member_guard(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_h = await _user(db_session, "owner")
    member, member_h = await _user(db_session, "member")
    editor, editor_h = await _user(db_session, "editor")
    stranger, _ = await _user(db_session, "stranger")
    org = await _org(db_session)
    for u, role in ((owner, "owner"), (member, "member"), (editor, "member")):
        await _member(db_session, org, u, role=role)
    group = await _group(db_session, org, owner)

    # Granting a non-member is a 422.
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/groups/{group.id}/members/{stranger.id}",
        headers=owner_h,
        json={"role": "reader"},
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "not_org_member"

    # A plain member without a grant can't even see the group.
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/groups/{group.id}/members/{member.id}",
        headers=member_h,
        json={"role": "reader"},
    )
    assert resp.status_code == 404, resp.text
    # An editor-granted member sees it but cannot grant (group admin only).
    await _grant(db_session, group, editor, "editor")
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/groups/{group.id}/members/{member.id}",
        headers=editor_h,
        json={"role": "reader"},
    )
    assert resp.status_code == 403, resp.text

    # The owner grants and then updates a role.
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/groups/{group.id}/members/{member.id}",
        headers=owner_h,
        json={"role": "reader"},
    )
    assert resp.status_code == 200, resp.text
    assert {m["user_id"]: m["role"] for m in resp.json()["members"]} == {
        member.id: "reader",
        editor.id: "editor",
    }

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/groups/{group.id}/members/{member.id}",
        headers=owner_h,
        json={"role": "editor"},
    )
    assert {m["user_id"]: m["role"] for m in resp.json()["members"]} == {
        member.id: "editor",
        editor.id: "editor",
    }

    # Revoke.
    resp = await client.delete(
        f"/api/v1/orgs/{org.id}/groups/{group.id}/members/{member.id}",
        headers=owner_h,
    )
    assert resp.status_code == 200, resp.text
    assert [m["user_id"] for m in resp.json()["members"]] == [editor.id]


@pytest.mark.asyncio
async def test_project_placement_same_org_guard(client: AsyncClient, db_session: AsyncSession):
    owner, owner_h = await _user(db_session, "owner")
    member, _ = await _user(db_session, "member")
    org = await _org(db_session)
    other_org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, member, role="member")
    group = await _group(db_session, org, owner)
    in_org = await _project(db_session, owner, org=org)
    out_of_org = await _project(db_session, owner, org=other_org)

    # Cross-org project is refused.
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/groups/{group.id}/projects/{out_of_org.id}",
        headers=owner_h,
    )
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "project_not_in_org"

    # Same-org project joins.
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/groups/{group.id}/projects/{in_org.id}",
        headers=owner_h,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["projects"] == [in_org.id]

    # Project-scoped read surfaces the group.
    resp = await client.get(
        f"/api/v1/projects/{in_org.id}/groups", headers=owner_h
    )
    assert resp.status_code == 200, resp.text
    assert [g["id"] for g in resp.json()] == [group.id]

    # Remove.
    resp = await client.delete(
        f"/api/v1/orgs/{org.id}/groups/{group.id}/projects/{in_org.id}",
        headers=owner_h,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["projects"] == []


@pytest.mark.asyncio
async def test_me_groups_lists_grants_and_admin_orgs(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_h = await _user(db_session, "owner")
    member, member_h = await _user(db_session, "member")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, member, role="member")
    group = await _group(db_session, org, owner)
    await _grant(db_session, group, member, "reader")

    # A granted member sees the group via /me/groups.
    resp = await client.get("/api/v1/me/groups", headers=member_h)
    assert resp.status_code == 200, resp.text
    assert [g["id"] for g in resp.json()] == [group.id]
    assert resp.json()[0]["my_role"] == "reader"

    # An org owner sees every group in their org via /me/groups.
    resp = await client.get("/api/v1/me/groups", headers=owner_h)
    assert [g["id"] for g in resp.json()] == [group.id]
    assert resp.json()[0]["my_role"] == "admin"


@pytest.mark.asyncio
async def test_restrict_visibility_toggle_gated_and_audited(
    client: AsyncClient, db_session: AsyncSession
):
    admin, admin_h = await _user(db_session, "admin")
    pro_admin, pro_admin_h = await _user(db_session, "pro-admin")
    team_org = await _org(db_session, tier="team")
    pro_org = await _org(db_session, tier="pro")
    await _member(db_session, team_org, admin, role="admin")
    await _member(db_session, pro_org, pro_admin, role="admin")

    # A pro org can't turn it on (project_groups requires Team+).
    resp = await client.put(
        f"/api/v1/orgs/{pro_org.id}/settings",
        headers=pro_admin_h,
        json={"restrict_project_visibility": True},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["details"]["error"] == "upgrade_required"

    # A team org can, and it writes an audit row.
    resp = await client.put(
        f"/api/v1/orgs/{team_org.id}/settings",
        headers=admin_h,
        json={"restrict_project_visibility": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["restrict_project_visibility"] is True

    audit = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_id == team_org.id,
                OrgAuditEvent.event_type == "org_settings_restrict_visibility_changed",
            )
        )
    ).scalars().one()
    assert json.loads(audit.after) == {"restrict_project_visibility": True}

    # Turning it off is always allowed and clears the flag.
    resp = await client.put(
        f"/api/v1/orgs/{team_org.id}/settings",
        headers=admin_h,
        json={"restrict_project_visibility": False},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["restrict_project_visibility"] is False


@pytest.mark.asyncio
async def test_restricted_org_end_to_end_visibility(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_h = await _user(db_session, "owner")
    member, member_h = await _user(db_session, "member")
    org = await _org(db_session, restrict=True)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, member, role="member")
    visible = await _project(db_session, owner, org=org)
    hidden = await _project(db_session, owner, org=org)
    group = await _group(db_session, org, owner)
    await _place(db_session, group, visible)
    await _grant(db_session, group, member, "reader")

    # The member sees the granted project in the list, not the ungranted one.
    rows = (await client.get("/api/v1/projects/", headers=member_h)).json()
    ids = {p["id"] for p in rows}
    assert visible.id in ids
    assert hidden.id not in ids

    # Project detail: granted → readable with can_edit False; ungranted → 404.
    resp = await client.get(f"/api/v1/projects/{visible.id}", headers=member_h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["can_edit"] is False
    resp = await client.get(f"/api/v1/projects/{hidden.id}", headers=member_h)
    assert resp.status_code == 403, resp.text


# ---------------------------------------------------------------------------
# Review follow-ups (GitHub #101)
# ---------------------------------------------------------------------------


async def _events(db: AsyncSession, org_id: str, event_type: str) -> list[OrgAuditEvent]:
    return list(
        (
            await db.execute(
                select(OrgAuditEvent)
                .where(
                    OrgAuditEvent.org_id == org_id,
                    OrgAuditEvent.event_type == event_type,
                )
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )


async def _access_rows(db: AsyncSession, **where) -> list[ProjectGroupAccess]:
    stmt = select(ProjectGroupAccess).execution_options(populate_existing=True)
    for k, v in where.items():
        stmt = stmt.where(getattr(ProjectGroupAccess, k) == v)
    return list((await db.execute(stmt)).scalars().all())


async def _grant_rows(db: AsyncSession, **where) -> list[ProjectGroupMember]:
    stmt = select(ProjectGroupMember).execution_options(populate_existing=True)
    for k, v in where.items():
        stmt = stmt.where(getattr(ProjectGroupMember, k) == v)
    return list((await db.execute(stmt)).scalars().all())


@pytest.mark.asyncio
async def test_add_project_requires_edit_standing_for_group_admins(
    client: AsyncClient, db_session: AsyncSession
):
    """H2 — a group admin who isn't an org admin can only add projects they
    can edit; an unreadable project is a 404 (existence never leaks)."""
    owner, owner_h = await _user(db_session, "owner")
    gadmin, gadmin_h = await _user(db_session, "gadmin")
    org = await _org(db_session, restrict=True)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, gadmin, role="member")
    group = await _group(db_session, org, owner, name="Mine")
    await _grant(db_session, group, gadmin, "admin")

    hidden = await _project(db_session, owner, org=org)      # no standing
    read_only = await _project(db_session, owner, org=org)   # reader elsewhere
    editable = await _project(db_session, owner, org=org)    # editor elsewhere
    other = await _group(db_session, org, owner, name="Other")
    await _place(db_session, other, read_only)
    await _place(db_session, other, editable)
    await _grant(db_session, other, gadmin, "reader")
    other2 = await _group(db_session, org, owner, name="Other Two")
    await _place(db_session, other2, editable)
    await _grant(db_session, other2, gadmin, "editor")

    base = f"/api/v1/orgs/{org.id}/groups/{group.id}/projects"
    resp = await client.put(f"{base}/{hidden.id}", headers=gadmin_h)
    assert resp.status_code == 404, resp.text
    resp = await client.put(f"{base}/proj_does_not_exist", headers=gadmin_h)
    assert resp.status_code == 404, resp.text
    resp = await client.put(f"{base}/{read_only.id}", headers=gadmin_h)
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "project_edit_forbidden"
    resp = await client.put(f"{base}/{editable.id}", headers=gadmin_h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["projects"] == [editable.id]

    # The org owner needs no project standing beyond org admin.
    resp = await client.put(f"{base}/{hidden.id}", headers=owner_h)
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
async def test_group_feature_gate_only_on_create_grant_and_add(
    client: AsyncClient, db_session: AsyncSession
):
    """M4 — after a downgrade, create/grant/add are refused but reads and
    cleanup (rename, revoke, remove project, delete) keep working."""
    owner, owner_h = await _user(db_session, "owner")
    member, _ = await _user(db_session, "member")
    org = await _org(db_session, tier="pro")  # plan without project_groups
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, member, role="member")
    project = await _project(db_session, owner, org=org)
    group = await _group(db_session, org, owner)
    await _grant(db_session, group, member, "reader")
    await _place(db_session, group, project)
    base = f"/api/v1/orgs/{org.id}/groups"

    for resp in (
        await client.post(base, headers=owner_h, json={"name": "New"}),
        await client.put(
            f"{base}/{group.id}/members/{owner.id}", headers=owner_h, json={"role": "admin"}
        ),
        await client.put(f"{base}/{group.id}/projects/{project.id}", headers=owner_h),
    ):
        assert resp.status_code == 403, resp.text
        assert resp.json()["error"]["details"]["error"] == "upgrade_required"

    assert (await client.get(base, headers=owner_h)).status_code == 200
    assert (await client.get(f"{base}/{group.id}", headers=owner_h)).status_code == 200
    resp = await client.patch(f"{base}/{group.id}", headers=owner_h, json={"name": "Renamed"})
    assert resp.status_code == 200, resp.text
    resp = await client.delete(f"{base}/{group.id}/members/{member.id}", headers=owner_h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["members"] == []
    resp = await client.delete(f"{base}/{group.id}/projects/{project.id}", headers=owner_h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["projects"] == []
    resp = await client.delete(f"{base}/{group.id}", headers=owner_h)
    assert resp.status_code == 204, resp.text


@pytest.mark.asyncio
async def test_update_validates_like_create(client: AsyncClient, db_session: AsyncSession):
    """L4 — PATCH validates slug pattern and name length like create."""
    owner, owner_h = await _user(db_session, "owner")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    group = await _group(db_session, org, owner)
    url = f"/api/v1/orgs/{org.id}/groups/{group.id}"
    for body in ({"name": ""}, {"name": "x" * 256}, {"slug": "Bad Slug!"}, {"slug": "-lead"}):
        resp = await client.patch(url, headers=owner_h, json=body)
        assert resp.status_code == 422, (body, resp.text)
    resp = await client.patch(url, headers=owner_h, json={"slug": "Good-Slug"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["slug"] == "good-slug"


@pytest.mark.asyncio
async def test_toggle_gate_only_on_off_to_on(client: AsyncClient, db_session: AsyncSession):
    """L5 — re-saving an already-on restriction after a downgrade is allowed;
    turning it on is gated."""
    admin, admin_h = await _user(db_session, "admin")
    org = await _org(db_session, tier="pro", restrict=True)
    await _member(db_session, org, admin, role="admin")
    url = f"/api/v1/orgs/{org.id}/settings"
    resp = await client.put(url, headers=admin_h, json={"restrict_project_visibility": True})
    assert resp.status_code == 200, resp.text
    assert resp.json()["restrict_project_visibility"] is True
    resp = await client.put(url, headers=admin_h, json={"restrict_project_visibility": False})
    assert resp.status_code == 200, resp.text
    resp = await client.put(url, headers=admin_h, json={"restrict_project_visibility": True})
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_corrupt_settings_fail_closed(client: AsyncClient, db_session: AsyncSession):
    """L6 — unreadable org settings count as restricted; owners and admins
    keep working, and settings still read (and save) cleanly."""
    owner, owner_h = await _user(db_session, "owner")
    admin, admin_h = await _user(db_session, "admin")
    member, member_h = await _user(db_session, "member")
    org = await _org(db_session)
    org.settings = "{not json"
    await db_session.commit()
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, admin, role="admin")
    await _member(db_session, org, member, role="member")
    project = await _project(db_session, owner, org=org)

    assert not await user_can_access_project(db_session, member.id, project)
    assert await user_can_access_project(db_session, admin.id, project)
    assert await user_can_edit_project(db_session, admin.id, project)
    assert (await client.get(f"/api/v1/projects/{project.id}", headers=owner_h)).status_code == 200
    assert (await client.get(f"/api/v1/projects/{project.id}", headers=admin_h)).status_code == 200
    assert (await client.get(f"/api/v1/projects/{project.id}", headers=member_h)).status_code == 403

    resp = await client.get(f"/api/v1/orgs/{org.id}/settings", headers=member_h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["restrict_project_visibility"] is True
    # A save that doesn't address the flag keeps the org restricted.
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/settings", headers=admin_h, json={"kb_retention_days": 30}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["restrict_project_visibility"] is True
    assert not await user_can_access_project(db_session, member.id, project)


@pytest.mark.asyncio
async def test_soft_deleted_group_and_orphan_grants_grant_nothing(db_session: AsyncSession):
    owner, _ = await _user(db_session, "owner")
    member, _ = await _user(db_session, "member")
    gone, _ = await _user(db_session, "gone")
    org = await _org(db_session, restrict=True)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, member, role="member")
    project = await _project(db_session, owner, org=org)
    group = await _group(db_session, org, owner)
    await _place(db_session, group, project)
    await _grant(db_session, group, member, "editor")
    # An orphan grant: the grantee is not (or no longer) an org member.
    await _grant(db_session, group, gone, "editor")

    assert await user_can_edit_project(db_session, member.id, project)
    assert not await user_can_access_project(db_session, gone.id, project)
    assert project.id not in {p.id for p in await list_accessible_projects(db_session, gone.id)}
    assert await user_group_role(db_session, gone.id, group) is None

    group.deleted_at = _now()
    await db_session.commit()
    assert not await user_can_access_project(db_session, member.id, project)
    assert not await user_can_edit_project(db_session, member.id, project)
    assert project.id not in {p.id for p in await list_accessible_projects(db_session, member.id)}


@pytest.mark.asyncio
async def test_member_removal_revokes_grants_with_audit(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_h = await _user(db_session, "owner")
    member, _ = await _user(db_session, "member")
    org = await _org(db_session, restrict=True)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, member, role="member")
    project = await _project(db_session, owner, org=org)
    group = await _group(db_session, org, owner)
    await _place(db_session, group, project)
    await _grant(db_session, group, member, "editor")

    resp = await client.delete(f"/api/v1/orgs/{org.id}/members/{member.id}", headers=owner_h)
    assert resp.status_code == 200, resp.text
    assert await _grant_rows(db_session, user_id=member.id) == []
    revoked = await _events(db_session, org.id, "project_group_member_revoked")
    assert len(revoked) == 1
    assert json.loads(revoked[0].after)["reason"] == "member_removed"
    assert not await user_can_access_project(db_session, member.id, project)


@pytest.mark.asyncio
async def test_admin_deactivation_revokes_grants_with_audit(
    client: AsyncClient, db_session: AsyncSession
):
    platform, platform_h = await _user(db_session, "platform", tier="admin")
    owner, _ = await _user(db_session, "owner")
    member, _ = await _user(db_session, "member")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, member, role="member")
    group = await _group(db_session, org, owner)
    await _grant(db_session, group, member, "reader")

    resp = await client.delete(f"/api/v1/admin/users/{member.id}", headers=platform_h)
    assert resp.status_code == 204, resp.text
    assert await _grant_rows(db_session, user_id=member.id) == []
    revoked = await _events(db_session, org.id, "project_group_member_revoked")
    assert [json.loads(e.after)["reason"] for e in revoked] == ["user_deactivated"]


@pytest.mark.asyncio
async def test_transfer_out_removes_memberships_with_audit(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_h = await _user(db_session, "owner")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    project = await _project(db_session, owner, org=org)
    g1 = await _group(db_session, org, owner, name="One")
    g2 = await _group(db_session, org, owner, name="Two")
    await _place(db_session, g1, project)
    await _place(db_session, g2, project)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/transfer", headers=owner_h, json={"to": "personal"}
    )
    assert resp.status_code in (200, 201), resp.text
    assert resp.json()["state"] == "accepted"
    assert await _access_rows(db_session, project_id=project.id) == []
    removed = await _events(db_session, org.id, "project_group_project_removed")
    assert sorted(e.target_id for e in removed) == sorted([g1.id, g2.id])
    assert {json.loads(e.after)["reason"] for e in removed} == {"project_transferred"}


@pytest.mark.asyncio
async def test_merge_moves_memberships_with_audit_and_stats(
    client: AsyncClient, db_session: AsyncSession
):
    owner, owner_h = await _user(db_session, "owner")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    source = await _project(db_session, owner, org=org)
    target = await _project(db_session, owner, org=org)
    moved = await _group(db_session, org, owner, name="Moved")
    shared = await _group(db_session, org, owner, name="Shared")
    await _place(db_session, moved, source)
    await _place(db_session, shared, source)
    await _place(db_session, shared, target)

    resp = await client.post(
        f"/api/v1/projects/{target.id}/merge",
        headers=owner_h,
        json={"source_project_id": source.id, "dry_run": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["stats"]["project_group_memberships_moved"] == 1
    assert resp.json()["stats"]["project_group_memberships_dropped"] == 1

    resp = await client.post(
        f"/api/v1/projects/{target.id}/merge",
        headers=owner_h,
        json={"source_project_id": source.id, "dry_run": False, "persona_policy": "rename"},
    )
    assert resp.status_code == 200, resp.text
    stats = resp.json()["stats"]
    assert stats["project_group_memberships_moved"] == 1
    assert stats["project_group_memberships_dropped"] == 1

    assert await _access_rows(db_session, project_id=source.id) == []
    assert sorted(r.group_id for r in await _access_rows(db_session, project_id=target.id)) == sorted(
        [moved.id, shared.id]
    )
    assert [e.target_id for e in await _events(db_session, org.id, "project_group_project_moved")] == [
        moved.id
    ]
    dropped = await _events(db_session, org.id, "project_group_project_removed")
    assert [e.target_id for e in dropped] == [shared.id]
    assert json.loads(dropped[0].after)["reason"] == "project_merged"


@pytest.mark.asyncio
async def test_project_delete_removes_memberships(client: AsyncClient, db_session: AsyncSession):
    owner, owner_h = await _user(db_session, "owner")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    project = await _project(db_session, owner, org=org)
    group = await _group(db_session, org, owner)
    await _place(db_session, group, project)

    resp = await client.delete(f"/api/v1/projects/{project.id}", headers=owner_h)
    assert resp.status_code == 200, resp.text
    assert await _access_rows(db_session, project_id=project.id) == []
    removed = await _events(db_session, org.id, "project_group_project_removed")
    assert [json.loads(e.after)["reason"] for e in removed] == ["project_deleted"]


@pytest.mark.asyncio
async def test_project_groups_shown_only_to_org_members(
    client: AsyncClient, db_session: AsyncSession
):
    """L2 — a captured-session reader outside the org sees the project but
    not its groups."""
    owner, owner_h = await _user(db_session, "owner")
    outsider, outsider_h = await _user(db_session, "outsider")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    project = await _project(db_session, owner, org=org)
    group = await _group(db_session, org, owner)
    await _place(db_session, group, project)
    await _capture_session(db_session, outsider, project)

    resp = await client.get(f"/api/v1/projects/{project.id}", headers=owner_h)
    assert [g["id"] for g in resp.json()["groups"]] == [group.id]
    resp = await client.get(f"/api/v1/projects/{project.id}", headers=outsider_h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["groups"] == []
    resp = await client.get(f"/api/v1/projects/{project.id}/groups", headers=outsider_h)
    assert resp.status_code == 200, resp.text
    assert resp.json() == []


# ── Captured sessions under restriction (M7) ──


async def _clone_session(db: AsyncSession, user: User, project: Project) -> None:
    """A session on the project's repository, not linked to it."""
    db.add(
        Session(
            id=f"ses_{uuid.uuid4().hex[:16]}",
            user_id=user.id,
            title="clone",
            tags="[]",
            source_tool="claude-code",
            blob_key="x",
            blob_size_bytes=0,
            etag="x",
            git_remote_normalized=project.git_remote_normalized,
            project_id=None,
            created_at=_now(),
            updated_at=_now(),
            uploaded_at=_now(),
        )
    )
    await db.commit()


async def _push(client: AsyncClient, headers: dict, remote: str) -> str:
    from .test_default_org_routing import _make_sfs_tarball

    session_id = f"ses_{uuid.uuid4().hex[:24]}"
    blob = _make_sfs_tarball(git_remote=f"https://github.com/{remote}.git", session_id=session_id)
    resp = await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=headers,
        files={"file": ("session.tar.gz", blob, "application/gzip")},
    )
    assert resp.status_code in (200, 201), resp.text
    return session_id


async def _session_project_id(db: AsyncSession, session_id: str) -> str | None:
    return (
        await db.execute(
            select(Session.project_id)
            .where(Session.id == session_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _remote_project(db: AsyncSession, owner: User, org: Organization) -> tuple[Project, str]:
    remote = f"acme/groups-{uuid.uuid4().hex[:8]}"
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="clone target",
        git_remote_normalized=remote,
        context_document="",
        owner_id=owner.id,
        org_id=org.id,
    )
    db.add(project)
    await db.commit()
    return project, remote


@pytest.mark.asyncio
async def test_restricted_member_push_gets_no_link_and_no_read(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _user(db_session, "owner")
    member, member_h = await _user(db_session, "member")
    granted, granted_h = await _user(db_session, "granted")
    org = await _org(db_session, restrict=True)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, member, role="member")
    await _member(db_session, org, granted, role="member")
    project, remote = await _remote_project(db_session, owner, org)
    group = await _group(db_session, org, owner)
    await _place(db_session, group, project)
    await _grant(db_session, group, granted, "reader")

    sid = await _push(client, member_h, remote)
    assert await _session_project_id(db_session, sid) is None
    assert not await user_can_access_project(db_session, member.id, project)
    assert project.id not in {p.id for p in await list_accessible_projects(db_session, member.id)}
    resp = await client.get(f"/api/v1/projects/{project.id}", headers=member_h)
    assert resp.status_code == 403, resp.text
    # The session stays the member's own.
    resp = await client.get(f"/api/v1/sessions/{sid}", headers=member_h)
    assert resp.status_code == 200, resp.text

    # A member with read standing still links.
    sid2 = await _push(client, granted_h, remote)
    assert await _session_project_id(db_session, sid2) == project.id


@pytest.mark.asyncio
async def test_unrestricted_member_push_links_and_reads(
    client: AsyncClient, db_session: AsyncSession
):
    owner, _ = await _user(db_session, "owner")
    member, member_h = await _user(db_session, "member")
    outsider, _ = await _user(db_session, "outsider")
    org = await _org(db_session, restrict=False)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, member, role="member")
    project, remote = await _remote_project(db_session, owner, org)

    sid = await _push(client, member_h, remote)
    assert await _session_project_id(db_session, sid) == project.id
    assert await user_can_access_project(db_session, member.id, project)
    # A non-member with a clone keeps today's captured-session read.
    await _clone_session(db_session, outsider, project)
    assert await user_can_access_project(db_session, outsider.id, project)
    assert not await user_can_edit_project(db_session, outsider.id, project)
    assert project.id in {p.id for p in await list_accessible_projects(db_session, outsider.id)}


@pytest.mark.asyncio
async def test_restricted_non_member_captured_session_gets_no_read(db_session: AsyncSession):
    owner, _ = await _user(db_session, "owner")
    outsider, _ = await _user(db_session, "outsider")
    org = await _org(db_session, restrict=True)
    await _member(db_session, org, owner, role="owner")
    project = await _project(db_session, owner, org=org)
    await _capture_session(db_session, outsider, project)
    await _clone_session(db_session, outsider, project)

    assert not await user_can_access_project(db_session, outsider.id, project)
    assert project.id not in {p.id for p in await list_accessible_projects(db_session, outsider.id)}

    from sessionfs.server.services.handoff_helpers import _accessible_project_ids

    assert project.id not in await _accessible_project_ids(db_session, outsider.id)


# ── Batch editor / reader sets under restriction (H3, L10) ──


@pytest.mark.asyncio
async def test_handoff_accessible_set_respects_restriction(db_session: AsyncSession):
    from sessionfs.server.services.handoff_helpers import _accessible_project_ids

    owner, _ = await _user(db_session, "owner")
    member, _ = await _user(db_session, "member")
    org = await _org(db_session, restrict=True)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, member, role="member")
    visible = await _project(db_session, owner, org=org)
    hidden = await _project(db_session, owner, org=org)
    group = await _group(db_session, org, owner)
    await _place(db_session, group, visible)
    await _grant(db_session, group, member, "reader")

    ids = await _accessible_project_ids(db_session, member.id)
    assert visible.id in ids
    assert hidden.id not in ids
    assert {visible.id, hidden.id} <= await _accessible_project_ids(db_session, owner.id)


@pytest.mark.asyncio
async def test_editor_sets_under_restriction(db_session: AsyncSession):
    """Compile (project_editor_user_ids / editor_authored_clause) and work
    queues (editor_backed_ticket_clause) follow the restricted edit tier."""
    from sessionfs.server.auth.project_access import (
        editor_authored_clause,
        project_editor_user_ids,
    )
    from sessionfs.server.db.models import Ticket
    from sessionfs.server.services.work_queues import editor_backed_ticket_clause

    owner, _ = await _user(db_session, "owner")
    admin, _ = await _user(db_session, "admin")
    member, _ = await _user(db_session, "member")
    editor, _ = await _user(db_session, "editor")
    reader, _ = await _user(db_session, "reader")
    orphan, _ = await _user(db_session, "orphan")
    org = await _org(db_session, restrict=True)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, admin, role="admin")
    for u in (member, editor, reader):
        await _member(db_session, org, u, role="member")
    project = await _project(db_session, owner, org=org)
    group = await _group(db_session, org, owner)
    await _place(db_session, group, project)
    await _grant(db_session, group, editor, "editor")
    await _grant(db_session, group, reader, "reader")
    await _grant(db_session, group, orphan, "admin")  # not an org member

    assert await project_editor_user_ids(db_session, project) == {owner.id, admin.id, editor.id}

    tickets = {}
    for u in (owner, admin, member, editor, reader, orphan):
        t = Ticket(
            id=f"tk_{uuid.uuid4().hex[:12]}",
            project_id=project.id,
            title=f"by {u.display_name}",
            created_by_user_id=u.id,
            status="suggested",
        )
        db_session.add(t)
        tickets[u.id] = t.id
    await db_session.commit()

    authored = set(
        (
            await db_session.execute(
                select(Ticket.created_by_user_id).where(
                    Ticket.project_id == project.id,
                    editor_authored_clause(Ticket.created_by_user_id, project, restricted=True),
                )
            )
        ).scalars().all()
    )
    assert authored == {owner.id, admin.id, editor.id}

    backed = set(
        (
            await db_session.execute(
                select(Ticket.id).where(
                    Ticket.project_id == project.id,
                    editor_backed_ticket_clause(project, restricted=True),
                )
            )
        ).scalars().all()
    )
    assert backed == {tickets[owner.id], tickets[admin.id], tickets[editor.id]}

    # Unrestricted, every org member is an editor again (orphan still isn't).
    unrestricted = set(
        (
            await db_session.execute(
                select(Ticket.created_by_user_id).where(
                    Ticket.project_id == project.id,
                    editor_authored_clause(Ticket.created_by_user_id, project, restricted=False),
                )
            )
        ).scalars().all()
    )
    assert unrestricted == {owner.id, admin.id, member.id, editor.id, reader.id}


@pytest.mark.asyncio
async def test_persona_rollup_scoped_under_restriction(
    client: AsyncClient, db_session: AsyncSession
):
    """H3 — a non-admin member's org persona roll-up only covers projects
    they can read when the org restricts visibility."""
    from sessionfs.server.db.models import Ticket

    owner, owner_h = await _user(db_session, "owner")
    member, member_h = await _user(db_session, "member")
    org = await _org(db_session, restrict=True)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, member, role="member")
    visible = await _project(db_session, owner, org=org)
    hidden = await _project(db_session, owner, org=org)
    group = await _group(db_session, org, owner)
    await _place(db_session, group, visible)
    await _grant(db_session, group, member, "reader")

    resp = await client.post(
        f"/api/v1/orgs/{org.id}/personas",
        headers=owner_h,
        json={"name": "infra", "role": "Infra", "content": "# Infra", "specializations": []},
    )
    assert resp.status_code == 201, resp.text
    ids = {}
    for p in (visible, hidden):
        t = Ticket(
            id=f"tk_{uuid.uuid4().hex[:12]}",
            project_id=p.id,
            title="t",
            created_by_user_id=owner.id,
            assigned_to="infra",
            status="open",
        )
        db_session.add(t)
        ids[p.id] = t.id
    await db_session.commit()

    url = f"/api/v1/orgs/{org.id}/personas/infra/rollup"
    resp = await client.get(url, headers=member_h)
    assert resp.status_code == 200, resp.text
    assert {t["id"] for t in resp.json()["open_tickets"]} == {ids[visible.id]}
    resp = await client.get(url, headers=owner_h)
    assert {t["id"] for t in resp.json()["open_tickets"]} == set(ids.values())


@pytest.mark.asyncio
async def test_retrieval_audit_scope_respects_restriction(db_session: AsyncSession):
    """M3 — the retrieval-audit project scope follows the restricted rule and
    still never admits captured-session readers."""
    from sessionfs.server.routes.retrieval_audit import _accessible_project_ids_subquery

    owner, _ = await _user(db_session, "owner")
    member, _ = await _user(db_session, "member")
    outsider, _ = await _user(db_session, "outsider")
    org = await _org(db_session, restrict=True)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, member, role="member")
    visible = await _project(db_session, owner, org=org)
    hidden = await _project(db_session, owner, org=org)
    group = await _group(db_session, org, owner)
    await _place(db_session, group, visible)
    await _grant(db_session, group, member, "reader")
    personal = await _project(db_session, outsider)
    await _capture_session(db_session, member, personal)

    async def _ids(user: User) -> set[str]:
        sub = await _accessible_project_ids_subquery(db_session, user.id)
        return set((await db_session.execute(sub)).scalars().all())

    member_ids = await _ids(member)
    assert visible.id in member_ids
    assert hidden.id not in member_ids
    assert personal.id not in member_ids  # captured-session readers excluded
    assert {visible.id, hidden.id} <= await _ids(owner)


# ── Auto-extracted knowledge on push (N1) ──


async def _run_auto_extract(monkeypatch, project: Project, user: User) -> None:
    from sessionfs.server.routes import sessions as sessions_routes
    from sessionfs.server.services import summarizer
    from sessionfs.server.services.summarizer import SessionSummary

    def _fake_summary(messages, manifest, workspace):
        return SessionSummary(
            session_id="ses_x",
            title="t",
            tool="claude-code",
            model="m",
            duration_minutes=1,
            message_count=6,
            tool_call_count=0,
            files_modified=[f"src/{uuid.uuid4().hex[:6]}.py"],
            files_read=[],
            commands_executed=0,
            tests_run=0,
            tests_passed=0,
            tests_failed=0,
            packages_installed=[],
            errors_encountered=[],
            what_happened="x",
            key_decisions=[f"Use approach {uuid.uuid4().hex[:6]} for the cache layer"],
            outcome="done",
            open_issues=[],
            generated_at="2026-10-26T00:00:00Z",
        )

    monkeypatch.setattr(summarizer, "summarize_session", _fake_summary)
    await sessions_routes._auto_extract_knowledge(
        f"ses_{uuid.uuid4().hex[:16]}", b"", project.git_remote_normalized, user.id
    )


async def _entry_users(db: AsyncSession, project: Project) -> set[str]:
    from sessionfs.server.db.models import KnowledgeEntry

    return set(
        (
            await db.execute(
                select(KnowledgeEntry.user_id)
                .where(KnowledgeEntry.project_id == project.id)
                .execution_options(populate_existing=True)
            )
        ).scalars().all()
    )


@pytest.mark.asyncio
async def test_auto_extract_respects_restriction(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    owner, _ = await _user(db_session, "owner")
    member, _ = await _user(db_session, "member")
    granted, _ = await _user(db_session, "granted")
    outsider, _ = await _user(db_session, "outsider")
    org = await _org(db_session, restrict=True)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, member, role="member")
    await _member(db_session, org, granted, role="member")
    project = await _project(db_session, owner, org=org)
    group = await _group(db_session, org, owner)
    await _place(db_session, group, project)
    await _grant(db_session, group, granted, "reader")

    for u in (member, outsider, granted):
        await _run_auto_extract(monkeypatch, project, u)
    assert await _entry_users(db_session, project) == {granted.id}


@pytest.mark.asyncio
async def test_auto_extract_unrestricted_unchanged(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """Outside restricted orgs any pusher on the remote still contributes,
    including a non-member (today's behaviour)."""
    owner, _ = await _user(db_session, "owner")
    member, _ = await _user(db_session, "member")
    outsider, _ = await _user(db_session, "outsider")
    org = await _org(db_session, restrict=False)
    await _member(db_session, org, owner, role="owner")
    await _member(db_session, org, member, role="member")
    project = await _project(db_session, owner, org=org)

    for u in (member, outsider):
        await _run_auto_extract(monkeypatch, project, u)
    assert await _entry_users(db_session, project) == {member.id, outsider.id}


@pytest.mark.asyncio
async def test_merged_project_cannot_join_a_group(client: AsyncClient, db_session: AsyncSession):
    """GitHub #111: a merge moves the source's group placements to its
    target; the tombstone itself is refused without naming the target."""
    owner, owner_h = await _user(db_session, "owner")
    org = await _org(db_session)
    await _member(db_session, org, owner, role="owner")
    group = await _group(db_session, org, owner)
    live = await _project(db_session, owner, org=org)
    merged = await _project(db_session, owner, org=org)
    merged.merged_into_project_id = live.id
    merged.merged_at = _now()
    await db_session.commit()

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/groups/{group.id}/projects/{merged.id}",
        headers=owner_h,
    )
    assert resp.status_code == 410, resp.text
    error = resp.json()["error"]
    assert error["code"] == "project_merged"
    details = error["details"]
    assert "merged_into" not in details
    assert await _access_rows(db_session, group_id=group.id) == []
