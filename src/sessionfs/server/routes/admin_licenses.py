"""Admin API routes for license management."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from sessionfs.server.auth.dependencies import require_admin
from sessionfs.server.db.engine import get_db
from sessionfs.server.db.models import HelmLicense, LicenseValidation, User
from sessionfs.server.license_keys import generate_license_key

router = APIRouter(prefix="/api/v1/admin/licenses", tags=["admin-licenses"])


class CreateLicenseRequest(BaseModel):
    org_name: str
    contact_email: str
    license_type: str = "paid"
    tier: str = "enterprise"
    seats_limit: int = 25
    days: int | None = None
    notes: str | None = None


class UpdateLicenseRequest(BaseModel):
    status: str | None = None
    tier: str | None = None
    seats_limit: int | None = None
    notes: str | None = None


class ExtendLicenseRequest(BaseModel):
    days: int = 14


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (UTC). SQLite may strip tzinfo."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _effective_status(lic: HelmLicense) -> str:
    """Compute the effective status of a license."""
    if lic.status == "revoked":
        return "revoked"

    now = datetime.now(timezone.utc)

    if lic.license_type == "trial":
        if lic.expires_at and _ensure_utc(lic.expires_at) < now:
            return "expired"
        return "trial"

    if lic.expires_at is None:
        return "active"

    if _ensure_utc(lic.expires_at) < now:
        return "expired"

    days_until = (_ensure_utc(lic.expires_at) - now).total_seconds() / 86400
    if days_until <= 30:
        return "expiring"

    return "active"


def _license_to_dict(lic: HelmLicense) -> dict:
    """Serialize a license to a dict."""
    return {
        "key": lic.id,
        "org_name": lic.org_name,
        "contact_email": lic.contact_email,
        "license_type": lic.license_type,
        "tier": lic.tier,
        "seats_limit": lic.seats_limit,
        "status": lic.status,
        "effective_status": _effective_status(lic),
        "cluster_id": lic.cluster_id,
        "last_validated_at": lic.last_validated_at.isoformat() if lic.last_validated_at else None,
        "validation_count": lic.validation_count,
        "expires_at": lic.expires_at.isoformat() if lic.expires_at else None,
        "created_at": lic.created_at.isoformat() if lic.created_at else None,
        "notes": lic.notes,
    }


@router.post("/", status_code=201)
async def create_license(
    body: CreateLicenseRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Create a new license key."""
    key = generate_license_key(body.license_type)

    expires_at = None
    if body.days is not None:
        expires_at = datetime.now(timezone.utc) + timedelta(days=body.days)

    lic = HelmLicense(
        id=key,
        org_name=body.org_name,
        contact_email=body.contact_email,
        license_type=body.license_type,
        tier=body.tier,
        seats_limit=body.seats_limit,
        status="active",
        expires_at=expires_at,
        notes=body.notes,
    )
    db.add(lic)
    await db.commit()
    await db.refresh(lic)

    return _license_to_dict(lic)


@router.get("/")
async def list_licenses(
    status: str = Query("all"),
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """List licenses with optional status filter."""
    query = select(HelmLicense).order_by(HelmLicense.created_at.desc())
    result = await db.execute(query)
    all_licenses = result.scalars().all()

    items = []
    for lic in all_licenses:
        eff = _effective_status(lic)
        if status != "all":
            if status == "active" and eff not in ("active",):
                continue
            elif status == "trial" and eff != "trial":
                continue
            elif status == "expired" and eff != "expired":
                continue
            elif status == "revoked" and eff != "revoked":
                continue
            elif status == "expiring" and eff != "expiring":
                continue
        items.append(_license_to_dict(lic))

    return {"total": len(items), "licenses": items}


@router.put("/{key}")
async def update_license(
    key: str,
    body: UpdateLicenseRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Update a license's status, tier, seats, or notes."""
    result = await db.execute(select(HelmLicense).where(HelmLicense.id == key))
    lic = result.scalar_one_or_none()
    if lic is None:
        raise HTTPException(status_code=404, detail="License not found")

    if body.status is not None:
        lic.status = body.status
    if body.tier is not None:
        lic.tier = body.tier
    if body.seats_limit is not None:
        lic.seats_limit = body.seats_limit
    if body.notes is not None:
        lic.notes = body.notes

    await db.commit()
    await db.refresh(lic)

    return _license_to_dict(lic)


@router.put("/{key}/extend")
async def extend_license(
    key: str,
    body: ExtendLicenseRequest,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Extend a license by N days from now."""
    result = await db.execute(select(HelmLicense).where(HelmLicense.id == key))
    lic = result.scalar_one_or_none()
    if lic is None:
        raise HTTPException(status_code=404, detail="License not found")

    lic.expires_at = datetime.now(timezone.utc) + timedelta(days=body.days)
    await db.commit()
    await db.refresh(lic)

    return _license_to_dict(lic)


@router.get("/{key}/history")
async def license_history(
    key: str,
    admin: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    """Return validation history for a license key."""
    # Verify the license exists
    result = await db.execute(select(HelmLicense).where(HelmLicense.id == key))
    lic = result.scalar_one_or_none()
    if lic is None:
        raise HTTPException(status_code=404, detail="License not found")

    result = await db.execute(
        select(LicenseValidation)
        .where(LicenseValidation.license_id == key)
        .order_by(LicenseValidation.created_at.desc())
        .limit(200)
    )
    validations = result.scalars().all()

    items = []
    for v in validations:
        items.append({
            "id": v.id,
            "cluster_id": v.cluster_id,
            "ip_address": v.ip_address,
            "result": v.result,
            "version": v.version,
            "created_at": v.created_at.isoformat() if v.created_at else None,
        })

    return {"total": len(items), "validations": items}
