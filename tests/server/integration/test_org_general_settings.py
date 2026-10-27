"""v0.10.0 Phase 6 — org general settings regression tests.

Covers GET/PUT /api/v1/orgs/{org_id}/settings. DLP policy is handled
by its own route (routes/dlp.py); this surface owns the three
kb_retention_days / kb_max_context_words / kb_section_page_limit
creation defaults that new org-scoped projects inherit at create
time (routes/projects.py:create_project). Round 3 (KB entry 298)
removed the earlier retention_days / compile_model fields after
Codex flagged they had no runtime consumer.
"""

from __future__ import annotations

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


async def _make_user(db: AsyncSession, name: str = "alice") -> tuple[User, str]:
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


async def _make_org_with(
    db: AsyncSession,
    admin: User,
    member: User | None = None,
) -> Organization:
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
    if member is not None:
        db.add(OrgMember(org_id=org.id, user_id=member.id, role="member"))
    await db.commit()
    await db.refresh(org)
    return org


@pytest.mark.asyncio
async def test_get_settings_returns_defaults_when_unset(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    resp = await client.get(f"/api/v1/orgs/{org.id}/settings", headers=_hdrs(key))
    assert resp.status_code == 200
    body = resp.json()
    # All fields are None when no override is set.
    assert all(body[k] is None for k in (
        "kb_retention_days", "kb_max_context_words", "kb_section_page_limit",
        "helper_retention_days",
    ))
    # auto_join_domains defaults to an empty list (GitHub #59, v0.17.0).
    assert body["auto_join_domains"] == []
    # Restricted project visibility (GitHub #101) defaults to False, not None.
    assert body["restrict_project_visibility"] is False
    # Phase 6 Round 3 (KB 298) — retention_days and compile_model are
    # not part of the schema anymore (no runtime consumer).
    assert "retention_days" not in body
    assert "compile_model" not in body


@pytest.mark.asyncio
async def test_admin_can_update_all_fields(
    client: AsyncClient, db_session: AsyncSession
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    payload = {
        "kb_retention_days": 90,
        "kb_max_context_words": 4000,
        "kb_section_page_limit": 50,
        "helper_retention_days": 30,
        "auto_join_domains": [],
    }
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/settings", headers=_hdrs(key), json=payload
    )
    assert resp.status_code == 200, resp.text
    # restrict_project_visibility (GitHub #101) always reads back as a bool.
    expected = {**payload, "restrict_project_visibility": False}
    assert resp.json() == expected

    # Round-trip via GET.
    resp2 = await client.get(f"/api/v1/orgs/{org.id}/settings", headers=_hdrs(key))
    assert resp2.json() == expected


@pytest.mark.asyncio
async def test_partial_update_preserves_other_fields(
    client: AsyncClient, db_session: AsyncSession
):
    """A partial PUT merges into the stored general block: fields the caller
    doesn't provide are preserved, not wiped (N4)."""
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    # Establish baseline.
    await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=_hdrs(key),
        json={"kb_retention_days": 365, "kb_max_context_words": 1000},
    )
    # Partial update — only kb_retention_days; kb_max_context_words survives.
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=_hdrs(key),
        json={"kb_retention_days": 180},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kb_retention_days"] == 180
    assert body["kb_max_context_words"] == 1000  # preserved by the merge

    # Round-trip via GET confirms the stored value, not just the response.
    got = await client.get(f"/api/v1/orgs/{org.id}/settings", headers=_hdrs(key))
    assert got.json()["kb_max_context_words"] == 1000


@pytest.mark.asyncio
async def test_unrelated_save_preserves_helper_retention_and_kb_fields(
    client: AsyncClient, db_session: AsyncSession
):
    """Setting helper_retention_days and then saving only a kb_* field must
    leave the retention window intact, and vice versa (N4)."""
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)

    await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=_hdrs(key),
        json={"helper_retention_days": 30, "kb_section_page_limit": 50},
    )

    # An unrelated kb_* save must not wipe helper_retention_days.
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=_hdrs(key),
        json={"kb_max_context_words": 4000},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["helper_retention_days"] == 30
    assert body["kb_section_page_limit"] == 50
    assert body["kb_max_context_words"] == 4000

    # And an unrelated helper_retention_days save must not wipe kb_* fields.
    resp2 = await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=_hdrs(key),
        json={"helper_retention_days": 7},
    )
    assert resp2.status_code == 200
    assert resp2.json()["kb_max_context_words"] == 4000
    assert resp2.json()["kb_section_page_limit"] == 50
    assert resp2.json()["helper_retention_days"] == 7


@pytest.mark.asyncio
async def test_explicit_null_clears_helper_retention_days(
    client: AsyncClient, db_session: AsyncSession
):
    """Sending ``"helper_retention_days": null`` clears the override (the key is
    removed), while omitting the field leaves it unchanged (R3-4)."""
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)

    await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=_hdrs(key),
        json={"helper_retention_days": 30, "kb_retention_days": 90},
    )

    # Explicit null clears the override -> reverts to the platform default.
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=_hdrs(key),
        json={"helper_retention_days": None},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["helper_retention_days"] is None
    assert body["kb_retention_days"] == 90  # untouched by the merge

    got = await client.get(f"/api/v1/orgs/{org.id}/settings", headers=_hdrs(key))
    assert got.json()["helper_retention_days"] is None


@pytest.mark.asyncio
async def test_explicit_null_clears_kb_field_keeps_others(
    client: AsyncClient, db_session: AsyncSession
):
    """Sending ``"kb_retention_days": null`` clears only that override, leaving
    the other kb_* and helper fields intact (R3-4)."""
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)

    await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=_hdrs(key),
        json={
            "kb_retention_days": 365,
            "kb_max_context_words": 4000,
            "helper_retention_days": 30,
        },
    )

    resp = await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=_hdrs(key),
        json={"kb_retention_days": None},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kb_retention_days"] is None
    assert body["kb_max_context_words"] == 4000
    assert body["helper_retention_days"] == 30

    got = await client.get(f"/api/v1/orgs/{org.id}/settings", headers=_hdrs(key))
    assert got.json()["kb_retention_days"] is None


@pytest.mark.asyncio
async def test_non_admin_cannot_update(
    client: AsyncClient, db_session: AsyncSession
):
    admin, _ = await _make_user(db_session, "admin")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org_with(db_session, admin, member)
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=_hdrs(member_key),
        json={"kb_retention_days": 100},
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_non_member_cannot_read(
    client: AsyncClient, db_session: AsyncSession
):
    admin, _ = await _make_user(db_session, "admin")
    outsider, outsider_key = await _make_user(db_session, "outsider")
    org = await _make_org_with(db_session, admin)
    resp = await client.get(
        f"/api/v1/orgs/{org.id}/settings", headers=_hdrs(outsider_key)
    )
    assert resp.status_code in (403, 404), resp.text


@pytest.mark.asyncio
async def test_member_can_read(
    client: AsyncClient, db_session: AsyncSession
):
    admin, _ = await _make_user(db_session, "admin")
    member, member_key = await _make_user(db_session, "member")
    org = await _make_org_with(db_session, admin, member)
    resp = await client.get(
        f"/api/v1/orgs/{org.id}/settings", headers=_hdrs(member_key)
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field,bad_value",
    [
        ("kb_retention_days", 0),
        ("kb_retention_days", 731),
        ("kb_retention_days", -5),
        ("kb_max_context_words", 99),
        ("kb_max_context_words", 50001),
        ("kb_section_page_limit", 0),
        ("kb_section_page_limit", 201),
        ("helper_retention_days", 0),
        ("helper_retention_days", 3651),
        ("helper_retention_days", -1),
    ],
)
async def test_validation_rejects_out_of_range(
    client: AsyncClient,
    db_session: AsyncSession,
    field: str,
    bad_value: int,
):
    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=_hdrs(key),
        json={field: bad_value},
    )
    assert resp.status_code == 400, f"{field}={bad_value} should be rejected"


@pytest.mark.asyncio
async def test_dlp_policy_block_not_clobbered_by_general_settings_put(
    client: AsyncClient, db_session: AsyncSession
):
    """Settings JSON stores DLP under "dlp" and general under "general".

    Updating one block must not erase the other. This proves the
    route does a structural merge, not a wholesale settings rewrite.
    """
    import json as _json

    admin, key = await _make_user(db_session, "admin")
    org = await _make_org_with(db_session, admin)
    # Seed a DLP block directly in the DB.
    org.settings = _json.dumps({
        "dlp": {"enabled": True, "mode": "warn", "categories": ["secrets"]},
    })
    await db_session.commit()

    # PUT general settings.
    resp = await client.put(
        f"/api/v1/orgs/{org.id}/settings",
        headers=_hdrs(key),
        json={"kb_retention_days": 90},
    )
    assert resp.status_code == 200

    # Re-read raw settings: dlp block must survive.
    await db_session.refresh(org)
    raw = _json.loads(org.settings)
    assert "dlp" in raw and raw["dlp"]["mode"] == "warn"
    assert "general" in raw and raw["general"]["kb_retention_days"] == 90
