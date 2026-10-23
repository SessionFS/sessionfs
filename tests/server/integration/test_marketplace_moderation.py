"""Marketplace takedown: reports, yank / block / restore, revocations feed."""

from __future__ import annotations

import io
import json
import os
import tarfile
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from tests.pg_helpers import wait_for_lock_waiters
from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    AdminAction,
    ApiKey,
    Organization,
    OrgAuditEvent,
    OrgMember,
    OrgSkillAssignment,
    Project,
    SkillBlockedContentHash,
    SkillConsentReceipt,
    SkillInstallRequest,
    SkillPackageReport,
    SkillRevocationEvent,
    User,
)
from sessionfs.server.routes import marketplace_moderation
from sessionfs.skills.package import AGENT_PLUGIN_SCHEMA

pytestmark = pytest.mark.asyncio


def _tar(body: str = "Inspect the diff and report concrete findings.") -> bytes:
    output = io.BytesIO()
    manifest = {
        "$schema": AGENT_PLUGIN_SCHEMA,
        "name": "review-helper",
        "version": "1.2.3",
        "description": "Review a change using the team contract.",
        "license": "MIT",
    }
    skill = (
        "---\nname: review-helper\n"
        "description: Review changes against the project contract.\n---\n\n"
        f"# Review Helper\n\n{body}\n"
    )
    files = {
        "plugin.json": json.dumps(manifest).encode(),
        "skills/review-helper/SKILL.md": skill.encode(),
    }
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


async def _user(db: AsyncSession, *, tier: str = "pro") -> tuple[User, dict]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"u-{uuid.uuid4().hex[:8]}@example.com",
        display_name="User",
        tier=tier,
        created_at=datetime.now(timezone.utc),
        email_verified=True,
    )
    raw = generate_api_key()
    db.add(user)
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name="key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, {"Authorization": f"Bearer {raw}"}


async def _service(db: AsyncSession, *, user: User, org_id: str, scopes: list[str]) -> dict:
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name="svc",
            key_kind="service",
            org_id=org_id,
            service_key_name="svc",
            scopes=json.dumps(scopes),
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return {"Authorization": f"Bearer {raw}"}


async def _org(db: AsyncSession, owner: User) -> tuple[Organization, Project]:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:16]}",
        name="Mod Org",
        slug=f"mod-{uuid.uuid4().hex[:8]}",
        tier="team",
    )
    project = Project(
        id=f"prj_{uuid.uuid4().hex[:16]}",
        name="mod project",
        git_remote_normalized=f"github.com/example/{uuid.uuid4().hex}",
        owner_id=owner.id,
        org_id=org.id,
    )
    db.add_all([org, project])
    await db.flush()
    db.add(OrgMember(org_id=org.id, user_id=owner.id, role="owner"))
    await db.commit()
    return org, project


async def _join(db: AsyncSession, org: Organization, user: User, role: str) -> None:
    db.add(OrgMember(org_id=org.id, user_id=user.id, role=role))
    await db.commit()


async def _publish_org(
    client: AsyncClient, headers: dict, org: Organization, *, visibility: str = "org",
    namespace: str = "mod", collection: str = "mod", body: str | None = None,
) -> dict:
    response = await client.post(
        f"/api/v1/orgs/{org.id}/skills/packages",
        headers=headers,
        files={"package": ("p.tar.gz", _tar(body) if body else _tar(), "application/gzip")},
        data={"namespace": namespace, "collection": collection, "visibility": visibility},
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _publish_personal(
    client: AsyncClient, headers: dict, *, namespace: str = "mine", collection: str = "mine",
    body: str | None = None,
) -> dict:
    response = await client.post(
        "/api/v1/users/me/skills/packages",
        headers=headers,
        files={"package": ("p.tar.gz", _tar(body) if body else _tar(), "application/gzip")},
        data={"namespace": namespace, "collection": collection},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _act(client: AsyncClient, headers: dict, kind: str, target: str, action: str, reason: str = "takedown"):
    return client.post(
        f"/api/v1/marketplace/{kind}/{target}/{action}", headers=headers, json={"reason": reason}
    )


async def _feed(client: AsyncClient, headers: dict, **params) -> dict:
    response = await client.get("/api/v1/marketplace/revocations", headers=headers, params=params)
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------


async def test_report_visibility_duplicates_validation_and_sanitising(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    pkg = await _publish_personal(client, auth_headers)
    url = f"/api/v1/marketplace/packages/{pkg['package_id']}/reports"
    _, other_headers = await _user(db_session)

    # A personal listing is invisible to anyone else: constant 404.
    hidden = await client.post(url, headers=other_headers, json={"reason": "spam"})
    assert hidden.status_code == 404
    assert (await client.post(
        "/api/v1/marketplace/packages/nope/reports", headers=auth_headers, json={"reason": "spam"}
    )).status_code == 404

    bad_reason = await client.post(url, headers=auth_headers, json={"reason": "bogus"})
    assert bad_reason.status_code == 422
    too_long = await client.post(url, headers=auth_headers, json={"reason": "other", "details": "x" * 2001})
    assert too_long.status_code == 422
    foreign_version = await client.post(
        url, headers=auth_headers, json={"reason": "broken", "version_id": "skv_missing"}
    )
    assert foreign_version.status_code == 404

    created = await client.post(
        url,
        headers=auth_headers,
        json={
            "reason": "malicious",
            "details": "  looks\x00 bad‮​\r\nline two\x1b[31m  ",
            "version_id": pkg["version_id"],
        },
    )
    assert created.status_code == 201, created.text
    report = created.json()
    assert report["details"] == "looks bad\nline two[31m"
    assert report["status"] == "open"
    assert "reporter_user_id" not in report

    duplicate = await client.post(url, headers=auth_headers, json={"reason": "spam"})
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "report_already_open"
    assert duplicate.json()["error"]["details"]["report_id"] == report["id"]

    # Service keys never file reports.
    org, _ = await _org(db_session, test_user)
    service = await _service(db_session, user=test_user, org_id=org.id, scopes=["marketplace:write"])
    refused = await client.post(url, headers=service, json={"reason": "spam"})
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "service_key_not_allowed"


async def test_report_daily_cap_is_durable(
    client: AsyncClient, auth_headers: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(marketplace_moderation, "MAX_REPORTS_PER_DAY", 2)
    packages = [
        await _publish_personal(client, auth_headers, namespace=f"cap{i}", collection=f"cap{i}", body=f"Body {i}.")
        for i in range(3)
    ]
    statuses = []
    for pkg in packages:
        response = await client.post(
            f"/api/v1/marketplace/packages/{pkg['package_id']}/reports",
            headers=auth_headers,
            json={"reason": "spam"},
        )
        statuses.append(response.status_code)
    assert statuses == [201, 201, 429]


async def test_report_burst_limit(
    client: AsyncClient, auth_headers: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sessionfs.server.auth.rate_limit import SlidingWindowRateLimiter

    monkeypatch.setattr(
        marketplace_moderation, "_report_burst_limiter", SlidingWindowRateLimiter(max_requests=1)
    )
    pkg = await _publish_personal(client, auth_headers)
    url = f"/api/v1/marketplace/packages/{pkg['package_id']}/reports"
    assert (await client.post(url, headers=auth_headers, json={"reason": "spam"})).status_code == 201
    limited = await client.post(url, headers=auth_headers, json={"reason": "spam"})
    assert limited.status_code == 429
    assert limited.json()["error"]["code"] == "report_rate_limited"


async def test_report_queue_scoping_and_dismiss(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)
    org_pkg = await _publish_org(client, auth_headers, org)
    admin_user, admin_headers = await _user(db_session)
    member_user, member_headers = await _user(db_session)
    await _join(db_session, org, admin_user, "admin")
    await _join(db_session, org, member_user, "member")
    platform, platform_headers = await _user(db_session, tier="admin")
    public_pkg = await _publish_org(
        client, platform_headers, org, visibility="public", namespace="pub", collection="pub", body="Public."
    )
    outsider, outsider_headers = await _user(db_session)
    outsider_org, _ = await _org(db_session, outsider)

    org_report = await client.post(
        f"/api/v1/marketplace/packages/{org_pkg['package_id']}/reports",
        headers=member_headers,
        json={"reason": "secrets", "details": "token in SKILL.md"},
    )
    assert org_report.status_code == 201, org_report.text
    public_report = await client.post(
        f"/api/v1/marketplace/packages/{public_pkg['package_id']}/reports",
        headers=outsider_headers,
        json={"reason": "impersonation"},
    )
    assert public_report.status_code == 201, public_report.text

    async def queue(headers: dict) -> set[str]:
        response = await client.get("/api/v1/marketplace/reports", headers=headers)
        assert response.status_code == 200, response.text
        return {item["id"] for item in response.json()["items"]}

    org_id, pub_id = org_report.json()["id"], public_report.json()["id"]
    # Owner AND admin both moderate the org listing (owners are never excluded).
    # The public package is org-owned too but listed publicly: org admins do
    # not see public reports, platform admins do.
    assert org_id in await queue(auth_headers)
    assert org_id in await queue(admin_headers)
    assert pub_id not in await queue(admin_headers)
    assert await queue(member_headers) == set()
    assert await queue(outsider_headers) == set()
    assert pub_id in await queue(platform_headers)
    assert org_id not in await queue(platform_headers)

    items = (await client.get("/api/v1/marketplace/reports", headers=admin_headers)).json()["items"]
    assert items[0]["reporter_user_id"] == member_user.id

    # Dismiss: outsider 404, member 404, admin ok, second dismiss 409.
    dismiss_url = f"/api/v1/marketplace/reports/{org_id}/dismiss"
    assert (await client.post(dismiss_url, headers=outsider_headers, json={"note": "no"})).status_code == 404
    assert (await client.post(dismiss_url, headers=member_headers, json={"note": "no"})).status_code == 404
    blank = await client.post(dismiss_url, headers=admin_headers, json={"note": "​\x00"})
    assert blank.status_code == 422
    dismissed = await client.post(dismiss_url, headers=admin_headers, json={"note": "false positive"})
    assert dismissed.status_code == 200, dismissed.text
    assert dismissed.json()["status"] == "dismissed"
    assert dismissed.json()["resolved_by_user_id"] == admin_user.id
    again = await client.post(dismiss_url, headers=admin_headers, json={"note": "x"})
    assert again.status_code == 409
    assert org_id not in await queue(admin_headers)
    all_items = (
        await client.get("/api/v1/marketplace/reports", headers=admin_headers, params={"status": "all"})
    ).json()["items"]
    assert org_id in {item["id"] for item in all_items}
    # After dismissal the reporter may report again.
    again_report = await client.post(
        f"/api/v1/marketplace/packages/{org_pkg['package_id']}/reports",
        headers=member_headers,
        json={"reason": "secrets"},
    )
    assert again_report.status_code == 201
    assert outsider_org  # outsider's own org never widens their view


# ---------------------------------------------------------------------------
# Authorization matrix
# ---------------------------------------------------------------------------


async def test_org_listing_authz_matrix(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)
    pkg = await _publish_org(client, auth_headers, org)
    admin_user, admin_headers = await _user(db_session)
    member_user, member_headers = await _user(db_session)
    await _join(db_session, org, admin_user, "admin")
    await _join(db_session, org, member_user, "member")
    outsider, outsider_headers = await _user(db_session)
    await _org(db_session, outsider)  # outsider owns ANOTHER org
    _, platform_headers = await _user(db_session, tier="admin")
    version = pkg["version_id"]

    # Cross-org: constant 404, never 403.
    assert (await _act(client, outsider_headers, "versions", version, "yank")).status_code == 404
    assert (await _act(client, outsider_headers, "packages", pkg["package_id"], "block")).status_code == 404
    assert (await _act(client, outsider_headers, "versions", "skv_missing", "yank")).status_code == 404
    # Member sees it but cannot act.
    denied = await _act(client, member_headers, "versions", version, "yank")
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "moderation_forbidden"
    # Reason is required and must be visible text.
    assert (await client.post(
        f"/api/v1/marketplace/versions/{version}/yank", headers=admin_headers, json={}
    )).status_code == 422
    assert (await _act(client, admin_headers, "versions", version, "yank", reason="‮\x00")).status_code == 422

    # Admin yanks; yank twice -> 409; owner blocks a yanked version.
    yanked = await _act(client, admin_headers, "versions", version, "yank", reason="broken build")
    assert yanked.status_code == 200, yanked.text
    assert yanked.json()["state"] == "yanked" and yanked.json()["previous_state"] == "active"
    again = await _act(client, admin_headers, "versions", version, "yank")
    assert again.status_code == 409
    assert again.json()["error"]["details"]["current_state"] == "yanked"
    blocked = await _act(client, auth_headers, "versions", version, "block", reason="leaks a token")
    assert blocked.status_code == 200, blocked.text
    assert (await _act(client, auth_headers, "versions", version, "block")).status_code == 409
    # Restore by the org owner (state was set by an org admin, not a platform admin).
    restored = await _act(client, auth_headers, "versions", version, "restore", reason="fixed")
    assert restored.status_code == 200, restored.text
    assert (await _act(client, auth_headers, "versions", version, "restore")).status_code == 409

    # Platform admin can moderate org listings; then org admins cannot restore it.
    assert (await _act(client, platform_headers, "versions", version, "block")).status_code == 200
    forbidden = await _act(client, admin_headers, "versions", version, "restore")
    assert forbidden.status_code == 403
    assert (await _act(client, auth_headers, "versions", version, "restore")).status_code == 403
    assert (await _act(client, platform_headers, "versions", version, "restore")).status_code == 200

    # Service keys never moderate, even with skills:admin in their own org.
    service = await _service(db_session, user=test_user, org_id=org.id, scopes=["skills:admin"])
    refused = await _act(client, service, "versions", version, "yank")
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "service_key_not_allowed"

    # Audit: org events written for every action, admin action for the platform admin.
    events = (
        await db_session.execute(
            select(OrgAuditEvent.event_type).where(OrgAuditEvent.org_id == org.id)
        )
    ).scalars().all()
    assert {"skill_package_yanked", "skill_package_blocked", "skill_package_restored"} <= set(events)
    admin_actions = (await db_session.execute(select(AdminAction.action))).scalars().all()
    assert {"skill_block", "skill_restore"} <= set(admin_actions)


async def test_public_listing_requires_platform_admin(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)  # test_user owns the publishing org
    _, platform_headers = await _user(db_session, tier="admin")
    pkg = await _publish_org(client, platform_headers, org, visibility="public", namespace="pub", collection="pub")
    _, stranger_headers = await _user(db_session)
    # Everyone can SEE a public listing, so non-admins get 403 (not 404).
    assert (await _act(client, auth_headers, "versions", pkg["version_id"], "yank")).status_code == 403
    assert (await _act(client, stranger_headers, "packages", pkg["package_id"], "yank")).status_code == 403
    assert (await _act(client, platform_headers, "packages", pkg["package_id"], "yank")).status_code == 200
    detail = await client.get(f"/api/v1/marketplace/packages/{pkg['package_id']}", headers=auth_headers)
    assert detail.status_code == 404
    assert (await _act(client, auth_headers, "packages", pkg["package_id"], "restore")).status_code == 403
    assert (await _act(client, platform_headers, "packages", pkg["package_id"], "restore")).status_code == 200
    detail = await client.get(f"/api/v1/marketplace/packages/{pkg['package_id']}", headers=auth_headers)
    assert detail.status_code == 200


async def test_personal_listing_owner_may_yank_only_platform_admin_blocks(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    pkg = await _publish_personal(client, auth_headers)
    version = pkg["version_id"]
    _, other_headers = await _user(db_session)
    _, platform_headers = await _user(db_session, tier="admin")

    assert (await _act(client, other_headers, "versions", version, "yank")).status_code == 404
    assert (await _act(client, auth_headers, "versions", version, "block")).status_code == 403
    assert (await _act(client, auth_headers, "versions", version, "yank")).status_code == 200
    assert (await _act(client, auth_headers, "versions", version, "restore")).status_code == 200
    # A platform admin's takedown cannot be undone by the owner.
    assert (await _act(client, platform_headers, "versions", version, "yank")).status_code == 200
    assert (await _act(client, auth_headers, "versions", version, "restore")).status_code == 403
    assert (await _act(client, platform_headers, "versions", version, "block")).status_code == 200
    assert (await _act(client, auth_headers, "versions", version, "restore")).status_code == 403
    assert (await _act(client, platform_headers, "versions", version, "restore")).status_code == 200
    download = await client.get(f"/api/v1/marketplace/versions/{version}/download", headers=auth_headers)
    assert download.status_code == 200


# ---------------------------------------------------------------------------
# Delivery denial and grant revocation
# ---------------------------------------------------------------------------


async def test_block_denies_every_delivery_path_and_revokes_grants(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, project = await _org(db_session, test_user)
    pkg = await _publish_org(client, auth_headers, org)
    version = pkg["version_id"]
    assigned = await client.post(
        f"/api/v1/orgs/{org.id}/skills/assignments",
        headers=auth_headers,
        json={"version_id": version, "content_hash": pkg["content_hash"], "target_type": "org"},
    )
    assert assigned.status_code == 201, assigned.text
    load_url = f"/api/v1/marketplace/versions/{version}/instructions?project_id={project.id}&tool=codex"
    assert (await client.get(load_url, headers=auth_headers)).status_code == 200

    pending = await client.post(
        f"/api/v1/projects/{project.id}/skill-install-requests",
        headers=auth_headers,
        json={"version_id": version, "content_hash": pkg["content_hash"]},
    )
    assert pending.status_code == 201, pending.text
    approved = await client.post(
        f"/api/v1/projects/{project.id}/skill-install-requests",
        headers=auth_headers,
        json={"version_id": version, "content_hash": pkg["content_hash"], "target_tools": ["codex"]},
    )
    assert approved.status_code == 201, approved.text
    approved_row = await db_session.get(SkillInstallRequest, approved.json()["id"])
    assert approved_row is not None
    approved_row.status = "approved"
    db_session.add(
        SkillConsentReceipt(
            id=f"scr_{uuid.uuid4().hex[:24]}",
            version_id=version,
            content_hash=pkg["content_hash"],
            actor_user_id=test_user.id,
            actor_type="user",
            capability="instructions",
            scope="project",
            install_request_id=approved_row.id,
            project_id=project.id,
        )
    )
    await db_session.commit()

    _, member_headers = await _user(db_session)
    report = await client.post(
        f"/api/v1/marketplace/packages/{pkg['package_id']}/reports",
        headers=auth_headers,
        json={"reason": "malicious", "version_id": version},
    )
    assert report.status_code == 201
    package_report = await client.post(
        f"/api/v1/marketplace/packages/{pkg['package_id']}/reports",
        headers=member_headers,
        json={"reason": "spam"},
    )
    assert package_report.status_code == 404  # non-member cannot see the org listing

    blocked = await _act(client, auth_headers, "versions", version, "block", reason="exfiltrates env")
    assert blocked.status_code == 200, blocked.text
    body = blocked.json()
    assert body["grants_revoked"] == {"install_requests": 2, "consent_receipts": 1, "assignments": 1}
    assert body["reports_resolved"] == 1

    for path in (
        f"/api/v1/marketplace/versions/{version}/download",
        f"/api/v1/marketplace/versions/{version}/preview",
        f"/api/v1/marketplace/packages/{pkg['package_id']}",
        load_url,
    ):
        assert (await client.get(path, headers=auth_headers)).status_code == 404, path
    search = await client.get("/api/v1/marketplace/packages", headers=auth_headers)
    assert all(item["version_id"] != version for item in search.json()["items"])
    effective = await client.get("/api/v1/users/me/skills/assignments", headers=auth_headers)
    assert effective.json()["items"] == []
    new_assignment = await client.post(
        f"/api/v1/orgs/{org.id}/skills/assignments",
        headers=auth_headers,
        json={"version_id": version, "content_hash": pkg["content_hash"], "target_type": "user", "target_id": test_user.id},
    )
    assert new_assignment.status_code == 404
    approve = await client.post(
        f"/api/v1/projects/{project.id}/skill-install-requests/{pending.json()['id']}/approve",
        headers=auth_headers,
    )
    assert approve.status_code == 409  # rejected by the block, never approvable
    new_request = await client.post(
        f"/api/v1/projects/{project.id}/skill-install-requests",
        headers=auth_headers,
        json={"version_id": version, "content_hash": pkg["content_hash"]},
    )
    assert new_request.status_code == 404

    # History is marked, never deleted.
    org_id = org.id
    db_session.expire_all()
    rows = {
        row.id: row.status
        for row in (await db_session.execute(select(SkillInstallRequest))).scalars().all()
    }
    assert rows == {pending.json()["id"]: "rejected", approved.json()["id"]: "revoked"}
    receipt = (await db_session.execute(select(SkillConsentReceipt))).scalars().one()
    assert receipt.revoked_at is not None
    assignment = (await db_session.execute(select(OrgSkillAssignment))).scalars().one()
    assert assignment.revoked_at is not None
    listed = await client.get(f"/api/v1/orgs/{org_id}/skills/assignments", headers=auth_headers)
    assert listed.json()["items"][0]["revoked_at"] is not None
    report_row = await db_session.get(SkillPackageReport, report.json()["id"])
    assert report_row is not None and report_row.status == "actioned"

    # Restore: deliverable again, but the revoked grants stay revoked.
    assert (await _act(client, auth_headers, "versions", version, "restore")).status_code == 200
    assert (await client.get(f"/api/v1/marketplace/versions/{version}/download", headers=auth_headers)).status_code == 200
    reload = await client.get(load_url, headers=auth_headers)
    assert reload.status_code == 403
    assert reload.json()["error"]["code"] == "install_approval_required"
    # A fresh assignment is allowed beside the revoked one.
    reassigned = await client.post(
        f"/api/v1/orgs/{org_id}/skills/assignments",
        headers=auth_headers,
        json={"version_id": version, "content_hash": pkg["content_hash"], "target_type": "org"},
    )
    assert reassigned.status_code == 201, reassigned.text
    assert (await client.get(load_url, headers=auth_headers)).status_code == 200


async def test_platform_yank_is_version_wide_owner_yank_is_listing_scoped(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    _, platform_headers = await _user(db_session, tier="admin")
    pkg = await _publish_personal(client, auth_headers, collection="first")
    version = pkg["version_id"]
    download = f"/api/v1/marketplace/versions/{version}/download"
    # Platform yank: re-listing the same immutable version in another
    # collection never revives it.
    assert (await _act(client, platform_headers, "versions", version, "yank")).status_code == 200
    relisted = await _publish_personal(client, auth_headers, collection="second")
    assert relisted["version_id"] == version
    assert (await client.get(download, headers=auth_headers)).status_code == 404
    assert (await _act(client, auth_headers, "versions", version, "restore")).status_code == 403
    assert (await _act(client, platform_headers, "versions", version, "restore")).status_code == 200
    assert (await client.get(download, headers=auth_headers)).status_code == 200
    # Owner yank: only the owner's existing listings; the owner may re-list.
    assert (await _act(client, auth_headers, "versions", version, "yank")).status_code == 200
    assert (await client.get(download, headers=auth_headers)).status_code == 404
    await _publish_personal(client, auth_headers, collection="third")
    assert (await client.get(download, headers=auth_headers)).status_code == 200


async def test_blocked_bytes_cannot_be_republished_anywhere_for_a_global_block(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    _, platform_headers = await _user(db_session, tier="admin")
    pkg = await _publish_personal(client, auth_headers, namespace="orig")
    # A copy of the same bytes that existed BEFORE the block.
    early_copy = await _publish_personal(client, auth_headers, namespace="early", collection="early")
    assert early_copy["content_hash"] == pkg["content_hash"]
    assert (await _act(client, platform_headers, "versions", pkg["version_id"], "block")).status_code == 200

    refused = await client.post(
        "/api/v1/users/me/skills/packages",
        headers=auth_headers,
        files={"package": ("p.tar.gz", _tar(), "application/gzip")},
        data={"namespace": "copy", "collection": "copy"},
    )
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "content_blocked"
    for path in ("download", "preview"):
        response = await client.get(
            f"/api/v1/marketplace/versions/{early_copy['version_id']}/{path}", headers=auth_headers
        )
        assert response.status_code == 404, path

    # The owner cannot lift a platform admin's hash block through the copy.
    assert (await _act(client, auth_headers, "versions", early_copy["version_id"], "restore")).status_code == 403
    # The copy is blocked only by the original's block: restoring the copy
    # is refused and names nothing; restoring the original lifts it everywhere.
    via_copy = await _act(client, platform_headers, "versions", early_copy["version_id"], "restore")
    assert via_copy.status_code == 409, via_copy.text
    assert via_copy.json()["error"]["code"] == "blocked_by_other_target"
    assert pkg["version_id"] not in via_copy.text and pkg["package_id"] not in via_copy.text
    lifted = await _act(client, platform_headers, "versions", pkg["version_id"], "restore")
    assert lifted.status_code == 200, lifted.text
    assert lifted.json()["previous_state"] == "blocked"
    download = await client.get(
        f"/api/v1/marketplace/versions/{early_copy['version_id']}/download", headers=auth_headers
    )
    assert download.status_code == 200


async def test_blocked_version_cannot_be_deleted_and_bytes_stay_blocked(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)
    pkg = await _publish_org(client, auth_headers, org)
    blocked = await _act(client, auth_headers, "versions", pkg["version_id"], "block", reason="leaks a key")
    assert blocked.status_code == 200, blocked.text

    delete_url = f"/api/v1/orgs/{org.id}/skills/packages/{pkg['package_id']}/versions/{pkg['version_id']}"
    refused = await client.delete(delete_url, headers=auth_headers)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "active_block"
    assert "restore" in refused.json()["error"]["message"]

    # Identical bytes, same package identity or a new one: refused.
    for namespace in ("mod", "renamed"):
        again = await client.post(
            f"/api/v1/orgs/{org.id}/skills/packages",
            headers=auth_headers,
            files={"package": ("p.tar.gz", _tar(), "application/gzip")},
            data={"namespace": namespace, "collection": namespace, "visibility": "org"},
        )
        assert again.status_code == 403, again.text
        assert again.json()["error"]["code"] == "content_blocked"

    # The revocation history survives the delete: event row, blocklist row, feed.
    events = (
        await db_session.execute(
            select(SkillRevocationEvent).where(SkillRevocationEvent.version_id == pkg["version_id"])
        )
    ).scalars().all()
    assert [event.state for event in events] == ["blocked"]
    blocks = (
        await db_session.execute(
            select(SkillBlockedContentHash).where(
                SkillBlockedContentHash.content_hash == pkg["content_hash"]
            )
        )
    ).scalars().all()
    assert len(blocks) == 1
    assert blocks[0].lifted_at is None and blocks[0].scope_org_id == org.id
    assert blocks[0].source_version_id == pkg["version_id"]
    feed = await _feed(client, auth_headers)
    assert [(i["version_id"], i["state"], i["kind"]) for i in feed["revocations"]] == [
        (pkg["version_id"], "blocked", "version"),
        (None, "blocked", "content"),
    ]

    # An org admin's block is confined to that org: the same bytes stay
    # publishable and deliverable as someone else's personal package.
    _, other_headers = await _user(db_session)
    personal = await _publish_personal(client, other_headers, namespace="theirs")
    assert personal["content_hash"] == pkg["content_hash"]
    download = await client.get(
        f"/api/v1/marketplace/versions/{personal['version_id']}/download", headers=other_headers
    )
    assert download.status_code == 200


async def test_whole_package_block_refuses_version_delete_until_restored(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)
    pkg = await _publish_org(client, auth_headers, org)
    assert (await _act(client, auth_headers, "packages", pkg["package_id"], "block")).status_code == 200
    delete_url = f"/api/v1/orgs/{org.id}/skills/packages/{pkg['package_id']}/versions/{pkg['version_id']}"
    refused = await client.delete(delete_url, headers=auth_headers)
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "active_block"
    again = await client.post(
        f"/api/v1/orgs/{org.id}/skills/packages",
        headers=auth_headers,
        files={"package": ("p.tar.gz", _tar(), "application/gzip")},
        data={"namespace": "elsewhere", "collection": "elsewhere", "visibility": "org"},
    )
    assert again.status_code == 403
    assert again.json()["error"]["code"] == "content_blocked"
    # After a moderator restores the package, the version can be deleted.
    assert (await _act(client, auth_headers, "packages", pkg["package_id"], "restore")).status_code == 200
    assert (await client.delete(delete_url, headers=auth_headers)).status_code == 204


async def test_report_scope_binds_to_what_the_reporter_saw_mixed_public_and_org(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """One package, version A listed org-private, version B listed public.
    Org admins moderate only reports bound to org-only scope; anything
    filed against a public listing (or the package as a whole, which the
    reporter saw publicly) belongs to platform admins."""
    org, _ = await _org(db_session, test_user)
    admin_user, admin_headers = await _user(db_session)
    member_user, member_headers = await _user(db_session)
    await _join(db_session, org, admin_user, "admin")
    await _join(db_session, org, member_user, "member")
    _, platform_headers = await _user(db_session, tier="admin")
    _, outsider_headers = await _user(db_session)

    private_version = await _publish_org(client, auth_headers, org, body="Private A.")
    public_version = await _publish_org(
        client, platform_headers, org, visibility="public", collection="pubmix", body="Public B."
    )
    assert private_version["package_id"] == public_version["package_id"]
    package_id = private_version["package_id"]
    reports_url = f"/api/v1/marketplace/packages/{package_id}/reports"

    on_public = await client.post(
        reports_url, headers=outsider_headers,
        json={"reason": "impersonation", "details": "outsider detail", "version_id": public_version["version_id"]},
    )
    assert on_public.status_code == 201, on_public.text
    on_private = await client.post(
        reports_url, headers=member_headers,
        json={"reason": "secrets", "version_id": private_version["version_id"]},
    )
    assert on_private.status_code == 201, on_private.text
    # Outsiders cannot report the org-private version at all.
    _, stranger_headers = await _user(db_session)
    hidden = await client.post(
        reports_url, headers=stranger_headers,
        json={"reason": "spam", "version_id": private_version["version_id"]},
    )
    assert hidden.status_code == 404
    whole = await client.post(reports_url, headers=admin_headers, json={"reason": "broken"})
    assert whole.status_code == 201, whole.text  # saw both listings -> platform scope

    async def queue(headers: dict) -> dict[str, dict]:
        response = await client.get(
            "/api/v1/marketplace/reports", headers=headers, params={"status": "all"}
        )
        assert response.status_code == 200, response.text
        return {item["id"]: item for item in response.json()["items"]}

    public_id, private_id, whole_id = on_public.json()["id"], on_private.json()["id"], whole.json()["id"]
    admin_queue = await queue(admin_headers)
    assert set(admin_queue) == {private_id}
    assert "outsider detail" not in json.dumps(admin_queue)
    assert set(await queue(auth_headers)) == {private_id}
    assert set(await queue(platform_headers)) == {public_id, whole_id}
    assert await queue(member_headers) == {}

    # Dismiss is bound to the same scope.
    for report_id in (public_id, whole_id):
        refused = await client.post(
            f"/api/v1/marketplace/reports/{report_id}/dismiss", headers=admin_headers, json={"note": "x"}
        )
        assert refused.status_code == 404
    rows = {
        row.id: (row.status, row.scope_kind, row.scope_org_id)
        for row in (await db_session.execute(select(SkillPackageReport))).scalars().all()
    }
    assert rows[public_id] == ("open", "platform", None)
    assert rows[whole_id] == ("open", "platform", None)
    assert rows[private_id] == ("open", "org", org.id)

    # An org admin acting on the org-only version resolves only the org report.
    yanked = await _act(client, admin_headers, "versions", private_version["version_id"], "yank")
    assert yanked.status_code == 200, yanked.text
    assert yanked.json()["reports_resolved"] == 1
    # An org admin's whole-package action is listing-scoped: it hides the
    # org's non-public listings, never the public one, and resolves only
    # reports bound to the org scope (none are left).
    org_yank = await _act(client, admin_headers, "packages", package_id, "yank")
    assert org_yank.status_code == 200, org_yank.text
    assert org_yank.json()["reports_resolved"] == 0
    public_download = f"/api/v1/marketplace/versions/{public_version['version_id']}/download"
    assert (await client.get(public_download, headers=outsider_headers)).status_code == 200
    # A platform admin's whole-package action resolves the rest.
    acted = await _act(client, platform_headers, "packages", package_id, "yank")
    assert acted.status_code == 200, acted.text
    assert acted.json()["reports_resolved"] == 2
    db_session.expire_all()
    statuses = {
        row.id: row.status
        for row in (await db_session.execute(select(SkillPackageReport))).scalars().all()
    }
    assert statuses == {public_id: "actioned", private_id: "actioned", whole_id: "actioned"}


async def test_whole_package_actions(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)
    first = await _publish_org(client, auth_headers, org, body="First.")
    second = await _publish_org(client, auth_headers, org, body="Second.")
    assert first["package_id"] == second["package_id"]
    package_id = first["package_id"]
    # One active assignment per (org, package, target): the two versions go
    # to different targets.
    for pkg, target in ((first, {"target_type": "org"}),
                        (second, {"target_type": "user", "target_id": test_user.id})):
        response = await client.post(
            f"/api/v1/orgs/{org.id}/skills/assignments",
            headers=auth_headers,
            json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"], **target},
        )
        assert response.status_code == 201, response.text

    assert (await _act(client, auth_headers, "packages", package_id, "yank")).status_code == 200
    for pkg in (first, second):
        assert (await client.get(f"/api/v1/marketplace/versions/{pkg['version_id']}/download", headers=auth_headers)).status_code == 404
    assert (await _act(client, auth_headers, "packages", package_id, "yank")).status_code == 409
    blocked = await _act(client, auth_headers, "packages", package_id, "block")
    assert blocked.status_code == 200
    assert blocked.json()["previous_state"] == "yanked"
    assert blocked.json()["grants_revoked"]["assignments"] == 2
    assert blocked.json()["version_id"] is None
    restored = await _act(client, auth_headers, "packages", package_id, "restore")
    assert restored.status_code == 200
    assert restored.json()["previous_state"] == "blocked"
    assert (await client.get(f"/api/v1/marketplace/packages/{package_id}", headers=auth_headers)).status_code == 200


# ---------------------------------------------------------------------------
# Revocations feed
# ---------------------------------------------------------------------------


async def test_revocations_feed_contract_scoping_and_pagination(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)
    _, platform_headers = await _user(db_session, tier="admin")
    outsider, outsider_headers = await _user(db_session)
    member_user, member_headers = await _user(db_session)
    await _join(db_session, org, member_user, "member")

    public_pkg = await _publish_org(client, platform_headers, org, visibility="public", namespace="pub", collection="pub", body="Pub.")
    org_pkg = await _publish_org(client, auth_headers, org, body="Org.")
    personal_pkg = await _publish_personal(client, auth_headers, body="Mine.")

    assert (await _act(client, platform_headers, "versions", public_pkg["version_id"], "yank", reason="r1")).status_code == 200
    assert (await _act(client, auth_headers, "versions", org_pkg["version_id"], "block", reason="r2")).status_code == 200
    assert (await _act(client, auth_headers, "versions", personal_pkg["version_id"], "yank", reason="r3")).status_code == 200
    assert (await _act(client, platform_headers, "packages", public_pkg["package_id"], "yank", reason="r4")).status_code == 200
    assert (await _act(client, platform_headers, "versions", public_pkg["version_id"], "restore", reason="r5")).status_code == 200

    def labels(page: dict) -> list[str]:
        return [f"{item['reason']}:{item['kind']}" for item in page["revocations"]]

    full = await _feed(client, auth_headers)
    assert set(full) == {"revocations", "next_since", "has_more"}
    assert full["has_more"] is False
    # The org admin's block (r2) also emits an org-scoped content event.
    expected = ["r1:version", "r2:version", "r2:content", "r3:version", "r4:package", "r5:version"]
    assert labels(full) == expected
    for item in full["revocations"]:
        assert set(item) == {
            "package_id", "version_id", "content_hash", "state", "reason", "changed_at", "kind", "block_scope",
            "scope_org_id",
        }
    by_label = dict(zip(labels(full), full["revocations"]))
    assert [item["state"] for item in full["revocations"]] == [
        "yanked", "blocked", "blocked", "yanked", "yanked", "restored",
    ]
    # r3 is the personal owner's yank: owner layer, no org.
    assert [item["block_scope"] for item in full["revocations"]] == [None, "org", "org", "owner", None, None]
    assert [item["scope_org_id"] for item in full["revocations"]] == [None, org.id, org.id, None, None, None]
    whole = by_label["r4:package"]
    assert whole["version_id"] is None and whole["content_hash"] is None
    assert by_label["r2:version"]["content_hash"] == org_pkg["content_hash"]
    content = by_label["r2:content"]
    assert content["package_id"] is None and content["version_id"] is None
    assert content["content_hash"] == org_pkg["content_hash"]
    stamps = [datetime.fromisoformat(item["changed_at"]) for item in full["revocations"]]
    assert stamps == sorted(stamps) and len(set(stamps)) == len(stamps)
    assert all(stamp.tzinfo is not None for stamp in stamps)
    assert full["next_since"] == full["revocations"][-1]["changed_at"]

    # Scoping: a member sees public + org (incl. the org content block) but not
    # the owner's personal event; an outsider sees only public events.
    assert labels(await _feed(client, member_headers)) == [
        "r1:version", "r2:version", "r2:content", "r4:package", "r5:version",
    ]
    assert labels(await _feed(client, outsider_headers)) == ["r1:version", "r4:package", "r5:version"]

    # Pagination: limit=1 walks every event once; has_more until the last page;
    # an empty page echoes the cursor.
    seen: list[str] = []
    since = None
    for _ in range(12):
        params = {"limit": 1}
        if since is not None:
            params["since"] = since
        page = await _feed(client, auth_headers, **params)
        if not page["revocations"]:
            assert page["next_since"] == since and page["has_more"] is False
            break
        seen.extend(labels(page))
        assert page["has_more"] is (len(seen) < len(expected))
        since = page["next_since"]
    assert seen == expected
    # A 'Z' suffix and other offsets are accepted; garbage is a 422.
    zulu = full["revocations"][2]["changed_at"].replace("+00:00", "Z")
    assert labels(await _feed(client, auth_headers, since=zulu)) == ["r3:version", "r4:package", "r5:version"]
    assert (await client.get("/api/v1/marketplace/revocations", headers=auth_headers, params={"since": "yesterday"})).status_code == 422
    assert (await client.get("/api/v1/marketplace/revocations", headers=auth_headers, params={"limit": 0})).status_code == 422
    empty = await _feed(client, outsider_headers, since=full["next_since"])
    assert empty == {"revocations": [], "next_since": full["next_since"], "has_more": False}
    fresh = await _feed(client, outsider_headers, since="2999-01-01T00:00:00+00:00")
    assert fresh["revocations"] == [] and fresh["next_since"] == "2999-01-01T00:00:00+00:00"

    # Service keys: skills:read required; scoped to public + their org.
    service = await _service(db_session, user=test_user, org_id=org.id, scopes=["skills:read"])
    assert labels(await _feed(client, service)) == [
        "r1:version", "r2:version", "r2:content", "r4:package", "r5:version",
    ]
    no_scope = await _service(db_session, user=test_user, org_id=org.id, scopes=["marketplace:read"])
    assert (await client.get("/api/v1/marketplace/revocations", headers=no_scope)).status_code == 403
    assert outsider  # outsider holds no org


async def test_global_content_block_reaches_every_tenant_without_disclosing_the_package(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """A platform admin blocks a PRIVATE personal version. Everyone gets a
    `content` event (hash only); only callers who can see the moderated
    listing get the moderator's reason."""
    _, platform_headers = await _user(db_session, tier="admin")
    _, stranger_headers = await _user(db_session)
    stranger_org_owner, stranger_org_headers = await _user(db_session)
    stranger_org, _ = await _org(db_session, stranger_org_owner)
    stranger_service = await _service(
        db_session, user=stranger_org_owner, org_id=stranger_org.id, scopes=["skills:read"]
    )
    pkg = await _publish_personal(client, auth_headers, namespace="secret-project")
    reason = "contains secret-project credentials"
    blocked = await _act(client, platform_headers, "versions", pkg["version_id"], "block", reason=reason)
    assert blocked.status_code == 200, blocked.text

    for headers in (stranger_headers, stranger_org_headers, stranger_service):
        page = await _feed(client, headers)
        assert page["revocations"] == [
            {
                "package_id": None,
                "version_id": None,
                "content_hash": pkg["content_hash"],
                "state": "blocked",
                "reason": "Blocked by SessionFS moderation",
                "changed_at": page["revocations"][0]["changed_at"],
                "kind": "content",
                "block_scope": "global",
                "scope_org_id": None,
            }
        ]
        assert pkg["package_id"] not in json.dumps(page) and "secret-project" not in json.dumps(page)

    owner_page = await _feed(client, auth_headers)
    assert [(i["kind"], i["reason"], i["block_scope"]) for i in owner_page["revocations"]] == [
        ("version", reason, "global"),
        ("content", reason, "global"),
    ]
    admin_page = await _feed(client, platform_headers)
    assert [(i["kind"], i["reason"]) for i in admin_page["revocations"]] == [("content", reason)]

    restored = await _act(client, platform_headers, "versions", pkg["version_id"], "restore", reason="cleared")
    assert restored.status_code == 200, restored.text
    after = await _feed(client, stranger_headers, since=(await _feed(client, stranger_headers))["revocations"][0]["changed_at"])
    assert [(i["kind"], i["state"], i["block_scope"], i["reason"]) for i in after["revocations"]] == [
        ("content", "restored", "global", "Blocked by SessionFS moderation"),
    ]
    owner_after = await _feed(client, auth_headers, since=owner_page["next_since"])
    assert [(i["kind"], i["state"], i["block_scope"]) for i in owner_after["revocations"]] == [
        ("version", "restored", "global"),
        ("content", "restored", "global"),
    ]


async def test_hash_block_revokes_grants_on_every_version_with_those_bytes(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    _, platform_headers = await _user(db_session, tier="admin")
    org_a, project_a = await _org(db_session, test_user)
    owner_b, owner_b_headers = await _user(db_session)
    org_b, _ = await _org(db_session, owner_b)

    public = await _publish_org(client, platform_headers, org_a, visibility="public", namespace="pub", collection="pub")
    copy_b = await _publish_org(client, owner_b_headers, org_b, namespace="copyb", collection="copyb")
    copy_a = await _publish_org(client, auth_headers, org_a, namespace="copya", collection="copya")
    assert public["content_hash"] == copy_b["content_hash"] == copy_a["content_hash"]
    assert len({public["version_id"], copy_b["version_id"], copy_a["version_id"]}) == 3

    async def assign(headers: dict, org: Organization, pkg: dict) -> str:
        response = await client.post(
            f"/api/v1/orgs/{org.id}/skills/assignments",
            headers=headers,
            json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"], "target_type": "org"},
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    a_public = await assign(auth_headers, org_a, public)
    a_copy = await assign(auth_headers, org_a, copy_a)
    b_copy = await assign(owner_b_headers, org_b, copy_b)
    request = await client.post(
        f"/api/v1/projects/{project_a.id}/skill-install-requests",
        headers=auth_headers,
        json={"version_id": copy_a["version_id"], "content_hash": copy_a["content_hash"]},
    )
    assert request.status_code == 201, request.text

    # An org admin's block on org B's copy stays inside org B.
    scoped = await _act(client, owner_b_headers, "versions", copy_b["version_id"], "block", reason="b only")
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["affected_version_ids"] == [copy_b["version_id"]]
    assert scoped.json()["grants_revoked"]["assignments"] == 1

    # A platform admin's block on the public version reaches every copy.
    blocked = await _act(client, platform_headers, "versions", public["version_id"], "block", reason="global")
    assert blocked.status_code == 200, blocked.text
    body = blocked.json()
    assert set(body["affected_version_ids"]) == {public["version_id"], copy_a["version_id"], copy_b["version_id"]}
    assert body["grants_revoked"] == {"install_requests": 1, "consent_receipts": 0, "assignments": 2}
    db_session.expire_all()
    revoked = {
        row.id: row.revoked_at is not None
        for row in (await db_session.execute(select(OrgSkillAssignment))).scalars().all()
    }
    assert revoked == {a_public: True, a_copy: True, b_copy: True}
    status = (await db_session.execute(select(SkillInstallRequest.status))).scalars().all()
    assert status == ["rejected"]
    # The audit records the whole affected set.
    audit = (
        await db_session.execute(
            select(AdminAction.details).where(AdminAction.action == "skill_block")
        )
    ).scalars().all()
    assert any(copy_b["version_id"] in details for details in audit)

    # Restore never re-grants anything, anywhere.
    restored = await _act(client, platform_headers, "versions", public["version_id"], "restore")
    assert restored.status_code == 200, restored.text
    db_session.expire_all()
    assert all(
        row.revoked_at is not None
        for row in (await db_session.execute(select(OrgSkillAssignment))).scalars().all()
    )


async def test_grant_paths_take_the_content_lock_before_checking_deliverability(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deterministic ordering: assignment, install request and approval all
    acquire the version + content-hash lock BEFORE their deliverability check,
    so a concurrent block can never slip between check and insert."""
    from sessionfs.server.routes import marketplace as marketplace_routes

    org, project = await _org(db_session, test_user)
    pkg = await _publish_org(client, auth_headers, org)
    calls: list[str] = []
    real_lock = marketplace_routes.lock_content_hashes
    real_listings = marketplace_routes._visible_listings
    real_row = marketplace_routes._visible_row

    async def lock(db, hashes):
        calls.append("lock")
        return await real_lock(db, hashes)

    async def listings(*args, **kwargs):
        calls.append("check")
        return await real_listings(*args, **kwargs)

    async def row(*args, **kwargs):
        calls.append("check")
        return await real_row(*args, **kwargs)

    monkeypatch.setattr(marketplace_routes, "lock_content_hashes", lock)
    monkeypatch.setattr(marketplace_routes, "_visible_listings", listings)
    monkeypatch.setattr(marketplace_routes, "_visible_row", row)

    assigned = await client.post(
        f"/api/v1/orgs/{org.id}/skills/assignments",
        headers=auth_headers,
        json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"], "target_type": "org"},
    )
    assert assigned.status_code == 201, assigned.text
    assert calls == ["lock", "check"]

    calls.clear()
    created = await client.post(
        f"/api/v1/projects/{project.id}/skill-install-requests",
        headers=auth_headers,
        json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"]},
    )
    assert created.status_code == 201, created.text
    assert calls[:2] == ["lock", "check"]

    calls.clear()
    second_user, second_headers = await _user(db_session)
    await _join(db_session, org, second_user, "admin")
    approved = await client.post(
        f"/api/v1/projects/{project.id}/skill-install-requests/{created.json()['id']}/approve",
        headers=second_headers,
    )
    assert approved.status_code == 200, approved.text
    assert calls[:2] == ["lock", "check"]


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database for the two-connection race",
)
async def test_assignment_waits_for_a_concurrent_block_on_postgres() -> None:
    """Two real PostgreSQL connections: a block holds the content-hash lock
    with its blocklist row uncommitted; an assignment for another version
    with the SAME bytes must wait, then see the block and refuse (404)."""
    import asyncio

    from fastapi import HTTPException
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sessionfs.server.auth.dependencies import AuthContext
    from sessionfs.server.db.models import (
        Base,
        SkillBlockedContentHash,
        SkillMarketplaceListing,
        SkillPackage,
        SkillPackageVersion,
    )
    from sessionfs.server.routes import marketplace as marketplace_routes
    from sessionfs.server.routes.marketplace import AssignmentCreate, create_org_assignment

    schema = f"race_{uuid.uuid4().hex[:8]}"
    admin_engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    async with admin_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        os.environ["SFS_TEST_PG_URL"], connect_args={"server_settings": {"search_path": schema}}
    )
    blocker = None
    task: asyncio.Task | None = None
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        content_hash = "sha256:" + "ab" * 32
        async with factory() as db:
            owner = User(id="owner", email="o@example.com", tier="pro", created_at=datetime.now(timezone.utc))
            org = Organization(id="org1", name="Org", slug="org1", tier="team")
            db.add_all([owner, org])
            await db.flush()
            db.add(OrgMember(org_id="org1", user_id="owner", role="owner"))
            for suffix in ("a", "b"):
                db.add(SkillPackage(id=f"pk{suffix}", owner_type="org", owner_id="org1", org_id="org1",
                                    namespace="ns", name=f"n{suffix}", display_name="N",
                                    source_type="hosted-upload", source_locator=suffix))
            await db.flush()
            for suffix in ("a", "b"):
                db.add(SkillPackageVersion(id=f"v{suffix}", package_id=f"pk{suffix}", content_hash=content_hash,
                                           agent_plugin_schema="s", manifest_json="{}", dlp_status="passed",
                                           blob_key=f"k{suffix}", blob_size_bytes=1))
            await db.flush()
            for suffix in ("a", "b"):
                db.add(SkillMarketplaceListing(id=f"l{suffix}", package_id=f"pk{suffix}", version_id=f"v{suffix}",
                                               org_id="org1", name="n", visibility="org",
                                               verification_state="unverified"))
            await db.commit()

        auth = AuthContext(
            user=User(id="owner", email="o@example.com", tier="pro"), api_key_id="k",
            key_kind="user", scopes=["*"],
        )
        # Connection 1: the blocker takes the content-hash lock and writes an
        # uncommitted blocklist row for the shared bytes.
        blocker = factory()
        await marketplace_routes.lock_content_hashes(blocker, [content_hash])
        blocker.add(SkillBlockedContentHash(
            content_hash=content_hash, scope_org_id="org1", source_package_id="pka",
            source_version_id="va", reason="r", actor_role="org_admin",
            blocked_at=datetime.now(timezone.utc),
        ))
        await blocker.flush()

        async def assign() -> object:
            async with factory() as db:
                try:
                    return await create_org_assignment(
                        "org1",
                        AssignmentCreate(version_id="vb", content_hash=content_hash, target_type="org"),
                        # Only the blob store is read from the request, and
                        # the refused assignment never reaches it.
                        request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(blob_store=None))),
                        auth=auth,
                        db=db,
                    )
                except HTTPException as exc:
                    return exc.status_code

        task = asyncio.create_task(assign())
        await wait_for_lock_waiters(engine, task=task)
        assert not task.done(), "assignment did not wait for the content-hash lock"
        await blocker.commit()
        assert await asyncio.wait_for(task, timeout=10) == 404
        async with factory() as db:
            assert (await db.execute(select(OrgSkillAssignment))).scalars().all() == []
    finally:
        # Release the blocker's transaction first, or the schema drop below
        # would wait on its locks forever after a failed assertion.
        if blocker is not None:
            await blocker.rollback()
            await blocker.close()
        if task is not None and not task.done():
            await asyncio.wait_for(task, timeout=10)
        await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


async def test_feed_cursor_does_not_skip_events_with_equal_wall_clock(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Writers that observe the same (or a backwards) wall clock still get
    strictly increasing changed_at values, so `changed_at > since` paging is exact."""
    frozen = datetime(2026, 1, 1, tzinfo=timezone.utc)

    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):  # type: ignore[override]
            return frozen

    monkeypatch.setattr(marketplace_moderation, "datetime", _Clock)
    packages = [
        await _publish_personal(client, auth_headers, namespace=f"clk{i}", collection=f"clk{i}", body=f"Clock {i}.")
        for i in range(3)
    ]
    for pkg in packages:
        assert (await _act(client, auth_headers, "versions", pkg["version_id"], "yank")).status_code == 200
    rows = (
        await db_session.execute(select(SkillRevocationEvent).order_by(SkillRevocationEvent.id))
    ).scalars().all()
    stamps = [row.changed_at for row in rows]
    assert len(set(stamps)) == 3 and stamps == sorted(stamps)
    since = None
    seen = []
    for _ in range(5):
        params = {"limit": 1, **({"since": since} if since else {})}
        page = await _feed(client, auth_headers, **params)
        if not page["revocations"]:
            break
        seen.append(page["revocations"][0]["version_id"])
        since = page["next_since"]
    assert seen == [pkg["version_id"] for pkg in packages]


async def test_feed_scope_org_id_and_owner_org_id(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """An org-scoped block names its org on every entry it writes (version and
    content), and package responses expose the owning org so a client can
    match it; global blocks and yanks carry scope_org_id null."""
    org, _ = await _org(db_session, test_user)
    _, platform_headers = await _user(db_session, tier="admin")
    org_pkg = await _publish_org(client, auth_headers, org)
    personal = await _publish_personal(client, auth_headers, namespace="solo", body="Solo.")

    detail = await client.get(f"/api/v1/marketplace/packages/{org_pkg['package_id']}", headers=auth_headers)
    assert detail.json()["owner_org_id"] == org.id
    search = await client.get("/api/v1/marketplace/packages", headers=auth_headers)
    owners = {item["package_id"]: item["owner_org_id"] for item in search.json()["items"]}
    assert owners == {org_pkg["package_id"]: org.id, personal["package_id"]: None}

    assert (await _act(client, auth_headers, "versions", org_pkg["version_id"], "block")).status_code == 200
    assert (await _act(client, platform_headers, "versions", personal["version_id"], "block")).status_code == 200
    assert (await _act(client, auth_headers, "versions", org_pkg["version_id"], "restore")).status_code == 200
    assert (await _act(client, platform_headers, "versions", personal["version_id"], "restore")).status_code == 200
    feed = await _feed(client, auth_headers)
    rows = [
        (item["kind"], item["state"], item["block_scope"], item["scope_org_id"])
        for item in feed["revocations"]
    ]
    assert rows == [
        ("version", "blocked", "org", org.id),
        ("content", "blocked", "org", org.id),
        ("version", "blocked", "global", None),
        ("content", "blocked", "global", None),
        ("version", "restored", "org", org.id),
        ("content", "restored", "org", org.id),
        ("version", "restored", "global", None),
        ("content", "restored", "global", None),
    ]
    # Every entry carries the key, null when the block is not org-scoped.
    assert all("scope_org_id" in item for item in feed["revocations"])
    events = (await db_session.execute(select(SkillRevocationEvent))).scalars().all()
    assert all((event.block_scope == "org") == (event.scope_org_id is not None) for event in events)


async def test_service_keys_receive_only_org_wide_assignments(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """A service key's auth.user is its CREATOR; it must not inherit the
    creator's user- or device-targeted assignments."""
    org, project = await _org(db_session, test_user)
    pkg = await _publish_org(client, auth_headers, org)
    service = await _service(db_session, user=test_user, org_id=org.id, scopes=["skills:read"])

    for target in (
        {"target_type": "user", "target_id": test_user.id},
        {"target_type": "device", "target_id": "runner-1", "target_user_id": test_user.id},
    ):
        response = await client.post(
            f"/api/v1/orgs/{org.id}/skills/assignments",
            headers=auth_headers,
            json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"], **target},
        )
        assert response.status_code == 201, response.text

    effective = "/api/v1/users/me/skills/assignments"
    load = f"/api/v1/marketplace/versions/{pkg['version_id']}/instructions"
    params = {"project_id": project.id, "tool": "codex", "device_id": "runner-1"}
    user_items = (await client.get(effective, headers=auth_headers, params={"device_id": "runner-1"})).json()["items"]
    assert {item["target_type"] for item in user_items} == {"user", "device"}
    assert (await client.get(load, headers=auth_headers, params=params)).status_code == 200

    service_items = (await client.get(effective, headers=service, params={"device_id": "runner-1"})).json()["items"]
    assert service_items == []
    denied = await client.get(load, headers=service, params=params)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "install_approval_required"

    org_wide = await client.post(
        f"/api/v1/orgs/{org.id}/skills/assignments",
        headers=auth_headers,
        json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"], "target_type": "org"},
    )
    assert org_wide.status_code == 201, org_wide.text
    service_items = (await client.get(effective, headers=service, params={"device_id": "runner-1"})).json()["items"]
    assert [item["target_type"] for item in service_items] == ["org"]
    assert (await client.get(load, headers=service, params=params)).status_code == 200


async def test_sqlite_event_writers_serialize_before_reading_max_changed_at(tmp_path) -> None:
    """Two connections to one file-backed SQLite database. Writer A takes the
    revocation writer lock but has not committed; writer B, which has NOT
    written anything yet, must wait at the lock instead of reading
    max(changed_at) early, and must then write a later timestamp than A."""
    import asyncio

    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sessionfs.server.db.models import Base

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'feed.db'}", connect_args={"timeout": 15}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)

    def event(changed_at: datetime, reason: str) -> SkillRevocationEvent:
        return SkillRevocationEvent(
            kind="package", package_id="pk", state="yanked", reason=reason,
            actor_role="platform_admin", visibility="public", changed_at=changed_at,
        )

    writer_a = factory()
    task = None
    try:
        await marketplace_moderation._acquire_revocation_writer_lock(writer_a)

        async def writer_b() -> datetime:
            async with factory() as db:
                await marketplace_moderation._acquire_revocation_writer_lock(db)
                changed_at = await marketplace_moderation._next_changed_at(db)
                db.add(event(changed_at, "b"))
                await db.commit()
                return changed_at

        task = asyncio.create_task(writer_b())
        await asyncio.sleep(0.5)
        assert not task.done(), "writer B read max(changed_at) without waiting for writer A"
        # A computes its timestamp only now, AFTER B started and is waiting.
        a_changed_at = await marketplace_moderation._next_changed_at(writer_a)
        writer_a.add(event(a_changed_at, "a"))
        await writer_a.commit()
        b_changed_at = await asyncio.wait_for(task, timeout=15)
        assert b_changed_at > a_changed_at
        async with factory() as db:
            reasons = (
                await db.execute(
                    select(SkillRevocationEvent.reason).order_by(SkillRevocationEvent.changed_at)
                )
            ).scalars().all()
            ids = (
                await db.execute(select(SkillRevocationEvent.reason).order_by(SkillRevocationEvent.id))
            ).scalars().all()
        assert reasons == ids == ["a", "b"]
    finally:
        await writer_a.close()
        if task is not None and not task.done():
            task.cancel()
        await engine.dispose()


async def test_service_keys_receive_only_project_scoped_install_grants(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """A service key's auth.user is its CREATOR: user- and device-scoped
    install grants targeting that creator must neither grant the service key
    instruction loads nor appear in what it can list or fetch."""
    org, project = await _org(db_session, test_user)
    pkg = await _publish_org(client, auth_headers, org)
    approver, approver_headers = await _user(db_session)
    await _join(db_session, org, approver, "admin")
    service = await _service(db_session, user=test_user, org_id=org.id, scopes=["skills:read"])
    requests_url = f"/api/v1/projects/{project.id}/skill-install-requests"

    async def granted(body: dict) -> str:
        created = await client.post(
            requests_url,
            headers=auth_headers,
            json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"], **body},
        )
        assert created.status_code == 201, created.text
        request_id = created.json()["id"]
        approved = await client.post(f"{requests_url}/{request_id}/approve", headers=approver_headers)
        assert approved.status_code == 200, approved.text
        return request_id

    user_request = await granted({"target_scope": "user", "target_user_id": test_user.id})
    device_request = await granted(
        {"target_scope": "device", "target_user_id": test_user.id, "target_device_id": "runner-1"}
    )

    load = f"/api/v1/marketplace/versions/{pkg['version_id']}/instructions"
    params = {"project_id": project.id, "tool": "codex", "device_id": "runner-1"}
    # The creator, as a human, is granted by both.
    assert (await client.get(load, headers=auth_headers, params=params)).status_code == 200
    user_listing = (await client.get(requests_url, headers=auth_headers)).json()["items"]
    assert {item["id"] for item in user_listing} == {user_request, device_request}

    # The creator's service key is granted by neither and sees neither.
    denied = await client.get(load, headers=service, params=params)
    assert denied.status_code == 403
    assert denied.json()["error"]["code"] == "install_approval_required"
    assert (await client.get(requests_url, headers=service)).json()["items"] == []
    for request_id in (user_request, device_request):
        assert (await client.get(f"{requests_url}/{request_id}", headers=service)).status_code == 404
    effective = await client.get(
        "/api/v1/users/me/skills/assignments", headers=service, params={"device_id": "runner-1"}
    )
    assert effective.json()["items"] == []

    # A project-scoped grant is the org-wide equivalent and does apply.
    project_request = await granted({"target_scope": "project"})
    assert (await client.get(load, headers=service, params=params)).status_code == 200
    service_listing = (await client.get(requests_url, headers=service)).json()["items"]
    assert [item["id"] for item in service_listing] == [project_request]
    fetched = await client.get(f"{requests_url}/{project_request}", headers=service)
    assert fetched.status_code == 200


async def test_user_only_routes_reject_service_keys_with_a_coded_envelope(
    client: AsyncClient, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)
    service = await _service(db_session, user=test_user, org_id=org.id, scopes=["skills:read"])
    response = await client.get("/api/v1/auth/me", headers=service)
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["code"] == "service_key_not_allowed"
    # Older clients matched on details.error; it is kept.
    assert error["details"]["error"] == "service_key_not_allowed"
    assert "code" not in error["details"]


async def test_resolve_project_by_git_remote_for_users_and_service_keys(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, project = await _org(db_session, test_user)
    _, other_project = await _org(db_session, test_user)
    outsider, outsider_headers = await _user(db_session)
    outsider_org, _ = await _org(db_session, outsider)
    url = "/api/v1/marketplace/projects/resolve"
    remote = project.git_remote_normalized

    owner = await client.get(url, headers=auth_headers, params={"git_remote": remote})
    assert owner.status_code == 200, owner.text
    assert owner.json() == {"project_id": project.id, "name": project.name}

    service = await _service(db_session, user=test_user, org_id=org.id, scopes=["skills:read"])
    resolved = await client.get(url, headers=service, params={"git_remote": remote})
    assert resolved.status_code == 200, resolved.text
    assert resolved.json() == {"project_id": project.id, "name": project.name}

    # The client's next calls work with the resolved id.
    listing = await client.get(
        f"/api/v1/projects/{resolved.json()['project_id']}/skill-install-requests", headers=service
    )
    assert listing.status_code == 200, listing.text

    # Allowlisted to another project of the same org: constant 404.
    raw = generate_api_key()
    db_session.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=test_user.id,
            key_hash=hash_api_key(raw),
            name="allowlisted",
            key_kind="service",
            org_id=org.id,
            service_key_name="allowlisted",
            scopes=json.dumps(["skills:read"]),
            project_ids=json.dumps([other_project.id]),
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()
    allowlisted = {"Authorization": f"Bearer {raw}"}
    responses = {
        "allowlist": await client.get(url, headers=allowlisted, params={"git_remote": remote}),
        "foreign org key": await client.get(
            url,
            headers=await _service(db_session, user=outsider, org_id=outsider_org.id, scopes=["skills:read"]),
            params={"git_remote": remote},
        ),
        "outsider user": await client.get(url, headers=outsider_headers, params={"git_remote": remote}),
        "unknown remote": await client.get(url, headers=auth_headers, params={"git_remote": "github.com/x/none"}),
        "blank remote": await client.get(url, headers=auth_headers, params={"git_remote": "   "}),
    }
    for label, response in responses.items():
        assert response.status_code == 404, (label, response.text)
        assert response.json()["error"]["message"] == "Project not found", label
    # Wrong scope is the usual scope error.
    no_scope = await _service(db_session, user=test_user, org_id=org.id, scopes=["marketplace:read"])
    assert (await client.get(url, headers=no_scope, params={"git_remote": remote})).status_code == 403


async def test_publish_rechecks_the_blocklist_under_the_content_lock(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch, blob_store,
) -> None:
    """Deterministic race: a block on the same bytes commits AFTER publish's
    early blocklist check. Publish takes the content-hash lock, re-checks,
    and refuses before writing current_version_id, the listing or the blob."""
    from sessionfs.server.db.models import (
        SkillMarketplaceListing,
        SkillPackage,
        SkillPackageVersion,
    )
    from sessionfs.server.routes import marketplace as marketplace_routes

    _, platform_headers = await _user(db_session, tier="admin")
    original = await _publish_personal(client, auth_headers, namespace="first")
    events: list[str] = []
    real_refuse = marketplace_routes._refuse_blocked_content
    real_lock = marketplace_routes.lock_content_hashes
    real_put = blob_store.put
    checks = {"n": 0}

    async def refuse(db, content_hash, org_id, **kwargs):
        checks["n"] += 1
        events.append(f"check{checks['n']}")
        await real_refuse(db, content_hash, org_id, **kwargs)
        if checks["n"] == 1:
            # The block lands between the early check and the lock.
            blocked = await client.post(
                f"/api/v1/marketplace/versions/{original['version_id']}/block",
                headers=platform_headers,
                json={"reason": "race"},
            )
            assert blocked.status_code == 200, blocked.text

    async def lock(db, hashes):
        events.append("lock")
        return await real_lock(db, hashes)

    written: list[str] = []

    async def put(key, data):
        events.append("put")
        written.append(key)
        return await real_put(key, data)

    monkeypatch.setattr(marketplace_routes, "_refuse_blocked_content", refuse)
    monkeypatch.setattr(marketplace_routes, "lock_content_hashes", lock)
    monkeypatch.setattr(blob_store, "put", put)

    response = await client.post(
        "/api/v1/users/me/skills/packages",
        headers=auth_headers,
        files={"package": ("p.tar.gz", _tar(), "application/gzip")},
        data={"namespace": "second", "collection": "second"},
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "content_blocked"
    # The blob is uploaded before any lock (no lock is held across the put);
    # the block that landed meanwhile is seen under the content lock and the
    # uploaded blob is removed again.
    assert events == ["check1", "put", "lock", "check2"]
    assert len(written) == 1 and await blob_store.get(written[0]) is None
    db_session.expire_all()
    packages = (await db_session.execute(select(SkillPackage.namespace))).scalars().all()
    assert packages == ["first"]
    versions = (await db_session.execute(select(SkillPackageVersion.id))).scalars().all()
    assert versions == [original["version_id"]]
    listings = (await db_session.execute(select(SkillMarketplaceListing.version_id))).scalars().all()
    assert listings == [original["version_id"]]

    # Without a block the order is: early check, blob, lock, re-check.
    events.clear()
    checks["n"] = 5  # skip the injected block
    clean = await client.post(
        "/api/v1/users/me/skills/packages",
        headers=auth_headers,
        files={"package": ("p.tar.gz", _tar("Different body."), "application/gzip")},
        data={"namespace": "third", "collection": "third"},
    )
    assert clean.status_code == 201, clean.text
    assert events == ["check6", "put", "lock", "check7"]
    assert await blob_store.get(written[-1]) is not None


async def test_report_daily_cap_is_counted_under_the_reporter_lock(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deterministic race: a concurrent report by the same user commits while
    this request is acquiring the per-reporter lock. The quota is counted
    after the lock, so it sees that report and refuses (429)."""
    monkeypatch.setattr(marketplace_moderation, "MAX_REPORTS_PER_DAY", 2)
    first = await _publish_personal(client, auth_headers, namespace="cap-a", body="A.")
    second = await _publish_personal(client, auth_headers, namespace="cap-b", body="B.")
    target = await _publish_personal(client, auth_headers, namespace="cap-c", body="C.")
    created = await client.post(
        f"/api/v1/marketplace/packages/{first['package_id']}/reports",
        headers=auth_headers,
        json={"reason": "spam"},
    )
    assert created.status_code == 201, created.text

    real_lock = marketplace_moderation._lock_reporter
    calls: list[str] = []

    async def lock(db, user_id):
        calls.append(user_id)
        if len(calls) == 1:
            # The concurrent report lands just before our lock is granted.
            db_session.add(
                SkillPackageReport(
                    id=f"spr_{uuid.uuid4().hex[:24]}",
                    package_id=second["package_id"],
                    reporter_user_id=test_user.id,
                    reason="spam",
                    status="open",
                    scope_kind="platform",
                    created_at=datetime.now(timezone.utc),
                )
            )
            await db_session.commit()
        await real_lock(db, user_id)

    monkeypatch.setattr(marketplace_moderation, "_lock_reporter", lock)
    raced = await client.post(
        f"/api/v1/marketplace/packages/{target['package_id']}/reports",
        headers=auth_headers,
        json={"reason": "spam"},
    )
    assert calls == [test_user.id]
    assert raced.status_code == 429, raced.text
    assert raced.json()["error"]["code"] == "report_rate_limited"
    count = (
        await db_session.execute(
            select(SkillPackageReport.id).where(SkillPackageReport.reporter_user_id == test_user.id)
        )
    ).scalars().all()
    assert len(count) == 2


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database for the two-connection race",
)
async def test_concurrent_reports_cannot_exceed_the_daily_cap_on_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two real PostgreSQL connections by the same reporter: the first holds
    the reporter lock with its report uncommitted; the second must wait,
    then count that report and refuse at a cap of 1."""
    import asyncio

    from fastapi import HTTPException
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sessionfs.server.auth.dependencies import AuthContext
    from sessionfs.server.db.models import (
        Base,
        SkillMarketplaceListing,
        SkillPackage,
        SkillPackageVersion,
    )
    from sessionfs.server.routes.marketplace_moderation import ReportCreate, create_report

    monkeypatch.setattr(marketplace_moderation, "MAX_REPORTS_PER_DAY", 1)
    schema = f"rep_{uuid.uuid4().hex[:8]}"
    admin_engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    async with admin_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        os.environ["SFS_TEST_PG_URL"], connect_args={"server_settings": {"search_path": schema}}
    )
    first = None
    task: asyncio.Task | None = None
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            db.add(User(id="reporter", email="r@example.com", tier="pro", created_at=datetime.now(timezone.utc)))
            for suffix in ("a", "b"):
                db.add(SkillPackage(id=f"pk{suffix}", owner_type="system", owner_id="sessionfs",
                                    namespace="ns", name=f"n{suffix}", display_name="N",
                                    source_type="hosted-upload", source_locator=suffix))
            await db.flush()
            for suffix in ("a", "b"):
                db.add(SkillPackageVersion(id=f"v{suffix}", package_id=f"pk{suffix}",
                                           content_hash=f"sha256:{suffix * 64}", agent_plugin_schema="s",
                                           manifest_json="{}", dlp_status="passed", blob_key=f"k{suffix}",
                                           blob_size_bytes=1))
            await db.flush()
            for suffix in ("a", "b"):
                db.add(SkillMarketplaceListing(id=f"l{suffix}", package_id=f"pk{suffix}", version_id=f"v{suffix}",
                                               name="n", visibility="public", verification_state="verified"))
            await db.commit()

        auth = AuthContext(
            user=User(id="reporter", email="r@example.com", tier="pro"), api_key_id="k",
            key_kind="user", scopes=["*"],
        )
        # Connection 1: holds the reporter lock with its report uncommitted.
        first = factory()
        await marketplace_moderation._lock_reporter(first, "reporter")
        first.add(SkillPackageReport(
            id="spr_first", package_id="pka", reporter_user_id="reporter", reason="spam",
            status="open", scope_kind="platform", created_at=datetime.now(timezone.utc),
        ))
        await first.flush()

        async def second_report() -> object:
            async with factory() as db:
                try:
                    return await create_report("pkb", ReportCreate(reason="spam"), auth=auth, db=db)
                except HTTPException as exc:
                    return exc.status_code

        task = asyncio.create_task(second_report())
        await wait_for_lock_waiters(engine, task=task)
        assert not task.done(), "second report did not wait for the reporter lock"
        await first.commit()
        assert await asyncio.wait_for(task, timeout=10) == 429
        async with factory() as db:
            rows = (await db.execute(select(SkillPackageReport.id))).scalars().all()
        assert rows == ["spr_first"]
    finally:
        if first is not None:
            await first.rollback()
            await first.close()
        if task is not None and not task.done():
            await asyncio.wait_for(task, timeout=10)
        await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.parametrize(
    ("path", "action"),
    [("download", "yank"), ("download", "block"), ("preview", "block"), ("instructions", "block")],
)
async def test_takedown_landing_mid_delivery_is_honored(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch, path: str, action: str,
) -> None:
    """Deterministic race: the takedown commits AFTER the delivery route's
    up-front visibility check and after it read the content. The route
    re-validates before returning and answers the same constant 404."""
    from sessionfs.server.routes import marketplace as marketplace_routes

    org, project = await _org(db_session, test_user)
    pkg = await _publish_org(client, auth_headers, org)
    version_id = pkg["version_id"]
    assigned = await client.post(
        f"/api/v1/orgs/{org.id}/skills/assignments",
        headers=auth_headers,
        json={"version_id": version_id, "content_hash": pkg["content_hash"], "target_type": "org"},
    )
    assert assigned.status_code == 201, assigned.text
    url = f"/api/v1/marketplace/versions/{version_id}/{path}"
    params = {"project_id": project.id, "tool": "codex"} if path == "instructions" else {}
    assert (await client.get(url, headers=auth_headers, params=params)).status_code == 200

    real_visible_row = marketplace_routes._visible_row
    calls = {"n": 0}

    async def visible_row(*args, **kwargs):
        calls["n"] += 1
        result = await real_visible_row(*args, **kwargs)
        if calls["n"] == 1:
            taken_down = await client.post(
                f"/api/v1/marketplace/versions/{version_id}/{action}",
                headers=auth_headers,
                json={"reason": "mid-delivery"},
            )
            assert taken_down.status_code == 200, taken_down.text
        return result

    monkeypatch.setattr(marketplace_routes, "_visible_row", visible_row)
    response = await client.get(url, headers=auth_headers, params=params)
    assert calls["n"] == 2  # up-front check, then the re-validation
    assert response.status_code == 404, response.text
    assert response.json()["error"]["message"] == "Marketplace package not found"
    assert "Review Helper" not in response.text


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database for the two-connection race",
)
async def test_delivery_revalidation_waits_for_an_in_flight_block_on_postgres() -> None:
    """Two real PostgreSQL connections: a block holds the exclusive content
    lock with its blocklist row uncommitted. Delivery re-validation takes
    the shared lock, waits for the block to commit, then refuses (404)."""
    import asyncio

    from fastapi import HTTPException
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sessionfs.server.auth.dependencies import AuthContext
    from sessionfs.server.db.models import (
        Base,
        SkillBlockedContentHash,
        SkillMarketplaceListing,
        SkillPackage,
        SkillPackageVersion,
    )
    from sessionfs.server.routes import marketplace as marketplace_routes

    schema = f"dlv_{uuid.uuid4().hex[:8]}"
    admin_engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    async with admin_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        os.environ["SFS_TEST_PG_URL"], connect_args={"server_settings": {"search_path": schema}}
    )
    blocker = None
    task: asyncio.Task | None = None
    content_hash = "sha256:" + "cd" * 32
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            db.add(User(id="reader", email="r@example.com", tier="pro", created_at=datetime.now(timezone.utc)))
            db.add(SkillPackage(id="pk", owner_type="system", owner_id="sessionfs", namespace="ns",
                                name="n", display_name="N", source_type="hosted-upload", source_locator="x"))
            await db.flush()
            db.add(SkillPackageVersion(id="v", package_id="pk", content_hash=content_hash,
                                       agent_plugin_schema="s", manifest_json="{}", dlp_status="passed",
                                       blob_key="k", blob_size_bytes=1))
            await db.flush()
            db.add(SkillMarketplaceListing(id="l", package_id="pk", version_id="v", name="n",
                                           visibility="public", verification_state="verified"))
            await db.commit()
        auth = AuthContext(
            user=User(id="reader", email="r@example.com", tier="pro"), api_key_id="k",
            key_kind="user", scopes=["*"],
        )

        blocker = factory()
        await marketplace_routes.lock_content_hashes(blocker, [content_hash])
        blocker.add(SkillBlockedContentHash(
            content_hash=content_hash, scope_org_id=None, source_package_id="pk",
            source_version_id="v", reason="r", actor_role="platform_admin",
            blocked_at=datetime.now(timezone.utc),
        ))
        await blocker.flush()

        async def deliver() -> object:
            async with factory() as db:
                _, _, version = await marketplace_routes._visible_row(db, auth, version_id="v")
                try:
                    await marketplace_routes._revalidate_delivery(db, auth, version)
                except HTTPException as exc:
                    return exc.status_code
                return 200

        task = asyncio.create_task(deliver())
        await wait_for_lock_waiters(engine, task=task)
        assert not task.done(), "re-validation did not wait for the in-flight block"
        await blocker.commit()
        assert await asyncio.wait_for(task, timeout=10) == 404
    finally:
        if blocker is not None:
            await blocker.rollback()
            await blocker.close()
        if task is not None and not task.done():
            await asyncio.wait_for(task, timeout=10)
        await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


@pytest.mark.parametrize("takedown", ["listing_block", "hash_block", "yank"])
async def test_sqlite_delivery_recheck_waits_for_an_in_flight_takedown(tmp_path, takedown: str) -> None:
    """Two connections to one file-backed SQLite database. The moderation
    connection holds the write lock with an uncommitted takedown; delivery's
    final re-check must wait for it, then answer 404 - never the old state."""
    import asyncio

    from fastapi import HTTPException
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sessionfs.server.auth.dependencies import AuthContext
    from sessionfs.server.db.models import (
        Base,
        SkillBlockedContentHash,
        SkillMarketplaceListing,
        SkillPackage,
        SkillPackageVersion,
    )
    from sessionfs.server.routes import marketplace as marketplace_routes

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'delivery.db'}", connect_args={"timeout": 15}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    content_hash = "sha256:" + "ef" * 32
    async with factory() as db:
        db.add(User(id="reader", email="r@example.com", tier="pro", created_at=datetime.now(timezone.utc)))
        db.add(SkillPackage(id="pk", owner_type="system", owner_id="sessionfs", namespace="ns",
                            name="n", display_name="N", source_type="hosted-upload", source_locator="x"))
        await db.flush()
        db.add(SkillPackageVersion(id="v", package_id="pk", content_hash=content_hash,
                                   agent_plugin_schema="s", manifest_json="{}", dlp_status="passed",
                                   blob_key="k", blob_size_bytes=1))
        await db.flush()
        db.add(SkillMarketplaceListing(id="l", package_id="pk", version_id="v", name="n",
                                       visibility="public", verification_state="verified"))
        await db.commit()
    auth = AuthContext(
        user=User(id="reader", email="r@example.com", tier="pro"), api_key_id="k",
        key_kind="user", scopes=["*"],
    )

    moderator = factory()
    task = None
    try:
        # The moderation transaction writes its takedown (taking SQLite's
        # write lock) and has not committed yet.
        if takedown == "listing_block":
            await moderator.execute(
                update(SkillMarketplaceListing).where(SkillMarketplaceListing.id == "l")
                .values(verification_state="blocked")
            )
        elif takedown == "yank":
            await moderator.execute(
                update(SkillMarketplaceListing).where(SkillMarketplaceListing.id == "l")
                .values(is_yanked=True)
            )
        else:
            moderator.add(SkillBlockedContentHash(
                content_hash=content_hash, scope_org_id=None, source_package_id="pk",
                source_version_id="v", reason="r", actor_role="platform_admin",
                blocked_at=datetime.now(timezone.utc),
            ))
            await moderator.flush()

        async def deliver() -> object:
            async with factory() as db:
                # The up-front check reads the pre-takedown state...
                _, _, version = await marketplace_routes._visible_row(db, auth, version_id="v")
                try:
                    # ...the final re-check must not.
                    await marketplace_routes._revalidate_delivery(db, auth, version)
                except HTTPException as exc:
                    return exc.status_code
                return 200

        task = asyncio.create_task(deliver())
        await asyncio.sleep(0.5)
        assert not task.done(), "delivery re-check did not wait for the in-flight takedown"
        await moderator.commit()
        assert await asyncio.wait_for(task, timeout=15) == 404
    finally:
        await moderator.close()
        if task is not None and not task.done():
            task.cancel()
        await engine.dispose()


async def test_sqlite_delivery_recheck_busy_timeout_is_a_retryable_503(tmp_path) -> None:
    """If the in-flight writer outlasts the busy timeout, delivery answers a
    retryable 503 instead of content or a 500."""
    from fastapi import HTTPException
    from sqlalchemy import update
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sessionfs.server.auth.dependencies import AuthContext
    from sessionfs.server.db.models import (
        Base,
        SkillMarketplaceListing,
        SkillPackage,
        SkillPackageVersion,
    )
    from sessionfs.server.routes import marketplace as marketplace_routes

    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'busy.db'}", connect_args={"timeout": 0.2}
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as db:
        db.add(User(id="reader", email="r@example.com", tier="pro", created_at=datetime.now(timezone.utc)))
        db.add(SkillPackage(id="pk", owner_type="system", owner_id="sessionfs", namespace="ns",
                            name="n", display_name="N", source_type="hosted-upload", source_locator="x"))
        await db.flush()
        db.add(SkillPackageVersion(id="v", package_id="pk", content_hash="sha256:" + "aa" * 32,
                                   agent_plugin_schema="s", manifest_json="{}", dlp_status="passed",
                                   blob_key="k", blob_size_bytes=1))
        await db.flush()
        db.add(SkillMarketplaceListing(id="l", package_id="pk", version_id="v", name="n",
                                       visibility="public", verification_state="verified"))
        await db.commit()
    auth = AuthContext(
        user=User(id="reader", email="r@example.com", tier="pro"), api_key_id="k",
        key_kind="user", scopes=["*"],
    )
    moderator = factory()
    try:
        await moderator.execute(
            update(SkillMarketplaceListing).where(SkillMarketplaceListing.id == "l")
            .values(verification_state="blocked")
        )
        async with factory() as db:
            _, _, version = await marketplace_routes._visible_row(db, auth, version_id="v")
            with pytest.raises(HTTPException) as caught:
                await marketplace_routes._revalidate_delivery(db, auth, version)
        assert caught.value.status_code == 503
        assert caught.value.detail["code"] == "marketplace_busy"
        assert caught.value.headers == {"Retry-After": "1"}
    finally:
        await moderator.rollback()
        await moderator.close()
        await engine.dispose()


async def test_org_block_never_takes_down_a_public_copy_the_org_owns(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """Org A owns public package P and org-private package Q with identical
    bytes. An org admin blocking Q hides and revokes only org A's
    non-public copies: P stays deliverable to everyone, its grants stay,
    and outsiders get no revocation. Only a platform block takes P down."""
    org, project = await _org(db_session, test_user)
    org_id = org.id
    _, platform_headers = await _user(db_session, tier="admin")
    outsider, outsider_headers = await _user(db_session)
    outsider_org, _ = await _org(db_session, outsider)

    public = await _publish_org(client, platform_headers, org, visibility="public", namespace="pub", collection="pub")
    private = await _publish_org(client, auth_headers, org, namespace="priv", collection="priv")
    assert public["package_id"] != private["package_id"]
    assert public["content_hash"] == private["content_hash"]

    async def assign(headers: dict, target_org: Organization, pkg: dict) -> str:
        response = await client.post(
            f"/api/v1/orgs/{target_org.id}/skills/assignments",
            headers=headers,
            json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"], "target_type": "org"},
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    own_public = await assign(auth_headers, org, public)
    foreign_public = await assign(outsider_headers, outsider_org, public)
    own_private = await assign(auth_headers, org, private)
    request = await client.post(
        f"/api/v1/projects/{project.id}/skill-install-requests",
        headers=auth_headers,
        json={"version_id": public["version_id"], "content_hash": public["content_hash"]},
    )
    assert request.status_code == 201, request.text

    blocked = await _act(client, auth_headers, "versions", private["version_id"], "block", reason="org only")
    assert blocked.status_code == 200, blocked.text
    assert blocked.json()["affected_version_ids"] == [private["version_id"]]
    assert blocked.json()["grants_revoked"] == {"install_requests": 0, "consent_receipts": 0, "assignments": 1}

    public_download = f"/api/v1/marketplace/versions/{public['version_id']}/download"
    private_download = f"/api/v1/marketplace/versions/{private['version_id']}/download"
    assert (await client.get(public_download, headers=outsider_headers)).status_code == 200
    assert (await client.get(public_download, headers=auth_headers)).status_code == 200
    assert (await client.get(private_download, headers=auth_headers)).status_code == 404
    db_session.expire_all()
    revoked = {
        row.id: row.revoked_at is not None
        for row in (await db_session.execute(select(OrgSkillAssignment))).scalars().all()
    }
    assert revoked == {own_public: False, foreign_public: False, own_private: True}
    statuses = (await db_session.execute(select(SkillInstallRequest.status))).scalars().all()
    assert statuses == ["pending"]
    # The public package's own state is untouched: nothing to restore.
    not_blocked = await _act(client, platform_headers, "versions", public["version_id"], "restore")
    assert not_blocked.status_code == 409
    # Outsiders get no revocation for it; org members see the org-scoped block.
    assert (await _feed(client, outsider_headers))["revocations"] == []
    member_feed = await _feed(client, auth_headers)
    assert [(i["kind"], i["block_scope"], i["scope_org_id"]) for i in member_feed["revocations"]] == [
        ("version", "org", org_id),
        ("content", "org", org_id),
    ]
    # Publishing the bytes publicly into org A stays possible; privately it does not.
    again_public = await client.post(
        f"/api/v1/orgs/{org_id}/skills/packages",
        headers=platform_headers,
        files={"package": ("p.tar.gz", _tar(), "application/gzip")},
        data={"namespace": "pub2", "collection": "pub2", "visibility": "public"},
    )
    assert again_public.status_code == 201, again_public.text
    again_private = await client.post(
        f"/api/v1/orgs/{org_id}/skills/packages",
        headers=auth_headers,
        files={"package": ("p.tar.gz", _tar(), "application/gzip")},
        data={"namespace": "priv2", "collection": "priv2", "visibility": "org"},
    )
    assert again_private.status_code == 403
    assert again_private.json()["error"]["code"] == "content_blocked"

    # A platform (global) block takes the public copies down for everyone.
    global_block = await _act(client, platform_headers, "versions", public["version_id"], "block")
    assert global_block.status_code == 200, global_block.text
    assert public["version_id"] in global_block.json()["affected_version_ids"]
    assert (await client.get(public_download, headers=outsider_headers)).status_code == 404
    assert (await client.get(public_download, headers=auth_headers)).status_code == 404
    db_session.expire_all()
    assert all(
        row.revoked_at is not None
        for row in (await db_session.execute(select(OrgSkillAssignment))).scalars().all()
    )
    outsider_feed = await _feed(client, outsider_headers)
    assert [(i["kind"], i["block_scope"]) for i in outsider_feed["revocations"]] == [
        ("version", "global"),
        ("content", "global"),
    ]


async def test_org_block_of_a_private_listing_never_controls_a_later_public_listing(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """Org moderation is listing-scoped: an org blocks its private listing of
    V; a platform admin then lists the same V publicly. The public listing is
    deliverable to everyone while the org listing stays blocked."""
    from sessionfs.server.db.models import SkillMarketplaceListing

    org, _ = await _org(db_session, test_user)
    org_id = org.id
    _, platform_headers = await _user(db_session, tier="admin")
    _, outsider_headers = await _user(db_session)
    private = await _publish_org(client, auth_headers, org, namespace="shared", collection="team")
    version_id = private["version_id"]
    blocked = await _act(client, auth_headers, "versions", version_id, "block", reason="team only")
    assert blocked.status_code == 200, blocked.text
    download = f"/api/v1/marketplace/versions/{version_id}/download"
    assert (await client.get(download, headers=auth_headers)).status_code == 404

    public = await client.post(
        f"/api/v1/orgs/{org_id}/skills/packages",
        headers=platform_headers,
        files={"package": ("p.tar.gz", _tar(), "application/gzip")},
        data={"namespace": "shared", "collection": "everyone", "visibility": "public"},
    )
    assert public.status_code == 201, public.text
    assert public.json()["version_id"] == version_id  # the same immutable version
    assert public.json()["package_id"] == private["package_id"]

    for headers in (outsider_headers, auth_headers):
        assert (await client.get(download, headers=headers)).status_code == 200
        preview = await client.get(f"/api/v1/marketplace/versions/{version_id}/preview", headers=headers)
        assert preview.json()["listing_id"] == public.json()["listing_id"]
    # The org listing stays blocked and is never served, even to the org.
    search = await client.get("/api/v1/marketplace/packages", headers=auth_headers)
    served = {item["listing_id"] for item in search.json()["items"]}
    assert public.json()["listing_id"] in served
    assert private["listing_id"] not in served
    db_session.expire_all()
    states = {
        row.id: (row.visibility, row.owner_state, row.verification_state, row.is_yanked)
        for row in (await db_session.execute(select(SkillMarketplaceListing))).scalars().all()
    }
    assert states[private["listing_id"]] == ("org", "blocked", "unverified", False)
    assert states[public.json()["listing_id"]] == ("public", None, "verified", False)
    # The org layer is already blocked (and never reaches the public listing);
    # only the platform layer can take the public listing down.
    assert (await _act(client, auth_headers, "versions", version_id, "yank")).status_code == 409
    platform_block = await _act(client, platform_headers, "versions", version_id, "block")
    assert platform_block.status_code == 200, platform_block.text
    assert (await client.get(download, headers=outsider_headers)).status_code == 404
    # An org restore lifts only its own layer; the platform block stays.
    org_restore = await _act(client, auth_headers, "versions", version_id, "restore")
    assert org_restore.status_code == 200, org_restore.text
    assert (await client.get(download, headers=outsider_headers)).status_code == 404
    assert (await _act(client, auth_headers, "versions", version_id, "restore")).status_code == 403
    assert (await _act(client, platform_headers, "versions", version_id, "restore")).status_code == 200
    assert (await client.get(download, headers=outsider_headers)).status_code == 200


async def test_org_whole_package_yank_never_hides_a_later_public_version(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)
    org_id = org.id
    _, platform_headers = await _user(db_session, tier="admin")
    _, outsider_headers = await _user(db_session)
    first = await _publish_org(client, auth_headers, org, namespace="pkg", collection="team", body="One.")
    yanked = await _act(client, auth_headers, "packages", first["package_id"], "yank", reason="retire")
    assert yanked.status_code == 200, yanked.text

    public = await client.post(
        f"/api/v1/orgs/{org_id}/skills/packages",
        headers=platform_headers,
        files={"package": ("p.tar.gz", _tar("Two."), "application/gzip")},
        data={"namespace": "pkg", "collection": "everyone", "visibility": "public"},
    )
    assert public.status_code == 201, public.text
    assert public.json()["package_id"] == first["package_id"]
    public_download = f"/api/v1/marketplace/versions/{public.json()['version_id']}/download"
    for headers in (outsider_headers, auth_headers):
        assert (await client.get(public_download, headers=headers)).status_code == 200
    detail = await client.get(f"/api/v1/marketplace/packages/{first['package_id']}", headers=outsider_headers)
    assert detail.status_code == 200
    # The org's own listings stay hidden - existing and newly published ones.
    assert (
        await client.get(f"/api/v1/marketplace/versions/{first['version_id']}/download", headers=auth_headers)
    ).status_code == 404
    later = await _publish_org(client, auth_headers, org, namespace="pkg", collection="team2", body="Three.")
    assert (
        await client.get(f"/api/v1/marketplace/versions/{later['version_id']}/download", headers=auth_headers)
    ).status_code == 404
    # Restoring the org's package-level yank brings its listings back.
    assert (await _act(client, auth_headers, "packages", first["package_id"], "restore")).status_code == 200
    assert (
        await client.get(f"/api/v1/marketplace/versions/{later['version_id']}/download", headers=auth_headers)
    ).status_code == 200
    # Org events stay org-scoped: outsiders never see them.
    assert (await _feed(client, outsider_headers))["revocations"] == []


async def test_owner_layer_yanks_are_scoped_owner_in_the_feed(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """Owner-layer yanks (and their restores) carry block_scope 'owner' and
    the owning org (null for a personal package); platform yanks stay null."""
    org, _ = await _org(db_session, test_user)
    org_id = org.id
    _, platform_headers = await _user(db_session, tier="admin")
    org_pkg = await _publish_org(client, auth_headers, org, namespace="own", body="Own.")
    personal = await _publish_personal(client, auth_headers, namespace="solo", body="Solo.")

    assert (await _act(client, auth_headers, "versions", org_pkg["version_id"], "yank")).status_code == 200
    assert (await _act(client, auth_headers, "versions", org_pkg["version_id"], "restore")).status_code == 200
    assert (await _act(client, auth_headers, "packages", personal["package_id"], "yank")).status_code == 200
    assert (await _act(client, auth_headers, "packages", personal["package_id"], "restore")).status_code == 200
    assert (await _act(client, platform_headers, "versions", org_pkg["version_id"], "yank")).status_code == 200
    assert (await _act(client, platform_headers, "versions", org_pkg["version_id"], "restore")).status_code == 200
    # A platform admin lifting an OWNER yank acts on the owner layer.
    assert (await _act(client, auth_headers, "versions", org_pkg["version_id"], "yank")).status_code == 200
    assert (await _act(client, platform_headers, "versions", org_pkg["version_id"], "restore")).status_code == 200

    rows = [
        (i["kind"], i["state"], i["block_scope"], i["scope_org_id"])
        for i in (await _feed(client, auth_headers))["revocations"]
    ]
    assert rows == [
        ("version", "yanked", "owner", org_id),
        ("version", "restored", "owner", org_id),
        ("package", "yanked", "owner", None),
        ("package", "restored", "owner", None),
        ("version", "yanked", None, None),
        ("version", "restored", None, None),
        ("version", "yanked", "owner", org_id),
        ("version", "restored", "owner", org_id),
    ]
    events = (await db_session.execute(select(SkillRevocationEvent))).scalars().all()
    assert all(
        (event.block_scope == "org") <= (event.scope_org_id is not None)
        and (event.block_scope in {None, "global"}) <= (event.scope_org_id is None)
        for event in events
    )


async def test_restored_version_with_revoked_assignments_can_be_deleted_and_history_stays(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)
    org_id = org.id
    pkg = await _publish_org(client, auth_headers, org)
    assigned = await client.post(
        f"/api/v1/orgs/{org_id}/skills/assignments",
        headers=auth_headers,
        json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"], "target_type": "org"},
    )
    assert assigned.status_code == 201, assigned.text
    delete_url = f"/api/v1/orgs/{org_id}/skills/packages/{pkg['package_id']}/versions/{pkg['version_id']}"
    # An ACTIVE assignment still blocks deletion.
    refused = await client.delete(delete_url, headers=auth_headers)
    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["assignment_ids"] == [assigned.json()["id"]]

    assert (await _act(client, auth_headers, "versions", pkg["version_id"], "block")).status_code == 200
    # While blocked the version cannot be deleted at all.
    assert (await client.delete(delete_url, headers=auth_headers)).json()["error"]["code"] == "active_block"
    # Restored (the revoked assignment stays revoked), it can; the revoked
    # assignment is history, not a live reference, and survives the delete.
    assert (await _act(client, auth_headers, "versions", pkg["version_id"], "restore")).status_code == 200
    deleted = await client.delete(delete_url, headers=auth_headers)
    assert deleted.status_code == 204, deleted.text

    db_session.expire_all()
    row = await db_session.get(OrgSkillAssignment, assigned.json()["id"])
    assert row is not None and row.revoked_at is not None
    assert row.version_id == pkg["version_id"] and row.content_hash == pkg["content_hash"]
    listed = await client.get(f"/api/v1/orgs/{org_id}/skills/assignments", headers=auth_headers)
    assert listed.json()["incomplete"] is False
    assert [(i["id"], i["version_id"]) for i in listed.json()["items"]] == [(assigned.json()["id"], pkg["version_id"])]


async def test_corrupt_grant_targeting_is_never_guessed_served_or_honored(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, project = await _org(db_session, test_user)
    org_id, project_id = org.id, project.id
    pkg = await _publish_org(client, auth_headers, org)
    approver, approver_headers = await _user(db_session)
    await _join(db_session, org, approver, "admin")

    async def assign(body: dict) -> str:
        response = await client.post(
            f"/api/v1/orgs/{org_id}/skills/assignments",
            headers=auth_headers,
            json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"], **body},
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    good = await assign({"target_type": "user", "target_id": test_user.id, "target_tools": ["codex"]})
    corrupt = await assign({"target_type": "org"})
    requests_url = f"/api/v1/projects/{project_id}/skill-install-requests"
    created = await client.post(
        requests_url,
        headers=auth_headers,
        json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"]},
    )
    assert created.status_code == 201, created.text
    request_id = created.json()["id"]

    load = f"/api/v1/marketplace/versions/{pkg['version_id']}/instructions"
    for broken in ("not json", '{"codex": true}', '["no-such-tool"]', "null", "[1]"):
        assignment = await db_session.get(OrgSkillAssignment, corrupt)
        assignment.target_tools = broken
        request = await db_session.get(SkillInstallRequest, request_id)
        request.requested_capabilities = broken
        await db_session.commit()

        listing = (await client.get(f"/api/v1/orgs/{org_id}/skills/assignments", headers=auth_headers)).json()
        assert listing["incomplete"] is True and listing["omitted_count"] == 1, broken
        assert [item["id"] for item in listing["items"]] == [good]
        assert all(isinstance(item["target_tools"], list) for item in listing["items"])
        effective = (await client.get("/api/v1/users/me/skills/assignments", headers=auth_headers)).json()
        assert effective["incomplete"] is True and effective["omitted_count"] == 1
        assert [item["id"] for item in effective["items"]] == [good]

        requests = (await client.get(requests_url, headers=auth_headers)).json()
        assert requests["items"] == [] and requests["incomplete"] is True and requests["omitted_count"] == 1
        single = await client.get(f"{requests_url}/{request_id}", headers=auth_headers)
        assert single.status_code == 500
        assert single.json()["error"]["code"] == "grant_record_corrupt"
        approve = await client.post(f"{requests_url}/{request_id}/approve", headers=approver_headers)
        assert approve.status_code == 500
        assert approve.json()["error"]["code"] == "grant_record_corrupt"

        # The corrupt org-wide assignment never grants (an empty list would
        # have meant "every tool"); only the good user assignment for codex does.
        assert (await client.get(load, headers=auth_headers, params={"project_id": project_id, "tool": "codex"})).status_code == 200
        denied = await client.get(load, headers=auth_headers, params={"project_id": project_id, "tool": "cursor"})
        assert denied.status_code == 403, broken

    # Clean lists report complete.
    assignment = await db_session.get(OrgSkillAssignment, corrupt)
    assignment.target_tools = "[]"
    await db_session.commit()
    listing = (await client.get(f"/api/v1/orgs/{org_id}/skills/assignments", headers=auth_headers)).json()
    assert listing["incomplete"] is False and listing["omitted_count"] == 0 and len(listing["items"]) == 2


async def test_yanked_version_can_still_be_deleted(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)
    pkg = await _publish_org(client, auth_headers, org)
    assert (await _act(client, auth_headers, "versions", pkg["version_id"], "yank")).status_code == 200
    deleted = await client.delete(
        f"/api/v1/orgs/{org.id}/skills/packages/{pkg['package_id']}/versions/{pkg['version_id']}",
        headers=auth_headers,
    )
    assert deleted.status_code == 204, deleted.text


async def test_global_block_on_a_copy_refuses_deleting_any_copy(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    """A global block on the bytes applies to every copy, so no copy can be
    deleted while it is active (the block must stay restorable)."""
    _, platform_headers = await _user(db_session, tier="admin")
    original = await _publish_personal(client, auth_headers, namespace="orig")
    copy = await _publish_personal(client, auth_headers, namespace="copy", collection="copy")
    assert (await _act(client, platform_headers, "versions", original["version_id"], "block")).status_code == 200
    for pkg in (original, copy):
        refused = await client.delete(
            f"/api/v1/users/me/skills/packages/{pkg['package_id']}/versions/{pkg['version_id']}",
            headers=auth_headers,
        )
        assert refused.status_code == 409, refused.text
        assert refused.json()["error"]["code"] == "active_block"


async def test_removing_accounts_or_orgs_never_lifts_a_block(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """Blocklist rows hold no reference to the package, version, owner or
    org, so deactivating the owner and even removing the owner, org,
    package and version rows outright leaves the block active: the bytes
    stay unpublishable."""
    from sqlalchemy import delete as sql_delete
    from sqlalchemy import update as sql_update

    from sessionfs.server.db.models import (
        SkillMarketplaceListing,
        SkillPackage,
        SkillPackageVersion,
    )

    platform_admin, platform_headers = await _user(db_session, tier="admin")
    org, _ = await _org(db_session, test_user)
    org_id = org.id
    org_pkg = await _publish_org(client, auth_headers, org, body="Org bytes.")
    personal = await _publish_personal(client, auth_headers, namespace="mine", body="Personal bytes.")
    assert (await _act(client, auth_headers, "versions", org_pkg["version_id"], "block")).status_code == 200
    assert (await _act(client, platform_headers, "versions", personal["version_id"], "block")).status_code == 200

    # Admin "delete user" is a soft delete (deactivation).
    owner_id = test_user.id
    deactivated = await client.delete(f"/api/v1/admin/users/{owner_id}", headers=platform_headers)
    assert deactivated.status_code in {204, 409}, deactivated.text  # 409 if the org needs a successor
    # An operator purge removing the rows outright.
    for model, column, value in (
        (SkillMarketplaceListing, SkillMarketplaceListing.package_id, org_pkg["package_id"]),
        (SkillMarketplaceListing, SkillMarketplaceListing.package_id, personal["package_id"]),
        (SkillPackageVersion, SkillPackageVersion.package_id, org_pkg["package_id"]),
        (SkillPackageVersion, SkillPackageVersion.package_id, personal["package_id"]),
        (SkillPackage, SkillPackage.id, org_pkg["package_id"]),
        (SkillPackage, SkillPackage.id, personal["package_id"]),
        (OrgMember, OrgMember.org_id, org_id),
        (Organization, Organization.id, org_id),
    ):
        await db_session.execute(sql_delete(model).where(column == value))
    await db_session.execute(
        sql_update(SkillBlockedContentHash)
        .where(SkillBlockedContentHash.actor_user_id.in_([owner_id, platform_admin.id]))
        .values(actor_user_id=None)  # what ON DELETE SET NULL does on PostgreSQL
    )
    await db_session.execute(sql_delete(ApiKey).where(ApiKey.user_id == owner_id))
    await db_session.execute(sql_delete(User).where(User.id == owner_id))
    await db_session.commit()

    db_session.expire_all()
    blocks = (await db_session.execute(select(SkillBlockedContentHash))).scalars().all()
    assert {(row.content_hash, row.scope_org_id) for row in blocks} == {
        (org_pkg["content_hash"], org_id),
        (personal["content_hash"], None),
    }
    assert all(row.lifted_at is None for row in blocks)
    # The global block still refuses the bytes for anyone.
    _, someone_headers = await _user(db_session)
    refused = await client.post(
        "/api/v1/users/me/skills/packages",
        headers=someone_headers,
        files={"package": ("p.tar.gz", _tar("Personal bytes."), "application/gzip")},
        data={"namespace": "again", "collection": "again"},
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "content_blocked"


async def test_restoring_one_target_never_lifts_another_targets_block(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    """A1 and B1 carry identical bytes in different packages. A1's global
    block applies to B1 too, but B1's restore never lifts it."""
    _, platform_headers = await _user(db_session, tier="admin")
    _, outsider_headers = await _user(db_session)
    a1 = await _publish_personal(client, auth_headers, namespace="alpha")
    b1 = await _publish_personal(client, auth_headers, namespace="beta", collection="beta")
    assert a1["content_hash"] == b1["content_hash"]
    a_download = f"/api/v1/marketplace/versions/{a1['version_id']}/download"
    b_download = f"/api/v1/marketplace/versions/{b1['version_id']}/download"

    assert (await _act(client, platform_headers, "versions", a1["version_id"], "block", reason="A")).status_code == 200
    assert (await client.get(b_download, headers=auth_headers)).status_code == 404
    # B1 is blocked only via A1's row: no direct restore.
    refused = await _act(client, platform_headers, "versions", b1["version_id"], "restore")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "blocked_by_other_target"
    db_session.expire_all()
    rows = (await db_session.execute(select(SkillBlockedContentHash))).scalars().all()
    assert [(row.source_version_id, row.lifted_at is None) for row in rows] == [(a1["version_id"], True)]

    # Both blocked: B1 may take its own block, and restoring B1 lifts only
    # B1's own row - A1's block keeps both hidden and the content blocked.
    own = await _act(client, platform_headers, "versions", b1["version_id"], "block", reason="B")
    assert own.status_code == 200, own.text
    restored_b = await _act(client, platform_headers, "versions", b1["version_id"], "restore")
    assert restored_b.status_code == 200, restored_b.text
    db_session.expire_all()
    active = {
        row.source_version_id
        for row in (await db_session.execute(select(SkillBlockedContentHash))).scalars().all()
        if row.lifted_at is None
    }
    assert active == {a1["version_id"]}
    for url in (a_download, b_download):
        assert (await client.get(url, headers=outsider_headers)).status_code in {404}
        assert (await client.get(url, headers=auth_headers)).status_code == 404
    content = [
        (i["state"], i["block_scope"])
        for i in (await _feed(client, auth_headers))["revocations"]
        if i["kind"] == "content"
    ]
    # Blocked twice (A, B), never restored while A1's row is active.
    assert content == [("blocked", "global"), ("blocked", "global")]

    # Restoring A1 lifts the last active row: both deliverable, one restore event.
    assert (await _act(client, platform_headers, "versions", a1["version_id"], "restore")).status_code == 200
    for url in (a_download, b_download):
        assert (await client.get(url, headers=auth_headers)).status_code == 200
    content = [
        (i["state"], i["block_scope"])
        for i in (await _feed(client, auth_headers))["revocations"]
        if i["kind"] == "content"
    ]
    assert content == [("blocked", "global"), ("blocked", "global"), ("restored", "global")]


async def test_platform_package_block_refuses_later_publishes_and_deletes(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sessionfs.server.db.models import SkillPackage
    from sessionfs.server.routes import marketplace as marketplace_routes

    _, platform_headers = await _user(db_session, tier="admin")
    first = await _publish_personal(client, auth_headers, namespace="pkg", body="One.")
    package_id = first["package_id"]

    # A platform block that lands MID-publish (after the early checks) is
    # still honored under the publish lock.
    real_refuse = marketplace_routes._refuse_blocked_content
    calls = {"n": 0}

    async def refuse(db, content_hash, org_id, **kwargs):
        calls["n"] += 1
        await real_refuse(db, content_hash, org_id, **kwargs)
        if calls["n"] == 1:
            blocked = await _act(client, platform_headers, "packages", package_id, "block", reason="mid")
            assert blocked.status_code == 200, blocked.text

    monkeypatch.setattr(marketplace_routes, "_refuse_blocked_content", refuse)
    raced = await client.post(
        "/api/v1/users/me/skills/packages",
        headers=auth_headers,
        files={"package": ("p.tar.gz", _tar("Two."), "application/gzip")},
        data={"namespace": "pkg", "collection": "mine"},
    )
    assert raced.status_code == 403, raced.text
    assert raced.json()["error"]["code"] == "package_blocked"
    monkeypatch.setattr(marketplace_routes, "_refuse_blocked_content", real_refuse)

    db_session.expire_all()
    package = await db_session.get(SkillPackage, package_id)
    assert package is not None and package.platform_state == "blocked" and package.is_yanked is True
    # Later publishes into the blocked package are refused outright.
    later = await client.post(
        "/api/v1/users/me/skills/packages",
        headers=auth_headers,
        files={"package": ("p.tar.gz", _tar("Three."), "application/gzip")},
        data={"namespace": "pkg", "collection": "mine"},
    )
    assert later.status_code == 403
    assert later.json()["error"]["code"] == "package_blocked"
    # Deletion is refused by the persisted package state itself, even with
    # no hash row covering the version (as for a version published later).
    rows = (await db_session.execute(select(SkillBlockedContentHash))).scalars().all()
    for row in rows:
        await db_session.delete(row)
    await db_session.commit()
    delete_url = f"/api/v1/users/me/skills/packages/{package_id}/versions/{first['version_id']}"
    refused = await client.delete(delete_url, headers=auth_headers)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "active_block"
    # Restore clears it: publishing and deleting work again.
    assert (await _act(client, platform_headers, "packages", package_id, "restore")).status_code == 200
    db_session.expire_all()
    package = await db_session.get(SkillPackage, package_id)
    assert package is not None and package.platform_state is None and package.is_yanked is False
    again = await client.post(
        "/api/v1/users/me/skills/packages",
        headers=auth_headers,
        files={"package": ("p.tar.gz", _tar("Four."), "application/gzip")},
        data={"namespace": "pkg", "collection": "mine"},
    )
    assert again.status_code == 201, again.text
    assert (await client.delete(delete_url, headers=auth_headers)).status_code == 204


async def test_package_restore_reports_a_block_owned_by_another_package(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    """Package B's whole-package block covers bytes package A also ships.
    Restoring A is refused as blocked_by_other_target (not "nothing to
    restore") and lifts nothing; restoring B clears both."""
    _, platform_headers = await _user(db_session, tier="admin")
    a = await _publish_personal(client, auth_headers, namespace="pkga")
    b = await _publish_personal(client, auth_headers, namespace="pkgb", collection="b")
    assert a["content_hash"] == b["content_hash"] and a["package_id"] != b["package_id"]

    assert (await _act(client, platform_headers, "packages", b["package_id"], "block")).status_code == 200
    refused = await _act(client, platform_headers, "packages", a["package_id"], "restore")
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "blocked_by_other_target"
    assert b["package_id"] not in refused.text and b["version_id"] not in refused.text
    db_session.expire_all()
    rows = (await db_session.execute(select(SkillBlockedContentHash))).scalars().all()
    assert [(row.source_package_id, row.lifted_at is None) for row in rows] == [(b["package_id"], True)]

    assert (await _act(client, platform_headers, "packages", b["package_id"], "restore")).status_code == 200
    nothing = await _act(client, platform_headers, "packages", a["package_id"], "restore")
    assert nothing.status_code == 409
    assert nothing.json()["error"]["code"] == "not_revoked"
    download = await client.get(f"/api/v1/marketplace/versions/{a['version_id']}/download", headers=auth_headers)
    assert download.status_code == 200


async def _walk(client: AsyncClient, url: str, headers: dict, **params) -> tuple[list[str], list[dict]]:
    seen: list[str] = []
    pages: list[dict] = []
    cursor = None
    for _ in range(50):
        query = dict(params)
        if cursor:
            query["cursor"] = cursor
        response = await client.get(url, headers=headers, params=query)
        assert response.status_code == 200, response.text
        page = response.json()
        assert set(page) >= {"items", "incomplete", "omitted_count", "has_more", "next_cursor"}
        pages.append(page)
        seen.extend(item["id"] for item in page["items"])
        if not page["has_more"]:
            assert page["next_cursor"] is None
            break
        assert page["next_cursor"]
        cursor = page["next_cursor"]
    return seen, pages


async def test_grant_lists_paginate_fully_without_duplicates_or_gaps(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, project = await _org(db_session, test_user)
    org_id, project_id, user_id = org.id, project.id, test_user.id
    pkg = await _publish_org(client, auth_headers, org)

    request_ids = [f"sir_{i:04d}{uuid.uuid4().hex[:8]}" for i in range(105)]
    for index, request_id in enumerate(request_ids):
        db_session.add(SkillInstallRequest(
            id=request_id, project_id=project_id, version_id=pkg["version_id"],
            content_hash=pkg["content_hash"], requested_by_user_id=user_id,
            target_scope="project", status="approved",
            target_tools="[]",
            # One unreadable record: omitted and flagged on its page only.
            requested_capabilities="not json" if index == 42 else '["instructions"]',
        ))
    assignment_ids = [f"osa_{i:04d}{uuid.uuid4().hex[:8]}" for i in range(103)]
    for index, assignment_id in enumerate(assignment_ids):
        db_session.add(OrgSkillAssignment(
            id=assignment_id, org_id=org_id, version_id=pkg["version_id"],
            content_hash=pkg["content_hash"], target_type="device",
            target_id=f"runner-{index}", target_user_id=user_id,
        ))
    await db_session.commit()

    # Default page size (100): 105 requests take two pages.
    seen, pages = await _walk(client, f"/api/v1/projects/{project_id}/skill-install-requests", auth_headers)
    assert len(pages) == 2 and pages[0]["has_more"] is True
    expected = sorted(rid for i, rid in enumerate(request_ids) if i != 42)
    assert seen == expected  # every readable row exactly once, in id order
    assert sum(page["omitted_count"] for page in pages) == 1
    assert [page["incomplete"] for page in pages].count(True) == 1

    # A smaller page size walks the same set.
    small, small_pages = await _walk(
        client, f"/api/v1/projects/{project_id}/skill-install-requests", auth_headers, limit=7
    )
    assert small == expected and len(small_pages) == 15

    seen, pages = await _walk(client, f"/api/v1/orgs/{org_id}/skills/assignments", auth_headers)
    assert seen == sorted(assignment_ids) and len(pages) == 2

    # Invalid cursors are rejected, never treated as "start over".
    for bad in ("garbage!!", "Zm9vOmJhcg"):  # not base64 / wrong prefix
        response = await client.get(
            f"/api/v1/orgs/{org_id}/skills/assignments", headers=auth_headers, params={"cursor": bad}
        )
        assert response.status_code == 422, bad
    assert (
        await client.get(
            f"/api/v1/orgs/{org_id}/skills/assignments", headers=auth_headers, params={"limit": 501}
        )
    ).status_code == 422


async def test_effective_assignments_paginate_through_filtered_rows(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """The users/me list pages by fetched rows, so tool filtering and
    omitted (corrupt) rows never open a gap between pages."""
    _, platform_headers = await _user(db_session, tier="admin")
    first_org, _ = await _org(db_session, test_user)
    public = await _publish_org(
        client, platform_headers, first_org, visibility="public", namespace="pub", collection="pub"
    )
    orgs = [first_org] + [(await _org(db_session, test_user))[0] for _ in range(6)]
    created: list[str] = []
    for index, org in enumerate(orgs):
        response = await client.post(
            f"/api/v1/orgs/{org.id}/skills/assignments",
            headers=auth_headers,
            json={
                "version_id": public["version_id"],
                "content_hash": public["content_hash"],
                "target_type": "org",
                "target_tools": ["codex"] if index % 2 == 0 else ["cursor"],
            },
        )
        assert response.status_code == 201, response.text
        created.append(response.json()["id"])
    corrupt = await db_session.get(OrgSkillAssignment, sorted(created)[3])
    corrupt.target_tools = "not json"
    await db_session.commit()

    everything, pages = await _walk(client, "/api/v1/users/me/skills/assignments", auth_headers, limit=2)
    assert everything == sorted(set(created) - {sorted(created)[3]})
    assert len(pages) == 4 and sum(page["omitted_count"] for page in pages) == 1
    codex, _ = await _walk(
        client, "/api/v1/users/me/skills/assignments", auth_headers, limit=2, tool="codex"
    )
    expected_codex = sorted(
        assignment_id for index, assignment_id in enumerate(created)
        if index % 2 == 0 and assignment_id != sorted(created)[3]
    )
    assert codex == expected_codex


async def test_grant_list_cursor_is_refused_after_the_collection_changes(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """A row inserted mid-walk could sort before the cursor (ids are not
    time-ordered); instead of missing it, the stale cursor gets 409."""
    org, project = await _org(db_session, test_user)
    org_id, project_id = org.id, project.id
    pkg = await _publish_org(client, auth_headers, org)
    requests_url = f"/api/v1/projects/{project_id}/skill-install-requests"

    async def new_request() -> str:
        created = await client.post(
            requests_url,
            headers=auth_headers,
            json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"]},
        )
        assert created.status_code == 201, created.text
        return created.json()["id"]

    for _ in range(4):
        await new_request()
    first = (await client.get(requests_url, headers=auth_headers, params={"limit": 2})).json()
    assert first["has_more"] is True and isinstance(first["generation"], str) and first["generation"]
    # Unchanged collection: the cursor keeps working and the generation is stable.
    second = (
        await client.get(requests_url, headers=auth_headers, params={"limit": 2, "cursor": first["next_cursor"]})
    ).json()
    assert second["generation"] == first["generation"]
    # A write mid-walk invalidates outstanding cursors.
    await new_request()
    assert second["has_more"] is False and second["next_cursor"] is None
    stale = await client.get(
        requests_url, headers=auth_headers, params={"limit": 2, "cursor": first["next_cursor"]}
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "listing_changed"
    restarted = (await client.get(requests_url, headers=auth_headers, params={"limit": 50})).json()
    assert restarted["generation"] != first["generation"] and len(restarted["items"]) == 5

    # Org assignments: an assignment created mid-walk.
    assignments_url = f"/api/v1/orgs/{org_id}/skills/assignments"
    for index in range(3):
        response = await client.post(
            assignments_url,
            headers=auth_headers,
            json={
                "version_id": pkg["version_id"], "content_hash": pkg["content_hash"],
                "target_type": "device", "target_id": f"runner-{index}", "target_user_id": test_user.id,
            },
        )
        assert response.status_code == 201, response.text
    page = (await client.get(assignments_url, headers=auth_headers, params={"limit": 1})).json()
    org_wide = await client.post(
        assignments_url,
        headers=auth_headers,
        json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"], "target_type": "org"},
    )
    assert org_wide.status_code == 201
    stale = await client.get(assignments_url, headers=auth_headers, params={"limit": 1, "cursor": page["next_cursor"]})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "listing_changed"

    # Effective assignments: a moderation block revokes them mid-walk.
    effective_url = "/api/v1/users/me/skills/assignments"
    params = {"limit": 1, "device_id": "runner-0"}
    page = (await client.get(effective_url, headers=auth_headers, params=params)).json()
    assert page["has_more"] is True
    assert (await _act(client, auth_headers, "versions", pkg["version_id"], "block")).status_code == 200
    stale = await client.get(effective_url, headers=auth_headers, params={**params, "cursor": page["next_cursor"]})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "listing_changed"
    # The request list changed too (the block rejected the pending requests).
    walk = (await client.get(requests_url, headers=auth_headers, params={"limit": 2})).json()
    assert (await _act(client, auth_headers, "versions", pkg["version_id"], "restore")).status_code == 200
    stale = await client.get(requests_url, headers=auth_headers, params={"limit": 2, "cursor": walk["next_cursor"]})
    assert stale.status_code == 409


async def test_revoked_assignments_cannot_be_deleted(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _ = await _org(db_session, test_user)
    org_id = org.id
    pkg = await _publish_org(client, auth_headers, org, body="One.")
    other = await _publish_org(client, auth_headers, org, namespace="other", collection="other", body="Two.")

    async def assign(target: dict) -> str:
        response = await client.post(
            f"/api/v1/orgs/{org_id}/skills/assignments",
            headers=auth_headers,
            json={"version_id": target["version_id"], "content_hash": target["content_hash"], "target_type": "org"},
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    revoked = await assign(pkg)
    active = await assign(other)
    assert (await _act(client, auth_headers, "versions", pkg["version_id"], "block")).status_code == 200

    refused = await client.delete(f"/api/v1/orgs/{org_id}/skills/assignments/{revoked}", headers=auth_headers)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "assignment_revoked"
    db_session.expire_all()
    row = await db_session.get(OrgSkillAssignment, revoked)
    assert row is not None and row.revoked_at is not None
    # Active assignments still delete normally.
    deleted = await client.delete(f"/api/v1/orgs/{org_id}/skills/assignments/{active}", headers=auth_headers)
    assert deleted.status_code == 204


async def test_version_delete_takes_the_content_lock_before_the_block_check(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deterministic race: a global block on a COPY of the bytes (another
    package) commits while the delete is acquiring the content-hash lock.
    The active-block check runs after the lock, sees it, and refuses."""
    from sessionfs.server.routes import marketplace as marketplace_routes

    _, platform_headers = await _user(db_session, tier="admin")
    victim = await _publish_personal(client, auth_headers, namespace="victim")
    copy = await _publish_personal(client, auth_headers, namespace="copy", collection="copy")
    assert victim["content_hash"] == copy["content_hash"]

    real_lock = marketplace_routes.lock_content_hashes
    real_check = marketplace_routes._has_active_block
    order: list[str] = []

    async def lock(db, hashes):
        order.append("lock")
        if order.count("lock") == 1:
            blocked = await _act(client, platform_headers, "versions", copy["version_id"], "block")
            assert blocked.status_code == 200, blocked.text
        return await real_lock(db, hashes)

    async def check(db, **kwargs):
        order.append("check")
        return await real_check(db, **kwargs)

    monkeypatch.setattr(marketplace_routes, "lock_content_hashes", lock)
    monkeypatch.setattr(marketplace_routes, "_has_active_block", check)
    refused = await client.delete(
        f"/api/v1/users/me/skills/packages/{victim['package_id']}/versions/{victim['version_id']}",
        headers=auth_headers,
    )
    assert order[:2] == ["lock", "check"]
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "active_block"


@pytest.mark.skipif(
    not os.environ.get("SFS_TEST_PG_URL"),
    reason="set SFS_TEST_PG_URL to a scratch PostgreSQL database for the two-connection race",
)
async def test_version_delete_waits_for_an_in_flight_block_on_postgres() -> None:
    """Two real PostgreSQL connections: a block holds the content-hash lock
    with its global blocklist row uncommitted. Deleting another version with
    the same bytes waits, then sees the block and refuses (409)."""
    import asyncio
    from types import SimpleNamespace

    from fastapi import HTTPException
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from sessionfs.server.auth.dependencies import AuthContext
    from sessionfs.server.db.models import (
        Base,
        SkillMarketplaceListing,
        SkillPackage,
        SkillPackageVersion,
    )
    from sessionfs.server.routes import marketplace as marketplace_routes

    schema = f"del_{uuid.uuid4().hex[:8]}"
    admin_engine = create_async_engine(os.environ["SFS_TEST_PG_URL"])
    async with admin_engine.begin() as conn:
        await conn.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        os.environ["SFS_TEST_PG_URL"], connect_args={"server_settings": {"search_path": schema}}
    )
    blocker = None
    task: asyncio.Task | None = None
    content_hash = "sha256:" + "be" * 32
    try:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        factory = async_sessionmaker(engine, expire_on_commit=False)
        async with factory() as db:
            db.add(User(id="owner", email="o@example.com", tier="pro", created_at=datetime.now(timezone.utc)))
            await db.flush()
            for suffix in ("a", "b"):
                db.add(SkillPackage(id=f"pk{suffix}", owner_type="user", owner_id="owner",
                                    namespace=f"ns{suffix}", name="n", display_name="N",
                                    source_type="hosted-upload", source_locator=suffix))
            await db.flush()
            for suffix in ("a", "b"):
                db.add(SkillPackageVersion(id=f"v{suffix}", package_id=f"pk{suffix}", content_hash=content_hash,
                                           agent_plugin_schema="s", manifest_json="{}", dlp_status="passed",
                                           blob_key=f"k{suffix}", blob_size_bytes=1))
            await db.flush()
            for suffix in ("a", "b"):
                db.add(SkillMarketplaceListing(id=f"l{suffix}", package_id=f"pk{suffix}", version_id=f"v{suffix}",
                                               owner_user_id="owner", name="n", visibility="user",
                                               verification_state="unverified"))
            await db.commit()
        auth = AuthContext(
            user=User(id="owner", email="o@example.com", tier="pro"), api_key_id="k",
            key_kind="user", scopes=["*"],
        )

        # Connection 1: the block on version b holds the content lock with
        # its global row uncommitted.
        blocker = factory()
        await marketplace_routes.lock_content_hashes(blocker, [content_hash])
        blocker.add(SkillBlockedContentHash(
            content_hash=content_hash, scope_org_id=None, source_package_id="pkb",
            source_version_id="vb", reason="r", actor_role="platform_admin",
            blocked_at=datetime.now(timezone.utc),
        ))
        await blocker.flush()

        async def delete() -> object:
            async with factory() as db:
                try:
                    await marketplace_routes._delete_version(
                        request=SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(blob_store=None))),
                        db=db, auth=auth, owner_type="user", owner_id="owner", org=None,
                        role="owner", package_id="pka", version_id="va",
                    )
                except HTTPException as exc:
                    return exc.status_code, exc.detail
                return 204

        task = asyncio.create_task(delete())
        await wait_for_lock_waiters(engine, task=task)
        assert not task.done(), "the delete did not wait for the content-hash lock"
        await blocker.commit()
        status, detail = await asyncio.wait_for(task, timeout=10)
        assert status == 409 and detail["code"] == "active_block"
        async with factory() as db:
            assert await db.get(SkillPackageVersion, "va") is not None
    finally:
        if blocker is not None:
            await blocker.rollback()
            await blocker.close()
        if task is not None and not task.done():
            await asyncio.wait_for(task, timeout=10)
        await engine.dispose()
        async with admin_engine.begin() as conn:
            await conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        await admin_engine.dispose()


async def test_other_tenants_get_a_redacted_record_of_a_shared_bytes_block(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """A platform block of org A's private package revokes org B's grants on
    identical bytes. Org B's audit names only its own rows - no A ids, no
    moderator reason, no moderator identity - and org A's audit never names
    B's versions. The full picture stays in the platform AdminAction."""
    platform_admin, platform_headers = await _user(db_session, tier="admin")
    admin_email, admin_id = platform_admin.email, platform_admin.id
    org_a, _ = await _org(db_session, test_user)
    owner_b, owner_b_headers = await _user(db_session)
    org_b, project_b = await _org(db_session, owner_b)
    org_a_id, org_b_id, project_b_id = org_a.id, org_b.id, project_b.id
    secret_reason = "exfiltrates the payroll database of org A"

    pkg_a = await _publish_org(client, auth_headers, org_a, namespace="a-secret", collection="a")
    pkg_b = await _publish_org(client, owner_b_headers, org_b, namespace="b-copy", collection="b")
    assert pkg_a["content_hash"] == pkg_b["content_hash"]
    assigned = await client.post(
        f"/api/v1/orgs/{org_b_id}/skills/assignments",
        headers=owner_b_headers,
        json={"version_id": pkg_b["version_id"], "content_hash": pkg_b["content_hash"], "target_type": "org"},
    )
    assert assigned.status_code == 201, assigned.text
    requested = await client.post(
        f"/api/v1/projects/{project_b_id}/skill-install-requests",
        headers=owner_b_headers,
        json={"version_id": pkg_b["version_id"], "content_hash": pkg_b["content_hash"]},
    )
    assert requested.status_code == 201, requested.text

    blocked = await _act(client, platform_headers, "versions", pkg_a["version_id"], "block", reason=secret_reason)
    assert blocked.status_code == 200, blocked.text

    db_session.expire_all()
    b_events = (
        await db_session.execute(select(OrgAuditEvent).where(OrgAuditEvent.org_id == org_b_id))
    ).scalars().all()
    moderation = [event for event in b_events if event.event_type == "skill_grants_revoked_by_moderation"]
    assert len(moderation) == 1
    event = moderation[0]
    assert event.actor_user_id is None and event.actor_email_snapshot is None
    assert event.actor_role_at_time == "system"
    serialized = json.dumps(
        {
            "after": event.after, "before": event.before, "target_id": event.target_id,
            "target_type": event.target_type,
        }
    )
    for leaked in (
        pkg_a["package_id"], pkg_a["version_id"], org_a_id, secret_reason,
        admin_email, admin_id,
    ):
        assert leaked not in serialized, leaked
    after = json.loads(event.after)
    assert after == {
        "reason": "Blocked by SessionFS moderation",
        "state": "blocked",
        "assignment_ids": [assigned.json()["id"]],
        "install_request_ids": [requested.json()["id"]],
        "version_ids": [pkg_b["version_id"]],
        "actor": {"actor_type": "system"},
    }
    # No other org-B row carries any of A's details either.
    for other in b_events:
        dumped = json.dumps({"after": other.after, "target_id": other.target_id})
        assert pkg_a["version_id"] not in dumped and secret_reason not in dumped

    # Org A's own record never names B's versions; the AdminAction has everything.
    a_events = (
        await db_session.execute(
            select(OrgAuditEvent).where(
                OrgAuditEvent.org_id == org_a_id, OrgAuditEvent.event_type == "skill_package_blocked"
            )
        )
    ).scalars().all()
    assert len(a_events) == 1
    a_after = json.loads(a_events[0].after)
    assert a_after["affected_version_ids"] == [pkg_a["version_id"]]
    assert pkg_b["version_id"] not in a_events[0].after
    admin_details = (
        await db_session.execute(select(AdminAction.details).where(AdminAction.action == "skill_block"))
    ).scalars().all()
    assert any(pkg_b["version_id"] in details and pkg_a["version_id"] in details for details in admin_details)


async def test_publishing_a_public_listing_mid_walk_invalidates_effective_cursors(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """A new public listing of a version whose only org listing was yanked
    makes its assignments effective again: an in-progress walk restarts."""
    org, _ = await _org(db_session, test_user)
    org_id = org.id
    _, platform_headers = await _user(db_session, tier="admin")
    hidden = await _publish_org(client, auth_headers, org, namespace="hidden", collection="team", body="Hidden.")

    async def assign(pkg: dict) -> str:
        response = await client.post(
            f"/api/v1/orgs/{org_id}/skills/assignments",
            headers=auth_headers,
            json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"], "target_type": "org"},
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    hidden_assignment = await assign(hidden)
    for index in range(2):
        await assign(await _publish_org(client, auth_headers, org, namespace=f"vis{index}", body=f"Visible {index}."))
    # Owner yank: the version's only listing is hidden, so is its assignment.
    assert (await _act(client, auth_headers, "versions", hidden["version_id"], "yank")).status_code == 200
    effective = "/api/v1/users/me/skills/assignments"
    page = (await client.get(effective, headers=auth_headers, params={"limit": 1})).json()
    assert page["has_more"] is True

    public = await client.post(
        f"/api/v1/orgs/{org_id}/skills/packages",
        headers=platform_headers,
        files={"package": ("p.tar.gz", _tar("Hidden."), "application/gzip")},
        data={"namespace": "hidden", "collection": "everyone", "visibility": "public"},
    )
    assert public.status_code == 201, public.text
    assert public.json()["version_id"] == hidden["version_id"]
    stale = await client.get(effective, headers=auth_headers, params={"limit": 1, "cursor": page["next_cursor"]})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "listing_changed"
    fresh = (await client.get(effective, headers=auth_headers, params={"limit": 50})).json()
    assert hidden_assignment in {item["id"] for item in fresh["items"]}


@pytest.mark.parametrize(
    "locator",
    [
        "git+https://oauth2:SECRET-TOKEN@github.com/x/y.git",
        "https://SECRET-USER@github.com/x/y.git",
        "https://user:SECRET-PW@github.com/x/y.git",
        "ssh://alice@github.com/x/y.git",
        "git+ssh://alice:SECRET-PW@github.com/x/y.git",
        "svn+https://SECRET-USER@svn.example.com/repo",
        "file://user:SECRET-PW@/srv/repo",
        "alice:SECRET-PW@github.com:x/y.git",
        "bob@github.com:x/y.git",
        "https://github.com/x/y.git?token=SECRET-TOKEN",
        "https://github.com/x/y.git#SECRET-FRAGMENT",
        "git@github.com:x/y.git?ref=SECRET-REF",
    ],
)
async def test_source_locators_with_credentials_or_query_are_refused(
    client: AsyncClient, auth_headers: dict, locator: str
) -> None:
    response = await client.post(
        "/api/v1/users/me/skills/packages",
        headers=auth_headers,
        files={"package": ("p.tar.gz", _tar(), "application/gzip")},
        data={"namespace": "loc", "collection": "loc", "source_locator": locator},
    )
    assert response.status_code == 422, (locator, response.text)
    assert response.json()["error"]["code"] == "invalid_source_locator"
    assert "SECRET" not in response.text


@pytest.mark.parametrize(
    ("locator", "canonical"),
    [
        ("HTTPS://GitHub.COM/x/y.git", "https://github.com/x/y.git"),
        ("ssh://git@GitHub.com:2222/x/y.git", "ssh://git@github.com:2222/x/y.git"),
        ("git+ssh://git@github.com/x/y.git", "git+ssh://git@github.com/x/y.git"),
        ("git@github.com:x/y.git", "git@github.com:x/y.git"),
        ("github.com/x/y", "github.com/x/y"),
    ],
)
async def test_source_locators_are_stored_and_returned_canonical(
    client: AsyncClient, auth_headers: dict, locator: str, canonical: str
) -> None:
    response = await client.post(
        "/api/v1/users/me/skills/packages",
        headers=auth_headers,
        files={"package": ("p.tar.gz", _tar(), "application/gzip")},
        data={"namespace": "loc", "collection": "loc", "source_locator": locator},
    )
    assert response.status_code == 201, response.text
    assert response.json()["source"]["locator"] == canonical


async def test_stored_credential_locators_are_never_returned(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    """Rows stored before the canonical rules are redacted on every read."""
    from sessionfs.server.db.models import SkillPackage

    pkg = await _publish_personal(client, auth_headers, namespace="legacy")
    detail_url = f"/api/v1/marketplace/packages/{pkg['package_id']}"
    for stored, shown in (
        ("git+https://oauth2:SECRET-TOKEN@github.com/x/y.git", "git+https://github.com/x/y.git"),
        ("https://SECRET-USER@github.com/x/y.git?token=SECRET-Q#SECRET-F", "https://github.com/x/y.git"),
        ("alice:SECRET-PW@github.com:x/y.git", "github.com:x/y.git"),
    ):
        package = await db_session.get(SkillPackage, pkg["package_id"])
        package.source_locator = stored
        await db_session.commit()
        for url in (detail_url, f"/api/v1/marketplace/versions/{pkg['version_id']}/preview"):
            response = await client.get(url, headers=auth_headers)
            assert response.status_code == 200, response.text
            assert "SECRET" not in response.text, (stored, url)
            assert response.json()["source"]["locator"] == shown
        search = await client.get("/api/v1/marketplace/packages", headers=auth_headers)
        assert "SECRET" not in search.text


@pytest.mark.parametrize("list_kind", ["requests", "org_assignments", "effective"])
async def test_grant_page_rechecks_the_generation_after_reading_rows(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch, list_kind: str,
) -> None:
    """Deterministic race: a write commits (and bumps the generation) after
    the page's rows were read. The post-read re-check refuses the page - even
    a first, terminal page - instead of returning a stale generation."""
    from sessionfs.server.routes import marketplace as marketplace_routes

    org, project = await _org(db_session, test_user)
    org_id, project_id = org.id, project.id
    pkg = await _publish_org(client, auth_headers, org)
    requests_url = f"/api/v1/projects/{project_id}/skill-install-requests"
    assignments_url = f"/api/v1/orgs/{org_id}/skills/assignments"

    async def write() -> None:
        if list_kind == "requests":
            response = await client.post(
                requests_url,
                headers=auth_headers,
                json={"version_id": pkg["version_id"], "content_hash": pkg["content_hash"]},
            )
        else:
            response = await client.post(
                assignments_url,
                headers=auth_headers,
                json={
                    "version_id": pkg["version_id"], "content_hash": pkg["content_hash"],
                    "target_type": "user", "target_id": test_user.id,
                },
            )
        assert response.status_code == 201, response.text

    url = {
        "requests": requests_url,
        "org_assignments": assignments_url,
        "effective": "/api/v1/users/me/skills/assignments",
    }[list_kind]
    real_token = marketplace_routes.grant_generation_token
    calls = {"n": 0, "injected": False}

    async def token(db, scope_type, scope_ids):
        calls["n"] += 1
        if calls["n"] == 2 and not calls["injected"]:
            # Second read = the post-rows re-check: a writer lands first.
            calls["injected"] = True
            await write()
        return await real_token(db, scope_type, scope_ids)

    monkeypatch.setattr(marketplace_routes, "grant_generation_token", token)
    raced = await client.get(url, headers=auth_headers)
    assert calls["injected"] is True
    assert raced.status_code == 409, raced.text
    assert raced.json()["error"]["code"] == "listing_changed"
    # Without a concurrent write, the same request succeeds.
    monkeypatch.setattr(marketplace_routes, "grant_generation_token", real_token)
    clean = await client.get(url, headers=auth_headers)
    assert clean.status_code == 200, clean.text
    assert len(clean.json()["items"]) == 1
