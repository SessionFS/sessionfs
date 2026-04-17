# Delete Lifecycle

How session deletion works in SessionFS: scopes, retention, recovery, and sync awareness.

## Three-Scope Delete Model

Every delete requires an explicit scope. There is no default.

| Scope | Server record | Local `.sfs` dir | Local index | Sync behavior | Storage quota |
|-------|--------------|-------------------|-------------|---------------|---------------|
| `--cloud` | Soft-deleted (30-day retention) | Kept | Kept | Skipped on push | Excluded |
| `--local` | Untouched | Removed | Removed | Skipped on pull | N/A (local only) |
| `--everywhere` | Soft-deleted (30-day retention) | Removed | Removed | Skipped on push and pull | Excluded |

## What Each Scope Does

### Cloud delete

```bash
sfs delete ses_abc123 --cloud
```

Removes the session from the server. Your local copy stays. The server marks the session as soft-deleted with a 30-day retention window. Autosync will not re-push this session.

### Local delete

```bash
sfs delete ses_abc123 --local
```

Removes the `.sfs` directory from `~/.sessionfs/sessions/` and clears it from the local SQLite index. No server call is made. Autosync will not re-pull this session.

### Delete everywhere

```bash
sfs delete ses_abc123 --everywhere
```

Combines both: removes the local copy and soft-deletes the server record. Autosync skips the session in both directions.

## Retention and Purge

Soft-deleted sessions are retained for **30 days**. During this window:

- The session does not appear in `sfs list` or `sfs list-remote`
- The session does not count against your storage quota
- The session can be restored with `sfs restore`

After 30 days, an admin can purge expired sessions via `POST /api/v1/admin/purge-deleted`. Purging hard-deletes the database row and removes the blob from storage. This is irreversible.

Automatic purge (via cron) is not yet implemented. For now, trigger purge manually or with a simple scheduled curl.

## Recovery

### Restore a cloud-deleted session

```bash
sfs restore ses_abc123
```

Clears the soft-delete flag on the server and removes the session from the local exclusion list. The session reappears in `sfs list-remote` and sync resumes normally.

### Re-download a locally-deleted session

```bash
sfs pull ses_abc123
```

If you deleted a session locally but it still exists in the cloud, `sfs pull <id>` re-downloads it. The explicit pull overrides the exclusion entry and removes it from `deleted.json`.

### Restore after delete-everywhere

Run `sfs restore` first (reverses the server soft-delete), then `sfs pull` to re-download the local copy.

## Sync Awareness

Before v0.9.9, deleting a session from the dashboard would be silently reversed by autosync re-pushing the local copy. This is fixed.

### How it works

SessionFS maintains an exclusion file at `~/.sessionfs/deleted.json`. Each entry records the session ID, deletion timestamp, and scope. The daemon checks this file before every sync cycle:

- **Push direction:** sessions in `deleted.json` are skipped
- **Pull direction:** sessions in `deleted.json` are skipped
- **Server-side:** soft-deleted sessions are already filtered from the remote session list

### Explicit commands override exclusions

`sfs push <id>` and `sfs pull <id>` with an explicit session ID will warn if the session is in `deleted.json`, but still allow the operation. The user typed the ID deliberately.

If you explicitly push a cloud-deleted session, SessionFS warns and asks for confirmation before un-deleting on the server.

## The `deleted.json` Exclusion File

**Location:** `~/.sessionfs/deleted.json`

**Format:**

```json
{
  "ses_abc123": {
    "deleted_at": "2026-04-16T12:00:00Z",
    "scope": "cloud"
  },
  "ses_def456": {
    "deleted_at": "2026-04-16T13:00:00Z",
    "scope": "everywhere"
  }
}
```

**Behavior:**

- Autosync reads this file before push and pull — matching sessions are skipped
- `sfs pull <id>` with an explicit ID overrides the exclusion and removes the entry on success
- `sfs restore <id>` removes the entry
- You should not need to edit this file manually

## Edge Cases

**Handoffs:** Deleting a source session does not affect the recipient's copy. The handoff record is preserved for audit. If someone views the source session context from a handoff, they see "Source session has been deleted."

**Knowledge entries:** Knowledge entries produced by a deleted session survive. Knowledge has its own lifecycle and outlives the session that created it.

**Share links:** When a shared session is deleted, the share link returns `410 Gone`. The share record is preserved for audit but the session content is inaccessible.
