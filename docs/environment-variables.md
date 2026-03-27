# Environment Variables

All SessionFS server environment variables use the `SFS_` prefix.

## Database

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_DATABASE_URL` | PostgreSQL connection URL | `sqlite+aiosqlite:///./sessionfs.db` |
| `SFS_DATABASE_ECHO` | Log all SQL queries | `false` |

**Note:** For asyncpg connections, use the `postgresql+asyncpg://` driver prefix. If you pass `postgresql://`, SessionFS will auto-convert it. SSL parameters like `?sslmode=require` are handled internally — asyncpg uses a proper SSL context instead of URL parameters.

## Email

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_EMAIL_PROVIDER` | Email provider: `resend`, `smtp`, `none`, or `auto` | `auto` |
| `SFS_EMAIL_FROM` | From address for all emails | `noreply@sessionfs.dev` |
| `SFS_RESEND_API_KEY` | Resend API key (when provider is `resend` or `auto`) | — |
| `SFS_SMTP_HOST` | SMTP server hostname | — |
| `SFS_SMTP_PORT` | SMTP server port | `587` |
| `SFS_SMTP_USERNAME` | SMTP auth username | — |
| `SFS_SMTP_PASSWORD` | SMTP auth password | — |
| `SFS_SMTP_TLS` | Use STARTTLS (port 587) | `true` |
| `SFS_SMTP_SSL` | Use implicit SSL (port 465) | `false` |

**Auto-detection:** When `SFS_EMAIL_PROVIDER=auto` (default), SessionFS checks:
1. If `SFS_RESEND_API_KEY` is set → uses Resend
2. If `SFS_SMTP_HOST` is set → uses SMTP
3. Otherwise → logs emails without sending (no crash)

## Authentication

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_VERIFICATION_SECRET` | Secret for email verification JWT tokens | `dev-verification-secret` |
| `SFS_REQUIRE_EMAIL_VERIFICATION` | Require email verification on signup | `true` |
| `SFS_ENCRYPTION_KEY` | Fernet key for encrypting stored API keys | — |

## Blob Storage

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_BLOB_STORE_TYPE` | Storage backend: `local`, `s3`, or `gcs` | `local` |
| `SFS_BLOB_STORE_LOCAL_PATH` | Path for local blob storage | `./data/blobs` |
| `SFS_S3_BUCKET` | S3 bucket name (no slashes — use `SFS_S3_PREFIX` for key prefixes) | — |
| `SFS_S3_REGION` | AWS region | `us-east-1` |
| `SFS_S3_PREFIX` | Optional key prefix for all S3 objects (e.g. `sessionfs/`) | — |
| `SFS_S3_ENDPOINT_URL` | Custom S3 endpoint (for MinIO) | — |
| `SFS_GCS_BUCKET` | GCS bucket name | — |

## Server

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_HOST` | Listen address | `0.0.0.0` |
| `SFS_PORT` | Listen port | `8000` |
| `SFS_LOG_LEVEL` | Log level: `DEBUG`, `INFO`, `WARNING`, `ERROR` | `INFO` |
| `SFS_CORS_ORIGINS` | Allowed CORS origins (comma-separated) | — |
| `SFS_RATE_LIMIT_PER_MINUTE` | API rate limit per API key per minute. Set to `0` to disable | `120` |
| `SFS_JUDGE_BASE_URL` | Custom OpenAI-compatible endpoint for LLM Judge (LiteLLM, vLLM, Ollama, etc.) | — |
| `SFS_DASHBOARD_DIR` | Path to dashboard static files | `./static` |

## Sync Limits

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_MAX_SYNC_BYTES_FREE` | Max sync payload for free tier | `52428800` (50 MB) |
| `SFS_MAX_SYNC_BYTES_PAID` | Max sync payload for paid tier | `314572800` (300 MB) |
| `SFS_RETENTION_DAYS_FREE` | Cloud retention for free tier | `14` |

## GitHub App

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_GITHUB_APP_ID` | GitHub App ID | — |
| `SFS_GITHUB_PRIVATE_KEY` | GitHub App private key (PEM contents) | — |
| `SFS_GITHUB_WEBHOOK_SECRET` | Webhook HMAC-SHA256 secret | — |
