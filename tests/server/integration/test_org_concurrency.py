"""Per-org concurrency caps (services/org_concurrency.py) and the heavy-read
statement timeout mapping (db/timeouts.py, errors.py).

Slots are rows in ``org_concurrency_slots``: claim, cap, release, the holder
fence, stale reclaim, the personal-project key, the 429 envelope through a
real route, and that a resource the caller can't see still answers 404.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server import metrics
from sessionfs.server.db import timeouts
from sessionfs.server.db.models import OrgConcurrencySlot, Project, User
from sessionfs.server.errors import register_exception_handlers
from sessionfs.server.services import org_concurrency as oc


@pytest.fixture(autouse=True)
def _limits():
    oc.reset_limits()
    metrics.reset_registry()
    yield
    oc.reset_limits()
    metrics.reset_registry()
    timeouts.configure_heavy_read_timeout(timeouts.DEFAULT_HEAVY_READ_TIMEOUT_MS)


async def _count(db: AsyncSession, key: str | None = None) -> int:
    stmt = select(func.count()).select_from(OrgConcurrencySlot)
    if key is not None:
        stmt = stmt.where(OrgConcurrencySlot.scope_key == key)
    return (await db.execute(stmt)).scalar_one()


def _rejections(route_class: str) -> float:
    return metrics.ORG_CONCURRENCY_REJECTED_TOTAL._values.get(
        (route_class, "", ""), 0.0
    )


# ── slot claim / release / reclaim ──────────────────────────────────────────


async def test_claims_up_to_the_cap_then_refuses_with_429(db_session: AsyncSession) -> None:
    oc.configure_limits({"compile": 2})
    first = await oc.acquire_org_slot(db_session, "compile", org_id="org_a", user_id="u1")
    second = await oc.acquire_org_slot(db_session, "compile", org_id="org_a", user_id="u2")
    assert {first.slot_no, second.slot_no} == {0, 1}
    assert await _count(db_session, "org:org_a") == 2

    with pytest.raises(HTTPException) as exc:
        await oc.acquire_org_slot(db_session, "compile", org_id="org_a", user_id="u3")
    assert exc.value.status_code == 429
    assert exc.value.detail["code"] == "org_concurrency_limit"
    assert exc.value.detail["route_class"] == "compile"
    assert exc.value.detail["limit"] == 2
    assert exc.value.headers == {"Retry-After": "30"}
    assert _rejections("compile") == 1

    # Another org and another route class are counted separately.
    other = await oc.acquire_org_slot(db_session, "compile", org_id="org_b", user_id="u1")
    export = await oc.acquire_org_slot(db_session, "export", org_id="org_a", user_id="u1")

    await first.release()
    await first.release()  # idempotent
    third = await oc.acquire_org_slot(db_session, "compile", org_id="org_a", user_id="u3")
    assert third.slot_no == first.slot_no
    for slot in (second, third, other, export):
        await slot.release()
    assert await _count(db_session) == 0


async def test_personal_scope_keys_on_the_owner(db_session: AsyncSession) -> None:
    oc.configure_limits({"helper": 1})
    slot = await oc.acquire_org_slot(db_session, "helper", org_id=None, user_id="owner_1")
    assert slot.scope_key == "user:owner_1"
    with pytest.raises(HTTPException):
        await oc.acquire_org_slot(db_session, "helper", org_id=None, user_id="owner_1")
    # A different person is a different scope.
    other = await oc.acquire_org_slot(db_session, "helper", org_id=None, user_id="owner_2")
    await slot.release()
    await other.release()


async def test_stale_slot_is_reclaimed_and_the_old_holder_is_fenced(
    db_session: AsyncSession,
) -> None:
    oc.configure_limits({"extraction": 1})
    dead = await oc.acquire_org_slot(db_session, "extraction", org_id="org_a", user_id="u1")
    with pytest.raises(HTTPException):
        await oc.acquire_org_slot(db_session, "extraction", org_id="org_a", user_id="u2")

    # The holder died: its lease runs out.
    await db_session.execute(
        update(OrgConcurrencySlot).values(
            expires_at=datetime.now(timezone.utc) - timedelta(seconds=1)
        )
    )
    await db_session.commit()
    taker = await oc.acquire_org_slot(db_session, "extraction", org_id="org_a", user_id="u2")
    assert taker.slot_no == dead.slot_no and taker.holder != dead.holder

    # The old holder finishing late must not free the new holder's slot.
    await dead.release()
    assert await _count(db_session, "org:org_a") == 1
    with pytest.raises(HTTPException):
        await oc.acquire_org_slot(db_session, "extraction", org_id="org_a", user_id="u3")
    await taker.release()
    assert await _count(db_session, "org:org_a") == 0


async def test_cap_zero_turns_it_off(db_session: AsyncSession) -> None:
    oc.configure_limits({"export": 0})
    slots = [
        await oc.acquire_org_slot(db_session, "export", org_id="org_a", user_id="u")
        for _ in range(5)
    ]
    assert all(not s.held for s in slots)
    assert await _count(db_session) == 0


async def test_lowered_cap_ignores_higher_numbered_slots(db_session: AsyncSession) -> None:
    oc.configure_limits({"compile": 3})
    held = [
        await oc.acquire_org_slot(db_session, "compile", org_id="org_a", user_id="u")
        for _ in range(3)
    ]
    oc.configure_limits({"compile": 1})
    await held[0].release()
    again = await oc.acquire_org_slot(db_session, "compile", org_id="org_a", user_id="u")
    assert again.slot_no == 0
    with pytest.raises(HTTPException):
        await oc.acquire_org_slot(db_session, "compile", org_id="org_a", user_id="u")
    for slot in (again, *held[1:]):
        await slot.release()


async def test_release_soon_frees_the_slot(db_session: AsyncSession) -> None:
    oc.configure_limits({"helper": 1})
    slot = await oc.acquire_org_slot(db_session, "helper", org_id="org_a", user_id="u")
    slot.release_soon()
    slot.release_soon()
    await oc.drain_pending_releases()
    assert await _count(db_session) == 0


async def test_context_manager_releases_on_error(db_session: AsyncSession) -> None:
    with pytest.raises(RuntimeError):
        async with oc.org_slot(db_session, "export", org_id="org_a", user_id=None):
            assert await _count(db_session) == 1
            raise RuntimeError("boom")
    assert await _count(db_session) == 0


async def test_claim_fails_open_when_the_table_is_missing(db_session: AsyncSession) -> None:
    await db_session.execute(text("DROP TABLE org_concurrency_slots"))
    await db_session.commit()
    slot = await oc.acquire_org_slot(db_session, "compile", org_id="org_a", user_id="u")
    assert not slot.held
    await slot.release()


def test_metric_carries_the_org_id_only_with_tenant_labels() -> None:
    from sessionfs.server.business_metrics import record_org_concurrency_rejection

    metrics.set_tenant_labels(False)
    record_org_concurrency_rejection("helper", "org_a")
    record_org_concurrency_rejection("not-a-class", "org_a")
    assert metrics.ORG_CONCURRENCY_REJECTED_TOTAL._values == {
        ("helper", "", ""): 1.0,
        ("other", "", ""): 1.0,
    }
    metrics.set_tenant_labels(True)
    try:
        record_org_concurrency_rejection("helper", "org_a")
        assert metrics.ORG_CONCURRENCY_REJECTED_TOTAL._values[("helper", "org_a", "")] == 1.0
    finally:
        metrics.set_tenant_labels(False)


# ── through a real route ────────────────────────────────────────────────────


@pytest.fixture
async def personal_project(db_session: AsyncSession, test_user: User) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="Capped",
        git_remote_normalized=f"github.com/example/{uuid.uuid4().hex[:8]}",
        context_document="# Project Context\n",
        owner_id=test_user.id,
    )
    db_session.add(project)
    await db_session.commit()
    return project


async def _fill(db: AsyncSession, key: str, route_class: str, n: int) -> None:
    now = datetime.now(timezone.utc)
    for i in range(n):
        db.add(
            OrgConcurrencySlot(
                scope_key=key,
                route_class=route_class,
                slot_no=i,
                holder=f"h{i}",
                acquired_at=now,
                expires_at=now + timedelta(minutes=10),
            )
        )
    await db.commit()


async def test_compile_over_the_cap_is_429_with_retry_after(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    personal_project: Project,
    test_user: User,
) -> None:
    await _fill(db_session, f"user:{test_user.id}", "compile", oc.limit_for("compile"))
    resp = await client.post(
        f"/api/v1/projects/{personal_project.id}/compile", headers=auth_headers, json={}
    )
    assert resp.status_code == 429, resp.text
    assert resp.headers["Retry-After"] == "30"
    err = resp.json()["error"]
    assert err["code"] == "org_concurrency_limit"
    assert err["details"] == {"route_class": "compile", "limit": 2}

    rebuild = await client.post(
        f"/api/v1/projects/{personal_project.id}/rebuild", headers=auth_headers, json={}
    )
    assert rebuild.status_code == 429


async def test_compile_frees_its_slot_when_it_finishes(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
    personal_project: Project,
) -> None:
    resp = await client.post(
        f"/api/v1/projects/{personal_project.id}/compile", headers=auth_headers, json={}
    )
    assert resp.status_code == 200, resp.text
    assert await _count(db_session) == 0


async def test_invisible_project_is_404_even_when_the_cap_is_full(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
) -> None:
    stranger = User(
        id=str(uuid.uuid4()),
        email=f"{uuid.uuid4().hex[:8]}@example.com",
        tier="pro",
        created_at=datetime.now(timezone.utc),
        email_verified=True,
    )
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name="Not yours",
        git_remote_normalized=f"github.com/other/{uuid.uuid4().hex[:8]}",
        context_document="",
        owner_id=stranger.id,
    )
    db_session.add_all([stranger, project])
    await db_session.commit()
    await _fill(db_session, f"user:{stranger.id}", "compile", 2)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/compile", headers=auth_headers, json={}
    )
    assert resp.status_code == 404
    await db_session.execute(delete(OrgConcurrencySlot))
    await db_session.commit()


# ── statement timeouts ─────────────────────────────────────────────────────


class _Canceled(Exception):
    sqlstate = "57014"


def _timeout_app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/slow")
    async def slow():
        raise DBAPIError("SELECT pg_sleep(60)", None, _Canceled("canceling statement"))

    @app.get("/other")
    async def other():
        class _Other(Exception):
            sqlstate = "42P01"

        raise DBAPIError("SELECT", None, _Other("missing table"))

    return app


async def test_statement_timeout_maps_to_503_query_timeout() -> None:
    transport = ASGITransport(app=_timeout_app(), raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://t") as c:
        resp = await c.get("/slow")
        assert resp.status_code == 503
        assert resp.headers["Retry-After"] == "5"
        assert resp.json()["error"]["code"] == "query_timeout"
        assert "pg_sleep" not in resp.text
        # Any other database error still takes the 500 path.
        assert (await c.get("/other")).status_code == 500


async def test_bound_heavy_reads_is_a_noop_on_sqlite(db_session: AsyncSession) -> None:
    await timeouts.bound_heavy_reads(db_session)
    assert "sfs_statement_timeout_ms" not in db_session.sync_session.info
    assert (await db_session.execute(text("SELECT 1"))).scalar() == 1


async def test_bound_heavy_reads_sets_every_transaction(
    db_session: AsyncSession, monkeypatch
) -> None:
    """The listener re-applies the bound on every transaction the session
    begins, so a route that commits part-way keeps it. A harmless statement
    stands in for SET LOCAL (SQLite has no statement_timeout)."""
    seen: list[int] = []
    monkeypatch.setattr(timeouts, "_is_postgres", lambda db: True)

    def fake_statement(ms: int) -> str:
        seen.append(ms)
        return "SELECT 1"

    monkeypatch.setattr(timeouts, "_statement", fake_statement)
    await db_session.execute(text("SELECT 1"))  # a transaction is open
    await timeouts.bound_heavy_reads(db_session, 1234)
    assert seen == [1234]  # applied to the open transaction now
    await db_session.commit()
    await db_session.execute(text("SELECT 1"))  # new transaction
    assert seen == [1234, 1234]
    await timeouts.bound_heavy_reads(db_session, 99)  # re-bound, one listener
    await db_session.commit()
    await db_session.execute(text("SELECT 1"))
    assert seen[-1] == 99 and seen.count(99) == 2

    timeouts.configure_heavy_read_timeout(0)
    seen.clear()
    fresh = AsyncSession(bind=db_session.bind)
    try:
        await timeouts.bound_heavy_reads(fresh, 500)
        await fresh.execute(text("SELECT 1"))
        assert seen == []
    finally:
        await fresh.close()
