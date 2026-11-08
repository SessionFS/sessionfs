"""Evaluations + lineage review fixes (tk_65c08a5293f84b00): fail-closed
trust for producer-less subjects, merge refusal while eval/artifact rows
exist, promotion/void serialization under the project scope lock, the plan
re-check under the lock on run writes, plan gates on every artifact write,
DLP on family requirements, and the bounded latest-trusted-run query."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    Artifact,
    ArtifactFamilyRequirement,
    EvalRun,
    Organization,
    Project,
)
from sessionfs.server.routes import artifacts as artifacts_routes
from sessionfs.server.routes import evals as evals_routes
from sessionfs.server.services import artifacts as artifacts_svc
from tests.server.integration.test_evals_api import (
    SHA,
    _artifact,
    _code,
    _env,
    _finish,
    _grade,
    _hdrs,
    _passing_run,
    _pp,
    _start,
    _suite,
)
from tests.server.integration.test_loops_api import _make_project

ALL_PASS = [{"case_key": k, "verdict": "pass", "score": 1} for k in ("c1", "c2", "c3")]


def _detail_code(r) -> str | None:
    body = r.json()
    err = body.get("error") if isinstance(body, dict) else None
    if isinstance(err, dict):
        return err.get("code")
    return None


async def _downgrade(db: AsyncSession, org_id: str) -> None:
    await db.execute(update(Organization).where(Organization.id == org_id).values(tier="free"))
    await db.commit()


# ── 1. No producer identity ⇒ untrusted ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "subject",
    [{"commit_sha": SHA}, {"subject_label": "nightly prompt build"}],
    ids=["commit_only", "free_text"],
)
async def test_subject_without_producer_is_never_trusted(
    client: AsyncClient, db_session: AsyncSession, subject: dict
):
    e = await _env(db_session)
    await _suite(client, e)
    r = await _start(client, e, **subject)
    assert r.status_code == 201, r.text
    assert r.json()["trust_preview"] == {"trusted": False, "reason": "producer_identity_unknown"}
    run_id = r.json()["run"]["id"]
    assert (await _grade(client, e, run_id, ALL_PASS)).status_code == 200
    fin = (await _finish(client, e, run_id)).json()
    assert fin["verdict"] == "passed"
    assert fin["trusted"] is False and fin["untrusted_reason"] == "producer_identity_unknown"
    assert fin["trust_label"] == "unverified"


# ── 2. Merge refused while either project holds eval / artifact rows ──


async def _merge(client, e, target_id, source_id, *, dry_run):
    return await client.post(
        f"/api/v1/projects/{target_id}/merge",
        headers=_hdrs(e.owner_key),
        json={"source_project_id": source_id, "dry_run": dry_run},
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("holder", ["source", "target"])
async def test_merge_refused_while_a_project_holds_evals(
    client: AsyncClient, db_session: AsyncSession, holder: str
):
    e = await _env(db_session)
    other = await _make_project(db_session, e.owner, e.org)
    source, target = (e.project, other) if holder == "source" else (other, e.project)
    await _artifact(client, e)  # lives in e.project

    plan = await _merge(client, e, target.id, source.id, dry_run=True)
    assert plan.status_code == 200, plan.text
    assert plan.json()["would_refuse"] is True
    assert plan.json()["blocking_reason"] == "merge_blocked_by_evals"

    done = await _merge(client, e, target.id, source.id, dry_run=False)
    assert done.status_code == 409, done.text
    assert _detail_code(done) == "merge_blocked_by_evals", done.text
    for pid in (source.id, target.id):
        row = (
            await db_session.execute(
                select(Project).where(Project.id == pid).execution_options(populate_existing=True)
            )
        ).scalar_one()
        assert row.merged_into_project_id is None
    arts = (
        await db_session.execute(
            select(Artifact.project_id).execution_options(populate_existing=True)
        )
    ).scalars().all()
    assert arts == [e.project.id]


@pytest.mark.asyncio
async def test_merge_without_evals_still_works(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    other = await _make_project(db_session, e.owner, e.org)
    plan = await _merge(client, e, e.project.id, other.id, dry_run=True)
    assert plan.status_code == 200 and not plan.json()["would_refuse"]
    done = await _merge(client, e, e.project.id, other.id, dry_run=False)
    assert done.status_code == 200, done.text


# ── 3. Promotion and void serialize under the project scope lock ──


def _spy_lock(monkeypatch, module, order: list[str], after=None):
    real = module.lock_project_scope_authorized

    async def spy(db, project, **kw):
        order.append("project_lock")
        await real(db, project, **kw)
        if after is not None:
            await after(db)

    monkeypatch.setattr(module, "lock_project_scope_authorized", spy)


@pytest.mark.asyncio
async def test_void_takes_project_lock_before_run_row_lock(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    e = await _env(db_session)
    await _suite(client, e)
    art = await _artifact(client, e)
    run = await _passing_run(client, e, art["id"])

    order: list[str] = []
    _spy_lock(monkeypatch, evals_routes, order)
    real_row = evals_routes._run_or_404

    async def row_spy(db, project, run_id, *, lock=False):
        if lock:
            order.append("row_lock")
        return await real_row(db, project, run_id, lock=lock)

    monkeypatch.setattr(evals_routes, "_run_or_404", row_spy)
    v = await client.post(
        _pp(e, f"eval-runs/{run['id']}/void"), headers=_hdrs(e.owner_key), json={"reason": "bad data"}
    )
    assert v.status_code == 200, v.text
    assert order[:2] == ["project_lock", "row_lock"]


@pytest.mark.asyncio
async def test_promotion_rechecks_the_gate_under_the_lock(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """A void that commits while promotion waits for the project lock is
    seen by the gate evaluated under the lock."""
    e = await _env(db_session)
    await _suite(client, e)
    art = await _artifact(client, e)
    run = await _passing_run(client, e, art["id"])
    req = await client.put(
        _pp(e, "artifact-families/model/clin/requirements"),
        headers=_hdrs(e.owner_key),
        json={"suites": ["clinical-notes"]},
    )
    assert req.status_code == 200, req.text

    async def void_meanwhile(db):
        await db.execute(
            update(EvalRun).where(EvalRun.id == run["id"]).values(state="voided", voided_reason="x")
        )

    order: list[str] = []
    _spy_lock(monkeypatch, evals_routes, order, after=void_meanwhile)
    p = await client.post(
        _pp(e, f"artifacts/{art['id']}/promote"), headers=_hdrs(e.owner_key), json={"to_stage": "staging"}
    )
    assert p.status_code == 409, p.text
    assert _code(p) == "promotion_blocked"
    assert order == ["project_lock"]


# ── 4. Run start / results / finish re-check the plan under the lock ──


@pytest.mark.asyncio
async def test_run_writes_recheck_plan_under_project_lock(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    e = await _env(db_session)
    await _suite(client, e)
    art = await _artifact(client, e)
    started = await _start(client, e, artifact_id=art["id"])
    run_id = started.json()["run"]["id"]

    real = evals_routes.lock_project_scope_authorized
    seen: list[dict] = []

    async def spy(db, project, **kw):
        seen.append(kw)
        # A downgrade lands while the request waits for the lock.
        await db.execute(update(Organization).where(Organization.id == e.org.id).values(tier="free"))
        await real(db, project, **kw)

    monkeypatch.setattr(evals_routes, "lock_project_scope_authorized", spy)
    for r in (
        await _start(client, e, artifact_id=art["id"]),
        await _grade(client, e, run_id, ALL_PASS),
        await _finish(client, e, run_id),
    ):
        assert r.status_code == 403, r.text
        assert _code(r) == "upgrade_required"
    assert len(seen) == 3 and all(kw.get("feature") == "evaluations" for kw in seen)
    row = await db_session.get(EvalRun, run_id)
    await db_session.refresh(row)
    assert row.state == "running"


# ── 5. Deprecate / delete / edge removal are plan-gated ──


@pytest.mark.asyncio
async def test_artifact_writes_gated_after_downgrade(client: AsyncClient, db_session: AsyncSession):
    e = await _env(db_session)
    await _suite(client, e)
    parent = await _artifact(client, e, kind="dataset", name="notes", version="1")
    child = await _artifact(client, e)
    edge = await client.post(
        _pp(e, "artifact-edges"),
        headers=_hdrs(e.owner_key),
        json={"parent_id": parent["id"], "child_id": child["id"], "relation": "trained_on"},
    )
    assert edge.status_code == 201, edge.text
    lone = await _artifact(client, e, version="7.0")
    started = await _start(client, e, artifact_id=child["id"])
    await _downgrade(db_session, e.org.id)

    dep = await client.post(_pp(e, f"artifacts/{child['id']}/deprecate"), headers=_hdrs(e.owner_key))
    assert dep.status_code == 403 and _code(dep) == "upgrade_required", dep.text
    rm = await client.delete(_pp(e, f"artifacts/{lone['id']}"), headers=_hdrs(e.owner_key))
    assert rm.status_code == 403 and _code(rm) == "upgrade_required", rm.text
    unlink = await client.delete(_pp(e, f"artifact-edges/{edge.json()['id']}"), headers=_hdrs(e.owner_key))
    assert unlink.status_code == 403 and _code(unlink) == "upgrade_required", unlink.text

    # Reads, suite deactivation and void stay open.
    assert (await client.get(_pp(e, f"artifacts/{child['id']}"), headers=_hdrs(e.member_key))).status_code == 200
    void = await client.post(
        _pp(e, f"eval-runs/{started.json()['run']['id']}/void"),
        headers=_hdrs(e.owner_key),
        json={"reason": "downgraded"},
    )
    assert void.status_code == 200, void.text
    off = await client.delete(_pp(e, "eval-suites/clinical-notes"), headers=_hdrs(e.owner_key))
    assert off.status_code == 204, off.text


# ── 6. Family requirement name and tag values are DLP-scanned ──


@pytest.mark.asyncio
async def test_requirements_text_is_dlp_scanned(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    e = await _env(db_session)
    await _suite(client, e)
    scanned: list[str] = []
    real = evals_routes.dlp_scanner

    async def scanner(db, auth, project):
        inner = await real(db, auth, project)

        def scan(value):
            scanned.append(value)
            out = inner(value)
            return out.replace("SECRET", "[REDACTED]") if out else out

        return scan

    monkeypatch.setattr(artifacts_routes, "dlp_scanner", scanner)
    r = await client.put(
        _pp(e, "artifact-families/model/clinSECRET/requirements"),
        headers=_hdrs(e.owner_key),
        json={"suites": ["clinical-notes"], "require_tags": {"phi_compliant": "v3-SECRET"}},
    )
    assert r.status_code == 200, r.text
    assert "clinSECRET" in scanned and "v3-SECRET" in scanned
    row = (
        await db_session.execute(
            select(ArtifactFamilyRequirement).where(
                ArtifactFamilyRequirement.project_id == e.project.id
            )
        )
    ).scalar_one()
    assert "SECRET" not in row.name and "SECRET" not in row.require_tags
    assert r.json()["require_tags"] == {"phi_compliant": "v3-[REDACTED]"}

    # A scan that can't run stores nothing (fail closed).
    async def broken_scanner(db, auth, project):
        def scan(value):
            raise evals_routes._dlp_unavailable()

        return scan

    monkeypatch.setattr(artifacts_routes, "dlp_scanner", broken_scanner)
    bad = await client.put(
        _pp(e, "artifact-families/dataset/notes/requirements"),
        headers=_hdrs(e.owner_key),
        json={"require_tags": {"k": "v"}},
    )
    assert bad.status_code == 422 and _code(bad) == "dlp_unavailable", bad.text


# ── 7. latest_trusted_runs selects one row per partition ──


@pytest.mark.asyncio
async def test_latest_trusted_runs_one_per_artifact_and_lineage(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    e = await _env(db_session)
    s1 = await _suite(client, e)
    s2 = await _suite(client, e, slug="second", name="Second")
    a1 = await _artifact(client, e, version="1")
    a2 = await _artifact(client, e, version="2")
    base = datetime.now(timezone.utc) - timedelta(days=1)
    rows = []
    for art in (a1, a2):
        for suite in (s1, s2):
            for i in range(4):
                rows.append(
                    EvalRun(
                        id=f"evr_{uuid.uuid4().hex[:16]}",
                        project_id=e.project.id,
                        suite_id=suite["id"],
                        suite_lineage_id=suite["lineage_id"],
                        artifact_id=art["id"],
                        trigger_source="ci",
                        grader_persona="clin-grader",
                        grader_user_id=e.grader_user.id,
                        state="finished",
                        verdict="passed",
                        trusted=i != 3,  # the newest is untrusted
                        started_at=base,
                        finished_at=base + timedelta(minutes=i),
                        created_by_user_id=e.owner.id,
                        actor_type="user",
                    )
                )
    db_session.add_all(rows)
    await db_session.commit()

    latest = await artifacts_svc.latest_trusted_runs(db_session, [a1["id"], a2["id"]])
    assert len(latest) == 4
    for (aid, lin), run in latest.items():
        expected = max(
            (r for r in rows if r.artifact_id == aid and r.suite_lineage_id == lin and r.trusted),
            key=lambda r: r.finished_at,
        )
        assert run.id == expected.id

    # The bound is explicit.
    monkeypatch.setattr(artifacts_svc, "MAX_LATEST_RUNS", 2)
    assert len(await artifacts_svc.latest_trusted_runs(db_session, [a1["id"], a2["id"]])) == 2


# ── Admin standing is re-checked under the project scope lock ──


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["void", "delete_artifact", "requirements"])
async def test_admin_demoted_while_waiting_for_lock_is_refused(
    client: AsyncClient, db_session: AsyncSession, monkeypatch, action: str
):
    from sessionfs.server.db.models import OrgMember

    e = await _env(db_session)
    await db_session.execute(
        update(OrgMember)
        .where(OrgMember.org_id == e.org.id, OrgMember.user_id == e.member.id)
        .values(role="admin")
    )
    await db_session.commit()
    await _suite(client, e)
    art = await _artifact(client, e)
    run = await _passing_run(client, e, art["id"]) if action == "void" else None

    real = evals_routes.lock_project_scope_authorized

    async def spy(db, project, **kw):
        # The demotion lands while the request waits for the lock.
        await db.execute(
            update(OrgMember)
            .where(OrgMember.org_id == e.org.id, OrgMember.user_id == e.member.id)
            .values(role="member")
        )
        await real(db, project, **kw)

    monkeypatch.setattr(evals_routes, "lock_project_scope_authorized", spy)
    h = _hdrs(e.member_key)
    if action == "void":
        r = await client.post(_pp(e, f"eval-runs/{run['id']}/void"), headers=h, json={"reason": "x"})
    elif action == "delete_artifact":
        r = await client.delete(_pp(e, f"artifacts/{art['id']}"), headers=h)
    else:
        r = await client.put(
            _pp(e, "artifact-families/model/clin/requirements"),
            headers=h,
            json={"suites": ["clinical-notes"]},
        )
    assert r.status_code == 403, r.text
    assert _code(r) == "project_admin_required"
    if action == "void":
        row = await db_session.get(EvalRun, run["id"])
        await db_session.refresh(row)
        assert row.state != "voided"
    elif action == "delete_artifact":
        row = await db_session.get(Artifact, art["id"])
        await db_session.refresh(row)
        assert row is not None
    else:
        reqs = (await db_session.execute(select(ArtifactFamilyRequirement))).scalars().all()
        assert reqs == []
