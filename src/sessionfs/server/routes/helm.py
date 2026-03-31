"""Helm license validation endpoint for self-hosted deployments."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from pydantic import BaseModel, field_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import JSONResponse

from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import HelmLicense
from sessionfs.server.tiers import get_features_for_tier

router = APIRouter(prefix="/api/v1/helm", tags=["helm"])


class HelmValidateRequest(BaseModel):
    license_key: str
    cluster_id: str | None = None

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


@router.post("/validate")
async def validate_helm_license(
    data: HelmValidateRequest,
    db: AsyncSession = Depends(get_db),
):
    """Validate a Helm license key. Called by init container on pod start."""
    result = await db.execute(
        select(HelmLicense).where(HelmLicense.id == data.license_key)
    )
    license = result.scalar_one_or_none()

    if not license or license.status != "active":
        return JSONResponse(
            status_code=403,
            content={"valid": False, "error": "Invalid license key"},
        )

    if license.expires_at and license.expires_at < datetime.now(timezone.utc):
        return JSONResponse(
            status_code=403,
            content={"valid": False, "error": "License expired"},
        )

    return {
        "valid": True,
        "tier": license.tier,
        "seats": license.seats_limit,
        "expires_at": license.expires_at.isoformat() if license.expires_at else None,
        "features": sorted(get_features_for_tier(license.tier)),
    }
