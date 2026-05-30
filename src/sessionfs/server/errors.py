"""Global exception handlers."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from sessionfs.server.schemas.errors import ErrorDetail, ErrorResponse

logger = logging.getLogger(__name__)


# v0.10.24 tk_e7da4c4508d94bac — PostgreSQL SQLSTATE codes for the
# IntegrityError subclasses we surface as structured envelopes.
# https://www.postgresql.org/docs/current/errcodes-appendix.html
_PG_UNIQUE_VIOLATION = "23505"
_PG_FOREIGN_KEY_VIOLATION = "23503"
_PG_NOT_NULL_VIOLATION = "23502"
_PG_CHECK_VIOLATION = "23514"


def _classify_integrity_error(exc: IntegrityError) -> tuple[str, str, str, int]:
    """Map a SQLAlchemy IntegrityError to (code, message, raw_detail, status).

    Cross-DB: tries the asyncpg/psycopg pgcode attribute first (production
    PostgreSQL); falls back to string-matching the SQLite/aiosqlite
    message shape so local-mode + tests classify too.

    Returns 4xx for user-correctable violations (unique = 409, NOT NULL
    and FK from a request shape = 422). FK violations get 500 because
    they almost always indicate a server bug: well-shaped requests
    shouldn't be able to produce a missing FK target.
    """
    raw_text = str(getattr(exc, "orig", exc))
    pgcode: str | None = None
    orig = getattr(exc, "orig", None)
    if orig is not None:
        pgcode = (
            getattr(orig, "pgcode", None)
            or getattr(getattr(orig, "sqlstate", None), "value", None)
            or getattr(orig, "sqlstate", None)
        )

    if pgcode == _PG_UNIQUE_VIOLATION or "UNIQUE constraint failed" in raw_text:
        return (
            "duplicate_resource",
            "A resource with that value already exists.",
            raw_text,
            409,
        )
    if pgcode == _PG_NOT_NULL_VIOLATION or "NOT NULL constraint failed" in raw_text:
        return (
            "missing_required_field",
            "A required field was not provided.",
            raw_text,
            422,
        )
    if pgcode == _PG_FOREIGN_KEY_VIOLATION or "FOREIGN KEY constraint failed" in raw_text:
        # Server bug class — well-shaped requests shouldn't produce a
        # dangling FK reference. Surface as 500 with structured body so
        # the CLI and dashboard can tell the difference from a generic
        # IntegrityError and so the diagnosis doesn't take three
        # releases like the 2026-05-20 incident.
        return (
            "foreign_key_violation",
            "Database integrity error: a referenced row was missing.",
            raw_text,
            500,
        )
    if pgcode == _PG_CHECK_VIOLATION or "CHECK constraint failed" in raw_text:
        return (
            "check_constraint_violation",
            "A field value violated a check constraint.",
            raw_text,
            422,
        )
    # Catch-all — still surface a structured body even when we don't
    # know the violation class so the client doesn't see bare
    # "Internal Server Error" with no detail.
    return (
        "integrity_error",
        "Database integrity error.",
        raw_text,
        500,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register global exception handlers on the app."""

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        if isinstance(exc.detail, dict):
            body = ErrorResponse(
                error=ErrorDetail(
                    code=exc.detail.get("code", str(exc.status_code)),
                    message=exc.detail.get("message", "Error"),
                    details={
                        k: v
                        for k, v in exc.detail.items()
                        if k not in ("code", "message")
                    },
                )
            )
        else:
            body = ErrorResponse(
                error=ErrorDetail(
                    code=str(exc.status_code),
                    message=str(exc.detail),
                )
            )
        # Preserve headers raised with the HTTPException (e.g. Retry-After
        # on rate limits, X-Deprecation-Warning on legacy paths). Without
        # this, FastAPI's default forwarding is bypassed by our custom
        # handler and the headers silently disappear.
        headers = getattr(exc, "headers", None) or None
        return JSONResponse(
            status_code=exc.status_code,
            content=body.model_dump(),
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        # Sanitize errors: Pydantic v2 can include non-serializable objects in ctx
        sanitized_errors = []
        for err in exc.errors():
            clean = {k: v for k, v in err.items() if k != "ctx"}
            if "ctx" in err:
                clean["ctx"] = {k: str(v) for k, v in err["ctx"].items()}
            sanitized_errors.append(clean)
        body = ErrorResponse(
            error=ErrorDetail(
                code="422",
                message="Validation error",
                details={"errors": sanitized_errors},
            )
        )
        return JSONResponse(status_code=422, content=body.model_dump())

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(request: Request, exc: IntegrityError):
        """v0.10.24 tk_e7da4c4508d94bac — surface structured envelopes for
        SQLAlchemy IntegrityError so clients see actionable detail
        instead of Starlette's default plain-text 'Internal Server
        Error'. najitestech (GH #51 ask #2) was the trigger.

        Always log the raw exception at ERROR with the request path so
        Cloud Run + Sentry-like ingestion still get the full DBAPI
        message — the client envelope intentionally strips the raw
        text to avoid leaking column names or row values."""
        code, message, raw_text, status = _classify_integrity_error(exc)
        logger.error(
            "IntegrityError on %s %s: %s",
            request.method,
            request.url.path,
            raw_text,
        )
        body = ErrorResponse(
            error=ErrorDetail(
                code=code,
                message=message,
                details={"status": status},
            )
        )
        return JSONResponse(status_code=status, content=body.model_dump())
