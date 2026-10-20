# Environment Variables

All SessionFS server environment variables use the `SFS_` prefix.

## Database

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_DATABASE_URL` | PostgreSQL connection URL | `sqlite+aiosqlite:///./sessionfs.db` |
| `SFS_DATABASE_ECHO` | Log all SQL queries | `false` |
| `SFS_DATABASE_POOL_SIZE` | Connections kept open in the pool (PostgreSQL only) | `20` |
| `SFS_DATABASE_MAX_OVERFLOW` | Extra connections allowed above the pool size under load (PostgreSQL only) | `40` |
| `SFS_DATABASE_POOL_TIMEOUT` | Seconds a request waits for a free connection before failing (PostgreSQL only) | `60` |
| `SFS_DATABASE_POOL_RECYCLE` | Seconds after which a pooled connection is replaced (PostgreSQL only) | `1800` |

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
| `SFS_VERIFICATION_SECRET` | Secret for email verification JWTs AND Fernet encryption of stored API keys/tokens. **Rotating this invalidates all stored judge keys and GitLab tokens.** Set a random value of at least 32 characters in production; org auto-join by email domain stays off while the default is in use. | `dev-verification-secret` |
| `SFS_REQUIRE_EMAIL_VERIFICATION` | Require email verification on signup | `true` |
| `SFS_SSO_CALLBACK_URL` | The OIDC redirect/callback URL registered with each org's IdP — where the provider returns the authorization code | `https://api.sessionfs.dev/api/v1/auth/sso/callback` |
| `SFS_SSO_REDIRECT_ORIGINS` | Comma-separated allowlist of absolute origins permitted as the post-login `redirect_after` destination (relative paths are always allowed; everything else is rejected) | — |
| `SFS_DASHBOARD_URL` | Dashboard origin the **browser** SSO login redirects back to (the one-time-code handoff lands at `<SFS_DASHBOARD_URL>/sso/callback`). Server-config only — never derived from request input. If unset, browser SSO login returns 500 (the CLI SSO flow is unaffected). | — |
| `SFS_NO_TELEMETRY` | **Client-side.** Set to any non-empty value to disable anonymous usage telemetry entirely (the other opt-out is `[telemetry] enabled = false` in config.toml). See docs/telemetry.md for the full disclosure. | — |

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
| `SFS_KNOWLEDGE_RATE_LIMIT_PER_HOUR` | Per-user, per-hour cap on `add_knowledge` / `POST /entries/add`. Overrides the tier-based defaults (`free=20`, `starter=50`, `pro=100`, `team=100`, `enterprise=200`, `admin=500`) when set. Bucket key is `user_id`, not `session_id`, so MCP `manual` callers don't share buckets across users | — |
| `SFS_SMTP_VERIFY_SSL` | Verify SSL certificates for SMTP. Set to `false` for self-signed certs | `true` |
| `SFS_JUDGE_BASE_URL` | Custom OpenAI-compatible endpoint for LLM Judge (LiteLLM, vLLM, Ollama, etc.) | — |
| `SFS_DASHBOARD_DIR` | Path to dashboard static files | `./static` |
| `SFS_APP_URL` | Dashboard origin used in links the server generates (organization invite emails, return pages after billing) | `https://app.sessionfs.dev` |
| `SFS_API_URL` | Public API origin used in generated share links (and by the remote MCP server to reach the API) | `https://api.sessionfs.dev` |

## Sync Limits

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_MAX_SYNC_BYTES_FREE` | Max sync payload for free tier | `52428800` (50 MB) |
| `SFS_MAX_SYNC_BYTES_PAID` | Max sync payload for paid tiers (Starter/Pro/Team/Enterprise) | `314572800` (300 MB) |
| `SFS_MAX_SYNC_MEMBER_BYTES_FREE` | Per-file cap inside a session archive for free tier. Long AI sessions (8000+ messages) typically have a single `messages.jsonl` file at this scale | `10485760` (10 MB) |
| `SFS_MAX_SYNC_MEMBER_BYTES_PAID` | Per-file cap inside a session archive for paid tiers (Starter/Pro/Team/Enterprise). The CLI also reads this env var to size its local pre-flight check and its unpack-time safety ceiling. **Self-hosted operators raising this cap above the 50 MB default MUST set the same value on every CLI host** — otherwise the local pull/unpack path rejects archives the server already accepted | `52428800` (50 MB) |
| `SFS_RETENTION_DAYS_FREE` | Cloud retention for free tier | `14` |

## GitHub App

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_GITHUB_APP_ID` | GitHub App ID | — |
| `SFS_GITHUB_APP_PRIVATE_KEY` | GitHub App private key (PEM contents). Store it in a secret manager and inject it as an env var | — |
| `SFS_GITHUB_WEBHOOK_SECRET` | Webhook HMAC-SHA256 secret (the App's webhook secret). **Required to receive GitHub webhooks**: when unset, `/webhooks/github` refuses every delivery with 503 and logs the misconfiguration once | — |
| `SFS_GITHUB_WEBHOOK_ALLOW_UNSIGNED` | Local development only. Set to `1` to accept unsigned GitHub webhooks when no secret is configured. Ignored when `SFS_GITHUB_WEBHOOK_SECRET` is set. Never enable in production | unset |
| `SFS_GITHUB_APP_CLIENT_ID` | The GitHub App's **Client ID** (not the App ID). Needed to link an installation to a SessionFS account | — |
| `SFS_GITHUB_APP_CLIENT_SECRET` | The GitHub App's client secret. Inject it from your secret manager (GCP Secret Manager, a Kubernetes Secret); never put it in config files or values | — |
| `SFS_GITHUB_APP_SLUG` | The App's URL slug (`github.com/apps/<slug>`). Optional: enables the one-step "install and link" flow. Without it, users install the App on GitHub and then link it with "Link existing installation" | — |
| `SFS_GITHUB_OAUTH_CALLBACK_URL` | **Required** for linking. This API's public URL + `/api/v1/settings/github/oauth/callback` (any other path disables linking). Sent as `redirect_uri` on every authorization and token exchange; register it as the App's SECOND callback URL, after `/api/v1/settings/github/oauth/install-return` (which is also the Setup URL). The order matters; see [github-app.md](github-app.md) | — |
| `SFS_GITHUB_OAUTH_INSECURE_LOCALHOST` | Local development only. Set to `1` to allow an `http://localhost` (or `127.0.0.1`) dashboard/callback URL; the claim cookie is then sent without `Secure`. Any other `http://` URL disables linking | unset |

`SFS_DASHBOARD_URL` (see Server) is also required and must be `https://` (as must `SFS_GITHUB_OAUTH_CALLBACK_URL` when set): the OAuth callback sends the browser back to `<SFS_DASHBOARD_URL>/settings?github=connected|choose|failed`.

Linking an installation goes through GitHub OAuth. SessionFS accepts the link only when GitHub's `GET /user/installations`, called with the token of the GitHub account that just signed in, lists that installation. If `SFS_GITHUB_APP_CLIENT_ID`, `SFS_GITHUB_APP_CLIENT_SECRET`, `SFS_DASHBOARD_URL` or `SFS_GITHUB_OAUTH_CALLBACK_URL` is unset, linking is **disabled**: there is no fallback to linking by installation id. Installations linked before this check existed are kept for PR comments, but they are not trusted for repo verification until the owner re-verifies them from Settings. Repo verification also requires a proof that the user is the installation's account or an active organization owner, refreshed at least every 7 days; organization owner checks need the App's **Members: Read** organization permission (see [github-app.md](github-app.md)).

## GitLab

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_GITLAB_WEBHOOK_SECRET` | Global GitLab webhook token for MR events. Optional: per-user webhook secrets from Settings also work. A delivery matching neither is refused with 401 | — |
| `SFS_PUBLIC_API_URL` | This deployment's public API origin, e.g. `https://api.sessionfs.dev`. Used as the default OIDC audience for marketplace trusted publishing | — |
| `SFS_TRUSTED_PUBLISHING_AUDIENCE` | Overrides the OIDC audience GitHub Actions workflows must request for marketplace trusted publishing. Must be unique to the deployment: outside development a generic value such as `sessionfs` refuses to start, and with neither this nor `SFS_PUBLIC_API_URL` trusted publishing is disabled | `SFS_PUBLIC_API_URL` |
| `SFS_ENVIRONMENT` | `development`/`test` relaxes the trusted-publishing audience checks (a development-only audience is used). Unset: development when the database is SQLite. Read once at startup together with the audience | — |

## CLI and Daemon Sync

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_UPLOAD_TIMEOUT` | Seconds the CLI and daemon wait on a session upload or download (1–3600). Overrides `upload_timeout` under `[sync]` in `config.toml`; `sfs push --timeout` overrides both. Invalid values are ignored with a warning. | `300` |

## Session Capture

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_CAPTURE_GLOBAL_RULES` | Controls whether global instruction artifacts (rule files in user home directories, global agent files) are hashed into session provenance. Values: `on`, `off`. When `off`, SessionFS skips all artifacts with `scope: global` and never hashes personal conventions from a developer's home directory into shared session records. Project-scope artifacts are captured regardless. | `on` |

## Defaults

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_DEFAULT_SYNC_MODE` | Default autosync mode for new users: `off`, `all`, `selective` | `off` |
| `SFS_DEFAULT_AUDIT_TRIGGER` | Default auto-audit trigger for new users: `manual`, `on_sync`, `on_pr` | `manual` |
