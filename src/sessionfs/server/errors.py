"""Global exception handlers."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError, IntegrityError
from starlette.exceptions import HTTPException as StarletteHTTPException

from sessionfs.server.llm_keys import EncryptionUnavailableError
from sessionfs.server.schemas.errors import ErrorDetail, ErrorResponse
from sessionfs.server.services.entitlements import BillingLockRetry

logger = logging.getLogger(__name__)


# v0.10.24 tk_e7da4c4508d94bac — PostgreSQL SQLSTATE codes for the
# IntegrityError subclasses we surface as structured envelopes.
# https://www.postgresql.org/docs/current/errcodes-appendix.html
_PG_UNIQUE_VIOLATION = "23505"
_PG_FOREIGN_KEY_VIOLATION = "23503"
_PG_NOT_NULL_VIOLATION = "23502"
_PG_CHECK_VIOLATION = "23514"
# Transaction aborted by a lost concurrency race: safe to retry.
_PG_DEADLOCK_DETECTED = "40P01"
_PG_SERIALIZATION_FAILURE = "40001"
_PG_RETRYABLE_SQLSTATES = frozenset({_PG_DEADLOCK_DETECTED, _PG_SERIALIZATION_FAILURE})


def db_sqlstate(exc: BaseException) -> str | None:
    """SQLSTATE of a SQLAlchemy DBAPIError (asyncpg or psycopg), if any."""
    orig = getattr(exc, "orig", None)
    if orig is None:
        return None
    code = (
        getattr(orig, "pgcode", None)
        or getattr(getattr(orig, "sqlstate", None), "value", None)
        or getattr(orig, "sqlstate", None)
    )
    if code is None:
        cause = getattr(orig, "__cause__", None)
        code = getattr(cause, "sqlstate", None)
    return code if isinstance(code, str) else None


def _integrity_error_summary(exc: IntegrityError, code: str) -> str:
    """Loggable description of an IntegrityError with no row values.

    Uses only the driver's structural attributes (asyncpg exposes them on the
    wrapped exception) and the classified code.
    """
    parts = [f"code={code}"]
    orig = getattr(exc, "orig", None)
    candidates = [orig, getattr(orig, "__cause__", None)]
    seen: set[str] = set()
    for source in candidates:
        if source is None:
            continue
        for attr in ("sqlstate", "pgcode", "constraint_name", "table_name", "column_name"):
            value = getattr(source, attr, None)
            value = getattr(value, "value", value)
            if isinstance(value, str) and value and attr not in seen:
                seen.add(attr)
                parts.append(f"{attr}={value}")
    parts.append(f"driver={type(orig).__name__ if orig is not None else type(exc).__name__}")
    return " ".join(parts)


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


def _concurrent_update_response() -> JSONResponse:
    """409 for a lost concurrency race: safe for the client to retry."""
    body = ErrorResponse(
        error=ErrorDetail(
            code="concurrent_update",
            message="Another request changed the same records at the same time. "
            "Retry the request.",
            details={"status": 409, "retryable": True},
        )
    )
    return JSONResponse(status_code=409, content=body.model_dump(), headers={"Retry-After": "1"})


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

    @app.exception_handler(EncryptionUnavailableError)
    async def encryption_unavailable_handler(
        request: Request, exc: EncryptionUnavailableError
    ):
        """A missing/default verification secret in production: 503 with a
        clear envelope so the operator sees the misconfiguration, not a bare
        ``Internal Server Error`` (and never a token encrypted under a public
        placeholder key)."""
        logger.error(
            "Encryption unavailable on %s %s: %s", request.method, request.url.path, exc
        )
        body = ErrorResponse(
            error=ErrorDetail(
                code="encryption_unavailable",
                message=str(exc),
                details={"status": 503},
            )
        )
        return JSONResponse(status_code=503, content=body.model_dump())

    @app.exception_handler(BillingLockRetry)
    async def billing_lock_retry_handler(request: Request, exc: BillingLockRetry):
        """A billing row's Stripe customer changed while its locks were
        being taken; the transaction was abandoned rather than lock out of
        order. Same retry contract as a deadlock."""
        logger.warning("Billing lock retry on %s %s: %s", request.method, request.url.path, exc)
        return _concurrent_update_response()

    @app.exception_handler(DBAPIError)
    async def dbapi_error_handler(request: Request, exc: DBAPIError):
        """A transaction aborted by PostgreSQL deadlock detection or a
        serialization failure is a lost race, not a server bug: answer 409
        with a retry hint (a Stripe webhook answered this way is retried by
        Stripe). Every other database error is re-raised to the default
        500 path unchanged. IntegrityError keeps its own handler (more
        specific class)."""
        sqlstate = db_sqlstate(exc)
        if sqlstate not in _PG_RETRYABLE_SQLSTATES:
            raise exc
        logger.warning(
            "Retryable database conflict on %s %s: %s (sqlstate=%s)",
            request.method,
            request.url.path,
            type(getattr(exc, "orig", exc)).__name__,
            sqlstate,
        )
        return _concurrent_update_response()

    @app.exception_handler(IntegrityError)
    async def integrity_error_handler(request: Request, exc: IntegrityError):
        """v0.10.24 tk_e7da4c4508d94bac — surface structured envelopes for
        SQLAlchemy IntegrityError so clients see actionable detail
        instead of Starlette's default plain-text 'Internal Server
        Error'. najitestech (GH #51 ask #2) was the trigger.

        The log line carries only structural facts (class, SQLSTATE,
        constraint/table/column names), never the database's message:
        PostgreSQL details such as "Failing row contains (...)" or
        "Key (...)=(...)" include row values — session text, titles,
        emails, even secrets. The client envelope is stripped too."""
        code, message, _raw_text, status = _classify_integrity_error(exc)
        logger.error(
            "IntegrityError on %s %s: %s",
            request.method,
            request.url.path,
            _integrity_error_summary(exc, code),
        )
        body = ErrorResponse(
            error=ErrorDetail(
                code=code,
                message=message,
                details={"status": status},
            )
        )
        return JSONResponse(status_code=status, content=body.model_dump())
