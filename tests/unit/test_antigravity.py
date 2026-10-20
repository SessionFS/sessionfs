"""Tests for Google Antigravity capture (desktop app, agy CLI, IDE build).

The fixtures under tests/fixtures/antigravity are synthetic: their shapes
mirror real Antigravity 2.x / agy samples (step schema, user-content
wrappers, GENERIC tool-result text, chunked transcripts, the
conversation_summaries table), but none of the text comes from a real
conversation. ``__WORKSPACE__`` / ``__GEMINI__`` are replaced with temp
paths when the tree is materialized.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
from pathlib import Path
from typing import Any

import pytest

from sessionfs.converters.antigravity_to_sfs import (
    SummaryIndex,
    conversation_fingerprint,
    conversation_id_from_path,
    convert_antigravity_to_sfs,
    derive_workspace_root,
    discover_antigravity_sessions,
    discover_unreadable_conversations,
    load_steps,
    parse_antigravity_conversation,
    read_summaries_db,
    sfs_session_id_for,
    top_level_conversation,
)

FIXTURES = Path(__file__).parent.parent / "fixtures" / "antigravity"

A = "11111111-aaaa-4bbb-8ccc-000000000001"  # desktop, outside-of-project, chunked
B = "22222222-bbbb-4ccc-8ddd-000000000002"  # agy CLI, workspace_uris set
C = "44444444-dddd-4eee-8fff-000000000004"  # agy CLI, transcript.jsonl only
D = "55555555-eeee-4fff-8aaa-000000000005"  # desktop subagent of A
PB = "33333333-cccc-4ddd-8eee-000000000003"  # encrypted .pb only

_COLUMNS = (
    "conversation_id TEXT, title TEXT, preview TEXT, step_count INTEGER, "
    "last_modified_time datetime, workspace_uris TEXT, status TEXT, source TEXT, "
    "project_id TEXT, agent_name TEXT, parent_conversation_id TEXT, nesting_depth INTEGER, "
    "battle_id TEXT, winning_conversation_id TEXT, not_fully_idle numeric, killed numeric, "
    "last_user_input_time datetime, last_user_input_step_index INTEGER, app_data_dir TEXT, "
    "raw_summary BLOB, group_id TEXT"
)


class AgTree:
    def __init__(self, gemini: Path, workspace: Path, conns: list[sqlite3.Connection]):
        self.gemini = gemini
        self.workspace = workspace
        self.conns = conns

    def conv(self, app: str, cid: str) -> Path:
        return self.gemini / app / "brain" / cid


def _write_db(path: Path, rows: list[dict[str, Any]], *, wal: bool) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), check_same_thread=False)
    if wal:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute(f"CREATE TABLE conversation_summaries ({_COLUMNS})")
    for row in rows:
        cols = ", ".join(row)
        marks = ", ".join("?" for _ in row)
        conn.execute(f"INSERT INTO conversation_summaries ({cols}) VALUES ({marks})", list(row.values()))
    conn.commit()
    return conn


@pytest.fixture
def ag(tmp_path: Path):
    """Materialize the fixture tree under a temp ~/.gemini with a git workspace."""
    workspace = tmp_path / "work" / "demo-repo"
    (workspace / ".git").mkdir(parents=True)
    (workspace / "README.md").write_text("# Demo\n\nSynthetic fixture.\n")
    (workspace / "notes").mkdir()
    gemini = tmp_path / "home" / ".gemini"
    gemini.mkdir(parents=True)
    for app in ("antigravity", "antigravity-cli"):
        shutil.copytree(FIXTURES / app, gemini / app)
    for path in gemini.rglob("*"):
        if path.is_file() and path.suffix in (".jsonl", ".txt", ".pbtxt"):
            text = path.read_text()
            text = text.replace("__WORKSPACE__", str(workspace)).replace("__GEMINI__", str(gemini))
            path.write_text(text)
    raw = (FIXTURES / "summaries.json").read_text()
    raw = raw.replace("__WORKSPACE__", str(workspace))
    summaries = json.loads(raw)
    conns = [
        # Desktop DB in WAL mode with the rows still only in the -wal file
        # (the connection stays open, so nothing is checkpointed).
        _write_db(gemini / "antigravity" / "conversation_summaries.db", summaries["antigravity"], wal=True),
        _write_db(gemini / "antigravity-cli" / "conversation_summaries.db", summaries["antigravity-cli"], wal=False),
    ]
    tree = AgTree(gemini, workspace, conns)
    yield tree
    for conn in conns:
        conn.close()


def _blocks(messages: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [b for m in messages for b in m["content"] if b.get("type") == kind]


# ---------------------------------------------------------------------------
# Step loading
# ---------------------------------------------------------------------------


class TestLoadSteps:
    def test_chunks_and_top_level_assembled_once_in_order(self, ag: AgTree):
        loaded = load_steps(ag.conv("antigravity", A))
        indices = [s["step_index"] for s in loaded.steps]
        assert indices == list(range(21))  # 13-20 appear in a chunk AND top-level
        # The half-written final line is skipped, not fatal.
        assert len(loaded.parse_errors) == 1

    def test_top_level_only_when_no_chunks(self, ag: AgTree):
        conv = ag.conv("antigravity", D)
        assert [s["step_index"] for s in load_steps(conv).steps] == [0, 1]

    def test_chunk_only_when_top_level_missing(self, ag: AgTree):
        conv = ag.conv("antigravity", A)
        (conv / ".system_generated" / "logs" / "transcript_full.jsonl").unlink()
        assert [s["step_index"] for s in load_steps(conv).steps] == list(range(21))

    def test_stringified_transcript_used_only_as_fallback(self, ag: AgTree):
        steps = load_steps(ag.conv("antigravity-cli", C)).steps
        assert len(steps) == 3
        # B has both families: transcript_full wins (typed args).
        b_steps = load_steps(ag.conv("antigravity-cli", B)).steps
        assert isinstance(b_steps[1]["tool_calls"][0]["args"], dict)
        assert len(b_steps) == 4


# ---------------------------------------------------------------------------
# Conversion
# ---------------------------------------------------------------------------


class TestParseDesktopConversation:
    @pytest.fixture
    def session(self, ag: AgTree):
        return parse_antigravity_conversation(ag.conv("antigravity", A))

    def test_user_wrappers_stripped(self, session):
        users = [m for m in session.messages if m["role"] == "user" and not m.get("is_sidechain")]
        texts = [m["content"][0]["text"] for m in users]
        assert texts[0] == "hello there"
        assert texts[2] == "write a note file"
        for t in texts:
            assert "<ADDITIONAL_METADATA>" not in t and "<USER_SETTINGS_CHANGE>" not in t
        assert users[1]["metadata"]["mentions"]
        assert session.turn_count == 3

    def test_model_from_settings_change(self, session):
        assert session.model_id == "Gemini 3.8 Flash"
        assert session.reasoning_effort == "high"
        assistants = [m for m in session.messages if m["role"] == "assistant" and not m.get("is_sidechain")]
        assert all(m.get("model") == "Gemini 3.8 Flash" for m in assistants)
        assert all(m.get("provider") == "google" for m in assistants)

    def test_thinking_text_and_tool_use(self, session):
        assert _blocks(session.messages, "thinking")
        uses = _blocks(session.messages, "tool_use")
        assert [u["name"] for u in uses].count("run_command") == 3
        assert {"view_file", "write_to_file", "ask_question"} <= {u["name"] for u in uses}
        assert session.tool_use_count == len(uses) == 7
        assert isinstance(uses[0]["input"], dict) and "CommandLine" in uses[0]["input"]

    def test_every_tool_result_links_to_a_preceding_call(self, session):
        seen: set[str] = set()
        for msg in session.messages:
            for block in msg["content"]:
                if block["type"] == "tool_use":
                    seen.add(block["tool_use_id"])
                elif block["type"] == "tool_result":
                    assert block["tool_use_id"] in seen
        assert len(_blocks(session.messages, "tool_result")) == 7

    def test_tool_result_fields(self, session):
        results = _blocks(session.messages, "tool_result")
        ok = results[0]
        assert ok["exit_code"] == 0 and ok["is_error"] is False
        assert ok["duration_ms"] == 1000
        assert not ok["content"].startswith("Created At")
        background = results[1]
        assert "background task" in background["content"] and background["is_error"] is False
        failed = results[2]
        assert failed["exit_code"] == 2 and failed["is_error"] is True
        # steps/12/output.txt is longer than the transcript copy -> used.
        assert "Synthetic fixture." in results[3]["content"]
        step_error = results[4]
        assert step_error["is_error"] is True
        assert step_error["content"].startswith("Encountered error in step execution")

    def test_system_message_keeps_inner_block(self, session):
        system = [m for m in session.messages if m["role"] == "system"]
        assert len(system) == 1 and system[0]["is_meta"] is True
        text = system[0]["content"][0]["text"]
        assert text.startswith("[Message]") and "not actually sent" not in text

    def test_tokens_and_usage(self, session):
        assert session.total_input_tokens > 0 and session.total_output_tokens > 0
        with_cache = [m for m in session.messages if m.get("usage", {}).get("cache_read_tokens")]
        assert with_cache

    def test_title_prefers_owning_app_row_over_stale_copy(self, session):
        assert session.title == "Greeting And Files"
        assert session.metadata["app_data_dir"] == "antigravity"
        assert session.metadata["project_id"] == "outside-of-project"

    def test_message_ids_unique_and_deterministic(self, ag: AgTree, session):
        ids = [m["msg_id"] for m in session.messages]
        assert len(ids) == len(set(ids))
        again = parse_antigravity_conversation(ag.conv("antigravity", A))
        assert [m["msg_id"] for m in again.messages] == ids


class TestWorkspace:
    def test_derived_from_tool_paths_git_root_scratch_ignored(self, ag: AgTree):
        session = parse_antigravity_conversation(ag.conv("antigravity", A))
        assert session.workspace_root == str(ag.workspace)
        assert session.workspace_source == "derived"
        refs = {r["path"]: r["role"] for r in session.file_refs}
        assert refs == {"README.md": "read", "notes/hello.txt": "created"}
        assert all(not p.startswith("/") for p in refs)

    def test_workspace_uris_used_when_present(self, ag: AgTree):
        session = parse_antigravity_conversation(ag.conv("antigravity-cli", B))
        assert session.workspace_root == str(ag.workspace)
        assert session.workspace_source == "workspace_uris"
        assert session.file_refs == [{"path": "agy.txt", "role": "written"}]
        assert session.metadata["app_data_dir"] == "antigravity-cli"

    def test_never_home_or_filesystem_root(self, tmp_path: Path):
        home = tmp_path / "home"
        (home / "a").mkdir(parents=True)
        (home / "b").mkdir()
        uses = [("view_file", {"AbsolutePath": str(home / "a" / "x.txt")}),
                ("view_file", {"AbsolutePath": str(home / "b" / "y.txt")})]
        assert derive_workspace_root(uses, [], exclude=[], home=home) is None
        uses = [("run_command", {"Cwd": "/"})]
        assert derive_workspace_root(uses, [], exclude=[], home=home) is None

    def test_no_paths_means_no_workspace(self, tmp_path: Path):
        assert derive_workspace_root([], [], exclude=[], home=tmp_path) is None


class TestSubagents:
    def test_child_folded_into_parent_as_sidechain(self, ag: AgTree):
        session = parse_antigravity_conversation(ag.conv("antigravity", A))
        side = [m for m in session.messages if m.get("is_sidechain")]
        assert len(side) == 2
        assert {m["agent_id"] for m in side} == {D}
        assert session.sub_agents == [{"agent_id": D, "message_count": 2, "model": "Gemini 3.8 Flash"}]
        main_ids = {m["msg_id"] for m in session.messages if not m.get("is_sidechain")}
        assert not main_ids & {m["msg_id"] for m in side}

    def test_child_resolves_to_parent_session(self, ag: AgTree):
        summaries = SummaryIndex().load(ag.gemini)
        resolved = top_level_conversation(D, "antigravity", summaries, ag.gemini)
        assert resolved is not None and resolved[0] == A


class TestConvert:
    def _convert(self, ag: AgTree, app: str, cid: str, tmp_path: Path) -> Path:
        session = parse_antigravity_conversation(ag.conv(app, cid))
        out = tmp_path / "out" / cid
        convert_antigravity_to_sfs(session, out, session_id="ses_" + cid.replace("-", "")[:16])
        return out

    @pytest.mark.parametrize("app,cid", [("antigravity", A), ("antigravity-cli", B), ("antigravity-cli", C)])
    def test_output_validates_against_sfs_schema(self, ag: AgTree, tmp_path: Path, app: str, cid: str):
        from sessionfs.spec.validate import validate_session

        out = self._convert(ag, app, cid, tmp_path)
        result = validate_session(out)
        assert result.errors == []
        assert result.warnings == []

    def test_manifest_shape(self, ag: AgTree, tmp_path: Path):
        out = self._convert(ag, "antigravity-cli", B, tmp_path)
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["source"]["tool"] == "antigravity"
        assert manifest["source"]["interface"] == "cli"
        assert manifest["source"]["original_session_id"] == B
        assert manifest["title"] == "Create File Agy.txt In Pwd"
        assert manifest["model"] == {"provider": "google", "model_id": "Gemini 3.8 Flash",
                                     "reasoning_effort": "high"}
        assert manifest["metadata"]["antigravity"]["app_data_dir"] == "antigravity-cli"
        workspace = json.loads((out / "workspace.json").read_text())
        assert workspace["root_path"] == str(ag.workspace)
        assert workspace["files"] == [{"path": "agy.txt", "role": "written"}]
        tools = json.loads((out / "tools.json").read_text())
        assert tools["tools_used"] == ["write_to_file"]

    def test_desktop_interface_and_slash_free(self, ag: AgTree, tmp_path: Path):
        out = self._convert(ag, "antigravity", A, tmp_path)
        manifest = json.loads((out / "manifest.json").read_text())
        assert manifest["source"]["interface"] == "desktop"
        assert manifest["stats"]["message_count"] == len((out / "messages.jsonl").read_text().splitlines())
        assert manifest["sub_agents"][0]["agent_id"] == D


# ---------------------------------------------------------------------------
# Discovery + metadata
# ---------------------------------------------------------------------------


class TestDiscovery:
    def test_top_level_conversations_across_roots(self, ag: AgTree):
        found = {s["session_id"]: s for s in discover_antigravity_sessions(ag.gemini)}
        assert set(found) == {A, B, C}  # D folded into A
        assert found[A]["app_data_dir"] == "antigravity"
        assert found[B]["app_data_dir"] == "antigravity-cli"
        assert found[B]["project_path"] == str(ag.workspace)
        assert all(s["tool"] == "antigravity" for s in found.values())

    def test_parent_fingerprint_covers_subagent(self, ag: AgTree):
        before = {s["session_id"]: s for s in discover_antigravity_sessions(ag.gemini)}[A]
        child_log = ag.conv("antigravity", D) / ".system_generated" / "logs" / "transcript_full.jsonl"
        with open(child_log, "a") as f:
            f.write(json.dumps({"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE",
                                "created_at": "2026-10-18T04:10:50Z", "content": "More."}) + "\n")
        after = {s["session_id"]: s for s in discover_antigravity_sessions(ag.gemini)}[A]
        assert after["size_bytes"] > before["size_bytes"]

    def test_duplicate_conversation_dir_picks_owning_root(self, ag: AgTree):
        shutil.copytree(ag.conv("antigravity", A), ag.conv("antigravity-cli", A))
        found = {s["session_id"]: s for s in discover_antigravity_sessions(ag.gemini)}
        assert found[A]["app_data_dir"] == "antigravity"

    def test_encrypted_only_conversations_reported(self, ag: AgTree):
        assert discover_unreadable_conversations(ag.gemini) == {PB: "encrypted"}

    def test_conversation_id_from_path(self, ag: AgTree):
        path = ag.conv("antigravity-cli", B) / ".system_generated" / "logs" / "transcript_full.jsonl"
        root, cid = conversation_id_from_path(path)
        assert cid == B and root == ag.gemini / "antigravity-cli"
        assert conversation_id_from_path(ag.gemini / "antigravity" / "conversation_summaries.db") is None


class TestSummariesDb:
    def test_reads_rows_still_in_wal_without_touching_live_files(self, ag: AgTree):
        root = ag.gemini / "antigravity"
        wal = root / "conversation_summaries.db-wal"
        assert wal.exists() and wal.stat().st_size > 0
        before = {p.name: p.stat().st_mtime_ns for p in root.iterdir() if p.is_file()}
        rows = read_summaries_db(root / "conversation_summaries.db")
        assert {r["conversation_id"] for r in rows} == {A, D}
        after = {p.name: p.stat().st_mtime_ns for p in root.iterdir() if p.is_file()}
        assert before == after  # no -shm/-journal created, nothing modified

    def test_missing_db_and_missing_columns_tolerated(self, tmp_path: Path):
        assert read_summaries_db(tmp_path / "nope.db") == []
        db = tmp_path / "odd.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE conversation_summaries (conversation_id TEXT, title TEXT)")
        conn.execute("INSERT INTO conversation_summaries VALUES ('x', 'T')")
        conn.commit()
        conn.close()
        assert read_summaries_db(db) == [{"conversation_id": "x", "title": "T"}]

    def test_parse_works_without_any_summaries_db(self, ag: AgTree):
        for conn in ag.conns:
            conn.close()
        ag.conns.clear()
        for app in ("antigravity", "antigravity-cli"):
            for p in (ag.gemini / app).glob("conversation_summaries.db*"):
                p.unlink()
        session = parse_antigravity_conversation(ag.conv("antigravity", A))
        assert session.title == "Greeting And Files"  # from annotations/<cid>.pbtxt
        assert session.workspace_root == str(ag.workspace)
        assert not any(m.get("is_sidechain") for m in session.messages)


class TestSettingsOnlyInput:
    def test_settings_change_without_request_is_meta(self, tmp_path: Path):
        conv = tmp_path / ".gemini" / "antigravity" / "brain" / "c0ffee00-0000-4000-8000-000000000000"
        logs = conv / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        step = {
            "step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": "2026-10-18T05:00:00Z",
            "content": "<USER_SETTINGS_CHANGE>\nThe user changed setting `Model Selection` from Gemini 3.8 Flash "
                       "(High) to Claude Sonnet 4.5 (Thinking). No need to comment.\n</USER_SETTINGS_CHANGE>",
        }
        (logs / "transcript_full.jsonl").write_text(json.dumps(step) + "\n")
        session = parse_antigravity_conversation(conv)
        assert session.messages[0]["role"] == "system" and session.messages[0]["is_meta"] is True
        assert session.model_id == "Claude Sonnet 4.5 (Thinking)"
        from sessionfs.converters.antigravity_to_sfs import _provider_for

        assert _provider_for(session.model_id) == "anthropic"


# ---------------------------------------------------------------------------
# Watcher
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path):
    from sessionfs.store.local import LocalStore

    s = LocalStore(tmp_path / "store")
    s.initialize()
    yield s
    s.close()


def _watcher(ag: AgTree, store):
    from sessionfs.daemon.config import AntigravityWatcherConfig
    from sessionfs.watchers.antigravity import AntigravityWatcher

    return AntigravityWatcher(AntigravityWatcherConfig(enabled=True, gemini_dir=ag.gemini), store, scan_interval=0)


class TestWatcher:
    def test_full_scan_captures_every_top_level_conversation(self, ag: AgTree, store):
        w = _watcher(ag, store)
        w.full_scan()
        status = w.get_status()
        assert status.health == "healthy"
        assert status.sessions_tracked == 3
        assert status.sessions_skipped == {"encrypted": 1}
        for cid in (A, B, C):
            ref = store.get_tracked_session(cid)
            assert ref is not None and ref.tool == "antigravity"
            manifest = json.loads((store.get_session_dir(ref.sfs_session_id) / "manifest.json").read_text())
            assert manifest["source"]["tool"] == "antigravity"
        assert store.get_tracked_session(D) is None  # captured inside A

    def test_unchanged_conversations_are_not_reconverted(self, ag: AgTree, store, monkeypatch):
        w = _watcher(ag, store)
        w.full_scan()
        calls: list[str] = []
        monkeypatch.setattr(w, "_capture_session", lambda cid, *a, **k: calls.append(cid))
        w.full_scan()
        assert calls == []

    def test_corrupt_manifest_fields_do_not_abort_scan(self, ag: AgTree, store):
        from sessionfs.watchers.antigravity import AntigravityWatcher, _dict, _list

        # Truthy non-dict manifest fields must degrade to empty on read, not
        # raise AttributeError (mirrors the write-path guard in store/index.py).
        assert _dict("x") == {}
        assert _dict(["codex"]) == {}
        assert _dict({"a": 1}) == {"a": 1}
        # Truthy non-list sub_agents must degrade to empty, not raise TypeError:
        # an iterable string reaches the element-level isinstance(dict) guard,
        # but a non-iterable int/bool would raise before it.
        assert _list("bad") == []
        assert _list(5) == []
        assert _list(True) == []
        assert _list([{"agent_id": "x"}]) == [{"agent_id": "x"}]
        assert AntigravityWatcher._thread_counts({"stats": "x", "sub_agents": "bad"}) == (None, {})
        assert AntigravityWatcher._thread_counts({"stats": "x", "sub_agents": 5}) == (None, {})
        assert AntigravityWatcher._thread_counts({"stats": "x", "sub_agents": True}) == (None, {})

        w = _watcher(ag, store)
        w.full_scan()
        assert w.get_status().health == "healthy"

        # Corrupt the stored manifest of B with truthy non-dict fields; a fresh
        # watcher re-reads it (empty digest cache) on the next scan.
        b_sfs = store.get_tracked_session(B).sfs_session_id
        manifest_path = store.get_session_dir(b_sfs) / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["metadata"] = "bad"
        manifest["stats"] = "x"
        manifest["source"] = ["codex"]
        manifest_path.write_text(json.dumps(manifest))

        w2 = _watcher(ag, store)
        w2.full_scan()
        assert w2.get_status().health == "healthy"
        # B was recaptured and its manifest repaired, and every other session
        # is still tracked (the scan did not abort partway through).
        assert store.get_tracked_session(C) is not None
        repaired = json.loads(manifest_path.read_text())
        assert isinstance(repaired["metadata"], dict)
        assert isinstance(repaired["stats"], dict)
        assert isinstance(repaired["source"], dict)

    def test_event_on_subagent_recaptures_parent(self, ag: AgTree, store):
        w = _watcher(ag, store)
        w.full_scan()
        sfs_id = store.get_tracked_session(A).sfs_session_id
        before = json.loads((store.get_session_dir(sfs_id) / "manifest.json").read_text())
        child_log = ag.conv("antigravity", D) / ".system_generated" / "logs" / "transcript_full.jsonl"
        with open(child_log, "a") as f:
            f.write(json.dumps({"step_index": 2, "source": "MODEL", "type": "PLANNER_RESPONSE",
                                "created_at": "2026-10-18T04:10:50Z", "content": "One more finding."}) + "\n")
        from sessionfs.watchers.antigravity import _AntigravityEventHandler

        handler = _AntigravityEventHandler(w._event_queue, w._event_lock, w._rescan)

        class _Ev:
            src_path = str(child_log)
            is_directory = False

        handler.on_modified(_Ev())  # type: ignore[arg-type]
        w.process_events()
        after = json.loads((store.get_session_dir(sfs_id) / "manifest.json").read_text())
        assert after["stats"]["message_count"] == before["stats"]["message_count"] + 1

    def test_handler_filters_and_rescan(self, tmp_path: Path):
        from sessionfs.watchers.antigravity import _AntigravityEventHandler

        queue: list[str] = []
        rescan = threading.Event()
        h = _AntigravityEventHandler(queue, threading.Lock(), rescan)

        class _Ev:
            def __init__(self, src, is_dir=False, dest=""):
                self.src_path = src
                self.is_directory = is_dir
                self.dest_path = dest

        h.on_modified(_Ev("/h/.gemini/antigravity/conversations/x.db"))
        h.on_moved(_Ev("/h/.gemini/antigravity/brain/c/.system_generated/logs/.tmp",
                       dest="/h/.gemini/antigravity/brain/c/.system_generated/logs/transcript_full.jsonl"))
        assert queue == ["/h/.gemini/antigravity/brain/c/.system_generated/logs/transcript_full.jsonl"]
        h.on_created(_Ev("/h/.gemini/antigravity-cli", is_dir=True))
        assert rescan.is_set()

    def test_capture_failures_degrade_health(self, ag: AgTree, store, monkeypatch):
        import sessionfs.converters.antigravity_to_sfs as conv

        def boom(*a, **k):
            raise ValueError("format changed")

        monkeypatch.setattr(conv, "parse_antigravity_conversation", boom)
        w = _watcher(ag, store)
        w.full_scan()
        status = w.get_status()
        assert status.health == "degraded"
        assert "format changed" in (status.last_error or "")

    def test_missing_storage_is_degraded(self, tmp_path: Path, store):
        from sessionfs.daemon.config import AntigravityWatcherConfig
        from sessionfs.watchers.antigravity import AntigravityWatcher

        w = AntigravityWatcher(AntigravityWatcherConfig(gemini_dir=tmp_path / "none"), store)
        w.full_scan()
        assert w.get_status().health == "degraded"

    def test_start_and_stop_watching_brain_dirs(self, ag: AgTree, store):
        w = _watcher(ag, store)
        w.start_watching()
        try:
            paths = w.get_status().watch_paths
            assert str(ag.gemini / "antigravity" / "brain") in paths
            assert str(ag.gemini / "antigravity-cli" / "brain") in paths
            assert str(ag.gemini) in paths  # antigravity-ide may appear later
        finally:
            w.stop_watching()

    def test_deleted_session_is_not_resurrected(self, ag: AgTree, store, monkeypatch):
        import sessionfs.store.deleted as deleted

        sfs_b = "ses_" + B.replace("-", "")[:16]
        monkeypatch.setattr(deleted, "is_excluded", lambda sid, base_dir=None: sid == sfs_b)
        w = _watcher(ag, store)
        w.full_scan()
        assert store.get_session_dir(sfs_b) is None


# ---------------------------------------------------------------------------
# Carrying stored sidechains forward (never drop stored messages)
# ---------------------------------------------------------------------------


class TestCarryForward:
    @staticmethod
    def _corrupt_child_transcript(ag: AgTree, child: str) -> Path:
        """Rewrite a subagent's transcript so a fresh parse yields 0 steps."""
        log = ag.conv("antigravity", child) / ".system_generated" / "logs" / "transcript_full.jsonl"
        log.write_text("this is not valid json\n")
        return log

    @staticmethod
    def _corrupt_stored_subagent_count(store, parent: str, child: str, value: Any) -> Path:
        sfs_id = store.get_tracked_session(parent).sfs_session_id
        manifest_path = store.get_session_dir(sfs_id) / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        for ref in manifest["sub_agents"]:
            if ref["agent_id"] == child:
                ref["message_count"] = value
        manifest_path.write_text(json.dumps(manifest))
        return manifest_path

    def test_corrupt_stored_subagent_count_is_carried_not_dropped(self, ag: AgTree, store):
        """A stored subagent count that is not an int must not be read as 0.

        Reading it as 0 would leave a still-linked child neither carried nor
        protected, silently dropping its stored sidechain (Finding 1).
        """
        w = _watcher(ag, store)
        w.full_scan()
        summaries = SummaryIndex().load(ag.gemini)

        self._corrupt_child_transcript(ag, D)
        manifest_path = self._corrupt_stored_subagent_count(store, A, D, "n/a")

        session = parse_antigravity_conversation(ag.conv("antigravity", A), summaries)
        assert D in session.pending_subagents  # the fresh parse lost D

        stored = json.loads(manifest_path.read_text())
        verdict = w._shrink_verdict(A, session, stored, summaries)
        assert verdict is None  # safe to install — nothing was dropped
        assert {r["agent_id"]: r["message_count"] for r in session.sub_agents} == {D: 2}
        assert sum(
            1 for m in session.messages if m.get("is_sidechain") and m.get("agent_id") == D
        ) == 2

    def test_held_elsewhere_requires_stored_messages(self, ag: AgTree, store):
        """A manifest claim alone does not prove a detached child held elsewhere.

        The owner's messages.jsonl must actually contain the child's sidechain;
        a manifest written just before a crash that never wrote its messages
        file is not proof (Finding 2).
        """
        w = _watcher(ag, store)
        w.full_scan()
        summaries = SummaryIndex().load(ag.gemini)

        # Detach D from A and re-home it under B, in memory only.
        for row in summaries.values():
            if row.get("conversation_id") == D:
                row["parent_conversation_id"] = B

        b_sfs = store.get_tracked_session(B).sfs_session_id
        manifest_path = store.get_session_dir(b_sfs) / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["sub_agents"] = list(manifest.get("sub_agents", [])) + [
            {"agent_id": D, "message_count": 2}
        ]
        manifest_path.write_text(json.dumps(manifest))

        # The manifest claims the child, but B's messages.jsonl holds nothing
        # for it: not proof.
        assert w._held_elsewhere(D, summaries, 2) is False

        # Once the messages are actually there, the claim becomes proof.
        msgs_path = store.get_session_dir(b_sfs) / "messages.jsonl"
        sidechain = {
            "role": "assistant",
            "content": [{"type": "text", "text": "child result"}],
            "is_sidechain": True,
            "agent_id": D,
        }
        with open(msgs_path, "a") as f:
            f.write(json.dumps(sidechain) + "\n")
            f.write(json.dumps(sidechain) + "\n")
        assert w._held_elsewhere(D, summaries, 2) is True

    def test_unreadable_pending_child_degrades_health(self, ag: AgTree, store):
        """A permanently malformed first-time child must not defer forever.

        Each capture defers on the pending child, but its stable fingerprint
        eventually records capture failures and degrades health (Finding 3).
        """
        self._corrupt_child_transcript(ag, D)
        w = _watcher(ag, store)
        for _ in range(5):
            w.capture_now(A)
            if w.get_status().health == "degraded":
                break
        status = w.get_status()
        assert status.health == "degraded"
        assert "55555555" in (status.last_error or "")

    def test_deleted_pending_child_releases_its_latch(self, ag: AgTree, store):
        """A child deleted at the source must not leave the watcher degraded.

        Once its transcript disappears, a previously-latched pending child's
        latch is released on the parent's next (still-deferred) capture.
        """
        self._corrupt_child_transcript(ag, D)
        w = _watcher(ag, store)
        for _ in range(5):
            w.capture_now(A)
            if w.get_status().health == "degraded":
                break
        assert w.get_status().health == "degraded"
        assert w._capture_health.has_unreadable_session(D)

        # The child's transcript is deleted at the source; its summary row is
        # still present, so the parent keeps deferring on it.
        child = ag.conv("antigravity", D)
        gone = child.parent / "gone-d"
        child.rename(gone)

        w.capture_now(A)
        assert not w._capture_health.has_unreadable_session(D)
        assert A not in w._pending_unreadable
        assert w.get_status().health == "healthy"

    def test_retry_target_gone_releases_pending_child_latches(self, ag: AgTree, store):
        """A deleted parent must release its pending children's latches.

        When a retry target has disappeared from both the summaries and the
        disk, the unreadable latches of the children it once linked are
        released too (they were never captured as top-level sessions).
        """
        w = _watcher(ag, store)
        summaries = SummaryIndex().load(ag.gemini)

        w._pending_unreadable[A] = {D}
        w._capture_health.record_unreadable_session("unreadable child", D)
        assert w.get_status().health == "degraded"

        # The parent is gone from the summaries and its transcript from disk.
        summaries = {k: v for k, v in summaries.items() if k[1] != A}
        parent = ag.conv("antigravity", A)
        gone = parent.parent / "gone-a"
        parent.rename(gone)

        w._retry[A] = (1, 0.0)
        w._retry_targets([A], summaries)

        assert A not in w._pending_unreadable
        assert not w._capture_health.has_unreadable_session(D)
        assert w.get_status().health == "healthy"

    def test_kept_unreadable_retries_instead_of_freezing(self, ag: AgTree, store, monkeypatch):
        """A transient stored-messages read failure must not freeze capture.

        The stored capture is kept, but the source fingerprint is not marked
        current and the session stays queued for retry, so the newer messages
        are recaptured once the stored messages read again.
        """
        w = _watcher(ag, store)
        w.full_scan()
        a_ref = store.get_tracked_session(A)
        a_dir = store.get_session_dir(a_ref.sfs_session_id)
        before = (a_dir / "messages.jsonl").read_text()
        fingerprint_before = (a_ref.last_mtime, a_ref.last_size)

        # The child's transcript becomes unreadable (still linked), so the
        # parent's recapture needs to carry D's stored sidechain forward.
        self._corrupt_child_transcript(ag, D)
        original = w._stored_messages
        fail_reads = {"on": True}

        def flaky(native_id):
            if fail_reads["on"]:
                return None
            return original(native_id)

        monkeypatch.setattr(w, "_stored_messages", flaky)

        log = ag.conv("antigravity", A) / ".system_generated" / "logs" / "transcript_full.jsonl"
        with open(log, "a") as f:
            f.write("\n")  # close the half-written final line first
        _file_event(w, _append_step(ag, "antigravity", A, 21, "more work"))

        # The stored capture is kept (not replaced by a lossy one)...
        assert (a_dir / "messages.jsonl").read_text() == before
        # ...the fingerprint is NOT marked current...
        ref = store.get_tracked_session(A)
        assert (ref.last_mtime, ref.last_size) == fingerprint_before
        # ...and a retry is scheduled so a transient failure self-heals.
        assert A in w._retry

        # Once the stored messages read again, the retry installs the real
        # capture (the newer message is no longer skipped).
        fail_reads["on"] = False
        w._retry = {k: (n, 0.0) for k, (n, _due) in w._retry.items()}
        w._last_event_time = 0.0
        w.process_events()
        assert any("more work" in json.dumps(m) for m in _messages(store, A))

    def test_carry_recomputes_aggregate_stats(self, ag: AgTree, store):
        """A carry must rewrite the token/tool aggregates to match the fold.

        The fresh parse that lost the child also lost its tokens and tools;
        the written manifest and tools.json must reflect the carried messages
        (Finding 4).
        """
        from sessionfs.watchers.antigravity import CaptureOutcome

        w = _watcher(ag, store)
        w.full_scan()
        a_sfs = store.get_tracked_session(A).sfs_session_id
        a_dir = store.get_session_dir(a_sfs)

        self._corrupt_child_transcript(ag, D)
        manifest_path = self._corrupt_stored_subagent_count(store, A, D, "n/a")

        assert w.capture_now(A) is CaptureOutcome.STORED

        manifest = json.loads(manifest_path.read_text())
        msgs = [
            json.loads(line)
            for line in (a_dir / "messages.jsonl").read_text().splitlines()
            if line.strip()
        ]
        total_in = sum((m.get("usage") or {}).get("input_tokens") or 0 for m in msgs)
        total_out = sum((m.get("usage") or {}).get("output_tokens") or 0 for m in msgs)
        tool_uses = [
            b for m in msgs for b in (m.get("content") or [])
            if b.get("type") == "tool_use"
        ]
        tools = sorted({b["name"] for b in tool_uses if isinstance(b.get("name"), str)})

        assert manifest["stats"]["total_input_tokens"] == total_in
        assert manifest["stats"]["total_output_tokens"] == total_out
        assert manifest["stats"]["tool_use_count"] == len(tool_uses)
        assert json.loads((a_dir / "tools.json").read_text()).get("tools_used") == tools
        # The carried child is still there, with its real (repaired) count.
        assert any(
            r["agent_id"] == D and r["message_count"] == 2 for r in manifest["sub_agents"]
        )


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


class TestCliWiring:
    def test_daemon_registers_watcher(self, tmp_path: Path):
        from sessionfs.daemon.config import DaemonConfig
        from sessionfs.daemon.main import Daemon

        cfg = DaemonConfig(store_dir=tmp_path / "store")
        cfg.claude_code.enabled = False
        cfg.antigravity.enabled = True
        daemon = Daemon(cfg)
        daemon._init_watchers()
        assert [type(w).__name__ for w in daemon.watchers] == ["AntigravityWatcher"]

    def test_init_detects_any_antigravity_root(self):
        from sessionfs.cli.cmd_init import _get_tool_definitions, detect_tools

        defs = [t for t in _get_tool_definitions() if t.config_key == "antigravity"]
        assert len(defs) == 1
        (Path.home() / ".gemini" / "antigravity-cli").mkdir(parents=True)
        detected, _ = detect_tools(defs)
        assert detected and detected[0].found_path.name == "antigravity-cli"

    def test_watcher_enable_writes_config(self):
        from sessionfs.cli import cmd_watcher

        assert "antigravity" in cmd_watcher.TOOLS
        cmd_watcher._config_path().parent.mkdir(parents=True, exist_ok=True)
        cmd_watcher._write_tool_enabled("antigravity", True)
        assert cmd_watcher._is_enabled(cmd_watcher._read_config(), "antigravity") is True
        cmd_watcher._write_tool_enabled("antigravity", False)
        assert cmd_watcher._is_enabled(cmd_watcher._read_config(), "antigravity") is False
        assert cmd_watcher._tool_is_installed("antigravity") is False
        (Path.home() / ".gemini" / "antigravity").mkdir(parents=True)
        assert cmd_watcher._tool_is_installed("antigravity") is True

    def test_recapture_handler_registered(self, ag: AgTree, store):
        from sessionfs.cli import cmd_recapture

        assert cmd_recapture._TOOL_HANDLERS["antigravity"] == "_recapture_antigravity"
        w = _watcher(ag, store)
        w.full_scan()
        ref = store.get_tracked_session(B)
        cmd_recapture._recapture_antigravity(store, ref.sfs_session_id, ref, Path(ref.native_path))
        assert (store.get_session_dir(ref.sfs_session_id) / "messages.jsonl").exists()

    def test_resume_in_antigravity_is_rejected_as_capture_only(self, ag: AgTree):
        from typer.testing import CliRunner

        from sessionfs.cli.common import open_store
        from sessionfs.cli.main import app

        s = open_store()
        try:
            w = _watcher(ag, s)
            w.full_scan()
            sfs_id = s.get_tracked_session(B).sfs_session_id
        finally:
            s.close()
        result = CliRunner().invoke(app, ["resume", sfs_id, "--in", "antigravity", "--no-rules-sync"])
        assert result.exit_code == 1
        assert "capture-only" in result.output

    def test_title_abbreviation(self):
        from sessionfs.cli.titles import abbreviate_tool

        assert abbreviate_tool("antigravity") == "AGY"


class TestMcpInstall:
    MCP = {"command": "/usr/local/bin/sfs", "args": ["mcp", "serve"]}

    def _paths(self):
        g = Path.home() / ".gemini"
        return g / "config" / "mcp_config.json", g / "antigravity" / "mcp_config.json"

    def test_writes_into_empty_shipped_file(self):
        from sessionfs.cli.cmd_mcp import _install_antigravity

        new, old = self._paths()
        new.parent.mkdir(parents=True)
        new.write_text("")  # Antigravity ships it as a zero-byte file
        (Path.home() / ".gemini" / "antigravity").mkdir(parents=True)
        _install_antigravity(self.MCP)
        assert json.loads(new.read_text()) == {"mcpServers": {"sessionfs": self.MCP}}
        assert not old.exists()  # never created alongside an existing config

    def test_preserves_other_servers_in_every_existing_file(self):
        from sessionfs.cli.cmd_mcp import _install_antigravity, _uninstall_antigravity

        new, old = self._paths()
        for p in (new, old):
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps({"mcpServers": {"other": {"serverUrl": "https://example.invalid/mcp"}}}))
        _install_antigravity(self.MCP)
        for p in (new, old):
            data = json.loads(p.read_text())
            assert data["mcpServers"]["other"] == {"serverUrl": "https://example.invalid/mcp"}
            assert data["mcpServers"]["sessionfs"] == self.MCP
        _uninstall_antigravity()
        for p in (new, old):
            assert json.loads(p.read_text()) == {"mcpServers": {"other": {"serverUrl": "https://example.invalid/mcp"}}}

    def test_creates_current_layout_file_when_none_exists(self):
        from sessionfs.cli.cmd_mcp import _install_antigravity

        new, _ = self._paths()
        (Path.home() / ".gemini" / "antigravity-cli").mkdir(parents=True)
        _install_antigravity(self.MCP)
        assert json.loads(new.read_text())["mcpServers"]["sessionfs"] == self.MCP

    def test_refuses_when_antigravity_missing(self):
        from sessionfs.cli.cmd_mcp import _install_antigravity

        with pytest.raises(SystemExit):
            _install_antigravity(self.MCP)

    def test_refuses_to_overwrite_malformed_config(self):
        from sessionfs.cli.cmd_mcp import _install_antigravity

        new, _ = self._paths()
        new.parent.mkdir(parents=True)
        new.write_text("{not json")
        with pytest.raises(SystemExit):
            _install_antigravity(self.MCP)
        assert new.read_text() == "{not json"

    def test_install_command_dispatch(self, monkeypatch):
        from sessionfs.cli import cmd_mcp

        seen: list[dict] = []
        monkeypatch.setattr(cmd_mcp.shutil, "which", lambda name: "/usr/local/bin/sfs")
        monkeypatch.setattr(cmd_mcp, "_install_antigravity", lambda cfg: seen.append(cfg))
        cmd_mcp._install_mcp_for_tool("antigravity")
        assert seen == [self.MCP]
        assert cmd_mcp.TOOL_INSTRUCTION_FILES["antigravity"] is None


class TestProvenance:
    def test_antigravity_rules_files_recorded_once(self, tmp_path: Path):
        from sessionfs.spec.instruction_provenance import discover

        (tmp_path / "GEMINI.md").write_text("# rules\n")
        (tmp_path / "AGENTS.md").write_text("# agents\n")
        prov = discover(tmp_path, tool="antigravity", home_dir=tmp_path / "home")
        paths = [a.path for a in prov.instruction_artifacts if a.artifact_type == "rules_file"]
        assert sorted(paths) == ["AGENTS.md", "GEMINI.md"]
        assert len(paths) == len(set(paths))


def test_doctor_reports_skipped_sessions(tmp_path: Path):
    from rich.table import Table

    from sessionfs.cli.cmd_doctor import _check_capture_health

    (tmp_path / "daemon.json").write_text(json.dumps({"watchers": [
        {"name": "antigravity", "health": "healthy", "sessions_skipped": {"encrypted": 2}},
    ]}))
    table = Table()
    table.add_column("a")
    table.add_column("b")
    table.add_column("c")
    _check_capture_health(table, tmp_path)
    cells = [str(c) for col in table.columns for c in col._cells]
    assert any("2 encrypted at rest" in c for c in cells)


def test_real_samples_parse_when_present():
    """Developer-machine check against real Antigravity data, if any.

    Skipped in CI and under the isolated test HOME (always, unless
    SFS_ANTIGRAVITY_REAL_DIR points at a real ~/.gemini).
    """
    real = os.environ.get("SFS_ANTIGRAVITY_REAL_DIR")
    if not real:
        pytest.skip("SFS_ANTIGRAVITY_REAL_DIR not set")
    sessions = discover_antigravity_sessions(Path(real))
    for s in sessions:
        parsed = parse_antigravity_conversation(Path(s["path"]))
        assert parsed.message_count > 0


# ---------------------------------------------------------------------------
# Result -> call pairing (no step carries a call id in real samples)
# ---------------------------------------------------------------------------


def _write_conv(tmp_path: Path, steps: list[dict[str, Any]]) -> Path:
    conv = tmp_path / ".gemini" / "antigravity" / "brain" / "abcdef00-0000-4000-8000-000000000000"
    logs = conv / ".system_generated" / "logs"
    logs.mkdir(parents=True)
    (logs / "transcript_full.jsonl").write_text("".join(json.dumps(s) + "\n" for s in steps))
    return conv


def _step(i: int, stype: str, **kw: Any) -> dict[str, Any]:
    return {"step_index": i, "source": "MODEL", "type": stype, "status": "DONE",
            "created_at": f"2026-10-18T05:00:{i:02d}Z", **kw}


def _pairs(session) -> dict[str, str]:
    """tool_result content (first line) -> name+input of the call it is linked to."""
    names = {b["tool_use_id"]: b["name"] + ":" + json.dumps(b["input"], sort_keys=True)
             for b in _blocks(session.messages, "tool_use")}
    return {b["content"].splitlines()[0]: names[b["tool_use_id"]]
            for b in _blocks(session.messages, "tool_result")}


class TestResultPairing:
    def test_out_of_order_results_follow_file_paths(self, tmp_path: Path):
        conv = _write_conv(tmp_path, [
            _step(0, "PLANNER_RESPONSE", tool_calls=[
                {"name": "write_to_file", "args": {"TargetFile": "/w/a.txt", "CodeContent": "a"}},
                {"name": "write_to_file", "args": {"TargetFile": "/w/b.txt", "CodeContent": "b"}},
                {"name": "view_file", "args": {"AbsolutePath": "/w/c.txt"}},
            ]),
            _step(1, "GENERIC", content="Created file file:///w/b.txt with requested content."),
            _step(2, "GENERIC", content="File Path: `file:///w/c.txt`\nTotal Lines: 1"),
            _step(3, "GENERIC", content="Created file file:///w/a.txt with requested content."),
        ])
        pairs = _pairs(parse_antigravity_conversation(conv))
        assert "/w/b.txt" in pairs["Created file file:///w/b.txt with requested content."]
        assert "/w/a.txt" in pairs["Created file file:///w/a.txt with requested content."]
        assert pairs["File Path: `file:///w/c.txt`"].startswith("view_file")

    def test_background_tasks_out_of_order_and_completion_linked(self, tmp_path: Path):
        cid = "abcdef00-0000-4000-8000-000000000000"
        conv = _write_conv(tmp_path, [
            _step(0, "PLANNER_RESPONSE", tool_calls=[
                {"name": "run_command", "args": {"CommandLine": "make build", "Cwd": "/w"}},
                {"name": "run_command", "args": {"CommandLine": "make test", "Cwd": "/w"}},
            ]),
            _step(1, "GENERIC", status="RUNNING",
                  content=f"Tool is running as a background task with task id: {cid}/task-1\n"
                          "Task Description: make test"),
            _step(2, "GENERIC", status="RUNNING",
                  content=f"Tool is running as a background task with task id: {cid}/task-2\n"
                          "Task Description: make build"),
            {"step_index": 3, "source": "SYSTEM", "type": "SYSTEM_MESSAGE", "created_at": "2026-10-18T05:00:03Z",
             "content": f"<SYSTEM_MESSAGE>\n[Message] sender={cid}/task-1 Task finished, exit code 0\n</SYSTEM_MESSAGE>"},
        ])
        session = parse_antigravity_conversation(conv)
        uses = {b["input"]["CommandLine"]: b["tool_use_id"] for b in _blocks(session.messages, "tool_use")}
        results = {m["metadata"]["background_task_id"]: m["content"][0]["tool_use_id"]
                   for m in session.messages if m["role"] == "tool"}
        assert results[f"{cid}/task-1"] == uses["make test"]
        assert results[f"{cid}/task-2"] == uses["make build"]
        system = [m for m in session.messages if m["role"] == "system"][0]
        assert system["metadata"]["tool_use_id"] == uses["make test"]

    def test_unanswered_call_does_not_shift_later_pairings(self, tmp_path: Path):
        conv = _write_conv(tmp_path, [
            _step(0, "PLANNER_RESPONSE", tool_calls=[{"name": "browser_open", "args": {"Url": "x"}}]),
            # no result for step 0 (cancelled)
            _step(1, "PLANNER_RESPONSE", tool_calls=[{"name": "list_dir", "args": {"DirectoryPath": "/w"}}]),
            _step(2, "GENERIC", content="Listed 3 entries"),
        ])
        pairs = _pairs(parse_antigravity_conversation(conv))
        assert pairs["Listed 3 entries"].startswith("list_dir")

    def test_kind_mismatch_is_skipped(self, tmp_path: Path):
        conv = _write_conv(tmp_path, [
            _step(0, "PLANNER_RESPONSE", tool_calls=[
                {"name": "ask_question", "args": {"questions": []}},
                {"name": "run_command", "args": {"CommandLine": "ls"}},
            ]),
            _step(1, "GENERIC", content="The command exited with code 0.\nOutput:\nx"),
            _step(2, "GENERIC", content="A1: yes"),
        ])
        pairs = _pairs(parse_antigravity_conversation(conv))
        assert pairs["The command exited with code 0."].startswith("run_command")
        assert pairs["A1: yes"].startswith("ask_question")

    def test_explicit_link_wins_when_present(self, tmp_path: Path):
        conv = _write_conv(tmp_path, [
            _step(0, "PLANNER_RESPONSE", tool_calls=[
                {"id": "call-1", "name": "custom_a", "args": {}},
                {"id": "call-2", "name": "custom_b", "args": {}},
            ]),
            _step(1, "GENERIC", content="second", tool_call_id="call-2"),
            _step(2, "GENERIC", content="first"),
        ])
        session = parse_antigravity_conversation(conv)
        linked = {b["content"]: b["tool_use_id"] for b in _blocks(session.messages, "tool_result")}
        assert linked == {"second": "call-2", "first": "call-1"}


def test_server_token_budget_knows_antigravity():
    from sessionfs.server.routes.tickets import _TOOL_TOKEN_LIMITS

    assert _TOOL_TOKEN_LIMITS["antigravity"] == 8000
    assert _TOOL_TOKEN_LIMITS["kilo-code"] == 4000


# ---------------------------------------------------------------------------
# Review round 1: path safety, symlinks, ids
# ---------------------------------------------------------------------------


class TestPathSafety:
    def test_ids_must_be_canonical_uuids(self, tmp_path: Path):
        from sessionfs.converters.antigravity_io import UnsafePath, is_conversation_id
        from sessionfs.converters.antigravity_to_sfs import (
            locate_conversation_dir,
            sfs_session_id_for,
            split_conversation_dir,
        )

        assert is_conversation_id(A)
        for bad in ("../../etc", "..", ".", "a/b", A.upper(), A + "/..", "", None, A[:-1]):
            assert not is_conversation_id(bad)
        assert locate_conversation_dir(tmp_path, "antigravity", "../../etc") is None
        with pytest.raises(UnsafePath):
            split_conversation_dir(tmp_path / "antigravity" / "brain" / "..")
        with pytest.raises(UnsafePath):
            split_conversation_dir(tmp_path / "elsewhere" / "brain" / A)
        with pytest.raises(UnsafePath):
            sfs_session_id_for("../../x")
        import re

        assert re.fullmatch(r"ses_[0-9a-f]{16}", sfs_session_id_for(A))
        assert conversation_id_from_path("/h/.gemini/antigravity/brain/../../x/logs/t.jsonl") is None

    def test_traversal_ids_in_summaries_db_are_dropped(self, ag: AgTree):
        conn = ag.conns[1]
        conn.execute(
            "INSERT INTO conversation_summaries (conversation_id, title, app_data_dir, parent_conversation_id)"
            " VALUES ('../../../etc', 'evil', 'antigravity-cli', ''),"
            f" ('{'99999999-9999-4999-8999-999999999999'}', 'child', '../x', '../../{A}')"
        )
        conn.commit()
        summaries = SummaryIndex().load(ag.gemini)
        assert all(cid != "../../../etc" for _app, cid in summaries)
        row = summaries[("antigravity-cli", "99999999-9999-4999-8999-999999999999")]
        assert row["parent_conversation_id"] == ""  # invalid parent link dropped
        assert row["app_data_dir"] == "antigravity-cli"  # invalid app ignored

    def test_symlinked_transcript_is_never_read(self, ag: AgTree, tmp_path: Path):
        secret = tmp_path / "secret.jsonl"
        secret.write_text(json.dumps({"step_index": 0, "type": "USER_INPUT", "content": "TOP SECRET"}) + "\n")
        logs = ag.conv("antigravity-cli", B) / ".system_generated" / "logs"
        for name in ("transcript_full.jsonl", "transcript.jsonl"):
            (logs / name).unlink()
        shutil.rmtree(logs / "chunks")
        (logs / "transcript_full.jsonl").symlink_to(secret)
        loaded = load_steps(ag.conv("antigravity-cli", B))
        assert loaded.steps == []
        assert "antigravity-cli" not in {s["app_data_dir"] for s in discover_antigravity_sessions(ag.gemini)
                                          if s["session_id"] == B}

    def test_symlinked_conversation_dir_and_logs_dir_skipped(self, ag: AgTree, tmp_path: Path):
        outside = tmp_path / "outside-conv"
        shutil.copytree(ag.conv("antigravity-cli", B), outside)
        link_id = "77777777-7777-4777-8777-777777777777"
        (ag.gemini / "antigravity-cli" / "brain" / link_id).symlink_to(outside)
        found = {s["session_id"] for s in discover_antigravity_sessions(ag.gemini)}
        assert link_id not in found
        # A symlinked logs/ directory inside a real conversation is not followed either.
        conv = ag.conv("antigravity-cli", C)
        logs = conv / ".system_generated" / "logs"
        shutil.rmtree(logs)
        logs.symlink_to(outside / ".system_generated" / "logs")
        assert load_steps(conv).steps == []

    def test_symlinked_step_output_ignored(self, ag: AgTree, tmp_path: Path):
        secret = tmp_path / "secret.txt"
        secret.write_text("TOP SECRET " * 100)
        out = ag.conv("antigravity", A) / ".system_generated" / "steps" / "12" / "output.txt"
        out.unlink()
        out.symlink_to(secret)
        session = parse_antigravity_conversation(ag.conv("antigravity", A))
        assert not any("TOP SECRET" in json.dumps(m) for m in session.messages)


# ---------------------------------------------------------------------------
# Review round 1: step outputs, summaries DB changes, torn snapshots
# ---------------------------------------------------------------------------


class TestStepOutputs:
    def test_output_txt_is_watched_and_fingerprinted(self, ag: AgTree, store):
        from sessionfs.converters.antigravity_to_sfs import conversation_fingerprint
        from sessionfs.watchers.antigravity import _AntigravityEventHandler

        w = _watcher(ag, store)
        w.full_scan()
        conv = ag.conv("antigravity", A)
        before = conversation_fingerprint(conv)
        out = conv / ".system_generated" / "steps" / "17" / "output.txt"
        out.parent.mkdir(parents=True)
        out.write_text(f"Created file file://{ag.workspace}/notes/hello.txt with requested content.\n"
                       "late output " + "x" * 500)
        assert conversation_fingerprint(conv) != before

        handler = _AntigravityEventHandler(w._event_queue, w._event_lock, w._rescan, w._summaries_changed)

        class _Ev:
            src_path = str(out)
            is_directory = False

        handler.on_created(_Ev())  # type: ignore[arg-type]
        assert w._event_queue == [str(out)]
        w.process_events()
        sfs_id = store.get_tracked_session(A).sfs_session_id
        text = (store.get_session_dir(sfs_id) / "messages.jsonl").read_text()
        assert "late output" in text

    def test_tool_output_capped_with_marker(self, ag: AgTree):
        from sessionfs.converters.antigravity_to_sfs import MAX_TOOL_OUTPUT_BYTES, TRUNCATION_MARKER

        out = ag.conv("antigravity", A) / ".system_generated" / "steps" / "12" / "output.txt"
        out.write_text("y" * (3 * MAX_TOOL_OUTPUT_BYTES))
        session = parse_antigravity_conversation(ag.conv("antigravity", A))
        capped = [b["content"] for b in _blocks(session.messages, "tool_result") if TRUNCATION_MARKER in b["content"]]
        assert len(capped) == 1
        assert len(capped[0].encode()) <= MAX_TOOL_OUTPUT_BYTES + len(TRUNCATION_MARKER.encode())


class TestSummariesChanges:
    def test_child_captured_before_parent_row_is_merged(self, ag: AgTree, store):
        from sessionfs.store.deleted import get_entry
        from sessionfs.watchers.antigravity import MERGED_REASON_PREFIX, _AntigravityEventHandler

        desk = ag.conns[0]
        desk.execute("UPDATE conversation_summaries SET parent_conversation_id = '' WHERE conversation_id = ?", (D,))
        desk.commit()
        w = _watcher(ag, store)
        w.full_scan()
        child_ref = store.get_tracked_session(D)
        assert child_ref is not None  # standalone while the parent link is missing
        child_sfs = child_ref.sfs_session_id
        assert store.get_session_dir(child_sfs) is not None

        desk.execute("UPDATE conversation_summaries SET parent_conversation_id = ? WHERE conversation_id = ?", (A, D))
        desk.commit()
        handler = _AntigravityEventHandler(w._event_queue, w._event_lock, w._rescan, w._summaries_changed)

        class _Ev:
            src_path = str(ag.gemini / "antigravity" / "conversation_summaries.db-wal")
            is_directory = False

        handler.on_modified(_Ev())  # type: ignore[arg-type]
        assert w._summaries_changed.is_set()
        w.process_events()

        parent_sfs = store.get_tracked_session(A).sfs_session_id
        manifest = json.loads((store.get_session_dir(parent_sfs) / "manifest.json").read_text())
        assert [s["agent_id"] for s in manifest["sub_agents"]] == [D]
        assert store.get_session_dir(child_sfs) is None
        assert store.get_tracked_session(D) is None
        entry = get_entry(child_sfs, base_dir=store.store_dir)
        assert entry["scope"] == "local"
        assert entry["reason"] == f"{MERGED_REASON_PREFIX}{parent_sfs}"

    def test_torn_snapshot_is_retried(self, ag: AgTree, monkeypatch):
        import sessionfs.converters.antigravity_to_sfs as conv

        real = conv._db_fingerprint
        calls = {"n": 0}

        def flaky(db):
            calls["n"] += 1
            fp = real(db)
            return fp + (calls["n"],) if calls["n"] <= 2 else fp  # first copy torn

        monkeypatch.setattr(conv, "_db_fingerprint", flaky)
        rows = read_summaries_db(ag.gemini / "antigravity" / "conversation_summaries.db")
        assert rows is not None and {r["conversation_id"] for r in rows} == {A, D}
        assert calls["n"] >= 4

    def test_never_stable_snapshot_is_a_failure(self, ag: AgTree, monkeypatch):
        import itertools

        import sessionfs.converters.antigravity_to_sfs as conv

        counter = itertools.count()
        monkeypatch.setattr(conv, "_db_fingerprint", lambda db: (next(counter),))
        assert read_summaries_db(ag.gemini / "antigravity" / "conversation_summaries.db") is None

    def test_failed_read_is_never_cached(self, ag: AgTree, monkeypatch):
        import sessionfs.converters.antigravity_to_sfs as conv

        real = conv.read_summaries_db
        monkeypatch.setattr(conv, "read_summaries_db", lambda db: None)
        index = SummaryIndex()
        assert index.load(ag.gemini) == {}
        monkeypatch.setattr(conv, "read_summaries_db", real)
        assert ("antigravity", A) in index.load(ag.gemini)  # the failure was not cached
        # A later failure (DB changed, re-read fails) keeps the last good rows.
        monkeypatch.setattr(conv, "read_summaries_db", lambda db: None)
        monkeypatch.setattr(conv, "_db_fingerprint", lambda db: ("changed",))
        assert ("antigravity", A) in index.load(ag.gemini)

    def test_forced_refresh_rereads_but_keeps_rows_on_failure(self, ag: AgTree, monkeypatch):
        import sessionfs.converters.antigravity_to_sfs as conv

        index = SummaryIndex()
        index.load(ag.gemini)
        calls: list[Path] = []
        real = conv.read_summaries_db

        def counting(db):
            calls.append(db)
            return real(db)

        monkeypatch.setattr(conv, "read_summaries_db", counting)
        index.load(ag.gemini)
        assert calls == []  # unchanged fingerprint: cached
        index.load(ag.gemini, force=True)
        assert len(calls) == 2  # forced: both DBs re-read
        monkeypatch.setattr(conv, "read_summaries_db", lambda db: None)
        rows = index.load(ag.gemini, force=True)
        assert ("antigravity", A) in rows and ("antigravity-cli", B) in rows


# ---------------------------------------------------------------------------
# Review round 1: workspace validation
# ---------------------------------------------------------------------------


class TestWorkspaceValidation:
    def test_rejects_system_home_and_other_users(self, tmp_path: Path):
        from sessionfs.converters.antigravity_to_sfs import validate_workspace_root

        home = tmp_path / "home" / "me"
        (home / "proj").mkdir(parents=True)
        for bad in ("/", "/etc", "/etc/nginx", "/usr/local", "/tmp", "/Users", "/home",
                    "/Users/someone-else/repo", "/home/someone-else/repo", str(home), str(home.parent),
                    "relative/path", ""):
            assert validate_workspace_root(bad, home=home) is None, bad
        assert validate_workspace_root(str(home / "proj"), home=home) == str(home / "proj")

    def test_outside_home_needs_owned_git_repo(self, tmp_path: Path):
        from sessionfs.converters.antigravity_to_sfs import validate_workspace_root

        home = tmp_path / "home" / "me"
        home.mkdir(parents=True)
        plain = tmp_path / "plain" / "dir"
        plain.mkdir(parents=True)
        repo = tmp_path / "repo"
        (repo / ".git").mkdir(parents=True)
        (repo / "src").mkdir()
        assert validate_workspace_root(str(plain), home=home) is None
        assert validate_workspace_root(str(repo), home=home) == str(repo)
        assert validate_workspace_root(str(repo / "src"), home=home) == str(repo / "src")

    def test_recorded_workspace_uri_goes_through_the_validator(self, ag: AgTree):
        cli = ag.conns[1]
        cli.execute("UPDATE conversation_summaries SET workspace_uris = ? WHERE conversation_id = ?",
                    (json.dumps(["file:///", "file:///etc"]), B))
        cli.commit()
        session = parse_antigravity_conversation(ag.conv("antigravity-cli", B))
        # Falls back to the repo the tools touched.
        assert session.workspace_root == str(ag.workspace)
        assert session.workspace_source == "derived"
        assert session.metadata["rejected_workspace_uris"] == ["/", "/etc"]

    def test_derived_root_in_system_dir_rejected(self, tmp_path: Path):
        uses = [("view_file", {"AbsolutePath": "/etc/hosts"}), ("view_file", {"AbsolutePath": "/etc/passwd"})]
        assert derive_workspace_root(uses, [], exclude=[], home=tmp_path / "home") is None


# ---------------------------------------------------------------------------
# Review round 1: pairing, background ids, effort, incremental reads
# ---------------------------------------------------------------------------


class TestReviewPairing:
    def test_incompatible_result_is_left_unpaired(self, tmp_path: Path):
        conv = _write_conv(tmp_path, [
            _step(0, "PLANNER_RESPONSE", tool_calls=[{"name": "view_file", "args": {"AbsolutePath": "/w/a.txt"}}]),
            _step(1, "GENERIC", content="Created file file:///w/b.txt with requested content."),
            _step(2, "GENERIC", content="File Path: `file:///w/a.txt`\nTotal Lines: 1"),
        ])
        session = parse_antigravity_conversation(conv)
        results = _blocks(session.messages, "tool_result")
        assert len(results) == 1 and results[0]["content"].startswith("File Path")
        unpaired = [m for m in session.messages if m.get("metadata", {}).get("unpaired_tool_result")]
        assert len(unpaired) == 1 and "b.txt" in unpaired[0]["content"][0]["text"]

    def test_background_task_ids_matched_exactly(self, tmp_path: Path):
        cid = "abcdef00-0000-4000-8000-000000000000"
        calls = [{"name": "run_command", "args": {"CommandLine": f"job {n}"}} for n in range(11)]
        steps = [_step(0, "PLANNER_RESPONSE", tool_calls=calls)]
        for n in range(11):
            steps.append(_step(n + 1, "GENERIC", status="RUNNING",
                               content=f"Tool is running as a background task with task id: {cid}/task-{n}\n"
                                       f"Task Description: job {n}"))
        steps.append({"step_index": 30, "source": "SYSTEM", "type": "SYSTEM_MESSAGE",
                      "created_at": "2026-10-18T05:00:30Z",
                      "content": f"<SYSTEM_MESSAGE>\n[Message] sender={cid}/task-10 done\n</SYSTEM_MESSAGE>"})
        session = parse_antigravity_conversation(_write_conv(tmp_path, steps))
        uses = {b["input"]["CommandLine"]: b["tool_use_id"] for b in _blocks(session.messages, "tool_use")}
        system = [m for m in session.messages if m["role"] == "system"][0]
        assert system["metadata"]["background_task_id"] == f"{cid}/task-10"
        assert system["metadata"]["tool_use_id"] == uses["job 10"]


class TestReasoningEffort:
    def test_minimal_maps_to_low_and_keeps_label(self, tmp_path: Path):
        from sessionfs.spec.validate import validate_session

        conv = _write_conv(tmp_path, [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "created_at": "2026-10-18T05:00:00Z",
             "content": "<USER_REQUEST>\nhi\n</USER_REQUEST>\n<USER_SETTINGS_CHANGE>\nThe user changed setting "
                        "`Model Selection` from None to Gemini 3.8 Flash (Minimal). No need.\n</USER_SETTINGS_CHANGE>"},
            _step(1, "PLANNER_RESPONSE", content="hello"),
        ])
        session = parse_antigravity_conversation(conv)
        assert session.model_id == "Gemini 3.8 Flash" and session.reasoning_effort == "low"
        assert session.metadata["reasoning_effort_label"] == "minimal"
        out = tmp_path / "out"
        convert_antigravity_to_sfs(session, out)
        assert validate_session(out).errors == []


class TestIncrementalReads:
    def test_appended_lines_are_read_incrementally(self, tmp_path: Path, monkeypatch):
        from sessionfs.converters import antigravity_io as io

        root = tmp_path / "antigravity"
        (root / "d").mkdir(parents=True)
        f = root / "d" / "t.jsonl"
        f.write_text('{"a": 1}\n{"a": 2}\n')
        cache = io.JsonlCache()
        consumed: list[int] = []
        real = io.JsonlCache._consume

        def spy(fh, state, name, hasher):
            consumed.append(state.offset)
            return real(fh, state, name, hasher)

        monkeypatch.setattr(io.JsonlCache, "_consume", staticmethod(spy))
        assert cache.read(root, ("d", "t.jsonl"))[0] == [{"a": 1}, {"a": 2}]
        assert cache.read(root, ("d", "t.jsonl"))[0] == [{"a": 1}, {"a": 2}]
        assert consumed == [0]  # unchanged file not re-read
        with open(f, "a") as fh:
            fh.write('{"a": 3}\n{"a": 4')  # one complete, one partial
        records, errors = cache.read(root, ("d", "t.jsonl"))
        assert records == [{"a": 1}, {"a": 2}, {"a": 3}]
        assert consumed[1] == len('{"a": 1}\n{"a": 2}\n')  # resumed, not from 0
        assert any("incomplete" in e for e in errors)
        with open(f, "a") as fh:
            fh.write('}\n')
        assert cache.read(root, ("d", "t.jsonl"))[0][-1] == {"a": 4}

    def test_rewritten_file_is_reparsed(self, tmp_path: Path):
        from sessionfs.converters import antigravity_io as io

        root = tmp_path / "antigravity"
        (root / "d").mkdir(parents=True)
        f = root / "d" / "t.jsonl"
        f.write_text('{"a": 1}\n')
        cache = io.JsonlCache()
        cache.read(root, ("d", "t.jsonl"))
        with open(f, "r+") as fh:  # same inode, same prefix length, different bytes
            fh.write('{"b": 9}\n{"c": 3}\n')
        assert cache.read(root, ("d", "t.jsonl"))[0] == [{"b": 9}, {"c": 3}]

    def test_oversized_line_skipped(self, tmp_path: Path, monkeypatch):
        from sessionfs.converters import antigravity_io as io

        monkeypatch.setattr(io, "MAX_LINE_BYTES", 32)
        root = tmp_path / "antigravity"
        (root / "d").mkdir(parents=True)
        (root / "d" / "t.jsonl").write_text('{"x": "' + "z" * 100 + '"}\n{"ok": 1}\n')
        records, errors = io.JsonlCache().read(root, ("d", "t.jsonl"))
        assert records == [{"ok": 1}]
        assert any("too large" in e for e in errors)


def test_antigravity_mcp_install_is_all_or_nothing():
    from sessionfs.cli.cmd_mcp import _install_antigravity

    g = Path.home() / ".gemini"
    new, old = g / "config" / "mcp_config.json", g / "antigravity" / "mcp_config.json"
    new.parent.mkdir(parents=True)
    old.parent.mkdir(parents=True)
    new.write_text("")
    old.write_text("{broken")
    with pytest.raises(SystemExit):
        _install_antigravity({"command": "/usr/local/bin/sfs", "args": ["mcp", "serve"]})
    assert new.read_text() == ""  # untouched: the second file failed preflight
    assert old.read_text() == "{broken"


# ---------------------------------------------------------------------------
# Review round 2: summary diffs, merge tombstones, failed removals
# ---------------------------------------------------------------------------


def _db_event(w, ag: AgTree, app: str = "antigravity") -> None:
    from sessionfs.watchers.antigravity import _AntigravityEventHandler

    handler = _AntigravityEventHandler(w._event_queue, w._event_lock, w._rescan, w._summaries_changed)

    class _Ev:
        src_path = str(ag.gemini / app / "conversation_summaries.db-wal")
        is_directory = False

    handler.on_modified(_Ev())  # type: ignore[arg-type]
    w._last_event_time = 0.0
    w.process_events()


def _manifest(store, cid: str) -> dict:
    ref = store.get_tracked_session(cid)
    return json.loads((store.get_session_dir(ref.sfs_session_id) / "manifest.json").read_text())


class TestSummaryDiffs:
    def test_title_change_recaptures(self, ag: AgTree, store):
        w = _watcher(ag, store)
        w.full_scan()
        assert _manifest(store, B)["title"] == "Create File Agy.txt In Pwd"
        ag.conns[1].execute("UPDATE conversation_summaries SET title = 'Renamed Task' WHERE conversation_id = ?", (B,))
        ag.conns[1].commit()
        _db_event(w, ag, "antigravity-cli")
        assert _manifest(store, B)["title"] == "Renamed Task"

    def test_workspace_change_recaptures(self, ag: AgTree, store, tmp_path: Path):
        other = tmp_path / "other-repo"
        (other / ".git").mkdir(parents=True)
        w = _watcher(ag, store)
        w.full_scan()
        ag.conns[1].execute("UPDATE conversation_summaries SET workspace_uris = ? WHERE conversation_id = ?",
                            (json.dumps([f"file://{other}"]), B))
        ag.conns[1].commit()
        _db_event(w, ag, "antigravity-cli")
        ref = store.get_tracked_session(B)
        workspace = json.loads((store.get_session_dir(ref.sfs_session_id) / "workspace.json").read_text())
        assert workspace["root_path"] == str(other)

    def test_child_becoming_top_level_is_captured_and_parent_shrinks(self, ag: AgTree, store):
        w = _watcher(ag, store)
        w.full_scan()
        parent_before = _manifest(store, A)
        assert [s["agent_id"] for s in parent_before["sub_agents"]] == [D]
        assert store.get_tracked_session(D) is None
        ag.conns[0].execute("UPDATE conversation_summaries SET parent_conversation_id = '' WHERE conversation_id = ?", (D,))
        ag.conns[0].commit()
        _db_event(w, ag)
        child = _manifest(store, D)
        assert child["source"]["original_session_id"] == D
        parent_after = _manifest(store, A)
        assert "sub_agents" not in parent_after  # the compression guard did not block it
        assert parent_after["stats"]["message_count"] == parent_before["stats"]["message_count"] - 2


class TestMergeTombstones:
    def _merge_child(self, ag: AgTree, store):
        desk = ag.conns[0]
        desk.execute("UPDATE conversation_summaries SET parent_conversation_id = '' WHERE conversation_id = ?", (D,))
        desk.commit()
        w = _watcher(ag, store)
        w.full_scan()
        child_sfs = store.get_tracked_session(D).sfs_session_id
        desk.execute("UPDATE conversation_summaries SET parent_conversation_id = ? WHERE conversation_id = ?", (A, D))
        desk.commit()
        _db_event(w, ag)
        assert store.get_session_dir(child_sfs) is None
        return w, child_sfs

    def test_merge_tombstone_cleared_when_child_is_top_level_again(self, ag: AgTree, store):
        from sessionfs.store.deleted import get_entry

        w, child_sfs = self._merge_child(ag, store)
        assert get_entry(child_sfs, base_dir=store.store_dir)["reason"].startswith("merged_into_parent:")
        ag.conns[0].execute("UPDATE conversation_summaries SET parent_conversation_id = '' WHERE conversation_id = ?", (D,))
        ag.conns[0].commit()
        _db_event(w, ag)
        assert get_entry(child_sfs, base_dir=store.store_dir) is None
        assert store.get_session_dir(child_sfs) is not None

    def test_user_deletion_is_never_cleared(self, ag: AgTree, store):
        from sessionfs.store.deleted import get_entry, mark_deleted

        w, child_sfs = self._merge_child(ag, store)
        mark_deleted(child_sfs, "local", base_dir=store.store_dir)  # the user deletes it
        ag.conns[0].execute("UPDATE conversation_summaries SET parent_conversation_id = '' WHERE conversation_id = ?", (D,))
        ag.conns[0].commit()
        _db_event(w, ag)
        entry = get_entry(child_sfs, base_dir=store.store_dir)
        assert entry is not None and "reason" not in entry
        assert store.get_session_dir(child_sfs) is None


class TestFailedRemoval:
    def test_failed_rmtree_keeps_index_and_tracking_for_retry(self, ag: AgTree, store, monkeypatch):
        import shutil as shutil_mod

        desk = ag.conns[0]
        desk.execute("UPDATE conversation_summaries SET parent_conversation_id = '' WHERE conversation_id = ?", (D,))
        desk.commit()
        w = _watcher(ag, store)
        w.full_scan()
        child_sfs = store.get_tracked_session(D).sfs_session_id
        desk.execute("UPDATE conversation_summaries SET parent_conversation_id = ? WHERE conversation_id = ?", (A, D))
        desk.commit()

        real_rmtree = shutil_mod.rmtree

        def refuse(path, *a, **k):
            if child_sfs in str(path):
                raise PermissionError("busy")
            return real_rmtree(path, *a, **k)

        monkeypatch.setattr(shutil_mod, "rmtree", refuse)
        _db_event(w, ag)
        assert store.get_session_dir(child_sfs) is not None
        assert store.get_tracked_session(D) is not None  # kept so the next pass retries
        assert store.index.get_session(child_sfs) is not None
        monkeypatch.undo()
        w.full_scan()  # next pass
        assert store.get_session_dir(child_sfs) is None
        assert store.get_tracked_session(D) is None


# ---------------------------------------------------------------------------
# Review round 3
# ---------------------------------------------------------------------------


def _messages(store, cid: str) -> list[dict]:
    ref = store.get_tracked_session(cid)
    text = (store.get_session_dir(ref.sfs_session_id) / "messages.jsonl").read_text()
    return [json.loads(line) for line in text.splitlines()]


def _append_step(ag: AgTree, app: str, cid: str, idx: int, text: str) -> Path:
    log = ag.conv(app, cid) / ".system_generated" / "logs" / "transcript_full.jsonl"
    with open(log, "a") as f:
        f.write(json.dumps({"step_index": idx, "source": "MODEL", "type": "PLANNER_RESPONSE",
                            "created_at": "2026-10-18T04:12:00Z", "content": text}) + "\n")
    return log


def _file_event(w, path: Path) -> None:
    from sessionfs.watchers.antigravity import _AntigravityEventHandler

    handler = _AntigravityEventHandler(w._event_queue, w._event_lock, w._rescan, w._summaries_changed)

    class _Ev:
        src_path = str(path)
        is_directory = False

    handler.on_modified(_Ev())  # type: ignore[arg-type]
    w._last_event_time = 0.0
    w.process_events()


class TestRound3Topology:
    def test_linked_child_unreadable_carries_forward_instead_of_deferring(self, ag: AgTree, store):
        w = _watcher(ag, store)
        w.full_scan()
        before = _messages(store, A)
        before_d = [m for m in before if m.get("agent_id") == D]
        assert before_d
        child = ag.conv("antigravity", D)
        hidden = child.parent / "hidden-d"
        child.rename(hidden)  # transcript temporarily unreadable; still linked
        try:
            log = ag.conv("antigravity", A) / ".system_generated" / "logs" / "transcript_full.jsonl"
            with open(log, "a") as f:
                f.write("\n")  # close the half-written final line first
            _file_event(w, _append_step(ag, "antigravity", A, 21, "more work"))
            after = _messages(store, A)
            # The parent keeps capturing its own thread, and the unreadable
            # child's stored sidechain is carried forward byte-for-byte (never
            # dropped, never deferred forever).
            assert [m for m in after if m.get("agent_id") == D] == before_d
            assert any("more work" in json.dumps(m) for m in after)
        finally:
            hidden.rename(child)
        _file_event(w, _append_step(ag, "antigravity", A, 22, "even more"))
        final = _messages(store, A)
        assert any(m.get("agent_id") == D for m in final)
        assert any("even more" in json.dumps(m) for m in final)

    def test_parent_not_shrunk_until_detached_child_is_stored(self, ag: AgTree, store, monkeypatch):
        """A detached child whose standalone capture fails transiently is
        carried forward in the parent (the parent is not deferred forever);
        once the child's standalone capture is stored, the parent's next
        recapture drops it."""
        import sessionfs.converters.antigravity_to_sfs as conv

        w = _watcher(ag, store)
        w.full_scan()
        real_parse = conv.parse_antigravity_conversation

        def failing_for_child(conv_dir, *a, **k):
            if Path(conv_dir).name == D:
                raise RuntimeError("transient read failure")
            return real_parse(conv_dir, *a, **k)

        monkeypatch.setattr(conv, "parse_antigravity_conversation", failing_for_child)
        ag.conns[0].execute("UPDATE conversation_summaries SET parent_conversation_id = '' WHERE conversation_id = ?", (D,))
        ag.conns[0].commit()
        _db_event(w, ag)
        assert [s["agent_id"] for s in _manifest(store, A)["sub_agents"]] == [D]  # carried, not dropped
        assert store.get_tracked_session(D) is None
        assert D in w._retry  # the failed capture is queued for retry
        monkeypatch.setattr(conv, "parse_antigravity_conversation", real_parse)
        w._retry = {k: (n, 0.0) for k, (n, _due) in w._retry.items()}  # backoff elapsed
        w._last_event_time = 0.0
        w.process_events()
        assert _manifest(store, D)["source"]["original_session_id"] == D
        # The child is now stored elsewhere; the parent's next recapture
        # proves it and drops the carried sidechain.
        log = ag.conv("antigravity", A) / ".system_generated" / "logs" / "transcript_full.jsonl"
        with open(log, "a") as f:
            f.write("\n")  # close the half-written final line first
        _file_event(w, _append_step(ag, "antigravity", A, 22, "parent keeps going"))
        assert "sub_agents" not in _manifest(store, A)

    def test_capture_outcomes(self, ag: AgTree, store, tmp_path: Path):
        from sessionfs.watchers.antigravity import CaptureOutcome

        w = _watcher(ag, store)
        summaries = SummaryIndex().load(ag.gemini)
        conv_a = ag.conv("antigravity", A)
        assert w._capture_session(A, conv_a, 1.0, 1, summaries) is CaptureOutcome.STORED
        # A subagent is never captured as its own session.
        assert w._capture_session(D, ag.conv("antigravity", D), 1.0, 1, summaries) is CaptureOutcome.DEFERRED
        from sessionfs.store.deleted import mark_deleted

        mark_deleted(sfs_session_id_for(B), "local", base_dir=store.store_dir)
        assert w._capture_session(B, ag.conv("antigravity-cli", B), 1.0, 1, summaries) is CaptureOutcome.EXCLUDED


class TestRound3Tombstones:
    def test_put_if_absent_never_overwrites(self, tmp_path: Path):
        from sessionfs.store.deleted import get_entry, mark_deleted, mark_deleted_if_absent

        mark_deleted("ses_aaaaaaaaaaaaaaaa", "everywhere", base_dir=tmp_path)
        assert mark_deleted_if_absent("ses_aaaaaaaaaaaaaaaa", "local", "merged_into_parent:x", base_dir=tmp_path) is False
        assert get_entry("ses_aaaaaaaaaaaaaaaa", base_dir=tmp_path)["scope"] == "everywhere"
        assert mark_deleted_if_absent("ses_bbbbbbbbbbbbbbbb", "local", "merged_into_parent:x", base_dir=tmp_path)
        assert get_entry("ses_bbbbbbbbbbbbbbbb", base_dir=tmp_path)["reason"] == "merged_into_parent:x"

    def test_compare_and_delete_only_removes_matching_reason(self, tmp_path: Path):
        from sessionfs.store.deleted import get_entry, mark_deleted, remove_exclusion_if_reason

        mark_deleted("ses_aaaaaaaaaaaaaaaa", "local", base_dir=tmp_path, reason="merged_into_parent:p")
        mark_deleted("ses_bbbbbbbbbbbbbbbb", "local", base_dir=tmp_path)  # a user deletion
        assert remove_exclusion_if_reason("ses_bbbbbbbbbbbbbbbb", "merged_into_parent:", base_dir=tmp_path) is False
        assert get_entry("ses_bbbbbbbbbbbbbbbb", base_dir=tmp_path) is not None
        # A user deletion that replaced the merge record wins.
        mark_deleted("ses_aaaaaaaaaaaaaaaa", "local", base_dir=tmp_path)
        assert remove_exclusion_if_reason("ses_aaaaaaaaaaaaaaaa", "merged_into_parent:", base_dir=tmp_path) is False
        assert get_entry("ses_aaaaaaaaaaaaaaaa", base_dir=tmp_path) is not None
        mark_deleted("ses_cccccccccccccccc", "local", base_dir=tmp_path, reason="merged_into_parent:p")
        assert remove_exclusion_if_reason("ses_cccccccccccccccc", "merged_into_parent:", base_dir=tmp_path) is True
        assert get_entry("ses_cccccccccccccccc", base_dir=tmp_path) is None

    def test_retire_keeps_a_concurrent_user_deletion(self, ag: AgTree, store):
        from sessionfs.store.deleted import get_entry, mark_deleted

        desk = ag.conns[0]
        desk.execute("UPDATE conversation_summaries SET parent_conversation_id = '' WHERE conversation_id = ?", (D,))
        desk.commit()
        w = _watcher(ag, store)
        w.full_scan()
        child_sfs = store.get_tracked_session(D).sfs_session_id
        mark_deleted(child_sfs, "everywhere", base_dir=store.store_dir)  # the user deletes it now
        desk.execute("UPDATE conversation_summaries SET parent_conversation_id = ? WHERE conversation_id = ?", (A, D))
        desk.commit()
        _db_event(w, ag)
        entry = get_entry(child_sfs, base_dir=store.store_dir)
        assert entry["scope"] == "everywhere" and "reason" not in entry


class TestRound3Staging:
    def test_staged_replacement_keeps_sync_state_and_checkpoints(self, ag: AgTree, store):
        w = _watcher(ag, store)
        w.full_scan()
        sfs_id = store.get_tracked_session(B).sfs_session_id
        live = store.get_session_dir(sfs_id)
        manifest = json.loads((live / "manifest.json").read_text())
        manifest["sync"] = {"etag": "abc", "dirty": False, "last_sync_at": "x"}
        (live / "manifest.json").write_text(json.dumps(manifest))
        (live / "checkpoints" / "before-refactor").mkdir(parents=True)
        (live / "checkpoints" / "before-refactor" / "manifest.json").write_text("{}")
        _file_event(w, _append_step(ag, "antigravity-cli", B, 4, "late step"))
        new_manifest = json.loads((live / "manifest.json").read_text())
        assert new_manifest["sync"]["etag"] == "abc" and new_manifest["sync"]["dirty"] is True
        assert (live / "checkpoints" / "before-refactor" / "manifest.json").exists()

    def test_obsolete_workspace_and_tools_files_disappear(self, ag: AgTree, store, monkeypatch):
        import sessionfs.converters.antigravity_to_sfs as conv

        w = _watcher(ag, store)
        w.full_scan()
        live = store.get_session_dir(store.get_tracked_session(B).sfs_session_id)
        assert (live / "workspace.json").exists() and (live / "tools.json").exists()
        monkeypatch.setattr(conv, "validate_workspace_root", lambda raw, home=None: None)
        real_parse = conv.parse_antigravity_conversation

        def no_tools(conv_dir, *a, **k):
            session = real_parse(conv_dir, *a, **k)
            session.tools_used = set()
            return session

        monkeypatch.setattr(conv, "parse_antigravity_conversation", no_tools)
        ag.conns[1].execute("UPDATE conversation_summaries SET title = 'Moved' WHERE conversation_id = ?", (B,))
        ag.conns[1].commit()
        _db_event(w, ag, "antigravity-cli")
        assert json.loads((live / "manifest.json").read_text())["title"] == "Moved"
        assert not (live / "workspace.json").exists()
        assert not (live / "tools.json").exists()


class TestRound3Summaries:
    def test_metadata_changed_while_daemon_stopped_is_captured(self, ag: AgTree, store):
        _watcher(ag, store).full_scan()
        ag.conns[1].execute("UPDATE conversation_summaries SET title = 'Renamed Offline' WHERE conversation_id = ?", (B,))
        ag.conns[1].commit()
        _watcher(ag, store).full_scan()  # a fresh daemon
        assert _manifest(store, B)["title"] == "Renamed Offline"

    def test_unchanged_metadata_is_not_recaptured_on_startup(self, ag: AgTree, store, monkeypatch):
        _watcher(ag, store).full_scan()
        w = _watcher(ag, store)
        calls: list[str] = []
        monkeypatch.setattr(w, "_capture_session", lambda cid, *a, **k: calls.append(cid))
        w.full_scan()
        assert calls == []

    def test_failed_forced_refresh_is_requeued(self, ag: AgTree, store, monkeypatch):
        import sessionfs.converters.antigravity_to_sfs as conv

        w = _watcher(ag, store)
        w.full_scan()
        ag.conns[1].execute("UPDATE conversation_summaries SET title = 'After Retry' WHERE conversation_id = ?", (B,))
        ag.conns[1].commit()
        real = conv.read_summaries_db
        monkeypatch.setattr(conv, "read_summaries_db", lambda db: None)
        _db_event(w, ag, "antigravity-cli")
        assert w._summaries_changed.is_set()  # re-queued, not consumed
        assert _manifest(store, B)["title"] != "After Retry"
        monkeypatch.setattr(conv, "read_summaries_db", real)
        w._last_event_time = 0.0
        w.process_events()
        assert _manifest(store, B)["title"] == "After Retry"


class TestRound3DeletePaths:
    """Prune and 410 cleanup remove the folder first and the index rows only
    once it is gone (tracking row before session row — foreign key); a
    folder that cannot be removed leaves the rows so the session stays
    visible for a retry."""

    def _seed(self, s):
        from sessionfs.watchers.base import NativeSessionRef

        sid = "ses_0123456789abcdef"
        d = s.allocate_session_dir(sid)
        manifest = {"sfs_version": "0.1.0", "session_id": sid, "created_at": "2026-10-18T00:00:00Z",
                    "source": {"tool": "codex"}}
        (d / "manifest.json").write_text(json.dumps(manifest))
        (d / "messages.jsonl").write_text("")
        s.upsert_session_metadata(sid, manifest, str(d))
        s.upsert_tracked_session(NativeSessionRef(
            tool="codex", native_session_id="native-1", native_path=str(d), sfs_session_id=sid,
        ))
        return sid, d

    def test_prune_removes_folder_then_rows(self, tmp_path: Path, monkeypatch):
        import sessionfs.store.pruner as pruner_mod
        from sessionfs.store.local import LocalStore
        from sessionfs.store.pruner import SessionPruner

        s = LocalStore(tmp_path / "s")
        s.initialize()
        try:
            sid, d = self._seed(s)
            seen = []
            real = pruner_mod.shutil.rmtree

            def spy(path, *a, **k):
                seen.append(s.index.get_session(sid) is not None)
                return real(path, *a, **k)

            monkeypatch.setattr(pruner_mod.shutil, "rmtree", spy)
            SessionPruner(s.sessions_dir, s.index.conn)._delete_session(sid)
            assert seen == [True]  # rows still there while the folder went
            assert s.index.get_session(sid) is None
            # The native tracking row survives pruning with its fingerprint,
            # only the sfs link is cleared.
            ref = s.get_tracked_session("native-1")
            assert ref is not None and ref.sfs_session_id is None
            assert not d.exists()
        finally:
            s.close()

    @staticmethod
    def _refuse(*a, **k):
        raise PermissionError("busy")

    def test_prune_rmtree_failure_keeps_rows(self, tmp_path: Path, monkeypatch):
        import sessionfs.store.pruner as pruner_mod
        from sessionfs.store.local import LocalStore
        from sessionfs.store.pruner import SessionPruner

        s = LocalStore(tmp_path / "s")
        s.initialize()
        try:
            sid, d = self._seed(s)
            monkeypatch.setattr(pruner_mod.shutil, "rmtree", self._refuse)
            with pytest.raises(PermissionError):
                SessionPruner(s.sessions_dir, s.index.conn)._delete_session(sid)
            assert s.index.get_session(sid) is not None
            assert s.get_tracked_session("native-1") is not None
        finally:
            s.close()

    def test_410_cleanup_rmtree_failure_keeps_rows(self, tmp_path: Path, monkeypatch):
        import sessionfs.sync.deleted_cleanup as cleanup_mod
        from sessionfs.store.local import LocalStore

        s = LocalStore(tmp_path / "s")
        s.initialize()
        try:
            sid, d = self._seed(s)
            monkeypatch.setattr(cleanup_mod.shutil, "rmtree", self._refuse)
            cleanup_mod.cleanup_deleted_session(sid, session_dir=d, store=s, base_dir=s.store_dir)
            assert d.exists()
            assert s.index.get_session(sid) is not None
            assert s.get_tracked_session("native-1") is not None
        finally:
            s.close()

    def test_410_cleanup_removes_folder_then_rows(self, tmp_path: Path, monkeypatch):
        import sessionfs.sync.deleted_cleanup as cleanup_mod
        from sessionfs.store.local import LocalStore

        s = LocalStore(tmp_path / "s")
        s.initialize()
        try:
            sid, d = self._seed(s)
            seen = []
            real = cleanup_mod.shutil.rmtree

            def spy(path, *a, **k):
                seen.append(s.index.get_session(sid) is not None)
                return real(path, *a, **k)

            monkeypatch.setattr(cleanup_mod.shutil, "rmtree", spy)
            cleanup_mod.cleanup_deleted_session(sid, session_dir=d, store=s, base_dir=s.store_dir)
            assert seen == [True]
            assert s.index.get_session(sid) is None
            assert s.get_tracked_session("native-1") is None
        finally:
            s.close()


# ---------------------------------------------------------------------------
# Review round 4
# ---------------------------------------------------------------------------


def _new_conv(ag: AgTree, cid: str, parent: str = "", app: str = "antigravity",
              text: str = "subagent work", with_transcript: bool = True) -> Path:
    conv = ag.conv(app, cid)
    logs = conv / ".system_generated" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    if with_transcript:
        steps = [
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
             "created_at": "2026-10-18T06:00:00Z", "content": f"task for {cid[:8]}"},
            {"step_index": 1, "source": "MODEL", "type": "PLANNER_RESPONSE",
             "created_at": "2026-10-18T06:00:01Z", "content": text},
        ]
        (logs / "transcript_full.jsonl").write_text("".join(json.dumps(s) + "\n" for s in steps))
    conn = ag.conns[0] if app == "antigravity" else ag.conns[1]
    conn.execute(
        "INSERT INTO conversation_summaries (conversation_id, title, app_data_dir, parent_conversation_id, "
        "last_modified_time) VALUES (?, ?, ?, ?, ?)",
        (cid, f"conv {cid[:8]}", app, parent, "2026-10-18 06:00:01+00:00"),
    )
    conn.commit()
    return conv


def _uuid(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-{n:012x}"


class TestRound4Recapture:
    def test_missing_manifest_is_stale(self, ag: AgTree, store):
        _watcher(ag, store).full_scan()
        live = store.get_session_dir(store.get_tracked_session(B).sfs_session_id)
        (live / "manifest.json").unlink()
        _watcher(ag, store).full_scan()
        assert (live / "manifest.json").exists()


class TestRound4Depth:
    def test_deep_chain_splits_into_sessions_instead_of_dropping(self, ag: AgTree, store, monkeypatch):
        from sessionfs.converters.antigravity_to_sfs import MAX_SUBAGENT_DEPTH, owner_of

        chain = [_uuid(100 + i) for i in range(MAX_SUBAGENT_DEPTH + 3)]  # depths 0..7
        for i, cid in enumerate(chain):
            _new_conv(ag, cid, parent=chain[i - 1] if i else "", text=f"level {i}")
        summaries = SummaryIndex().load(ag.gemini)
        deep = MAX_SUBAGENT_DEPTH + 1
        assert owner_of(chain[MAX_SUBAGENT_DEPTH], "antigravity", summaries, ag.gemini) == chain[0]
        assert owner_of(chain[deep], "antigravity", summaries, ag.gemini) == chain[deep]
        assert owner_of(chain[deep + 1], "antigravity", summaries, ag.gemini) == chain[deep]
        found = {s["session_id"] for s in discover_antigravity_sessions(ag.gemini, summaries)}
        assert chain[0] in found and chain[deep] in found and chain[1] not in found
        root = parse_antigravity_conversation(ag.conv("antigravity", chain[0]), summaries)
        assert [s["agent_id"] for s in root.sub_agents] == chain[1:deep]
        deeper = parse_antigravity_conversation(ag.conv("antigravity", chain[deep]), summaries)
        assert [s["agent_id"] for s in deeper.sub_agents] == [chain[deep + 1]]

    def test_node_limit_is_reported(self, ag: AgTree, monkeypatch):
        import sessionfs.converters.antigravity_to_sfs as conv

        for n in range(5):
            _new_conv(ag, _uuid(200 + n), parent=A)
        monkeypatch.setattr(conv, "MAX_SUBAGENT_NODES", 3)
        session = parse_antigravity_conversation(ag.conv("antigravity", A))
        assert session.subagents_truncated is True
        assert session.metadata["subagents_truncated"] is True


class TestRound4Retries:
    def test_file_event_failures_retry_until_success(self, ag: AgTree, store, monkeypatch):
        import sessionfs.converters.antigravity_to_sfs as conv

        w = _watcher(ag, store)
        w.full_scan()
        real = conv.parse_antigravity_conversation
        failures = {"left": 8}  # more than the old 5-attempt cap

        def flaky(conv_dir, *a, **k):
            if Path(conv_dir).name == B and failures["left"] > 0:
                failures["left"] -= 1
                raise RuntimeError("transient")
            return real(conv_dir, *a, **k)

        monkeypatch.setattr(conv, "parse_antigravity_conversation", flaky)
        _file_event(w, _append_step(ag, "antigravity-cli", B, 4, "retry me"))
        for _ in range(10):
            assert B in w._retry or "retry me" in json.dumps(_messages(store, B))
            w._retry = {k: (n, 0.0) for k, (n, _due) in w._retry.items()}
            w._last_event_time = 0.0
            w.process_events()
        assert B not in w._retry
        assert any("retry me" in json.dumps(m) for m in _messages(store, B))

    def test_backoff_grows(self, ag: AgTree, store, monkeypatch):
        w = _watcher(ag, store)
        from sessionfs.watchers.antigravity import CaptureOutcome

        monkeypatch.setattr(w, "_capture_session", lambda *a, **k: CaptureOutcome.FAILED)
        summaries = SummaryIndex().load(ag.gemini)
        delays = []
        for _ in range(4):
            import time as _t

            before = _t.monotonic()
            w._capture(B, ag.conv("antigravity-cli", B), 1.0, 1, summaries)
            delays.append(w._retry[B][1] - before)
        assert delays == sorted(delays) and delays[-1] > delays[0]


class TestRound4Linking:
    def test_newly_linked_unreadable_child_defers_parent(self, ag: AgTree, store):
        w = _watcher(ag, store)
        w.full_scan()
        e = _uuid(300)
        _new_conv(ag, e, parent=A, with_transcript=False)  # folder exists, transcript not yet
        _db_event(w, ag)
        assert A in w._retry
        assert all(s["agent_id"] != e for s in _manifest(store, A).get("sub_agents", []))
        logs = ag.conv("antigravity", e) / ".system_generated" / "logs"
        (logs / "transcript_full.jsonl").write_text(json.dumps({
            "step_index": 0, "source": "MODEL", "type": "PLANNER_RESPONSE",
            "created_at": "2026-10-18T06:00:00Z", "content": "now readable"}) + "\n")
        w._retry = {k: (n, 0.0) for k, (n, _due) in w._retry.items()}
        w._last_event_time = 0.0
        w.process_events()
        assert e in [s["agent_id"] for s in _manifest(store, A)["sub_agents"]]

    def test_detached_nested_subtree_lets_old_parent_shrink(self, ag: AgTree, store):
        g = _uuid(400)
        _new_conv(ag, g, parent=D, text="grandchild work")
        w = _watcher(ag, store)
        w.full_scan()
        assert {s["agent_id"] for s in _manifest(store, A)["sub_agents"]} == {D, g}
        ag.conns[0].execute("UPDATE conversation_summaries SET parent_conversation_id = '' WHERE conversation_id = ?", (D,))
        ag.conns[0].commit()
        _db_event(w, ag)
        assert [s["agent_id"] for s in _manifest(store, D)["sub_agents"]] == [g]
        assert "sub_agents" not in _manifest(store, A)  # shrank: G is held by D's session


class TestRound4JsonlCache:
    def test_same_size_rewrite_with_new_mtime_is_reparsed(self, tmp_path: Path):
        from sessionfs.converters import antigravity_io as io

        root = tmp_path / "antigravity"
        (root / "d").mkdir(parents=True)
        f = root / "d" / "t.jsonl"
        f.write_text('{"a": 1}\n{"b": 2}\n')
        cache = io.JsonlCache()
        cache.read(root, ("d", "t.jsonl"))
        st = f.stat()
        with open(f, "r+") as fh:
            fh.write('{"z": 9}\n')  # same length, earlier record changed
        os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
        assert cache.read(root, ("d", "t.jsonl"))[0] == [{"z": 9}, {"b": 2}]

    def test_growing_file_with_rewritten_prefix_is_reparsed(self, tmp_path: Path):
        from sessionfs.converters import antigravity_io as io

        root = tmp_path / "antigravity"
        (root / "d").mkdir(parents=True)
        f = root / "d" / "t.jsonl"
        f.write_text('{"a": 1}\n' + '{"tail": "' + "x" * 100 + '"}\n')
        cache = io.JsonlCache()
        cache.read(root, ("d", "t.jsonl"))
        content = f.read_text().replace('{"a": 1}', '{"q": 7}')
        f.write_text(content + '{"c": 3}\n')  # first record changed, tail identical, file grew
        records = cache.read(root, ("d", "t.jsonl"))[0]
        assert records[0] == {"q": 7} and records[-1] == {"c": 3} and len(records) == 3



# ---------------------------------------------------------------------------
# Review round 5
# ---------------------------------------------------------------------------


class TestRound5:
    def test_standalone_child_retired_only_after_parent_is_stored(self, ag: AgTree, store, monkeypatch):
        import sessionfs.converters.antigravity_to_sfs as conv

        desk = ag.conns[0]
        desk.execute("UPDATE conversation_summaries SET parent_conversation_id = '' WHERE conversation_id = ?", (D,))
        desk.commit()
        w = _watcher(ag, store)
        w.full_scan()
        child_sfs = store.get_tracked_session(D).sfs_session_id
        # A stale parent manifest that already lists D must not count as proof.
        parent_dir = store.get_session_dir(sfs_session_id_for(A))
        manifest = json.loads((parent_dir / "manifest.json").read_text())
        manifest["sub_agents"] = [{"agent_id": D, "message_count": 2}]
        (parent_dir / "manifest.json").write_text(json.dumps(manifest))
        real_parse = conv.parse_antigravity_conversation

        def fail_parent(conv_dir, *a, **k):
            if Path(conv_dir).name == A:
                raise RuntimeError("transient")
            return real_parse(conv_dir, *a, **k)

        monkeypatch.setattr(conv, "parse_antigravity_conversation", fail_parent)
        desk.execute("UPDATE conversation_summaries SET parent_conversation_id = ? WHERE conversation_id = ?", (A, D))
        desk.commit()
        _db_event(w, ag)
        assert store.get_session_dir(child_sfs) is not None  # not retired
        monkeypatch.setattr(conv, "parse_antigravity_conversation", real_parse)
        w._retry = {k: (n, 0.0) for k, (n, _due) in w._retry.items()}
        w._summaries_changed.set()
        _db_event(w, ag)
        assert store.get_session_dir(child_sfs) is None  # retired once A was STORED

    def test_cycle_members_are_all_captured(self, ag: AgTree):
        x, y = _uuid(500), _uuid(501)
        _new_conv(ag, x, parent=y)
        _new_conv(ag, y, parent=x)
        summaries = SummaryIndex().load(ag.gemini)
        found = {s["session_id"] for s in discover_antigravity_sessions(ag.gemini, summaries)}
        assert x in found  # the smallest id roots the cycle
        session = parse_antigravity_conversation(ag.conv("antigravity", x), summaries)
        assert [s["agent_id"] for s in session.sub_agents] == [y]

    def test_node_limit_overflow_becomes_standalone(self, ag: AgTree, monkeypatch):
        import sessionfs.converters.antigravity_to_sfs as conv

        kids = [_uuid(600 + n) for n in range(4)]
        for k in kids:
            _new_conv(ag, k, parent=A)
        monkeypatch.setattr(conv, "MAX_SUBAGENT_NODES", 2)
        summaries = SummaryIndex().load(ag.gemini)
        found = {s["session_id"] for s in discover_antigravity_sessions(ag.gemini, summaries)}
        parsed = parse_antigravity_conversation(ag.conv("antigravity", A), summaries)
        folded = {s["agent_id"] for s in parsed.sub_agents}
        assert len(folded) == 2
        overflow = (set(kids) | {D}) - folded
        assert overflow and overflow <= found  # every pushed-out child is its own session
        assert parsed.subagents_truncated

    def test_partial_or_empty_child_defers_the_parent(self, ag: AgTree, store):
        from sessionfs.watchers.antigravity import CaptureOutcome

        w = _watcher(ag, store)
        partial = _uuid(700)
        conv_dir = _new_conv(ag, partial, parent=A, with_transcript=False)
        logs = conv_dir / ".system_generated" / "logs"
        (logs / "transcript_full.jsonl").write_text('{"step_index": 0, "type": "PLANNER_RESP')  # mid-append
        summaries = SummaryIndex().load(ag.gemini)
        assert w._capture_session(A, ag.conv("antigravity", A), 1.0, 1, summaries) is CaptureOutcome.DEFERRED
        (logs / "transcript_full.jsonl").write_text("")  # readable but nothing in it yet
        summaries = SummaryIndex().load(ag.gemini)
        assert w._capture_session(A, ag.conv("antigravity", A), 1.0, 1, summaries) is CaptureOutcome.DEFERRED

    def test_restored_session_is_recaptured_without_a_source_change(self, ag: AgTree, store):
        from sessionfs.store.deleted import mark_deleted, remove_exclusion

        sfs_b = sfs_session_id_for(B)
        mark_deleted(sfs_b, "local", base_dir=store.store_dir)
        w = _watcher(ag, store)
        w.full_scan()
        assert store.get_session_dir(sfs_b) is None and B in w._excluded_seen
        remove_exclusion(sfs_b, base_dir=store.store_dir)  # sfs restore
        w.full_scan()
        assert store.get_session_dir(sfs_b) is not None
        assert B not in w._excluded_seen

    def test_mcp_install_always_writes_the_current_target(self):
        from sessionfs.cli.cmd_mcp import _install_antigravity

        g = Path.home() / ".gemini"
        legacy = g / "antigravity" / "mcp_config.json"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("{}")  # a legacy-only install
        entry = {"command": "/usr/local/bin/sfs", "args": ["mcp", "serve"]}
        _install_antigravity(entry)
        current = g / "config" / "mcp_config.json"
        assert json.loads(current.read_text())["mcpServers"]["sessionfs"] == entry
        assert json.loads(legacy.read_text())["mcpServers"]["sessionfs"] == entry


# ---------------------------------------------------------------------------
# Review round 6
# ---------------------------------------------------------------------------


class TestRound6:
    def _cli_store(self):
        from sessionfs.cli.common import open_store

        return open_store()

    def test_recapture_uses_the_ownership_pipeline(self, ag: AgTree):
        """`sfs recapture <parent>` after a child detached: the child's own
        session is captured first, then the parent shrinks — never the
        other way round."""
        from sessionfs.cli.cmd_recapture import _recapture_antigravity

        s = self._cli_store()
        try:
            _watcher(ag, s).full_scan()
            ag.conns[0].execute("UPDATE conversation_summaries SET parent_conversation_id = '' WHERE conversation_id = ?", (D,))
            ag.conns[0].commit()
            ref = s.get_tracked_session(A)
            _recapture_antigravity(s, ref.sfs_session_id, ref, Path(ref.native_path))
            assert _manifest(s, D)["source"]["original_session_id"] == D
            assert "sub_agents" not in _manifest(s, A)
        finally:
            s.close()

    def test_recapture_refuses_while_a_subagent_is_being_written(self, ag: AgTree, capsys):
        from sessionfs.cli.cmd_recapture import _recapture_antigravity

        s = self._cli_store()
        try:
            _watcher(ag, s).full_scan()
            before = _messages(s, A)
            _new_conv(ag, _uuid(800), parent=A, with_transcript=False)
            ref = s.get_tracked_session(A)
            with pytest.raises(SystemExit):
                _recapture_antigravity(s, ref.sfs_session_id, ref, Path(ref.native_path))
            assert "not ready" in capsys.readouterr().err
            assert _messages(s, A) == before
        finally:
            s.close()

    def test_recapture_of_a_subagent_points_at_its_owner(self, ag: AgTree, capsys):
        from sessionfs.cli.cmd_recapture import _recapture_antigravity
        from sessionfs.watchers.base import NativeSessionRef

        s = self._cli_store()
        try:
            ref = NativeSessionRef(
                tool="antigravity", native_session_id=D,
                native_path=str(ag.conv("antigravity", D)), sfs_session_id=sfs_session_id_for(D),
            )
            with pytest.raises(SystemExit):
                _recapture_antigravity(s, ref.sfs_session_id, ref, Path(ref.native_path))
            assert A in capsys.readouterr().err
            assert s.get_session_dir(sfs_session_id_for(D)) is None
        finally:
            s.close()

    def test_init_magic_moment_skips_a_capture_with_pending_subagents(self, ag: AgTree, capsys):
        from sessionfs.cli.cmd_init import _capture_one_session
        from sessionfs.cli.common import open_store

        _new_conv(ag, _uuid(900), parent=A, with_transcript=False)
        info = {s["session_id"]: s for s in discover_antigravity_sessions(ag.gemini)}[A]
        info["tool"] = "antigravity"
        assert _capture_one_session(info) is None
        assert "still being written" in capsys.readouterr().out
        s = open_store()
        try:
            assert s.get_session_dir(sfs_session_id_for(A)) is None
        finally:
            s.close()

    def test_linked_child_without_any_folder_is_pending(self, ag: AgTree):
        ghost, pb_only = _uuid(910), _uuid(911)
        for cid in (ghost, pb_only):
            ag.conns[0].execute(
                "INSERT INTO conversation_summaries (conversation_id, title, app_data_dir, parent_conversation_id)"
                " VALUES (?, 't', 'antigravity', ?)", (cid, A),
            )
        ag.conns[0].commit()
        (ag.gemini / "antigravity" / "conversations" / f"{pb_only}.pb").write_bytes(b"\x00secret")
        session = parse_antigravity_conversation(ag.conv("antigravity", A))
        assert ghost in session.pending_subagents
        assert pb_only in session.unreadable_subagents and pb_only not in session.pending_subagents

    def test_skipped_counts_refresh_on_events(self, ag: AgTree, store):
        from sessionfs.watchers.antigravity import _AntigravityEventHandler

        w = _watcher(ag, store)
        w.full_scan()
        assert w.get_status().sessions_skipped == {"encrypted": 1}
        new_pb = ag.gemini / "antigravity" / "conversations" / f"{_uuid(920)}.pb"
        new_pb.write_bytes(b"\x00")
        handler = _AntigravityEventHandler(
            w._event_queue, w._event_lock, w._rescan, w._summaries_changed, w._skipped_dirty,
        )

        class _Ev:
            src_path = str(new_pb)
            is_directory = False

        handler.on_created(_Ev())  # type: ignore[arg-type]
        assert w._skipped_dirty.is_set()
        w._last_event_time = 0.0
        w.process_events()
        assert w.get_status().sessions_skipped == {"encrypted": 2}

    def test_conversations_dir_is_watched(self, ag: AgTree, store):
        w = _watcher(ag, store)
        w.start_watching()
        try:
            assert str(ag.gemini / "antigravity" / "conversations") in w.get_status().watch_paths
        finally:
            w.stop_watching()


class TestRound7:
    def _cli_store(self):
        from sessionfs.cli.common import open_store

        return open_store()

    def test_recapture_defers_while_summaries_are_busy(self, ag: AgTree, capsys, monkeypatch):
        import sessionfs.converters.antigravity_to_sfs as conv
        from sessionfs.cli.cmd_recapture import _recapture_antigravity

        s = self._cli_store()
        try:
            _watcher(ag, s).full_scan()
            before = _manifest(s, A)
            monkeypatch.setattr(conv, "read_summaries_db", lambda *a, **k: None)
            ref = s.get_tracked_session(A)
            with pytest.raises(SystemExit):
                _recapture_antigravity(s, ref.sfs_session_id, ref, Path(ref.native_path))
            assert "busy" in capsys.readouterr().err
            assert _manifest(s, A) == before
        finally:
            s.close()

    def test_recapture_retires_a_newly_absorbed_child(self, ag: AgTree):
        from sessionfs.cli.cmd_recapture import _recapture_antigravity

        s = self._cli_store()
        try:
            desk = ag.conns[0]
            desk.execute("UPDATE conversation_summaries SET parent_conversation_id = '' WHERE conversation_id = ?", (D,))
            desk.commit()
            _watcher(ag, s).full_scan()
            child_sfs = s.get_tracked_session(D).sfs_session_id
            desk.execute("UPDATE conversation_summaries SET parent_conversation_id = ? WHERE conversation_id = ?", (A, D))
            desk.commit()
            ref = s.get_tracked_session(A)
            _recapture_antigravity(s, ref.sfs_session_id, ref, Path(ref.native_path))
            assert any(m.get("agent_id") == D for m in _messages(s, A))
            assert s.get_session_dir(child_sfs) is None
            assert s.get_tracked_session(D) is None
        finally:
            s.close()

    def test_annotation_title_change_recaptures(self, ag: AgTree, store):
        cid = _uuid(700)
        _new_conv(ag, cid)
        ag.conns[0].execute("UPDATE conversation_summaries SET title = '' WHERE conversation_id = ?", (cid,))
        ag.conns[0].commit()
        note = ag.gemini / "antigravity" / "annotations" / f"{cid}.pbtxt"
        note.write_text('title:"First Title"\n')
        w = _watcher(ag, store)
        w.full_scan()
        assert _manifest(store, cid)["title"] == "First Title"
        before = conversation_fingerprint(ag.conv("antigravity", cid))
        note.write_text('title:"A Much Better Title"\n')
        assert conversation_fingerprint(ag.conv("antigravity", cid)) != before
        assert conversation_id_from_path(note) == (ag.gemini / "antigravity", cid)
        _file_event(w, note)
        assert _manifest(store, cid)["title"] == "A Much Better Title"

    def test_annotations_dir_is_watched(self, ag: AgTree, store):
        w = _watcher(ag, store)
        w.start_watching()
        try:
            assert str(ag.gemini / "antigravity" / "annotations") in w.get_status().watch_paths
        finally:
            w.stop_watching()

    def _handler(self, w):
        from sessionfs.watchers.antigravity import _AntigravityEventHandler

        return _AntigravityEventHandler(
            w._event_queue, w._event_lock, w._rescan, w._summaries_changed, w._skipped_dirty,
        )

    def test_skipped_counts_drop_on_delete_and_move_out(self, ag: AgTree, store, tmp_path: Path):
        w = _watcher(ag, store)
        w.full_scan()
        convs = ag.gemini / "antigravity" / "conversations"
        extra = convs / f"{_uuid(930)}.pb"
        extra.write_bytes(b"\x00")
        w._skipped_dirty.set()
        w._last_event_time = 0.0
        w.process_events()
        assert w.get_status().sessions_skipped == {"encrypted": 2}
        handler = self._handler(w)

        extra.unlink()

        class _Del:
            src_path = str(extra)
            is_directory = False

        handler.on_deleted(_Del())  # type: ignore[arg-type]
        assert w._skipped_dirty.is_set()
        w._last_event_time = 0.0
        w.process_events()
        assert w.get_status().sessions_skipped == {"encrypted": 1}

        existing = next(convs.glob("*.pb"))
        moved = tmp_path / "elsewhere.pb"
        existing.rename(moved)

        class _Mv:
            src_path = str(existing)
            dest_path = str(moved)
            is_directory = False

        handler.on_moved(_Mv())  # type: ignore[arg-type]
        assert w._skipped_dirty.is_set()
        w._last_event_time = 0.0
        w.process_events()
        assert w.get_status().sessions_skipped in ({}, {"encrypted": 0})

    def test_creating_conversations_or_annotations_dir_rescans(self, ag: AgTree, store):
        w = _watcher(ag, store)
        handler = self._handler(w)
        for name in ("conversations", "annotations"):
            w._rescan.clear()

            class _Ev:
                src_path = str(ag.gemini / "antigravity-cli" / name)
                is_directory = True

            handler.on_created(_Ev())  # type: ignore[arg-type]
            assert w._rescan.is_set(), name


class TestRound8:
    def test_parent_shrink_is_not_hidden_by_a_growing_subagent(self, ag: AgTree, store):
        from types import SimpleNamespace

        from sessionfs.watchers.antigravity import CaptureOutcome

        w = _watcher(ag, store)
        w.full_scan()
        stored ={"stats": {"message_count": 10}, "sub_agents": [{"agent_id": D, "message_count": 2}]}
        # Parent thread 8 -> 6 while the subagent grows 2 -> 5: total 10 -> 11.
        shrunk = SimpleNamespace(message_count=11, sub_agents=[{"agent_id": D, "message_count": 5}])
        assert w._shrink_verdict(A, shrunk, stored, w._last_summaries) is CaptureOutcome.UNCHANGED
        grown = SimpleNamespace(message_count=13, sub_agents=[{"agent_id": D, "message_count": 5}])
        assert w._shrink_verdict(A, grown, stored, w._last_summaries) is None

    def test_fresh_daemon_with_a_busy_summaries_db_captures_nothing(self, ag: AgTree, store, monkeypatch):
        import sessionfs.converters.antigravity_to_sfs as conv

        real = conv.read_summaries_db
        desktop = ag.gemini / "antigravity" / "conversation_summaries.db"

        def busy(db, *a, **k):
            return None if Path(db) == desktop else real(db, *a, **k)

        monkeypatch.setattr(conv, "read_summaries_db", busy)
        w = _watcher(ag, store)
        w.full_scan()
        assert store.list_sessions() == []
        w._last_event_time = 0.0
        w.process_events()  # still busy: still nothing
        assert store.list_sessions() == []
        monkeypatch.setattr(conv, "read_summaries_db", real)
        w._last_event_time = 0.0
        w.process_events()
        assert store.get_tracked_session(B) is not None
        assert any(m.get("agent_id") == D for m in _messages(store, A))
        assert store.get_tracked_session(D) is None

    def test_owner_capture_retires_a_standalone_child_without_a_summaries_event(self, ag: AgTree, store):
        desk = ag.conns[0]
        desk.execute("UPDATE conversation_summaries SET parent_conversation_id = '' WHERE conversation_id = ?", (D,))
        desk.commit()
        w = _watcher(ag, store)
        w.full_scan()
        child_sfs = store.get_tracked_session(D).sfs_session_id
        desk.execute("UPDATE conversation_summaries SET parent_conversation_id = ? WHERE conversation_id = ?", (A, D))
        desk.commit()
        # Only a transcript event: the summaries flag is never raised.
        _file_event(w, _append_step(ag, "antigravity", A, 21, "more work"))
        assert any(m.get("agent_id") == D for m in _messages(store, A))
        assert store.get_session_dir(child_sfs) is None


class TestRound9:
    def _busy(self, monkeypatch, db: Path | None = None):
        import sessionfs.converters.antigravity_to_sfs as conv

        real = conv.read_summaries_db
        monkeypatch.setattr(
            conv, "read_summaries_db",
            lambda p, *a, **k: None if db is None or Path(p) == db else real(p, *a, **k),
        )
        return real

    def test_unchanged_db_with_failed_read_is_current(self, ag: AgTree, monkeypatch):
        idx = SummaryIndex()
        _rows, status = idx.load_with_status(ag.gemini)
        assert SummaryIndex.is_current(status)
        self._busy(monkeypatch)
        rows, status = idx.load_with_status(ag.gemini, force=True)
        assert set(status.values()) == {SummaryIndex.CACHED} and rows

    def test_fallback_rows_are_never_captured_and_events_are_kept(self, ag: AgTree, store, monkeypatch):
        import sessionfs.converters.antigravity_to_sfs as conv

        w = _watcher(ag, store)
        w.full_scan()
        before = _manifest(store, B)
        cli = ag.conns[1]
        cli.execute("UPDATE conversation_summaries SET title = 'Renamed Later' WHERE conversation_id = ?", (B,))
        cli.commit()
        real = self._busy(monkeypatch, ag.gemini / "antigravity-cli" / "conversation_summaries.db")
        step = _append_step(ag, "antigravity-cli", B, 30, "fresh step")
        _file_event(w, step)
        assert _manifest(store, B) == before  # stale (cached) rows: no capture
        assert str(step) in w._event_queue  # the work is kept
        monkeypatch.setattr(conv, "read_summaries_db", real)
        w._last_event_time = 0.0
        w.process_events()
        assert _manifest(store, B)["title"] == "Renamed Later"
        assert "fresh step" in json.dumps(_messages(store, B))

    def test_init_first_capture_checks_freshness_and_ownership(self, ag: AgTree, monkeypatch, capsys):
        from sessionfs.cli.cmd_init import _capture_one_session
        from sessionfs.cli.common import open_store

        infos = {s["session_id"]: s for s in discover_antigravity_sessions(ag.gemini)}
        child = dict(infos[A], session_id=D, path=str(ag.conv("antigravity", D)), tool="antigravity")
        assert _capture_one_session(child) is None  # a subagent is never its own session
        real = self._busy(monkeypatch, ag.gemini / "antigravity" / "conversation_summaries.db")
        parent = dict(infos[A], tool="antigravity")
        assert _capture_one_session(parent) is None
        assert "busy" in capsys.readouterr().out
        s = open_store()
        try:
            assert s.get_session_dir(sfs_session_id_for(A)) is None
            assert s.get_session_dir(sfs_session_id_for(D)) is None
        finally:
            s.close()
        import sessionfs.converters.antigravity_to_sfs as conv

        monkeypatch.setattr(conv, "read_summaries_db", real)
        assert _capture_one_session(parent) is not None

    def test_retry_backoff_does_not_overflow(self, ag: AgTree, store, monkeypatch):
        import time

        from sessionfs.watchers.antigravity import RETRY_MAX_S, CaptureOutcome

        w = _watcher(ag, store)
        monkeypatch.setattr(w, "_capture_session", lambda *a, **k: CaptureOutcome.DEFERRED)
        w._retry[A] = (5000, 0.0)
        assert w._capture(A, ag.conv("antigravity", A), 0.0, 0, {}) is CaptureOutcome.DEFERRED
        attempts, due = w._retry[A]
        assert attempts == 5001 and due <= time.monotonic() + RETRY_MAX_S

    def test_unreadable_summaries_degrade_then_recover(self, ag: AgTree, store, monkeypatch):
        import sessionfs.converters.antigravity_to_sfs as conv
        from sessionfs.watchers.antigravity import SUMMARY_FAILURE_THRESHOLD

        real = self._busy(monkeypatch, ag.gemini / "antigravity" / "conversation_summaries.db")
        w = _watcher(ag, store)
        w.full_scan()
        assert w.get_status().health == "healthy"
        for _ in range(SUMMARY_FAILURE_THRESHOLD):
            w._last_event_time = 0.0
            w.process_events()
        status = w.get_status()
        assert status.health == "degraded" and "summaries" in (status.last_error or "")
        monkeypatch.setattr(conv, "read_summaries_db", real)
        w._last_event_time = 0.0
        w.process_events()
        assert w.get_status().health == "healthy"
        assert store.get_tracked_session(A) is not None

    def test_deleted_output_txt_recaptures(self, ag: AgTree, store):
        from sessionfs.watchers.antigravity import _AntigravityEventHandler

        out = ag.conv("antigravity", A) / ".system_generated" / "steps" / "17" / "output.txt"
        out.parent.mkdir(parents=True)
        out.write_text("late output " + "x" * 500)
        w = _watcher(ag, store)
        w.full_scan()
        assert "late output" in json.dumps(_messages(store, A))
        out.unlink()
        handler = _AntigravityEventHandler(w._event_queue, w._event_lock, w._rescan, w._summaries_changed)

        class _Ev:
            src_path = str(out)
            is_directory = False

        handler.on_deleted(_Ev())  # type: ignore[arg-type]
        assert w._event_queue == [str(out)]
        w._last_event_time = 0.0
        w.process_events()
        assert "late output" not in json.dumps(_messages(store, A))


class TestRound10:
    def test_unreadable_wal_is_never_a_snapshot(self, ag: AgTree, monkeypatch):
        import sessionfs.converters.antigravity_io as io_mod

        db = ag.gemini / "antigravity" / "conversation_summaries.db"
        assert Path(str(db) + "-wal").stat().st_size > 0  # rows live only in the WAL
        assert read_summaries_db(db)
        real = io_mod.read_bytes

        def no_wal(root, parts, *a, **k):
            return None if parts[-1].endswith("-wal") else real(root, parts, *a, **k)

        monkeypatch.setattr(io_mod, "read_bytes", no_wal)
        assert read_summaries_db(db) is None  # not "[]" and not the DB without its WAL

    def test_symlinked_wal_is_never_a_snapshot(self, tmp_path: Path):
        root = tmp_path / "antigravity"
        root.mkdir()
        db = root / "conversation_summaries.db"
        conn = sqlite3.connect(str(db))
        conn.execute(f"CREATE TABLE conversation_summaries ({_COLUMNS})")
        conn.execute(
            "INSERT INTO conversation_summaries (conversation_id, title) VALUES (?, ?)",
            (A, "T"),
        )
        conn.commit()
        conn.close()
        # A symlinked -wal is present but not a regular file: it must fail the
        # read rather than being treated as absent, which would drop its rows
        # and report the copy as a complete snapshot.
        (tmp_path / "wal-target").write_bytes(b"not a real wal")
        (Path(str(db) + "-wal")).symlink_to(tmp_path / "wal-target")
        assert read_summaries_db(db) is None
        # A dangling symlink is still a present link, not an absent WAL.
        (Path(str(db) + "-wal")).unlink()
        (Path(str(db) + "-wal")).symlink_to(tmp_path / "does-not-exist")
        assert read_summaries_db(db) is None

    def test_oversized_line_is_streamed_with_bounded_memory(self, tmp_path: Path, monkeypatch):
        import hashlib
        import tracemalloc

        import sessionfs.converters.antigravity_io as io_mod

        monkeypatch.setattr(io_mod, "MAX_LINE_BYTES", 1024)
        monkeypatch.setattr(io_mod, "SKIP_CHUNK_BYTES", 64 * 1024)
        d = tmp_path / "t"
        d.mkdir()
        f = d / "log.jsonl"
        big = 24 * 1024 * 1024
        with open(f, "wb") as out:
            out.write(b'{"n": 1}\n')
            out.write(b'{"pad": "' + b"x" * big)  # no newline yet: mid-append
        cache = io_mod.JsonlCache()
        tracemalloc.start()
        try:
            recs, errs = cache.read(d, ("log.jsonl",))
            assert recs == [{"n": 1}] and any("incomplete" in e for e in errs)
            with open(f, "ab") as out:
                out.write(b'"}\n{"n": 2}\n')
            recs, errs = cache.read(d, ("log.jsonl",))
            _cur, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        assert recs == [{"n": 1}, {"n": 2}]
        assert any("too large" in e for e in errs)
        assert peak < 4 * 1024 * 1024  # about one chunk, not the 24 MiB line
        # The committed prefix hash matches the file, so an append resumes.
        state = next(iter(cache._files.values()))
        assert state.prefix_sha == hashlib.sha256(f.read_bytes()).hexdigest()
        with open(f, "ab") as out:
            out.write(b'{"n": 3}\n')
        recs, _errs = cache.read(d, ("log.jsonl",))
        assert recs == [{"n": 1}, {"n": 2}, {"n": 3}]


class TestRound11:
    def _handler(self, w):
        from sessionfs.watchers.antigravity import _AntigravityEventHandler

        return _AntigravityEventHandler(
            w._event_queue, w._event_lock, w._rescan, w._summaries_changed, w._skipped_dirty,
        )

    def test_removed_steps_directory_queues_recapture(self, ag: AgTree, store, tmp_path: Path):
        out = ag.conv("antigravity", A) / ".system_generated" / "steps" / "17" / "output.txt"
        out.parent.mkdir(parents=True)
        out.write_text("late output " + "x" * 500)
        w = _watcher(ag, store)
        w.full_scan()
        assert "late output" in json.dumps(_messages(store, A))
        shutil.rmtree(out.parent)  # only a directory event is produced
        handler = self._handler(w)

        class _Ev:
            src_path = str(out.parent)
            is_directory = True

        handler.on_deleted(_Ev())  # type: ignore[arg-type]
        assert w._event_queue == [str(out.parent)]
        w._last_event_time = 0.0
        w.process_events()
        assert "late output" not in json.dumps(_messages(store, A))

        moved = tmp_path / "elsewhere"
        steps = ag.conv("antigravity", A) / ".system_generated" / "steps"

        class _Mv:
            src_path = str(steps)
            dest_path = str(moved)
            is_directory = True

        w._event_queue.clear()
        handler.on_moved(_Mv())  # type: ignore[arg-type]
        assert w._event_queue == [str(steps)]

    def test_removed_watched_directories_trigger_rescan(self, ag: AgTree, store):
        w = _watcher(ag, store)
        handler = self._handler(w)
        watched = [
            ag.gemini / "antigravity",  # a storage root: its observers die with it
            ag.gemini / "antigravity" / "brain",
            ag.gemini / "antigravity" / "conversations",
            ag.gemini / "antigravity" / "annotations",
        ]
        for gone in watched:
            w._rescan.clear()

            class _Ev:
                src_path = str(gone)
                is_directory = True

            handler.on_deleted(_Ev())  # type: ignore[arg-type]
            assert w._rescan.is_set(), gone.name
        assert w._event_queue == []  # root/brain removals are rescans, not recaptures
        w._skipped_dirty.clear()

        class _Ev2:
            src_path = str(ag.gemini / "antigravity" / "conversations")
            is_directory = True

        handler.on_deleted(_Ev2())  # type: ignore[arg-type]
        assert w._skipped_dirty.is_set()  # the .pb/.db counts may have gone with it

    def test_unrelated_directory_removal_is_ignored(self, ag: AgTree, store):
        w = _watcher(ag, store)
        handler = self._handler(w)

        class _Ev:
            src_path = str(ag.gemini / "tmp")
            is_directory = True

        handler.on_deleted(_Ev())  # type: ignore[arg-type]
        assert not w._rescan.is_set()
        assert w._event_queue == []

    def test_owner_without_summary_row_uses_the_transcript_fallback(self, ag: AgTree, store):
        """A detached child whose summary row disappeared is still captured
        through _owner_stored_now (found on disk across roots), so its old
        parent is allowed to shrink instead of deferring forever."""
        w = _watcher(ag, store)
        w.full_scan()
        assert [s["agent_id"] for s in _manifest(store, A)["sub_agents"]] == [D]
        ag.conns[0].execute("DELETE FROM conversation_summaries WHERE conversation_id = ?", (D,))
        ag.conns[0].commit()
        summaries = SummaryIndex().load(ag.gemini)
        assert w._app_of(D, summaries) is None
        assert w._owner_stored_now(D, summaries) is True  # capture_now's fallback
        ref = store.get_tracked_session(D)
        assert ref is not None
        assert w._held_elsewhere(D, summaries) is True
        _file_event(w, _append_step(ag, "antigravity", A, 21, "more work"))
        assert "sub_agents" not in _manifest(store, A)  # the parent shrank

    def test_deleted_subagent_source_is_carried_forward_indefinitely(self, ag: AgTree, store):
        """Deleting a subagent's summary row AND transcript at the source no
        longer drops it from the parent after a few scans: the stored sidechain
        is carried forward unchanged, and the parent keeps capturing its own
        new messages."""
        w = _watcher(ag, store)
        w.full_scan()
        assert [s["agent_id"] for s in _manifest(store, A)["sub_agents"]] == [D]
        before = _messages(store, A)
        d_before = [m for m in before if m.get("agent_id") == D]
        assert d_before

        ag.conns[0].execute("DELETE FROM conversation_summaries WHERE conversation_id = ?", (D,))
        ag.conns[0].commit()
        shutil.rmtree(ag.conv("antigravity", D))

        # The fixture transcript ends with a half-written final line; close it
        # out so the appended step lands as its own well-formed line.
        log = ag.conv("antigravity", A) / ".system_generated" / "logs" / "transcript_full.jsonl"
        with open(log, "a") as f:
            f.write("\n")
        _file_event(w, _append_step(ag, "antigravity", A, 22, "more work"))
        after = _messages(store, A)
        assert any(m.get("agent_id") == D for m in after)  # still carried
        assert any("more work" in json.dumps(m) for m in after)  # own new message captured
        assert [m for m in after if m.get("agent_id") == D] == d_before  # unchanged sidechain
        assert [s["agent_id"] for s in _manifest(store, A)["sub_agents"]] == [D]

        # A further scan still carries it: absence is never treated as a
        # deletion that releases the stored sidechain.
        w.full_scan()
        assert any(m.get("agent_id") == D for m in _messages(store, A))

    def test_linked_child_came_back_shorter_is_carried_forward(self, ag: AgTree, store):
        """A subagent that is still linked but parses shorter than the stored
        sidechain (compressed or truncated at the source) is carried forward:
        its stored sidechain is kept unchanged instead of shrinking the parent."""
        w = _watcher(ag, store)
        w.full_scan()
        before = _messages(store, A)
        d_before = [m for m in before if m.get("agent_id") == D]
        assert len(d_before) == 2

        # Truncate D's transcript to a single step so it parses shorter than
        # the sidechain the parent stored.
        d_log = ag.conv("antigravity", D) / ".system_generated" / "logs" / "transcript_full.jsonl"
        d_log.write_text(d_log.read_text().splitlines()[0] + "\n")

        log = ag.conv("antigravity", A) / ".system_generated" / "logs" / "transcript_full.jsonl"
        with open(log, "a") as f:
            f.write("\n")
        _file_event(w, _append_step(ag, "antigravity", A, 22, "parent keeps going"))

        after = _messages(store, A)
        assert [m for m in after if m.get("agent_id") == D] == d_before  # unchanged sidechain
        assert any("parent keeps going" in json.dumps(m) for m in after)
        refs = [s for s in _manifest(store, A)["sub_agents"] if s["agent_id"] == D]
        assert len(refs) == 1 and refs[0]["message_count"] == 2

    def test_detached_child_dropped_via_compression_guarded_owner(self, ag: AgTree, store, monkeypatch):
        """A detached child whose owner already stores it is dropped from its
        old parent even when the owner's own fresh capture would be
        compression-guarded: proof comes from the owner's stored manifest, not
        from a fresh capture that would refuse to overwrite."""
        import sessionfs.converters.antigravity_to_sfs as conv

        w = _watcher(ag, store)
        w.full_scan()
        real_parse = conv.parse_antigravity_conversation

        ag.conns[0].execute("UPDATE conversation_summaries SET parent_conversation_id = '' WHERE conversation_id = ?", (D,))
        ag.conns[0].commit()

        # Plant a standalone capture of D holding all 2 of its messages; a
        # fresh capture of D parses to fewer (compression-guarded), so only the
        # stored capture (manifest AND messages) can prove the child is held
        # elsewhere.
        d_sfs = sfs_session_id_for(D)
        d_dir = store.allocate_session_dir(d_sfs)
        (d_dir / "manifest.json").write_text(json.dumps({
            "source": {"original_session_id": D},
            "stats": {"message_count": 2},
        }))
        with open(d_dir / "messages.jsonl", "w") as f:
            for _ in range(2):
                f.write(json.dumps({"role": "assistant",
                                    "content": [{"type": "text", "text": "held elsewhere"}]}) + "\n")

        def compressed_for_child(conv_dir, *a, **k):
            session = real_parse(conv_dir, *a, **k)
            if Path(conv_dir).name == D:
                session.messages = session.messages[:1]
                session.message_count = 1
            return session

        monkeypatch.setattr(conv, "parse_antigravity_conversation", compressed_for_child)

        log = ag.conv("antigravity", A) / ".system_generated" / "logs" / "transcript_full.jsonl"
        with open(log, "a") as f:
            f.write("\n")
        _file_event(w, _append_step(ag, "antigravity", A, 22, "parent keeps going"))
        assert "sub_agents" not in _manifest(store, A)  # dropped, not stuck

    def test_unreadable_carry_keeps_stored_capture_and_fails_health(self, ag: AgTree, store):
        """When a needed carry-forward cannot read the stored sidechain, the
        stored capture is kept (the lossy one is not installed) and the health
        tracker records the failure."""
        w = _watcher(ag, store)
        w.full_scan()
        stored = _manifest(store, A)
        old_count = stored["stats"]["message_count"]
        assert [s["agent_id"] for s in stored["sub_agents"]] == [D]

        ag.conns[0].execute("UPDATE conversation_summaries SET parent_conversation_id = '' WHERE conversation_id = ?", (D,))
        ag.conns[0].commit()

        # Make A's stored messages unreadable so the carry cannot read the
        # sidechain it must preserve.
        a_dir = store.get_session_dir(sfs_session_id_for(A))
        (a_dir / "messages.jsonl").write_text("not json\n")

        log = ag.conv("antigravity", A) / ".system_generated" / "logs" / "transcript_full.jsonl"
        with open(log, "a") as f:
            f.write("\n")
        _file_event(w, _append_step(ag, "antigravity", A, 22, "parent keeps going"))

        after = _manifest(store, A)
        assert after["stats"]["message_count"] == old_count  # stored capture kept
        assert [s["agent_id"] for s in after["sub_agents"]] == [D]
        assert "kept its stored capture" in (w._capture_health.last_error or "")


# ---------------------------------------------------------------------------
# Review round 12
# ---------------------------------------------------------------------------


class TestRound12:
    def test_detached_child_dropped_only_once_an_equally_rich_home_is_stored(self, ag: AgTree, store):
        """A detached subagent is proven held elsewhere only once the session
        that now owns it stores at least as many of its messages as the parent
        did — a poorer stored capture is not proof, so the parent carries the
        stored sidechain forward instead of dropping it."""
        w = _watcher(ag, store)
        w.full_scan()
        # Detach D: it becomes its own top-level session.
        ag.conns[0].execute(
            "UPDATE conversation_summaries SET parent_conversation_id = '' WHERE conversation_id = ?",
            (D,),
        )
        ag.conns[0].commit()
        summaries = SummaryIndex().load(ag.gemini)

        # A standalone capture of D holding only 1 of the 2 messages the parent
        # stored for it is not proof the child is held elsewhere.
        d_sfs = sfs_session_id_for(D)
        d_dir = store.allocate_session_dir(d_sfs)
        manifest = {"source": {"original_session_id": D}, "stats": {"message_count": 1}}
        (d_dir / "manifest.json").write_text(json.dumps(manifest))
        with open(d_dir / "messages.jsonl", "w") as f:
            f.write(json.dumps({"role": "assistant",
                                "content": [{"type": "text", "text": "one"}]}) + "\n")
        assert w._held_elsewhere(D, summaries, 2) is False

        # Once the new home stores at least as many messages, the child is
        # released from its old parent.
        manifest["stats"]["message_count"] = 2
        (d_dir / "manifest.json").write_text(json.dumps(manifest))
        with open(d_dir / "messages.jsonl", "a") as f:
            f.write(json.dumps({"role": "assistant",
                                "content": [{"type": "text", "text": "two"}]}) + "\n")
        assert w._held_elsewhere(D, summaries, 2) is True

    def test_richer_standalone_is_not_retired_into_a_poorer_parent(self, ag: AgTree, store):
        """Retiring a standalone capture requires the new parent capture to
        retain at least as much of the child — a parent holding only a
        compressed sidechain must not delete the richer standalone capture."""
        desk = ag.conns[0]
        desk.execute(
            "UPDATE conversation_summaries SET parent_conversation_id = '' WHERE conversation_id = ?",
            (D,),
        )
        desk.commit()
        w = _watcher(ag, store)
        w.full_scan()
        child_sfs = store.get_tracked_session(D).sfs_session_id
        # The standalone capture was made from a 10-message transcript; the
        # sidechain the parent can fold in now has compressed to 2.
        child_dir = store.get_session_dir(child_sfs)
        rich = json.loads((child_dir / "manifest.json").read_text())
        rich["stats"]["message_count"] = 10
        (child_dir / "manifest.json").write_text(json.dumps(rich))
        # D becomes a subagent of A again; A's capture holds only 2 messages.
        desk.execute(
            "UPDATE conversation_summaries SET parent_conversation_id = ? WHERE conversation_id = ?",
            (A, D),
        )
        desk.commit()
        _db_event(w, ag)
        assert any(m.get("agent_id") == D for m in _messages(store, A))  # folded, but poorer
        assert store.get_session_dir(child_sfs) is not None  # retirement deferred
        # Once the parent's capture retains at least as much, it retires.
        parent_dir = store.get_session_dir(sfs_session_id_for(A))
        parent = json.loads((parent_dir / "manifest.json").read_text())
        for ref in parent.get("sub_agents") or []:
            if ref.get("agent_id") == D:
                ref["message_count"] = 10
        (parent_dir / "manifest.json").write_text(json.dumps(parent))
        w._reconcile_subagents(w._last_summaries)
        assert store.get_session_dir(child_sfs) is None

    def test_summaries_db_removal_marks_summaries_changed(self, ag: AgTree, store):
        """Deleting or moving away the summaries DB (or its WAL) must reload
        the summaries, symmetrically with creations and modifications."""
        from sessionfs.watchers.antigravity import _AntigravityEventHandler

        w = _watcher(ag, store)
        handler = _AntigravityEventHandler(
            w._event_queue, w._event_lock, w._rescan, w._summaries_changed
        )

        class _Ev:
            is_directory = False

            def __init__(self, src: Path, dest: Path | None = None):
                self.src_path = str(src)
                if dest is not None:
                    self.dest_path = str(dest)

        db = ag.gemini / "antigravity" / "conversation_summaries.db"
        wal = ag.gemini / "antigravity" / "conversation_summaries.db-wal"
        for path in (db, wal):
            w._summaries_changed.clear()
            handler.on_deleted(_Ev(path))  # type: ignore[arg-type]
            assert w._summaries_changed.is_set(), f"deleted {path.name}"
            w._summaries_changed.clear()
            handler.on_moved(_Ev(path, path.with_name(path.name + ".bak")))  # type: ignore[arg-type]
            assert w._summaries_changed.is_set(), f"moved-away {path.name}"
            assert w._event_queue == []

    def test_summaries_event_mid_pass_survives_and_is_applied(self, ag: AgTree, store, monkeypatch):
        """A summaries-DB event that lands while a pass is already running
        must not be swallowed: the flag survives the pass, the pre-pass rows
        are kept for the next diff, and the next pass applies the change."""
        w = _watcher(ag, store)
        w.full_scan()
        ag.conns[1].execute(
            "UPDATE conversation_summaries SET title = 'Mid-pass Title' WHERE conversation_id = ?",
            (B,),
        )
        ag.conns[1].commit()

        class _RacyEvent(threading.Event):
            armed = True

            def is_set(self) -> bool:
                if self.armed:
                    self.armed = False
                    # A summaries-DB event lands mid-consume, right after the
                    # flag was read — exactly like the handler does it.
                    with w._event_lock:
                        w._summaries_changed.set()
                return super().is_set()

        monkeypatch.setattr(w, "_skipped_dirty", _RacyEvent())
        # Only a transcript event: the summaries flag is down when the pass starts.
        _file_event(w, _append_step(ag, "antigravity", A, 30, "keep talking"))
        assert w._summaries_changed.is_set()  # not swallowed by the pass
        w._last_event_time = 0.0
        w.process_events()
        assert _manifest(store, B)["title"] == "Mid-pass Title"


class TestRound13Findings:
    """Fixes for review round 13: owner-capture waivers, pruning, and
    unreadable-transcript health."""

    def test_held_elsewhere_only_waives_on_hard_local_deletion(self, ag: AgTree, store, monkeypatch):
        from sessionfs.store.deleted import mark_deleted

        w = _watcher(ag, store)
        summaries = SummaryIndex().load(ag.gemini)
        owner_sfs = sfs_session_id_for(A)
        monkeypatch.setattr(w, "_stored_manifest", lambda sfs: None)

        # No tombstone and no stored owner manifest: not proven held elsewhere.
        assert w._held_elsewhere(D, summaries) is False
        # A cloud-only deletion keeps the local copy, so it does not waive.
        mark_deleted(owner_sfs, "cloud", base_dir=store.store_dir)
        assert w._held_elsewhere(D, summaries) is False
        # A transient auto-exclusion is not a user deletion.
        mark_deleted(owner_sfs, "local", base_dir=store.store_dir, reason="too_large")
        assert w._held_elsewhere(D, summaries) is False
        # A hard local deletion waives owner capture.
        mark_deleted(owner_sfs, "everywhere", base_dir=store.store_dir)
        assert w._held_elsewhere(D, summaries) is True

    def test_pruned_session_is_not_recaptured_until_source_changes(self, ag: AgTree, store):
        from sessionfs.store.pruner import SessionPruner

        w = _watcher(ag, store)
        w.full_scan()
        ref = store.get_tracked_session(A)
        assert ref is not None and ref.sfs_session_id is not None
        sfs_id = ref.sfs_session_id
        SessionPruner(store.sessions_dir, store.index.conn)._delete_session(sfs_id)

        # The tracking row survives pruning with its fingerprint, no sfs link.
        pruned = store.get_tracked_session(A)
        assert pruned is not None and pruned.sfs_session_id is None

        # A fresh scan does not re-capture the unchanged source.
        w.full_scan()
        after = store.get_tracked_session(A)
        assert after is not None and after.sfs_session_id is None
        assert store.index.get_session(sfs_id) is None

        # A genuine source change recaptures it.
        _append_step(ag, "antigravity", A, 21, "work after prune")
        w.full_scan()
        recaptured = store.get_tracked_session(A)
        assert recaptured is not None and recaptured.sfs_session_id is not None

    def test_unreadable_transcript_degrades_after_stable_reads(self, ag: AgTree, store, monkeypatch):
        import sessionfs.converters.antigravity_to_sfs as conv
        from sessionfs.converters.antigravity_to_sfs import AntigravityParsedSession
        from sessionfs.watchers.antigravity import CaptureOutcome

        w = _watcher(ag, store)
        summaries = SummaryIndex().load(ag.gemini)

        def unreadable(conv_dir, *a, **k):
            return AntigravityParsedSession(
                session_id=Path(conv_dir).name,
                parse_errors=["transcript_full.jsonl: invalid JSON"],
            )

        monkeypatch.setattr(conv, "parse_antigravity_conversation", unreadable)
        conv_a = ag.conv("antigravity", A)
        for _ in range(4):  # the same fingerprint read again and again
            assert w._capture_session(A, conv_a, 1.0, 1, summaries) is CaptureOutcome.DEFERRED
        assert w.get_status().health == "degraded"

    def test_incomplete_transcript_stays_healthy(self, ag: AgTree, store, monkeypatch):
        import sessionfs.converters.antigravity_to_sfs as conv
        from sessionfs.converters.antigravity_to_sfs import AntigravityParsedSession
        from sessionfs.watchers.antigravity import CaptureOutcome

        w = _watcher(ag, store)
        summaries = SummaryIndex().load(ag.gemini)

        def still_writing(conv_dir, *a, **k):
            return AntigravityParsedSession(
                session_id=Path(conv_dir).name,
                parse_errors=["transcript_full.jsonl: incomplete final line"],
            )

        monkeypatch.setattr(conv, "parse_antigravity_conversation", still_writing)
        conv_a = ag.conv("antigravity", A)
        for _ in range(5):
            assert w._capture_session(A, conv_a, 1.0, 1, summaries) is CaptureOutcome.DEFERRED
        assert w.get_status().health == "healthy"


class TestRound14:
    """Fixes for review round 14: a deleted summaries DB must not become a
    trusted empty snapshot, and valid-JSON non-object steps must surface as
    parse errors instead of being silently dropped."""

    def test_deleted_summaries_db_falls_back_to_cached_rows(self, tmp_path: Path):
        gemini = tmp_path / "home" / ".gemini"
        root = gemini / "antigravity"
        root.mkdir(parents=True)
        conn = _write_db(root / "conversation_summaries.db", [
            {"conversation_id": A, "title": "Kept Title", "app_data_dir": "antigravity"},
        ], wal=False)
        conn.close()

        idx = SummaryIndex()
        _rows, status = idx.load_with_status(gemini)
        assert SummaryIndex.is_current(status)

        # Antigravity deletes the DB (migration, corruption recovery, reset).
        (root / "conversation_summaries.db").unlink()
        (root / "conversation_summaries.db-wal").unlink(missing_ok=True)
        (root / "conversation_summaries.db-shm").unlink(missing_ok=True)

        rows, status = idx.load_with_status(gemini)
        assert status[root / "conversation_summaries.db"] == SummaryIndex.FALLBACK
        assert not SummaryIndex.is_current(status)
        # The last good row survives instead of being silently dropped.
        assert rows[("antigravity", A)]["title"] == "Kept Title"

    def test_present_but_empty_summaries_db_stays_fresh(self, tmp_path: Path):
        gemini = tmp_path / "home" / ".gemini"
        root = gemini / "antigravity"
        root.mkdir(parents=True)
        conn = _write_db(root / "conversation_summaries.db", [
            {"conversation_id": A, "title": "Old", "app_data_dir": "antigravity"},
        ], wal=False)
        conn.close()

        idx = SummaryIndex()
        rows, status = idx.load_with_status(gemini)
        assert rows and SummaryIndex.is_current(status)

        # A reset that recreates an empty DB *in place* is a real empty
        # snapshot, not a failure: it must stay FRESH (and be cached empty).
        (root / "conversation_summaries.db").unlink()
        conn = _write_db(root / "conversation_summaries.db", [], wal=False)
        conn.close()
        rows, status = idx.load_with_status(gemini, force=True)
        assert status[root / "conversation_summaries.db"] == SummaryIndex.FRESH
        assert SummaryIndex.is_current(status)
        assert rows == {}

    def test_non_object_records_are_parse_errors(self, tmp_path: Path):
        conv = _write_conv(tmp_path, [])
        log = conv / ".system_generated" / "logs" / "transcript_full.jsonl"
        log.write_text('"just a string"\n[1, 2, 3]\n')
        loaded = load_steps(conv)
        assert loaded.steps == []
        assert loaded.parse_errors == [
            "transcript_full.jsonl: non-object record skipped",
            "transcript_full.jsonl: non-object record skipped",
        ]

    def test_only_non_object_steps_degrade_health(self, ag: AgTree, store):
        from sessionfs.watchers.antigravity import CaptureOutcome

        cid = _uuid(99)
        conv_dir = _new_conv(ag, cid, with_transcript=False)
        logs = conv_dir / ".system_generated" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "transcript_full.jsonl").write_text('"just a string"\n[1, 2, 3]\n')

        w = _watcher(ag, store)
        summaries = SummaryIndex().load(ag.gemini)
        for _ in range(4):
            assert w._capture_session(cid, conv_dir, 1.0, 1, summaries) is CaptureOutcome.DEFERRED
        assert w.get_status().health == "degraded"


class TestRound15:
    """A cold SummaryIndex must not treat an absent summaries DB that still
    has capturable conversations on disk as a trusted empty snapshot."""

    def test_absent_db_with_transcripts_is_unavailable_when_cold(self, tmp_path: Path):
        gemini = tmp_path / "home" / ".gemini"
        root = gemini / "antigravity"
        logs = root / "brain" / A / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        (logs / "transcript_full.jsonl").write_text(
            json.dumps({"step_index": 0, "source": "MODEL", "type": "PLANNER_RESPONSE"}) + "\n"
        )
        # No conversation_summaries.db at all, so a fresh SummaryIndex has no cache.

        idx = SummaryIndex()
        rows, status = idx.load_with_status(gemini)
        assert status[root / "conversation_summaries.db"] == SummaryIndex.UNAVAILABLE
        assert not SummaryIndex.is_current(status)
        assert rows == {}

    def test_absent_db_without_transcripts_stays_fresh(self, tmp_path: Path):
        gemini = tmp_path / "home" / ".gemini"
        root = gemini / "antigravity"
        root.mkdir(parents=True)
        # No DB and no brain/<uuid> transcripts: legitimately empty.

        idx = SummaryIndex()
        rows, status = idx.load_with_status(gemini)
        assert status[root / "conversation_summaries.db"] == SummaryIndex.FRESH
        assert SummaryIndex.is_current(status)
        assert rows == {}

    def test_transcript_appearing_after_empty_absent_db_becomes_unavailable(self, tmp_path: Path):
        gemini = tmp_path / "home" / ".gemini"
        root = gemini / "antigravity"
        root.mkdir(parents=True)

        idx = SummaryIndex()
        _rows, status = idx.load_with_status(gemini)
        assert status[root / "conversation_summaries.db"] == SummaryIndex.FRESH
        assert SummaryIndex.is_current(status)

        # Antigravity writes a transcript before it (re)creates the summaries
        # DB. The absent-DB fingerprint is unchanged, so the empty snapshot
        # must NOT be served from cache as CACHED — the root now has a
        # capturable conversation and the watcher has to defer until the DB
        # carries its title/parent links.
        logs = root / "brain" / A / ".system_generated" / "logs"
        logs.mkdir(parents=True)
        (logs / "transcript_full.jsonl").write_text(
            json.dumps({"step_index": 0, "source": "MODEL", "type": "PLANNER_RESPONSE"}) + "\n"
        )

        rows, status = idx.load_with_status(gemini)
        assert status[root / "conversation_summaries.db"] == SummaryIndex.UNAVAILABLE
        assert not SummaryIndex.is_current(status)
        assert rows == {}


class TestRound22Findings:
    """Fixes for review round 22: carry a stored sidechain forward when a
    still-linked subagent parses to zero messages, and tolerate non-numeric
    message_count fields in a hand-edited stored manifest."""

    def test_empty_linked_child_carries_forward_instead_of_deferring(self, ag: AgTree, store):
        w = _watcher(ag, store)
        w.full_scan()
        before = _messages(store, A)
        before_d = [m for m in before if m.get("agent_id") == D]
        assert before_d
        # D's transcript becomes empty (0 bytes) while its summary row keeps
        # the parent link to A: it parses to zero messages and lands in
        # pending_subagents instead of a 0-message sidechain.
        d_log = ag.conv("antigravity", D) / ".system_generated" / "logs" / "transcript_full.jsonl"
        d_log.write_text("")
        log = ag.conv("antigravity", A) / ".system_generated" / "logs" / "transcript_full.jsonl"
        with open(log, "a") as f:
            f.write("\n")  # close the half-written final line first
        _file_event(w, _append_step(ag, "antigravity", A, 21, "parent keeps going"))
        after = _messages(store, A)
        # The stored sidechain is carried forward byte-for-byte and the parent
        # keeps capturing its own thread instead of deferring forever.
        assert [m for m in after if m.get("agent_id") == D] == before_d
        assert any("parent keeps going" in json.dumps(m) for m in after)

    def test_first_time_pending_child_still_defers(self, ag: AgTree, store):
        # A linked child with NO stored sidechain is genuinely in flight and
        # must still defer the parent, not drop the child.
        w = _watcher(ag, store)
        w.full_scan()
        e = _uuid(1200)
        _new_conv(ag, e, parent=A, with_transcript=False)
        _db_event(w, ag)
        assert A in w._retry
        assert all(s["agent_id"] != e for s in _manifest(store, A).get("sub_agents", []))

    def test_non_numeric_message_count_degrades_to_zero(self):
        from sessionfs.watchers.antigravity import AntigravityWatcher, _count

        assert _count(3) == 3
        assert _count(0) == 0
        assert _count(True) == 0  # bool is an int subclass but not a count
        assert _count(False) == 0
        assert _count("n/a") == 0
        assert _count({"a": 1}) == 0
        assert _count([1, 2]) == 0
        assert _count(3.5) == 0
        assert _count(None) == 0
        # A non-int stored total/sidechain count is UNKNOWN, never 0: the
        # caller derives the true count from stored messages or refuses.
        assert AntigravityWatcher._thread_counts({
            "stats": {"message_count": "n/a"},
            "sub_agents": [{"agent_id": "x", "message_count": "n/a"}],
        }) == (None, {"x": None})

    def test_hand_edited_message_count_does_not_fail_capture(self, ag: AgTree, store):
        w = _watcher(ag, store)
        w.full_scan()
        assert w.get_status().health == "healthy"
        a_sfs = store.get_tracked_session(A).sfs_session_id
        manifest_path = store.get_session_dir(a_sfs) / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        # A hand-edited manifest with truthy non-numeric counts must not make
        # _shrink_verdict raise: it degrades to zero and the recapture repairs
        # the manifest instead of failing forever.
        manifest["stats"]["message_count"] = "n/a"
        for ref in manifest["sub_agents"]:
            ref["message_count"] = {"bogus": True}
        manifest_path.write_text(json.dumps(manifest))
        _file_event(w, _append_step(ag, "antigravity", A, 21, "parent keeps going"))
        assert w.get_status().health == "healthy"
        repaired = json.loads(manifest_path.read_text())
        assert isinstance(repaired["stats"]["message_count"], int)
        assert all(isinstance(r["message_count"], int) for r in repaired["sub_agents"])


class TestRound25Findings:
    """Fixes for review round 25: a missing/non-int stored total is UNKNOWN,
    never 0, and a permanently-unreadable session is a sticky per-session
    latch that other sessions' successes do not clear."""

    def test_corrupt_stored_total_never_shrinks(self, ag: AgTree, store):
        """A stored total that is missing/corrupt must not read as 0.

        Degrading it to 0 turns off the compression guard and the retire
        floor, so a shorter recapture truncates the stored messages (Finding
        1, lossy recapture)."""
        from types import SimpleNamespace

        from sessionfs.watchers.antigravity import CaptureOutcome

        w = _watcher(ag, store)
        w.full_scan()
        summaries = SummaryIndex().load(ag.gemini)

        a_sfs = store.get_tracked_session(A).sfs_session_id
        manifest_path = store.get_session_dir(a_sfs) / "manifest.json"
        stored = json.loads(manifest_path.read_text())
        assert stored["stats"]["message_count"] > 1
        stored["stats"]["message_count"] = "n/a"
        manifest_path.write_text(json.dumps(stored))

        # A fresh parse where the main thread shrank (D is still linked and
        # the same size, so nothing is carried or dropped — the shrink is A's
        # own thread). The unknown total is derived from the stored messages,
        # so the compression guard keeps the richer stored capture.
        shrunk = SimpleNamespace(
            message_count=5,
            sub_agents=[{"agent_id": D, "message_count": 2}],
        )
        verdict = w._shrink_verdict(A, shrunk, json.loads(manifest_path.read_text()), summaries)
        assert verdict is CaptureOutcome.UNCHANGED

    def test_corrupt_standalone_total_never_retires(self, ag: AgTree, store):
        """A standalone capture whose stored total is corrupt must not retire
        into a poorer parent (Finding 1, lossy retire)."""
        w = _watcher(ag, store)
        w.full_scan()

        # A standalone capture of D whose manifest total is corrupt but whose
        # messages.jsonl proves it holds 10 messages.
        d_sfs = sfs_session_id_for(D)
        d_dir = store.allocate_session_dir(d_sfs)
        standalone = {"source": {"original_session_id": D}, "stats": {"message_count": "n/a"}}
        (d_dir / "manifest.json").write_text(json.dumps(standalone))
        with open(d_dir / "messages.jsonl", "w") as f:
            for _ in range(10):
                f.write(json.dumps({"role": "assistant",
                                    "content": [{"type": "text", "text": "child"}]}) + "\n")

        # The parent's stored manifest holds only 2 messages for D. A corrupt
        # total read as 0 would make that look rich enough to retire the
        # 10-message standalone; the unknown total is derived from the stored
        # messages instead, so the retire is refused.
        owner = _manifest(store, A)
        assert any(r.get("agent_id") == D for r in owner.get("sub_agents") or [])
        assert w._owner_covers(standalone, owner, D) is False

    def test_unreadable_session_survives_successes_elsewhere(self, ag: AgTree, store):
        """A stuck unreadable session keeps the watcher degraded even while
        other sessions capture cleanly; only that session healing recovers it
        (Finding 2: interleaved-success monitoring gap)."""
        from sessionfs.watchers.antigravity import CaptureOutcome

        d_log = ag.conv("antigravity", D) / ".system_generated" / "logs" / "transcript_full.jsonl"
        original = d_log.read_text()
        d_log.write_text("this is not valid json\n")

        w = _watcher(ag, store)
        for _ in range(5):
            w.capture_now(A)
            if w.get_status().health == "degraded":
                break
        assert w.get_status().health == "degraded"
        assert "55555555" in (w.get_status().last_error or "")

        # A clean capture of an unrelated session must not clear the latch.
        assert w.capture_now(B) is CaptureOutcome.STORED
        assert w.get_status().health == "degraded"

        # Healing the unreadable session releases the latch and recovers.
        d_log.write_text(original)
        assert w.capture_now(A) is CaptureOutcome.STORED
        assert w.get_status().health == "healthy"


class TestStuckPendingChild:
    """A linked subagent that stays pending (torn final line, no transcript)
    must not block its stored parent forever; health degrades while it is
    stuck, and nothing stored is lost."""

    @staticmethod
    def _close_parent_and_append(ag: AgTree, text: str) -> None:
        log = ag.conv("antigravity", A) / ".system_generated" / "logs" / "transcript_full.jsonl"
        with open(log, "a") as f:
            f.write("\n")  # close the fixture's half-written final line
        for i in range(4):
            _append_step(ag, "antigravity", A, 21 + i, f"{text} {i}")

    def test_torn_final_line_child_stops_blocking_parent(self, ag: AgTree, store, monkeypatch):
        import sessionfs.watchers.antigravity as agw
        from sessionfs.watchers.antigravity import CaptureOutcome

        w = _watcher(ag, store)
        w.full_scan()
        before_d = [m for m in _messages(store, A) if m.get("agent_id") == D]

        e = _uuid(1300)
        conv = _new_conv(ag, e, parent=A, text="child partial work")
        e_log = conv / ".system_generated" / "logs" / "transcript_full.jsonl"
        with open(e_log, "a") as f:
            f.write('{"step_index": 2, "source": "MODEL", "type": "PLANNER_RES')  # torn
        self._close_parent_and_append(ag, "parent step")

        # Within the settle window the parent waits on the child, but the
        # child's stable fingerprint degrades health instead of a silent defer.
        for _ in range(3):
            assert w.capture_now(A) is CaptureOutcome.DEFERRED
        assert w.get_status().health == "degraded"
        assert e[:8] in w.last_defer_reason

        # Past the settle window the parent is installed with the child's
        # readable messages; health stays degraded while the child is stuck.
        monkeypatch.setattr(agw, "PENDING_CHILD_SETTLE_S", 0.0)
        assert w.capture_now(A) is CaptureOutcome.STORED
        after = _messages(store, A)
        assert sum("parent step" in json.dumps(m) for m in after) == 4
        assert [m for m in after if m.get("agent_id") == D] == before_d
        assert sum(1 for m in after if m.get("agent_id") == e) == 2
        assert {r["agent_id"]: r["message_count"] for r in _manifest(store, A)["sub_agents"]}[e] == 2
        assert w.get_status().health == "degraded"
        assert w._capture_health.has_unreadable_session(e)

        # The child finishes its line: it grows in and health recovers.
        with open(e_log, "a") as f:
            f.write('PONSE", "created_at": "2026-10-18T06:00:02Z", "content": "child done"}\n')
        assert w.capture_now(A) is CaptureOutcome.STORED
        assert sum(1 for m in _messages(store, A) if m.get("agent_id") == e) == 3
        assert w.get_status().health == "healthy"

    def test_linked_child_without_transcript_stops_blocking_parent(self, ag: AgTree, store, monkeypatch):
        import sessionfs.watchers.antigravity as agw
        from sessionfs.watchers.antigravity import CaptureOutcome

        w = _watcher(ag, store)
        w.full_scan()
        before = _messages(store, A)
        e = _uuid(1301)
        _new_conv(ag, e, parent=A, with_transcript=False)  # row linked, nothing readable
        self._close_parent_and_append(ag, "parent goes on")

        assert w.capture_now(A) is CaptureOutcome.DEFERRED
        assert w.capture_now(A) is CaptureOutcome.DEFERRED  # still inside the window
        monkeypatch.setattr(agw, "PENDING_CHILD_SETTLE_S", 0.0)
        assert w.capture_now(A) is CaptureOutcome.STORED
        after = _messages(store, A)
        assert all(m in after for m in before)  # nothing stored is lost
        assert sum("parent goes on" in json.dumps(m) for m in after) == 4
        assert all(r["agent_id"] != e for r in _manifest(store, A).get("sub_agents", []))

        # The child's transcript appears later: it grows in normally.
        logs = ag.conv("antigravity", e) / ".system_generated" / "logs"
        (logs / "transcript_full.jsonl").write_text(json.dumps(
            {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT",
             "created_at": "2026-10-18T06:00:00Z", "content": "late child task"}) + "\n")
        assert w.capture_now(A) is CaptureOutcome.STORED
        assert any(r["agent_id"] == e for r in _manifest(store, A)["sub_agents"])

    def test_actively_written_child_is_still_waited_on(self, ag: AgTree, store, monkeypatch):
        import sessionfs.watchers.antigravity as agw
        from sessionfs.watchers.antigravity import CaptureOutcome

        monkeypatch.setattr(agw, "PENDING_CHILD_SETTLE_S", 0.0)
        w = _watcher(ag, store)
        w.full_scan()
        e = _uuid(1302)
        conv = _new_conv(ag, e, parent=A)
        e_log = conv / ".system_generated" / "logs" / "transcript_full.jsonl"
        for i in range(3):
            with open(e_log, "a") as f:
                f.write("x" * (i + 1))  # grows each read: torn line still being written
            assert w.capture_now(A) is CaptureOutcome.DEFERRED


    def _settle_torn_child(self, ag: AgTree, store, monkeypatch) -> tuple[Any, str]:
        import sessionfs.watchers.antigravity as agw
        from sessionfs.watchers.antigravity import CaptureOutcome

        monkeypatch.setattr(agw, "PENDING_CHILD_SETTLE_S", 0.0)
        w = _watcher(ag, store)
        w.full_scan()
        e = _uuid(1303)
        conv = _new_conv(ag, e, parent=A)
        with open(conv / ".system_generated" / "logs" / "transcript_full.jsonl", "a") as f:
            f.write('{"step_index": 2, "source": "MODEL"')  # torn
        self._close_parent_and_append(ag, "parent step")
        assert w.capture_now(A) is CaptureOutcome.DEFERRED
        assert w.capture_now(A) is CaptureOutcome.STORED  # settled
        assert w.get_status().health == "degraded"
        assert w._pending_unreadable == {A: {e}}
        return w, e

    def test_deleted_settled_parent_releases_child_latch(self, ag: AgTree, store, monkeypatch):
        """A settle-installed parent leaves the retry queue; deleting the
        conversation afterwards must still release its stuck child's latch."""
        import shutil

        w, e = self._settle_torn_child(ag, store, monkeypatch)
        assert A not in w._retry
        for cid in (A, D, e):
            shutil.rmtree(ag.conv("antigravity", cid))
        conn = ag.conns[0]
        conn.execute(
            "DELETE FROM conversation_summaries WHERE conversation_id IN (?, ?, ?)", (A, D, e),
        )
        conn.commit()
        _db_event(w, ag)
        assert w._pending_unreadable == {}
        assert e not in w._pending_seen
        assert not w._capture_health.has_unreadable_session(e)
        assert w.get_status().health == "healthy"

    def test_deleted_parent_released_by_full_scan(self, ag: AgTree, store, monkeypatch):
        import shutil

        w, e = self._settle_torn_child(ag, store, monkeypatch)
        for cid in (A, D, e):
            shutil.rmtree(ag.conv("antigravity", cid))
        conn = ag.conns[0]
        conn.execute(
            "DELETE FROM conversation_summaries WHERE conversation_id IN (?, ?, ?)", (A, D, e),
        )
        conn.commit()
        w._summaries_changed.set()
        w._scan_pending = True  # force a fresh summaries read
        w.full_scan()
        assert w._pending_unreadable == {}
        assert w._pending_seen == {}
        assert w.get_status().health == "healthy"

    def test_excluded_parent_does_not_latch_its_children(self, ag: AgTree, store, monkeypatch):
        import sessionfs.store.deleted as deleted
        from sessionfs.watchers.antigravity import CaptureOutcome

        sfs_a = "ses_" + A.replace("-", "")[:16]
        excluded = {"on": False}
        real = deleted.is_excluded
        monkeypatch.setattr(
            deleted, "is_excluded",
            lambda sid, base_dir=None: (excluded["on"] and sid == sfs_a) or real(sid, base_dir=base_dir),
        )
        # A stuck child latched while the parent was still live...
        w, e = self._settle_torn_child(ag, store, monkeypatch)
        # ...then the user deletes the parent: the next capture is EXCLUDED and
        # owns no latch, and no deferral re-latches the child.
        excluded["on"] = True
        for _ in range(3):
            assert w.capture_now(A) is CaptureOutcome.EXCLUDED
        assert A not in w._pending_unreadable
        assert e not in w._pending_seen
        assert not w._capture_health.has_unreadable_session(e)
        assert w.get_status().health == "healthy"

    def test_excluded_parent_never_latches_a_new_stuck_child(self, ag: AgTree, store, monkeypatch):
        import sessionfs.store.deleted as deleted
        from sessionfs.watchers.antigravity import CaptureOutcome

        sfs_a = "ses_" + A.replace("-", "")[:16]
        w = _watcher(ag, store)
        w.full_scan()
        real = deleted.is_excluded
        monkeypatch.setattr(
            deleted, "is_excluded",
            lambda sid, base_dir=None: sid == sfs_a or real(sid, base_dir=base_dir),
        )
        e = _uuid(1304)
        _new_conv(ag, e, parent=A, with_transcript=False)
        (ag.conv("antigravity", e) / ".system_generated" / "logs" / "transcript_full.jsonl").write_text(
            "this is not valid json\n"
        )
        for _ in range(4):
            assert w.capture_now(A) is CaptureOutcome.EXCLUDED
        assert w._pending_unreadable == {}
        assert e not in w._unreadable and e not in w._pending_seen
        assert w.get_status().health == "healthy"

    def test_pending_seen_pruned_when_child_unlinked(self, ag: AgTree, store, monkeypatch):
        w = _watcher(ag, store)
        w.full_scan()
        summaries = SummaryIndex().load(ag.gemini)
        w._pending_seen["deadbeef-0000-4000-8000-000000000000"] = (None, 5, 0.0)  # no row
        w._pending_seen[B] = (None, 5, 0.0)  # top-level, not a subagent
        w._pending_seen[D] = (None, 1, 0.0)  # still a linked subagent of A
        w._sweep_pending(summaries)
        assert set(w._pending_seen) == {D}


class TestNonFiniteNumbers:
    def test_nan_and_infinite_token_counts_do_not_crash(self, tmp_path: Path):
        conv = _write_conv(tmp_path, [_step(0, "PLANNER_RESPONSE", content="hello")])
        log = conv / ".system_generated" / "logs" / "transcript_full.jsonl"
        with open(log, "a") as f:
            for i, value in enumerate(("NaN", "Infinity", "-Infinity", "1e999")):
                f.write(
                    '{"step_index": %d, "source": "MODEL", "type": "PLANNER_RESPONSE", '
                    '"created_at": "2026-10-18T05:00:0%dZ", "content": "step %d", '
                    '"input_tokens": %s, "output_tokens": %s, "cache_read_tokens": 3.0}\n'
                    % (i + 1, i + 1, i + 1, value, value)
                )
        session = parse_antigravity_conversation(conv, {})
        assert session.message_count == 5
        assert session.total_input_tokens == 0 and session.total_output_tokens == 0

    def test_huge_step_index_and_edge_timestamps_do_not_crash(self, tmp_path: Path):
        conv = _write_conv(tmp_path, [
            _step(0, "PLANNER_RESPONSE", content="first"),
            {"step_index": 10 ** 400, "source": "MODEL", "type": "PLANNER_RESPONSE",
             "created_at": "0001-01-01T00:00:00+05:00", "content": "huge index"},
            {"source": "MODEL", "type": "PLANNER_RESPONSE",
             "created_at": "9999-12-31T23:59:59-05:00", "content": "unindexed after"},
        ])
        steps = load_steps(conv).steps
        assert [s.get("content") for s in steps] == ["first", "huge index", "unindexed after"]
        session = parse_antigravity_conversation(conv, {})
        assert session.message_count == 3
