"""Helm license validation endpoint for self-hosted deployments."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import HelmLicense, LicenseValidation
from sessionfs.server.tiers import get_features_for_tier

router = APIRouter(prefix="/api/v1/helm", tags=["helm"])

GRACE_PERIOD_DAYS = 14


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (UTC). SQLite may strip tzinfo."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


class HelmValidateRequest(BaseModel):
    license_key: str
    cluster_id: str | None = None
    version: str | None = None

    @field_validator("license_key")
    @classmethod
    def validate_license_key(cls, v: str) -> str:
        if not v or len(v) > 100:
            raise ValueError("Invalid license key format")
        # Only allow alphanumeric, underscores, hyphens
        import re
        if not re.match(r"^[a-zA-Z0-9_-]+$", v):
            raise ValueError("License key contains invalid characters")
        return v


async def _log_validation(
    db: AsyncSession,
    license_id: str,
    cluster_id: str | None,
    ip_address: str | None,
    result: str,
    version: str | None,
) -> None:
    """Record a validation attempt."""
    entry = LicenseValidation(
        license_id=license_id,
        cluster_id=cluster_id,
        ip_address=ip_address,
        result=result,
        version=version,
    )
    db.add(entry)


@router.post("/validate")
async def validate_helm_license(
    data: HelmValidateRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Validate a Helm license key. Called by init container on pod start."""
    result = await db.execute(
        select(HelmLicense).where(HelmLicense.id == data.license_key)
    )
    lic = result.scalar_one_or_none()

    client_ip = request.client.host if request.client else None

    # License not found
    if not lic:
        return JSONResponse(
            status_code=403,
            content={"valid": False, "error": "Invalid license key"},
        )

    # Revoked license — immediate 403
    if lic.status == "revoked":
        await _log_validation(db, lic.id, data.cluster_id, client_ip, "rejected_revoked", data.version)
        lic.last_validated_at = datetime.now(timezone.utc)
        lic.validation_count = (lic.validation_count or 0) + 1
        await db.commit()
        return JSONResponse(
            status_code=403,
            content={"valid": False, "error": "License has been revoked"},
        )

    # Inactive license (not active)
    if lic.status not in ("active",):
        await _log_validation(db, lic.id, data.cluster_id, client_ip, "rejected_inactive", data.version)
        lic.last_validated_at = datetime.now(timezone.utc)
        lic.validation_count = (lic.validation_count or 0) + 1
        await db.commit()
        return JSONResponse(
            status_code=403,
            content={"valid": False, "error": "Invalid license key"},
        )

    now = datetime.now(timezone.utc)
    features = sorted(get_features_for_tier(lic.tier))

    # Determine expiry state
    if lic.expires_at is None:
        # No expiry — perpetual license
        validation_result = "valid"
        response = {
            "valid": True,
            "status": "valid",
            "tier": lic.tier,
            "seats": lic.seats_limit,
            "expires_at": None,
            "features": features,
        }
    else:
        days_until_expiry = (_ensure_utc(lic.expires_at) - now).total_seconds() / 86400

        if days_until_expiry > 30:
            # Active, not expiring soon
            validation_result = "valid"
            response = {
                "valid": True,
                "status": "valid",
                "tier": lic.tier,
                "seats": lic.seats_limit,
                "expires_at": lic.expires_at.isoformat(),
                "features": features,
            }
        elif days_until_expiry > 0:
            # Expiring in 1-30 days — warning
            validation_result = "warning"
            days_remaining = int(days_until_expiry)
            response = {
                "valid": True,
                "status": "warning",
                "tier": lic.tier,
                "seats": lic.seats_limit,
                "expires_at": lic.expires_at.isoformat(),
                "features": features,
                "days_remaining": days_remaining,
                "warning": f"License expires in {days_remaining} day{'s' if days_remaining != 1 else ''}",
            }
        else:
            # Expired
            days_since_expiry = abs(days_until_expiry)
            if days_since_expiry <= GRACE_PERIOD_DAYS:
                # Grace period — degraded to free tier
                validation_result = "degraded"
                days_since = int(days_since_expiry)
                grace_remaining = GRACE_PERIOD_DAYS - days_since
                response = {
                    "valid": True,
                    "status": "degraded",
                    "tier": "free",
                    "seats": lic.seats_limit,
                    "expires_at": lic.expires_at.isoformat(),
                    "features": sorted(get_features_for_tier("free")),
                    "days_remaining": grace_remaining,
                    "warning": f"License expired {days_since} day{'s' if days_since != 1 else ''} ago. "
                               f"Grace period ends in {grace_remaining} day{'s' if grace_remaining != 1 else ''}.",
                }
            else:
                # Past grace period — hard stop
                await _log_validation(db, lic.id, data.cluster_id, client_ip, "rejected_expired", data.version)
                lic.last_validated_at = now
                lic.validation_count = (lic.validation_count or 0) + 1
                if data.cluster_id:
                    lic.cluster_id = data.cluster_id
                await db.commit()
                return JSONResponse(
                    status_code=403,
                    content={"valid": False, "error": "License expired beyond grace period"},
                )

    # Update license tracking
    lic.last_validated_at = now
    lic.validation_count = (lic.validation_count or 0) + 1
    if data.cluster_id:
        lic.cluster_id = data.cluster_id

    await _log_validation(db, lic.id, data.cluster_id, client_ip, validation_result, data.version)
    await db.commit()

    return response
