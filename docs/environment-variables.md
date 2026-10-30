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
| `SFS_DASHBOARD_URL` | Dashboard origin the **browser** SSO login redirects back to (the one-time-code handoff lands at `<SFS_DASHBOARD_URL>/sso/callback`). Server-config only — never derived from request input. If unset, browser SSO login returns 500 (the CLI SSO flow is unaffected). Also the origin for links in the weekly org activity digest email (falls back to an explicitly set `SFS_APP_URL`; links are left out when neither is set). | — |
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
| `SFS_LOG_LEVEL` | Log level for the API's own logs: `DEBUG`, `INFO`, `WARNING`, `ERROR`. Applied when the process has no other logging configuration (plain `uvicorn`); `INFO` includes one timing line per session push | `INFO` |
| `SFS_CORS_ORIGINS` | Allowed CORS origins (comma-separated) | — |
| `SFS_RATE_LIMIT_PER_MINUTE` | API rate limit per API key per minute. Set to `0` to disable | `120` |
| `SFS_KNOWLEDGE_RATE_LIMIT_PER_HOUR` | Per-user, per-hour cap on `add_knowledge` / `POST /entries/add`. Overrides the tier-based defaults (`free=20`, `starter=50`, `pro=100`, `team=100`, `enterprise=200`, `admin=500`) when set. Bucket key is `user_id`, not `session_id`, so MCP `manual` callers don't share buckets across users | — |
| `SFS_SMTP_VERIFY_SSL` | Verify SSL certificates for SMTP. Set to `false` for self-signed certs | `true` |
| `SFS_JUDGE_BASE_URL` | Custom OpenAI-compatible endpoint for LLM Judge (LiteLLM, vLLM, Ollama, etc.). Operator configuration: this exact URL may be on a private host even when `SFS_LLM_ALLOW_PRIVATE_HOSTS` is false (link-local and cloud-metadata addresses are still refused) | — |
| `SFS_LLM_ALLOW_PRIVATE_HOSTS` | Let users point their own LLM endpoint (judge settings `base_url`, and `base_url` on audit, narrative-summary and knowledge compile requests) at private, loopback or plain-http hosts, e.g. an in-cluster Ollama or vLLM. When false the URL must be https and resolve to public addresses only. The URL is checked when it is saved or submitted and again before every outbound request; redirects are never followed. Link-local, unspecified and cloud-metadata addresses are blocked in both modes. Helm: `api.llm.allowPrivateHosts` (default `true` in the chart, since self-hosted model gateways usually live on the private network) | `false` |
| `SFS_DASHBOARD_DIR` | Path to dashboard static files | `./static` |
| `SFS_APP_URL` | Dashboard origin used in links the server generates (organization invite emails, return pages after billing; weekly digest links only when `SFS_DASHBOARD_URL` is unset and this is set explicitly) | `https://app.sessionfs.dev` |
| `SFS_API_URL` | Public API origin used in generated share links (and by the remote MCP server to reach the API) | `https://api.sessionfs.dev` |

## Sync Limits

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_MAX_SYNC_BYTES_FREE` | Max sync payload for free tier | `52428800` (50 MB) |
| `SFS_MAX_SYNC_BYTES_PAID` | Max sync payload for paid tiers (Starter/Pro/Team/Enterprise) | `314572800` (300 MB) |
| `SFS_MAX_SYNC_MEMBER_BYTES_FREE` | Per-file cap inside a session archive for free tier. Long AI sessions (8000+ messages) typically have a single `messages.jsonl` file at this scale | `10485760` (10 MB) |
| `SFS_MAX_SYNC_MEMBER_BYTES_PAID` | Per-file cap inside a session archive for paid tiers (Starter/Pro/Team/Enterprise). The CLI also reads this env var to size its local pre-flight check and its unpack-time safety ceiling. **Self-hosted operators raising this cap above the 50 MB default MUST set the same value on every CLI host** — otherwise the local pull/unpack path rejects archives the server already accepted | `52428800` (50 MB) |
| `SFS_RETENTION_DAYS_FREE` | Cloud retention for free tier | `14` |

## GitLab integration

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_GITLAB_ALLOW_PRIVATE_HOSTS` | Let users point the GitLab integration (`PUT /api/v1/settings/gitlab`) at private, loopback or plain-http hosts, for an internal self-hosted GitLab. When false the instance URL must be https and resolve to public addresses only; the host is re-checked before every outbound request. Link-local and cloud-metadata addresses are blocked in both modes. The Helm chart sets this to true (`api.gitlab.allowPrivateHosts`) | `false` |

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
| `SFS_PUBLIC_API_URL` | This deployment's public API origin, e.g. `https://api.sessionfs.dev`. Used as the default OIDC audience for marketplace trusted publishing, and as the base of native catalog clone URLs (`/api/v1/git/...`; the request's own origin when unset) | — |
| `SFS_PUBLIC_CATALOG_MIRROR` | Public GitHub mirror of the public native catalog, as `owner/repo` (e.g. `SessionFS/marketplace`). When set, the public catalog's add commands (`GET /api/v1/marketplace/catalogs`, `sfs catalog add`, the dashboard) lead with the keyless GitHub form for Claude Code, Copilot CLI, Codex and Cursor, keep the SessionFS-hosted URL as `add_hosted`, and the public catalog's README names the mirror. Gemini CLI keeps installing from this deployment. Set it only when a scheduled `sfs catalog export` keeps that repository in sync. Anything but `owner/repo` refuses startup | — |
| `SFS_WORKS_IN_ORG_DAILY_QUOTA` | Works-in harness runs an organization may request per 24 hours (`POST /api/v1/orgs/{org_id}/skills/verify`). Platform sweeps of the org's listings do not count | `25` |
| `SFS_NATIVE_CATALOG_MAX_FILES` | File budget of one native catalog build (at most 19968, the repository cap less room for root files). Plugins are admitted in namespace, name, version-id order; one that does not fit is excluded and listed with its reason in `SESSIONFS-CATALOG.json` | `19968` |
| `SFS_NATIVE_CATALOG_MAX_PLUGIN_BYTES` | Largest single plugin a native catalog admits (at most 64 MiB, and never more than the catalog byte budget), so one package cannot crowd the others out. Larger packages are excluded as `plugin_too_large`; excluded packages are not charged to the budget | `16777216` |
| `SFS_NATIVE_CATALOG_MAX_BYTES` | Byte budget of one native catalog build (plugin files, generated manifests included). Bounds the memory a rebuild needs; packages past it are not even fetched, and are excluded and listed like the file budget | `268435456` |
| `SFS_REGISTRY_POLLING_ENABLED` | Server-side polling of organization Git marketplace registries (GitHub #69). `false` makes `POST /api/v1/admin/skills/registries/poll` answer `503` and every registry report `server_fetch_enabled: false`. Polling only runs when that endpoint is called (scheduler, `sfs admin registries poll`, the Helm CronJob) | `true` |
| `SFS_REGISTRY_POLL_ALLOWED_PRIVATE_CIDRS` | Comma-separated private ranges the registry poller may connect to, e.g. `10.20.0.0/16` for an internal GitLab. Unset: only globally routable addresses. Loopback, link-local and cloud metadata addresses are never reachable; a malformed value (or one naming loopback/link-local) disables the opt-in entirely | — |
| `SFS_REGISTRY_POLL_TIMEOUT_SECONDS` | Fetch budget of one registry poll (30-3600). Each git command also has its own 60 s timeout | `300` |
| `SFS_REGISTRY_POLL_RUN_BUDGET_SECONDS` | Budget of one poll run (120-3000). A run claims no new registry once elapsed + fetch budget + 300 s publish slack would exceed it, so with the defaults it starts registries during its first 600 s and finishes by about 1200 s. Scheduler / CronJob timeouts must exceed budget + 120 s (the Helm CronJob uses + 120 s for curl and + 180 s for the job) | `1200` |
| `SFS_REGISTRY_POLL_MAX_PER_ORG` | Polling-enabled registries an organization may have; more are refused with `409 poll_registry_limit` | `10` |
| `SFS_REGISTRY_POLL_MAX_PER_ORG_PER_RUN` | Registries of one organization polled per run (due registries are taken round-robin across organizations) | `2` |
| `SFS_REGISTRY_CRED_<NAME>` | A registry credential (PAT or deploy token) an org references as `credential_secret_ref: env:SFS_REGISTRY_CRED_<NAME>`. Only names with this prefix can be referenced, so no other server variable can ever be sent to a registry | — |
| `SFS_REGISTRY_CRED_<NAME>_ORG` | **Required** with the credential: the one organization id allowed to use it. A credential without it, or referenced by another org, is refused | — |
| `SFS_REGISTRY_CRED_<NAME>_HOSTS` | **Required**: comma-separated hosts the credential may be sent to. A credential without a host binding is never used | — |
| `SFS_REGISTRY_CRED_<NAME>_USERNAME` | Optional HTTP Basic username (GitLab deploy tokens need theirs) | `x-access-token` |
| `SFS_REGISTRY_SECRETS_DIR` | Directory of mounted credential Secrets for `k8s:<name>` references: `<dir>/<name>/token`, `org` and `hosts` (both required), optional `username` | `/var/run/secrets/sessionfs/registry-credentials` |
| `SFS_GITLAB_OIDC_ISSUERS` | GitLab instances whose CI ID tokens may be exchanged for marketplace publish tokens: comma-separated `https://` origins as GitLab puts them in `iss` (a relative URL root such as `https://example.com/gitlab` is allowed). Listed values REPLACE the default, so include `https://gitlab.com` to keep it; `none` disables GitLab trusted publishing. Each entry must equal the token's `iss` exactly (GitLab derives it from `external_url`: lowercase host, no `:443`, no trailing slash); a near miss is refused with a hint naming the listed form. Removing an entry also stops publish tokens already issued under that instance's trusts. The JWKS is fetched only for a listed instance, from `<origin>/oauth/discovery/keys`, through the SSRF-guarded fetch (public IPs only, no redirects), so the instance must be reachable on a public address. A trust for an unlisted instance is refused at creation, and a token from one is refused before any fetch. An invalid entry refuses startup | `https://gitlab.com` |
| `SFS_TRUSTED_PUBLISHING_AUDIENCE` | Overrides the OIDC audience GitHub Actions workflows (and GitLab CI `id_tokens: aud:`) must request for marketplace trusted publishing. Must be unique to the deployment: outside development a generic value such as `sessionfs` refuses to start, and with neither this nor `SFS_PUBLIC_API_URL` trusted publishing is disabled | `SFS_PUBLIC_API_URL` |
| `SFS_ENVIRONMENT` | `development`/`test` relaxes the trusted-publishing audience checks (a development-only audience is used). Unset: development when the database is SQLite. Read once at startup together with the audience | — |

## CLI and Daemon Sync

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_UPLOAD_TIMEOUT` | Seconds the CLI and daemon wait on a session upload or download (1–3600). Overrides `upload_timeout` under `[sync]` in `config.toml`; `sfs push --timeout` overrides both. Invalid values are ignored with a warning. | `300` |
| `SFS_WORKS_IN_HARNESS` | Must be `1` for `sfs catalog harness run` to start. The harness runs real tool installers against untrusted packages; set it only inside the sandboxed harness job | — |
| `SFS_HARNESS_API_URL` | API origin the harness talks to. The harness never reads the SessionFS profile | — |
| `SFS_HARNESS_API_KEY` | The harness credential: a service key with only the `skills_verify:run` scope, minted by a platform admin. Removed from the process environment before any tool runs | — |
| `SFS_HARNESS_TOOL_USER` | Unprivileged user tool commands run as. The harness must run as root (it drops to this user for every tool command, with a HOME and tmp owned by it); the harness image must create the user | `sfs-tool` |
| `SFS_HARNESS_ALLOW_SAME_UID` | `1` lets the harness run tool commands as its own uid when not root. Local development only: a hostile installer could then read the harness's memory | — |
| `SFS_HARNESS_IMAGE_DIGEST` | Default for `sfs catalog harness run --image-digest`: the harness container image digest recorded with each works-in result | — |
| `SFS_CATALOG_EXPORT_SSH_KEY_FILE` | Default for `sfs catalog export --ssh-key-file`: a private SSH deploy key used for the push only (copied to a private 0600 temporary file; no ssh-agent, no other keys, strict host-key checking). Needs an SSH destination remote | — |
| `SFS_CATALOG_EXPORT_KNOWN_HOSTS` | Default for `sfs catalog export --known-hosts`: the known_hosts file the push trusts. Unset: GitHub's published host keys, shipped with SessionFS | bundled GitHub keys |
| `SESSIONFS_DEVICE_ID` | This device's id for device-targeted skill assignments and for skills-policy compliance reports (`sfs skill pull`, the daemon). When unset, compliance reports use a random id generated once and kept in `~/.sessionfs/skills/policy_device_id`. | unset |
| `SFS_SKILL_USAGE_REPORTING` | **Client-side.** Set to `0`, `false`, `off` or `no` to stop `sfs skill add/remove/pull` and the daemon's skill reconcile from reporting hosted-marketplace install counts (installs, uninstalls, translation failures, active-install pings). Counts are also off whenever telemetry is off (`SFS_NO_TELEMETRY` or `[telemetry] enabled = false`). The server keeps daily aggregates only. Counts are sent only after the one-time install-count notice has been shown by an interactive `sfs skill` command; `1`, `true`, `on` or `yes` enables them without it (for example on CI or for the daemon). | on, after the notice |

## Session Capture

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_CAPTURE_GLOBAL_RULES` | Controls whether global instruction artifacts (rule files in user home directories, global agent files) are hashed into session provenance. Values: `on`, `off`. When `off`, SessionFS skips all artifacts with `scope: global` and never hashes personal conventions from a developer's home directory into shared session records. Project-scope artifacts are captured regardless. | `on` |
| `SFS_AGENT_RUN_ID` | **Client-side.** Gives a parallel worker its own active ticket: the bundle is kept at `~/.sessionfs/agents/<id>/active_ticket.json` (plus a per-worktree copy under `~/.sessionfs/workspaces/` for the daemon), so workers in different worktrees never overwrite each other. Letters, digits, `.`, `_` and `-`, up to 128 characters. | unset |
| `SFS_ACTIVE_TICKET_PATH` | **Client-side.** Explicit path for the active-ticket bundle; takes precedence over `SFS_AGENT_RUN_ID`. | unset |

## Defaults

| Variable | Description | Default |
|----------|-------------|---------|
| `SFS_DEFAULT_SYNC_MODE` | Default autosync mode for new users: `off`, `all`, `selective` | `off` |
| `SFS_DEFAULT_AUDIT_TRIGGER` | Default auto-audit trigger for new users: `manual`, `on_sync`, `on_pr` | `manual` |
