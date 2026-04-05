"""Server configuration via environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class ServerConfig(BaseSettings):
    """Server configuration with SFS_ env prefix."""

    model_config = {"env_prefix": "SFS_"}

    database_url: str = "sqlite+aiosqlite:///./sessionfs.db"
    database_echo: bool = False

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

    # Stripe billing
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""
    stripe_price_starter: str = ""
    stripe_price_pro: str = ""
    stripe_price_team: str = ""
