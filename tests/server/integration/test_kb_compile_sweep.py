"""Scheduled KB compile sweep + compile-status endpoint (GitHub #93).

Covers the selection rules (schedule due + pending-claim threshold), the
deterministic-only guarantee (the sweep never calls the LLM), per-project
failure isolation, idempotency, and the dashboard's compile-status signal.
The auth matrix lives in test_platform_sweeps_scope.py (the sweep is one of
the ``platform_sweeps:run`` endpoints)."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from sessionfs.server.db.models import ContextCompilation, KnowledgeEntry, Project, User
from sessionfs.server.services.scheduled_compile import (
    select_due_projects,
    run_kb_compile_sweep,
)

pytestmark = pytest.mark.asyncio


async def _project(
    db_session: AsyncSession,
    user: User,
    *,
    schedule: str = "nightly",
    after_claims: int | None = None,
    last_scheduled: datetime | None = None,
    id: str | None = None,
) -> Project:
    project = Project(
        id=id or f"proj_{uuid.uuid4().hex[:16]}",
        name="Sweep Project",
        git_remote_normalized=f"github.com/example/{uuid.uuid4().hex}",
        owner_id=user.id,
        compile_schedule=schedule,
        compile_after_claims=after_claims,
        last_scheduled_compile_at=last_scheduled,
    )
    db_session.add(project)
    await db_session.commit()
    await db_session.refresh(project)
    return project


async def _add_claims(
    db_session: AsyncSession, project: Project, user: User, n: int
) -> None:
    for i in range(n):
        db_session.add(
            KnowledgeEntry(
                project_id=project.id,
                session_id=f"ses_{uuid.uuid4().hex[:8]}",
                user_id=user.id,
                entry_type="decision",
                content=f"Claim {i} for {project.id}",
                confidence=0.9,
                claim_class="claim",
                freshness_class="current",
            )
        )
    await db_session.commit()


async def _add_evidence(
    db_session: AsyncSession, project: Project, user: User, n: int = 1
) -> None:
    """Auto-promotable evidence: claim_class='evidence', confidence >= 0.5,
    content >= 30 chars, current freshness, not dismissed."""
    for i in range(n):
        db_session.add(
            KnowledgeEntry(
                project_id=project.id,
                session_id=f"ses_{uuid.uuid4().hex[:8]}",
                user_id=user.id,
                entry_type="discovery",
                content=f"Evidence {i} for {project.id} that is long enough to promote",
                confidence=0.7,
                claim_class="evidence",
                freshness_class="current",
            )
        )
    await db_session.commit()


async def _refetch_project(db_session: AsyncSession, project_id: str) -> Project:
    """Re-read a project row, bypassing a stale identity-map copy (the sweep
    compiles in a separate session, so the caller's session hasn't seen the
    writes)."""
    result = await db_session.execute(
        select(Project)
        .where(Project.id == project_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


# ---------------------------------------------------------------------------
# Selection
# ---------------------------------------------------------------------------


async def test_select_due_projects_schedule_and_threshold(
    db_session: AsyncSession, test_user: User
) -> None:
    now = datetime.now(timezone.utc)

    # nightly, never compiled → due
    nightly_never = await _project(db_session, test_user, schedule="nightly")
    await _add_claims(db_session, nightly_never, test_user, 1)

    # weekly, last compile 8 days ago → due
    weekly_overdue = await _project(
        db_session, test_user, schedule="weekly", last_scheduled=now - timedelta(days=8)
    )
    await _add_claims(db_session, weekly_overdue, test_user, 1)

    # weekly, last compile 1 day ago → not due
    weekly_recent = await _project(
        db_session, test_user, schedule="weekly", last_scheduled=now - timedelta(days=1)
    )
    await _add_claims(db_session, weekly_recent, test_user, 1)

    # off, threshold reached → due regardless of schedule
    off_threshold = await _project(db_session, test_user, schedule="off", after_claims=3)
    await _add_claims(db_session, off_threshold, test_user, 3)

    # off, threshold not reached → not due
    off_below = await _project(db_session, test_user, schedule="off", after_claims=3)
    await _add_claims(db_session, off_below, test_user, 2)

    # no pending claims → never selected, even though schedule is due
    no_pending = await _project(db_session, test_user, schedule="nightly")

    due = await select_due_projects(db_session, now=now, limit=100)
    ids = {pid for pid, _pending, _owner in due}

    assert nightly_never.id in ids
    assert weekly_overdue.id in ids
    assert weekly_recent.id not in ids
    assert off_threshold.id in ids
    assert off_below.id not in ids
    assert no_pending.id not in ids


async def test_select_due_projects_counts_auto_promotable_evidence(
    db_session: AsyncSession, test_user: User
) -> None:
    """GitHub #93 review round 1 MEDIUM — a project whose only new material
    is auto-promotable evidence is still due a compile (the evidence promotes
    to a claim and folds in during the same housekeeping pass)."""
    now = datetime.now(timezone.utc)
    project = await _project(db_session, test_user, schedule="nightly")
    await _add_evidence(db_session, project, test_user, n=1)

    due = await select_due_projects(db_session, now=now, limit=100)
    by_id = {pid: pending for pid, pending, _owner in due}
    assert project.id in by_id
    assert by_id[project.id] == 1


async def test_select_due_projects_skips_plans_without_project_context(
    db_session: AsyncSession, test_user: User
) -> None:
    """GitHub #93 review round 1 MEDIUM — Free/Starter projects are never
    compiled by the sweep (their plan does not pay for project_context)."""
    now = datetime.now(timezone.utc)
    free_user = User(
        id=str(uuid.uuid4()),
        email=f"free-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Free Owner",
        tier="free",
        created_at=now,
        email_verified=True,
    )
    starter_user = User(
        id=str(uuid.uuid4()),
        email=f"starter-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Starter Owner",
        tier="starter",
        created_at=now,
        email_verified=True,
    )
    db_session.add_all([free_user, starter_user])
    await db_session.commit()

    free_project = await _project(db_session, free_user, schedule="nightly")
    await _add_claims(db_session, free_project, free_user, 1)
    starter_project = await _project(db_session, starter_user, schedule="nightly")
    await _add_claims(db_session, starter_project, starter_user, 1)
    pro_project = await _project(db_session, test_user, schedule="nightly")
    await _add_claims(db_session, pro_project, test_user, 1)

    due = await select_due_projects(db_session, now=now, limit=100)
    ids = {pid for pid, _pending, _owner in due}
    assert free_project.id not in ids
    assert starter_project.id not in ids
    assert pro_project.id in ids


async def test_select_due_projects_overfetch_absorbs_tier_filter(
    db_session: AsyncSession, test_user: User
) -> None:
    """GitHub #93 review round 2 LOW — selection applies a SQL LIMIT
    (SELECT_OVERFETCH_FACTOR × limit) BEFORE the Python tier filter, so a
    Free/Starter candidate that sorts first cannot starve the batch."""
    now = datetime.now(timezone.utc)
    free_user = User(
        id=str(uuid.uuid4()),
        email=f"free-overfetch-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Free Owner",
        tier="free",
        created_at=now,
        email_verified=True,
    )
    db_session.add(free_user)
    await db_session.commit()

    # The Free projects sort first (all never-compiled, so ordered by id), and
    # the pro project sorts last. A 1× limit would fetch only the first Free
    # row and drop it; the overfetch factor fetches all three.
    free_a = await _project(
        db_session, free_user, schedule="nightly", id="proj_000000000000000a"
    )
    await _add_claims(db_session, free_a, free_user, 1)
    free_b = await _project(
        db_session, free_user, schedule="nightly", id="proj_000000000000000b"
    )
    await _add_claims(db_session, free_b, free_user, 1)
    pro_project = await _project(
        db_session, test_user, schedule="nightly", id="proj_000000000000000c"
    )
    await _add_claims(db_session, pro_project, test_user, 1)

    due = await select_due_projects(db_session, now=now, limit=1)
    assert [pid for pid, _pending, _owner in due] == [pro_project.id]


async def test_plan_resolution_chunks_in_lists() -> None:
    """GitHub #93 review round 2 LOW — bulk plan resolution chunks every
    ``IN (...)`` lookup at 1000 ids, so a large due set never trips SQLite's
    host-parameter limit."""
    from sessionfs.server.tier_gate import _IN_CHUNK_SIZE, _chunks

    assert _chunks(set()) == []
    ids = {f"id_{i}" for i in range(_IN_CHUNK_SIZE * 2 + 7)}
    chunks = _chunks(ids)
    assert len(chunks) == 3
    assert [len(c) for c in chunks] == [_IN_CHUNK_SIZE, _IN_CHUNK_SIZE, 7]
    assert sorted(c for chunk in chunks for c in chunk) == sorted(ids)


# ---------------------------------------------------------------------------
# Sweep execution (file-backed: the sweep opens one session per project, which
# the shared in-memory engine cannot service on a single connection).
# ---------------------------------------------------------------------------


@pytest.fixture
async def file_engine(tmp_path):
    from sessionfs.server.db.engine import install_sqlite_unicode_lower
    from sessionfs.server.db.models import Base

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'sweep.db'}")
    install_sqlite_unicode_lower(engine)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def sweep_session(file_engine):
    factory = async_sessionmaker(file_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


@pytest.fixture
def sweep_factory(file_engine):
    return async_sessionmaker(file_engine, expire_on_commit=False)


async def _sweep_user(sweep_session: AsyncSession) -> User:
    user = User(
        id=str(uuid.uuid4()),
        email=f"sweep-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Sweep Owner",
        tier="pro",
        created_at=datetime.now(timezone.utc),
        email_verified=True,
    )
    sweep_session.add(user)
    await sweep_session.commit()
    await sweep_session.refresh(user)
    return user


async def test_sweep_compiles_deterministically_and_never_calls_llm(
    sweep_session: AsyncSession, sweep_factory, monkeypatch
) -> None:
    import sessionfs.server.services.compiler as compiler_mod

    user = await _sweep_user(sweep_session)
    owner_id = user.id
    project = await _project(sweep_session, user, schedule="nightly")
    await _add_claims(sweep_session, project, user, 1)
    pid = project.id

    def _boom(*args, **kwargs) -> str:
        raise AssertionError("the sweep must never call the LLM merge")

    monkeypatch.setattr(compiler_mod, "_llm_merge", _boom)

    result = await run_kb_compile_sweep(
        sweep_session, limit=100, session_factory=sweep_factory
    )

    assert result["selected"] == 1
    assert result["compiled"] == 1
    assert result["failed"] == 0
    assert result["errors"] == []

    compilation = (
        await sweep_session.execute(
            select(ContextCompilation).where(ContextCompilation.project_id == pid)
        )
    ).scalar_one()
    assert compilation.trigger == "scheduled"
    # A scheduled compile is attributed to the project's owner, not the
    # platform admin that ran the sweep (GitHub #93 review round 1 LOW).
    assert compilation.user_id == owner_id
    project_row = await _refetch_project(sweep_session, pid)
    assert project_row.last_scheduled_compile_at is not None


async def test_sweep_failure_isolation(
    sweep_session: AsyncSession, sweep_factory, monkeypatch
) -> None:
    import sessionfs.server.services.compiler as compiler_mod

    user = await _sweep_user(sweep_session)
    good = await _project(sweep_session, user, schedule="nightly")
    bad = await _project(sweep_session, user, schedule="nightly")
    await _add_claims(sweep_session, good, user, 1)
    await _add_claims(sweep_session, bad, user, 1)
    bad_id = bad.id

    real_compile = compiler_mod.compile_project_context

    async def _flaky(project_id: str, **kwargs):
        if project_id == bad_id:
            raise RuntimeError("boom")
        return await real_compile(project_id=project_id, **kwargs)

    monkeypatch.setattr(compiler_mod, "compile_project_context", _flaky)

    result = await run_kb_compile_sweep(
        sweep_session, limit=100, session_factory=sweep_factory
    )

    assert result["selected"] == 2
    assert result["compiled"] == 1
    assert result["failed"] == 1
    assert [e["project_id"] for e in result["errors"]] == [bad_id]


async def test_sweep_is_idempotent(
    sweep_session: AsyncSession, sweep_factory
) -> None:
    user = await _sweep_user(sweep_session)
    project = await _project(sweep_session, user, schedule="nightly")
    await _add_claims(sweep_session, project, user, 1)

    first = await run_kb_compile_sweep(
        sweep_session, limit=100, session_factory=sweep_factory
    )
    assert first["selected"] == 1
    assert first["compiled"] == 1

    second = await run_kb_compile_sweep(
        sweep_session, limit=100, session_factory=sweep_factory
    )
    assert second["selected"] == 0
    assert second["compiled"] == 0
    assert second["failed"] == 0


async def test_failing_project_does_not_starve_batch(
    sweep_session: AsyncSession, sweep_factory, monkeypatch
) -> None:
    """GitHub #93 review round 1 LOW — a project that keeps failing records a
    failure timestamp (so it drops to the back of the NULLS FIRST ordering)
    instead of being re-selected forever and starving every healthy project."""
    import sessionfs.server.services.compiler as compiler_mod

    user = await _sweep_user(sweep_session)
    # The failing project sorts first (all-zeros id among never-compiled rows).
    failing = await _project(
        sweep_session, user, schedule="nightly", id="proj_0000000000000000"
    )
    healthy = await _project(sweep_session, user, schedule="nightly")
    await _add_claims(sweep_session, failing, user, 1)
    await _add_claims(sweep_session, healthy, user, 1)
    failing_id = failing.id

    real_compile = compiler_mod.compile_project_context

    async def _always_fail(project_id: str, **kwargs):
        if project_id == failing_id:
            raise RuntimeError("boom")
        return await real_compile(project_id=project_id, **kwargs)

    monkeypatch.setattr(compiler_mod, "compile_project_context", _always_fail)

    # limit=1: the failing project is the only selection the first time.
    first = await run_kb_compile_sweep(
        sweep_session, limit=1, session_factory=sweep_factory
    )
    assert first["selected"] == 1
    assert first["failed"] == 1
    assert [e["project_id"] for e in first["errors"]] == [failing_id]

    # The failure timestamp makes it not-due, so the healthy project is next.
    second = await run_kb_compile_sweep(
        sweep_session, limit=1, session_factory=sweep_factory
    )
    assert second["selected"] == 1
    assert second["compiled"] == 1

    failing_row = await _refetch_project(sweep_session, failing_id)
    assert failing_row.last_scheduled_compile_at is not None


# ---------------------------------------------------------------------------
# Compile status + settings (through the ASGI client)
# ---------------------------------------------------------------------------


async def test_compile_status_endpoint(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    project = await _project(db_session, test_user, schedule="nightly")
    await _add_claims(db_session, project, test_user, 2)
    pid = project.id

    resp = await client.get(
        f"/api/v1/projects/{pid}/compile-status", headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pending_claims"] == 2
    assert data["compile_needed"] is True  # never compiled, pending >= 1
    assert data["last_compile_at"] is None
    assert data["compile_schedule"] == "nightly"
    assert data["compile_after_claims"] is None
    assert data["compile_history"] == []

    compiled = await client.post(f"/api/v1/projects/{pid}/compile", headers=auth_headers)
    assert compiled.status_code == 200, compiled.text

    after = await client.get(
        f"/api/v1/projects/{pid}/compile-status", headers=auth_headers
    )
    assert after.status_code == 200, after.text
    body = after.json()
    assert body["pending_claims"] == 0
    assert body["compile_needed"] is False
    assert body["last_compile_at"] is not None
    assert len(body["compile_history"]) == 1
    assert body["compile_history"][0]["trigger"] == "manual"
    assert body["compile_history"][0]["entries_compiled"] == 2


async def test_compile_needed_threshold_ignores_age(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    # 20+ pending claims must trigger the banner regardless of how recent
    # the last compile was.
    project = await _project(
        db_session, test_user, schedule="nightly", last_scheduled=datetime.now(timezone.utc)
    )
    await _add_claims(db_session, project, test_user, 20)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/compile-status", headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["compile_needed"] is True


async def test_settings_validation_and_persistence(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    project = await _project(db_session, test_user, schedule="nightly")
    pid = project.id

    # Invalid schedule value → 422.
    bad_schedule = await client.put(
        f"/api/v1/projects/{pid}/settings",
        headers=auth_headers,
        json={"auto_narrative": False, "compile_schedule": "hourly"},
    )
    assert bad_schedule.status_code == 422, bad_schedule.text

    # compile_after_claims must be >= 1.
    bad_threshold = await client.put(
        f"/api/v1/projects/{pid}/settings",
        headers=auth_headers,
        json={"auto_narrative": False, "compile_after_claims": 0},
    )
    assert bad_threshold.status_code == 422, bad_threshold.text

    # Valid values persist and round-trip.
    ok = await client.put(
        f"/api/v1/projects/{pid}/settings",
        headers=auth_headers,
        json={
            "auto_narrative": False,
            "compile_schedule": "weekly",
            "compile_after_claims": 5,
        },
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["compile_schedule"] == "weekly"
    assert body["compile_after_claims"] == 5

    project_row = await _refetch_project(db_session, pid)
    assert project_row.compile_schedule == "weekly"
    assert project_row.compile_after_claims == 5


async def test_new_project_defaults_to_nightly_schedule(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    """GitHub #93 review round 1 — new projects opt into the nightly sweep
    explicitly at creation, while migration 089 backfills 'off' onto
    pre-existing rows (covered in test_migration_089.py)."""
    resp = await client.post(
        "/api/v1/projects/",
        headers=auth_headers,
        json={
            "name": "New Sweep Project",
            "git_remote_normalized": f"github.com/example/new-{uuid.uuid4().hex}",
        },
    )
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]

    row = await db_session.execute(select(Project).where(Project.id == pid))
    project = row.scalar_one()
    assert project.compile_schedule == "nightly"


async def test_compile_status_counts_auto_promotable_evidence(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """GitHub #93 review round 1 MEDIUM — compile-status must count
    auto-promotable evidence in pending_claims (the sweep selects on the same
    predicate), so a project whose only new material is evidence still
    triggers the compile-needed banner."""
    project = await _project(db_session, test_user, schedule="nightly")
    await _add_evidence(db_session, project, test_user, n=1)

    resp = await client.get(
        f"/api/v1/projects/{project.id}/compile-status", headers=auth_headers
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["pending_claims"] == 1
    assert data["compile_needed"] is True


async def test_clear_compile_after_claims_with_explicit_null(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """GitHub #93 review round 1 LOW — sending compile_after_claims: null
    clears the threshold (model_fields_set distinguishes explicit null from
    an absent field), so the schedule becomes the only trigger again."""
    project = await _project(db_session, test_user, schedule="nightly", after_claims=7)

    resp = await client.put(
        f"/api/v1/projects/{project.id}/settings",
        headers=auth_headers,
        json={"auto_narrative": False, "compile_after_claims": None},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["compile_after_claims"] is None

    project_row = await _refetch_project(db_session, project.id)
    assert project_row.compile_after_claims is None


async def test_settings_off_bypass_and_enable_gated(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """GitHub #93 review round 1 MEDIUM — turning the schedule off is always
    allowed for a settings editor (the migration default), but enabling a
    schedule or setting a threshold still requires the project_context
    feature."""
    test_user.tier = "free"
    await db_session.commit()
    project = await _project(db_session, test_user, schedule="off")

    # Enabling the schedule is gated on the feature.
    enable = await client.put(
        f"/api/v1/projects/{project.id}/settings",
        headers=auth_headers,
        json={"auto_narrative": False, "compile_schedule": "nightly"},
    )
    assert enable.status_code == 403, enable.text

    # Setting a threshold is also gated.
    threshold = await client.put(
        f"/api/v1/projects/{project.id}/settings",
        headers=auth_headers,
        json={"auto_narrative": False, "compile_after_claims": 5},
    )
    assert threshold.status_code == 403, threshold.text

    # Turning it off is always allowed.
    off = await client.put(
        f"/api/v1/projects/{project.id}/settings",
        headers=auth_headers,
        json={"auto_narrative": False, "compile_schedule": "off"},
    )
    assert off.status_code == 200, off.text
    assert off.json()["compile_schedule"] == "off"


async def test_settings_off_exemption_does_not_smuggle_other_changes(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """GitHub #93 review round 2 MEDIUM — the 'off' exemption must only skip
    the project_context gate when the body is nothing but schedule-off (plus
    an optional threshold clear). Any other changed field re-applies the gate,
    so a Free editor cannot toggle auto_narrative under cover of turning the
    schedule off."""
    test_user.tier = "free"
    await db_session.commit()
    project = await _project(db_session, test_user, schedule="off")

    # Turning the schedule off while ALSO changing auto_narrative must be
    # rejected outright, not partially applied.
    resp = await client.put(
        f"/api/v1/projects/{project.id}/settings",
        headers=auth_headers,
        json={"auto_narrative": True, "compile_schedule": "off"},
    )
    assert resp.status_code == 403, resp.text

    project_row = await _refetch_project(db_session, project.id)
    assert project_row.auto_narrative is False
    assert project_row.compile_schedule == "off"

    # Pure off (auto_narrative unchanged) is still allowed.
    off = await client.put(
        f"/api/v1/projects/{project.id}/settings",
        headers=auth_headers,
        json={"auto_narrative": False, "compile_schedule": "off"},
    )
    assert off.status_code == 200, off.text


async def test_ineligible_projects_rotate_behind_eligible_ones(
    sweep_session: AsyncSession, sweep_factory
) -> None:
    """Downgraded (Free) projects ahead in the queue must not starve an
    eligible project: the sweep stamps them so they rotate to the back."""
    free_user = User(
        id=str(uuid.uuid4()),
        email=f"free-{uuid.uuid4().hex[:6]}@example.com",
        display_name="Free Owner",
        tier="free",
        created_at=datetime.now(timezone.utc),
        email_verified=True,
    )
    sweep_session.add(free_user)
    await sweep_session.commit()
    pro_user = await _sweep_user(sweep_session)

    for i in range(5):
        free_project = await _project(
            sweep_session, free_user, schedule="nightly", id=f"proj_a00{i}"
        )
        await _add_claims(sweep_session, free_project, free_user, 1)
    pro_project = await _project(sweep_session, pro_user, schedule="nightly", id="proj_zzz")
    await _add_claims(sweep_session, pro_project, pro_user, 1)

    first = await run_kb_compile_sweep(sweep_session, limit=1, session_factory=sweep_factory)
    assert first["skipped_ineligible"] == 5
    second = await run_kb_compile_sweep(sweep_session, limit=1, session_factory=sweep_factory)
    assert second["compiled"] == 1
    pro_row = await _refetch_project(sweep_session, "proj_zzz")
    assert pro_row.last_scheduled_compile_at is not None
