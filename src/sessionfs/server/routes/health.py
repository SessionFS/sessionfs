"""Health check endpoint."""

from __future__ import annotations

from fastapi import APIRouter

from sessionfs import __version__

router = APIRouter()


@router.get("/health")
async def health():
    return {"status": "healthy", "version": __version__, "service": "sessionfs-api"}
