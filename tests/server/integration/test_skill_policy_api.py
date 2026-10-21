"""Org skills policy API and server-side enforcement (marketplace v2 §8,
tk_8ca7d3dcf9724c62)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    DevicePolicyReport,
    DevicePolicyReportHistory,
    Organization,
    OrgAuditEvent,
    OrgPolicyCompilation,
    OrgSkillPolicy,
    SkillDevice,
    User,
)
from tests.server.integration.test_marketplace_moderation import _join, _org, _service, _user
from tests.server.integration.test_skill_disclosure_api import _publish

pytestmark = pytest.mark.asyncio

NO_SCRIPTS = {"mode": "open", "allow_hooks": True, "allow_skill_scripts": False, "max_scan_severity": "critical"}
ACK_HOOKS_OFF = ["claude-code.managed_hooks_only", "vscode.managed_hooks_only", "codex.managed_hooks_only"]


async def _enterprise(db: AsyncSession, owner: User):
    org, project = await _org(db, owner)
    org.tier = "enterprise"
    await db.commit()
    return org, project


async def _put(client: AsyncClient, headers: dict, org: Organization, policy: dict, *, ack=(), etag=None):
    if etag is None:
        etag = (await client.get(f"/api/v1/orgs/{org.id}/skills/policy", headers=headers)).headers["etag"]
    return await client.put(
        f"/api/v1/orgs/{org.id}/skills/policy",
        headers={**headers, "If-Match": etag},
        json={"policy": {"skills": policy}, "acknowledge_warnings": list(ack)},
    )


def _code(response) -> str:
    body = response.json()
    error = body.get("error") or body.get("detail") or {}
    return error.get("code") if isinstance(error, dict) else str(error)


async def test_policy_is_enterprise_only(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)  # Team tier
    got = await client.get(f"/api/v1/orgs/{org.id}/skills/policy", headers=auth_headers)
    assert got.status_code == 403 and _code(got) == "upgrade_required"
    put = await _put(client, auth_headers, org, NO_SCRIPTS, etag='"osp-0"')
    assert put.status_code == 403


async def test_revisions_etag_acknowledgement_and_audit(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _enterprise(db_session, test_user)
    url = f"/api/v1/orgs/{org.id}/skills/policy"
    empty = await client.get(url, headers=auth_headers)
    assert empty.status_code == 200 and empty.json()["revision"] == 0
    assert empty.headers["etag"] == '"osp-0"'

    no_match = await client.put(url, headers=auth_headers, json={"policy": {"skills": NO_SCRIPTS}})
    assert no_match.status_code == 428
    invalid = await _put(client, auth_headers, org, {"mode": "everything"})
    assert invalid.status_code == 422 and _code(invalid) == "invalid_policy"

    # Hooks forbidden: side effects in three tools must be acknowledged.
    unacked = await _put(client, auth_headers, org, {"mode": "open"})
    assert unacked.status_code == 409 and _code(unacked) == "acknowledgement_required"
    first = await _put(client, auth_headers, org, {"mode": "open"}, ack=ACK_HOOKS_OFF)
    assert first.status_code == 200, first.text
    assert first.json()["revision"] == 1 and first.headers["etag"] == '"osp-1"'
    assert first.json()["policy"]["skills"]["policy_version"] == 2

    stale = await _put(client, auth_headers, org, NO_SCRIPTS, etag='"osp-0"')
    assert stale.status_code == 409 and _code(stale) == "policy_changed"
    second = await _put(client, auth_headers, org, NO_SCRIPTS)
    assert second.status_code == 200 and second.json()["revision"] == 2

    rows = (await db_session.execute(select(OrgSkillPolicy).where(OrgSkillPolicy.org_id == org.id))).scalars().all()
    assert sorted(row.revision for row in rows) == [1, 2]  # immutable history
    compilations = (await db_session.execute(select(OrgPolicyCompilation))).scalars().all()
    assert {row.tool for row in compilations} >= {"claude-code", "copilot", "vscode", "gemini", "codex", "cursor"}
    events = (
        await db_session.execute(select(OrgAuditEvent).where(OrgAuditEvent.event_type == "skill_policy_updated"))
    ).scalars().all()
    assert len(events) == 2
    assert json.loads(events[0].after)["acknowledged_warnings"] or json.loads(events[1].after)["acknowledged_warnings"]

    revisions = await client.get(f"{url}/revisions", headers=auth_headers)
    assert [item["revision"] for item in revisions.json()["items"]] == [2, 1]

    preview = await client.post(f"{url}/preview", headers=auth_headers, json={"policy": {"skills": {"mode": "disabled"}}})
    assert preview.status_code == 200, preview.text
    assert "claude-code" in preview.json()["changed_tools"]
    assert any(w["code"] == "claude-code.marketplace_lockdown" for w in preview.json()["warnings"])

    compiled = await client.get(f"{url}/compiled", headers=auth_headers, params={"tool": "claude-code"})
    assert compiled.status_code == 200
    result = compiled.json()["compiled"]["claude-code"]
    assert result["artifacts"][0]["paths"]["linux"] == "/etc/claude-code/managed-settings.json"
    assert "allow_hooks" not in result["enforceable"]  # hooks allowed in revision 2
    assert (await client.get(f"{url}/compiled", headers=auth_headers, params={"tool": "notepad"})).status_code == 422


async def test_members_read_but_only_admins_write(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _enterprise(db_session, test_user)
    member, member_headers = await _user(db_session)
    await _join(db_session, org, member, "member")
    assert (await _put(client, auth_headers, org, NO_SCRIPTS)).status_code == 200
    assert (await client.get(f"/api/v1/orgs/{org.id}/skills/policy", headers=member_headers)).status_code == 200
    denied = await _put(client, member_headers, org, {"mode": "disabled"}, ack=["x"])
    assert denied.status_code == 403
    assert (await client.get(f"/api/v1/orgs/{org.id}/skills/compliance", headers=member_headers)).status_code == 403
    outsider, outsider_headers = await _user(db_session)
    assert (await client.get(f"/api/v1/orgs/{org.id}/skills/policy", headers=outsider_headers)).status_code == 404


async def test_assignment_and_install_request_enforced(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, project = await _enterprise(db_session, test_user)
    scripted = await _publish(client, auth_headers, org=org, script=b"print('x')\n")
    assert (await _put(client, auth_headers, org, NO_SCRIPTS)).status_code == 200

    assign_url = f"/api/v1/orgs/{org.id}/skills/assignments"
    refused = await client.post(
        assign_url, headers=auth_headers,
        json={"version_id": scripted["version_id"], "content_hash": scripted["content_hash"]},
    )
    assert refused.status_code == 403, refused.text
    assert _code(refused) == "org_policy_violation"
    assert "Mod Org skills policy" in refused.json()["error"]["message"]
    assert "Ask an owner or admin" in refused.json()["error"]["message"]

    request = await client.post(
        f"/api/v1/projects/{project.id}/skill-install-requests",
        headers=auth_headers,
        json={
            "version_id": scripted["version_id"],
            "content_hash": scripted["content_hash"],
            "target_scope": "project",
            "requested_capabilities": ["scripts"],
        },
    )
    assert request.status_code == 403 and _code(request) == "org_policy_violation"

    # Downgraded below Enterprise: the last revision stays enforced...
    org.tier = "team"
    await db_session.commit()
    still = await client.post(
        assign_url, headers=auth_headers,
        json={"version_id": scripted["version_id"], "content_hash": scripted["content_hash"]},
    )
    assert still.status_code == 403 and _code(still) == "org_policy_violation", still.text
    # ...until an admin disables it (allowed at any tier).
    etag = (await client.get(f"/api/v1/orgs/{org.id}/skills/policy", headers=auth_headers)).headers["etag"]
    disabled = await client.post(
        f"/api/v1/orgs/{org.id}/skills/policy/disable", headers={**auth_headers, "If-Match": etag}
    )
    assert disabled.status_code == 200, disabled.text
    allowed = await client.post(
        assign_url, headers=auth_headers,
        json={"version_id": scripted["version_id"], "content_hash": scripted["content_hash"]},
    )
    assert allowed.status_code == 201, allowed.text


async def test_reconcile_listing_withholds_assignments_the_current_policy_refuses(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _enterprise(db_session, test_user)
    scripted = await _publish(client, auth_headers, org=org, script=b"print('x')\n")
    plain = await _publish(client, auth_headers, org=org, body="Plain text only.", version="1.0.1")
    assign_url = f"/api/v1/orgs/{org.id}/skills/assignments"
    for pkg in (scripted, plain):
        created = await client.post(
            assign_url, headers=auth_headers, json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"]}
        )
        # plain is a newer version of the same package: assign it to a user instead
        if created.status_code == 409:
            created = await client.post(
                assign_url, headers=auth_headers,
                json={
                    "version_id": pkg["version_id"], "content_hash": pkg["content_hash"],
                    "target_type": "user", "target_id": test_user.id,
                },
            )
        assert created.status_code == 201, created.text

    mine = await client.get("/api/v1/users/me/skills/assignments", headers=auth_headers)
    assert len(mine.json()["items"]) == 2 and mine.json()["policy_refused"] == []

    # The policy arrives after the assignments: the next reconcile withholds
    # the scripted version and says why.
    assert (await _put(client, auth_headers, org, NO_SCRIPTS)).status_code == 200
    mine = await client.get("/api/v1/users/me/skills/assignments", headers=auth_headers)
    body = mine.json()
    assert [item["version_id"] for item in body["items"]] == [plain["version_id"]]
    [refused] = body["policy_refused"]
    assert refused["version_id"] == scripted["version_id"]
    assert refused["policy"]["allowed"] is False
    assert refused["policy"]["policy_revision"] == 1
    assert {v["code"] for v in refused["policy"]["violations"]} == {"scripts_not_allowed"}


async def test_device_policy_reports_and_compliance(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _enterprise(db_session, test_user)
    stranger = User(id="u-stranger-078", email="stranger078@example.com", tier="pro", email_verified=True)
    db_session.add(stranger)
    await db_session.commit()
    other_org, _ = await _enterprise(db_session, stranger)
    assert (await _put(client, auth_headers, org, NO_SCRIPTS)).status_code == 200

    mine = await client.get("/api/v1/devices/me/policy", headers=auth_headers)
    assert mine.status_code == 200
    [item] = mine.json()["items"]
    assert item["org_id"] == org.id and item["revision"] == 1 and item["delivery"] == "advisory"
    claude = item["tools"]["claude-code"]
    assert claude["artifacts"][0]["content_hash"].startswith("sha256:")
    assert "content" not in claude["artifacts"][0]

    report = {
        "device_id": "laptop-1",
        "display_name": "Laptop",
        "platform": "macos",
        "reports": [
            {
                "org_id": org.id,
                "policy_revision": 1,
                "per_tool": {"claude-code": {"enforced_mode": "advisory", "violations": []}},
                "refused": [],
                "noncompliant_installs": [],
            },
            {"org_id": other_org.id, "policy_revision": 1},  # not a member: ignored
        ],
    }
    posted = await client.post("/api/v1/devices/me/policy-report", headers=auth_headers, json=report)
    assert posted.status_code == 202, posted.text
    assert posted.json()["accepted"] == [{"org_id": org.id, "compliant": True}]
    assert posted.json()["ignored"] == [other_org.id]

    bad = await client.post(
        "/api/v1/devices/me/policy-report", headers=auth_headers,
        json={**report, "reports": [{"org_id": org.id, "policy_revision": 1, "per_tool": {"notepad": {}}}]},
    )
    assert bad.status_code == 422

    # A history row from 120 days ago, and a non-compliant later report.
    old = (await db_session.execute(select(DevicePolicyReportHistory))).scalars().one()
    long_ago = datetime.now(timezone.utc) - timedelta(days=120)
    old.reported_at, old.report_day = long_ago, long_ago.date().isoformat()
    await db_session.commit()
    noncompliant = {
        **report,
        "reports": [
            {
                "org_id": org.id,
                "policy_revision": 1,
                "noncompliant_installs": [{"version_id": "v", "package_id": "p", "violations": "scripts_not_allowed"}],
            }
        ],
    }
    assert (await client.post("/api/v1/devices/me/policy-report", headers=auth_headers, json=noncompliant)).status_code == 202
    # The latest report is one row, replaced in place; retention dropped
    # the 120-day-old history row and today's row is the latest report.
    fresh = {"populate_existing": True}
    reports = (await db_session.execute(select(DevicePolicyReport).execution_options(**fresh))).scalars().all()
    assert len(reports) == 1 and reports[0].compliant is False
    history = (
        await db_session.execute(select(DevicePolicyReportHistory).execution_options(**fresh))
    ).scalars().all()
    assert len(history) == 1 and history[0].compliant is False
    assert history[0].report_day == datetime.now(timezone.utc).date().isoformat()
    devices = (await db_session.execute(select(SkillDevice))).scalars().all()
    assert [d.device_key for d in devices] == ["laptop-1"]

    compliance = await client.get(f"/api/v1/orgs/{org.id}/skills/compliance", headers=auth_headers)
    assert compliance.status_code == 200, compliance.text
    body = compliance.json()
    assert body["summary"] == {"devices": 1, "compliant": 0, "stale": 0}
    [device] = body["devices"]
    assert device["device_key"] == "laptop-1" and device["mode"] == "advisory"
    assert device["user_email"] == test_user.email


async def test_service_key_reports_need_devices_report_scope(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _enterprise(db_session, test_user)
    assert (await _put(client, auth_headers, org, NO_SCRIPTS)).status_code == 200
    reader = await _service(db_session, user=test_user, org_id=org.id, scopes=["skills:read"])
    payload = {"device_id": "ci-1", "reports": [{"org_id": org.id, "policy_revision": 1}]}
    assert (await client.post("/api/v1/devices/me/policy-report", headers=reader, json=payload)).status_code == 403
    reporter = await _service(db_session, user=test_user, org_id=org.id, scopes=["devices:report"])
    assert (await client.get("/api/v1/devices/me/policy", headers=reporter)).status_code == 200
    posted = await client.post("/api/v1/devices/me/policy-report", headers=reporter, json=payload)
    assert posted.status_code == 202 and posted.json()["accepted"][0]["org_id"] == org.id


async def test_origin_only_mcp_allowlist_needs_the_sessionfs_mcp_acknowledgement(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _enterprise(db_session, test_user)
    policy = {
        "mode": "open",
        "allow_hooks": True,
        "allow_local_mcp": False,
        "allow_remote_mcp": True,
        "allowed_remote_mcp_origins": ["https://mcp.acme.com"],
    }
    unacked = await _put(client, auth_headers, org, policy)
    assert unacked.status_code == 409 and _code(unacked) == "acknowledgement_required"
    codes = {w["code"] for w in unacked.json()["error"]["details"]["warnings"]}
    assert codes == {"claude-code.sessionfs_mcp_blocked", "copilot.sessionfs_mcp_blocked"}
    saved = await _put(client, auth_headers, org, policy, ack=sorted(codes))
    assert saved.status_code == 200, saved.text


@pytest.fixture
def _roomy_report_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter
    from sessionfs.server.routes import skill_policy as routes

    monkeypatch.setattr(routes, "_report_limiter", SlidingWindowRateLimiter(1000, 3600))


@pytest.mark.usefixtures("_roomy_report_limiter")
async def test_device_reports_are_bounded_per_device_day_and_user(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    from sessionfs.server.routes.skill_policy import MAX_DEVICES_PER_USER

    org, _ = await _enterprise(db_session, test_user)
    assert (await _put(client, auth_headers, org, NO_SCRIPTS)).status_code == 200
    url = "/api/v1/devices/me/policy-report"

    def report(device: str) -> dict:
        return {"device_id": device, "reports": [{"org_id": org.id, "policy_revision": 1}]}

    # Many reports from one device on one day: one latest row, one history row.
    for _ in range(5):
        assert (await client.post(url, headers=auth_headers, json=report("laptop-1"))).status_code == 202
    assert len((await db_session.execute(select(DevicePolicyReport))).scalars().all()) == 1
    assert len((await db_session.execute(select(DevicePolicyReportHistory))).scalars().all()) == 1

    # Up to the per-user cap of devices; one more is refused, nothing evicted.
    for index in range(2, MAX_DEVICES_PER_USER + 1):
        assert (await client.post(url, headers=auth_headers, json=report(f"laptop-{index}"))).status_code == 202
    refused = await client.post(url, headers=auth_headers, json=report("one-too-many"))
    assert refused.status_code == 409 and _code(refused) == "device_limit"
    keys = {d.device_key for d in (await db_session.execute(select(SkillDevice))).scalars().all()}
    assert len(keys) == MAX_DEVICES_PER_USER and "laptop-1" in keys
    # A known device keeps reporting at the cap.
    assert (await client.post(url, headers=auth_headers, json=report("laptop-1"))).status_code == 202

    # A device unseen for the retention window stops counting.
    stale = (
        await db_session.execute(select(SkillDevice).where(SkillDevice.device_key == "laptop-2"))
    ).scalar_one()
    stale.last_seen_at = datetime.now(timezone.utc) - timedelta(days=91)
    await db_session.commit()
    assert (await client.post(url, headers=auth_headers, json=report("one-too-many"))).status_code == 202
    keys = {d.device_key for d in (await db_session.execute(select(SkillDevice))).scalars().all()}
    assert "laptop-2" not in keys and "one-too-many" in keys
    latest = (await db_session.execute(select(DevicePolicyReport))).scalars().all()
    assert len(latest) == MAX_DEVICES_PER_USER  # the pruned device's report went with it

    compliance = (await client.get(f"/api/v1/orgs/{org.id}/skills/compliance", headers=auth_headers)).json()
    assert compliance["summary"]["devices"] == MAX_DEVICES_PER_USER


async def _committed_user(db: AsyncSession) -> tuple[User, dict]:
    """Like _user, but commits the user before its key (PostgreSQL checks
    the api_keys foreign key at insert time)."""
    import uuid

    from sessionfs.server.auth.keys import generate_api_key, hash_api_key
    from sessionfs.server.db.models import ApiKey

    user = User(
        id=str(uuid.uuid4()), email=f"u-{uuid.uuid4().hex[:8]}@example.com", tier="pro",
        email_verified=True, created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    raw = generate_api_key()
    db.add(ApiKey(id=str(uuid.uuid4()), user_id=user.id, key_hash=hash_api_key(raw), name="key",
                  created_at=datetime.now(timezone.utc)))
    await db.commit()
    return user, {"Authorization": f"Bearer {raw}"}


async def test_downgraded_org_keeps_its_policy_and_may_only_relax_it(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _enterprise(db_session, test_user)
    member, member_headers = await _committed_user(db_session)
    await _join(db_session, org, member, "member")
    url = f"/api/v1/orgs/{org.id}/skills/policy"
    strict = {**NO_SCRIPTS, "max_scan_severity": "medium"}
    assert (await _put(client, auth_headers, org, strict)).status_code == 200
    org.tier = "team"
    await db_session.commit()

    # Owners and admins still read it (and see it is no longer entitled);
    # members get the usual upgrade_required.
    got = await client.get(url, headers=auth_headers)
    assert got.status_code == 200 and got.json()["revision"] == 1 and got.json()["entitled"] is False
    assert (await client.get(f"{url}/revisions", headers=auth_headers)).status_code == 200
    assert (await client.get(f"{url}/compiled", headers=auth_headers)).status_code == 200
    member_read = await client.get(url, headers=member_headers)
    assert member_read.status_code == 403 and _code(member_read) == "upgrade_required"

    # The policy still applies to devices.
    mine = (await client.get("/api/v1/devices/me/policy", headers=auth_headers)).json()
    assert [item["org_id"] for item in mine["items"]] == [org.id]

    # Tightening is refused (preview and PUT), naming the fields.
    tighter = {**strict, "mode": "library_only", "max_scan_severity": "low"}
    preview = await client.post(f"{url}/preview", headers=auth_headers, json={"policy": {"skills": tighter}})
    assert preview.status_code == 403 and _code(preview) == "upgrade_required"
    refused = await _put(client, auth_headers, org, tighter)
    assert refused.status_code == 403 and _code(refused) == "upgrade_required"
    assert set(refused.json()["error"]["details"]["tightened_fields"]) == {"mode", "max_scan_severity"}
    added = await _put(client, auth_headers, org, {**strict, "allowed_remote_mcp_origins": ["https://mcp.acme.com"]})
    assert added.status_code == 403

    # Relaxing is allowed.
    relaxed = await _put(client, auth_headers, org, {**strict, "allow_skill_scripts": True, "max_scan_severity": "high"})
    assert relaxed.status_code == 200, relaxed.text
    assert relaxed.json()["revision"] == 2

    # An org that never had a policy cannot start one below Enterprise.
    other, _ = await _org(db_session, test_user)
    first = await _put(client, auth_headers, other, NO_SCRIPTS, etag='"osp-0"')
    assert first.status_code == 403 and _code(first) == "upgrade_required"
    assert (await client.get(f"/api/v1/orgs/{other.id}/skills/policy", headers=auth_headers)).status_code == 403


@pytest.mark.usefixtures("_roomy_report_limiter")
async def test_compliance_pages_devices_and_counts_all_of_them(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sessionfs.server.routes import skill_policy as routes

    monkeypatch.setattr(routes, "COMPLIANCE_PAGE_SIZE", 2)
    org, _ = await _enterprise(db_session, test_user)
    assert (await _put(client, auth_headers, org, NO_SCRIPTS)).status_code == 200
    url = "/api/v1/devices/me/policy-report"
    bad = [{"version_id": "v", "package_id": "p", "violations": "scripts_not_allowed"}]
    for index in range(5):
        report = {"org_id": org.id, "policy_revision": 0 if index == 4 else 1}
        if index == 3:
            report["noncompliant_installs"] = bad
        posted = await client.post(url, headers=auth_headers, json={"device_id": f"pc-{index}", "reports": [report]})
        assert posted.status_code == 202, posted.text

    compliance_url = f"/api/v1/orgs/{org.id}/skills/compliance"
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"cursor": cursor} if cursor else {}
        page = (await client.get(compliance_url, headers=auth_headers, params=params)).json()
        pages += 1
        # Counted over every device, not this page.
        assert page["summary"] == {"devices": 5, "compliant": 3, "stale": 1}
        assert len(page["devices"]) <= 2
        seen += [item["device_key"] for item in page["devices"]]
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        cursor = page["next_cursor"]
    assert pages == 3
    assert sorted(seen) == [f"pc-{i}" for i in range(5)]
