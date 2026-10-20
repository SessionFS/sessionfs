"""Install disclosure, update diffs and D2 re-approval on the server
(marketplace v2 §3, tk_d8bc543da8344124)."""

from __future__ import annotations

import io
import json
import tarfile

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import (
    OrgAuditEvent,
    SkillInstallRequest,
    OrgMember,
    OrgSkillAssignment,
    SkillConsentReceipt,
    SkillPackageVersion,
    User,
)
from sessionfs.skills import disclosure
from sessionfs.skills.package import AGENT_PLUGIN_SCHEMA
from tests.server.integration.test_marketplace_moderation import _org, _user

pytestmark = pytest.mark.asyncio


def _tar(body: str = "Inspect the diff.", *, script: bytes | None = None, version: str = "1.0.0") -> bytes:
    manifest = {
        "$schema": AGENT_PLUGIN_SCHEMA,
        "name": "kit",
        "version": version,
        "description": "Disclosure fixture.",
        "license": "MIT",
    }
    files = {
        "plugin.json": json.dumps(manifest).encode(),
        "skills/kit/SKILL.md": f"---\nname: kit\ndescription: Kit skill.\n---\n\n# Kit\n\n{body}\n".encode(),
    }
    if script is not None:
        files["skills/kit/scripts/run.py"] = script
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode="w:gz") as archive:
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
    return output.getvalue()


async def _publish(client: AsyncClient, headers: dict, *, org=None, **kwargs) -> dict:
    url = f"/api/v1/orgs/{org.id}/skills/packages" if org else "/api/v1/users/me/skills/packages"
    data = {"namespace": "kit", "collection": "kit"}
    if org:
        data["visibility"] = "org"
    response = await client.post(
        url, headers=headers, files={"package": ("p.tar.gz", _tar(**kwargs), "application/gzip")}, data=data
    )
    assert response.status_code == 201, response.text
    return response.json()


async def _sequence_versions(db_session: AsyncSession, *published: dict) -> None:
    """Pin created_at in publish order. SQLite's server_default func.now() has
    SECOND precision, so versions published within one second tie and "newer
    than" (created_at, id) would fall to the random id — PostgreSQL uses
    microsecond precision, so this only matters under the SQLite test run."""
    from datetime import datetime, timedelta, timezone

    for index, item in enumerate(published):
        row = await db_session.get(SkillPackageVersion, item["version_id"])
        assert row is not None
        row.created_at = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=index)
    await db_session.commit()


async def test_publish_stores_manifest_and_exposes_disclosure(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    pkg = await _publish(
        client, auth_headers, body="Run it:\n\n```bash\nmake build\n```\n\nDocs: https://docs.example.com",
        script=b"print('x')\n",
    )
    assert pkg["capability_digest"].startswith("sha256:")
    row = await db_session.get(SkillPackageVersion, pkg["version_id"])
    assert row is not None and row.manifest_version == disclosure.MANIFEST_VERSION
    assert row.capability_digest == pkg["capability_digest"]

    card = await client.get(
        f"/api/v1/marketplace/versions/{pkg['version_id']}/disclosure",
        headers=auth_headers,
        params={"tools": "codex,claude-code"},
    )
    assert card.status_code == 200, card.text
    body = card.json()
    assert sorted(body["tools"]) == ["claude-code", "codex"]
    assert ".codex/skills/kit/scripts/run.py" in body["tools"]["codex"]["writes"]
    assert body["tools"]["codex"]["consent_required"] == ["skill-scripts"]
    assert body["network_endpoints"] == ["docs.example.com"]
    assert body["shell_commands"][0]["command"] == "make"
    assert body["capability_digest"] == pkg["capability_digest"]
    assert "scripts" in body["capabilities"] and "network" in body["capabilities"]
    assert body["consent_rule_version"] == 2

    user_scope = await client.get(
        f"/api/v1/marketplace/versions/{pkg['version_id']}/disclosure",
        headers=auth_headers,
        params={"tools": "codex", "scope": "user"},
    )
    assert user_scope.json()["tools"]["codex"]["writes"][0].startswith("~/.codex/skills/kit/")
    bad = await client.get(
        f"/api/v1/marketplace/versions/{pkg['version_id']}/disclosure",
        headers=auth_headers,
        params={"tools": "notepad"},
    )
    assert bad.status_code == 422

    preview = await client.get(f"/api/v1/marketplace/versions/{pkg['version_id']}/preview", headers=auth_headers)
    assert preview.status_code == 200, preview.text
    assert preview.json()["disclosure"]["capability_digest"] == pkg["capability_digest"]

    # A personal listing is invisible to anyone else: constant 404.
    _, other = await _user(db_session)
    hidden = await client.get(f"/api/v1/marketplace/versions/{pkg['version_id']}/disclosure", headers=other)
    assert hidden.status_code == 404


async def test_manifest_is_computed_lazily_for_older_versions(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    pkg = await _publish(client, auth_headers)
    row = await db_session.get(SkillPackageVersion, pkg["version_id"])
    assert row is not None
    digest = row.capability_digest
    row.capability_manifest = "{}"
    row.capability_digest = None
    row.manifest_version = 0
    await db_session.commit()
    card = await client.get(f"/api/v1/marketplace/versions/{pkg['version_id']}/disclosure", headers=auth_headers)
    assert card.status_code == 200, card.text
    assert card.json()["capability_digest"] == digest
    await db_session.refresh(row)
    assert row.manifest_version == disclosure.MANIFEST_VERSION and row.capability_digest == digest


async def test_diff_classifies_text_code_and_widening(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
) -> None:
    v1 = await _publish(client, auth_headers, body="One.", script=b"print(1)\n")
    v2 = await _publish(client, auth_headers, body="Two.", script=b"print(1)\n", version="2.0.0")
    v3 = await _publish(client, auth_headers, body="Two.", script=b"print(2)\n", version="3.0.0")
    v4 = await _publish(client, auth_headers, body="See https://evil.example.net", script=b"print(2)\n", version="4.0.0")
    assert len({v1["package_id"], v2["package_id"], v3["package_id"], v4["package_id"]}) == 1

    async def diff(a: dict, b: dict, **params) -> dict:
        response = await client.get(
            f"/api/v1/marketplace/versions/{a['version_id']}/diff/{b['version_id']}",
            headers=auth_headers,
            params=params,
        )
        assert response.status_code == 200, response.text
        return response.json()

    text = await diff(v1, v2)
    assert text["update_class"] == "text"
    assert "skills/kit/SKILL.md" in text["files"]["changed"]
    [entry] = [e for e in text["text_diffs"]["entries"] if e["path"] == "skills/kit/SKILL.md"]
    assert "-One." in entry["diff"] and "+Two." in entry["diff"]
    assert text["consent"]["personal"] == "confirm"

    code = await diff(v2, v3, text="false")
    assert code["update_class"] == "code"
    assert code["code_changes"] == [{"kind": "script", "change": "changed", "path": "skills/kit/scripts/run.py"}]
    assert "text_diffs" not in code

    wide = await diff(v3, v4)
    assert wide["update_class"] == "widening"
    assert {"kind": "new_network_endpoint", "item": "evil.example.net"} in wide["widens"]
    assert wide["consent"]["org_required"] == "never_auto_apply"

    other = await client.post(
        "/api/v1/users/me/skills/packages",
        headers=auth_headers,
        files={"package": ("p.tar.gz", _tar(), "application/gzip")},
        data={"namespace": "otherns", "collection": "kit"},
    )
    assert other.status_code == 201, other.text
    mismatch = await client.get(
        f"/api/v1/marketplace/versions/{v1['version_id']}/diff/{other.json()['version_id']}",
        headers=auth_headers,
    )
    assert mismatch.status_code == 422


async def test_diff_is_unavailable_when_an_archive_is_missing(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, blob_store
) -> None:
    """A stored manifest but a deleted archive must not fabricate a text diff
    (the missing side treated as empty): the endpoint fails closed with 503."""
    v1 = await _publish(client, auth_headers, body="One.")
    v2 = await _publish(client, auth_headers, body="Two.", version="2.0.0")
    row = await db_session.get(SkillPackageVersion, v1["version_id"])
    assert row is not None
    await blob_store.delete(row.blob_key)

    # Classification still works from the stored manifests.
    classified = await client.get(
        f"/api/v1/marketplace/versions/{v1['version_id']}/diff/{v2['version_id']}",
        headers=auth_headers,
        params={"text": "false"},
    )
    assert classified.status_code == 200, classified.text
    assert classified.json()["update_class"] == "text"

    unavailable = await client.get(
        f"/api/v1/marketplace/versions/{v1['version_id']}/diff/{v2['version_id']}",
        headers=auth_headers,
    )
    assert unavailable.status_code == 503
    assert unavailable.json()["error"]["code"] == "diff_unavailable"


async def test_assignment_update_requires_reapproval_when_widening(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _project = await _org(db_session, test_user)
    v1 = await _publish(client, auth_headers, org=org)
    v2 = await _publish(client, auth_headers, org=org, body="Upload to https://sink.example.org", version="2.0.0")
    await _sequence_versions(db_session, v1, v2)
    url = f"/api/v1/orgs/{org.id}/skills/assignments"
    first = await client.post(url, headers=auth_headers, json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]})
    assert first.status_code == 201, first.text
    assert first.json()["capability_digest"] == v1["capability_digest"]

    # An assignment of v1 never authorizes v2: moving the target goes through
    # /advance, which re-checks D2.
    refused = await client.post(url, headers=auth_headers, json={"version_id": v2["version_id"], "content_hash": v2["content_hash"]})
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "use_advance"

    held = await client.get(f"{url}/updates", headers=auth_headers)
    assert held.status_code == 200, held.text
    [item] = held.json()["items"]
    assert item["held"] is True and item["requires"] == "approve_widening"
    assert item["to_version_id"] == v2["version_id"]
    # The queue carries the exact diff the update_digest binds, as ONE nested
    # object (the same object the reapproval 409 returns as details.diff).
    diff = item["diff"]
    for key in (
        "files", "scan_delta", "unicode_delta", "provenance_delta", "consent",
        "baseline_digest", "changed_requirements", "capability_digest_changed",
    ):
        assert key in diff, key
    assert diff["consent"]["org_required"] == "never_auto_apply"

    advance_url = f"{url}/{first.json()['id']}/advance"
    body = {"to_version_id": v2["version_id"], "content_hash": v2["content_hash"]}
    first_try = await client.post(advance_url, headers=auth_headers, json=body)
    assert first_try.status_code == 409
    details = first_try.json()["error"]["details"]
    digest = details["update_digest"]
    assert digest == item["update_digest"]  # the queue shows the same update
    assert details["diff"] == item["diff"]
    # An acknowledgement without the reviewed diff's digest is not enough.
    assert (await client.post(advance_url, headers=auth_headers, json={**body, "approve_widening": True})).status_code == 409
    advanced = await client.post(
        advance_url, headers=auth_headers, json={**body, "approve_widening": True, "update_digest": digest}
    )
    assert advanced.status_code == 200, advanced.text
    result = advanced.json()
    assert result["version_id"] == v2["version_id"]
    assert result["capability_digest"] == v2["capability_digest"]
    assert result["update_class"] == "widening"
    rows = (await db_session.execute(select(OrgSkillAssignment).where(OrgSkillAssignment.org_id == org.id))).scalars().all()
    by_version = {row.version_id: row for row in rows}
    # The old assignment is kept as history: superseded (and never granting).
    old_row = by_version[v1["version_id"]]
    assert old_row.superseded_by_assignment_id == result["id"]
    assert old_row.superseded_at is not None and old_row.revoked_at is not None
    assert old_row.capability_digest == v1["capability_digest"]
    assert by_version[v2["version_id"]].revoked_at is None
    listed = (await client.get(url, headers=auth_headers)).json()["items"]
    assert {item["id"]: item["superseded_by_assignment_id"] for item in listed}[first.json()["id"]] == result["id"]
    deleted = await client.delete(f"{url}/{first.json()['id']}", headers=auth_headers)
    assert deleted.status_code == 409 and deleted.json()["error"]["code"] == "assignment_superseded"
    audit = (
        await db_session.execute(
            select(OrgAuditEvent).where(OrgAuditEvent.event_type == "skill_assignment_advanced")
        )
    ).scalars().one()
    assert json.loads(audit.after)["approved_widening"] is True
    assert (await client.get(f"{url}/updates", headers=auth_headers)).json()["items"] == []


async def test_advance_text_update_needs_no_flag_and_code_needs_one(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _project = await _org(db_session, test_user)
    v1 = await _publish(client, auth_headers, org=org, script=b"print(1)\n")
    v2 = await _publish(client, auth_headers, org=org, body="Reworded.", script=b"print(1)\n", version="2.0.0")
    v3 = await _publish(client, auth_headers, org=org, body="Reworded.", script=b"print(2)\n", version="3.0.0")
    await _sequence_versions(db_session, v1, v2, v3)
    url = f"/api/v1/orgs/{org.id}/skills/assignments"
    first = await client.post(url, headers=auth_headers, json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]})
    # Text advances need no approval flag, but every advance is still bound to
    # the reviewed diff: an advance without update_digest is refused with the
    # digest to retry against (the dashboard sends it back with the retry).
    text_refused = await client.post(
        f"{url}/{first.json()['id']}/advance",
        headers=auth_headers,
        json={"to_version_id": v2["version_id"], "content_hash": v2["content_hash"]},
    )
    assert text_refused.status_code == 409
    assert text_refused.json()["error"]["code"] == "reapproval_required"
    assert text_refused.json()["error"]["details"]["update_class"] == "text"
    text = await client.post(
        f"{url}/{first.json()['id']}/advance",
        headers=auth_headers,
        json={
            "to_version_id": v2["version_id"],
            "content_hash": v2["content_hash"],
            "update_digest": text_refused.json()["error"]["details"]["update_digest"],
        },
    )
    assert text.status_code == 200, text.text
    assert text.json()["update_class"] == "text"
    code_body = {"to_version_id": v3["version_id"], "content_hash": v3["content_hash"]}
    refused = await client.post(f"{url}/{text.json()['id']}/advance", headers=auth_headers, json=code_body)
    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["update_class"] == "code"
    ok = await client.post(
        f"{url}/{text.json()['id']}/advance", headers=auth_headers,
        json={**code_body, "approve_code_change": True, "update_digest": refused.json()["error"]["details"]["update_digest"]},
    )
    assert ok.status_code == 200, ok.text

    # Members who are not admins cannot advance or see the queue.
    member, member_headers = await _user(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=member.id, role="member"))
    await db_session.commit()
    assert (await client.get(f"{url}/updates", headers=member_headers)).status_code == 403
    assert (
        await client.post(f"{url}/{ok.json()['id']}/advance", headers=member_headers, json=code_body)
    ).status_code == 403
    # Another org cannot touch it at all.
    _outsider, outsider_headers = await _user(db_session)
    assert (
        await client.post(f"{url}/{ok.json()['id']}/advance", headers=outsider_headers, json=code_body)
    ).status_code == 404


async def test_install_approval_of_a_widening_update_needs_acknowledgement(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, project = await _org(db_session, test_user)
    approver, approver_headers = await _user(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=approver.id, role="admin"))
    await db_session.commit()
    v1 = await _publish(client, auth_headers, org=org)
    v2 = await _publish(client, auth_headers, org=org, body="Post to https://collect.example.io", version="2.0.0")

    async def request(item: dict) -> str:
        created = await client.post(
            f"/api/v1/projects/{project.id}/skill-install-requests",
            headers=auth_headers,
            json={"version_id": item["version_id"], "content_hash": item["content_hash"]},
        )
        assert created.status_code == 201, created.text
        return created.json()["id"]

    first = await request(v1)
    approved = await client.post(
        f"/api/v1/projects/{project.id}/skill-install-requests/{first}/approve", headers=approver_headers, json={}
    )
    assert approved.status_code == 200, approved.text
    assert approved.json()["capability_digest"] == v1["capability_digest"]
    receipts = (
        await db_session.execute(select(SkillConsentReceipt).where(SkillConsentReceipt.install_request_id == first))
    ).scalars().all()
    assert receipts and all(r.capability_digest == v1["capability_digest"] for r in receipts)
    assert all(r.consent_rule_version == 2 and json.loads(r.consented_manifest)["instructions"] for r in receipts)

    second = await request(v2)
    approve_url = f"/api/v1/projects/{project.id}/skill-install-requests/{second}/approve"
    refused = await client.post(approve_url, headers=approver_headers, json={})
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "reapproval_required"
    ok = await client.post(approve_url, headers=approver_headers, json={
        "acknowledge_widening": True, "update_digest": refused.json()["error"]["details"]["update_digest"],
    })
    assert ok.status_code == 200, ok.text



async def test_legacy_approvals_and_assignments_never_count_as_digest_approval(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, project = await _org(db_session, test_user)
    approver, approver_headers = await _user(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=approver.id, role="admin"))
    await db_session.commit()
    v1 = await _publish(client, auth_headers, org=org)
    created = await client.post(
        f"/api/v1/projects/{project.id}/skill-install-requests",
        headers=auth_headers,
        json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]},
    )
    approved = await client.post(
        f"/api/v1/projects/{project.id}/skill-install-requests/{created.json()['id']}/approve",
        headers=approver_headers, json={},
    )
    assert approved.json()["capability_digest"] == v1["capability_digest"]
    row = await db_session.get(SkillInstallRequest, created.json()["id"])
    assert row is not None and row.approved_capability_digest == v1["capability_digest"]
    # A pre-070 approval has no bound digest: the live version digest is
    # never shown in its place.
    row.approved_capability_digest = None
    row.approved_consent_rule_version = None
    await db_session.commit()
    detail = await client.get(
        f"/api/v1/projects/{project.id}/skill-install-requests/{row.id}", headers=auth_headers
    )
    assert detail.json()["capability_digest"] is None
    # A stale rule version also fails closed.
    row.approved_capability_digest = v1["capability_digest"]
    row.approved_consent_rule_version = 1
    await db_session.commit()
    detail = await client.get(
        f"/api/v1/projects/{project.id}/skill-install-requests/{row.id}", headers=auth_headers
    )
    assert detail.json()["capability_digest"] is None

    assigned = await client.post(
        f"/api/v1/orgs/{org.id}/skills/assignments",
        headers=auth_headers,
        json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]},
    )
    assignment = await db_session.get(OrgSkillAssignment, assigned.json()["id"])
    assert assignment is not None and assignment.consent_rule_version == 2
    assignment.consent_rule_version = None
    await db_session.commit()
    listed = (await client.get(f"/api/v1/orgs/{org.id}/skills/assignments", headers=auth_headers)).json()
    assert listed["items"][0]["capability_digest"] is None


async def test_text_update_approval_carries_capability_consent_forward(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, project = await _org(db_session, test_user)
    approver, approver_headers = await _user(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=approver.id, role="admin"))
    await db_session.commit()
    v1 = await _publish(client, auth_headers, org=org, script=b"print(1)\n")
    v2 = await _publish(client, auth_headers, org=org, body="Reworded.", script=b"print(1)\n", version="2.0.0")
    v3 = await _publish(client, auth_headers, org=org, body="Reworded.", script=b"print(2)\n", version="3.0.0")

    async def request(item: dict) -> str:
        created = await client.post(
            f"/api/v1/projects/{project.id}/skill-install-requests",
            headers=auth_headers,
            json={"version_id": item["version_id"], "content_hash": item["content_hash"],
                  "requested_capabilities": ["instructions", "scripts"]},
        )
        assert created.status_code == 201, created.text
        return created.json()["id"]

    def approve(request_id: str, body: dict):
        return client.post(
            f"/api/v1/projects/{project.id}/skill-install-requests/{request_id}/approve",
            headers=approver_headers, json=body,
        )

    first = await request(v1)
    assert (await approve(first, {})).status_code == 422  # scripts must be confirmed
    assert (await approve(first, {"approved_capabilities": ["scripts"]})).status_code == 200
    first_scripts = (
        await db_session.execute(
            select(SkillConsentReceipt).where(
                SkillConsentReceipt.install_request_id == first, SkillConsentReceipt.capability == "scripts"
            )
        )
    ).scalars().one()

    # Text update: the scripts consent carries forward, chained to v1's receipt.
    second = await request(v2)
    ok = await approve(second, {})
    assert ok.status_code == 200, ok.text
    carried = (
        await db_session.execute(
            select(SkillConsentReceipt).where(
                SkillConsentReceipt.install_request_id == second, SkillConsentReceipt.capability == "scripts"
            )
        )
    ).scalars().one()
    assert carried.carried_forward_from_receipt_id == first_scripts.id
    assert carried.consent_rule_version == 2

    # Code change: acknowledgement AND the changed capability again.
    third = await request(v3)
    held = await approve(third, {})
    assert held.status_code == 409
    ack = {"acknowledge_code_change": True, "update_digest": held.json()["error"]["details"]["update_digest"]}
    missing = await approve(third, ack)
    assert missing.status_code == 422
    assert missing.json()["error"]["details"]["missing"] == ["scripts"]
    done = await approve(third, {**ack, "approved_capabilities": ["scripts"]})
    assert done.status_code == 200, done.text


async def test_disclosure_and_diff_recheck_delivery_last(
    client: AsyncClient, auth_headers: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sessionfs.server.routes import marketplace_disclosure

    v1 = await _publish(client, auth_headers, body="One.")
    v2 = await _publish(client, auth_headers, body="Two.", version="2.0.0")
    calls: list[str] = []
    original = marketplace_disclosure._revalidate_delivery

    async def _spy(db, auth, version, *, release=True):
        calls.append(version.id)
        return await original(db, auth, version, release=release)

    monkeypatch.setattr(marketplace_disclosure, "_revalidate_delivery", _spy)
    assert (await client.get(f"/api/v1/marketplace/versions/{v1['version_id']}/disclosure", headers=auth_headers)).status_code == 200
    assert calls == [v1["version_id"]]
    calls.clear()
    assert (
        await client.get(f"/api/v1/marketplace/versions/{v2['version_id']}/diff/{v1['version_id']}", headers=auth_headers)
    ).status_code == 200
    assert calls == sorted([v1["version_id"], v2["version_id"]])

    async def _taken_down(db, auth, version, *, release=True):
        from fastapi import HTTPException

        raise HTTPException(404, "Marketplace package not found")

    monkeypatch.setattr(marketplace_disclosure, "_revalidate_delivery", _taken_down)
    gone = await client.get(f"/api/v1/marketplace/versions/{v1['version_id']}/disclosure", headers=auth_headers)
    assert gone.status_code == 404



async def test_legacy_prior_grants_are_an_unknown_baseline(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, project = await _org(db_session, test_user)
    approver, approver_headers = await _user(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=approver.id, role="admin"))
    await db_session.commit()
    v1 = await _publish(client, auth_headers, org=org, script=b"print(1)\n")
    v2 = await _publish(client, auth_headers, org=org, body="Reworded.", script=b"print(1)\n", version="2.0.0")
    await _sequence_versions(db_session, v1, v2)

    async def request(item: dict) -> str:
        created = await client.post(
            f"/api/v1/projects/{project.id}/skill-install-requests",
            headers=auth_headers,
            json={"version_id": item["version_id"], "content_hash": item["content_hash"],
                  "requested_capabilities": ["instructions", "scripts"]},
        )
        return created.json()["id"]

    first = await request(v1)
    url = f"/api/v1/projects/{project.id}/skill-install-requests"
    assert (await client.post(f"{url}/{first}/approve", headers=approver_headers,
                              json={"approved_capabilities": ["scripts"]})).status_code == 200
    # Make the prior approval legacy (rule v1): the text update is now widening
    # and nothing carries forward.
    row = await db_session.get(SkillInstallRequest, first)
    assert row is not None
    row.approved_consent_rule_version = 1
    await db_session.commit()
    second = await request(v2)
    refused = await client.post(f"{url}/{second}/approve", headers=approver_headers, json={})
    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["update_class"] == "widening"
    missing = await client.post(f"{url}/{second}/approve", headers=approver_headers, json={
        "acknowledge_widening": True, "update_digest": refused.json()["error"]["details"]["update_digest"],
    })
    assert missing.status_code == 422  # scripts not carried from a legacy grant

    # Same for /advance: a legacy assignment binding is an unknown baseline.
    assignments = f"/api/v1/orgs/{org.id}/skills/assignments"
    created = await client.post(assignments, headers=auth_headers,
                                json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]})
    assignment = await db_session.get(OrgSkillAssignment, created.json()["id"])
    assert assignment is not None and assignment.package_id == v1["package_id"]
    assignment.consent_rule_version = None
    await db_session.commit()
    advance = f"{assignments}/{created.json()['id']}/advance"
    body = {"to_version_id": v2["version_id"], "content_hash": v2["content_hash"]}
    held = await client.post(advance, headers=auth_headers, json=body)
    assert held.status_code == 409 and held.json()["error"]["details"]["update_class"] == "widening"
    assert (await client.post(advance, headers=auth_headers, json={
        **body, "approve_widening": True, "update_digest": held.json()["error"]["details"]["update_digest"],
    })).status_code == 200


async def test_grants_need_a_bound_manifest_and_create_never_replaces_a_version(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sessionfs.server.services import skill_disclosure

    org, project = await _org(db_session, test_user)
    v1 = await _publish(client, auth_headers, org=org)
    v2 = await _publish(client, auth_headers, org=org, body="Two.", version="2.0.0")
    await _sequence_versions(db_session, v1, v2)
    assignments = f"/api/v1/orgs/{org.id}/skills/assignments"

    async def _outage(db, store, version, *, persist=True):
        return None

    with monkeypatch.context() as patched:
        patched.setattr(skill_disclosure, "version_manifest", _outage)
        outage = await client.post(assignments, headers=auth_headers,
                                   json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]})
        assert outage.status_code == 503
        created = await client.post(
            f"/api/v1/projects/{project.id}/skill-install-requests",
            headers=auth_headers, json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]},
        )
        approver, approver_headers = await _user(db_session)
        db_session.add(OrgMember(org_id=org.id, user_id=approver.id, role="admin"))
        await db_session.commit()
        approve = await client.post(
            f"/api/v1/projects/{project.id}/skill-install-requests/{created.json()['id']}/approve",
            headers=approver_headers, json={},
        )
        assert approve.status_code == 503
    assert (await db_session.execute(select(SkillConsentReceipt))).scalars().all() == []

    first = await client.post(assignments, headers=auth_headers,
                              json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]})
    assert first.status_code == 201
    replaced = await client.post(assignments, headers=auth_headers,
                                 json={"version_id": v2["version_id"], "content_hash": v2["content_hash"]})
    assert replaced.status_code == 409
    assert replaced.json()["error"]["code"] == "use_advance"
    assert replaced.json()["error"]["details"]["assignment_id"] == first.json()["id"]



async def test_carried_server_consent_is_per_tool(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, project = await _org(db_session, test_user)
    approver, approver_headers = await _user(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=approver.id, role="admin"))
    await db_session.commit()
    v1 = await _publish(client, auth_headers, org=org, script=b"print(1)\n")
    v2 = await _publish(client, auth_headers, org=org, body="Reworded.", script=b"print(1)\n", version="2.0.0")
    url = f"/api/v1/projects/{project.id}/skill-install-requests"

    async def request(item: dict, tools: list[str]) -> str:
        created = await client.post(url, headers=auth_headers, json={
            "version_id": item["version_id"], "content_hash": item["content_hash"],
            "requested_capabilities": ["instructions", "scripts"], "target_tools": tools,
        })
        assert created.status_code == 201, created.text
        return created.json()["id"]

    first = await request(v1, ["codex"])
    assert (await client.post(f"{url}/{first}/approve", headers=approver_headers,
                              json={"approved_capabilities": ["scripts"]})).status_code == 200
    # codex's scripts receipt never covers claude-code.
    second = await request(v2, ["claude-code"])
    refused = await client.post(f"{url}/{second}/approve", headers=approver_headers, json={})
    assert refused.status_code == 422
    assert refused.json()["error"]["details"]["missing"] == ["scripts"]


async def test_delete_never_removes_a_concurrently_superseded_assignment(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timezone

    from sessionfs.server.routes import marketplace

    org, _project = await _org(db_session, test_user)
    v1 = await _publish(client, auth_headers, org=org)
    url = f"/api/v1/orgs/{org.id}/skills/assignments"
    created = await client.post(url, headers=auth_headers,
                                json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]})
    assignment_id = created.json()["id"]
    original = marketplace._assignment_for_delete

    async def _read_then_supersede(db, org_id, target_id):
        row = await original(db, org_id, target_id)
        # An /advance commits between the delete's read and its write.
        stored = await db_session.get(OrgSkillAssignment, target_id)
        assert stored is not None
        stored.superseded_at = datetime.now(timezone.utc)
        stored.revoked_at = stored.superseded_at
        stored.superseded_by_assignment_id = "osa_" + "f" * 24
        await db_session.commit()
        return row

    monkeypatch.setattr(marketplace, "_assignment_for_delete", _read_then_supersede)
    response = await client.delete(f"{url}/{assignment_id}", headers=auth_headers)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "assignment_changed"
    db_session.expire_all()
    kept = await db_session.get(OrgSkillAssignment, assignment_id)
    assert kept is not None and kept.superseded_by_assignment_id == "osa_" + "f" * 24


async def test_concurrent_create_loser_gets_use_advance(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sessionfs.server.services import skill_disclosure

    org, _project = await _org(db_session, test_user)
    v1 = await _publish(client, auth_headers, org=org)
    v2 = await _publish(client, auth_headers, org=org, body="Two.", version="2.0.0")
    url = f"/api/v1/orgs/{org.id}/skills/assignments"
    winner = await client.post(url, headers=auth_headers,
                               json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]})
    assert winner.status_code == 201
    real = skill_disclosure.previous_assignment_version
    calls = {"n": 0}

    async def _stale_precheck(db, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # the loser's pre-check ran before the winner committed
        return await real(db, **kwargs)

    monkeypatch.setattr(skill_disclosure, "previous_assignment_version", _stale_precheck)
    loser = await client.post(url, headers=auth_headers,
                              json={"version_id": v2["version_id"], "content_hash": v2["content_hash"]})
    assert loser.status_code == 409, loser.text
    assert loser.json()["error"]["code"] == "use_advance"
    assert loser.json()["error"]["details"]["assignment_id"] == winner.json()["id"]



async def test_approvals_are_serialized_per_scope_and_advance_locks_versions_first(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sessionfs.server.routes import marketplace_disclosure
    from sessionfs.server.services import skill_disclosure

    org, project = await _org(db_session, test_user)
    approver, approver_headers = await _user(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=approver.id, role="admin"))
    await db_session.commit()
    v1 = await _publish(client, auth_headers, org=org)
    v2 = await _publish(client, auth_headers, org=org, body="Two.", version="2.0.0")
    await _sequence_versions(db_session, v1, v2)
    order: list[str] = []
    real_lock = skill_disclosure.lock_grant_scope
    real_previous = skill_disclosure.previous_request_version

    async def _lock(db, key):
        order.append(f"lock:{key}")
        return await real_lock(db, key)

    async def _previous(db, row, package_id):
        order.append("previous")
        return await real_previous(db, row, package_id)

    monkeypatch.setattr(skill_disclosure, "lock_grant_scope", _lock)
    monkeypatch.setattr(skill_disclosure, "previous_request_version", _previous)
    created = await client.post(
        f"/api/v1/projects/{project.id}/skill-install-requests",
        headers=auth_headers, json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]},
    )
    approved = await client.post(
        f"/api/v1/projects/{project.id}/skill-install-requests/{created.json()['id']}/approve",
        headers=approver_headers, json={},
    )
    assert approved.status_code == 200
    body = created.json()
    expected = skill_disclosure.grant_scope_lock_key(
        project.id, v1["package_id"], "project", body["target_user_id"], body["target_device_id"]
    )
    assert order == [f"lock:{expected}", "previous"]

    # /advance: version rows (id order) are locked before the assignment row.
    url = f"/api/v1/orgs/{org.id}/skills/assignments"
    assignment = await client.post(url, headers=auth_headers,
                                   json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]})
    events: list = []
    real_versions = marketplace_disclosure.lock_versions_in_order

    async def _versions(db, ids):
        events.append(("versions", sorted(set(ids))))
        return await real_versions(db, ids)

    monkeypatch.setattr(marketplace_disclosure, "lock_versions_in_order", _versions)
    # A text advance still needs the reviewed digest; get it from a refused
    # attempt (which also locks the versions), then reset the probe so the
    # successful advance records exactly one ordered lock.
    refused = await client.post(
        f"{url}/{assignment.json()['id']}/advance", headers=auth_headers,
        json={"to_version_id": v2["version_id"], "content_hash": v2["content_hash"]},
    )
    assert refused.status_code == 409
    events.clear()
    advanced = await client.post(
        f"{url}/{assignment.json()['id']}/advance", headers=auth_headers,
        json={
            "to_version_id": v2["version_id"],
            "content_hash": v2["content_hash"],
            "update_digest": refused.json()["error"]["details"]["update_digest"],
        },
    )
    assert advanced.status_code == 200, advanced.text
    assert events == [("versions", sorted([v1["version_id"], v2["version_id"]]))]



async def test_grant_scope_lock_key_follows_the_scope() -> None:
    from sessionfs.server.services.skill_disclosure import grant_scope_lock_key as key

    assert key("p", "k", "project", "u1", None) == key("p", "k", "project", "u2", "d")
    assert key("p", "k", "user", "u1", "d1") == key("p", "k", "user", "u1", "d2")
    assert key("p", "k", "user", "u1", None) != key("p", "k", "user", "u2", None)
    assert key("p", "k", "device", "u1", "d1") != key("p", "k", "device", "u1", "d2")


async def test_ruleset_bump_without_new_findings_keeps_the_binding(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    from sessionfs.skills.instruction_scan import RULESET_VERSION

    org, _project = await _org(db_session, test_user)
    v1 = await _publish(client, auth_headers, org=org)
    v2 = await _publish(client, auth_headers, org=org, body="Two.", version="2.0.0")
    await _sequence_versions(db_session, v1, v2)
    url = f"/api/v1/orgs/{org.id}/skills/assignments"
    created = await client.post(url, headers=auth_headers,
                                json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]})
    # A newer scanner ruleset ran; it finds nothing new in v1.
    row = await db_session.get(SkillPackageVersion, v1["version_id"])
    assert row is not None
    manifest = json.loads(row.capability_manifest)
    manifest["scan"]["ruleset"] = "2000.01.1"
    row.capability_manifest = json.dumps(manifest)
    await db_session.commit()
    # Same recomputed digest: the binding holds, a text update needs no prompt.
    refused = await client.post(
        f"{url}/{created.json()['id']}/advance", headers=auth_headers,
        json={"to_version_id": v2["version_id"], "content_hash": v2["content_hash"]},
    )
    assert refused.status_code == 409
    assert refused.json()["error"]["details"]["update_class"] == "text"
    advanced = await client.post(
        f"{url}/{created.json()['id']}/advance", headers=auth_headers,
        json={
            "to_version_id": v2["version_id"],
            "content_hash": v2["content_hash"],
            "update_digest": refused.json()["error"]["details"]["update_digest"],
        },
    )
    assert advanced.status_code == 200, advanced.text
    assert advanced.json()["update_class"] == "text"
    # Reading the disclosure refreshes the stored manifest to the running ruleset.
    assert (await client.get(f"/api/v1/marketplace/versions/{v1['version_id']}/disclosure",
                             headers=auth_headers)).status_code == 200
    await db_session.refresh(row)
    assert json.loads(row.capability_manifest)["scan"]["ruleset"] == RULESET_VERSION


async def test_a_capability_only_the_new_ruleset_sees_makes_the_update_widening(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    from sessionfs.skills import disclosure

    org, _project = await _org(db_session, test_user)
    v1 = await _publish(client, auth_headers, org=org, body="Then run: echo 'alias x=y' >> ~/.bashrc")
    v2 = await _publish(client, auth_headers, org=org, body="Then run: echo 'alias x=y' >> ~/.bashrc\nMore.",
                        version="2.0.0")
    await _sequence_versions(db_session, v1, v2)
    url = f"/api/v1/orgs/{org.id}/skills/assignments"
    created = await client.post(url, headers=auth_headers,
                                json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]})
    # What the OLDER ruleset saw: no persistence target. The grant is bound
    # to that digest, and the stored manifest is stale.
    row = await db_session.get(SkillPackageVersion, v1["version_id"])
    assert row is not None
    current = json.loads(row.capability_manifest)
    assert current["persistence_targets"]
    older = dict(current)
    older["scan"] = {**current["scan"], "ruleset": "2000.01.1"}
    older_digest_source = {**older}
    for key in ("persistence_targets",):
        older_digest_source[key] = []
        older_digest_source[f"{key}_total"] = 0
        older_digest_source[f"{key}_set_digest"] = disclosure._sha(disclosure._canonical([]))
        older_digest_source[f"{key}_content_digest"] = disclosure._sha(disclosure._canonical([]))
    row.capability_manifest = json.dumps(older_digest_source)
    row.capability_digest = disclosure.capability_digest(older_digest_source)
    assignment = await db_session.get(OrgSkillAssignment, created.json()["id"])
    assert assignment is not None
    assignment.capability_digest = row.capability_digest
    await db_session.commit()
    held = await client.post(
        f"{url}/{created.json()['id']}/advance", headers=auth_headers,
        json={"to_version_id": v2["version_id"], "content_hash": v2["content_hash"]},
    )
    assert held.status_code == 409, held.text
    assert held.json()["error"]["details"]["update_class"] == "widening"


async def test_lazy_manifests_persist_in_version_id_order_and_legacy_grants_skip_the_old_row(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sessionfs.server.services import skill_disclosure

    org, _project = await _org(db_session, test_user)
    v1 = await _publish(client, auth_headers, org=org)
    v2 = await _publish(client, auth_headers, org=org, body="Two.", version="2.0.0")
    await _sequence_versions(db_session, v1, v2)
    for version_id in (v1["version_id"], v2["version_id"]):
        row = await db_session.get(SkillPackageVersion, version_id)
        assert row is not None
        row.manifest_version = 0
    await db_session.commit()
    order: list[str] = []
    real = skill_disclosure.persist_manifest

    async def _spy(db, version, manifest):
        order.append(version.id)
        return await real(db, version, manifest)

    monkeypatch.setattr(skill_disclosure, "persist_manifest", _spy)
    newer, older = sorted([v1["version_id"], v2["version_id"]], reverse=True)
    response = await client.get(f"/api/v1/marketplace/versions/{newer}/diff/{older}", headers=auth_headers)
    assert response.status_code == 200
    assert order == sorted(order)  # id order, whatever the request's order

    # A legacy assignment binding: /advance never computes the old manifest.
    url = f"/api/v1/orgs/{org.id}/skills/assignments"
    created = await client.post(url, headers=auth_headers,
                                json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]})
    assignment = await db_session.get(OrgSkillAssignment, created.json()["id"])
    assert assignment is not None
    assignment.consent_rule_version = None
    old_row = await db_session.get(SkillPackageVersion, v1["version_id"])
    assert old_row is not None
    old_row.manifest_version = 0
    await db_session.commit()
    order.clear()
    held = await client.post(
        f"{url}/{created.json()['id']}/advance", headers=auth_headers,
        json={"to_version_id": v2["version_id"], "content_hash": v2["content_hash"]},
    )
    assert held.status_code == 409
    assert v1["version_id"] not in order
    await db_session.refresh(old_row)
    assert old_row.manifest_version == 0


async def test_publish_refuses_a_package_that_cannot_be_analyzed_completely() -> None:
    from types import SimpleNamespace

    from fastapi import HTTPException

    from sessionfs.server.services.skill_disclosure import reject_incomplete_manifest

    reject_incomplete_manifest(SimpleNamespace(capability_manifest={"incomplete": []}))
    with pytest.raises(HTTPException) as raised:
        reject_incomplete_manifest(SimpleNamespace(capability_manifest={"incomplete": ["scan_truncated"]}))
    assert raised.value.status_code == 422


async def test_held_updates_queue_is_keyset_paginated(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _project = await _org(db_session, test_user)
    url = f"/api/v1/orgs/{org.id}/skills/assignments"
    expected = set()
    for index in range(3):
        response = await client.post(
            f"/api/v1/orgs/{org.id}/skills/packages",
            headers=auth_headers,
            files={"package": ("p.tar.gz", _tar(), "application/gzip")},
            data={"namespace": f"ns{index}", "collection": "kit", "visibility": "org"},
        )
        first = response.json()
        second = await client.post(
            f"/api/v1/orgs/{org.id}/skills/packages",
            headers=auth_headers,
            files={"package": ("p.tar.gz", _tar(body="Two.", version="2.0.0"), "application/gzip")},
            data={"namespace": f"ns{index}", "collection": "kit", "visibility": "org"},
        )
        assert second.status_code == 201
        await _sequence_versions(db_session, first, second.json())
        created = await client.post(url, headers=auth_headers,
                                    json={"version_id": first["version_id"], "content_hash": first["content_hash"]})
        expected.add(created.json()["id"])
    seen: list[str] = []
    cursor = None
    for _ in range(5):
        params = {"limit": 1, **({"cursor": cursor} if cursor else {})}
        page = (await client.get(f"{url}/updates", headers=auth_headers, params=params)).json()
        seen.extend(item["assignment"]["id"] for item in page["items"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    assert set(seen) == expected and len(seen) == 3
    assert (await client.get(f"{url}/updates", headers=auth_headers, params={"cursor": "%%%"})).status_code == 422



async def test_grants_are_bound_to_the_disclosure_that_was_shown(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, project = await _org(db_session, test_user)
    approver, approver_headers = await _user(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=approver.id, role="admin"))
    await db_session.commit()
    v1 = await _publish(client, auth_headers, org=org)
    v2 = await _publish(client, auth_headers, org=org, body="Two.", version="2.0.0")
    await _sequence_versions(db_session, v1, v2)
    shown = (await client.get(f"/api/v1/marketplace/versions/{v1['version_id']}/disclosure",
                              headers=auth_headers)).json()
    created = await client.post(
        f"/api/v1/projects/{project.id}/skill-install-requests",
        headers=auth_headers, json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]},
    )
    approve_url = f"/api/v1/projects/{project.id}/skill-install-requests/{created.json()['id']}/approve"
    stale = await client.post(approve_url, headers=approver_headers,
                              json={"capability_digest": "sha256:" + "0" * 64, "consent_rule_version": 2})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "disclosure_changed"
    old_rule = await client.post(approve_url, headers=approver_headers,
                                 json={"capability_digest": shown["capability_digest"], "consent_rule_version": 1})
    assert old_rule.status_code == 409 and old_rule.json()["error"]["code"] == "disclosure_changed"
    ok = await client.post(approve_url, headers=approver_headers,
                           json={"capability_digest": shown["capability_digest"],
                                 "consent_rule_version": shown["consent_rule_version"]})
    assert ok.status_code == 200, ok.text

    url = f"/api/v1/orgs/{org.id}/skills/assignments"
    refused = await client.post(url, headers=auth_headers, json={
        "version_id": v1["version_id"], "content_hash": v1["content_hash"],
        "capability_digest": "sha256:" + "1" * 64, "consent_rule_version": 2,
    })
    assert refused.status_code == 409 and refused.json()["error"]["code"] == "disclosure_changed"
    assigned = await client.post(url, headers=auth_headers, json={
        "version_id": v1["version_id"], "content_hash": v1["content_hash"],
        "capability_digest": shown["capability_digest"], "consent_rule_version": 2,
    })
    assert assigned.status_code == 201, assigned.text
    advance = f"{url}/{assigned.json()['id']}/advance"
    body = {"to_version_id": v2["version_id"], "content_hash": v2["content_hash"]}
    changed = await client.post(advance, headers=auth_headers,
                                json={**body, "capability_digest": "sha256:" + "2" * 64, "consent_rule_version": 2})
    assert changed.status_code == 409 and changed.json()["error"]["code"] == "disclosure_changed"
    # API callers that send no disclosure approve the access computed now,
    # but every advance is still bound to the reviewed diff: get the digest
    # from a refused attempt, then advance with the digest and no disclosure.
    needed = await client.post(advance, headers=auth_headers, json=body)
    assert needed.status_code == 409 and needed.json()["error"]["code"] == "reapproval_required"
    ok_advance = await client.post(advance, headers=auth_headers, json={
        **body, "update_digest": needed.json()["error"]["details"]["update_digest"],
    })
    assert ok_advance.status_code == 200, ok_advance.text


async def test_republishing_existing_bytes_still_refuses_an_incomplete_manifest(
    client: AsyncClient, auth_headers: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sessionfs.skills import publish_validation

    await _publish(client, auth_headers)
    real = publish_validation.compute_manifest

    def _incomplete(root, *, scan=None):
        manifest = real(root, scan=scan)
        manifest["incomplete"] = ["scan_truncated"]
        return manifest

    monkeypatch.setattr(publish_validation, "compute_manifest", _incomplete)
    again = await client.post(
        "/api/v1/users/me/skills/packages",
        headers=auth_headers,
        files={"package": ("p.tar.gz", _tar(), "application/gzip")},
        data={"namespace": "kit", "collection": "kit"},
    )
    assert again.status_code == 422, again.text
    assert again.json()["error"]["details"]["reason"] == "manifest_incomplete"



async def test_approval_binds_the_whole_card_and_supersedes_the_previous_version(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, project = await _org(db_session, test_user)
    approver, approver_headers = await _user(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=approver.id, role="admin"))
    await db_session.commit()
    v1 = await _publish(client, auth_headers, org=org)
    v2 = await _publish(client, auth_headers, org=org, body="Two.", version="2.0.0")
    url = f"/api/v1/projects/{project.id}/skill-install-requests"

    async def request(item: dict, headers: dict) -> str:
        created = await client.post(url, headers=headers, json={
            "version_id": item["version_id"], "content_hash": item["content_hash"], "target_tools": ["codex"],
        })
        assert created.status_code == 201, created.text
        return created.json()["id"]

    first = await request(v1, auth_headers)
    card = (await client.get(f"/api/v1/marketplace/versions/{v1['version_id']}/disclosure",
                             headers=auth_headers, params={"tools": "codex"})).json()
    binding = {"capability_digest": card["capability_digest"], "consent_rule_version": 2}
    stale = await client.post(f"{url}/{first}/approve", headers=approver_headers,
                              json={**binding, "disclosure_digest": "sha256:" + "9" * 64})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "disclosure_changed"
    # A card for other tools is not the card for this request.
    other_card = (await client.get(f"/api/v1/marketplace/versions/{v1['version_id']}/disclosure",
                                   headers=auth_headers, params={"tools": "claude-code"})).json()
    wrong = await client.post(f"{url}/{first}/approve", headers=approver_headers,
                              json={**binding, "disclosure_digest": other_card["disclosure_digest"]})
    assert wrong.status_code == 409
    ok = await client.post(f"{url}/{first}/approve", headers=approver_headers,
                           json={**binding, "disclosure_digest": card["disclosure_digest"]})
    assert ok.status_code == 200, ok.text

    # Approving v2 for the same project scope (even filed by another user)
    # supersedes the v1 approval: revoked, receipts invalidated, kept as history.
    second = await request(v2, approver_headers)
    assert (await client.post(f"{url}/{second}/approve", headers=auth_headers, json={})).status_code == 200
    old = await db_session.get(SkillInstallRequest, first)
    await db_session.refresh(old)
    assert old is not None and old.status == "revoked" and old.superseded_by_request_id == second
    receipts = (await db_session.execute(
        select(SkillConsentReceipt).where(SkillConsentReceipt.install_request_id == first)
    )).scalars().all()
    assert receipts and all(r.revoked_at is not None for r in receipts)
    approved = (await client.get(url, headers=auth_headers, params={"status": "approved"})).json()["items"]
    assert [item["id"] for item in approved] == [second]



async def test_approval_rechecks_carried_consent_under_lock(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import datetime, timezone

    from sessionfs.server.services import skill_disclosure

    org, project = await _org(db_session, test_user)
    approver, approver_headers = await _user(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=approver.id, role="admin"))
    await db_session.commit()
    v1 = await _publish(client, auth_headers, org=org, script=b"print(1)\n")
    v2 = await _publish(client, auth_headers, org=org, body="Reworded.", script=b"print(1)\n", version="2.0.0")
    url = f"/api/v1/projects/{project.id}/skill-install-requests"

    async def request(item: dict) -> str:
        created = await client.post(url, headers=auth_headers, json={
            "version_id": item["version_id"], "content_hash": item["content_hash"],
            "requested_capabilities": ["instructions", "scripts"],
        })
        return created.json()["id"]

    first = await request(v1)
    assert (await client.post(f"{url}/{first}/approve", headers=approver_headers,
                              json={"approved_capabilities": ["scripts"]})).status_code == 200
    second = await request(v2)
    real = skill_disclosure.carried_receipts

    async def _read_then_revoked(db, previous, update):
        carried = await real(db, previous, update)
        # A revocation commits right after the carried receipts were read.
        rows = (await db_session.execute(
            select(SkillConsentReceipt).where(SkillConsentReceipt.install_request_id == first)
        )).scalars().all()
        for receipt in rows:
            receipt.revoked_at = datetime.now(timezone.utc)
        await db_session.commit()
        return carried

    monkeypatch.setattr(skill_disclosure, "carried_receipts", _read_then_revoked)
    refused = await client.post(f"{url}/{second}/approve", headers=approver_headers, json={})
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "consent_changed"
    pending = await db_session.get(SkillInstallRequest, second)
    await db_session.refresh(pending)
    assert pending is not None and pending.status == "pending"


async def _approve_carried_update(client, auth_headers, db_session, test_user):
    org, project = await _org(db_session, test_user)
    approver, approver_headers = await _user(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=approver.id, role="admin"))
    await db_session.commit()
    v1 = await _publish(client, auth_headers, org=org, script=b"print(1)\n")
    v2 = await _publish(client, auth_headers, org=org, body="Reworded.", script=b"print(1)\n", version="2.0.0")
    url = f"/api/v1/projects/{project.id}/skill-install-requests"

    async def request(item: dict) -> str:
        created = await client.post(url, headers=auth_headers, json={
            "version_id": item["version_id"], "content_hash": item["content_hash"],
            "requested_capabilities": ["instructions", "scripts"],
        })
        return created.json()["id"]

    first = await request(v1)
    assert (await client.post(f"{url}/{first}/approve", headers=approver_headers,
                              json={"approved_capabilities": ["scripts"]})).status_code == 200
    second = await request(v2)
    return url, first, second, approver_headers


async def test_approval_refuses_when_the_source_request_was_revoked_meanwhile(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A block that revokes the source approval's REQUEST row after the
    carried receipts were read is seen under the request-row lock: the
    approval refuses instead of carrying consent from a revoked grant."""
    from sessionfs.server.services import skill_disclosure

    url, first, second, approver_headers = await _approve_carried_update(
        client, auth_headers, db_session, test_user
    )
    real = skill_disclosure.carried_receipts

    async def _read_then_request_revoked(db, previous, update):
        carried = await real(db, previous, update)
        source = await db_session.get(SkillInstallRequest, first)
        assert source is not None
        source.status = "revoked"
        await db_session.commit()
        return carried

    monkeypatch.setattr(skill_disclosure, "carried_receipts", _read_then_request_revoked)
    refused = await client.post(f"{url}/{second}/approve", headers=approver_headers, json={})
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "consent_changed"
    source = await db_session.get(SkillInstallRequest, first)
    await db_session.refresh(source)
    assert source is not None
    # Never rewritten as a supersession.
    assert source.superseded_at is None and source.superseded_by_request_id is None


async def test_approval_locks_requests_before_receipts_and_supersedes_only_active_rows(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Row-lock order shared with moderation's _revoke_grants: request rows
    before consent receipts (the inverse could deadlock on PostgreSQL), and
    the supersede UPDATE carries a status predicate so a row a concurrent
    block revoked keeps its revocation."""
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.ext.asyncio import AsyncSession as _AsyncSession

    url, first, second, approver_headers = await _approve_carried_update(
        client, auth_headers, db_session, test_user
    )
    statements: list[str] = []
    real_execute = _AsyncSession.execute

    async def _spy(self, statement, *args, **kwargs):
        try:
            statements.append(str(statement.compile(dialect=postgresql.dialect())))
        except Exception:  # noqa: BLE001 - text() and friends
            statements.append(str(statement))
        return await real_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(_AsyncSession, "execute", _spy)
    approved = await client.post(f"{url}/{second}/approve", headers=approver_headers, json={})
    monkeypatch.undo()
    assert approved.status_code == 200, approved.text
    source = await db_session.get(SkillInstallRequest, first)
    await db_session.refresh(source)
    assert source is not None and source.superseded_by_request_id == second

    def first_index(predicate) -> int:
        return next(index for index, sql in enumerate(statements) if predicate(sql))

    request_lock = first_index(
        lambda sql: "FOR UPDATE" in sql and "FROM skill_install_requests" in sql
    )
    receipt_lock = first_index(
        lambda sql: "FOR UPDATE" in sql and "FROM skill_consent_receipts" in sql
    )
    assert request_lock < receipt_lock
    # No receipt row is written or locked before the first request lock.
    assert not any(
        "skill_consent_receipts" in sql and ("FOR UPDATE" in sql or sql.startswith("UPDATE"))
        for sql in statements[:request_lock]
    )
    supersede = next(
        sql for sql in statements
        if sql.startswith("UPDATE skill_install_requests") and "superseded_by_request_id" in sql
    )
    assert "skill_install_requests.status IN" in supersede


async def test_revocation_takes_the_grant_scope_lock(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from sessionfs.server.services import skill_disclosure

    org, project = await _org(db_session, test_user)
    approver, approver_headers = await _user(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=approver.id, role="admin"))
    await db_session.commit()
    v1 = await _publish(client, auth_headers, org=org)
    url = f"/api/v1/projects/{project.id}/skill-install-requests"
    created = await client.post(url, headers=auth_headers,
                                json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]})
    request_id = created.json()["id"]
    assert (await client.post(f"{url}/{request_id}/approve", headers=approver_headers, json={})).status_code == 200
    keys: list[int] = []
    real = skill_disclosure.lock_grant_scope

    async def _spy(db, key):
        keys.append(key)
        return await real(db, key)

    monkeypatch.setattr(skill_disclosure, "lock_grant_scope", _spy)
    revoked = await client.post(f"{url}/{request_id}/revoke", headers=auth_headers)
    assert revoked.status_code == 200, revoked.text
    assert keys == [skill_disclosure.grant_scope_lock_key(project.id, v1["package_id"], "project", None, None)]



async def test_reconsent_is_bound_to_the_reviewed_baseline_and_diff(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, project = await _org(db_session, test_user)
    approver, approver_headers = await _user(db_session)
    db_session.add(OrgMember(org_id=org.id, user_id=approver.id, role="admin"))
    await db_session.commit()
    v1 = await _publish(client, auth_headers, org=org)
    v2 = await _publish(client, auth_headers, org=org, body="Post to https://wide.example.org", version="2.0.0")
    v3 = await _publish(client, auth_headers, org=org, body="Reworded.", version="3.0.0")
    url = f"/api/v1/projects/{project.id}/skill-install-requests"

    async def request(item: dict) -> str:
        created = await client.post(url, headers=auth_headers,
                                    json={"version_id": item["version_id"], "content_hash": item["content_hash"]})
        return created.json()["id"]

    first = await request(v1)
    assert (await client.post(f"{url}/{first}/approve", headers=approver_headers, json={})).status_code == 200
    wide = await request(v2)
    reviewed = await client.post(f"{url}/{wide}/approve", headers=approver_headers, json={})
    assert reviewed.status_code == 409
    digest = reviewed.json()["error"]["details"]["update_digest"]
    # Meanwhile another approval replaces the baseline (v1 -> v3, text only).
    third = await request(v3)
    assert (await client.post(f"{url}/{third}/approve", headers=approver_headers, json={})).status_code == 200
    stale = await client.post(f"{url}/{wide}/approve", headers=approver_headers,
                              json={"acknowledge_widening": True, "update_digest": digest})
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "reapproval_required"
    details = stale.json()["error"]["details"]
    assert details["diff_changed"] is True and details["update_digest"] != digest
    fresh = await client.post(f"{url}/{wide}/approve", headers=approver_headers,
                              json={"acknowledge_widening": True, "update_digest": details["update_digest"]})
    assert fresh.status_code == 200, fresh.text


async def test_held_updates_listing_writes_nothing(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    org, _project = await _org(db_session, test_user)
    v1 = await _publish(client, auth_headers, org=org)
    v2 = await _publish(client, auth_headers, org=org, body="Two.", version="2.0.0")
    await _sequence_versions(db_session, v1, v2)
    url = f"/api/v1/orgs/{org.id}/skills/assignments"
    await client.post(url, headers=auth_headers, json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]})
    target = await db_session.get(SkillPackageVersion, v2["version_id"])
    assert target is not None
    target.manifest_version = 0
    await db_session.commit()
    listed = await client.get(f"{url}/updates", headers=auth_headers)
    assert listed.status_code == 200
    [item] = listed.json()["items"]
    assert item["update_class"] == "text" and item["update_digest"].startswith("sha256:")
    await db_session.refresh(target)
    assert target.manifest_version == 0  # the GET persisted nothing


async def test_held_updates_list_an_undiffable_target_instead_of_failing(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User, blob_store
) -> None:
    """A target whose manifest cannot be computed (stale stored manifest and
    a missing archive) is listed as held with no diff and no update_digest —
    one such version never 503s the whole org's queue — and /advance keeps
    refusing it."""
    org, _project = await _org(db_session, test_user)
    v1 = await _publish(client, auth_headers, org=org)
    v2 = await _publish(client, auth_headers, org=org, body="Two.", version="2.0.0")
    await _sequence_versions(db_session, v1, v2)
    url = f"/api/v1/orgs/{org.id}/skills/assignments"
    created = await client.post(
        url, headers=auth_headers, json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]}
    )
    assert created.status_code == 201, created.text
    target = await db_session.get(SkillPackageVersion, v2["version_id"])
    assert target is not None
    target.manifest_version = 0  # force a lazy recompute ...
    await db_session.commit()
    await blob_store.delete(target.blob_key)  # ... that cannot succeed

    listed = await client.get(f"{url}/updates", headers=auth_headers)
    assert listed.status_code == 200, listed.text
    [item] = listed.json()["items"]
    assert item["to_version_id"] == v2["version_id"]
    assert item["diff_unavailable"] is True
    assert item["held"] is True and item["requires"] == "approve_widening"
    assert item["diff"] is None and item["update_digest"] is None

    advance_url = f"{url}/{created.json()['id']}/advance"
    body = {"to_version_id": v2["version_id"], "content_hash": v2["content_hash"], "approve_widening": True}
    refused = await client.post(advance_url, headers=auth_headers, json=body)
    assert refused.status_code == 503, refused.text
    rows = (
        await db_session.execute(select(OrgSkillAssignment).where(OrgSkillAssignment.org_id == org.id))
    ).scalars().all()
    assert [row.version_id for row in rows] == [v1["version_id"]]


async def test_stale_baseline_reconsent_is_bound_to_the_full_diff(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """A stale (legacy) binding fails closed to an unknown baseline, but the
    acknowledgement is still bound to the FULL diff of the target: a
    provenance change to the same target version invalidates the digest that
    was shown (diff_changed), and the 409 carries the exact diff object with
    the hashed fields (changed_requirements, capability_digest_changed)."""
    org, _project = await _org(db_session, test_user)
    v1 = await _publish(client, auth_headers, org=org)
    v2 = await _publish(client, auth_headers, org=org, body="Two.", version="2.0.0")
    await _sequence_versions(db_session, v1, v2)
    url = f"/api/v1/orgs/{org.id}/skills/assignments"
    created = await client.post(url, headers=auth_headers,
                                json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]})
    assert created.status_code == 201, created.text
    # A legacy binding: the baseline is unknown (D2 fails closed to widening).
    assignment = await db_session.get(OrgSkillAssignment, created.json()["id"])
    assert assignment is not None
    assignment.consent_rule_version = None
    await db_session.commit()

    advance = f"{url}/{created.json()['id']}/advance"
    body = {"to_version_id": v2["version_id"], "content_hash": v2["content_hash"]}
    shown = await client.post(advance, headers=auth_headers, json=body)
    assert shown.status_code == 409
    details = shown.json()["error"]["details"]
    diff = details["diff"]
    assert diff["baseline_known"] is False and "baseline_digest" not in diff
    assert diff["capability_digest_changed"] is True and diff["changed_requirements"]
    assert diff["files"]["unknown"] is True
    # The target's scan and provenance are part of what is hashed and shown.
    assert diff["scan_delta"]["new_ruleset"] is not None
    assert diff["provenance_delta"]["new"] == {"publisher_verified": False, "attested": False}

    # The target's provenance flips after the diff was reviewed: the
    # acknowledgement bound to the old diff no longer matches.
    target = await db_session.get(SkillPackageVersion, v2["version_id"])
    assert target is not None
    target.signature_metadata = json.dumps({"publisher_verified": True})
    await db_session.commit()
    stale = await client.post(advance, headers=auth_headers, json={
        **body, "approve_widening": True, "update_digest": details["update_digest"],
    })
    assert stale.status_code == 409
    refreshed = stale.json()["error"]["details"]
    assert refreshed["diff_changed"] is True and refreshed["update_digest"] != details["update_digest"]
    assert refreshed["diff"]["provenance_delta"]["new"]["publisher_verified"] is True
    advanced = await client.post(advance, headers=auth_headers, json={
        **body, "approve_widening": True, "update_digest": refreshed["update_digest"],
    })
    assert advanced.status_code == 200, advanced.text


async def test_text_diff_needs_both_archives_whatever_the_manifests_say(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, blob_store
) -> None:
    """With text=true, a missing archive is a structured 503 diff_unavailable
    on EITHER side — even when the version predates stored manifests (no
    manifest could be computed from the missing blob anyway)."""
    v1 = await _publish(client, auth_headers, body="One.")
    v2 = await _publish(client, auth_headers, body="Two.", version="2.0.0")

    old_row = await db_session.get(SkillPackageVersion, v1["version_id"])
    assert old_row is not None
    old_row.manifest_version = 0  # pre-070: no stored manifest
    await db_session.commit()
    await blob_store.delete(old_row.blob_key)
    missing_old = await client.get(
        f"/api/v1/marketplace/versions/{v1['version_id']}/diff/{v2['version_id']}",
        headers=auth_headers,
    )
    assert missing_old.status_code == 503
    assert missing_old.json()["error"]["code"] == "diff_unavailable"

    new_row = await db_session.get(SkillPackageVersion, v2["version_id"])
    assert new_row is not None
    new_row.manifest_version = 0
    await db_session.commit()
    await blob_store.delete(new_row.blob_key)
    missing_new = await client.get(
        f"/api/v1/marketplace/versions/{v1['version_id']}/diff/{v2['version_id']}",
        headers=auth_headers,
        params={"text": "false"},
    )
    assert missing_new.status_code == 503
    assert missing_new.json()["error"]["code"] == "diff_unavailable"


async def test_publication_order_is_monotonic_and_ignores_random_ids(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """Versions are ordered by a monotonic, package-scoped publication sequence
    allocated at publish time — not by (created_at, random id). Two publishes
    within the same SQLite second still sort in publish order, so the queue
    deterministically proposes the later version."""
    org, _project = await _org(db_session, test_user)
    # No _sequence_versions: SQLite's second-precision created_at ties here and
    # the random ids must never decide which version is newer.
    v1 = await _publish(client, auth_headers, org=org, body="One.")
    v2 = await _publish(client, auth_headers, org=org, body="Two.", version="2.0.0")
    first_row = await db_session.get(SkillPackageVersion, v1["version_id"])
    second_row = await db_session.get(SkillPackageVersion, v2["version_id"])
    assert first_row is not None and second_row is not None
    assert first_row.publication_seq is not None and second_row.publication_seq is not None
    assert second_row.publication_seq > first_row.publication_seq

    url = f"/api/v1/orgs/{org.id}/skills/assignments"
    created = await client.post(url, headers=auth_headers,
                                json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]})
    assert created.status_code == 201, created.text
    [item] = (await client.get(f"{url}/updates", headers=auth_headers)).json()["items"]
    assert item["to_version_id"] == v2["version_id"]


async def test_held_updates_offer_the_newest_deliverable_version(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
) -> None:
    """The queue never blindly follows the package's current pointer: when it
    is quarantined, the newest DELIVERABLE version after the assigned one is
    offered; when the pointer was republished to an older version, no
    downgrade is proposed and /advance rejects a non-newer target."""
    from datetime import datetime, timezone

    from sessionfs.server.db.models import SkillPackage

    org, _project = await _org(db_session, test_user)
    url = f"/api/v1/orgs/{org.id}/skills/assignments"
    v1 = await _publish(client, auth_headers, org=org)
    v2 = await _publish(client, auth_headers, org=org, body="Two.", version="2.0.0")
    v3 = await _publish(client, auth_headers, org=org, body="Three.", version="3.0.0")
    await _sequence_versions(db_session, v1, v2, v3)

    created = await client.post(url, headers=auth_headers,
                                json={"version_id": v1["version_id"], "content_hash": v1["content_hash"]})
    assert created.status_code == 201, created.text
    listed = await client.get(f"{url}/updates", headers=auth_headers)
    [item] = listed.json()["items"]
    assert item["to_version_id"] == v3["version_id"]

    # The current pointer is quarantined: the queue offers the newest
    # deliverable version instead of dropping the update.
    newest = await db_session.get(SkillPackageVersion, v3["version_id"])
    assert newest is not None
    newest.quarantined_at = datetime(2026, 10, 2, tzinfo=timezone.utc)
    await db_session.commit()
    listed = await client.get(f"{url}/updates", headers=auth_headers)
    [item] = listed.json()["items"]
    assert item["to_version_id"] == v2["version_id"]

    # A second package whose current pointer was republished to an OLDER
    # version: no downgrade is proposed, and /advance refuses it.
    for version, body in (("1.0.0", "One."), ("2.0.0", "Two.")):
        response = await client.post(
            f"/api/v1/orgs/{org.id}/skills/packages",
            headers=auth_headers,
            files={"package": ("p.tar.gz", _tar(body=body, version=version), "application/gzip")},
            data={"namespace": "other", "collection": "kit", "visibility": "org"},
        )
        assert response.status_code == 201, response.text
        if version == "1.0.0":
            w1 = response.json()
        else:
            w2 = response.json()
    await _sequence_versions(db_session, w1, w2)
    package = await db_session.get(SkillPackage, w2["package_id"])
    assert package is not None
    package.current_version_id = w1["version_id"]  # republished older current
    assigned = await client.post(url, headers=auth_headers,
                                 json={"version_id": w2["version_id"], "content_hash": w2["content_hash"]})
    assert assigned.status_code == 201, assigned.text
    await db_session.commit()

    listed = await client.get(f"{url}/updates", headers=auth_headers)
    assert [entry["assignment"]["id"] for entry in listed.json()["items"]] == [created.json()["id"]]
    downgrade = await client.post(
        f"{url}/{assigned.json()['id']}/advance", headers=auth_headers,
        json={"to_version_id": w1["version_id"], "content_hash": w1["content_hash"]},
    )
    assert downgrade.status_code == 409
    assert downgrade.json()["error"]["code"] == "not_newer"
