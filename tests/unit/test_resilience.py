"""Tests for resilient local store: self-healing index and handle_errors decorator."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


def _make_sfs_session(sessions_dir: Path, session_id: str) -> Path:
    """Create a minimal .sfs session directory with a manifest."""
    sfs_dir = sessions_dir / f"{session_id}.sfs"
    sfs_dir.mkdir(parents=True)
    manifest = {
        "session_id": session_id,
        "title": f"Test session {session_id}",
        "source": {"tool": "claude-code"},
        "created_at": "2025-01-01T00:00:00Z",
        "stats": {"message_count": 5},
    }
    (sfs_dir / "manifest.json").write_text(json.dumps(manifest))
    return sfs_dir


class TestSelfHealingIndex:
    """Test that a corrupted index.db is automatically rebuilt."""

    def test_corrupted_index_auto_heals(self, tmp_path: Path) -> None:
        """Write garbage to index.db, initialize, verify it works."""
        store_dir = tmp_path / ".sessionfs"
        store_dir.mkdir()
        sessions_dir = store_dir / "sessions"
        sessions_dir.mkdir()

        # Create a session on disk
        _make_sfs_session(sessions_dir, "ses_test1234abcd")

        # Write garbage to index.db to simulate corruption
        index_path = store_dir / "index.db"
        index_path.write_bytes(b"this is not a valid sqlite database at all!!")

        # Initialize should auto-heal
        from sessionfs.store.local import LocalStore

        local_store = LocalStore(store_dir)
        local_store.initialize()

        # Verify the index is functional and the session was reindexed
        sessions = local_store.list_sessions()
        assert len(sessions) == 1
        assert sessions[0]["session_id"] == "ses_test1234abcd"

        local_store.close()

    def test_missing_index_creates_fresh(self, tmp_path: Path) -> None:
        """If no index.db exists, a fresh one is created normally."""
        store_dir = tmp_path / ".sessionfs"

        from sessionfs.store.local import LocalStore

        local_store = LocalStore(store_dir)
        local_store.initialize()

        sessions = local_store.list_sessions()
        assert sessions == []

        local_store.close()

    def test_valid_index_not_rebuilt(self, tmp_path: Path) -> None:
        """A healthy index.db is not rebuilt unnecessarily."""
        store_dir = tmp_path / ".sessionfs"

        from sessionfs.store.local import LocalStore

        local_store = LocalStore(store_dir)
        local_store.initialize()

        # _needs_reindex should be False
        assert local_store.index._needs_reindex is False

        local_store.close()

    def test_reindex_skips_bad_manifest_without_dropping_later_sessions(
        self, tmp_path: Path
    ) -> None:
        """KB entry 204 regression — daemon-reindex data loss.

        A manifest with `"source": null` previously caused
        `source.get("tool", "unknown")` to raise AttributeError. The
        per-session except clause in `_rebuild_index_from_disk` only
        caught `(json.JSONDecodeError, OSError)`, so the unhandled
        AttributeError aborted the entire reindex loop. Every session
        sorted alphabetically AFTER the bad one was silently dropped
        — matching the user-reported symptom of "7-8 Codex sessions
        disappeared from sfs list after daemon restart".

        Fix: `manifest.get("source") or {}` (null-safe), and broaden
        the per-session except clause to `Exception` so one bad
        manifest can never poison the loop again.
        """
        store_dir = tmp_path / ".sessionfs"
        store_dir.mkdir()
        sessions_dir = store_dir / "sessions"
        sessions_dir.mkdir()

        def _write(sid: str, source) -> None:
            sfs = sessions_dir / f"{sid}.sfs"
            sfs.mkdir()
            manifest = {
                "session_id": sid,
                "title": f"Session {sid}",
                "source": source,
                "created_at": "2025-01-01T00:00:00Z",
                "stats": {"message_count": 5},
            }
            (sfs / "manifest.json").write_text(json.dumps(manifest))

        # Five sessions in alphabetical order; the middle one is the
        # poison shape — `"source": null`. Pre-fix, ses_b_bravo's bad
        # manifest aborted the reindex and ses_c/d/e all went missing.
        _write("ses_a_alpha", {"tool": "claude-code"})
        _write("ses_b_bravo", None)
        _write("ses_c_charlie", {"tool": "codex"})
        _write("ses_d_delta", {"tool": "gemini"})
        _write("ses_e_echo", {"tool": "claude-code"})

        # Corrupt index.db so self-heal triggers a full rebuild.
        (store_dir / "index.db").write_bytes(b"not a sqlite db")

        from sessionfs.store.local import LocalStore

        local_store = LocalStore(store_dir)
        # Initialize MUST NOT raise — pre-fix this raised AttributeError.
        local_store.initialize()

        sessions = local_store.list_sessions()
        ids = {s["session_id"] for s in sessions}

        # With null-safe defaults, the null-source session is also
        # recoverable (source.get(...) returns the "unknown" fallback).
        # All five sessions land in the rebuilt index.
        assert ids == {
            "ses_a_alpha",
            "ses_b_bravo",
            "ses_c_charlie",
            "ses_d_delta",
            "ses_e_echo",
        }, f"missing sessions after rebuild: {ids}"

        local_store.close()

    def test_reindex_handles_null_in_not_null_field_promptly(
        self, tmp_path: Path
    ) -> None:
        """Codex HIGH (KB entry 223) regression.

        Round-1 fix made `source` / `model` / `stats` / `tags` null-
        safe but missed `created_at`, which is also NOT NULL. The
        resulting `sqlite3.IntegrityError` was caught by
        `upsert_session_metadata`'s `except sqlite3.DatabaseError`
        (parent class), misinterpreted as index corruption, and
        triggered a destructive recreate-the-index path per bad
        session — noisy, wasteful, and per Codex "did not complete
        promptly".

        Two-layer fix verified here:
          1. `created_at: null` AND `source.tool: null` lands the
             `or` fallback (empty string / "unknown") instead of
             None, so no IntegrityError fires in the first place.
          2. If an IntegrityError DID fire for some other field, the
             corruption-recovery path is bypassed (IntegrityError
             is now caught before the DatabaseError branch and
             propagates to the per-session skip).
        """
        import logging

        store_dir = tmp_path / ".sessionfs"
        store_dir.mkdir()
        sessions_dir = store_dir / "sessions"
        sessions_dir.mkdir()

        def _write(sid: str, manifest_overrides: dict) -> None:
            sfs = sessions_dir / f"{sid}.sfs"
            sfs.mkdir()
            base = {
                "session_id": sid,
                "title": f"Session {sid}",
                "source": {"tool": "claude-code"},
                "created_at": "2025-01-01T00:00:00Z",
                "stats": {"message_count": 5},
            }
            base.update(manifest_overrides)
            (sfs / "manifest.json").write_text(json.dumps(base))

        _write("ses_a_alpha", {})
        _write("ses_b_bravo", {"created_at": None})  # null in NOT NULL
        _write("ses_c_charlie", {"source": {"tool": None}})  # null in NOT NULL
        _write("ses_d_delta", {})

        (store_dir / "index.db").write_bytes(b"corrupted")

        from sessionfs.store.local import LocalStore

        local_store = LocalStore(store_dir)

        # Capture the daemon log to assert that the destructive
        # "Index corrupted during write. Rebuilding..." message does
        # NOT fire — that was the noisy symptom Codex reported.
        store_logger = logging.getLogger("sessionfs.store.local")
        store_records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                store_records.append(record)

        handler = _Capture(level=logging.WARNING)
        store_logger.addHandler(handler)
        try:
            local_store.initialize()
        finally:
            store_logger.removeHandler(handler)

        # All four sessions land in the index (the `or` fallback
        # rescues both null-NOT-NULL shapes).
        ids = {s["session_id"] for s in local_store.list_sessions()}
        assert ids == {
            "ses_a_alpha",
            "ses_b_bravo",
            "ses_c_charlie",
            "ses_d_delta",
        }, f"missing sessions: {ids}"

        # The destructive "Index corrupted during write" line must NOT
        # have fired. If it did, the IntegrityError-vs-DatabaseError
        # split regressed.
        corrupt_msgs = [
            r for r in store_records
            if "Index corrupted during write" in r.getMessage()
        ]
        assert corrupt_msgs == [], (
            "Index recreate path fired for a data-level IntegrityError "
            f"(would be destructive + noisy): {[r.getMessage() for r in corrupt_msgs]}"
        )

        local_store.close()

    def test_integrity_error_propagates_not_recovers(
        self, tmp_path: Path
    ) -> None:
        """Direct unit test for the IntegrityError / DatabaseError split.

        Mocks the underlying index.upsert_session so we can deterministically
        raise sqlite3.IntegrityError without needing to violate the live
        schema. Pre-Codex-HIGH-fix, upsert_session_metadata caught this
        under `except sqlite3.DatabaseError` (parent class) and ran the
        destructive index-rebuild path per bad session — costing time and
        log noise. Post-fix, IntegrityError propagates so _rebuild_index_
        from_disk's broad except can isolate the one bad session.
        """
        store_dir = tmp_path / ".sessionfs"
        store_dir.mkdir()
        (store_dir / "sessions").mkdir()

        from sessionfs.store.local import LocalStore

        local_store = LocalStore(store_dir)
        local_store.initialize()

        # Track whether the destructive "Rebuilding..." path runs.
        rebuild_called = {"count": 0}
        original_rebuild = local_store._rebuild_index_from_disk

        def _spy_rebuild() -> None:
            rebuild_called["count"] += 1
            original_rebuild()

        local_store._rebuild_index_from_disk = _spy_rebuild  # type: ignore[method-assign]

        # Force upsert_session to raise IntegrityError on first call,
        # succeed on subsequent (so we can detect if a retry runs).
        call_count = {"n": 0}
        original_upsert = local_store.index.upsert_session

        def _flaky_upsert(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise sqlite3.IntegrityError("NOT NULL constraint failed: sessions.x")
            return original_upsert(*args, **kwargs)

        local_store.index.upsert_session = _flaky_upsert  # type: ignore[method-assign]

        # Call upsert_session_metadata. IntegrityError MUST propagate,
        # NOT trigger recovery.
        manifest = {
            "session_id": "ses_test",
            "source": {"tool": "claude-code"},
            "created_at": "2025-01-01T00:00:00Z",
        }
        try:
            local_store.upsert_session_metadata("ses_test", manifest, "/tmp/x.sfs")
        except sqlite3.IntegrityError:
            pass  # expected
        else:
            raise AssertionError(
                "Expected IntegrityError to propagate — recovery path swallowed it"
            )

        # The destructive rebuild MUST NOT have fired for an IntegrityError.
        assert rebuild_called["count"] == 0, (
            f"_rebuild_index_from_disk fired {rebuild_called['count']} times "
            "for an IntegrityError — that's the pre-fix destructive path"
        )
        # The upsert was called exactly once (no retry on IntegrityError).
        assert call_count["n"] == 1, (
            f"upsert_session called {call_count['n']} times — IntegrityError "
            "should not trigger the retry path"
        )

        local_store.close()

    def test_cli_rebuild_index_isolates_per_session(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex round-2 MEDIUM (KB entry 225) regression.

        Round-1 + round-2 fixes covered `LocalStore._rebuild_index_
        from_disk` but missed the parallel loop in the user-facing
        `sfs daemon rebuild-index` CLI command. That loop had its
        own copy of the same two bugs:
          - `manifest.get("source", {}).get("tool")` → AttributeError
            on `"source": null`
          - `except (json.JSONDecodeError, OSError)` → too narrow,
            so AttributeError aborts the whole command instead of
            skipping the one bad session.

        Repro shape Codex shipped: three sessions ses_a_alpha (clean),
        ses_b_bravo with `source: null`, ses_c_charlie (clean). Pre-fix
        the command exited 1; post-fix exit 0 with bravo skipped and
        alpha + charlie both indexed.
        """
        from typer.testing import CliRunner

        from sessionfs.cli.main import app

        store_dir = tmp_path / ".sessionfs"
        store_dir.mkdir()
        sessions_dir = store_dir / "sessions"
        sessions_dir.mkdir()

        def _write(sid: str, source) -> None:
            sfs = sessions_dir / f"{sid}.sfs"
            sfs.mkdir()
            (sfs / "manifest.json").write_text(
                json.dumps(
                    {
                        "session_id": sid,
                        "title": f"Session {sid}",
                        "source": source,
                        "created_at": "2025-01-01T00:00:00Z",
                        "stats": {"message_count": 5},
                    }
                )
            )

        _write("ses_a_alpha", {"tool": "claude-code"})
        _write("ses_b_bravo", None)
        _write("ses_c_charlie", {"tool": "codex"})

        # Point the CLI's open_store at our tmp dir.
        monkeypatch.setattr(
            "sessionfs.cli.common.get_store_dir",
            lambda: store_dir,
        )

        runner = CliRunner()
        result = runner.invoke(app, ["daemon", "rebuild-index"])

        assert result.exit_code == 0, (
            f"sfs daemon rebuild-index failed: exit={result.exit_code}\n"
            f"output:\n{result.output}\n"
            f"exc: {result.exception!r}"
        )
        # Strip ANSI before substring assert (FORCE_COLOR=1 CI fragility).
        from tests.utils.ansi import strip_ansi

        assert "Rebuilt index" in strip_ansi(result.output)

        # Verify the index has alpha + charlie at minimum (not just
        # exit 0 — bug pre-fix was that ses_b_bravo aborted the loop
        # mid-iteration, so ses_c_charlie was silently dropped).
        from sessionfs.store.local import LocalStore

        ls = LocalStore(store_dir)
        ls.initialize()
        ids = {s["session_id"] for s in ls.list_sessions()}
        ls.close()
        assert "ses_a_alpha" in ids, (
            f"ses_a_alpha missing — loop never started. ids={ids}"
        )
        assert "ses_c_charlie" in ids, (
            f"ses_c_charlie missing — loop aborted at ses_b_bravo. ids={ids}"
        )
        # ses_b_bravo is rescued by the `source = manifest.get("source") or {}`
        # null-safe fallback, so it lands in the index too. The
        # non-negotiable guarantee is that ses_c_charlie (sorted AFTER
        # the poison) is present.

    def test_reindex_handles_truthy_non_dict_source(
        self, tmp_path: Path
    ) -> None:
        """Codex round-3 LOW (KB entry 227) regression.

        `manifest.get("source") or {}` only catches falsy values, so
        a manifest with `"source": "codex"` (truthy string) passed
        through to `source.get("tool")` and raised AttributeError.
        Pre-fix, the broader except caught it but the session was
        SKIPPED rather than indexed. Post-fix, an `isinstance(...,
        dict)` guard normalizes any wrong-type value to `{}` so the
        session lands cleanly with `source_tool = "unknown"`.

        Same shape verified for `model` / `stats` / `tags`.
        """
        store_dir = tmp_path / ".sessionfs"
        store_dir.mkdir()
        sessions_dir = store_dir / "sessions"
        sessions_dir.mkdir()

        def _write(sid: str, overrides: dict) -> None:
            sfs = sessions_dir / f"{sid}.sfs"
            sfs.mkdir()
            base = {
                "session_id": sid,
                "title": f"Session {sid}",
                "source": {"tool": "claude-code"},
                "created_at": "2025-01-01T00:00:00Z",
                "stats": {"message_count": 5},
            }
            base.update(overrides)
            (sfs / "manifest.json").write_text(json.dumps(base))

        # Each session breaks a different field with a TRUTHY non-dict
        # value. All should land in the index post-fix.
        _write("ses_a_clean", {})
        _write("ses_b_str_source", {"source": "codex"})
        _write("ses_c_list_source", {"source": ["codex"]})
        _write("ses_d_str_model", {"model": "claude-opus-4"})
        _write("ses_e_str_stats", {"stats": "noop"})
        _write("ses_f_str_tags", {"tags": "not-a-list"})

        (store_dir / "index.db").write_bytes(b"corrupted")

        from sessionfs.store.local import LocalStore

        local_store = LocalStore(store_dir)
        local_store.initialize()

        ids = {s["session_id"] for s in local_store.list_sessions()}
        assert ids == {
            "ses_a_clean",
            "ses_b_str_source",
            "ses_c_list_source",
            "ses_d_str_model",
            "ses_e_str_stats",
            "ses_f_str_tags",
        }, f"missing sessions: {ids}"

        local_store.close()

    def test_cli_rebuild_index_repairs_string_source(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex round-3 LOW (KB entry 227) CLI-side repro.

        Direct invocation of `sfs daemon rebuild-index` with the
        exact shape Codex flagged: `"source": "codex"` (truthy
        string). Pre-fix: session was skipped with
        `AttributeError: 'str' object has no attribute 'get'`.
        Post-fix: the backfill repairs the manifest to
        `{"source": {"tool": <looked-up>}}` and the session lands
        in the index.
        """
        from typer.testing import CliRunner

        from sessionfs.cli.main import app

        store_dir = tmp_path / ".sessionfs"
        store_dir.mkdir()
        sessions_dir = store_dir / "sessions"
        sessions_dir.mkdir()

        sfs = sessions_dir / "ses_strsrc.sfs"
        sfs.mkdir()
        manifest_path = sfs / "manifest.json"
        manifest_path.write_text(
            json.dumps(
                {
                    "session_id": "ses_strsrc",
                    "title": "Wrong-type source",
                    "source": "codex",  # the poison shape Codex flagged
                    "created_at": "2025-01-01T00:00:00Z",
                    "stats": {"message_count": 1},
                }
            )
        )

        monkeypatch.setattr(
            "sessionfs.cli.common.get_store_dir",
            lambda: store_dir,
        )

        runner = CliRunner()
        result = runner.invoke(app, ["daemon", "rebuild-index"])

        assert result.exit_code == 0, (
            f"exit={result.exit_code}\noutput:\n{result.output}\n"
            f"exc: {result.exception!r}"
        )

        # Session is indexed (not skipped).
        from sessionfs.store.local import LocalStore

        ls = LocalStore(store_dir)
        ls.initialize()
        ids = {s["session_id"] for s in ls.list_sessions()}
        ls.close()
        assert "ses_strsrc" in ids, (
            f"ses_strsrc should be indexed not skipped. ids={ids}"
        )

        # If there's a tracked_sessions row for this id, the backfill
        # would have written a repaired manifest. Without one, the
        # manifest may be unchanged — but the session is still in the
        # index. Both outcomes satisfy the contract.

    def test_cli_rebuild_index_skips_unparseable_and_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """CLI-side counterpart of the per-session isolation guarantee.

        Same shape as test_reindex_skips_unparseable_manifest_and_
        continues but invoking the `sfs daemon rebuild-index` command
        directly. Uses invalid JSON in the middle session — that's a
        genuinely unrecoverable manifest (no `or`-fallback can rescue
        it), so we get a real skip path through the broadened except.
        """
        from typer.testing import CliRunner

        from sessionfs.cli.main import app

        store_dir = tmp_path / ".sessionfs"
        store_dir.mkdir()
        sessions_dir = store_dir / "sessions"
        sessions_dir.mkdir()

        _make_sfs_session(sessions_dir, "ses_aaa_good")

        bad = sessions_dir / "ses_bbb_bad.sfs"
        bad.mkdir()
        (bad / "manifest.json").write_text("{not valid json,,,")

        _make_sfs_session(sessions_dir, "ses_ccc_good")

        monkeypatch.setattr(
            "sessionfs.cli.common.get_store_dir",
            lambda: store_dir,
        )

        runner = CliRunner()
        result = runner.invoke(app, ["daemon", "rebuild-index"])

        assert result.exit_code == 0, (
            f"exit={result.exit_code}\noutput:\n{result.output}\n"
            f"exc: {result.exception!r}"
        )
        # FORCE_COLOR=1 in CI splits substrings across ANSI escape
        # sequences. Use the shared strip helper before substring
        # asserts — same hazard class as the v0.9.9.8 CI flake the
        # tests/utils/ansi helper was built for.
        from tests.utils.ansi import strip_ansi

        plain = strip_ansi(result.output)
        assert "Skipped ses_bbb_bad.sfs" in plain
        assert "1 skipped" in plain

        from sessionfs.store.local import LocalStore

        ls = LocalStore(store_dir)
        ls.initialize()
        ids = {s["session_id"] for s in ls.list_sessions()}
        ls.close()
        assert ids == {"ses_aaa_good", "ses_ccc_good"}

    def test_reindex_skips_unparseable_manifest_and_continues(
        self, tmp_path: Path
    ) -> None:
        """Same isolation guarantee for the broader exception class.

        Even with the null-safe defaults, future manifest shapes can
        introduce new failure modes (sqlite IntegrityError on a
        required-field violation, TypeError on a non-serializable
        tag value, etc.). The except clause is now `Exception` so
        each session reindexes independently.
        """
        store_dir = tmp_path / ".sessionfs"
        store_dir.mkdir()
        sessions_dir = store_dir / "sessions"
        sessions_dir.mkdir()

        # Good session sorted FIRST.
        _make_sfs_session(sessions_dir, "ses_aaa_good")

        # Broken: manifest.json has invalid JSON (sorted second).
        bad = sessions_dir / "ses_bbb_bad.sfs"
        bad.mkdir()
        (bad / "manifest.json").write_text("{not valid json,,,")

        # Good session sorted LAST — must still be picked up.
        _make_sfs_session(sessions_dir, "ses_ccc_good")

        (store_dir / "index.db").write_bytes(b"corrupted")

        from sessionfs.store.local import LocalStore

        local_store = LocalStore(store_dir)
        local_store.initialize()

        ids = {s["session_id"] for s in local_store.list_sessions()}
        assert ids == {"ses_aaa_good", "ses_ccc_good"}

        local_store.close()


class TestHandleErrors:
    """Test the handle_errors decorator."""

    def test_catches_database_error(self) -> None:
        from sessionfs.cli.common import handle_errors

        @handle_errors
        def bad_db():
            raise sqlite3.DatabaseError("disk I/O error")

        with pytest.raises(SystemExit) as exc_info:
            bad_db()
        assert exc_info.value.code == 1

    def test_catches_keyboard_interrupt(self) -> None:
        from sessionfs.cli.common import handle_errors

        @handle_errors
        def interrupted():
            raise KeyboardInterrupt()

        with pytest.raises(SystemExit) as exc_info:
            interrupted()
        assert exc_info.value.code == 130

    def test_catches_generic_exception(self) -> None:
        from sessionfs.cli.common import handle_errors

        @handle_errors
        def explode():
            raise RuntimeError("something went wrong")

        with pytest.raises(SystemExit) as exc_info:
            explode()
        assert exc_info.value.code == 1

    def test_catches_connection_error(self) -> None:
        from sessionfs.cli.common import handle_errors

        @handle_errors
        def no_net():
            raise ConnectionError("refused")

        with pytest.raises(SystemExit) as exc_info:
            no_net()
        assert exc_info.value.code == 1

    def test_catches_permission_error(self) -> None:
        from sessionfs.cli.common import handle_errors

        @handle_errors
        def no_perm():
            raise PermissionError("access denied")

        with pytest.raises(SystemExit) as exc_info:
            no_perm()
        assert exc_info.value.code == 1

    def test_catches_file_not_found(self) -> None:
        from sessionfs.cli.common import handle_errors

        @handle_errors
        def missing():
            raise FileNotFoundError("config.toml")

        with pytest.raises(SystemExit) as exc_info:
            missing()
        assert exc_info.value.code == 1

    def test_passes_through_system_exit(self) -> None:
        from sessionfs.cli.common import handle_errors

        @handle_errors
        def normal_exit():
            raise SystemExit(0)

        with pytest.raises(SystemExit) as exc_info:
            normal_exit()
        assert exc_info.value.code == 0

    def test_successful_function_returns_value(self) -> None:
        from sessionfs.cli.common import handle_errors

        @handle_errors
        def ok():
            return 42

        assert ok() == 42
