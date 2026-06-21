"""WQ-P2 (tk_3481237f3b0847d6) — work-queue management surface tests.

Covers the CRUD + lifecycle routes under
`/api/v1/projects/{project_id}/work-queues`:

- create: each valid mode; invalid mode → 422; cadence < 120 → 422;
  max_tickets_per_run > 5 → 422; defaults applied when omitted; seeds
  items from selector.ticket_ids; cross-project ticket_ids → 422.
- list + get; get of another project's queue → 404.
- set_status: full transition matrix (active->paused->active->completed;
  any->cancelled); illegal transition → 409; stale lease_epoch → 409;
  lease_epoch bumps on success.
- scope enforcement: service key with only work_queues:read cannot
  create/set_status (403 insufficient_scope); service key with no
  work_queues scope → 403; user wildcard key works.
- cross-project denial via service key (assert_service_key_can_access_project).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    OrgMember,
    Organization,
    Project,
    Ticket,
    User,
)


# ── helpers ──


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _structured_error(body: dict) -> dict:
    """Navigate the {error: {code, details, message}} envelope the
    server's exception handler wraps structured-dict HTTPException detail
    in (mirrors test_scoped_service_keys._structured_error). Returns the
    inner `details` dict (what we raised), or {} on shape mismatch."""
    if not isinstance(body, dict):
        return {}
    err = body.get("error") or body.get("detail") or body
    if isinstance(err, dict):
        inner = err.get("details") or err.get("detail")
        if isinstance(inner, dict):
            return inner
        return err
    return {}


async def _make_user_with_key(
    db: AsyncSession, email: str | None = None, tier: str = "team"
) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=email or f"u-{uuid.uuid4().hex[:6]}@example.com",
        display_name="U",
        tier=tier,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.flush()
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name="user-key",
            is_active=True,
            key_kind="user",
            scopes=json.dumps(["*"]),
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


async def _make_org_with_admin(
    db: AsyncSession,
) -> tuple[Organization, User, str]:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name=f"Org-{uuid.uuid4().hex[:6]}",
        slug=f"o-{uuid.uuid4().hex[:6]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db.add(org)
    await db.flush()
    admin_user, raw = await _make_user_with_key(db)
    db.add(
        OrgMember(
            org_id=org.id,
            user_id=admin_user.id,
            role="admin",
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return org, admin_user, raw


async def _make_project(
    db: AsyncSession, owner: User, org: Organization | None = None
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name=f"wq-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"github.com/acme/{uuid.uuid4().hex[:8]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id if org else None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_ticket(db: AsyncSession, project: Project) -> Ticket:
    t = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        title="A ticket",
        status="open",
        kind="task",
        priority="medium",
        created_by_user_id=project.owner_id,
        created_at=datetime.now(timezone.utc),
    )
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return t


async def _make_service_key(
    db: AsyncSession,
    org_id: str,
    minter: User,
    scopes: list[str],
    project_ids: list[str] | None = None,
) -> str:
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=minter.id,
            key_hash=hash_api_key(raw),
            name=f"svc-{uuid.uuid4().hex[:6]}",
            is_active=True,
            key_kind="service",
            org_id=org_id,
            scopes=json.dumps(scopes),
            created_by_user_id=minter.id,
            service_key_name=f"svc-{uuid.uuid4().hex[:6]}",
            project_ids=json.dumps(project_ids) if project_ids else None,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return raw


def _wq_url(project_id: str) -> str:
    return f"/api/v1/projects/{project_id}/work-queues"


# ── create ──


@pytest.mark.parametrize(
    "mode",
    ["review_until_clean", "implement_until_done", "triage"],
)
async def test_create_valid_mode(
    client: AsyncClient, db_session: AsyncSession, test_user: User,
    auth_headers, mode: str,
):
    project = await _make_project(db_session, test_user)
    resp = await client.post(
        _wq_url(project.id),
        headers=auth_headers,
        json={"name": f"q-{mode}", "mode": mode},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["work_queue"]["mode"] == mode
    assert body["work_queue"]["status"] == "active"
    assert body["work_queue"]["lease_epoch"] == 0
    assert body["items"] == []


async def test_create_invalid_mode_422(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers
):
    project = await _make_project(db_session, test_user)
    resp = await client.post(
        _wq_url(project.id),
        headers=auth_headers,
        json={"name": "q", "mode": "nonsense"},
    )
    assert resp.status_code == 422


async def test_create_cadence_below_floor_422(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers
):
    project = await _make_project(db_session, test_user)
    resp = await client.post(
        _wq_url(project.id),
        headers=auth_headers,
        json={"name": "q", "mode": "triage", "cadence_seconds": 60},
    )
    assert resp.status_code == 422


async def test_create_max_tickets_over_cap_422(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers
):
    project = await _make_project(db_session, test_user)
    resp = await client.post(
        _wq_url(project.id),
        headers=auth_headers,
        json={"name": "q", "mode": "triage", "max_tickets_per_run": 6},
    )
    assert resp.status_code == 422


async def test_create_defaults_applied(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers
):
    project = await _make_project(db_session, test_user)
    resp = await client.post(
        _wq_url(project.id),
        headers=auth_headers,
        json={"name": "q-defaults", "mode": "triage"},
    )
    assert resp.status_code == 201, resp.text
    wq = resp.json()["work_queue"]
    assert wq["cadence_seconds"] == 300
    assert wq["max_tickets_per_run"] == 1
    assert wq["max_attempts_per_item"] == 3
    assert wq["auto_adopt"] is False
    assert wq["max_adopt_per_wake"] == 5
    assert wq["stop_condition"] == "queue_empty"


async def test_create_seeds_items_from_ticket_ids(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers
):
    project = await _make_project(db_session, test_user)
    t1 = await _make_ticket(db_session, project)
    t2 = await _make_ticket(db_session, project)
    resp = await client.post(
        _wq_url(project.id),
        headers=auth_headers,
        json={
            "name": "seeded",
            "mode": "review_until_clean",
            "selector": {"status": "review", "ticket_ids": [t1.id, t2.id]},
        },
    )
    assert resp.status_code == 201, resp.text
    items = resp.json()["items"]
    assert {i["ticket_id"] for i in items} == {t1.id, t2.id}
    assert all(i["item_status"] == "pending" for i in items)
    assert resp.json()["work_queue"]["progress"]["pending"] == 2


async def test_create_cross_project_ticket_ids_422(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers
):
    project = await _make_project(db_session, test_user)
    other = await _make_project(db_session, test_user)
    other_ticket = await _make_ticket(db_session, other)
    resp = await client.post(
        _wq_url(project.id),
        headers=auth_headers,
        json={
            "name": "leaky",
            "mode": "triage",
            "selector": {"ticket_ids": [other_ticket.id]},
        },
    )
    assert resp.status_code == 422
    assert _structured_error(resp.json())["error"] == "cross_project_ticket"


async def test_create_duplicate_name_409(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers
):
    project = await _make_project(db_session, test_user)
    payload = {"name": "dup", "mode": "triage"}
    r1 = await client.post(_wq_url(project.id), headers=auth_headers, json=payload)
    assert r1.status_code == 201
    r2 = await client.post(_wq_url(project.id), headers=auth_headers, json=payload)
    assert r2.status_code == 409
    assert _structured_error(r2.json())["error"] == "work_queue_name_conflict"


# ── list + get ──


async def test_list_and_get(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers
):
    project = await _make_project(db_session, test_user)
    await client.post(
        _wq_url(project.id), headers=auth_headers,
        json={"name": "a", "mode": "triage"},
    )
    create = await client.post(
        _wq_url(project.id), headers=auth_headers,
        json={"name": "b", "mode": "triage"},
    )
    qid = create.json()["work_queue"]["id"]

    lst = await client.get(_wq_url(project.id), headers=auth_headers)
    assert lst.status_code == 200
    assert len(lst.json()) == 2

    got = await client.get(f"{_wq_url(project.id)}/{qid}", headers=auth_headers)
    assert got.status_code == 200
    assert got.json()["id"] == qid
    assert got.json()["items"] == []


async def test_get_other_project_queue_404(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers
):
    project = await _make_project(db_session, test_user)
    other = await _make_project(db_session, test_user)
    create = await client.post(
        _wq_url(other.id), headers=auth_headers,
        json={"name": "elsewhere", "mode": "triage"},
    )
    qid = create.json()["work_queue"]["id"]
    # Same valid project in the URL, but the queue belongs to `other`.
    got = await client.get(f"{_wq_url(project.id)}/{qid}", headers=auth_headers)
    assert got.status_code == 404


# ── set_status transition matrix ──


async def _create_queue(client, auth_headers, project_id, name="lc") -> dict:
    r = await client.post(
        _wq_url(project_id), headers=auth_headers,
        json={"name": name, "mode": "triage"},
    )
    assert r.status_code == 201, r.text
    return r.json()["work_queue"]


async def _set_status(client, auth_headers, project_id, qid, status, lease=None):
    body: dict = {"status": status}
    if lease is not None:
        body["lease_epoch"] = lease
    return await client.post(
        f"{_wq_url(project_id)}/{qid}/status", headers=auth_headers, json=body
    )


async def test_status_full_transition_chain(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers
):
    project = await _make_project(db_session, test_user)
    q = await _create_queue(client, auth_headers, project.id)
    qid = q["id"]
    assert q["lease_epoch"] == 0

    r = await _set_status(client, auth_headers, project.id, qid, "paused")
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "paused"
    assert r.json()["lease_epoch"] == 1

    r = await _set_status(client, auth_headers, project.id, qid, "active")
    assert r.status_code == 200
    assert r.json()["status"] == "active"
    assert r.json()["lease_epoch"] == 2

    r = await _set_status(client, auth_headers, project.id, qid, "completed")
    assert r.status_code == 200
    assert r.json()["status"] == "completed"
    assert r.json()["lease_epoch"] == 3


async def test_status_any_to_cancelled(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers
):
    project = await _make_project(db_session, test_user)
    q = await _create_queue(client, auth_headers, project.id, name="c1")
    r = await _set_status(client, auth_headers, project.id, q["id"], "cancelled")
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


async def test_status_illegal_transition_409(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers
):
    project = await _make_project(db_session, test_user)
    q = await _create_queue(client, auth_headers, project.id, name="ill")
    qid = q["id"]
    # Cancel (terminal), then try to reactivate → illegal.
    await _set_status(client, auth_headers, project.id, qid, "cancelled")
    r = await _set_status(client, auth_headers, project.id, qid, "active")
    assert r.status_code == 409
    assert _structured_error(r.json())["error"] == "invalid_status_transition"


async def test_status_noop_rejected_409(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers
):
    project = await _make_project(db_session, test_user)
    q = await _create_queue(client, auth_headers, project.id, name="noop")
    r = await _set_status(client, auth_headers, project.id, q["id"], "active")
    assert r.status_code == 409
    assert _structured_error(r.json())["error"] == "invalid_status_transition"


async def test_status_stale_lease_epoch_409(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers
):
    project = await _make_project(db_session, test_user)
    q = await _create_queue(client, auth_headers, project.id, name="stale")
    qid = q["id"]
    # First transition bumps epoch 0 -> 1.
    ok = await _set_status(client, auth_headers, project.id, qid, "paused", lease=0)
    assert ok.status_code == 200
    # Replaying lease_epoch=0 is now stale.
    stale = await _set_status(
        client, auth_headers, project.id, qid, "active", lease=0
    )
    assert stale.status_code == 409
    assert _structured_error(stale.json())["error"] == "stale_lease_epoch"
    assert _structured_error(stale.json())["current_lease_epoch"] == 1


# ── scope enforcement (service keys) ──


async def test_read_scope_cannot_create(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _ = await _make_org_with_admin(db_session)
    project = await _make_project(db_session, admin, org)
    raw = await _make_service_key(
        db_session, org.id, admin, ["work_queues:read"]
    )
    resp = await client.post(
        _wq_url(project.id), headers=_hdrs(raw),
        json={"name": "q", "mode": "triage"},
    )
    assert resp.status_code == 403
    assert _structured_error(resp.json())["error"] == "insufficient_scope"


async def test_read_scope_cannot_set_status(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, admin_key = await _make_org_with_admin(db_session)
    project = await _make_project(db_session, admin, org)
    # Create with a write key so a queue exists.
    write_key = await _make_service_key(
        db_session, org.id, admin, ["work_queues:write"]
    )
    create = await client.post(
        _wq_url(project.id), headers=_hdrs(write_key),
        json={"name": "q", "mode": "triage"},
    )
    qid = create.json()["work_queue"]["id"]

    read_key = await _make_service_key(
        db_session, org.id, admin, ["work_queues:read"]
    )
    resp = await _set_status(client, _hdrs(read_key), project.id, qid, "paused")
    assert resp.status_code == 403
    assert _structured_error(resp.json())["error"] == "insufficient_scope"


async def test_no_work_queue_scope_denied(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _ = await _make_org_with_admin(db_session)
    project = await _make_project(db_session, admin, org)
    raw = await _make_service_key(
        db_session, org.id, admin, ["tickets:read"]
    )
    # read endpoint
    r = await client.get(_wq_url(project.id), headers=_hdrs(raw))
    assert r.status_code == 403
    assert _structured_error(r.json())["error"] == "insufficient_scope"
    # write endpoint
    w = await client.post(
        _wq_url(project.id), headers=_hdrs(raw),
        json={"name": "q", "mode": "triage"},
    )
    assert w.status_code == 403


async def test_write_scope_can_create(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _ = await _make_org_with_admin(db_session)
    project = await _make_project(db_session, admin, org)
    raw = await _make_service_key(
        db_session, org.id, admin, ["work_queues:write", "work_queues:read"]
    )
    resp = await client.post(
        _wq_url(project.id), headers=_hdrs(raw),
        json={"name": "svc-made", "mode": "triage"},
    )
    assert resp.status_code == 201, resp.text


async def test_user_wildcard_key_works(
    client: AsyncClient, db_session: AsyncSession, test_user: User, auth_headers
):
    # The default test_api_key is a user wildcard key.
    project = await _make_project(db_session, test_user)
    resp = await client.post(
        _wq_url(project.id), headers=auth_headers,
        json={"name": "wildcard", "mode": "triage"},
    )
    assert resp.status_code == 201


# ── cross-project denial via service key ──


async def test_service_key_cross_org_denied(
    client: AsyncClient, db_session: AsyncSession
):
    org_a, admin_a, _ = await _make_org_with_admin(db_session)
    org_b, admin_b, _ = await _make_org_with_admin(db_session)
    project_b = await _make_project(db_session, admin_b, org_b)
    # Key bound to org_a hitting a project in org_b.
    key_a = await _make_service_key(
        db_session, org_a.id, admin_a, ["work_queues:write", "work_queues:read"]
    )
    resp = await client.post(
        _wq_url(project_b.id), headers=_hdrs(key_a),
        json={"name": "leak", "mode": "triage"},
    )
    assert resp.status_code == 403
    assert _structured_error(resp.json())["error"] == "cross_org_denied"


async def test_service_key_project_allowlist_denied(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _ = await _make_org_with_admin(db_session)
    in_project = await _make_project(db_session, admin, org)
    out_project = await _make_project(db_session, admin, org)
    # Allowlist only `in_project`; hit `out_project`.
    key = await _make_service_key(
        db_session, org.id, admin,
        ["work_queues:write", "work_queues:read"],
        project_ids=[in_project.id],
    )
    resp = await client.post(
        _wq_url(out_project.id), headers=_hdrs(key),
        json={"name": "blocked", "mode": "triage"},
    )
    assert resp.status_code == 403
    assert _structured_error(resp.json())["error"] == "project_not_in_allowlist"
