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


class TestExclusionListGuard:
    """Watcher must skip captures for sessions in deleted.json.

    Regression: without this check, after a 410 / sfs delete, native
    watchers would re-discover the session from the still-present
    native source and resurrect it on every machine.
    """

    def test_excluded_session_blocked_on_first_capture(self, store):
        from sessionfs.store import deleted as deleted_mod

        # tk_457d060822bc48c0 R2 — capture_guard now scopes the exclusion
        # check to store.store_dir, so seed the exclusion there.
        deleted_mod.mark_deleted("ses_excluded", "everywhere", base_dir=store.store_dir)
        assert should_recapture(store, "ses_excluded", 50, "claude-code") is False

    def test_excluded_session_blocked_on_recapture(self, store):
        from sessionfs.store import deleted as deleted_mod

        sfs_id = "ses_existing_then_deleted"
        _create_existing_session(store, sfs_id, 10)
        deleted_mod.mark_deleted(sfs_id, "cloud", base_dir=store.store_dir)
        assert should_recapture(store, sfs_id, 100, "codex") is False

    def test_recapture_honors_profile_scoped_exclusion_not_global(
        self, store, tmp_path, monkeypatch
    ):
        """tk_457d060822bc48c0 R2 MEDIUM — a deletion recorded under the
        active profile's store must block recapture even when the global
        default deleted.json is empty. Without base_dir threading, the
        watcher read the global file, missed the profile-scoped deletion,
        and resurrected the session."""
        from sessionfs.store import deleted as deleted_mod

        # Global/default deleted.json points somewhere empty.
        empty_global = tmp_path / "global"
        empty_global.mkdir()
        monkeypatch.setattr(deleted_mod, "_DEFAULT_DIR", empty_global)
        monkeypatch.setattr(
            deleted_mod, "_DEFAULT_PATH", empty_global / "deleted.json"
        )

        # The deletion lives ONLY in the profile store (store.store_dir).
        deleted_mod.mark_deleted("ses_profile_del", "cloud", base_dir=store.store_dir)

        # Sanity: the global default does NOT see it.
        assert deleted_mod.is_excluded("ses_profile_del") is False
        assert deleted_mod.is_excluded(
            "ses_profile_del", base_dir=store.store_dir
        ) is True

        # The watcher must skip recapture — it checks store.store_dir.
        assert should_recapture(store, "ses_profile_del", 50, "claude-code") is False

    def test_non_excluded_session_allowed(self, store, tmp_path, monkeypatch):
        from sessionfs.store import deleted as deleted_mod

        monkeypatch.setattr(deleted_mod, "_DEFAULT_DIR", tmp_path)
        monkeypatch.setattr(deleted_mod, "_DEFAULT_PATH", tmp_path / "deleted.json")
        assert should_recapture(store, "ses_normal", 20, "gemini") is True
