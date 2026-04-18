"""Tests for the compression-safe capture guard."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessionfs.watchers.capture_guard import should_recapture


@pytest.fixture()
def store(tmp_path: Path):
    """Create a minimal LocalStore for testing."""
    from sessionfs.store.local import LocalStore

    return LocalStore(tmp_path / "sfs_store")


def _create_existing_session(store, sfs_id: str, message_count: int) -> None:
    """Create a minimal .sfs session directory with a manifest."""
    session_dir = store.allocate_session_dir(sfs_id)
    manifest = {
        "sfs_version": "0.1.0",
        "session_id": sfs_id,
        "stats": {"message_count": message_count},
    }
    (session_dir / "manifest.json").write_text(json.dumps(manifest))


class TestShouldRecapture:
    def test_first_capture_always_allowed(self, store):
        """No existing .sfs directory -> should always capture."""
        assert should_recapture(store, "ses_new_session_id", 10, "claude-code") is True

    def test_growth_allowed(self, store):
        """New message count > existing -> normal growth, should capture."""
        _create_existing_session(store, "ses_growth_test", 10)
        assert should_recapture(store, "ses_growth_test", 15, "claude-code") is True

    def test_compression_blocked(self, store):
        """New message count < existing -> compression detected, skip."""
        _create_existing_session(store, "ses_compress_test", 50)
        assert should_recapture(store, "ses_compress_test", 20, "claude-code") is False

    def test_equal_count_allowed(self, store):
        """Same count -> content may have changed, should capture."""
        _create_existing_session(store, "ses_equal_test", 25)
        assert should_recapture(store, "ses_equal_test", 25, "gemini") is True

    def test_no_manifest_allows_capture(self, store):
        """Existing dir but no manifest -> should capture."""
        sfs_id = "ses_no_manifest"
        store.allocate_session_dir(sfs_id)
        # Don't write a manifest
        assert should_recapture(store, sfs_id, 10, "codex") is True

    def test_corrupt_manifest_allows_capture(self, store):
        """Existing dir with corrupt manifest -> should capture."""
        sfs_id = "ses_corrupt_manifest"
        session_dir = store.allocate_session_dir(sfs_id)
        (session_dir / "manifest.json").write_text("not valid json{{{")
        assert should_recapture(store, sfs_id, 10, "cursor") is True
