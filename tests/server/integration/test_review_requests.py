"""Auto-reviewer P1a — review request lifecycle and personal runners.

Covers the requester surface (create + supersede, list, get, packet, claim,
finish) and the personal runner surface (enrol, list, next/304, retire), the
fenced writes that must store nothing on a stale claim, findings validation +
DLP redaction, path-word ranking, the For You card, and the service-key /
one-runner limits.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

from httpx import AsyncClient
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    AgentPersona,
    LoopChecklistItem,
    Organization,
    OrgMember,
    Project,
    ReviewRequest,
    Ticket,
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
SHA2 = "b" * 40


def _request_body(project: Project, **over) -> dict:
    body = {
        "profile": "default",
        "provider": "github",
        "repo": "acme/widgets",
        "pr_number": 42,
        "head_sha": SHA1,
    }
    body.update(over)
    return body


def _err(resp) -> dict:
    """The error envelope's ``code`` plus its ``details`` in one dict."""
    error = resp.json()["error"]
    return {"code": error["code"], **(error.get("details") or {})}


async def _enrol(client: AsyncClient, key: str, project: Project) -> str:
    resp = await client.post(
        "/api/v1/me/review-runners",
        headers=_hdrs(key),
        json={"name": "local", "project_ids": [project.id]},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


async def _create(client: AsyncClient, key: str, project: Project, **over) -> dict:
    resp = await client.post(
        f"/api/v1/projects/{project.id}/review-requests",
        headers=_hdrs(key),
        json=_request_body(project, **over),
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()


async def _claim(client: AsyncClient, key: str, rid: str, runner_id: str) -> int:
    resp = await client.post(
        f"/api/v1/review-requests/{rid}/claim",
        headers=_hdrs(key),
        json={"runner_id": runner_id},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["lease_epoch"]


async def test_create_idempotent_and_supersede(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)

    r1 = await client.post(
        f"/api/v1/projects/{project.id}/review-requests",
        headers=_hdrs(key),
        json=_request_body(project),
    )
    assert r1.status_code == 201, r1.text
    rid = r1.json()["id"]
    assert r1.json()["status"] == "queued"
    assert r1.json()["requester_user_id"] == owner.id
    assert r1.json()["profile"] == "default"

    # Same target → same row, 200.
    r2 = await client.post(
        f"/api/v1/projects/{project.id}/review-requests",
        headers=_hdrs(key),
        json=_request_body(project),
    )
    assert r2.status_code == 200, r2.text
    assert r2.json()["id"] == rid

    # New head sha → a new row; the old pending one is superseded.
    r3 = await client.post(
        f"/api/v1/projects/{project.id}/review-requests",
        headers=_hdrs(key),
        json=_request_body(project, head_sha=SHA2),
    )
    assert r3.status_code == 201, r3.text
    assert r3.json()["id"] != rid

    rows = (
        await db_session.execute(
            select(ReviewRequest).where(ReviewRequest.project_id == project.id)
        )
    ).scalars().all()
    by_id = {r.id: r for r in rows}
    assert by_id[rid].status == "superseded"
    assert by_id[rid].superseded_by_id == r3.json()["id"]
    assert by_id[r3.json()["id"]].status == "queued"


async def test_authorization_and_validation(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, "owner")
    other, other_key = await _make_user(db_session, "other")
    project = await _make_project(db_session, owner)

    # Bad sha.
    bad = await client.post(
        f"/api/v1/projects/{project.id}/review-requests",
        headers=_hdrs(key),
        json=_request_body(project, head_sha="not-a-sha"),
    )
    assert bad.status_code == 422
    assert _err(bad)["code"] == "invalid_head_sha"

    # No target at all.
    none = await client.post(
        f"/api/v1/projects/{project.id}/review-requests",
        headers=_hdrs(key),
        json={"profile": "default"},
    )
    assert none.status_code == 422
    assert _err(none)["code"] == "missing_target"

    # Unknown profile.
    no_persona = await client.post(
        f"/api/v1/projects/{project.id}/review-requests",
        headers=_hdrs(key),
        json=_request_body(project, profile="nope"),
    )
    assert no_persona.status_code == 404
    assert _err(no_persona)["code"] == "persona_not_found"

    # mode is draft_for_me only.
    bad_mode = await client.post(
        f"/api/v1/projects/{project.id}/review-requests",
        headers=_hdrs(key),
        json=_request_body(project, mode="org_reviewer"),
    )
    assert bad_mode.status_code == 422

    # A request is visible only to its requester (a miss is one 404 body).
    created = await client.post(
        f"/api/v1/projects/{project.id}/review-requests",
        headers=_hdrs(key),
        json=_request_body(project),
    )
    rid = created.json()["id"]
    hidden = await client.get(f"/api/v1/review-requests/{rid}", headers=_hdrs(other_key))
    assert hidden.status_code == 404
    assert hidden.json()["error"]["code"] == "404"


async def test_service_key_rejected(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, owner, "admin")
    _svc, svc_key = await _make_service_key(
        db_session, org, owner, ["review_requests:read"]
    )

    resp = await client.get("/api/v1/me/review-requests", headers=_hdrs(svc_key))
    assert resp.status_code == 403
    assert _err(resp)["code"] == "service_key_not_allowed"


async def test_claim_race(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)
    rid = (await _create(client, key, project))["id"]
    runner_id = await _enrol(client, key, project)

    claim = await client.post(
        f"/api/v1/review-requests/{rid}/claim",
        headers=_hdrs(key),
        json={"runner_id": runner_id},
    )
    assert claim.status_code == 200, claim.text
    assert claim.json()["status"] == "claimed"
    assert claim.json()["lease_epoch"] == 1

    # A second claim of the same (still leased) row loses.
    again = await client.post(
        f"/api/v1/review-requests/{rid}/claim",
        headers=_hdrs(key),
        json={"runner_id": runner_id},
    )
    assert again.status_code == 409
    assert _err(again)["code"] == "lease_conflict"


async def test_finish_fences_store_nothing(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)
    rid = (await _create(client, key, project))["id"]
    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, rid, runner_id)

    def _finish(body: dict):
        return client.post(
            f"/api/v1/review-requests/{rid}/finish", headers=_hdrs(key), json=body
        )

    async def _still_claimed() -> None:
        row = await db_session.get(ReviewRequest, rid)
        assert row.status == "claimed"
        assert row.verdict is None
        assert row.findings_count == 0
        assert row.findings == "[]"

    # Wrong head → 409 head_mismatch, nothing stored.
    wrong_head = await _finish(
        {"runner_id": runner_id, "lease_epoch": epoch, "head_sha": SHA2, "verdict": "clean"}
    )
    assert wrong_head.status_code == 409
    assert _err(wrong_head)["code"] == "head_mismatch"
    await _still_claimed()

    # Stale epoch → 409 lease_superseded, nothing stored.
    stale = await _finish(
        {"runner_id": runner_id, "lease_epoch": epoch - 1, "head_sha": SHA1, "verdict": "clean"}
    )
    assert stale.status_code == 409
    assert _err(stale)["code"] == "lease_superseded"
    await _still_claimed()

    # Expired lease → 409 lease_superseded, nothing stored.
    await db_session.execute(
        update(ReviewRequest)
        .where(ReviewRequest.id == rid)
        .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    await db_session.commit()
    expired = await _finish(
        {"runner_id": runner_id, "lease_epoch": epoch, "head_sha": SHA1, "verdict": "clean"}
    )
    assert expired.status_code == 409
    assert _err(expired)["code"] == "lease_superseded"
    await _still_claimed()


async def test_finish_success(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)
    rid = (await _create(client, key, project))["id"]
    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, rid, runner_id)

    finish = await client.post(
        f"/api/v1/review-requests/{rid}/finish",
        headers=_hdrs(key),
        json={
            "runner_id": runner_id,
            "lease_epoch": epoch,
            "head_sha": SHA1,
            "verdict": "findings",
            "findings": [
                {
                    "path": "src/p.py",
                    "line": 10,
                    "severity": "high",
                    "class": "null-deref",
                    "message": "Parser dereferences an unvalidated pointer.",
                }
            ],
            "input_tokens": 10,
            "output_tokens": 20,
            "cost_usd": 0.012345,
        },
    )
    assert finish.status_code == 200, finish.text
    body = finish.json()
    assert body["status"] == "done"
    assert body["verdict"] == "findings"
    assert body["findings_count"] == 1
    assert body["findings"][0]["class"] == "null-deref"
    assert body["findings"][0]["severity"] == "high"
    assert body["findings"][0]["line"] == 10
    assert body["input_tokens"] == 10
    assert body["output_tokens"] == 20
    assert body["cost_usd"] == "0.012345"


async def test_findings_validation(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)
    rid = (await _create(client, key, project))["id"]
    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, rid, runner_id)

    def _finish(findings: list[dict]):
        return client.post(
            f"/api/v1/review-requests/{rid}/finish",
            headers=_hdrs(key),
            json={
                "runner_id": runner_id,
                "lease_epoch": epoch,
                "head_sha": SHA1,
                "verdict": "findings",
                "findings": findings,
            },
        )

    # A bad finding class slug is refused before any write.
    bad_class = await _finish(
        [{"path": "a.py", "line": 1, "severity": "low", "class": "Not A Slug!", "message": "x"}]
    )
    assert bad_class.status_code == 422
    assert _err(bad_class)["code"] == "invalid_finding_class"

    # Too many findings.
    too_many = await _finish(
        [{"path": "a.py", "line": 1, "severity": "low", "message": "m"}] * 201
    )
    assert too_many.status_code == 422
    assert _err(too_many)["code"] == "too_many_findings"

    # Nothing was written.
    row = await db_session.get(ReviewRequest, rid)
    assert row.status == "claimed"
    assert row.findings_count == 0


async def test_finish_dlp_redaction(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session, "owner")
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="DLP Org",
        slug=f"dlp-{uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
        settings=json.dumps(
            {"dlp": {"enabled": True, "mode": "redact", "categories": ["secrets"]}}
        ),
    )
    db_session.add(org)
    await db_session.commit()
    db_session.add(OrgMember(org_id=org.id, user_id=owner.id, role="admin"))
    await db_session.commit()
    project = await _make_project(db_session, owner, org)

    rid = (await _create(client, key, project))["id"]
    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, rid, runner_id)

    secret = "supersecretvalue123"
    finish = await client.post(
        f"/api/v1/review-requests/{rid}/finish",
        headers=_hdrs(key),
        json={
            "runner_id": runner_id,
            "lease_epoch": epoch,
            "head_sha": SHA1,
            "verdict": "findings",
            "findings": [
                {
                    "path": "src/config.py",
                    "line": 4,
                    "severity": "high",
                    "message": f'Set password = "{secret}" here.',
                }
            ],
        },
    )
    assert finish.status_code == 200, finish.text
    stored = finish.json()["findings"][0]["message"]
    assert secret not in stored
    assert "[REDACTED:" in stored


async def test_packet_ranking_prefers_changed_paths(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)
    project.context_document = (
        "## Overview\nBilling and invoicing for the platform.\n\n"
        "## Architecture\nCheckout and payment flow with card tokens.\n"
    )
    await db_session.commit()

    rid = (await _create(client, key, project))["id"]
    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, rid, runner_id)

    packet_resp = await client.get(
        f"/api/v1/review-requests/{rid}/packet",
        headers=_hdrs(key),
        params={
            "runner_id": runner_id,
            "lease_epoch": epoch,
            "paths": "src/checkout/payment.py",
        },
    )
    assert packet_resp.status_code == 200, packet_resp.text
    packet = packet_resp.json()["packet"]
    sections = list(packet["context_sections"]["content"])
    assert "architecture" in sections and "overview" in sections
    # The section mentioning the changed path words ranks first.
    assert sections[0] == "architecture"


async def test_runner_lifecycle_and_next_304(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)

    enrolled = await client.post(
        "/api/v1/me/review-runners",
        headers=_hdrs(key),
        json={"name": "local", "project_ids": [project.id]},
    )
    assert enrolled.status_code == 200, enrolled.text
    rid = enrolled.json()["id"]
    assert enrolled.json()["status"] == "active"
    assert enrolled.json()["project_ids"] == [project.id]

    # Idempotent enrol returns the same runner.
    again = await client.post(
        "/api/v1/me/review-runners",
        headers=_hdrs(key),
        json={"name": "local", "project_ids": [project.id]},
    )
    assert again.json()["id"] == rid

    listing = await client.get("/api/v1/me/review-runners", headers=_hdrs(key))
    assert [r["id"] for r in listing.json()["runners"]] == [rid]

    # Idle next → null with an ETag; a repeat answers 304.
    nxt = await client.get(f"/api/v1/me/review-runners/{rid}/next", headers=_hdrs(key))
    assert nxt.status_code == 200
    assert nxt.json()["next"] is None
    etag = nxt.headers["ETag"]
    not_modified = await client.get(
        f"/api/v1/me/review-runners/{rid}/next",
        headers={**_hdrs(key), "If-None-Match": etag},
    )
    assert not_modified.status_code == 304

    # A queued request is the next candidate.
    await _create(client, key, project)
    nxt = await client.get(f"/api/v1/me/review-runners/{rid}/next", headers=_hdrs(key))
    assert nxt.status_code == 200
    assert nxt.json()["next"]["repo"] == "acme/widgets"
    assert nxt.json()["next"]["head_sha"] == SHA1

    # Retire → 204; a retired runner no longer has a work source.
    retired = await client.post(
        f"/api/v1/me/review-runners/{rid}/retire", headers=_hdrs(key)
    )
    assert retired.status_code == 204
    no_next = await client.get(
        f"/api/v1/me/review-runners/{rid}/next", headers=_hdrs(key)
    )
    assert no_next.status_code == 404


async def test_community_single_runner(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    from sessionfs.server.routes import review_runners

    monkeypatch.setattr(review_runners, "get_edition", lambda: "community")

    alice, alice_key = await _make_user(db_session, "alice")
    bob, bob_key = await _make_user(db_session, "bob")
    alice_project = await _make_project(db_session, alice)
    bob_project = await _make_project(db_session, bob)

    first = await client.post(
        "/api/v1/me/review-runners",
        headers=_hdrs(alice_key),
        json={"name": "alice-runner", "project_ids": [alice_project.id]},
    )
    assert first.status_code == 200, first.text

    second = await client.post(
        "/api/v1/me/review-runners",
        headers=_hdrs(bob_key),
        json={"name": "bob-runner", "project_ids": [bob_project.id]},
    )
    assert second.status_code == 409
    assert _err(second)["code"] == "runner_limit"


async def test_for_you_card_requester_only(
    client: AsyncClient, db_session: AsyncSession
):
    owner, key = await _make_user(db_session, "owner")
    other, other_key = await _make_user(db_session, "other")
    project = await _make_project(db_session, owner)

    rid = (await _create(client, key, project))["id"]
    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, rid, runner_id)
    finish = await client.post(
        f"/api/v1/review-requests/{rid}/finish",
        headers=_hdrs(key),
        json={
            "runner_id": runner_id,
            "lease_epoch": epoch,
            "head_sha": SHA1,
            "verdict": "findings",
            "findings": [
                {"path": "src/a.py", "line": 2, "severity": "high", "message": "leak"}
            ],
        },
    )
    assert finish.status_code == 200, finish.text

    requester_feed = (
        await client.get("/api/v1/me/for-you", headers=_hdrs(key))
    ).json()
    assert "review_draft" in [i["kind"] for i in requester_feed["items"]]

    other_feed = (
        await client.get("/api/v1/me/for-you", headers=_hdrs(other_key))
    ).json()
    assert "review_draft" not in [i["kind"] for i in other_feed["items"]]


async def test_finish_redacts_secrets_without_any_org_policy(
    client: AsyncClient, db_session: AsyncSession
):
    """A personal project (no org, so no org DLP policy) still has secrets
    redacted from findings: the model read the code and may quote one."""
    owner, key = await _make_user(db_session, "solo")
    project = await _make_project(db_session, owner, None)

    rid = (await _create(client, key, project))["id"]
    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, rid, runner_id)
    token = "ghp_" + "a1B2c3D4e5F6g7H8i9J0k1L2m3N4o5P6q7R8"
    finish = await client.post(
        f"/api/v1/review-requests/{rid}/finish",
        headers=_hdrs(key),
        json={
            "runner_id": runner_id,
            "lease_epoch": epoch,
            "head_sha": SHA1,
            "verdict": "findings",
            "findings": [
                {"path": "deploy.sh", "line": 3, "severity": "critical",
                 "message": f"A GitHub token is committed here: {token}"}
            ],
        },
    )
    assert finish.status_code == 200, finish.text
    stored = finish.json()["findings"][0]["message"]
    assert token not in stored


async def test_supersede_is_scoped_to_requester(
    client: AsyncClient, db_session: AsyncSession
):
    """A new head supersedes only the requester's own pending row, never
    another requester's request for the same target in the same project."""
    owner, owner_key = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, owner, "admin")
    member, member_key = await _make_user(db_session, "member")
    await _add_org_member(db_session, org, member, "member")
    project = await _make_project(db_session, owner, org)

    owner_rid = (await _create(client, owner_key, project))["id"]
    member_rid = (await _create(client, member_key, project))["id"]

    await _create(client, owner_key, project, head_sha=SHA2)

    rows = (
        await db_session.execute(
            select(ReviewRequest).where(ReviewRequest.project_id == project.id)
        )
    ).scalars().all()
    by_id = {r.id: r for r in rows}
    assert by_id[owner_rid].status == "superseded"
    assert by_id[member_rid].status == "queued"


async def test_create_rejects_mixed_target_and_foreign_ticket(
    client: AsyncClient, db_session: AsyncSession
):
    """An explicit target plus a ticket_id is ambiguous, and a ticket that
    belongs to another project is never accepted as a target."""
    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)
    other_project = await _make_project(db_session, owner)

    mixed = await client.post(
        f"/api/v1/projects/{project.id}/review-requests",
        headers=_hdrs(key),
        json={**_request_body(project), "ticket_id": "tk_whatever"},
    )
    assert mixed.status_code == 422
    assert _err(mixed)["code"] == "ambiguous_target"

    foreign = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=other_project.id,
        title="foreign ticket",
        status="open",
        kind="task",
        priority="medium",
        created_by_user_id=owner.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(foreign)
    await db_session.commit()

    cross = await client.post(
        f"/api/v1/projects/{project.id}/review-requests",
        headers=_hdrs(key),
        json={"profile": "default", "ticket_id": foreign.id},
    )
    assert cross.status_code == 404


async def test_packet_does_not_leak_foreign_ticket(
    client: AsyncClient, db_session: AsyncSession
):
    """The packet constrains its ticket query by the request's project, so a
    stored ticket_id from a different project yields no ticket metadata."""
    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)
    other_project = await _make_project(db_session, owner)

    foreign = Ticket(
        id=f"tk_{uuid.uuid4().hex[:16]}",
        project_id=other_project.id,
        title="secret cross-project ticket",
        status="open",
        kind="task",
        priority="medium",
        created_by_user_id=owner.id,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(foreign)
    await db_session.commit()

    request = ReviewRequest(
        id=f"rvq_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        requester_user_id=owner.id,
        provider="github",
        repo="acme/widgets",
        pr_number=42,
        head_sha=SHA1,
        profile="default",
        instructions_version=1,
        mode="draft_for_me",
        ticket_id=foreign.id,
        status="queued",
    )
    db_session.add(request)
    await db_session.commit()

    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, request.id, runner_id)
    packet_resp = await client.get(
        f"/api/v1/review-requests/{request.id}/packet",
        headers=_hdrs(key),
        params={"runner_id": runner_id, "lease_epoch": epoch},
    )
    assert packet_resp.status_code == 200, packet_resp.text
    assert packet_resp.json()["packet"]["ticket"]["content"] is None


async def test_losing_project_access_revokes_draft_access(
    client: AsyncClient, db_session: AsyncSession
):
    """Removing a requester from the org hides their stored drafts and takes
    them out of the runner's ``next`` queue."""
    owner, _ = await _make_user(db_session, "owner")
    org = await _make_org(db_session)
    await _add_org_member(db_session, org, owner, "admin")
    member, member_key = await _make_user(db_session, "member")
    await _add_org_member(db_session, org, member, "member")
    project = await _make_project(db_session, owner, org)

    rid = (await _create(client, member_key, project))["id"]
    runner_id = await _enrol(client, member_key, project)

    await db_session.execute(
        delete(OrgMember).where(
            OrgMember.org_id == org.id, OrgMember.user_id == member.id
        )
    )
    await db_session.commit()

    listing = await client.get("/api/v1/me/review-requests", headers=_hdrs(member_key))
    assert listing.status_code == 200
    assert listing.json()["items"] == []

    single = await client.get(f"/api/v1/review-requests/{rid}", headers=_hdrs(member_key))
    assert single.status_code == 404

    nxt = await client.get(
        f"/api/v1/me/review-runners/{runner_id}/next", headers=_hdrs(member_key)
    )
    assert nxt.status_code == 200
    assert nxt.json()["next"] is None


async def test_finish_scans_paths_coverage_and_error(
    client: AsyncClient, db_session: AsyncSession
):
    """Every persisted finish string — finding path, coverage, error — is
    DLP-scanned, not just finding messages."""
    owner, key = await _make_user(db_session, "solo")
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="DLP Org",
        slug=f"dlp-{uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
        settings=json.dumps(
            {"dlp": {"enabled": True, "mode": "redact", "categories": ["secrets"]}}
        ),
    )
    db_session.add(org)
    await db_session.commit()
    db_session.add(OrgMember(org_id=org.id, user_id=owner.id, role="admin"))
    await db_session.commit()
    project = await _make_project(db_session, owner, org)

    rid = (await _create(client, key, project))["id"]
    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, rid, runner_id)

    token = "ghp_" + "z9Y8x7W6v5U4t3S2r1Q0p9O8i7U6y5T4r3E2w1Q"
    finish = await client.post(
        f"/api/v1/review-requests/{rid}/finish",
        headers=_hdrs(key),
        json={
            "runner_id": runner_id,
            "lease_epoch": epoch,
            "head_sha": SHA1,
            "verdict": "findings",
            "findings": [
                {"path": f"src/{token}/config.py", "line": 4, "severity": "high",
                 "message": "ok"}
            ],
            "coverage": {
                "reviewed_files": [f"src/{token}/a.py"],
                "skipped_files": [
                    {"path": f"src/{token}/b.py", "reason": f"skipped {token}"}
                ],
                "incomplete": True,
                "reason": f"did not finish {token}",
            },
            "error": f"crashed with {token}",
        },
    )
    assert finish.status_code == 200, finish.text
    assert token not in finish.text


async def test_heartbeat_rejects_expired_lease(
    client: AsyncClient, db_session: AsyncSession
):
    """A heartbeat cannot resurrect a claim whose lease has already lapsed."""
    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)
    rid = (await _create(client, key, project))["id"]
    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, rid, runner_id)

    await db_session.execute(
        update(ReviewRequest)
        .where(ReviewRequest.id == rid)
        .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    await db_session.commit()

    hb = await client.post(
        f"/api/v1/review-requests/{rid}/heartbeat",
        headers=_hdrs(key),
        json={"runner_id": runner_id, "lease_epoch": epoch},
    )
    assert hb.status_code == 409
    assert _err(hb)["code"] == "lease_superseded"


async def test_claim_enforces_runner_project_allowlist(
    client: AsyncClient, db_session: AsyncSession
):
    """A runner enrolled for project A cannot claim a request in project B."""
    owner, key = await _make_user(db_session, "owner")
    project_a = await _make_project(db_session, owner)
    project_b = await _make_project(db_session, owner)

    enrolled = await client.post(
        "/api/v1/me/review-runners",
        headers=_hdrs(key),
        json={"name": "local", "project_ids": [project_a.id]},
    )
    assert enrolled.status_code == 200, enrolled.text
    runner_id = enrolled.json()["id"]

    rid_b = (await _create(client, key, project_b))["id"]
    claim = await client.post(
        f"/api/v1/review-requests/{rid_b}/claim",
        headers=_hdrs(key),
        json={"runner_id": runner_id},
    )
    assert claim.status_code == 403
    assert _err(claim)["code"] == "runner_project_not_allowed"


async def test_packet_trust_labels_and_instructions_pinning(
    client: AsyncClient, db_session: AsyncSession
):
    """Packet sections carry provenance/trust labels; only the pinned persona
    and fixed SessionFS text are trusted. A later persona edit is ignored: the
    packet keeps serving the exact instructions the request was created with."""
    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)

    persona = AgentPersona(
        id=f"per_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        name="strict",
        role="reviewer",
        content="Be strict.",
        specializations="[]",
        created_by=owner.id,
        version=1,
    )
    db_session.add(persona)
    await db_session.commit()

    rid = (await _create(client, key, project, profile="strict"))["id"]
    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, rid, runner_id)

    packet_resp = await client.get(
        f"/api/v1/review-requests/{rid}/packet",
        headers=_hdrs(key),
        params={"runner_id": runner_id, "lease_epoch": epoch},
    )
    assert packet_resp.status_code == 200, packet_resp.text
    packet = packet_resp.json()["packet"]

    for section in ("request", "project", "ticket", "persona", "changed_paths",
                    "instructions_version"):
        assert set(packet[section]) == {"source", "trust", "content"}
    assert packet["persona"]["trust"] == "trusted"
    assert packet["instructions_version"]["trust"] == "trusted"
    assert packet["persona"]["content"]["name"] == "strict"
    assert packet["instructions_version"]["content"] == 1
    assert packet["request"]["trust"] == "untrusted"
    assert packet["project"]["trust"] == "untrusted"
    assert packet["ticket"]["trust"] == "untrusted"
    assert packet["changed_paths"]["trust"] == "untrusted"

    await db_session.execute(
        update(AgentPersona).where(AgentPersona.id == persona.id).values(version=2)
    )
    await db_session.commit()

    stale = await client.get(
        f"/api/v1/review-requests/{rid}/packet",
        headers=_hdrs(key),
        params={"runner_id": runner_id, "lease_epoch": epoch},
    )
    assert stale.status_code == 200, stale.text
    stale_packet = stale.json()["packet"]
    # The instructions are pinned at create time, so the bump changes nothing.
    assert stale_packet["persona"]["content"]["name"] == "strict"
    assert stale_packet["persona"]["content"]["version"] == 1
    assert stale_packet["persona"]["content"]["content"] == "Be strict."


async def test_finish_body_over_limit_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    """A finish body over the 1 MiB cap is refused before model validation."""
    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)
    rid = (await _create(client, key, project))["id"]
    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, rid, runner_id)

    big = await client.post(
        f"/api/v1/review-requests/{rid}/finish",
        headers=_hdrs(key),
        json={
            "runner_id": runner_id,
            "lease_epoch": epoch,
            "head_sha": SHA1,
            "verdict": "clean",
            "error": "x" * (1024 * 1024 + 1),
        },
    )
    assert big.status_code == 413
    assert _err(big)["code"] == "finish_too_large"


async def test_idempotency_distinguishes_provider_and_pr_number(
    client: AsyncClient, db_session: AsyncSession
):
    """The idempotency key includes provider and PR number, so a different
    provider or PR at the same repo+head sha is a distinct target, not an alias
    of the existing row."""
    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)

    a = (await _create(client, key, project))["id"]  # github, pr 42
    # Same repo/head sha but a different provider is a new row.
    b = (await _create(client, key, project, provider="gitlab"))["id"]
    assert b != a
    # Same provider/repo/head sha but a different PR number is a new row.
    c = (await _create(client, key, project, pr_number=43))["id"]
    assert c not in (a, b)
    # The exact same target is still idempotent.
    again = (await _create(client, key, project))["id"]
    assert again == a


async def test_packet_requires_runner_and_fences_lease(
    client: AsyncClient, db_session: AsyncSession
):
    """The packet is only served to the active, owned runner holding an
    unexpired lease: a missing or foreign runner, a wrong epoch or a lapsed
    lease all refuse before any context is assembled."""
    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)
    rid = (await _create(client, key, project))["id"]
    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, rid, runner_id)

    # A missing runner_id is rejected by FastAPI before the endpoint runs.
    missing = await client.get(
        f"/api/v1/review-requests/{rid}/packet",
        headers=_hdrs(key),
        params={"lease_epoch": epoch},
    )
    assert missing.status_code == 422

    # A runner owned by someone else is not found for this caller.
    stranger, stranger_key = await _make_user(db_session, "stranger")
    stranger_project = await _make_project(db_session, stranger)
    foreign_runner = await _enrol(client, stranger_key, stranger_project)
    foreign = await client.get(
        f"/api/v1/review-requests/{rid}/packet",
        headers=_hdrs(key),
        params={"runner_id": foreign_runner, "lease_epoch": epoch},
    )
    assert foreign.status_code == 404

    # A wrong lease epoch is a stale claim.
    wrong_epoch = await client.get(
        f"/api/v1/review-requests/{rid}/packet",
        headers=_hdrs(key),
        params={"runner_id": runner_id, "lease_epoch": epoch + 1},
    )
    assert wrong_epoch.status_code == 409
    assert _err(wrong_epoch)["code"] == "lease_superseded"

    # An expired lease is a stale claim too, and stores no manifest.
    await db_session.execute(
        update(ReviewRequest)
        .where(ReviewRequest.id == rid)
        .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    await db_session.commit()
    expired = await client.get(
        f"/api/v1/review-requests/{rid}/packet",
        headers=_hdrs(key),
        params={"runner_id": runner_id, "lease_epoch": epoch},
    )
    assert expired.status_code == 409
    assert _err(expired)["code"] == "lease_superseded"


async def test_packet_runner_project_not_allowed(
    client: AsyncClient, db_session: AsyncSession
):
    """A packet is refused when the caller's runner is not enrolled for the
    request's project (the same allowlist the claim path enforces)."""
    owner, key = await _make_user(db_session, "owner")
    project_a = await _make_project(db_session, owner)
    project_b = await _make_project(db_session, owner)

    rid_a = (await _create(client, key, project_a))["id"]
    runner_a = await _enrol(client, key, project_a)
    epoch = await _claim(client, key, rid_a, runner_a)

    # Retire runner_a so the owner can enrol a second runner scoped to B only.
    retired = await client.post(
        f"/api/v1/me/review-runners/{runner_a}/retire", headers=_hdrs(key)
    )
    assert retired.status_code == 204
    runner_b = await _enrol(client, key, project_b)

    blocked = await client.get(
        f"/api/v1/review-requests/{rid_a}/packet",
        headers=_hdrs(key),
        params={"runner_id": runner_b, "lease_epoch": epoch},
    )
    assert blocked.status_code == 403
    assert _err(blocked)["code"] == "runner_project_not_allowed"


async def test_packet_request_block_is_untrusted(
    client: AsyncClient, db_session: AsyncSession
):
    """Requester-supplied target metadata (repo, head sha, …) is labelled
    untrusted in the packet, never trusted like the pinned persona or fixed
    SessionFS text."""
    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)
    rid = (await _create(client, key, project))["id"]
    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, rid, runner_id)

    resp = await client.get(
        f"/api/v1/review-requests/{rid}/packet",
        headers=_hdrs(key),
        params={"runner_id": runner_id, "lease_epoch": epoch},
    )
    assert resp.status_code == 200, resp.text
    request_block = resp.json()["packet"]["request"]
    assert request_block["trust"] == "untrusted"
    assert request_block["source"] == "request"
    assert request_block["content"]["repo"] == "acme/widgets"


async def test_packet_manifest_dlp_scans_all_strings(
    client: AsyncClient, db_session: AsyncSession
):
    """Every string in the packet manifest (checklist slug, context heading,
    KB ref, path) is DLP-scanned before storage, so a secret in a checklist
    slug is redacted rather than persisted in the clear."""
    owner, key = await _make_user(db_session, "owner")
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Manifest DLP",
        slug=f"manifest-dlp-{uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
        settings=json.dumps(
            {"dlp": {"enabled": True, "mode": "redact", "categories": ["secrets"]}}
        ),
    )
    db_session.add(org)
    await db_session.commit()
    db_session.add(OrgMember(org_id=org.id, user_id=owner.id, role="admin"))
    await db_session.commit()
    project = await _make_project(db_session, owner, org)

    token = "ghp_" + "z9Y8x7W6v5U4t3S2r1Q0p9O8i7U6y5T4r3E2w1Q"
    db_session.add(
        LoopChecklistItem(
            id=f"lci_{uuid.uuid4().hex[:16]}",
            project_id=project.id,
            slug=token,
            title="trap",
            check_text="check",
            status="active",
            source="manual",
        )
    )
    await db_session.commit()

    rid = (await _create(client, key, project))["id"]
    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, rid, runner_id)

    resp = await client.get(
        f"/api/v1/review-requests/{rid}/packet",
        headers=_hdrs(key),
        params={"runner_id": runner_id, "lease_epoch": epoch},
    )
    assert resp.status_code == 200, resp.text
    manifest = resp.json()["manifest"]
    assert token not in json.dumps(manifest)


async def test_packet_paths_raw_length_rejected(
    client: AsyncClient, db_session: AsyncSession, monkeypatch
):
    """An over-long raw ``paths`` query is refused before splitting, so a client
    can't make the server split a near-unbounded string. The cap is lowered here
    so the test stays under httpx's own URL length limit."""
    from sessionfs.server.routes import review_requests

    monkeypatch.setattr(review_requests, "_MAX_PACKET_PATHS_RAW", 100)

    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)
    rid = (await _create(client, key, project))["id"]
    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, rid, runner_id)

    too_long = "a" * 101
    resp = await client.get(
        f"/api/v1/review-requests/{rid}/packet",
        headers=_hdrs(key),
        params={"runner_id": runner_id, "lease_epoch": epoch, "paths": too_long},
    )
    assert resp.status_code == 422
    assert _err(resp)["code"] == "paths_too_long"


async def test_finish_streaming_body_over_limit_rejected(
    client: AsyncClient, db_session: AsyncSession
):
    """A chunked finish body (no Content-Length) over 1 MiB is refused while
    streaming, before any JSON parsing or model validation."""
    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)
    rid = (await _create(client, key, project))["id"]
    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, rid, runner_id)

    async def body_chunks():
        yield b'{"runner_id":"'
        yield runner_id.encode()
        yield b'","lease_epoch":'
        yield str(epoch).encode()
        yield b',"head_sha":"' + SHA1.encode() + b'","verdict":"clean","error":"'
        yield b"x" * (1024 * 1024)
        yield b'"}'

    resp = await client.post(
        f"/api/v1/review-requests/{rid}/finish",
        headers=_hdrs(key),
        content=body_chunks(),
    )
    assert resp.status_code == 413
    assert _err(resp)["code"] == "finish_too_large"


async def test_packet_serves_pinned_instructions_snapshot(
    client: AsyncClient, db_session: AsyncSession
):
    """The packet serves the instructions snapshot pinned at create time, not a
    live re-resolution — a persona edit after creation changes nothing."""
    owner, key = await _make_user(db_session, "owner")
    project = await _make_project(db_session, owner)

    persona = AgentPersona(
        id=f"per_{uuid.uuid4().hex[:16]}",
        project_id=project.id,
        name="pinner",
        role="reviewer",
        content="Original guidance.",
        specializations="[]",
        created_by=owner.id,
        version=1,
    )
    db_session.add(persona)
    await db_session.commit()

    rid = (await _create(client, key, project, profile="pinner"))["id"]
    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, rid, runner_id)

    # Mutate the persona after the request was created.
    await db_session.execute(
        update(AgentPersona)
        .where(AgentPersona.id == persona.id)
        .values(content="Changed guidance.", version=2)
    )
    await db_session.commit()

    resp = await client.get(
        f"/api/v1/review-requests/{rid}/packet",
        headers=_hdrs(key),
        params={"runner_id": runner_id, "lease_epoch": epoch},
    )
    assert resp.status_code == 200, resp.text
    persona_block = resp.json()["packet"]["persona"]["content"]
    assert persona_block["name"] == "pinner"
    assert persona_block["content"] == "Original guidance."
    assert persona_block["version"] == 1


async def test_retiring_a_runner_revokes_its_lease(client: AsyncClient, db_session: AsyncSession):
    owner, key = await _make_user(db_session, "retire")
    project = await _make_project(db_session, owner, None)
    rid = (await _create(client, key, project))["id"]
    runner_id = await _enrol(client, key, project)
    epoch = await _claim(client, key, rid, runner_id)

    r = await client.post(f"/api/v1/me/review-runners/{runner_id}/retire", headers=_hdrs(key))
    assert r.status_code == 204
    got = await client.get(f"/api/v1/review-requests/{rid}", headers=_hdrs(key))
    assert got.json()["status"] == "queued"

    hb = await client.post(
        f"/api/v1/review-requests/{rid}/heartbeat",
        headers=_hdrs(key),
        json={"runner_id": runner_id, "lease_epoch": epoch},
    )
    assert hb.status_code == 409
    fin = await client.post(
        f"/api/v1/review-requests/{rid}/finish",
        headers=_hdrs(key),
        json={"runner_id": runner_id, "lease_epoch": epoch, "head_sha": SHA1,
              "verdict": "clean", "findings": []},
    )
    assert fin.status_code == 409


async def test_cycling_heads_stays_idempotent(client: AsyncClient, db_session: AsyncSession):
    """head1 -> head2 -> head1 -> head2 -> head1 leaves several superseded
    rows per head; creating again must still return one live row, not 500."""
    owner, key = await _make_user(db_session, "cycle")
    project = await _make_project(db_session, owner, None)
    ids = []
    for head in (SHA1, SHA2, SHA1, SHA2, SHA1):
        ids.append((await _create(client, key, project, head_sha=head))["id"])
    again = await _create(client, key, project, head_sha=SHA1)
    assert again["id"] == ids[-1]
    assert again["status"] == "queued"
