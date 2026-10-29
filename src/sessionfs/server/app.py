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
from sessionfs.server.middleware import MetricsMiddleware, RequestLoggingMiddleware
from sessionfs.server.routes import activation, admin, admin_licenses, agent_feedback, agent_runs, api_keys, audit, auth, auth_sso, billing, bookmarks, confluence, dlp, handoffs, health, helm, helper, helper_settings, jira, knowledge, llm_providers, marketplace, marketplace_disclosure, marketplace_discovery, marketplace_moderation, marketplace_publishers, marketplace_scanning, me, meeting_actions, meetings, metrics, native_catalogs, org, org_llm_settings, org_members, personas, project_groups, project_transfers, projects, residents, retrieval_audit, rules, sessions, settings, skill_policy, sso_admin, summaries, sync, teams, telemetry, tickets, trusted_reviewers, uploads, webhooks, wiki, work_queues
from sessionfs.server.storage.local import LocalBlobStore


def _configure_logging(level: str = "INFO") -> None:
    """Ensure sessionfs loggers propagate to root so uvicorn shows them.

    Under plain uvicorn nothing handles the root logger, so only WARNING and
    above reached stderr and INFO lines (e.g. the per-push timing line) were
    dropped. In that case only, the ``sessionfs`` loggers get a stderr
    handler at ``level`` (SFS_LOG_LEVEL); a deployment with its own logging
    config (or pytest's capture) is left alone.
    """
    import logging

    for name in ("sessionfs.api", "sessionfs.email", "sessionfs.judge.providers"):
        logger = logging.getLogger(name)
        if not logger.handlers:
            logger.propagate = True

    package = logging.getLogger("sessionfs")
    resolved = logging.getLevelName(str(level).upper())
    if not logging.getLogger().handlers and not package.handlers and isinstance(resolved, int):
        if package.level == logging.NOTSET:
            package.setLevel(resolved)
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(levelname)s:     %(name)s: %(message)s"))
        package.addHandler(handler)


def create_app(config: ServerConfig | None = None) -> FastAPI:
    """Create and configure the FastAPI application."""
    if config is None:
        config = ServerConfig()

    _configure_logging(config.log_level)

    # User-supplied LLM endpoints are re-checked against the SSRF policy right
    # before every outbound request (judge, summaries, knowledge, compile).
    from sessionfs.judge.providers import set_base_url_guard
    from sessionfs.server.llm_url_guard import guard_llm_base_url

    set_base_url_guard(guard_llm_base_url)

    # Marketplace trusted publishing: resolve environment + OIDC audience ONCE
    # (refusing to start outside development on a generic audience, whatever
    # its source); the routes use exactly this value.
    from sessionfs.server.services.github_oidc import resolve_trusted_publishing

    trusted_publishing = resolve_trusted_publishing(config)

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

        # Defense in depth (GitHub #66): if the DB schema does not match the
        # code's alembic head (e.g. the migration step was skipped on deploy),
        # log one actionable error. Best-effort and bounded, so it never blocks
        # startup or /health.
        from sessionfs.server.db.migration_check import log_migration_gap_if_behind

        await log_migration_gap_if_behind()

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

        # The Jira completion-evidence job runs in a spawned task with no
        # request, so it reads session archives through this bound store.
        from sessionfs.server.services.jira_sync import set_jira_sync_blob_store

        set_jira_sync_blob_store(app.state.blob_store)

        from sessionfs.server.email import NullProvider, create_email_provider

        provider = create_email_provider(config)
        # Only set email_service if a real provider is configured
        app.state.email_service = None if isinstance(provider, NullProvider) else provider

        # v0.10.22 — services that need app_url for building dashboard
        # links (org-invite emails, future handoff deep-links) read from
        # request.app.state.config rather than re-importing ServerConfig.
        app.state.config = config

        # Parse the session helper's bundled docs index off the event loop so
        # the first docs search doesn't pay for it (non-blocking).
        from sessionfs.server.helper.docs import warm_docs_index_in_background

        warm_docs_index_in_background()

        yield

        # Shutdown
        await close_engine()

    app = FastAPI(
        title="SessionFS API",
        version=__version__,
        lifespan=lifespan,
    )
    app.state.trusted_publishing = trusted_publishing

    # M4: CORS — only add middleware when explicitly configured (default is empty)
    if config.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=config.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "Authorization", "If-Match", "If-None-Match"],
            # tk_1a73a7bf359f41c3 — expose ETag so cross-origin browsers can
            # READ it. The dashboard's GET /rules stores response.headers
            # .get('ETag') and replays it as If-Match on the next PUT
            # (optimistic concurrency). Without this, a cross-origin fetch
            # (app.sessionfs.dev → api.sessionfs.dev on cloud) hides the
            # ETag from JS per the Fetch spec, so etag resolves to '' and
            # the PUT sends `If-Match: ''` → rules.py raises 428. Same-origin
            # Helm deployments behaved correctly because the header was
            # always readable there — which is why the bug was cloud-only.
            # v0.10.24 (febd732) added If-Match to allow_headers (send side);
            # this completes the round-trip by exposing ETag (read side).
            # GitHub #103: the unpaged Jira projects list reports a cut-off
            # list through X-Jira-Projects-Truncated; expose it so a
            # cross-origin caller can read it like a same-origin one.
            expose_headers=["ETag", "X-Jira-Projects-Truncated"],
            max_age=3600,
        )

    # Request logging (4xx responses)
    app.add_middleware(RequestLoggingMiddleware)
    # Operator metrics: request duration by route template. Added after the
    # logging middleware so both observe the same request/response pair.
    app.add_middleware(MetricsMiddleware)

    # Exception handlers
    register_exception_handlers(app)

    # Routes
    app.include_router(health.router)
    app.include_router(auth.router)
    app.include_router(auth_sso.router)
    app.include_router(sessions.router)
    app.include_router(uploads.router)
    # v0.14.0 — public share view at /api/v1/share/{token}/view
    app.include_router(sessions.share_view_router, prefix="/api/v1")
    app.include_router(handoffs.router)
    app.include_router(teams.router)
    # v0.10.10 — scoped service keys (org-scoped admin) + personal user keys
    app.include_router(api_keys.service_key_router)
    app.include_router(api_keys.personal_key_router)
    app.include_router(trusted_reviewers.router)
    app.include_router(residents.router)
    app.include_router(audit.router)
    app.include_router(settings.router)
    app.include_router(llm_providers.router)
    app.include_router(bookmarks.router)
    app.include_router(admin.router)
    app.include_router(metrics.router)
    app.include_router(knowledge.router)
    app.include_router(wiki.router)
    # IMPORTANT: rules.router AND project_transfers.router must precede
    # projects.router because the latter uses a catch-all
    # `/{git_remote_normalized:path}` pattern that would otherwise swallow
    # `/{project_id}/rules`, `/{project_id}/transfer`, and sibling endpoints.
    app.include_router(rules.router)
    app.include_router(project_transfers.router)
    # Project-groups project subpath must precede projects' catch-all
    # `/{git_remote_normalized:path}` (same precedence trick as rules/transfers).
    app.include_router(project_groups.project_router)
    # Personas register BEFORE projects so the `/personas` subpath is
    # matched before projects' catch-all `/{git_remote_normalized:path}`
    # route (v0.10.1 Phase 2 — same precedence trick as rules/transfers).
    app.include_router(personas.router)
    # GH #70 — org-level personas under /api/v1/orgs/{org_id}/personas.
    app.include_router(personas.org_router)
    # Tickets same precedence (v0.10.1 Phase 3): `/tickets` subpath
    # under projects must beat the catch-all.
    app.include_router(tickets.router)
    app.include_router(tickets.by_id_router)
    app.include_router(me.router)
    app.include_router(project_groups.me_router)
    # Jira promotion routes register before projects.router's catch-all
    # `/{git_remote_normalized:path}` (same precedence trick as
    # rules/transfers/personas/tickets); org-scoped connection routes are
    # safe wherever they land but sit here for symmetry.
    app.include_router(jira.router)
    app.include_router(jira.org_router)
    # Confluence promotion routes register before projects.router's catch-all
    # for the same reason as Jira (page/context publish under /projects).
    app.include_router(confluence.router)
    app.include_router(confluence.org_router)
    app.include_router(jira.webhook_router)
    # Agent runs same precedence (v0.10.2): `/agent-runs` subpath under
    # projects must beat the projects catch-all `/{git_remote:path}`.
    app.include_router(agent_runs.router)
    app.include_router(agent_feedback.router)
    app.include_router(retrieval_audit.router)
    # Work queues same precedence (WQ-P2): `/work-queues` subpath under
    # projects must beat the projects catch-all `/{git_remote:path}`.
    app.include_router(work_queues.router)
    # Meetings' project subpath must precede projects' catch-all
    # `/{git_remote_normalized:path}` (same precedence trick as rules/transfers).
    app.include_router(meetings.project_router)
    # Marketplace has project subpaths and must precede the project catch-all.
    app.include_router(marketplace.router)
    app.include_router(marketplace_moderation.router)
    app.include_router(marketplace_scanning.router)
    app.include_router(marketplace_disclosure.router)
    app.include_router(marketplace_publishers.router)
    app.include_router(native_catalogs.router)
    app.include_router(skill_policy.router)
    app.include_router(marketplace_discovery.router)
    app.include_router(projects.router)
    app.include_router(summaries.router)
    app.include_router(summaries.batch_router)
    app.include_router(sync.router)
    app.include_router(webhooks.router)
    app.include_router(billing.router)
    app.include_router(billing.webhook_router)
    app.include_router(org.router)
    app.include_router(org_members.router)
    app.include_router(project_groups.router)
    app.include_router(org_llm_settings.router)
    app.include_router(helper_settings.user_router)
    app.include_router(helper_settings.org_router)
    app.include_router(helper.router)
    # Meeting transcripts (GitHub #116): top-level /meetings and group
    # /groups/{id}/meetings paths have no projects catch-all to beat.
    app.include_router(meetings.router)
    app.include_router(meetings.group_router)
    app.include_router(meeting_actions.router)
    app.include_router(sso_admin.router)
    app.include_router(activation.router)
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
