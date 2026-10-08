"""Integration test: full capture pipeline.

Places CC session fixtures in a fake ~/.claude/, runs the watcher's
full_scan(), and verifies the output .sfs sessions pass schema validation.
"""

from __future__ import annotations

import json
from pathlib import Path

from sessionfs.daemon.config import ClaudeCodeWatcherConfig
from sessionfs.spec.validate import validate_session
from sessionfs.store.local import LocalStore
from sessionfs.watchers.claude_code import ClaudeCodeWatcher


def test_full_capture_pipeline(tmp_claude_home: Path, tmp_store: Path):
    """End-to-end: CC fixtures → watcher full_scan → .sfs → validation."""
    store = LocalStore(tmp_store)
    store.initialize()

    config = ClaudeCodeWatcherConfig(home_dir=tmp_claude_home)
    watcher = ClaudeCodeWatcher(config=config, store=store, scan_interval=0.0)

    # Run full scan
    watcher.full_scan()

    # Check watcher status
    status = watcher.get_status()
    assert status.health == "healthy"
    assert status.sessions_tracked >= 3  # minimal, with_tools, test-subagent-9999

    # Verify captured .sfs sessions
    sessions = store.list_sessions()
    assert len(sessions) >= 3

    for session_row in sessions:
        session_dir = Path(session_row["sfs_dir_path"])
        assert session_dir.is_dir(), f"Session dir missing: {session_dir}"

        # Validate against JSON schemas
        result = validate_session(session_dir)
        assert result.valid, (
            f"Session {session_row['session_id']} failed validation: {result.errors}"
        )

    store.close()


def test_capture_detects_changes(tmp_claude_home: Path, tmp_store: Path):
    """Watcher skips unchanged sessions on second scan."""
    store = LocalStore(tmp_store)
    store.initialize()

    config = ClaudeCodeWatcherConfig(home_dir=tmp_claude_home)
    watcher = ClaudeCodeWatcher(config=config, store=store, scan_interval=0.0)

    # First scan
    watcher.full_scan()
    status1 = watcher.get_status()

    # Second scan — nothing changed, should skip
    watcher.full_scan()
    status2 = watcher.get_status()

    assert status1.sessions_tracked == status2.sessions_tracked
    assert status2.health == "healthy"

    store.close()


def test_capture_subagent_session(tmp_claude_home: Path, tmp_store: Path):
    """Sub-agent messages are captured as sidechain in .sfs output."""
    store = LocalStore(tmp_store)
    store.initialize()

    config = ClaudeCodeWatcherConfig(home_dir=tmp_claude_home)
    watcher = ClaudeCodeWatcher(config=config, store=store, scan_interval=0.0)
    watcher.full_scan()

    # Find the subagent session (ID is now ses_ prefixed)
    from sessionfs.session_id import session_id_from_native
    session_dir = store.get_session_dir(session_id_from_native("test-subagent-9999"))
    assert session_dir is not None

    # Read messages and check for sidechain entries
    messages = []
    with open(session_dir / "messages.jsonl") as f:
        for line in f:
            line = line.strip()
            if line:
                messages.append(json.loads(line))

    sidechain = [m for m in messages if m.get("is_sidechain")]
    assert len(sidechain) == 4

    # Check manifest has sub_agents
    manifest = json.loads((session_dir / "manifest.json").read_text())
    assert "sub_agents" in manifest
    assert manifest["sub_agents"][0]["agent_id"] == "agent-explore-001"

    store.close()


def test_missing_claude_dir(tmp_path: Path):
    """Watcher degrades gracefully when Claude Code is not installed."""
    store = LocalStore(tmp_path / "store")
    store.initialize()

    config = ClaudeCodeWatcherConfig(home_dir=tmp_path / "nonexistent")
    watcher = ClaudeCodeWatcher(config=config, store=store, scan_interval=0.0)
    watcher.full_scan()

    status = watcher.get_status()
    assert status.health == "degraded"
    assert status.sessions_tracked == 0

    store.close()


def test_capture_queues_autosync_and_keeps_sync_state(
    tmp_claude_home: Path, tmp_store: Path
):
    """A session captured while the daemon runs is queued for autosync, and a
    re-capture keeps the etag from its last sync (regression: autosync only
    synced sessions at daemon startup, and each capture erased sync state)."""
    import os

    from sessionfs.daemon.config import DaemonConfig
    from sessionfs.daemon.main import DaemonSyncer

    store = LocalStore(tmp_store)
    store.initialize()
    syncer = DaemonSyncer(
        DaemonConfig(sync={"enabled": True, "api_key": "k", "auto": "all"}), store
    )
    store.add_write_listener(syncer.mark_session_dirty)

    config = ClaudeCodeWatcherConfig(home_dir=tmp_claude_home)
    watcher = ClaudeCodeWatcher(config=config, store=store, scan_interval=0.0)
    watcher.full_scan()

    captured = {row["session_id"] for row in store.list_sessions()}
    assert captured
    assert captured <= set(syncer._debounce_timestamps)

    # Simulate a completed sync, then a change to the native session.
    sid = sorted(captured)[0]
    session_dir = store.get_session_dir(sid)
    manifest_path = session_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["sync"] = {"etag": "etag-from-last-push", "dirty": False}
    manifest_path.write_text(json.dumps(manifest))
    syncer._debounce_timestamps.clear()

    native = next(
        Path(ref.native_path) for ref in watcher._tracked.values() if ref.sfs_session_id == sid
    )
    with open(native, "a") as f:
        f.write("\n")
    st = native.stat()
    os.utime(native, (st.st_atime, st.st_mtime + 5))
    watcher.full_scan()

    sync = json.loads(manifest_path.read_text())["sync"]
    assert sync["etag"] == "etag-from-last-push"
    assert sync["dirty"] is True
    assert sid in syncer._debounce_timestamps
    store.close()
