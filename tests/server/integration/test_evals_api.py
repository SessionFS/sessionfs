"""Evaluations + lineage v1 (GitHub #121/#122): suites, cases, runs, trust,
acceptance, artifacts, lineage, promotion, compare and retention."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.eval_definition import case_content_sha256
from sessionfs.server.db.models import (
    AgentRun,
    EvalCaseResult,
    EvalRun,
    TicketEvent,
    TrustedReviewer,
)
from tests.server.integration.test_loops_api import (
    _add_org_member,
    _hdrs,
    _make_org,
    _make_project,
    _make_service_key,
    _make_user,
)

SHA = "b" * 40


class Env:
    pass


def _code(r) -> str | None:
    body = r.json()
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        # Eval/artifact routes put their code in error.code; the shared plan
        # gate (upgrade_required) keeps its code in error.details.error.
        details = err.get("details")
        if isinstance(details, dict) and details.get("error"):
            return details.get("error")
        return err.get("code")
    return None


async def _env(db: AsyncSession, *, org_tier: str = "team") -> Env:
    e = Env()
    e.owner, e.owner_key = await _make_user(db, "owner")
    e.grader_user, e.grader_user_key = await _make_user(db, "grader")
    e.member, e.member_key = await _make_user(db, "member")
    e.outsider, e.outsider_key = await _make_user(db, "outsider")
    e.org = await _make_org(db)
    if org_tier != "team":
        e.org.tier = org_tier
        await db.commit()
    await _add_org_member(db, e.org, e.owner, "admin")
    await _add_org_member(db, e.org, e.grader_user, "member")
    await _add_org_member(db, e.org, e.member, "member")
    e.project = await _make_project(db, e.owner, e.org)
    # The CI grader: a service key minted by the grader user (independent of
    # the owner who registers artifacts).
    e.grader_key_row, e.grader_key = await _make_service_key(
        db, e.org, e.grader_user, ["evals:read", "evals:write", "artifacts:read"]
    )
    # A producer key minted by the owner (registers artifacts from CI).
    e.producer_key_row, e.producer_key = await _make_service_key(
        db, e.org, e.owner, ["artifacts:read", "artifacts:write", "evals:read", "evals:write"]
    )
    db.add(
        TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:16]}",
            project_id=e.project.id,
            service_key_id=e.grader_key_row.id,
            reviewer_persona="clin-grader",
            is_active=True,
            created_by_user_id=e.owner.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    db.add(
        TrustedReviewer(
            id=f"tr_{uuid.uuid4().hex[:16]}",
            project_id=e.project.id,
            user_id=e.grader_user.id,
            reviewer_persona="clin-grader",
            is_active=True,
            created_by_user_id=e.owner.id,
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return e


def _pp(e: Env, tail: str) -> str:
    return f"/api/v1/projects/{e.project.id}/{tail}"


def _suite_body(**over) -> dict:
    body = {
        "slug": "clinical-notes",
        "name": "Clinical notes",
        "grader": {"kind": "llm_judge", "persona": "clin-grader", "rubric": "Grade the summary."},
        "acceptance": {"min_pass_rate": 0.5, "max_failures_by_tag": {"critical": 0}},
        "cases": [
            {"case_key": "c1", "input": "note one", "expected": "sum one", "tags": ["critical"]},
            {"case_key": "c2", "input": "note two", "expected": "sum two"},
            {"case_key": "c3", "input": "note three", "expected": "sum three"},
        ],
    }
    body.update(over)
    return body


async def _suite(client: AsyncClient, e: Env, **over) -> dict:
    r = await client.post(_pp(e, "eval-suites"), headers=_hdrs(e.owner_key), json=_suite_body(**over))
    assert r.status_code == 201, r.text
    return r.json()


async def _artifact(client: AsyncClient, e: Env, key=None, **over) -> dict:
    body = {"kind": "model", "name": "clin", "version": "2.3", "external_uri": "hf://acme/clin-2.3"}
    body.update(over)
    r = await client.post(_pp(e, "artifacts"), headers=_hdrs(key or e.producer_key), json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _start(client, e, suite="clinical-notes", key=None, **over):
    body = {"suite": suite, "trigger_source": "ci", **over}
    return await client.post(_pp(e, "eval-runs"), headers=_hdrs(key or e.grader_key), json=body)


async def _grade(client, e, run_id, results, key=None):
    return await client.post(
        _pp(e, f"eval-runs/{run_id}/results"),
        headers=_hdrs(key or e.grader_key),
        json={"results": results},
    )


async def _finish(client, e, run_id, key=None, **body):
    return await client.post(
        _pp(e, f"eval-runs/{run_id}/finish"), headers=_hdrs(key or e.grader_key), json=body
    )


async def _passing_run(client, e, artifact_id, suite="clinical-notes") -> dict:
    r = await _start(client, e, suite=suite, artifact_id=artifact_id)
    assert r.status_code == 201, r.text
    run_id = r.json()["run"]["id"]
    g = await _grade(
        client,
        e,
        run_id,
        [{"case_key": c["case_key"], "verdict": "pass", "score": 1} for c in r.json()["cases"]],
    )
    assert g.status_code == 200, g.text
    f = await _finish(client, e, run_id)
    assert f.status_code == 200, f.text
    return f.json()


# ── Suites ──


@pytest.mark.asyncio
async def test_suite_versioning_and_case_liveness(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    v1 = await _suite(client, e)
    assert v1["version"] == 1 and v1["live_cases"] == 3 and v1["active"]

    added = await client.post(
        _pp(e, "eval-suites/clinical-notes/cases"),
        headers=_hdrs(e.owner_key),
        json={"cases": [{"case_key": "c4", "input": "n4", "expected": "s4"}]},
    )
    assert added.status_code == 201, added.text
    v2 = added.json()
    assert v2["version"] == 2 and v2["live_cases"] == 4 and v2["lineage_id"] == v1["lineage_id"]

    retired = await client.post(
        _pp(e, f"eval-suites/{v1['lineage_id']}/cases/retire"),
        headers=_hdrs(e.owner_key),
        json={"case_keys": ["c2"]},
    )
    assert retired.status_code == 200, retired.text
    assert retired.json()["version"] == 3 and retired.json()["live_cases"] == 3

    # Old versions keep their own case set.
    old = await client.get(_pp(e, f"eval-suites/{v1['id']}/cases"), headers=_hdrs(e.member_key))
    assert [c["case_key"] for c in old.json()["cases"]] == ["c1", "c2", "c3"]
    assert old.json()["cases"][0]["input"] == "note one"
    assert old.json()["cases"][0]["content_sha256"] == case_content_sha256("note one", "sum one")
    cur = await client.get(_pp(e, "eval-suites/clinical-notes/cases"), headers=_hdrs(e.member_key))
    assert [c["case_key"] for c in cur.json()["cases"]] == ["c1", "c3", "c4"]

    # A retired key is never reused.
    reuse = await client.post(
        _pp(e, "eval-suites/clinical-notes/cases"),
        headers=_hdrs(e.owner_key),
        json={"cases": [{"case_key": "c2", "input": "x"}]},
    )
    assert reuse.status_code == 409 and _code(reuse) == "case_exists"

    # Edit: new version, slug immutable, the old id is stale.
    body = _suite_body(name="Clinical notes v2")
    body.pop("cases")
    up = await client.put(_pp(e, f"eval-suites/{retired.json()['id']}"), headers=_hdrs(e.owner_key), json=body)
    assert up.status_code == 200, up.text
    assert up.json()["version"] == 4 and up.json()["live_cases"] == 3
    stale = await client.put(_pp(e, f"eval-suites/{v1['id']}"), headers=_hdrs(e.owner_key), json=body)
    assert stale.status_code == 409 and _code(stale) == "suite_superseded"
    body["slug"] = "renamed"
    bad = await client.put(_pp(e, f"eval-suites/{up.json()['id']}"), headers=_hdrs(e.owner_key), json=body)
    assert bad.status_code == 422 and _code(bad) == "slug_immutable"

    listed = await client.get(_pp(e, "eval-suites"), headers=_hdrs(e.grader_key))
    assert [s["id"] for s in listed.json()["suites"]] == [up.json()["id"]]
    got = await client.get(_pp(e, "eval-suites/clinical-notes"), headers=_hdrs(e.member_key))
    assert [v["version"] for v in got.json()["versions"]] == [4, 3, 2, 1]

    d = await client.delete(_pp(e, f"eval-suites/{up.json()['id']}"), headers=_hdrs(e.owner_key))
    assert d.status_code == 204
    assert (await client.get(_pp(e, "eval-suites"), headers=_hdrs(e.owner_key))).json()["suites"] == []


@pytest.mark.asyncio
async def test_suite_writes_are_people_only_admin_and_plan_gated(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    svc = await client.post(_pp(e, "eval-suites"), headers=_hdrs(e.producer_key), json=_suite_body())
    assert svc.status_code == 403 and _code(svc) == "service_key_not_allowed"
    member = await client.post(_pp(e, "eval-suites"), headers=_hdrs(e.member_key), json=_suite_body())
    assert member.status_code == 403 and _code(member) == "project_admin_required"
    outsider = await client.post(_pp(e, "eval-suites"), headers=_hdrs(e.outsider_key), json=_suite_body())
    assert outsider.status_code == 404
    unknown = await client.get("/api/v1/projects/proj_nope/eval-suites", headers=_hdrs(e.owner_key))
    assert unknown.status_code == 404 and unknown.text == outsider.text
    bad = await client.post(
        _pp(e, "eval-suites"),
        headers=_hdrs(e.owner_key),
        json=_suite_body(acceptance={"min_pass_rate": True}),
    )
    assert bad.status_code == 422 and _code(bad) == "invalid_eval_suite"
    extra = await client.post(
        _pp(e, "eval-suites"), headers=_hdrs(e.owner_key), json=_suite_body(acceptance={"vibes": 1})
    )
    assert extra.status_code == 422

    free = await _env(db_session, org_tier="free")
    gated = await client.post(_pp(free, "eval-suites"), headers=_hdrs(free.owner_key), json=_suite_body())
    assert gated.status_code == 403 and _code(gated) == "upgrade_required"
    # Reads stay open on any plan.
    assert (await client.get(_pp(free, "eval-suites"), headers=_hdrs(free.owner_key))).status_code == 200


@pytest.mark.asyncio
async def test_cross_org_service_key_gets_opaque_404(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    other = await _env(db_session)
    await _suite(client, e)
    r = await client.get(_pp(e, "eval-suites"), headers=_hdrs(other.grader_key))
    assert r.status_code == 404
    r2 = await client.get(_pp(e, "artifacts"), headers=_hdrs(other.producer_key))
    assert r2.status_code == 404


# ── Runs, trust and acceptance ──


@pytest.mark.asyncio
async def test_trusted_run_lifecycle(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    await _suite(client, e)
    art = await _artifact(client, e)
    started = await _start(client, e, artifact_id=art["id"], commit_sha=SHA)
    assert started.status_code == 201, started.text
    body = started.json()
    run_id = body["run"]["id"]
    assert body["trust_preview"] == {"trusted": True, "reason": None}
    assert [c["case_key"] for c in body["cases"]] == ["c1", "c2", "c3"]
    assert body["cases"][0]["input"] == "note one"
    assert "untrusted_content_notice" in body

    # A different credential cannot post into this run.
    other = await _grade(client, e, run_id, [{"case_key": "c1", "verdict": "pass"}], key=e.producer_key)
    assert other.status_code == 403 and _code(other) == "grader_credential_mismatch"

    batch = [
        {"case_key": "c1", "verdict": "pass", "score": 0.9, "output": "sum one", "notes": "ok"},
        {"case_key": "c2", "verdict": "fail", "score": 0.2},
    ]
    r = await _grade(client, e, run_id, batch)
    assert r.status_code == 200 and r.json()["recorded"] == 2
    replay = await _grade(client, e, run_id, batch)
    assert replay.json() == {"run_id": run_id, "recorded": 0, "replayed": 2}
    conflict = await _grade(client, e, run_id, [{"case_key": "c2", "verdict": "pass"}])
    assert conflict.status_code == 409 and _code(conflict) == "result_conflict"
    not_live = await _grade(client, e, run_id, [{"case_key": "zz", "verdict": "pass"}])
    assert not_live.status_code == 422 and _code(not_live) == "case_not_live"
    wrong_hash = await _grade(
        client, e, run_id, [{"case_key": "c3", "verdict": "pass", "content_sha256": "0" * 64}]
    )
    assert wrong_hash.status_code == 422 and _code(wrong_hash) == "case_hash_mismatch"

    # Missing c3: incomplete (require_all_cases defaults to true).
    early = await client.get(_pp(e, f"eval-runs/{run_id}"), headers=_hdrs(e.member_key))
    assert early.json()["state"] == "running"
    ok = await _grade(client, e, run_id, [{"case_key": "c3", "verdict": "pass", "score": 1}])
    assert ok.status_code == 200
    fin = await _finish(client, e, run_id, usage={"input_tokens": 120, "output_tokens": 30})
    assert fin.status_code == 200, fin.text
    run = fin.json()
    assert run["state"] == "finished" and run["verdict"] == "passed" and run["trusted"] is True
    assert run["summary"]["passed"] == 2 and run["summary"]["failed"] == 1
    assert run["summary"]["by_tag"]["critical"]["passed"] == 1
    again = await _finish(client, e, run_id)
    assert again.status_code == 200 and again.json()["finished_at"] == run["finished_at"]
    late = await _grade(client, e, run_id, [{"case_key": "c1", "verdict": "pass"}])
    assert late.status_code == 409 and _code(late) == "run_not_running"

    agent_run = await db_session.get(AgentRun, run["agent_run_id"])
    await db_session.refresh(agent_run)
    assert agent_run.role == "grader" and agent_run.status == "passed"
    assert agent_run.eval_run_id == run_id and agent_run.input_tokens == 120

    results = await client.get(_pp(e, f"eval-runs/{run_id}/results?tag=critical"), headers=_hdrs(e.member_key))
    assert [x["case_key"] for x in results.json()["results"]] == ["c1"]
    assert results.json()["results"][0]["output"] == "sum one"


@pytest.mark.asyncio
async def test_untrusted_when_grader_not_independent_or_unregistered(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    await _suite(client, e)
    # The grader user registers the artifact, then grades it with a key they minted.
    own = await _artifact(client, e, key=e.grader_user_key, version="9.9")
    r = await _start(client, e, artifact_id=own["id"])
    assert r.json()["trust_preview"]["reason"] == "grader_not_independent"
    run_id = r.json()["run"]["id"]
    await _grade(client, e, run_id, [{"case_key": k, "verdict": "pass"} for k in ("c1", "c2", "c3")])
    fin = (await _finish(client, e, run_id)).json()
    assert fin["verdict"] == "passed" and fin["trusted"] is False
    assert fin["untrusted_reason"] == "grader_not_independent" and fin["trust_label"] == "unverified"

    # A credential that is not a registered grader is never trusted.
    unreg = await _start(client, e, artifact_id=own["id"], key=e.producer_key)
    assert unreg.json()["trust_preview"]["reason"] == "grader_not_registered"

    # Revoked mid-run: trust is re-checked at finish.
    art = await _artifact(client, e, version="3.0")
    mid = await _start(client, e, artifact_id=art["id"])
    assert mid.json()["trust_preview"]["trusted"] is True
    await db_session.execute(
        update(TrustedReviewer)
        .where(TrustedReviewer.service_key_id == e.grader_key_row.id)
        .values(is_active=False)
    )
    await db_session.commit()
    mid_id = mid.json()["run"]["id"]
    await _grade(client, e, mid_id, [{"case_key": k, "verdict": "pass"} for k in ("c1", "c2", "c3")])
    assert (await _finish(client, e, mid_id)).json()["untrusted_reason"] == "grader_not_registered"


@pytest.mark.asyncio
async def test_human_grader_needs_user_key(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    await _suite(client, e, grader={"kind": "human", "persona": "clin-grader"})
    art = await _artifact(client, e)
    svc = await _start(client, e, artifact_id=art["id"])
    assert svc.json()["trust_preview"]["reason"] == "human_grader_needs_user_key"
    person = await _start(client, e, artifact_id=art["id"], key=e.grader_user_key)
    assert person.json()["trust_preview"] == {"trusted": True, "reason": None}


@pytest.mark.asyncio
async def test_acceptance_thresholds_and_production_regressions(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    await _suite(
        client,
        e,
        acceptance={
            "min_pass_rate": 0.6,
            "no_regression_vs": "production",
            "regression_tags": ["critical"],
        },
    )
    prod = await _artifact(client, e, version="1.0")
    await _passing_run(client, e, prod["id"])
    p = await client.post(
        _pp(e, f"artifacts/{prod['id']}/promote"), headers=_hdrs(e.owner_key), json={"to_stage": "production"}
    )
    assert p.status_code == 200, p.text

    cand = await _artifact(client, e, version="2.0")
    r = await _start(client, e, artifact_id=cand["id"])
    run_id = r.json()["run"]["id"]
    await _grade(
        client,
        e,
        run_id,
        [
            {"case_key": "c1", "verdict": "fail"},
            {"case_key": "c2", "verdict": "pass"},
            {"case_key": "c3", "verdict": "pass"},
        ],
    )
    fin = (await _finish(client, e, run_id)).json()
    assert fin["verdict"] == "failed"
    assert fin["summary"]["regressions"] == ["c1"]
    assert fin["baseline_run_id"] is not None

    # Not all cases graded: incomplete.
    r2 = await _start(client, e, artifact_id=cand["id"])
    await _grade(client, e, r2.json()["run"]["id"], [{"case_key": "c2", "verdict": "pass"}])
    assert (await _finish(client, e, r2.json()["run"]["id"])).json()["verdict"] == "incomplete"


@pytest.mark.asyncio
async def test_reference_storage_hash_only(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    h1 = case_content_sha256("patient text", "expected")
    with_text = await client.post(
        _pp(e, "eval-suites"),
        headers=_hdrs(e.owner_key),
        json=_suite_body(slug="phi", case_storage="reference", cases=[{"case_key": "p1", "input": "x"}]),
    )
    assert with_text.status_code == 422 and _code(with_text) == "case_text_not_allowed"
    no_hash = await client.post(
        _pp(e, "eval-suites"),
        headers=_hdrs(e.owner_key),
        json=_suite_body(slug="phi", case_storage="reference", cases=[{"case_key": "p1"}]),
    )
    assert no_hash.status_code == 422 and _code(no_hash) == "content_sha256_required"
    ok = await client.post(
        _pp(e, "eval-suites"),
        headers=_hdrs(e.owner_key),
        json=_suite_body(
            slug="phi",
            case_storage="reference",
            acceptance={},
            cases=[{"case_key": "p1", "content_sha256": h1, "tags": ["critical"]}],
        ),
    )
    assert ok.status_code == 201, ok.text
    art = await _artifact(client, e)
    started = await _start(client, e, suite="phi", artifact_id=art["id"])
    case = started.json()["cases"][0]
    assert "input" not in case and case["content_sha256"] == h1
    run_id = started.json()["run"]["id"]
    missing = await _grade(client, e, run_id, [{"case_key": "p1", "verdict": "pass"}])
    assert missing.status_code == 422 and _code(missing) == "content_sha256_required"
    out = await _grade(client, e, run_id, [{"case_key": "p1", "verdict": "pass", "content_sha256": h1, "output": "leak"}])
    assert out.status_code == 422 and _code(out) == "output_not_allowed"
    good = await _grade(
        client, e, run_id, [{"case_key": "p1", "verdict": "pass", "content_sha256": h1, "output_sha256": "a" * 64}]
    )
    assert good.status_code == 200, good.text
    assert (await _finish(client, e, run_id)).json()["verdict"] == "passed"


@pytest.mark.asyncio
async def test_void_expire_and_ticket_event(client: AsyncClient, db_session: AsyncSession):
    from sessionfs.server.db.models import Ticket

    e = await _env(db_session)
    await _suite(client, e)
    art = await _artifact(client, e)
    ticket = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=e.project.id,
        title="Ship clin 2.3",
        created_by_user_id=e.owner.id,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    db_session.add(ticket)
    await db_session.commit()
    r = await _start(client, e, artifact_id=art["id"], ticket_id=ticket.id)
    run_id = r.json()["run"]["id"]
    await _grade(client, e, run_id, [{"case_key": k, "verdict": "pass"} for k in ("c1", "c2", "c3")])
    await _finish(client, e, run_id)
    events = (
        await db_session.execute(select(TicketEvent).where(TicketEvent.ticket_id == ticket.id))
    ).scalars().all()
    assert [ev.action for ev in events] == ["eval_run_finished"]

    svc_void = await client.post(_pp(e, f"eval-runs/{run_id}/void"), headers=_hdrs(e.grader_key), json={"reason": "x"})
    assert svc_void.status_code == 403
    member_void = await client.post(_pp(e, f"eval-runs/{run_id}/void"), headers=_hdrs(e.member_key), json={"reason": "x"})
    assert member_void.status_code == 403
    v = await client.post(_pp(e, f"eval-runs/{run_id}/void"), headers=_hdrs(e.owner_key), json={"reason": "bad grader build"})
    assert v.status_code == 200 and v.json()["state"] == "voided"

    # A voided run never satisfies the gate.
    await client.put(
        _pp(e, "artifact-families/model/clin/requirements"),
        headers=_hdrs(e.owner_key),
        json={"suites": ["clinical-notes"]},
    )
    check = await client.get(
        _pp(e, f"artifacts/{art['id']}/promotion-check?to_stage=staging"), headers=_hdrs(e.member_key)
    )
    assert check.json()["allowed"] is False
    assert check.json()["blockers"][0]["code"] == "suite_not_passed"

    stale = await _start(client, e, artifact_id=art["id"])
    stale_id = stale.json()["run"]["id"]
    await db_session.execute(
        update(EvalRun)
        .where(EvalRun.id == stale_id)
        .values(started_at=datetime.now(timezone.utc) - timedelta(days=8))
    )
    await db_session.commit()
    got = await client.get(_pp(e, f"eval-runs/{stale_id}"), headers=_hdrs(e.member_key))
    assert got.json()["state"] == "expired"
    late = await _grade(client, e, stale_id, [{"case_key": "c1", "verdict": "pass"}])
    assert late.status_code == 409


@pytest.mark.asyncio
async def test_compare_runs(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    await _suite(client, e)
    art = await _artifact(client, e)
    a = await _passing_run(client, e, art["id"])
    r = await _start(client, e, artifact_id=art["id"])
    b_id = r.json()["run"]["id"]
    await _grade(
        client,
        e,
        b_id,
        [
            {"case_key": "c1", "verdict": "fail", "score": 0.1},
            {"case_key": "c2", "verdict": "pass", "score": 1},
            {"case_key": "c3", "verdict": "pass", "score": 1},
        ],
    )
    await _finish(client, e, b_id)
    cmp = await client.get(_pp(e, f"eval-runs/compare?a={a['id']}&b={b_id}"), headers=_hdrs(e.owner_key))
    assert cmp.status_code == 200, cmp.text
    body = cmp.json()
    assert body["regressions"] == ["c1"] and body["fixes"] == []
    assert body["unchanged"]["pass"] == 2
    assert body["a"]["tokens"] is not None
    # Org admin sees dollars; a service key never does.
    assert "cost_usd" in body["a"]
    svc = await client.get(_pp(e, f"eval-runs/compare?a={a['id']}&b={b_id}"), headers=_hdrs(e.grader_key))
    assert "cost_usd" not in svc.json()["a"]


# ── Artifacts and lineage ──


@pytest.mark.asyncio
async def test_artifact_uri_validation(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    for uri, code in (
        ("https://bucket.s3.amazonaws.com/m?X-Amz-Signature=abc", "uri_has_credentials"),
        ("https://acct.blob.core.windows.net/c/m?sv=2020&sig=abc", "uri_has_credentials"),
        ("https://user:pw@example.com/m", "uri_has_credentials"),
        ("ftp://example.com/m", "invalid_uri_scheme"),
        ("file:///etc/passwd", "invalid_uri_scheme"),
        ("s3://", "invalid_uri"),
    ):
        r = await client.post(
            _pp(e, "artifacts"),
            headers=_hdrs(e.producer_key),
            json={"kind": "model", "name": "m", "version": "1", "external_uri": uri},
        )
        assert r.status_code == 422 and _code(r) == code, (uri, r.text)
    ok = await _artifact(client, e, external_uri="HF://acme/clin", tags={"phi_compliant": "v3"})
    assert ok["external_uri"] == "hf://acme/clin"
    assert ok["link_url"] == "https://huggingface.co/acme/clin"
    s3 = await _artifact(client, e, version="2.4", external_uri="s3://models/clin/2.4")
    assert s3["link_url"] is None
    dup = await client.post(
        _pp(e, "artifacts"),
        headers=_hdrs(e.producer_key),
        json={"kind": "model", "name": "clin", "version": "2.4", "external_uri": "s3://x/y"},
    )
    assert dup.status_code == 409 and _code(dup) == "artifact_version_exists"
    reader = await client.post(
        _pp(e, "artifacts"),
        headers=_hdrs(e.outsider_key),
        json={"kind": "model", "name": "x", "version": "1", "external_uri": "s3://x/y"},
    )
    assert reader.status_code == 404


@pytest.mark.asyncio
async def test_lineage_cycles_trace_and_promotion(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    await _suite(client, e)
    data = await _artifact(client, e, kind="dataset", name="notes", version="v3", external_uri="s3://d/notes", tags={"phi_compliant": "v3"})
    base = await _artifact(client, e, name="base", version="1", external_uri="hf://acme/base")
    model = await _artifact(client, e)

    async def link(parent, child, relation):
        return await client.post(
            _pp(e, "artifact-edges"),
            headers=_hdrs(e.producer_key),
            json={"parent_id": parent, "child_id": child, "relation": relation},
        )

    assert (await link(data["id"], model["id"], "trained_on")).status_code == 201
    assert (await link(base["id"], model["id"], "fine_tuned_from")).status_code == 201
    cyc = await link(model["id"], base["id"], "derived_from")
    assert cyc.status_code == 422 and _code(cyc) == "lineage_cycle"
    self_edge = await link(model["id"], model["id"], "uses")
    assert self_edge.status_code == 422
    dup = await link(data["id"], model["id"], "trained_on")
    assert dup.status_code == 409

    up = await client.get(_pp(e, f"artifacts/{model['id']}/lineage?direction=up"), headers=_hdrs(e.member_key))
    assert up.status_code == 200
    assert {n["id"] for n in up.json()["nodes"]} == {model["id"], data["id"], base["id"]}
    assert len(up.json()["edges"]) == 2 and up.json()["truncated"] is False
    down = await client.get(_pp(e, f"artifacts/{data['id']}/lineage?direction=down"), headers=_hdrs(e.member_key))
    assert {n["id"] for n in down.json()["nodes"]} == {data["id"], model["id"]}

    # Requirements: people only; then the gate.
    svc_req = await client.put(
        _pp(e, "artifact-families/model/clin/requirements"),
        headers=_hdrs(e.producer_key),
        json={"suites": ["clinical-notes"]},
    )
    assert svc_req.status_code == 403
    req = await client.put(
        _pp(e, "artifact-families/model/clin/requirements"),
        headers=_hdrs(e.owner_key),
        json={"suites": ["clinical-notes"], "require_tags": {"phi_compliant": "v3"}},
    )
    assert req.status_code == 200 and len(req.json()["suites"]) == 1
    blocked = await client.post(
        _pp(e, f"artifacts/{model['id']}/promote"), headers=_hdrs(e.owner_key), json={"to_stage": "staging"}
    )
    assert blocked.status_code == 409 and _code(blocked) == "promotion_blocked"
    svc_promote = await client.post(
        _pp(e, f"artifacts/{model['id']}/promote"), headers=_hdrs(e.producer_key), json={"to_stage": "staging"}
    )
    assert svc_promote.status_code == 403

    await _passing_run(client, e, model["id"])
    staged = await client.post(
        _pp(e, f"artifacts/{model['id']}/promote"), headers=_hdrs(e.owner_key), json={"to_stage": "staging"}
    )
    assert staged.status_code == 200, staged.text
    assert staged.json()["stage"] == "staging"

    # A second production version archives the first.
    prod1 = await client.post(
        _pp(e, f"artifacts/{model['id']}/promote"), headers=_hdrs(e.owner_key), json={"to_stage": "production"}
    )
    assert prod1.status_code == 200
    m2 = await _artifact(client, e, version="2.4")
    await link(data["id"], m2["id"], "trained_on")
    await _passing_run(client, e, m2["id"])
    prod2 = await client.post(
        _pp(e, f"artifacts/{m2['id']}/promote"), headers=_hdrs(e.owner_key), json={"to_stage": "production"}
    )
    assert prod2.status_code == 200 and prod2.json()["archived_ids"] == [model["id"]]

    matrix = await client.get(_pp(e, "artifact-families/model/clin/evals"), headers=_hdrs(e.member_key))
    assert matrix.status_code == 200
    assert [r["version"] for r in matrix.json()["rows"]] == ["2.4", "2.3"]
    col = matrix.json()["columns"][0]
    assert col["required"] is True and col["slug"] == "clinical-notes"
    assert matrix.json()["rows"][0]["cells"][col["suite_lineage_id"]]["verdict"] == "passed"

    fams = await client.get(_pp(e, "artifact-families"), headers=_hdrs(e.member_key))
    clin = [f for f in fams.json()["families"] if f["name"] == "clin"][0]
    assert clin["versions"] == 2 and clin["production_version"] == "2.4"

    # In use: hard delete refused.
    d = await client.delete(_pp(e, f"artifacts/{data['id']}"), headers=_hdrs(e.owner_key))
    assert d.status_code == 409 and _code(d) == "artifact_in_use"


@pytest.mark.asyncio
async def test_eval_output_retention_sweep(client: AsyncClient, db_session: AsyncSession):
    from tests.server.integration.test_skill_marketplace import _platform_admin_headers

    e = await _env(db_session)
    await _suite(client, e)
    art = await _artifact(client, e)
    run = await _passing_run(client, e, art["id"])
    rows = (
        await db_session.execute(select(EvalCaseResult).where(EvalCaseResult.run_id == run["id"]))
    ).scalars().all()
    await db_session.execute(
        update(EvalCaseResult)
        .where(EvalCaseResult.id == rows[0].id)
        .values(
            created_at=datetime.now(timezone.utc) - timedelta(days=181),
            output="old output",
            notes="old notes",
            output_sha256="c" * 64,
        )
    )
    await db_session.commit()
    _, admin_headers = await _platform_admin_headers(db_session)
    denied = await client.post("/api/v1/admin/sweeps/eval-outputs", headers=_hdrs(e.owner_key), json={})
    assert denied.status_code == 403
    r = await client.post("/api/v1/admin/sweeps/eval-outputs", headers=admin_headers, json={})
    assert r.status_code == 200 and r.json()["results_cleared"] == 1
    row = await db_session.get(EvalCaseResult, rows[0].id)
    await db_session.refresh(row)
    assert row.output is None and row.notes is None and row.output_cleared_at is not None
    assert row.verdict == "pass" and row.output_sha256 == "c" * 64
