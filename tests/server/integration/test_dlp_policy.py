"""DLP policy route regression tests.

``validate_dlp_policy`` used to compile every stored custom pattern, so an org
whose saved policy predated strict validation (patterns missing category /
severity, or carrying an uncompilable regex) could no longer save ANY change —
even ``{"enabled": false}``.  These tests pin the fix: stored patterns are
normalized (category/severity defaulted) and only caller-supplied regexes are
compiled, while an invalid regex the caller actually submits still 422s.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import (
    ApiKey,
    OrgMember,
    Organization,
    User,
)


async def _make_user(db: AsyncSession, name: str = "admin") -> tuple[User, str]:
    user = User(
        id=str(uuid.uuid4()),
        email=f"{name}-{uuid.uuid4().hex[:6]}@example.com",
        display_name=name.capitalize(),
        tier="team",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    raw = generate_api_key()
    db.add(
        ApiKey(
            id=str(uuid.uuid4()),
            user_id=user.id,
            key_hash=hash_api_key(raw),
            name=f"{name}-key",
            created_at=datetime.now(timezone.utc),
        )
    )
    await db.commit()
    return user, raw


def _hdrs(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


async def _make_org_admin(db: AsyncSession, admin: User) -> Organization:
    org = Organization(
        id=f"org_{uuid.uuid4().hex[:12]}",
        name="Test Org",
        slug=f"test-{uuid.uuid4().hex[:6]}",
        tier="team",
        seats_limit=10,
    )
    db.add(org)
    await db.commit()
    db.add(OrgMember(org_id=org.id, user_id=admin.id, role="admin"))
    await db.commit()
    await db.refresh(org)
    return org


async def _seed_dlp(db: AsyncSession, org: Organization, policy: dict) -> None:
    org.settings = json.dumps({"dlp": policy})
    await db.commit()
    await db.refresh(org)


@pytest.mark.asyncio
async def test_update_policy_succeeds_with_old_shape_stored_pattern(
    client: AsyncClient, db_session: AsyncSession
):
    """A stored pattern missing category/severity must not block a save."""
    admin, key = await _make_user(db_session)
    org = await _make_org_admin(db_session, admin)
    await _seed_dlp(db_session, org, {
        "enabled": True,
        "mode": "warn",
        "categories": ["secrets"],
        "custom_patterns": [{"name": "old", "regex": r"SECRET-\d+"}],
    })

    resp = await client.put(
        "/api/v1/dlp/policy", headers=_hdrs(key), json={"enabled": False}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["enabled"] is False
    # The old pattern survived, normalized to the default category/severity.
    assert body["custom_patterns"][0]["category"] == "secret"
    assert body["custom_patterns"][0]["severity"] == "high"


@pytest.mark.asyncio
async def test_update_policy_succeeds_with_stored_invalid_regex(
    client: AsyncClient, db_session: AsyncSession
):
    """A stored uncompilable regex must not block an unrelated save."""
    admin, key = await _make_user(db_session)
    org = await _make_org_admin(db_session, admin)
    await _seed_dlp(db_session, org, {
        "enabled": True,
        "mode": "warn",
        "categories": ["secrets"],
        "custom_patterns": [
            {"name": "bad", "regex": "(", "category": "secret", "severity": "high"}
        ],
    })

    resp = await client.put(
        "/api/v1/dlp/policy", headers=_hdrs(key), json={"mode": "redact"}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["mode"] == "redact"
    # The stored bad regex is preserved untouched (skipped safely at scan time).
    assert resp.json()["custom_patterns"][0]["regex"] == "("


@pytest.mark.asyncio
async def test_update_policy_rejects_invalid_regex_in_request(
    client: AsyncClient, db_session: AsyncSession
):
    """An invalid regex the caller actually submits still 422s."""
    admin, key = await _make_user(db_session)
    await _make_org_admin(db_session, admin)

    resp = await client.put(
        "/api/v1/dlp/policy",
        headers=_hdrs(key),
        json={
            "custom_patterns": [
                {"name": "bad", "regex": "(", "category": "secret", "severity": "high"}
            ]
        },
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_update_policy_succeeds_with_stored_malformed_pattern(
    client: AsyncClient, db_session: AsyncSession
):
    """Stored non-dict / missing-name / missing-regex patterns must not block a
    save: they are dropped silently and the well-formed ones survive."""
    admin, key = await _make_user(db_session)
    org = await _make_org_admin(db_session, admin)
    await _seed_dlp(db_session, org, {
        "enabled": True,
        "mode": "warn",
        "categories": ["secrets"],
        "custom_patterns": [
            "not-a-dict",
            {"name": "no_regex"},
            {"name": "ok", "regex": r"OK-\d+", "category": "secret", "severity": "high"},
        ],
    })

    resp = await client.put(
        "/api/v1/dlp/policy", headers=_hdrs(key), json={"mode": "redact"}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mode"] == "redact"
    assert [cp["name"] for cp in body["custom_patterns"]] == ["ok"]


@pytest.mark.asyncio
async def test_get_policy_reports_invalid_custom_patterns(
    client: AsyncClient, db_session: AsyncSession
):
    """GET exposes the malformed stored patterns so admins can find and fix."""
    admin, key = await _make_user(db_session)
    org = await _make_org_admin(db_session, admin)
    await _seed_dlp(db_session, org, {
        "enabled": True,
        "mode": "warn",
        "categories": ["secrets"],
        "custom_patterns": [
            {"name": "ok", "regex": r"OK-\d+", "category": "secret", "severity": "high"},
            "not-a-dict",
            {"name": "no_regex"},
        ],
    })

    resp = await client.get("/api/v1/dlp/policy", headers=_hdrs(key))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    invalid = body["invalid_custom_patterns"]
    assert [i["index"] for i in invalid] == [1, 2]
    assert invalid[0]["reason"] == "entry is not a dict"
    assert invalid[1]["name"] == "no_regex"
    assert invalid[1]["reason"] == "missing 'regex'"
