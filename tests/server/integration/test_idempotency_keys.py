"""Idempotency-Key on POST routes (tk_bd80efd511da4a2b).

End to end on two real routes (ticket create, meeting upload) through the
real app, plus a small app with the same middleware for the paths a real
route can't be steered into (5xx, exceptions, slow handlers, big bodies).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from sessionfs.server import idempotency
from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import (
    AdminAction,
    ApiKey,
    IdempotencyKey,
    MeetingTranscript,
    Project,
    Ticket,
    User,
)
from sessionfs.server.idempotency import (
    IdempotencyMiddleware,
    request_hash,
    scope_hash,
    sweep_idempotency_keys,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _user(db: AsyncSession, *, tier: str = "team") -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"idem-{uuid.uuid4().hex[:8]}@example.com",
        display_name="Idem",
        tier=tier,
        email_verified=True,
        created_at=_now(),
    )
    db.add(user)
    await db.commit()
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name="idem",
            created_at=_now(),
        )
    )
    await db.commit()
    return user, raw


async def _project(db: AsyncSession, owner: User) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="idem",
        git_remote_normalized=f"github.com/acme/{uuid.uuid4().hex[:8]}",
        context_document="",
        owner_id=owner.id,
    )
    db.add(project)
    await db.commit()
    return project


def _h(raw: str, key: str | None = None) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {raw}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


async def _rows(db: AsyncSession) -> list[IdempotencyKey]:
    stmt = select(IdempotencyKey).execution_options(populate_existing=True)
    return list((await db.execute(stmt)).scalars().all())


# ── Real routes ──────────────────────────────────────────────


async def test_ticket_create_replays_and_writes_once(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, raw = await _user(db_session)
    project = await _project(db_session, user)
    url = f"/api/v1/projects/{project.id}/tickets"
    body = {"title": "Ship the release record", "description": "retry safe"}

    first = await client.post(url, headers=_h(raw, "tk-create-1"), json=body)
    assert first.status_code == 201, first.text
    assert "idempotent-replayed" not in first.headers

    # Same key, same body (key order differs): replayed, not re-run.
    second = await client.post(
        url,
        headers=_h(raw, "tk-create-1"),
        json={"description": "retry safe", "title": "Ship the release record"},
    )
    assert second.status_code == 201
    assert second.headers["idempotent-replayed"] == "true"
    assert second.content == first.content
    assert second.headers["content-type"] == first.headers["content-type"]

    count = (
        await db_session.execute(
            select(func.count()).select_from(Ticket).where(Ticket.project_id == project.id)
        )
    ).scalar_one()
    assert count == 1
    rows = await _rows(db_session)
    assert len(rows) == 1
    assert rows[0].status == "completed" and rows[0].replayable is True
    assert rows[0].response_status == 201
    # The raw credential is never stored.
    assert raw not in repr(rows[0].__dict__)

    # A different body under the same key is refused, and writes nothing.
    reused = await client.post(url, headers=_h(raw, "tk-create-1"), json={"title": "Other"})
    assert reused.status_code == 422
    assert reused.json()["error"]["code"] == "idempotency_key_reused"

    # A revoked credential can't read back what it stored.
    key_row = (
        await db_session.execute(select(ApiKey).where(ApiKey.key_hash == hash_api_key(raw)))
    ).scalar_one()
    key_row.revoked_at = _now()
    key_row.is_active = False
    await db_session.commit()
    revoked = await client.post(url, headers=_h(raw, "tk-create-1"), json=body)
    assert revoked.status_code == 401
    assert "idempotent-replayed" not in revoked.headers
    key_row.revoked_at = None
    key_row.is_active = True
    await db_session.commit()

    # A deactivated account whose key is still active can't read it back
    # either: the replay never reaches the route's own auth.
    user.is_active = False
    await db_session.commit()
    deactivated = await client.post(url, headers=_h(raw, "tk-create-1"), json=body)
    assert deactivated.status_code in (401, 403)
    assert "idempotent-replayed" not in deactivated.headers
    user.is_active = True
    await db_session.commit()

    # No header: today's behaviour (a second ticket).
    plain = await client.post(url, headers=_h(raw), json=body)
    assert plain.status_code == 201
    count = (
        await db_session.execute(
            select(func.count()).select_from(Ticket).where(Ticket.project_id == project.id)
        )
    ).scalar_one()
    assert count == 2


async def test_meeting_upload_replays_and_writes_once(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, raw = await _user(db_session)
    project = await _project(db_session, user)
    url = f"/api/v1/projects/{project.id}/meetings"
    body = {"title": "Planning", "content": "Alice: plan\nBob: ship", "duration_minutes": 30}

    first = await client.post(url, headers=_h(raw, "mtg-1"), json=body)
    assert first.status_code == 200, first.text
    second = await client.post(url, headers=_h(raw, "mtg-1"), json=body)
    assert second.status_code == 200
    assert second.headers["idempotent-replayed"] == "true"
    assert second.json() == first.json()

    count = (
        await db_session.execute(
            select(func.count())
            .select_from(MeetingTranscript)
            .where(MeetingTranscript.project_id == project.id)
        )
    ).scalar_one()
    assert count == 1

    # Another credential using the same key runs its own request.
    other_raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(other_raw),
            name="second",
            created_at=_now(),
        )
    )
    await db_session.commit()
    third = await client.post(url, headers=_h(other_raw, "mtg-1"), json=body)
    assert third.status_code == 200
    assert "idempotent-replayed" not in third.headers
    assert third.json()["id"] != first.json()["id"]


async def test_in_progress_key_gets_409_on_real_route(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, raw = await _user(db_session)
    project = await _project(db_session, user)
    path = f"/api/v1/projects/{project.id}/tickets"
    body = b'{"title":"Busy"}'
    db_session.add(
        IdempotencyKey(
            scope_hash=scope_hash(raw, "POST", "/api/v1/projects/{project_id}/tickets", "busy"),
            request_hash=request_hash("POST", path, b"", body, "application/json"),
            status="in_progress",
            lease_token="t" * 32,
            created_at=_now(),
            locked_until=_now() + timedelta(minutes=5),
        )
    )
    await db_session.commit()
    resp = await client.post(path, headers=_h(raw, "busy"), json={"title": "Busy"})
    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "idempotency_in_progress"
    assert (
        await db_session.execute(select(func.count()).select_from(Ticket))
    ).scalar_one() == 0


async def test_stale_lease_is_taken_over(client: AsyncClient, db_session: AsyncSession) -> None:
    user, raw = await _user(db_session)
    project = await _project(db_session, user)
    path = f"/api/v1/projects/{project.id}/tickets"
    body = b'{"title":"Stale"}'
    s_hash = scope_hash(raw, "POST", "/api/v1/projects/{project_id}/tickets", "stale")
    db_session.add(
        IdempotencyKey(
            scope_hash=s_hash,
            request_hash=request_hash("POST", path, b"", body, "application/json"),
            status="in_progress",
            lease_token="s" * 32,
            created_at=_now() - timedelta(minutes=10),
            locked_until=_now() - timedelta(minutes=1),
        )
    )
    await db_session.commit()
    resp = await client.post(path, headers=_h(raw, "stale"), json={"title": "Stale"})
    assert resp.status_code == 201, resp.text
    rows = await _rows(db_session)
    assert len(rows) == 1 and rows[0].status == "completed" and rows[0].lease_token is None


async def test_excluded_secret_route_ignores_header(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, raw = await _user(db_session)
    first = await client.post(
        "/api/v1/auth/me/api-keys", headers=_h(raw, "mint-1"), json={"name": "a"}
    )
    second = await client.post(
        "/api/v1/auth/me/api-keys", headers=_h(raw, "mint-1"), json={"name": "a"}
    )
    assert first.status_code == second.status_code
    assert first.status_code < 300, first.text
    assert "idempotent-replayed" not in second.headers
    assert first.content != second.content  # two keys minted, nothing stored
    assert await _rows(db_session) == []


async def test_invalid_key_and_unauthenticated(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    user, raw = await _user(db_session)
    project = await _project(db_session, user)
    url = f"/api/v1/projects/{project.id}/tickets"

    bad = await client.post(url, headers=_h(raw, "x" * 256), json={"title": "t"})
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "idempotency_key_invalid"
    ctrl = await client.post(url, headers=_h(raw, "bad\tkey"), json={"title": "t"})
    assert ctrl.status_code == 400

    # A credential that doesn't authenticate leaves nothing behind.
    wrong = await client.post(url, headers=_h("sk_sfs_" + "0" * 32, "k"), json={"title": "t"})
    assert wrong.status_code == 401
    assert await _rows(db_session) == []

    # No credential at all: header ignored.
    none = await client.post(url, headers={"Idempotency-Key": "k"}, json={"title": "t"})
    assert none.status_code == 401
    assert await _rows(db_session) == []


async def test_sweep_deletes_rows_older_than_a_day(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, raw = await _user(db_session, tier="admin")
    old = _now() - timedelta(hours=25)
    for i in range(3):
        db_session.add(
            IdempotencyKey(
                scope_hash=f"{i:064x}",
                request_hash="0" * 64,
                status="completed",
                replayable=False,
                created_at=old,
            )
        )
    db_session.add(
        IdempotencyKey(
            scope_hash="f" * 64,
            request_hash="0" * 64,
            status="completed",
            replayable=False,
            created_at=_now() - timedelta(hours=23),
        )
    )
    await db_session.commit()

    resp = await client.post(
        "/api/v1/admin/sweeps/idempotency-keys", headers=_h(raw), params={"batch_size": 2}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": 3, "batches": 2}
    rows = await _rows(db_session)
    assert [r.scope_hash for r in rows] == ["f" * 64]
    action = (
        await db_session.execute(
            select(AdminAction).where(AdminAction.action == "idempotency_keys_sweep")
        )
    ).scalar_one()
    assert action.admin_id == admin.id

    # Idempotent: nothing left to delete.
    again = await sweep_idempotency_keys(db_session)
    assert again == {"deleted": 0, "batches": 0}


# ── Small app: behaviours a real route can't be steered into ─────────


class _Calls:
    def __init__(self) -> None:
        self.count = 0
        self.fail_next = 0
        self.raise_next = 0
        self.gate: asyncio.Event | None = None
        self.started = asyncio.Event()


@pytest.fixture
async def mini(db_engine, db_session, test_user):
    for raw in ("good-a", "good-b"):
        db_session.add(
            ApiKey(
                id=str(uuid.uuid4()),
                user_id=test_user.id,
                key_hash=hash_api_key(raw),
                name=raw,
                created_at=_now(),
            )
        )
    await db_session.commit()
    calls = _Calls()
    app = FastAPI()
    app.add_middleware(IdempotencyMiddleware)
    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db

    def _auth(request: Request) -> bool:
        if request.headers.get("authorization", "").startswith("Bearer good"):
            request.state.auth_context = object()
            return True
        return False

    @app.post("/things")
    async def create_thing(request: Request):
        if not _auth(request):
            return JSONResponse({"detail": "no"}, status_code=401)
        await request.body()
        calls.count += 1
        calls.started.set()
        if calls.gate is not None:
            await calls.gate.wait()
        if calls.raise_next:
            calls.raise_next -= 1
            raise RuntimeError("boom")
        if calls.fail_next:
            calls.fail_next -= 1
            return JSONResponse({"error": "down"}, status_code=503)
        return JSONResponse(
            {"n": calls.count}, status_code=201, headers={"ETag": f'"{calls.count}"', "X-Other": "1"}
        )

    @app.post("/big")
    async def big(request: Request):
        _auth(request)
        calls.count += 1
        return Response(b"x" * (idempotency.MAX_RESPONSE_BYTES + 1), media_type="text/plain")

    @app.post("/echo")
    async def echo(request: Request):
        _auth(request)
        calls.count += 1
        body = await request.body()
        return Response(body, media_type="application/octet-stream")

    @app.post("/limited")
    async def limited(request: Request):
        _auth(request)
        calls.count += 1
        return JSONResponse({"error": "slow down"}, status_code=429)

    @app.get("/things")
    async def list_things():
        return {"n": calls.count}

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c, calls, factory


async def test_replay_carries_only_safe_headers(mini) -> None:
    c, calls, _ = mini
    first = await c.post("/things", headers=_h("good-a", "k1"), json={"a": 1})
    second = await c.post("/things", headers=_h("good-a", "k1"), json={"a": 1})
    assert first.status_code == second.status_code == 201
    assert second.json() == first.json() == {"n": 1}
    assert second.headers["etag"] == '"1"'
    assert "x-other" not in second.headers
    assert second.headers["idempotent-replayed"] == "true"
    assert calls.count == 1


async def test_5xx_and_exception_release_the_key(mini) -> None:
    c, calls, factory = mini
    calls.fail_next = 1
    first = await c.post("/things", headers=_h("good-a", "k5"), json={})
    assert first.status_code == 503
    retry = await c.post("/things", headers=_h("good-a", "k5"), json={})
    assert retry.status_code == 201
    assert "idempotent-replayed" not in retry.headers
    assert calls.count == 2

    calls.raise_next = 1
    crashed = await c.post("/things", headers=_h("good-a", "kx"), json={})
    assert crashed.status_code == 500
    async with factory() as s:
        assert (
            await s.execute(select(func.count()).select_from(IdempotencyKey))
        ).scalar_one() == 1  # only k5's completed row
    after = await c.post("/things", headers=_h("good-a", "kx"), json={})
    assert after.status_code == 201
    assert calls.count == 4


async def test_concurrent_same_key_one_409(mini) -> None:
    c, calls, _ = mini
    calls.gate = asyncio.Event()
    first = asyncio.create_task(c.post("/things", headers=_h("good-a", "kc"), json={"x": 1}))
    await asyncio.wait_for(calls.started.wait(), 10)
    second = await c.post("/things", headers=_h("good-a", "kc"), json={"x": 1})
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "idempotency_in_progress"
    calls.gate.set()
    done = await first
    assert done.status_code == 201
    assert calls.count == 1
    replay = await c.post("/things", headers=_h("good-a", "kc"), json={"x": 1})
    assert replay.headers["idempotent-replayed"] == "true"
    assert replay.json() == done.json()


async def test_credentials_are_scoped(mini) -> None:
    c, calls, _ = mini
    a = await c.post("/things", headers=_h("good-a", "same"), json={})
    b = await c.post("/things", headers=_h("good-b", "same"), json={})
    assert a.status_code == b.status_code == 201
    assert a.json() != b.json()
    assert calls.count == 2


async def test_big_response_is_not_replayable(mini) -> None:
    c, calls, _ = mini
    first = await c.post("/big", headers=_h("good-a", "kb"))
    assert first.status_code == 200
    assert len(first.content) == idempotency.MAX_RESPONSE_BYTES + 1
    again = await c.post("/big", headers=_h("good-a", "kb"))
    assert again.status_code == 409
    assert again.json()["error"]["code"] == "idempotency_response_unavailable"
    assert calls.count == 1


async def test_body_reaches_route_unchanged_and_big_bodies_pass_through(
    mini, monkeypatch
) -> None:
    c, calls, factory = mini
    payload = bytes(range(256)) * 64
    resp = await c.post("/echo", headers=_h("good-a", "ke"), content=payload)
    assert resp.content == payload

    monkeypatch.setattr(idempotency, "MAX_BODY_BYTES", 1024)
    big = await c.post("/echo", headers=_h("good-a", "kbig"), content=payload)
    assert big.content == payload
    again = await c.post("/echo", headers=_h("good-a", "kbig"), content=payload)
    assert "idempotent-replayed" not in again.headers
    assert calls.count == 3
    async with factory() as s:
        keys = (await s.execute(select(IdempotencyKey))).scalars().all()
        assert len(keys) == 1  # only "ke"


async def test_transient_4xx_and_non_post_are_not_stored(mini) -> None:
    c, calls, factory = mini
    for _ in range(2):
        r = await c.post("/limited", headers=_h("good-a", "kl"))
        assert r.status_code == 429
    assert calls.count == 2
    got = await c.get("/things", headers=_h("good-a", "kl"))
    assert got.status_code == 200
    async with factory() as s:
        assert (
            await s.execute(select(func.count()).select_from(IdempotencyKey))
        ).scalar_one() == 0


async def test_unauthenticated_request_leaves_no_row(mini) -> None:
    c, calls, factory = mini
    r = await c.post("/things", headers=_h("nope", "ku"), json={})
    assert r.status_code == 401
    async with factory() as s:
        assert (
            await s.execute(select(func.count()).select_from(IdempotencyKey))
        ).scalar_one() == 0


def test_exclusions_and_leases() -> None:
    assert idempotency.is_excluded("/api/v1/webhooks/github/{org_id}")
    assert idempotency.is_excluded("/webhooks/stripe")
    assert idempotency.is_excluded("/api/v1/helper/turns")
    assert idempotency.is_excluded("/api/v1/sessions")
    assert idempotency.is_excluded("/api/v1/uploads/{upload_id}/complete")
    assert idempotency.is_excluded("/api/v1/orgs/{org_id}/service-keys")
    assert not idempotency.is_excluded("/api/v1/projects/{project_id}/tickets")
    assert not idempotency.is_excluded("/api/v1/projects/{project_id}/releases")
    assert idempotency.lease_for("/api/v1/meetings/{meeting_id}/extract") == idempotency.LONG_LEASE
    assert (
        idempotency.lease_for("/api/v1/projects/{project_id}/tickets") == idempotency.DEFAULT_LEASE
    )


def test_every_excluded_template_is_a_real_post_route() -> None:
    from sessionfs.server.app import create_app
    from sessionfs.server.config import ServerConfig

    app = create_app(ServerConfig(database_url="sqlite+aiosqlite://"))
    from sessionfs.server.route_introspection import iter_routes

    posts = {e.path for e in iter_routes(app) if "POST" in e.methods}
    assert idempotency.EXCLUDED_TEMPLATES <= posts, idempotency.EXCLUDED_TEMPLATES - posts
