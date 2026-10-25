"""Async database engine and session factory."""

from __future__ import annotations

import ssl as ssl_mod
from collections.abc import AsyncGenerator
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _prepare_url(database_url: str) -> tuple[str, dict]:
    """Strip SSL params that asyncpg doesn't understand and return clean URL + connect_args."""
    connect_args: dict = {}

    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
        return database_url, connect_args

    # Only attempt SSL param extraction if URL has a query string with sslmode/ssl
    # Use string manipulation to avoid urlparse failures on complex URLs
    # (e.g. Cloud SQL socket paths, passwords with special chars)
    ssl_mode = None
    clean_url = database_url

    if "?" in database_url and ("sslmode=" in database_url or "ssl=" in database_url):
        try:
            parsed = urlparse(database_url)
            params = parse_qs(parsed.query)

            for key in ("sslmode", "ssl"):
                vals = params.pop(key, None)
                if vals:
                    ssl_mode = vals[0]

            clean_query = urlencode({k: v[0] for k, v in params.items()})
            clean_url = urlunparse(parsed._replace(query=clean_query))
        except (ValueError, TypeError):
            # urlparse can fail on some URL formats (IPv6, special chars)
            # Fall through with original URL — let SQLAlchemy handle it
            pass

    # Configure SSL via connect_args for asyncpg
    if ssl_mode in ("require", "prefer"):
        ctx = ssl_mod.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl_mod.CERT_NONE
        connect_args["ssl"] = ctx
    elif ssl_mode in ("verify-ca", "verify-full"):
        ctx = ssl_mod.create_default_context()
        connect_args["ssl"] = ctx

    return clean_url, connect_args


def _lower(s: str | None) -> str | None:
    """Unicode-aware `str.lower` that is NULL-safe.

    SQLite's built-in `lower()` returns NULL for NULL input, but
    `str.lower(None)` raises TypeError, which sqlite3 surfaces as
    ``OperationalError: user-defined function raised exception``. This
    override replaces the built-in app-wide, so folding any nullable
    column (e.g. ``Ticket.assigned_to``, ``Handoff.recipient_email``,
    ``Session.title``) would 500 as soon as a scanned row held NULL.
    Pass NULL through to keep the built-in's NULL semantics.
    """
    return None if s is None else s.lower()


def _register_unicode_lower(dbapi_connection, connection_record) -> None:
    """Register Python's Unicode-aware `str.lower` as SQLite's `lower()`.

    SQLite's built-in `lower()` folds ASCII only, so `Álex` and `álex`
    keep their distinct non-ASCII letters and the (org_id, lower(name))
    org-persona UNIQUE index treats them as different names — letting both
    through, violating its case-insensitive-unique contract. Python's
    `str.lower` is full-Unicode and matches the app's `.lower()` checks.
    """
    dbapi_connection.create_function("lower", 1, _lower, deterministic=True)


def install_sqlite_unicode_lower(engine: AsyncEngine) -> None:
    """Install the Unicode-aware `lower()` on every SQLite connection of `engine`."""
    event.listen(engine.sync_engine, "connect", _register_unicode_lower)


def init_engine(
    database_url: str,
    echo: bool = False,
    pool_size: int = 20,
    max_overflow: int = 40,
    pool_timeout: int = 60,
    pool_recycle: int = 1800,
) -> AsyncEngine:
    """Create the async engine and session factory."""
    global _engine, _session_factory

    clean_url, connect_args = _prepare_url(database_url)

    # SQLite doesn't support connection pooling params
    pool_kwargs: dict = {}
    if not database_url.startswith("sqlite"):
        pool_kwargs = {
            "pool_size": pool_size,
            "max_overflow": max_overflow,
            "pool_timeout": pool_timeout,
            "pool_recycle": pool_recycle,
            "pool_pre_ping": True,  # Verify connections before use
        }

    # hide_parameters: a failed statement's error (and anything that logs it)
    # must not include the bound values — they carry session text, titles and
    # search content, which can hold secrets before DLP has run.
    _engine = create_async_engine(
        clean_url,
        echo=echo,
        connect_args=connect_args,
        hide_parameters=True,
        **pool_kwargs,
    )
    if database_url.startswith("sqlite"):
        # SQLite's lower() is ASCII-only; the org-persona unique index
        # (org_id, lower(name)) needs Unicode-aware folding (GH #70).
        install_sqlite_unicode_lower(_engine)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)
    return _engine


async def close_engine() -> None:
    """Dispose of the engine."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None


async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency that yields an async DB session."""
    if _session_factory is None:
        raise RuntimeError("Database engine not initialized. Call init_engine() first.")
    async with _session_factory() as session:
        yield session


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the configured session factory for callers that need fresh
    sessions (e.g. helper turns, which persist outside the request scope).

    Raises when the engine is not initialized, mirroring :func:`get_db`.
    """
    if _session_factory is None:
        raise RuntimeError("Database engine not initialized. Call init_engine() first.")
    return _session_factory
