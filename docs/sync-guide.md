# Cloud Sync Guide

Sync your sessions across machines and share them with teammates. Cloud sync is optional — SessionFS works fully offline by default.

## Overview

When sync is enabled, the daemon pushes captured sessions to the SessionFS API server. You can pull sessions on another machine, or teammates with access can pull sessions you've shared. Sync uses HTTP with ETags for conflict detection — no WebSockets, no polling.

## Prerequisites

- SessionFS installed and daemon running (`sfs daemon status`)
- A SessionFS account (self-hosted or cloud)
- An API key

## Step 1: Get an API Key

### Cloud (sessionfs.dev)

Sign up at [sessionfs.dev](https://sessionfs.dev) and generate an API key from your account settings.

### Self-hosted

Create an API key via the API:

```bash
curl -X POST https://your-server/api/v1/auth/keys \
  -H "Content-Type: application/json" \
  -d '{"name": "my-laptop"}'
```

The response includes your API key. Save it — it's only shown once.

## Step 2: Configure Sync

```bash
sfs config set sync.enabled true
sfs config set sync.api_url https://api.sessionfs.dev
sfs config set sync.api_key YOUR_API_KEY
```

Restart the daemon to apply:

```bash
sfs daemon stop
sfs daemon start
```

The daemon will now push new sessions to the server automatically.

## Step 3: Sync Across Machines

On your other machine, install SessionFS and configure the same sync settings:

```bash
pip install sessionfs
sfs config set sync.enabled true
sfs config set sync.api_url https://api.sessionfs.dev
sfs config set sync.api_key YOUR_API_KEY
sfs daemon start
```

Sessions are synced automatically. You can also manually pull:

```bash
# Pull all remote sessions not yet on this machine
sfs sync pull

# Push all local sessions not yet on the server
sfs sync push
```

## Conflict Handling

Sessions are append-only. When the same session is modified on two machines:

1. Both versions are uploaded with different ETags
2. On sync, both sets of messages are appended (not merged)
3. The manifest is updated with the combined stats

This means you never lose data. Duplicate messages may appear in rare cases but the conversation history is always complete.

## Sync-Aware Deletes

Sessions you delete stay deleted — autosync respects your intent. When you run `sfs delete`, the session is added to a local exclusion list (`~/.sessionfs/deleted.json`) and autosync skips it in both push and pull directions. See [Delete Lifecycle](delete-lifecycle.md) for the full model.

## Selective Sync

By default, all sessions are synced. To sync only specific sessions:

```bash
# Sync a single session
sfs sync push <session_id>
sfs sync pull <session_id>
```

## Self-Hosted Server {#self-hosted}

Run your own SessionFS API server with Docker Compose:

```bash
git clone https://github.com/SessionFS/sessionfs
cd sessionfs
docker compose up -d
```

This starts:
- **API server** on port 8000
- **PostgreSQL** on port 5432

### Configuration

Server configuration is via environment variables in `docker-compose.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `SFS_DATABASE_URL` | — | PostgreSQL connection string |
| `SFS_BLOB_STORE_TYPE` | `local` | Storage backend: `local` or `s3` |
| `SFS_BLOB_STORE_LOCAL_PATH` | `/data/blobs` | Path for local blob storage |
| `SFS_S3_BUCKET` | — | S3 bucket name (when using S3) |
| `SFS_S3_REGION` | — | AWS region |
| `SFS_S3_ENDPOINT_URL` | — | Custom S3 endpoint (for MinIO) |
| `SFS_CORS_ORIGINS` | `*` | Allowed CORS origins |
| `SFS_RATE_LIMIT_PER_MINUTE` | `60` | API rate limit per key |

### S3 Storage

For production, use S3-compatible storage instead of local filesystem:

```yaml
# docker-compose.yml
environment:
  SFS_BLOB_STORE_TYPE: s3
  SFS_S3_BUCKET: my-sessionfs-bucket
  SFS_S3_REGION: us-east-1
```

For MinIO (self-hosted S3):

```yaml
services:
  minio:
    image: minio/minio
    command: server /data
    ports:
      - "9000:9000"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin

  api:
    environment:
      SFS_BLOB_STORE_TYPE: s3
      SFS_S3_BUCKET: sessionfs
      SFS_S3_ENDPOINT_URL: http://minio:9000
      AWS_ACCESS_KEY_ID: minioadmin
      AWS_SECRET_ACCESS_KEY: minioadmin
```

### Database Migrations

Run migrations after updating the server:

```bash
docker compose exec api alembic upgrade head
```

### Health Check

```bash
curl http://localhost:8000/health
```

Expected response:

```json
{"status": "ok"}
```

## Security Notes

- API keys are hashed (SHA-256) before storage — the server never stores plaintext keys
- All sync traffic should use HTTPS in production
- Sessions may contain sensitive data (code, file contents, API keys in conversation) — treat your sync server with the same security as your source code
- See the [Security Spec](security/security-spec.md) for full details
