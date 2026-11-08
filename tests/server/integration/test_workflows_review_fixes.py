"""Workflow templates (GitHub #120, W1): regression tests for the review
findings — per-step reviewer verdicts, transitive reviewer subjects, plan
query counts, service-key allowlists on org templates, org-hidden plans,
write gates and caps, start re-checks, DLP and approval-policy scope."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    ApprovalPolicy,
    Organization,
    Project,
    Ticket,
    TicketComment,
    TrustedReviewer,
    WorkflowTemplate,
)
from sessionfs.server.services import workflows as wf
from tests.server.integration.approvals_helpers import CLEAN
from tests.server.integration.test_loops_api import (
    _add_org_member,
    _hdrs,
    _make_org,
    _make_project,
    _make_service_key,
    _make_user,
)
from tests.server.integration.test_workflows import (
    SHA1,
    _code,
    _env,
    _finish,
    _msg,
    _persona,
    _plan,
    _pp,
    _round,
    _start,
    _start_worker,
    _template,
    _ticket_ids,
    _work,
)


async def _trust(db: AsyncSession, e, user, persona: str) -> None:
    db.add(
        TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:16]}",
            project_id=e.project.id,
            user_id=user.id,
            reviewer_persona=persona,
            is_active=True,
            created_by_user_id=e.owner.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


def _two_reviewers(brief: str = "") -> list[dict]:
    return [
        {"id": "implement", "title": "Implement", "role": "implementer", "agent": "atlas", "fan_out": 2},
        {"id": "review", "title": "Review", "role": "reviewer", "agent": "reviewer", "depends_on": ["implement"]},
        {
            "id": "security-review",
            "title": "Security review",
            "role": "reviewer",
            "agent": "sentinel",
            # Depends only on another reviewer step: reviews the same tickets.
            "depends_on": ["review"],
            "brief_template": brief,
        },
        {"id": "docs", "title": "Docs", "role": "documenter", "agent": "scribe", "depends_on": ["security-review"]},
    ]


# ── 1, 2, 11: per-step reviewer verdicts, transitive subjects, reviewer briefs ──


@pytest.mark.asyncio
async def test_second_reviewer_step_needs_its_own_trusted_round(
    client: AsyncClient, db_session: AsyncSession
):
    e = await _env(db_session)
    sec, sec_key = await _make_user(db_session, "sec")
    await _add_org_member(db_session, e.org, sec, "member")
    await _trust(db_session, e, sec, "sentinel")
    t = await _template(
        client, e, steps=_two_reviewers("Security pass on {ticket.title}\n\n{checklist}")
    )
    started = (await _start(client, e, t["id"])).json()
    loop_id = started["loop_id"]
    impl = _ticket_ids(started, "implement")
    for tid in impl:
        await _work(client, e, tid)

    plan, _ = await _plan(client, e, loop_id)
    # Transitive subjects: the second reviewer reviews the implement tickets.
    assert sorted(plan["security-review"]["reviews"]) == sorted(impl)
    assert plan["security-review"]["state"] == "blocked"

    for tid in impl:
        r = await _round(client, e, loop_id, tid, step_id="review")
        assert r.status_code == 201 and r.json()["item_status"] == "done", r.text
    plan, raw = await _plan(client, e, loop_id)
    assert plan["review"]["state"] == "done"
    # Codex's clean (which closed the items) does not pass the security step.
    assert plan["security-review"]["state"] == "ready"
    assert raw["ready_steps"] == ["security-review"]
    ready = plan["security-review"]["ready_children"]
    assert sorted(c["ticket_id"] for c in ready) == sorted(impl)
    # The reviewer step's own brief, not the implement ticket's.
    assert ready[0]["brief_markdown"].startswith("Security pass on Payments retry")
    assert plan["docs"]["state"] == "blocked"

    # A round by the queue's reviewer tagged for the security step is
    # checked against the sentinel persona: not trusted, so not passed.
    wrong = await _round(client, e, loop_id, impl[0], step_id="security-review")
    assert wrong.status_code == 201, wrong.text
    assert wrong.json()["verdict_trusted"] is False
    assert wrong.json()["idempotent"] is False  # its own round 1, not review's
    comment = await db_session.get(TicketComment, wrong.json()["comment_id"])
    assert comment.author_persona == "sentinel"
    plan, _ = await _plan(client, e, loop_id)
    assert plan["security-review"]["state"] == "running"

    # The sentinel's trusted rounds on the already-closed items pass it.
    for tid in impl:
        r = await _round(client, e, loop_id, tid, key=sec_key, step_id="security-review", round=2)
        assert r.status_code == 201, r.text
        assert r.json()["verdict_trusted"] is True and r.json()["item_status"] == "done"
    plan, raw = await _plan(client, e, loop_id)
    assert plan["security-review"]["state"] == "done"
    assert raw["ready_steps"] == ["docs"]


@pytest.mark.asyncio
async def test_reviewer_round_at_old_head_does_not_pass(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    steps = _two_reviewers()[:2]
    t = await _template(client, e, steps=steps)
    started = (await _start(client, e, t["id"])).json()
    loop_id = started["loop_id"]
    impl = _ticket_ids(started, "implement")
    for tid in impl:
        await _work(client, e, tid)
    other = "b" * 40
    r = await _round(client, e, loop_id, impl[0], step_id="review", reviewed_commit=other)
    assert r.status_code == 201, r.text
    await _round(client, e, loop_id, impl[1], step_id="review")
    plan, _ = await _plan(client, e, loop_id)
    assert plan["review"]["state"] != "done"
    assert [c["ticket_id"] for c in plan["review"]["ready_children"]] == [impl[0]]


# ── 3: query count is not linear in fan-out ──


async def _reviewed_loop(client, db: AsyncSession, fan_out: int):
    e = await _env(db)
    steps = [
        {"id": "implement", "title": "Implement", "role": "implementer", "agent": "atlas", "fan_out": fan_out},
        {
            "id": "review", "title": "Review", "role": "reviewer", "agent": "reviewer",
            "depends_on": ["implement"], "gate": "trusted_clean",
        },
        {"id": "docs", "title": "Docs", "role": "documenter", "agent": "scribe", "depends_on": ["review"]},
    ]
    t = await _template(client, e, steps=steps)
    started = (await _start(client, e, t["id"])).json()
    for tid in _ticket_ids(started, "implement"):
        await _work(client, e, tid)
    for tid in _ticket_ids(started, "implement"):
        r = await _round(client, e, started["loop_id"], tid, step_id="review")
        assert r.status_code == 201, r.text
    loop = await db.get(Ticket, started["loop_id"])
    docs = await db.get(Ticket, _ticket_ids(started, "docs")[0])
    return loop, docs


async def _count(db: AsyncSession, coro_factory) -> int:
    seen: list[str] = []

    def _on(conn, cursor, statement, *args):
        seen.append(statement)

    engine = db.bind.sync_engine
    event.listen(engine, "before_cursor_execute", _on)
    try:
        await coro_factory()
    finally:
        event.remove(engine, "before_cursor_execute", _on)
    return len(seen)


@pytest.mark.asyncio
async def test_plan_query_count_not_linear_in_fan_out(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    from sessionfs.server.routes.loops import _load_loop_config
    from sessionfs.server.services import merge_checks

    # The engine-wide counter would also see the detached merge-check
    # rechecks the review rounds spawn; only compute_plan is measured here.
    monkeypatch.setattr(merge_checks, "schedule_recheck", lambda t: None)

    counts = []
    for fan_out in (2, 8):
        loop, docs = await _reviewed_loop(client, db_session, fan_out)
        config = _load_loop_config(loop.loop_config)
        plan = await wf.compute_plan(db_session, loop, config)
        st = plan.steps["review"]
        assert st.done, (st.state, st.reason, st.gate_reason)
        full = await _count(db_session, lambda: wf.compute_plan(db_session, loop, config))
        check = await _count(
            db_session,
            lambda: wf.check_step_report(
                db_session, loop=loop, loop_config=config, ticket=docs,
                step_id="docs", kind="worker",
            ),
        )
        counts.append((full, check))
    assert counts[0] == counts[1], counts
    # Report validation skips gates of steps it does not depend on.
    assert counts[0][1] <= counts[0][0]


def test_gate_cap_reports_instead_of_querying():
    import asyncio

    ctx = wf._GateContext(db=None, project_id="p")  # type: ignore[arg-type]
    ctx.budget = 1

    def child(i: int):
        t = Ticket(id=f"tk_{i}", project_id="p", title="t")
        return wf._Child(
            ticket=t, index=i, item=None,
            runs=[wf._Run(f"r{i}", t.id, "implementer", "passed", SHA1, "s", None, None)],
        )

    ok, why = asyncio.run(wf._trusted_clean_gate(ctx, [child(1), child(2)]))
    assert ok is False and "too many tickets" in why


# ── 4: allowlisted service keys never read org templates org-wide ──


@pytest.mark.asyncio
async def test_allowlisted_service_key_refused_on_org_templates(
    client: AsyncClient, db_session: AsyncSession
):
    e = await _env(db_session)
    base = f"/api/v1/orgs/{e.org.id}/workflow-templates"
    made = await client.post(
        base, headers=_hdrs(e.owner_key), json={"name": "Org", "definition": {"steps": _two_reviewers()}}
    )
    assert made.status_code == 201, made.text
    tid = made.json()["id"]
    _r, scoped = await _make_service_key(
        db_session, e.org, e.owner, ["loops:read"], project_ids=[e.project.id]
    )
    unknown = await client.get("/api/v1/orgs/org_nope/workflow-templates", headers=_hdrs(e.owner_key))
    listed = await client.get(base, headers=_hdrs(scoped))
    got = await client.get(f"{base}/{tid}", headers=_hdrs(scoped))
    assert listed.status_code == got.status_code == unknown.status_code == 404
    assert listed.json() == got.json() == unknown.json()
    # The project route still serves it to the key.
    via_project = await client.get(_pp(e, f"workflow-templates/{tid}"), headers=_hdrs(scoped))
    assert via_project.status_code == 200, via_project.text
    listed_p = await client.get(_pp(e, "workflow-templates"), headers=_hdrs(scoped))
    assert tid in [t["id"] for t in listed_p.json()]
    assert listed_p.headers["X-Result-Truncated"] == "false"


# ── 5: plan hides org-template fields from callers who cannot see the org ──


@pytest.mark.asyncio
async def test_plan_omits_org_template_fields_when_org_hidden(
    client: AsyncClient, db_session: AsyncSession
):
    e = await _env(db_session)
    ghost, ghost_key = await _make_user(db_session, "ghost")  # owns, not an org member
    project = await _make_project(db_session, ghost, e.org)
    for name in ("atlas", "reviewer"):
        await _persona(db_session, project, ghost, name)
    steps = _two_reviewers()[:2]
    made = await client.post(
        f"/api/v1/orgs/{e.org.id}/workflow-templates",
        headers=_hdrs(e.owner_key),
        json={"name": "Secret org flow", "definition": {"steps": steps}},
    )
    assert made.status_code == 201, made.text
    started = await client.post(
        f"/api/v1/projects/{project.id}/workflows",
        headers=_hdrs(e.owner_key),
        json={"template_id": made.json()["id"], "title": "x"},
    )
    assert started.status_code == 201, started.text
    loop_id = started.json()["loop_id"]
    url = f"/api/v1/projects/{project.id}/workflows/{loop_id}/plan"

    member = (await client.get(url, headers=_hdrs(e.owner_key))).json()
    assert member["template"]["name"] == "Secret org flow"
    assert member["steps"][0]["title"] == "Implement"

    hidden = await client.get(url, headers=_hdrs(ghost_key))
    assert hidden.status_code == 200, hidden.text
    body = hidden.json()
    assert body["template"]["name"] is None
    for step in body["steps"]:
        assert step["title"] is None and step["agent"] is None and step["gate"] is None
        assert step["state"]
    # Their own ticket briefs stay.
    ready = body["steps"][0]["ready_children"]
    assert ready and "brief_markdown" in ready[0]


# ── 6: template writes are plan-gated and capped ──


@pytest.mark.asyncio
async def test_template_cap_and_org_plan_gate(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    from sessionfs.server.routes import workflows as routes

    monkeypatch.setattr(routes, "MAX_ACTIVE_TEMPLATES", 2)
    e = await _env(db_session)
    a = await _template(client, e)
    await _template(client, e)
    over = await client.post(
        _pp(e, "workflow-templates"),
        headers=_hdrs(e.owner_key),
        json={"name": "Third", "definition": {"steps": _two_reviewers()}},
    )
    assert over.status_code == 409 and _code(over) == "template_limit"
    # Editing replaces an active version, so it is not refused.
    edit = await client.put(
        _pp(e, f"workflow-templates/{a['id']}"),
        headers=_hdrs(e.owner_key),
        json={"name": "A2", "definition": {"steps": _two_reviewers()}},
    )
    assert edit.status_code == 200, edit.text
    # Disabling frees a slot.
    assert (await client.delete(_pp(e, f"workflow-templates/{edit.json()['id']}"), headers=_hdrs(e.owner_key))).status_code == 204
    assert (await _template(client, e))["active"]

    free_org = await _make_org(db_session)
    free_org.tier = "free"
    await db_session.commit()
    await _add_org_member(db_session, free_org, e.owner, "admin")
    gated = await client.post(
        f"/api/v1/orgs/{free_org.id}/workflow-templates",
        headers=_hdrs(e.owner_key),
        json={"name": "x", "definition": {"steps": _two_reviewers()}},
    )
    assert gated.status_code == 403 and _code(gated) == "upgrade_required"
    # Visibility first: a stranger gets the 404, not the plan answer.
    stranger = await client.post(
        f"/api/v1/orgs/{free_org.id}/workflow-templates",
        headers=_hdrs(e.outsider_key),
        json={"name": "x", "definition": {"steps": _two_reviewers()}},
    )
    assert stranger.status_code == 404


# ── 7: start re-checks the template and maps errors precisely ──


@pytest.mark.asyncio
async def test_start_refuses_superseded_and_broken_templates(
    client: AsyncClient, db_session: AsyncSession
):
    e = await _env(db_session)
    t = await _template(client, e)
    # An (inconsistent) active row that a newer version already supersedes.
    db_session.add(
        WorkflowTemplate(
            id="wft_newer",
            project_id=e.project.id,
            name="Newer",
            lineage_id="wfl_other",
            version=1,
            definition=json.dumps({"version": 1, "steps": _two_reviewers()}),
            active=False,
            supersedes_id=t["id"],
            created_by_user_id=e.owner.id,
        )
    )
    await db_session.commit()
    r = await _start(client, e, t["id"])
    assert r.status_code == 409 and _code(r) == "workflow_template_inactive"

    db_session.add(
        WorkflowTemplate(
            id="wft_broken",
            project_id=e.project.id,
            name="Broken",
            lineage_id="wfl_broken",
            version=1,
            definition='{"version": 1, "steps": []}',
            active=True,
            created_by_user_id=e.owner.id,
        )
    )
    await db_session.commit()
    broken = await _start(client, e, "wft_broken")
    assert broken.status_code == 422 and _code(broken) == "invalid_workflow_definition"


def test_only_step_slot_conflicts_map_to_already_started():
    from sessionfs.server.routes.workflows import _is_step_slot_conflict

    slot = IntegrityError("x", {}, Exception("UNIQUE constraint failed: workflow_step_children.ticket_id"))
    pg = IntegrityError("x", {}, Exception('duplicate key value violates unique constraint "uq_workflow_step_children_slot"'))
    other = IntegrityError("x", {}, Exception("UNIQUE constraint failed: tickets.id"))
    fk = IntegrityError("x", {}, Exception("FOREIGN KEY constraint failed"))
    assert _is_step_slot_conflict(slot) and _is_step_slot_conflict(pg)
    assert not _is_step_slot_conflict(other) and not _is_step_slot_conflict(fk)


# ── 8: DLP fails closed and applies the org policy on writes ──


def test_clean_multiline_fails_closed(monkeypatch):
    from sessionfs import workflow_definition as wd
    from sessionfs.server import dlp

    def boom(*a, **k):
        raise RuntimeError("scanner down")

    monkeypatch.setattr(dlp, "scan_dlp", boom)
    with pytest.raises(wd.WorkflowDefinitionError):
        wd.clean_multiline("anything")
    with pytest.raises(wd.WorkflowDefinitionError, match="scanned for secrets"):
        wd.parse_definition({"steps": [{"id": "a", "title": "A", "role": "implementer", "agent": "x"}]})


@pytest.mark.asyncio
async def test_template_write_applies_org_dlp_policy(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    e = await _env(db_session)
    org = await db_session.get(Organization, e.org.id)
    policy = {
        "enabled": True,
        "mode": "redact",
        "categories": ["secrets"],
        "custom_patterns": [{"name": "ssn", "regex": r"\d{3}-\d{2}-\d{4}", "category": "pii", "severity": "high"}],
    }
    org.settings = json.dumps({"dlp": policy})
    await db_session.commit()
    steps = [{"id": "a", "title": "A", "role": "implementer", "agent": "atlas", "brief_template": "SSN 123-45-6789"}]
    r = await client.post(
        _pp(e, "workflow-templates"), headers=_hdrs(e.owner_key), json={"name": "T", "definition": {"steps": steps}}
    )
    assert r.status_code == 201, r.text
    assert "123-45-6789" not in r.json()["definition"]["steps"][0]["brief_template"]

    org.settings = json.dumps({"dlp": {**policy, "mode": "block"}})
    await db_session.commit()
    blocked = await client.post(
        _pp(e, "workflow-templates"), headers=_hdrs(e.owner_key), json={"name": "T", "definition": {"steps": steps}}
    )
    assert blocked.status_code == 422 and _code(blocked) == "dlp_blocked"

    from sessionfs.server.routes import meetings

    def boom(*a, **k):
        raise RuntimeError("scanner down")

    monkeypatch.setattr(meetings, "scan_dlp_full", boom)
    down = await client.post(
        _pp(e, "workflow-templates"),
        headers=_hdrs(e.owner_key),
        json={"name": "T", "definition": {"steps": steps}},
    )
    assert down.status_code == 422 and _code(down) == "dlp_unavailable"


# ── 9: approval-policy gates: real policies, scope, errors, self-approval ──


async def _policy(db: AsyncSession, org_id: str, *, name: str = "Security sign-off") -> str:
    pid = f"ap_{uuid.uuid4().hex[:24]}"
    db.add(
        ApprovalPolicy(
            id=pid,
            org_id=org_id,
            lineage_id=pid,
            name=name,
            target_kind="merge",
            filter="{}",
            requirements=json.dumps([{"who": "org_role", "role": "admin", "count": 1}]),
        )
    )
    await db.commit()
    return pid


def _gated_steps(policy: str) -> list[dict]:
    return [
        {"id": "build", "title": "Build", "role": "implementer", "agent": "atlas",
         "gate": f"approval_policy:{policy}"},
        {"id": "docs", "title": "Docs", "role": "documenter", "agent": "scribe", "depends_on": ["build"]},
    ]


async def _seed_trusted_clean(db: AsyncSession, ticket_id: str, reviewer) -> None:
    db.add(
        TicketComment(
            id=f"tc_{uuid.uuid4().hex[:16]}",
            ticket_id=ticket_id,
            author_user_id=reviewer.id,
            author_persona="codex-reviewer",
            content=CLEAN,
            verdict_trusted=True,
            reviewed_commit=SHA1,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


@pytest.mark.asyncio
async def test_approval_policy_gate_with_a_real_policy(client: AsyncClient, db_session: AsyncSession):
    """Not passed while the request is pending, never passed by the maker's
    own approval, passed once an independent approver approves."""
    from sessionfs.server.services import approvals

    e = await _env(db_session)
    approver, approver_key = await _make_user(db_session, "approver")
    await _add_org_member(db_session, e.org, approver, "admin")
    pid = await _policy(db_session, e.org.id)

    t = await _template(client, e, steps=_gated_steps(pid))
    started = (await _start(client, e, t["id"])).json()
    loop_id = started["loop_id"]
    build = _ticket_ids(started, "build")[0]
    await _work(client, e, build)  # implemented with the owner's key

    plan, raw = await _plan(client, e, loop_id)
    assert plan["build"]["gate_status"]["passed"] is False
    assert plan["build"]["gate_status"]["reason"] == "Waiting on approval: Security sign-off (0 of 1)"
    assert raw["ready_steps"] == []

    # A trusted clean at the head: the trigger raises the pending request.
    await _seed_trusted_clean(db_session, build, e.reviewer)
    ticket = await db_session.get(Ticket, build)
    await approvals.raise_ticket_approvals(db_session, ticket)
    await db_session.commit()
    readiness = await approvals.ticket_readiness(db_session, ticket, SHA1)
    (req,) = [a for a in readiness["approvals"] if a["policy_id"] == pid]
    assert req["state"] == "pending"
    plan, raw = await _plan(client, e, loop_id)
    assert plan["build"]["gate_status"]["passed"] is False
    assert "Waiting on approval: Security sign-off" in plan["build"]["gate_status"]["reason"]
    assert plan["build"]["state"] != "done" and raw["ready_steps"] == []

    # The maker (owner implemented the step and is an org admin) can't approve.
    maker = await client.post(
        f"/api/v1/approval-requests/{req['request_id']}/decide",
        headers=_hdrs(e.owner_key),
        json={"decision": "approve", "reason": "Looks fine to me."},
    )
    assert maker.status_code == 403 and "self_approval_forbidden" in maker.text, maker.text
    plan, raw = await _plan(client, e, loop_id)
    assert plan["build"]["gate_status"]["passed"] is False and raw["ready_steps"] == []

    ok = await client.post(
        f"/api/v1/approval-requests/{req['request_id']}/decide",
        headers=_hdrs(approver_key),
        json={"decision": "approve", "reason": "Reviewed the change."},
    )
    assert ok.status_code == 200, ok.text
    plan, raw = await _plan(client, e, loop_id)
    assert plan["build"]["gate_status"]["passed"] is True
    assert plan["build"]["state"] == "done" and raw["ready_steps"] == ["docs"]


@pytest.mark.asyncio
async def test_approval_policy_scope_and_errors_fail_closed(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    from sessionfs.server.services import approvals

    e = await _env(db_session)
    other = await _make_org(db_session)
    far = await _policy(db_session, other.id)

    for policy in (far, "ap_missing"):
        r = await client.post(
            _pp(e, "workflow-templates"),
            headers=_hdrs(e.owner_key),
            json={"name": "T", "definition": {"steps": _gated_steps(policy)}},
        )
        assert r.status_code == 422, r.text
        assert "is not available to this template's organization" in _msg(r)

    pid = await _policy(db_session, e.org.id)
    t = await _template(client, e, steps=_gated_steps(pid))
    started = (await _start(client, e, t["id"])).json()
    loop_id = started["loop_id"]
    await _work(client, e, _ticket_ids(started, "build")[0])

    async def boom(*a, **k):
        raise RuntimeError("engine down")

    monkeypatch.setattr(approvals, "workflow_gate_passed", boom)
    plan, _ = await _plan(client, e, loop_id)
    assert plan["build"]["gate_status"] == {
        "passed": False, "reason": "approval gate could not be evaluated",
    }

    async def always(*a, **k):
        return True, "Approved"

    monkeypatch.setattr(approvals, "workflow_gate_passed", always)
    plan, raw = await _plan(client, e, loop_id)
    assert plan["build"]["state"] == "done" and raw["ready_steps"] == ["docs"]

    # A deactivated policy (no active version in its lineage) never passes.
    row = await db_session.get(ApprovalPolicy, pid)
    row.active = False
    await db_session.commit()
    plan, _ = await _plan(client, e, loop_id)
    assert plan["build"]["gate_status"]["passed"] is False
    assert "no longer active" in plan["build"]["gate_status"]["reason"]
    row.active = True
    await db_session.commit()

    # A policy moved to another org after the template was saved never passes.
    row.org_id = other.id
    await db_session.commit()
    plan, _ = await _plan(client, e, loop_id)
    assert plan["build"]["gate_status"]["passed"] is False
    assert "not available here" in plan["build"]["gate_status"]["reason"]
    # Starting re-validates the scope too.
    again = await _start(client, e, t["id"], title="again")
    assert again.status_code == 422 and _code(again) == "invalid_workflow_definition"


@pytest.mark.asyncio
async def test_personal_project_cannot_use_approval_policies(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, "solo", tier="team")
    project = await _make_project(db_session, owner)
    org = await _make_org(db_session)
    pid = await _policy(db_session, org.id)
    p = await db_session.get(Project, project.id)
    assert p is not None and p.org_id is None
    r = await client.post(
        f"/api/v1/projects/{project.id}/workflow-templates",
        headers=_hdrs(key),
        json={
            "name": "T",
            "definition": {
                "steps": [{"id": "a", "title": "A", "role": "implementer", "agent": "atlas",
                           "gate": f"approval_policy:{pid}"}]
            },
        },
    )
    assert r.status_code == 422, r.text
    rows = (await db_session.execute(select(WorkflowTemplate))).scalars().all()
    assert rows == []


# ── Self-review: a later reviewer step never passes on the implementer ──


async def _trust_key(db: AsyncSession, e, key_id: str, persona: str) -> None:
    db.add(
        TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:16]}",
            project_id=e.project.id,
            service_key_id=key_id,
            reviewer_persona=persona,
            is_active=True,
            created_by_user_id=e.owner.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()


_ROUND_SCOPES = ["loops:read", "loops:write", "tickets:write", "agent_runs:write", "work_queues:write"]


@pytest.mark.asyncio
@pytest.mark.parametrize("who", ["implementer_user", "minted_key", "rotated_key"])
async def test_second_reviewer_step_refuses_self_review(
    client: AsyncClient, db_session: AsyncSession, who: str
):
    from sessionfs.server.db.models import ApiKey

    e = await _env(db_session)
    # The owner implements (their key starts and finishes the worker runs).
    if who == "implementer_user":
        key = e.owner_key
        await _trust(db_session, e, e.owner, "sentinel")
    else:
        row, key = await _make_service_key(db_session, e.org, e.owner, _ROUND_SCOPES)
        if who == "rotated_key":
            # A key someone else holds now, rotated from one the owner minted.
            other, _k = await _make_user(db_session, "other")
            await _add_org_member(db_session, e.org, other, "member")
            new_row, key = await _make_service_key(db_session, e.org, other, _ROUND_SCOPES)
            fresh = await db_session.get(ApiKey, new_row.id)
            fresh.rotated_from_key_id = row.id
            await db_session.commit()
            row = fresh
        await _trust_key(db_session, e, row.id, "sentinel")

    t = await _template(client, e, steps=_two_reviewers())
    started = (await _start(client, e, t["id"])).json()
    loop_id = started["loop_id"]
    impl = _ticket_ids(started, "implement")
    for tid in impl:
        await _work(client, e, tid)
    for tid in impl:
        r = await _round(client, e, loop_id, tid, step_id="review")
        assert r.status_code == 201 and r.json()["item_status"] == "done", r.text

    for tid in impl:
        r = await _round(client, e, loop_id, tid, key=key, step_id="security-review")
        assert r.status_code == 201, r.text
        # Trusted for the sentinel persona, but not independent.
        assert r.json()["verdict_trusted"] is True
    plan, raw = await _plan(client, e, loop_id)
    assert plan["security-review"]["state"] == "running"
    assert raw["ready_steps"] == []
    assert plan["docs"]["state"] == "blocked"


def test_independence_fails_closed():
    run = wf._Run("r", "t", "reviewer", "passed", SHA1, "s", "sentinel", "c", "u_rev", None)
    key_run = wf._Run("r2", "t", "reviewer", "passed", SHA1, "s", "sentinel", "c", "u_min", "k_rev")
    anon = wf._Run("r3", "t", "reviewer", "passed", SHA1, "s", "sentinel", "c", None, None)
    impl = [("k_impl", "u_impl")]
    ok = {"k_impl": frozenset({"u_impl"}), "k_rev": frozenset({"u_min"})}
    assert wf._is_independent(run, impl, ok) is True
    assert wf._is_independent(run, [], ok) is False  # no implementer recorded
    assert wf._is_independent(anon, impl, ok) is False  # no recorded actor
    assert wf._is_independent(key_run, impl, {**ok, "k_rev": None}) is False  # unresolvable reviewer key
    assert wf._is_independent(run, impl, {**ok, "k_impl": None}) is False  # unresolvable implementer key
    assert wf._is_independent(key_run, impl, {**ok, "k_rev": frozenset({"u_min", "u_impl"})}) is False


# ── Codex round: transitive dependencies, bounded history, guarded disable ──


@pytest.mark.asyncio
async def test_report_needs_dependencies_done_transitively(
    client: AsyncClient, db_session: AsyncSession
):
    e = await _env(db_session)
    steps = [
        {"id": "a", "title": "A", "role": "dispatcher", "agent": "scout"},
        {"id": "b", "title": "B", "role": "implementer", "agent": "atlas", "depends_on": ["a"]},
        {"id": "c", "title": "C", "role": "documenter", "agent": "scribe", "depends_on": ["b"]},
    ]
    t = await _template(client, e, steps=steps)
    started = (await _start(client, e, t["id"])).json()
    # B's ticket is closed by hand while A never ran.
    b = await db_session.get(Ticket, _ticket_ids(started, "b")[0])
    b.status = "done"
    await db_session.commit()
    c = await _start_worker(client, e, _ticket_ids(started, "c")[0])
    assert c.status_code == 422, c.text
    assert "waiting for: b" in _msg(c)
    await _work(client, e, _ticket_ids(started, "a")[0])
    ok = await _start_worker(client, e, _ticket_ids(started, "c")[0])
    assert ok.status_code == 201, ok.text


def _verdict(ticket_id: str, n: int, verdict: str, *, trusted: bool, at: datetime) -> TicketComment:
    return TicketComment(
        id=f"tc_{uuid.uuid4().hex[:16]}",
        ticket_id=ticket_id,
        author_user_id="u",
        author_persona="reviewer",
        content=f"Reviewer R{n} review on {ticket_id}: {verdict}\n\nFindings: none.",
        created_at=at,
        verdict_trusted=trusted,
        reviewed_commit=SHA1,
    )


@pytest.mark.asyncio
async def test_trusted_clean_history_is_newest_and_fails_closed(
    db_session: AsyncSession, monkeypatch
):
    from datetime import timedelta

    monkeypatch.setattr(wf, "MAX_VERDICT_HISTORY", 3)
    owner, _k = await _make_user(db_session, "o")
    project = await _make_project(db_session, owner)
    base = datetime(2026, 11, 1, tzinfo=timezone.utc)
    tickets = []
    for name in ("noisy", "long"):
        t = Ticket(id=f"tk_{uuid.uuid4().hex[:16]}", project_id=project.id, title=name, created_by_user_id=owner.id)
        db_session.add(t)
        tickets.append(t)
    noisy, long_ = tickets
    # Untrusted comments never count against the cap.
    for i in range(10):
        db_session.add(_verdict(noisy.id, i + 1, "VERIFIED-CLEAN", trusted=False, at=base + timedelta(minutes=i)))
    db_session.add(_verdict(noisy.id, 11, "VERIFIED-CLEAN", trusted=True, at=base + timedelta(hours=1)))
    # More trusted verdicts than the cap, the newest one contrary.
    for i in range(3):
        db_session.add(_verdict(long_.id, i + 1, "VERIFIED-CLEAN", trusted=True, at=base + timedelta(minutes=i)))
    db_session.add(_verdict(long_.id, 4, "CHANGES REQUESTED", trusted=True, at=base + timedelta(hours=1)))
    await db_session.commit()

    ctx = wf._GateContext(db_session, project.id)
    await wf._batch_readiness(ctx, [(noisy, SHA1), (long_, SHA1)])
    _ok, why_long = ctx.readiness[(long_.id, SHA1)]
    assert _ok is False and "too long to evaluate completely" in why_long
    _ok2, why_noisy = ctx.readiness[(noisy.id, SHA1)]
    assert "too long" not in (why_noisy or "")


@pytest.mark.asyncio
async def test_plan_reads_only_the_latest_run_per_kind(client: AsyncClient, db_session: AsyncSession):
    from sessionfs.server.routes.loops import _load_loop_config

    e = await _env(db_session)
    steps = [{"id": "build", "title": "Build", "role": "implementer", "agent": "atlas"}]
    t = await _template(client, e, steps=steps)
    started = (await _start(client, e, t["id"])).json()
    build = _ticket_ids(started, "build")[0]
    await _work(client, e, build)
    for _ in range(3):
        s = await _start_worker(client, e, build)
        assert s.status_code == 201, s.text
        f = await _finish(client, e, s.json()["run_id"], exit_reason="crashed")
        assert f.status_code == 200, f.text
    loop = await db_session.get(Ticket, started["loop_id"])
    plan = await wf.compute_plan(db_session, loop, _load_loop_config(loop.loop_config))
    child = plan.steps["build"].children[0]
    assert len(child.runs) == 1 and child.runs[0].status == "errored"
    assert plan.steps["build"].state == "running"


@pytest.mark.asyncio
async def test_disable_is_guarded(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    t = await _template(client, e)
    url = _pp(e, f"workflow-templates/{t['id']}")
    assert (await client.delete(url, headers=_hdrs(e.owner_key))).status_code == 204
    again = await client.delete(url, headers=_hdrs(e.owner_key))
    assert again.status_code == 409 and _code(again) == "template_stale"


def test_run_order_ignores_host_timezone_for_naive_timestamps(monkeypatch):
    """Naive (SQLite) timestamps sort as UTC, so a DST change on the host can
    never put an older passed run ahead of a newer one."""
    import os
    import time
    from datetime import datetime

    from sessionfs.server.services.workflows import _Run, _run_sort_key

    monkeypatch.setenv("TZ", "America/New_York")
    if hasattr(time, "tzset"):
        time.tzset()
    try:
        old = _Run("old", "t", "implementer", "passed", "a" * 40, "build", "atlas", None,
                   order=(datetime(2026, 3, 8, 2, 30), "old"))
        new = _Run("new", "t", "implementer", "running", None, "build", "atlas", None,
                   order=(datetime(2026, 3, 8, 3, 0), "new"))
        ordered = sorted([old, new], key=_run_sort_key, reverse=True)
        assert [r.id for r in ordered] == ["new", "old"]
    finally:
        monkeypatch.delenv("TZ", raising=False)
        if hasattr(time, "tzset"):
            time.tzset()
        os.environ.pop("TZ", None)


@pytest.mark.asyncio
async def test_approval_gate_is_cached_per_plan_and_budgeted(
    db_session: AsyncSession, monkeypatch
):
    """tk_e12fd48d64e14976 #1: an approval gate checks each (policy, ticket)
    once per plan, and its checks count against the plan's gate budget (past
    it the gate fails closed without querying)."""
    from types import SimpleNamespace

    from sessionfs.server.services import approvals as approvals_svc
    from sessionfs.workflow_definition import StepDefinition

    calls: list[str] = []

    async def gate_passed(db, *, policy_id, ticket):
        calls.append(ticket.id)
        return True, "Approved"

    async def policy_row(db, policy_id):
        return SimpleNamespace(id=policy_id, org_id="org_1")

    async def policy_live(db, policy):
        return True

    monkeypatch.setattr(approvals_svc, "workflow_gate_passed", gate_passed)
    monkeypatch.setattr(wf, "_policy_row", policy_row)
    monkeypatch.setattr(wf, "_policy_live", policy_live)

    ctx = wf._GateContext(db_session, "proj_1")
    ctx._org_loaded, ctx._org_id = True, "org_1"
    children = [
        wf._Child(ticket=SimpleNamespace(id=f"tk_{i}"), index=i, item=None)
        for i in range(3)
    ]
    step = StepDefinition(
        id="approve", title="Approve", role="reviewer", agent="lead",
        gate="approval_policy:apol_1",
    )
    other = StepDefinition(
        id="approve-again", title="Approve again", role="reviewer", agent="lead",
        gate="approval_policy:apol_1",
    )
    assert await wf._gate(ctx, step, children) == (True, None)
    assert await wf._gate(ctx, other, children) == (True, None)
    assert calls == ["tk_0", "tk_1", "tk_2"]
    assert ctx.budget == wf.MAX_GATE_TICKETS - 3

    ctx.budget = 0
    fresh = [wf._Child(ticket=SimpleNamespace(id="tk_new"), index=0, item=None)]
    ok, why = await wf._gate(ctx, step, fresh)
    assert ok is False and "too many tickets" in (why or "")
    assert calls == ["tk_0", "tk_1", "tk_2"]


@pytest.mark.asyncio
async def test_untagged_round_waits_for_the_reviewer_steps_dependencies(
    client: AsyncClient, db_session: AsyncSession
):
    """tk_e12fd48d64e14976 #2: a round without step_id counts for the reviewer
    step pinned to the queue persona, so it is refused until that step's
    dependencies are done, like a tagged round."""
    e = await _env(db_session)
    t = await _template(client, e)
    started = (await _start(client, e, t["id"])).json()
    loop_id = started["loop_id"]
    impl = _ticket_ids(started, "implement")

    early = await _round(client, e, loop_id, impl[0])
    assert early.status_code == 422, early.text
    assert _code(early) == "workflow_step_mismatch"
    assert "'review'" in _msg(early) and "waiting for: implement" in _msg(early)

    await _work(client, e, _ticket_ids(started, "dispatch")[0])
    for tid in impl:
        await _work(client, e, tid)
    ok = await _round(client, e, loop_id, impl[0])
    assert ok.status_code == 201, ok.text
    plan, _ = await _plan(client, e, loop_id)
    assert impl[0] not in [c["ticket_id"] for c in plan["review"]["ready_children"]]
