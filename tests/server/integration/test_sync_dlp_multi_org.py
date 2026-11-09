"""Session-push DLP for a person in several organizations.

A push is scanned under the strictest combination of every org it could
link into (the caller's memberships plus orgs of projects they own), never
the default org alone: a user whose default org is lenient still can't push
a session into a stricter org's project, and a personal push gets the same
union.
"""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.db.models import OrgMember, Organization, Project, Session, User
from sessionfs.server.helper.redaction import strictest_dlp_policy

_SECRET_LINE = "the launch code is ACME-PROJECT-4242 do not share"


def _archive(session_id: str, git_remote: str | None) -> bytes:
    manifest = json.dumps({
        "sfs_version": "0.1.0",
        "session_id": session_id,
        "title": "multi-org dlp",
        "source": {"tool": "claude-code"},
        "stats": {"message_count": 1, "turn_count": 1},
    }).encode()
    messages = (
        json.dumps({
            "msg_id": "m1",
            "role": "user",
            "content": [{"type": "text", "text": _SECRET_LINE}],
        }) + "\n"
    ).encode()
    files = [("manifest.json", manifest), ("messages.jsonl", messages)]
    if git_remote:
        files.append(("workspace.json", json.dumps({"git_remote": git_remote}).encode()))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in files:
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


async def _orgs(db: AsyncSession, user: User) -> tuple[Organization, Organization]:
    """A: lenient (warn, secrets only) and the user's default org.
    B: BLOCK with a custom pattern that matches the session text."""
    now = datetime.now(timezone.utc)
    org_a = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}", name="Lenient", slug=f"len-{uuid.uuid4().hex[:6]}",
        tier="team", seats_limit=10,
        settings=json.dumps({"dlp": {"enabled": True, "mode": "warn", "categories": ["secrets"]}}),
    )
    org_b = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}", name="Strict", slug=f"str-{uuid.uuid4().hex[:6]}",
        tier="team", seats_limit=10,
        settings=json.dumps({"dlp": {
            "enabled": True, "mode": "block", "categories": ["secrets"],
            "custom_patterns": [
                {"name": "launch_code", "regex": r"ACME-PROJECT-\d{4}", "category": "secret", "severity": "high"}
            ],
        }}),
    )
    db.add_all([org_a, org_b])
    await db.flush()
    db.add(OrgMember(org_id=org_a.id, user_id=user.id, role="member", joined_at=now - timedelta(hours=2)))
    db.add(OrgMember(org_id=org_b.id, user_id=user.id, role="member", joined_at=now))
    user.default_org_id = org_a.id
    await db.commit()
    return org_a, org_b


async def _push(client: AsyncClient, headers: dict, session_id: str, remote: str | None):
    return await client.put(
        f"/api/v1/sessions/{session_id}/sync",
        headers=headers,
        files={"file": ("s.tar.gz", io.BytesIO(_archive(session_id, remote)), "application/gzip")},
    )


@pytest.mark.asyncio
async def test_push_into_strict_orgs_project_is_blocked_despite_lenient_default(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
):
    _, org_b = await _orgs(db_session, test_user)
    db_session.add(Project(
        id=f"proj_{uuid.uuid4().hex[:16]}", name="strict-repo",
        git_remote_normalized="github.com/strict/repo", owner_id=test_user.id, org_id=org_b.id,
    ))
    await db_session.commit()

    resp = await _push(client, auth_headers, "ses_mdlpblock000001", "git@github.com:strict/repo.git")
    assert resp.status_code == 403, resp.text[:300]
    stored = (
        await db_session.execute(select(Session.id).where(Session.id == "ses_mdlpblock000001"))
    ).scalar_one_or_none()
    assert stored is None


@pytest.mark.asyncio
async def test_personal_push_gets_the_union_too(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
):
    await _orgs(db_session, test_user)
    resp = await _push(client, auth_headers, "ses_mdlppersonal001", None)
    assert resp.status_code == 403, resp.text[:300]


@pytest.mark.asyncio
async def test_single_org_policy_is_used_as_stored(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
):
    """Only the lenient org: the pattern isn't in play and warn never blocks
    (the behaviour before multi-org)."""
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}", name="Lenient", slug=f"len-{uuid.uuid4().hex[:6]}",
        tier="team", seats_limit=10,
        settings=json.dumps({"dlp": {"enabled": True, "mode": "warn", "categories": ["secrets"]}}),
    )
    db_session.add(org)
    await db_session.flush()
    db_session.add(OrgMember(org_id=org.id, user_id=test_user.id, role="member"))
    await db_session.commit()

    resp = await _push(client, auth_headers, "ses_mdlpsingle0001", None)
    assert resp.status_code == 201, resp.text[:300]


def test_strictest_dlp_policy_combines_mode_categories_and_allowlist():
    combined = strictest_dlp_policy([
        {"enabled": True, "mode": "warn", "allowlist": ["a", "b"]},
        {"enabled": True, "mode": "redact", "categories": ["phi"], "allowlist": ["b"],
         "custom_patterns": [{"name": "mine", "regex": "X"}]},
        {"enabled": False, "mode": "block", "categories": ["pii"]},
    ])
    assert combined is not None
    assert combined["enabled"] is True
    assert combined["mode"] == "redact"  # disabled block policy doesn't count
    assert set(combined["categories"]) == {"secrets", "phi"}  # missing categories = secrets
    assert combined["allowlist"] == ["b"]
    # Org-private pattern names are relabelled when several orgs combine.
    assert combined["custom_patterns"] == [{"name": "custom", "regex": "X"}]
    assert strictest_dlp_policy([None, {"enabled": False}]) is None


# ── POST /api/v1/sessions runs the same gate ───────────────────────────

_PASSWORD_TEXT = 'password = "supersecretvalue123"'


def _password_archive(remote: str | None = None) -> bytes:
    manifest = json.dumps({
        "sfs_version": "0.1.0",
        "session_id": "ses_ignored",
        "title": "upload dlp",
        "source": {"tool": "claude-code"},
        "stats": {"message_count": 1, "turn_count": 1},
    }).encode()
    messages = (
        json.dumps({
            "msg_id": "m1",
            "role": "assistant",
            "content": [{"type": "text", "text": _PASSWORD_TEXT}],
        }) + "\n"
    ).encode()
    files = [("manifest.json", manifest), ("messages.jsonl", messages)]
    if remote:
        files.append(("workspace.json", json.dumps({"git_remote": remote}).encode()))
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, payload in files:
            info = tarfile.TarInfo(name=name)
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
    return buf.getvalue()


async def _single_policy_org(db: AsyncSession, user: User, mode: str) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}", name=f"DLP {mode}", slug=f"dlp-{uuid.uuid4().hex[:6]}",
        tier="team", seats_limit=10,
        settings=json.dumps({"dlp": {"enabled": True, "mode": mode, "categories": ["secrets"]}}),
    )
    db.add(org)
    await db.flush()
    db.add(OrgMember(org_id=org.id, user_id=user.id, role="member"))
    await db.commit()
    return org


async def _upload(client: AsyncClient, headers: dict, archive: bytes):
    return await client.post(
        "/api/v1/sessions?source_tool=claude-code",
        headers=headers,
        files={"file": ("s.tar.gz", io.BytesIO(archive), "application/gzip")},
    )


async def _user_sessions(db: AsyncSession, user: User) -> list[Session]:
    return list(
        (await db.execute(select(Session).where(Session.user_id == user.id))).scalars().all()
    )


async def _stored_messages(client: AsyncClient, headers: dict, session_id: str) -> str:
    pull = await client.get(f"/api/v1/sessions/{session_id}/sync", headers=headers)
    assert pull.status_code == 200, pull.text[:300]
    with tarfile.open(fileobj=io.BytesIO(pull.content), mode="r:gz") as tar:
        return tar.extractfile("messages.jsonl").read().decode("utf-8")


@pytest.mark.asyncio
async def test_upload_block_refused_and_nothing_stored(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
):
    await _single_policy_org(db_session, test_user, "block")
    resp = await _upload(client, auth_headers, _password_archive())
    assert resp.status_code == 403, resp.text[:300]
    assert "dlp_blocked" in resp.text
    assert await _user_sessions(db_session, test_user) == []


@pytest.mark.asyncio
async def test_upload_redact_stores_redacted_content(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
):
    await _single_policy_org(db_session, test_user, "redact")
    resp = await _upload(client, auth_headers, _password_archive())
    assert resp.status_code == 201, resp.text[:300]
    sid = resp.json()["session_id"]
    stored = await _stored_messages(client, auth_headers, sid)
    assert "supersecretvalue123" not in stored
    assert "[REDACTED:generic_password_assignment]" in stored
    row = (await db_session.execute(select(Session).where(Session.id == sid))).scalar_one()
    assert "supersecretvalue123" not in (row.messages_text or "")
    assert json.loads(row.dlp_scan_results)["action_taken"] == "redact"


@pytest.mark.asyncio
async def test_upload_warn_stores_with_findings(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
):
    await _single_policy_org(db_session, test_user, "warn")
    resp = await _upload(client, auth_headers, _password_archive())
    assert resp.status_code == 201, resp.text[:300]
    sid = resp.json()["session_id"]
    assert "supersecretvalue123" in await _stored_messages(client, auth_headers, sid)
    row = (await db_session.execute(select(Session).where(Session.id == sid))).scalar_one()
    results = json.loads(row.dlp_scan_results)
    assert results["mode"] == "warn"
    assert results["findings_count"] >= 1
    assert results["action_taken"] == "warn"


@pytest.mark.asyncio
async def test_upload_multi_org_uses_strictest_policy(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
):
    _, org_b = await _orgs(db_session, test_user)
    db_session.add(Project(
        id=f"proj_{uuid.uuid4().hex[:16]}", name="strict-repo-up",
        git_remote_normalized="github.com/strict/repo-up", owner_id=test_user.id, org_id=org_b.id,
    ))
    await db_session.commit()
    resp = await _upload(
        client, auth_headers, _archive("ses_ignored", "git@github.com:strict/repo-up.git")
    )
    assert resp.status_code == 403, resp.text[:300]
    assert await _user_sessions(db_session, test_user) == []


@pytest.mark.asyncio
async def test_upload_without_policy_unchanged(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession, test_user: User
):
    resp = await _upload(client, auth_headers, _password_archive())
    assert resp.status_code == 201, resp.text[:300]
    sid = resp.json()["session_id"]
    assert "supersecretvalue123" in await _stored_messages(client, auth_headers, sid)
    row = (await db_session.execute(select(Session).where(Session.id == sid))).scalar_one()
    assert row.dlp_scan_results is None
