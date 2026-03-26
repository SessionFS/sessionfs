# Troubleshooting

## Common Error Responses

| Status | Error | Cause | Fix |
|--------|-------|-------|-----|
| 400 | Invalid session ID format | Session ID doesn't match `ses_[hex]{8,40}` | Check ID format — must be `ses_` + 8-40 lowercase hex chars |
| 400 | Payload too large | Sync payload exceeds server limit | Compact session first or check `MAX_UPLOAD_SIZE` |
| 401 | Unauthorized | Missing or expired auth token | Run `sfs auth login` |
| 403 | Email not verified | Account needs verification | Check email for verification link, or set `SFS_REQUIRE_EMAIL_VERIFICATION=false` |
| 409 | ETag mismatch | Concurrent modification | Pull latest with `sfs pull`, then push again |
| 413 | Request entity too large | Upload exceeds server limit | Check `MAX_UPLOAD_SIZE` setting |
| 429 | Rate limited | Too many requests per minute | Wait, or increase `SFS_RATE_LIMIT_PER_MINUTE`. Set to `0` to disable |

## Session ID Format

SessionFS generates session IDs in the format: `ses_` followed by 8-40 lowercase hex characters.

Examples:
- `ses_ae7652a4` (8 chars — short form)
- `ses_346b4d7288214b0f` (16 chars — standard)
- `ses_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2` (40 chars — long form)

All formats are valid for sync, push, pull, and handoff operations.

## Troubleshooting Sync Failures

1. **Check daemon status:**
   ```bash
   sfs daemon status
   sfs daemon logs
   ```

2. **Try a manual push** (shows detailed errors):
   ```bash
   sfs push ses_abc12345
   ```

3. **Check server logs:**
   ```bash
   kubectl logs deploy/sessionfs-api -n sessionfs --tail=100
   ```

4. **Verify authentication:**
   ```bash
   sfs auth status
   ```

5. **Check local storage:**
   ```bash
   sfs storage
   ```

## Kubernetes Deployment Issues

### MCP pods crash-looping

Check that the MCP service port matches the container port (should be 8080):

```bash
kubectl describe deploy sessionfs-mcp -n sessionfs
```

Verify liveness/readiness probes target the correct port.

### Dashboard returns 405 or shows malformed URLs

The dashboard must proxy API requests through nginx, not call an external API URL directly. In the Helm chart, the dashboard nginx ConfigMap handles `/api/` proxying automatically. If you see URLs like `https://your-domain/https://api.sessionfs.dev`, the dashboard image was built with a hardcoded API URL. Rebuild with:

```bash
docker build --build-arg VITE_API_URL=/api -t sessionfs-dashboard .
```

### S3 ParamValidationError: Invalid bucket name

S3 bucket names cannot contain `/`. If you need a key prefix:

```yaml
storage:
  s3:
    bucket: "my-bucket"        # Bucket name only, no slashes
    prefix: "sessionfs/"       # Optional key prefix
```

The code also handles `bucket: "my-bucket/prefix"` gracefully by splitting on the first `/`.

### asyncpg SSL errors

Do not add `?sslmode=require` to the database URL. SessionFS handles SSL parameter translation internally. For RDS and Cloud SQL, asyncpg negotiates SSL automatically for non-localhost connections.

```yaml
# Correct — no sslmode
externalDatabase:
  host: mydb.cluster-abc123.us-east-1.rds.amazonaws.com
  existingSecret: sessionfs-db

# Wrong — do not add sslMode
externalDatabase:
  host: mydb.cluster-abc123.us-east-1.rds.amazonaws.com
  sslMode: require  # This will cause errors
```

### Rate limiting returns 429 unexpectedly

Rate limiting is per API key, not per IP. Check your configured limit:

```yaml
api:
  rateLimitPerMinute: 120    # Default: 120 requests/min per API key
  # Set to 0 to disable rate limiting entirely
```

Or via environment variable:

```bash
SFS_RATE_LIMIT_PER_MINUTE=0     # Disable
SFS_RATE_LIMIT_PER_MINUTE=10000 # Effectively unlimited
```

Changes require a pod restart to take effect.
