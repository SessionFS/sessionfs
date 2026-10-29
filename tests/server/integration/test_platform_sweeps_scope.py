"""``platform_sweeps:run``: scheduled jobs run the platform sweep endpoints
with a narrow service key instead of a full platform-admin user key.

The key works only on the six sweep endpoints, only while the platform admin
who minted it (and the user it runs as) is still an active platform admin,
and only a platform admin can mint or rotate it."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import AdminAction, ApiKey, User
from tests.server.integration.test_skill_marketplace import (
    _org_with_project,
    _platform_admin_headers,
)

pytestmark = pytest.mark.asyncio

SCOPE = "platform_sweeps:run"

# (path, body, AdminAction.action written by the sweep or None)
SWEEPS: list[tuple[str, dict, str | None]] = [
    ("/api/v1/admin/skills/catalogs/regenerate", {}, None),
    ("/api/v1/admin/skills/verify", {"tools": ["claude-code"]}, "skill_verify"),
    ("/api/v1/admin/marketplace/freshness/sweep", {}, "skill_freshness_sweep"),
    ("/api/v1/admin/skills/rescan", {}, "skill_rescan"),
    ("/api/v1/admin/marketplace/publishers/recheck", {}, "skill_publisher_recheck"),
    ("/api/v1/admin/github-webhook-deliveries/sweep", {}, "purge_github_webhook_deliveries"),
    ("/api/v1/admin/github-webhook-deliveries/purge", {}, "purge_github_webhook_deliveries"),
    ("/api/v1/admin/sweeps/org-digest", {}, "run_org_digest"),
    ("/api/v1/admin/sweeps/kb-compile", {}, "kb_compile_sweep"),
    ("/api/v1/admin/sweeps/helper-retention", {}, "helper_retention_sweep"),
    ("/api/v1/admin/sweeps/ticket-notifications", {}, "run_ticket_notifications_sweep"),
    ("/api/v1/admin/sweeps/confluence-publish", {}, "confluence_publish_sweep"),
    ("/api/v1/admin/sweeps/jira-sync", {}, "jira_sync_sweep"),
    ("/api/v1/admin/sweeps/meeting-retention", {}, "meeting_retention_sweep"),
]
SWEEP_PATHS = {path for path, _, _ in SWEEPS}


async def _service_key(
    db: AsyncSession,
    *,
    owner: User,
    org_id: str,
    scopes: list[str],
    minter: User | None = None,
    name: str = "sweeper",
    **extra,
) -> tuple[ApiKey, dict]:
    raw = generate_api_key()
    key = ApiKey(
        id=str(uuid.uuid4()),
        user_id=owner.id,
        key_hash=hash_api_key(raw),
        name=name,
        created_at=datetime.now(timezone.utc),
        key_kind="service",
        org_id=org_id,
        scopes=json.dumps(scopes),
        service_key_name=name,
        created_by_user_id=(minter or owner).id,
        **extra,
    )
    db.add(key)
    await db.commit()
    return key, {"Authorization": f"Bearer {raw}"}


async def _admin_org(db: AsyncSession, admin: User) -> str:
    org, _ = await _org_with_project(db, admin)
    return org.id


async def _sweep_key(db: AsyncSession) -> tuple[User, str, ApiKey, dict]:
    admin, _ = await _platform_admin_headers(db)
    org_id = await _admin_org(db, admin)
    key, headers = await _service_key(db, owner=admin, org_id=org_id, scopes=[SCOPE])
    return admin, org_id, key, headers


async def _run_all(client: AsyncClient, headers: dict) -> dict[str, int]:
    out = {}
    for path, body, _ in SWEEPS:
        response = await client.post(path, headers=headers, json=body)
        out[path] = response.status_code
    return out


async def test_scoped_key_from_current_admin_runs_every_sweep_and_is_audited(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, _, key, headers = await _sweep_key(db_session)
    admin_id, key_id = admin.id, key.id
    for path, body, _ in SWEEPS:
        response = await client.post(path, headers=headers, json=body)
        assert response.status_code == 200, (path, response.text)
    rows = (
        await db_session.execute(select(AdminAction).where(AdminAction.admin_id == admin_id))
    ).scalars().all()
    by_action = {row.action: json.loads(row.details or "{}") for row in rows}
    for _, _, action in SWEEPS:
        if action is None:
            continue
        details = by_action[action]
        assert details["actor_type"] == "service_key", action
        assert details["service_key_id"] == key_id, action
        assert details["service_key_name"] == "sweeper", action


async def test_platform_admin_user_key_still_runs_every_sweep(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, admin_headers = await _platform_admin_headers(db_session)
    admin_id = admin.id
    assert set((await _run_all(client, admin_headers)).values()) == {200}
    rows = (
        await db_session.execute(select(AdminAction).where(AdminAction.admin_id == admin_id))
    ).scalars().all()
    assert rows and all(json.loads(row.details or "{}")["actor_type"] == "user" for row in rows)


async def test_ordinary_user_key_is_refused(client: AsyncClient, auth_headers: dict) -> None:
    assert set((await _run_all(client, auth_headers)).values()) == {403}


@pytest.mark.parametrize("change", ["demoted", "deactivated"])
async def test_key_stops_when_minter_loses_platform_admin(
    client: AsyncClient, db_session: AsyncSession, change: str
) -> None:
    admin, _, _, headers = await _sweep_key(db_session)
    if change == "demoted":
        admin.tier = "pro"
    else:
        admin.is_active = False
    await db_session.commit()
    assert set((await _run_all(client, headers)).values()) == {403}


async def test_key_stops_when_rotating_admin_is_demoted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """After a rotation the key runs as the original owner but was minted by
    the rotator: both must still be platform admins."""
    owner, _ = await _platform_admin_headers(db_session)
    rotator, _ = await _platform_admin_headers(db_session)
    org_id = await _admin_org(db_session, owner)
    _, headers = await _service_key(db_session, owner=owner, org_id=org_id, scopes=[SCOPE], minter=rotator)
    response = await client.post("/api/v1/admin/marketplace/freshness/sweep", headers=headers, json={})
    assert response.status_code == 200, response.text
    rotator.tier = "team"
    await db_session.commit()
    assert set((await _run_all(client, headers)).values()) == {403}
    refused = await client.post("/api/v1/admin/marketplace/freshness/sweep", headers=headers, json={})
    assert refused.json()["error"]["details"]["error"] == "platform_sweeper_denied", refused.text


async def test_key_without_recorded_minter_is_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, _, key, headers = await _sweep_key(db_session)
    key.created_by_user_id = None
    await db_session.commit()
    assert set((await _run_all(client, headers)).values()) == {403}


async def test_revoked_and_expired_keys_are_refused(client: AsyncClient, db_session: AsyncSession) -> None:
    admin, _ = await _platform_admin_headers(db_session)
    org_id = await _admin_org(db_session, admin)
    now = datetime.now(timezone.utc)
    _, revoked = await _service_key(
        db_session, owner=admin, org_id=org_id, scopes=[SCOPE], name="revoked",
        is_active=False, revoked_at=now, revoke_reason="test",
    )
    _, expired = await _service_key(
        db_session, owner=admin, org_id=org_id, scopes=[SCOPE], name="expired",
        expires_at=now - timedelta(minutes=1),
    )
    assert set((await _run_all(client, revoked)).values()) == {401}
    assert set((await _run_all(client, expired)).values()) == {401}


async def test_wrong_scope_and_org_admin_minted_keys_are_refused(
    client: AsyncClient, db_session: AsyncSession, test_user: User
) -> None:
    admin, _ = await _platform_admin_headers(db_session)
    org_id = await _admin_org(db_session, admin)
    _, wrong_scope = await _service_key(
        db_session, owner=admin, org_id=org_id, scopes=["skills_registry:poll", "marketplace:write"],
    )
    for path, body, _ in SWEEPS:
        response = await client.post(path, headers=wrong_scope, json=body)
        assert response.status_code == 403, (path, response.text)
        assert response.json()["error"]["details"]["error"] == "insufficient_scope", response.text
    org, _ = await _org_with_project(db_session, test_user)
    _, org_admin_key = await _service_key(db_session, owner=test_user, org_id=org.id, scopes=[SCOPE])
    assert set((await _run_all(client, org_admin_key)).values()) == {403}


async def test_service_key_cannot_override_webhook_retention(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, _, headers = await _sweep_key(db_session)
    for body in ({"retention_days": 0}, {"tombstone_retention_days": 500}):
        response = await client.post("/api/v1/admin/github-webhook-deliveries/sweep", headers=headers, json=body)
        assert response.status_code == 403, response.text
        assert "sweep_override_not_allowed" in response.text
    ok = await client.post("/api/v1/admin/github-webhook-deliveries/sweep", headers=headers,
                           json={"retry_limit": 5})
    assert ok.status_code == 200, ok.text


@pytest.mark.parametrize(
    "body", [{"report_only": True}, {"force": True}, {"report_only": True, "force": True}]
)
async def test_service_key_cannot_downgrade_or_force_the_rescan(
    client: AsyncClient, db_session: AsyncSession, body: dict
) -> None:
    """A forced report-only pass would mark every version scanned without
    quarantining, hiding criticals from the enforcing sweep for a week."""
    _, _, _, headers = await _sweep_key(db_session)
    response = await client.post("/api/v1/admin/skills/rescan", headers=headers, json=body)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["details"]["error"] == "sweep_override_not_allowed"


async def test_service_key_rescan_accepts_explicit_defaults_and_admin_keeps_overrides(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, _, headers = await _sweep_key(db_session)
    ok = await client.post("/api/v1/admin/skills/rescan", headers=headers,
                           json={"report_only": False, "force": False, "limit": 10})
    assert ok.status_code == 200, ok.text
    _, admin_headers = await _platform_admin_headers(db_session)
    admin = await client.post("/api/v1/admin/skills/rescan", headers=admin_headers,
                              json={"report_only": True, "force": True})
    assert admin.status_code == 200, admin.text


# ---------------------------------------------------------------------------
# Minting
# ---------------------------------------------------------------------------


async def test_only_a_platform_admin_can_mint_or_rotate_the_scope(
    client: AsyncClient, db_session: AsyncSession, auth_headers: dict, test_user: User
) -> None:
    # An ordinary org admin (owner of a Team org) cannot mint it.
    org, _ = await _org_with_project(db_session, test_user)
    org_id = org.id
    refused = await client.post(
        f"/api/v1/orgs/{org_id}/service-keys", headers=auth_headers,
        json={"name": "sweeper", "scopes": [SCOPE]},
    )
    assert refused.status_code == 403, refused.text
    assert "platform_admin_scope" in refused.text
    # ... nor rotate one that a platform admin put in their org.
    admin, admin_headers = await _platform_admin_headers(db_session)
    planted, _ = await _service_key(db_session, owner=admin, org_id=org_id, scopes=[SCOPE])
    rotated = await client.post(
        f"/api/v1/orgs/{org_id}/service-keys/{planted.id}/rotate", headers=auth_headers,
    )
    assert rotated.status_code == 403, rotated.text
    assert "platform_admin_scope" in rotated.text

    # A platform admin who administers an org mints and rotates it.
    admin_org = await _admin_org(db_session, admin)
    minted = await client.post(
        f"/api/v1/orgs/{admin_org}/service-keys", headers=admin_headers,
        json={"name": "sweeper", "scopes": [SCOPE]},
    )
    assert minted.status_code == 201, minted.text
    raw = minted.json()["key"]
    sweep = await client.post(
        "/api/v1/admin/marketplace/freshness/sweep",
        headers={"Authorization": f"Bearer {raw}"}, json={},
    )
    assert sweep.status_code == 200, sweep.text
    again = await client.post(
        f"/api/v1/orgs/{admin_org}/service-keys/{minted.json()['id']}/rotate", headers=admin_headers,
    )
    assert again.status_code == 200, again.text
    new_headers = {"Authorization": f"Bearer {again.json()['key']}"}
    assert (await client.post("/api/v1/admin/marketplace/freshness/sweep", headers=new_headers,
                              json={})).status_code == 200
    old = await client.post("/api/v1/admin/marketplace/freshness/sweep",
                            headers={"Authorization": f"Bearer {raw}"}, json={})
    assert old.status_code == 401


async def test_scope_is_in_the_vocabulary_and_platform_mint_only() -> None:
    from sessionfs.server.routes.api_keys import PLATFORM_ADMIN_SCOPES
    from sessionfs.server.schemas.api_keys import VALID_SCOPES

    assert SCOPE in VALID_SCOPES
    assert SCOPE in PLATFORM_ADMIN_SCOPES


# ---------------------------------------------------------------------------
# Grants nothing else
# ---------------------------------------------------------------------------


def _api_routes(routes, prefix: str = ""):
    """(path, route) for every APIRoute. Newer FastAPI keeps included routers
    as wrappers in ``app.routes`` (their prefix on ``include_context``), older
    releases flatten them, so walk both shapes."""
    from fastapi.routing import APIRoute

    for route in routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
        elif getattr(route, "original_router", None) is not None:
            context = getattr(route, "include_context", None)
            yield from _api_routes(
                route.original_router.routes, prefix + (getattr(context, "prefix", "") or "")
            )
        elif hasattr(route, "routes"):
            yield from _api_routes(route.routes, prefix + (getattr(route, "path", "") or ""))


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}:]+(?::path)?\}", "x0", path)


async def test_sweep_key_grants_nothing_beyond_the_sweeps(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Every other API route answers the narrow key exactly as it answers an
    unauthenticated request (or refuses it): the scope unlocks nothing else."""
    _, _, _, headers = await _sweep_key(db_session)
    app = client._transport.app  # type: ignore[attr-defined]
    checked = 0
    for path, route in _api_routes(app.routes):
        if not path.startswith("/api/") or path in SWEEP_PATHS:
            continue
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            url = _concrete(path)
            kwargs = {} if method in ("GET", "DELETE") else {"json": {}}
            keyed = await client.request(method, url, headers=headers, **kwargs)
            if keyed.status_code < 300:
                anonymous = await client.request(method, url, **kwargs)
                assert anonymous.status_code < 300, (
                    f"{method} {path} answers the sweep key {keyed.status_code} "
                    f"but anonymous {anonymous.status_code}"
                )
            checked += 1
    assert checked > 200


# ---------------------------------------------------------------------------
# The other platform-only scopes follow the same minter rule
# ---------------------------------------------------------------------------

PLATFORM_SCOPE_PROBES = [
    ("skills_verify:run", "/api/v1/admin/skills/verify/jobs/claim", {"tool": "claude-code"}),
    ("skills_registry:poll", "/api/v1/admin/skills/registries/poll", {}),
]


@pytest.mark.parametrize(("scope", "path", "body"), PLATFORM_SCOPE_PROBES)
@pytest.mark.parametrize("who", ["owner", "minter"])
@pytest.mark.parametrize("change", ["demoted", "deactivated"])
async def test_platform_scoped_keys_stop_when_owner_or_minter_loses_admin(
    client: AsyncClient, db_session: AsyncSession, scope: str, path: str, body: dict, who: str, change: str
) -> None:
    owner, _ = await _platform_admin_headers(db_session)
    minter, _ = await _platform_admin_headers(db_session)
    org_id = await _admin_org(db_session, owner)
    _, headers = await _service_key(db_session, owner=owner, org_id=org_id, scopes=[scope], minter=minter)
    ok = await client.post(path, headers=headers, json=body)
    assert ok.status_code == 200, ok.text
    target = owner if who == "owner" else minter
    if change == "demoted":
        target.tier = "team"
    else:
        target.is_active = False
    await db_session.commit()
    refused = await client.post(path, headers=headers, json=body)
    assert refused.status_code == 403, refused.text


@pytest.mark.parametrize(("scope", "path", "body"), PLATFORM_SCOPE_PROBES)
async def test_platform_scoped_key_without_recorded_minter_is_refused(
    client: AsyncClient, db_session: AsyncSession, scope: str, path: str, body: dict
) -> None:
    admin, _ = await _platform_admin_headers(db_session)
    org_id = await _admin_org(db_session, admin)
    key, headers = await _service_key(db_session, owner=admin, org_id=org_id, scopes=[scope])
    key.created_by_user_id = None
    await db_session.commit()
    refused = await client.post(path, headers=headers, json=body)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["details"]["error"] == "platform_key_minter_not_admin", refused.text


# ---------------------------------------------------------------------------
# Mint-time coupling visibility (GitHub #115)
# ---------------------------------------------------------------------------


async def test_mint_with_coupled_scope_warns_and_without_does_not(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, admin_headers = await _platform_admin_headers(db_session)
    org_id = await _admin_org(db_session, admin)

    coupled = await client.post(
        f"/api/v1/orgs/{org_id}/service-keys", headers=admin_headers,
        json={"name": "sweeper", "scopes": [SCOPE]},
    )
    assert coupled.status_code == 201, coupled.text
    warnings = coupled.json()["warnings"]
    assert len(warnings) == 1, coupled.text
    assert warnings[0]["code"] == "minter_tier_coupling"
    assert "platform_sweeps:run" in warnings[0]["message"]
    assert "platform admin" in warnings[0]["message"]
    # A freshly minted key still couples cleanly right now.
    assert coupled.json()["coupling_ok"] is True
    assert coupled.json()["coupling_reason"] is None

    plain = await client.post(
        f"/api/v1/orgs/{org_id}/service-keys", headers=admin_headers,
        json={"name": "plain", "scopes": ["tickets:read"]},
    )
    assert plain.status_code == 201, plain.text
    assert plain.json()["warnings"] == []
    assert plain.json()["coupling_ok"] is None
    assert plain.json()["coupling_reason"] is None


async def test_rotate_with_coupled_scope_warns(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, admin_headers = await _platform_admin_headers(db_session)
    org_id = await _admin_org(db_session, admin)
    minted = await client.post(
        f"/api/v1/orgs/{org_id}/service-keys", headers=admin_headers,
        json={"name": "sweeper", "scopes": [SCOPE]},
    )
    assert minted.status_code == 201, minted.text
    rotated = await client.post(
        f"/api/v1/orgs/{org_id}/service-keys/{minted.json()['id']}/rotate",
        headers=admin_headers,
    )
    assert rotated.status_code == 200, rotated.text
    warnings = rotated.json()["warnings"]
    assert len(warnings) == 1, rotated.text
    assert warnings[0]["code"] == "minter_tier_coupling"


@pytest.mark.parametrize("change", ["demoted", "deactivated"])
async def test_denied_details_report_minter_state(
    client: AsyncClient, db_session: AsyncSession, change: str
) -> None:
    # Use a distinct minter (the rotator) so the key's owner stays an active
    # platform admin and the request reaches the minter-coupling check. When
    # owner == minter and the account is deactivated, auth rejects earlier
    # with the generic "User account is inactive" (no minter details).
    owner, _ = await _platform_admin_headers(db_session)
    minter, _ = await _platform_admin_headers(db_session)
    org_id = await _admin_org(db_session, owner)
    _, headers = await _service_key(
        db_session, owner=owner, org_id=org_id, scopes=[SCOPE], minter=minter
    )
    if change == "demoted":
        minter.tier = "pro"
        expected_tier = "pro"
    else:
        minter.is_active = False
        expected_tier = "deactivated"
    await db_session.commit()

    refused = await client.post(
        "/api/v1/admin/marketplace/freshness/sweep", headers=headers, json={}
    )
    assert refused.status_code == 403, refused.text
    details = refused.json()["error"]["details"]
    assert details["error"] == "platform_sweeper_denied"
    assert details["minter_user_id"] == minter.id
    assert details["minter_current_tier"] == expected_tier
    assert details["required_minter_tier"] == "admin"
    assert details["resolution"]
    # The minter's email must never leak to the key holder.
    assert minter.email not in refused.text


async def test_list_reports_coupling_ok_false_after_demotion(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, admin_headers = await _platform_admin_headers(db_session)
    org_id = await _admin_org(db_session, admin)
    minted = await client.post(
        f"/api/v1/orgs/{org_id}/service-keys", headers=admin_headers,
        json={"name": "sweeper", "scopes": [SCOPE]},
    )
    assert minted.status_code == 201, minted.text
    key_id = minted.json()["id"]

    def _row(listing_body: list) -> dict:
        return [r for r in listing_body if r["id"] == key_id][0]

    before = await client.get(f"/api/v1/orgs/{org_id}/service-keys", headers=admin_headers)
    assert before.status_code == 200, before.text
    assert _row(before.json())["coupling_ok"] is True
    assert _row(before.json())["coupling_reason"] is None

    admin.tier = "pro"
    await db_session.commit()

    after = await client.get(f"/api/v1/orgs/{org_id}/service-keys", headers=admin_headers)
    assert after.status_code == 200, after.text
    assert _row(after.json())["coupling_ok"] is False
    assert _row(after.json())["coupling_reason"] == "minter no longer admin"
