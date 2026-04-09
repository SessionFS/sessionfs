"""FastAPI application factory."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from sessionfs import __version__
from sessionfs.server.config import ServerConfig
from sessionfs.server.db.engine import close_engine, init_engine
from sessionfs.server.errors import register_exception_handlers
from sessionfs.server.middleware import RequestLoggingMiddleware
from sessionfs.server.routes import admin, admin_licenses, audit, auth, billing, bookmarks, dlp, handoffs, health, helm, knowledge, org, projects, sessions, settings, summaries, sync, telemetry, webhooks, wiki
from sessionfs.server.storage.local import LocalBlobStore


def _configure_logging() -> None:
    """Ensure sessionfs loggers propagate to root so uvicorn shows them."""
    import logging

    for name in ("sessionfs.api", "sessionfs.email", "sessionfs.judge.providers"):
        logger = logging.getLogger(name)
        if not logger.handlers:
            logger.propagate = True


def create_app(config: ServerConfig | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if config is None:
        config = ServerConfig()

    _configure_logging()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        init_engine(
            config.database_url,
            echo=config.database_echo,
            pool_size=config.database_pool_size,
            max_overflow=config.database_max_overflow,
            pool_timeout=config.database_pool_timeout,
            pool_recycle=config.database_pool_recycle,
        )

        if config.blob_store_type == "s3":
            from sessionfs.server.storage.s3 import S3BlobStore

            app.state.blob_store = S3BlobStore(
                bucket=config.s3_bucket,
                region=config.s3_region,
                endpoint_url=config.s3_endpoint_url,
                prefix=config.s3_prefix,
            )
        elif config.blob_store_type == "gcs":
            from sessionfs.server.storage.gcs import GCSBlobStore

            app.state.blob_store = GCSBlobStore(bucket=config.gcs_bucket)
        else:
            root = Path(config.blob_store_local_path)
            root.mkdir(parents=True, exist_ok=True)
            app.state.blob_store = LocalBlobStore(root)

        from sessionfs.server.email import NullProvider, create_email_provider

        provider = create_email_provider(config)
        # Only set email_service if a real provider is configured
        app.state.email_service = None if isinstance(provider, NullProvider) else provider

        yield

        # Shutdown
        await close_engine()

    app = FastAPI(
        title="SessionFS API",
        version=__version__,
        lifespan=lifespan,
    )

    # M4: CORS — only add middleware when explicitly configured (default is empty)
    if config.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "Authorization"],
            max_age=3600,
        )

    # Request logging (4xx responses)
    app.add_middleware(RequestLoggingMiddleware)

    # Exception handlers
    register_exception_handlers(app)

    # Routes
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(sessions.router)
    app.include_router(handoffs.router)
    app.include_router(audit.router)
    app.include_router(settings.router)
    app.include_router(bookmarks.router)
    app.include_router(admin.router)
    app.include_router(knowledge.router)
    app.include_router(wiki.router)
    app.include_router(projects.router)
    app.include_router(summaries.router)
    app.include_router(summaries.batch_router)
    app.include_router(sync.router)
    app.include_router(webhooks.router)
    app.include_router(billing.router)
    app.include_router(billing.webhook_router)
    app.include_router(org.router)
    app.include_router(helm.router)
    app.include_router(admin_licenses.router)
    app.include_router(telemetry.router)
    app.include_router(dlp.router)

    # Serve dashboard static files if the dist directory exists.
    # The dashboard is a React SPA — all non-API routes serve index.html.
    static_dir = Path(config.dashboard_dir)
    if static_dir.is_dir() and (static_dir / "index.html").exists():
        from starlette.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        # Serve /assets/* directly (JS, CSS, images)
        assets_dir = static_dir / "assets"
        if assets_dir.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="dashboard-assets")

        # SPA fallback: all other non-API paths serve index.html
        @app.get("/{path:path}", include_in_schema=False)
        async def spa_fallback(path: str):
            # Don't intercept API or health routes
            if path.startswith("api/") or path == "health":
                from fastapi import HTTPException
                raise HTTPException(status_code=404)
            file = static_dir / path
            if file.is_file():
                return FileResponse(str(file))
            return FileResponse(str(static_dir / "index.html"))

    return app
