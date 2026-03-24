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
    gcs_bucket: str = ""

    resend_api_key: str = ""
    verification_secret: str = ""
    max_sync_bytes_free: int = 52_428_800  # 50 MB — free tier
    max_sync_bytes_paid: int = 314_572_800  # 300 MB — pro/team/enterprise/admin
    retention_days_free: int = 14

    host: str = "0.0.0.0"
    port: int = 8000

    cors_origins: list[str] = []
    log_level: str = "INFO"
    rate_limit_per_minute: int = 100
    dashboard_dir: str = "./static"
