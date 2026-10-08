"""Tests for MCP search index."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sessionfs.mcp.search import SessionSearchIndex


@pytest.fixture
def search_index(tmp_path: Path) -> SessionSearchIndex:
    idx = SessionSearchIndex(tmp_path / "search.db")
    idx.initialize()
    return idx


@pytest.fixture
def sample_session(tmp_path: Path) -> Path:
    """Create a sample .sfs session for indexing."""
    d = tmp_path / "sessions" / "ses_test1234abcdef.sfs"
    d.mkdir(parents=True)

    manifest = {
        "sfs_version": "0.1.0",
        "session_id": "ses_test1234abcdef",
        "title": "Debug auth middleware",
        "created_at": "2026-03-20T10:00:00Z",
        "source": {"tool": "claude-code"},
        "model": {"model_id": "claude-opus-4-6"},
        "stats": {"message_count": 4},
    }
    (d / "manifest.json").write_text(json.dumps(manifest))

    messages = [
        {"role": "user", "content": [{"type": "text", "text": "The /api/users endpoint returns 401 unauthorized"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "I'll check the auth middleware in src/middleware/auth.ts"}]},
        {"role": "assistant", "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "cat src/middleware/auth.ts"}}]},
        {"role": "assistant", "content": [{"type": "text", "text": "The JWT token expiry check has an off-by-one error on line 42."}]},
    ]
    with open(d / "messages.jsonl", "w") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")

    return d


@pytest.fixture
def second_session(tmp_path: Path) -> Path:
    """Create a second session about a different topic."""
    d = tmp_path / "sessions" / "ses_db1234migration.sfs"
    d.mkdir(parents=True)

    (d / "manifest.json").write_text(json.dumps({
        "sfs_version": "0.1.0",
        "session_id": "ses_db1234migration",
        "title": "Database migration for users table",
        "created_at": "2026-03-19T08:00:00Z",
        "source": {"tool": "codex"},
        "model": {"model_id": "gpt-4.1"},
        "stats": {"message_count": 3},
    }))

    messages = [
        {"role": "user", "content": [{"type": "text", "text": "Add a new column 'role' to the users table"}]},
        {"role": "assistant", "content": [{"type": "text", "text": "I'll create an alembic migration for /src/db/migrations/add_role.py"}]},
        {"role": "assistant", "content": [{"type": "tool_result", "content": "Error: relation \"users\" does not exist"}]},
    ]
    with open(d / "messages.jsonl", "w") as f:
        for m in messages:
            f.write(json.dumps(m) + "\n")

    return d


class TestIndexing:
    def test_index_session(self, search_index: SessionSearchIndex, sample_session: Path):
        search_index.index_session("ses_test1234abcdef", sample_session)
        assert search_index.is_indexed("ses_test1234abcdef")

    def test_not_indexed_by_default(self, search_index: SessionSearchIndex):
        assert not search_index.is_indexed("ses_nonexistent")

    def test_reindex_all(self, search_index: SessionSearchIndex, sample_session: Path, tmp_path: Path):
        count = search_index.reindex_all(tmp_path)
        assert count == 1
        assert search_index.is_indexed("ses_test1234abcdef")


class TestSearch:
    def test_keyword_search(self, search_index: SessionSearchIndex, sample_session: Path):
        search_index.index_session("ses_test1234abcdef", sample_session)
        results = search_index.search("401 unauthorized")
        assert len(results) >= 1
        assert results[0]["session_id"] == "ses_test1234abcdef"

    def test_search_by_title(self, search_index: SessionSearchIndex, sample_session: Path):
        search_index.index_session("ses_test1234abcdef", sample_session)
        results = search_index.search("auth middleware")
        assert len(results) >= 1

    def test_search_returns_excerpt(self, search_index: SessionSearchIndex, sample_session: Path):
        search_index.index_session("ses_test1234abcdef", sample_session)
        results = search_index.search("JWT token")
        assert len(results) >= 1
        assert results[0]["excerpt"]  # Should have a text snippet

    def test_search_no_results(self, search_index: SessionSearchIndex, sample_session: Path):
        search_index.index_session("ses_test1234abcdef", sample_session)
        results = search_index.search("kubernetes deployment")
        assert len(results) == 0

    def test_search_tool_filter(
        self, search_index: SessionSearchIndex, sample_session: Path, second_session: Path
    ):
        search_index.index_session("ses_test1234abcdef", sample_session)
        search_index.index_session("ses_db1234migration", second_session)

        # Search for "users" with tool filter
        all_results = search_index.search("users")
        codex_results = search_index.search("users", tool_filter="codex")

        assert len(all_results) >= 2
        assert all(r["source_tool"] == "codex" for r in codex_results)

    def test_search_max_results(self, search_index: SessionSearchIndex, sample_session: Path):
        search_index.index_session("ses_test1234abcdef", sample_session)
        results = search_index.search("auth", limit=1)
        assert len(results) <= 1

    def test_empty_query(self, search_index: SessionSearchIndex):
        results = search_index.search("")
        assert results == []


class TestFindByFile:
    def test_find_by_file_path(self, search_index: SessionSearchIndex, sample_session: Path):
        search_index.index_session("ses_test1234abcdef", sample_session)
        results = search_index.find_by_file("auth.ts")
        assert len(results) >= 1
        assert results[0]["session_id"] == "ses_test1234abcdef"

    def test_find_by_file_no_match(self, search_index: SessionSearchIndex, sample_session: Path):
        search_index.index_session("ses_test1234abcdef", sample_session)
        results = search_index.find_by_file("kubernetes.yaml")
        assert len(results) == 0


class TestFindByError:
    def test_find_by_error(self, search_index: SessionSearchIndex, second_session: Path):
        search_index.index_session("ses_db1234migration", second_session)
        results = search_index.find_by_error("relation does not exist")
        assert len(results) >= 1

    def test_find_by_error_no_match(self, search_index: SessionSearchIndex, second_session: Path):
        search_index.index_session("ses_db1234migration", second_session)
        results = search_index.find_by_error("segmentation fault")
        assert len(results) == 0


def _append_message(sfs_dir: Path, text: str) -> None:
    with open(sfs_dir / "messages.jsonl", "a") as f:
        f.write(json.dumps({"role": "user", "content": [{"type": "text", "text": text}]}) + "\n")


class TestIncrementalReindex:
    def test_unchanged_sessions_are_skipped(
        self, search_index: SessionSearchIndex, sample_session: Path, second_session: Path,
        tmp_path: Path,
    ):
        assert search_index.reindex_all(tmp_path) == 2
        assert search_index.reindex_all(tmp_path) == 0

    def test_changed_session_is_reindexed(
        self, search_index: SessionSearchIndex, sample_session: Path, second_session: Path,
        tmp_path: Path,
    ):
        search_index.reindex_all(tmp_path)
        _append_message(sample_session, "Rotated the zanzibar signing key")

        assert search_index.reindex_all(tmp_path) == 1
        results = search_index.search("zanzibar")
        assert [r["session_id"] for r in results] == ["ses_test1234abcdef"]

    def test_deleted_session_is_removed_from_index(
        self, search_index: SessionSearchIndex, sample_session: Path, second_session: Path,
        tmp_path: Path,
    ):
        import shutil

        search_index.reindex_all(tmp_path)
        shutil.rmtree(second_session)

        search_index.reindex_all(tmp_path)
        assert not search_index.is_indexed("ses_db1234migration")
        assert search_index.search("migration") == []
        assert search_index.is_indexed("ses_test1234abcdef")

    def test_sync_bookkeeping_rewrite_does_not_trigger_reindex(
        self, search_index: SessionSearchIndex, sample_session: Path, tmp_path: Path,
    ):
        search_index.reindex_all(tmp_path)
        manifest_path = sample_session / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["sync"] = {"etag": "abc", "last_sync_at": "2026-10-08T05:30:00+00:00"}
        manifest_path.write_text(json.dumps(manifest, indent=2))

        assert search_index.reindex_all(tmp_path) == 0

    def test_title_change_triggers_reindex(
        self, search_index: SessionSearchIndex, sample_session: Path, tmp_path: Path,
    ):
        search_index.reindex_all(tmp_path)
        manifest_path = sample_session / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["title"] = "Quokka rollout plan"
        manifest_path.write_text(json.dumps(manifest))

        assert search_index.reindex_all(tmp_path) == 1
        assert [r["session_id"] for r in search_index.search("quokka")] == ["ses_test1234abcdef"]

    def test_unreadable_manifest_keeps_entry_and_retries(
        self, search_index: SessionSearchIndex, sample_session: Path, tmp_path: Path,
    ):
        """A manifest caught mid-rewrite must not drop the session from search."""
        search_index.reindex_all(tmp_path)
        manifest_path = sample_session / "manifest.json"
        good = manifest_path.read_text()
        manifest_path.write_text('{"title": "half-writ')

        assert search_index.reindex_all(tmp_path) == 0
        assert search_index.is_indexed("ses_test1234abcdef")
        assert search_index.search("middleware")

        # Once the manifest is whole again the session is re-indexed, even
        # though it is back to exactly the content it was fingerprinted with.
        manifest_path.write_text(good)
        assert search_index.reindex_all(tmp_path) == 1

    def test_same_size_rewrite_with_restored_mtime_is_detected(
        self, search_index: SessionSearchIndex, sample_session: Path, tmp_path: Path,
    ):
        """Archive unpacks restore mtimes, so size+mtime alone would miss this."""
        import os

        search_index.reindex_all(tmp_path)
        messages = sample_session / "messages.jsonl"
        st = messages.stat()
        content = messages.read_text()
        assert "endpoint" in content
        messages.write_text(content.replace("endpoint", "wombatxx"))  # same length
        os.utime(messages, ns=(st.st_atime_ns, st.st_mtime_ns))
        assert messages.stat().st_size == st.st_size
        assert messages.stat().st_mtime_ns == st.st_mtime_ns

        assert search_index.reindex_all(tmp_path) == 1
        assert search_index.search("wombatxx")

    def test_read_error_keeps_last_good_entry(
        self, search_index: SessionSearchIndex, sample_session: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import builtins

        search_index.reindex_all(tmp_path)
        _append_message(sample_session, "new text")  # forces a re-read
        real_open = builtins.open

        def failing_open(file, *args, **kwargs):
            if str(file).endswith("messages.jsonl"):
                raise PermissionError("transient")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", failing_open)
        assert search_index.reindex_all(tmp_path) == 0
        monkeypatch.setattr(builtins, "open", real_open)
        assert search_index.search("middleware")  # previous content still served

    def test_manifest_unreadable_on_two_passes_is_removed(
        self, search_index: SessionSearchIndex, sample_session: Path, tmp_path: Path,
    ):
        search_index.reindex_all(tmp_path)
        (sample_session / "manifest.json").write_text("not json")
        search_index.reindex_all(tmp_path)
        assert search_index.is_indexed("ses_test1234abcdef")  # one grace pass
        search_index.reindex_all(tmp_path)
        assert not search_index.is_indexed("ses_test1234abcdef")

    def test_forced_rebuild_removes_unreadable_immediately(
        self, search_index: SessionSearchIndex, sample_session: Path, tmp_path: Path,
    ):
        search_index.reindex_all(tmp_path)
        (sample_session / "manifest.json").write_text("not json")
        search_index.reindex_all(tmp_path, force=True)
        assert not search_index.is_indexed("ses_test1234abcdef")

    def test_transient_read_error_is_retried(
        self, search_index: SessionSearchIndex, sample_session: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import builtins

        real_open = builtins.open

        def failing_open(file, *args, **kwargs):
            if str(file).endswith("messages.jsonl"):
                raise PermissionError("transient")
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", failing_open)
        search_index.reindex_all(tmp_path)
        monkeypatch.setattr(builtins, "open", real_open)

        # Nothing about the files changed, but the earlier read was incomplete.
        assert search_index.reindex_all(tmp_path) == 1
        assert search_index.search("middleware")

    def test_malformed_session_does_not_stop_the_pass(
        self, search_index: SessionSearchIndex, sample_session: Path, tmp_path: Path,
    ):
        hostile = tmp_path / "sessions" / "ses_0000hostile0000.sfs"
        hostile.mkdir(parents=True)
        (hostile / "manifest.json").write_text(json.dumps({
            "title": {"nested": "dict"},
            "source": "not-a-dict",
            "model": ["list"],
            "stats": {"message_count": {"x": 1}},
            "created_at": ["2026"],
        }))
        (hostile / "workspace.json").write_text(json.dumps(["not", "a", "dict"]))
        (hostile / "messages.jsonl").write_text('"just a string"\n42\n[1, 2]\n')
        deep = tmp_path / "sessions" / "ses_0001deepnesting.sfs"
        deep.mkdir(parents=True)
        (deep / "manifest.json").write_text("[" * 100000 + "]" * 100000)

        assert search_index.reindex_all(tmp_path) == 2
        assert search_index.is_indexed("ses_test1234abcdef")
        assert search_index.is_indexed("ses_0000hostile0000")
        assert search_index.search("middleware")

    def test_session_whose_directory_disappears_leaves_search(
        self, search_index: SessionSearchIndex, sample_session: Path, tmp_path: Path,
    ):
        import shutil

        search_index.reindex_all(tmp_path)
        shutil.rmtree(sample_session)
        search_index.reindex_all(tmp_path)
        assert not search_index.is_indexed("ses_test1234abcdef")

    def test_force_reindexes_everything(
        self, search_index: SessionSearchIndex, sample_session: Path, second_session: Path,
        tmp_path: Path,
    ):
        search_index.reindex_all(tmp_path)
        assert search_index.reindex_all(tmp_path, force=True) == 2

    def test_session_without_manifest_is_not_counted_as_indexed(
        self, search_index: SessionSearchIndex, tmp_path: Path,
    ):
        broken = tmp_path / "sessions" / "ses_nomanifest00000.sfs"
        broken.mkdir(parents=True)
        search_index.reindex_all(tmp_path)
        assert not search_index.is_indexed("ses_nomanifest00000")

    def test_index_built_before_fingerprints_is_upgraded(
        self, sample_session: Path, tmp_path: Path,
    ):
        import sqlite3

        db = tmp_path / "legacy.db"
        conn = sqlite3.connect(str(db))
        conn.executescript(
            """
            CREATE VIRTUAL TABLE session_search USING fts5(
                session_id UNINDEXED, title, source_tool UNINDEXED,
                model_id UNINDEXED, project_path, messages_text, file_paths,
                error_messages, created_at UNINDEXED, message_count UNINDEXED
            );
            CREATE TABLE search_meta (
                session_id TEXT PRIMARY KEY,
                indexed_at TEXT NOT NULL,
                message_count INTEGER DEFAULT 0
            );
            INSERT INTO search_meta (session_id, indexed_at, message_count)
                VALUES ('ses_test1234abcdef', '2026-01-01T00:00:00+00:00', 4);
            """
        )
        conn.commit()
        conn.close()

        idx = SessionSearchIndex(db)
        idx.initialize()
        # Legacy rows have no fingerprint, so they are re-indexed once...
        assert idx.reindex_all(tmp_path) == 1
        # ...and skipped from then on.
        assert idx.reindex_all(tmp_path) == 0
        assert idx.search("middleware")
        idx.close()


class TestPersistentFailures:
    def test_failing_session_is_retried_only_after_it_changes(
        self, search_index: SessionSearchIndex, sample_session: Path, tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        calls = {"n": 0}
        real = SessionSearchIndex.index_session

        def failing(self, session_id, sfs_dir, **kwargs):
            calls["n"] += 1
            raise RuntimeError("always fails")

        monkeypatch.setattr(SessionSearchIndex, "index_session", failing)
        search_index.reindex_all(tmp_path)
        search_index.reindex_all(tmp_path)
        assert calls["n"] == 1  # not re-read while unchanged

        monkeypatch.setattr(SessionSearchIndex, "index_session", real)
        _append_message(sample_session, "now fixed")
        assert search_index.reindex_all(tmp_path) == 1
        assert search_index.search("middleware")

    def test_message_shapes_that_used_to_escape_are_indexed(
        self, search_index: SessionSearchIndex, sample_session: Path, tmp_path: Path,
    ):
        with open(sample_session / "messages.jsonl", "ab") as f:
            f.write(b'{"role": "user", "content": null}\n')
            f.write(b'{"role": "user", "content": [{"type": "text", "text": {"x": 1}}]}\n')
            f.write(b'{"role": "user", "content": [{"type": "tool_use", "name": 5,'
                    b' "input": {"command": ["ls"]}}]}\n')
            f.write(b'{"role": "user", "content": "caf\xe9 kangaroo"}\n')  # invalid UTF-8
        assert search_index.reindex_all(tmp_path) == 1
        assert search_index.search("kangaroo")
        assert search_index.search("middleware")

    def test_session_without_usable_manifest_is_not_reread(
        self, search_index: SessionSearchIndex, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        bad = tmp_path / "sessions" / "ses_badmanifest00000.sfs"
        bad.mkdir(parents=True)
        (bad / "manifest.json").write_text("{ not json")
        reads = {"n": 0}
        real = SessionSearchIndex.index_session

        def counting(self, *args, **kwargs):
            reads["n"] += 1
            return real(self, *args, **kwargs)

        monkeypatch.setattr(SessionSearchIndex, "index_session", counting)
        search_index.reindex_all(tmp_path)
        search_index.reindex_all(tmp_path)
        assert reads["n"] == 1

        (bad / "manifest.json").write_text(json.dumps({"title": "Wallaby fixed"}))
        search_index.reindex_all(tmp_path)
        assert search_index.search("wallaby")

    def test_stopped_pass_ends_early_and_does_not_prune(
        self, search_index: SessionSearchIndex, sample_session: Path, second_session: Path,
        tmp_path: Path,
    ):
        import shutil
        import threading

        search_index.reindex_all(tmp_path)
        shutil.rmtree(second_session)
        stop = threading.Event()
        stop.set()
        assert search_index.reindex_all(tmp_path, stop=stop) == 0
        assert search_index.is_indexed("ses_db1234migration")  # not pruned

    def test_legacy_row_still_gets_its_grace_pass(
        self, search_index: SessionSearchIndex, sample_session: Path, tmp_path: Path,
    ):
        search_index.reindex_all(tmp_path)
        search_index.conn.execute("UPDATE search_meta SET fingerprint = NULL")
        search_index.conn.commit()
        (sample_session / "manifest.json").write_text("not json")

        search_index.reindex_all(tmp_path)
        assert search_index.is_indexed("ses_test1234abcdef")


class TestConcurrentUpgrade:
    def test_column_added_by_another_process_is_tolerated(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """Two servers upgrading the same legacy index at once must both start."""
        import sqlite3

        db = tmp_path / "search.db"
        first = SessionSearchIndex(db)
        first.initialize()  # creates the current schema, column included
        first.close()

        # Simulate the race: this process saw no column, then lost to another.
        real_connect = sqlite3.connect

        class StaleView(sqlite3.Connection):
            def execute(self, sql, *args):  # type: ignore[override]
                if sql.startswith("PRAGMA table_info(search_meta)"):
                    return super().execute(
                        "SELECT 'session_id' AS name UNION ALL SELECT 'indexed_at'"
                    )
                return super().execute(sql, *args)

        monkeypatch.setattr(
            sqlite3, "connect", lambda *a, **k: real_connect(*a, factory=StaleView, **k)
        )
        second = SessionSearchIndex(db)
        second.initialize()  # must not raise "duplicate column name"
        second.close()


class TestServerStartup:
    def test_init_server_does_not_wait_for_indexing(
        self, sample_session: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        """The server must be able to answer before the index is built."""
        import threading

        from sessionfs.mcp import server

        started = threading.Event()
        release = threading.Event()
        real_reindex = SessionSearchIndex.reindex_all

        def slow_reindex(self, store_dir, **kwargs):
            started.set()
            assert release.wait(10)
            return real_reindex(self, store_dir, **kwargs)

        monkeypatch.setattr(SessionSearchIndex, "reindex_all", slow_reindex)
        monkeypatch.setattr(server, "_store", None)
        monkeypatch.setattr(server, "_search", None)

        stop = threading.Event()
        threads: list[threading.Thread] = []
        real_start = server._start_background_reindex

        def start(*args, **kwargs):
            threads.append(real_start(*args, **{**kwargs, "stop": stop}))
            return threads[-1]

        monkeypatch.setattr(server, "_start_background_reindex", start)

        server.init_server(tmp_path)  # returns while indexing is still blocked
        assert started.wait(5)
        assert threads[0].is_alive()
        assert server._get_search().search("middleware") == []

        release.set()
        for _ in range(100):
            if server._get_search().search("middleware"):
                break
            threading.Event().wait(0.1)
        assert server._get_search().search("middleware")
        stop.set()
        threads[0].join(10)
        assert not threads[0].is_alive()

    def test_periodic_pass_picks_up_new_sessions_and_survives_errors(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        import threading

        from sessionfs.mcp import server

        (tmp_path / "sessions").mkdir()
        calls = {"n": 0}
        real_reindex = SessionSearchIndex.reindex_all

        def flaky(self, store_dir, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("first pass fails")
            return real_reindex(self, store_dir, **kwargs)

        monkeypatch.setattr(SessionSearchIndex, "reindex_all", flaky)
        stop = threading.Event()
        thread = server._start_background_reindex(
            tmp_path, tmp_path / "search.db", interval=0.05, stop=stop
        )
        # A session captured while the server is running...
        d = tmp_path / "sessions" / "ses_live0000capture.sfs"
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps({"title": "Platypus incident review"}))

        reader = SessionSearchIndex(tmp_path / "search.db")
        reader.initialize()
        for _ in range(100):
            if reader.search("platypus"):
                break
            threading.Event().wait(0.05)
        # ...is picked up by a later pass, even after an earlier pass failed.
        assert reader.search("platypus")
        assert calls["n"] >= 2
        stop.set()
        thread.join(5)
        assert not thread.is_alive()
        reader.close()

    def test_stop_background_reindex_ends_the_thread(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        from sessionfs.mcp import server

        monkeypatch.setattr(server, "_store", None)
        monkeypatch.setattr(server, "_search", None)
        server.init_server(tmp_path)
        thread = server._reindex_thread
        assert thread is not None and thread.is_alive()

        server.stop_background_reindex()
        assert not thread.is_alive()
        assert server._reindex_thread is None

    def test_slow_indexer_is_waited_for_not_doubled(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ):
        import threading

        from sessionfs.mcp import server

        (tmp_path / "sessions").mkdir()
        release = threading.Event()
        started = threading.Event()
        running = {"now": 0, "max": 0}
        lock = threading.Lock()

        def slow(self, store_dir, **kwargs):
            with lock:
                running["now"] += 1
                running["max"] = max(running["max"], running["now"])
            started.set()
            release.wait(10)
            with lock:
                running["now"] -= 1
            return 0

        monkeypatch.setattr(SessionSearchIndex, "reindex_all", slow)
        monkeypatch.setattr(server, "_store", None)
        monkeypatch.setattr(server, "_search", None)
        server.init_server(tmp_path)
        assert started.wait(5)

        server.stop_background_reindex(timeout=0.1)  # times out mid-session
        assert server._reindex_thread is not None  # still referenced

        threading.Timer(0.3, release.set).start()
        server.init_server(tmp_path)  # waits for the old indexer first
        server.stop_background_reindex()
        assert running["max"] == 1

    def test_background_indexing_failure_is_logged_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
    ):
        from sessionfs.mcp import server

        def boom(self, store_dir, **kwargs):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(SessionSearchIndex, "reindex_all", boom)
        (tmp_path / "sessions").mkdir()
        with caplog.at_level("WARNING", logger="sessionfs.mcp"):
            thread = server._start_background_reindex(tmp_path, tmp_path / "search.db")
            thread.join(10)
        assert "Background search indexing failed" in caplog.text
