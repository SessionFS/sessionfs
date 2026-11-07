"""Record-only workflow templates (GitHub #120, W1): templates, start, plan,
step reporting."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentPersona,
    AgentRun,
    ApprovalPolicy,
    Ticket,
    TrustedReviewer,
    WorkflowStepChild,
    WorkflowTemplate,
)
from tests.server.integration.test_loops_api import (
    _add_org_member,
    _hdrs,
    _make_org,
    _make_project,
    _make_service_key,
    _make_user,
)

SHA1 = "a" * 40


def _steps() -> list[dict]:
    return [
        {"id": "dispatch", "title": "Dispatch", "role": "dispatcher", "agent": "scout"},
        {
            "id": "implement",
            "title": "Implement",
            "role": "implementer",
            "agent": "atlas",
            "depends_on": ["dispatch"],
            "fan_out": 2,
            "brief_template": "Build {ticket.title}\n\n{ticket.description}\n\n{checklist}",
        },
        {
            "id": "review",
            "title": "Review",
            "role": "reviewer",
            "agent": "reviewer",
            "depends_on": ["implement"],
            "gate": "trusted_clean",
        },
        {
            "id": "verify",
            "title": "Verify",
            "role": "verifier",
            "agent": "sentinel",
            "depends_on": ["review"],
            "gate": "approval_policy:security",
        },
        {
            "id": "document",
            "title": "Document",
            "role": "documenter",
            "agent": "scribe",
            "depends_on": ["verify"],
        },
    ]


class Env:
    pass


def _code(r) -> str | None:
    return r.json()["error"]["details"].get("error")


def _msg(r) -> str:
    return r.json()["error"]["message"]


async def _persona(db: AsyncSession, project, owner, name: str) -> None:
    db.add(
        AgentPersona(
            id=f"per_{uuid.uuid4().hex[:12]}",
            project_id=project.id,
            name=name,
            role="Agent",
            created_by=owner.id,
        )
    )
    await db.commit()


async def _env(db: AsyncSession, *, personas=("scout", "atlas", "reviewer", "sentinel", "scribe")) -> Env:
    e = Env()
    e.owner, e.owner_key = await _make_user(db, "owner")
    e.reviewer, e.reviewer_key = await _make_user(db, "rev")
    e.member, e.member_key = await _make_user(db, "mem")
    e.outsider, e.outsider_key = await _make_user(db, "out")
    e.org = await _make_org(db)
    await _add_org_member(db, e.org, e.owner, "admin")
    await _add_org_member(db, e.org, e.reviewer, "member")
    await _add_org_member(db, e.org, e.member, "member")
    e.project = await _make_project(db, e.owner, e.org)
    for name in personas:
        await _persona(db, e.project, e.owner, name)
    db.add(
        TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:16]}",
            project_id=e.project.id,
            user_id=e.reviewer.id,
            reviewer_persona="reviewer",
            is_active=True,
            created_by_user_id=e.owner.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    # The verify step's ``approval_policy:security`` gate needs a real
    # policy in the project's org (the first env in a test owns it).
    if await db.get(ApprovalPolicy, "security") is None:
        db.add(
            ApprovalPolicy(
                id="security",
                org_id=e.org.id,
                lineage_id="security",
                name="Security sign-off",
                target_kind="merge",
                filter="{}",
                requirements=json.dumps([{"who": "org_role", "role": "admin", "count": 1}]),
            )
        )
    await db.commit()
    return e


def _pp(e: Env, tail: str) -> str:
    return f"/api/v1/projects/{e.project.id}/{tail}"


async def _template(client: AsyncClient, e: Env, steps=None, key=None) -> dict:
    r = await client.post(
        _pp(e, "workflow-templates"),
        headers=_hdrs(key or e.owner_key),
        json={"name": "Ship", "definition": {"steps": steps or _steps()}},
    )
    assert r.status_code == 201, r.text
    return r.json()


async def _start(client: AsyncClient, e: Env, template_id: str, key=None, **body):
    payload = {"template_id": template_id, "title": "Payments retry", "description": "Retry failed charges"}
    payload.update(body)
    return await client.post(_pp(e, "workflows"), headers=_hdrs(key or e.owner_key), json=payload)


async def _plan(client: AsyncClient, e: Env, loop_id: str, key=None):
    r = await client.get(_pp(e, f"workflows/{loop_id}/plan"), headers=_hdrs(key or e.owner_key))
    assert r.status_code == 200, r.text
    return {s["step_id"]: s for s in r.json()["steps"]}, r.json()


def _ticket_ids(started: dict, step: str) -> list[str]:
    return [t["id"] for s in started["steps"] if s["step_id"] == step for t in s["tickets"]]


async def _start_worker(client, e, ticket_id, key=None, **extra):
    return await client.post(
        _pp(e, "agent-runs/start-worker"),
        headers=_hdrs(key or e.owner_key),
        json={"ticket_id": ticket_id, **extra},
    )


async def _finish(client, e, run_id, key=None, **extra):
    return await client.post(
        _pp(e, f"agent-runs/{run_id}/finish"),
        headers=_hdrs(key or e.owner_key),
        json={"head_sha": SHA1, "exit_reason": "finished", **extra},
    )


async def _round(client, e, loop_id, ticket_id, key=None, **over):
    body = {
        "ticket_id": ticket_id,
        "round": 1,
        "stage": "code",
        "reviewed_commit": SHA1,
        "verdict": "VERIFIED-CLEAN",
        "findings": [],
        **over,
    }
    return await client.post(
        _pp(e, f"loops/{loop_id}/rounds"), headers=_hdrs(key or e.reviewer_key), json=body
    )


async def _work(client, e, ticket_id, **extra) -> None:
    r = await _start_worker(client, e, ticket_id, **extra)
    assert r.status_code == 201, r.text
    f = await _finish(client, e, r.json()["run_id"])
    assert f.status_code == 200, f.text


# ── Templates ──


@pytest.mark.asyncio
async def test_template_versioning(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    v1 = await _template(client, e)
    assert v1["version"] == 1 and v1["active"] and v1["scope"] == "project"
    assert v1["definition"]["steps"][1]["fan_out"] == 2

    steps = _steps()[:2]
    r = await client.put(
        _pp(e, f"workflow-templates/{v1['id']}"),
        headers=_hdrs(e.owner_key),
        json={"name": "Ship v2", "definition": {"steps": steps}},
    )
    assert r.status_code == 200, r.text
    v2 = r.json()
    assert v2["version"] == 2 and v2["lineage_id"] == v1["lineage_id"]
    assert v2["supersedes_id"] == v1["id"] and v2["id"] != v1["id"]

    # The old version is still readable by id but no longer listed or editable.
    old = await client.get(_pp(e, f"workflow-templates/{v1['id']}"), headers=_hdrs(e.member_key))
    assert old.status_code == 200 and old.json()["active"] is False
    assert len(old.json()["definition"]["steps"]) == 5
    listed = await client.get(_pp(e, "workflow-templates"), headers=_hdrs(e.member_key))
    assert [t["id"] for t in listed.json()] == [v2["id"]]
    stale = await client.put(
        _pp(e, f"workflow-templates/{v1['id']}"),
        headers=_hdrs(e.owner_key),
        json={"name": "x", "definition": {"steps": steps}},
    )
    assert stale.status_code == 409
    assert _code(stale) == "workflow_template_superseded"

    d = await client.delete(_pp(e, f"workflow-templates/{v2['id']}"), headers=_hdrs(e.owner_key))
    assert d.status_code == 204
    listed = await client.get(_pp(e, "workflow-templates"), headers=_hdrs(e.owner_key))
    assert listed.json() == []
    inactive = await _start(client, e, v2["id"])
    assert inactive.status_code == 409
    assert _code(inactive) == "workflow_template_inactive"


@pytest.mark.asyncio
async def test_template_write_validation_and_access(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    bad = await client.post(
        _pp(e, "workflow-templates"),
        headers=_hdrs(e.owner_key),
        json={
            "name": "Loop",
            "definition": {
                "steps": [
                    {"id": "a", "title": "A", "role": "implementer", "agent": "atlas", "depends_on": ["b"]},
                    {"id": "b", "title": "B", "role": "implementer", "agent": "atlas", "depends_on": ["a"]},
                ]
            },
        },
    )
    assert bad.status_code == 422
    assert _code(bad) == "invalid_workflow_definition"
    assert "cycle" in _msg(bad)

    # Outsider: same 404 as an unknown project; no write.
    t = await _template(client, e)
    miss = await client.get(_pp(e, f"workflow-templates/{t['id']}"), headers=_hdrs(e.outsider_key))
    unknown = await client.get(
        f"/api/v1/projects/proj_{'0' * 16}/workflow-templates/{t['id']}", headers=_hdrs(e.owner_key)
    )
    nope = await client.get(_pp(e, "workflow-templates/wft_nope"), headers=_hdrs(e.owner_key))
    assert miss.status_code == unknown.status_code == nope.status_code == 404
    assert miss.json() == unknown.json() == nope.json()
    w = await client.post(
        _pp(e, "workflow-templates"),
        headers=_hdrs(e.outsider_key),
        json={"name": "x", "definition": {"steps": _steps()}},
    )
    assert w.status_code == 404 and w.json() == miss.json()


@pytest.mark.asyncio
async def test_org_templates(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    base = f"/api/v1/orgs/{e.org.id}/workflow-templates"
    body = {"name": "Org flow", "definition": {"steps": _steps()}}
    denied = await client.post(base, headers=_hdrs(e.member_key), json=body)
    assert denied.status_code == 403
    r = await client.post(base, headers=_hdrs(e.owner_key), json=body)
    assert r.status_code == 201, r.text
    org_t = r.json()
    assert org_t["scope"] == "org"
    assert (await client.get(base, headers=_hdrs(e.member_key))).json()[0]["id"] == org_t["id"]
    out = await client.get(base, headers=_hdrs(e.outsider_key))
    assert out.status_code == 404
    # Usable from a project in that org, and listed there.
    listed = await client.get(_pp(e, "workflow-templates"), headers=_hdrs(e.member_key))
    assert org_t["id"] in [t["id"] for t in listed.json()]
    started = await _start(client, e, org_t["id"])
    assert started.status_code == 201, started.text

    # Service keys read their own org's templates and never write them.
    _row, svc = await _make_service_key(db_session, e.org, e.owner, ["loops:read", "loops:write"])
    assert (await client.get(base, headers=_hdrs(svc))).status_code == 200
    assert (await client.post(base, headers=_hdrs(svc), json=body)).status_code == 403


# ── Start ──


@pytest.mark.asyncio
async def test_start_creates_children_per_slot(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    t = await _template(client, e)
    r = await _start(client, e, t["id"])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["workflow"] == {
        "template_id": t["id"], "lineage_id": t["lineage_id"], "version": 1, "name": "Ship",
    }
    counts = {s["step_id"]: len(s["tickets"]) for s in body["steps"]}
    # The reviewer step reviews the implement tickets and gets none of its own.
    assert counts == {"dispatch": 1, "implement": 2, "review": 0, "verify": 1, "document": 1}
    impl = [t for s in body["steps"] if s["step_id"] == "implement" for t in s["tickets"]]
    assert [t["index"] for t in impl] == [0, 1]
    assert impl[0]["agent"] == "atlas" and impl[0]["title"] == "Implement (1/2)"
    assert impl[0]["brief_markdown"].startswith("Build Payments retry\n\nRetry failed charges")
    assert body["reviewer"]["persona"] == "reviewer" and body["reviewer"]["trusted"] is True

    loop = await db_session.get(Ticket, body["loop_id"])
    assert '"workflow"' in loop.loop_config
    slots = (
        await db_session.execute(
            select(WorkflowStepChild).where(WorkflowStepChild.loop_ticket_id == loop.id)
        )
    ).scalars().all()
    assert sorted((s.step_id, s.step_index) for s in slots) == [
        ("dispatch", 0), ("document", 0), ("implement", 0), ("implement", 1), ("verify", 0),
    ]
    # The (loop, step, index) key is enforced by the database.
    db_session.add(
        WorkflowStepChild(
            id="wsc_dupe",
            project_id=e.project.id,
            loop_ticket_id=loop.id,
            ticket_id=_ticket_ids(body, "dispatch")[0],
            step_id="implement",
            step_index=0,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_start_adopt_twice_and_unknown_agent(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session, personas=("scout", "atlas", "reviewer", "sentinel"))
    t = await _template(client, e)
    missing = await _start(client, e, t["id"])
    assert missing.status_code == 422
    assert _code(missing) == "workflow_agent_unknown"
    assert missing.json()["error"]["details"]["agent"] == "scribe"
    assert _msg(missing) == "No active agent named 'scribe' is visible in this project."
    # Nothing was left behind by the refused start.
    assert (await db_session.execute(select(WorkflowStepChild))).scalars().all() == []

    await _persona(db_session, e.project, e.owner, "scribe")
    issue = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=e.project.id,
        title="Existing issue",
        description="Adopt me",
        kind="issue",
        status="open",
        created_by_user_id=e.owner.id,
    )
    db_session.add(issue)
    await db_session.commit()
    a = await _start(client, e, t["id"], ticket_id=issue.id, title="")
    assert a.status_code == 201, a.text
    assert a.json()["loop_id"] == issue.id
    brief = a.json()["steps"][1]["tickets"][0]["brief_markdown"]
    assert brief.startswith("Build Existing issue")
    b = await _start(client, e, t["id"], ticket_id=issue.id, title="")
    assert b.status_code == 409


@pytest.mark.asyncio
async def test_start_plan_gate_and_404_parity(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session, "solo", tier="free")
    project = await _make_project(db_session, owner)
    await _persona(db_session, project, owner, "atlas")
    steps = [{"id": "a", "title": "A", "role": "implementer", "agent": "atlas"}]
    r = await client.post(
        f"/api/v1/projects/{project.id}/workflow-templates",
        headers=_hdrs(key),
        json={"name": "Solo", "definition": {"steps": steps}},
    )
    # Template writes are Review Loop writes: plan-gated like starting one.
    assert r.status_code == 403, r.text
    assert _code(r) == "upgrade_required"
    row = WorkflowTemplate(
        id="wft_solo",
        project_id=project.id,
        name="Solo",
        lineage_id="wfl_solo",
        version=1,
        definition=json.dumps({"version": 1, "steps": steps}),
        active=True,
        created_by_user_id=owner.id,
    )
    db_session.add(row)
    await db_session.commit()
    started = await client.post(
        f"/api/v1/projects/{project.id}/workflows",
        headers=_hdrs(key),
        json={"template_id": row.id, "title": "x"},
    )
    assert started.status_code == 403

    e = await _env(db_session)
    t = await _template(client, e)
    other = await _start(client, e, row.id)  # another project's template
    unknown = await _start(client, e, "wft_missing")
    foreign = await client.post(
        _pp(e, "workflows"), headers=_hdrs(e.outsider_key), json={"template_id": t["id"], "title": "x"}
    )
    assert other.status_code == unknown.status_code == foreign.status_code == 404
    assert other.json() == unknown.json() == foreign.json()

    # Plan: a loop without a workflow is the same 404 as an unknown loop.
    loop = await client.post(
        _pp(e, "loops"),
        headers=_hdrs(e.owner_key),
        json={"title": "Plain", "tickets": [{"title": "c"}], "reviewer_persona": "reviewer"},
    )
    assert loop.status_code == 201, loop.text
    plain = await client.get(_pp(e, f"workflows/{loop.json()['loop_id']}/plan"), headers=_hdrs(e.owner_key))
    gone = await client.get(_pp(e, "workflows/tk_nope/plan"), headers=_hdrs(e.owner_key))
    assert plain.status_code == gone.status_code == 404 and plain.json() == gone.json()


# ── Plan + step reports ──


@pytest.mark.asyncio
async def test_plan_happy_path_and_step_validation(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    t = await _template(client, e)
    started = (await _start(client, e, t["id"])).json()
    loop_id = started["loop_id"]
    dispatch = _ticket_ids(started, "dispatch")[0]
    impl = _ticket_ids(started, "implement")
    verify = _ticket_ids(started, "verify")[0]

    plan, raw = await _plan(client, e, loop_id, key=e.member_key)
    assert raw["ready_steps"] == ["dispatch"]
    assert plan["dispatch"]["ready_children"][0]["ticket_id"] == dispatch
    assert plan["dispatch"]["ready_children"][0]["agent"] == "scout"
    assert plan["implement"]["state"] == "blocked"
    assert plan["implement"]["reason"] == "waiting for dispatch"

    # Dependencies not done.
    early = await _start_worker(client, e, impl[0])
    assert early.status_code == 422
    assert _code(early) == "workflow_step_mismatch"
    assert "waiting for: dispatch" in _msg(early)
    # Unknown step, wrong role, ticket of another step.
    for step_id, needle in (("ghost", "not in this workflow"), ("review", "role"), ("implement", "not a ticket")):
        r = await _start_worker(client, e, dispatch, step_id=step_id)
        assert r.status_code == 422, (step_id, r.text)
        assert needle in _msg(r)

    # Dispatch: the step is inferred from the ticket's slot.
    s = await _start_worker(client, e, dispatch)
    assert s.status_code == 201 and s.json()["step_id"] == "dispatch"
    plan, _ = await _plan(client, e, loop_id)
    assert plan["dispatch"]["state"] == "running"
    f = await _finish(client, e, s.json()["run_id"], step_id="dispatch")
    assert f.status_code == 200 and f.json()["step_id"] == "dispatch"
    plan, raw = await _plan(client, e, loop_id)
    assert plan["dispatch"]["state"] == "done"
    assert raw["ready_steps"] == ["implement"]
    assert len(plan["implement"]["ready_children"]) == 2

    # A finish naming a different step than the run's is refused.
    s0 = await _start_worker(client, e, impl[0], step_id="implement")
    assert s0.status_code == 201, s0.text
    wrong = await _finish(client, e, s0.json()["run_id"], step_id="dispatch")
    assert wrong.status_code == 422
    assert (await _finish(client, e, s0.json()["run_id"])).status_code == 200
    plan, _ = await _plan(client, e, loop_id)
    assert plan["implement"]["state"] == "running"
    assert [c["ticket_id"] for c in plan["implement"]["ready_children"]] == [impl[1]]
    await _work(client, e, impl[1])
    plan, raw = await _plan(client, e, loop_id)
    assert plan["implement"]["state"] == "done"
    assert raw["ready_steps"] == ["review"]
    assert sorted(c["ticket_id"] for c in plan["review"]["ready_children"]) == sorted(impl)
    assert sorted(plan["review"]["reviews"]) == sorted(impl)

    # Review rounds: role check, then trusted cleans on both implement tickets.
    bad_role = await _round(client, e, loop_id, impl[0], step_id="implement")
    assert bad_role.status_code == 422 and "role" in _msg(bad_role)
    r0 = await _round(client, e, loop_id, impl[0], step_id="review")
    assert r0.status_code == 201, r0.text
    assert r0.json()["step_id"] == "review" and r0.json()["item_status"] == "done"
    run = await db_session.get(AgentRun, r0.json()["run_id"])
    assert run.workflow_step_id == "review"
    plan, _ = await _plan(client, e, loop_id)
    assert plan["review"]["state"] == "running"
    assert plan["verify"]["state"] == "blocked"
    r1 = await _round(client, e, loop_id, impl[1], step_id="review")
    assert r1.status_code == 201, r1.text
    plan, raw = await _plan(client, e, loop_id)
    assert plan["review"]["state"] == "done"
    assert plan["review"]["gate_status"] == {"passed": True, "reason": None}
    assert raw["ready_steps"] == ["verify"]

    # Verify: work finishes but the approval gate has no approval yet.
    await _work(client, e, verify)
    plan, raw = await _plan(client, e, loop_id)
    assert plan["verify"]["state"] == "blocked"
    assert plan["verify"]["gate_status"]["passed"] is False
    assert plan["verify"]["gate_status"]["reason"].startswith("Waiting on")
    assert "Security sign-off" in plan["verify"]["reason"]
    assert plan["document"]["state"] == "blocked"
    assert raw["ready_steps"] == [] and raw["done"] is False


@pytest.mark.asyncio
async def test_trusted_clean_gate_blocks_until_clean(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    steps = [
        {"id": "build", "title": "Build", "role": "implementer", "agent": "atlas"},
        {
            "id": "review", "title": "Review", "role": "reviewer", "agent": "reviewer",
            "depends_on": ["build"], "gate": "trusted_clean",
        },
        {"id": "docs", "title": "Docs", "role": "documenter", "agent": "scribe", "depends_on": ["review"]},
    ]
    t = await _template(client, e, steps=steps)
    started = (await _start(client, e, t["id"])).json()
    loop_id = started["loop_id"]
    build = _ticket_ids(started, "build")[0]
    await _work(client, e, build)
    plan, _ = await _plan(client, e, loop_id)
    assert plan["review"]["state"] == "ready"
    assert plan["docs"]["state"] == "blocked"
    # A changes-requested round is the step's latest round: not done.
    r = await _round(client, e, loop_id, build, step_id="review", verdict="CHANGES-REQUESTED")
    assert r.status_code == 201, r.text
    plan, _ = await _plan(client, e, loop_id)
    assert plan["review"]["state"] == "running"
    r = await _round(client, e, loop_id, build, step_id="review", round=2)
    assert r.status_code == 201, r.text
    plan, raw = await _plan(client, e, loop_id)
    assert plan["review"]["state"] == "done"
    assert plan["review"]["gate_status"] == {"passed": True, "reason": None}
    assert raw["ready_steps"] == ["docs"]


@pytest.mark.asyncio
async def test_review_gate_refused_on_worker_steps(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    for role in ("implementer", "documenter", "dispatcher"):
        r = await client.post(
            _pp(e, "workflow-templates"),
            headers=_hdrs(e.owner_key),
            json={
                "name": "Bad",
                "definition": {
                    "steps": [
                        {"id": "a", "title": "A", "role": role, "agent": "atlas", "gate": "trusted_clean"}
                    ]
                },
            },
        )
        assert r.status_code == 422, r.text
        assert _code(r) == "invalid_workflow_definition"
        assert "belongs on a reviewer or verifier step" in _msg(r)
        assert f"role {role!r}" in _msg(r)


@pytest.mark.asyncio
async def test_step_id_outside_workflow_refused(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    loop = await client.post(
        _pp(e, "loops"),
        headers=_hdrs(e.owner_key),
        json={"title": "Plain", "tickets": [{"title": "c"}], "reviewer_persona": "reviewer"},
    )
    child = loop.json()["tickets"][0]["id"]
    r = await _start_worker(client, e, child, step_id="build")
    assert r.status_code == 422
    assert _msg(r) == "step_id was given but this loop has no workflow"
    ok = await _start_worker(client, e, child)
    assert ok.status_code == 201 and ok.json()["step_id"] is None


@pytest.mark.asyncio
async def test_service_key_scopes_on_project_templates(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    t = await _template(client, e)
    _r, reader = await _make_service_key(db_session, e.org, e.owner, ["loops:read"])
    _w, writer = await _make_service_key(db_session, e.org, e.owner, ["loops:read", "loops:write"])
    other_org = await _make_org(db_session)
    _o, foreign = await _make_service_key(db_session, other_org, e.owner, ["loops:read"])

    assert (await client.get(_pp(e, f"workflow-templates/{t['id']}"), headers=_hdrs(reader))).status_code == 200
    denied = await client.post(
        _pp(e, "workflow-templates"), headers=_hdrs(reader), json={"name": "x", "definition": {"steps": _steps()}}
    )
    assert denied.status_code == 403
    made = await client.post(
        _pp(e, "workflow-templates"), headers=_hdrs(writer), json={"name": "x", "definition": {"steps": _steps()}}
    )
    assert made.status_code == 201, made.text
    assert made.json()["actor_type"] == "service_key"
    cross = await client.get(_pp(e, f"workflow-templates/{t['id']}"), headers=_hdrs(foreign))
    unknown = await client.get(_pp(e, "workflow-templates/wft_nope"), headers=_hdrs(e.owner_key))
    assert cross.status_code == 404 and cross.json() == unknown.json()
    # Starting needs the downstream write scopes too.
    no_down = await client.post(
        _pp(e, "workflows"), headers=_hdrs(writer), json={"template_id": t["id"], "title": "x"}
    )
    assert no_down.status_code == 403 and _code(no_down) == "insufficient_scope"
