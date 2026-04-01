"""Integration tests for license lifecycle management."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.keys import generate_api_key, hash_api_key
from sessionfs.server.db.models import ApiKey, HelmLicense, LicenseValidation, User
from sessionfs.server.license_keys import generate_license_key


@pytest.fixture
async def admin_user(db_session: AsyncSession) -> User:
    """Create an admin user."""
    user = User(
        id=str(uuid.uuid4()),
        email="licadmin@sessionfs.dev",
        display_name="License Admin",
        tier="admin",
        email_verified=True,
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.fixture
async def admin_api_key(db_session: AsyncSession, admin_user: User) -> tuple[str, ApiKey]:
    """Create an API key for the admin user."""
    raw_key = generate_api_key()
    api_key = ApiKey(
        id=str(uuid.uuid4()),
        user_id=admin_user.id,
        key_hash=hash_api_key(raw_key),
        name="admin-key",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(api_key)
    await db_session.commit()
    await db_session.refresh(api_key)
    return raw_key, api_key


@pytest.fixture
def admin_headers(admin_api_key: tuple[str, ApiKey]) -> dict[str, str]:
    """Authorization headers for the admin user."""
    return {"Authorization": f"Bearer {admin_api_key[0]}"}


# ---------------------------------------------------------------------------
# Trial license full lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_trial_lifecycle(
    client: AsyncClient, admin_headers: dict, db_session: AsyncSession
):
    """Trial: create -> validate OK -> expire -> degrade -> hard stop."""
    # Create trial (14 days)
    resp = await client.post(
        "/api/v1/admin/licenses/",
        json={
            "org_name": "Trial Corp",
            "contact_email": "trial@example.com",
            "license_type": "trial",
            "tier": "enterprise",
            "seats_limit": 10,
            "days": 14,
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201
    data = resp.json()
    key = data["key"]
    assert key.startswith("sfs_trial_")
    assert data["license_type"] == "trial"
    assert data["effective_status"] == "trial"

    # Validate — should be valid (expiring within 30 days = warning)
    resp = await client.post(
        "/api/v1/helm/validate",
        json={"license_key": key, "cluster_id": "test-cluster"},
    )
    assert resp.status_code == 200
    vdata = resp.json()
    assert vdata["valid"] is True
    assert vdata["status"] == "warning"
    assert "days_remaining" in vdata

    # Manually expire the license (set expires_at to yesterday)
    result = await db_session.execute(select(HelmLicense).where(HelmLicense.id == key))
    lic = result.scalar_one()
    lic.expires_at = datetime.now(timezone.utc) - timedelta(days=1)
    await db_session.commit()

    # Validate — should be degraded (within 14-day grace)
    resp = await client.post(
        "/api/v1/helm/validate",
        json={"license_key": key},
    )
    assert resp.status_code == 200
    vdata = resp.json()
    assert vdata["valid"] is True
    assert vdata["status"] == "degraded"
    assert vdata["tier"] == "free"

    # Expire beyond grace period (15 days ago)
    lic.expires_at = datetime.now(timezone.utc) - timedelta(days=15)
    await db_session.commit()

    # Validate — should get 403
    resp = await client.post(
        "/api/v1/helm/validate",
        json={"license_key": key},
    )
    assert resp.status_code == 403
    assert resp.json()["valid"] is False


# ---------------------------------------------------------------------------
# Paid license (no expiry)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paid_license_no_expiry(
    client: AsyncClient, admin_headers: dict
):
    """Paid license without expiry validates indefinitely."""
    resp = await client.post(
        "/api/v1/admin/licenses/",
        json={
            "org_name": "Paid Corp",
            "contact_email": "paid@example.com",
            "license_type": "paid",
            "tier": "enterprise",
            "seats_limit": 50,
        },
        headers=admin_headers,
    )
    assert resp.status_code == 201
    key = resp.json()["key"]
    assert key.startswith("sfs_helm_")

    # Validate — should be valid with no expiry
    resp = await client.post(
        "/api/v1/helm/validate",
        json={"license_key": key},
    )
    assert resp.status_code == 200
    vdata = resp.json()
    assert vdata["valid"] is True
    assert vdata["status"] == "valid"
    assert vdata["expires_at"] is None
    assert vdata["seats"] == 50
    assert "features" in vdata


# ---------------------------------------------------------------------------
# Revoke — immediate 403
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_immediate_403(
    client: AsyncClient, admin_headers: dict, db_session: AsyncSession
):
    """Revoking a license causes immediate 403."""
    # Create license
    resp = await client.post(
        "/api/v1/admin/licenses/",
        json={
            "org_name": "Revoke Corp",
            "contact_email": "revoke@example.com",
            "license_type": "paid",
        },
        headers=admin_headers,
    )
    key = resp.json()["key"]

    # Validate — OK
    resp = await client.post("/api/v1/helm/validate", json={"license_key": key})
    assert resp.status_code == 200

    # Revoke
    resp = await client.put(
        f"/api/v1/admin/licenses/{key}",
        json={"status": "revoked"},
        headers=admin_headers,
    )
    assert resp.status_code == 200

    # Validate — 403
    resp = await client.post("/api/v1/helm/validate", json={"license_key": key})
    assert resp.status_code == 403
    assert "revoked" in resp.json()["error"].lower()


# ---------------------------------------------------------------------------
# Extend trial — new expiry from NOW
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_extend_trial(
    client: AsyncClient, admin_headers: dict, db_session: AsyncSession
):
    """Extending a trial sets new expiry from now."""
    # Create trial (2 days — will be in warning state)
    resp = await client.post(
        "/api/v1/admin/licenses/",
        json={
            "org_name": "Extend Corp",
            "contact_email": "extend@example.com",
            "license_type": "trial",
            "days": 2,
        },
        headers=admin_headers,
    )
    key = resp.json()["key"]

    # Expire it
    result = await db_session.execute(select(HelmLicense).where(HelmLicense.id == key))
    lic = result.scalar_one()
    lic.expires_at = datetime.now(timezone.utc) - timedelta(days=5)
    await db_session.commit()

    # Validate — should be degraded
    resp = await client.post("/api/v1/helm/validate", json={"license_key": key})
    assert resp.status_code == 200
    assert resp.json()["status"] == "degraded"

    # Extend by 14 days
    resp = await client.put(
        f"/api/v1/admin/licenses/{key}/extend",
        json={"days": 14},
        headers=admin_headers,
    )
    assert resp.status_code == 200
    new_expires = resp.json()["expires_at"]
    assert new_expires is not None

    # Validate — should be warning (14 days is within 30-day window)
    resp = await client.post("/api/v1/helm/validate", json={"license_key": key})
    assert resp.status_code == 200
    assert resp.json()["status"] == "warning"
    assert resp.json()["valid"] is True


# ---------------------------------------------------------------------------
# Validation logging
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_validation_logging(
    client: AsyncClient, admin_headers: dict, db_session: AsyncSession
):
    """Each validation call is logged to license_validations table."""
    resp = await client.post(
        "/api/v1/admin/licenses/",
        json={
            "org_name": "Log Corp",
            "contact_email": "log@example.com",
            "license_type": "paid",
        },
        headers=admin_headers,
    )
    key = resp.json()["key"]

    # Validate 3 times
    for i in range(3):
        await client.post(
            "/api/v1/helm/validate",
            json={"license_key": key, "cluster_id": f"cluster-{i}", "version": "0.9.6"},
        )

    # Check validation count on license
    result = await db_session.execute(select(HelmLicense).where(HelmLicense.id == key))
    lic = result.scalar_one()
    assert lic.validation_count == 3
    assert lic.last_validated_at is not None

    # Check validation history via admin API
    resp = await client.get(
        f"/api/v1/admin/licenses/{key}/history",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    history = resp.json()
    assert history["total"] == 3
    assert history["validations"][0]["result"] == "valid"
    assert history["validations"][0]["version"] == "0.9.6"


# ---------------------------------------------------------------------------
# Warning state (expiring in 4 days)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_warning_state(
    client: AsyncClient, admin_headers: dict, db_session: AsyncSession
):
    """License expiring in 4 days returns warning status."""
    resp = await client.post(
        "/api/v1/admin/licenses/",
        json={
            "org_name": "Warn Corp",
            "contact_email": "warn@example.com",
            "license_type": "paid",
            "days": 90,
        },
        headers=admin_headers,
    )
    key = resp.json()["key"]

    # Set expiry to ~4.5 days from now (buffer avoids int truncation edge)
    result = await db_session.execute(select(HelmLicense).where(HelmLicense.id == key))
    lic = result.scalar_one()
    lic.expires_at = datetime.now(timezone.utc) + timedelta(days=4, hours=12)
    await db_session.commit()

    resp = await client.post("/api/v1/helm/validate", json={"license_key": key})
    assert resp.status_code == 200
    vdata = resp.json()
    assert vdata["status"] == "warning"
    assert vdata["days_remaining"] == 4
    assert "warning" in vdata
    assert vdata["valid"] is True


# ---------------------------------------------------------------------------
# List licenses with status filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_licenses_filter(
    client: AsyncClient, admin_headers: dict, db_session: AsyncSession
):
    """List licenses with status filter returns correct subsets."""
    # Create a trial and a paid license
    await client.post(
        "/api/v1/admin/licenses/",
        json={
            "org_name": "Filter Trial",
            "contact_email": "ft@example.com",
            "license_type": "trial",
            "days": 14,
        },
        headers=admin_headers,
    )
    await client.post(
        "/api/v1/admin/licenses/",
        json={
            "org_name": "Filter Paid",
            "contact_email": "fp@example.com",
            "license_type": "paid",
        },
        headers=admin_headers,
    )

    # List all
    resp = await client.get(
        "/api/v1/admin/licenses/?status=all",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    all_data = resp.json()
    assert all_data["total"] >= 2

    # List trial only
    resp = await client.get(
        "/api/v1/admin/licenses/?status=trial",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    trial_data = resp.json()
    for lic in trial_data["licenses"]:
        assert lic["effective_status"] == "trial"

    # List active only (paid with no expiry)
    resp = await client.get(
        "/api/v1/admin/licenses/?status=active",
        headers=admin_headers,
    )
    assert resp.status_code == 200
    active_data = resp.json()
    for lic in active_data["licenses"]:
        assert lic["effective_status"] == "active"


# ---------------------------------------------------------------------------
# License key generation
# ---------------------------------------------------------------------------


def test_license_key_generation():
    """License keys have correct prefixes and sufficient entropy."""
    trial_key = generate_license_key("trial")
    assert trial_key.startswith("sfs_trial_")
    assert len(trial_key) == len("sfs_trial_") + 24

    paid_key = generate_license_key("paid")
    assert paid_key.startswith("sfs_helm_")
    assert len(paid_key) == len("sfs_helm_") + 24

    internal_key = generate_license_key("internal")
    assert internal_key.startswith("sfs_int_")
    assert len(internal_key) == len("sfs_int_") + 24

    # Unknown type defaults to paid prefix
    unknown_key = generate_license_key("unknown")
    assert unknown_key.startswith("sfs_helm_")

    # Keys are unique
    keys = {generate_license_key() for _ in range(100)}
    assert len(keys) == 100
