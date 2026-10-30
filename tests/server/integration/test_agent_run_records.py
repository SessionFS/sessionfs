"""Review Loop R3 track A — agent-run record fields (tk_3b0e1df1f32a4c1b).

Covers the new nullable `agent_runs` columns end-to-end:

- create accepts model/provider/role/branch/base_sha/loop-linkage ids and
  validates their shapes (enum roles, hex SHAs, length caps).
- complete accepts usage + cost_basis + head_sha + exit_reason + occurred_at,
  validating ints/costs/enums/SHAs/time bounds (bool is rejected as an int).
- findings validation (round / stage / class / harm / status / fixed_in_sha)
  with unknown keys preserved.
- additive usage via POST …/usage on queued and running runs; fixed after
  completion (409); cross-project parent id → uniform 404.
- dollar visibility: cost_usd is omitted for plain org members and service
  keys, present for org admins and personal-project owners; tokens are
  always visible.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AgentPersona,
    ApiKey,
    OrgMember,
    Organization,
    Project,
    User,
)

_MAX_TOKEN_COUNT = 10**12
_MAX_COST_USD = 999_999


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


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
    await db.flush()
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"{name}-key",
            is_active=True,
            key_kind="user",
            scopes=json.dumps(["*"]),
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


async def _make_project(
    db: AsyncSession, owner: User, org: Organization | None = None
) -> Project:
    project = Project(
        id=f"proj_{uuid.uuid4().hex[:12]}",
        name=f"rec-{uuid.uuid4().hex[:6]}",
        git_remote_normalized=f"acme/rec-{uuid.uuid4().hex[:6]}",
        context_document="",
        owner_id=owner.id,
        org_id=org.id if org else None,
        created_at=datetime.now(timezone.utc),
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return project


async def _make_persona(
    db: AsyncSession, project: Project, owner: User, name: str = "atlas"
) -> AgentPersona:
    persona = AgentPersona(
        id=f"per_{uuid.uuid4().hex[:12]}",
        project_id=project.id,
        name=name,
        role="Backend",
        created_by=owner.id,
        is_active=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(persona)
    await db.commit()
    await db.refresh(persona)
    return persona


async def _make_org_with_admin(
    db: AsyncSession, slug: str | None = None
) -> tuple[Organization, User, str]:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:8]}",
        name=slug or f"Org-{uuid.uuid4().hex[:6]}",
        slug=slug or f"o-{uuid.uuid4().hex[:6]}",
        tier="team",
        created_at=datetime.now(timezone.utc),
    )
    db.add(org)
    await db.flush()
    admin, raw = await _make_user(db, name="admin", tier="team")
    db.add(
        OrgMember(
            org_id=org.id,
            user_id=admin.id,
            role="admin",
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return org, admin, raw


async def _add_org_member(
    db: AsyncSession, org: Organization, user: User, role: str
) -> None:
    db.add(
        OrgMember(
            org_id=org.id,
            user_id=user.id,
            role=role,
            joined_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


async def _make_service_key(
    db: AsyncSession, org_id: str, minter: User, scopes: list[str]
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
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return raw


async def _create_run(
    client: AsyncClient, project: Project, key: str, **extra
) -> str:
    resp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs",
        headers=_hdrs(key),
        json={"persona_name": "atlas", **extra},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _start_run(client: AsyncClient, project: Project, key: str, run_id: str) -> None:
    resp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/start",
        headers=_hdrs(key),
    )
    assert resp.status_code == 200, resp.text


# ── create: field acceptance + shape validation ──────────────────


@pytest.mark.asyncio
async def test_create_accepts_and_echoes_record_fields(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    parent_id = await _create_run(client, project, key)

    resp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs",
        headers=_hdrs(key),
        json={
            "persona_name": "atlas",
            "model": "claude-opus-5-5",
            "provider": "anthropic",
            "role": "IMPLEMENTER",  # normalized to lowercase
            "branch": "feat/rl-a1",
            "base_sha": "ABCDEF1234567",  # normalized to lowercase hex
            "parent_run_id": parent_id,
            "continues_run_id": parent_id,
        },
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["model"] == "claude-opus-5-5"
    assert body["provider"] == "anthropic"
    assert body["role"] == "implementer"
    assert body["branch"] == "feat/rl-a1"
    assert body["base_sha"] == "abcdef1234567"
    assert body["parent_run_id"] == parent_id
    assert body["continues_run_id"] == parent_id


@pytest.mark.asyncio
async def test_create_rejects_invalid_role_and_base_sha(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")

    for extra, needle in (
        ({"role": "bogus"}, "role must be one of"),
        ({"base_sha": "not-a-sha"}, "base_sha"),
        ({"base_sha": "abc"}, "base_sha"),  # too short (< 7 hex chars)
        ({"model": "x" * 121}, "model"),
        ({"provider": "x" * 41}, "provider"),
        ({"branch": "x" * 201}, "branch"),
        ({"parent_run_id": "x" * 65}, "run ids"),
    ):
        resp = await client.post(
            f"/api/v1/projects/{project.id}/agent-runs",
            headers=_hdrs(key),
            json={"persona_name": "atlas", **extra},
        )
        assert resp.status_code == 422, (extra, resp.text)
        assert needle in resp.text, (extra, resp.text)


# ── complete: usage / enum / sha / time validation ───────────────


@pytest.mark.asyncio
async def test_complete_rejects_bad_usage(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    run_id = await _create_run(client, project, key)
    await _start_run(client, project, key, run_id)

    for extra, needle in (
        ({"input_tokens": -1}, "token counts must be between 0"),
        ({"input_tokens": True}, "not booleans"),
        ({"input_tokens": 1.5}, "must be integers"),
        ({"output_tokens": _MAX_TOKEN_COUNT + 1}, "token counts must be between 0"),
        ({"cost_usd": -0.01}, "cost_usd must be between 0"),
        ({"cost_usd": True}, "not a boolean"),
        ({"cost_usd": "x"}, "cost_usd must be a number"),
        ({"cost_usd": _MAX_COST_USD + 1}, "cost_usd must be between 0"),
    ):
        resp = await client.post(
            f"/api/v1/projects/{project.id}/agent-runs/{run_id}/complete",
            headers=_hdrs(key),
            json={"status": "passed", **extra},
        )
        assert resp.status_code == 422, (extra, resp.text)
        assert needle in resp.text, (extra, resp.text)


@pytest.mark.asyncio
async def test_complete_rejects_bad_enum_and_sha_and_time(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    run_id = await _create_run(client, project, key)
    await _start_run(client, project, key, run_id)

    now = datetime.now(timezone.utc)
    cases = (
        ({"cost_basis": "bogus"}, "cost_basis must be one of"),
        ({"exit_reason": "bogus"}, "exit_reason must be one of"),
        ({"head_sha": "xyz"}, "head_sha"),
        (
            {"occurred_at": (now + timedelta(minutes=10)).isoformat()},
            "5 minutes in the future",
        ),
        (
            {"occurred_at": (now - timedelta(days=31)).isoformat()},
            "30 days in the past",
        ),
    )
    for extra, needle in cases:
        resp = await client.post(
            f"/api/v1/projects/{project.id}/agent-runs/{run_id}/complete",
            headers=_hdrs(key),
            json={"status": "passed", **extra},
        )
        assert resp.status_code == 422, (extra, resp.text)
        assert needle in resp.text, (extra, resp.text)


@pytest.mark.asyncio
async def test_complete_accepts_valid_record_fields(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    run_id = await _create_run(client, project, key)
    await _start_run(client, project, key, run_id)

    occurred = datetime.now(timezone.utc) - timedelta(hours=1)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/complete",
        headers=_hdrs(key),
        json={
            "status": "passed",
            "input_tokens": 100,
            "output_tokens": 50,
            "cost_usd": 0.123456,
            "cost_basis": "reported",
            "head_sha": "abcdef1234567",
            "exit_reason": "finished",
            "occurred_at": occurred.isoformat(),
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["input_tokens"] == 100
    assert body["output_tokens"] == 50
    assert body["cost_usd"] == pytest.approx(0.123456)
    assert body["cost_basis"] == "reported"
    assert body["head_sha"] == "abcdef1234567"
    assert body["exit_reason"] == "finished"
    assert body["occurred_at"] is not None


# ── findings validation ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_findings_validation_rejects_bad_values_and_preserves_unknown_keys(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    run_id = await _create_run(client, project, key)
    await _start_run(client, project, key, run_id)

    bad_cases = (
        ({"round": 0}, "round"),
        ({"round": True}, "round"),
        ({"stage": "review"}, "stage"),
        ({"class": "UPPER"}, "class"),
        ({"class": "-leading"}, "class"),
        ({"harm": "leak"}, "harm"),
        ({"status": "won't-fix", "harm": "none"}, "status"),
        ({"fixed_in_sha": "xyz"}, "fixed_in_sha"),
    )
    for finding, needle in bad_cases:
        resp = await client.post(
            f"/api/v1/projects/{project.id}/agent-runs/{run_id}/complete",
            headers=_hdrs(key),
            json={"status": "passed", "findings": [finding]},
        )
        assert resp.status_code == 422, (finding, resp.text)
        assert needle in resp.text, (finding, resp.text)

    # A fully-validated finding with an unknown key round-trips intact.
    resp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/complete",
        headers=_hdrs(key),
        json={
            "status": "passed",
            "findings": [
                {
                    "title": "money exposed",
                    "round": 2,
                    "stage": "code",
                    "severity": "high",
                    "class": "money-spent",
                    "harm": "money-spent",
                    "file": "src/pay.py",
                    "line": 12,
                    "fixed_in_sha": "abcdef1234567",
                    "status": "fixed",
                    "custom_key": "kept-as-is",
                }
            ],
        },
    )
    assert resp.status_code == 200, resp.text
    stored = resp.json()["findings"][0]
    assert stored["custom_key"] == "kept-as-is"
    assert stored["class"] == "money-spent"
    assert stored["status"] == "fixed"


# ── additive usage ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_usage_is_additive_across_reports_and_complete(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    run_id = await _create_run(client, project, key)

    # Usage is allowed while still queued.
    r1 = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/usage",
        headers=_hdrs(key),
        json={"input_tokens": 100, "output_tokens": 50, "cost_usd": 0.1},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["input_tokens"] == 100
    assert r1.json()["status"] == "queued"

    await _start_run(client, project, key, run_id)
    r2 = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/usage",
        headers=_hdrs(key),
        json={"input_tokens": 50},
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["input_tokens"] == 150
    assert r2.json()["output_tokens"] == 50

    # Completion adds on top (does not overwrite), then fixes the totals.
    cp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/complete",
        headers=_hdrs(key),
        json={"status": "passed", "input_tokens": 25, "cost_usd": 0.2},
    )
    assert cp.status_code == 200, cp.text
    body = cp.json()
    assert body["input_tokens"] == 175
    assert body["output_tokens"] == 50
    assert body["cost_usd"] == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_running_cost_total_is_held_to_the_cap(
    client: AsyncClient, db_session: AsyncSession
):
    """Repeated reports can't push the stored total past what Numeric(12,6)
    holds: the report that would overflow is a 422 and the total is kept."""
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    run_id = await _create_run(client, project, key)
    url = f"/api/v1/projects/{project.id}/agent-runs/{run_id}"

    ok = await client.post(f"{url}/usage", headers=_hdrs(key), json={"cost_usd": 600_000})
    assert ok.status_code == 200, ok.text
    over = await client.post(f"{url}/usage", headers=_hdrs(key), json={"cost_usd": 600_000})
    assert over.status_code == 422, over.text
    await _start_run(client, project, key, run_id)
    done = await client.post(
        f"{url}/complete", headers=_hdrs(key), json={"status": "passed", "cost_usd": 500_000}
    )
    assert done.status_code == 422, done.text
    got = await client.get(url, headers=_hdrs(key))
    assert got.json()["cost_usd"] == pytest.approx(600_000)
    assert got.json()["status"] == "running"


@pytest.mark.asyncio
async def test_usage_after_completion_is_409(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    run_id = await _create_run(client, project, key)
    await _start_run(client, project, key, run_id)
    await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/complete",
        headers=_hdrs(key),
        json={"status": "passed"},
    )

    resp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/usage",
        headers=_hdrs(key),
        json={"input_tokens": 1},
    )
    assert resp.status_code == 409
    assert "terminal" in resp.text


# ── cross-project loop-linkage id → 404 ──────────────────────────


@pytest.mark.asyncio
async def test_cross_project_parent_run_id_is_404(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project_a = await _make_project(db_session, user)
    project_b = await _make_project(db_session, user)
    await _make_persona(db_session, project_a, user, "atlas")
    await _make_persona(db_session, project_b, user, "atlas")

    parent_id = await _create_run(client, project_a, key)
    resp = await client.post(
        f"/api/v1/projects/{project_b.id}/agent-runs",
        headers=_hdrs(key),
        json={"persona_name": "atlas", "parent_run_id": parent_id},
    )
    assert resp.status_code == 404
    assert "Agent run not found" in resp.text


# ── dollar visibility ────────────────────────────────────────────


async def _complete_with_cost(
    client: AsyncClient, project: Project, key: str, run_id: str
) -> dict:
    await _start_run(client, project, key, run_id)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/complete",
        headers=_hdrs(key),
        json={"status": "passed", "input_tokens": 10, "cost_usd": 1.5},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.mark.asyncio
async def test_personal_project_owner_sees_cost(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)  # org_id None
    await _make_persona(db_session, project, user, "atlas")
    run_id = await _create_run(client, project, key)
    body = await _complete_with_cost(client, project, key, run_id)
    assert body["cost_usd"] == pytest.approx(1.5)
    assert body["input_tokens"] == 10


@pytest.mark.asyncio
async def test_org_member_does_not_see_cost(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _admin_key = await _make_org_with_admin(db_session)
    member, member_key = await _make_user(db_session, name="member", tier="team")
    await _add_org_member(db_session, org, member, "member")
    project = await _make_project(db_session, admin, org=org)
    await _make_persona(db_session, project, admin, "atlas")

    run_id = await _create_run(client, project, member_key)
    body = await _complete_with_cost(client, project, member_key, run_id)
    assert "cost_usd" not in body
    assert body["input_tokens"] == 10  # tokens still visible


@pytest.mark.asyncio
async def test_org_admin_sees_cost(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, admin_key = await _make_org_with_admin(db_session)
    project = await _make_project(db_session, admin, org=org)
    await _make_persona(db_session, project, admin, "atlas")

    run_id = await _create_run(client, project, admin_key)
    body = await _complete_with_cost(client, project, admin_key, run_id)
    assert body["cost_usd"] == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_service_key_sees_tokens_not_dollars(
    client: AsyncClient, db_session: AsyncSession
):
    org, admin, _admin_key = await _make_org_with_admin(db_session)
    project = await _make_project(db_session, admin, org=org)
    await _make_persona(db_session, project, admin, "atlas")
    svc_key = await _make_service_key(
        db_session, org.id, admin, ["agent_runs:write"]
    )

    run_id = await _create_run(client, project, svc_key)
    # The complete response is returned to the service key, so it must
    # already omit cost_usd while still exposing token totals. (The start
    # route is user-key-only; completion works straight from queued.)
    resp = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/complete",
        headers=_hdrs(svc_key),
        json={"status": "passed", "input_tokens": 7, "cost_usd": 2.0},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "cost_usd" not in body
    assert body["input_tokens"] == 7


@pytest.mark.asyncio
async def test_usage_addition_happens_in_sql_not_from_a_stale_read(
    client: AsyncClient, db_session: AsyncSession
):
    """A report made after another writer changed the totals still adds to
    the stored value (no read-modify-write that could lose an update)."""
    from sqlalchemy import update as sa_update

    from sessionfs.server.db.models import AgentRun

    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    run_id = await _create_run(client, project, key)
    await _start_run(client, project, key, run_id)
    # Another writer bumps the counter directly.
    await db_session.execute(
        sa_update(AgentRun).where(AgentRun.id == run_id).values(input_tokens=1000)
    )
    await db_session.commit()
    r = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/usage",
        headers=_hdrs(key),
        json={"input_tokens": 5},
    )
    assert r.status_code == 200, r.text
    assert r.json()["input_tokens"] == 1005


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-Infinity"])
async def test_non_finite_cost_is_rejected(
    client: AsyncClient, db_session: AsyncSession, bad: str
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    run_id = await _create_run(client, project, key)
    r = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/usage",
        headers={**_hdrs(key), "Content-Type": "application/json"},
        content=f'{{"cost_usd": {bad}}}',
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_cost_without_basis_is_recorded_as_reported(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    run_id = await _create_run(client, project, key)
    body = await _complete_with_cost(client, project, key, run_id)
    assert body["cost_basis"] == "reported"
    spend = await client.get(f"/api/v1/projects/{project.id}/spend", headers=_hdrs(key))
    assert spend.status_code == 200, spend.text
    assert spend.json()["totals"]["cost_usd"] == pytest.approx(1.5)


@pytest.mark.asyncio
async def test_older_free_text_finding_severity_still_accepted(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    run_id = await _create_run(client, project, key)
    await _start_run(client, project, key, run_id)
    r = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/complete",
        headers=_hdrs(key),
        json={
            "status": "passed",
            "findings": [{"severity": "info", "status": "wontfix", "title": "old client"}],
        },
    )
    assert r.status_code == 200, r.text


@pytest.mark.asyncio
async def test_empty_usage_report_is_422(client: AsyncClient, db_session: AsyncSession):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    run_id = await _create_run(client, project, key)
    r = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/usage",
        headers=_hdrs(key),
        json={},
    )
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_token_only_run_counts_as_unknown_in_rollup(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    run_id = await _create_run(client, project, key)
    await _start_run(client, project, key, run_id)
    r = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs/{run_id}/complete",
        headers=_hdrs(key),
        json={"status": "passed", "input_tokens": 100000},
    )
    assert r.status_code == 200, r.text
    spend = await client.get(f"/api/v1/projects/{project.id}/spend", headers=_hdrs(key))
    assert spend.json()["totals"]["tokens_on_plans"] == 100000


@pytest.mark.asyncio
async def test_run_linked_to_work_queue_must_be_in_project(
    client: AsyncClient, db_session: AsyncSession
):
    user, key = await _make_user(db_session)
    project = await _make_project(db_session, user)
    await _make_persona(db_session, project, user, "atlas")
    r = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs",
        headers=_hdrs(key),
        json={"persona_name": "atlas", "work_queue_id": "wq_doesnotexist"},
    )
    assert r.status_code == 404, r.text
    r = await client.post(
        f"/api/v1/projects/{project.id}/agent-runs",
        headers=_hdrs(key),
        json={"persona_name": "atlas", "work_queue_item_id": "wqi_x"},
    )
    assert r.status_code == 422, r.text
