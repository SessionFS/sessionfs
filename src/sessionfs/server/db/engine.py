"""Async database engine and session factory."""

from __future__ import annotations

import ssl as ssl_mod
from collections.abc import AsyncGenerator
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

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

    _engine = create_async_engine(
        clean_url, echo=echo, connect_args=connect_args, **pool_kwargs,
    )
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
