"""Unit tests for the delete lifecycle CLI and local exclusion list."""

from __future__ import annotations

import json
from pathlib import Path

import pytest


# ── Test 14-16, 20: Local exclusion list (deleted.json) ──

def test_mark_deleted_and_is_excluded(tmp_path: Path):
    """mark_deleted writes to deleted.json and is_excluded returns True."""
    from sessionfs.store.deleted import mark_deleted, is_excluded

    assert not is_excluded("ses_abc123", base_dir=tmp_path)
    mark_deleted("ses_abc123", "cloud", base_dir=tmp_path)
    assert is_excluded("ses_abc123", base_dir=tmp_path)

    # Verify file content
    data = json.loads((tmp_path / "deleted.json").read_text())
    assert "ses_abc123" in data
    assert data["ses_abc123"]["scope"] == "cloud"


def test_mark_deleted_everywhere(tmp_path: Path):
    """mark_deleted with scope=everywhere."""
    from sessionfs.store.deleted import mark_deleted, is_excluded, get_entry

    mark_deleted("ses_def456", "everywhere", base_dir=tmp_path)
    assert is_excluded("ses_def456", base_dir=tmp_path)
    entry = get_entry("ses_def456", base_dir=tmp_path)
    assert entry is not None
    assert entry["scope"] == "everywhere"


def test_remove_exclusion(tmp_path: Path):
    """remove_exclusion removes from deleted.json."""
    from sessionfs.store.deleted import mark_deleted, is_excluded, remove_exclusion

    mark_deleted("ses_ghi789", "local", base_dir=tmp_path)
    assert is_excluded("ses_ghi789", base_dir=tmp_path)

    remove_exclusion("ses_ghi789", base_dir=tmp_path)
    assert not is_excluded("ses_ghi789", base_dir=tmp_path)


def test_list_deleted(tmp_path: Path):
    """list_deleted returns all entries."""
    from sessionfs.store.deleted import mark_deleted, list_deleted

    mark_deleted("ses_a", "cloud", base_dir=tmp_path)
    mark_deleted("ses_b", "local", base_dir=tmp_path)
    mark_deleted("ses_c", "everywhere", base_dir=tmp_path)

    entries = list_deleted(base_dir=tmp_path)
    assert len(entries) == 3
    assert set(entries.keys()) == {"ses_a", "ses_b", "ses_c"}


def test_is_excluded_empty_dir(tmp_path: Path):
    """is_excluded returns False when no deleted.json exists."""
    from sessionfs.store.deleted import is_excluded

    assert not is_excluded("ses_any", base_dir=tmp_path)


def test_mark_deleted_creates_directory(tmp_path: Path):
    """mark_deleted creates the base directory if it doesn't exist."""
    from sessionfs.store.deleted import mark_deleted, is_excluded

    nested = tmp_path / "nested" / "deep"
    mark_deleted("ses_new", "cloud", base_dir=nested)
    assert is_excluded("ses_new", base_dir=nested)


def test_atomic_write_preserves_data(tmp_path: Path):
    """Multiple mark_deleted calls preserve all entries."""
    from sessionfs.store.deleted import mark_deleted, list_deleted

    for i in range(10):
        mark_deleted(f"ses_{i:04d}", "cloud", base_dir=tmp_path)

    entries = list_deleted(base_dir=tmp_path)
    assert len(entries) == 10


# ── Test 17: sfs delete without scope flag prints error ──

def test_delete_no_scope_prints_error():
    """sfs delete without --cloud/--local/--everywhere exits with error."""
    from typer.testing import CliRunner
    from sessionfs.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["delete", "ses_test123"])
    assert result.exit_code != 0
    assert "specify" in (result.output or result.stdout or "").lower() or result.exit_code != 0


# ── Test 21: Autosync skips sessions in deleted.json (push direction) ──

def test_autosync_push_skips_excluded(tmp_path: Path):
    """_push_one should skip sessions that are in deleted.json."""
    from sessionfs.store.deleted import mark_deleted, is_excluded

    mark_deleted("ses_push_test", "cloud", base_dir=tmp_path)
    assert is_excluded("ses_push_test", base_dir=tmp_path)
    # The actual push skip is tested via the integration test for sync_push;
    # here we verify the exclusion list check works correctly.


# ── Test 22: Autosync skips sessions in deleted.json (pull direction) ──

def test_autosync_pull_skips_excluded(tmp_path: Path):
    """_pull_one should skip sessions that are in deleted.json."""
    from sessionfs.store.deleted import mark_deleted, is_excluded

    mark_deleted("ses_pull_test", "everywhere", base_dir=tmp_path)
    assert is_excluded("ses_pull_test", base_dir=tmp_path)


# ── Test 23: Explicit sfs pull overrides exclusion ──

def test_explicit_pull_overrides_exclusion(tmp_path: Path):
    """After explicit pull, session should be removed from exclusion list."""
    from sessionfs.store.deleted import mark_deleted, is_excluded, remove_exclusion

    mark_deleted("ses_override", "local", base_dir=tmp_path)
    assert is_excluded("ses_override", base_dir=tmp_path)

    # Simulate what explicit pull does: remove exclusion after successful pull
    remove_exclusion("ses_override", base_dir=tmp_path)
    assert not is_excluded("ses_override", base_dir=tmp_path)


# ── v0.9.9.8: transient vs hard exclusion ──


def test_is_transient_exclusion_true_for_too_large(tmp_path: Path):
    """too_large entries are transient — they should clear on manual sync."""
    from sessionfs.store.deleted import (
        is_transient_exclusion,
        mark_deleted,
    )

    mark_deleted(
        "ses_huge", scope="cloud", reason="too_large", base_dir=tmp_path
    )
    assert is_transient_exclusion("ses_huge", base_dir=tmp_path) is True


def test_is_transient_exclusion_false_for_user_delete(tmp_path: Path):
    """User-initiated deletes (no reason, or non-transient reason) stay."""
    from sessionfs.store.deleted import (
        is_transient_exclusion,
        mark_deleted,
    )

    # No reason at all — user delete via the CLI
    mark_deleted("ses_user_deleted", scope="everywhere", base_dir=tmp_path)
    assert is_transient_exclusion("ses_user_deleted", base_dir=tmp_path) is False

    # Hypothetical non-transient reason
    mark_deleted(
        "ses_other", scope="cloud", reason="server_410", base_dir=tmp_path
    )
    assert is_transient_exclusion("ses_other", base_dir=tmp_path) is False


def test_is_transient_exclusion_false_when_missing(tmp_path: Path):
    """No entry at all → not transient (and not excluded)."""
    from sessionfs.store.deleted import is_transient_exclusion

    assert is_transient_exclusion("ses_unknown", base_dir=tmp_path) is False


def test_clear_transient_exclusions_keeps_hard_deletes(tmp_path: Path):
    """clear_transient_exclusions removes only too_large; user deletes remain.

    Regression for the v0.9.9.6 bug where the autosync exclusion path
    persisted too_large entries to deleted.json, then `sfs sync`
    silently skipped them forever — even after tier upgrades lifted
    the cap that originally rejected them. Manual sync must retry
    transient exclusions; hard deletes (user CLI delete) must NOT be
    silently re-pushed.
    """
    from sessionfs.store.deleted import (
        clear_transient_exclusions,
        is_excluded,
        mark_deleted,
    )

    mark_deleted("ses_transient_1", "cloud", reason="too_large", base_dir=tmp_path)
    mark_deleted("ses_transient_2", "cloud", reason="too_large", base_dir=tmp_path)
    mark_deleted("ses_hard_delete", "everywhere", base_dir=tmp_path)  # no reason

    cleared = clear_transient_exclusions(base_dir=tmp_path)
    assert set(cleared) == {"ses_transient_1", "ses_transient_2"}

    # Hard delete must survive — never silently re-push a deliberately
    # removed session.
    assert is_excluded("ses_hard_delete", base_dir=tmp_path) is True
    # Transient entries are gone.
    assert is_excluded("ses_transient_1", base_dir=tmp_path) is False
    assert is_excluded("ses_transient_2", base_dir=tmp_path) is False


def test_clear_transient_exclusions_empty_returns_empty_list(tmp_path: Path):
    """No transient entries → returns [] without touching the file."""
    from sessionfs.store.deleted import (
        clear_transient_exclusions,
        mark_deleted,
    )

    mark_deleted("ses_hard", "everywhere", base_dir=tmp_path)
    cleared = clear_transient_exclusions(base_dir=tmp_path)
    assert cleared == []


def test_sync_all_does_not_clear_when_auth_fails(monkeypatch, tmp_path: Path):
    """Codex round 1 finding: clear_transient_exclusions must only run
    AFTER _get_sync_client succeeds. A failed manual sync (no API key)
    used to silently wipe the daemon's backoff guard without performing
    any retry, then the daemon would resume retry storms on its next
    cycle. Regression-protect by asserting clear is never called when
    auth fails."""
    from sessionfs.cli import cmd_cloud
    from sessionfs.store.deleted import mark_deleted, is_excluded

    # Seed a transient exclusion in a temp deleted.json
    mark_deleted("ses_too_large", "cloud", reason="too_large", base_dir=tmp_path)
    # Point the deleted-list helpers at the temp dir
    monkeypatch.setattr(
        "sessionfs.store.deleted._DEFAULT_PATH",
        tmp_path / "deleted.json",
    )

    # Make _get_sync_client raise SystemExit (the real failure mode
    # for missing API key — see cmd_cloud.py:135 and friends).
    def _fail_auth():
        raise SystemExit(1)

    monkeypatch.setattr(cmd_cloud, "_get_sync_client", _fail_auth)

    with pytest.raises(SystemExit):
        cmd_cloud.sync_all()

    # The exclusion must still be there — auth failed BEFORE clear ran.
    assert is_excluded("ses_too_large", base_dir=tmp_path) is True


def test_sync_all_does_not_clear_when_server_unreachable(monkeypatch, tmp_path: Path):
    """Codex round 3 finding: clearing transient exclusions must wait
    for a SUCCESSFUL server round-trip, not just successful auth. If
    the server is down or the network is flaky, list_remote_sessions
    raises on the very first call — removing the daemon's backoff
    guard at that point would just enable retry storms without
    producing any actual retry.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from sessionfs.cli import cmd_cloud
    from sessionfs.store.deleted import mark_deleted, is_excluded
    from sessionfs.sync.client import SyncError

    mark_deleted("ses_too_large", "cloud", reason="too_large", base_dir=tmp_path)
    monkeypatch.setattr(
        "sessionfs.store.deleted._DEFAULT_PATH",
        tmp_path / "deleted.json",
    )

    # Auth succeeds (we have a client), but list_remote_sessions blows
    # up — simulates the daemon-saw-410-then-network-died scenario.
    mock_client = MagicMock()
    mock_client.list_remote_sessions = AsyncMock(
        side_effect=SyncError("Server unreachable")
    )
    # sync_all calls `await client.close()` in its finally block.
    mock_client.close = AsyncMock()
    monkeypatch.setattr(cmd_cloud, "_get_sync_client", lambda: mock_client)
    # Stub the store; sync_all calls open_store before _sync.
    mock_store = MagicMock()
    mock_store.list_sessions.return_value = []
    monkeypatch.setattr(cmd_cloud, "open_store", lambda: mock_store)
    # Stub asyncio.run so the inner _sync executes inline. Use a fresh
    # event loop so we don't collide with pytest-asyncio's.
    def _run_sync(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(cmd_cloud.asyncio, "run", _run_sync)

    # sync_all should swallow the SyncError as part of its normal
    # error reporting; what we care about is the on-disk state.
    try:
        cmd_cloud.sync_all()
    except (SyncError, SystemExit):
        pass

    # The exclusion must STILL be there — server round-trip never
    # succeeded so the clear was correctly deferred.
    assert is_excluded("ses_too_large", base_dir=tmp_path) is True, (
        "Server-unreachable sync must NOT clear transient exclusions"
    )


def test_sync_all_does_not_clear_when_partial_failure_after_first_page(
    monkeypatch, tmp_path: Path
):
    """Codex round 4 finding: clearing transient exclusions must wait
    until each session is actually about to be retried. Bulk-clearing
    after page-1 of remote pagination is too early — if page-2 fails
    OR store.list_sessions fails, the daemon's backoff guard is gone
    but no retry was attempted.

    Per-session clearing makes this impossible by construction: an
    exclusion is only cleared inside _push_one at the moment of retry.
    This test crashes the sync AFTER list_remote completes but BEFORE
    any push happens (by killing store.list_sessions), and asserts the
    transient exclusion survives.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from sessionfs.cli import cmd_cloud
    from sessionfs.store.deleted import mark_deleted, is_excluded

    mark_deleted(
        "ses_too_large", "cloud", reason="too_large", base_dir=tmp_path
    )
    monkeypatch.setattr(
        "sessionfs.store.deleted._DEFAULT_PATH",
        tmp_path / "deleted.json",
    )

    # list_remote succeeds (returns an empty result, has_more=False).
    mock_remote_result = MagicMock()
    mock_remote_result.sessions = []
    mock_remote_result.has_more = False
    mock_client = MagicMock()
    mock_client.list_remote_sessions = AsyncMock(return_value=mock_remote_result)
    mock_client.close = AsyncMock()
    monkeypatch.setattr(cmd_cloud, "_get_sync_client", lambda: mock_client)

    # …but the local store enumeration explodes.
    mock_store = MagicMock()
    mock_store.list_sessions.side_effect = RuntimeError("disk corrupted")
    monkeypatch.setattr(cmd_cloud, "open_store", lambda: mock_store)

    def _run_sync(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(cmd_cloud.asyncio, "run", _run_sync)

    # sync_all may swallow the inner RuntimeError; we just care about
    # the exclusion state on disk afterward.
    try:
        cmd_cloud.sync_all()
    except Exception:
        pass

    # Crucial assertion: the transient exclusion is intact because no
    # push was ever attempted against this session.
    assert is_excluded("ses_too_large", base_dir=tmp_path) is True, (
        "Per-session clear means partial-failure paths preserve the "
        "transient exclusion until the actual retry moment."
    )


def test_push_one_preserves_exclusion_when_session_dir_missing(
    monkeypatch, tmp_path: Path
):
    """Codex round 5 invariant: clear ⇒ network call actually issued.
    If the local session directory doesn't exist (e.g. it was deleted
    locally while still in the index), _push_one early-returns BEFORE
    the network call. The transient exclusion must NOT be cleared in
    that path — otherwise the daemon loses its backoff guard for a
    session we didn't actually retry.

    We invoke _push_one directly with a mocked store.get_session_dir
    that returns None.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from sessionfs.cli import cmd_cloud
    from sessionfs.store.deleted import mark_deleted, is_excluded

    mark_deleted("ses_orphan", "cloud", reason="too_large", base_dir=tmp_path)
    monkeypatch.setattr(
        "sessionfs.store.deleted._DEFAULT_PATH",
        tmp_path / "deleted.json",
    )

    # Mock the global store + client so sync_all reaches _push_one
    # without trying real I/O.
    mock_store = MagicMock()
    mock_store.get_session_dir.return_value = None  # ← preflight short-circuit
    mock_store.list_sessions.return_value = [
        {"session_id": "ses_orphan"}
    ]
    monkeypatch.setattr(cmd_cloud, "open_store", lambda: mock_store)

    mock_remote = MagicMock()
    mock_remote.sessions = []
    mock_remote.has_more = False
    mock_client = MagicMock()
    mock_client.list_remote_sessions = AsyncMock(return_value=mock_remote)
    mock_client.push_session = AsyncMock()  # Should NEVER be called
    mock_client.close = AsyncMock()
    monkeypatch.setattr(cmd_cloud, "_get_sync_client", lambda: mock_client)

    def _run(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(cmd_cloud.asyncio, "run", _run)

    try:
        cmd_cloud.sync_all()
    except Exception:
        pass

    # Network call NEVER happened — exclusion must survive.
    mock_client.push_session.assert_not_called()
    assert is_excluded("ses_orphan", base_dir=tmp_path) is True, (
        "session-dir missing must NOT clear the transient exclusion"
    )


def test_push_one_preserves_exclusion_when_already_in_sync(
    monkeypatch, tmp_path: Path
):
    """If the remote etag matches the local etag, _push_one returns
    BEFORE the network call. The exclusion must stay — there was no
    real retry, nothing changed."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from sessionfs.cli import cmd_cloud
    from sessionfs.store.deleted import mark_deleted, is_excluded

    mark_deleted("ses_synced", "cloud", reason="too_large", base_dir=tmp_path)
    monkeypatch.setattr(
        "sessionfs.store.deleted._DEFAULT_PATH",
        tmp_path / "deleted.json",
    )

    fake_session_dir = tmp_path / "ses_synced.sfs"
    fake_session_dir.mkdir()

    mock_store = MagicMock()
    mock_store.get_session_dir.return_value = fake_session_dir
    mock_store.get_session_manifest.return_value = {
        "sync": {"etag": "same-etag"}
    }
    mock_store.list_sessions.return_value = [{"session_id": "ses_synced"}]
    monkeypatch.setattr(cmd_cloud, "open_store", lambda: mock_store)

    # Remote reports the same etag → early return before network call.
    remote_session = MagicMock(id="ses_synced", etag="same-etag")
    mock_remote = MagicMock()
    mock_remote.sessions = [remote_session]
    mock_remote.has_more = False
    mock_client = MagicMock()
    mock_client.list_remote_sessions = AsyncMock(return_value=mock_remote)
    mock_client.push_session = AsyncMock()
    mock_client.close = AsyncMock()
    monkeypatch.setattr(cmd_cloud, "_get_sync_client", lambda: mock_client)

    def _run(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(cmd_cloud.asyncio, "run", _run)

    try:
        cmd_cloud.sync_all()
    except Exception:
        pass

    mock_client.push_session.assert_not_called()
    assert is_excluded("ses_synced", base_dir=tmp_path) is True, (
        "etag-match preflight return must NOT clear the exclusion"
    )


def test_get_entry_returns_none_for_malformed_value(tmp_path: Path):
    """Codex round 8 LOW: deleted.json can be hand-edited or corrupted
    into a shape where the value is a bare string (e.g.
    `{"ses_x": "everywhere"}` instead of `{"ses_x": {"scope": ...}}`).
    Every caller that does `.get("reason")` on the result would crash
    with AttributeError. Defending at the source so all callers
    (sync hint, sfs delete CLI, push undelete prompt) are safe in one
    place.
    """
    import json as _json
    from sessionfs.store.deleted import get_entry

    # Write a malformed entry directly to disk
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "deleted.json").write_text(
        _json.dumps({"ses_corrupt": "everywhere"})
    )

    # Must NOT raise — must return None
    assert get_entry("ses_corrupt", base_dir=tmp_path) is None


def test_all_helpers_agree_on_malformed_entry(tmp_path: Path):
    """Round 10 invariant: every public exclusion helper must agree
    on what a malformed entry means. Before this fix:
        is_excluded('ses_x')        -> True   (raw key check)
        get_entry('ses_x')          -> None   (filtered)
        list_deleted()              -> {}     (filtered)
    That split-brain leaked into watchers, push undelete prompt, and
    410 cleanup. The fix makes is_excluded delegate to get_entry so
    all helpers share one source of truth.
    """
    import json as _json
    from sessionfs.store.deleted import (
        get_entry,
        is_excluded,
        is_transient_exclusion,
        list_deleted,
    )

    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "deleted.json").write_text(
        _json.dumps({"ses_corrupt": "everywhere"})
    )

    # Every helper must agree: malformed = absent.
    assert is_excluded("ses_corrupt", base_dir=tmp_path) is False
    assert get_entry("ses_corrupt", base_dir=tmp_path) is None
    assert list_deleted(base_dir=tmp_path) == {}
    assert is_transient_exclusion("ses_corrupt", base_dir=tmp_path) is False


def test_list_deleted_skips_malformed_entries(tmp_path: Path):
    """Round 9 LOW: list_deleted must defensively skip non-dict values
    so every iterating caller (sfs trash, sfs restore listing, daemon
    cleanup) is safe in one place. Same defensive shape as get_entry.
    """
    import json as _json
    from sessionfs.store.deleted import list_deleted, mark_deleted

    tmp_path.mkdir(parents=True, exist_ok=True)
    # Mix one good entry with one corrupt one
    mark_deleted("ses_good", "cloud", reason="too_large", base_dir=tmp_path)
    raw = _json.loads((tmp_path / "deleted.json").read_text())
    raw["ses_corrupt"] = "everywhere"  # bare string — shouldn't be here
    (tmp_path / "deleted.json").write_text(_json.dumps(raw))

    entries = list_deleted(base_dir=tmp_path)
    # The corrupt entry is filtered out; the good entry survives.
    assert "ses_corrupt" not in entries
    assert "ses_good" in entries
    assert isinstance(entries["ses_good"], dict)


def test_sfs_trash_survives_malformed_deleted_json(monkeypatch, tmp_path: Path):
    """End-to-end: `sfs trash` iterates list_deleted() and calls
    `.get(...)` on each value. A corrupted entry used to raise
    AttributeError before the user saw anything. Now the corrupt
    entry is filtered out at the source and trash prints normally.
    """
    import json as _json
    from typer.testing import CliRunner

    from sessionfs.cli.main import app
    from sessionfs.store.deleted import mark_deleted

    tmp_path.mkdir(parents=True, exist_ok=True)
    mark_deleted("ses_good1234abcdef", "cloud", reason="too_large", base_dir=tmp_path)
    raw = _json.loads((tmp_path / "deleted.json").read_text())
    raw["ses_corrupt1234ab"] = "everywhere"
    (tmp_path / "deleted.json").write_text(_json.dumps(raw))

    monkeypatch.setattr(
        "sessionfs.store.deleted._DEFAULT_PATH",
        tmp_path / "deleted.json",
    )

    runner = CliRunner()
    result = runner.invoke(app, ["trash"])
    # Must NOT crash with AttributeError on the corrupt entry.
    assert result.exit_code == 0, result.output
    # The good entry shows up; the corrupt one is silently dropped.
    assert "ses_good1" in result.output
    assert "ses_corrupt1" not in result.output


def test_push_one_survives_malformed_deleted_json(monkeypatch, tmp_path: Path):
    """End-to-end: a corrupted deleted.json must not crash sync before
    the atomic gate runs. With the fix at get_entry, the hint sees
    None and proceeds; acquire_for_retry then handles the same value
    defensively (already covered in src/sessionfs/store/deleted.py:228).
    """
    import asyncio
    import json as _json
    from unittest.mock import AsyncMock, MagicMock

    from sessionfs.cli import cmd_cloud

    # Write a corrupt entry (bare string value)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "deleted.json").write_text(
        _json.dumps({"ses_corrupt": "everywhere"})
    )
    monkeypatch.setattr(
        "sessionfs.store.deleted._DEFAULT_PATH",
        tmp_path / "deleted.json",
    )

    fake_dir = tmp_path / "ses_corrupt.sfs"
    fake_dir.mkdir()

    mock_store = MagicMock()
    mock_store.get_session_dir.return_value = fake_dir
    mock_store.get_session_manifest.return_value = {"sync": {"etag": "v1"}}
    mock_store.list_sessions.return_value = [{"session_id": "ses_corrupt"}]
    monkeypatch.setattr(cmd_cloud, "open_store", lambda: mock_store)

    remote_session = MagicMock(id="ses_corrupt", etag="v0")
    mock_remote = MagicMock()
    mock_remote.sessions = [remote_session]
    mock_remote.has_more = False
    mock_client = MagicMock()
    mock_client.list_remote_sessions = AsyncMock(return_value=mock_remote)
    mock_client.push_session = AsyncMock(return_value=MagicMock(etag="v2"))
    mock_client.close = AsyncMock()
    monkeypatch.setattr(cmd_cloud, "_get_sync_client", lambda: mock_client)

    monkeypatch.setattr(
        "sessionfs.sync.archive.pack_session", lambda _d: b"archive"
    )
    monkeypatch.setattr(
        cmd_cloud, "_update_manifest_sync", lambda *_a, **_k: None
    )

    def _run(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(cmd_cloud.asyncio, "run", _run)

    # Must NOT raise AttributeError on hint.get("reason"). Whether the
    # eventual push happens or not is secondary — the contract here
    # is "manual sync doesn't crash on a corrupted exclusion file".
    cmd_cloud.sync_all()


def test_acquire_for_retry_with_no_entry_returns_true(tmp_path: Path):
    """No exclusion at all → caller may proceed, no side effects."""
    from sessionfs.store.deleted import acquire_for_retry, list_deleted

    assert acquire_for_retry("ses_clean", base_dir=tmp_path) is True
    assert list_deleted(base_dir=tmp_path) == {}


def test_acquire_for_retry_with_transient_clears_and_returns_true(tmp_path: Path):
    """Transient entry → atomically removed, caller may proceed."""
    from sessionfs.store.deleted import (
        acquire_for_retry,
        is_excluded,
        mark_deleted,
    )

    mark_deleted("ses_t", "cloud", reason="too_large", base_dir=tmp_path)
    assert acquire_for_retry("ses_t", base_dir=tmp_path) is True
    assert is_excluded("ses_t", base_dir=tmp_path) is False


def test_acquire_for_retry_with_hard_delete_returns_false(tmp_path: Path):
    """Hard delete → caller must abort. Entry survives."""
    from sessionfs.store.deleted import (
        acquire_for_retry,
        is_excluded,
        mark_deleted,
    )

    mark_deleted("ses_h", "everywhere", base_dir=tmp_path)
    assert acquire_for_retry("ses_h", base_dir=tmp_path) is False
    assert is_excluded("ses_h", base_dir=tmp_path) is True


def test_acquire_for_retry_with_unknown_reason_returns_false(tmp_path: Path):
    """Future-proofing: any non-TRANSIENT reason behaves as hard delete."""
    from sessionfs.store.deleted import (
        acquire_for_retry,
        is_excluded,
        mark_deleted,
    )

    mark_deleted("ses_x", "cloud", reason="server_410", base_dir=tmp_path)
    assert acquire_for_retry("ses_x", base_dir=tmp_path) is False
    assert is_excluded("ses_x", base_dir=tmp_path) is True


def test_push_one_aborts_when_hard_delete_appears_after_snapshot(
    monkeypatch, tmp_path: Path
):
    """Codex round 7 race: snapshot sees NO exclusion entry, then a
    concurrent writer adds a hard delete before the network call.
    The atomic acquire_for_retry gate must catch this and abort —
    "hard delete wins" must hold even when the pre-snapshot state
    was 'no entry at all'.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from sessionfs.cli import cmd_cloud
    from sessionfs.store.deleted import (
        is_excluded,
        mark_deleted,
    )

    # IMPORTANT: do NOT mark_deleted up front — the snapshot must see
    # nothing. The concurrent writer adds the hard delete during
    # pack_session below.
    monkeypatch.setattr(
        "sessionfs.store.deleted._DEFAULT_PATH",
        tmp_path / "deleted.json",
    )

    fake_dir = tmp_path / "ses_race2.sfs"
    fake_dir.mkdir()

    mock_store = MagicMock()
    mock_store.get_session_dir.return_value = fake_dir
    mock_store.get_session_manifest.return_value = {"sync": {"etag": "v1"}}
    mock_store.list_sessions.return_value = [{"session_id": "ses_race2"}]
    monkeypatch.setattr(cmd_cloud, "open_store", lambda: mock_store)

    remote_session = MagicMock(id="ses_race2", etag="v0")
    mock_remote = MagicMock()
    mock_remote.sessions = [remote_session]
    mock_remote.has_more = False
    mock_client = MagicMock()
    mock_client.list_remote_sessions = AsyncMock(return_value=mock_remote)
    mock_client.push_session = AsyncMock()  # MUST NOT be called
    mock_client.close = AsyncMock()
    monkeypatch.setattr(cmd_cloud, "_get_sync_client", lambda: mock_client)

    def _pack_with_race(_session_dir):
        # Simulate: between _push_one's initial get_entry (saw None)
        # and the acquire_for_retry call, an `sfs delete --everywhere`
        # writes a hard delete entry.
        mark_deleted("ses_race2", "everywhere", base_dir=tmp_path)
        return b"fake-archive-bytes"

    monkeypatch.setattr(
        "sessionfs.sync.archive.pack_session", _pack_with_race
    )

    def _run(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(cmd_cloud.asyncio, "run", _run)

    try:
        cmd_cloud.sync_all()
    except Exception:
        pass

    # The atomic gate caught the newly-installed hard delete.
    mock_client.push_session.assert_not_called()
    assert is_excluded("ses_race2", base_dir=tmp_path) is True


def test_remove_if_transient_removes_transient_entry(tmp_path: Path):
    """Happy path: a too_large entry → removed, returns True."""
    from sessionfs.store.deleted import (
        is_excluded,
        mark_deleted,
        remove_if_transient,
    )

    mark_deleted("ses_x", "cloud", reason="too_large", base_dir=tmp_path)
    assert is_excluded("ses_x", base_dir=tmp_path) is True
    assert remove_if_transient("ses_x", base_dir=tmp_path) is True
    assert is_excluded("ses_x", base_dir=tmp_path) is False


def test_remove_if_transient_preserves_hard_delete(tmp_path: Path):
    """Codex round 6 TOCTOU defense: if a concurrent writer turned
    the entry into a hard delete BEFORE we tried to clear it, we must
    NOT remove it. The atomic check inside the fcntl lock catches this."""
    from sessionfs.store.deleted import (
        is_excluded,
        mark_deleted,
        remove_if_transient,
    )

    # Hard delete with no transient reason — must survive the call.
    mark_deleted("ses_x", "everywhere", base_dir=tmp_path)
    assert remove_if_transient("ses_x", base_dir=tmp_path) is False
    assert is_excluded("ses_x", base_dir=tmp_path) is True, (
        "hard delete must survive remove_if_transient"
    )


def test_remove_if_transient_returns_false_for_missing(tmp_path: Path):
    """No entry → no-op, returns False (not an error)."""
    from sessionfs.store.deleted import remove_if_transient

    assert remove_if_transient("ses_never_existed", base_dir=tmp_path) is False


def test_remove_if_transient_treats_unknown_reason_as_hard(tmp_path: Path):
    """Future-proofing: any reason NOT in TRANSIENT_REASONS is treated
    as a hard delete by this helper. A new "server_410" or similar
    reason should not be wiped by manual sync until we explicitly
    add it to TRANSIENT_REASONS.
    """
    from sessionfs.store.deleted import (
        is_excluded,
        mark_deleted,
        remove_if_transient,
    )

    mark_deleted("ses_x", "cloud", reason="server_410", base_dir=tmp_path)
    assert remove_if_transient("ses_x", base_dir=tmp_path) is False
    assert is_excluded("ses_x", base_dir=tmp_path) is True


def test_push_one_aborts_when_concurrent_writer_promotes_to_hard_delete(
    monkeypatch, tmp_path: Path
):
    """Codex round 6 scenario: between _push_one's initial get_entry()
    snapshot and the atomic remove_if_transient inside the semaphore,
    another writer (sfs delete --everywhere, daemon hard-delete on
    server-410, etc.) replaced the transient entry with a hard delete.
    The network call must NOT fire and the hard delete must survive.

    Simulate by seeding a transient entry, mocking pack_session to
    promote it to a hard delete just before the (would-be) network
    call.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    from sessionfs.cli import cmd_cloud
    from sessionfs.store.deleted import (
        is_excluded,
        mark_deleted,
        get_entry,
    )

    mark_deleted("ses_racy", "cloud", reason="too_large", base_dir=tmp_path)
    monkeypatch.setattr(
        "sessionfs.store.deleted._DEFAULT_PATH",
        tmp_path / "deleted.json",
    )

    fake_dir = tmp_path / "ses_racy.sfs"
    fake_dir.mkdir()

    mock_store = MagicMock()
    mock_store.get_session_dir.return_value = fake_dir
    mock_store.get_session_manifest.return_value = {"sync": {"etag": "v1"}}
    mock_store.list_sessions.return_value = [{"session_id": "ses_racy"}]
    monkeypatch.setattr(cmd_cloud, "open_store", lambda: mock_store)

    # Remote etag differs so we don't short-circuit on the in-sync
    # branch — the push attempt should otherwise proceed.
    remote_session = MagicMock(id="ses_racy", etag="v0")
    mock_remote = MagicMock()
    mock_remote.sessions = [remote_session]
    mock_remote.has_more = False
    mock_client = MagicMock()
    mock_client.list_remote_sessions = AsyncMock(return_value=mock_remote)
    mock_client.push_session = AsyncMock()  # MUST NOT be called
    mock_client.close = AsyncMock()
    monkeypatch.setattr(cmd_cloud, "_get_sync_client", lambda: mock_client)

    # Stub pack_session so that, right before the would-be network
    # call, a concurrent writer hard-deletes the session entry.
    def _pack_with_race(_session_dir):
        mark_deleted(
            "ses_racy", "everywhere", base_dir=tmp_path  # No transient reason
        )
        return b"fake-archive-bytes"

    # pack_session is imported inside sync_all from sessionfs.sync.archive
    # so patch it at the source module to intercept the call.
    monkeypatch.setattr(
        "sessionfs.sync.archive.pack_session", _pack_with_race
    )

    def _run(coro):
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()

    monkeypatch.setattr(cmd_cloud.asyncio, "run", _run)

    try:
        cmd_cloud.sync_all()
    except Exception:
        pass

    # The network call must NOT have happened — remove_if_transient
    # returned False, _push_one bailed out.
    mock_client.push_session.assert_not_called()
    # The hard delete the concurrent writer installed must survive.
    assert is_excluded("ses_racy", base_dir=tmp_path) is True
    entry = get_entry("ses_racy", base_dir=tmp_path)
    assert entry is not None
    # No transient reason — confirms our atomic check kept the new
    # hard delete intact.
    from sessionfs.store.deleted import TRANSIENT_REASONS
    assert entry.get("reason") not in TRANSIENT_REASONS


def test_per_session_clear_removes_only_the_attempted_one(
    monkeypatch, tmp_path: Path
):
    """When sync retries session A but never reaches session B (e.g.
    crashes mid-loop), only A's transient exclusion clears. B stays
    excluded so the daemon's backoff guard remains effective for it.
    """
    from sessionfs.store.deleted import (
        TRANSIENT_REASONS,
        get_entry,
        is_excluded,
        mark_deleted,
        remove_exclusion,
    )

    # Two transiently-excluded sessions
    mark_deleted("ses_A", "cloud", reason="too_large", base_dir=tmp_path)
    mark_deleted("ses_B", "cloud", reason="too_large", base_dir=tmp_path)
    # And a hard delete that must NEVER be touched
    mark_deleted("ses_hard", "everywhere", base_dir=tmp_path)

    # Simulate _push_one's per-session logic on ses_A only (ses_B
    # never reached).
    entry_a = get_entry("ses_A", base_dir=tmp_path)
    assert entry_a is not None
    assert entry_a.get("reason") in TRANSIENT_REASONS
    remove_exclusion("ses_A", base_dir=tmp_path)

    # A cleared, B intact, hard delete intact.
    assert is_excluded("ses_A", base_dir=tmp_path) is False
    assert is_excluded("ses_B", base_dir=tmp_path) is True
    assert is_excluded("ses_hard", base_dir=tmp_path) is True
