"""``admin_metrics:read`` + the operator metrics endpoint (GitHub #90).

The endpoint renders the in-process Prometheus registry. It admits a
platform-admin user key, or a service key carrying ``admin_metrics:read`` whose
owner and minter are both currently active platform admins. The request-duration
metric records the matched route TEMPLATE, never a raw path or identifier."""

from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import ApiKey, User
from sessionfs.server.metrics import CONTENT_TYPE, reset_registry
from sessionfs.server.routes.metrics import reset_db_gauge_cache
from tests.server.integration.test_skill_marketplace import (
    _org_with_project,
    _platform_admin_headers,
)

pytestmark = pytest.mark.asyncio

SCOPE = "admin_metrics:read"
METRICS_PATH = "/api/v1/admin/metrics"


@pytest.fixture(autouse=True)
def _fresh_metrics():
    reset_registry()
    reset_db_gauge_cache()
    yield
    reset_registry()
    reset_db_gauge_cache()


async def _service_key(
    db: AsyncSession,
    *,
    owner: User,
    org_id: str,
    scopes: list[str],
    minter: User | None = None,
    name: str = "metrics-scraper",
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


async def _metrics_key(db: AsyncSession) -> tuple[User, str, ApiKey, dict]:
    admin, _ = await _platform_admin_headers(db)
    org, _ = await _org_with_project(db, admin)
    key, headers = await _service_key(db, owner=admin, org_id=org.id, scopes=[SCOPE])
    return admin, org.id, key, headers


async def test_anonymous_is_refused(client: AsyncClient) -> None:
    response = await client.get(METRICS_PATH)
    assert response.status_code == 401


async def test_ordinary_user_key_is_refused(
    client: AsyncClient, auth_headers: dict
) -> None:
    response = await client.get(METRICS_PATH, headers=auth_headers)
    assert response.status_code == 403


async def test_platform_admin_user_key_is_admitted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, admin_headers = await _platform_admin_headers(db_session)
    response = await client.get(METRICS_PATH, headers=admin_headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(CONTENT_TYPE)
    assert "sessionfs_push_total" in response.text
    assert "http_request_duration_seconds" in response.text


async def test_scoped_service_key_is_admitted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, _, headers = await _metrics_key(db_session)
    response = await client.get(METRICS_PATH, headers=headers)
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith(CONTENT_TYPE)


async def test_service_key_without_the_scope_is_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, _ = await _platform_admin_headers(db_session)
    org, _ = await _org_with_project(db_session, admin)
    _, headers = await _service_key(db_session, owner=admin, org_id=org.id, scopes=["sessions:read"])
    response = await client.get(METRICS_PATH, headers=headers)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["details"]["error"] == "insufficient_scope"


@pytest.mark.parametrize("change", ["demoted", "deactivated"])
async def test_key_stops_when_owner_loses_platform_admin(
    client: AsyncClient, db_session: AsyncSession, change: str
) -> None:
    # Demotion trips the minter check (still-active but not admin); a
    # deactivated owner is refused even earlier by the auth layer. Both stop
    # the key, so only the status code is asserted here.
    admin, _, _, headers = await _metrics_key(db_session)
    if change == "demoted":
        admin.tier = "pro"
    else:
        admin.is_active = False
    await db_session.commit()
    response = await client.get(METRICS_PATH, headers=headers)
    assert response.status_code == 403, response.text


async def test_key_stops_when_minter_loses_platform_admin(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    owner, _ = await _platform_admin_headers(db_session)
    minter, _ = await _platform_admin_headers(db_session)
    org, _ = await _org_with_project(db_session, owner)
    _, headers = await _service_key(db_session, owner=owner, org_id=org.id, scopes=[SCOPE], minter=minter)
    assert (await client.get(METRICS_PATH, headers=headers)).status_code == 200
    minter.tier = "team"
    await db_session.commit()
    response = await client.get(METRICS_PATH, headers=headers)
    assert response.status_code == 403, response.text
    assert response.json()["error"]["details"]["error"] == "admin_metrics_denied"


async def test_key_without_recorded_minter_is_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _, _, key, headers = await _metrics_key(db_session)
    key.created_by_user_id = None
    await db_session.commit()
    response = await client.get(METRICS_PATH, headers=headers)
    assert response.status_code == 403, response.text


async def test_revoked_key_is_refused(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    admin, _ = await _platform_admin_headers(db_session)
    org, _ = await _org_with_project(db_session, admin)
    _, headers = await _service_key(
        db_session, owner=admin, org_id=org.id, scopes=[SCOPE],
        is_active=False, revoked_at=datetime.now(timezone.utc), revoke_reason="test",
    )
    response = await client.get(METRICS_PATH, headers=headers)
    assert response.status_code == 401


async def test_duration_records_unmatched_not_raw_path_on_404(
    client: AsyncClient, db_session: AsyncSession, auth_headers: dict
) -> None:
    # A request to a path-parameterized route that 404s must collapse to
    # "unmatched": the request never reached a handler, and recording the
    # template would let an anonymous caller mint a series per route.
    raw_id = "raw-identifier-123"
    await client.get(f"/api/v1/sessions/{raw_id}", headers=auth_headers)

    _, admin_headers = await _platform_admin_headers(db_session)
    response = await client.get(METRICS_PATH, headers=admin_headers)
    body = response.text
    assert 'route="unmatched"' in body
    assert raw_id not in body
    assert 'route="/api/v1/sessions/{session_id}"' not in body


async def test_duration_records_route_template_for_success(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # A successful request records the matched template (never a raw path).
    _, admin_headers = await _platform_admin_headers(db_session)
    await client.get(METRICS_PATH, headers=admin_headers)
    response = await client.get(METRICS_PATH, headers=admin_headers)
    assert 'route="/api/v1/admin/metrics"' in response.text
    assert 'status_class="2xx"' in response.text


async def test_duration_clamps_unknown_method_and_405_route(
    client: AsyncClient, db_session: AsyncSession, auth_headers: dict
) -> None:
    # PROPFIND is not a known method. The router still matches a template but
    # answers 405, so both the method and the route must collapse rather than
    # mint a (method, template) series.
    await client.request(
        "PROPFIND", "/api/v1/sessions/ses_propfindabcd1234", headers=auth_headers
    )

    _, admin_headers = await _platform_admin_headers(db_session)
    response = await client.get(METRICS_PATH, headers=admin_headers)
    body = response.text
    assert 'method="other"' in body
    assert 'route="unmatched"' in body
    assert 'method="PROPFIND"' not in body


async def test_duration_records_unhandled_exception_as_5xx(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # An unhandled exception propagates out of call_next; the middleware must
    # record the crash as 5xx (with the template) before re-raising it up to
    # the server-error middleware. httpx's ASGITransport then re-raises that
    # exception (raise_app_exceptions=True) rather than returning the 500, so
    # we assert the crash propagates and then read the recorded metric.
    app = client._transport.app  # type: ignore[attr-defined]

    @app.get("/api/v1/metrics-crash-test")
    async def crash():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        await client.get("/api/v1/metrics-crash-test")

    _, admin_headers = await _platform_admin_headers(db_session)
    metrics_response = await client.get(METRICS_PATH, headers=admin_headers)
    body = metrics_response.text
    assert 'status_class="5xx"' in body
    assert 'route="/api/v1/metrics-crash-test"' in body


async def test_413_content_length_rejection_uses_declared_size_bucket(
    client: AsyncClient,
    db_session: AsyncSession,
    auth_headers: dict,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A 413 rejected on the Content-Length header alone never reads the body,
    # so bytes_in is unset. The push must still be bucketed from the declared
    # header (200 MB -> >100MB), not fall into <1MB.
    from sessionfs.server.routes import sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "_sync_limit_for_tier", lambda tier: 10)
    response = await client.put(
        "/api/v1/sessions/ses_big1234abcdefgh/sync",
        headers={**auth_headers, "Content-Length": "200000000"},
        files={"file": ("session.tar.gz", io.BytesIO(b"x" * 64), "application/gzip")},
    )
    assert response.status_code == 413

    _, admin_headers = await _platform_admin_headers(db_session)
    metrics_response = await client.get(METRICS_PATH, headers=admin_headers)
    body = metrics_response.text
    assert 'outcome="rejected_quota"' in body
    assert 'size_bucket=">100MB"' in body
    assert 'size_bucket="<1MB"' not in body


async def test_duration_records_unmatched_for_unknown_api_path(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await client.get("/api/v1/does-not-exist-anywhere")

    _, admin_headers = await _platform_admin_headers(db_session)
    response = await client.get(METRICS_PATH, headers=admin_headers)
    assert 'route="unmatched"' in response.text


async def test_scope_is_in_the_vocabulary_and_platform_mint_only() -> None:
    from sessionfs.server.routes.api_keys import PLATFORM_ADMIN_SCOPES
    from sessionfs.server.schemas.api_keys import VALID_SCOPES

    assert SCOPE in VALID_SCOPES
    assert SCOPE in PLATFORM_ADMIN_SCOPES
