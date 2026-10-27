"""Server configuration via environment variables."""

from __future__ import annotations

import os

from pydantic import field_validator
from pydantic_settings import BaseSettings


class ServerConfig(BaseSettings):
    """Server configuration with SFS_ env prefix."""

    model_config = {"env_prefix": "SFS_"}

    database_url: str = "sqlite+aiosqlite:///./sessionfs.db"
    database_echo: bool = False
    database_pool_size: int = 20
    database_max_overflow: int = 40
    database_pool_timeout: int = 60
    database_pool_recycle: int = 1800  # Recycle connections after 30 min

    blob_store_type: str = "local"  # "local", "s3", or "gcs"
    blob_store_local_path: str = "./data/blobs"
    s3_bucket: str = ""
    s3_region: str = "us-east-1"
    s3_endpoint_url: str | None = None
    s3_prefix: str = ""
    gcs_bucket: str = ""

    require_email_verification: bool = True
    email_provider: str = "auto"  # "resend", "smtp", "none", or "auto"
    email_from: str = "SessionFS <noreply@sessionfs.dev>"
    resend_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_tls: bool = True
    smtp_ssl: bool = False
    smtp_verify_ssl: bool = True
    verification_secret: str = ""
    max_sync_bytes_free: int = 52_428_800  # 50 MB — free tier
    max_sync_bytes_paid: int = 314_572_800  # 300 MB — pro/team/enterprise/admin
    retention_days_free: int = 14

    host: str = "0.0.0.0"
    port: int = 8000

    cors_origins: list[str] = []
    log_level: str = "INFO"
    rate_limit_per_minute: int = 120
    dashboard_dir: str = "./static"

    # App URL for billing redirects (configurable for staging/self-hosted)
    app_url: str = "https://app.sessionfs.dev"
    # Dashboard origin for building filtered-view links (org digest emails).
    # Empty by default so self-hosted installs never link to the hosted
    # dashboard; the digest sweep falls back to app_url, then skips links.
    dashboard_url: str = ""

    # Deployment environment ("development"/"test" relax production-only
    # checks; unset = development when the database is SQLite).
    environment: str = ""
    # Canonical public API origin, e.g. https://api.sessionfs.dev. Default
    # OIDC audience for marketplace trusted publishing.
    public_api_url: str = ""
    # Overrides the trusted-publishing OIDC audience (must be deployment-unique).
    trusted_publishing_audience: str = ""
    # GitLab instances whose CI ID tokens may be exchanged for publish tokens
    # (comma-separated https origins, e.g. https://gitlab.example.com).
    # Unset = https://gitlab.com only; "none" disables GitLab. Listed values
    # replace the default. An invalid entry refuses startup.
    gitlab_oidc_issuers: str = ""
    # Allow the per-user GitLab integration to target private / loopback /
    # plain-http hosts (an internal self-hosted GitLab). Default false: the
    # hosted service only talks to public https GitLab hosts. The Helm chart
    # sets this true. Link-local and cloud-metadata addresses stay blocked.
    gitlab_allow_private_hosts: bool = False
    # Allow user-supplied LLM endpoints (judge settings base_url, audit and
    # narrative-summary base_url) to target private / loopback / plain-http
    # hosts, e.g. an in-cluster Ollama or vLLM. Default false: the hosted
    # service only calls public https endpoints. Link-local and cloud-metadata
    # addresses stay blocked either way.
    llm_allow_private_hosts: bool = False
    # Public GitHub mirror of the public native catalog, as owner/repo (e.g.
    # SessionFS/marketplace). Set: the public catalog's add commands lead with
    # the keyless GitHub form. A scheduled `sfs catalog export` keeps it in
    # sync. Anything but owner/repo refuses startup.
    public_catalog_mirror: str = ""

    # Stripe billing
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_starter: str = ""
    stripe_price_pro: str = ""
    stripe_price_team: str = ""

    @field_validator("public_catalog_mirror")
    @classmethod
    def _check_public_catalog_mirror(cls, value: str) -> str:
        from sessionfs.skills.catalog_mirror import parse_mirror

        return parse_mirror(value) or ""


def dashboard_link_base(config) -> str:
    """Dashboard origin for links the server emits (emails, Jira footers).

    Resolution order: ``SFS_DASHBOARD_URL`` (``config.dashboard_url``), then
    ``config.app_url`` only when the operator set it explicitly (field or
    ``SFS_APP_URL``), then ``""`` which means "emit no link". There is no
    hosted-dashboard fallback, so self-hosted installs never link to the
    hosted app.
    """
    explicit_app_url = "app_url" in getattr(config, "model_fields_set", set()) or bool(
        os.environ.get("SFS_APP_URL")
    )
    return str(
        getattr(config, "dashboard_url", None)
        or (getattr(config, "app_url", None) if explicit_app_url else None)
        or ""
    ).strip().rstrip("/")
