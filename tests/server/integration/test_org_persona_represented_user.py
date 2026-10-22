"""GitHub #81 — the member an org persona represents may update it.

Covers:
1. Admins set the represented member on create and update (by user id or
   email), clear it with an explicit null, and only to current org members.
2. The represented member updates role / content / specializations of
   their own persona without admin standing; everything else stays
   admin-only (other personas, create, delete, is_active, the link itself).
3. Project personas refuse represented_user.
4. Removing the member from the org, or deactivating the user, clears the
   link, so rejoining does not restore edit rights.
5. Audit rows for self-edits and link changes.
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import AgentPersona, OrgAuditEvent, OrgMember

from .test_org_personas import (
    _BODY,
    _create_org_persona,
    _hdrs,
    _index,
    _member,
    _org,
    _project,
    _service_key,
    _statement_log,
    _user,
)


def _url(org_id: str, name: str = "") -> str:
    return f"/api/v1/orgs/{org_id}/personas" + (f"/{name}" if name else "")


async def _row(db: AsyncSession, persona_id: str) -> AgentPersona:
    return (
        await db.execute(
            select(AgentPersona)
            .where(AgentPersona.id == persona_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()


async def _events(db: AsyncSession, org_id: str, event_type: str) -> list[OrgAuditEvent]:
    return list(
        (
            await db.execute(
                select(OrgAuditEvent).where(
                    OrgAuditEvent.org_id == org_id, OrgAuditEvent.event_type == event_type
                )
            )
        ).scalars().all()
    )


# ── setting the link ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_admin_sets_link_on_create_by_id_and_by_email(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    _, admin_key = await _member(db_session, org, "admin")
    kelvin, _ = await _member(db_session, org, "member")
    julia, _ = await _member(db_session, org, "member")

    by_id = await _create_org_persona(
        client, org, admin_key, name="kelvin", represented_user=kelvin.id
    )
    assert by_id["represented_user_id"] == kelvin.id
    by_email = await _create_org_persona(
        client, org, admin_key, name="julia", represented_user=julia.email.upper()
    )
    assert by_email["represented_user_id"] == julia.id
    plain = await _create_org_persona(client, org, admin_key, name="remediator")
    assert plain["represented_user_id"] is None

    created = await _events(db_session, org.id, "persona_created")
    assert {json.loads(e.after)["represented_user_id"] for e in created} == {
        kelvin.id, julia.id, None,
    }


@pytest.mark.asyncio
async def test_link_must_name_a_current_member(client: AsyncClient, db_session: AsyncSession):
    org = await _org(db_session)
    other = await _org(db_session)
    _, admin_key = await _member(db_session, org, "admin")
    stranger, _ = await _member(db_session, other, "member")
    loner, _ = await _user(db_session)

    for value in (stranger.id, stranger.email, loner.id, loner.email, "nobody@example.com", "  "):
        resp = await client.post(
            _url(org.id), headers=_hdrs(admin_key), json={**_BODY, "represented_user": value}
        )
        assert resp.status_code == 422, (value, resp.text)
    rows = (
        await db_session.execute(select(AgentPersona).where(AgentPersona.org_id == org.id))
    ).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_email_matching_two_members_is_refused(client: AsyncClient, db_session: AsyncSession):
    org = await _org(db_session)
    _, admin_key = await _member(db_session, org, "admin")
    first, _ = await _member(db_session, org, "member")
    second, _ = await _member(db_session, org, "member")
    first.email = "Kelvin@example.com"
    second.email = "kelvin@example.com"
    await db_session.commit()
    resp = await client.post(
        _url(org.id), headers=_hdrs(admin_key),
        json={**_BODY, "represented_user": "KELVIN@example.com"},
    )
    assert resp.status_code == 422, resp.text
    # A user id stays unambiguous.
    resp = await client.post(
        _url(org.id), headers=_hdrs(admin_key), json={**_BODY, "represented_user": second.id}
    )
    assert resp.status_code == 201, resp.text


@pytest.mark.asyncio
async def test_admin_changes_and_clears_link_without_version_bump(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    _, admin_key = await _member(db_session, org, "admin")
    first, _ = await _member(db_session, org, "member")
    second, _ = await _member(db_session, org, "member")
    persona = await _create_org_persona(client, org, admin_key, represented_user=first.id)

    resp = await client.put(
        _url(org.id, "gcp-infra"), headers=_hdrs(admin_key),
        json={"represented_user": second.email},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["represented_user_id"] == second.id
    assert resp.json()["version"] == 1

    # Omitting the field leaves the link alone.
    resp = await client.put(
        _url(org.id, "gcp-infra"), headers=_hdrs(admin_key), json={"role": "Lead"}
    )
    assert resp.json()["represented_user_id"] == second.id
    assert resp.json()["version"] == 2

    resp = await client.put(
        _url(org.id, "gcp-infra"), headers=_hdrs(admin_key), json={"represented_user": None}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["represented_user_id"] is None
    assert (await _row(db_session, persona["id"])).represented_user_id is None

    changes = await _events(db_session, org.id, "persona_represented_user_changed")
    assert len(changes) == 2
    assert all(e.actor_role_at_time == "admin" for e in changes)


@pytest.mark.asyncio
async def test_service_key_with_write_scope_can_set_link(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, admin_key = await _member(db_session, org, "admin")
    member, _ = await _member(db_session, org, "member")
    await _create_org_persona(client, org, admin_key)
    svc = await _service_key(db_session, org, admin, ["personas:read", "personas:write"])
    resp = await client.put(
        _url(org.id, "gcp-infra"), headers=_hdrs(svc), json={"represented_user": member.id}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["represented_user_id"] == member.id


# ── self-edit by the represented member ──────────────────────


@pytest.mark.asyncio
async def test_represented_member_updates_own_persona(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    _, admin_key = await _member(db_session, org, "admin")
    kelvin, kelvin_key = await _member(db_session, org, "member")
    persona = await _create_org_persona(
        client, org, admin_key, name="kelvin", represented_user=kelvin.id
    )

    resp = await client.put(
        _url(org.id, "KELVIN"), headers=_hdrs(kelvin_key),
        json={"role": "Platform engineer", "content": "# Kelvin\n\nOwns GKE.", "specializations": ["gke"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["role"] == "Platform engineer"
    assert body["specializations"] == ["gke"]
    assert body["version"] == 2
    assert body["represented_user_id"] == kelvin.id

    updated = await _events(db_session, org.id, "persona_updated")
    assert len(updated) == 1
    assert updated[0].actor_user_id == kelvin.id
    assert updated[0].actor_role_at_time == "member"
    assert (await _row(db_session, persona["id"])).content == "# Kelvin\n\nOwns GKE."


@pytest.mark.asyncio
async def test_represented_member_cannot_touch_admin_only_fields(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    _, admin_key = await _member(db_session, org, "admin")
    kelvin, kelvin_key = await _member(db_session, org, "member")
    other, _ = await _member(db_session, org, "member")
    persona = await _create_org_persona(
        client, org, admin_key, name="kelvin", represented_user=kelvin.id
    )

    for body in (
        {"is_active": False},
        {"is_active": True},
        {"represented_user": other.id},
        {"represented_user": None},
        {"role": "x", "represented_user": kelvin.id},
    ):
        resp = await client.put(_url(org.id, "kelvin"), headers=_hdrs(kelvin_key), json=body)
        assert resp.status_code == 403, (body, resp.text)
    row = await _row(db_session, persona["id"])
    assert (row.is_active, row.represented_user_id, row.role, row.version) == (
        True, kelvin.id, _BODY["role"], 1,
    )

    resp = await client.delete(_url(org.id, "kelvin"), headers=_hdrs(kelvin_key))
    assert resp.status_code == 403
    resp = await client.post(
        _url(org.id), headers=_hdrs(kelvin_key), json={**_BODY, "name": "kelvin2"}
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_member_cannot_edit_someone_elses_or_unlinked_persona(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    _, admin_key = await _member(db_session, org, "admin")
    kelvin, kelvin_key = await _member(db_session, org, "member")
    tolu, _ = await _member(db_session, org, "member")
    await _create_org_persona(client, org, admin_key, name="tolu", represented_user=tolu.id)
    await _create_org_persona(client, org, admin_key, name="remediator")

    for name in ("tolu", "remediator", "missing"):
        resp = await client.put(
            _url(org.id, name), headers=_hdrs(kelvin_key), json={"role": "mine now"}
        )
        assert resp.status_code == 403, (name, resp.text)


@pytest.mark.asyncio
async def test_self_edit_is_scoped_to_the_persona_org(client: AsyncClient, db_session: AsyncSession):
    """A link in one org gives nothing in another org the user also joined."""
    org = await _org(db_session)
    other = await _org(db_session)
    _, admin_key = await _member(db_session, org, "admin")
    _, other_admin_key = await _member(db_session, other, "admin")
    kelvin, kelvin_key = await _member(db_session, org, "member")
    db_session.add(OrgMember(org_id=other.id, user_id=kelvin.id, role="member"))
    await db_session.commit()
    await _create_org_persona(client, org, admin_key, name="kelvin", represented_user=kelvin.id)
    await _create_org_persona(client, other, other_admin_key, name="kelvin")

    resp = await client.put(
        _url(other.id, "kelvin"), headers=_hdrs(kelvin_key), json={"role": "r"}
    )
    assert resp.status_code == 403
    resp = await client.put(
        _url(org.id, "kelvin"), headers=_hdrs(kelvin_key), json={"role": "r"}
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_refused_member_edit_never_takes_the_org_lock(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    _, admin_key = await _member(db_session, org, "admin")
    kelvin, kelvin_key = await _member(db_session, org, "member")
    await _create_org_persona(client, org, admin_key, name="remediator")
    await _create_org_persona(client, org, admin_key, name="kelvin", represented_user=kelvin.id)
    log = _statement_log(db_session)
    try:
        refused = await client.put(
            _url(org.id, "remediator"), headers=_hdrs(kelvin_key), json={"role": "r"}
        )
        mark = len(log)
        allowed = await client.put(
            _url(org.id, "kelvin"), headers=_hdrs(kelvin_key), json={"role": "r"}
        )
    finally:
        _statement_log.remove()  # type: ignore[attr-defined]
    assert refused.status_code == 403
    assert not any("update organizations set id = id" in stmt for stmt in log[:mark])
    assert allowed.status_code == 200, allowed.text
    # The permitted self-edit locks the org, then re-checks under the lock.
    lock = _index(log, "update organizations set id = id", mark)
    assert _index(log, "from org_members", lock) > lock


@pytest.mark.asyncio
async def test_link_moved_while_waiting_for_the_lock_is_honoured(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """The under-lock check must read the persona afresh, not reuse the row
    loaded by the pre-lock check."""
    from sqlalchemy import update

    from sessionfs.server.routes import personas as personas_routes

    org = await _org(db_session)
    _, admin_key = await _member(db_session, org, "admin")
    kelvin, kelvin_key = await _member(db_session, org, "member")
    tolu, _ = await _member(db_session, org, "member")
    persona = await _create_org_persona(
        client, org, admin_key, name="kelvin", represented_user=kelvin.id
    )
    real_lock = personas_routes.lock_scopes

    async def _admin_moves_link_first(db, **kwargs):
        await db_session.execute(
            update(AgentPersona)
            .where(AgentPersona.id == persona["id"])
            .values(represented_user_id=tolu.id)
        )
        await db_session.commit()
        await real_lock(db, **kwargs)

    monkeypatch.setattr(personas_routes, "lock_scopes", _admin_moves_link_first)
    resp = await client.put(_url(org.id, "kelvin"), headers=_hdrs(kelvin_key), json={"role": "r"})
    assert resp.status_code == 403, resp.text
    row = await _row(db_session, persona["id"])
    assert (row.role, row.version, row.represented_user_id) == (_BODY["role"], 1, tolu.id)


@pytest.mark.asyncio
async def test_member_cannot_edit_a_deactivated_persona(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    _, admin_key = await _member(db_session, org, "admin")
    kelvin, kelvin_key = await _member(db_session, org, "member")
    await _create_org_persona(client, org, admin_key, name="kelvin", represented_user=kelvin.id)
    resp = await client.delete(_url(org.id, "kelvin"), headers=_hdrs(admin_key))
    assert resp.status_code == 204, resp.text
    resp = await client.put(_url(org.id, "kelvin"), headers=_hdrs(kelvin_key), json={"role": "r"})
    assert resp.status_code == 403


# ── project personas ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_project_personas_refuse_represented_user(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    owner, owner_key = await _member(db_session, org, "owner")
    project = await _project(db_session, owner, org)
    base = f"/api/v1/projects/{project.id}/personas"

    resp = await client.post(
        base, headers=_hdrs(owner_key), json={**_BODY, "represented_user": owner.id}
    )
    assert resp.status_code == 422, resp.text
    resp = await client.post(base, headers=_hdrs(owner_key), json=_BODY)
    assert resp.status_code == 201, resp.text
    assert resp.json()["represented_user_id"] is None
    for value in (owner.id, None):
        resp = await client.put(
            f"{base}/gcp-infra", headers=_hdrs(owner_key), json={"represented_user": value}
        )
        assert resp.status_code == 422, (value, resp.text)


@pytest.mark.asyncio
async def test_project_list_shows_link_on_org_personas(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    owner, owner_key = await _member(db_session, org, "owner")
    kelvin, _ = await _member(db_session, org, "member")
    project = await _project(db_session, owner, org)
    await _create_org_persona(client, org, owner_key, name="kelvin", represented_user=kelvin.id)
    resp = await client.get(f"/api/v1/projects/{project.id}/personas", headers=_hdrs(owner_key))
    assert resp.status_code == 200, resp.text
    [row] = [p for p in resp.json() if p["name"] == "kelvin"]
    assert row["scope"] == "org"
    assert row["represented_user_id"] == kelvin.id


# ── offboarding ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_member_removal_clears_link_and_rejoin_does_not_restore_it(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    _, admin_key = await _member(db_session, org, "admin")
    kelvin, kelvin_key = await _member(db_session, org, "member")
    other_org = await _org(db_session)
    _, other_admin_key = await _member(db_session, other_org, "admin")
    db_session.add(OrgMember(org_id=other_org.id, user_id=kelvin.id, role="member"))
    await db_session.commit()
    persona = await _create_org_persona(
        client, org, admin_key, name="kelvin", represented_user=kelvin.id
    )
    elsewhere = await _create_org_persona(
        client, other_org, other_admin_key, name="kelvin", represented_user=kelvin.id
    )

    resp = await client.delete(f"/api/v1/orgs/{org.id}/members/{kelvin.id}", headers=_hdrs(admin_key))
    assert resp.status_code == 200, resp.text
    assert (await _row(db_session, persona["id"])).represented_user_id is None
    [event] = await _events(db_session, org.id, "persona_represented_user_changed")
    assert event.target_id == persona["id"]
    assert event.actor_role_at_time == "admin"
    assert json.loads(event.before)["represented_user_id"] == kelvin.id
    assert json.loads(event.after) == {
        "name": "kelvin", "represented_user_id": None, "reason": "member_removed",
    }
    # Only the org they left.
    assert (await _row(db_session, elsewhere["id"])).represented_user_id == kelvin.id

    db_session.add(OrgMember(org_id=org.id, user_id=kelvin.id, role="member"))
    await db_session.commit()
    resp = await client.put(_url(org.id, "kelvin"), headers=_hdrs(kelvin_key), json={"role": "r"})
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_user_deactivation_clears_every_link(client: AsyncClient, db_session: AsyncSession):
    _, platform_key = await _user(db_session, tier="admin")
    org = await _org(db_session)
    _, admin_key = await _member(db_session, org, "admin")
    kelvin, _ = await _member(db_session, org, "member")
    persona = await _create_org_persona(
        client, org, admin_key, name="kelvin", represented_user=kelvin.id
    )
    resp = await client.delete(f"/api/v1/admin/users/{kelvin.id}", headers=_hdrs(platform_key))
    assert resp.status_code == 204, resp.text
    assert (await _row(db_session, persona["id"])).represented_user_id is None
    [event] = await _events(db_session, org.id, "persona_represented_user_changed")
    assert event.actor_role_at_time == "platform_admin"
    assert json.loads(event.after)["reason"] == "user_deactivated"
