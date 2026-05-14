"""v0.10.2 — AgentRun API regression tests.

Covers:
- CRUD + lifecycle (queued → running → passed/failed/errored/cancelled).
- Atomic concurrent transition guard.
- Cross-project leak defenses on ticket + persona references.
- Policy evaluation matrix (severity × fail_on → policy_result + exit_code).
- Project access gate (non-member → 403/404).
- Tier gate (Pro user gets 403; Team unlocks).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentPersona,
    ApiKey,
    Project,
    User,
)
from sessionfs.server.routes.agent_runs import _evaluate_policy


async def _make_user(
    db: AsyncSession, name: str = "alice", tier: str = "team"
) -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier=tier,
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"{name}-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _make_project(db: AsyncSession, owner: User) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:16]}",
        name=f"runs-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/runs-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_persona(
    db: AsyncSession, project: Project, owner: User, name: str = "atlas"
) -> AgentPersona:
    persona = AgentPersona(
        id=f"per_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        name=name,
        role="Backend",
        content="# Atlas\n\nBackend persona.",
        created_by=owner.id,
    )
    db.add(persona)
    await db.commit()
    await db.refresh(persona)
    return persona


# ── Policy evaluation matrix (pure-function) ──────────────────────


def test_policy_no_fail_on_always_passes():
    """Without fail_on, every severity passes with exit 0."""
    for sev in ("none", "low", "medium", "high", "critical"):
        assert _evaluate_policy(sev, None) == ("pass", 0)
        assert _evaluate_policy(sev, "none") == ("pass", 0)


def test_policy_severity_none_never_trips_threshold():
    """severity=none always passes regardless of fail_on level."""
    for fo in ("low", "medium", "high", "critical"):
        assert _evaluate_policy("none", fo) == ("pass", 0)


def test_policy_threshold_at_boundary():
    """Severity == fail_on trips; severity < fail_on passes."""
    assert _evaluate_policy("high", "high") == ("fail", 1)
    assert _evaluate_policy("medium", "high") == ("pass", 0)
    assert _evaluate_policy("critical", "high") == ("fail", 1)
    assert _evaluate_policy("low", "medium") == ("pass", 0)
    assert _evaluate_policy("critical", "critical") == ("fail", 1)
    assert _evaluate_policy("high", "critical") == ("pass", 0)


# ── CRUD + lifecycle ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_create_run_queued_and_get_back(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")

    resp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs",
        headers=_hdrs(key),
        json={"persona_name": "atlas", "trigger_source": "manual"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "queued"
    assert body["persona_name"] == "atlas"
    assert body["id"].startswith("run_")
    run_id = body["id"]

    g = await client.get(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}",
        headers=_hdrs(key),
    )
    assert g.status_code == 200
    assert g.json()["id"] == run_id


@pytest.mark.asyncio
async def test_lifecycle_queued_running_passed_with_compiled_context(
    client: AsyncClient, db_session: AsyncSession
):
    """Full happy path: create → start (returns context) → complete."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")

    cr = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs",
        headers=_hdrs(key),
        json={"persona_name": "atlas", "fail_on": "high"},
    )
    run_id = cr.json()["id"]

    st = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/start",
        headers=_hdrs(key),
    )
    assert st.status_code == 200
    payload = st.json()
    assert payload["run"]["status"] == "running"
    assert payload["run"]["started_at"] is not None
    # Persona-only compile (no ticket) — must include persona header.
    assert "You are atlas — Backend" in payload["compiled_context"]

    cp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/complete",
        headers=_hdrs(key),
        json={
            "status": "passed",
            "severity": "low",
            "result_summary": "All green.",
            "findings": [{"rule": "x", "severity": "low"}],
        },
    )
    assert cp.status_code == 200
    final = cp.json()
    assert final["status"] == "passed"
    assert final["severity"] == "low"
    assert final["findings_count"] == 1
    assert final["policy_result"] == "pass"
    assert final["exit_code"] == 0
    assert final["completed_at"] is not None


@pytest.mark.asyncio
async def test_complete_severity_meets_fail_on_flips_to_failed(
    client: AsyncClient, db_session: AsyncSession
):
    """When caller submits status=passed but severity ≥ fail_on, the run
    is recorded as failed and exit_code=1."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")

    cr = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs",
        headers=_hdrs(key),
        json={"persona_name": "atlas", "fail_on": "medium"},
    )
    run_id = cr.json()["id"]
    await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/start",
        headers=_hdrs(key),
    )
    cp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/complete",
        headers=_hdrs(key),
        json={"status": "passed", "severity": "high"},
    )
    assert cp.status_code == 200
    final = cp.json()
    assert final["status"] == "failed"  # flipped by policy
    assert final["policy_result"] == "fail"
    assert final["exit_code"] == 1


@pytest.mark.asyncio
async def test_complete_errored_status_preserved_regardless_of_policy(
    client: AsyncClient, db_session: AsyncSession
):
    """status='errored' (caller signaling tool crash) is preserved even
    when policy would otherwise pass."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")

    cr = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs",
        headers=_hdrs(key),
        json={"persona_name": "atlas", "fail_on": "high"},
    )
    run_id = cr.json()["id"]
    await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/start",
        headers=_hdrs(key),
    )
    cp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/complete",
        headers=_hdrs(key),
        json={"status": "errored", "severity": "none"},
    )
    assert cp.json()["status"] == "errored"


@pytest.mark.asyncio
async def test_cancel_from_queued(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    cr = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs",
        headers=_hdrs(key),
        json={"persona_name": "atlas"},
    )
    run_id = cr.json()["id"]
    cn = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/cancel",
        headers=_hdrs(key),
    )
    assert cn.status_code == 200
    assert cn.json()["status"] == "cancelled"


# ── Atomic transition guards ──────────────────────────────────────


@pytest.mark.asyncio
async def test_start_twice_returns_409(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    cr = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs",
        headers=_hdrs(key),
        json={"persona_name": "atlas"},
    )
    run_id = cr.json()["id"]
    s1 = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/start",
        headers=_hdrs(key),
    )
    s2 = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/start",
        headers=_hdrs(key),
    )
    assert s1.status_code == 200
    assert s2.status_code == 409
    assert "running" in s2.text  # error mentions current state


@pytest.mark.asyncio
async def test_complete_after_terminal_returns_409(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    cr = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs",
        headers=_hdrs(key),
        json={"persona_name": "atlas"},
    )
    run_id = cr.json()["id"]
    await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/cancel",
        headers=_hdrs(key),
    )
    # Trying to complete an already-cancelled run should fail.
    cp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/complete",
        headers=_hdrs(key),
        json={"status": "passed", "severity": "none"},
    )
    assert cp.status_code == 409


# ── Cross-project guards ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_cannot_reference_ticket_from_another_project(
    client: AsyncClient, db_session: AsyncSession
):
    """If ticket_id belongs to project A, you cannot create a run in
    project B referencing it (422)."""
    user, key = await _make_user(db_session)
    project_a = await _make_project(db_session, user)
    project_b = await _make_project(db_session, user)
    await _make_persona(db_session, project_b, user, "atlas")

    # Create a real ticket in project A.
    await _make_persona(db_session, project_a, user, "atlas")
    tk = await client.post(
        f"/api/v1/projects/{project_a.id}/tickets",
        headers=_hdrs(key),
        json={"title": "in-A"},
    )
    a_ticket_id = tk.json()["id"]

    # Try to create a run in B referencing A's ticket.
    resp = await client.post(
        f"/api/v1/projects/{project_b.id}/agent-runs",
        headers=_hdrs(key),
        json={"persona_name": "atlas", "ticket_id": a_ticket_id},
    )
    assert resp.status_code == 422
    assert "Ticket" in resp.text


@pytest.mark.asyncio
async def test_cross_project_run_lookup_returns_404(
    client: AsyncClient, db_session: AsyncSession
):
    """A run created in project A is not visible via project B's URL."""
    user, key = await _make_user(db_session)
    project_a = await _make_project(db_session, user)
    project_b = await _make_project(db_session, user)
    await _make_persona(db_session, project_a, user, "atlas")

    cr = await client.post(
        f"/api/v1/projects/{project_a.id}/agent-runs",
        headers=_hdrs(key),
        json={"persona_name": "atlas"},
    )
    run_id = cr.json()["id"]
    # Same run id, wrong project URL.
    cross = await client.get(
        f"/api/v1/projects/{project_b.id}/agent-runs/{run_id}",
        headers=_hdrs(key),
    )
    assert cross.status_code == 404


@pytest.mark.asyncio
async def test_inactive_persona_rejected_at_create_time(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    # Persona named atlas does NOT exist.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs",
        headers=_hdrs(key),
        json={"persona_name": "atlas"},
    )
    assert resp.status_code == 422
    assert "Persona" in resp.text


# ── Listing + filters ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_filters_by_persona_status_trigger(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    await _make_persona(db_session, project, user, "sentinel")
    # Two atlas runs (one queued, one started+passed), one sentinel run.
    for persona, status in (("atlas", "queued"), ("atlas", "complete"), ("sentinel", "queued")):
        cr = await client.post(
            f"/api/v1/projects/{project.id}/agent-runs",
            headers=_hdrs(key),
            json={"persona_name": persona, "trigger_source": "ci"},
        )
        if status == "complete":
            rid = cr.json()["id"]
            await client.post(
                f"/api/v1/projects/{project.id}/agent-runs/{rid}/start",
                headers=_hdrs(key),
            )
            await client.post(
                f"/api/v1/projects/{project.id}/agent-runs/{rid}/complete",
                headers=_hdrs(key),
                json={"status": "passed", "severity": "none"},
            )

    # Filter by persona.
    by_atlas = await client.get(
        f"/api/v1/projects/{project.id}/agent-runs?persona_name=atlas",
        headers=_hdrs(key),
    )
    assert len(by_atlas.json()) == 2

    # Filter by status.
    queued = await client.get(
        f"/api/v1/projects/{project.id}/agent-runs?status=queued",
        headers=_hdrs(key),
    )
    assert len(queued.json()) == 2  # the two queued (atlas + sentinel)

    # Filter by trigger_source.
    by_ci = await client.get(
        f"/api/v1/projects/{project.id}/agent-runs?trigger_source=ci",
        headers=_hdrs(key),
    )
    assert len(by_ci.json()) == 3  # all three were ci


# ── Tier gating ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_pro_tier_blocked(client: AsyncClient, db_session: AsyncSession):
    """Pro user without team tier gets 403 on agent-runs routes."""
    user, key = await _make_user(db_session, tier="pro")
    project = await _make_project(db_session, user)
    resp = await client.get(
        f"/api/v1/projects/{project.id}/agent-runs",
        headers=_hdrs(key),
    )
    assert resp.status_code == 403


# ── Concurrent transitions (atomic guard) ────────────────────────


@pytest.mark.asyncio
async def test_complete_errored_forces_exit_code_1(
    client: AsyncClient, db_session: AsyncSession
):
    """Post-Round 1 HIGH: an errored run with severity=none must store
    exit_code=1 so `sfs agent complete --enforce` fails CI even when
    fail_on=high would normally pass severity=none.
    """
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")

    cr = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs",
        headers=_hdrs(key),
        json={"persona_name": "atlas", "fail_on": "high"},
    )
    run_id = cr.json()["id"]
    await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/start",
        headers=_hdrs(key),
    )
    # Caller submits errored + severity=none — pre-fix this stored
    # exit_code=0 because severity=none never trips the threshold.
    cp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/complete",
        headers=_hdrs(key),
        json={"status": "errored", "severity": "none"},
    )
    assert cp.status_code == 200
    final = cp.json()
    assert final["status"] == "errored"
    assert final["policy_result"] == "fail"
    assert final["exit_code"] == 1


@pytest.mark.asyncio
async def test_complete_failed_forces_exit_code_1(
    client: AsyncClient, db_session: AsyncSession
):
    """Symmetric to the errored case: explicit `failed` always exits non-zero."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")

    cr = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs",
        headers=_hdrs(key),
        json={"persona_name": "atlas"},  # no fail_on at all
    )
    run_id = cr.json()["id"]
    await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/start",
        headers=_hdrs(key),
    )
    cp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/complete",
        headers=_hdrs(key),
        json={"status": "failed", "severity": "none"},
    )
    final = cp.json()
    assert final["status"] == "failed"
    assert final["policy_result"] == "fail"
    assert final["exit_code"] == 1


@pytest.mark.asyncio
async def test_concurrent_starts_only_one_wins(
    client: AsyncClient, db_session: AsyncSession
):
    """Two concurrent start requests on the same queued run: exactly one
    moves it to running, the other returns 409. Verifies the atomic
    UPDATE...WHERE status='queued' rowcount-1 guard.
    """
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    cr = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs",
        headers=_hdrs(key),
        json={"persona_name": "atlas"},
    )
    run_id = cr.json()["id"]

    async def _start():
        return await client.post(
            f"/api/v1/projects/{project.id}/agent-runs/{run_id}/start",
            headers=_hdrs(key),
        )

    results = await asyncio.gather(_start(), _start())
    statuses = sorted(r.status_code for r in results)
    assert statuses == [200, 409]
