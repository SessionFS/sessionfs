"""GitHub #92 — persona roll-up and represent-history.

Covers:
1. `GET /api/v1/orgs/{org_id}/personas/{name}/rollup` — the persona plus
   the caller's recent sessions under it, open (non-terminal) tickets
   assigned to it, the latest KB entry and the latest agent run, scoped
   to the projects the caller may read.
2. `GET /api/v1/projects/{project_id}/personas/{name}/rollup` — the same
   shape, scoped to one project.
3. `GET /api/v1/orgs/{org_id}/personas/{name}/represent-history` — the
   org-audit trail of who represented the persona, admin-only.
4. `GET /api/v1/auth/me` exposes `represented_personas`.

Access rules: org members read the roll-up for their org's personas; a
project-restricted service key sees only its allowlisted projects; every
miss for an outsider is a 404; represent-history is admin-only.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentRun,
    KnowledgeEntry,
    OrgAuditEvent,
    Session,
    Ticket,
)

from .test_org_personas import (
    _create_org_persona,
    _hdrs,
    _member,
    _org,
    _project,
    _service_key,
    _user,
)


def _now(**delta) -> datetime:
    return datetime.now(timezone.utc) + timedelta(**delta)


async def _session(
    db: AsyncSession, user_id: str, persona: str, **over
) -> str:
    fields = dict(
        id=f"ses_{uuid.uuid4().hex[:16]}",
        user_id=user_id,
        source_tool="claude-code",
        blob_key=f"sessions/{user_id}/x.tar.gz",
        etag=uuid.uuid4().hex,
        persona_name=persona,
        created_at=_now(),
    )
    fields.update(over)
    row = Session(**fields)
    db.add(row)
    await db.commit()
    return row.id


async def _ticket(
    db: AsyncSession, project, created_by: str, persona: str, status: str = "open", **over
) -> str:
    fields = dict(
        id=f"tk_{uuid.uuid4().hex[:12]}",
        project_id=project.id,
        title=f"t-{uuid.uuid4().hex[:6]}",
        created_by_user_id=created_by,
        assigned_to=persona,
        status=status,
        created_at=_now(),
    )
    fields.update(over)
    row = Ticket(**fields)
    db.add(row)
    await db.commit()
    return row.id


async def _kb(
    db: AsyncSession, project, user_id: str, persona: str, **over
) -> int:
    fields = dict(
        project_id=project.id,
        session_id=f"ses_{uuid.uuid4().hex[:16]}",
        user_id=user_id,
        entry_type="discovery",
        content=f"kb-{uuid.uuid4().hex[:6]}",
        persona_name=persona,
        created_at=_now(),
    )
    fields.update(over)
    row = KnowledgeEntry(**fields)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row.id


async def _run(
    db: AsyncSession, project, persona: str, **over
) -> str:
    fields = dict(
        id=f"run_{uuid.uuid4().hex[:12]}",
        project_id=project.id,
        persona_name=persona,
        status="passed",
        created_at=_now(),
    )
    fields.update(over)
    row = AgentRun(**fields)
    db.add(row)
    await db.commit()
    return row.id


# ── org roll-up ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_org_rollup_returns_each_section(client: AsyncClient, db_session: AsyncSession):
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    persona = await _create_org_persona(client, org, key, name="gcp-infra")

    older = await _session(db_session, admin.id, "gcp-infra", project_id=project.id, created_at=_now(days=-2))
    newer = await _session(db_session, admin.id, "gcp-infra", project_id=project.id, created_at=_now(days=-1))
    # A session under the same name owned by someone else is not the caller's.
    other, _ = await _member(db_session, org, "member")
    other_session = await _session(db_session, other.id, "gcp-infra", project_id=project.id)

    open_tk = await _ticket(db_session, project, admin.id, "gcp-infra", status="in_progress")
    done_tk = await _ticket(db_session, project, admin.id, "gcp-infra", status="done")
    await _kb(db_session, project, admin.id, "gcp-infra", created_at=_now(days=-3))
    kb_new = await _kb(db_session, project, admin.id, "gcp-infra", created_at=_now(days=-1))
    await _run(db_session, project, "gcp-infra", created_at=_now(days=-2))
    run_new = await _run(db_session, project, "gcp-infra", created_at=_now(days=-1))

    resp = await client.get(
        f"/api/v1/orgs/{org.id}/personas/gcp-infra/rollup", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["persona"]["id"] == persona["id"]
    assert body["persona"]["name"] == "gcp-infra"

    session_ids = [s["id"] for s in body["recent_sessions"]]
    assert session_ids == [newer, older]  # caller's own, newest first
    assert other_session not in session_ids

    ticket_ids = [t["id"] for t in body["open_tickets"]]
    assert open_tk in ticket_ids
    assert done_tk not in ticket_ids  # terminal statuses are excluded

    assert body["last_knowledge_entry"]["id"] == kb_new  # latest wins
    assert body["last_agent_run"]["id"] == run_new


@pytest.mark.asyncio
async def test_org_rollup_filters_out_projects_outside_service_key_allowlist(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, admin_key = await _member(db_session, org, "admin")
    in_project = await _project(db_session, admin, org)
    out_project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, admin_key, name="gcp-infra")

    in_tk = await _ticket(db_session, in_project, admin.id, "gcp-infra")
    out_tk = await _ticket(db_session, out_project, admin.id, "gcp-infra")
    in_kb = await _kb(db_session, in_project, admin.id, "gcp-infra")
    await _kb(db_session, out_project, admin.id, "gcp-infra")
    in_run = await _run(db_session, in_project, "gcp-infra")
    await _run(db_session, out_project, "gcp-infra")

    svc = await _service_key(
        db_session, org, admin, ["personas:read"], project_ids=[in_project.id]
    )
    resp = await client.get(
        f"/api/v1/orgs/{org.id}/personas/gcp-infra/rollup", headers=_hdrs(svc)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    ticket_ids = {t["id"] for t in body["open_tickets"]}
    assert in_tk in ticket_ids
    assert out_tk not in ticket_ids
    assert body["last_knowledge_entry"]["id"] == in_kb
    assert body["last_agent_run"]["id"] == in_run


@pytest.mark.asyncio
async def test_service_key_sessions_need_sessions_read_and_stay_project_scoped(
    client: AsyncClient, db_session: AsyncSession
):
    """Finding 1 — a service key runs as its creator, so the roll-up must not
    leak the creator's sessions: no ``sessions:read`` scope means no sessions
    at all, and sessions are project-scoped like every other item."""
    org = await _org(db_session)
    admin, admin_key = await _member(db_session, org, "admin")
    in_project = await _project(db_session, admin, org)
    out_project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, admin_key, name="gcp-infra")

    in_ses = await _session(db_session, admin.id, "gcp-infra", project_id=in_project.id)
    out_ses = await _session(db_session, admin.id, "gcp-infra", project_id=out_project.id)
    noproj_ses = await _session(db_session, admin.id, "gcp-infra")  # project_id None

    url = f"/api/v1/orgs/{org.id}/personas/gcp-infra/rollup"

    # Without sessions:read the creator's sessions are invisible to the key.
    no_sessions_key = await _service_key(
        db_session, org, admin, ["personas:read"], project_ids=[in_project.id, out_project.id]
    )
    resp = await client.get(url, headers=_hdrs(no_sessions_key))
    assert resp.status_code == 200, resp.text
    assert resp.json()["recent_sessions"] == []

    # With sessions:read, only the allowlisted project's session appears;
    # the out-of-allowlist and no-project sessions stay hidden.
    sessions_key = await _service_key(
        db_session, org, admin, ["personas:read", "sessions:read"], project_ids=[in_project.id]
    )
    resp = await client.get(url, headers=_hdrs(sessions_key))
    assert resp.status_code == 200, resp.text
    session_ids = [s["id"] for s in resp.json()["recent_sessions"]]
    assert session_ids == [in_ses]
    assert out_ses not in session_ids
    assert noproj_ses not in session_ids


@pytest.mark.asyncio
async def test_org_rollup_excludes_projects_with_same_named_project_persona(
    client: AsyncClient, db_session: AsyncSession
):
    """Finding 6 — a project with its own active same-named project persona
    overrides the org persona there, so that project's activity belongs to
    the project persona and must not count against the org roll-up."""
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    plain = await _project(db_session, admin, org)
    overridden = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key, name="gcp-infra")
    resp = await client.post(
        f"/api/v1/projects/{overridden.id}/personas", headers=_hdrs(key),
        json={"name": "gcp-infra", "role": "Repo-specific"},
    )
    assert resp.status_code == 201

    plain_tk = await _ticket(db_session, plain, admin.id, "gcp-infra")
    over_tk = await _ticket(db_session, overridden, admin.id, "gcp-infra")

    resp = await client.get(
        f"/api/v1/orgs/{org.id}/personas/gcp-infra/rollup", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    ticket_ids = [t["id"] for t in resp.json()["open_tickets"]]
    assert plain_tk in ticket_ids
    assert over_tk not in ticket_ids


@pytest.mark.asyncio
async def test_org_rollup_latest_kb_skips_dismissed_and_superseded(
    client: AsyncClient, db_session: AsyncSession
):
    """Finding 4 — a dismissed or superseded KB entry is no longer the
    persona's latest knowledge, even if it is newer."""
    org = await _org(db_session)
    admin, key = await _member(db_session, org, "admin")
    project = await _project(db_session, admin, org)
    await _create_org_persona(client, org, key, name="gcp-infra")

    good = await _kb(db_session, project, admin.id, "gcp-infra", created_at=_now(days=-3))
    await _kb(db_session, project, admin.id, "gcp-infra", created_at=_now(days=-1), dismissed=True)
    await _kb(db_session, project, admin.id, "gcp-infra", created_at=_now(days=-2), superseded_by=good)

    resp = await client.get(
        f"/api/v1/orgs/{org.id}/personas/gcp-infra/rollup", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["last_knowledge_entry"]["id"] == good


@pytest.mark.asyncio
async def test_org_rollup_non_member_gets_404(client: AsyncClient, db_session: AsyncSession):
    org = await _org(db_session)
    admin, admin_key = await _member(db_session, org, "admin")
    await _create_org_persona(client, org, admin_key, name="gcp-infra")
    outsider, outsider_key = await _user(db_session)

    for suffix in ("rollup", "represent-history"):
        resp = await client.get(
            f"/api/v1/orgs/{org.id}/personas/gcp-infra/{suffix}",
            headers=_hdrs(outsider_key),
        )
        assert resp.status_code == 404, (suffix, resp.text)


@pytest.mark.asyncio
async def test_org_rollup_unknown_persona_is_404(client: AsyncClient, db_session: AsyncSession):
    org = await _org(db_session)
    _, key = await _member(db_session, org, "admin")
    resp = await client.get(
        f"/api/v1/orgs/{org.id}/personas/missing/rollup", headers=_hdrs(key)
    )
    assert resp.status_code == 404, resp.text


# ── project roll-up ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_project_rollup_scopes_to_one_project(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    owner, key = await _member(db_session, org, "owner")
    project = await _project(db_session, owner, org)
    other_project = await _project(db_session, owner, org)
    await _create_org_persona(client, org, key, name="gcp-infra")

    session = await _session(db_session, owner.id, "gcp-infra", project_id=project.id)
    ticket = await _ticket(db_session, project, owner.id, "gcp-infra")
    await _ticket(db_session, other_project, owner.id, "gcp-infra")
    kb = await _kb(db_session, project, owner.id, "gcp-infra")
    await _kb(db_session, other_project, owner.id, "gcp-infra")
    run = await _run(db_session, project, "gcp-infra")
    await _run(db_session, other_project, "gcp-infra")

    resp = await client.get(
        f"/api/v1/projects/{project.id}/personas/gcp-infra/rollup", headers=_hdrs(key)
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["persona"]["name"] == "gcp-infra"
    assert [s["id"] for s in body["recent_sessions"]] == [session]
    assert [t["id"] for t in body["open_tickets"]] == [ticket]
    assert body["last_knowledge_entry"]["id"] == kb
    assert body["last_agent_run"]["id"] == run


# ── represent-history ────────────────────────────────────────


@pytest.mark.asyncio
async def test_represent_history_lists_assignments_and_is_admin_only(
    client: AsyncClient, db_session: AsyncSession
):
    org = await _org(db_session)
    admin, admin_key = await _member(db_session, org, "admin")
    kelvin, kelvin_key = await _member(db_session, org, "member")
    tolu, _ = await _member(db_session, org, "member")
    await _create_org_persona(client, org, admin_key, name="kelvin")

    url = f"/api/v1/orgs/{org.id}/personas/kelvin"
    assert (
        await client.put(url, headers=_hdrs(admin_key), json={"represented_user": kelvin.id})
    ).status_code == 200
    assert (
        await client.put(url, headers=_hdrs(admin_key), json={"represented_user": tolu.id})
    ).status_code == 200
    assert (
        await client.put(url, headers=_hdrs(admin_key), json={"represented_user": None})
    ).status_code == 200

    resp = await client.get(f"{url}/represent-history", headers=_hdrs(admin_key))
    assert resp.status_code == 200, resp.text
    history = resp.json()
    assert len(history) == 3
    # Order is newest-first by created_at, but the three events can land in
    # the same second (audit rows share a CURRENT_TIMESTAMP), so assert the
    # set of transitions instead of their order.
    assert {(h["from_user_id"], h["to_user_id"]) for h in history} == {
        (None, kelvin.id),
        (kelvin.id, tolu.id),
        (tolu.id, None),
    }
    assert all(h["actor_role"] == "admin" for h in history)
    assert all(h["actor_user_id"] == admin.id for h in history)

    # A non-admin member cannot read it.
    assert (
        await client.get(f"{url}/represent-history", headers=_hdrs(kelvin_key))
    ).status_code == 403


@pytest.mark.asyncio
async def test_represent_history_includes_create_with_representative(
    client: AsyncClient, db_session: AsyncSession
):
    """Finding 2 — a representative set at create time is logged as
    ``persona_created``, not ``persona_represented_user_changed``, so it
    must still appear in the history. A role-only edit keeps the same
    representative and must not."""
    org = await _org(db_session)
    admin, admin_key = await _member(db_session, org, "admin")
    kelvin, _ = await _member(db_session, org, "member")
    tolu, _ = await _member(db_session, org, "member")
    await _create_org_persona(client, org, admin_key, name="kelvin", represented_user=kelvin.id)

    url = f"/api/v1/orgs/{org.id}/personas/kelvin"
    # Role-only edit: representative unchanged, so no history entry.
    assert (
        await client.put(url, headers=_hdrs(admin_key), json={"role": "New role"})
    ).status_code == 200
    # Explicit change of representative.
    assert (
        await client.put(url, headers=_hdrs(admin_key), json={"represented_user": tolu.id})
    ).status_code == 200

    resp = await client.get(f"{url}/represent-history", headers=_hdrs(admin_key))
    assert resp.status_code == 200, resp.text
    history = resp.json()
    assert {(h["from_user_id"], h["to_user_id"]) for h in history} == {
        (None, kelvin.id),   # persona_created with a representative
        (kelvin.id, tolu.id),  # persona_represented_user_changed
    }


@pytest.mark.asyncio
async def test_represent_history_tolerates_non_string_represented_user_id(
    client: AsyncClient, db_session: AsyncSession
):
    """Finding 3 — a hand-edited audit row with a non-string
    ``represented_user_id`` must be dropped to None, not blow up pydantic."""
    org = await _org(db_session)
    admin, admin_key = await _member(db_session, org, "admin")
    kelvin, _ = await _member(db_session, org, "member")
    persona = await _create_org_persona(client, org, admin_key, name="kelvin", represented_user=kelvin.id)

    db_session.add(
        OrgAuditEvent(
            id=f"oae_{uuid.uuid4().hex[:24]}",
            org_id=org.id,
            org_name_snapshot=org.name,
            event_type="persona_represented_user_changed",
            actor_user_id=admin.id,
            actor_email_snapshot=admin.email,
            actor_role_at_time="admin",
            target_type="persona",
            target_id=persona["id"],
            before=json.dumps({"represented_user_id": 12345}),
            after=json.dumps({"represented_user_id": kelvin.id}),
        )
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/orgs/{org.id}/personas/kelvin/represent-history", headers=_hdrs(admin_key)
    )
    assert resp.status_code == 200, resp.text
    transitions = {(h["from_user_id"], h["to_user_id"]) for h in resp.json()}
    # The numeric value is normalised to None, not surfaced as an int.
    assert (None, kelvin.id) in transitions
    assert (12345, kelvin.id) not in transitions


# ── /auth/me represented_personas ────────────────────────────


@pytest.mark.asyncio
async def test_me_exposes_represented_personas(client: AsyncClient, db_session: AsyncSession):
    org = await _org(db_session)
    _, admin_key = await _member(db_session, org, "admin")
    kelvin, kelvin_key = await _member(db_session, org, "member")
    await _create_org_persona(
        client, org, admin_key, name="kelvin", represented_user=kelvin.id
    )
    # A persona representing someone else must not appear.
    await _create_org_persona(client, org, admin_key, name="other")

    resp = await client.get("/api/v1/auth/me", headers=_hdrs(kelvin_key))
    assert resp.status_code == 200, resp.text
    represented = resp.json()["represented_personas"]
    assert [p["name"] for p in represented] == ["kelvin"]
    assert represented[0]["org_id"] == org.id
    assert represented[0]["org_name"] == org.name

    # The admin represents nobody.
    admin_me = await client.get("/api/v1/auth/me", headers=_hdrs(admin_key))
    assert admin_me.json()["represented_personas"] == []
